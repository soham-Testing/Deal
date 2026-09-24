"""
deal_addons.py - add-on module for Flipkart.py (Deal Tracker)
==============================================================

Two self-contained features, kept in a separate module so Flipkart.py needs
only a handful of one-line edits:

1. PRODUCT TYPE CLASSIFIER
   detect_product_type("Levi's 511 Slim Fit Stretch Jeans (Deep Navy)")
       -> "Jeans & Denim"
   Powers the new "Product Type" filter beside the Brand filter, so you can
   narrow a department to shirts / pants / watches / sunglasses etc.

2. PRICE HISTORY TOOL (URL based)
   Paste any Flipkart product URL: it fetches the live product page, records a
   dated price point in a local store, and reports current price, 1-year
   average, 1-year low/high, and last year's BBD (festive) low.

   Flipkart does not publish past prices, so history is built from YOUR OWN
   recorded checks plus any historical prices you enter manually. Nothing is
   invented - if there is not enough history the UI says so.

This module has NO import dependency on Flipkart.py (no circular imports).

Requirements: streamlit, pandas, requests
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import statistics
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import pandas as pd
import streamlit as st

try:
    import requests
except ImportError:
    requests = None


# --------------------------------------------------------------------------- #
# CONFIG
# --------------------------------------------------------------------------- #

PRICE_HISTORY_FILE = os.environ.get("PRICE_HISTORY_FILE", "flipkart_price_history.json")

FLIPKART_HOST = "www.flipkart.com"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}
KEEP_PARAMS = {"pid", "lid", "marketplace"}

ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{10,20})(?:[/?#]|$)", re.IGNORECASE)
NON_PDP_PATH_MARKERS = ("/search", "/pr?", "/q/", "/item/p/product", "/offers-store",
                        "/all-offers", "/clp/", "/sale/")

HISTORY_TIMEOUT = 15
HISTORY_RETRIES = 2
HISTORY_DELAY = 1.0
HISTORY_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# Festive (Big Billion Days) window used to compute "Last BBD Low".
# Flipkart announces BBD dates each year, so this is an EDITABLE default
# window rather than a hard-coded claim - adjust it in the UI if needed.
DEFAULT_BBD_START = (9, 20)   # (month, day)
DEFAULT_BBD_END = (10, 15)

ONE_YEAR_DAYS = 365


# --------------------------------------------------------------------------- #
# 1. PRODUCT TYPE CLASSIFIER
# --------------------------------------------------------------------------- #

# Ordered most-specific -> most-general. First match wins.
# Word-boundary matching is used so "laptop" never matches the keyword "top".
_PRODUCT_TYPE_RULES: List[Tuple[str, Tuple[str, ...]]] = [
    ("T-Shirts & Polos",      ("t-shirt", "tshirt", "t shirt", "tee", "polo")),
    ("Shirts",                ("shirt", "shirts")),
    ("Jeans & Denim",         ("jeans", "denim", "jean")),
    ("Trousers & Chinos",     ("trouser", "trousers", "chino", "chinos", "slacks")),
    ("Cargos & Joggers",      ("cargo", "jogger", "joggers", "track pant", "track pants")),
    ("Shorts",                ("shorts", "boxer", "boxers")),
    ("Hoodies & Sweatshirts", ("hoodie", "sweatshirt", "fleece", "pullover", "sweater",
                               "cardigan", "thermal")),
    ("Suits & Blazers",       ("suit", "blazer", "bandhgala", "tuxedo")),
    ("Jackets & Coats",       ("jacket", "bomber", "windcheater", "trucker", "coat",
                               "shrug", "windproof")),
    ("Sarees",                ("saree", "sari", "sarees")),
    ("Kurtas & Ethnic",       ("kurta", "kurti", "anarkali", "lehenga", "ethnic",
                               "chikankari", "salwar", "dupatta")),
    ("Dresses & Gowns",       ("dress", "gown", "maxi", "frock")),
    ("Jumpsuits & Co-ords",   ("jumpsuit", "co ord", "co-ord", "dungaree")),
    # NOTE: bare "top"/"tops" is deliberately excluded - it collides with
    # "Laptop", "Top Tier", "Tabletop" etc. Only explicit garment phrases match.
    ("Tops & Tunics",         ("tunic", "tunics", "blouse", "crop top", "crop tops",
                               "peplum", "tank top", "casual top", "ladies top")),
    ("Leggings & Skirts",     ("legging", "leggings", "palazzo", "skirt", "jegging")),

    ("Running & Sports Shoes", ("running", "runner", "runners", "trainer", "marathon",
                                "gym", "sports shoe", "sports shoes", "badminton",
                                "cricket shoe", "football", "studs", "walking shoes")),
    ("Formal Shoes",          ("derby", "oxford", "formal shoe", "formal shoes", "loafer",
                               "loafers", "brogue")),
    # Sandals/clogs are checked BEFORE sneakers: "Slip-On Clogs" must not be
    # captured by the sneaker rule's "slip-on" keyword.
    ("Sandals & Slippers",    ("sandal", "sandals", "slipper", "slippers", "flip flop",
                               "flip flops", "clog", "clogs", "slide", "slides")),
    ("Boots",                 ("boot", "boots", "trekking")),
    ("Sneakers & Casuals",    ("sneaker", "sneakers", "casual shoe", "casual shoes",
                               "street walker", "street walkers", "slip-on", "slip on")),

    ("Smartwatches & Bands",  ("smartwatch", "smart watch", "smart band", "fitness band",
                               "bt calling", "amoled")),
    ("Watches",               ("watch", "watches", "chronograph", "analog", "g-shock",
                               "quartz", "diver")),
    ("Sunglasses & Eyewear",  ("sunglass", "sunglasses", "eyeglass", "eyeglasses",
                               "spectacle", "spectacles", "aviator", "wayfarer",
                               "clubmaster", "shades", "polarized", "eyewear")),

    ("Smartphones",           ("smartphone", "smartphones", "mobile", "iphone", "5g",
                               "galaxy", "foldable", "phone")),
    ("Laptops",               ("laptop", "laptops", "macbook", "cloudbook", "chromebook",
                               "ultrabook")),
    ("Monitors & Displays",   ("monitor", "monitors", "display", "ultrawide", "qhd", "fhd")),
    ("Headphones & Earbuds",  ("headphone", "headphones", "earbud", "earbuds", "earphone",
                               "earphones", "tws", "neckband", "headset", "anc",
                               "over-ear", "in-ear")),
    ("Speakers & Audio",      ("speaker", "speakers", "soundbar", "soundbox",
                               "home theatre", "home theater", "party speaker")),
    ("Tablets",               ("tablet", "tablets", "ipad")),

    ("Skincare",              ("serum", "moisturizer", "moisturiser", "sunscreen",
                               "face wash", "facewash", "cleanser", "lotion",
                               "face cream", "water gel", "niacinamide", "hyaluronic",
                               "spf")),
    ("Makeup",                ("lipstick", "kajal", "compact", "eye shadow", "eyeshadow",
                               "nail polish", "makeup", "foundation", "concealer",
                               "mascara")),
    ("Haircare",              ("shampoo", "conditioner", "hair mask", "hair oil",
                               "keratin", "straightener", "hair dryer", "hairfall")),
    ("Fragrance",             ("perfume", "parfum", "edp", "edt", "body spray",
                               "deodorant", "cologne")),
    ("Grooming Tools",        ("trimmer", "shaver", "grooming", "beard", "oneblade",
                               "shaving", "epilator", "clipper")),

    ("Water Purifiers",       ("purifier", "purifiers")),
    ("Kitchen Appliances",    ("mixer", "grinder", "cooker", "fryer", "induction",
                               "microwave", "kettle", "oven", "blender", "sandwich maker",
                               "gas stove", "cooktop", "toaster")),
    ("Cooling & Heating",     ("fan", "cooler", "geyser", "heater", "refrigerator",
                               "air conditioner")),
    ("Home Cleaning & Care",  ("vacuum", "iron", "mop", "washing machine", "dishwasher")),

    ("Books",                 ("book", "books", "novel", "literature", "bestseller",
                               "bestsellers", "hardbound", "paperback", "habits")),
    ("Pens & Writing",        ("pen", "pens", "fountain", "rollerball", "ballpoint",
                               "marker", "highlighter", "pencil", "pencils", "fineliner",
                               "calligraphy")),
    ("Notebooks & Paper",     ("notebook", "notebooks", "diary", "sticky note",
                               "sticky notes", "project book", "paper", "register")),
    ("Calculators",           ("calculator", "calculators")),
    ("Bags & Backpacks",      ("backpack", "bag", "bags", "school bag", "luggage")),
    ("Art & Craft",           ("acrylic", "color set", "colour set", "sketch",
                               "sketch pen", "paint", "geometry", "compass", "drawing")),
    ("Desk Accessories",      ("organizer", "organiser", "stapler", "desk", "punch")),
]

PRODUCT_TYPE_OTHER = "Other"

_PRODUCT_TYPE_PATTERNS: List[Tuple[str, re.Pattern]] = [
    (label, re.compile(r"\b(?:" + "|".join(re.escape(k) for k in keywords) + r")\b",
                       re.IGNORECASE))
    for label, keywords in _PRODUCT_TYPE_RULES
]

PRODUCT_TYPES: List[str] = [label for label, _ in _PRODUCT_TYPE_RULES] + [PRODUCT_TYPE_OTHER]

_TYPE_CACHE: Dict[str, str] = {}


def detect_product_type(title: str) -> str:
    """Classify a product title into a shopping type (Shirts, Watches, ...)."""
    if not title or not isinstance(title, str):
        return PRODUCT_TYPE_OTHER
    cached = _TYPE_CACHE.get(title)
    if cached is not None:
        return cached

    for label, pattern in _PRODUCT_TYPE_PATTERNS:
        if pattern.search(title):
            _TYPE_CACHE[title] = label
            return label

    _TYPE_CACHE[title] = PRODUCT_TYPE_OTHER
    return PRODUCT_TYPE_OTHER


def attach_product_type(df: pd.DataFrame, source_col: str = "Product",
                        target_col: str = "Type") -> pd.DataFrame:
    """Vectorised helper: add/refresh the Type column on an analytics frame."""
    if df is None or df.empty or source_col not in df.columns:
        return df
    df[target_col] = df[source_col].astype(str).map(detect_product_type)
    return df


# --------------------------------------------------------------------------- #
# SHARED SMALL UTILITIES (local, so this module stays dependency-free)
# --------------------------------------------------------------------------- #


def _atomic_write_json(path: str, payload: Any) -> None:
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


def clean_product_url(raw_url: str) -> str:
    """Normalise a pasted Flipkart URL and keep only product-identifying params."""
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


def is_product_url(url: str) -> bool:
    """True only for a single Flipkart product page (/p/itm...)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return False
    if any(marker in parsed.path.lower() for marker in NON_PDP_PATH_MARKERS):
        return False
    if parsed.query and "q=" in parsed.query:
        return False
    return bool(ITM_ID_PATTERN.search(parsed.path))


def product_key(url: str) -> str:
    match = ITM_ID_PATTERN.search(urlparse(url).path)
    return match.group(1).lower() if match else ""


def _slug_title(url: str) -> str:
    try:
        slug = urlparse(url).path.split("/p/")[0].strip("/").replace("-", " ")
        return slug.title()[:140]
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# 2a. LIVE PRODUCT PAGE SNAPSHOT
# --------------------------------------------------------------------------- #

_PRICE_CLASS_PATTERNS = (
    re.compile(r'class="[^"]*(?:Nx9bqj\s+CxhGGd|_30jeq3\s+_16Jk6d|Nx9bqj|_30jeq3)[^"]*"[^>]*>\s*(?:\u20b9|Rs\.?)\s*([0-9,]+)'),
)
_MRP_CLASS_PATTERNS = (
    re.compile(r'class="[^"]*(?:yRaY8j|_3I9_wc|_2p6lqe)[^"]*"[^>]*>\s*(?:\u20b9|Rs\.?)\s*([0-9,]+)'),
)
_TITLE_PATTERNS = (
    re.compile(r'<span[^>]*class="[^"]*(?:VU-ZEz|B_NuCI|_35KyD6)[^"]*"[^>]*>(.*?)</span>', re.DOTALL),
    re.compile(r'<h1[^>]*>(.*?)</h1>', re.DOTALL),
    re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"', re.IGNORECASE),
    re.compile(r'<title[^>]*>(.*?)</title>', re.DOTALL),
)


def _strip_tags(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text or "")).strip()


def _to_int(value: Any) -> int:
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    digits = re.sub(r"[^\d]", "", str(value or ""))
    return int(digits) if digits else 0


def _walk_json(node: Any, depth: int = 0):
    if depth > 14:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk_json(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk_json(item, depth + 1)


def parse_pdp_snapshot(html: str, url: str = "") -> Dict[str, Any]:
    """Extract title / selling price / MRP from a Flipkart product page."""
    snapshot: Dict[str, Any] = {"title": "", "price": 0, "mrp": 0, "source": ""}
    if not html:
        return snapshot

    # Strategy 1 - JSON-LD structured data (most stable when present).
    for block in re.finditer(
            r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(block.group(1).strip())
        except Exception:
            continue
        for node in _walk_json(data):
            if not isinstance(node, dict):
                continue
            if str(node.get("@type", "")).lower() != "product":
                continue
            if not snapshot["title"]:
                snapshot["title"] = str(node.get("name") or "").strip()
            offers = node.get("offers")
            if isinstance(offers, list) and offers:
                offers = offers[0]
            if isinstance(offers, dict):
                price = _to_int(offers.get("price") or offers.get("lowPrice"))
                if price and not snapshot["price"]:
                    snapshot["price"] = price
                    snapshot["source"] = "json-ld"

    # Strategy 2 - embedded app state.
    if not snapshot["price"]:
        state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>",
                                html, re.DOTALL)
        if state_match:
            try:
                state = json.loads(state_match.group(1))
            except Exception:
                state = None
            if state is not None:
                for node in _walk_json(state):
                    if not isinstance(node, dict):
                        continue
                    raw_final = node.get("finalPrice")
                    price = _to_int(raw_final.get("value") if isinstance(raw_final, dict) else raw_final)
                    if not price:
                        raw_sell = node.get("sellingPrice")
                        price = _to_int(raw_sell.get("value") if isinstance(raw_sell, dict) else raw_sell)
                    raw_mrp = node.get("mrp")
                    mrp = _to_int(raw_mrp.get("value") if isinstance(raw_mrp, dict) else raw_mrp)
                    if price:
                        snapshot["price"] = price
                        snapshot["mrp"] = snapshot["mrp"] or mrp
                        snapshot["source"] = snapshot["source"] or "app-state"
                        title = node.get("title") or node.get("name")
                        if isinstance(title, str) and title.strip() and not snapshot["title"]:
                            snapshot["title"] = title.strip()
                        break

    # Strategy 3 - rendered HTML classes.
    if not snapshot["price"]:
        for pattern in _PRICE_CLASS_PATTERNS:
            found = pattern.search(html)
            if found:
                snapshot["price"] = _to_int(found.group(1))
                snapshot["source"] = snapshot["source"] or "html"
                break
    if not snapshot["mrp"]:
        for pattern in _MRP_CLASS_PATTERNS:
            found = pattern.search(html)
            if found:
                snapshot["mrp"] = _to_int(found.group(1))
                break

    if not snapshot["title"]:
        for pattern in _TITLE_PATTERNS:
            found = pattern.search(html)
            if found:
                title = _strip_tags(found.group(1))
                title = re.sub(r"\s*[-|]\s*Buy .*$", "", title).strip()
                if title:
                    snapshot["title"] = title[:160]
                    break
    if not snapshot["title"]:
        snapshot["title"] = _slug_title(url)

    if snapshot["mrp"] and snapshot["price"] and snapshot["mrp"] < snapshot["price"]:
        snapshot["mrp"] = 0
    return snapshot


@st.cache_resource(show_spinner=False)
def _history_session():
    if requests is None:
        return None
    session = requests.Session()
    session.headers.update({
        "User-Agent": HISTORY_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-IN,en;q=0.9",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    })
    return session


def fetch_product_snapshot(url: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """Fetch a product page and return (snapshot, error_message)."""
    if not url:
        return None, "No product URL stored for this item."
    session = _history_session()
    if session is None:
        return None, "The `requests` package is required (`pip install requests`)."
    for attempt in range(1, HISTORY_RETRIES + 1):
        try:
            response = session.get(url, timeout=HISTORY_TIMEOUT)
        except Exception as exc:
            if attempt == HISTORY_RETRIES:
                return None, f"Network error: {str(exc)[:120]}"
            time.sleep(0.6 * attempt)
            continue
        if response.status_code != 200 or not response.text:
            if attempt == HISTORY_RETRIES:
                return None, f"Flipkart returned HTTP {response.status_code}."
            time.sleep(0.6 * attempt)
            continue
        snapshot = parse_pdp_snapshot(response.text, url)
        if not snapshot["price"]:
            return snapshot, ("Page fetched, but the price could not be read "
                              "(bot challenge or layout change).")
        return snapshot, ""
    return None, "Could not fetch the product page."


# --------------------------------------------------------------------------- #
# 2b. PRICE HISTORY STORE
# --------------------------------------------------------------------------- #


def load_history() -> Dict[str, Any]:
    if not os.path.exists(PRICE_HISTORY_FILE):
        return {}
    try:
        with open(PRICE_HISTORY_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_history(store: Dict[str, Any]) -> None:
    try:
        _atomic_write_json(PRICE_HISTORY_FILE, store)
    except OSError:
        pass


def record_price_point(url: str, title: str, price: int, mrp: int = 0,
                       when: Optional[dt.date] = None, note: str = "auto") -> str:
    """Append a dated price observation. Returns the product key."""
    key = product_key(url)
    if not key or price <= 0:
        return ""
    stamp = (when or dt.date.today()).isoformat()

    store = load_history()
    entry = store.get(key) or {"url": url, "title": title, "points": []}
    entry["url"] = url or entry.get("url", "")
    if title:
        entry["title"] = title
    if mrp:
        entry["mrp"] = mrp

    points: List[Dict[str, Any]] = entry.get("points", [])
    for point in points:
        if point.get("date") == stamp and point.get("note") == note:
            point["price"] = int(price)
            break
    else:
        points.append({"date": stamp, "price": int(price), "note": note})

    points.sort(key=lambda p: p.get("date", ""))
    entry["points"] = points
    entry["last_checked"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
    store[key] = entry
    save_history(store)
    return key


def delete_history_entry(key: str) -> None:
    store = load_history()
    if key in store:
        store.pop(key)
        save_history(store)


def _bbd_window(year: int, start_md: Tuple[int, int],
                end_md: Tuple[int, int]) -> Tuple[dt.date, dt.date]:
    return (dt.date(year, start_md[0], start_md[1]), dt.date(year, end_md[0], end_md[1]))


def summarise_history(entry: Dict[str, Any], *, today: Optional[dt.date] = None,
                      bbd_start: Tuple[int, int] = DEFAULT_BBD_START,
                      bbd_end: Tuple[int, int] = DEFAULT_BBD_END) -> Dict[str, Any]:
    """Compute 1-year average / low / high and last year's BBD low.

    Everything is derived strictly from recorded observations. Fields that
    cannot be computed are returned as None so the UI can say 'not enough
    history yet' instead of showing a made-up number.
    """
    today = today or dt.date.today()
    points = entry.get("points") or []
    parsed: List[Tuple[dt.date, int, str]] = []
    for point in points:
        try:
            day = dt.date.fromisoformat(str(point.get("date")))
        except Exception:
            continue
        price = int(point.get("price") or 0)
        if price > 0:
            parsed.append((day, price, str(point.get("note") or "")))
    parsed.sort(key=lambda p: p[0])

    summary: Dict[str, Any] = {
        "title": entry.get("title", ""),
        "url": entry.get("url", ""),
        "mrp": int(entry.get("mrp") or 0),
        "points": len(parsed),
        "first_seen": parsed[0][0].isoformat() if parsed else None,
        "last_seen": parsed[-1][0].isoformat() if parsed else None,
        "current": parsed[-1][1] if parsed else None,
        "avg_1y": None, "low_1y": None, "high_1y": None, "points_1y": 0,
        "bbd_low": None, "bbd_year": None, "bbd_points": 0,
        "span_days": (parsed[-1][0] - parsed[0][0]).days if len(parsed) > 1 else 0,
    }
    if not parsed:
        return summary

    cutoff = today - dt.timedelta(days=ONE_YEAR_DAYS)
    window = [price for day, price, _ in parsed if day >= cutoff]
    if window:
        summary["points_1y"] = len(window)
        summary["avg_1y"] = int(round(statistics.fmean(window)))
        summary["low_1y"] = min(window)
        summary["high_1y"] = max(window)

    # Most recent COMPLETED festive window.
    bbd_year = today.year
    start, end = _bbd_window(bbd_year, bbd_start, bbd_end)
    if today <= end:
        bbd_year -= 1
        start, end = _bbd_window(bbd_year, bbd_start, bbd_end)
    festive = [price for day, price, _ in parsed if start <= day <= end]
    summary["bbd_year"] = bbd_year
    if festive:
        summary["bbd_low"] = min(festive)
        summary["bbd_points"] = len(festive)

    return summary


def history_frame(entry: Dict[str, Any]) -> pd.DataFrame:
    rows = []
    for point in entry.get("points") or []:
        try:
            day = dt.date.fromisoformat(str(point.get("date")))
        except Exception:
            continue
        rows.append({"Date": day, "Price": int(point.get("price") or 0),
                     "Source": point.get("note") or "auto"})
    frame = pd.DataFrame(rows)
    return frame.sort_values("Date") if not frame.empty else frame


def verdict_from_summary(summary: Dict[str, Any]) -> Tuple[str, str]:
    """Return (verdict, explanation) based only on what the history supports."""
    current = summary.get("current")
    avg = summary.get("avg_1y")
    bbd = summary.get("bbd_low")
    low = summary.get("low_1y")

    if current is None:
        return "NO DATA", "No price has been recorded yet."
    if avg is None or summary.get("points_1y", 0) < 3:
        return "BUILDING HISTORY", (
            f"Only {summary.get('points_1y', 0)} recorded point(s) in the last year - "
            "re-check this product over a few days/weeks to build a reliable baseline.")

    parts = []
    if bbd:
        gap = current - bbd
        parts.append(f"Rs.{abs(gap):,} {'above' if gap > 0 else 'below'} the "
                     f"BBD {summary['bbd_year']} low of Rs.{bbd:,}")
    drop = round((avg - current) / avg * 100, 1) if avg else 0.0
    parts.append(f"{abs(drop):.1f}% {'below' if drop > 0 else 'above'} "
                 f"the 1-year average of Rs.{avg:,}")

    if bbd and current <= bbd:
        return "BUY NOW", "At or under last year's festive floor - " + "; ".join(parts) + "."
    if low is not None and current <= low:
        return "BUY NOW", "Lowest price recorded in the last year - " + "; ".join(parts) + "."
    if drop >= 15:
        return "GOOD DEAL", "; ".join(parts).capitalize() + "."
    if drop <= -5:
        return "OVERPRICED", "Above its own 1-year average - " + "; ".join(parts) + "."
    return "WAIT", "Close to its usual price - " + "; ".join(parts) + "."


def _money(value: Optional[int]) -> str:
    return f"\u20b9{value:,}" if isinstance(value, int) and value > 0 else "-"


# --------------------------------------------------------------------------- #
# 2c. PRICE HISTORY UI (separate, URL-driven section)
# --------------------------------------------------------------------------- #


def render_price_history_tool() -> None:
    """Standalone 'Price History Checker' section. Call this from main()."""
    st.markdown(
        '<div style="background:linear-gradient(90deg,#065f46,#022c22);padding:12px 18px;'
        'border-radius:8px;border-left:6px solid #34d399;margin:30px 0 15px 0;color:white;">'
        '<h2 style="margin:0;">4. \U0001F4C8 Price History Checker (by Product URL)</h2>'
        '<p style="margin:2px 0 0 0;font-size:0.9rem;">Paste any Flipkart product link to track its '
        'live price, 1-year average and last year&#39;s Big Billion Days floor.</p></div>',
        unsafe_allow_html=True)

    store = load_history()

    with st.expander("\U0001F4C8 Check a product's price history", expanded=True):
        st.caption(
            "Flipkart does not publish past prices, so this builds a real, dated history from your "
            "own checks. Check a product regularly (or use *Re-check all* below) and the 1-year "
            "average and BBD comparison get sharper over time. You can also enter prices you "
            "already know from previous sales."
        )

        with st.form("price_history_lookup", clear_on_submit=False):
            url_input = st.text_input(
                "\U0001F517 Flipkart product URL:",
                placeholder="https://www.flipkart.com/<product-slug>/p/itm...?pid=...")
            submitted = st.form_submit_button("\U0001F4C8 Fetch price & update history",
                                              type="primary")

        if submitted:
            clean = clean_product_url(url_input)
            if not clean or not is_product_url(clean):
                st.error("That is not a direct product link. Open the product on Flipkart and copy "
                         "the URL containing `/p/itm...` - a search or category link will not work.")
            else:
                with st.spinner("Fetching the live product page..."):
                    snapshot, error = fetch_product_snapshot(clean)
                if snapshot and snapshot.get("price"):
                    key = record_price_point(clean, snapshot["title"], snapshot["price"],
                                             snapshot.get("mrp", 0), note="live")
                    st.session_state["ph_selected"] = key
                    st.success(f"Recorded \u20b9{snapshot['price']:,} for **{snapshot['title']}**.")
                    store = load_history()
                else:
                    st.error(error or "Could not read a price from that page.")

        if not store:
            st.info("No products tracked yet. Paste a product URL above to start its price history.")
            return

        cfg1, cfg2, cfg3 = st.columns([2.6, 1.2, 1.2])
        labels = {k: (v.get("title") or _slug_title(v.get("url", "")) or k)
                  for k, v in store.items()}
        options = sorted(labels, key=lambda k: labels[k].lower())
        default_key = st.session_state.get("ph_selected")
        index = options.index(default_key) if default_key in options else 0
        selected = cfg1.selectbox("Tracked product:", options=options, index=index,
                                  format_func=lambda k: labels.get(k, k)[:90], key="ph_pick")
        bbd_from = cfg2.date_input("Festive window from:",
                                   value=dt.date(2000, *DEFAULT_BBD_START), key="ph_bbd_from",
                                   help="Month/day only - the year is chosen automatically.")
        bbd_to = cfg3.date_input("Festive window to:",
                                 value=dt.date(2000, *DEFAULT_BBD_END), key="ph_bbd_to")

        entry = store[selected]
        summary = summarise_history(entry,
                                    bbd_start=(bbd_from.month, bbd_from.day),
                                    bbd_end=(bbd_to.month, bbd_to.day))

        st.markdown(f"**{summary['title']}**")
        if summary.get("url"):
            st.markdown(f"[Open product page \u2197]({summary['url']})")

        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Current Price", _money(summary["current"]))
        m2.metric("1-Year Average", _money(summary["avg_1y"]),
                  delta=(f"{summary['current'] - summary['avg_1y']:+,}"
                         if summary["avg_1y"] and summary["current"] else None),
                  delta_color="inverse")
        m3.metric("1-Year Low", _money(summary["low_1y"]))
        m4.metric("1-Year High", _money(summary["high_1y"]))
        m5.metric(f"BBD {summary['bbd_year']} Low", _money(summary["bbd_low"]),
                  delta=(f"{summary['current'] - summary['bbd_low']:+,}"
                         if summary["bbd_low"] and summary["current"] else None),
                  delta_color="inverse")

        verdict, reason = verdict_from_summary(summary)
        writer = {"BUY NOW": st.success, "GOOD DEAL": st.success, "WAIT": st.warning,
                  "OVERPRICED": st.error, "BUILDING HISTORY": st.info,
                  "NO DATA": st.info}[verdict]
        writer(f"**{verdict}** - {reason}")

        if summary["bbd_low"] is None:
            st.caption(f"No price was recorded inside the {summary['bbd_year']} festive window "
                       f"({bbd_from.strftime('%d %b')} - {bbd_to.strftime('%d %b')}), so the BBD low "
                       "is unknown. Add it manually below if you know what it sold for.")

        frame = history_frame(entry)
        if len(frame) > 1:
            st.line_chart(frame.set_index("Date")["Price"], height=260)
        elif len(frame) == 1:
            st.caption("One data point so far - the trend chart appears from the second check.")

        st.dataframe(frame.sort_values("Date", ascending=False), hide_index=True,
                     use_container_width=True, height=220)

        with st.popover("\u2795 Add a known historical price"):
            st.caption("Useful for prices you remember from a previous sale - e.g. last year's BBD.")
            with st.form(f"ph_manual_{selected}", clear_on_submit=True):
                man_date = st.date_input("Date of that price:", value=dt.date.today(),
                                         max_value=dt.date.today(), key=f"ph_md_{selected}")
                man_price = st.number_input("Price (\u20b9):", min_value=1, step=100, value=999,
                                            key=f"ph_mp_{selected}")
                if st.form_submit_button("Save price point"):
                    record_price_point(entry.get("url", ""), summary["title"], int(man_price),
                                       when=man_date, note="manual")
                    st.success(f"Added \u20b9{int(man_price):,} on {man_date:%d %b %Y}.")
                    st.rerun()

        a1, a2, a3 = st.columns(3)
        if a1.button("\U0001F504 Re-check this product", key="ph_recheck_one"):
            with st.spinner("Fetching live price..."):
                snapshot, error = fetch_product_snapshot(entry.get("url", ""))
            if snapshot and snapshot.get("price"):
                record_price_point(entry["url"], snapshot["title"], snapshot["price"],
                                   snapshot.get("mrp", 0), note="live")
                st.toast(f"Recorded \u20b9{snapshot['price']:,}.", icon="\U0001F4C8")
                st.rerun()
            else:
                st.error(error or "Could not read the price.")

        if a2.button(f"\U0001F501 Re-check all {len(store)} tracked product(s)",
                     key="ph_recheck_all"):
            bar = st.progress(0.0)
            status = st.empty()
            done = failed = 0
            items = list(store.items())
            for position, (key, record) in enumerate(items, start=1):
                status.caption(f"Checking {position}/{len(items)} - {labels.get(key, key)[:70]}")
                bar.progress(position / max(len(items), 1))
                snapshot, _ = fetch_product_snapshot(record.get("url", ""))
                if snapshot and snapshot.get("price"):
                    record_price_point(record["url"], snapshot["title"], snapshot["price"],
                                       snapshot.get("mrp", 0), note="live")
                    done += 1
                else:
                    failed += 1
                if position < len(items):
                    time.sleep(HISTORY_DELAY)
            bar.empty()
            status.empty()
            st.success(f"Updated {done} product(s)."
                       + (f" {failed} could not be read." if failed else ""))
            st.rerun()

        if a3.button("\U0001F5D1\uFE0F Stop tracking this product", key="ph_delete"):
            delete_history_entry(selected)
            st.session_state.pop("ph_selected", None)
            st.toast("Removed from price tracking.", icon="\U0001F5D1\uFE0F")
            st.rerun()

        rows = []
        for key, record in store.items():
            item = summarise_history(record,
                                     bbd_start=(bbd_from.month, bbd_from.day),
                                     bbd_end=(bbd_to.month, bbd_to.day))
            verdict_label, _ = verdict_from_summary(item)
            rows.append({
                "Product": item["title"][:70],
                "Current Price": item["current"],
                "1-Year Avg": item["avg_1y"],
                "1-Year Low": item["low_1y"],
                "Last BBD Low": item["bbd_low"],
                "Data Points": item["points"],
                "Verdict": verdict_label,
                "Product Link": item["url"],
            })
        overview = pd.DataFrame(rows).sort_values("Product")
        st.markdown("##### All tracked products")
        st.dataframe(
            overview, hide_index=True, use_container_width=True,
            column_config={
                "Current Price": st.column_config.NumberColumn(format="\u20b9%d"),
                "1-Year Avg": st.column_config.NumberColumn(format="\u20b9%d"),
                "1-Year Low": st.column_config.NumberColumn(format="\u20b9%d"),
                "Last BBD Low": st.column_config.NumberColumn(format="\u20b9%d"),
                "Product Link": st.column_config.LinkColumn("Product Link",
                                                            display_text="Open \u2197"),
            })
        st.download_button("\U0001F4E5 Download price history CSV",
                           data=overview.to_csv(index=False).encode("utf-8"),
                           file_name="flipkart_price_history.csv", mime="text/csv",
                           key="ph_dl")
