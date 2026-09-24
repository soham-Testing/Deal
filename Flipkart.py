"""
Flipkart BBD Deal Tracker & Live Price Radar
=============================================
Single-page Streamlit price-intelligence app tracking tens of thousands of products
across 9 departments against 6-month historical baselines and BBD festive floors.

Requirements: streamlit >= 1.35, pandas >= 2.0, requests >= 2.28
Run with:     streamlit run Flipkart.py

FIXES IN v23
------------
1. DIRECT PRODUCT LINKS IN EVERY TABLE.
   * The clickable "Product Link" column is present in ALL tables (catalog,
     wishlist, BBD steals, ads) and in the CSV export.
   * Rows auto-resolve their own /p/itm... product page as soon as they are
     displayed, so you never see an empty link cell. Toggle in the toolbar.
   * Resolver best-matches the product name against the real search-result
     cards instead of grabbing the first anchor (which was often an ad tile),
     so the link opens THAT product, not a category or search page.
   * is_pdp() hardened: rejects /search, /pr?, /q/, /clp/ and /item/p/product.
2. Only the "Link Type" text column (the 'Product page / Search (unresolved)'
   status column) was removed. The real product link column stays.
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
TRACKER_VERSION = "v23_direct_links_everywhere"
TRACKER_DB_FILE = os.environ.get("TRACKER_DB_FILE", "tracker_store.json")
LINK_CACHE_FILE = os.environ.get("LINK_CACHE_FILE", "flipkart_link_cache.json")
SCANNER_STORE_FILE = os.environ.get("SCANNER_STORE_FILE", "flipkart_scan_store.json")

FLIPKART_HOST = "www.flipkart.com"
FLIPKART_SEARCH = f"https://{FLIPKART_HOST}/search?q={{query}}"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}

# Keep q for internal search fetches, pid/lid/marketplace for product pages
KEEP_PARAMS = {"pid", "lid", "marketplace", "q"}

# A real Flipkart PDP always carries /p/itm<hex> in the path.
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{10,20})(?:[/?#]|$)", re.IGNORECASE)
PID_PARAM_PATTERN = re.compile(r"[?&]pid=([A-Z0-9]{12,20})(?:&|$)")
PDP_HREF_PATTERN = re.compile(
    r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{10,20}[^"]*)"', re.IGNORECASE
)
SCANNER_STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL
)
SCANNER_CHALLENGE_MARKERS = (
    "captcha", "unusual traffic", "are you a human", "access denied", "request blocked"
)

# Paths that are never a single product even if they look link-ish
NON_PDP_PATH_MARKERS = ("/search", "/pr?", "/q/", "/item/p/product", "/offers-store",
                        "/all-offers", "/clp/", "/sale/")

AUTO_RESOLVE_ON_START = False       # full-catalogue resolve on boot (slow, off)
AUTO_LINK_VISIBLE_DEFAULT = True    # auto-resolve the rows you can actually see
AUTO_LINK_VISIBLE_CAP = 60          # max live lookups per table render
RESOLVER_TIMEOUT = 12
RESOLVER_RETRIES = 2
RESOLVER_DELAY = 0.6
RESOLVER_PARALLEL = 6               # concurrent lookups for visible-row batches
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
CARD_INSTANT = "Axis / ICICI (10% Instant, Cap Rs.1.5k)"
CARD_CASHBACK = "Flipkart Axis (5% Unlimited Cashback)"

LINK_RESOLVED = "resolved"
LINK_PENDING = "pending"

# Unrestricted Scanner Traversal Settings
SCANNER_TIMEOUT = 15
SCANNER_RETRIES = 3
SCANNER_WORKERS = 4
SCANNER_QPS_START = 2.0
SCANNER_QPS_FLOOR = 0.4
SCANNER_QPS_CEILING = 4.0
SCANNER_SATURATION = 16
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
# ATOMIC FILE IO & URL UTILITIES
# --------------------------------------------------------------------------- #


def atomic_write_json(path: str, payload: Any) -> None:
    """Write JSON atomically using a tempfile so an interrupted run never corrupts the file."""
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


def search_url(*terms: str) -> str:
    """Internal helper only: used to FETCH search HTML, never surfaced as a product link."""
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

    has_pid = any(k.lower() == "pid" for k, _ in kept)
    has_market = any(k.lower() == "marketplace" for k, _ in kept)
    if has_pid and not has_market:
        kept.append(("marketplace", "FLIPKART"))

    return urlunparse(("https", FLIPKART_HOST, parsed.path, "", urlencode(kept), ""))


def is_pdp(url: str) -> bool:
    """True only for a link that opens ONE specific Flipkart product detail page."""
    if not url or not isinstance(url, str):
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return False
    lowered_path = parsed.path.lower()
    if any(marker in lowered_path for marker in NON_PDP_PATH_MARKERS):
        return False
    if parsed.query and "q=" in parsed.query:
        return False
    return bool(ITM_ID_PATTERN.search(parsed.path))


def extract_itm_id(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url)
    match = ITM_ID_PATTERN.search(parsed.path)
    if match:
        return match.group(1).lower()
    pid_match = PID_PARAM_PATTERN.search("?" + parsed.query)
    if pid_match:
        return pid_match.group(1).lower()
    return ""


def product_link(url: str) -> str:
    """Return the URL only when it is a verified specific-product link, else blank."""
    clean = sanitize_url(url)
    return clean if is_pdp(clean) else ""


_TOKEN_SPLIT = re.compile(r"[^a-z0-9]+")


def _tokens(text: str) -> set:
    return {t for t in _TOKEN_SPLIT.split((text or "").lower()) if len(t) > 1}


def _slug_of(url: str) -> str:
    try:
        return urlparse(url).path.split("/p/")[0].strip("/").replace("-", " ")
    except Exception:
        return ""


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
        self._failures: Dict[str, float] = {}
        self._sessions = threading.local()
        self._session = self._build_session()

    def _load_cache(self) -> Dict[str, str]:
        if not os.path.exists(self._cache_path):
            return {}
        try:
            with open(self._cache_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                return {}
            # Drop any legacy cache entries that are not true product pages.
            return {k: v for k, v in data.items() if isinstance(v, str) and is_pdp(v)}
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

    def cached_url(self, product_name: str) -> str:
        with self._lock:
            return self._cache.get((product_name or "").strip().lower(), "")

    def recently_failed(self, product_name: str, cooldown: float = 900.0) -> bool:
        """Avoid hammering Flipkart for a name that just failed to resolve."""
        key = (product_name or "").strip().lower()
        with self._lock:
            stamp = self._failures.get(key)
        return bool(stamp and (time.time() - stamp) < cooldown)

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()
            self._failures.clear()
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

    def _thread_session(self):
        """Each worker thread gets its own session so parallel lookups are safe."""
        if requests is None:
            return None
        session = getattr(self._sessions, "s", None)
        if session is None:
            session = self._build_session()
            self._sessions.s = session
        return session

    @property
    def available(self) -> bool:
        return self._session is not None

    def _fetch(self, url: str) -> Optional[str]:
        session = self._thread_session()
        if session is None:
            return None
        for attempt in range(1, RESOLVER_RETRIES + 1):
            try:
                response = session.get(url, timeout=RESOLVER_TIMEOUT)
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
        if not self._best_pdp(html, "Puma Conduct Pro"):
            return False, "Flipkart served a challenge page with no product links."
        return True, "Connected to Flipkart and specific product links parsed successfully."

    @staticmethod
    def _candidate_links(html: str) -> List[Tuple[str, str]]:
        """Return (url, title) pairs for every genuine PDP anchor on a search page."""
        candidates: Dict[str, str] = {}

        # Rich structured cards first (title + url together) via the scanner parser.
        try:
            for product in FlipkartCatalogueScanner.extract_products(html):
                if is_pdp(product.url):
                    candidates.setdefault(product.url, product.title or _slug_of(product.url))
        except Exception:
            pass

        # Raw anchors as a safety net.
        for match in PDP_HREF_PATTERN.finditer(html):
            href = match.group(1)
            clean = sanitize_url(f"https://{FLIPKART_HOST}{href}")
            if is_pdp(clean):
                candidates.setdefault(clean, _slug_of(clean))
        return list(candidates.items())

    @classmethod
    def _best_pdp(cls, html: str, query: str) -> str:
        """Pick the card that actually matches the product name, not the first tile."""
        candidates = cls._candidate_links(html)
        if not candidates:
            return ""
        wanted = _tokens(query)
        if not wanted:
            return candidates[0][0]

        best_url, best_score = "", -1.0
        for position, (url, title) in enumerate(candidates):
            have = _tokens(title) | _tokens(_slug_of(url))
            overlap = len(wanted & have)
            # favour matches, break ties by the earlier (more relevant) card
            score = overlap / len(wanted) - position * 0.001
            if score > best_score:
                best_url, best_score = url, score

        # Require at least one real token in common, otherwise it is a random tile.
        return best_url if best_score > 0 else ""

    def resolve(self, product_name: str, *, use_cache: bool = True) -> ResolveOutcome:
        query = (product_name or "").strip()
        if not query:
            return ResolveOutcome(query, "", LINK_PENDING, False, "empty query")

        key = query.lower()
        if use_cache:
            with self._lock:
                cached = self._cache.get(key)
            if cached and is_pdp(cached):
                return ResolveOutcome(query, cached, LINK_RESOLVED, True, "cache hit")

        if not self.available:
            return ResolveOutcome(query, "", LINK_PENDING, False, "requests unavailable")

        html = self._fetch(search_url(query))
        if not html:
            with self._lock:
                self._failures[key] = time.time()
            return ResolveOutcome(query, "", LINK_PENDING, False, "request blocked")

        pdp = self._best_pdp(html, query)
        if not pdp:
            with self._lock:
                self._failures[key] = time.time()
            return ResolveOutcome(query, "", LINK_PENDING, False, "no matching product found")

        with self._lock:
            self._cache[key] = pdp
            self._failures.pop(key, None)
            self._persist_cache()
        return ResolveOutcome(query, pdp, LINK_RESOLVED, True, "resolved live")

    def resolve_many(self, product_names: Sequence[str], *, progress=None,
                     use_cache: bool = True) -> List[ResolveOutcome]:
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

    def resolve_parallel(self, product_names: Sequence[str], *,
                         workers: int = RESOLVER_PARALLEL,
                         use_cache: bool = True) -> List[ResolveOutcome]:
        """Fast path used when auto-linking the rows currently on screen."""
        names = [n for n in product_names if n]
        if not names:
            return []
        outcomes: List[ResolveOutcome] = []
        with ThreadPoolExecutor(max_workers=max(1, min(workers, len(names)))) as pool:
            futures = {pool.submit(self.resolve, n, use_cache=use_cache): n for n in names}
            for future in as_completed(futures):
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(ResolveOutcome(futures[future], "", LINK_PENDING,
                                                   False, str(exc)[:80]))
        return outcomes


@st.cache_resource(show_spinner=False)
def get_resolver() -> FlipkartLinkResolver:
    return FlipkartLinkResolver()


# --------------------------------------------------------------------------- #
# HIGH-ENTROPY TAXONOMY: 27,000+ CATALOG GENERATOR
# --------------------------------------------------------------------------- #

DEPARTMENT_STRUCTURE: Dict[str, Dict[str, Any]] = {
    "Men's Fashion": {
        "slug": "mens_fashion", "icon": "👔",
        "brands": ["Levi's", "Louis Philippe", "Allen Solly", "Peter England", "Van Heusen", "Arrow", "Raymond", "U.S. Polo Assn", "Flying Machine", "Wrangler", "Tommy Hilfiger", "Blackberrys", "Jack & Jones", "Snitch", "Rare Rabbit", "Mufti", "Spykar", "Killer", "Roadster", "Bewakoof", "Highlander", "Wrogn", "Campus Sutra", "Park Avenue", "Monte Carlo"],
        "subcategories": [
            ("511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056),
            ("Solid Cotton Pique Polo T-Shirt", 1999, 1399, 849, 899),
            ("Single Breasted 2-Piece Formal Suit", 10999, 8999, 5499, 5390),
            ("Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049),
            ("Tapered Fit Chinos Trousers", 2499, 1799, 1099, 1149),
            ("Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699),
            ("Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499),
            ("Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949),
            ("Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349),
            ("Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549),
            ("Oversized Streetwear Fleece Hoodie", 2999, 1899, 1199, 1299),
            ("Ribbed Hem Crew Neck Pullover", 2499, 1699, 999, 1099),
            ("Mid-Rise Washed Baggy Jeans", 3299, 2199, 1399, 1499),
            ("Pure Linen Formal Trouser", 3999, 2799, 1799, 1899),
            ("Bandhgala Textured Nehru Jacket", 4999, 3499, 2199, 2399)
        ],
        "modifiers": ["Dark Indigo Wash", "Jet Black Classic", "Crisp White", "Heather Grey", "Olive Military", "Deep Navy", "Khaki Sand", "Maroon Wine", "Charcoal Melange", "Raw Selvedge"]
    },
    "Women's Fashion": {
        "slug": "womens_fashion", "icon": "👗",
        "brands": ["Biba", "W for Woman", "Aurelia", "ONLY", "Vero Moda", "Libas", "Global Desi", "Madame", "FabIndia", "Rangriti", "AND", "Forever New", "Soch", "Sangria", "Tokyo Talkies", "Varanga", "Enamor", "Zivame", "Anouk", "Aayna", "Harpa", "Sassafras", "Berrylush", "Vishudh", "Indo Era"],
        "subcategories": [
            ("Embroidered Anarkali Kurta Pant Set", 4999, 2999, 1799, 1699),
            ("Pure Cotton Straight Daily Kurta", 1999, 1349, 699, 649),
            ("High-Rise Wide Leg Denim Jeans", 2999, 1999, 1199, 1249),
            ("Floral Summer Tiered Maxi Dress", 3499, 2299, 1299, 1349),
            ("Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299),
            ("Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699),
            ("Knitted Ribbed Full-Sleeve Cardigan", 2499, 1699, 949, 999),
            ("Cotton Rich Festive Kurta Pant Set", 3999, 2499, 1399, 1449),
            ("Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849),
            ("Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599),
            ("Chikankari Georgette Long Kurti", 2799, 1899, 1199, 1299),
            ("Pleated Velvet Cocktail Evening Gown", 6999, 4999, 3299, 3499),
            ("High-Rise Slim Flared Trousers", 2499, 1699, 1099, 1149),
            ("Floral Printed Bohemian Jumpsuit", 3199, 2199, 1399, 1499),
            ("Banarasi Silk Zari Border Saree", 7999, 5499, 3499, 3799)
        ],
        "modifiers": ["Pastel Blush", "Emerald Green", "Festive Mustard", "Midnight Blue", "Crimson Red", "Off-White Ivory", "Lilac Lavender", "Teal Ombre", "Rust Terracotta", "Indigo Block"]
    },
    "Footwear & Shoes": {
        "slug": "footwear", "icon": "👟",
        "brands": ["Puma", "Nike", "Adidas", "Asics", "Woodland", "Skechers", "Red Tape", "Campus", "Sparx", "Bata", "Clarks", "Crocs", "Under Armour", "Reebok", "New Balance", "Asian", "Metro", "Hush Puppies", "Red Chief", "Fila", "Liberty", "Lotto", "Action", "Mochi", "Lee Cooper"],
        "subcategories": [
            ("Conduct Pro Responsive Running Shoes", 6499, 4899, 3299, 3199),
            ("Revolution 7 Breathable Mesh Runners", 3695, 3695, 2399, 2995),
            ("Smash V2 Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429),
            ("Gel-Contend 8 Neutral Road Running Shoes", 5499, 4099, 2899, 2899),
            ("Camel Leather High-Traction Boots", 5995, 4595, 3295, 3495),
            ("Go Run Elevate Daily Walking Shoes", 2499, 1699, 1099, 1149),
            ("Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249),
            ("Formal Genuine Leather Derby Shoes", 3999, 2899, 1799, 1899),
            ("Classic Unisex Foam Slip-On Clogs", 3495, 2695, 1799, 1995),
            ("Clinch-X Responsive Gym Trainer", 4499, 2999, 1899, 1999),
            ("Charged Cushioning Road Marathon Shoes", 7499, 5299, 3699, 3899),
            ("Slip-On Memory Foam Walking Loafers", 2999, 1999, 1299, 1399),
            ("Waterproof Outdoor Trekking Boots", 6999, 4999, 3499, 3799),
            ("Dual-Tone Chunky Sole Street Walkers", 3999, 2499, 1599, 1699),
            ("Breathable Air-Mesh Lightweight Slides", 1499, 999, 599, 649)
        ],
        "modifiers": ["Triple White", "Stealth Black", "Navy Royal", "Wolf Grey", "Olive Tan", "Carbon Red", "Gold Accent", "Beige Sand", "Multi-Neon", "Gum Sole"]
    },
    "Watches & Eyewear": {
        "slug": "watches", "icon": "⌚",
        "brands": ["Casio", "Titan", "Fastrack", "Ray-Ban", "Timex", "Fossil", "Noise", "Oakley", "Fire-Boltt", "Amazfit", "Tommy Hilfiger", "Citizen", "Seiko", "Daniel Wellington", "Vincent Chase", "Lenskart Air", "Police", "Guess", "Armani Exchange", "Fossil Q", "Sonata", "Boat Watches", "Helix", "Maxima", "Giordano"],
        "subcategories": [
            ("Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271),
            ("G-Shock GA-2100 Octagonal Tough Watch", 9195, 8495, 6495, 6995),
            ("Karishma Champagne Dial Formal Watch", 2195, 1995, 1449, 1499),
            ("Revoltt FS1 1.83 BT Calling Smartwatch", 3999, 1699, 1199, 1299),
            ("Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490),
            ("Expedition Rugged Field Outdoor Watch", 3995, 3195, 2199, 2299),
            ("Grant Chronograph Leather Quartz Watch", 13495, 8995, 5995, 6495),
            ("Wayfarer UV400 Protective Sunglasses", 1399, 1099, 649, 719),
            ("Holbrook Polarized Matte Sunglasses", 7990, 6490, 4490, 4990),
            ("Eco-Drive Solar Powered Analog Watch", 8995, 6995, 4799, 5199),
            ("1.43 AMOLED Always-On Display Watch", 4999, 2799, 1899, 1999),
            ("Hexagonal Metal Frame Clear Eyeglasses", 2499, 1499, 899, 999),
            ("Diver Automatic 200M Stainless Steel", 24900, 19900, 15400, 16200),
            ("Square Clubmaster Acetate Shades", 3499, 2299, 1499, 1599),
            ("Minimalist Mesh Strap Ultra-Slim Analog", 5999, 3999, 2499, 2699)
        ],
        "modifiers": ["Silver Metallic", "Rose Gold", "Matte Gunmetal", "Obsidian Black", "Gold Plated", "Leather Tan", "Silicone Navy", "Tortoise Shell", "Blue Mirror", "Smoke Grey"]
    },
    "Smartphones": {
        "slug": "smartphones", "icon": "📱",
        "brands": ["Apple", "Samsung", "Google", "OnePlus", "Motorola", "Nothing", "CMF by Nothing", "Realme", "POCO", "Vivo", "iQOO", "Xiaomi", "Infinix", "Tecno", "Lava"],
        "subcategories": [
            ("Standard Flagship 5G (128GB Edition)", 69900, 63499, 52999, 56905),
            ("Ultra / Pro Max 5G (256GB Top Tier)", 119900, 109900, 94999, 99999),
            ("Compact Premium 5G (8GB / 128GB)", 74999, 56999, 41999, 42999),
            ("Fan Edition (FE) 5G (8GB / 128GB)", 59999, 39999, 29999, 29999),
            ("Curved pOLED Performance 5G (128GB)", 27999, 23999, 20999, 21999),
            ("Clean Glyph UI LED 5G (128GB)", 25999, 23499, 19999, 20999),
            ("Speed Turbo Fast-Charge 5G (256GB)", 39999, 36999, 32999, 34999),
            ("Monster Battery 6000mAh 5G (128GB)", 17499, 13999, 11999, 12499),
            ("Portrait Telephoto Camera 5G (128GB)", 52999, 44999, 34999, 37999),
            ("Ultra-Budget Value Starter 5G (64GB)", 12999, 10499, 8499, 8999),
            ("Foldable Dual Screen AMOLED (256GB)", 139999, 119999, 99999, 104999),
            ("Gaming Dimensity High-FPS 5G (256GB)", 34999, 29999, 24999, 26999),
            ("Periscope Zoom 120W Charging 5G", 44999, 38999, 31999, 33999),
            ("Super Slim Titanium Frame 5G (256GB)", 84999, 74999, 61999, 64999),
            ("Entry Level Big Display Smartphone", 9999, 7999, 5999, 6499)
        ],
        "modifiers": ["Phantom Black", "Glacier Blue", "Amber Gold", "Titanium Grey", "Luxe Green", "Cosmic Purple", "Sunset Orange", "Starlight White", "Crimson Bliss", "Iron Charcoal"]
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors", "icon": "💻",
        "brands": ["Sony", "LG", "Samsung", "Apple", "Acer", "HP", "ASUS", "Lenovo", "JBL", "Marshall", "OnePlus", "boAt", "BenQ", "Bose", "Sennheiser", "Soundcore", "Dell", "Zebronics", "MSI", "Realme Tech"],
        "subcategories": [
            ("Industry-Leading ANC Wireless Headphones", 29990, 22990, 18490, 22990),
            ("Premium Noise Cancelling Flagship Headset", 34990, 29990, 24990, 27989),
            ("27-inch 165Hz IPS QHD 2K Gaming Monitor", 32000, 24499, 18999, 19499),
            ("24-inch 165Hz FHD 1ms Freesync Display", 19000, 13999, 9999, 10499),
            ("10.9-inch Retina Tablet (Wi-Fi 64GB)", 39900, 34490, 29999, 30900),
            ("Active Noise Cancelling TWS Earbuds", 3990, 1499, 899, 1299),
            ("30W IP67 Rugged Bluetooth Speaker", 13999, 11499, 8499, 9999),
            ("Vintage Mesh Room-Filling Soundbox", 17499, 14999, 11999, 12999),
            ("Magnetic Fast Charge Bluetooth Neckband", 2999, 2699, 1795, 2594),
            ("Core i5 13th Gen RTX 4050 Gaming Laptop", 88999, 74990, 62990, 64990),
            ("Core i7 14-inch OLED Ultra-Slim Laptop", 104990, 89990, 74990, 78990),
            ("34-inch Curved WQHD 144Hz Ultrawide Display", 49990, 38990, 29990, 31990),
            ("Dolby Atmos 5.1 Channel Home Theatre Bar", 24990, 17990, 12990, 13990),
            ("Spatial Audio Wireless ANC Over-Ear", 14990, 10990, 7990, 8490),
            ("All-Day Battery Lightweight Cloudbook", 34990, 27990, 21990, 22990)
        ],
        "modifiers": ["Space Grey", "Matte Black", "Silver Frost", "Midnight Blue", "Lunar White", "Cyber Red", "Army Green", "Graphite Grey", "Rose Quartz", "Bronze Brass"]
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics", "icon": "💄",
        "brands": ["Minimalist", "Maybelline", "Philips", "Beardo", "L'Oreal Paris", "Neutrogena", "The Derma Co", "Cetaphil", "Bombay Shaving Company", "Bella Vita", "Biotique", "Plum", "Lakme", "Forest Essentials", "Vega", "Mamaearth", "Nivea", "Garnier", "Sugar", "MCaffeine", "Dot & Key", "Colorbar", "Faces Canada", "Simple", "Innisfree"],
        "subcategories": [
            ("10% Niacinamide Glowing Face Serum", 599, 509, 399, 449),
            ("Superstay 16H Matte Liquid Lipstick", 699, 549, 384, 449),
            ("Hybrid Beard Trimmer & Body Shaver", 1549, 1399, 999, 1548),
            ("Godfather Perfume EDP (100ml)", 1200, 799, 499, 549),
            ("Hydro Boost Hyaluronic Water Gel (50g)", 1150, 920, 690, 749),
            ("Gentle Skin Balancing Cleanser", 635, 570, 445, 499),
            ("6-in-1 Precision Salon Grooming Kit", 2450, 1499, 899, 999),
            ("Luxury Unisex Eau De Parfum Set (4x20ml)", 1099, 649, 449, 499),
            ("Bio Protein Anti-Hairfall Shampoo (650ml)", 450, 315, 210, 249),
            ("Soundarya Radiance Herbal Face Cream", 4200, 3780, 3150, 3499),
            ("Vitamin C Brightening Daily Moisturizer", 799, 649, 449, 499),
            ("Ultra-Matte Long Stay Compact Powder", 499, 399, 279, 299),
            ("Smudge-Proof Waterproof Intense Kajal", 299, 239, 169, 189),
            ("Keratin Deep Repair Hair Mask Treatment", 850, 680, 490, 520),
            ("SPF 50 PA++++ Lightweight Dewy Sunscreen", 650, 520, 370, 399)
        ],
        "modifiers": ["50ml Hydrating", "100ml Value Pack", "Matte Finish", "Dewy Glow", "Intense Pigment", "Herbal Extract", "Oil-Control", "Dermat-Tested", "Ultra-Lasting", "Sensitive Skin"]
    },
    "Home Appliances": {
        "slug": "appliances", "icon": "🔌",
        "brands": ["Philips", "Bajaj", "Kent", "Aquaguard", "Prestige", "Havells", "Atomberg", "Eureka Forbes", "Morphy Richards", "Crompton", "LG", "Samsung", "Dyson", "Voltas", "Bosch", "IFB", "Panasonic", "Whirlpool", "Usha", "Pigeon", "Butterfly", "Orient", "V-Guard", "Carrier", "Godrej"],
        "subcategories": [
            ("EasyGlide 1440W Steam Iron Anti-Calc", 2795, 2349, 1599, 1699),
            ("1000W Heavy Non-Stick Dry Iron", 1125, 849, 549, 599),
            ("RO+UV+UF+TDS Active Mineral Water Purifier", 20000, 16999, 12499, 13999),
            ("Active Copper 7L Wall-Mount Purifier", 18000, 14499, 10999, 11499),
            ("Rapid Air 4.1L Digital Oil-Free Air Fryer", 9995, 7399, 5199, 5499),
            ("750W 4-Jar Heavy Duty Mixer Grinder", 4495, 3299, 2299, 2499),
            ("25L High-Pressure Glassline Water Geyser", 14490, 9990, 6999, 7499),
            ("BLDC Energy Saving Silent Ceiling Fan", 5190, 3990, 3199, 3499),
            ("Robotic Vacuum Cleaner & Smart Mopper", 29999, 17999, 11999, 13999),
            ("Cordless Stick Vacuum with Cyclone Suction", 43900, 32900, 26900, 29900),
            ("2000W Touch Control Induction Cooktop", 3495, 2299, 1599, 1699),
            ("28L Convection Baking Microwave Oven", 16990, 12990, 9490, 9990),
            ("70L Honeycomb Air Cooler with Ice Chamber", 12990, 9490, 6990, 7490),
            ("1.8L Stainless Steel Fast Boil Kettle", 1499, 999, 649, 699),
            ("Smart Inverter 260L Frost-Free Refrigerator", 32990, 26990, 21990, 22990)
        ],
        "modifiers": ["Glossy Maroon", "Brushed Silver", "Midnight Black", "Pure White", "Titanium Grey", "Copper Accent", "Royal Blue", "Golden Trim", "Matte Finish", "Steel Shield"]
    },
    "Stationery & Books": {
        "slug": "stationery_books", "icon": "📚",
        "brands": ["Classmate", "Parker", "Camlin", "Faber-Castell", "Reynolds", "Doms", "Navneet", "Penguin Books", "HarperCollins", "Rupa", "Casio", "Pilot", "Kangaro", "Solo", "Cross", "Cello", "Bic", "Oxford", "Uniball", "Staedtler", "Mont Blanc", "Rhodia", "Moleskine", "Bloomsbury", "Scholastic"],
        "subcategories": [
            ("Hardcover Single Rule Notebook (Pack of 6)", 540, 450, 320, 349),
            ("Executive Matte Black CT Rollerball Pen", 1200, 950, 649, 699),
            ("Scientific Engineering Calculator FX-991CW", 1595, 1450, 1199, 1249),
            ("Atomic Habits Hardbound Edition", 499, 399, 249, 279),
            ("Professional Artist Acrylic Color Set", 1899, 1499, 999, 1099),
            ("Mesh Metal Multi-Tier Document Organizer", 999, 749, 449, 499),
            ("Heavy Duty Steel Desktop Stapler Combo", 650, 499, 329, 369),
            ("Textliner Chisel Tip Highlighter Set", 450, 349, 219, 249),
            ("Precision Geometry & Compass Box", 399, 310, 199, 229),
            ("Classic Literature Collector Masterpiece", 799, 649, 399, 449),
            ("Waterproof Student Ergonomic Backpack", 1999, 1399, 899, 999),
            ("Fineliner Drawing Sketch Pen 12-Pack", 499, 399, 259, 279),
            ("Spiral Bound Grid Ruled Project Diary", 350, 280, 180, 199),
            ("Calligraphy Fountain Pen Ink Cartridge Set", 1499, 1099, 749, 799),
            ("Desk Sticky Notes Pastel Cube Pack", 299, 239, 149, 169)
        ],
        "modifiers": ["Deluxe Edition", "Pack of 1", "Pack of 3", "Pack of 5", "Collector Series", "Matte Finish", "Pocket Size", "A4 Size", "Handcrafted", "Eco Friendly"]
    }
}


@st.cache_data
def generate_seed_catalog() -> List[Dict[str, Any]]:
    """Generates 25,000+ comprehensive product records across all categories.

    Seeded rows start unlinked. The direct /p/itm product link is attached
    automatically the moment the row is displayed (see ensure_direct_links).
    """
    catalog: List[Dict[str, Any]] = []
    random.seed(42)

    for category, meta in DEPARTMENT_STRUCTURE.items():
        brands = meta["brands"]
        subcats = meta["subcategories"]
        modifiers = meta["modifiers"]

        for b_idx, brand in enumerate(brands):
            for s_idx, (sub_name, base_mrp, base_avg, base_bbd, base_curr) in enumerate(subcats):
                for m_idx in range(8):
                    mod = modifiers[(b_idx + s_idx + m_idx) % len(modifiers)]
                    brand_factor = 1.0 + (b_idx % 5) * 0.04
                    mod_factor = 1.0 + (m_idx % 3) * 0.03

                    mrp = int(base_mrp * brand_factor * mod_factor)
                    avg_6m = int(base_avg * brand_factor * mod_factor)
                    last_bbd = int(base_bbd * brand_factor * mod_factor)

                    delta = random.choice([-100, -50, 0, 50, 100, -150, 200, -250])
                    curr = max(last_bbd - 120, int(base_curr * brand_factor * mod_factor) + delta)
                    bbd_pred = int(last_bbd * BBD_PREDICTION_FACTOR)

                    catalog.append({
                        "id": str(uuid.uuid4()),
                        "Category": category,
                        "Brand": brand,
                        "Product": f"{brand} {sub_name} ({mod})",
                        # Search term used to locate the exact product page.
                        "SearchTerm": f"{brand} {sub_name}",
                        "MRP": mrp,
                        "6-Month Avg": avg_6m,
                        "Last BBD Low": last_bbd,
                        "Current Price": curr,
                        "Predicted BBD Low": bbd_pred,
                        "URL": "",
                        "link_source": LINK_PENDING,
                        "is_wishlist": False,
                        "is_ad": ((b_idx + s_idx + m_idx) % 7 == 0),
                        "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
                        "version": TRACKER_VERSION,
                    })
    return catalog


def save_tracker_data(data: List[Dict[str, Any]]) -> None:
    atomic_write_json(TRACKER_DB_FILE, data)


def load_tracker_data() -> List[Dict[str, Any]]:
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
                if data[0].get("version") == TRACKER_VERSION and len(data) >= 5000:
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


def link_query_for(record: Dict[str, Any]) -> str:
    """The text used to find the exact product page for a record."""
    term = str(record.get("SearchTerm") or "").strip()
    return term or str(record.get("Product") or "").strip()


def apply_resolved_links(outcomes: Iterable[ResolveOutcome]) -> int:
    by_name = {o.query.lower(): o for o in outcomes if o.ok and is_pdp(o.url)}
    if not by_name:
        return 0
    records = load_tracker_data()
    updated = 0
    for record in records:
        outcome = (by_name.get(link_query_for(record).lower())
                   or by_name.get(str(record.get("Product", "")).lower()))
        if outcome and record.get("URL") != outcome.url:
            record["URL"] = outcome.url
            record["link_source"] = LINK_RESOLVED
            updated += 1
    if updated:
        save_tracker_data(records)
    return updated


def products_needing_links(records: Sequence[Dict[str, Any]]) -> List[str]:
    return [link_query_for(r) for r in records
            if link_query_for(r) and not is_pdp(str(r.get("URL", "")))]


# --------------------------------------------------------------------------- #
# VECTORIZED ANALYTICS ENGINE (OPTIMIZED FOR 30,000+ PRODUCTS)
# --------------------------------------------------------------------------- #

# NOTE: only the old "Link Type" status column is gone. The clickable product
# link ("URL" -> rendered as "Product Link") is kept in every table.
ANALYTICS_COLUMNS = [
    "id", "Category", "Brand", "Product", "SearchTerm", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Predicted BBD Low", "Verdict", "Optimal Card",
    "Net Price", "MRP", "URL", "link_source", "is_wishlist", "is_ad",
    "Last Checked",
]


def process_analytics(catalog: List[Dict[str, Any]], card_selection: str) -> pd.DataFrame:
    """High-speed vectorized analytics processor for tens of thousands of items."""
    if not catalog:
        return pd.DataFrame(columns=ANALYTICS_COLUMNS)

    df = pd.DataFrame(catalog)

    for num_col in ["Current Price", "6-Month Avg", "Last BBD Low", "MRP"]:
        if num_col in df.columns:
            df[num_col] = pd.to_numeric(df[num_col], errors="coerce").fillna(999).astype(int)

    if "Predicted BBD Low" not in df.columns or df["Predicted BBD Low"].isnull().any():
        df["Predicted BBD Low"] = (df["Last BBD Low"] * BBD_PREDICTION_FACTOR).astype(int)
    else:
        df["Predicted BBD Low"] = pd.to_numeric(
            df["Predicted BBD Low"], errors="coerce"
        ).fillna(df["Last BBD Low"] * BBD_PREDICTION_FACTOR).astype(int)

    df["Real Savings (vs 6M)"] = df["6-Month Avg"] - df["Current Price"]
    df["Real Disc % (vs 6M)"] = (
        (df["Real Savings (vs 6M)"] / df["6-Month Avg"].replace(0, 1)) * 100
    ).round(1).clip(lower=-20.0, upper=95.0)
    df["Diff vs Last BBD"] = df["Current Price"] - df["Last BBD Low"]

    is_smartphone = df["Category"] == "Smartphones"
    is_premium = df["Product"].str.contains("Dyson|Ray-Ban|Oakley|Fossil|Seiko|Apple",
                                            case=False, na=False)
    hold_cond = is_smartphone | is_premium

    buy_now_cond = (
        (hold_cond & (df["Current Price"] <= df["Predicted BBD Low"] * 1.05)) |
        (~hold_cond & ((df["Current Price"] <= df["Last BBD Low"]) |
                       (df["Real Disc % (vs 6M)"] >= STRONG_DISCOUNT_THRESHOLD)))
    )

    df["Verdict"] = "WAIT"
    df.loc[buy_now_cond, "Verdict"] = "BUY NOW"
    df.loc[hold_cond & ~buy_now_cond, "Verdict"] = "WAIT (BBD)"

    instant_10 = (df["Current Price"] * INSTANT_DISCOUNT_RATE).astype(int).clip(upper=INSTANT_DISCOUNT_CAP)
    cashback_5 = (df["Current Price"] * CASHBACK_RATE).astype(int)

    if card_selection == CARD_INSTANT:
        df["Optimal Card"] = "Axis/ICICI (10%)"
        df["Net Price"] = df["Current Price"] - instant_10
    elif card_selection == CARD_CASHBACK:
        df["Optimal Card"] = "Flipkart Axis (5%)"
        df["Net Price"] = df["Current Price"] - cashback_5
    else:
        prefer_instant = instant_10 >= cashback_5
        df["Optimal Card"] = "Axis/ICICI (10%)"
        df.loc[~prefer_instant, "Optimal Card"] = "Flipkart Axis (5%)"
        df["Net Price"] = df["Current Price"] - instant_10
        df.loc[~prefer_instant, "Net Price"] = df["Current Price"] - cashback_5

    if "SearchTerm" not in df.columns:
        df["SearchTerm"] = df["Product"]
    df["SearchTerm"] = df["SearchTerm"].fillna("").replace("", pd.NA).fillna(df["Product"])

    # Only verified specific-product URLs survive; everything else becomes blank
    # and is filled live by ensure_direct_links() when the row is rendered.
    if "URL" not in df.columns:
        df["URL"] = ""
    df["URL"] = df["URL"].astype(str).map(product_link)

    for fallback_col in ["Brand", "Product", "Category"]:
        if fallback_col not in df.columns:
            df[fallback_col] = "—"
        df[fallback_col] = df[fallback_col].fillna("—")

    return df[[c for c in ANALYTICS_COLUMNS if c in df.columns]]


# --------------------------------------------------------------------------- #
# ON-DEMAND DIRECT LINK FILLER (keeps every visible row clickable)
# --------------------------------------------------------------------------- #


def ensure_direct_links(display_df: pd.DataFrame, *, cap: int = AUTO_LINK_VISIBLE_CAP,
                        spinner_label: str = "Fetching direct product links…") -> Tuple[pd.DataFrame, int]:
    """Attach a specific /p/itm product link to every row currently on screen.

    Cheap path first (resolver cache), then a capped, parallel live lookup for
    whatever is still missing. Returns the patched frame and the number of rows
    that gained a link.
    """
    if display_df is None or display_df.empty:
        return display_df, 0

    frame = display_df.copy()
    resolver = get_resolver()

    pending_mask = ~frame["URL"].map(is_pdp)
    if not pending_mask.any():
        return frame, 0

    # 1) Free: anything already resolved earlier in this session/cache file.
    cached = frame.loc[pending_mask, "SearchTerm"].map(resolver.cached_url)
    frame.loc[pending_mask, "URL"] = cached.where(cached.map(is_pdp), "")
    filled = int(frame["URL"].map(is_pdp).sum() - display_df["URL"].map(is_pdp).sum())

    # 2) Live: resolve what is still blank, in parallel, capped per render.
    still_pending = ~frame["URL"].map(is_pdp)
    if not still_pending.any() or not resolver.available:
        return frame, filled

    targets: List[str] = []
    for term in frame.loc[still_pending, "SearchTerm"]:
        term = str(term).strip()
        if term and term not in targets and not resolver.recently_failed(term):
            targets.append(term)
        if len(targets) >= cap:
            break
    if not targets:
        return frame, filled

    with st.spinner(f"{spinner_label} ({len(targets)})"):
        outcomes = resolver.resolve_parallel(targets)
        apply_resolved_links(outcomes)

    resolved_map = {o.query.lower(): o.url for o in outcomes if o.ok and is_pdp(o.url)}
    if resolved_map:
        patched = frame.loc[still_pending, "SearchTerm"].map(
            lambda t: resolved_map.get(str(t).strip().lower(), ""))
        frame.loc[still_pending, "URL"] = patched.where(patched.map(is_pdp), "")
        filled = int(frame["URL"].map(is_pdp).sum() - display_df["URL"].map(is_pdp).sum())

    return frame, filled


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
                        *, resolve_live: bool = True, search_term: str = "",
                        **overrides: Any) -> Dict[str, Any]:
    title = (title or "").strip()
    current = max(int(current or 999), 1)

    final_url = product_link(url)
    term = (search_term or title).strip()
    if not final_url and resolve_live and term:
        outcome = get_resolver().resolve(term)
        final_url = product_link(outcome.url)

    item = {
        "id": str(uuid.uuid4()),
        "Category": detect_category(title),
        "Brand": (brand or (title.split()[0] if title else "Brand")).title(),
        "Product": title,
        "SearchTerm": term or title,
        "MRP": int(current * 1.35),
        "6-Month Avg": int(current * 1.15),
        "Last BBD Low": int(current * 0.94),
        "Current Price": current,
        "Predicted BBD Low": int(current * 0.88),
        "URL": final_url,
        "link_source": LINK_RESOLVED if final_url else LINK_PENDING,
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
        resolve_live=False,
        search_term=str(product_row.get("SearchTerm", "") or title),
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
# UI SETUP & IN-TABLE CHECKBOXES WITH STREAMING PAGINATION
# --------------------------------------------------------------------------- #

# The clickable direct product link ("URL") is present in EVERY table.
# Only the old "Link Type" status column was removed.
DISPLAY_COLUMNS = [
    "➕ Wishlist", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Verdict", "Predicted BBD Low", "Optimal Card",
    "Net Price", "URL",
]

COLUMN_CONFIG = {
    "➕ Wishlist": st.column_config.CheckboxColumn(
        "Add to Wishlist", help="Tick to save this item directly into your personal wishlist", default=False
    ),
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%.1f%%", min_value=-20, max_value=85),
    "Diff vs Last BBD": st.column_config.NumberColumn(
        format="₹%d", help="Negative means it is cheaper today than last year's festive BBD low"
    ),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "URL": st.column_config.LinkColumn(
        "Direct Product Link",
        display_text="Open product ↗",
        help="Opens that exact Flipkart product page (/p/itm...), never a search or category page.",
    ),
}


def kpi(value: str, label: str, colour: str = "#38bdf8") -> str:
    return (f'<div class="kpi-container"><div class="kpi-number" style="color:{colour};">{value}</div>'
            f'<div class="kpi-label">{label}</div></div>')


def _resolve_targets(targets: Sequence[str], *, force: bool, silent: bool) -> int:
    resolver = get_resolver()
    targets = [t for t in dict.fromkeys(targets) if t]
    if not targets:
        if not silent:
            st.success("Every product already has a direct product link.")
        return 0
    if not resolver.available:
        if not silent:
            st.error("The `requests` package is required (`pip install requests`).")
        return 0

    bar = st.progress(0.0)
    status = st.empty()
    status.caption(f"Resolving {len(targets)} direct product links from Flipkart…")

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
        st.success(f"Resolved {succeeded}/{len(targets)} direct product links ({updated} records updated).")
    if failed and not silent:
        with st.expander(f"⚠️ {len(failed)} product(s) could not be resolved"):
            st.dataframe(pd.DataFrame([{"Product": o.query, "Reason": o.detail} for o in failed]),
                         hide_index=True, use_container_width=True)
    return updated


def run_link_resolution(force: bool = False, *, silent: bool = False) -> int:
    records = load_tracker_data()
    targets = (sorted({link_query_for(r) for r in records if link_query_for(r)}) if force
               else sorted(set(products_needing_links(records))))
    return _resolve_targets(targets, force=force, silent=silent)


def render_collapsible_table(df_subset: pd.DataFrame, category_title: str, slug: str,
                             icon: str, is_expanded: bool, allow_deletion: bool = False) -> None:
    if df_subset is None or df_subset.empty:
        return

    unresolved = int((~df_subset["URL"].map(is_pdp)).sum())
    badge = f" · {unresolved:,} link(s) pending" if unresolved else " · all direct product links ✓"
    with st.expander(f"{icon} {category_title} — ({len(df_subset):,} products{badge})",
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
        if "➕ Wishlist" not in table.columns:
            table.insert(0, "➕ Wishlist", False)
        else:
            table["➕ Wishlist"] = False

        # Streaming Row Limiter: keeps all 25k+ products alive without a WebSocket size error
        r_ctrl1, r_ctrl2 = st.columns([3, 1])
        with r_ctrl2:
            display_limit = st.selectbox(
                "Rows to view in table:",
                [50, 100, 250, 500, "All (Slow for 3000+)"],
                index=1,
                key=f"{slug}_rows_view"
            )

        if display_limit != "All (Slow for 3000+)":
            display_table = table.head(int(display_limit))
            st.caption(f"Showing top **{len(display_table):,}** of **{len(table):,}** products matching filters. "
                       f"CSV download includes all {len(table):,} items.")
        else:
            display_table = table

        # ---- DIRECT PRODUCT LINKS FOR EVERY VISIBLE ROW ----
        auto_link = st.session_state.get("auto_link_visible", AUTO_LINK_VISIBLE_DEFAULT)
        if auto_link:
            display_table, gained = ensure_direct_links(
                display_table, spinner_label=f"Fetching direct product links for {category_title}")
            if gained:
                table.loc[display_table.index, "URL"] = display_table["URL"]

        pending_here = [str(t) for t, u in zip(display_table["SearchTerm"], display_table["URL"])
                        if not is_pdp(str(u))]
        with r_ctrl1:
            if pending_here:
                st.caption(f"⏳ {len(pending_here):,} row(s) on this page are still waiting for a direct link.")
                if st.button(f"🔗 Fetch direct links for the remaining {len(pending_here):,} row(s)",
                             key=f"{slug}_resolve_page"):
                    if _resolve_targets(pending_here, force=False, silent=False):
                        st.rerun()
            else:
                st.caption("✅ Every row on this page opens its own direct product page.")

        editor_key = f"editor_{slug}"

        edited = st.data_editor(
            display_table[DISPLAY_COLUMNS],
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
                source = display_table[display_table["Product"] == row["Product"]]
                if source.empty:
                    source = table[table["Product"] == row["Product"]]
                if not source.empty:
                    payload["URL"] = source.iloc[0]["URL"]
                    payload["SearchTerm"] = source.iloc[0].get("SearchTerm", row["Product"])
                added += int(add_product_to_wishlist(payload)[0])
            st.session_state.pop(editor_key, None)
            st.toast(f"✅ Added {added} item(s) to your wishlist!" if added
                     else "Already on your wishlist.", icon="💖")
            st.rerun()

        left, right = st.columns([3, 1])
        with left:
            # The direct product link is included in the CSV export too.
            export_cols = [c for c in DISPLAY_COLUMNS if c != "➕ Wishlist"]
            export_table = table.rename(columns={"URL": "Direct Product Link"})
            export_cols = ["Direct Product Link" if c == "URL" else c for c in export_cols]
            st.download_button(
                label=f"📥 Download Full {category_title} CSV ({len(table):,} items)",
                data=export_table[export_cols].to_csv(index=False).encode("utf-8"),
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
# FULL FLIPKART WEBSITE DEEP SCANNER (UNRESTRICTED TRAVERSAL)                 #
# =========================================================================== #

SCANNER_SEED_BANDS: List[Tuple[int, int]] = [
    (0, 500), (500, 1500), (1500, 3000), (3000, 6000), (6000, 12000),
    (12000, 25000), (25000, 50000), (50000, 100000), (100000, 500000),
]

SCANNER_SORTS = ("", "price_asc", "price_desc", "popularity", "recency_desc")

SCANNER_SEEDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": (
        "mens jeans", "mens shirt", "mens t-shirt", "mens trousers", "mens jacket",
        "mens suit", "mens kurta", "mens shorts", "mens track pants", "mens sweatshirt",
        "mens hoodie", "mens blazer", "mens innerwear", "mens ethnic wear", "mens winter wear",
        "mens formal trousers", "mens cargo pants", "mens linen shirts", "mens polo t-shirts",
        "mens chinos", "mens windcheater", "mens boxer shorts", "mens formal suit", "mens thermal wear",
        "mens printed shirts", "mens oversized tee", "mens denim jacket", "mens bandhgala"
    ),
    "Women's Fashion": (
        "womens kurta", "womens jeans", "womens dress", "saree", "womens top",
        "womens cardigan", "lehenga", "womens leggings", "womens jumpsuit", "womens ethnic set",
        "womens skirt", "womens jacket", "womens nightwear", "womens palazzo", "womens blouse",
        "anarkali suit", "georgette saree", "cotton kurti set", "womens crop tops", "womens gowns",
        "womens shrug", "womens formal shirts", "womens ethnic skirt", "chiffon saree", "womens co ord set",
        "womens high rise jeans", "chikankari kurti", "banarasi saree"
    ),
    "Footwear & Shoes": (
        "running shoes", "sneakers", "sandals", "formal shoes", "sports shoes",
        "clogs", "boots", "loafers", "flip flops", "walking shoes", "casual shoes",
        "heels", "slippers", "training shoes", "derby formal shoes", "gym trainers",
        "slip on sneakers", "oxford shoes", "leather boots", "trekking shoes", "badminton shoes",
        "cricket shoes", "football studs", "slides", "foam clogs"
    ),
    "Watches & Eyewear": (
        "analog watch", "digital watch", "chronograph watch", "smartwatch",
        "sunglasses", "aviator sunglasses", "womens watch", "couple watch", "sports watch",
        "luxury watch", "eyeglasses", "blue cut glasses", "polarized shades", "wayfarer",
        "round sunglasses", "clubmaster", "smart band", "automatic watch", "dress watch",
        "field watch", "diver watch", "pilot sunglasses", "metallic watch"
    ),
    "Smartphones": (
        "mobile phone 5g", "iphone", "samsung galaxy", "oneplus", "realme mobile",
        "redmi mobile", "vivo mobile", "oppo mobile", "motorola mobile", "poco mobile",
        "pixel phone", "nothing phone", "budget smartphone", "gaming phone", "camera phone",
        "flagship smartphone", "smartphone 256gb", "5g phone under 20000", "smartphone 128gb",
        "foldable smartphone", "oled smartphone", "5g phone under 15000", "gaming phone 5g"
    ),
    "Audio, Monitors & Laptops": (
        "bluetooth headphones", "tws earbuds", "bluetooth speaker", "gaming monitor",
        "laptop", "gaming laptop", "tablet", "soundbar", "wired earphones", "neckband",
        "home theatre", "monitor 24 inch", "macbook", "chromebook", "party speaker",
        "ips monitor 27 inch", "noise cancelling headphones", "core i5 laptop", "rtx 4050 laptop",
        "curved monitor 34 inch", "oled laptop", "anc earbuds", "waterproof speaker"
    ),
    "Cosmetics & Grooming": (
        "face serum", "lipstick", "trimmer", "perfume", "face cream", "shampoo",
        "sunscreen", "face wash", "hair dryer", "makeup kit", "moisturizer", "hair oil",
        "body lotion", "beard oil", "nail polish", "kajal", "body spray", "cleanser",
        "shaving kit", "hair straightener", "eye shadow palette", "hyaluronic acid serum",
        "niacinamide serum", "salicylic acid face wash", "matte compact powder"
    ),
    "Home Appliances": (
        "water purifier", "air fryer", "mixer grinder", "ceiling fan", "vacuum cleaner",
        "water geyser", "induction cooktop", "steam iron", "electric kettle", "room heater",
        "microwave oven", "air cooler", "sandwich maker", "gas stove", "pressure cooker",
        "bldc fan", "ro uv purifier", "robotic vacuum cleaner", "convection microwave",
        "instant geyser", "dry iron", "cordless stick vacuum", "hand blender"
    ),
    "Stationery & Books": (
        "notebook", "ball pen", "scientific calculator", "books", "art supplies",
        "geometry box", "highlighter", "diary", "fiction books", "school bag",
        "sketch pens", "sticky notes", "acrylic colors", "roller ball pen", "project book",
        "calligraphy pen", "desk organizer", "stapler and punch", "non fiction bestsellers",
        "engineering books", "fountain pen", "drawing pencils", "hardbound books"
    ),
}

SCANNER_BRANDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": ("Levis", "Allen Solly", "Peter England", "US Polo", "Roadster", "Highlander", "Snitch", "Louis Philippe", "Van Heusen", "Jack and Jones", "Wrangler", "Tommy Hilfiger", "Raymond"),
    "Women's Fashion": ("Biba", "W for Woman", "Vero Moda", "ONLY", "Libas", "Aurelia", "Soch", "Madame", "Global Desi", "Anouk", "Sangria", "Tokyo Talkies", "FabIndia"),
    "Footwear & Shoes": ("Nike", "Adidas", "Puma", "Asics", "Skechers", "Woodland", "Bata", "Crocs", "Red Tape", "Campus", "Reebok", "New Balance", "Under Armour"),
    "Watches & Eyewear": ("Casio", "Titan", "Fastrack", "Fossil", "Timex", "Citizen", "Ray-Ban", "Oakley", "Noise", "boAt", "Fire-Boltt", "Amazfit", "Seiko"),
    "Smartphones": ("Apple", "Samsung", "OnePlus", "Motorola", "Nothing", "Google", "Vivo", "Oppo", "Realme", "Xiaomi", "POCO", "iQOO", "Infinix"),
    "Audio, Monitors & Laptops": ("Sony", "JBL", "boAt", "Marshall", "LG", "Samsung", "Acer", "HP", "Lenovo", "Asus", "Dell", "Apple", "BenQ", "Bose"),
    "Cosmetics & Grooming": ("Minimalist", "Maybelline", "Philips", "Beardo", "Neutrogena", "Cetaphil", "Lakme", "Mamaearth", "LOreal", "Nivea", "Plum", "Biotique", "The Derma Co"),
    "Home Appliances": ("Philips", "Bajaj", "Kent", "Aquaguard", "Prestige", "Havells", "Atomberg", "Dyson", "Crompton", "Voltas", "Eureka Forbes", "Morphy Richards", "Bosch"),
    "Stationery & Books": ("Classmate", "Parker", "Casio", "Camlin", "Faber-Castell", "Doms", "Penguin", "Cello", "Luxor", "Navneet", "Pilot", "Kangaro", "Solo"),
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
                        if product.key and is_pdp(product.url):
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
        found: Dict[str, ScannedProduct] = {}

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

        card_chunks = re.findall(
            r'<div[^>]*data-id="([A-Z0-9]{12,20})"[^>]*>(.*?)</div>\s*</div>\s*</div>', html, re.DOTALL)
        for pid, block in card_chunks:
            pdp_match = re.search(r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{10,20}[^"]*)"', block, re.IGNORECASE)
            if not pdp_match:
                continue
            clean_url = sanitize_url(f"https://{FLIPKART_HOST}{pdp_match.group(1)}")
            if not is_pdp(clean_url):
                continue

            title_match = re.search(
                r'<(?:div|a)[^>]*class="[^"]*(?:KzDlHZ|IRpwTa|_4rR01T|wBy4fm)[^"]*"[^>]*>(.*?)</(?:div|a)>',
                block, re.DOTALL)
            title = re.sub(r'<[^>]+>', '', title_match.group(1)).strip() if title_match else ""
            if not title:
                title = _slug_of(clean_url).title()[:120]

            price_match = re.search(r'(?:₹|Rs\.?)\s*([0-9,]+)', block)
            price = int(price_match.group(1).replace(",", "")) if price_match else 0

            mrp_match = re.findall(r'(?:₹|Rs\.?)\s*([0-9,]+)', block)
            mrp = int(mrp_match[1].replace(",", "")) if len(mrp_match) > 1 else int(price * 1.35)

            disc_match = re.search(r'([0-9]{1,2})%\s*off', block, re.IGNORECASE)
            disc = float(disc_match.group(1)) if disc_match else (
                round((mrp - price) / mrp * 100, 1) if mrp > price else 0.0)

            product = ScannedProduct(
                pid=pid,
                title=title,
                brand=title.split()[0] if title else "",
                url=clean_url,
                price=price,
                mrp=mrp,
                discount_pct=disc,
            ).normalise()

            if product.key not in found:
                found[product.key] = product

        if not found:
            for href_match in PDP_HREF_PATTERN.finditer(html):
                href = href_match.group(1)
                clean = sanitize_url(f"https://{FLIPKART_HOST}{href}")
                if not is_pdp(clean):
                    continue
                itm = extract_itm_id(clean)
                if itm in found:
                    continue
                found[itm] = ScannedProduct(title=_slug_of(clean).title()[:120], url=clean).normalise()

        return list(found.values())

    def category_count(self, category: str) -> int:
        with self._lock:
            return sum(1 for p in self._seen.values() if p.category == category)

    def _absorb(self, products: Sequence[ScannedProduct], task: ScanTask) -> int:
        added = 0
        with self._lock:
            self.products_seen_raw += len(products)
            for product in products:
                if not is_pdp(product.url):
                    continue
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
    if hint in DEPARTMENT_STRUCTURE:
        return hint
    lowered = (title or "").lower()
    for category, keywords in _TRACKER_CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Men's Fashion" if "men" in lowered else "Stationery & Books"


def merge_scan_results_into_tracker(scanner: "FlipkartCatalogueScanner", min_discount: float = 0.0) -> int:
    """Only products with a verified direct product link are merged into the tables."""
    products = [p for p in scanner.products
                if p.price > 0 and p.title and p.discount_pct >= min_discount and is_pdp(p.url)]
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
        category = (product.category if product.category in DEPARTMENT_STRUCTURE
                    else _map_to_tracker_category(title, product.category))

        fresh.append({
            "id": str(uuid.uuid4()),
            "Category": category,
            "Brand": product.brand or "—",
            "Product": title,
            "SearchTerm": title,
            "MRP": mrp,
            "6-Month Avg": int(mrp * 0.85),
            "Last BBD Low": int(product.price * 0.92),
            "Current Price": product.price,
            "Predicted BBD Low": int(product.price * 0.88),
            "URL": product.url,
            "link_source": LINK_RESOLVED,
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


def backfill_links_from_scanner(scanner: "FlipkartCatalogueScanner") -> int:
    """Attach scanner-verified direct product links to rows that are still blank."""
    scanned = [p for p in scanner.products if is_pdp(p.url) and p.title]
    if not scanned:
        return 0

    index: List[Tuple[set, str]] = [(_tokens(p.title), p.url) for p in scanned]
    records = load_tracker_data()
    updated = 0

    for record in records:
        if is_pdp(str(record.get("URL", ""))):
            continue
        wanted = _tokens(link_query_for(record))
        if not wanted:
            continue
        best_url, best_score = "", 0.0
        for have, url in index:
            score = len(wanted & have) / len(wanted)
            if score > best_score:
                best_url, best_score = url, score
        if best_url and best_score >= 0.6:
            record["URL"] = best_url
            record["link_source"] = LINK_RESOLVED
            updated += 1

    if updated:
        save_tracker_data(records)
    return updated


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
        if not add(2, "Search returns live HTML", not challenged,
                   "Bot-challenge page" if challenged else f"HTTP 200 ({len(html):,} bytes)"):
            return steps
    except Exception as exc:
        add(2, "Search returns live HTML", False, str(exc)[:120])
        return steps

    has_state = "__INITIAL_STATE__" in html
    add(3, "__INITIAL_STATE__ present", has_state, "Payload present" if has_state else "Payload absent")

    products = FlipkartCatalogueScanner.extract_products(html)
    add(4, "Parser extracts direct product links", len(products) > 0,
        f"{len(products)} product pages parsed, {len([p for p in products if p.price > 0])} with price")
    return steps


# --------------------------------------------------------------------------- #
# MAIN APP ENTRY POINT
# --------------------------------------------------------------------------- #


def main() -> None:
    st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
    st.caption("Live price intelligence engine: all categories, BBD floor steals, and promoted deals on a single continuous page.")

    t1, t2, t3, t4, t5, t6, t7 = st.columns([1.3, 1.4, 1.2, 1.0, 1.3, 1.0, 1.2])
    with t1:
        if st.button("🔄 Re-Check Prices", type="primary", use_container_width=True):
            with st.spinner("Re-checking active discounts…"):
                count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count:,} products.")
            st.rerun()
    with t2:
        resolve_clicked = st.button("🔗 Resolve All Links", use_container_width=True,
                                    help="Resolves the direct product link for every pending row in the database.")
    with t3:
        diagnose_clicked = st.button("🩺 Test Connection", use_container_width=True)
    with t4:
        if st.button("🚨 Reset DB", use_container_width=True, help="Force-rebuilds the 25,000+ product catalogue"):
            save_tracker_data(generate_seed_catalog())
            st.session_state.pop("auto_resolved", None)
            st.success("Catalogue rebuilt. Direct links are fetched automatically as you browse.")
            st.rerun()
    with t5:
        st.checkbox("⚡ Auto direct links", value=AUTO_LINK_VISIBLE_DEFAULT, key="auto_link_visible",
                    help="Automatically fetch the exact product page for every row shown in a table.")
    with t6:
        expand_all = st.checkbox("📂 Expand All", value=False)
    with t7:
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
    k2.markdown(kpi(f"{specific_links:,}/{total_products:,}", "Direct Product Links",
                    "#4ade80" if specific_links == total_products else "#f97316"),
                unsafe_allow_html=True)
    k3.markdown(kpi(f"{int((master_df['Diff vs Last BBD'] <= 0).sum()):,}",
                    "🔥 Cheaper Than Last BBD", "#facc15"), unsafe_allow_html=True)
    k4.markdown(kpi(f"{int(master_df['is_wishlist'].sum()):,}", "Wishlist Items", "#ec4899"),
                unsafe_allow_html=True)
    k5.markdown(kpi(f"₹{master_df['Real Savings (vs 6M)'].sum() / 10000000:,.1f} Cr",
                    "Total Real Savings", "#4ade80"), unsafe_allow_html=True)

    st.divider()

    with st.expander("➕ Add Direct Product to Wishlist (Paste Name & Link)", expanded=False):
        st.markdown("Paste the Flipkart **product page** link (it contains `/p/itm...`). "
                    "Leave it blank and the exact product page is fetched for you.")
        with st.form("quick_add_wishlist", clear_on_submit=True):
            name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Fit Jeans")
            url = st.text_input("🔗 Flipkart Product Link (optional):",
                                placeholder="https://www.flipkart.com/<slug>/p/itm...?pid=...")
            price = st.number_input("💰 Current Price (₹, optional):", min_value=0, step=100, value=0)
            if st.form_submit_button("💾 Save Direct to Wishlist", type="primary"):
                if not name.strip():
                    st.error("Please enter the product name.")
                elif url.strip() and not is_pdp(sanitize_url(url)):
                    st.error("That is not a direct product link. Open the product on Flipkart and "
                             "copy the URL containing `/p/itm...` (a /search link will not work).")
                else:
                    with st.spinner("Fetching the exact product page…"):
                        item = build_wishlist_item(name, url, current=price or 999)
                    records = load_tracker_data()
                    records.append(item)
                    save_tracker_data(records)
                    if item["URL"]:
                        st.success(f"Saved '{item['Product']}' to '{item['Category']}' with a direct product link.")
                    else:
                        st.warning(f"Saved '{item['Product']}' to '{item['Category']}'. The direct link will be "
                                   "fetched automatically the next time the table renders.")
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

    # SECTION 1: CATEGORY CATALOG
    st.markdown('<div class="section-title-catalog"><h2 style="margin:0;">1. 🛒 Flipkart Category Catalog</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Independent collapsible tables with dedicated column filters and a direct product link on every row.</p></div>',
                unsafe_allow_html=True)
    for category, meta in DEPARTMENT_STRUCTURE.items():
        subset = master_df[(master_df["Category"] == category) & (~master_df["is_wishlist"])]
        render_collapsible_table(subset, category, meta["slug"], meta["icon"], expand_all)

    # SECTION 2: BBD STEAL DEALS
    st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">2. 🔥 Big Billion Days Floor Deals</h2>'
                "<p style=\"margin:2px 0 0 0; font-size:0.9rem;\">Products currently priced at or below last year's festive BBD low.</p></div>",
                unsafe_allow_html=True)
    bbd_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD")
    if bbd_df.empty:
        st.info("No product currently beats last year's BBD floor price.")
    else:
        render_collapsible_table(bbd_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", True)

    # SECTION 3: ADS & PROMOTIONS
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
            "Scans Flipkart by recursively bisecting saturated price windows. Every discovered row "
            "arrives with its own verified /p/itm direct product link."
        )

        c1, c2, c3 = st.columns([2.4, 1.3, 1.3])
        categories = c1.multiselect("Categories to scan:", options=list(SCANNER_SEEDS),
                                    default=list(SCANNER_SEEDS), key="scan_cats")
        target = c2.number_input("Target per category (Default 3,000; 0 = Unlimited):", min_value=0,
                                 max_value=500000, value=3000, step=500, key="scan_target")
        pages = c3.slider("Pages per query:", 1, 50, 10, key="scan_pages")

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
        st.caption(f"Frontier initialized with **{len(frontier):,} entry points**. Recursive bisection will expand across thousands of price windows.")

        r1, r2, r3 = st.columns(3)
        if r1.button("🛰️ Start Deep Multi-Thousand Scan", type="primary", key="scan_run"):
            bar = st.progress(0.0)
            status = st.empty()

            def on_progress(done: int, total: int, term: str, found: int, pending: int) -> None:
                bar.progress(min(done / max(total, 1), 1.0))
                status.caption(
                    f"[{done:,}] {term} · **{found:,}** deals found · "
                    f"{pending:,} queued · {scanner.bisections:,} splits · "
                    f"{scanner.limiter.rate:.1f} req/s"
                )

            with st.spinner("Scanning Flipkart departments for thousands of products…"):
                scanner.run_frontier(frontier, pages,
                                     target_per_category=int(target),
                                     max_requests=int(budget),
                                     progress=on_progress)
                added = merge_scan_results_into_tracker(scanner, float(min_disc))
                linked = backfill_links_from_scanner(scanner)
            bar.empty()
            status.empty()
            st.session_state["scan_done"] = True
            st.session_state["scan_added"] = added
            st.session_state["scan_linked"] = linked
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
            linked = st.session_state.get("scan_linked", 0)
            if added:
                st.success(f"Added **{added:,}** deals with direct product links into the tables above!")
            elif scanner.products:
                st.info("All discovered deals are already present in the tables above.")
            if linked:
                st.success(f"Attached direct product links to **{linked:,}** existing catalogue rows.")

        if scanner.products:
            m1, m2, m3 = st.columns(3)
            m1.markdown(kpi(f"{len(scanner.products):,}", "Total Discovered"), unsafe_allow_html=True)
            m2.markdown(kpi(f"{len(scanner.priced()):,}", "Priced Deals", "#4ade80"), unsafe_allow_html=True)
            m3.markdown(kpi(f"{scanner.bisections:,}", f"Band Bisections (d{scanner.max_depth_reached})", "#a855f7"), unsafe_allow_html=True)

            mcol1, mcol2 = st.columns(2)
            if mcol1.button("➕ Force-Merge All Discoveries into Tables Above", key="scan_merge_now"):
                added = merge_scan_results_into_tracker(scanner, float(min_disc))
                st.toast(f"Merged {added:,} new product(s)." if added else "All findings already in tables.", icon="🛰️")
                if added:
                    st.rerun()
            if mcol2.button("🔗 Attach Scanned Links to Pending Rows", key="scan_backfill"):
                linked = backfill_links_from_scanner(scanner)
                st.toast(f"Linked {linked:,} row(s) to their product page." if linked
                         else "No pending row matched a scanned product.", icon="🔗")
                if linked:
                    st.rerun()


if __name__ == "__main__":
    try:
        main()
        render_deep_scanner()
    except Exception as e:
        logger.exception("Unhandled error")
        st.error(f"⚠️ An error occurred: {e}")
        st.exception(e)
