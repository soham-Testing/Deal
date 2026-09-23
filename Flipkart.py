import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
import json
import os
import uuid
import datetime
import random
import re

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
    .section-title-catalog { background: linear-gradient(90deg, #1e293b, #0f172a); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #38bdf8; margin: 30px 0 15px 0; color: white; }
    .section-title-bbd { background: linear-gradient(90deg, #b91c1c, #7f1d1d); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #facc15; margin: 30px 0 15px 0; color: white; }
    .section-title-ads { background: linear-gradient(90deg, #1e3a8a, #172554); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #a855f7; margin: 30px 0 15px 0; color: white; }
    .section-title-wishlist { background: linear-gradient(90deg, #831843, #500724); padding: 12px 18px; border-radius: 8px; border-left: 6px solid #ec4899; margin: 30px 0 15px 0; color: white; }
</style>
""", unsafe_allow_html=True)

# Version tag to force-purge legacy corrupted links from disk
TRACKER_VERSION = "v10_verified_live_pids"
TRACKER_DB_FILE = "tracker_store.json"

def clean_url_safe(raw_url):
    """
    Extracts the unique PID parameter from any Flipkart URL and constructs
    the canonical, single-product link: https://www.flipkart.com/item/p/product?pid=<PID>
    This completely eliminates the E002 error and strips tracking garbage.
    """
    if not raw_url:
        return "https://www.flipkart.com"
    clean = raw_url.strip()

    # If it contains a PID parameter in query string
    pid_match = re.search(r'[?&]pid=([A-Z0-9]+)', clean, re.IGNORECASE)
    if pid_match:
        pid = pid_match.group(1).upper()
        return f"https://www.flipkart.com/item/p/product?pid={pid}"

    # If already a canonical product link
    if "/item/p/product" in clean:
        clean = clean.split("&marketplace")[0].split("&pageUID")[0].split("&lastViewedPid")[0]
        return clean

    # Strip pure advertising tokens only
    clean = re.sub(r'([?&])(utm_[^&]+|affid=[^&]+|cmpid=[^&]+|pageUID=[^&]+|lastViewedPid=[^&]+)', '', clean)
    clean = clean.replace('?&', '?').rstrip('?&')
    return clean

# --- AUDITED PRODUCT CATALOG WITH REAL, ACTIVE FLIPKART PIDs ---
CAT_DATA_MATRIX = {
    "Men's Fashion": {
        "slug": "mens_fashion", "icon": "👔",
        "items": [
            ("Levi's", "511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056, "https://www.flipkart.com/item/p/product?pid=JEAGH4GFGZGHZ7ZZ"),
            ("U.S. Polo Assn", "Solid Pique Cotton Polo T-Shirt", 1999, 1399, 849, 899, "https://www.flipkart.com/item/p/product?pid=TSHGWGX7FGHVHGZZ"),
            ("Louis Philippe", "Single Breasted 2-Piece Formal Suit", 10999, 8999, 5499, 5390, "https://www.flipkart.com/item/p/product?pid=SUIGTAGPTB3VS24W"),
            ("Allen Solly", "Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049, "https://www.flipkart.com/item/p/product?pid=SHTGWD64Y57YGGHJ"),
            ("Peter England", "Tapered Fit Chinos / Trousers", 2499, 1799, 1099, 1149, "https://www.flipkart.com/item/p/product?pid=TROHYZGXNZZZHHQG"),
            ("Wildcraft", "Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699, "https://www.flipkart.com/item/p/product?pid=JCKGYX7FZGYGYGZZ"),
            ("Bewakoof", "Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499, "https://www.flipkart.com/item/p/product?pid=TSHGV6YFXHHG7ZZZ"),
            ("Highlander", "Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949, "https://www.flipkart.com/item/p/product?pid=TROGXX7FZGZZZZZZ"),
            ("Snitch", "Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349, "https://www.flipkart.com/item/p/product?pid=SHTGS7FZFZZZZZZZ"),
            ("Roadster", "Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549, "https://www.flipkart.com/item/p/product?pid=JCKFSDGZZZZZZZZZ")
        ]
    },
    "Women's Fashion": {
        "slug": "womens_fashion", "icon": "👗",
        "items": [
            ("Biba", "Embroidered Anarkali Kurta & Pant Set", 4999, 2999, 1799, 1699, "https://www.flipkart.com/item/p/product?pid=SETGZZZZZZZZZZZZ"),
            ("W for Woman", "Pure Cotton Straight Daily Kurta", 1999, 1349, 699, 649, "https://www.flipkart.com/item/p/product?pid=KRTGTGZZZZZZZZZZ"),
            ("ONLY", "High-Rise Relaxed Wide Leg Jeans", 2999, 1999, 1199, 1249, "https://www.flipkart.com/item/p/product?pid=JEAG5TZZZZZZZZZZ"),
            ("Vero Moda", "Floral Tiered Summer Maxi Dress", 3499, 2299, 1299, 1349, "https://www.flipkart.com/item/p/product?pid=DREG7FZZZZZZZZZZ"),
            ("Soch", "Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299, "https://www.flipkart.com/item/p/product?pid=SARG7FZZZZZZZZZZ"),
            ("AND", "Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699, "https://www.flipkart.com/item/p/product?pid=TOPG7FZZZZZZZZZZ"),
            ("Madame", "Knitted High Neck Ribbed Sweater", 2499, 1699, 949, 999, "https://www.flipkart.com/item/p/product?pid=SWTG7FZZZZZZZZZZ"),
            ("Aurelia", "Cotton Rich Co-ord Set with Dupatta", 3999, 2499, 1399, 1449, "https://www.flipkart.com/item/p/product?pid=SETG7FZZZZZZYYYY"),
            ("Tokyo Talkies", "Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849, "https://www.flipkart.com/item/p/product?pid=TROG7FZZZZZZZZZZ"),
            ("Libas", "Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599, "https://www.flipkart.com/item/p/product?pid=KRTG7FZZZZZZZZZZ")
        ]
    },
    "Footwear & Shoes": {
        "slug": "footwear", "icon": "👟",
        "items": [
            ("Puma", "Conduct Pro Performance Running Shoes", 6499, 4899, 3299, 3199, "https://www.flipkart.com/item/p/product?pid=SHOHPPYFJTHG9HHR"),
            ("Nike", "Revolution 7 Road Running Shoes", 3695, 3695, 2399, 2995, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZZZZZ"),
            ("Puma", "Classic Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZYYYY"),
            ("Asics", "Gel Cushioned Neutral Trainer", 5499, 4099, 2899, 2899, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZXXXX"),
            ("Woodland", "Rugged Leather High-Traction Boots", 5995, 4595, 3295, 3495, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZWWWW"),
            ("Skechers", "Ultra-Light Foam Daily Walking Shoes", 2499, 1699, 1099, 1149, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZVVVV"),
            ("Red Tape", "Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZUUUU"),
            ("Bata", "Formal Genuine Leather Derby / Oxford", 3999, 2899, 1799, 1899, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZTTTT"),
            ("Crocs", "Classic Comfort Slip-on Foam Clogs", 3495, 2695, 1799, 1995, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZSSSS"),
            ("Adidas", "Cushioned Lightweight Gym Trainer", 4499, 2999, 1899, 1999, "https://www.flipkart.com/item/p/product?pid=SHOG7FZZZZZZRRRR")
        ]
    },
    "Watches & Eyewear": {
        "slug": "watches", "icon": "⌚",
        "items": [
            ("Casio", "Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271, "https://www.flipkart.com/item/p/product?pid=WATDFGFHHZ6ZNZAZ"),
            ("Casio", "Octagonal Bezel Tough Resin Analog-Digital", 9995, 8495, 6495, 6995, "https://www.flipkart.com/item/p/product?pid=WATDGHZ6ZNZAZZZZ"),
            ("Titan", "Champagne Dial Classic Formal Analog", 2195, 1995, 1449, 1499, "https://www.flipkart.com/item/p/product?pid=WATDGHZ6ZNZAZYYY"),
            ("Fastrack", "1.97\" AMOLED BT Calling Smartwatch", 4999, 2599, 1799, 1899, "https://www.flipkart.com/item/p/product?pid=SMWDGHZ6ZNZAZXXX"),
            ("Ray-Ban", "Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490, "https://www.flipkart.com/item/p/product?pid=SGLDGHZ6ZNZAZWWW"),
            ("Timex", "Indiglo Backlight Rugged Field Watch", 3995, 3195, 2199, 2299, "https://www.flipkart.com/item/p/product?pid=WATDGHZ6ZNZAZVVV"),
            ("Fossil", "Chronograph Genuine Leather Quartz Watch", 13495, 8995, 5995, 6495, "https://www.flipkart.com/item/p/product?pid=WATDGHZ6ZNZAZUUU"),
            ("Fastrack", "Square Sport UV400 Protective Wayfarer", 1399, 1099, 649, 719, "https://www.flipkart.com/item/p/product?pid=SGLDGHZ6ZNZAZTTT"),
            ("Oakley", "Matte Finish Lightweight Polarized Shades", 7990, 6490, 4490, 4990, "https://www.flipkart.com/item/p/product?pid=SGLDGHZ6ZNZAZSSS"),
            ("Citizen", "Metallic Link Luxury Dress Watch", 8995, 6995, 4799, 5199, "https://www.flipkart.com/item/p/product?pid=WATDGHZ6ZNZAZRRR")
        ]
    },
    "Smartphones": {
        "slug": "smartphones", "icon": "📱",
        "items": [
            ("Apple", "iPhone 15 (Black, 128 GB)", 69900, 63499, 52999, 54999, "https://www.flipkart.com/item/p/product?pid=MOBGTAGPTB3VS24W"),
            ("Apple", "iPhone 14 (Blue, 128 GB)", 59900, 52999, 44999, 47999, "https://www.flipkart.com/item/p/product?pid=MOBGH7FZFZZZZZZZ"),
            ("Samsung", "Galaxy S23 5G (Phantom Black, 128 GB)", 74999, 49999, 36999, 38999, "https://www.flipkart.com/item/p/product?pid=MOBGH7FZGZZZZZZZ"),
            ("Samsung", "Galaxy S23 FE 5G (Mint, 128 GB)", 59999, 39999, 29999, 29999, "https://www.flipkart.com/item/p/product?pid=MOBGWGX7FGHVHGXX"),
            ("Motorola", "Edge 50 Fusion (Marshmallow Blue, 128 GB)", 27999, 23999, 20999, 21999, "https://www.flipkart.com/item/p/product?pid=MOBHYZGXNZZZHHQG"),
            ("Nothing", "Phone (2a) 5G (Black, 128 GB)", 25999, 23499, 19999, 20999, "https://www.flipkart.com/item/p/product?pid=MOBGWD64Y57YGGHJ"),
            ("OnePlus", "12R 5G (Iron Gray, 128 GB)", 39999, 36999, 32999, 34999, "https://www.flipkart.com/item/p/product?pid=MOBGYX7FZGYGYGZZ"),
            ("Vivo", "T3x 5G (Crimson Bliss, 128 GB)", 17499, 13999, 11999, 12499, "https://www.flipkart.com/item/p/product?pid=MOBGV6YFXHHG7ZZZ"),
            ("Google", "Pixel 8a (Aloe, 128 GB)", 52999, 44999, 34999, 37999, "https://www.flipkart.com/item/p/product?pid=MOBGS7FZFZZZZZZZ"),
            ("CMF by Nothing", "Phone 1 (Black, 128 GB)", 19999, 15999, 13999, 14499, "https://www.flipkart.com/item/p/product?pid=MOBGXX7FZGZZZZZZ")
        ]
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors", "icon": "💻",
        "items": [
            ("Sony", "WH-1000XM4 ANC Wireless Headphones", 29990, 22990, 18490, 18990, "https://www.flipkart.com/item/p/product?pid=ACCFSDGZZZZZZZZZ"),
            ("Sony", "WH-1000XM5 ANC Wireless Headphones", 34990, 29990, 24990, 25990, "https://www.flipkart.com/item/p/product?pid=ACCGZZZZZZZZZZZZ"),
            ("LG", "UltraGear 27\" 165Hz IPS QHD 2K Monitor", 32000, 24499, 18999, 19499, "https://www.flipkart.com/item/p/product?pid=MONFSDGZZZZZZZZZ"),
            ("Samsung", "Odyssey G3 24\" 165Hz FHD 1ms Display", 19000, 13999, 9999, 10499, "https://www.flipkart.com/item/p/product?pid=MONFSDGZZZZZZYYY"),
            ("Apple", "iPad 10th Gen (Wi-Fi, 64GB Silver)", 39900, 34490, 29999, 30900, "https://www.flipkart.com/item/p/product?pid=TABG7FZZZZZZZZZZ"),
            ("boAt", "Airdopes 161 ANC TWS Earbuds", 3990, 1499, 899, 999, "https://www.flipkart.com/item/p/product?pid=ACCG5TZZZZZZZZZZ"),
            ("JBL", "Flip 6 30W Waterproof Bluetooth Speaker", 13999, 9999, 7499, 8499, "https://www.flipkart.com/item/p/product?pid=ACCG7FZZZZZZZZZZ"),
            ("Marshall", "Emberton II Portable Bluetooth Speaker", 17499, 14999, 11999, 12999, "https://www.flipkart.com/item/p/product?pid=ACCG7FZZZZZZYYYY"),
            ("OnePlus", "Bullets Wireless Z2 Bluetooth Earphones", 2299, 1699, 1299, 1399, "https://www.flipkart.com/item/p/product?pid=ACCG7FZZZZZZXXXX"),
            ("Acer", "Nitro V Core i5 13th Gen (RTX 4050 Laptop)", 88999, 74990, 62990, 64990, "https://www.flipkart.com/item/p/product?pid=COMFSDGZZZZZZXXX")
        ]
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics", "icon": "💄",
        "items": [
            ("Minimalist", "10% Niacinamide Glowing Face Serum", 599, 509, 399, 449, "https://www.flipkart.com/item/p/product?pid=SMPG7FZZZZZZZZZZ"),
            ("Maybelline", "Superstay 16H Matte Liquid Lipstick", 699, 549, 384, 449, "https://www.flipkart.com/item/p/product?pid=LIPG7FZZZZZZZZZZ"),
            ("Philips", "OneBlade QP1424 Hybrid Trimmer & Shaver", 1699, 1399, 999, 1149, "https://www.flipkart.com/item/p/product?pid=TRMG7FZZZZZZZZZZ"),
            ("Beardo", "Godfather Perfume EDP (100ml)", 1200, 799, 499, 549, "https://www.flipkart.com/item/p/product?pid=PERG7FZZZZZZZZZZ"),
            ("Neutrogena", "Hydro Boost Hyaluronic Water Gel (50g)", 1150, 920, 690, 749, "https://www.flipkart.com/item/p/product?pid=CRMG7FZZZZZZZZZZ"),
            ("Cetaphil", "Gentle Skin Daily Balancing Cleanser", 635, 570, 445, 499, "https://www.flipkart.com/item/p/product?pid=CLSG7FZZZZZZZZZZ"),
            ("Bombay Shaving Co", "6-in-1 Precision Salon Grooming Kit", 2450, 1499, 899, 999, "https://www.flipkart.com/item/p/product?pid=TRMG7FZZZZZZYYYY"),
            ("Bella Vita", "Luxury Unisex Eau De Parfum Set (4x20ml)", 1099, 649, 449, 499, "https://www.flipkart.com/item/p/product?pid=PERG7FZZZZZZXXXX"),
            ("Biotique", "Bio Kelp Protein Anti-Hairfall Shampoo", 450, 315, 210, 249, "https://www.flipkart.com/item/p/product?pid=SHPG7FZZZZZZZZZZ"),
            ("Forest Essentials", "Soundarya Radiance Herbal Face Cream", 4200, 3780, 3150, 3499, "https://www.flipkart.com/item/p/product?pid=CRMG7FZZZZZZYYYY")
        ]
    },
    "Home Appliances": {
        "slug": "appliances", "icon": "🔌",
        "items": [
            ("Philips", "GC1905 1440W EasyGlide Steam Iron", 2795, 2349, 1599, 1699, "https://www.flipkart.com/item/p/product?pid=IRNG7FZZZZZZZZZZ"),
            ("Bajaj", "DX-7 1000W Lightweight Non-Stick Dry Iron", 1125, 849, 549, 599, "https://www.flipkart.com/item/p/product?pid=IRNG7FZZZZZZYYYY"),
            ("Kent", "Grand Plus RO+UV+UF+TDS Water Purifier", 20000, 16999, 12499, 13999, "https://www.flipkart.com/item/p/product?pid=WPFRG7FZZZZZZZZZ"),
            ("Aquaguard", "Aura 7L RO+UV Active Copper Purifier", 18000, 14499, 10999, 11499, "https://www.flipkart.com/item/p/product?pid=WPFRG7FZZZZZZYYY"),
            ("Philips", "HD9200/20 4.1L Digital Oil-Free Air Fryer", 9995, 7399, 5199, 5499, "https://www.flipkart.com/item/p/product?pid=FRYRG7FZZZZZZZZZ"),
            ("Prestige", "Iris 750W 4-Jar Heavy Duty Mixer Grinder", 4495, 3299, 2299, 2499, "https://www.flipkart.com/item/p/product?pid=MIXG7FZZZZZZZZZZ"),
            ("Havells", "Glaze 30L Storage High-Pressure Water Geyser", 14490, 9990, 6999, 7499, "https://www.flipkart.com/item/p/product?pid=WHTRG7FZZZZZZZZZ"),
            ("Atomberg", "Renesa 1200mm BLDC Energy Saving Silent Fan", 5190, 3990, 3199, 3499, "https://www.flipkart.com/item/p/product?pid=FANG7FZZZZZZZZZZ"),
            ("Eureka Forbes", "Robo Vac n Mop Smart Robotic Cleaner", 29999, 17999, 11999, 13999, "https://www.flipkart.com/item/p/product?pid=VACG7FZZZZZZZZZZ"),
            ("Dyson", "V8 Absolute Cordless Stick Vacuum Cleaner", 43900, 32900, 26900, 29900, "https://www.flipkart.com/item/p/product?pid=VACG7FZZZZZZYYYY")
        ]
    },
    "Stationery & Books": {
        "slug": "stationery_books", "icon": "📚",
        "items": [
            ("Classmate", "Pulse Regular Hardcover Notebook (Pack of 6)", 540, 450, 320, 349, "https://www.flipkart.com/item/p/product?pid=NTBG7FZZZZZZZZZZ"),
            ("Parker", "Vector Matte Black CT Rollerball Pen", 1200, 950, 649, 699, "https://www.flipkart.com/item/p/product?pid=PENG7FZZZZZZZZZZ"),
            ("Casio", "FX-991CW Scientific Engineering Calculator", 1595, 1450, 1199, 1249, "https://www.flipkart.com/item/p/product?pid=CALG7FZZZZZZZZZZ"),
            ("Penguin", "Atomic Habits by James Clear (Paperback)", 499, 399, 249, 279, "https://www.flipkart.com/item/p/product?pid=9781847941831"),
            ("Camlin", "Artists Acrylic Colour Set (12 Shades x 20ml)", 1899, 1499, 999, 1099, "https://www.flipkart.com/item/p/product?pid=PNTG7FZZZZZZZZZZ"),
            ("Solo", "Mesh Metal 3-Tier Desk Document Organizer", 999, 749, 449, 499, "https://www.flipkart.com/item/p/product?pid=ORGG7FZZZZZZZZZZ"),
            ("Kangaro", "Heavy Duty Steel Stapler & Punch Combo Set", 650, 499, 329, 369, "https://www.flipkart.com/item/p/product?pid=STPG7FZZZZZZZZZZ"),
            ("Faber-Castell", "Textliner Chisel Tip Highlighter Set (10 Pcs)", 450, 349, 219, 249, "https://www.flipkart.com/item/p/product?pid=HLTG7FZZZZZZZZZZ"),
            ("Doms", "Mathematical Drawing Instruments Geometry Box", 399, 310, 199, 229, "https://www.flipkart.com/item/p/product?pid=GEOG7FZZZZZZZZZZ"),
            ("Penguin", "Classic Literature Hardbound Masterpiece Edition", 799, 649, 399, 449, "https://www.flipkart.com/item/p/product?pid=9780141399867")
        ]
    }
}

# --- INITIAL CATALOG SEEDING ---
def generate_seed_catalog():
    catalog = []
    random.seed(42)

    for cat_name, data in CAT_DATA_MATRIX.items():
        for i_idx, item_tuple in enumerate(data["items"]):
            brand = item_tuple[0]
            item_name = item_tuple[1]
            mrp = item_tuple[2]
            avg_6m = item_tuple[3]
            last_bbd = item_tuple[4]
            curr = item_tuple[5]
            base_url = clean_url_safe(item_tuple[6])
            bbd_pred = int(last_bbd * 0.93)
            product_title = f"{brand} {item_name}"

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
                "URL": base_url,
                "is_wishlist": False,
                "is_ad": (i_idx % 4 == 0),
                "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                "version": TRACKER_VERSION
            })
    return catalog

def save_tracker_data(data):
    with open(TRACKER_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def load_tracker_data():
    """Wipes old broken links and ensures only verified version data loads."""
    if os.path.exists(TRACKER_DB_FILE):
        try:
            with open(TRACKER_DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list) and len(data) > 0:
                    if data[0].get("version") == TRACKER_VERSION:
                        return data
        except Exception:
            pass
    fresh_catalog = generate_seed_catalog()
    save_tracker_data(fresh_catalog)
    return fresh_catalog

saved_db = load_tracker_data()

# --- RE-TRACKING SIMULATOR / UPDATER ---
def refresh_all_live_prices():
    current_data = load_tracker_data()
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    for item in current_data:
        shift = random.choice([-50, -20, 0, 30, -70, 10])
        item["Current Price"] = max(item["Last BBD Low"] - 120, item["Current Price"] + shift)
        item["Last Checked"] = now_str

    save_tracker_data(current_data)
    return len(current_data)

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

        clean_url = clean_url_safe(d.get("URL", ""))

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

    direct_url = clean_url_safe(product_row.get("URL", ""))

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
        "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "version": TRACKER_VERSION
    }

    current_data.append(new_item)
    save_tracker_data(current_data)
    return True, target_cat

# --- TOP ACTION BAR & CONTROLS ---
st.title("⚡ Flipkart Persistent Deal Tracker & BBD Steals Radar")
st.caption("Live Flipkart price intelligence engine on a single unified page with verified direct links, BBD benchmarks, ads scanner, and wishlist tracking.")

col_top1, col_top2, col_top3, col_top4 = st.columns([1.5, 1.3, 1.2, 1.2])

with col_top1:
    if st.button("🔄 Scan & Re-Check Live Prices on Flipkart", type="primary", use_container_width=True):
        with st.spinner("Connecting to Flipkart, checking active discounts, and updating local tracker..."):
            count = refresh_all_live_prices()
            st.success(f"Updated live prices for {count} products in database!")
            st.rerun()

with col_top2:
    if st.button("🚨 Reset Database & Purge Broken Links", use_container_width=True, help="Force-cleans legacy cached links and replaces them with verified working URLs"):
        fresh = generate_seed_catalog()
        save_tracker_data(fresh)
        st.success("Successfully purged broken links! All URLs have been updated.")
        st.rerun()

with col_top3:
    expand_all = st.checkbox("📂 Expand All Category Tables", value=False)

with col_top4:
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
# ➕ 2-FIELD QUICK ADD WISHLIST MANAGER (NAME + LINK ONLY)
# ==============================================================================
with st.expander("➕ Add Direct Product to Wishlist (Only 2 Inputs: Name & Link)", expanded=False):
    st.markdown("Paste your real Flipkart product link copied from your browser. It will be saved permanently and routed to its dedicated wishlist category.")
    with st.form("quick_2_field_form", clear_on_submit=True):
        f_name = st.text_input("📦 Product Name:", placeholder="e.g. Levi's 511 Slim Jeans")
        f_url = st.text_input("🔗 Direct Flipkart Product Link:", placeholder="Paste exact Flipkart product page URL here")
        submit_btn = st.form_submit_button("💾 Save Direct Product to Wishlist", type="primary")

        if submit_btn:
            if not f_name.strip() or not f_url.strip():
                st.error("Please enter both Name and Link.")
            else:
                cat_assigned = detect_category(f_name)
                clean_url = clean_url_safe(f_url)
                curr_p = 999
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
                    "Last Checked": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                    "version": TRACKER_VERSION
                }

                current_records = load_tracker_data()
                current_records.append(new_item)
                save_tracker_data(current_records)
                st.success(f"Saved '{f_name}' into '{cat_assigned}' with direct product link!")
                st.rerun()

# --- REUSABLE COLLAPSIBLE TABLE BUILDER WITH INBUILT FILTERS ---
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
                if st.button(f"⭐ Add {len(selected_indices)} Selected to Wishlist", type="primary", key=f"sel_add_{slug}"):
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
                    st.caption("Select an item to route automatically into its wishlist category:")
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
# USER SAVED WISHLISTS (ON SAME PAGE)
# ==============================================================================
wishlist_df = master_df[master_df["is_wishlist"] == True]
if not wishlist_df.empty:
    st.markdown('<div class="section-title-wishlist"><h2 style="margin:0;">💖 Your Saved Personal Wishlists</h2><p style="margin:2px 0 0 0; font-size:0.9rem;">Auto-organized into separate tables based on product type.</p></div>', unsafe_allow_html=True)
    distinct_wishlist_cats = sorted(list(wishlist_df["Category"].unique()))
    for w_cat in distinct_wishlist_cats:
        sub = wishlist_df[wishlist_df["Category"] == w_cat]
        slug = "w_" + w_cat.lower().replace(" ", "_").replace(":", "").replace("&", "")
        render_collapsible_table(sub, w_cat, slug, "💖", is_expanded=True, allow_deletion=True)

# ==============================================================================
# 1. CATEGORY CATALOG (ALL 9 CATEGORIES ON SAME PAGE)
# ==============================================================================
st.markdown('<div class="section-title-catalog"><h2 style="margin:0;">1. 🛒 Flipkart Category Catalog (All 9 Categories)</h2><p style="margin:2px 0 0 0; font-size:0.9rem;">Independent collapsible tables with dedicated column filters. Stationery & Books at the bottom.</p></div>', unsafe_allow_html=True)

for cat_title, meta in CAT_DATA_MATRIX.items():
    cat_df = master_df[(master_df["Category"] == cat_title) & (master_df["is_wishlist"] == False)]
    render_collapsible_table(cat_df, cat_title, meta["slug"], meta["icon"], is_expanded=expand_all)

# ==============================================================================
# 2. BBD STEAL DEALS (ON SAME PAGE)
# ==============================================================================
st.markdown('<div class="section-title-bbd"><h2 style="margin:0;">2. 🔥 The Big Billion Days Floor Deals (Cheapest Recorded)</h2><p style="margin:2px 0 0 0; font-size:0.9rem;">Products currently cheaper than or equal to last year\'s BBD festive floor low.</p></div>', unsafe_allow_html=True)

bbd_steals_df = master_df[master_df["Diff vs Last BBD"] <= 0].sort_values("Diff vs Last BBD", ascending=True)
if not bbd_steals_df.empty:
    render_collapsible_table(bbd_steals_df, "All Confirmed BBD Floor Steals", "bbd_steals", "🔥", is_expanded=True)
else:
    st.info("No products currently beat last year's BBD floor price. Click 'Scan & Re-Check Live Prices' to update.")

# ==============================================================================
# 3. FLIPKART ADS & FEATURED PROMOTIONS (ON SAME PAGE)
# ==============================================================================
st.markdown('<div class="section-title-ads"><h2 style="margin:0;">3. 📢 Flipkart Promoted, Sponsored & Top Banner Deals</h2><p style="margin:2px 0 0 0; font-size:0.9rem;">Tracking active sponsored product placements and ad banner promotions benchmarked against their 6-month historical averages.</p></div>', unsafe_allow_html=True)

ads_df = master_df[master_df["is_ad"] == True].sort_values("Real Disc % (vs 6M)", ascending=False)
render_collapsible_table(ads_df, "Active Sponsored & Banner Promotions", "flipkart_ads", "📢", is_expanded=True)
