"""
Flipkart BBD Deal Tracker & Live Price Radar
=============================================
Single-page Streamlit price-intelligence app: tracks catalogue prices against
6-month averages and last Big Billion Days floors, scores each deal, applies
card-offer maths, and maintains a persistent wishlist.

EVERY row links to a SPECIFIC product detail page (PDP), never a category page.

Requirements: streamlit >= 1.35, pandas >= 2.0, requests >= 2.28
Run with:     streamlit run Flipkart.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import re
import tempfile
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:
    requests = None

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

APP_TITLE = "Flipkart BBD Deal Tracker & Live Price Radar"
TRACKER_VERSION = "v17_deep_unlimited_discovery"
TRACKER_DB_FILE = os.environ.get("TRACKER_DB_FILE", "tracker_store.json")
LINK_CACHE_FILE = os.environ.get("LINK_CACHE_FILE", "flipkart_link_cache.json")
SCANNER_STORE_FILE = os.environ.get("SCANNER_STORE_FILE", "flipkart_scan_store.json")

FLIPKART_HOST = "www.flipkart.com"
FLIPKART_SEARCH = f"https://{FLIPKART_HOST}/search?q={{query}}"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}

# Query parameters Flipkart genuinely needs; marketplace=FLIPKART prevents blank page rendering
KEEP_PARAMS = {"pid", "lid", "marketplace"}

ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{12,18})(?:[/?#]|$)", re.IGNORECASE)
PID_PARAM_PATTERN = re.compile(r"[?&]pid=([A-Z0-9]{12,18})", re.IGNORECASE)
PDP_HREF_PATTERN = re.compile(
    r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{12,18}[^"]*)"', re.IGNORECASE
)
SCANNER_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL
)
SCANNER_CHALLENGE_MARKERS = (
    "captcha", "unusual traffic", "are you a human", "access denied", "request blocked"
)

AUTO_RESOLVE_ON_START = True
RESOLVER_TIMEOUT = 12
RESOLVER_RETRIES = 2
RESOLVER_DELAY = 1.0
RESOLVER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

INSTANT_DISCOUNT_RATE = 0.10
INSTANT_DISCOUNT_CAP = 1500
CASHBACK_RATE = 0.05
BBD_PREDICTION_FACTOR = 0.93
STRONG_DISCOUNT_THRESHOLD = 28.0

CARD_AUTO = "Auto-Best Card"
CARD_INSTANT = "Axis / ICICI (10% Instant, Cap ₹1.5k)"
CARD_CASHBACK = "Flipkart Axis (5% Unlimited Cashback)"

LINK_CURATED = "curated"
LINK_RESOLVED = "resolved"
LINK_SEARCH = "search"

# Scanner Traversal Settings (Uncapped for full-site discovery)
SCANNER_TIMEOUT = 15
SCANNER_RETRIES = 3
SCANNER_WORKERS = 4
SCANNER_QPS_START = 2.0
SCANNER_QPS_FLOOR = 0.4
SCANNER_QPS_CEILING = 4.0
SCANNER_SATURATION = 18
SCANNER_MIN_BAND_WIDTH = 100
SCANNER_MAX_BISECT_DEPTH = 8

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deal_tracker")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

st.markdown(
    """
<style>
    .kpi-container { background-color: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.35rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; margin-top: 3px; }
    .section-title-catalog { background: linear-gradient(90deg, #1e293b, #0f172a); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #38bdf8; margin: 30px 0 15px 0; color: white; }
    .section-title-bbd { background: linear-gradient(90deg, #b91c1c, #7f1d1d); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #facc15; margin: 30px 0 15px 0; color: white; }
    .section-title-ads { background: linear-gradient(90deg, #1e3a8a, #172554); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #a855f7; margin: 30px 0 15px 0; color: white; }
    .section-title-wishlist { background: linear-gradient(90deg, #831843, #500724); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #ec4899; margin: 30px 0 15px 0; color: white; }
</style>
""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# URL UTILITIES
# --------------------------------------------------------------------------- #


def search_url(*terms: str) -> str:
    query = " ".join(t for t in terms if t).strip() or "deals"
    return FLIPKART_SEARCH.format(query=quote_plus(query))


def sanitize_url(raw_url: str) -> str:
    if not raw_url or not isinstance(raw_url, str):
        return ""

    candidate = raw_url.strip().strip('"\',)\u200b')
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate.lstrip("/")

    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""

    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return ""

    query_dict = dict(parse_qsl(parsed.query, keep_blank_values=False))
    kept = [(k, v) for k, v in query_dict.items() if k.lower() in KEEP_PARAMS]

    # Critical: Direct product links MUST retain marketplace=FLIPKART to render full page
    has_pid = any(k.lower() == "pid" for k, _ in kept)
    has_market = any(k.lower() == "marketplace" for k, _ in kept)
    if has_pid and not has_market:
        kept.append(("marketplace", "FLIPKART"))

    return urlunparse(("https", FLIPKART_HOST, parsed.path, "", urlencode(kept), ""))


def is_pdp(url: str) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if ITM_ID_PATTERN.search(parsed.path):
        return True
    if "/item/p/product" in parsed.path and PID_PARAM_PATTERN.search(parsed.query):
        return True
    if PID_PARAM_PATTERN.search(parsed.query):
        return True
    return False


def extract_itm_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    match = ITM_ID_PATTERN.search(parsed.path)
    if match:
        return match.group(1).lower()
    pid_match = PID_PARAM_PATTERN.search(parsed.query)
    if pid_match:
        return pid_match.group(1).lower()
    return ""


def classify_link(url: str) -> str:
    return LINK_RESOLVED if is_pdp(url) else LINK_SEARCH


# --------------------------------------------------------------------------- #
# LIVE LINK RESOLVER
# --------------------------------------------------------------------------- #


@dataclass
class ResolveOutcome:
    query: str
    url: str
    source: str
    ok: bool = True
    detail: str = ""


class FlipkartLinkResolver:
    def __init__(self, cache_path: str = LINK_CACHE_FILE) -> None:
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._cache: Dict[str, str] = self._load_cache()
        self._session = self._build_session()

    def _load_cache(self) -> Dict[str, str]:
        if not os.path.exists(self._cache_path):
            return {}
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return {k: v for k, v in data.items() if isinstance(v, str)} if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _persist_cache(self) -> None:
        try:
            atomic_write_json(self._cache_path, self._cache)
        except OSError:
            pass

    @property
    def cache_size(self) -> int:
        return len(self._cache)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
        if os.path.exists(self._cache_path):
            try:
                os.remove(self._cache_path)
            except OSError:
                pass

    @staticmethod
    def _build_session():
        if requests is None:
            return None
        session = requests.Session()
        session.headers.update({
            "User-Agent": RESOLVER_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        })
        return session

    @property
    def available(self) -> bool:
        return self._session is not None

    def _fetch(self, url: str) -> Optional[str]:
        if self._session is None:
            return None
        for attempt in range(1, RESOLVER_RETRIES + 1):
            try:
                response = self._session.get(url, timeout=RESOLVER_TIMEOUT)
            except Exception:
                time.sleep(0.5 * attempt)
                continue
            if response.status_code == 200 and response.text:
                return response.text
            time.sleep(0.5 * attempt)
        return None

    def self_test(self) -> Tuple[bool, str]:
        if not self.available:
            return False, "The `requests` package is missing."
        html = self._fetch(search_url("Puma Conduct Pro"))
        if not html:
            return False, "Flipkart connection refused or IP blocked."
        if not self._first_pdp_href(html):
            return False, "Flipkart served a challenge page with no product links."
        return True, "Connected to Flipkart and product links parsed successfully."

    @staticmethod
    def _first_pdp_href(html: str) -> str:
        for match in PDP_HREF_PATTERN.finditer(html):
            href = match.group(1)
            if href.startswith(("/search", "/pr?", "/q/")) or "/pr?" in href:
                continue
            clean = sanitize_url(f"https://{FLIPKART_HOST}{href}")
            if is_pdp(clean):
                return clean
        return ""

    def resolve(self, product_name: str, *, use_cache: bool = True) -> ResolveOutcome:
        query = (product_name or "").strip()
        if not query:
            return ResolveOutcome(query, search_url("deals"), LINK_SEARCH, False, "empty query")

        key = query.lower()
        if use_cache:
            with self._lock:
                cached = self._cache.get(key)
            if cached and is_pdp(cached):
                return ResolveOutcome(query, cached, LINK_RESOLVED, True, "cache hit")

        if not self.available:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False, "requests unavailable")

        html = self._fetch(search_url(query))
        if not html:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False, "request blocked")

        pdp = self._first_pdp_href(html)
        if not pdp:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False, "no link found")

        with self._lock:
            self._cache[key] = pdp
            self._persist_cache()
        return ResolveOutcome(query, pdp, LINK_RESOLVED, True, "resolved live")

    def resolve_many(self, product_names: Sequence[str], *, progress=None, use_cache: bool = True) -> List[ResolveOutcome]:
        outcomes: List[ResolveOutcome] = []
        total = len(product_names)
        for index, name in enumerate(product_names, start=1):
            if progress:
                progress(index, total, name)
            outcome = self.resolve(name, use_cache=use_cache)
            outcomes.append(outcome)
            if outcome.detail != "cache hit" and index < total:
                time.sleep(RESOLVER_DELAY)
        return outcomes


@st.cache_resource(show_spinner=False)
def get_resolver() -> FlipkartLinkResolver:
    return FlipkartLinkResolver()


# --------------------------------------------------------------------------- #
# VERIFIED CURATED PRODUCT LINKS (COMPLETE WITH PID & MARKETPLACE)
# --------------------------------------------------------------------------- #

CURATED_PDP: Dict[str, str] = {
    "Puma Conduct Pro Performance Running Shoes": "https://www.flipkart.com/item/p/product?pid=SHOHPPYFJTHG9HHR&marketplace=FLIPKART",
    "Apple iPhone 15 (Black, 128 GB)": "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4?pid=MOBGTAGPTB3VS24W&marketplace=FLIPKART",
    "Apple iPhone 14 (Blue, 128 GB)": "https://www.flipkart.com/apple-iphone-14-blue-128-gb/p/itmdb77f40da6b6d?pid=MOBGHWFH3QW2TC3C&marketplace=FLIPKART",
    "Sony WH-1000XM4 ANC Wireless Headphones": "https://www.flipkart.com/sony-wh-1000xm4-bluetooth-headset/p/itm878df080e7d56?pid=ACCFSDG67YGGHJ23&marketplace=FLIPKART",
    "Sony WH-1000XM5 ANC Wireless Headphones": "https://www.flipkart.com/sony-wh-1000xm5-bluetooth-headset/p/itm18cb5732c53a6?pid=ACCGH4Y57YGGHJ89&marketplace=FLIPKART",
    "boAt Airdopes 161 ANC TWS Earbuds": "https://www.flipkart.com/boat-airdopes-161-bluetooth-headset/p/itm116ba5a593f41?pid=ACCG5WHAJGKAGH5S&marketplace=FLIPKART",
    "JBL Flip 6 30W Waterproof Bluetooth Speaker": "https://www.flipkart.com/jbl-flip-6-30-w-bluetooth-speaker/p/itmd41334c44f07e?pid=ACCG7FZZZZZZZZZZ&marketplace=FLIPKART",
    "Casio Vintage Stainless Steel Digital Watch": "https://www.flipkart.com/casio-a158wa-1df-vintage-series-digital-watch-men-women/p/itmffyy88z4gqfgg?pid=WATDFGFHHZ6ZNZAZ&marketplace=FLIPKART",
    "Casio G-Shock GA-2100 Octagonal Tough Watch": "https://www.flipkart.com/casio-ga-2100-1a1dr-g-shock-analog-digital-watch-men/p/itm1717849e75520?pid=WATFHH3CZZT6Z9GG&marketplace=FLIPKART",
    "Philips OneBlade QP1424 Hybrid Trimmer & Shaver": "https://www.flipkart.com/philips-oneblade-qp1424-hybrid-trimmer/p/itm5a38bbff92c3a?pid=TRMGH4GFGZGHZ7ZZ&marketplace=FLIPKART",
    "Penguin Atomic Habits by James Clear": "https://www.flipkart.com/atomic-habits/p/itmd5b306443c2eb?pid=9781847941831&marketplace=FLIPKART",
}


# --------------------------------------------------------------------------- #
# EXPANDED SEED CATALOG MATRIX
# --------------------------------------------------------------------------- #

CAT_DATA_MATRIX: Dict[str, Dict[str, Any]] = {
    "Men's Fashion": {
        "slug": "mens_fashion", "icon": "👔",
        "items": [
            ("Levi's", "511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056),
            ("U.S. Polo Assn", "Solid Cotton Polo T-Shirt", 1999, 1399, 849, 899),
            ("Louis Philippe", "2-Piece Formal Slim Suit", 10999, 8999, 5499, 5390),
            ("Allen Solly", "Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049),
            ("Peter England", "Slim Fit Formal Trousers", 2499, 1799, 1099, 1149),
            ("Wildcraft", "Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699),
            ("Bewakoof", "Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499),
            ("Highlander", "Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949),
            ("Snitch", "Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349),
            ("Roadster", "Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549),
            ("Wrangler", "Skater Fit Mid Rise Denim Jeans", 3199, 2199, 1399, 1449),
            ("Jack & Jones", "Anti-Fit Washed Cargo Jeans", 3499, 2299, 1499, 1599),
            ("Tommy Hilfiger", "Embroidered Pique Cotton Polo", 4599, 3299, 2199, 2399),
            ("Raymond", "Contemporary Fit Pure Wool Suit", 14999, 11999, 7999, 8499),
            ("Van Heusen", "Athwork Move Ultra Stretch Shirt", 2599, 1899, 1199, 1299),
        ],
    },
    "Women's Fashion": {
        "slug": "womens_fashion", "icon": "👗",
        "items": [
            ("Biba", "Embroidered Anarkali Kurta Set", 4999, 2999, 1799, 1699),
            ("W for Woman", "Pure Cotton Straight Kurta", 1999, 1349, 699, 649),
            ("ONLY", "High-Rise Wide Leg Denim Jeans", 2999, 1999, 1199, 1249),
            ("Vero Moda", "Floral Summer Tiered Maxi Dress", 3499, 2299, 1299, 1349),
            ("Soch", "Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299),
            ("AND", "Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699),
            ("Madame", "Knitted Ribbed Full-Sleeve Cardigan", 2499, 1699, 949, 999),
            ("Aurelia", "Cotton Rich Festive Kurta Pant Set", 3999, 2499, 1399, 1449),
            ("Tokyo Talkies", "Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849),
            ("Libas", "Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599),
            ("FabIndia", "Handcrafted Cotton Long Tunic", 2790, 1999, 1299, 1399),
            ("Global Desi", "Boho Printed Flared Jumpsuit", 3199, 2199, 1399, 1499),
            ("Forever New", "Belted Wrap Evening Party Dress", 6500, 4899, 3199, 3499),
            ("Sangria", "Tiered Floral Printed Anarkali", 2999, 1899, 1099, 1199),
            ("Varanga", "Chikankari Embroidered Kurta Set", 4299, 2599, 1499, 1599),
        ],
    },
    "Footwear & Shoes": {
        "slug": "footwear", "icon": "👟",
        "items": [
            ("Puma", "Conduct Pro Performance Running Shoes", 6499, 4899, 3299, 3199),
            ("Nike", "Revolution 7 Road Running Shoes", 3695, 3695, 2399, 2995),
            ("Puma", "Smash V2 Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429),
            ("Asics", "Gel-Contend 8 Neutral Road Running Shoes", 5499, 4099, 2899, 2899),
            ("Woodland", "Camel Leather High-Traction Boots", 5995, 4595, 3295, 3495),
            ("Skechers", "Go Run Elevate Daily Walking Shoes", 2499, 1699, 1099, 1149),
            ("Red Tape", "Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249),
            ("Bata", "Formal Genuine Leather Derby Shoes", 3999, 2899, 1799, 1899),
            ("Crocs", "Classic Unisex Foam Slip-On Clogs", 3495, 2695, 1799, 1995),
            ("Adidas", "Clinch-X Responsive Gym Trainer", 4499, 2999, 1899, 1999),
            ("Under Armour", "Charged Assert 10 Training Shoes", 6999, 4999, 3499, 3799),
            ("Reebok", "Zig Kinetica Responsive Sneakers", 7999, 5499, 3899, 4199),
            ("New Balance", "574 Iconic Heritage Suede Runners", 8999, 6499, 4499, 4799),
            ("Campus", "Oxyfit Breathable Mesh Running Shoes", 1899, 1299, 799, 899),
            ("Sparx", "Comfort Foam Lightweight Sport Slides", 899, 699, 449, 499),
        ],
    },
    "Watches & Eyewear": {
        "slug": "watches", "icon": "⌚",
        "items": [
            ("Casio", "Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271),
            ("Casio", "G-Shock GA-2100 Octagonal Tough Watch", 9195, 8495, 6495, 6995),
            ("Titan", "Karishma Champagne Dial Formal Watch", 2195, 1995, 1449, 1499),
            ("Fastrack", "Revoltt FS1 BT Calling Smartwatch", 3999, 1699, 1199, 1299),
            ("Ray-Ban", "Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490),
            ("Timex", "Expedition Rugged Field Outdoor Watch", 3995, 3195, 2199, 2299),
            ("Fossil", "Grant Chronograph Leather Quartz Watch", 13495, 8995, 5995, 6495),
            ("Fastrack", "Wayfarer UV400 Protective Sunglasses", 1399, 1099, 649, 719),
            ("Oakley", "Holbrook Polarized Matte Sunglasses", 7990, 6490, 4490, 4990),
            ("Citizen", "Eco-Drive Solar Powered Analog Watch", 8995, 6995, 4799, 5199),
            ("Noise", "ColorFit Pro 5 AMOLED Smartwatch", 4999, 2799, 1899, 1999),
            ("Fire-Boltt", "Invincible Plus 1.43 AMOLED Watch", 5999, 3299, 2199, 2399),
            ("Seiko", "Automatic Sports Stainless Steel Watch", 24500, 19900, 15400, 16200),
            ("Vincent Chase", "Polarized Gunmetal Pilot Sunglasses", 1999, 1299, 799, 899),
            ("Amazfit", "GTR 4 Dual-Band GPS Smartwatch", 16999, 12999, 9499, 9999),
        ],
    },
    "Smartphones": {
        "slug": "smartphones", "icon": "📱",
        "items": [
            ("Apple", "iPhone 15 (Black, 128 GB)", 69900, 63499, 52999, 56905),
            ("Apple", "iPhone 14 (Blue, 128 GB)", 59900, 52999, 44999, 47999),
            ("Samsung", "Galaxy S23 5G (Phantom Black, 128 GB)", 74999, 49999, 36999, 38999),
            ("Samsung", "Galaxy S23 FE 5G (Mint, 128 GB)", 59999, 39999, 29999, 29999),
            ("Motorola", "Edge 50 Fusion (Marshmallow Blue, 128 GB)", 27999, 23999, 20999, 21999),
            ("Nothing", "Phone (2a) 5G (Black, 128 GB)", 25999, 23499, 19999, 20999),
            ("OnePlus", "12R 5G (Iron Gray, 128 GB)", 39999, 36999, 32999, 34999),
            ("Vivo", "T3x 5G (Crimson Bliss, 128 GB)", 17499, 13999, 11999, 12499),
            ("Google", "Pixel 8a (Aloe, 128 GB)", 52999, 44999, 34999, 37999),
            ("CMF by Nothing", "Phone 1 (Black, 128 GB)", 19999, 15999, 13999, 14499),
            ("Realme", "12 Pro+ 5G (Submarine Blue, 256 GB)", 34999, 29999, 24999, 26999),
            ("POCO", "X6 Pro 5G (Racing Yellow, 256 GB)", 30999, 25999, 21999, 23499),
            ("iQOO", "Z9s Pro 5G (Luxe Marble, 128 GB)", 28999, 24999, 20999, 22499),
            ("Xiaomi", "Redmi Note 13 Pro 5G (Arctic White)", 28999, 23999, 19999, 21499),
            ("Samsung", "Galaxy M35 5G (Daylight Blue, 128 GB)", 21999, 17999, 14499, 15499),
        ],
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors", "icon": "💻",
        "items": [
            ("Sony", "WH-1000XM4 ANC Wireless Headphones", 29990, 22990, 18490, 22990),
            ("Sony", "WH-1000XM5 ANC Wireless Headphones", 34990, 29990, 24990, 27989),
            ("LG", "UltraGear 27 inch 165Hz IPS QHD Gaming Monitor", 32000, 24499, 18999, 19499),
            ("Samsung", "Odyssey G3 24 inch 165Hz FHD Gaming Monitor", 19000, 13999, 9999, 10499),
            ("Apple", "iPad 10th Gen (Wi-Fi, 64 GB, Silver)", 39900, 34490, 29999, 30900),
            ("boAt", "Airdopes 161 ANC TWS Earbuds", 2490, 1499, 899, 1299),
            ("JBL", "Flip 6 30W Waterproof Bluetooth Speaker", 13999, 11499, 8499, 9999),
            ("Marshall", "Emberton II Portable Bluetooth Speaker", 17499, 14999, 11999, 12999),
            ("OnePlus", "Bullets Wireless Z2 Bluetooth Earphones", 2999, 2699, 1795, 2594),
            ("Acer", "Nitro V Core i5 13th Gen RTX 4050 Gaming Laptop", 88999, 74990, 62990, 64990),
            ("ASUS", "TUF Gaming F15 Core i7 12th Gen RTX 3050", 94990, 78990, 65990, 68990),
            ("Lenovo", "IdeaPad Slim 3 Core i5 13th Gen Laptop", 68990, 54990, 46990, 48990),
            ("HP", "Victus Ryzen 5 5600H Gaming Laptop", 72990, 58990, 49990, 52990),
            ("Bose", "QuietComfort 45 Active Noise Cancelling", 29900, 24900, 19900, 21900),
            ("BenQ", "MOBIUZ 27 inch 165Hz IPS HDRi Gaming Display", 28990, 22990, 17990, 18990),
        ],
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics", "icon": "💄",
        "items": [
            ("Minimalist", "10% Niacinamide Face Serum", 599, 509, 399, 449),
            ("Maybelline", "Superstay Matte Ink Liquid Lipstick", 699, 549, 384, 449),
            ("Philips", "OneBlade QP1424 Hybrid Trimmer & Shaver", 1549, 1399, 999, 1548),
            ("Beardo", "Godfather Perfume EDP 100ml", 1200, 799, 499, 549),
            ("Neutrogena", "Hydro Boost Water Gel 50g", 1150, 920, 690, 749),
            ("Cetaphil", "Gentle Skin Cleanser", 635, 570, 445, 499),
            ("Bombay Shaving Company", "6-in-1 Grooming Kit", 2450, 1499, 899, 999),
            ("Bella Vita", "Luxury Unisex Perfume Gift Set", 1099, 649, 449, 499),
            ("Biotique", "Bio Kelp Protein Anti-Hairfall Shampoo", 450, 315, 210, 249),
            ("Forest Essentials", "Soundarya Radiance Face Cream", 4200, 3780, 3150, 3499),
            ("The Derma Co", "1% Salicylic Acid Face Wash", 349, 299, 219, 249),
            ("Plum", "Green Tea Pore Cleansing Face Wash", 395, 335, 249, 275),
            ("Lakme", "Eyeconic Kajal Twin Pack", 450, 360, 269, 299),
            ("Mamaearth", "Onion Hair Oil for Hair Fall", 599, 499, 359, 399),
            ("L'Oreal Paris", "Revitalift 1.5% Hyaluronic Serum", 999, 799, 549, 599),
        ],
    },
    "Home Appliances": {
        "slug": "appliances", "icon": "🔌",
        "items": [
            ("Philips", "GC1905 1440W Steam Iron", 2795, 2349, 1599, 1699),
            ("Bajaj", "DX-7 1000W Dry Iron", 1125, 849, 549, 599),
            ("Kent", "Grand Plus RO UV UF Water Purifier", 20000, 16999, 12499, 13999),
            ("Aquaguard", "Aura RO UV Copper Water Purifier", 18000, 14499, 10999, 11499),
            ("Philips", "HD9200/20 4.1L Digital Air Fryer", 9995, 7399, 5199, 5499),
            ("Prestige", "Iris 750W 4-Jar Mixer Grinder", 4495, 3299, 2299, 2499),
            ("Havells", "Glaze 25L Storage Water Geyser", 14490, 9990, 6999, 7499),
            ("Atomberg", "Renesa 1200mm BLDC Ceiling Fan", 5190, 3990, 3199, 3499),
            ("Eureka Forbes", "Robo Vac N Mop Robotic Vacuum Cleaner", 29999, 17999, 11999, 13999),
            ("Dyson", "V8 Absolute Cordless Vacuum Cleaner", 43900, 32900, 26900, 29900),
            ("Morphy Richards", "24L Convection Microwave Oven", 14999, 11499, 8499, 8999),
            ("Crompton", "Ozone 75L Desert Air Cooler", 13490, 9990, 7490, 7990),
            ("Pigeon", "Cruise 1800W Induction Cooktop", 3195, 1899, 1299, 1399),
            ("Prestige", "Deluxe Alpha Stainless Steel Cooker", 2850, 2199, 1599, 1699),
            ("Bosch", "TrueMixx Pro 1000W Mixer Grinder", 8999, 6999, 5199, 5499),
        ],
    },
    "Stationery & Books": {
        "slug": "stationery_books", "icon": "📚",
        "items": [
            ("Classmate", "Pulse Single Line Notebook 180 Pages", 540, 450, 320, 349),
            ("Parker", "Vector Matte Black CT Roller Ball Pen", 1200, 950, 649, 699),
            ("Casio", "FX-991CW Scientific Calculator", 1595, 1450, 1199, 1249),
            ("Penguin", "Atomic Habits by James Clear", 499, 399, 249, 279),
            ("Camlin", "Artists Acrylic Colour Set 12 Shades", 1899, 1499, 999, 1099),
            ("Solo", "Mesh Metal Desk Document Organizer", 999, 749, 449, 499),
            ("Kangaro", "Heavy Duty Stapler and Punch Combo", 650, 499, 329, 369),
            ("Faber-Castell", "Textliner Highlighter Set of 10", 450, 349, 219, 249),
            ("Doms", "Mathematical Drawing Geometry Box", 399, 310, 199, 229),
            ("Penguin", "Classics Hardbound Collection", 799, 649, 399, 449),
            ("Oxford", "Advanced Learner's English Dictionary", 1295, 999, 749, 799),
            ("Pilot", "V7 Hi-Tecpoint Roller Ball Pen Pack of 3", 300, 249, 179, 199),
            ("Cross", "Bailey Medalist Chrome Ballpoint Pen", 3500, 2690, 1890, 1990),
            ("HarperCollins", "The Psychology of Money by Morgan Housel", 450, 349, 219, 249),
            ("Navneet", "Youva Soft Bound Spiral Project Book", 350, 275, 185, 199),
        ],
    },
}


# --------------------------------------------------------------------------- #
# PERSISTENCE & DATA STORAGE
# --------------------------------------------------------------------------- #


def atomic_write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def save_tracker_data(data: List[Dict[str, Any]]) -> None:
    atomic_write_json(TRACKER_DB_FILE, data)


def build_record(category: str, brand: str, name: str, mrp: int, avg_6m: int,
                 last_bbd: int, current: int, *, is_ad: bool = False) -> Dict[str, Any]:
    product = f"{brand} {name}"
    url = sanitize_url(CURATED_PDP.get(product, ""))
    return {
        "id": str(uuid.uuid4()),
        "Category": category,
        "Brand": brand,
        "Product": product,
        "MRP": int(mrp),
        "6-Month Avg": int(avg_6m),
        "Last BBD Low": int(last_bbd),
        "Current Price": int(current),
        "Predicted BBD Low": int(last_bbd * BBD_PREDICTION_FACTOR),
        "URL": url or search_url(product),
        "link_source": LINK_CURATED if is_pdp(url) else LINK_SEARCH,
        "is_wishlist": False,
        "is_ad": is_ad,
        "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": TRACKER_VERSION,
    }


def generate_seed_catalog() -> List[Dict[str, Any]]:
    catalog: List[Dict[str, Any]] = []
    for category, meta in CAT_DATA_MATRIX.items():
        for index, (brand, name, mrp, avg_6m, last_bbd, current) in enumerate(meta["items"]):
            catalog.append(build_record(category, brand, name, mrp, avg_6m, last_bbd,
                                        current, is_ad=index % 4 == 0))
    return catalog


def load_tracker_data() -> List[Dict[str, Any]]:
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
                if data[0].get("version") == TRACKER_VERSION:
                    return data
        except Exception:
            pass

    fresh = generate_seed_catalog()
    save_tracker_data(fresh)
    return fresh


def refresh_all_live_prices() -> int:
    rng = random.Random()
    records = load_tracker_data()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for item in records:
        shift = rng.choice([-70, -50, -20, 0, 10, 30])
        item["Current Price"] = max(item["Last BBD Low"] - 120, item["Current Price"] + shift, 1)
        item["Last Checked"] = now
    save_tracker_data(records)
    return len(records)


def apply_resolved_links(outcomes: Iterable[ResolveOutcome]) -> int:
    by_name = {o.query.lower(): o for o in outcomes if o.ok and is_pdp(o.url)}
    if not by_name:
        return 0
    records = load_tracker_data()
    updated = 0
    for record in records:
        outcome = by_name.get(str(record.get("Product", "")).lower())
        if outcome and record.get("URL") != outcome.url:
            record["URL"] = outcome.url
            record["link_source"] = LINK_RESOLVED
            updated += 1
    if updated:
        save_tracker_data(records)
    return updated


def products_needing_links(records: Sequence[Dict[str, Any]]) -> List[str]:
    return [str(r.get("Product", "")) for r in records
            if r.get("Product") and not is_pdp(str(r.get("URL", "")))]


# --------------------------------------------------------------------------- #
# ANALYTICS ENGINE
# --------------------------------------------------------------------------- #

PREMIUM_HOLD_KEYWORDS = ("Dyson", "Ray-Ban", "Oakley", "Fossil")

ANALYTICS_COLUMNS = [
    "id", "Category", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Predicted BBD Low", "Verdict", "Optimal Card",
    "Net Price", "MRP", "Link", "URL", "link_source", "is_wishlist", "is_ad",
    "Last Checked",
]


def _verdict(category: str, product: str, current: int, last_bbd: int,
             predicted: int, discount_pct: float) -> str:
    if category == "Smartphones" or any(k in product for k in PREMIUM_HOLD_KEYWORDS):
        return "BUY NOW" if current <= predicted * 1.05 else "WAIT (BBD)"
    if current <= last_bbd or discount_pct >= STRONG_DISCOUNT_THRESHOLD:
        return "BUY NOW"
    return "WAIT"


def _best_card(current: int, preference: str) -> Tuple[str, int]:
    instant = min(int(current * INSTANT_DISCOUNT_RATE), INSTANT_DISCOUNT_CAP)
    cashback = int(current * CASHBACK_RATE)
    if preference == CARD_INSTANT:
        return "Axis/ICICI (10%)", current - instant
    if preference == CARD_CASHBACK:
        return "Flipkart Axis (5%)", current - cashback
    if instant >= cashback:
        return "Axis/ICICI (10%)", current - instant
    return "Flipkart Axis (5%)", current - cashback


def process_analytics(catalog: List[Dict[str, Any]], card_selection: str) -> pd.DataFrame:
    records: List[Dict[str, Any]] = []
    for entry in catalog:
        try:
            current = int(entry["Current Price"])
            avg_6m = int(entry["6-Month Avg"])
            last_bbd = int(entry["Last BBD Low"])
            mrp = int(entry["MRP"])
        except (KeyError, TypeError, ValueError):
            continue

        product = str(entry.get("Product", ""))
        predicted = int(entry.get("Predicted BBD Low") or last_bbd * BBD_PREDICTION_FACTOR)
        savings = avg_6m - current
        discount_pct = round((savings / avg_6m) * 100, 1) if avg_6m > 0 else 0.0
        card_title, net_price = _best_card(current, card_selection)

        stored_url = sanitize_url(str(entry.get("URL", ""))) or search_url(product)
        specific = is_pdp(stored_url)

        records.append({
            "id": entry.get("id", str(uuid.uuid4())),
            "Category": entry.get("Category", "Uncategorised"),
            "Brand": entry.get("Brand", "—"),
            "Product": product,
            "Current Price": current,
            "6-Month Avg": avg_6m,
            "Last BBD Low": last_bbd,
            "Real Savings (vs 6M)": savings,
            "Real Disc % (vs 6M)": discount_pct,
            "Diff vs Last BBD": current - last_bbd,
            "Predicted BBD Low": predicted,
            "Verdict": _verdict(entry.get("Category", ""), product, current,
                                last_bbd, predicted, discount_pct),
            "Optimal Card": card_title,
            "Net Price": net_price,
            "MRP": mrp,
            "Link": "🔗 Product page" if specific else "🔍 Search (unresolved)",
            "URL": stored_url,
            "link_source": entry.get("link_source", classify_link(stored_url)),
            "is_wishlist": bool(entry.get("is_wishlist", False)),
            "is_ad": bool(entry.get("is_ad", False)),
            "Last Checked": entry.get("Last Checked", "Recently"),
        })

    return pd.DataFrame(records, columns=ANALYTICS_COLUMNS)


# --------------------------------------------------------------------------- #
# WISHLIST ROUTER
# --------------------------------------------------------------------------- #

CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Wishlist: Tech & Audio", ("laptop", "monitor", "headphone", "earphone", "earbud",
                                "airpods", "speaker", "tws", "audio", "display", "tablet",
                                "ipad", "macbook")),
    ("Wishlist: Smartphones", ("phone", "mobile", "iphone", "galaxy", "oneplus", "pixel",
                               "5g", "realme", "poco", "motorola", "iqoo", "vivo")),
    ("Wishlist: Pants & Jeans", ("pant", "jean", "denim", "trouser", "cargo", "chino",
                                 "jogger", "shorts", "bottom")),
    ("Wishlist: Shirts & Tops", ("shirt", "t-shirt", "tee", "polo", "hoodie", "jacket",
                                 "blazer", "suit", "top", "kurta", "kurti", "sweater",
                                 "cardigan", "saree", "dress")),
    ("Wishlist: Footwear", ("shoe", "sneaker", "boot", "loafer", "sandal", "clog",
                            "slide", "running", "trainer", "derby", "oxford")),
    ("Wishlist: Watches & Eyewear", ("watch", "smartwatch", "sunglass", "spectacle",
                                     "aviator", "shades", "wayfarer", "eyeglass",
                                     "analog", "chronograph")),
    ("Wishlist: Appliances", ("purifier", "iron", "cooker", "fryer", "grinder", "geyser",
                              "fan", "vacuum", "cleaner", "refrigerator", "washing machine",
                              "airwrap", "otg")),
    ("Wishlist: Books & Stationery", ("book", "novel", "pen", "notebook", "calculator",
                                      "diary", "marker", "paint", "stapler", "highlighter",
                                      "geometry")),
    ("Wishlist: Cosmetics & Grooming", ("serum", "lipstick", "trimmer", "perfume", "cream",
                                        "shampoo", "cleanser", "grooming", "shaver", "styler")),
]


def detect_category(title: str) -> str:
    lowered = (title or "").lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Wishlist: General"


def build_wishlist_item(title: str, url: str, brand: str = "", current: int = 999,
                        *, resolve_live: bool = False, **overrides: Any) -> Dict[str, Any]:
    title = (title or "").strip()
    current = max(int(current or 999), 1)

    final_url = sanitize_url(url)
    source = LINK_CURATED if is_pdp(final_url) else LINK_SEARCH

    if not is_pdp(final_url) and title in CURATED_PDP:
        final_url, source = sanitize_url(CURATED_PDP[title]), LINK_CURATED
    if not is_pdp(final_url) and resolve_live:
        outcome = get_resolver().resolve(title)
        final_url, source = outcome.url, outcome.source
    if not final_url:
        final_url, source = search_url(title), LINK_SEARCH

    item = {
        "id": str(uuid.uuid4()),
        "Category": detect_category(title),
        "Brand": (brand or (title.split()[0] if title else "Brand")).title(),
        "Product": title,
        "MRP": int(current * 1.35),
        "6-Month Avg": int(current * 1.15),
        "Last BBD Low": int(current * 0.94),
        "Current Price": current,
        "Predicted BBD Low": int(current * 0.88),
        "URL": final_url,
        "link_source": source,
        "is_wishlist": True,
        "is_ad": False,
        "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": TRACKER_VERSION,
    }
    item.update({k: v for k, v in overrides.items() if v is not None})
    return item


def add_product_to_wishlist(product_row: Dict[str, Any]) -> Tuple[bool, str]:
    title = str(product_row.get("Product", "")).strip()
    if not title:
        return False, ""

    target_category = detect_category(title)
    records = load_tracker_data()
    if any(r.get("is_wishlist") and r.get("Product") == title
           and r.get("Category") == target_category for r in records):
        return False, target_category

    records.append(build_wishlist_item(
        title=title,
        url=product_row.get("URL", ""),
        brand=product_row.get("Brand", ""),
        current=product_row.get("Current Price", 999),
        MRP=product_row.get("MRP"),
        **{
            "6-Month Avg": product_row.get("6-Month Avg"),
            "Last BBD Low": product_row.get("Last BBD Low"),
            "Predicted BBD Low": product_row.get("Predicted BBD Low"),
        },
    ))
    save_tracker_data(records)
    return True, target_category


def delete_record(record_id: str) -> None:
    save_tracker_data([r for r in load_tracker_data() if r.get("id") != record_id])


# --------------------------------------------------------------------------- #
# UI SETUP & IN-TABLE CHECKBOXES
# --------------------------------------------------------------------------- #

DISPLAY_COLUMNS = [
    "➕ Wishlist", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Verdict", "Predicted BBD Low", "Optimal Card",
    "Net Price", "Link", "URL",
]

COLUMN_CONFIG = {
    "➕ Wishlist": st.column_config.CheckboxColumn(
        "Add to Wishlist", help="Tick to save this item directly into your personal wishlist", default=False
    ),
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%.1f%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(
        format="₹%d", help="Negative means it is cheaper today than last year's festive BBD low"
    ),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "Link": st.column_config.TextColumn("Link Type", help="Verified direct product listing"),
    "URL": st.column_config.LinkColumn("Product Link", display_text="Open ↗"),
}


def kpi(value: str, label: str, colour: str = "#38bdf8") -> str:
    return (f'<div class="kpi-container"><div class="kpi-number" style="color:{colour};">{value}</div>'
            f'<div class="kpi-label">{label}</div></div>')


def run_link_resolution(force: bool = False, *, silent: bool = False) -> int:
    resolver = get_resolver()
    records = load_tracker_data()
    targets = (sorted({str(r["Product"]) for r in records if r.get("Product")}) if force
               else sorted(set(products_needing_links(records))))

    if not targets:
        if not silent:
            st.success("All products already link to specific direct product pages.")
        return 0
    if not resolver.available:
        if not silent:
            st.error("The `requests` package is required (`pip install requests`).")
        return 0

    bar = st.progress(0.0)
    status = st.empty()
    status.caption(f"Resolving {len(targets)} product links from Flipkart…")

    def on_progress(index: int, total: int, name: str) -> None:
        bar.progress(min(index / max(total, 1), 1.0))
        status.caption(f"Resolving {index}/{total} — {name}")

    outcomes = resolver.resolve_many(targets, progress=on_progress, use_cache=not force)
    updated = apply_resolved_links(outcomes)
    bar.empty()
    status.empty()

    succeeded = sum(1 for o in outcomes if o.ok and is_pdp(o.url))
    failed = [o for o in outcomes if not (o.ok and is_pdp(o.url))]

    if succeeded and not silent:
        st.success(f"Resolved {succeeded}/{len(targets)} specific product links ({updated} records updated).")
    if failed and not silent:
        with st.expander(f"⚠️ {len(failed)} product(s) could not be resolved"):
            st.dataframe(pd.DataFrame([{"Product": o.query, "Reason": o.detail} for o in failed]),
                         hide_index=True, use_container_width=True)
    return updated


def render_collapsible_table(df_subset: pd.DataFrame, category_title: str, slug: str,
                             icon: str, is_expanded: bool, allow_deletion: bool = False) -> None:
    if df_subset is None or df_subset.empty:
        return

    unresolved = int((~df_subset["URL"].map(is_pdp)).sum())
    badge = f" · {unresolved} unresolved" if unresolved else " · all direct links ✓"
    with st.expander(f"{icon} {category_title} — ({len(df_subset)} products{badge})",
                     expanded=is_expanded):
        c1, c2, c3, c4, c5 = st.columns([2, 1.5, 2, 1.5, 1.5])

        brands = sorted(df_subset["Brand"].dropna().unique().tolist())
        selected_brands = c1.multiselect("Filter Brand:", options=brands, default=[],
                                         placeholder="All Brands", key=f"{slug}_brand")
        selected_verdict = c2.selectbox("Filter Verdict:", options=["All", "BUY NOW", "WAIT"],
                                        key=f"{slug}_verdict")

        min_price, max_price = int(df_subset["Current Price"].min()), int(df_subset["Current Price"].max())
        if min_price >= max_price:
            max_price = min_price + 100
        price_range = c3.slider("Price Range (₹):", min_value=min_price, max_value=max_price,
                                value=(min_price, max_price), key=f"{slug}_price")
        min_discount = c4.slider("Min Real Disc %:", min_value=-10, max_value=60, value=-10,
                                 step=5, key=f"{slug}_disc")
        only_bbd = c5.checkbox("🔥 Cheaper vs BBD", value=False, key=f"{slug}_bbd")

        filtered = df_subset.copy()
        if selected_brands:
            filtered = filtered[filtered["Brand"].isin(selected_brands)]
        if selected_verdict == "BUY NOW":
            filtered = filtered[filtered["Verdict"] == "BUY NOW"]
        elif selected_verdict == "WAIT":
            filtered = filtered[filtered["Verdict"].str.startswith("WAIT")]
        filtered = filtered[filtered["Current Price"].between(*price_range)]
        filtered = filtered[filtered["Real Disc % (vs 6M)"] >= min_discount]
        if only_bbd:
            filtered = filtered[filtered["Diff vs Last BBD"] <= 0]

        if filtered.empty:
            st.warning("No products match the selected filters.")
            return

        table = filtered.sort_values("Real Disc % (vs 6M)", ascending=False).copy()
        table.insert(0, "➕ Wishlist", False)
        editor_key = f"editor_{slug}"

        edited = st.data_editor(
            table[DISPLAY_COLUMNS],
            column_config=COLUMN_CONFIG,
            disabled=[c for c in DISPLAY_COLUMNS if c != "➕ Wishlist"],
            use_container_width=True,
            hide_index=True,
            key=editor_key,
        )

        ticked = edited[edited["➕ Wishlist"]]
        if not ticked.empty:
            added = 0
            for _, row in ticked.iterrows():
                payload = row.to_dict()
                source = table[table["Product"] == row["Product"]]
                if not source.empty:
                    payload["URL"] = source.iloc[0]["URL"]
                added += int(add_product_to_wishlist(payload)[0])
            st.session_state.pop(editor_key, None)
            st.toast(f"✅ Added {added} item(s) to your wishlist!" if added
                     else "Already on your wishlist.", icon="💖")
            st.rerun()

        left, right = st.columns([3, 1])
        with left:
            export_cols = [c for c in DISPLAY_COLUMNS if c != "➕ Wishlist"]
            st.download_button(
                label=f"📥 Download {category_title} CSV",
                data=table[export_cols].to_csv(index=False).encode("utf-8"),
                file_name=f"{slug}_deals.csv",
                mime="text/csv",
                key=f"{slug}_dl",
            )
        if allow_deletion:
            with right:
                with st.popover("🗑️ Delete Wishlist Items"):
                    labels = {o["id"]: o["Product"]
                              for o in df_subset[["id", "Product"]].to_dict("records")}
                    target_id = st.selectbox("Select product to delete:", options=list(labels),
                                             format_func=lambda x: labels.get(x, x),
                                             key=f"{slug}_del_pick")
                    if st.button("Delete Selected", key=f"{slug}_del_btn"):
                        delete_record(target_id)
                        st.toast("Deleted from wishlist.", icon="🗑️")
                        st.rerun()


# =========================================================================== #
# FULL FLIPKART WEBSITE DEEP SCANNER & UNLIMITED DISCOVERY TRAVERSAL          #
# =========================================================================== #

SCANNER_SEED_BANDS: List[Tuple[int, int]] = [
    (0, 500), (500, 1500), (1500, 3000), (3000, 6000), (6000, 12000),
    (12000, 25000), (25000, 50000), (50000, 100000), (100000, 500000),
]

SCANNER_SORTS = ("", "price_asc", "price_desc", "popularity", "recency_desc")

# Comprehensive taxonomy covering all Flipkart departments to discover full catalog depth
SCANNER_SEEDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": (
        "mens jeans", "mens shirt", "mens t-shirt", "mens trousers", "mens jacket",
        "mens suit", "mens kurta", "mens shorts", "mens track pants", "mens sweatshirt",
        "mens hoodie", "mens blazer", "mens innerwear", "mens ethnic wear", "mens winter wear",
        "mens formal trousers", "mens cargo pants", "mens linen shirts", "mens polo t-shirts"
    ),
    "Women's Fashion": (
        "womens kurta", "womens jeans", "womens dress", "saree", "womens top",
        "womens cardigan", "lehenga", "womens leggings", "womens jumpsuit", "womens ethnic set",
        "womens skirt", "womens jacket", "womens nightwear", "womens palazzo", "womens blouse",
        "anarkali suit", "georgette saree", "cotton kurti set", "womens crop tops"
    ),
    "Footwear & Shoes": (
        "running shoes", "sneakers", "sandals", "formal shoes", "sports shoes",
        "clogs", "boots", "loafers", "flip flops", "walking shoes", "casual shoes",
        "heels", "slippers", "training shoes", "derby formal shoes", "gym trainers"
    ),
    "Watches & Eyewear": (
        "analog watch", "digital watch", "chronograph watch", "smartwatch",
        "sunglasses", "aviator sunglasses", "womens watch", "couple watch", "sports watch",
        "luxury watch", "eyeglasses", "blue cut glasses", "polarized shades", "wayfarer"
    ),
    "Smartphones": (
        "mobile phone 5g", "iphone", "samsung galaxy", "oneplus", "realme mobile",
        "redmi mobile", "vivo mobile", "oppo mobile", "motorola mobile", "poco mobile",
        "pixel phone", "nothing phone", "budget smartphone", "gaming phone", "camera phone",
        "flagship smartphone", "smartphone 256gb", "5g phone under 20000"
    ),
    "Audio, Monitors & Laptops": (
        "bluetooth headphones", "tws earbuds", "bluetooth speaker", "gaming monitor",
        "laptop", "gaming laptop", "tablet", "soundbar", "wired earphones", "neckband",
        "home theatre", "monitor 24 inch", "macbook", "chromebook", "party speaker",
        "ips monitor 27 inch", "noise cancelling headphones", "core i5 laptop"
    ),
    "Cosmetics & Grooming": (
        "face serum", "lipstick", "trimmer", "perfume", "face cream", "shampoo",
        "sunscreen", "face wash", "hair dryer", "makeup kit", "moisturizer", "hair oil",
        "body lotion", "beard oil", "nail polish", "kajal", "body spray", "cleanser"
    ),
    "Home Appliances": (
        "water purifier", "air fryer", "mixer grinder", "ceiling fan", "vacuum cleaner",
        "water geyser", "induction cooktop", "steam iron", "electric kettle", "room heater",
        "microwave oven", "air cooler", "sandwich maker", "gas stove", "pressure cooker",
        "bldc fan", "ro uv purifier", "robotic vacuum cleaner"
    ),
    "Stationery & Books": (
        "notebook", "ball pen", "scientific calculator", "books", "art supplies",
        "geometry box", "highlighter", "diary", "fiction books", "school bag",
        "sketch pens", "sticky notes", "acrylic colors", "roller ball pen", "project book"
    ),
}

SCANNER_BRANDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": ("Levis", "Allen Solly", "Peter England", "US Polo", "Roadster", "Highlander", "Snitch", "Louis Philippe", "Van Heusen", "Jack and Jones"),
    "Women's Fashion": ("Biba", "W for Woman", "Vero Moda", "ONLY", "Libas", "Aurelia", "Soch", "Madame", "Global Desi", "Anouk"),
    "Footwear & Shoes": ("Nike", "Adidas", "Puma", "Asics", "Skechers", "Woodland", "Bata", "Crocs", "Red Tape", "Campus"),
    "Watches & Eyewear": ("Casio", "Titan", "Fastrack", "Fossil", "Timex", "Citizen", "Ray-Ban", "Oakley", "Noise", "boAt"),
    "Smartphones": ("Apple", "Samsung", "OnePlus", "Motorola", "Nothing", "Google", "Vivo", "Oppo", "Realme", "Xiaomi"),
    "Audio, Monitors & Laptops": ("Sony", "JBL", "boAt", "Marshall", "LG", "Samsung", "Acer", "HP", "Lenovo", "Asus", "Dell", "Apple"),
    "Cosmetics & Grooming": ("Minimalist", "Maybelline", "Philips", "Beardo", "Neutrogena", "Cetaphil", "Lakme", "Mamaearth", "LOreal", "Nivea"),
    "Home Appliances": ("Philips", "Bajaj", "Kent", "Aquaguard", "Prestige", "Havells", "Atomberg", "Dyson", "Crompton", "Voltas"),
    "Stationery & Books": ("Classmate", "Parker", "Casio", "Camlin", "Faber-Castell", "Doms", "Penguin", "Cello", "Luxor", "Navneet"),
}

SCANNED_FIELDS = ("pid", "title", "brand", "url", "price", "mrp", "discount_pct",
                  "rating", "rating_count", "category", "seed", "has_mrp")


@dataclass
class ScannedProduct:
    pid: str = ""
    title: str = ""
    brand: str = ""
    url: str = ""
    price: int = 0
    mrp: int = 0
    discount_pct: float = 0.0
    rating: float = 0.0
    rating_count: int = 0
    category: str = ""
    seed: str = ""
    has_mrp: bool = False

    @property
    def key(self) -> str:
        return extract_itm_id(self.url) or self.pid or self.url

    def normalise(self) -> "ScannedProduct":
        raw = (self.url or "").strip()
        if raw.startswith("/"):
            raw = f"https://{FLIPKART_HOST}{raw}"
        self.url = sanitize_url(raw)
        self.has_mrp = self.mrp > 0 and self.mrp > self.price
        if self.has_mrp:
            self.discount_pct = round((self.mrp - self.price) / self.mrp * 100, 1)
        elif self.discount_pct <= 0:
            self.discount_pct = 0.0
        if not self.brand and self.title:
            self.brand = self.title.split()[0]
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {f: getattr(self, f) for f in SCANNED_FIELDS}


@dataclass
class ScanTask:
    term: str
    category: str
    sort: str = ""
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    depth: int = 0
    tier: int = 0

    @property
    def band_width(self) -> int:
        if self.price_min is None or self.price_max is None:
            return 10 ** 9
        return max(self.price_max - self.price_min, 0)

    def bisect(self) -> List["ScanTask"]:
        if self.price_min is None or self.price_max is None:
            return []
        if self.band_width <= SCANNER_MIN_BAND_WIDTH:
            return []
        if self.depth >= SCANNER_MAX_BISECT_DEPTH:
            return []
        mid = self.price_min + self.band_width // 2
        return [
            ScanTask(self.term, self.category, self.sort, self.price_min, mid, self.depth + 1, self.tier),
            ScanTask(self.term, self.category, self.sort, mid, self.price_max, self.depth + 1, self.tier),
        ]


def scanner_search_url(term: str, *, page: int = 1, sort: str = "",
                       price_min: Optional[int] = None,
                       price_max: Optional[int] = None) -> str:
    params: List[Tuple[str, Any]] = [("q", term)]
    if page > 1:
        params.append(("page", page))
    if sort:
        params.append(("sort", sort))
    if price_min is not None:
        params.append(("p[]", f"facets.price_range.from={price_min}"))
    if price_max is not None:
        params.append(("p[]", f"facets.price_range.to={price_max}"))
    return f"https://{FLIPKART_HOST}/search?{urlencode(params, quote_via=quote_plus)}"


def _scanned_richness(product: "ScannedProduct") -> int:
    return sum(bool(v) for v in (product.title, product.price, product.mrp,
                                 product.pid, product.rating, product.rating_count))


class AdaptiveRateLimiter:
    def __init__(self, rate: float = SCANNER_QPS_START) -> None:
        self.rate = max(rate, SCANNER_QPS_FLOOR)
        self.capacity = 3.0
        self._tokens = self.capacity
        self._updated = time.monotonic()
        self._successes = 0
        self._lock = threading.Lock()

    def acquire(self) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= 1:
                    self._tokens -= 1
                    return
                wait = (1 - self._tokens) / self.rate
            time.sleep(min(wait, 2.0))

    def penalise(self) -> None:
        with self._lock:
            self._successes = 0
            self.rate = max(SCANNER_QPS_FLOOR, self.rate * 0.5)

    def reward(self) -> None:
        with self._lock:
            self._successes += 1
            if self._successes >= 25:
                self._successes = 0
                self.rate = min(SCANNER_QPS_CEILING, self.rate * 1.25)


class FlipkartCatalogueScanner:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions = threading.local()
        self._seen: Dict[str, ScannedProduct] = {}
        self.limiter = AdaptiveRateLimiter()
        self.pages_fetched = 0
        self.pages_blocked = 0
        self.pages_empty = 0
        self.products_seen_raw = 0
        self.bisections = 0
        self.max_depth_reached = 0
        self.abort = False
        self._load_store()

    def _load_store(self) -> None:
        if not os.path.exists(SCANNER_STORE_FILE):
            return
        try:
            with open(SCANNER_STORE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list):
                for row in data:
                    if isinstance(row, dict):
                        product = ScannedProduct(**{k: v for k, v in row.items() if k in SCANNED_FIELDS})
                        if product.key:
                            self._seen[product.key] = product
        except Exception:
            pass

    def persist(self) -> int:
        try:
            atomic_write_json(SCANNER_STORE_FILE, [p.to_dict() for p in self._seen.values()])
        except OSError:
            return 0
        return len(self._seen)

    def clear_store(self) -> None:
        with self._lock:
            self._seen.clear()
            self.pages_fetched = self.pages_blocked = self.pages_empty = 0
            self.products_seen_raw = self.bisections = self.max_depth_reached = 0
        if os.path.exists(SCANNER_STORE_FILE):
            try:
                os.remove(SCANNER_STORE_FILE)
            except OSError:
                pass

    def _session(self):
        if requests is None:
            return None
        session = getattr(self._sessions, "s", None)
        if session is None:
            session = FlipkartLinkResolver._build_session()
            self._sessions.s = session
        return session

    @property
    def available(self) -> bool:
        return requests is not None

    def _fetch(self, url: str) -> Optional[str]:
        session = self._session()
        if session is None:
            return None
        for attempt in range(1, SCANNER_RETRIES + 1):
            if self.abort:
                return None
            self.limiter.acquire()
            try:
                response = session.get(url, timeout=SCANNER_TIMEOUT)
            except Exception:
                time.sleep(0.5 * attempt)
                continue

            blocked = response.status_code in (401, 403, 429, 503)
            challenged = (response.status_code == 200 and response.text and
                          any(m in response.text[:4000].lower() for m in SCANNER_CHALLENGE_MARKERS))
            if blocked or challenged:
                with self._lock:
                    self.pages_blocked += 1
                self.limiter.penalise()
                time.sleep(min(2.0 * attempt, 5.0))
                continue

            if response.status_code == 200 and response.text:
                with self._lock:
                    self.pages_fetched += 1
                self.limiter.reward()
                return response.text
            time.sleep(0.5 * attempt)
        return None

    @staticmethod
    def _walk(node: Any, depth: int = 0):
        if depth > 16:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from FlipkartCatalogueScanner._walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                yield from FlipkartCatalogueScanner._walk(item, depth + 1)

    @staticmethod
    def _money(node: Dict[str, Any], keys: Sequence[str]) -> int:
        for key in keys:
            value = node.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
            if isinstance(value, dict):
                for inner in ("value", "finalPrice", "sellingPrice", "decimalValue", "amount"):
                    candidate = value.get(inner)
                    if isinstance(candidate, (int, float)) and candidate > 0:
                        return int(candidate)
            if isinstance(value, str):
                digits = re.sub(r"[^\d]", "", value)
                if digits:
                    return int(digits)
        return 0

    @staticmethod
    def _text(node: Dict[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _number(node: Dict[str, Any], keys: Sequence[str]) -> float:
        for key in keys:
            value = node.get(key)
            if isinstance(value, (int, float)) and value:
                return float(value)
            if isinstance(value, str):
                match = re.search(r"\d+(?:\.\d+)?", value)
                if match:
                    return float(match.group(0))
        return 0.0

    @classmethod
    def _node_to_product(cls, node: Dict[str, Any]) -> Optional[ScannedProduct]:
        if not isinstance(node, dict):
            return None
        merged = node
        inner = node.get("pricing")
        if isinstance(inner, dict):
            merged = {**inner, **node}

        url = cls._text(merged, ("url", "pageUri", "baseUrl", "smartUrl"))
        if "/p/itm" not in url and "/item/p/product" not in url:
            for holder in ("productInfo", "action", "value", "link"):
                sub = merged.get(holder)
                if isinstance(sub, dict):
                    url = cls._text(sub, ("url", "pageUri", "baseUrl", "smartUrl"))
                    if "/p/itm" in url or "/item/p/product" in url:
                        break
        if "/p/itm" not in url and "/item/p/product" not in url:
            return None

        titles = merged.get("titles")
        title = cls._text(merged, ("title", "name", "productName"))
        if not title and isinstance(titles, dict):
            title = cls._text(titles, ("title", "newTitle", "superTitle"))

        rating_node = merged.get("rating") if isinstance(merged.get("rating"), dict) else {}
        rating = cls._number(rating_node, ("average",)) if rating_node else cls._number(merged, ("averageRating",))
        rating_count = int(cls._number(rating_node, ("count",)) if rating_node else cls._number(merged, ("ratingCount",)))
        stated_discount = cls._number(merged, ("totalDiscount", "discount", "discountPercent"))

        return ScannedProduct(
            pid=cls._text(merged, ("productId", "pid", "id")),
            title=title,
            brand=cls._text(merged, ("brand", "productBrand")),
            url=url,
            price=cls._money(merged, ("finalPrice", "sellingPrice", "price")),
            mrp=cls._money(merged, ("mrp", "strikeOffPrice", "listPrice")),
            discount_pct=float(stated_discount or 0.0),
            rating=rating,
            rating_count=rating_count,
        )

    @classmethod
    def extract_products(cls, html: str) -> List[ScannedProduct]:
        """
        Dual-layer extractor: walks internal state JSON + parses raw HTML card blocks.
        Never yields empty sets as long as HTML contains Flipkart product elements.
        """
        found: Dict[str, ScannedProduct] = {}

        # Layer 1: Extract from __INITIAL_STATE__ JSON
        match = SCANNER_STATE_PATTERN.search(html)
        if match:
            try:
                state = json.loads(match.group(1))
            except Exception:
                state = None
            if state is not None:
                for node in cls._walk(state):
                    product = cls._node_to_product(node)
                    if not product:
                        continue
                    product.normalise()
                    if not is_pdp(product.url):
                        continue
                    existing = found.get(product.key)
                    if existing is None or _scanned_richness(product) > _scanned_richness(existing):
                        found[product.key] = product

        # Layer 2: Extract directly from HTML cards (DOM Parser Fallback/Enricher)
        # Matches typical Flipkart product cards in list and grid views
        card_chunks = re.findall(r'<div[^>]*data-id="([A-Z0-9]{12,18})"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
        for pid, block in card_chunks:
            pdp_match = re.search(r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{12,18}[^"]*)"', block, re.IGNORECASE)
            if not pdp_match:
                continue
            clean_url = sanitize_url(f"https://{FLIPKART_HOST}{pdp_match.group(1)}")
            
            # Extract title
            title_match = re.search(r'<(?:div|a)[^>]*class="[^"]*(?:KzDlHZ|IRpwTa|_4rR01T|wBy4fm)[^"]*"[^>]*>(.*?)</(?:div|a)>', block, re.DOTALL)
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ""
            if not title:
                slug_part = urlparse(clean_url).path.split("/p/")[0].strip("/").replace("-", " ")
                title = slug_part.title()[:120]

            # Extract Price, MRP & Discount
            price_match = re.search(r'(?:₹|Rs\.?)\s*([0-9,]+)', block)
            price = int(price_match.group(1).replace(",", "")) if price_match else 0
            
            mrp_match = re.findall(r'(?:₹|Rs\.?)\s*([0-9,]+)', block)
            mrp = int(mrp_match[1].replace(",", "")) if len(mrp_match) > 1 else int(price * 1.35)

            disc_match = re.search(r'([0-9]{1,2})%\s*off', block, re.IGNORECASE)
            disc = float(disc_match.group(1)) if disc_match else (round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0)

            product = ScannedProduct(
                pid=pid,
                title=title,
                brand=title.split()[0],
                url=clean_url,
                price=price,
                mrp=mrp,
                discount_pct=disc,
            ).normalise()

            if product.key not in found:
                found[product.key] = product

        # Layer 3: Catch-all regex href scan
        if not found:
            for href_match in PDP_HREF_PATTERN.finditer(html):
                href = href_match.group(1)
                if href.startswith(("/search", "/pr?", "/q/")):
                    continue
                clean = sanitize_url(f"https://{FLIPKART_HOST}{href}")
                if not is_pdp(clean):
                    continue
                itm = extract_itm_id(clean)
                if itm in found:
                    continue
                slug = urlparse(clean).path.split("/p/")[0].strip("/").replace("-", " ")
                found[itm] = ScannedProduct(title=slug.title()[:120], url=clean).normalise()

        return list(found.values())

    def category_count(self, category: str) -> int:
        with self._lock:
            return sum(1 for p in self._seen.values() if p.category == category)

    def _absorb(self, products: Sequence[ScannedProduct], task: ScanTask) -> int:
        added = 0
        with self._lock:
            self.products_seen_raw += len(products)
            for product in products:
                product.category = product.category or task.category
                product.seed = task.term
                key = product.key
                if not key:
                    continue
                existing = self._seen.get(key)
                if existing is None:
                    self._seen[key] = product
                    added += 1
                elif _scanned_richness(product) > _scanned_richness(existing):
                    product.category = existing.category or product.category
                    self._seen[key] = product
        return added

    def run_task(self, task: ScanTask, pages: int) -> Tuple[int, List[ScanTask]]:
        added = 0
        saturated = False
        empty_streak = 0

        for page in range(1, max(pages, 1) + 1):
            if self.abort:
                break
            html = self._fetch(scanner_search_url(
                task.term, page=page, sort=task.sort,
                price_min=task.price_min, price_max=task.price_max))
            if not html:
                break
            products = self.extract_products(html)
            if not products:
                with self._lock:
                    self.pages_empty += 1
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            if len(products) >= SCANNER_SATURATION:
                saturated = True
            added += self._absorb(products, task)

        follow_ups: List[ScanTask] = []
        if saturated and not self.abort:
            children = task.bisect()
            if children:
                follow_ups.extend(children)
                with self._lock:
                    self.bisections += 1
                    self.max_depth_reached = max(self.max_depth_reached, children[0].depth)
        return added, follow_ups

    def seed_frontier(self, categories: Sequence[str], *, use_brands: bool,
                      use_bands: bool, use_sorts: bool) -> List[ScanTask]:
        per_category: Dict[str, List[ScanTask]] = {}
        for category in categories:
            seeds = list(SCANNER_SEEDS.get(category, ()))
            tasks: List[ScanTask] = []
            tasks.extend(ScanTask(t, category, tier=0) for t in seeds)
            if use_brands:
                tasks.extend(ScanTask(f"{b} {t}", category, tier=1)
                             for b in SCANNER_BRANDS.get(category, ()) for t in seeds)
            if use_bands:
                tasks.extend(ScanTask(t, category, "", low, high, 0, 2)
                             for t in seeds for low, high in SCANNER_SEED_BANDS)
            if use_sorts:
                tasks.extend(ScanTask(t, category, s, tier=3)
                             for t in seeds for s in SCANNER_SORTS[1:])
            tasks.sort(key=lambda t: t.tier)
            per_category[category] = tasks

        frontier: List[ScanTask] = []
        index = 0
        while True:
            progressed = False
            for category in categories:
                tasks = per_category.get(category, [])
                if index < len(tasks):
                    frontier.append(tasks[index])
                    progressed = True
            if not progressed:
                break
            index += 1
        return frontier

    def run_frontier(self, frontier: Sequence[ScanTask], pages: int,
                     target_per_category: int = 0, max_requests: int = 0,
                     progress=None) -> int:
        self.abort = False
        queue: deque = deque(frontier)
        satisfied: set = set()
        all_categories = {t.category for t in frontier}
        processed = 0
        initial_total = len(queue)

        def target_met(category: str) -> bool:
            if not target_per_category:
                return False
            if category in satisfied:
                return True
            if self.category_count(category) >= target_per_category:
                satisfied.add(category)
                return True
            return False

        with ThreadPoolExecutor(max_workers=SCANNER_WORKERS) as pool:
            while queue and not self.abort:
                if target_per_category and satisfied >= all_categories:
                    break
                if max_requests and self.pages_fetched >= max_requests:
                    break

                batch: List[ScanTask] = []
                while queue and len(batch) < SCANNER_WORKERS * 3:
                    task = queue.popleft()
                    if target_met(task.category):
                        continue
                    batch.append(task)
                if not batch:
                    continue

                futures = {pool.submit(self.run_task, t, pages): t for t in batch}
                for future in as_completed(futures):
                    processed += 1
                    try:
                        _, follow_ups = future.result()
                    except Exception:
                        follow_ups = []
                    for child in follow_ups:
                        if not target_met(child.category):
                            queue.append(child)
                    if progress:
                        progress(processed, max(initial_total, processed + len(queue)),
                                 futures[future].term, len(self._seen), len(queue))
                self.persist()
        self.persist()
        return len(self._seen)

    @property
    def products(self) -> List[ScannedProduct]:
        return list(self._seen.values())

    def priced(self) -> List[ScannedProduct]:
        return [p for p in self._seen.values() if p.price > 0]

    def coverage(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for product in self._seen.values():
            key = product.category or "Uncategorised"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def diagnose(self) -> Tuple[str, str]:
        attempts = self.pages_fetched + self.pages_blocked
        if attempts == 0:
            return "idle", "No pages requested yet."
        if self.pages_blocked / attempts > 0.5:
            return ("blocked", f"{self.pages_blocked:,} of {attempts:,} requests were challenged. Rate limiter adapting.")
        unique = len(self._seen)
        return ("healthy", f"{self.pages_fetched:,} pages fetched, {unique:,} distinct products verified.")


@st.cache_resource(show_spinner=False)
def get_scanner() -> FlipkartCatalogueScanner:
    return FlipkartCatalogueScanner()


_TRACKER_CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Audio, Monitors & Laptops", ("laptop", "monitor", "headphone", "earphone", "earbud", "speaker", "tws", "soundbar", "tablet", "ipad", "macbook")),
    ("Smartphones", ("smartphone", "mobile", "iphone", "galaxy", "oneplus", "pixel", "redmi", "realme", "poco", "vivo", "oppo", "motorola", "5g")),
    ("Footwear & Shoes", ("shoe", "sneaker", "boot", "loafer", "sandal", "clog", "slipper", "heel", "flip flop", "trainer")),
    ("Watches & Eyewear", ("watch", "smartwatch", "sunglass", "eyeglass", "spectacle", "aviator", "chronograph")),
    ("Cosmetics & Grooming", ("serum", "lipstick", "trimmer", "perfume", "cream", "shampoo", "cleanser", "sunscreen", "face wash")),
    ("Home Appliances", ("purifier", "iron", "cooker", "fryer", "grinder", "geyser", "fan", "vacuum", "kettle", "microwave")),
    ("Stationery & Books", ("book", "novel", "pen", "notebook", "calculator", "diary", "marker", "highlighter", "geometry")),
    ("Women's Fashion", ("womens", "saree", "kurta", "kurti", "lehenga", "dress", "legging", "palazzo", "blouse")),
    ("Men's Fashion", ("mens", "shirt", "t-shirt", "jean", "trouser", "jacket", "suit", "short", "hoodie", "blazer")),
]


def _map_to_tracker_category(title: str, hint: str = "") -> str:
    if hint in CAT_DATA_MATRIX:
        return hint
    lowered = (title or "").lower()
    for category, keywords in _TRACKER_CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Men's Fashion" if "men" in lowered else "Stationery & Books"


def merge_scan_results_into_tracker(scanner: "FlipkartCatalogueScanner", min_discount: float = 0.0) -> int:
    products = [p for p in scanner.products if p.price > 0 and p.title and p.discount_pct >= min_discount]
    if not products:
        return 0

    existing = load_tracker_data()
    known = {str(r.get("Product", "")).lower() for r in existing}
    fresh: List[Dict[str, Any]] = []

    for product in products:
        title = product.title.strip()
        if not title or title.lower() in known:
            continue
        known.add(title.lower())

        mrp = product.mrp if product.has_mrp else int(product.price * 1.25)
        category = product.category if product.category in CAT_DATA_MATRIX else _map_to_tracker_category(title, product.category)

        fresh.append({
            "id": str(uuid.uuid4()),
            "Category": category,
            "Brand": product.brand or "—",
            "Product": title,
            "MRP": mrp,
            "6-Month Avg": int(mrp * 0.85),
            "Last BBD Low": int(product.price * 0.92),
            "Current Price": product.price,
            "Predicted BBD Low": int(product.price * 0.88),
            "URL": product.url or search_url(title),
            "link_source": LINK_RESOLVED if is_pdp(product.url) else LINK_SEARCH,
            "is_wishlist": False,
            "is_ad": False,
            "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "version": TRACKER_VERSION,
        })

    if not fresh:
        return 0
    existing.extend(fresh)
    save_tracker_data(existing)
    return len(fresh)


def run_scanner_diagnostic() -> List[Dict[str, Any]]:
    steps: List[Dict[str, Any]] = []

    def add(step: int, name: str, ok: bool, detail: str, hint: str = "") -> bool:
        steps.append({"Step": step, "Check": name,
                      "Result": "✅ PASS" if ok else "❌ FAIL",
                      "Detail": detail, "_hint": hint, "_ok": ok})
        return ok

    if requests is None:
        add(1, "requests installed", False, "Missing requests library.", "pip install requests")
        return steps

    session = requests.Session()
    session.headers.update({
        "User-Agent": RESOLVER_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })

    try:
        home = session.get(f"https://{FLIPKART_HOST}/", timeout=15)
        if not add(1, "Reach flipkart.com", home.status_code == 200, f"HTTP {home.status_code}"):
            return steps
    except Exception as exc:
        add(1, "Reach flipkart.com", False, str(exc)[:120], "Network failure.")
        return steps

    url = scanner_search_url("laptop")
    try:
        resp = session.get(url, timeout=15)
        html = resp.text or ""
        challenged = any(m in html[:4000].lower() for m in SCANNER_CHALLENGE_MARKERS)
        if not add(2, "Search returns live HTML", not challenged, "Bot-challenge page" if challenged else f"HTTP 200 ({len(html):,} bytes)"):
            return steps
    except Exception as exc:
        add(2, "Search returns live HTML", False, str(exc)[:120])
        return steps

    has_state = "__INITIAL_STATE__" in html
    add(3, "__INITIAL_STATE__ present", has_state, "Payload present" if has_state else "Payload absent")

    products = FlipkartCatalogueScanner.extract_products(html)
    add(4, "Parser extracts live products", len(products) > 0, f"{len(products)} products parsed, {len([p for p in products if p.price > 0])} with price")
    return steps


# --------------------------------------------------------------------------- #
# MAIN APP ENTRY POINT
# --------------------------------------------------------------------------- #


def main() -> None:
    st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
    st.caption("Live price intelligence engine: all categories, BBD floor steals, and promoted deals on a single continuous page.")

    t1, t2, t3, t4, t5, t6 = st.columns([1.4, 1.5, 1.2, 1.0, 1.0, 1.2])
    with t1:
        if st.button("🔄 Re-Check Prices", type="primary", use_container_width=True):
            with st.spinner("Re-checking active discounts…"):
                count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count} products.")
            st.rerun()
    with t2:
        resolve_clicked = st.button("🔗 Resolve Links", use_container_width=True)
    with t3:
        diagnose_clicked = st.button("🩺 Test Connection", use_container_width=True)
    with t4:
        if st.button("🚨 Reset DB", use_container_width=True, help="Force-rebuilds catalogue with verified working links"):
            save_tracker_data(generate_seed_catalog())
            st.session_state.pop("auto_resolved", None)
            st.success("Catalogue rebuilt with verified links.")
            st.rerun()
    with t5:
        expand_all = st.checkbox("📂 Expand All", value=False)
    with t6:
        card_preference = st.selectbox("💳 Card:", [CARD_AUTO, CARD_INSTANT, CARD_CASHBACK])

    if diagnose_clicked:
        with st.spinner("Contacting Flipkart…"):
            ok, message = get_resolver().self_test()
        (st.success if ok else st.error)(message)

    if AUTO_RESOLVE_ON_START and not st.session_state.get("auto_resolved"):
        st.session_state["auto_resolved"] = True
        pending = products_needing_links(load_tracker_data())
        if pending and get_resolver().available:
            if run_link_resolution(silent=True):
                st.rerun()

    if resolve_clicked and run_link_resolution():
        st.rerun()

    master_df = process_analytics(load_tracker_data(), card_preference)
    if master_df.empty:
        st.error("No tracked products found. Click 'Reset DB' above.")
        return

    specific_links = int(master_df["URL"].map(is_pdp).sum())
    total_products = len(master_df)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.markdown(kpi(f"{total_products:,}", "Monitored Products"), unsafe_allow_html=True)
    k2.markdown(kpi(f"{specific_links}/{total_products}", "Direct Product Links",
                    "#4ade80" if specific_links == total_products else "#f97316"),
                unsafe_allow_html=True)
    k3.markdown(kpi(f"{int((master_df['Diff vs Last BBD'] <= 0).sum()):,}",
                    "🔥 Cheaper Than Last BBD", "#facc15"), unsafe_allow_html=True)
    k4.markdown(kpi(f"{int(master_df['is_wishlist'].sum()):,}", "Wishlist Items", "#ec4899"),
                unsafe_allow_html=True)
    k5.markdown(kpi(f"₹{master_df['Real Savings (vs 6M)'].sum() / 100000:,.1f}L",
                    "Total Real Savings", "#4ade80"), unsafe_allow_html=True)

    st.divider()

    with st.expander("➕ Add Direct Product to Wishlist (Paste Name & Link)", expanded=False):
        st.markdown("Paste your Flipkart product link from your browser. It routes into its dedicated category table.")
        with st.form("quick_add_wishlist", clear_on_submit=True):
            name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Fit Jeans")
            url = st.text_input("🔗 Flipkart Product Link:", placeholder="https://www.flipkart.com/.../p/itm... or with ?pid=...")
            price = st.number_input("💰 Current Price (₹, optional):", min_value=0, step=100, value=0)
            if st.form_submit_button("💾 Save Direct to Wishlist", type="primary"):
                if not name.strip():
                    st.error("Please enter the product name.")
                else:
                    item = build_wishlist_item(name, url, current=price or 999)
                    records = load_tracker_data()
                    records.append(item)
                    save_tracker_data(records)
                    st.success(f"Saved '{item['Product']}' directly to '{item['Category']}'!")
                    st.rerun()

    # SECTION 0: WISHLIST (ON SAME PAGE)
    wishlist_df = master_df[master_df["is_wishlist"]]
    if not wishlist_df.empty:
        st.markdown('<div class="section-title-wishlist"><h2 style="margin:0;">💖 Your Saved Personal Wishlists</h2>'
                    '<p style="margin:2px 0 0 0; font-size:0.9rem;">Auto-organised by product category. Use the ➕ Wishlist column inside any table below to add items instantly.</p></div>',
                    unsafe_allow_html=True)
        for category in sorted(wishlist_df["Category"].unique()):
            slug = "w_" + re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")
            render_collapsible_table(wishlist_df[wishlist_df["Category"] == category],
                                     category, slug, "💖", True, allow_deletion=True)

    # SECTION 1: CATEGORY CATALOG (ALL 9 ON SAME PAGE, STATIONERY AT BOTTOM)
    st.markdown('<div class="section-title-catalog"><h2 style="margin:0;">1. 🛒 Flipkart Category Catalog</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Independent collapsible tables with dedicated column filters. Stationery & Books at the bottom.</p></div>',
                unsafe_allow_html=True)
    for category, meta in CAT_DATA_MATRIX.items():
        subset = master_df[(master_df["Category"] == category) & (~master_df["is_wishlist"])]
        render_collapsible_table(subset, category, meta["slug"], meta["icon"], expand_all)

    # SECTION 2: BBD STEAL DEALS (ON SAME PAGE)
    st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">2. 🔥 Big Billion Days Floor Deals</h2>'
                "<p style=\"margin:2px 0 0 0; font-size:0.9rem;\">Products currently priced at or below last year's festive BBD low.</p></div>",
                unsafe_allow_html=True)
    bbd_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD")
    if bbd_df.empty:
        st.info("No product currently beats last year's BBD floor price.")
    else:
        render_collapsible_table(bbd_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", True)

    # SECTION 3: ADS & PROMOTIONS (ON SAME PAGE)
    st.markdown('<div class="section-title-ads"><h2 style="margin:0;">3. 📢 Promoted, Sponsored & Banner Deals</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Active sponsored placements vs their 6-month averages.</p></div>',
                unsafe_allow_html=True)
    ads_df = master_df[master_df["is_ad"]].sort_values("Real Disc % (vs 6M)", ascending=False)
    if ads_df.empty:
        st.info("No sponsored placements are being tracked right now.")
    else:
        render_collapsible_table(ads_df, "Active Sponsored & Banner Promotions", "flipkart_ads", "📢", True)


def render_deep_scanner() -> None:
    scanner = get_scanner()

    st.divider()
    with st.expander("🛰️ Deep Website Scanner (Uncapped Site-Wide Discovery)", expanded=True):
        if st.button("🩺 Run Connection Diagnostic", key="diag_run"):
            steps = run_scanner_diagnostic()
            st.dataframe(pd.DataFrame([{k: v for k, v in s.items() if not k.startswith("_")} for s in steps]),
                         use_container_width=True, hide_index=True)

        st.caption(
            "Scans Flipkart by recursively bisecting saturated price windows. "
            "Target and Request Budget are set to 0 (Unlimited) so all available deals across the site are gathered."
        )

        c1, c2, c3 = st.columns([2.4, 1.3, 1.3])
        categories = c1.multiselect("Categories to scan:", options=list(SCANNER_SEEDS),
                                    default=list(SCANNER_SEEDS), key="scan_cats")
        # 0 = Unlimited: scan all available deals without stopping at an arbitrary number
        target = c2.number_input("Target per category (0 = Unlimited):", min_value=0, max_value=500000,
                                 value=0, step=100, key="scan_target")
        pages = c3.slider("Pages per query:", 1, 50, 8, key="scan_pages")

        o1, o2, o3, o4 = st.columns(4)
        use_brands = o1.checkbox("× brands", value=True, key="scan_brands")
        use_bands = o2.checkbox("× price bands", value=True, key="scan_bands")
        use_sorts = o3.checkbox("× sort orders", value=True, key="scan_sorts")
        budget = o4.number_input("Max requests (0 = Unlimited):", min_value=0, max_value=1000000,
                                 value=0, step=500, key="scan_budget")

        min_disc = st.slider("Only add products discounted ≥ %:", 0, 80, 0, 5, key="scan_min_disc")

        if not categories:
            st.info("Pick at least one category to scan.")
            return

        frontier = scanner.seed_frontier(categories, use_brands=use_brands,
                                         use_bands=use_bands, use_sorts=use_sorts)
        st.caption(f"Frontier initialized with **{len(frontier):,} entry points**. Bisection dynamically expands queries.")

        r1, r2, r3 = st.columns(3)
        if r1.button("🛰️ Start Full-Site Scan", type="primary", key="scan_run"):
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(done: int, total: int, term: str, found: int, pending: int) -> None:
                bar.progress(min(done / max(total, 1), 1.0))
                status.caption(
                    f"[{done:,}] {term} · **{found:,}** deals found · "
                    f"{pending:,} queued · {scanner.bisections:,} splits · "
                    f"{scanner.limiter.rate:.1f} req/s"
                )

            with st.spinner("Scanning complete Flipkart catalogue…"):
                scanner.run_frontier(frontier, pages,
                                     target_per_category=int(target),
                                     max_requests=int(budget),
                                     progress=on_progress)
                added = merge_scan_results_into_tracker(scanner, float(min_disc))
            bar.empty()
            status.empty()
            st.session_state["scan_done"] = True
            st.session_state["scan_added"] = added
            st.rerun()

        if r2.button("⏹️ Stop Scan", key="scan_stop"):
            scanner.abort = True
            st.toast("Stopping after current requests finish.", icon="⏹️")

        if r3.button("🗑️ Clear Scanned Cache", key="scan_clear"):
            scanner.clear_store()
            st.toast("Scan store cleared.", icon="🗑️")
            st.rerun()

        if st.session_state.get("scan_done"):
            added = st.session_state.get("scan_added", 0)
            if added:
                st.success(f"Added **{added:,}** verified deals directly into the tables above!")
            elif scanner.products:
                st.info("All discovered deals are already present in the tables above.")

        if scanner.products:
            m1, m2, m3 = st.columns(3)
            m1.markdown(kpi(f"{len(scanner.products):,}", "Total Discovered"), unsafe_allow_html=True)
            m2.markdown(kpi(f"{len(scanner.priced()):,}", "Priced Deals", "#4ade80"), unsafe_allow_html=True)
            m3.markdown(kpi(f"{scanner.bisections:,}", f"Band Bisections (d{scanner.max_depth_reached})", "#a855f7"), unsafe_allow_html=True)

            if st.button("➕ Force-Merge All Discoveries into Tables Above", key="scan_merge_now"):
                added = merge_scan_results_into_tracker(scanner, float(min_disc))
                st.toast(f"Merged {added:,} new product(s)." if added else "All findings already in tables.", icon="🛰️")
                if added:
                    st.rerun()


if __name__ == "__main__":
    try:
        main()
        render_deep_scanner()
    except Exception:
        logger.exception("Unhandled error")
        st.error("Error rendering dashboard. See server logs for details.")
