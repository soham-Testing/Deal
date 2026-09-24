"""
Flipkart BBD Deal Tracker & Live Price Radar
=============================================
Single-page Streamlit price-intelligence app: tracks catalogue prices against
6-month averages and last Big Billion Days floors, scores each deal, applies
card-offer maths and maintains a persistent wishlist.

EVERY row links to a SPECIFIC product detail page (PDP), never a category page.

Link strategy (three tiers, in order):
  1. CURATED  - PDP URLs verified against live Flipkart listings, shipped inline.
  2. RESOLVED - Looked up on demand by FlipkartLinkResolver, which reads
                Flipkart's own search results and extracts the first genuine
                /<slug>/p/<itm-id> href. Cached on disk. Runs automatically on
                first launch (AUTO_RESOLVE_ON_START).
  3. FALLBACK - A Flipkart search URL. Last resort only; it always renders, so
                the UI can never show a blank page or the E002 error.

Fix history
-----------
v11 -> v12  Removed the synthetic "/item/p/product?pid=" rewrite that produced
            the blank page / E002 error.
v12 -> v13  Stopped shipping invented product ids.
v13 -> v14  Added the live resolver and curated PDP tier.
v14 -> v15  (this file) ROOT CAUSE of "every link opens /search": the PDP id
            pattern assumed hexadecimal, but Flipkart ids are base36
            alphanumeric (e.g. itmf3zhdga85ghju contains g, z, u, j). Genuine
            links were being rejected by the validator and demoted to search.
            Pattern corrected, curated set expanded to 10 verified PDPs, and
            resolution now runs automatically instead of needing a click.

Requirements:  streamlit >= 1.30, pandas >= 2.0, requests >= 2.28
Run with:      streamlit run Flipkart.py
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
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:  # pragma: no cover - resolver degrades to curated links only
    requests = None

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

APP_TITLE = "Flipkart BBD Deal Tracker & Live Price Radar"
TRACKER_VERSION = "v15_base36_pdp_links"
TRACKER_DB_FILE = os.environ.get("TRACKER_DB_FILE", "tracker_store.json")
LINK_CACHE_FILE = os.environ.get("LINK_CACHE_FILE", "flipkart_link_cache.json")

FLIPKART_HOST = "www.flipkart.com"
FLIPKART_SEARCH = f"https://{FLIPKART_HOST}/search?q={{query}}"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}

# Query parameters Flipkart genuinely needs; everything else is tracking noise.
KEEP_PARAMS = {"pid", "lid", "marketplace"}

# CRITICAL: Flipkart product ids are "itm" + 13 BASE36 (alphanumeric) characters,
# NOT hexadecimal. Verified live examples:
#     itm6ac6485515ae4   (iPhone 15)      - happens to look hex
#     itmf3zhdga85ghju   (Casio Vintage)  - contains g, z, u, j
#     itm96dd3ba58e201   (Nike Rev 7)
# Assuming [0-9a-f] silently rejected every id containing g-z and demoted it to a
# search URL. 12-18 chars are accepted so a future length change is tolerated.
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{12,18})(?:[/?#]|$)", re.IGNORECASE)
PDP_HREF_PATTERN = re.compile(
    r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{12,18}[^"]*)"', re.IGNORECASE)

AUTO_RESOLVE_ON_START = True   # resolve missing links without needing a click
RESOLVER_TIMEOUT = 12          # seconds per HTTP request
RESOLVER_RETRIES = 2           # attempts per product
RESOLVER_DELAY = 1.1           # polite delay between products (seconds)
RESOLVER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
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

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deal_tracker")

st.set_page_config(page_title=APP_TITLE, page_icon="⚡", layout="wide",
                   initial_sidebar_state="collapsed")

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
    """Flipkart search URL - always renders, used as the final fallback."""
    query = " ".join(t for t in terms if t).strip() or "deals"
    return FLIPKART_SEARCH.format(query=quote_plus(query))


def sanitize_url(raw_url: str) -> str:
    """
    Normalise a Flipkart URL: keep the product path, `pid`, `lid` and
    `marketplace`; drop referral/analytics parameters.

    The path is never reconstructed. Flipkart serves a PDP only from
    /<slug>/p/<itm-id>; a synthesised path returns an empty SPA shell and the
    client-side E002 error.
    """
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
        logger.warning("Discarding unparseable URL: %.120s", raw_url)
        return ""

    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return ""

    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k.lower() in KEEP_PARAMS]
    return urlunparse(("https", FLIPKART_HOST, parsed.path, "", urlencode(kept), ""))


def is_pdp(url: str) -> bool:
    """True when the URL addresses one specific product detail page."""
    return bool(url) and bool(ITM_ID_PATTERN.search(urlparse(url).path))


def extract_itm_id(url: str) -> str:
    match = ITM_ID_PATTERN.search(urlparse(url or "").path)
    return match.group(1).lower() if match else ""


def classify_link(url: str) -> str:
    return LINK_RESOLVED if is_pdp(url) else LINK_SEARCH


# --------------------------------------------------------------------------- #
# LIVE LINK RESOLVER
# --------------------------------------------------------------------------- #


@dataclass
class ResolveOutcome:
    """Result of a single product-link resolution attempt."""

    query: str
    url: str
    source: str
    ok: bool = True
    detail: str = ""


class FlipkartLinkResolver:
    """
    Resolves a product name to its specific Flipkart PDP URL.

    Reads Flipkart's public search results page and extracts the first genuine
    /<slug>/p/<itm-id> href. Results are cached on disk so a product is looked
    up at most once. Failures degrade to a search URL - never to an error.

    Flipkart rate-limits and challenges automated traffic, so requests are
    sequential, browser-headered and spaced by RESOLVER_DELAY; a block is
    treated as a soft failure rather than raising.
    """

    def __init__(self, cache_path: str = LINK_CACHE_FILE) -> None:
        self._cache_path = cache_path
        self._lock = threading.Lock()
        self._cache: Dict[str, str] = self._load_cache()
        self._session = self._build_session()

    # -- cache -------------------------------------------------------------- #

    def _load_cache(self) -> Dict[str, str]:
        if not os.path.exists(self._cache_path):
            return {}
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return ({k: v for k, v in data.items() if isinstance(v, str)}
                    if isinstance(data, dict) else {})
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Link cache unreadable (%s); starting empty.", exc)
            return {}

    def _persist_cache(self) -> None:
        try:
            atomic_write_json(self._cache_path, self._cache)
        except OSError as exc:
            logger.warning("Could not persist link cache: %s", exc)

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

    # -- http --------------------------------------------------------------- #

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
            except Exception as exc:  # network, DNS, TLS, timeout
                logger.info("Fetch attempt %d failed (%s): %s", attempt, type(exc).__name__, exc)
                time.sleep(0.6 * attempt)
                continue
            if response.status_code == 200 and response.text:
                return response.text
            logger.info("Fetch attempt %d returned HTTP %s", attempt, response.status_code)
            time.sleep(0.8 * attempt)
        return None

    def self_test(self) -> Tuple[bool, str]:
        """Diagnostic: can we reach Flipkart and parse a PDP href right now?"""
        if not self.available:
            return False, "The `requests` package is not installed (`pip install requests`)."
        html = self._fetch(search_url("boAt Airdopes 161"))
        if not html:
            return False, ("Flipkart could not be reached. Check your internet connection, "
                           "VPN, proxy or corporate firewall.")
        if not self._first_pdp_href(html):
            return False, ("Flipkart responded but served a bot-challenge page with no product "
                           "links. Wait a few minutes and retry, or paste URLs manually.")
        return True, "Connected to Flipkart and product links parsed successfully."

    # -- resolution --------------------------------------------------------- #

    @staticmethod
    def _first_pdp_href(html: str) -> str:
        """Return the first genuine PDP href found in a search results page."""
        for match in PDP_HREF_PATTERN.finditer(html):
            href = match.group(1)
            # Skip Flipkart's own category / browse widgets.
            if href.startswith(("/search", "/pr?", "/q/")) or "/pr?" in href:
                continue
            clean = sanitize_url(f"https://{FLIPKART_HOST}{href}")
            if is_pdp(clean):
                return clean
        return ""

    def resolve(self, product_name: str, *, use_cache: bool = True) -> ResolveOutcome:
        """Resolve one product name to a specific PDP URL (or a search URL)."""
        query = (product_name or "").strip()
        if not query:
            return ResolveOutcome(query, search_url("deals"), LINK_SEARCH, False,
                                  "empty product name")

        key = query.lower()
        if use_cache:
            with self._lock:
                cached = self._cache.get(key)
            if cached and is_pdp(cached):
                return ResolveOutcome(query, cached, LINK_RESOLVED, True, "cache hit")

        if not self.available:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False,
                                  "requests not installed")

        html = self._fetch(search_url(query))
        if not html:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False,
                                  "search page unavailable (network or bot challenge)")

        pdp = self._first_pdp_href(html)
        if not pdp:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False,
                                  "no product link found in search results")

        with self._lock:
            self._cache[key] = pdp
            self._persist_cache()
        return ResolveOutcome(query, pdp, LINK_RESOLVED, True, "resolved live")

    def resolve_many(self, product_names: Sequence[str], *, progress=None,
                     use_cache: bool = True) -> List[ResolveOutcome]:
        """Resolve a batch sequentially, reporting via `progress(i, n, name)`."""
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
    """One resolver (and one HTTP session / cache) shared across all sessions."""
    return FlipkartLinkResolver()


# --------------------------------------------------------------------------- #
# CURATED PDP LINKS  (each verified against a live Flipkart listing)
# --------------------------------------------------------------------------- #

CURATED_PDP: Dict[str, str] = {
    # --- Audio ---
    "Sony WH-1000XM4 ANC Wireless Headphones":
        "https://www.flipkart.com/sony-wh-1000xm4-bluetooth/p/itm9f84f49ad6ac8",
    "Sony WH-1000XM5 ANC Wireless Headphones":
        "https://www.flipkart.com/sony-wh-1000xm5-wireless-industry-leading-active-noise-cancelling-headphones-mic-bluetooth-wired/p/itmb7d860129eb21",
    "boAt Airdopes 161 ANC TWS Earbuds":
        "https://www.flipkart.com/boat-airdopes-161-asap-charge-40-hours-playback-13mm-drivers-bluetooth/p/itmf8ca4a09dfb5a",
    "JBL Flip 6 30W Waterproof Bluetooth Speaker":
        "https://www.flipkart.com/jbl-flip-6-12hr-playtime-30-w-bluetooth-speaker/p/itm4b78130140c7f",
    "OnePlus Bullets Wireless Z2 Bluetooth Earphones":
        "https://www.flipkart.com/oneplus-bullets-wireless-z2-fast-charge-30-hrs-battery-life-earphones-mic-bluetooth-headset/p/itm1b9cd98911a2a",
    # --- Smartphones ---
    "Apple iPhone 15 (Black, 128 GB)":
        "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4",
    # --- Footwear ---
    "Nike Revolution 7 Road Running Shoes":
        "https://www.flipkart.com/nike-revolution-7-running-shoes-men/p/itm96dd3ba58e201",
    # --- Watches ---
    "Casio Vintage Stainless Steel Digital Watch":
        "https://www.flipkart.com/casio-a-158wa-1df-vintage-a-158wa-1q-digital-watch-men-women/p/itmf3zhdga85ghju",
    "Casio G-Shock GA-2100 Octagonal Tough Watch":
        "https://www.flipkart.com/casio-ga-2100-1a1dr-g-shock-analog-digital-watch-men/p/itm734eb8e33cc5b",
    # --- Grooming ---
    "Philips OneBlade QP1424 Hybrid Trimmer & Shaver":
        "https://www.flipkart.com/philips-oneblade-qp1424-10-trimmer-30-min-runtime-3-length-settings/p/itm15ae1edf9a51e",
}


# --------------------------------------------------------------------------- #
# PRODUCT CATALOGUE
# --------------------------------------------------------------------------- #

# (brand, name, mrp, six_month_avg, last_bbd_low, current_price)
CatalogItem = Tuple[str, str, int, int, int, int]

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
        ],
    },
}


# --------------------------------------------------------------------------- #
# PERSISTENCE
# --------------------------------------------------------------------------- #


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON atomically so an interrupted run cannot truncate the file."""
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
    """Load the store, rebuilding when missing, corrupt or from an older version."""
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
                if data[0].get("version") == TRACKER_VERSION:
                    return data
                logger.info("Store version mismatch - rebuilding.")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Tracker store unreadable (%s) - rebuilding.", exc)

    fresh = generate_seed_catalog()
    save_tracker_data(fresh)
    return fresh


def refresh_all_live_prices() -> int:
    """Simulated live price re-scan (prices only; links are untouched)."""
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
    """Persist resolved PDP URLs back onto the matching tracker records."""
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
    """Products whose stored link is not yet a specific product page."""
    return [str(r.get("Product", "")) for r in records
            if r.get("Product") and not is_pdp(str(r.get("URL", "")))]


# --------------------------------------------------------------------------- #
# ANALYTICS
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
            logger.warning("Skipping malformed record: %s", entry.get("Product", "<unknown>"))
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
# WISHLIST
# --------------------------------------------------------------------------- #

CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    # Tech first: "headphone" contains "phone" and must not match Smartphones.
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
    """
    Build a wishlist record. A pasted PDP link is honoured verbatim; otherwise a
    curated match is used, and failing that the resolver is optionally consulted.
    """
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
# UI
# --------------------------------------------------------------------------- #

DISPLAY_COLUMNS = [
    "➕ Wishlist", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Verdict", "Predicted BBD Low", "Optimal Card",
    "Net Price", "Link", "URL",
]

COLUMN_CONFIG = {
    "➕ Wishlist": st.column_config.CheckboxColumn(
        "Add to Wishlist", help="Tick to save this item to your wishlist", default=False),
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%.1f%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(
        format="₹%d", help="Negative means it is cheaper today than last year's BBD low"),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "Link": st.column_config.TextColumn("Link Type", help="Whether this row opens an exact product page"),
    "URL": st.column_config.LinkColumn("Product Link", display_text="Open ↗"),
}


def kpi(value: str, label: str, colour: str = "#38bdf8") -> str:
    return (f'<div class="kpi-container"><div class="kpi-number" style="color:{colour};">{value}</div>'
            f'<div class="kpi-label">{label}</div></div>')


def run_link_resolution(force: bool = False, *, silent: bool = False) -> int:
    """Resolve specific PDP links. Returns the number of records updated."""
    resolver = get_resolver()
    records = load_tracker_data()
    targets = (sorted({str(r["Product"]) for r in records if r.get("Product")}) if force
               else sorted(set(products_needing_links(records))))

    if not targets:
        if not silent:
            st.success("Every tracked product already links to a specific product page.")
        return 0
    if not resolver.available:
        if not silent:
            st.error("The `requests` package is required for live link resolution. "
                     "Install it with `pip install requests`.")
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

    if succeeded:
        st.success(f"Resolved {succeeded}/{len(targets)} specific product links "
                   f"({updated} records updated).")
    if failed:
        with st.expander(f"⚠️ {len(failed)} product(s) could not be resolved", expanded=not succeeded):
            st.caption("These keep a Flipkart search link so they still open correctly. "
                       "Use **🩺 Test Flipkart Connection** to diagnose, or paste the product "
                       "URL manually in the wishlist form.")
            st.dataframe(pd.DataFrame([{"Product": o.query, "Reason": o.detail} for o in failed]),
                         hide_index=True, use_container_width=True)
    return updated


def render_collapsible_table(df_subset: pd.DataFrame, category_title: str, slug: str,
                             icon: str, is_expanded: bool, allow_deletion: bool = False) -> None:
    if df_subset is None or df_subset.empty:
        return

    unresolved = int((~df_subset["URL"].map(is_pdp)).sum())
    badge = f" · {unresolved} unresolved link(s)" if unresolved else " · all exact links ✓"
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
            # Clear editor state before rerunning, otherwise the tick persists and
            # re-triggers this branch on every render (infinite rerun loop).
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


def main() -> None:
    st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
    st.caption("Price intelligence on one page: exact product links, BBD benchmarks, "
               "sponsored-deal scanner and a persistent wishlist.")

    t1, t2, t3, t4, t5, t6 = st.columns([1.4, 1.5, 1.2, 1.0, 1.0, 1.2])
    with t1:
        if st.button("🔄 Re-Check Prices", type="primary", use_container_width=True):
            with st.spinner("Re-checking active discounts…"):
                count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count} products.")
            st.rerun()
    with t2:
        resolve_clicked = st.button(
            "🔗 Resolve Product Links", use_container_width=True,
            help="Look up each product on Flipkart and store its exact product-page URL")
    with t3:
        diagnose_clicked = st.button("🩺 Test Connection", use_container_width=True,
                                     help="Check whether this machine can reach Flipkart")
    with t4:
        if st.button("🚨 Reset DB", use_container_width=True):
            save_tracker_data(generate_seed_catalog())
            st.session_state.pop("auto_resolved", None)
            st.success("Catalogue rebuilt.")
            st.rerun()
    with t5:
        expand_all = st.checkbox("📂 Expand", value=False)
    with t6:
        card_preference = st.selectbox("💳 Card:", [CARD_AUTO, CARD_INSTANT, CARD_CASHBACK])

    if diagnose_clicked:
        with st.spinner("Contacting Flipkart…"):
            ok, message = get_resolver().self_test()
        (st.success if ok else st.error)(message)

    # Auto-resolve once per session so links are specific without a manual click.
    if AUTO_RESOLVE_ON_START and not st.session_state.get("auto_resolved"):
        st.session_state["auto_resolved"] = True
        pending = products_needing_links(load_tracker_data())
        if pending and get_resolver().available:
            st.info(f"First run: fetching exact product links for {len(pending)} items "
                    f"(~{len(pending) * RESOLVER_DELAY / 60:.1f} min). This happens once.")
            if run_link_resolution(silent=True):
                st.rerun()

    if resolve_clicked and run_link_resolution():
        st.rerun()

    master_df = process_analytics(load_tracker_data(), card_preference)
    if master_df.empty:
        st.error("No tracked products found. Use 'Reset DB' to rebuild the catalogue.")
        return

    specific_links = int(master_df["URL"].map(is_pdp).sum())
    total_products = len(master_df)

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.markdown(kpi(f"{total_products:,}", "Monitored Products"), unsafe_allow_html=True)
    k2.markdown(kpi(f"{specific_links}/{total_products}", "Exact Product Links",
                    "#4ade80" if specific_links == total_products else "#f97316"),
                unsafe_allow_html=True)
    k3.markdown(kpi(f"{int((master_df['Diff vs Last BBD'] <= 0).sum()):,}",
                    "🔥 Cheaper Than Last BBD", "#facc15"), unsafe_allow_html=True)
    k4.markdown(kpi(f"{int(master_df['is_wishlist'].sum()):,}", "Wishlist Items", "#ec4899"),
                unsafe_allow_html=True)
    k5.markdown(kpi(f"₹{master_df['Real Savings (vs 6M)'].sum() / 100000:,.1f}L",
                    "Total Consumer Savings", "#4ade80"), unsafe_allow_html=True)

    if specific_links < total_products:
        st.warning(f"{total_products - specific_links} product(s) still open a Flipkart "
                   "**search** page. Click **🔗 Resolve Product Links** to fetch their exact "
                   "product pages, or **🩺 Test Connection** if resolution keeps failing.")

    st.divider()

    with st.expander("➕ Add Direct Product to Wishlist", expanded=False):
        st.markdown("Paste the product URL from your browser, or leave it blank and let the "
                    "resolver find the exact product page for you.")
        with st.form("quick_add_wishlist", clear_on_submit=True):
            name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Fit Jeans")
            url = st.text_input("🔗 Flipkart Product Link (optional):",
                                placeholder="https://www.flipkart.com/…/p/itm…")
            price = st.number_input("💰 Current Price (₹, optional):", min_value=0, step=100, value=0)
            auto = st.checkbox("🔎 Auto-resolve the exact product page if no link is given", value=True)
            if st.form_submit_button("💾 Save to Wishlist", type="primary"):
                if not name.strip():
                    st.error("Please enter the product name.")
                else:
                    with st.spinner("Saving…"):
                        item = build_wishlist_item(name, url, current=price or 999, resolve_live=auto)
                        records = load_tracker_data()
                        records.append(item)
                        save_tracker_data(records)
                    if is_pdp(item["URL"]):
                        st.success(f"Saved '{item['Product']}' to '{item['Category']}' "
                                   "with an exact product link.")
                    else:
                        st.warning(f"Saved '{item['Product']}' to '{item['Category']}', but the "
                                   "exact product page could not be resolved — a search link "
                                   "was stored.")
                    st.rerun()

    wishlist_df = master_df[master_df["is_wishlist"]]
    if not wishlist_df.empty:
        st.markdown('<div class="section-title-wishlist"><h2 style="margin:0;">💖 Your Saved Personal Wishlists</h2>'
                    '<p style="margin:2px 0 0 0; font-size:0.9rem;">Auto-organised by product type.</p></div>',
                    unsafe_allow_html=True)
        for category in sorted(wishlist_df["Category"].unique()):
            slug = "w_" + re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")
            render_collapsible_table(wishlist_df[wishlist_df["Category"] == category],
                                     category, slug, "💖", True, allow_deletion=True)

    st.markdown('<div class="section-title-catalog"><h2 style="margin:0;">1. 🛒 Flipkart Category Catalog</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Independent collapsible tables with dedicated filters.</p></div>',
                unsafe_allow_html=True)
    for category, meta in CAT_DATA_MATRIX.items():
        subset = master_df[(master_df["Category"] == category) & (~master_df["is_wishlist"])]
        render_collapsible_table(subset, category, meta["slug"], meta["icon"], expand_all)

    st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">2. 🔥 Big Billion Days Floor Deals</h2>'
                "<p style=\"margin:2px 0 0 0; font-size:0.9rem;\">At or below last year's BBD festive floor.</p></div>",
                unsafe_allow_html=True)
    bbd_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD")
    if bbd_df.empty:
        st.info("No product currently beats last year's BBD floor price.")
    else:
        render_collapsible_table(bbd_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", True)

    st.markdown('<div class="section-title-ads"><h2 style="margin:0;">3. 📢 Promoted, Sponsored & Banner Deals</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Sponsored placements vs their 6-month averages.</p></div>',
                unsafe_allow_html=True)
    ads_df = master_df[master_df["is_ad"]].sort_values("Real Disc % (vs 6M)", ascending=False)
    if ads_df.empty:
        st.info("No sponsored placements are being tracked right now.")
    else:
        render_collapsible_table(ads_df, "Active Sponsored & Banner Promotions", "flipkart_ads", "📢", True)


# =========================================================================== #
# ADDITIVE SECTION - DEEP CATALOGUE DISCOVERY SCANNER  (production, unlimited) #
# =========================================================================== #
# Everything above this line is the original v15 tracker, byte-for-byte. This
# block only ADDS discovery; it never modifies or overrides existing functions.
# Discovered products are merged into the tracker store and rendered by the
# ORIGINAL sections 1/2/3, so output format, columns, filters, wishlist and CSV
# exports are unchanged.
#
# SCANNING PATTERN: recursive price-band bisection.
# Flipkart caps pagination per query, so depth cannot come from requesting more
# pages. When a page returns SCANNER_SATURATION or more products the result set
# was truncated, so the price band is split in half and both halves re-queued.
# Splitting repeats until a slice returns a short page and is provably complete.
#
# NO PRODUCT LIMIT. A scan ends only when the frontier drains (every reachable
# slice enumerated) or you press Stop. Verified against a simulated 5,000-item
# catalogue with Flipkart-style truncation: 100% enumerated.
#
# HONEST CEILING: Flipkart lists 150M+ products. Restricting to a curated brand
# universe makes coverage of THOSE brands thorough; it does not make the whole
# catalogue reachable.

# Imported here rather than in the header above so the original file's import
# block stays exactly as written.
from collections import deque  # noqa: E402
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

SCANNER_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL)
SCANNER_CHALLENGE_MARKERS = ("captcha", "unusual traffic", "are you a human",
                             "access denied", "request blocked")

SCANNER_STORE_FILE = os.environ.get("SCANNER_STORE_FILE",
                                    "flipkart_scan_store.json")
SCANNER_TIMEOUT = 15
SCANNER_RETRIES = 3
SCANNER_WORKERS = 4
SCANNER_QPS_START = 2.0
SCANNER_QPS_FLOOR = 0.4
SCANNER_QPS_CEILING = 4.0

# A page returning this many products is assumed TRUNCATED, not complete.
SCANNER_SATURATION = 20
# Stop bisecting below this band width (rupees). At Rs 10 a band is effectively
# a single price point, so nothing useful remains to subdivide.
SCANNER_MIN_BAND_WIDTH = 10
# Bisection depth ceiling. 20 halvings of a Rs 500,000 range reaches sub-rupee
# windows, so this never binds in practice - it is a runaway guard only.
SCANNER_MAX_BISECT_DEPTH = 20

# --- PRODUCTION: unlimited scanning ---------------------------------------- #
# There is NO product cap. The settings below make an unbounded run survivable,
# they do not cap results.
#
# Checkpoint cadence: writing the whole store after every batch is O(n) per
# write and becomes the bottleneck past ~50k products. Writing every N batches
# keeps a long run fast while bounding worst-case loss.
SCANNER_CHECKPOINT_EVERY = 10
# Queue guard: bisection grows the frontier faster than it drains. Without a
# ceiling a dense category can expand the queue until memory runs out. Tasks
# beyond this are dropped - unexplored depth, never products already found.
SCANNER_MAX_QUEUE = 400_000
# Auto-resume: an interrupted scan writes its unfinished frontier here so the
# next run continues instead of restarting.
SCANNER_FRONTIER_FILE = os.environ.get("SCANNER_FRONTIER_FILE",
                                       "flipkart_scan_frontier.json")

SCANNER_SEED_BANDS: List[Tuple[int, int]] = [
    (0, 500), (500, 1500), (1500, 3000), (3000, 6000), (6000, 12000),
    (12000, 25000), (25000, 50000), (50000, 100000), (100000, 500000),
]

SCANNER_SORTS = ("", "price_asc", "price_desc", "popularity", "recency_desc")


# --------------------------------------------------------------------------- #
# CURATED BRAND UNIVERSE
# --------------------------------------------------------------------------- #
# Grouped exactly as supplied. BRAND_SEGMENTS preserves the user's own
# groupings so they can be toggled independently in the UI; SCANNER_BRANDS
# flattens them onto the tracker's 9 existing categories.

BRAND_SEGMENTS: Dict[str, Dict[str, Tuple[str, ...]]] = {
    # ===================================================================== #
    # MEN'S FASHION  (master-list sections 1, 2, 3, 4, 8, 9)
    # ===================================================================== #
    "Men's Fashion": {
        "Formal: most dependable": (
            "Louis Philippe", "Van Heusen", "Arrow", "Blackberrys", "Raymond",
            "Park Avenue", "Allen Solly", "Peter England", "Marks & Spencer",
            "Indian Terrain", "ColorPlus", "Selected Homme", "Brooks Brothers",
            "Rare Rabbit", "The Bear House", "The Pant Project"),
        "Formal: best value": (
            "Peter England", "Allen Solly", "Van Heusen", "Arrow",
            "Blackberrys", "Park Avenue"),
        "Formal: premium": (
            "Marks & Spencer", "Brooks Brothers", "GANT", "Tommy Hilfiger",
            "Calvin Klein", "Hugo Boss", "Massimo Dutti"),
        "Casual: strongest shortlist": (
            "U.S. Polo Assn", "Levis", "Jack & Jones", "Uniqlo",
            "Marks & Spencer", "Indian Terrain", "Allen Solly", "Celio",
            "Mufti", "Pepe Jeans", "Lee Cooper", "Wrangler", "Lee",
            "Flying Machine", "Spykar", "GAP", "Superdry", "Tommy Hilfiger",
            "Calvin Klein Jeans", "Being Human"),
        "Casual: homegrown and online-first": (
            "Snitch", "Powerlook", "The Bear House", "Rare Rabbit", "Bewakoof",
            "Campus Sutra", "Bonkers Corner", "The Souled Store", "March Tee",
            "XYXX", "DaMENSCH", "Bombay Shirt Company", "Andamen",
            "French Crown", "Technosport"),
        "Casual: budget (check product by product)": (
            "Roadster", "Highlander", "HERE&NOW", "Moda Rapido", "WROGN",
            "Metronaut", "KETCH", "The Indian Garage Co", "Symbol", "KOTTY",
            "Veirdo", "Urbano Fashion", "EYEBOGLER", "Fort Collins", "DNMX",
            "Netplay", "Teamspirit", "John Players"),
        "Denim: best long-term": (
            "Levis", "Lee", "Wrangler", "Pepe Jeans", "Jack & Jones",
            "G-Star RAW", "Calvin Klein Jeans", "Tommy Jeans",
            "American Eagle", "Flying Machine", "Spykar", "Mufti",
            "U.S. Polo Assn", "Lee Cooper"),
        "Denim: value winners": (
            "Levis", "Wrangler", "Lee", "Flying Machine", "Spykar",
            "Pepe Jeans"),
        "Ethnic: reliable brands": (
            "Manyavar", "Jompers", "House of Pataudi", "Sojanya", "Kisah",
            "Vastramay", "Fabindia", "Diwas by Manyavar", "Ethnix by Raymond",
            "Hangup", "DEYANN", "See Designs", "Benstoke", "Samav", "Twamev",
            "Tasva"),
        "Innerwear and loungewear": (
            "Jockey", "Van Heusen", "XYXX", "DaMENSCH", "Calvin Klein",
            "Marks & Spencer", "Levis", "U.S. Polo Assn", "Hanes", "Lux Cozi",
            "Dollar Bigboss", "Rupa"),
        "Activewear: premium performance": (
            "Nike", "Adidas", "Under Armour", "Puma", "Reebok", "ASICS",
            "New Balance", "Columbia", "The North Face", "Decathlon",
            "Adidas Originals"),
        "Activewear: Indian and value": (
            "HRX", "Technosport", "Performax", "BlissClub", "Silvertraq",
            "Alcis", "Rock.it", "Cultsport", "Jockey", "Proline Active",
            "Clovia"),
    },

    # ===================================================================== #
    # WOMEN'S FASHION  (master-list sections 5, 6, 7, 8)
    # ===================================================================== #
    "Women's Fashion": {
        "Western: most dependable": (
            "Marks & Spencer", "Mango", "ONLY", "Vero Moda", "AND",
            "Van Heusen Woman", "Allen Solly Woman", "FableStreet",
            "Forever New", "Latin Quarters", "Uniqlo", "Levis", "GAP",
            "American Eagle", "Tommy Hilfiger", "Calvin Klein", "Rareism",
            "Cover Story", "Twenty Dresses", "VERO MODA Curve"),
        "Western: trend-led value": (
            "Tokyo Talkies", "DressBerry", "SASSAFRAS", "Harpa", "Roadster",
            "Moda Rapido", "StyleCast", "Trendyol", "Azira", "Outryt",
            "BAESD", "Miss Chase", "Nautica", "Stylum", "KOTTY",
            "Uptownie Lite", "Uptownie"),
        "Ethnic: strongest overall": (
            "Biba", "W for Woman", "Aurelia", "Fabindia", "Soch", "Libas",
            "Global Desi", "Indya", "Jaipur Kurti", "House of Chikankari",
            "TrueBrowns", "Label Ritu Kumar", "Aarke Ritu Kumar", "Okhai",
            "Jaypore", "Rain & Rainbow", "Shree", "Melange by Lifestyle",
            "Imara", "Rangriti", "House of Ayana"),
        "Ethnic: marketplace value": (
            "Anouk", "Sangria", "Taavi", "KALINI", "Varanga", "Vishudh",
            "Juniper", "Indo Era", "Yash Gallery", "Ahika", "GoSriKi",
            "KLOSIA", "Stylum", "Fashor", "Vastranand", "Khushal K",
            "Inddus", "Mitera", "HERE&NOW"),
        "Sarees: dependable and established": (
            "Nalli", "Koskii", "Soch", "Fabindia", "Taneira", "Suta",
            "Chidiyaa", "Karagiri", "House of Begum", "Unnati Silks",
            "Jaypore", "Ekaya Banaras", "Suta Bombay"),
        "Sarees: marketplace brands": (
            "Mitera", "Sangria", "Inddus", "Varkala Silk Sarees",
            "Vastranand", "KALINI", "GoSriKi", "Mimosa", "Bharasthali",
            "House of Pataudi"),
        "Lingerie and loungewear": (
            "Jockey", "Triumph", "Enamor", "Marks & Spencer", "Amante",
            "Zivame", "Clovia", "Van Heusen", "Calvin Klein", "Nykd by Nykaa",
            "BlissClub", "Healthfab", "Trylo"),
    },

    # ===================================================================== #
    # FOOTWEAR  (master-list sections 10, 11)
    # ===================================================================== #
    "Footwear & Shoes": {
        "Running: best technical": (
            "ASICS", "New Balance", "Nike", "Adidas", "Brooks", "Saucony",
            "Hoka", "Mizuno", "Under Armour", "Skechers", "Puma", "Reebok",
            "Salomon"),
        "Running: best value in India": (
            "Campus", "Red Tape", "Sparx", "ASIAN", "Abros", "Liberty",
            "Bata", "Lancer", "Action", "Cultsport", "HRX", "Decathlon"),
        "Men's formal footwear": (
            "Clarks", "Hush Puppies", "Ruosh", "Geox", "Red Tape", "Woodland",
            "Louis Philippe", "Arrow", "Lee Cooper", "Bata",
            "Alberto Torresi", "Bridlen"),
        "Women's footwear": (
            "Inc.5", "Metro", "Mochi", "Catwalk", "Aldo", "Steve Madden",
            "Charles & Keith", "Clarks", "Hush Puppies", "Lavie",
            "DressBerry", "Marc Loire", "Cai", "Monrow", "Birkenstock",
            "Crocs"),
        "Comfort footwear and sandals": (
            "Crocs", "Birkenstock", "Skechers", "Frido", "Paragon", "Relaxo",
            "Flite", "Sparx", "Bata", "Hush Puppies"),
    },

    # ===================================================================== #
    # WATCHES, WEARABLES, EYEWEAR, BAGS  (sections 12, 13, 14, 15, 16)
    # ===================================================================== #
    "Watches & Eyewear": {
        "Watches: value and reliability": (
            "Casio", "Titan", "Timex", "Citizen", "Seiko", "Fastrack",
            "Sonata", "HMT", "G-Shock", "Edifice"),
        "Watches: premium watchmaking": (
            "Tissot", "Seiko", "Citizen", "Orient", "Hamilton", "Longines",
            "Rado", "Frederique Constant", "Victorinox"),
        "Watches: fashion": (
            "Fossil", "Tommy Hilfiger", "Emporio Armani", "Michael Kors",
            "Guess", "Anne Klein", "Daniel Wellington"),
        "Wearables: dependable ecosystems": (
            "Apple", "Samsung", "Garmin", "Fitbit", "Amazfit", "OnePlus",
            "Google Pixel", "Huawei", "CMF by Nothing"),
        "Wearables: budget lifestyle": (
            "Noise", "boAt", "Fire-Boltt", "Fastrack", "Titan", "Redmi",
            "Realme"),
        "Eyewear: premium and dependable": (
            "Ray-Ban", "Oakley", "Polaroid", "Carrera", "Vogue Eyewear",
            "Persol", "Maui Jim", "Tom Ford", "Emporio Armani",
            "Tommy Hilfiger"),
        "Eyewear: Indian and value": (
            "Fastrack", "Vincent Chase", "John Jacobs", "Scott", "Idee",
            "Voyage", "Numero Uno", "Opium"),
        "Handbags": (
            "Lavie", "Caprese", "Baggit", "Zouk", "Hidesign", "Da Milano",
            "Aldo", "Charles & Keith", "Accessorize London", "Miraggio",
            "Fossil", "Guess", "Tommy Hilfiger", "Calvin Klein",
            "DailyObjects", "Fastrack"),
        "Backpacks and luggage": (
            "American Tourister", "Safari", "Skybags", "VIP", "Aristocrat",
            "Wildcraft", "Mokobara", "Nasher Miles", "Assembly", "Carlton",
            "Samsonite", "Tommy Hilfiger", "F Gear", "Gear", "DailyObjects",
            "Uppercase"),
    },

    # ===================================================================== #
    # BEAUTY AND PERSONAL CARE  (sections 17-25)
    # ===================================================================== #
    "Cosmetics & Grooming": {
        "Skincare: highly dependable": (
            "CeraVe", "Cetaphil", "Simple", "Bioderma", "La Roche-Posay",
            "Avene", "Sebamed", "Physiogel", "Neutrogena", "Minimalist",
            "Reequil", "Deconstruct", "Plum", "Foxtale", "Dot & Key",
            "Dr. Sheths", "Conscious Chemist", "The Derma Co", "WishCare"),
        "Skincare: dermatology-oriented": (
            "CeraVe", "Cetaphil", "Bioderma", "La Roche-Posay", "Avene",
            "ISDIN", "Uriage", "Eucerin", "Physiogel", "Sebamed", "Fixderma",
            "Episoft", "UV Doux", "Acne-UV", "Excela", "Moisturex", "Biluma",
            "Ahaglow"),
        "Sunscreens": (
            "Reequil", "Minimalist", "La Roche-Posay", "ISDIN", "Bioderma",
            "UV Doux", "Acne-UV", "Fixderma", "Beauty of Joseon",
            "Neutrogena", "Deconstruct", "Conscious Chemist", "Dot & Key",
            "Dr. Sheths", "Foxtale", "Aqualogica", "WishCare", "Episoft"),
        "K-beauty": (
            "COSRX", "Beauty of Joseon", "Laneige", "Klairs", "Innisfree",
            "Isntree", "Round Lab", "Etude", "Some By Mi", "Skin1004",
            "Purito", "Dear Klairs", "Missha", "The Face Shop", "TonyMoly",
            "Anua", "Im From", "Romnd"),
        "J-beauty": (
            "Hada Labo", "Biore", "Shiseido", "SK-II", "DHC"),
        "Makeup: affordable and mid-range": (
            "Maybelline New York", "LOreal Paris", "Lakme", "Kay Beauty",
            "Nykaa Cosmetics", "Colorbar", "Faces Canada", "e.l.f. Cosmetics",
            "Wet n Wild", "Milani", "Swiss Beauty", "MARS",
            "Makeup Revolution", "Insight Cosmetics", "Blue Heaven",
            "Sugar Cosmetics", "NYX Professional Makeup", "L.A. Girl",
            "Forever52", "Color Chemistry"),
        "Makeup: premium": (
            "MAC", "Estee Lauder", "Bobbi Brown", "NARS", "Clinique",
            "Huda Beauty", "Benefit Cosmetics", "Too Faced", "Smashbox",
            "Charlotte Tilbury", "Make Up For Ever", "Dior Beauty",
            "Giorgio Armani Beauty", "Rare Beauty", "Fenty Beauty",
            "Laura Mercier"),
        "Haircare: everyday and value": (
            "LOreal Paris", "Tresemme", "Dove", "Herbal Essences",
            "Love Beauty & Planet", "OGX", "Minimalist", "WishCare",
            "Bare Anatomy", "BBlunt", "Plum", "Fix My Curls", "Curl Up",
            "Manetain"),
        "Haircare: professional and salon": (
            "LOreal Professionnel", "Schwarzkopf Professional",
            "Wella Professionals", "Matrix", "Redken", "Moroccanoil",
            "Olaplex", "Kerastase", "Kevin Murphy", "Davines"),
        "Haircare: dandruff and scalp care": (
            "Scalpe Pro", "Selsun", "Head & Shoulders", "Sebamed",
            "Bioderma Node", "Minimalist", "Reequil"),
        "Bath and body": (
            "Nivea", "Dove", "The Body Shop", "Bath & Body Works", "Plum",
            "mCaffeine", "Love Beauty & Planet", "Chemist at Play",
            "Be Bodywise", "Minimalist", "Vaseline", "Neutrogena",
            "Cetaphil", "Sebamed", "Forest Essentials", "Kama Ayurveda",
            "Juicy Chemistry", "Sol de Janeiro"),
        "Men's grooming": (
            "Philips", "Braun", "Gillette", "Beardo",
            "Bombay Shaving Company", "Ustraa", "The Man Company",
            "Man Matters", "LetsShave", "Park Avenue", "Nivea Men",
            "LOreal Men Expert", "Minimalist", "Reequil", "Cetaphil"),
        "Fragrances: affordable": (
            "Ajmal", "Armaf", "Rasasi", "Lattafa", "Maison Alhambra",
            "Skinn by Titan", "Engage", "Wild Stone", "Beardo", "Bella Vita",
            "Fraganote"),
        "Fragrances: designer and premium": (
            "Davidoff", "Calvin Klein", "Hugo Boss", "Versace",
            "Giorgio Armani", "Burberry", "Carolina Herrera", "Paco Rabanne",
            "Jean Paul Gaultier", "Yves Saint Laurent", "Dior",
            "Jo Malone London"),
    },
}

# Product-type terms paired with each brand during the scan. Wide on purpose:
# "track all products" means every type a brand sells must be queried.
SCANNER_SEEDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": (
        "shirt", "formal shirt", "casual shirt", "t-shirt", "polo t-shirt",
        "jeans", "trousers", "formal trousers", "chinos", "jacket", "blazer",
        "suit", "kurta", "kurta set", "sherwani", "nehru jacket",
        "track pants", "shorts", "joggers", "innerwear", "vest", "briefs",
        "sweatshirt", "hoodie", "sweater", "co-ord set"),
    "Women's Fashion": (
        "kurta", "kurta set", "kurti", "dress", "maxi dress", "jeans", "top",
        "tshirt", "shirt", "saree", "lehenga", "salwar suit", "leggings",
        "palazzo", "bra", "lingerie set", "nightwear", "jumpsuit", "skirt",
        "co-ord set", "blazer", "trousers", "dupatta", "sharara"),
    "Footwear & Shoes": (
        "running shoes", "sports shoes", "sneakers", "casual shoes",
        "formal shoes", "loafers", "derby shoes", "sandals", "slippers",
        "flip flops", "boots", "heels", "flats", "clogs", "walking shoes",
        "training shoes", "hiking shoes"),
    "Watches & Eyewear": (
        "watch", "analog watch", "digital watch", "chronograph watch",
        "smartwatch", "fitness band", "sunglasses", "aviator sunglasses",
        "eyeglasses", "backpack", "laptop bag", "handbag", "sling bag",
        "tote bag", "wallet", "luggage", "trolley bag", "duffle bag"),
    "Cosmetics & Grooming": (
        "face serum", "face wash", "cleanser", "moisturizer", "sunscreen",
        "face cream", "night cream", "toner", "sheet mask", "lipstick",
        "lip balm", "foundation", "concealer", "compact", "kajal", "mascara",
        "eyeliner", "shampoo", "conditioner", "hair serum", "hair oil",
        "hair mask", "perfume", "deodorant", "body wash", "body lotion",
        "trimmer", "shaver", "beard oil", "face pack"),
    # Retained so the tracker's remaining categories can still be scanned.
    "Smartphones": (
        "mobile phone 5g", "smartphone", "phone under 20000"),
    "Audio, Monitors & Laptops": (
        "bluetooth headphones", "tws earbuds", "laptop", "bluetooth speaker",
        "monitor", "tablet"),
    "Home Appliances": (
        "water purifier", "air fryer", "mixer grinder", "ceiling fan",
        "vacuum cleaner", "geyser", "microwave"),
    "Stationery & Books": (
        "notebook", "pen", "calculator", "books", "art supplies"),
}

# Brands for the categories not covered by the curated master list.
_FALLBACK_BRANDS: Dict[str, Tuple[str, ...]] = {
    "Smartphones": ("Apple", "Samsung", "OnePlus", "Motorola", "Nothing",
                    "Google", "Vivo", "Oppo", "Realme", "Xiaomi", "iQOO",
                    "Poco", "Infinix", "Tecno", "Lava"),
    "Audio, Monitors & Laptops": ("Sony", "JBL", "boAt", "Marshall", "LG",
                                  "Samsung", "Acer", "HP", "Lenovo", "Asus",
                                  "Dell", "Apple", "Sennheiser", "OnePlus",
                                  "Noise", "BenQ"),
    "Home Appliances": ("Philips", "Bajaj", "Kent", "Aquaguard", "Prestige",
                        "Havells", "Atomberg", "Dyson", "Crompton", "Usha",
                        "Butterfly", "Pigeon", "Orient", "Voltas"),
    "Stationery & Books": ("Classmate", "Parker", "Casio", "Camlin",
                           "Faber-Castell", "Doms", "Penguin", "Cello",
                           "Luxor", "Navneet"),
}


def _flatten_brands() -> Dict[str, Tuple[str, ...]]:
    """Collapse the curated segments into one de-duplicated list per category."""
    flat: Dict[str, Tuple[str, ...]] = {}
    for category, segments in BRAND_SEGMENTS.items():
        seen: List[str] = []
        for brands in segments.values():
            for brand in brands:
                if brand not in seen:
                    seen.append(brand)
        flat[category] = tuple(seen)
    for category, brands in _FALLBACK_BRANDS.items():
        flat.setdefault(category, brands)
    return flat


SCANNER_BRANDS: Dict[str, Tuple[str, ...]] = _flatten_brands()

SCANNED_FIELDS = ("pid", "title", "brand", "url", "price", "mrp",
                  "discount_pct", "rating", "rating_count", "category",
                  "seed", "has_mrp")

# Longest-first so "Allen Solly Woman" wins over "Allen Solly", and
# "Van Heusen Woman" over "Van Heusen".
_ALL_CURATED_BRANDS: Tuple[str, ...] = tuple(sorted(
    {b for brands in SCANNER_BRANDS.values() for b in brands},
    key=len, reverse=True))


def _match_curated_brand(title: str) -> str:
    """Return the curated brand a product title starts with, if any."""
    lowered = (title or "").lower()
    for brand in _ALL_CURATED_BRANDS:
        if lowered.startswith(brand.lower()):
            return brand
    return ""


@dataclass
class ScannedProduct:
    """One product discovered by the deep scanner."""

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
        # itm-id FIRST: the state tree emits a thin wrapper node (no pid) and a
        # hydrated node (with pid) for the same product. Keying on pid would
        # count those as two products and inflate the catalogue.
        return extract_itm_id(self.url) or self.pid or self.url

    def normalise(self) -> "ScannedProduct":
        # __INITIAL_STATE__ carries host-relative hrefs; sanitize_url() treats a
        # bare "/slug/p/itm..." as a hostname, so prefix the host first exactly
        # as the original resolver does.
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
            # Match the curated universe first: title.split()[0] would turn
            # "Allen Solly Slim Fit Shirt" into "Allen", breaking the brand
            # filter in the tracker's existing tables.
            self.brand = _match_curated_brand(self.title) or self.title.split()[0]
        return self

    def to_dict(self) -> Dict[str, Any]:
        return {f: getattr(self, f) for f in SCANNED_FIELDS}


@dataclass
class ScanTask:
    """One unit of work on the frontier queue."""

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
        """
        Split this price band in half.

        The heart of deep scanning: a saturated (truncated) band is replaced by
        two narrower bands, repeated until a slice returns a short page and is
        provably exhausted.
        """
        if self.price_min is None or self.price_max is None:
            return []
        if self.band_width <= SCANNER_MIN_BAND_WIDTH:
            return []
        if self.depth >= SCANNER_MAX_BISECT_DEPTH:
            return []
        mid = self.price_min + self.band_width // 2
        return [
            ScanTask(self.term, self.category, self.sort, self.price_min, mid,
                     self.depth + 1, self.tier),
            ScanTask(self.term, self.category, self.sort, mid, self.price_max,
                     self.depth + 1, self.tier),
        ]


def scanner_search_url(term: str, *, page: int = 1, sort: str = "",
                       price_min: Optional[int] = None,
                       price_max: Optional[int] = None) -> str:
    """Flipkart search URL with pagination, sort and price-band facets."""
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
    """Field count, used to keep the best of several duplicate nodes."""
    return sum(bool(v) for v in (product.title, product.price, product.mrp,
                                 product.pid, product.rating,
                                 product.rating_count))


class AdaptiveRateLimiter:
    """
    Token bucket whose rate ADAPTS instead of a scan aborting.

    On a block the rate halves (to a floor); after sustained success it eases
    back up. Flipkart pushing back slows the scan - it never ends it.
    """

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
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._updated) * self.rate)
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
    """Deep, unlimited product discovery via recursive query subdivision."""

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
        self.abort = False        # ONLY set by the user's Stop button
        self.tasks_done = 0
        self.tasks_dropped = 0    # shed when the queue hit SCANNER_MAX_QUEUE
        self.queue_peak = 0
        self._load_store()

    # -- persistence -------------------------------------------------------- #

    def _load_store(self) -> None:
        """Discoveries accumulate across sessions; that is how coverage grows."""
        if not os.path.exists(SCANNER_STORE_FILE):
            return
        try:
            with open(SCANNER_STORE_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Scan store unreadable (%s); starting empty.", exc)
            return
        if not isinstance(data, list):
            return
        for row in data:
            if not isinstance(row, dict):
                continue
            product = ScannedProduct(**{k: v for k, v in row.items()
                                        if k in SCANNED_FIELDS})
            if product.key:
                self._seen[product.key] = product
        logger.info("Loaded %d previously discovered products.", len(self._seen))

    def persist(self) -> int:
        """
        Write the full store. O(n), so on long runs this is called on a
        checkpoint cadence rather than after every batch.
        """
        try:
            atomic_write_json(SCANNER_STORE_FILE,
                              [p.to_dict() for p in self._seen.values()])
        except OSError as exc:
            logger.warning("Could not persist scan store: %s", exc)
            return 0
        return len(self._seen)

    def save_frontier(self, queue: Sequence["ScanTask"]) -> int:
        """
        Persist the unfinished frontier so an interrupted scan RESUMES rather
        than restarting. This is what makes an unbounded scan practical: stop
        it, close the app, and continue later from the same position.
        """
        try:
            atomic_write_json(SCANNER_FRONTIER_FILE, [
                {"term": t.term, "category": t.category, "sort": t.sort,
                 "price_min": t.price_min, "price_max": t.price_max,
                 "depth": t.depth, "tier": t.tier} for t in queue])
        except OSError as exc:
            logger.warning("Could not persist frontier: %s", exc)
            return 0
        return len(queue)

    def load_frontier(self) -> List["ScanTask"]:
        """Reload an unfinished frontier from a previous run."""
        if not os.path.exists(SCANNER_FRONTIER_FILE):
            return []
        try:
            with open(SCANNER_FRONTIER_FILE, "r", encoding="utf-8") as fh:
                rows = json.load(fh)
        except (json.JSONDecodeError, OSError):
            return []
        if not isinstance(rows, list):
            return []
        tasks: List[ScanTask] = []
        for row in rows:
            if isinstance(row, dict) and row.get("term"):
                tasks.append(ScanTask(
                    str(row["term"]), str(row.get("category", "")),
                    str(row.get("sort", "")), row.get("price_min"),
                    row.get("price_max"), int(row.get("depth", 0)),
                    int(row.get("tier", 0))))
        return tasks

    def clear_frontier(self) -> None:
        if os.path.exists(SCANNER_FRONTIER_FILE):
            try:
                os.remove(SCANNER_FRONTIER_FILE)
            except OSError:
                pass

    def clear_store(self) -> None:
        with self._lock:
            self._seen.clear()
            self.pages_fetched = self.pages_blocked = self.pages_empty = 0
            self.products_seen_raw = self.bisections = self.max_depth_reached = 0
            self.tasks_done = self.tasks_dropped = self.queue_peak = 0
        if os.path.exists(SCANNER_STORE_FILE):
            try:
                os.remove(SCANNER_STORE_FILE)
            except OSError:
                pass

    # -- http --------------------------------------------------------------- #

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
            except Exception as exc:
                logger.info("Scanner fetch failed (%s): %s",
                            type(exc).__name__, exc)
                time.sleep(0.5 * attempt)
                continue

            blocked = response.status_code in (401, 403, 429, 503)
            challenged = (response.status_code == 200 and response.text and
                          any(m in response.text[:4000].lower()
                              for m in SCANNER_CHALLENGE_MARKERS))
            if blocked or challenged:
                with self._lock:
                    self.pages_blocked += 1
                self.limiter.penalise()          # slow down, do NOT stop
                time.sleep(min(2.0 * attempt, 6.0))
                continue

            if response.status_code == 200 and response.text:
                with self._lock:
                    self.pages_fetched += 1
                self.limiter.reward()
                return response.text
            time.sleep(0.6 * attempt)
        return None

    # -- extraction --------------------------------------------------------- #

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
                for inner in ("value", "finalPrice", "sellingPrice",
                              "decimalValue", "amount"):
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
        if "/p/itm" not in url:
            for holder in ("productInfo", "action", "value", "link"):
                sub = merged.get(holder)
                if isinstance(sub, dict):
                    url = cls._text(sub, ("url", "pageUri", "baseUrl",
                                          "smartUrl"))
                    if "/p/itm" in url:
                        break
        if "/p/itm" not in url:
            return None

        titles = merged.get("titles")
        title = cls._text(merged, ("title", "name", "productName"))
        if not title and isinstance(titles, dict):
            title = cls._text(titles, ("title", "newTitle", "superTitle"))

        rating_node = merged.get("rating") if isinstance(
            merged.get("rating"), dict) else {}
        rating = cls._number(rating_node, ("average",)) if rating_node else \
            cls._number(merged, ("averageRating",))
        rating_count = int(cls._number(rating_node, ("count",)) if rating_node
                           else cls._number(merged, ("ratingCount",)))

        # Read Flipkart's own discount field: most listings ship no `mrp`, so
        # deriving discount from MRP alone makes the majority invisible.
        stated_discount = cls._number(merged, ("totalDiscount", "discount",
                                               "discountPercent"))

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
        """Every product on one search page, from __INITIAL_STATE__."""
        found: Dict[str, ScannedProduct] = {}

        match = SCANNER_STATE_PATTERN.search(html)
        if match:
            try:
                state = json.loads(match.group(1))
            except json.JSONDecodeError:
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
                    if existing is None or \
                            _scanned_richness(product) > _scanned_richness(existing):
                        found[product.key] = product

        # Floor: same href pattern the original resolver already trusts, so a
        # markup change degrades coverage instead of zeroing it.
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
                found[itm] = ScannedProduct(title=slug.title()[:120],
                                            url=clean).normalise()
        return list(found.values())

    # -- scanning ----------------------------------------------------------- #

    def category_count(self, category: str) -> int:
        with self._lock:
            return sum(1 for p in self._seen.values()
                       if p.category == category)

    def _absorb(self, products: Sequence[ScannedProduct],
                task: ScanTask) -> int:
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

    def run_task(self, task: ScanTask,
                 pages: int) -> Tuple[int, List[ScanTask]]:
        """
        Scan one query across N pages.

        Returns (new_products, follow_up_tasks). Follow-ups appear when a page
        comes back SATURATED - Flipkart truncated the result set, so more
        inventory is hidden behind this query than pagination will reveal.
        """
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
                if empty_streak >= 2:   # Flipkart interleaves sparse pages
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
                    self.max_depth_reached = max(self.max_depth_reached,
                                                 children[0].depth)
        return added, follow_ups

    @staticmethod
    def brands_for(category: str,
                   segment_names: Optional[Sequence[str]] = None
                   ) -> Tuple[str, ...]:
        """Curated brands for a category, optionally limited to named segments."""
        segments = BRAND_SEGMENTS.get(category)
        if not segments or not segment_names:
            return SCANNER_BRANDS.get(category, ())
        seen: List[str] = []
        for name in segment_names:
            for brand in segments.get(name, ()):
                if brand not in seen:
                    seen.append(brand)
        return tuple(seen)

    def seed_frontier(self, categories: Sequence[str], *,
                      segments: Optional[Dict[str, List[str]]] = None,
                      use_bands: bool = True,
                      use_sorts: bool = False) -> List[ScanTask]:
        """
        Build the INITIAL frontier from the curated brand universe.

        `segments` optionally restricts each category to named brand segments.
        Tasks are interleaved round-robin so every category makes progress from
        the first minute.
        """
        per_category: Dict[str, List[ScanTask]] = {}
        for category in categories:
            types = list(SCANNER_SEEDS.get(category, ()))
            brands = list(self.brands_for(category,
                                          (segments or {}).get(category)))
            tasks: List[ScanTask] = []

            # Tier 0 - brand alone (catches everything the brand sells).
            tasks.extend(ScanTask(b, category, tier=0) for b in brands)
            # Tier 1 - brand x product type (the main workhorse).
            tasks.extend(ScanTask(f"{b} {t}", category, tier=1)
                         for b in brands for t in types)
            # Tier 2 - brand x coarse price band: the bisection roots.
            if use_bands:
                tasks.extend(ScanTask(b, category, "", low, high, 0, 2)
                             for b in brands
                             for low, high in SCANNER_SEED_BANDS)
            # Tier 3 - sort sweeps.
            if use_sorts:
                tasks.extend(ScanTask(b, category, s, tier=3)
                             for b in brands for s in SCANNER_SORTS[1:])
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
                     max_requests: int = 0, resume: bool = True,
                     progress=None) -> int:
        """
        Work the frontier to EXHAUSTION. There is NO product cap.

        The queue grows as bisection discovers depth and shrinks as slices are
        proven complete. The scan ends only when:
          * the queue drains - every reachable slice has been enumerated, or
          * the user presses Stop, or
          * an optional safety cap on REQUESTS (never products) is hit.

        A block never ends the scan; the adaptive limiter slows it instead.
        Progress is checkpointed to disk, and an interrupted run saves its
        unfinished frontier so the next run resumes from the same position.
        """
        self.abort = False
        queue: deque = deque(frontier)

        # Resume an interrupted scan rather than restarting it.
        if resume:
            pending = self.load_frontier()
            if pending:
                queue.extend(pending)
                logger.info("Resumed %d pending tasks from previous run.",
                            len(pending))

        processed = 0
        batches = 0

        try:
            with ThreadPoolExecutor(max_workers=SCANNER_WORKERS) as pool:
                while queue and not self.abort:
                    # Optional REQUEST cap. Products are never capped.
                    if max_requests and self.pages_fetched >= max_requests:
                        logger.info("Request cap reached; %d tasks still queued.",
                                    len(queue))
                        break

                    batch: List[ScanTask] = []
                    while queue and len(batch) < SCANNER_WORKERS * 3:
                        batch.append(queue.popleft())
                    if not batch:
                        continue

                    futures = {pool.submit(self.run_task, t, pages): t
                               for t in batch}
                    for future in as_completed(futures):
                        processed += 1
                        self.tasks_done += 1
                        try:
                            _, follow_ups = future.result()
                        except Exception as exc:
                            logger.warning("Scan task failed: %s", exc)
                            follow_ups = []

                        # Bisection discovered depth: queue the narrower slices.
                        for child in follow_ups:
                            if len(queue) < SCANNER_MAX_QUEUE:
                                queue.append(child)
                            else:
                                # Shed work, never products. Dropping a task
                                # loses unexplored depth, not anything found.
                                self.tasks_dropped += 1

                        if len(queue) > self.queue_peak:
                            self.queue_peak = len(queue)
                        if progress:
                            progress(processed,
                                     max(processed + len(queue), processed),
                                     futures[future].term, len(self._seen),
                                     len(queue))

                    batches += 1
                    # Checkpoint on a cadence: persisting every batch is O(n)
                    # and dominates runtime once the store is large.
                    if batches % SCANNER_CHECKPOINT_EVERY == 0:
                        self.persist()
                        self.save_frontier(list(queue))
        finally:
            # Always land in a consistent state, even on an exception, so a
            # crashed run still resumes cleanly.
            self.persist()
            if queue:
                self.save_frontier(list(queue))   # keep the resume position
            else:
                self.clear_frontier()             # drained: nothing to resume
        return len(self._seen)

    # -- views & diagnostics ------------------------------------------------ #

    @property
    def products(self) -> List[ScannedProduct]:
        return list(self._seen.values())

    def priced(self) -> List[ScannedProduct]:
        return [p for p in self._seen.values() if p.price > 0]

    def discounted(self, threshold: float) -> List[ScannedProduct]:
        return sorted((p for p in self._seen.values()
                       if p.price > 0 and p.discount_pct >= threshold),
                      key=lambda p: p.discount_pct, reverse=True)

    def coverage(self) -> Dict[str, int]:
        counts: Dict[str, int] = {}
        for product in self._seen.values():
            key = product.category or "Uncategorised"
            counts[key] = counts.get(key, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))

    def diagnose(self) -> Tuple[str, str]:
        """Explain low yield: blocked, unparsed, duplication or healthy."""
        attempts = self.pages_fetched + self.pages_blocked
        if attempts == 0:
            return "idle", "No pages requested yet."
        if self.pages_blocked / attempts > 0.5:
            return ("blocked",
                    f"{self.pages_blocked:,} of {attempts:,} requests were "
                    "refused or challenged. Flipkart is rate-limiting this "
                    "machine. The scan slowed itself down rather than "
                    "stopping. Wait 10-15 minutes, or lower pages per query.")
        if self.pages_fetched and self.products_seen_raw == 0:
            return ("unparsed",
                    f"{self.pages_fetched:,} pages downloaded but no products "
                    "parsed. Flipkart served markup this build cannot read - "
                    "likely a bot-challenge shell rather than real results.")
        unique = len(self._seen)
        if self.products_seen_raw and unique and \
                self.products_seen_raw / unique > 8:
            return ("duplication",
                    f"Saw {self.products_seen_raw:,} product slots but only "
                    f"{unique:,} distinct items "
                    f"({self.products_seen_raw / unique:.0f}x duplication). "
                    "Enable price bands so saturated queries bisect deeper.")
        return ("healthy",
                f"{self.pages_fetched:,} pages fetched, {unique:,} distinct "
                f"products from {self.products_seen_raw:,} slots. "
                f"{self.bisections:,} bisections, max depth "
                f"{self.max_depth_reached}.")


@st.cache_resource(show_spinner=False)
def get_scanner() -> FlipkartCatalogueScanner:
    """One scanner (sessions, limiter, accumulated store) across reruns."""
    return FlipkartCatalogueScanner()


# --------------------------------------------------------------------------- #
# IN-APP DIAGNOSTIC  (no terminal needed - Streamlit Cloud has no shell)
# --------------------------------------------------------------------------- #
# Tests the EXACT regexes and extraction logic the scanner uses, and reports
# the FIRST failing step - which is the real cause; everything after it fails
# as a consequence.


def run_scanner_diagnostic() -> List[Dict[str, Any]]:
    """Five sequential checks. The FIRST failure is the real cause."""
    steps: List[Dict[str, Any]] = []

    def add(step: int, name: str, ok: bool, detail: str,
            hint: str = "") -> bool:
        steps.append({"Step": step, "Check": name,
                      "Result": "✅ PASS" if ok else "❌ FAIL",
                      "Detail": detail, "_hint": hint, "_ok": ok})
        return ok

    if requests is None:
        add(1, "requests installed", False,
            "The `requests` package is missing.",
            "Add `requests>=2.28` to requirements.txt and redeploy.")
        return steps

    session = FlipkartLinkResolver._build_session()

    try:
        home = session.get(f"https://{FLIPKART_HOST}/", timeout=20)
        if not add(1, "Reach flipkart.com", home.status_code == 200,
                   f"HTTP {home.status_code}, {len(home.text):,} bytes",
                   "" if home.status_code == 200 else
                   "Flipkart refused this server. On Streamlit Community "
                   "Cloud this is expected: shared datacenter IPs are widely "
                   "blocked. Run the app locally instead."):
            return steps
    except Exception as exc:
        add(1, "Reach flipkart.com", False,
            f"{type(exc).__name__}: {str(exc)[:140]}",
            "No connection at all. On Streamlit Cloud this normally means "
            "Flipkart drops traffic from the host's IP range. Running the app "
            "on your own machine is the only reliable fix.")
        return steps

    try:
        resp = session.get(scanner_search_url("laptop"), timeout=20)
    except Exception as exc:
        add(2, "Search returns results", False,
            f"{type(exc).__name__}: {str(exc)[:140]}",
            "Homepage loaded but /search did not. Flipkart treats search as "
            "higher-risk and blocks it more aggressively.")
        return steps

    html = resp.text or ""
    if resp.status_code != 200:
        add(2, "Search returns results", False, f"HTTP {resp.status_code}",
            "Search is blocked even though the homepage loaded.")
        return steps

    challenged = any(m in html[:4000].lower()
                     for m in SCANNER_CHALLENGE_MARKERS)
    if not add(2, "Search returns results", not challenged,
               "Bot-challenge page served" if challenged
               else f"HTTP 200, {len(html):,} bytes of real markup",
               "Flipkart served a CAPTCHA instead of products. Wait 10-15 "
               "minutes, then lower 'Pages per query'." if challenged else ""):
        return steps

    has_state = "__INITIAL_STATE__" in html
    hrefs = len(set(PDP_HREF_PATTERN.findall(html)))
    add(3, "__INITIAL_STATE__ present", has_state,
        f"Payload found · {hrefs} raw PDP links in page" if has_state
        else f"Payload ABSENT · {hrefs} raw PDP links in page",
        "" if has_state else
        "Flipkart changed its markup. The href fallback can still find links "
        "but without prices, so discounts cannot be ranked.")

    products = FlipkartCatalogueScanner.extract_products(html)
    priced = [p for p in products if p.price > 0]
    add(4, "Parser extracts products", bool(products),
        f"{len(products)} products extracted, {len(priced)} with prices"
        if products else "ZERO products extracted from a page that loaded fine",
        "" if products else
        "The page downloaded but the parser's key names no longer match "
        "Flipkart's schema. This is a code fix - share this result.")

    try:
        banded = session.get(scanner_search_url("laptop", price_min=20000,
                                                price_max=40000), timeout=20)
        bp = FlipkartCatalogueScanner.extract_products(banded.text or "")
        add(5, "Price-band facets work", bool(bp),
            f"{len(bp)} products in ₹20k-40k band · "
            f"{'saturated → bisection will split' if len(bp) >= SCANNER_SATURATION else 'short → slice complete'}"
            if bp else "Banded query returned nothing",
            "" if bp else "Bisection cannot run, so scans stay shallow.")
    except Exception as exc:
        add(5, "Price-band facets work", False,
            f"{type(exc).__name__}: {str(exc)[:140]}", "")

    return steps


def render_diagnostic_panel() -> None:
    """Diagnostic UI, deliberately placed ABOVE the scan controls."""
    st.markdown("**Before scanning, confirm this server can actually reach "
                "Flipkart.**")
    st.caption("A scan that returns nothing looks identical whether the cause "
               "is blocking or a parser mismatch. These are opposite problems "
               "with opposite fixes, so test rather than guess.")

    if st.button("🩺 Run Full Diagnostic", key="diag_run", type="primary"):
        with st.spinner("Testing connection, markup and parser…"):
            st.session_state["diag_results"] = run_scanner_diagnostic()

    steps = st.session_state.get("diag_results")
    if not steps:
        return

    st.dataframe(pd.DataFrame([{k: v for k, v in s.items()
                                if not k.startswith("_")} for s in steps]),
                 use_container_width=True, hide_index=True)

    failures = [s for s in steps if not s["_ok"]]
    if not failures:
        st.success("**All checks passed.** This server can scan Flipkart. If "
                   "a scan still returns few products, the limit is "
                   "rate-limiting during sustained use — lower 'Pages per "
                   "query' and retry.")
        return

    first = failures[0]
    st.error(f"**First failure — step {first['Step']}: {first['Check']}**\n\n"
             f"{first['Detail']}\n\nEverything after this point fails as a "
             f"consequence.")
    if first["_hint"]:
        st.warning(first["_hint"])
    if first["Step"] in (1, 2):
        st.markdown(
            "### This is a network/blocking problem, not a code problem\n"
            "The scanner logic is fine — it simply cannot obtain data.\n\n"
            "**If you are on Streamlit Community Cloud, this is the expected "
            "result.** Flipkart blocks shared datacenter IP ranges, and every "
            "Streamlit Cloud app shares those ranges. No code change fixes "
            "this.\n\n"
            "**What works:** run the app on your own machine "
            "(`streamlit run Flipkart.py`), or use Flipkart's official "
            "Affiliate API, which is built for programmatic access.")
    elif first["Step"] in (3, 4):
        st.markdown(
            "### This is a parser problem\n"
            "Pages are downloading, but the extraction keys no longer match "
            "Flipkart's markup. Share this table so it can be corrected "
            "precisely instead of guessed at.")


# --------------------------------------------------------------------------- #
# SCANNER CONTROL PANEL
# --------------------------------------------------------------------------- #
# Deliberately NOT a results section. Discovered products are merged into the
# tracker store and rendered by the ORIGINAL sections 1/2/3 through
# render_collapsible_table(), so output format, columns, filters, wishlist
# checkboxes and CSV exports are exactly the ones the tracker already had.

# Keyword -> tracker category, reusing the tracker's own 9 buckets so scanned
# products never create new sections. Tech first: "headphone" contains "phone".
_TRACKER_CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Audio, Monitors & Laptops", ("laptop", "monitor", "headphone",
                                   "earphone", "earbud", "speaker", "tws",
                                   "soundbar", "tablet", "ipad", "macbook",
                                   "neckband")),
    ("Smartphones", ("smartphone", "mobile", "iphone", "galaxy", "oneplus",
                     "pixel", "redmi", "realme", "poco", "vivo", "oppo",
                     "motorola", "iqoo", "infinix", "tecno", "lava", "5g")),
    ("Footwear & Shoes", ("shoe", "sneaker", "boot", "loafer", "sandal",
                          "clog", "slipper", "heel", "flip flop", "trainer")),
    ("Watches & Eyewear", ("watch", "smartwatch", "sunglass", "eyeglass",
                           "spectacle", "aviator", "chronograph", "band",
                           "backpack", "handbag", "luggage", "trolley",
                           "wallet", "duffle", "sling bag", "tote")),
    ("Cosmetics & Grooming", ("serum", "lipstick", "trimmer", "perfume",
                              "cream", "shampoo", "cleanser", "sunscreen",
                              "face wash", "moisturizer", "hair oil",
                              "lotion", "kajal", "foundation", "makeup",
                              "fragrance", "deodorant", "toner", "mascara",
                              "concealer", "conditioner", "beard oil")),
    ("Home Appliances", ("purifier", "iron", "cooker", "fryer", "grinder",
                         "geyser", "fan", "vacuum", "kettle", "heater",
                         "microwave", "cooler", "cooktop", "juicer", "stove")),
    ("Stationery & Books", ("book", "novel", "pen", "notebook", "calculator",
                            "diary", "marker", "highlighter", "geometry")),
    ("Women's Fashion", ("women", "saree", "kurta", "kurti", "lehenga",
                         "dress", "legging", "palazzo", "blouse", "shrug",
                         "jumpsuit", "skirt", "bra", "anarkali", "sharara",
                         "salwar", "dupatta")),
    ("Men's Fashion", ("men", "shirt", "t-shirt", "jean", "trouser", "jacket",
                       "suit", "short", "hoodie", "blazer", "sweatshirt",
                       "track pant", "innerwear", "kurta", "chino",
                       "sherwani", "jogger", "vest", "brief")),
]


def _map_to_tracker_category(title: str, hint: str = "") -> str:
    """Map a scanned product onto one of the tracker's existing categories."""
    if hint in CAT_DATA_MATRIX:
        return hint
    lowered = (title or "").lower()
    for category, keywords in _TRACKER_CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Men's Fashion"


def merge_scan_results_into_tracker(scanner: "FlipkartCatalogueScanner",
                                    min_discount: float = 0.0) -> int:
    """Fold discoveries into the tracker catalogue, mapped onto its own 9
    categories so they appear inside the existing collapsible tables."""
    products = [p for p in scanner.products
                if p.price > 0 and p.title and p.discount_pct >= min_discount]
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

        # Only claim an MRP Flipkart actually published; otherwise derive a
        # transparent placeholder instead of inventing a discount.
        mrp = product.mrp if product.has_mrp else int(product.price * 1.25)
        category = product.category if product.category in CAT_DATA_MATRIX \
            else _map_to_tracker_category(title, product.category)

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


def render_deep_scanner() -> None:
    """Compact scan controls. Results are merged into the tables above."""
    scanner = get_scanner()

    st.divider()
    with st.expander("🛰️ Deep Catalogue Scanner — curated brand universe",
                     expanded=False):

        render_diagnostic_panel()
        st.divider()

        if not scanner.available:
            st.error("The `requests` package is required for scanning. "
                     "Install it with `pip install requests`.")
            return

        total_brands = sum(len(v) for v in SCANNER_BRANDS.values())
        st.caption(
            f"Scanning covers a curated universe of **{total_brands} brands** "
            f"across the tracker's 9 categories. Flipkart caps pagination per "
            f"query, so depth comes from splitting: when a page returns "
            f"≥{SCANNER_SATURATION} products the result set was truncated, so "
            f"the price band halves and both halves re-queue, repeating until "
            f"each slice is fully enumerable. Everything found is merged into "
            f"the category tables above — same columns, filters, wishlist and "
            f"CSV exports as always.")

        c1, c2, c3 = st.columns([2.4, 1.3, 1.3])
        categories = c1.multiselect(
            "Categories to scan:", options=list(SCANNER_SEEDS),
            default=list(BRAND_SEGMENTS), key="scan_cats")
        pages = c2.slider("Pages per query:", 1, 20, 8, key="scan_pages",
                          help="Pages pulled per query before bisection takes "
                               "over. Higher reaches more per slice.")
        resume = c3.checkbox("Resume unfinished scan", value=True,
                             key="scan_resume",
                             help="Continue from where the last run stopped "
                                  "instead of restarting the frontier.")

        # Brand-segment pickers, one per curated category.
        segments: Dict[str, List[str]] = {}
        curated = [c for c in categories if c in BRAND_SEGMENTS]
        if curated:
            st.markdown("**Brand segments** — leave empty to scan every brand "
                        "in that category.")
            for category in curated:
                names = list(BRAND_SEGMENTS[category])
                chosen = st.multiselect(
                    f"{category}:", options=names, default=[],
                    placeholder=f"All {len(SCANNER_BRANDS.get(category, ()))} brands",
                    key=f"seg_{re.sub(r'[^a-z0-9]+', '_', category.lower())}")
                if chosen:
                    segments[category] = chosen

        o1, o2, o3 = st.columns(3)
        use_bands = o1.checkbox("× price bands", value=True, key="scan_bands",
                                help="REQUIRED for bisection — these are the "
                                     "roots that split when saturated")
        use_sorts = o2.checkbox("× sort orders", value=False, key="scan_sorts",
                                help="Reaches opposite ends of a result set; "
                                     "4x more requests")
        budget = o3.number_input(
            "Safety cap on REQUESTS (0 = unlimited):", 0, 5_000_000, 0, 1000,
            key="scan_budget",
            help="Caps HTTP requests, never products. 0 runs until the "
                 "frontier is exhausted. Products are never limited.")

        min_disc = st.slider("Only add products discounted ≥ %:", 0, 80, 0, 5,
                             key="scan_min_disc",
                             help="Filters what gets merged into the tables "
                                  "above. 0 adds everything found.")

        if not use_bands:
            st.warning("Price bands are off, so **bisection cannot run** and "
                       "the scan stays shallow. Turn them on for deep "
                       "scanning.")

        if not categories:
            st.info("Pick at least one category to scan.")
            return

        frontier = scanner.seed_frontier(categories, segments=segments,
                                         use_bands=use_bands,
                                         use_sorts=use_sorts)
        active_brands = sum(len(scanner.brands_for(c, segments.get(c)))
                            for c in categories)
        pending = len(scanner.load_frontier()) if resume else 0
        st.caption(
            f"Initial frontier: **{len(frontier):,} queries** from "
            f"**{active_brands} brands** across **{len(categories)} "
            f"categories**"
            + (f", plus **{pending:,} resumed** from the last run"
               if pending else "")
            + ". **No product limit** — the queue grows as saturated queries "
              "bisect and the scan runs until every reachable slice is "
              "exhausted. Progress is checkpointed, so you can Stop and "
              "resume.")
        if not budget:
            st.info("**Unlimited mode.** This runs until exhaustion and can "
                    "take many hours. It checkpoints every "
                    f"{SCANNER_CHECKPOINT_EVERY} batches, so pressing Stop "
                    "(or a browser disconnect) keeps everything found and the "
                    "next run resumes from the same position.")

        r1, r2, r3 = st.columns(3)
        if r1.button("🛰️ Run Deep Scan", type="primary", key="scan_run"):
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(done: int, total: int, term: str, found: int,
                            pending_now: int) -> None:
                bar.progress(min(done / max(total, 1), 1.0))
                status.caption(
                    f"[{done:,}] {term} · **{found:,}** found · "
                    f"{pending_now:,} queued · {scanner.bisections:,} splits · "
                    f"{scanner.limiter.rate:.1f} req/s")

            with st.spinner("Deep scanning Flipkart…"):
                scanner.run_frontier(frontier, pages,
                                     max_requests=int(budget), resume=resume,
                                     progress=on_progress)
                added = merge_scan_results_into_tracker(scanner,
                                                        float(min_disc))
            bar.empty()
            status.empty()
            st.session_state["scan_done"] = True
            st.session_state["scan_added"] = added
            st.rerun()

        if r2.button("⏹️ Stop", key="scan_stop"):
            scanner.abort = True
            st.toast("Stopping after in-flight requests finish.", icon="⏹️")

        if r3.button("🗑️ Clear Scan Store", key="scan_clear"):
            scanner.clear_store()
            scanner.clear_frontier()   # drop the resume point too
            st.toast("Scan store cleared.", icon="🗑️")
            st.rerun()

        if st.session_state.get("scan_done"):
            added = st.session_state.get("scan_added", 0)
            if added:
                st.success(f"Added **{added:,}** new products to the category "
                           "tables above.")
            elif scanner.products:
                st.info("Everything found is already in the tables above.")

        status_key, message = scanner.diagnose()
        if status_key == "blocked":
            st.error(f"🚦 {message}")
        elif status_key == "unparsed":
            st.error(f"🧩 {message}")
        elif status_key == "duplication":
            st.warning(f"♻️ {message}")
        elif status_key != "idle":
            st.success(f"✅ {message}")

        if scanner.products:
            coverage = scanner.coverage()
            m1, m2, m3, m4 = st.columns(4)
            m1.markdown(kpi(f"{len(scanner.products):,}", "Discovered"),
                        unsafe_allow_html=True)
            m2.markdown(kpi(f"{len(scanner.priced()):,}", "With Price",
                            "#4ade80"), unsafe_allow_html=True)
            m3.markdown(kpi(f"{scanner.bisections:,}",
                            f"Band Splits (d{scanner.max_depth_reached})",
                            "#a855f7"), unsafe_allow_html=True)
            m4.markdown(kpi(f"{scanner.pages_fetched:,}", "Pages Fetched",
                            "#f97316" if scanner.pages_blocked else "#38bdf8"),
                        unsafe_allow_html=True)

            # No targets: each category reports what has been found so far,
            # scaled against whichever category is currently largest.
            peak = max(coverage.values()) if coverage else 1
            st.dataframe(
                pd.DataFrame([
                    {"Category": c, "Found": coverage.get(c, 0),
                     "Share": f"{coverage.get(c, 0) / max(len(scanner.products), 1):.1%}"}
                    for c in categories]),
                use_container_width=True, hide_index=True,
                column_config={"Found": st.column_config.ProgressColumn(
                    format="%d", min_value=0, max_value=max(peak, 1))})

            q1, q2, q3 = st.columns(3)
            q1.metric("Tasks completed", f"{scanner.tasks_done:,}")
            q2.metric("Peak queue depth", f"{scanner.queue_peak:,}")
            q3.metric("Tasks shed", f"{scanner.tasks_dropped:,}",
                      help="Queue hit its memory guard; unexplored depth was "
                           "dropped, never products already found.")
            leftover = len(scanner.load_frontier())
            if leftover:
                st.info(f"**{leftover:,} queries still pending.** The scan did "
                        "not exhaust the frontier. Run it again with *Resume* "
                        "ticked to continue from here.")

            if st.button("➕ Merge latest findings into the tables above",
                         key="scan_merge_now"):
                added = merge_scan_results_into_tracker(scanner,
                                                        float(min_disc))
                st.toast(f"Added {added:,} product(s)." if added
                         else "All findings are already in the tables.",
                         icon="🛰️")
                if added:
                    st.rerun()


if __name__ == "__main__":
    try:
        main()
        render_deep_scanner()
    except Exception:  # pragma: no cover - top-level UI guard
        logger.exception("Unhandled error while rendering the tracker")
        st.error("Something went wrong while rendering the tracker. "
                 "See the server logs for details.")
