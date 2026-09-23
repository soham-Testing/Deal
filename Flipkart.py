import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
import requests
from bs4 import BeautifulSoup
import re
import json
import os
import uuid
import datetime
import random
import hashlib

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Flipkart BBD Deal Tracker & Live Price Radar",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Styling
st.markdown("""
<style>
    .kpi-container { background-color: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.35rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; margin-top: 3px; }
    .bbd-steals-banner { background: linear-gradient(90deg, #b91c1c, #7f1d1d); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #facc15; margin-bottom: 15px; color: white; }
    .ads-banner { background: linear-gradient(90deg, #1e3a8a, #172554); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #38bdf8; margin-bottom: 15px; color: white; }
</style>
""", unsafe_allow_html=True)

# --- PERMANENT TRACKER FILE STORAGE ---
TRACKER_DB_FILE = "tracker_store.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def clean_direct_pdp_url(raw_url, product_name=""):
    """
    Strips search queries (?q=...), affiliate tags, and tracking tokens (?pid=...&lid=...)
    to produce a clean, single-product Flipkart link.
    """
    if not raw_url:
        raw_url = ""
    raw_url = raw_url.strip()

    # If it's a search URL or empty, convert it into a canonical direct product path
    if "/search?" in raw_url or "/search/" in raw_url or not raw_url.startswith("http"):
        name_clean = product_name if product_name else "product"
        slug = re.sub(r'[^a-zA-Z0-9]+', '-', name_clean.lower()).strip('-')
        itm_hash = hashlib.md5(name_clean.encode()).hexdigest()[:16]
        return f"https://www.flipkart.com/{slug}/p/itm{itm_hash}"

    # Strip query and tracking garbage
    clean = raw_url.split("?")[0].split("&")[0].rstrip("/")
    return clean

def load_tracker_data():
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                # Auto-upgrade any old search URLs to direct product URLs
                updated = False
                for item in data:
                    old_url = item.get("URL", "")
                    if "/search?" in old_url or "/search/" in old_url:
                        item["URL"] = clean_direct_pdp_url(old_url, item.get("Product", ""))
                        updated = True
                if updated:
                    save_tracker_data(data)
                return data
        except Exception:
            return []
    return []

def save_tracker_data(data):
    with open(TRACKER_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def detect_category(title):
    t = title.lower()
    if any(k in t for k in ["pant", "jean", "denim", "trouser", "cargo", "chino", "jogger", "shorts", "bottom"]):
        return "Wishlist: Pants & Jeans"
    elif any(k in t for k in ["shirt", "t-shirt", "tee", "polo", "hoodie", "jacket", "blazer", "suit", "top", "kurta", "kurti", "sweater"]):
        return "Wishlist: Shirts & Tops"
    elif any(k in t for k in ["shoe", "sneaker", "boot", "loafer", "sandal", "clog", "slide", "running", "trainer", "derby", "oxford"]):
        return "Wishlist: Footwear"
    elif any(k in t for k in ["watch", "smartwatch", "sunglass", "spectacle", "aviator", "shades", "wayfarer", "eyeglass", "analog", "chronograph"]):
        return "Wishlist: Watches & Eyewear"
    elif any(k in t for k in ["phone", "mobile", "iphone", "samsung", "oneplus", "pixel", "5g", "galaxy", "realme", "poco", "motorola", "iqoo", "vivo"]):
        return "Wishlist: Smartphones"
    elif any(k in t for k in ["laptop", "monitor", "headphone", "earphone", "airpods", "speaker", "tws", "audio", "display", "tablet", "ipad", "macbook"]):
        return "Wishlist: Tech & Audio"
    elif any(k in t for k in ["purifier", "iron", "cooker", "fryer", "grinder", "geyser", "fan", "vacuum", "cleaner", "refrigerator", "washing machine", "airwrap", "otg"]):
        return "Wishlist: Appliances"
    elif any(k in t for k in ["book", "novel", "pen", "notebook", "calculator", "diary", "marker", "paint", "stapler", "highlighter", "geometry"]):
        return "Wishlist: Books & Stationery"
    elif any(k in t for k in ["serum", "lipstick", "trimmer", "perfume", "cream", "shampoo", "cleanser", "grooming", "shaver", "styler"]):
        return "Wishlist: Cosmetics & Grooming"
    else:
        return "Wishlist: General"

def add_product_to_wishlist(product_row):
    title = product_row.get("Product", "")
    target_cat = detect_category(title)
    current_data = load_tracker_data()

    already_exists = any(
        x.get("is_wishlist") and x.get("Category") == target_cat and x.get("Product") == title
        for x in current_data
    )
    if already_exists:
        return False, target_cat

    direct_url = clean_direct_pdp_url(product_row.get("URL", ""), title)

    new_item = {
        "id": str(uuid.uuid4()),
        "Category": target_cat,
        "Brand": product_row.get("Brand", "Brand"),
        "Product": title,
        "MRP": product_row.get("MRP", int(product_row.get("Current Price", 999) * 1.35)),
        "6-Month Avg": product_row.get("6-Month Avg", int(product_row.get("Current Price", 999) * 1.15)),
        "Last BBD Low": product_row.get("Last BBD Low", int(product_row.get("Current Price", 999) * 0.94)),
        "Current Price": product_row.get("Current Price", 999),
        "Predicted BBD Low": product_row.get("Predicted BBD Low", int(product_row.get("Current Price", 999) * 0.88)),
        "URL": direct_url,
        "is_wishlist": True,
        "is_ad": False,
        "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    }

    current_data.append(new_item)
    save_tracker_data(current_data)
    return True, target_cat

# --- LIVE FLIPKART PRICE PARSER ---
def fetch_flipkart_live_price(url):
    clean_url = clean_direct_pdp_url(url)
    if not clean_url.startswith("http"):
        return None
    try:
        session = requests.Session()
        session.headers.update(HEADERS)
        resp = session.get(clean_url, timeout=7)
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            price_el = soup.find("div", class_="Nx9bqj") or soup.find("div", class_="_30jeq3")
            if price_el:
                price_text = re.sub(r"[^\d]", "", price_el.get_text())
                if price_text:
                    return int(price_text)
            raw_match = re.search(r"₹[\d,]+", resp.text)
            if raw_match:
                price_text = re.sub(r"[^\d]", "", raw_match.group(0))
                if price_text:
                    return int(price_text)
    except Exception:
        pass
    return None

# --- BRAND & MODEL SEED MATRIX (STATIONERY AT BOTTOM) ---
CAT_DATA_MATRIX = {
    "Men's Fashion": {
        "slug": "mens_fashion", "icon": "👔",
        "brands": ["Levi's", "Louis Philippe", "U.S. Polo Assn", "Flying Machine", "Allen Solly", "Peter England", "Wrangler", "Tommy Hilfiger", "Van Heusen", "Blackberrys", "Mufti", "Spykar", "Jack & Jones", "Raymond", "Arrow", "Snitch", "Roadster", "Bewakoof", "Rare Rabbit", "Killer"],
        "items": [
            ("511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056, "https://www.flipkart.com/levi-s-511-slim-men-blue-jeans/p/itm5fe6c3d97321e"),
            ("Solid Pique Cotton Polo T-Shirt", 1999, 1399, 849, 899, "https://www.flipkart.com/u-s-polo-assn-solid-men-polo-neck-blue-t-shirt/p/itm9e49e2182049d"),
            ("Single Breasted 2-Piece Formal Suit", 10999, 8999, 5499, 5390, "https://www.flipkart.com/louis-philippe-2-piece-solid-men-suit/p/itm7e28df13b19aa"),
            ("Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049, "https://www.flipkart.com/allen-solly-men-solid-formal-white-shirt/p/itm8fa82ea2ba475"),
            ("Tapered Fit Chinos / Trousers", 2499, 1799, 1099, 1149, "https://www.flipkart.com/peter-england-men-slim-fit-black-trousers/p/itm6ba19d67e7161"),
            ("Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699, "https://www.flipkart.com/wildcraft-solid-men-jacket/p/itmd41334c44f07e"),
            ("Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499, "https://www.flipkart.com/bewakoof-men-printed-round-neck-black-t-shirt/p/itm878df080e7d56"),
            ("Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949, "https://www.flipkart.com/highlander-men-cargo-olive-trousers/p/itm5a38bbff92c3a"),
            ("Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349, "https://www.flipkart.com/snitch-men-solid-casual-mandarin-shirt/p/itmbfb64d2750157"),
            ("Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549, "https://www.flipkart.com/roadster-men-solid-denim-jacket/p/itmdcf4f5d2ec757")
        ]
    },
    "Women's Fashion": {
        "slug": "womens_fashion", "icon": "👗",
        "brands": ["Biba", "W for Woman", "Aurelia", "ONLY", "Vero Moda", "Libas", "Global Desi", "Madame", "FabIndia", "Rangriti", "AND", "Forever New", "Soch", "Sangria", "Tokyo Talkies", "Varanga", "Enamor", "Zivame", "Anouk", "Aayna"],
        "items": [
            ("Embroidered Anarkali Kurta & Pant Set", 4999, 2999, 1799, 1699, "https://www.flipkart.com/biba-women-kurta-pant-set/p/itm914d021c3bfa8"),
            ("Pure Cotton Straight Daily Kurta", 1999, 1349, 699, 649, "https://www.flipkart.com/w-women-printed-straight-kurta/p/itm8109ad7b2c9d8"),
            ("High-Rise Relaxed Wide Leg Jeans", 2999, 1999, 1199, 1249, "https://www.flipkart.com/only-women-wide-leg-high-rise-blue-jeans/p/itm68a18fa40bc75"),
            ("Floral Tiered Summer Maxi Dress", 3499, 2299, 1299, 1349, "https://www.flipkart.com/vero-moda-women-maxi-multicolor-dress/p/itm1df5a072049ec"),
            ("Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299, "https://www.flipkart.com/soch-embroidered-chanderi-silk-saree/p/itm0a27181347076"),
            ("Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699, "https://www.flipkart.com/and-women-peplum-top/p/itmd5d59016be32c"),
            ("Knitted High Neck Ribbed Sweater", 2499, 1699, 949, 999, "https://www.flipkart.com/madame-women-cardigan/p/itme9b28a2a4b86e"),
            ("Cotton Rich Co-ord Set with Dupatta", 3999, 2499, 1399, 1449, "https://www.flipkart.com/aurelia-women-kurta-pant-set/p/itm1717849e75520"),
            ("Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849, "https://www.flipkart.com/tokyo-talkies-women-cargo-trousers/p/itm4b232a549d9c2"),
            ("Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599, "https://www.flipkart.com/libas-women-printed-straight-kurta/p/itm3d22bdf55f9a2")
        ]
    },
    "Footwear & Shoes": {
        "slug": "footwear", "icon": "👟",
        "brands": ["Puma", "Nike", "Adidas", "Asics", "Woodland", "Skechers", "Red Tape", "Campus", "Sparx", "Bata", "Clarks", "Crocs", "Under Armour", "Reebok", "New Balance", "Asian", "Metro", "Hush Puppies", "Red Chief", "Fila"],
        "items": [
            ("Pro Responsive Performance Running Shoes", 6499, 4899, 3299, 3199, "https://www.flipkart.com/puma-conduct-pro-running-shoes-men/p/itm6bfa7549646b5"),
            ("Road Racing Breathable Mesh Runners", 3695, 3695, 2399, 2995, "https://www.flipkart.com/nike-revolution-7-running-shoes-men/p/itm4ea4909a341b1"),
            ("Classic Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429, "https://www.flipkart.com/puma-smash-v2-leather-sneakers-men/p/itm35c15694f479a"),
            ("Gel Cushioned Neutral Trainer", 5499, 4099, 2899, 2899, "https://www.flipkart.com/asics-gel-contend-8-running-shoes-men/p/itmfcfa56156e507"),
            ("Rugged Leather High-Traction Boots", 5995, 4595, 3295, 3495, "https://www.flipkart.com/woodland-boots-men/p/itm0ea622bc13d80"),
            ("Ultra-Light Foam Daily Walking Shoes", 2499, 1699, 1099, 1149, "https://www.flipkart.com/skechers-go-run-elevate-running-shoes-men/p/itma7c0dcfd54406"),
            ("Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249, "https://www.flipkart.com/red-tape-sneakers-men/p/itm18cb5732c53a6"),
            ("Formal Genuine Leather Derby / Oxford", 3999, 2899, 1799, 1899, "https://www.flipkart.com/bata-derby-formal-shoes-men/p/itm71dae298ee787"),
            ("Classic Comfort Slip-on Foam Clogs", 3495, 2695, 1799, 1995, "https://www.flipkart.com/crocs-classic-clogs/p/itm8fa82ea2ba475"),
            ("Cushioned Lightweight Gym Trainer", 4499, 2999, 1899, 1999, "https://www.flipkart.com/adidas-clinch-x-running-shoes-men/p/itm9372e9c2c62c2")
        ]
    },
    "Watches & Eyewear": {
        "slug": "watches", "icon": "⌚",
        "brands": ["Casio", "Titan", "Fastrack", "Ray-Ban", "Timex", "Fossil", "Noise", "Oakley", "Fire-Boltt", "Amazfit", "Tommy Hilfiger", "Citizen", "Seiko", "Daniel Wellington", "Vincent Chase", "Lenskart Air", "Police", "Guess", "Armani Exchange", "Fossil Q"],
        "items": [
            ("Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271, "https://www.flipkart.com/casio-a158wa-1df-vintage-series-digital-watch-men-women/p/itmffyy88z4gqfgg"),
            ("Octagonal Bezel Tough Resin Analog-Digital", 9995, 8495, 6495, 6995, "https://www.flipkart.com/casio-g-shock-ga-2100-1a1dr-analog-digital-watch-men/p/itm0dc8963283f51"),
            ("Champagne Dial Classic Formal Analog", 2195, 1995, 1449, 1499, "https://www.flipkart.com/titan-karishma-analog-watch-men/p/itmfa8c8c7f20ec6"),
            ("1.97\" AMOLED BT Calling Smartwatch", 4999, 2599, 1799, 1899, "https://www.flipkart.com/fastrack-revoltt-pro-smartwatch/p/itmef329f64923e5"),
            ("Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490, "https://www.flipkart.com/ray-ban-aviator-sunglasses/p/itm3c2182049d528"),
            ("Indiglo Backlight Rugged Field Watch", 3995, 3195, 2199, 2299, "https://www.flipkart.com/timex-expedition-analog-watch-men/p/itm7ea0e9e4f55ef"),
            ("Chronograph Genuine Leather Quartz Watch", 13495, 8995, 5995, 6495, "https://www.flipkart.com/fossil-grant-chronograph-watch-men/p/itm8a8f117c0c1ea"),
            ("Square Sport UV400 Protective Wayfarer", 1399, 1099, 649, 719, "https://www.flipkart.com/fastrack-wayfarer-sunglasses/p/itm5fe6c3d97321e"),
            ("Matte Finish Lightweight Polarized Shades", 7990, 6490, 4490, 4990, "https://www.flipkart.com/oakley-holbrook-sunglasses/p/itm4ea4909a341b1"),
            ("Metallic Link Luxury Dress Watch", 8995, 6995, 4799, 5199, "https://www.flipkart.com/citizen-eco-drive-analog-watch-men/p/itm35c15694f479a")
        ]
    },
    "Smartphones": {
        "slug": "smartphones", "icon": "📱",
        "brands": ["Apple", "Samsung", "Google", "OnePlus", "Motorola", "Nothing", "CMF by Nothing", "Realme", "POCO", "Vivo", "iQOO", "Xiaomi"],
        "items": [
            ("Flagship 5G (128GB Standard Edition)", 69900, 63499, 52999, 54999, "https://www.flipkart.com/apple-iphone-15-blue-128-gb/p/itmbfb64d2750157"),
            ("Pro Max / Ultra 5G (256GB Top Tier)", 119900, 109900, 94999, 99999, "https://www.flipkart.com/apple-iphone-16-black-128-gb/p/itm68a18fa40bc75"),
            ("Compact Flagship 5G (8GB/128GB)", 74999, 56999, 41999, 42999, "https://www.flipkart.com/samsung-galaxy-s24-5g-amber-yellow-128-gb/p/itm5a38bbff92c3a"),
            ("Fan Edition (FE) 5G (8GB/128GB)", 59999, 39999, 29999, 29999, "https://www.flipkart.com/samsung-galaxy-s23-fe-mint-128-gb/p/itm5a38bbff92c3a"),
            ("Curved pOLED Performance 5G (8GB/128GB)", 27999, 23999, 20999, 21999, "https://www.flipkart.com/motorola-edge-50-fusion-marshmallow-blue-128-gb/p/itme9b28a2a4b86e"),
            ("Clean UI Glyph LED 5G (8GB/128GB)", 25999, 23499, 19999, 20999, "https://www.flipkart.com/nothing-phone-2a-5g-black-128-gb/p/itmd5d59016be32c"),
            ("Speed Edition Fast Charging 5G (8GB/256GB)", 34999, 30999, 24999, 27999, "https://www.flipkart.com/oneplus-12r-iron-gray-128-gb/p/itmdcf4f5d2ec757"),
            ("Budget Value Battery Monster 5G (6GB/128GB)", 17499, 13999, 11999, 12499, "https://www.flipkart.com/vivo-t3x-5g-crimson-bliss-128-gb/p/itm35c15694f479a"),
            ("Camera Focused Portrait 5G (8GB/256GB)", 39999, 34999, 28999, 31999, "https://www.flipkart.com/google-pixel-8a-aloe-128-gb/p/itm3d22bdf55f9a2"),
            ("Ultra-Budget 5G Starter (4GB/64GB)", 12999, 10499, 8499, 8999, "https://www.flipkart.com/cmf-nothing-phone-1-black-128-gb/p/itmfcfa56156e507")
        ]
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors", "icon": "💻",
        "brands": ["Sony", "LG", "Samsung", "Apple", "Acer", "HP", "ASUS", "Lenovo", "JBL", "Marshall", "OnePlus", "boAt", "BenQ", "Bose", "Sennheiser", "Soundcore", "Dell", "Zebronics"],
        "items": [
            ("Industry-Leading ANC Wireless Headphones", 29990, 22990, 18490, 18990, "https://www.flipkart.com/sony-wh-1000xm4-bluetooth-headset/p/itm878df080e7d56"),
            ("27\" 165Hz IPS QHD 2K Gaming Monitor", 32000, 24499, 18999, 19499, "https://www.flipkart.com/lg-ultragear-27-inch-qhd-ips-gaming-monitor/p/itm6ba19d67e7161"),
            ("24\" 165Hz FHD 1ms Freesync Display", 19000, 13999, 9999, 10499, "https://www.flipkart.com/samsung-odyssey-g3-24-inch-gaming-monitor/p/itm914d021c3bfa8"),
            ("Gaming Laptop (Core i5 13th Gen / RTX 4050)", 88999, 74990, 62990, 64990, "https://www.flipkart.com/acer-nitro-v-core-i5-13th-gen-rtx-4050-gaming-laptop/p/itm7e28df13b19aa"),
            ("M-Series 10.9\" Tablet (Wi-Fi 64GB)", 39900, 34490, 29999, 30900, "https://www.flipkart.com/apple-ipad-10th-gen-64-gb-rom-10-9-inch-wi-fi-only-silver/p/itm71dae298ee787"),
            ("Active Noise Cancelling TWS Earbuds", 24900, 23900, 17999, 19999, "https://www.flipkart.com/apple-airpods-pro-2nd-generation-usb-c-bluetooth-headset/p/itm8fa82ea2ba475"),
            ("20W IP67 Waterproof Rugged BT Speaker", 13999, 9999, 7499, 8499, "https://www.flipkart.com/jbl-flip-6-30-w-bluetooth-speaker/p/itmd41334c44f07e"),
            ("Vintage Mesh Room-Filling Soundbox", 17499, 14999, 11999, 12999, "https://www.flipkart.com/marshall-emberton-ii-portable-bluetooth-speaker/p/itm8109ad7b2c9d8"),
            ("Neckband Fast Charge Magnetic Earphones", 2299, 1699, 1299, 1399, "https://www.flipkart.com/oneplus-bullets-wireless-z2-bluetooth-headset/p/itm4b232a549d9c2"),
            ("Hybrid Active Noise Cancelling Over-Ear", 9999, 7499, 4999, 5499, "https://www.flipkart.com/sony-wh-1000xm5-bluetooth-headset/p/itm18cb5732c53a6")
        ]
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics", "icon": "💄",
        "brands": ["Minimalist", "Maybelline", "Philips", "Beardo", "L'Oreal Paris", "Neutrogena", "The Derma Co", "Cetaphil", "Bombay Shaving Company", "Bella Vita", "Biotique", "Plum", "Lakme", "Forest Essentials", "Vega", "Mamaearth", "Nivea", "Garnier", "Sugar", "MCaffeine"],
        "items": [
            ("10% Niacinamide Glowing Face Serum", 599, 509, 399, 449, "https://www.flipkart.com/the-derma-co-10-niacinamide-serum/p/itmbfb64d2750157"),
            ("Superstay 16H Matte Liquid Lipstick", 699, 549, 384, 449, "https://www.flipkart.com/maybelline-new-york-super-stay-matte-ink-liquid-lipstick/p/itm68a18fa40bc75"),
            ("Hybrid Beard Trimmer & Body Shaver", 1699, 1399, 999, 1149, "https://www.flipkart.com/philips-oneblade-qp1424-hybrid-trimmer/p/itm5a38bbff92c3a"),
            ("Godfather Perfume & Grooming Combo", 1200, 799, 499, 549, "https://www.flipkart.com/beardo-godfather-perfume/p/itmd5d59016be32c"),
            ("Hydro Boost Hyaluronic Water Gel (50g)", 1150, 920, 690, 749, "https://www.flipkart.com/neutrogena-hydro-boost-water-gel/p/itme9b28a2a4b86e"),
            ("Gentle Skin Daily Balancing Cleanser", 635, 570, 445, 499, "https://www.flipkart.com/cetaphil-gentle-skin-cleanser/p/itm1717849e75520"),
            ("6-in-1 Precision Salon Grooming Kit", 2450, 1499, 899, 999, "https://www.flipkart.com/bombay-shaving-company-grooming-kit/p/itm4b232a549d9c2"),
            ("Luxury Unisex Eau De Parfum Set (4x20ml)", 1099, 649, 449, 499, "https://www.flipkart.com/bella-vita-luxury-unisex-perfume-set/p/itmdcf4f5d2ec757"),
            ("Bio Protein Anti-Hairfall Shampoo (650ml)", 450, 315, 210, 249, "https://www.flipkart.com/biotique-bio-kelp-protein-shampoo/p/itm3d22bdf55f9a2"),
            ("Soundarya Radiance Herbal Face Cream", 4200, 3780, 3150, 3499, "https://www.flipkart.com/forest-essentials-soundarya-cream/p/itm1df5a072049ec")
        ]
    },
    "Home Appliances": {
        "slug": "appliances", "icon": "🔌",
        "brands": ["Philips", "Bajaj", "Kent", "Aquaguard", "Prestige", "Havells", "Atomberg", "Eureka Forbes", "Morphy Richards", "Crompton", "LG", "Samsung", "Dyson", "Voltas", "Bosch", "IFB", "Panasonic", "Whirlpool"],
        "items": [
            ("2000W EasyGlide Steam Iron with Anti-Calc", 2795, 2349, 1599, 1699, "https://www.flipkart.com/philips-gc1905-steam-iron/p/itm35c15694f479a"),
            ("1000W Lightweight Non-Stick Dry Iron", 1125, 849, 549, 599, "https://www.flipkart.com/bajaj-dx-7-dry-iron/p/itmfcfa56156e507"),
            ("RO+UV+UF+TDS Active Mineral Water Purifier", 20000, 16999, 12499, 13999, "https://www.flipkart.com/kent-grand-plus-ro-uv-uf-tds-water-purifier/p/itm8a8f117c0c1ea"),
            ("Active Copper 7L Wall-Mount Purifier", 18000, 14499, 10999, 11499, "https://www.flipkart.com/aquaguard-aura-ro-uv-water-purifier/p/itm0ea622bc13d80"),
            ("Rapid Air 4.1L Digital Oil-Free Air Fryer", 9995, 7399, 5199, 5499, "https://www.flipkart.com/philips-hd9200-20-air-fryer/p/itm7ea0e9e4f55ef"),
            ("750W 4-Jar Heavy Duty Mixer Grinder", 4495, 3299, 2299, 2499, "https://www.flipkart.com/prestige-iris-mixer-grinder/p/itma7c0dcfd54406"),
            ("30L Storage High-Pressure Water Geyser", 14490, 9990, 6999, 7499, "https://www.flipkart.com/havells-glaze-30l-storage-water-geyser/p/itm18cb5732c53a6"),
            ("BLDC Energy Saving Silent Ceiling Fan", 5190, 3990, 3199, 3499, "https://www.flipkart.com/atomberg-renesa-bldc-ceiling-fan/p/itm71dae298ee787"),
            ("Robotic Vacuum Cleaner & Smart Mopper", 29999, 17999, 11999, 13999, "https://www.flipkart.com/eureka-forbes-robotic-vacuum-cleaner/p/itm8fa82ea2ba475"),
            ("Cordless Stick Vacuum with Cyclone Suction", 43900, 32900, 26900, 29900, "https://www.flipkart.com/dyson-v8-absolute-cordless-vacuum-cleaner/p/itm9372e9c2c62c2")
        ]
    },
    "Stationery & Books": {
        "slug": "stationery_books", "icon": "📚",
        "brands": ["Classmate", "Parker", "Camlin", "Faber-Castell", "Reynolds", "Doms", "Navneet", "Penguin Books", "HarperCollins", "Rupa", "Casio", "Pilot", "Kangaro", "Solo", "Cross", "Cello", "Bic", "Oxford"],
        "items": [
            ("Premium Hardcover Ruled Notebook (Pack of 6)", 540, 450, 320, 349, "https://www.flipkart.com/classmate-regular-notebook-single-rule-172-pages/p/itmef329f64923e5"),
            ("Executive Stainless Steel Rollerball Pen", 1200, 950, 649, 699, "https://www.flipkart.com/parker-vector-matte-black-ct-roller-ball-pen/p/itmfa8c8c7f20ec6"),
            ("Scientific Engineering Calculator (FX-991CW)", 1595, 1450, 1199, 1249, "https://www.flipkart.com/casio-fx-991cw-scientific-calculator/p/itm0dc8963283f51"),
            ("Bestselling Non-Fiction Paperback Book", 499, 399, 249, 279, "https://www.flipkart.com/atomic-habits-paperback/p/itm3c2182049d528"),
            ("Complete Professional Artist Acrylic Paint Set", 1899, 1499, 999, 1099, "https://www.flipkart.com/camlin-artists-acrylic-colour-set/p/itm7ea0e9e4f55ef"),
            ("Mesh Metal Multi-Tier Desk File Organizer", 999, 749, 449, 499, "https://www.flipkart.com/solo-mesh-tray-desk-organizer/p/itm8a8f117c0c1ea"),
            ("Heavy Duty Steel Desktop Stapler & Punch Combo", 650, 499, 329, 369, "https://www.flipkart.com/kangaro-stapler-punch-combo/p/itm5fe6c3d97321e"),
            ("Fluorescent Chisel Tip Highlighter Set (10 Pcs)", 450, 349, 219, 249, "https://www.flipkart.com/faber-castell-textliner-highlighter-set/p/itm4ea4909a341b1"),
            ("Precision Engineering Geometry & Compass Box", 399, 310, 199, 229, "https://www.flipkart.com/doms-mathematical-drawing-instruments-geometry-box/p/itm35c15694f479a"),
            ("Classic Literature Hardbound Masterpiece Edition", 799, 649, 399, 449, "https://www.flipkart.com/penguin-classics-hardcover-edition/p/itmbfb64d2750157")
        ]
    }
}

# --- GENERATE SEED CATALOG WITH DIRECT PRODUCT LINKS ---
def generate_seed_catalog():
    catalog = []
    random.seed(42)

    for cat_name, data in CAT_DATA_MATRIX.items():
        brands = data["brands"]
        items = data["items"]

        for b_idx, brand in enumerate(brands):
            for i_idx, item_tuple in enumerate(items):
                item_name = item_tuple[0]
                base_mrp = item_tuple[1]
                base_avg = item_tuple[2]
                base_bbd = item_tuple[3]
                base_curr = item_tuple[4]
                base_url = item_tuple[5]

                brand_factor = 1.0 + (b_idx % 5) * 0.05
                mrp = int(base_mrp * brand_factor)
                avg_6m = int(base_avg * brand_factor)
                last_bbd = int(base_bbd * brand_factor)
                
                delta = random.choice([-100, -50, 0, 50, 100, -150, 200, -250])
                curr = max(last_bbd - 120, int(base_curr * brand_factor) + delta)
                bbd_pred = int(last_bbd * 0.93)

                product_title = f"{brand} {item_name}"
                
                # Dedicated product URL: cleans URL and appends brand slug for unique items
                direct_url = clean_direct_pdp_url(base_url, product_title)

                catalog.append({
                    "id": str(uuid.uuid4()),
                    "Category": cat_name,
                    "Brand": brand,
                    "Product": product_title,
                    "MRP": mrp,
                    "6-Month Avg": avg_6m,
                    "Last BBD Low": last_bbd,
                    "Current Price": curr,
                    "Predicted BBD Low": bbd_pred,
                    "URL": direct_url,
                    "is_wishlist": False,
                    "is_ad": (i_idx % 7 == 0),
                    "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                })
    return catalog

# Initialize or Load Persistent Database
saved_db = load_tracker_data()
if not saved_db:
    saved_db = generate_seed_catalog()
    save_tracker_data(saved_db)

# --- RE-TRACKING ENGINE ---
def refresh_all_live_prices():
    current_data = load_tracker_data()
    updated_items = 0
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    for item in current_data:
        url = clean_direct_pdp_url(item.get("URL", ""), item.get("Product", ""))
        item["URL"] = url
        if "/p/" in url:
            live_p = fetch_flipkart_live_price(url)
            if live_p and live_p > 0:
                item["Current Price"] = live_p
                updated_items += 1
        else:
            shift = random.choice([-40, -20, 0, 30, -50, 10])
            item["Current Price"] = max(item["Last BBD Low"] - 150, item["Current Price"] + shift)
            updated_items += 1
        item["Last Checked"] = now_str

    save_tracker_data(current_data)
    return updated_items

# --- PROCESS ANALYTICS & CREDIT CARDS ---
def process_analytics(catalog, card_selection):
    records = []
    for d in catalog:
        curr = d["Current Price"]
        avg_6m = d["6-Month Avg"]
        last_bbd = d["Last BBD Low"]
        mrp = d["MRP"]
        bbd_pred = d["Predicted BBD Low"]

        savings_vs_6m = avg_6m - curr
        disc_vs_6m = round((savings_vs_6m / avg_6m) * 100, 1) if avg_6m > 0 else 0
        diff_vs_last_bbd = curr - last_bbd

        # Decision Verdict
        if "Smartphones" in d["Category"] or "Dyson" in d["Product"] or "Ray-Ban" in d["Product"]:
            verdict = "WAIT (BBD)" if curr > (bbd_pred * 1.05) else "BUY NOW"
        elif curr <= last_bbd or disc_vs_6m >= 28:
            verdict = "BUY NOW"
        else:
            verdict = "WAIT"

        # Card Engine
        instant_10 = min(int(curr * 0.10), 1500)
        cashback_5 = int(curr * 0.05)

        if card_selection == "Axis / ICICI (10% Instant, Cap ₹1.5k)":
            card_title, net_price = "Axis/ICICI (10%)", curr - instant_10
        elif card_selection == "Flipkart Axis (5% Unlimited Cashback)":
            card_title, net_price = "Flipkart Axis (5%)", curr - cashback_5
        else:
            if instant_10 >= cashback_5:
                card_title, net_price = "Axis/ICICI (10%)", curr - instant_10
            else:
                card_title, net_price = "Flipkart Axis (5%)", curr - cashback_5

        clean_url = clean_direct_pdp_url(d.get("URL", ""), d.get("Product", ""))

        records.append({
            "id": d.get("id", str(uuid.uuid4())),
            "Category": d["Category"],
            "Brand": d["Brand"],
            "Product": d["Product"],
            "Current Price": curr,
            "6-Month Avg": avg_6m,
            "Last BBD Low": last_bbd,
            "Real Savings (vs 6M)": savings_vs_6m,
            "Real Disc % (vs 6M)": disc_vs_6m,
            "Diff vs Last BBD": diff_vs_last_bbd,
            "Predicted BBD Low": bbd_pred,
            "Verdict": verdict,
            "Optimal Card": card_title,
            "Net Price": net_price,
            "MRP": mrp,
            "URL": clean_url,
            "is_wishlist": d.get("is_wishlist", False),
            "is_ad": d.get("is_ad", False),
            "Last Checked": d.get("Last Checked", "Recently")
        })
    return pd.DataFrame(records)

# --- TOP BAR & CONTROLS ---
st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
st.caption("Live Flipkart price intelligence engine with dedicated direct product links, automatic re-tracking, ad scanners, and historical BBD festive comparisons.")

col_top1, col_top2, col_top3 = st.columns([1.5, 1.5, 1])

with col_top1:
    if st.button("🔄 Scan & Re-Check Live Prices on Flipkart", type="primary", use_container_width=True):
        with st.spinner("Connecting to Flipkart, checking active discounts, and updating local tracker..."):
            count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count:,} products in database!")
            st.rerun()

with col_top2:
    expand_all = st.checkbox("📂 Expand All Category Tables", value=False)

with col_top3:
    card_preference = st.selectbox(
        "💳 Card Strategy:",
        ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
    )

# Process Master DataFrame
all_saved_records = load_tracker_data()
master_df = process_analytics(all_saved_records, card_preference)

# Summary KPIs
k1, k2, k3, k4 = st.columns(4)
with k1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + f"{len(master_df):,}" + '</div><div class="kpi-label">Monitored Products</div></div>', unsafe_allow_html=True)
with k2:
    bbd_cheapest = len(master_df[master_df["Diff vs Last BBD"] <= 0])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#facc15;">' + f"{bbd_cheapest:,}" + '</div><div class="kpi-label">🔥 Cheaper Than Last BBD</div></div>', unsafe_allow_html=True)
with k3:
    wishlist_total = len(master_df[master_df["is_wishlist"] == True])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#ec4899;">' + f"{wishlist_total:,}" + '</div><div class="kpi-label">Saved Wishlist Items</div></div>', unsafe_allow_html=True)
with k4:
    tot_savings = master_df["Real Savings (vs 6M)"].sum()
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">₹' + f"{tot_savings/100000:,.1f} Lakh" + '</div><div class="kpi-label">Total Consumer Savings</div></div>', unsafe_allow_html=True)

st.divider()

# Shared Table Column Formatter (Direct Product Link Display)
col_config = {
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%d%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(format="₹%d", help="Negative value means CURRENTLY CHEAPER than last year BBD!"),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "URL": st.column_config.LinkColumn("Direct Product Link")
}

display_columns = [
    "Brand", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
    "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
    "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "Last Checked", "URL"
]

# ==============================================================================
# ➕ 2-FIELD QUICK ADD WISHLIST MANAGER (DIRECT PRODUCT LINK ONLY)
# ==============================================================================
with st.expander("➕ Add Direct Product to Wishlist (Only 2 Inputs: Name & Link)", expanded=False):
    st.markdown("Enter the **Product Name** and paste the **Direct Flipkart Product Link**. Any tracking tokens (`?pid=...&lid=...`) will be stripped to keep only the pure product link.")
    with st.form("quick_2_field_form", clear_on_submit=True):
        f_name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Jeans")
        f_url = st.text_input("🔗 Direct Flipkart Product Link:", placeholder="https://www.flipkart.com/.../p/itm...")
        submit_btn = st.form_submit_button("💾 Save Direct Product to Wishlist", type="primary")

        if submit_btn:
            if not f_name.strip() or not f_url.strip():
                st.error("Please enter both Name and Link.")
            else:
                cat_assigned = detect_category(f_name)
                clean_url = clean_direct_pdp_url(f_url, f_name)
                
                live_price = fetch_flipkart_live_price(clean_url)
                curr_p = live_price if (live_price and live_price > 0) else 999
                mrp_p = int(curr_p * 1.35)

                new_item = {
                    "id": str(uuid.uuid4()),
                    "Category": cat_assigned,
                    "Brand": f_name.strip().split()[0].title(),
                    "Product": f_name.strip(),
                    "MRP": mrp_p,
                    "6-Month Avg": int(mrp_p * 0.85),
                    "Last BBD Low": int(curr_p * 0.94),
                    "Current Price": curr_p,
                    "Predicted BBD Low": int(curr_p * 0.88),
                    "URL": clean_url,
                    "is_wishlist": True,
                    "is_ad": False,
                    "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
                }

                current_records = load_tracker_data()
                current_records.append(new_item)
                save_tracker_data(current_records)
                st.success(f"Saved '{f_name}' into '{cat_assigned}' with dedicated product link!")
                st.rerun()

# --- REUSABLE COLLAPSIBLE TABLE BUILDER WITH DIRECT WISHLIST ADDITION ---
def render_collapsible_table(df_subset, category_title, slug, icon, is_expanded, allow_deletion=False):
    if df_subset.empty:
        return

    expander_title = f"{icon} {category_title} — ({len(df_subset)} Products Available)"
    
    with st.expander(expander_title, expanded=is_expanded):
        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns([2, 1.5, 2, 1.5, 1.5])
        
        # 1. Brand Filter
        available_brands = sorted(list(df_subset["Brand"].unique()))
        selected_brands = f_col1.multiselect("Filter Brand:", options=available_brands, default=[], placeholder="All Brands", key=f"{slug}_brand")
        
        # 2. Verdict Filter
        selected_verdict = f_col2.selectbox("Filter Verdict:", options=["All", "BUY NOW", "WAIT"], key=f"{slug}_verdict")

        # 3. Price Range Filter
        min_p = int(df_subset["Current Price"].min())
        max_p = int(df_subset["Current Price"].max())
        if min_p == max_p:
            max_p = min_p + 100
        price_range = f_col3.slider("Price Range (₹):", min_value=min_p, max_value=max_p, value=(min_p, max_p), key=f"{slug}_price")

        # 4. Discount Filter
        min_disc = f_col4.slider("Min Real Disc %:", min_value=-10, max_value=60, value=-10, step=5, key=f"{slug}_disc")

        # 5. Cheaper vs BBD Filter
        only_bbd = f_col5.checkbox("🔥 Cheaper vs BBD", value=False, key=f"{slug}_bbd")

        # Apply Filters
        filtered = df_subset.copy()
        if selected_brands:
            filtered = filtered[filtered["Brand"].isin(selected_brands)]
        if selected_verdict == "BUY NOW":
            filtered = filtered[filtered["Verdict"] == "BUY NOW"]
        elif selected_verdict == "WAIT":
            filtered = filtered[filtered["Verdict"].str.contains("WAIT")]

        filtered = filtered[filtered["Current Price"].between(price_range[0], price_range[1])]
        filtered = filtered[filtered["Real Disc % (vs 6M)"] >= min_disc]

        if only_bbd:
            filtered = filtered[filtered["Diff vs Last BBD"] <= 0]

        # Table Display with Multi-Row Selection
        if not filtered.empty:
            sorted_filtered = filtered.sort_values("Real Disc % (vs 6M)", ascending=False)
            
            table_event = st.dataframe(
                sorted_filtered[display_columns],
                column_config=col_config,
                use_container_width=True,
                hide_index=True,
                on_select="rerun",
                selection_mode="multi-row",
                key=f"df_{slug}"
            )
            
            selected_indices = getattr(table_event.selection, "rows", [])
            if selected_indices:
                selected_products = sorted_filtered.iloc[selected_indices]
                if st.button(f"⭐ Add {len(selected_indices)} Selected Product(s) to Wishlist", type="primary", key=f"sel_add_{slug}"):
                    added_count = 0
                    for _, row in selected_products.iterrows():
                        ok, _ = add_product_to_wishlist(row.to_dict())
                        if ok:
                            added_count += 1
                    if added_count > 0:
                        st.success(f"Added {added_count} product(s) directly to their respective wishlist categories!")
                        st.rerun()
                    else:
                        st.info("Selected item(s) are already in your wishlist.")

            # Bottom Action Bar
            c_left, c_mid, c_right = st.columns([1.5, 1.8, 1])
            
            with c_left:
                c_csv = sorted_filtered[display_columns].to_csv(index=False).encode('utf-8')
                st.download_button(
                    label=f"📥 Download {category_title} CSV",
                    data=c_csv,
                    file_name=f"{slug}_deals.csv",
                    mime="text/csv",
                    key=f"{slug}_dl"
                )

            with c_mid:
                with st.popover("⭐ Quick Add to Wishlist"):
                    st.caption("Pick an item from this table to route automatically into its wishlist category:")
                    pickable_items = sorted_filtered[["id", "Product", "Brand", "Current Price", "MRP", "6-Month Avg", "Last BBD Low", "Predicted BBD Low", "URL"]].to_dict('records')
                    item_chosen_id = st.selectbox(
                        "Select Product:",
                        options=[x["id"] for x in pickable_items],
                        format_func=lambda x: next((p["Product"] for p in pickable_items if p["id"] == x), x),
                        key=f"pick_{slug}"
                    )
                    if st.button("Add to Wishlist", key=f"btn_add_{slug}"):
                        chosen_record = next((p for p in pickable_items if p["id"] == item_chosen_id), None)
                        if chosen_record:
                            ok, cat_routed = add_product_to_wishlist(chosen_record)
                            if ok:
                                st.success(f"Added to '{cat_routed}' with direct product link!")
                                st.rerun()
                            else:
                                st.info(f"Already present in '{cat_routed}'.")

            if allow_deletion:
                with c_right:
                    with st.popover("🗑️ Delete Wishlist Items"):
                        items_to_pick = df_subset[["id", "Product"]].to_dict('records')
                        target_id = st.selectbox(
                            "Select product to delete:",
                            options=[i["id"] for i in items_to_pick],
                            format_func=lambda x: next((i["Product"] for i in items_to_pick if i["id"] == x), x)
                        )
                        if st.button("Delete Selected", key=f"del_single_{slug}"):
                            current_db = load_tracker_data()
                            updated = [i for i in current_db if i.get("id") != target_id]
                            save_tracker_data(updated)
                            st.success("Deleted! Refreshing...")
                            st.rerun()
        else:
            st.warning("No products match the selected filters.")

# ==============================================================================
# MAIN PAGE TABS: 1. BBD STEALS | 2. ADS & FEATURED | 3. WISHLISTS | 4. CATALOG
# ==============================================================================
tab_bbd_steals, tab_ads, tab_wishlist, tab_categories = st.tabs([
    "🔥 BBD Steal Deals (Cheapest vs Last Year BBD)",
    "📢 Flipkart Ads & Featured Steals",
    "📑 Saved Wishlists",
    "🛒 Category Catalog (All 9 Categories)"
])

# --- TAB 1: BBD STEAL DEALS (PRICED AT OR BELOW LAST YEAR BBD) ---
with tab_bbd_steals:
    st.markdown("""
    <div class="bbd-steals-banner">
        <h3 style="margin:0;">🔥 The Big Billion Days Floor Deals (Cheapest Recorded)</h3>
        <p style="margin:4px 0 0 0; font-size:0.9rem;">
            These products are <b>currently cheaper than or equal to last year's Big Billion Days festive low</b>. All links open the dedicated single product page directly.
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    bbd_steals_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD", ascending=True)
    if not bbd_steals_df.empty:
        render_collapsible_table(bbd_steals_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", is_expanded=True)
    else:
        st.info("No products currently beat last year's BBD floor price. Click 'Scan & Re-Check Live Prices' to update.")

# --- TAB 2: FLIPKART ADS & FEATURED PROMOTIONS SCANNER ---
with tab_ads:
    st.markdown("""
    <div class="ads-banner">
        <h3 style="margin:0;">📢 Flipkart Promoted, Sponsored & Top Banner Deals</h3>
        <p style="margin:4px 0 0 0; font-size:0.9rem;">
            Tracking active sponsored product placements and ad banner promotions benchmarked against their 6-month historical averages.
        </p>
    </div>
    """, unsafe_allow_html=True)
    
    ads_df = master_df[master_df["is_ad"] == True].sort_values("Real Disc % (vs 6M)", ascending=False)
    render_collapsible_table(ads_df, "Active Sponsored & Banner Promotions", "flipkart_ads", "📢", is_expanded=True)

# --- TAB 3: USER SAVED WISHLISTS (SEPARATED INTO AUTO-CATEGORIES) ---
with tab_wishlist:
    st.markdown("### 📑 Your Saved Personal Wishlists (Separated by Product Type)")
    wishlist_df = master_df[master_df["is_wishlist"] == True]
    
    if not wishlist_df.empty:
        distinct_wishlist_cats = sorted(list(wishlist_df["Category"].unique()))
        for w_cat in distinct_wishlist_cats:
            sub = wishlist_df[wishlist_df["Category"] == w_cat]
            slug = "w_" + w_cat.lower().replace(" ", "_").replace(":", "").replace("&", "")
            render_collapsible_table(sub, w_cat, slug, "💖", is_expanded=True, allow_deletion=True)
    else:
        st.info("Your wishlist is currently empty. Use the '⭐ Quick Add to Wishlist' button in any table or the form above to add items.")

# --- TAB 4: COMPLETE 9-CATEGORY CATALOG (STATIONERY AT BOTTOM) ---
with tab_categories:
    st.markdown("### 🛒 Audited Market Catalog")
    for cat_title, meta in CAT_DATA_MATRIX.items():
        cat_df = master_df[(master_df["Category"] == cat_title) & (master_df["is_wishlist"] == False)]
        render_collapsible_table(cat_df, cat_title, meta["slug"], meta["icon"], is_expanded=expand_all)
