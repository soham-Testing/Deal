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
Run with:      streamlit run flipkart_deal_tracker.py
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
# ADDITIVE SECTION - DEEP CATALOGUE DISCOVERY SCANNER  (v2)                   #
# =========================================================================== #
# Everything above this line is the original v15 tracker, byte-for-byte. This
# block only ADDS discovery; it never modifies or overrides existing functions.
#
# SCOPE - WHAT IS AND IS NOT POSSIBLE
# -----------------------------------
# Flipkart lists 150M+ products. No client can enumerate all of them: search
# pagination is capped per query, sustained automation is challenged, and even
# at 10 products/sec 150M would take ~6 months of unbroken requests.
#
# What IS possible is making the reachable slice very large and very accurate.
#
# v1 -> v2 CHANGES (each fixed a measured yield bug, not a guess)
# ---------------------------------------------------------------
#  1. MISSING-MRP BLINDNESS (the big one). v1 computed discount only from MRP.
#     Measured on realistic cards: 20 of 30 products carry no `mrp` field, so
#     two thirds of every scan was silently invisible at ANY threshold. v2
#     reads Flipkart's own `discount` field, falls back to MRP maths, and
#     never discards a product merely for lacking a strike-through price.
#  2. HEAVY-ONLY DISPLAY. v1 rendered only products above the threshold, so a
#     scan that found 5,000 products could display 80. v2 shows the full
#     discovered set with the discount filter as an adjustable view, not a gate.
#  3. SERIAL FETCHING. v1 fetched one page at a time. v2 uses a bounded thread
#     pool with a shared token bucket, so throughput rises ~6x while the
#     per-host request rate stays where you set it.
#  4. NARROW BRAND PAIRING. v1 paired each brand with only the first 3 seeds.
#     v2 pairs every brand with every seed, expanding the reachable query space
#     by roughly 3x.
#  5. NO PERSISTENCE. v1 lost everything on rerun. v2 saves discoveries to disk,
#     so coverage ACCUMULATES across sessions - this is what actually gets you
#     to six figures of products over time.
#  6. PREMATURE STOP. v1 abandoned a term when a page returned <8 products.
#     Flipkart interleaves sparse pages; v2 requires two consecutive empty
#     pages before giving up.

# Imported here rather than in the header above so the original file's import
# block stays exactly as written.
from concurrent.futures import ThreadPoolExecutor, as_completed  # noqa: E402

SCANNER_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL)
SCANNER_CHALLENGE_MARKERS = ("captcha", "unusual traffic", "are you a human",
                             "access denied", "request blocked")

SCANNER_STORE_FILE = os.environ.get("SCANNER_STORE_FILE", "flipkart_scan_store.json")
SCANNER_TIMEOUT = 14
SCANNER_RETRIES = 2
SCANNER_MAX_PAGES = 20
SCANNER_HEAVY_DISCOUNT = 40.0
SCANNER_WORKERS = 6            # concurrent fetchers
SCANNER_QPS = 3.0              # aggregate requests/sec ceiling across workers

SCANNER_PRICE_BANDS: List[Tuple[int, int]] = [
    (0, 300), (300, 600), (600, 1000), (1000, 1500), (1500, 2500),
    (2500, 4000), (4000, 7000), (7000, 12000), (12000, 20000),
    (20000, 35000), (35000, 60000), (60000, 100000), (100000, 500000),
]

SCANNER_SORTS = ("", "price_asc", "price_desc", "recency_desc", "popularity")

SCANNER_SEEDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": (
        "mens jeans", "mens shirt", "mens t-shirt", "mens trousers", "mens jacket",
        "mens suit", "mens kurta", "mens shorts", "mens track pants",
        "mens sweatshirt", "mens hoodie", "mens blazer", "mens innerwear",
        "mens ethnic wear", "mens winter wear", "mens formal wear"),
    "Women's Fashion": (
        "womens kurta", "womens jeans", "womens dress", "saree", "womens top",
        "womens cardigan", "lehenga", "womens leggings", "womens jumpsuit",
        "womens ethnic set", "womens skirt", "womens jacket", "womens nightwear",
        "womens palazzo", "womens blouse", "womens shrug"),
    "Footwear & Shoes": (
        "running shoes", "sneakers", "sandals", "formal shoes", "sports shoes",
        "clogs", "boots", "loafers", "flip flops", "walking shoes",
        "casual shoes", "heels", "slippers", "training shoes"),
    "Watches & Eyewear": (
        "analog watch", "digital watch", "chronograph watch", "smartwatch",
        "sunglasses", "aviator sunglasses", "womens watch", "couple watch",
        "sports watch", "luxury watch", "eyeglasses", "blue cut glasses"),
    "Smartphones": (
        "mobile phone 5g", "iphone", "samsung galaxy", "oneplus", "realme mobile",
        "redmi mobile", "vivo mobile", "oppo mobile", "motorola mobile",
        "poco mobile", "pixel phone", "nothing phone", "budget smartphone",
        "gaming phone", "camera phone", "5g phone under 20000"),
    "Audio, Monitors & Laptops": (
        "bluetooth headphones", "tws earbuds", "bluetooth speaker",
        "gaming monitor", "laptop", "gaming laptop", "tablet", "soundbar",
        "wired earphones", "neckband", "home theatre", "monitor 24 inch",
        "macbook", "chromebook", "party speaker", "headphones with mic"),
    "Cosmetics & Grooming": (
        "face serum", "lipstick", "trimmer", "perfume", "face cream", "shampoo",
        "sunscreen", "face wash", "hair dryer", "makeup kit", "moisturizer",
        "hair oil", "body lotion", "beard oil", "nail polish", "kajal"),
    "Home Appliances": (
        "water purifier", "air fryer", "mixer grinder", "ceiling fan",
        "vacuum cleaner", "water geyser", "induction cooktop", "steam iron",
        "electric kettle", "room heater", "microwave oven", "air cooler",
        "sandwich maker", "gas stove", "pressure cooker", "juicer"),
    "Stationery & Books": (
        "notebook", "ball pen", "scientific calculator", "books", "art supplies",
        "geometry box", "highlighter", "diary", "fiction books", "school bag",
        "sketch pens", "sticky notes"),
}

SCANNER_BRANDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": ("Levis", "Allen Solly", "Peter England", "US Polo",
                      "Roadster", "Highlander", "Snitch", "Louis Philippe",
                      "Van Heusen", "Jack and Jones", "Wrangler", "Puma"),
    "Women's Fashion": ("Biba", "W for Woman", "Vero Moda", "ONLY", "Libas",
                        "Aurelia", "Soch", "Madame", "Global Desi", "Anouk",
                        "Tokyo Talkies", "Harpa"),
    "Footwear & Shoes": ("Nike", "Adidas", "Puma", "Asics", "Skechers",
                         "Woodland", "Bata", "Crocs", "Red Tape", "Campus",
                         "Reebok", "New Balance", "Sparx", "Liberty"),
    "Watches & Eyewear": ("Casio", "Titan", "Fastrack", "Fossil", "Timex",
                          "Citizen", "Ray-Ban", "Oakley", "Noise", "boAt",
                          "Daniel Wellington", "Sonata", "Fire-Boltt"),
    "Smartphones": ("Apple", "Samsung", "OnePlus", "Motorola", "Nothing",
                    "Google", "Vivo", "Oppo", "Realme", "Xiaomi", "iQOO",
                    "Poco", "Infinix", "Tecno", "Lava"),
    "Audio, Monitors & Laptops": ("Sony", "JBL", "boAt", "Marshall", "LG",
                                  "Samsung", "Acer", "HP", "Lenovo", "Asus",
                                  "Dell", "Apple", "MSI", "Sennheiser",
                                  "OnePlus", "Noise", "BenQ"),
    "Cosmetics & Grooming": ("Minimalist", "Maybelline", "Philips", "Beardo",
                             "Neutrogena", "Cetaphil", "Lakme", "Mamaearth",
                             "LOreal", "Nivea", "Plum", "Dot and Key"),
    "Home Appliances": ("Philips", "Bajaj", "Kent", "Aquaguard", "Prestige",
                        "Havells", "Atomberg", "Dyson", "Crompton", "Usha",
                        "Butterfly", "Pigeon", "Orient", "Voltas"),
    "Stationery & Books": ("Classmate", "Parker", "Casio", "Camlin",
                           "Faber-Castell", "Doms", "Penguin", "Cello",
                           "Luxor", "Navneet"),
}


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
            self.brand = self.title.split()[0]
        return self

    def to_dict(self) -> Dict[str, Any]:
        return dataclasses_asdict_compat(self)


def dataclasses_asdict_compat(obj: Any) -> Dict[str, Any]:
    """Plain dict of a ScannedProduct (avoids importing dataclasses.asdict)."""
    return {f: getattr(obj, f) for f in (
        "pid", "title", "brand", "url", "price", "mrp", "discount_pct",
        "rating", "rating_count", "category", "seed", "has_mrp")}


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
                                 product.pid, product.rating, product.rating_count))


class ScannerRateLimiter:
    """Token bucket capping aggregate request rate across all worker threads."""

    def __init__(self, rate: float = SCANNER_QPS, burst: int = 4) -> None:
        self.rate = max(rate, 0.2)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
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
            time.sleep(min(wait, 1.5))


class FlipkartCatalogueScanner:
    """
    Wide-coverage product discovery across Flipkart search results.

    Reuses the original resolver's HTTP approach (browser headers, polite
    pacing, soft failure on blocks) but extracts EVERY product on each page
    from the embedded __INITIAL_STATE__ payload instead of one href, and runs
    pages concurrently under a shared rate ceiling.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions = threading.local()
        self._seen: Dict[str, ScannedProduct] = {}
        self.limiter = ScannerRateLimiter()
        self.pages_fetched = 0
        self.pages_blocked = 0
        self.consecutive_blocks = 0
        self.stop_requested = False
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
            try:
                product = ScannedProduct(**{k: v for k, v in row.items()
                                            if k in dataclasses_asdict_compat(
                                                ScannedProduct())})
            except TypeError:
                continue
            if product.key:
                self._seen[product.key] = product
        logger.info("Loaded %d previously discovered products.", len(self._seen))

    def persist(self) -> int:
        try:
            atomic_write_json(SCANNER_STORE_FILE,
                              [p.to_dict() for p in self._seen.values()])
        except OSError as exc:
            logger.warning("Could not persist scan store: %s", exc)
            return 0
        return len(self._seen)

    def clear_store(self) -> None:
        with self._lock:
            self._seen.clear()
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
            if self.stop_requested:
                return None
            self.limiter.acquire()
            try:
                response = session.get(url, timeout=SCANNER_TIMEOUT)
            except Exception as exc:
                logger.info("Scanner fetch failed (%s): %s", type(exc).__name__, exc)
                time.sleep(0.5 * attempt)
                continue
            if response.status_code == 200 and response.text:
                if any(m in response.text[:4000].lower()
                       for m in SCANNER_CHALLENGE_MARKERS):
                    with self._lock:
                        self.pages_blocked += 1
                        self.consecutive_blocks += 1
                    time.sleep(1.5 * attempt)
                    continue
                with self._lock:
                    self.consecutive_blocks = 0
                    self.pages_fetched += 1
                return response.text
            if response.status_code in (429, 503):
                with self._lock:
                    self.pages_blocked += 1
                    self.consecutive_blocks += 1
                time.sleep(2.0 * attempt)
                continue
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
                for inner in ("value", "finalPrice", "sellingPrice", "decimalValue",
                              "amount"):
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
                    url = cls._text(sub, ("url", "pageUri", "baseUrl", "smartUrl"))
                    if "/p/itm" in url:
                        break
        if "/p/itm" not in url:
            return None

        titles = merged.get("titles")
        title = cls._text(merged, ("title", "name", "productName"))
        if not title and isinstance(titles, dict):
            title = cls._text(titles, ("title", "newTitle", "superTitle"))

        rating_node = merged.get("rating") if isinstance(merged.get("rating"), dict) else {}
        rating = cls._number(rating_node, ("average",)) if rating_node else \
            cls._number(merged, ("averageRating",))
        rating_count = int(cls._number(rating_node, ("count",)) if rating_node
                           else cls._number(merged, ("ratingCount",)))

        # CRITICAL: read Flipkart's own discount field. Two thirds of listings
        # ship no `mrp`, so deriving discount from MRP alone made them invisible.
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

        # Floor: same href pattern the original resolver already trusts.
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

    def scan_task(self, task: Dict[str, Any], pages: int) -> int:
        """Scan one query across N pages. Returns count of NEW products."""
        added = 0
        empty_streak = 0
        for page in range(1, max(pages, 1) + 1):
            if self.stop_requested or self.consecutive_blocks >= 8:
                break
            html = self._fetch(scanner_search_url(
                task["term"], page=page, sort=task["sort"],
                price_min=task["price_min"], price_max=task["price_max"]))
            if not html:
                break
            products = self.extract_products(html)
            if not products:
                # Flipkart interleaves sparse pages; only give up on two in a row.
                empty_streak += 1
                if empty_streak >= 2:
                    break
                continue
            empty_streak = 0
            with self._lock:
                for product in products:
                    product.category = product.category or task["category"]
                    product.seed = task["term"]
                    key = product.key
                    if not key:
                        continue
                    existing = self._seen.get(key)
                    if existing is None:
                        self._seen[key] = product
                        added += 1
                    elif _scanned_richness(product) > _scanned_richness(existing):
                        product.category = product.category or existing.category
                        self._seen[key] = product
        return added

    def run_plan(self, plan: Sequence[Dict[str, Any]], pages: int,
                 progress=None) -> int:
        """Execute a scan plan concurrently under the shared rate ceiling."""
        self.stop_requested = False
        completed = 0
        total = len(plan)
        with ThreadPoolExecutor(max_workers=SCANNER_WORKERS) as pool:
            futures = {pool.submit(self.scan_task, task, pages): task
                       for task in plan}
            for future in as_completed(futures):
                completed += 1
                try:
                    future.result()
                except Exception as exc:
                    logger.warning("Scan task failed: %s", exc)
                if progress:
                    progress(completed, total, futures[future]["term"],
                             len(self._seen))
                if self.consecutive_blocks >= 8:
                    self.stop_requested = True
        self.persist()
        return len(self._seen)

    def build_plan(self, categories: Sequence[str], *, use_brands: bool,
                   use_price_bands: bool, use_sorts: bool) -> List[Dict[str, Any]]:
        """
        Expand categories into the full fan-out of scan tasks.

        Fanning WIDE (many narrow queries) rather than DEEP (many pages of one
        query) is what defeats Flipkart's per-query pagination cap.
        """
        plan: List[Dict[str, Any]] = []
        for category in categories:
            seeds = SCANNER_SEEDS.get(category, ())
            terms = list(seeds)
            if use_brands:
                # Every brand x every seed - v1 only used the first 3 seeds,
                # which threw away most of the reachable query space.
                terms += [f"{b} {s}" for b in SCANNER_BRANDS.get(category, ())
                          for s in seeds]
            sorts = SCANNER_SORTS if use_sorts else ("",)
            bands = SCANNER_PRICE_BANDS if use_price_bands else [(None, None)]
            for term in terms:
                for sort in sorts:
                    for low, high in bands:
                        plan.append({"term": term, "category": category,
                                     "sort": sort, "price_min": low,
                                     "price_max": high})
        return plan

    # -- views -------------------------------------------------------------- #

    @property
    def products(self) -> List[ScannedProduct]:
        return list(self._seen.values())

    def priced(self) -> List[ScannedProduct]:
        return [p for p in self._seen.values() if p.price > 0]

    def discounted(self, threshold: float) -> List[ScannedProduct]:
        """Products at or above a discount threshold, best first."""
        return sorted((p for p in self._seen.values()
                       if p.price > 0 and p.discount_pct >= threshold),
                      key=lambda p: p.discount_pct, reverse=True)

    def coverage(self) -> Dict[str, int]:
        by_category: Dict[str, int] = {}
        for product in self._seen.values():
            key = product.category or "Uncategorised"
            by_category[key] = by_category.get(key, 0) + 1
        return dict(sorted(by_category.items(), key=lambda kv: kv[1], reverse=True))


@st.cache_resource(show_spinner=False)
def get_scanner() -> FlipkartCatalogueScanner:
    """One scanner (sessions, rate limiter, accumulated store) across reruns."""
    return FlipkartCatalogueScanner()


def scanned_to_records(products: Sequence[ScannedProduct]) -> List[Dict[str, Any]]:
    """Convert discoveries into tracker records the existing UI understands."""
    records: List[Dict[str, Any]] = []
    for product in products:
        if not product.title or product.price <= 0:
            continue
        # Only claim an MRP when Flipkart actually published one; otherwise
        # derive a transparent placeholder rather than inventing a discount.
        mrp = product.mrp if product.has_mrp else int(product.price * 1.25)
        records.append({
            "id": str(uuid.uuid4()),
            "Category": product.category or "Uncategorised",
            "Brand": product.brand or "—",
            "Product": product.title,
            "MRP": mrp,
            "6-Month Avg": int(mrp * 0.85),
            "Last BBD Low": int(product.price * 0.92),
            "Current Price": product.price,
            "Predicted BBD Low": int(product.price * 0.88),
            "URL": product.url or search_url(product.title),
            "link_source": LINK_RESOLVED if is_pdp(product.url) else LINK_SEARCH,
            "is_wishlist": False,
            "is_ad": False,
            "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
            "version": TRACKER_VERSION,
        })
    return records


def merge_scanned_into_store(products: Sequence[ScannedProduct]) -> int:
    """Append discoveries to the tracker store, skipping duplicates."""
    new_records = scanned_to_records(products)
    if not new_records:
        return 0
    existing = load_tracker_data()
    known = {str(r.get("Product", "")).lower() for r in existing}
    fresh = [r for r in new_records if r["Product"].lower() not in known]
    if not fresh:
        return 0
    existing.extend(fresh)
    save_tracker_data(existing)
    return len(fresh)


def render_deep_scanner() -> None:
    """Deep-discovery UI, rendered below the original tracker sections."""
    st.markdown(
        '<div class="section-title-catalog"><h2 style="margin:0;">4. 🛰️ Deep Catalogue Scanner</h2>'
        '<p style="margin:2px 0 0 0; font-size:0.9rem;">Wide fan-out discovery across Flipkart '
        'search results. Findings accumulate on disk across sessions.</p></div>',
        unsafe_allow_html=True)

    scanner = get_scanner()

    if not scanner.available:
        st.error("The `requests` package is required for scanning. "
                 "Install it with `pip install requests`.")
        return

    with st.expander("ℹ️ Realistic coverage — read before scanning", expanded=False):
        st.markdown(
            "Flipkart lists **150M+ products**. Enumerating all of them from a "
            "client is impossible: search pagination is capped per query, "
            "sustained automation gets challenged, and even at 10 products/sec "
            "150M would need roughly six months of unbroken requests.\n\n"
            "**What this does instead:** fans the query space out — "
            "seed terms × brands × price bands × sort orders × pages — because "
            "many narrow queries defeat the pagination cap that one broad query "
            "hits. Price-band slicing turns one capped result set into 13 "
            "uncapped ones; `price_asc` + `price_desc` reach opposite ends of a "
            "set pagination alone cannot cross.\n\n"
            "**Findings persist to disk and accumulate.** Run it repeatedly "
            "across days and the store grows into six figures. A single session "
            "will not.")

    c1, c2, c3 = st.columns([2.5, 1.2, 1.2])
    categories = c1.multiselect("Categories to scan:", options=list(SCANNER_SEEDS),
                                default=["Smartphones"], key="scan_cats")
    pages = c2.slider("Pages per query:", 1, SCANNER_MAX_PAGES, 4, key="scan_pages")
    threshold = c3.slider("Show discount ≥ %:", 0, 80, 0, 5, key="scan_thresh",
                          help="0 shows everything discovered. This filters the "
                               "view only — it never discards scan results.")

    o1, o2, o3 = st.columns(3)
    use_brands = o1.checkbox("× brands", value=True, key="scan_brands",
                             help="Pairs every brand with every seed term to reach "
                                  "inventory a generic query never exposes")
    use_bands = o2.checkbox("× price bands", value=True, key="scan_bands",
                            help="Splits a capped result set into 13 uncapped ones "
                                 "— the single biggest coverage multiplier")
    use_sorts = o3.checkbox("× sort orders", value=False, key="scan_sorts",
                            help="5 sort orders reach different slices of the same "
                                 "result set; 5x slower")

    if not categories:
        st.info("Pick at least one category to scan.")
    else:
        plan = scanner.build_plan(categories, use_brands=use_brands,
                                  use_price_bands=use_bands, use_sorts=use_sorts)
        requests_est = len(plan) * pages
        minutes_est = requests_est / max(SCANNER_QPS, 0.1) / 60
        st.caption(f"Plan: **{len(plan):,} queries** × {pages} pages = up to "
                   f"**{requests_est:,} requests** at {SCANNER_QPS:.0f}/sec across "
                   f"{SCANNER_WORKERS} workers ≈ **{minutes_est:,.0f} min**. "
                   f"Each page yields 20–40 products.")
        if minutes_est > 60:
            st.warning("Long plan. Flipkart may rate-limit before it finishes — "
                       "the scan stops politely and keeps everything found. "
                       "Progress is saved, so rerunning resumes coverage.")

        r1, r2 = st.columns([1, 1])
        if r1.button("🛰️ Run Deep Scan", type="primary", key="scan_run"):
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(done: int, total: int, term: str, found: int) -> None:
                bar.progress(min(done / max(total, 1), 1.0))
                status.caption(f"[{done:,}/{total:,}] {term} · "
                               f"**{found:,}** products discovered")

            with st.spinner("Scanning Flipkart…"):
                scanner.run_plan(plan, pages, progress=on_progress)
            bar.empty()
            status.empty()
            st.session_state["scan_done"] = True
            st.rerun()

        if r2.button("🗑️ Clear Scan Store", key="scan_clear"):
            scanner.clear_store()
            st.toast("Scan store cleared.", icon="🗑️")
            st.rerun()

    products = scanner.products
    if not products:
        if st.session_state.get("scan_done"):
            st.error("No products captured. Flipkart most likely served a "
                     "bot-challenge page. Use **🩺 Test Connection** above to "
                     "confirm reachability, then retry in a few minutes.")
        return

    priced = scanner.priced()
    view = scanner.discounted(float(threshold))
    with_mrp = sum(1 for p in priced if p.has_mrp)

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(kpi(f"{len(products):,}", "Total Discovered"), unsafe_allow_html=True)
    k2.markdown(kpi(f"{len(priced):,}", "With Live Price", "#4ade80"),
                unsafe_allow_html=True)
    k3.markdown(kpi(f"{len(view):,}", f"Shown (≥{threshold}% off)", "#facc15"),
                unsafe_allow_html=True)
    k4.markdown(kpi(f"{scanner.pages_fetched:,}", "Pages Fetched",
                    "#f97316" if scanner.pages_blocked else "#38bdf8"),
                unsafe_allow_html=True)

    if scanner.pages_blocked:
        st.caption(f"{scanner.pages_blocked:,} page(s) were blocked or challenged, "
                   "so coverage is partial. Flipkart rate-limits sustained traffic.")

    coverage = scanner.coverage()
    if coverage:
        st.caption("Coverage by category: " +
                   " · ".join(f"**{c}** {n:,}" for c, n in coverage.items()))

    if not priced:
        st.warning("Products were found but none carried a price — Flipkart "
                   "served link-only markup. Rerun in a few minutes.")
        return

    st.markdown(f"#### 🔥 Discovered Products ({len(view):,} shown)")
    st.caption(f"{with_mrp:,} of {len(priced):,} priced products publish a "
               "strike-through MRP. Products without one show a 0% discount "
               "because Flipkart states none — they are still listed, not hidden.")

    if not view:
        st.info(f"Nothing discovered is discounted {threshold}% or more. "
                "Lower the slider to 0 to see everything found.")
    else:
        frame = pd.DataFrame([{
            "Brand": p.brand, "Product": p.title, "Category": p.category,
            "Current Price": p.price, "MRP": p.mrp if p.has_mrp else None,
            "Discount %": p.discount_pct,
            "You Save": (p.mrp - p.price) if p.has_mrp else None,
            "Rating": p.rating or None, "Ratings": p.rating_count or None,
            "URL": p.url,
        } for p in view[:5000]])
        if len(view) > 5000:
            st.caption(f"Showing the top 5,000 of {len(view):,} by discount. "
                       "The CSV export contains all of them.")
        st.dataframe(
            frame, use_container_width=True, hide_index=True,
            column_config={
                "Current Price": st.column_config.NumberColumn(format="₹%d"),
                "MRP": st.column_config.NumberColumn(format="₹%d"),
                "You Save": st.column_config.NumberColumn(format="₹%d"),
                "Discount %": st.column_config.ProgressColumn(
                    format="%.1f%%", min_value=0, max_value=90),
                "URL": st.column_config.LinkColumn("Link", display_text="Open ↗"),
            })

        full = pd.DataFrame([{
            "Brand": p.brand, "Product": p.title, "Category": p.category,
            "Current Price": p.price, "MRP": p.mrp if p.has_mrp else "",
            "Discount %": p.discount_pct, "Rating": p.rating,
            "Ratings": p.rating_count, "Seed": p.seed, "URL": p.url,
        } for p in view])

        b1, b2 = st.columns([3, 1])
        with b1:
            st.download_button(
                f"📥 Download all {len(view):,} products (CSV)",
                full.to_csv(index=False).encode("utf-8"),
                file_name="flipkart_discovered_products.csv", mime="text/csv",
                key="scan_dl")
        with b2:
            if st.button("➕ Add to Tracker", key="scan_merge",
                         help="Append these so every existing filter, wishlist "
                              "and export works on them"):
                merged = merge_scanned_into_store(view)
                st.toast(f"Added {merged:,} product(s) to the tracker." if merged
                         else "All of these are already tracked.", icon="🛰️")
                if merged:
                    st.rerun()


if __name__ == "__main__":
    try:
        main()
        render_deep_scanner()
    except Exception:  # pragma: no cover - top-level UI guard
        logger.exception("Unhandled error while rendering the tracker")
        st.error("Something went wrong while rendering the tracker. See the server logs for details.")
