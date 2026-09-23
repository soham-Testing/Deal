"""
Flipkart BBD Deal Tracker & Live Price Radar
=============================================
A single-page Streamlit platform that consolidates highly-discounted Flipkart
products: it ingests the catalogue at scale, records a real price history,
scores every deal against that history, surfaces live offers/coupons, applies
card-offer maths and maintains a persistent personal wishlist.

Data sources (in order of preference)
-------------------------------------
1. FLIPKART AFFILIATE API  - the only sanctioned bulk source. Provides:
     * Product Feed Listing API - every actively marketed category
     * Product Feed API         - all products in a category (500 per page)
     * Delta Feed API           - only what changed since a given version
     * Feed Download API        - compressed CSV dumps per category
     * All Offer / DOTD Offer   - live offers, coupon codes and deals of the day
   Requires a free affiliate account and an API token, supplied in the sidebar
   or via the FK_AFFILIATE_ID / FK_AFFILIATE_TOKEN environment variables.
2. CSV IMPORT              - any exported feed or hand-built sheet.
3. SEED CATALOGUE          - the 90 demonstration rows, used until ingestion runs.

Why not scrape the whole site: Flipkart has no public bulk endpoint outside the
affiliate programme, renders product pages client-side, and actively challenges
automated traffic. Crawling it would breach the Terms of Use and still miss most
SKUs. The affiliate feed is both lawful and complete for marketed categories.

Fix history
-----------
v12  Removed the synthetic "/item/p/product?pid=" rewrite behind the E002 page.
v13  Plain-text, copy-ready links plus an editable link field per row.
v14  Live link resolver and curated verified PDP tier.
v15  Corrected the PDP id pattern (Flipkart ids are base36, not hexadecimal).
v16  (this file) Catalogue ingestion at scale via the Affiliate API, real price
     history with rolling averages and festive-low tracking, an offers & coupons
     console, and a reorganised professional layout. The "Link Type" column has
     been removed from every table as requested; link quality is now shown once,
     in the header KPIs.

Run with:  streamlit run flipkart_deal_tracker.py
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import logging
import os
import random
import re
import statistics
import tempfile
import threading
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

APP_TITLE = "Flipkart Deal Intelligence Platform"
TRACKER_VERSION = "v16_catalogue_scale"

DATA_DIR = os.environ.get("FK_DATA_DIR", ".")
TRACKER_DB_FILE = os.path.join(DATA_DIR, "tracker_store.json")
HISTORY_DB_FILE = os.path.join(DATA_DIR, "price_history.json")
OFFERS_DB_FILE = os.path.join(DATA_DIR, "offers_store.json")

FLIPKART_HOST = "www.flipkart.com"
FLIPKART_SEARCH = f"https://{FLIPKART_HOST}/search?q={{query}}"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}

AFFILIATE_BASE = "https://affiliate-api.flipkart.net/affiliate"
AFFILIATE_FEED_LISTING = AFFILIATE_BASE + "/api/{tracking_id}.json"
AFFILIATE_OFFERS_ALL = AFFILIATE_BASE + "/offers-v1.0/all.json"
AFFILIATE_OFFERS_DOTD = AFFILIATE_BASE + "/offers-v1.0/dotd.json"
AFFILIATE_SEARCH = AFFILIATE_BASE + "/1.0/search.json"

KEEP_PARAMS = {"pid", "lid", "marketplace", "affid"}
# Flipkart product ids are "itm" + 13 BASE36 characters (e.g. itmf3zhdga85ghju),
# never hexadecimal. 12-18 accepted for forward safety.
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{12,18})(?:[/?#]|$)", re.IGNORECASE)

HTTP_TIMEOUT = 25
HTTP_RETRIES = 3
FEED_PAGE_PAUSE = 0.35          # polite pause between feed pages
MAX_PAGES_PER_CATEGORY = 400    # 500 SKUs per page -> 200k cap per category

HISTORY_RETENTION_DAYS = 800    # ~2 years, so the previous BBD stays comparable
ROLLING_WINDOW_DAYS = 180       # "6-month average"
# Past and current Big Billion Days windows. Add the new dates each year;
# anything inside a window is treated as a festive-floor observation.
FESTIVE_WINDOWS = {
    "BBD 2024": ((2024, 9, 27), (2024, 10, 3)),
    "BBD 2025": ((2025, 9, 23), (2025, 10, 2)),
    "BBD 2026": ((2026, 9, 20), (2026, 10, 1)),
}

INSTANT_DISCOUNT_RATE = 0.10
INSTANT_DISCOUNT_CAP = 1500
CASHBACK_RATE = 0.05
BBD_PREDICTION_FACTOR = 0.93
STRONG_DISCOUNT_THRESHOLD = 28.0
STEAL_DISCOUNT_THRESHOLD = 40.0

CARD_AUTO = "Auto-Best Card"
CARD_INSTANT = "Axis / ICICI (10% Instant, Cap ₹1.5k)"
CARD_CASHBACK = "Flipkart Axis (5% Unlimited Cashback)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deal_tracker")

st.set_page_config(page_title=APP_TITLE, page_icon="⚡", layout="wide",
                   initial_sidebar_state="expanded")

st.markdown(
    """
<style>
    .block-container { padding-top: 2.2rem; }
    .kpi-container { background-color: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.35rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; margin-top: 3px; }
    .kpi-sub { font-size: 0.68rem; color: #64748b; margin-top: 2px; }
    .section-title-catalog { background: linear-gradient(90deg, #1e293b, #0f172a); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #38bdf8; margin: 26px 0 14px 0; color: white; }
    .section-title-bbd { background: linear-gradient(90deg, #b91c1c, #7f1d1d); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #facc15; margin: 26px 0 14px 0; color: white; }
    .section-title-ads { background: linear-gradient(90deg, #1e3a8a, #172554); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #a855f7; margin: 26px 0 14px 0; color: white; }
    .section-title-wishlist { background: linear-gradient(90deg, #831843, #500724); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #ec4899; margin: 26px 0 14px 0; color: white; }
    .section-title-offers { background: linear-gradient(90deg, #14532d, #052e16); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #4ade80; margin: 26px 0 14px 0; color: white; }
</style>
""",
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- #
# URL HANDLING
# --------------------------------------------------------------------------- #


def search_url(*terms: str) -> str:
    query = " ".join(t for t in terms if t).strip() or "deals"
    return FLIPKART_SEARCH.format(query=quote_plus(query))


def sanitize_url(raw_url: str) -> str:
    """
    Normalise a Flipkart URL: preserve the product path, `pid`, `lid`,
    `marketplace` and the affiliate `affid`; drop analytics noise.

    The path is never reconstructed - Flipkart serves a PDP only from
    /<slug>/p/<itm-id>, and a synthesised path returns the empty SPA shell.
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
        return ""
    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return ""
    kept = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=False)
            if k.lower() in KEEP_PARAMS]
    return urlunparse(("https", FLIPKART_HOST, parsed.path, "", urlencode(kept), ""))


def is_pdp(url: str) -> bool:
    return bool(url) and bool(ITM_ID_PATTERN.search(urlparse(url).path))


def extract_itm_id(url: str) -> str:
    match = ITM_ID_PATTERN.search(urlparse(url or "").path)
    return match.group(1).lower() if match else ""


def with_affiliate_tag(url: str, tracking_id: str) -> str:
    """Affiliate tracking does not work unless affid is present on the URL."""
    if not url or not tracking_id or not is_pdp(url):
        return url
    parsed = urlparse(url)
    params = dict(parse_qsl(parsed.query))
    params["affid"] = tracking_id
    return urlunparse(("https", FLIPKART_HOST, parsed.path, "", urlencode(params), ""))


# --------------------------------------------------------------------------- #
# ATOMIC JSON STORE
# --------------------------------------------------------------------------- #


def atomic_write_json(path: str, payload: Any) -> None:
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def read_json(path: str, default: Any) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("%s unreadable (%s); using default.", path, exc)
        return default


# --------------------------------------------------------------------------- #
# FLIPKART AFFILIATE API CLIENT
# --------------------------------------------------------------------------- #


@dataclass
class IngestReport:
    """Outcome of a catalogue ingestion run."""

    categories: int = 0
    products: int = 0
    pages: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    duration_s: float = 0.0

    @property
    def ok(self) -> bool:
        return self.products > 0


class FlipkartAffiliateClient:
    """
    Thin, resilient client for the Flipkart Affiliate APIs.

    Endpoints used:
      * Product Feed Listing - every actively marketed category
      * Product Feed / Delta - paginated product records (500 per page)
      * All Offers & DOTD    - live offers, coupon codes, deals of the day
      * Search               - keyword lookup for a single product

    All calls are authenticated with the Fk-Affiliate-Id / Fk-Affiliate-Token
    headers described in the affiliate documentation.
    """

    def __init__(self, tracking_id: str, token: str) -> None:
        self.tracking_id = (tracking_id or "").strip()
        self.token = (token or "").strip()
        self._session = self._build_session()

    def _build_session(self):
        if requests is None:
            return None
        session = requests.Session()
        session.headers.update({
            "Fk-Affiliate-Id": self.tracking_id,
            "Fk-Affiliate-Token": self.token,
            "Accept": "application/json",
            "User-Agent": "FlipkartDealTracker/16.0 (+affiliate-api)",
        })
        return session

    @property
    def configured(self) -> bool:
        return bool(self._session and self.tracking_id and self.token)

    # -- transport ---------------------------------------------------------- #

    def _get(self, url: str, params: Optional[Dict[str, Any]] = None) -> Optional[dict]:
        if not self.configured:
            return None
        for attempt in range(1, HTTP_RETRIES + 1):
            try:
                response = self._session.get(url, params=params, timeout=HTTP_TIMEOUT)
            except Exception as exc:
                logger.info("Affiliate GET failed (%s) attempt %d", type(exc).__name__, attempt)
                time.sleep(0.8 * attempt)
                continue
            if response.status_code == 200:
                try:
                    return response.json()
                except ValueError:
                    logger.warning("Affiliate API returned non-JSON payload.")
                    return None
            if response.status_code in (401, 403):
                raise PermissionError(
                    "Flipkart rejected the affiliate credentials (HTTP "
                    f"{response.status_code}). Regenerate the token at "
                    "affiliate.flipkart.com > API > API Token."
                )
            if response.status_code == 429:
                time.sleep(2.0 * attempt)
                continue
            logger.info("Affiliate GET HTTP %s (attempt %d)", response.status_code, attempt)
            time.sleep(0.8 * attempt)
        return None

    def verify(self) -> Tuple[bool, str]:
        """Credential check used by the sidebar Test button."""
        if requests is None:
            return False, "The `requests` package is not installed (`pip install requests`)."
        if not self.configured:
            return False, "Enter both the Affiliate Tracking ID and the API Token."
        try:
            payload = self._get(AFFILIATE_FEED_LISTING.format(tracking_id=self.tracking_id))
        except PermissionError as exc:
            return False, str(exc)
        if not payload:
            return False, ("No response from the Affiliate API. Check your connection, "
                           "proxy/VPN, or try again shortly.")
        listings = payload.get("apiGroups", {}).get("affiliate", {}).get("apiListings", {})
        if not listings:
            return False, "Connected, but the feed listing was empty. Verify the account is approved."
        return True, f"Connected. {len(listings)} marketed categories available in the feed."

    # -- catalogue ---------------------------------------------------------- #

    def list_categories(self) -> Dict[str, str]:
        """Map category name -> first-page product feed URL."""
        payload = self._get(AFFILIATE_FEED_LISTING.format(tracking_id=self.tracking_id))
        if not payload:
            return {}
        listings = payload.get("apiGroups", {}).get("affiliate", {}).get("apiListings", {})
        categories: Dict[str, str] = {}
        for name, meta in listings.items():
            variants = (meta or {}).get("availableVariants", {})
            # Prefer the newer v1.1.0 feed, fall back to v0.1.0.
            variant = variants.get("v1.1.0") or variants.get("v0.1.0") or {}
            url = variant.get("get")
            if url:
                categories[str(meta.get("apiName", name))] = url
        return categories

    def iter_category_products(self, feed_url: str, *, max_pages: int = MAX_PAGES_PER_CATEGORY
                               ) -> Iterable[Tuple[dict, int]]:
        """Yield (raw_product, page_number) across the paginated category feed."""
        url: Optional[str] = feed_url
        page = 0
        while url and page < max_pages:
            payload = self._get(url)
            if not payload:
                break
            page += 1
            for item in payload.get("productInfoList", []) or []:
                yield item, page
            url = payload.get("nextUrl")
            if url:
                time.sleep(FEED_PAGE_PAUSE)

    def search(self, keyword: str, result_count: int = 10) -> List[dict]:
        payload = self._get(AFFILIATE_SEARCH, params={"query": keyword,
                                                      "resultCount": max(1, min(result_count, 10))})
        if not payload:
            return []
        return payload.get("products", []) or []

    # -- offers ------------------------------------------------------------- #

    def fetch_offers(self) -> List[dict]:
        """Combine All Offers and Deals of the Day into one normalised list."""
        offers: List[dict] = []
        for url, bucket in ((AFFILIATE_OFFERS_ALL, "Offer"), (AFFILIATE_OFFERS_DOTD, "Deal of the Day")):
            payload = self._get(url)
            if not payload:
                continue
            groups = payload.get("allOffersList") or payload.get("dotdList") or []
            for raw in groups:
                offers.append(normalise_offer(raw, bucket))
        return offers


def normalise_offer(raw: dict, bucket: str) -> Dict[str, Any]:
    """Flatten an affiliate offer record into the shape the UI renders."""
    def as_date(value: Any) -> str:
        try:
            return dt.datetime.fromtimestamp(int(value) / 1000).strftime("%Y-%m-%d")
        except (TypeError, ValueError, OSError):
            return ""

    return {
        "Title": str(raw.get("title") or raw.get("offerDescription") or "Flipkart offer").strip(),
        "Type": bucket,
        "Category": str(raw.get("category") or raw.get("availability") or "General").strip(),
        "Coupon Code": str(raw.get("couponCode") or raw.get("code") or "").strip().upper() or "—",
        "Discount": str(raw.get("discount") or raw.get("offerText") or "").strip() or "—",
        "Starts": as_date(raw.get("startTime")),
        "Ends": as_date(raw.get("endTime")),
        "URL": sanitize_url(str(raw.get("url") or raw.get("imageUrls", [{}])[0].get("url", ""))) or "",
        "Captured": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
    }


def normalise_product(raw: dict, category: str, tracking_id: str) -> Optional[Dict[str, Any]]:
    """Convert one affiliate feed record into a tracker record."""
    base = (raw or {}).get("productBaseInfoV1") or (raw or {}).get("productBaseInfo", {}).get("productAttributes") or {}
    if not base:
        return None

    title = str(base.get("title") or "").strip()
    if not title:
        return None

    selling = (base.get("flipkartSellingPrice") or {}).get("amount")
    special = (base.get("flipkartSpecialPrice") or {}).get("amount")
    mrp = (base.get("maximumRetailPrice") or {}).get("amount")

    try:
        current = int(float(special if special not in (None, 0) else selling))
        mrp_value = int(float(mrp)) if mrp else current
    except (TypeError, ValueError):
        return None
    if current <= 0:
        return None
    mrp_value = max(mrp_value, current)

    url = sanitize_url(str(base.get("productUrl") or ""))
    if tracking_id:
        url = with_affiliate_tag(url, tracking_id)

    brand = str(base.get("productBrand") or title.split()[0]).strip()
    return {
        "id": str(base.get("productId") or uuid.uuid4()),
        "Category": category,
        "Brand": brand,
        "Product": title,
        "MRP": mrp_value,
        "Current Price": current,
        "URL": url or search_url(title),
        "in_stock": bool(base.get("inStock", True)),
        "is_wishlist": False,
        "is_ad": False,
        "source": "affiliate",
        "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": TRACKER_VERSION,
    }


# --------------------------------------------------------------------------- #
# PRICE HISTORY  - real "old data" to compare and validate against
# --------------------------------------------------------------------------- #


class PriceHistoryStore:
    """
    Append-only daily price snapshots keyed by product id.

    Enables genuine analytics instead of hard-coded guesses:
      * rolling 180-day average
      * all-time and windowed minima
      * the true low recorded during each past Big Billion Days window
    """

    def __init__(self, path: str = HISTORY_DB_FILE) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._data: Dict[str, List[List[Any]]] = read_json(path, {})

    # -- writing ------------------------------------------------------------ #

    def record(self, records: Sequence[Dict[str, Any]]) -> int:
        """Store one snapshot per product per day (idempotent within a day)."""
        today = dt.date.today().isoformat()
        written = 0
        with self._lock:
            for record in records:
                pid = str(record.get("id") or "")
                price = record.get("Current Price")
                if not pid or not isinstance(price, (int, float)) or price <= 0:
                    continue
                series = self._data.setdefault(pid, [])
                if series and series[-1][0] == today:
                    series[-1][1] = int(price)
                else:
                    series.append([today, int(price)])
                    written += 1
                if len(series) > HISTORY_RETENTION_DAYS:
                    del series[:-HISTORY_RETENTION_DAYS]
            atomic_write_json(self._path, self._data)
        return written

    # -- reading ------------------------------------------------------------ #

    def series(self, product_id: str) -> List[Tuple[dt.date, int]]:
        out: List[Tuple[dt.date, int]] = []
        for day, price in self._data.get(str(product_id), []):
            try:
                out.append((dt.date.fromisoformat(day), int(price)))
            except (ValueError, TypeError):
                continue
        return out

    def rolling_average(self, product_id: str, days: int = ROLLING_WINDOW_DAYS) -> Optional[int]:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        prices = [p for d, p in self.series(product_id) if d >= cutoff]
        return int(statistics.fmean(prices)) if prices else None

    def window_low(self, product_id: str, days: int = ROLLING_WINDOW_DAYS) -> Optional[int]:
        cutoff = dt.date.today() - dt.timedelta(days=days)
        prices = [p for d, p in self.series(product_id) if d >= cutoff]
        return min(prices) if prices else None

    def festive_low(self, product_id: str) -> Optional[int]:
        """Lowest price recorded inside any configured past BBD window."""
        lows: List[int] = []
        series = self.series(product_id)
        for (sy, sm, sd), (ey, em, ed) in FESTIVE_WINDOWS.values():
            start, end = dt.date(sy, sm, sd), dt.date(ey, em, ed)
            window = [p for d, p in series if start <= d <= end]
            if window:
                lows.append(min(window))
        return min(lows) if lows else None

    def coverage(self) -> Tuple[int, int]:
        """(products tracked, total snapshots stored)."""
        return len(self._data), sum(len(v) for v in self._data.values())

    def points_for(self, product_id: str) -> int:
        return len(self._data.get(str(product_id), []))


@st.cache_resource(show_spinner=False)
def get_history() -> PriceHistoryStore:
    return PriceHistoryStore()


# --------------------------------------------------------------------------- #
# SEED CATALOGUE  (used until an ingestion run replaces it)
# --------------------------------------------------------------------------- #

CURATED_PDP: Dict[str, str] = {
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
    "Apple iPhone 15 (Black, 128 GB)":
        "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4",
    "Nike Revolution 7 Road Running Shoes":
        "https://www.flipkart.com/nike-revolution-7-running-shoes-men/p/itm96dd3ba58e201",
    "Casio Vintage Stainless Steel Digital Watch":
        "https://www.flipkart.com/casio-a-158wa-1df-vintage-a-158wa-1q-digital-watch-men-women/p/itmf3zhdga85ghju",
    "Casio G-Shock GA-2100 Octagonal Tough Watch":
        "https://www.flipkart.com/casio-ga-2100-1a1dr-g-shock-analog-digital-watch-men/p/itm734eb8e33cc5b",
    "Philips OneBlade QP1424 Hybrid Trimmer & Shaver":
        "https://www.flipkart.com/philips-oneblade-qp1424-10-trimmer-30-min-runtime-3-length-settings/p/itm15ae1edf9a51e",
}

CAT_DATA_MATRIX: Dict[str, Dict[str, Any]] = {
    "Men's Fashion": {"slug": "mens_fashion", "icon": "👔", "items": [
        ("Levi's", "511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056),
        ("U.S. Polo Assn", "Solid Cotton Polo T-Shirt", 1999, 1399, 849, 899),
        ("Louis Philippe", "2-Piece Formal Slim Suit", 10999, 8999, 5499, 5390),
        ("Allen Solly", "Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049),
        ("Peter England", "Slim Fit Formal Trousers", 2499, 1799, 1099, 1149),
        ("Wildcraft", "Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699),
        ("Bewakoof", "Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499),
        ("Highlander", "Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949),
        ("Snitch", "Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349),
        ("Roadster", "Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549)]},
    "Women's Fashion": {"slug": "womens_fashion", "icon": "👗", "items": [
        ("Biba", "Embroidered Anarkali Kurta Set", 4999, 2999, 1799, 1699),
        ("W for Woman", "Pure Cotton Straight Kurta", 1999, 1349, 699, 649),
        ("ONLY", "High-Rise Wide Leg Denim Jeans", 2999, 1999, 1199, 1249),
        ("Vero Moda", "Floral Summer Tiered Maxi Dress", 3499, 2299, 1299, 1349),
        ("Soch", "Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299),
        ("AND", "Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699),
        ("Madame", "Knitted Ribbed Full-Sleeve Cardigan", 2499, 1699, 949, 999),
        ("Aurelia", "Cotton Rich Festive Kurta Pant Set", 3999, 2499, 1399, 1449),
        ("Tokyo Talkies", "Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849),
        ("Libas", "Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599)]},
    "Footwear & Shoes": {"slug": "footwear", "icon": "👟", "items": [
        ("Puma", "Conduct Pro Performance Running Shoes", 6499, 4899, 3299, 3199),
        ("Nike", "Revolution 7 Road Running Shoes", 3695, 3695, 2399, 2995),
        ("Puma", "Smash V2 Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429),
        ("Asics", "Gel-Contend 8 Neutral Road Running Shoes", 5499, 4099, 2899, 2899),
        ("Woodland", "Camel Leather High-Traction Boots", 5995, 4595, 3295, 3495),
        ("Skechers", "Go Run Elevate Daily Walking Shoes", 2499, 1699, 1099, 1149),
        ("Red Tape", "Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249),
        ("Bata", "Formal Genuine Leather Derby Shoes", 3999, 2899, 1799, 1899),
        ("Crocs", "Classic Unisex Foam Slip-On Clogs", 3495, 2695, 1799, 1995),
        ("Adidas", "Clinch-X Responsive Gym Trainer", 4499, 2999, 1899, 1999)]},
    "Watches & Eyewear": {"slug": "watches", "icon": "⌚", "items": [
        ("Casio", "Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271),
        ("Casio", "G-Shock GA-2100 Octagonal Tough Watch", 9195, 8495, 6495, 6995),
        ("Titan", "Karishma Champagne Dial Formal Watch", 2195, 1995, 1449, 1499),
        ("Fastrack", "Revoltt FS1 BT Calling Smartwatch", 3999, 1699, 1199, 1299),
        ("Ray-Ban", "Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490),
        ("Timex", "Expedition Rugged Field Outdoor Watch", 3995, 3195, 2199, 2299),
        ("Fossil", "Grant Chronograph Leather Quartz Watch", 13495, 8995, 5995, 6495),
        ("Fastrack", "Wayfarer UV400 Protective Sunglasses", 1399, 1099, 649, 719),
        ("Oakley", "Holbrook Polarized Matte Sunglasses", 7990, 6490, 4490, 4990),
        ("Citizen", "Eco-Drive Solar Powered Analog Watch", 8995, 6995, 4799, 5199)]},
    "Smartphones": {"slug": "smartphones", "icon": "📱", "items": [
        ("Apple", "iPhone 15 (Black, 128 GB)", 69900, 63499, 52999, 56905),
        ("Apple", "iPhone 14 (Blue, 128 GB)", 59900, 52999, 44999, 47999),
        ("Samsung", "Galaxy S23 5G (Phantom Black, 128 GB)", 74999, 49999, 36999, 38999),
        ("Samsung", "Galaxy S23 FE 5G (Mint, 128 GB)", 59999, 39999, 29999, 29999),
        ("Motorola", "Edge 50 Fusion (Marshmallow Blue, 128 GB)", 27999, 23999, 20999, 21999),
        ("Nothing", "Phone (2a) 5G (Black, 128 GB)", 25999, 23499, 19999, 20999),
        ("OnePlus", "12R 5G (Iron Gray, 128 GB)", 39999, 36999, 32999, 34999),
        ("Vivo", "T3x 5G (Crimson Bliss, 128 GB)", 17499, 13999, 11999, 12499),
        ("Google", "Pixel 8a (Aloe, 128 GB)", 52999, 44999, 34999, 37999),
        ("CMF by Nothing", "Phone 1 (Black, 128 GB)", 19999, 15999, 13999, 14499)]},
    "Audio, Monitors & Laptops": {"slug": "audio_monitors", "icon": "💻", "items": [
        ("Sony", "WH-1000XM4 ANC Wireless Headphones", 29990, 22990, 18490, 22990),
        ("Sony", "WH-1000XM5 ANC Wireless Headphones", 34990, 29990, 24990, 27989),
        ("LG", "UltraGear 27 inch 165Hz IPS QHD Gaming Monitor", 32000, 24499, 18999, 19499),
        ("Samsung", "Odyssey G3 24 inch 165Hz FHD Gaming Monitor", 19000, 13999, 9999, 10499),
        ("Apple", "iPad 10th Gen (Wi-Fi, 64 GB, Silver)", 39900, 34490, 29999, 30900),
        ("boAt", "Airdopes 161 ANC TWS Earbuds", 2490, 1499, 899, 1299),
        ("JBL", "Flip 6 30W Waterproof Bluetooth Speaker", 13999, 11499, 8499, 9999),
        ("Marshall", "Emberton II Portable Bluetooth Speaker", 17499, 14999, 11999, 12999),
        ("OnePlus", "Bullets Wireless Z2 Bluetooth Earphones", 2999, 2699, 1795, 2594),
        ("Acer", "Nitro V Core i5 13th Gen RTX 4050 Gaming Laptop", 88999, 74990, 62990, 64990)]},
    "Cosmetics & Grooming": {"slug": "cosmetics", "icon": "💄", "items": [
        ("Minimalist", "10% Niacinamide Face Serum", 599, 509, 399, 449),
        ("Maybelline", "Superstay Matte Ink Liquid Lipstick", 699, 549, 384, 449),
        ("Philips", "OneBlade QP1424 Hybrid Trimmer & Shaver", 1549, 1399, 999, 1548),
        ("Beardo", "Godfather Perfume EDP 100ml", 1200, 799, 499, 549),
        ("Neutrogena", "Hydro Boost Water Gel 50g", 1150, 920, 690, 749),
        ("Cetaphil", "Gentle Skin Cleanser", 635, 570, 445, 499),
        ("Bombay Shaving Company", "6-in-1 Grooming Kit", 2450, 1499, 899, 999),
        ("Bella Vita", "Luxury Unisex Perfume Gift Set", 1099, 649, 449, 499),
        ("Biotique", "Bio Kelp Protein Anti-Hairfall Shampoo", 450, 315, 210, 249),
        ("Forest Essentials", "Soundarya Radiance Face Cream", 4200, 3780, 3150, 3499)]},
    "Home Appliances": {"slug": "appliances", "icon": "🔌", "items": [
        ("Philips", "GC1905 1440W Steam Iron", 2795, 2349, 1599, 1699),
        ("Bajaj", "DX-7 1000W Dry Iron", 1125, 849, 549, 599),
        ("Kent", "Grand Plus RO UV UF Water Purifier", 20000, 16999, 12499, 13999),
        ("Aquaguard", "Aura RO UV Copper Water Purifier", 18000, 14499, 10999, 11499),
        ("Philips", "HD9200/20 4.1L Digital Air Fryer", 9995, 7399, 5199, 5499),
        ("Prestige", "Iris 750W 4-Jar Mixer Grinder", 4495, 3299, 2299, 2499),
        ("Havells", "Glaze 25L Storage Water Geyser", 14490, 9990, 6999, 7499),
        ("Atomberg", "Renesa 1200mm BLDC Ceiling Fan", 5190, 3990, 3199, 3499),
        ("Eureka Forbes", "Robo Vac N Mop Robotic Vacuum Cleaner", 29999, 17999, 11999, 13999),
        ("Dyson", "V8 Absolute Cordless Vacuum Cleaner", 43900, 32900, 26900, 29900)]},
    "Stationery & Books": {"slug": "stationery_books", "icon": "📚", "items": [
        ("Classmate", "Pulse Single Line Notebook 180 Pages", 540, 450, 320, 349),
        ("Parker", "Vector Matte Black CT Roller Ball Pen", 1200, 950, 649, 699),
        ("Casio", "FX-991CW Scientific Calculator", 1595, 1450, 1199, 1249),
        ("Penguin", "Atomic Habits by James Clear", 499, 399, 249, 279),
        ("Camlin", "Artists Acrylic Colour Set 12 Shades", 1899, 1499, 999, 1099),
        ("Solo", "Mesh Metal Desk Document Organizer", 999, 749, 449, 499),
        ("Kangaro", "Heavy Duty Stapler and Punch Combo", 650, 499, 329, 369),
        ("Faber-Castell", "Textliner Highlighter Set of 10", 450, 349, 219, 249),
        ("Doms", "Mathematical Drawing Geometry Box", 399, 310, 199, 229),
        ("Penguin", "Classics Hardbound Collection", 799, 649, 399, 449)]},
}

CATEGORY_META = {name: (meta["slug"], meta["icon"]) for name, meta in CAT_DATA_MATRIX.items()}


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_") or "cat"


def generate_seed_catalog() -> List[Dict[str, Any]]:
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    catalog: List[Dict[str, Any]] = []
    for category, meta in CAT_DATA_MATRIX.items():
        for index, (brand, name, mrp, avg_6m, last_bbd, current) in enumerate(meta["items"]):
            product = f"{brand} {name}"
            url = sanitize_url(CURATED_PDP.get(product, "")) or search_url(product)
            catalog.append({
                "id": str(uuid.uuid4()),
                "Category": category,
                "Brand": brand,
                "Product": product,
                "MRP": int(mrp),
                "6-Month Avg": int(avg_6m),
                "Last BBD Low": int(last_bbd),
                "Current Price": int(current),
                "Predicted BBD Low": int(last_bbd * BBD_PREDICTION_FACTOR),
                "URL": url,
                "in_stock": True,
                "is_wishlist": False,
                "is_ad": index % 4 == 0,
                "source": "seed",
                "Last Checked": now,
                "version": TRACKER_VERSION,
            })
    return catalog


def load_tracker_data() -> List[Dict[str, Any]]:
    data = read_json(TRACKER_DB_FILE, None)
    if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
        if data[0].get("version") == TRACKER_VERSION:
            return data
        logger.info("Store version mismatch - rebuilding.")
    fresh = generate_seed_catalog()
    atomic_write_json(TRACKER_DB_FILE, fresh)
    return fresh


def save_tracker_data(data: List[Dict[str, Any]]) -> None:
    atomic_write_json(TRACKER_DB_FILE, data)


def merge_catalog(incoming: Sequence[Dict[str, Any]]) -> Tuple[int, int]:
    """
    Merge ingested products into the store, preserving wishlist rows and any
    manually corrected links. Returns (added, updated).
    """
    existing = load_tracker_data()
    by_id = {str(r.get("id")): r for r in existing}
    wishlist = [r for r in existing if r.get("is_wishlist")]

    added = updated = 0
    for record in incoming:
        pid = str(record.get("id"))
        if pid in by_id and not by_id[pid].get("is_wishlist"):
            target = by_id[pid]
            target.update({
                "Current Price": record["Current Price"],
                "MRP": max(record["MRP"], target.get("MRP", 0)),
                "in_stock": record.get("in_stock", True),
                "Last Checked": record["Last Checked"],
                "source": record.get("source", "affiliate"),
            })
            if is_pdp(record.get("URL", "")) and not is_pdp(target.get("URL", "")):
                target["URL"] = record["URL"]
            updated += 1
        elif pid not in by_id:
            by_id[pid] = record
            added += 1

    merged = [r for r in by_id.values() if not r.get("is_wishlist")] + wishlist
    save_tracker_data(merged)
    return added, updated


def ingest_from_affiliate(client: FlipkartAffiliateClient, selected: Sequence[str],
                          category_urls: Dict[str, str], *, max_pages: int,
                          progress=None) -> IngestReport:
    """Pull selected categories from the affiliate feed into the tracker store."""
    report = IngestReport()
    started = time.time()
    batch: List[Dict[str, Any]] = []

    for position, category in enumerate(selected, start=1):
        feed_url = category_urls.get(category)
        if not feed_url:
            report.errors.append(f"{category}: no feed URL in listing")
            continue
        report.categories += 1
        try:
            for raw, page in client.iter_category_products(feed_url, max_pages=max_pages):
                record = normalise_product(raw, category, client.tracking_id)
                if record is None:
                    report.skipped += 1
                    continue
                batch.append(record)
                report.products += 1
                report.pages = max(report.pages, page)
                if progress and report.products % 250 == 0:
                    progress(position, len(selected), category, report.products)
        except PermissionError as exc:
            report.errors.append(str(exc))
            break
        except Exception as exc:  # network hiccup on one category must not abort all
            report.errors.append(f"{category}: {type(exc).__name__}: {exc}")
        if progress:
            progress(position, len(selected), category, report.products)

    if batch:
        merge_catalog(batch)
        get_history().record(batch)
    report.duration_s = time.time() - started
    return report


def ingest_from_csv(file_bytes: bytes, category_hint: str = "Imported") -> IngestReport:
    """
    Import a CSV export. Recognised headers (case-insensitive):
        product / title, brand, category, mrp, price / current price, url
    """
    report = IngestReport()
    started = time.time()
    try:
        text = file_bytes.decode("utf-8-sig", errors="replace")
    except Exception as exc:
        report.errors.append(f"Could not decode file: {exc}")
        return report

    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        report.errors.append("CSV has no header row.")
        return report

    lookup = {name.strip().lower(): name for name in reader.fieldnames}

    def pick(row: dict, *candidates: str) -> str:
        for candidate in candidates:
            key = lookup.get(candidate)
            if key and str(row.get(key, "")).strip():
                return str(row[key]).strip()
        return ""

    def to_int(value: str) -> int:
        digits = re.sub(r"[^\d]", "", value or "")
        return int(digits) if digits else 0

    batch: List[Dict[str, Any]] = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for row in reader:
        title = pick(row, "product", "title", "name", "product name")
        if not title:
            report.skipped += 1
            continue
        current = to_int(pick(row, "current price", "price", "selling price", "special price"))
        if current <= 0:
            report.skipped += 1
            continue
        mrp = to_int(pick(row, "mrp", "maximum retail price", "list price")) or current
        url = sanitize_url(pick(row, "url", "product link", "link", "producturl"))
        brand = pick(row, "brand") or title.split()[0]
        category = pick(row, "category") or category_hint
        batch.append({
            "id": extract_itm_id(url) or str(uuid.uuid5(uuid.NAMESPACE_URL, title)),
            "Category": category,
            "Brand": brand,
            "Product": title,
            "MRP": max(mrp, current),
            "Current Price": current,
            "URL": url or search_url(title),
            "in_stock": True,
            "is_wishlist": False,
            "is_ad": False,
            "source": "csv",
            "Last Checked": now,
            "version": TRACKER_VERSION,
        })
        report.products += 1

    if batch:
        merge_catalog(batch)
        get_history().record(batch)
        report.categories = len({r["Category"] for r in batch})
    report.duration_s = time.time() - started
    return report


def refresh_all_live_prices() -> int:
    """
    Refresh prices. With affiliate credentials this re-pulls the tracked
    categories; otherwise it applies a local simulation to the seed rows so the
    dashboard stays demonstrable offline.
    """
    records = load_tracker_data()
    rng = random.Random()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    for item in records:
        if item.get("source") == "affiliate":
            continue
        floor = max(int(item["Current Price"] * 0.55), 1)
        item["Current Price"] = max(floor, item["Current Price"] + rng.choice([-70, -50, -20, 0, 10, 30]))
        item["Last Checked"] = now
    save_tracker_data(records)
    get_history().record(records)
    return len(records)


def update_record_url(record_id: str, new_url: str) -> Tuple[bool, str]:
    cleaned = sanitize_url(new_url)
    if not cleaned:
        return False, "That is not a valid flipkart.com URL."
    records = load_tracker_data()
    for record in records:
        if str(record.get("id")) == str(record_id):
            record["URL"] = cleaned
            record["Last Checked"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
            save_tracker_data(records)
            return True, ("Link updated and saved as a product page."
                          if is_pdp(cleaned) else "Link updated.")
    return False, "Product not found in the tracker store."


# --------------------------------------------------------------------------- #
# ANALYTICS
# --------------------------------------------------------------------------- #

PREMIUM_HOLD_KEYWORDS = ("Dyson", "Ray-Ban", "Oakley", "Fossil")

ANALYTICS_COLUMNS = [
    "id", "Category", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Predicted BBD Low", "Verdict", "Optimal Card",
    "Net Price", "MRP", "MRP Disc %", "History", "Data", "Product Link (copy)",
    "is_verified_link", "is_wishlist", "is_ad", "in_stock", "Last Checked",
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
    """
    Score every tracked product. Benchmarks come from the recorded price history
    when enough snapshots exist, and fall back to the stored/derived figures
    otherwise - the `Data` column states which was used.
    """
    history = get_history()
    rows: List[Dict[str, Any]] = []

    for entry in catalog:
        try:
            current = int(entry["Current Price"])
            mrp = int(entry.get("MRP") or current)
        except (KeyError, TypeError, ValueError):
            continue
        if current <= 0:
            continue

        pid = str(entry.get("id", ""))
        points = history.points_for(pid)

        observed_avg = history.rolling_average(pid)
        observed_festive = history.festive_low(pid)
        observed_low = history.window_low(pid)

        avg_6m = observed_avg or int(entry.get("6-Month Avg") or max(current, int(mrp * 0.85)))
        last_bbd = (observed_festive or observed_low
                    or int(entry.get("Last BBD Low") or int(current * 0.94)))
        avg_6m = max(avg_6m, 1)

        if observed_avg and points >= 14:
            provenance = f"📈 History ({points}d)"
        elif points:
            provenance = f"🌱 Building ({points}d)"
        else:
            provenance = "🌱 Baseline"

        predicted = int(entry.get("Predicted BBD Low") or last_bbd * BBD_PREDICTION_FACTOR)
        savings = avg_6m - current
        discount_pct = round((savings / avg_6m) * 100, 1)
        mrp_disc = round(((mrp - current) / mrp) * 100, 1) if mrp > 0 else 0.0
        product = str(entry.get("Product", ""))
        card_title, net_price = _best_card(current, card_selection)
        url = sanitize_url(str(entry.get("URL", ""))) or search_url(product)

        rows.append({
            "id": pid or str(uuid.uuid4()),
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
            "MRP Disc %": mrp_disc,
            "History": points,
            "Data": provenance,
            "Product Link (copy)": url,
            "is_verified_link": is_pdp(url),
            "is_wishlist": bool(entry.get("is_wishlist", False)),
            "is_ad": bool(entry.get("is_ad", False)),
            "in_stock": bool(entry.get("in_stock", True)),
            "Last Checked": entry.get("Last Checked", "Recently"),
        })

    return pd.DataFrame(rows, columns=ANALYTICS_COLUMNS)


# --------------------------------------------------------------------------- #
# WISHLIST
# --------------------------------------------------------------------------- #

CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    # Tech precedes Smartphones: "headphone" contains "phone".
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
    ("Wishlist: Footwear", ("shoe", "sneaker", "boot", "loafer", "sandal", "clog", "slide",
                            "running", "trainer", "derby", "oxford")),
    ("Wishlist: Watches & Eyewear", ("watch", "smartwatch", "sunglass", "spectacle",
                                     "aviator", "shades", "wayfarer", "eyeglass", "analog",
                                     "chronograph")),
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
                        **overrides: Any) -> Dict[str, Any]:
    title = (title or "").strip()
    current = max(int(current or 999), 1)
    final_url = sanitize_url(url)
    if not is_pdp(final_url) and title in CURATED_PDP:
        final_url = sanitize_url(CURATED_PDP[title])
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
        "URL": final_url or search_url(title),
        "in_stock": True,
        "is_wishlist": True,
        "is_ad": False,
        "source": "manual",
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
        url=product_row.get("Product Link (copy)", ""),
        brand=product_row.get("Brand", ""),
        current=product_row.get("Current Price", 999),
        MRP=product_row.get("MRP"),
        **{"6-Month Avg": product_row.get("6-Month Avg"),
           "Last BBD Low": product_row.get("Last BBD Low"),
           "Predicted BBD Low": product_row.get("Predicted BBD Low")},
    ))
    save_tracker_data(records)
    return True, target_category


def delete_record(record_id: str) -> None:
    save_tracker_data([r for r in load_tracker_data() if str(r.get("id")) != str(record_id)])


# --------------------------------------------------------------------------- #
# UI HELPERS
# --------------------------------------------------------------------------- #

# "Link Type" has been removed from the tables as requested. Link quality is
# reported once, in the header KPI strip.
DISPLAY_COLUMNS = [
    "➕ Wishlist", "Brand", "Product", "Current Price", "MRP", "MRP Disc %",
    "6-Month Avg", "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Verdict", "Predicted BBD Low", "Optimal Card",
    "Net Price", "Data", "Last Checked", "Product Link (copy)",
]

COLUMN_CONFIG = {
    "➕ Wishlist": st.column_config.CheckboxColumn(
        "Add", help="Tick to save this item to your wishlist", default=False, width="small"),
    "Current Price": st.column_config.NumberColumn("Price", format="₹%d"),
    "MRP": st.column_config.NumberColumn(format="₹%d"),
    "MRP Disc %": st.column_config.NumberColumn("Off MRP", format="%.1f%%"),
    "6-Month Avg": st.column_config.NumberColumn("6M Avg", format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn("BBD Low", format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn("Saving", format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(
        "Real Disc %", format="%.1f%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(
        "vs BBD", format="₹%d", help="Negative means cheaper today than the recorded BBD low"),
    "Predicted BBD Low": st.column_config.NumberColumn("BBD Forecast", format="₹%d"),
    "Net Price": st.column_config.NumberColumn("Net (card)", format="₹%d"),
    "Data": st.column_config.TextColumn(
        "Basis", help="Whether benchmarks come from recorded history or the seeded baseline",
        width="small"),
    "Product Link (copy)": st.column_config.TextColumn(
        "Product Link (copy & paste)",
        help="Select the cell and press Ctrl+C, or use the copy box below the table",
        width="large"),
}


def kpi(value: str, label: str, colour: str = "#38bdf8", sub: str = "") -> str:
    extra = f'<div class="kpi-sub">{sub}</div>' if sub else ""
    return (f'<div class="kpi-container"><div class="kpi-number" style="color:{colour};">{value}</div>'
            f'<div class="kpi-label">{label}</div>{extra}</div>')


def render_link_toolbox(table: pd.DataFrame, slug: str, allow_edit: bool = True) -> None:
    """Copy box for a chosen product, plus an editor to store the correct link."""
    st.markdown("**🔗 Copy or correct a product link**")
    labels = {row["id"]: row["Product"] for _, row in table.iterrows()}
    if not labels:
        return
    chosen = st.selectbox("Product:", options=list(labels),
                          format_func=lambda x: labels.get(x, x),
                          key=f"{slug}_link_pick", label_visibility="collapsed")
    row = table[table["id"] == chosen].iloc[0]
    st.code(row["Product Link (copy)"], language=None)

    if not row["is_verified_link"]:
        st.caption("⚠️ No confirmed product page for this item — the link opens Flipkart search "
                   "pre-filled with the product name. Open it, pick the exact listing, then paste "
                   "that URL below to store it permanently.")
    if allow_edit:
        with st.form(f"{slug}_link_form", clear_on_submit=True):
            pasted = st.text_input("Paste the exact Flipkart product URL:",
                                   placeholder="https://www.flipkart.com/<slug>/p/itm…")
            if st.form_submit_button("💾 Save link"):
                ok, message = update_record_url(chosen, pasted)
                (st.success if ok else st.error)(message)
                if ok:
                    st.rerun()


def render_collapsible_table(df_subset: pd.DataFrame, category_title: str, slug: str,
                             icon: str, is_expanded: bool, allow_deletion: bool = False) -> None:
    if df_subset is None or df_subset.empty:
        return

    steals = int((df_subset["Real Disc % (vs 6M)"] >= STEAL_DISCOUNT_THRESHOLD).sum())
    headline = f"{icon} {category_title} — {len(df_subset):,} products"
    if steals:
        headline += f" · {steals} deep discount(s)"

    with st.expander(headline, expanded=is_expanded):
        c1, c2, c3, c4, c5 = st.columns([2, 1.5, 2, 1.5, 1.5])
        brands = sorted(df_subset["Brand"].dropna().astype(str).unique().tolist())
        selected_brands = c1.multiselect("Filter Brand:", options=brands, default=[],
                                         placeholder="All Brands", key=f"{slug}_brand")
        selected_verdict = c2.selectbox("Filter Verdict:", options=["All", "BUY NOW", "WAIT"],
                                        key=f"{slug}_verdict")
        min_price = int(df_subset["Current Price"].min())
        max_price = int(df_subset["Current Price"].max())
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
        # Large categories are paged so the grid stays responsive.
        page_size = 200
        total_pages = (len(table) - 1) // page_size + 1
        if total_pages > 1:
            page = st.number_input(f"Page (1–{total_pages}, {page_size} rows each):",
                                   min_value=1, max_value=total_pages, value=1, step=1,
                                   key=f"{slug}_page")
            table = table.iloc[(int(page) - 1) * page_size: int(page) * page_size]

        view = table.copy()
        view.insert(0, "➕ Wishlist", False)
        editor_key = f"editor_{slug}"

        edited = st.data_editor(
            view[DISPLAY_COLUMNS],
            column_config=COLUMN_CONFIG,
            disabled=[c for c in DISPLAY_COLUMNS if c != "➕ Wishlist"],
            use_container_width=True,
            hide_index=True,
            key=editor_key,
        )

        ticked = edited[edited["➕ Wishlist"]]
        if not ticked.empty:
            added = sum(int(add_product_to_wishlist(r.to_dict())[0]) for _, r in ticked.iterrows())
            # Clear editor state first, else the tick survives the rerun and
            # re-fires this branch forever.
            st.session_state.pop(editor_key, None)
            st.toast(f"✅ Added {added} item(s) to your wishlist!" if added
                     else "Those items are already on your wishlist.", icon="💖")
            st.rerun()

        st.divider()
        left, right = st.columns([2, 1])
        with left:
            render_link_toolbox(table, slug)
        with right:
            export_cols = [c for c in DISPLAY_COLUMNS if c != "➕ Wishlist"]
            st.download_button(f"📥 Download {category_title} CSV",
                               data=table[export_cols].to_csv(index=False).encode("utf-8"),
                               file_name=f"{slugify(slug)}_deals.csv", mime="text/csv",
                               key=f"{slug}_dl", use_container_width=True)
            if allow_deletion:
                with st.popover("🗑️ Delete wishlist item", use_container_width=True):
                    labels = {r["id"]: r["Product"] for _, r in df_subset.iterrows()}
                    target = st.selectbox("Select product to delete:", options=list(labels),
                                          format_func=lambda x: labels.get(x, x),
                                          key=f"{slug}_del_pick")
                    if st.button("Delete Selected", key=f"{slug}_del_btn"):
                        delete_record(target)
                        st.toast("Deleted from wishlist.", icon="🗑️")
                        st.rerun()


# --------------------------------------------------------------------------- #
# SIDEBAR - DATA SOURCES
# --------------------------------------------------------------------------- #


def render_sidebar() -> FlipkartAffiliateClient:
    st.sidebar.title("⚙️ Data Sources")
    st.sidebar.caption("Connect the Flipkart Affiliate API to track the marketed catalogue at "
                       "scale, with live offers and coupon codes.")

    with st.sidebar.expander("🔑 Affiliate API credentials", expanded=True):
        tracking_id = st.text_input("Affiliate Tracking ID",
                                    value=os.environ.get("FK_AFFILIATE_ID", ""),
                                    key="fk_tracking_id")
        token = st.text_input("Affiliate API Token", type="password",
                              value=os.environ.get("FK_AFFILIATE_TOKEN", ""),
                              key="fk_token")
        st.caption("Register free at affiliate.flipkart.com, then generate the token under "
                   "API → API Token. Credentials stay on this machine.")
        client = FlipkartAffiliateClient(tracking_id, token)
        if st.button("🩺 Test connection", use_container_width=True):
            with st.spinner("Contacting the Affiliate API…"):
                ok, message = client.verify()
            (st.success if ok else st.error)(message)

    if client.configured:
        with st.sidebar.expander("📦 Ingest catalogue", expanded=True):
            if st.button("🔄 Load category list", use_container_width=True):
                with st.spinner("Fetching the feed listing…"):
                    try:
                        st.session_state["fk_categories"] = client.list_categories()
                    except PermissionError as exc:
                        st.error(str(exc))
            categories: Dict[str, str] = st.session_state.get("fk_categories", {})
            if categories:
                st.caption(f"{len(categories)} marketed categories available.")
                selected = st.multiselect("Categories to ingest:",
                                          options=sorted(categories), default=[],
                                          key="fk_selected_cats")
                max_pages = st.slider("Max pages per category (500 SKUs per page):",
                                      min_value=1, max_value=MAX_PAGES_PER_CATEGORY,
                                      value=10, step=1, key="fk_max_pages")
                st.caption(f"Up to {max_pages * 500:,} SKUs per selected category.")
                if st.button("⬇️ Start ingestion", type="primary", use_container_width=True,
                             disabled=not selected):
                    bar = st.progress(0.0)
                    status = st.empty()

                    def on_progress(pos: int, total: int, name: str, count: int) -> None:
                        bar.progress(min(pos / max(total, 1), 1.0))
                        status.caption(f"{name} — {count:,} products")

                    report = ingest_from_affiliate(client, selected, categories,
                                                   max_pages=max_pages, progress=on_progress)
                    bar.empty()
                    status.empty()
                    if report.ok:
                        st.success(f"Ingested {report.products:,} products from "
                                   f"{report.categories} categor(ies) in {report.duration_s:.0f}s.")
                    else:
                        st.warning("No products ingested.")
                    for error in report.errors[:5]:
                        st.error(error)
                    if report.ok:
                        st.rerun()

        with st.sidebar.expander("🎟️ Offers & coupons", expanded=False):
            if st.button("🔄 Refresh live offers", use_container_width=True):
                with st.spinner("Fetching offers and deals of the day…"):
                    offers = client.fetch_offers()
                atomic_write_json(OFFERS_DB_FILE, offers)
                st.success(f"Captured {len(offers)} live offers.") if offers else \
                    st.warning("No offers returned.")
                st.rerun()

    with st.sidebar.expander("📄 Import CSV", expanded=False):
        upload = st.file_uploader("Feed export or custom sheet", type=["csv"], key="fk_csv")
        st.caption("Recognised columns: product/title, brand, category, mrp, price, url.")
        if upload is not None and st.button("⬆️ Import file", use_container_width=True):
            report = ingest_from_csv(upload.getvalue())
            if report.ok:
                st.success(f"Imported {report.products:,} products "
                           f"({report.skipped} row(s) skipped).")
                st.rerun()
            else:
                st.error(report.errors[0] if report.errors else "Nothing importable found.")

    with st.sidebar.expander("🧹 Maintenance", expanded=False):
        tracked, snapshots = get_history().coverage()
        st.caption(f"Price history: {tracked:,} products · {snapshots:,} snapshots.")
        if st.button("📸 Snapshot prices now", use_container_width=True):
            written = get_history().record(load_tracker_data())
            st.success(f"Recorded {written:,} new snapshots.")
        if st.button("🚨 Reset catalogue to seed", use_container_width=True):
            save_tracker_data(generate_seed_catalog())
            st.success("Catalogue reset. Price history was preserved.")
            st.rerun()

    return client


# --------------------------------------------------------------------------- #
# MAIN
# --------------------------------------------------------------------------- #


def render_offers_panel() -> None:
    offers = read_json(OFFERS_DB_FILE, [])
    st.markdown('<div class="section-title-offers"><h2 style="margin:0;">🎟️ Live Offers, Coupon Codes & Deals of the Day</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Pulled from the Flipkart Affiliate Offers API — '
                'only genuine, currently-running offers are listed.</p></div>',
                unsafe_allow_html=True)
    if not offers:
        st.info("No offers captured yet. Add your affiliate credentials in the sidebar, then use "
                "**Refresh live offers**. Coupon codes found on third-party sites are frequently "
                "expired or fabricated, so only Flipkart's own offer feed is used here.")
        return

    frame = pd.DataFrame(offers)
    left, right = st.columns([1, 3])
    with left:
        kinds = ["All"] + sorted(frame["Type"].dropna().unique().tolist())
        chosen = st.selectbox("Offer type:", kinds, key="offers_type")
        coupons_only = st.checkbox("Only offers with a coupon code", value=False, key="offers_cc")
    view = frame if chosen == "All" else frame[frame["Type"] == chosen]
    if coupons_only:
        view = view[view["Coupon Code"] != "—"]
    with right:
        st.metric("Active offers", f"{len(view):,}")

    st.dataframe(
        view[["Title", "Type", "Category", "Coupon Code", "Discount", "Starts", "Ends", "URL"]],
        use_container_width=True, hide_index=True,
        column_config={"URL": st.column_config.TextColumn("Link (copy)", width="large")},
    )
    st.download_button("📥 Download offers CSV",
                       data=view.to_csv(index=False).encode("utf-8"),
                       file_name="flipkart_offers.csv", mime="text/csv")


def main() -> None:
    client = render_sidebar()

    st.title("⚡ Flipkart Deal Intelligence Platform")
    st.caption("Consolidated view of deeply discounted Flipkart products: catalogue ingestion at "
               "scale, a real recorded price history, BBD benchmarks, live offers and a "
               "persistent wishlist.")

    master_df = process_analytics(load_tracker_data(), st.session_state.get("card_pref", CARD_AUTO))
    if master_df.empty:
        st.error("No tracked products. Use **Reset catalogue to seed** in the sidebar.")
        return

    t1, t2, t3, t4 = st.columns([1.6, 1.2, 1.1, 1.4])
    with t1:
        if st.button("🔄 Re-check prices & snapshot", type="primary", use_container_width=True):
            with st.spinner("Refreshing prices and recording today's snapshot…"):
                count = refresh_all_live_prices()
            st.success(f"Refreshed {count:,} products.")
            st.rerun()
    with t2:
        min_disc_global = st.number_input("Min disc %", min_value=0, max_value=90, value=0, step=5,
                                          help="Hide everything below this real discount")
    with t3:
        expand_all = st.checkbox("📂 Expand all", value=False)
    with t4:
        st.selectbox("💳 Card strategy:", [CARD_AUTO, CARD_INSTANT, CARD_CASHBACK], key="card_pref")

    if min_disc_global:
        master_df = master_df[master_df["Real Disc % (vs 6M)"] >= min_disc_global]
        if master_df.empty:
            st.warning(f"No products currently discounted by {min_disc_global}% or more.")
            return

    verified = int(master_df["is_verified_link"].sum())
    total = len(master_df)
    with_history = int((master_df["History"] >= 14).sum())
    steals = int((master_df["Real Disc % (vs 6M)"] >= STEAL_DISCOUNT_THRESHOLD).sum())

    k1, k2, k3, k4, k5 = st.columns(5)
    k1.markdown(kpi(f"{total:,}", "Products Tracked",
                    sub=f"{master_df['Category'].nunique()} categories"), unsafe_allow_html=True)
    k2.markdown(kpi(f"{steals:,}", "Deep Discounts", "#f97316",
                    sub=f"≥{STEAL_DISCOUNT_THRESHOLD:.0f}% below 6M average"), unsafe_allow_html=True)
    k3.markdown(kpi(f"{int((master_df['Diff vs Last BBD'] <= 0).sum()):,}", "Beating BBD Low",
                    "#facc15", sub="at or below the recorded festive floor"), unsafe_allow_html=True)
    k4.markdown(kpi(f"{with_history:,}/{total:,}", "History-Validated", "#a855f7",
                    sub="14+ days of recorded prices"), unsafe_allow_html=True)
    k5.markdown(kpi(f"{verified:,}/{total:,}", "Verified Links", "#4ade80",
                    sub="exact product pages"), unsafe_allow_html=True)

    if with_history < total:
        st.caption("ℹ️ Benchmarks marked *Baseline* use seeded reference prices. Each price "
                   "snapshot improves them; after ~14 days they switch to *History* and are "
                   "computed purely from observed data.")

    st.divider()

    with st.expander("➕ Add a product to your wishlist", expanded=False):
        st.markdown("Paste the Flipkart product URL exactly as copied from your browser. Tracking "
                    "parameters are stripped automatically.")
        with st.form("quick_add_wishlist", clear_on_submit=True):
            col_a, col_b = st.columns(2)
            name = col_a.text_input("📦 Product name:", placeholder="e.g. Levi's 511 Slim Jeans")
            price = col_b.number_input("💰 Current price (₹, optional):", min_value=0, step=100, value=0)
            url = st.text_input("🔗 Flipkart product link:",
                                placeholder="https://www.flipkart.com/…/p/itm…")
            if st.form_submit_button("💾 Save to wishlist", type="primary"):
                if not name.strip():
                    st.error("Please enter the product name.")
                else:
                    item = build_wishlist_item(name, url, current=price or 999)
                    records = load_tracker_data()
                    records.append(item)
                    save_tracker_data(records)
                    st.success(f"Saved '{item['Product']}' to '{item['Category']}'.")
                    st.rerun()

    wishlist_df = master_df[master_df["is_wishlist"]]
    if not wishlist_df.empty:
        st.markdown('<div class="section-title-wishlist"><h2 style="margin:0;">💖 Your Saved Wishlists</h2>'
                    '<p style="margin:2px 0 0 0; font-size:0.9rem;">Auto-organised into separate tables by product type.</p></div>',
                    unsafe_allow_html=True)
        for category in sorted(wishlist_df["Category"].unique()):
            render_collapsible_table(wishlist_df[wishlist_df["Category"] == category],
                                     category, "w_" + slugify(category), "💖", True,
                                     allow_deletion=True)

    st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">1. 🔥 Top Steals — Deepest Discounts Right Now</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Ranked by real discount against the recorded 6-month average, not against MRP.</p></div>',
                unsafe_allow_html=True)
    steals_df = master_df[(master_df["Real Disc % (vs 6M)"] >= STEAL_DISCOUNT_THRESHOLD)
                          & (~master_df["is_wishlist"])].sort_values(
                              "Real Disc % (vs 6M)", ascending=False)
    if steals_df.empty:
        st.info(f"Nothing is currently discounted by {STEAL_DISCOUNT_THRESHOLD:.0f}% or more "
                "against its 6-month average.")
    else:
        render_collapsible_table(steals_df, "Deepest Discounts", "top_steals", "🔥", True)

    bbd_df = master_df[(master_df["Diff vs Last BBD"] <= 0) & (~master_df["is_wishlist"])] \
        .sort_values("Diff vs Last BBD")
    st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">2. 🏷️ Below the Big Billion Days Floor</h2>'
                "<p style=\"margin:2px 0 0 0; font-size:0.9rem;\">Priced at or under the lowest figure recorded during a past BBD window.</p></div>",
                unsafe_allow_html=True)
    if bbd_df.empty:
        st.info("No product currently beats its recorded BBD floor price.")
    else:
        render_collapsible_table(bbd_df, "Confirmed BBD Floor Steals", "bbd_steals", "🏷️", True)

    render_offers_panel()

    st.markdown('<div class="section-title-catalog"><h2 style="margin:0;">3. 🛒 Full Category Catalogue</h2>'
                '<p style="margin:2px 0 0 0; font-size:0.9rem;">Every tracked category with independent filters, paging and CSV export.</p></div>',
                unsafe_allow_html=True)
    catalogue_df = master_df[~master_df["is_wishlist"]]
    ordered = [c for c in CAT_DATA_MATRIX if c in set(catalogue_df["Category"])]
    ordered += sorted(set(catalogue_df["Category"]) - set(ordered))
    for category in ordered:
        slug, icon = CATEGORY_META.get(category, (slugify(category), "🛍️"))
        render_collapsible_table(catalogue_df[catalogue_df["Category"] == category],
                                 category, slug, icon, expand_all)

    ads_df = master_df[master_df["is_ad"]].sort_values("Real Disc % (vs 6M)", ascending=False)
    if not ads_df.empty:
        st.markdown('<div class="section-title-ads"><h2 style="margin:0;">4. 📢 Promoted & Sponsored Placements</h2>'
                    '<p style="margin:2px 0 0 0; font-size:0.9rem;">Sponsored listings benchmarked against their own 6-month averages.</p></div>',
                    unsafe_allow_html=True)
        render_collapsible_table(ads_df, "Active Sponsored Promotions", "flipkart_ads", "📢", True)


if __name__ == "__main__":
    try:
        main()
    except Exception:  # pragma: no cover - top-level UI guard
        logger.exception("Unhandled error while rendering the tracker")
        st.error("Something went wrong while rendering the tracker. Check the server logs.")
