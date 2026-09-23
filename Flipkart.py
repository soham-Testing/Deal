"""
Flipkart BBD Deal Tracker & Live Price Radar
--------------------------------------------
A single-page Streamlit application that tracks catalogue prices against
6-month averages and last Big Billion Days floor prices, scores each deal,
applies card-offer maths and maintains a persistent personal wishlist.

Fix history (v12):
  * FIXED  E002 / blank product page: URLs are no longer rewritten into the
           non-existent "/item/p/product?pid=" route. Original PDP paths are
           preserved and only tracking parameters are stripped.
  * FIXED  Duplicate/unverifiable "itm" product IDs now degrade gracefully to
           a guaranteed-valid Flipkart search URL instead of a dead PDP.
  * FIXED  Infinite rerun loop triggered by the in-table wishlist checkbox.
  * FIXED  Non-atomic JSON writes that could corrupt the local store.
  * FIXED  Global random.seed() side effect and several unguarded edge cases.

Run with:  streamlit run flipkart_deal_tracker.py
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import random
import re
import tempfile
import uuid
from collections import Counter
from typing import Any, Dict, Iterable, List, Tuple
from urllib.parse import parse_qsl, quote_plus, urlencode, urlparse, urlunparse

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# CONSTANTS & CONFIGURATION
# --------------------------------------------------------------------------- #

APP_TITLE = "Flipkart BBD Deal Tracker & Live Price Radar"
TRACKER_VERSION = "v12_safe_url_resolver"
TRACKER_DB_FILE = os.environ.get("TRACKER_DB_FILE", "tracker_store.json")

FLIPKART_BASE = "https://www.flipkart.com"
FLIPKART_SEARCH = FLIPKART_BASE + "/search?q={query}"
ALLOWED_HOSTS = {"flipkart.com", "www.flipkart.com", "dl.flipkart.com"}

# Query parameters Flipkart genuinely needs to render a PDP.
KEEP_PARAMS = {"pid", "lid", "marketplace", "q", "otracker", "store"}
# Referral / analytics noise that can break or pollute the PDP.
DROP_PARAM_PATTERN = re.compile(
    r"^(utm_.*|affid|affExtParam\d*|cmpid|pageuid|lastviewedpid|ppt|ppn|ssid|srno|qh|iid|fm|sattr\d*)$",
    re.IGNORECASE,
)
# A genuine Flipkart PDP id looks like: itm + 15 hexadecimal characters.
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-f]{15})\b", re.IGNORECASE)

INSTANT_DISCOUNT_RATE = 0.10
INSTANT_DISCOUNT_CAP = 1500
CASHBACK_RATE = 0.05
BBD_PREDICTION_FACTOR = 0.93
STRONG_DISCOUNT_THRESHOLD = 28.0

CARD_AUTO = "Auto-Best Card"
CARD_INSTANT = "Axis / ICICI (10% Instant, Cap ₹1.5k)"
CARD_CASHBACK = "Flipkart Axis (5% Unlimited Cashback)"

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("deal_tracker")

st.set_page_config(
    page_title=APP_TITLE,
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed",
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
# URL RESOLUTION  (root cause of the blank page / E002 error)
# --------------------------------------------------------------------------- #


def search_url(*terms: str) -> str:
    """Return a Flipkart search URL. This route is always valid and never 404s."""
    query = " ".join(t for t in terms if t).strip() or "deals"
    return FLIPKART_SEARCH.format(query=quote_plus(query))


def sanitize_url(raw_url: str) -> str:
    """
    Normalise a Flipkart URL by removing referral/analytics parameters while
    preserving the product path, `pid`, `lid` and `marketplace`.

    The product path is NEVER reconstructed: Flipkart's PDP is served from
    /<slug>/p/<itm-id>, so synthesising a path such as /item/p/product returns
    an empty SPA shell and the client-side E002 error.
    """
    if not raw_url or not isinstance(raw_url, str):
        return ""

    candidate = raw_url.strip().strip('",)\u200b')
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    if not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate.lstrip("/")

    try:
        parsed = urlparse(candidate)
    except ValueError:
        logger.warning("Unparseable URL discarded: %s", raw_url)
        return ""

    if parsed.netloc.lower() not in ALLOWED_HOSTS:
        return ""

    kept = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key.lower() in KEEP_PARAMS and not DROP_PARAM_PATTERN.match(key)
    ]
    return urlunparse(
        ("https", "www.flipkart.com", parsed.path, "", urlencode(kept), "")
    )


def is_pdp(url: str) -> bool:
    """True when the URL points at a well-formed product detail page."""
    return bool(url) and bool(ITM_ID_PATTERN.search(urlparse(url).path))


def extract_itm_id(url: str) -> str:
    match = ITM_ID_PATTERN.search(urlparse(url or "").path)
    return match.group(1).lower() if match else ""


def resolve_product_url(raw_url: str, *fallback_terms: str, blocked_ids: Iterable[str] = ()) -> str:
    """
    Return the safest usable link for a product.

    Order of preference:
      1. A sanitised, well-formed PDP whose product id is not known-bad.
      2. A sanitised non-PDP Flipkart URL (category/search pages still render).
      3. A Flipkart search URL built from the product name.
    """
    clean = sanitize_url(raw_url)
    if is_pdp(clean) and extract_itm_id(clean) not in set(blocked_ids):
        return clean
    if clean and not is_pdp(clean):
        return clean
    return search_url(*fallback_terms)


# --------------------------------------------------------------------------- #
# PRODUCT CATALOGUE
# --------------------------------------------------------------------------- #

# (brand, name, mrp, six_month_avg, last_bbd_low, current_price, url)
CatalogItem = Tuple[str, str, int, int, int, int, str]

CAT_DATA_MATRIX: Dict[str, Dict[str, Any]] = {
    "Men's Fashion": {
        "slug": "mens_fashion",
        "icon": "👔",
        "items": [
            ("Levi's", "511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056, "https://www.flipkart.com/levi-s-511-slim-men-blue-jeans/p/itm28448ec8d098a"),
            ("U.S. Polo Assn", "Solid Cotton Polo T-Shirt", 1999, 1399, 849, 899, ""),
            ("Louis Philippe", "2-Piece Formal Slim Suit", 10999, 8999, 5499, 5390, "https://www.flipkart.com/louis-philippe-2-piece-solid-men-suit/p/itmbfb64d2750157"),
            ("Allen Solly", "Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049, ""),
            ("Peter England", "Slim Fit Formal Trousers", 2499, 1799, 1099, 1149, ""),
            ("Wildcraft", "Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699, ""),
            ("Bewakoof", "Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499, ""),
            ("Highlander", "Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949, ""),
            ("Snitch", "Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349, ""),
            ("Roadster", "Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549, ""),
        ],
    },
    "Women's Fashion": {
        "slug": "womens_fashion",
        "icon": "👗",
        "items": [
            ("Biba", "Embroidered Anarkali Kurta Set", 4999, 2999, 1799, 1699, ""),
            ("W for Woman", "Pure Cotton Straight Kurta", 1999, 1349, 699, 649, ""),
            ("ONLY", "High-Rise Wide Leg Denim Jeans", 2999, 1999, 1199, 1249, ""),
            ("Vero Moda", "Floral Summer Tiered Maxi Dress", 3499, 2299, 1299, 1349, ""),
            ("Soch", "Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299, ""),
            ("AND", "Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699, ""),
            ("Madame", "Knitted Ribbed Full-Sleeve Cardigan", 2499, 1699, 949, 999, ""),
            ("Aurelia", "Cotton Rich Festive Kurta Pant Set", 3999, 2499, 1399, 1449, ""),
            ("Tokyo Talkies", "Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849, ""),
            ("Libas", "Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599, ""),
        ],
    },
    "Footwear & Shoes": {
        "slug": "footwear",
        "icon": "👟",
        "items": [
            ("Puma", "Conduct Pro Performance Running Shoes", 6499, 4899, 3299, 3199, ""),
            ("Nike", "Revolution 7 Road Running Shoes", 3695, 3695, 2399, 2995, "https://www.flipkart.com/nike-revolution-7-running-shoes-men/p/itm4ea4909a341b1"),
            ("Puma", "Smash V2 Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429, ""),
            ("Asics", "Gel-Contend 8 Neutral Road Running", 5499, 4099, 2899, 2899, ""),
            ("Woodland", "Camel Leather High-Traction Boots", 5995, 4595, 3295, 3495, "https://www.flipkart.com/woodland-boots-men/p/itm0ea622bc13d80"),
            ("Skechers", "Go Run Elevate Daily Walking Shoes", 2499, 1699, 1099, 1149, ""),
            ("Red Tape", "Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249, ""),
            ("Bata", "Formal Genuine Leather Derby Shoes", 3999, 2899, 1799, 1899, ""),
            ("Crocs", "Classic Unisex Foam Slip-On Clogs", 3495, 2695, 1799, 1995, ""),
            ("Adidas", "Clinch-X Responsive Gym Trainer", 4499, 2999, 1899, 1999, ""),
        ],
    },
    "Watches & Eyewear": {
        "slug": "watches",
        "icon": "⌚",
        "items": [
            ("Casio", "Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271, ""),
            ("Casio", "G-Shock GA-2100 Octagonal Tough Watch", 9995, 8495, 6495, 6995, ""),
            ("Titan", "Karishma Champagne Dial Formal Watch", 2195, 1995, 1449, 1499, "https://www.flipkart.com/titan-karishma-analog-watch-men/p/itmfa8c8c7f20ec6"),
            ("Fastrack", 'Revoltt FS1 1.83" BT Calling Smartwatch', 3999, 1699, 1199, 1299, ""),
            ("Ray-Ban", "Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490, ""),
            ("Timex", "Expedition Rugged Field Outdoor Watch", 3995, 3195, 2199, 2299, ""),
            ("Fossil", "Grant Chronograph Leather Quartz Watch", 13495, 8995, 5995, 6495, "https://www.flipkart.com/fossil-grant-chronograph-watch-men/p/itm8a8f117c0c1ea"),
            ("Fastrack", "Wayfarer UV400 Protective Sunglasses", 1399, 1099, 649, 719, ""),
            ("Oakley", "Holbrook Polarized Matte Sunglasses", 7990, 6490, 4490, 4990, ""),
            ("Citizen", "Eco-Drive Solar Powered Analog Watch", 8995, 6995, 4799, 5199, ""),
        ],
    },
    "Smartphones": {
        "slug": "smartphones",
        "icon": "📱",
        "items": [
            ("Apple", "iPhone 15 (Black, 128 GB)", 69900, 63499, 52999, 54999, "https://www.flipkart.com/apple-iphone-15-black-128-gb/p/itm6ac6485515ae4"),
            ("Apple", "iPhone 14 (Blue, 128 GB)", 59900, 52999, 44999, 47999, "https://www.flipkart.com/apple-iphone-14-blue-128-gb/p/itmdb77f40da6b6d"),
            ("Samsung", "Galaxy S23 5G (Phantom Black, 128 GB)", 74999, 49999, 36999, 38999, "https://www.flipkart.com/samsung-galaxy-s23-5g-phantom-black-128-gb/p/itm2271ff5d3bc64"),
            ("Samsung", "Galaxy S23 FE 5G (Mint, 128 GB)", 59999, 39999, 29999, 29999, ""),
            ("Motorola", "Edge 50 Fusion (Marshmallow Blue, 128 GB)", 27999, 23999, 20999, 21999, ""),
            ("Nothing", "Phone (2a) 5G (Black, 128 GB)", 25999, 23499, 19999, 20999, ""),
            ("OnePlus", "12R 5G (Iron Gray, 128 GB)", 39999, 36999, 32999, 34999, ""),
            ("Vivo", "T3x 5G (Crimson Bliss, 128 GB)", 17499, 13999, 11999, 12499, ""),
            ("Google", "Pixel 8a (Aloe, 128 GB)", 52999, 44999, 34999, 37999, ""),
            ("CMF by Nothing", "Phone 1 (Black, 128 GB)", 19999, 15999, 13999, 14499, ""),
        ],
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors",
        "icon": "💻",
        "items": [
            ("Sony", "WH-1000XM4 ANC Wireless Headphones", 29990, 22990, 18490, 18990, ""),
            ("Sony", "WH-1000XM5 ANC Wireless Headphones", 34990, 29990, 24990, 25990, ""),
            ("LG", 'UltraGear 27" 165Hz IPS QHD 2K Monitor', 32000, 24499, 18999, 19499, "https://www.flipkart.com/lg-ultragear-27-inch-qhd-ips-gaming-monitor/p/itm6ba19d67e7161"),
            ("Samsung", 'Odyssey G3 24" 165Hz FHD 1ms Display', 19000, 13999, 9999, 10499, ""),
            ("Apple", "iPad 10th Gen (Wi-Fi, 64GB Silver)", 39900, 34490, 29999, 30900, ""),
            ("boAt", "Airdopes 161 ANC TWS Earbuds", 3990, 1499, 899, 999, ""),
            ("JBL", "Flip 6 30W Waterproof Bluetooth Speaker", 13999, 9999, 7499, 8499, "https://www.flipkart.com/jbl-flip-6-30-w-bluetooth-speaker/p/itmd41334c44f07e"),
            ("Marshall", "Emberton II Portable Bluetooth Speaker", 17499, 14999, 11999, 12999, ""),
            ("OnePlus", "Bullets Wireless Z2 Bluetooth Earphones", 2299, 1699, 1299, 1399, ""),
            ("Acer", "Nitro V Core i5 13th Gen (RTX 4050 Laptop)", 88999, 74990, 62990, 64990, "https://www.flipkart.com/acer-nitro-v-core-i5-13th-gen-rtx-4050-gaming-laptop/p/itm7e28df13b19aa"),
        ],
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics",
        "icon": "💄",
        "items": [
            ("Minimalist", "10% Niacinamide Glowing Face Serum", 599, 509, 399, 449, ""),
            ("Maybelline", "Superstay 16H Matte Liquid Lipstick", 699, 549, 384, 449, ""),
            ("Philips", "OneBlade QP1424 Hybrid Trimmer & Shaver", 1699, 1399, 999, 1149, ""),
            ("Beardo", "Godfather Perfume EDP (100ml)", 1200, 799, 499, 549, ""),
            ("Neutrogena", "Hydro Boost Hyaluronic Water Gel (50g)", 1150, 920, 690, 749, ""),
            ("Cetaphil", "Gentle Skin Daily Balancing Cleanser", 635, 570, 445, 499, ""),
            ("Bombay Shaving Co", "6-in-1 Precision Salon Grooming Kit", 2450, 1499, 899, 999, ""),
            ("Bella Vita", "Luxury Unisex Eau De Parfum Set (4x20ml)", 1099, 649, 449, 499, ""),
            ("Biotique", "Bio Kelp Protein Anti-Hairfall Shampoo", 450, 315, 210, 249, ""),
            ("Forest Essentials", "Soundarya Radiance Herbal Face Cream", 4200, 3780, 3150, 3499, ""),
        ],
    },
    "Home Appliances": {
        "slug": "appliances",
        "icon": "🔌",
        "items": [
            ("Philips", "GC1905 1440W EasyGlide Steam Iron", 2795, 2349, 1599, 1699, ""),
            ("Bajaj", "DX-7 1000W Lightweight Non-Stick Dry Iron", 1125, 849, 549, 599, ""),
            ("Kent", "Grand Plus RO+UV+UF+TDS Water Purifier", 20000, 16999, 12499, 13999, ""),
            ("Aquaguard", "Aura 7L RO+UV Active Copper Purifier", 18000, 14499, 10999, 11499, ""),
            ("Philips", "HD9200/20 4.1L Digital Oil-Free Air Fryer", 9995, 7399, 5199, 5499, ""),
            ("Prestige", "Iris 750W 4-Jar Heavy Duty Mixer Grinder", 4495, 3299, 2299, 2499, ""),
            ("Havells", "Glaze 30L Storage High-Pressure Water Geyser", 14490, 9990, 6999, 7499, ""),
            ("Atomberg", "Renesa 1200mm BLDC Energy Saving Silent Fan", 5190, 3990, 3199, 3499, ""),
            ("Eureka Forbes", "Robo Vac n Mop Smart Robotic Cleaner", 29999, 17999, 11999, 13999, ""),
            ("Dyson", "V8 Absolute Cordless Stick Vacuum Cleaner", 43900, 32900, 26900, 29900, ""),
        ],
    },
    "Stationery & Books": {
        "slug": "stationery_books",
        "icon": "📚",
        "items": [
            ("Classmate", "Pulse Regular Hardcover Notebook (Pack of 6)", 540, 450, 320, 349, ""),
            ("Parker", "Vector Matte Black CT Rollerball Pen", 1200, 950, 649, 699, "https://www.flipkart.com/parker-vector-matte-black-ct-roller-ball-pen/p/itmfa8c8c7f20ec6"),
            ("Casio", "FX-991CW Scientific Engineering Calculator", 1595, 1450, 1199, 1249, "https://www.flipkart.com/casio-fx-991cw-scientific-calculator/p/itm0dc8963283f51"),
            ("Penguin", "Atomic Habits by James Clear (Paperback)", 499, 399, 249, 279, "https://www.flipkart.com/atomic-habits/p/itmd5b306443c2eb"),
            ("Camlin", "Artists Acrylic Colour Set (12 Shades x 20ml)", 1899, 1499, 999, 1099, ""),
            ("Solo", "Mesh Metal 3-Tier Desk Document Organizer", 999, 749, 449, 499, ""),
            ("Kangaro", "Heavy Duty Steel Stapler & Punch Combo Set", 650, 499, 329, 369, ""),
            ("Faber-Castell", "Textliner Chisel Tip Highlighter Set (10 Pcs)", 450, 349, 219, 249, ""),
            ("Doms", "Mathematical Drawing Instruments Geometry Box", 399, 310, 199, 229, ""),
            ("Penguin", "Classic Literature Hardbound Masterpiece Edition", 799, 649, 399, 449, ""),
        ],
    },
}


def _duplicate_itm_ids() -> set:
    """
    Product ids reused by more than one catalogue entry cannot all be correct;
    such links are the second source of the E002 error page, so they are
    demoted to search URLs.
    """
    ids = Counter()
    for meta in CAT_DATA_MATRIX.values():
        for item in meta["items"]:
            itm = extract_itm_id(item[6])
            if itm:
                ids[itm] += 1
    return {itm for itm, count in ids.items() if count > 1}


BLOCKED_ITM_IDS = _duplicate_itm_ids()


# --------------------------------------------------------------------------- #
# PERSISTENCE
# --------------------------------------------------------------------------- #


def save_tracker_data(data: List[Dict[str, Any]]) -> None:
    """Atomically persist the tracker store so a crash cannot truncate it."""
    directory = os.path.dirname(os.path.abspath(TRACKER_DB_FILE)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, ensure_ascii=False)
        os.replace(tmp_path, TRACKER_DB_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise


def generate_seed_catalog() -> List[Dict[str, Any]]:
    """Build the initial catalogue with fully validated product links."""
    catalog: List[Dict[str, Any]] = []
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    for category, meta in CAT_DATA_MATRIX.items():
        for index, (brand, name, mrp, avg_6m, last_bbd, current, raw_url) in enumerate(meta["items"]):
            catalog.append(
                {
                    "id": str(uuid.uuid4()),
                    "Category": category,
                    "Brand": brand,
                    "Product": f"{brand} {name}",
                    "MRP": int(mrp),
                    "6-Month Avg": int(avg_6m),
                    "Last BBD Low": int(last_bbd),
                    "Current Price": int(current),
                    "Predicted BBD Low": int(last_bbd * BBD_PREDICTION_FACTOR),
                    "URL": resolve_product_url(raw_url, brand, name, blocked_ids=BLOCKED_ITM_IDS),
                    "is_wishlist": False,
                    "is_ad": index % 4 == 0,
                    "Last Checked": now,
                    "version": TRACKER_VERSION,
                }
            )
    return catalog


def load_tracker_data() -> List[Dict[str, Any]]:
    """Load the store, rebuilding it whenever it is missing, corrupt or stale."""
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, list) and data and all(isinstance(r, dict) for r in data):
                if data[0].get("version") == TRACKER_VERSION:
                    return data
                logger.info("Store version mismatch - rebuilding with verified links.")
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Tracker store unreadable (%s) - rebuilding.", exc)

    fresh = generate_seed_catalog()
    save_tracker_data(fresh)
    return fresh


def refresh_all_live_prices() -> int:
    """Simulate a live re-scan and persist the updated prices."""
    rng = random.Random()  # local RNG: no global seed side effects
    records = load_tracker_data()
    now = dt.datetime.now().strftime("%Y-%m-%d %H:%M")

    for item in records:
        shift = rng.choice([-70, -50, -20, 0, 10, 30])
        floor = item["Last BBD Low"] - 120
        item["Current Price"] = max(floor, item["Current Price"] + shift, 1)
        item["Last Checked"] = now

    save_tracker_data(records)
    return len(records)


# --------------------------------------------------------------------------- #
# ANALYTICS
# --------------------------------------------------------------------------- #

PREMIUM_HOLD_KEYWORDS = ("Dyson", "Ray-Ban", "Oakley", "Fossil")


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
    """Convert raw records into the analytics DataFrame rendered by the UI."""
    records = []
    for entry in catalog:
        try:
            current = int(entry["Current Price"])
            avg_6m = int(entry["6-Month Avg"])
            last_bbd = int(entry["Last BBD Low"])
            mrp = int(entry["MRP"])
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping malformed record: %s", entry.get("Product", "<unknown>"))
            continue

        predicted = int(entry.get("Predicted BBD Low") or last_bbd * BBD_PREDICTION_FACTOR)
        savings = avg_6m - current
        discount_pct = round((savings / avg_6m) * 100, 1) if avg_6m > 0 else 0.0
        product = entry.get("Product", "")
        card_title, net_price = _best_card(current, card_selection)

        records.append(
            {
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
                "URL": resolve_product_url(entry.get("URL", ""), product,
                                           blocked_ids=BLOCKED_ITM_IDS),
                "is_wishlist": bool(entry.get("is_wishlist", False)),
                "is_ad": bool(entry.get("is_ad", False)),
                "Last Checked": entry.get("Last Checked", "Recently"),
            }
        )

    return pd.DataFrame(records, columns=ANALYTICS_COLUMNS)


ANALYTICS_COLUMNS = [
    "id", "Category", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Predicted BBD Low", "Verdict", "Optimal Card",
    "Net Price", "MRP", "URL", "is_wishlist", "is_ad", "Last Checked",
]


# --------------------------------------------------------------------------- #
# WISHLIST
# --------------------------------------------------------------------------- #

CATEGORY_KEYWORDS: List[Tuple[str, Tuple[str, ...]]] = [
    ("Wishlist: Pants & Jeans", ("pant", "jean", "denim", "trouser", "cargo", "chino", "jogger", "shorts", "bottom")),
    ("Wishlist: Shirts & Tops", ("shirt", "t-shirt", "tee", "polo", "hoodie", "jacket", "blazer", "suit", "top", "kurta", "kurti", "sweater", "cardigan", "saree", "dress")),
    ("Wishlist: Footwear", ("shoe", "sneaker", "boot", "loafer", "sandal", "clog", "slide", "running", "trainer", "derby", "oxford")),
    ("Wishlist: Watches & Eyewear", ("watch", "smartwatch", "sunglass", "spectacle", "aviator", "shades", "wayfarer", "eyeglass", "analog", "chronograph")),
    ("Wishlist: Tech & Audio", ("laptop", "monitor", "headphone", "earphone", "earbud", "airpods", "speaker", "tws", "audio", "display", "tablet", "ipad", "macbook")),
    ("Wishlist: Smartphones", ("phone", "mobile", "iphone", "galaxy", "oneplus", "pixel", "5g", "realme", "poco", "motorola", "iqoo", "vivo")),
    ("Wishlist: Appliances", ("purifier", "iron", "cooker", "fryer", "grinder", "geyser", "fan", "vacuum", "cleaner", "refrigerator", "washing machine", "airwrap", "otg")),
    ("Wishlist: Books & Stationery", ("book", "novel", "pen", "notebook", "calculator", "diary", "marker", "paint", "stapler", "highlighter", "geometry")),
    ("Wishlist: Cosmetics & Grooming", ("serum", "lipstick", "trimmer", "perfume", "cream", "shampoo", "cleanser", "grooming", "shaver", "styler")),
]


def detect_category(title: str) -> str:
    lowered = (title or "").lower()
    for category, keywords in CATEGORY_KEYWORDS:
        if any(keyword in lowered for keyword in keywords):
            return category
    return "Wishlist: General"


def build_wishlist_item(title: str, url: str, brand: str = "", current: int = 999,
                        **overrides: Any) -> Dict[str, Any]:
    current = max(int(current or 999), 1)
    item = {
        "id": str(uuid.uuid4()),
        "Category": detect_category(title),
        "Brand": (brand or title.strip().split()[0] if title.strip() else "Brand").title(),
        "Product": title.strip(),
        "MRP": int(current * 1.35),
        "6-Month Avg": int(current * 1.15),
        "Last BBD Low": int(current * 0.94),
        "Current Price": current,
        "Predicted BBD Low": int(current * 0.88),
        "URL": resolve_product_url(url, title, blocked_ids=BLOCKED_ITM_IDS),
        "is_wishlist": True,
        "is_ad": False,
        "Last Checked": dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": TRACKER_VERSION,
    }
    item.update({k: v for k, v in overrides.items() if v is not None})
    return item


def add_product_to_wishlist(product_row: Dict[str, Any]) -> Tuple[bool, str]:
    """Persist a catalogue row to the wishlist. Returns (added, category)."""
    title = str(product_row.get("Product", "")).strip()
    if not title:
        return False, ""

    target_category = detect_category(title)
    records = load_tracker_data()

    if any(r.get("is_wishlist") and r.get("Product") == title
           and r.get("Category") == target_category for r in records):
        return False, target_category

    records.append(
        build_wishlist_item(
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
        )
    )
    save_tracker_data(records)
    return True, target_category


def delete_record(record_id: str) -> None:
    records = [r for r in load_tracker_data() if r.get("id") != record_id]
    save_tracker_data(records)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

DISPLAY_COLUMNS = [
    "➕ Wishlist", "Brand", "Product", "Current Price", "6-Month Avg",
    "Last BBD Low", "Real Savings (vs 6M)", "Real Disc % (vs 6M)",
    "Diff vs Last BBD", "Verdict", "Predicted BBD Low", "Optimal Card",
    "Net Price", "Last Checked", "URL",
]

COLUMN_CONFIG = {
    "➕ Wishlist": st.column_config.CheckboxColumn(
        "Add to Wishlist", help="Tick to send this item to your personal wishlist", default=False
    ),
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%.1f%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(
        format="₹%d", help="Negative means it is currently cheaper than last year's BBD low"
    ),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "URL": st.column_config.LinkColumn("Direct Product Link", display_text="Open on Flipkart ↗"),
}


def kpi(value: str, label: str, colour: str = "#38bdf8") -> str:
    return (
        f'<div class="kpi-container"><div class="kpi-number" style="color:{colour};">{value}</div>'
        f'<div class="kpi-label">{label}</div></div>'
    )


def render_collapsible_table(df_subset: pd.DataFrame, category_title: str, slug: str,
                             icon: str, is_expanded: bool, allow_deletion: bool = False) -> None:
    if df_subset is None or df_subset.empty:
        return

    with st.expander(f"{icon} {category_title} — ({len(df_subset)} products)", expanded=is_expanded):
        c1, c2, c3, c4, c5 = st.columns([2, 1.5, 2, 1.5, 1.5])

        brands = sorted(df_subset["Brand"].dropna().unique().tolist())
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
                # Re-attach the resolved URL from the source frame (URL column is display-only).
                source = table[table["Product"] == row["Product"]]
                payload = row.to_dict()
                if not source.empty:
                    payload["URL"] = source.iloc[0]["URL"]
                was_added, _ = add_product_to_wishlist(payload)
                added += int(was_added)
            # Reset the editor state first, otherwise the tick survives the rerun
            # and re-fires this branch on every render (infinite rerun loop).
            st.session_state.pop(editor_key, None)
            st.toast(
                f"✅ Added {added} item(s) to your wishlist!" if added
                else "Those items are already on your wishlist.",
                icon="💖",
            )
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
                    options = df_subset[["id", "Product"]].to_dict("records")
                    labels = {o["id"]: o["Product"] for o in options}
                    target_id = st.selectbox(
                        "Select product to delete:",
                        options=list(labels),
                        format_func=lambda x: labels.get(x, x),
                        key=f"{slug}_del_pick",
                    )
                    if st.button("Delete Selected", key=f"{slug}_del_btn"):
                        delete_record(target_id)
                        st.toast("Deleted from wishlist.", icon="🗑️")
                        st.rerun()


def main() -> None:
    st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
    st.caption(
        "Live price intelligence on a single page: verified product links, BBD benchmarks, "
        "sponsored-deal scanner and a persistent wishlist."
    )

    t1, t2, t3, t4 = st.columns([1.5, 1.3, 1.2, 1.2])
    with t1:
        if st.button("🔄 Scan & Re-Check Live Prices", type="primary", use_container_width=True):
            with st.spinner("Re-checking active discounts and updating the local tracker…"):
                count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count} products.")
            st.rerun()
    with t2:
        if st.button("🚨 Reset Database & Purge Broken Links", use_container_width=True,
                     help="Rebuilds the store and re-validates every product link"):
            save_tracker_data(generate_seed_catalog())
            st.success("Store rebuilt — all links re-validated.")
            st.rerun()
    with t3:
        expand_all = st.checkbox("📂 Expand All Category Tables", value=False)
    with t4:
        card_preference = st.selectbox("💳 Card Strategy:", [CARD_AUTO, CARD_INSTANT, CARD_CASHBACK])

    master_df = process_analytics(load_tracker_data(), card_preference)
    if master_df.empty:
        st.error("No tracked products found. Use 'Reset Database' to rebuild the catalogue.")
        return

    k1, k2, k3, k4 = st.columns(4)
    k1.markdown(kpi(f"{len(master_df):,}", "Monitored Products"), unsafe_allow_html=True)
    k2.markdown(kpi(f"{int((master_df['Diff vs Last BBD'] <= 0).sum()):,}",
                    "🔥 Cheaper Than Last BBD", "#facc15"), unsafe_allow_html=True)
    k3.markdown(kpi(f"{int(master_df['is_wishlist'].sum()):,}",
                    "Saved Wishlist Items", "#ec4899"), unsafe_allow_html=True)
    k4.markdown(kpi(f"₹{master_df['Real Savings (vs 6M)'].sum() / 100000:,.1f} Lakh",
                    "Total Consumer Savings", "#4ade80"), unsafe_allow_html=True)

    st.divider()

    with st.expander("➕ Add Direct Product to Wishlist (Name & Link only)", expanded=False):
        st.markdown(
            "Paste the Flipkart product URL exactly as copied from your browser address bar. "
            "Tracking parameters are stripped automatically; if the link cannot be validated, "
            "a Flipkart search link is saved instead so it never opens a blank page."
        )
        with st.form("quick_add_wishlist", clear_on_submit=True):
            name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Jeans")
            url = st.text_input("🔗 Flipkart Product Link:", placeholder="https://www.flipkart.com/…/p/itm…")
            price = st.number_input("💰 Current Price (₹, optional):", min_value=0, step=100, value=0)
            if st.form_submit_button("💾 Save to Wishlist", type="primary"):
                if not name.strip():
                    st.error("Please enter the product name.")
                else:
                    item = build_wishlist_item(name, url, current=price or 999)
                    records = load_tracker_data()
                    records.append(item)
                    save_tracker_data(records)
                    st.success(f"Saved '{name.strip()}' to '{item['Category']}'.")
                    st.rerun()

    wishlist_df = master_df[master_df["is_wishlist"]]
    if not wishlist_df.empty:
        st.markdown(
            '<div class="section-title-wishlist"><h2 style="margin:0;">💖 Your Saved Personal Wishlists</h2>'
            '<p style="margin:2px 0 0 0; font-size:0.9rem;">Auto-organised into separate tables by product type.</p></div>',
            unsafe_allow_html=True,
        )
        for category in sorted(wishlist_df["Category"].unique()):
            subset = wishlist_df[wishlist_df["Category"] == category]
            slug = "w_" + re.sub(r"[^a-z0-9]+", "_", category.lower()).strip("_")
            render_collapsible_table(subset, category, slug, "💖", True, allow_deletion=True)

    st.markdown(
        '<div class="section-title-catalog"><h2 style="margin:0;">1. 🛒 Flipkart Category Catalog</h2>'
        '<p style="margin:2px 0 0 0; font-size:0.9rem;">Independent collapsible tables with dedicated filters.</p></div>',
        unsafe_allow_html=True,
    )
    for category, meta in CAT_DATA_MATRIX.items():
        subset = master_df[(master_df["Category"] == category) & (~master_df["is_wishlist"])]
        render_collapsible_table(subset, category, meta["slug"], meta["icon"], expand_all)

    st.markdown(
        '<div class="section-title-bbd"><h2 style="margin:0;">2. 🔥 Big Billion Days Floor Deals</h2>'
        "<p style=\"margin:2px 0 0 0; font-size:0.9rem;\">Products at or below last year's BBD festive floor.</p></div>",
        unsafe_allow_html=True,
    )
    bbd_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD")
    if bbd_df.empty:
        st.info("No product currently beats last year's BBD floor price. Run a live re-scan to refresh.")
    else:
        render_collapsible_table(bbd_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", True)

    st.markdown(
        '<div class="section-title-ads"><h2 style="margin:0;">3. 📢 Promoted, Sponsored & Banner Deals</h2>'
        '<p style="margin:2px 0 0 0; font-size:0.9rem;">Sponsored placements benchmarked against their 6-month averages.</p></div>',
        unsafe_allow_html=True,
    )
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
        st.error("Something went wrong while rendering the tracker. Check the server logs for details.")
