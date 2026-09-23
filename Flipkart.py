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


if __name__ == "__main__":
    try:
        main()
    except Exception:  # pragma: no cover - top-level UI guard
        logger.exception("Unhandled error while rendering the tracker")
        st.error("Something went wrong while rendering the tracker. See the server logs for details.")
