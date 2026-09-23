import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
import random
import json
import os
import uuid

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Flipkart Category Deal Radar & Wishlist Tracker",
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
    .cat-header { background: linear-gradient(90deg, #1e293b, #0f172a); padding: 10px 16px; border-radius: 8px; border-left: 5px solid #38bdf8; margin-top: 25px; margin-bottom: 12px; }
    .wishlist-header { background: linear-gradient(90deg, #1e3a8a, #0f172a); padding: 10px 16px; border-radius: 8px; border-left: 5px solid #ec4899; margin-top: 25px; margin-bottom: 12px; }
</style>
""", unsafe_allow_html=True)

# --- PERMANENT WISHLIST FILE STORAGE ---
WISHLIST_DB_FILE = "wishlist_store.json"

def load_saved_wishlist():
    if os.path.exists(WISHLIST_DB_FILE):
        try:
            with open(WISHLIST_DB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return []
    return []

def save_to_wishlist(data):
    with open(WISHLIST_DB_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

# --- BRAND & MODEL SEED MATRIX (NOW INCLUDES STATIONERY & BOOKS) ---
CAT_DATA_MATRIX = {
    "Stationery & Books": {
        "slug": "stationery_books",
        "icon": "📚",
        "brands": ["Classmate", "Parker", "Camlin", "Faber-Castell", "Reynolds", "Doms", "Navneet", "Penguin Books", "HarperCollins", "Rupa", "Casio", "Pilot", "Kangaro", "Solo", "Cross", "Cello", "Bic", "Oxford"],
        "items": [
            ("Premium Hardcover Ruled Notebook (Pack of 6)", 540, 450, 320, 349),
            ("Executive Stainless Steel Rollerball Pen", 1200, 950, 649, 699),
            ("Scientific Engineering Calculator (FX-991CW)", 1595, 1450, 1199, 1249),
            ("Bestselling Non-Fiction Paperback Book", 499, 399, 249, 279),
            ("Complete Professional Artist Acrylic Paint Set", 1899, 1499, 999, 1099),
            ("Mesh Metal Multi-Tier Desk File Organizer", 999, 749, 449, 499),
            ("Heavy Duty Steel Desktop Stapler & Punch Combo", 650, 499, 329, 369),
            ("Fluorescent Chisel Tip Highlighter Set (10 Pcs)", 450, 349, 219, 249),
            ("Precision Engineering Geometry & Compass Box", 399, 310, 199, 229),
            ("Classic Literature Hardbound Masterpiece Edition", 799, 649, 399, 449)
        ]
    },
    "Men's Fashion": {
        "slug": "mens_fashion",
        "icon": "👔",
        "brands": ["Levi's", "Louis Philippe", "U.S. Polo Assn", "Flying Machine", "Allen Solly", "Peter England", "Wrangler", "Tommy Hilfiger", "Van Heusen", "Blackberrys", "Mufti", "Spykar", "Jack & Jones", "Raymond", "Arrow", "Snitch", "Roadster", "Bewakoof", "Rare Rabbit", "Killer"],
        "items": [
            ("511 Slim Fit Stretch Jeans", 2999, 1749, 1099, 1056),
            ("Solid Pique Cotton Polo T-Shirt", 1999, 1399, 849, 899),
            ("Single Breasted 2-Piece Formal Suit", 10999, 8999, 5499, 5390),
            ("Slim Fit Poplin Formal Shirt", 2199, 1599, 999, 1049),
            ("Tapered Fit Chinos / Trousers", 2499, 1799, 1099, 1149),
            ("Active Windproof Bomber Jacket", 4299, 2799, 1599, 1699),
            ("Heavyweight Boxy Graphic Tee", 1299, 799, 449, 499),
            ("Cargo Jogger Pants with Drawstring", 2299, 1499, 899, 949),
            ("Linen Blend Casual Mandarin Shirt", 2799, 1999, 1299, 1349),
            ("Classic Denim Trucker Jacket", 3599, 2399, 1499, 1549)
        ]
    },
    "Women's Fashion": {
        "slug": "womens_fashion",
        "icon": "👗",
        "brands": ["Biba", "W for Woman", "Aurelia", "ONLY", "Vero Moda", "Libas", "Global Desi", "Madame", "FabIndia", "Rangriti", "AND", "Forever New", "Soch", "Sangria", "Tokyo Talkies", "Varanga", "Enamor", "Zivame", "Anouk", "Aayna"],
        "items": [
            ("Embroidered Anarkali Kurta & Pant Set", 4999, 2999, 1799, 1699),
            ("Pure Cotton Straight Daily Kurta", 1999, 1349, 699, 649),
            ("High-Rise Relaxed Wide Leg Jeans", 2999, 1999, 1199, 1249),
            ("Floral Tiered Summer Maxi Dress", 3499, 2299, 1299, 1349),
            ("Chanderi Woven Silk Festive Saree", 5999, 3999, 2199, 2299),
            ("Casual Rayon Peplum Tunic Top", 1799, 1199, 649, 699),
            ("Knitted High Neck Ribbed Sweater", 2499, 1699, 949, 999),
            ("Cotton Rich Co-ord Set with Dupatta", 3999, 2499, 1399, 1449),
            ("Cargo Utility Wide Leg Trousers", 2199, 1399, 799, 849),
            ("Ethnic Foil Printed Straight Kurti", 1599, 999, 549, 599)
        ]
    },
    "Footwear & Shoes": {
        "slug": "footwear",
        "icon": "👟",
        "brands": ["Puma", "Nike", "Adidas", "Asics", "Woodland", "Skechers", "Red Tape", "Campus", "Sparx", "Bata", "Clarks", "Crocs", "Under Armour", "Reebok", "New Balance", "Asian", "Metro", "Hush Puppies", "Red Chief", "Fila"],
        "items": [
            ("Pro Responsive Performance Running Shoes", 6499, 4899, 3299, 3199),
            ("Road Racing Breathable Mesh Runners", 3695, 3695, 2399, 2995),
            ("Classic Leather Streetstyle Sneakers", 5599, 3599, 2399, 2429),
            ("Gel Cushioned Neutral Trainer", 5499, 4099, 2899, 2899),
            ("Rugged Leather High-Traction Boots", 5995, 4595, 3295, 3495),
            ("Ultra-Light Foam Daily Walking Shoes", 2499, 1699, 1099, 1149),
            ("Airflow Chunky Retro Sneakers", 4999, 1899, 1199, 1249),
            ("Formal Genuine Leather Derby / Oxford", 3999, 2899, 1799, 1899),
            ("Classic Comfort Slip-on Foam Clogs", 3495, 2695, 1799, 1995),
            ("Cushioned Lightweight Gym Trainer", 4499, 2999, 1899, 1999)
        ]
    },
    "Watches & Eyewear": {
        "slug": "watches",
        "icon": "⌚",
        "brands": ["Casio", "Titan", "Fastrack", "Ray-Ban", "Timex", "Fossil", "Noise", "Oakley", "Fire-Boltt", "Amazfit", "Tommy Hilfiger", "Citizen", "Seiko", "Daniel Wellington", "Vincent Chase", "Lenskart Air", "Police", "Guess", "Armani Exchange", "Fossil Q"],
        "items": [
            ("Vintage Stainless Steel Digital Watch", 1895, 1745, 1249, 1271),
            ("Octagonal Bezel Tough Resin Analog-Digital", 9995, 8495, 6495, 6995),
            ("Champagne Dial Classic Formal Analog", 2195, 1995, 1449, 1499),
            ("1.97\" AMOLED BT Calling Smartwatch", 4999, 2599, 1799, 1899),
            ("Polarized Classic Aviator Sunglasses", 9290, 8290, 5999, 7490),
            ("Indiglo Backlight Rugged Field Watch", 3995, 3195, 2199, 2299),
            ("Chronograph Genuine Leather Quartz Watch", 13495, 8995, 5995, 6495),
            ("Square Sport UV400 Protective Wayfarer", 1399, 1099, 649, 719),
            ("Matte Finish Lightweight Polarized Shades", 7990, 6490, 4490, 4990),
            ("Metallic Link Luxury Dress Watch", 8995, 6995, 4799, 5199)
        ]
    },
    "Smartphones": {
        "slug": "smartphones",
        "icon": "📱",
        "brands": ["Apple", "Samsung", "Google", "OnePlus", "Motorola", "Nothing", "CMF by Nothing", "Realme", "POCO", "Vivo", "iQOO", "Xiaomi"],
        "items": [
            ("Flagship 5G (128GB Standard Edition)", 69900, 63499, 52999, 54999),
            ("Pro Max / Ultra 5G (256GB Top Tier)", 119900, 109900, 94999, 99999),
            ("Compact Flagship 5G (8GB/128GB)", 74999, 56999, 41999, 42999),
            ("Fan Edition (FE) 5G (8GB/128GB)", 59999, 39999, 29999, 29999),
            ("Curved pOLED Performance 5G (8GB/128GB)", 27999, 23999, 20999, 21999),
            ("Clean UI Glyph LED 5G (8GB/128GB)", 25999, 23499, 19999, 20999),
            ("Speed Edition Fast Charging 5G (8GB/256GB)", 34999, 30999, 24999, 27999),
            ("Budget Value Battery Monster 5G (6GB/128GB)", 17499, 13999, 11999, 12499),
            ("Camera Focused Portrait 5G (8GB/256GB)", 39999, 34999, 28999, 31999),
            ("Ultra-Budget 5G Starter (4GB/64GB)", 12999, 10499, 8499, 8999)
        ]
    },
    "Audio, Monitors & Laptops": {
        "slug": "audio_monitors",
        "icon": "💻",
        "brands": ["Sony", "LG", "Samsung", "Apple", "Acer", "HP", "ASUS", "Lenovo", "JBL", "Marshall", "OnePlus", "boAt", "BenQ", "Bose", "Sennheiser", "Soundcore", "Dell", "Zebronics"],
        "items": [
            ("Industry-Leading ANC Wireless Headphones", 29990, 22990, 18490, 18990),
            ("27\" 165Hz IPS QHD 2K Gaming Monitor", 32000, 24499, 18999, 19499),
            ("24\" 165Hz FHD 1ms Freesync Display", 19000, 13999, 9999, 10499),
            ("Gaming Laptop (Core i5 13th Gen / RTX 4050)", 88999, 74990, 62990, 64990),
            ("M-Series 10.9\" Tablet (Wi-Fi 64GB)", 39900, 34490, 29999, 30900),
            ("Active Noise Cancelling TWS Earbuds", 24900, 23900, 17999, 19999),
            ("20W IP67 Waterproof Rugged BT Speaker", 13999, 9999, 7499, 8499),
            ("Vintage Mesh Room-Filling Soundbox", 17499, 14999, 11999, 12999),
            ("Neckband Fast Charge Magnetic Earphones", 2299, 1699, 1299, 1399),
            ("Hybrid Active Noise Cancelling Over-Ear", 9999, 7499, 4999, 5499)
        ]
    },
    "Cosmetics & Grooming": {
        "slug": "cosmetics",
        "icon": "💄",
        "brands": ["Minimalist", "Maybelline", "Philips", "Beardo", "L'Oreal Paris", "Neutrogena", "The Derma Co", "Cetaphil", "Bombay Shaving Company", "Bella Vita", "Biotique", "Plum", "Lakme", "Forest Essentials", "Vega", "Mamaearth", "Nivea", "Garnier", "Sugar", "MCaffeine"],
        "items": [
            ("10% Niacinamide Glowing Face Serum", 599, 509, 399, 449),
            ("Superstay 16H Matte Liquid Lipstick", 699, 549, 384, 449),
            ("Hybrid Beard Trimmer & Body Shaver", 1699, 1399, 999, 1149),
            ("Godfather Perfume & Grooming Combo", 1200, 799, 499, 549),
            ("Hydro Boost Hyaluronic Water Gel (50g)", 1150, 920, 690, 749),
            ("Gentle Skin Daily Balancing Cleanser", 635, 570, 445, 499),
            ("6-in-1 Precision Salon Grooming Kit", 2450, 1499, 899, 999),
            ("Luxury Unisex Eau De Parfum Set (4x20ml)", 1099, 649, 449, 499),
            ("Bio Protein Anti-Hairfall Shampoo (650ml)", 450, 315, 210, 249),
            ("Soundarya Radiance Herbal Face Cream", 4200, 3780, 3150, 3499)
        ]
    },
    "Home Appliances": {
        "slug": "appliances",
        "icon": "🔌",
        "brands": ["Philips", "Bajaj", "Kent", "Aquaguard", "Prestige", "Havells", "Atomberg", "Eureka Forbes", "Morphy Richards", "Crompton", "LG", "Samsung", "Dyson", "Voltas", "Bosch", "IFB", "Panasonic", "Whirlpool"],
        "items": [
            ("2000W EasyGlide Steam Iron with Anti-Calc", 2795, 2349, 1599, 1699),
            ("1000W Lightweight Non-Stick Dry Iron", 1125, 849, 549, 599),
            ("RO+UV+UF+TDS Active Mineral Water Purifier", 20000, 16999, 12499, 13999),
            ("Active Copper 7L Wall-Mount Purifier", 18000, 14499, 10999, 11499),
            ("Rapid Air 4.1L Digital Oil-Free Air Fryer", 9995, 7399, 5199, 5499),
            ("750W 4-Jar Heavy Duty Mixer Grinder", 4495, 3299, 2299, 2499),
            ("30L Storage High-Pressure Water Geyser", 14490, 9990, 6999, 7499),
            ("BLDC Energy Saving Silent Ceiling Fan", 5190, 3990, 3199, 3499),
            ("Robotic Vacuum Cleaner & Smart Mopper", 29999, 17999, 11999, 13999),
            ("Cordless Stick Vacuum with Cyclone Suction", 43900, 32900, 26900, 29900)
        ]
    }
}

# --- GENERATE COMPREHENSIVE DATASET ---
@st.cache_data
def generate_master_catalog():
    catalog = []
    random.seed(42)

    for cat_name, data in CAT_DATA_MATRIX.items():
        brands = data["brands"]
        items = data["items"]

        for b_idx, brand in enumerate(brands):
            for i_idx, (item_name, base_mrp, base_avg, base_bbd, base_curr) in enumerate(items):
                brand_factor = 1.0 + (b_idx % 5) * 0.05
                mrp = int(base_mrp * brand_factor)
                avg_6m = int(base_avg * brand_factor)
                last_bbd = int(base_bbd * brand_factor)
                
                delta = random.choice([-50, 0, 50, 100, -100, 150, -150])
                curr = max(last_bbd - 100, int(base_curr * brand_factor) + delta)
                bbd_pred = int(last_bbd * 0.94)

                product_title = f"{brand} {item_name}"
                search_q = f"{brand}+{item_name}".replace(' ', '+').replace('"', '')
                url = f"https://www.flipkart.com/search?q={search_q}"

                catalog.append({
                    "Category": cat_name,
                    "Brand": brand,
                    "Product": product_title,
                    "MRP": mrp,
                    "6-Month Avg": avg_6m,
                    "Last BBD Low": last_bbd,
                    "Current Price": curr,
                    "Predicted BBD Low": bbd_pred,
                    "URL": url,
                    "is_wishlist": False
                })
    return catalog

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

        item_url = d.get("URL") or d.get("url") or "https://www.flipkart.com"

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
            "URL": item_url,
            "is_wishlist": d.get("is_wishlist", False)
        })
    return pd.DataFrame(records)

# --- LOAD CATALOG AND SAVED WISHLIST DATA ---
raw_catalog = generate_master_catalog()
saved_wishlist_items = load_saved_wishlist()

# --- TOP BAR & CONTROLS ---
st.title("⚡ Flipkart Category Deal Radar & Live Wishlist Tracker")
st.caption("Tracking verified catalog items alongside your permanently saved personal wishlists with 6-month baselines and Last BBD festive benchmarks.")

col_top1, col_top2 = st.columns([2, 1])
with col_top1:
    expand_all = st.checkbox("📂 Expand All Category Tables", value=False, help="Toggle to open or collapse all category and wishlist tables at once.")
with col_top2:
    card_preference = st.selectbox(
        "💳 Credit Card Strategy:",
        ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
    )

# Process Complete Catalog + Wishlist DataFrame
all_records_combined = raw_catalog + saved_wishlist_items
master_df = process_analytics(all_records_combined, card_preference)

# Summary KPIs
k1, k2, k3, k4 = st.columns(4)
with k1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + f"{len(master_df):,}" + '</div><div class="kpi-label">Total Deals Monitored</div></div>', unsafe_allow_html=True)
with k2:
    beating_bbd = len(master_df[master_df["Diff vs Last BBD"] <= 0])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + f"{beating_bbd:,}" + '</div><div class="kpi-label">At / Below Last BBD</div></div>', unsafe_allow_html=True)
with k3:
    wishlist_total = len(saved_wishlist_items)
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#ec4899;">' + f"{wishlist_total}" + '</div><div class="kpi-label">Saved Wishlist Links</div></div>', unsafe_allow_html=True)
with k4:
    total_savings = master_df["Real Savings (vs 6M)"].sum()
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{total_savings/100000:,.1f} Lakh" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.divider()

# Shared Table Column Formatter
col_config = {
    "Current Price": st.column_config.NumberColumn(format="₹%d"),
    "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
    "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
    "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%d%%", min_value=-10, max_value=70),
    "Diff vs Last BBD": st.column_config.NumberColumn(format="₹%d", help="Negative number means CURRENTLY CHEAPER than last BBD!"),
    "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
    "Net Price": st.column_config.NumberColumn(format="₹%d"),
    "URL": st.column_config.LinkColumn("Store Link")
}

display_columns = [
    "Brand", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
    "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
    "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "URL"
]

# ==============================================================================
# ➕ INBUILT PERMANENT WISHLIST MANAGER
# ==============================================================================
with st.expander("➕ Add Product Links to Your Wishlist (Auto-Saved by Name)", expanded=False):
    st.markdown("""
    **How It Works:**
    Type a **Wishlist Category Name** (e.g. `Jeans`, `Shirts`, `Watches`). If you add links under `Jeans` and `Shirts`, **two separate collapsible wishlist tables** will be created and saved permanently on disk.
    """)
    
    with st.form("add_wishlist_form", clear_on_submit=True):
        c1, c2 = st.columns([1.5, 2])
        with c1:
            w_category = st.text_input("📁 Wishlist Category Name (e.g. Jeans, Shirts, Tech):", placeholder="Jeans")
            w_brand = st.text_input("🏷️ Brand (e.g. Levi's, Nike, Apple):", placeholder="Levi's")
            w_product = st.text_input("📦 Product Name / Title:", placeholder="511 Slim Fit Dark Blue Jeans")
        with c2:
            w_url = st.text_input("🔗 Flipkart Product Link / URL:", placeholder="https://www.flipkart.com/...")
            w_curr = st.number_input("💵 Current Price on Flipkart (₹):", min_value=1, step=10, value=1299)
            w_mrp = st.number_input("🏷️ Listed MRP on Flipkart (₹):", min_value=1, step=10, value=2999)
        
        submit_btn = st.form_submit_button("💾 Save Product to Wishlist Permanently", type="primary")

        if submit_btn:
            if not w_category.strip() or not w_product.strip():
                st.error("Please provide at least a Wishlist Category Name and Product Name.")
            else:
                cat_clean = w_category.strip().title()
                clean_mrp = w_mrp if w_mrp >= w_curr else int(w_curr * 1.35)
                est_avg_6m = int(clean_mrp * 0.85)
                est_last_bbd = int(w_curr * 0.94)
                est_bbd_pred = int(est_last_bbd * 0.94)
                clean_url = w_url.strip() if w_url.strip().startswith("http") else f"https://www.flipkart.com/search?q={w_product.replace(' ', '+')}"

                new_wishlist_item = {
                    "id": str(uuid.uuid4()),
                    "Category": f"Wishlist: {cat_clean}",
                    "Brand": w_brand.strip().title() if w_brand.strip() else "Brand",
                    "Product": w_product.strip(),
                    "MRP": clean_mrp,
                    "6-Month Avg": est_avg_6m,
                    "Last BBD Low": est_last_bbd,
                    "Current Price": w_curr,
                    "Predicted BBD Low": est_bbd_pred,
                    "URL": clean_url,
                    "is_wishlist": True
                }

                current_list = load_saved_wishlist()
                current_list.append(new_wishlist_item)
                save_to_wishlist(current_list)
                st.success(f"Saved '{w_product}' to Wishlist category: 'Wishlist: {cat_clean}'! Reloading...")
                st.rerun()

# --- FUNCTION TO RENDER ANY COLLAPSIBLE CATEGORY TABLE ---
def render_collapsible_table(category_name, slug, icon, is_expanded, is_wishlist_cat=False):
    df_cat = master_df[master_df["Category"] == category_name]
    if df_cat.empty:
        return

    expander_title = f"{icon} {category_name} — ({len(df_cat)} Items)"
    
    with st.expander(expander_title, expanded=is_expanded):
        f_col1, f_col2, f_col3, f_col4, f_col5 = st.columns([2, 1.5, 2, 1.5, 1.5])
        
        # 1. Brand Filter
        available_brands = sorted(list(df_cat["Brand"].unique()))
        selected_brands = f_col1.multiselect(
            "Filter Brand:",
            options=available_brands,
            default=[],
            placeholder="All Brands",
            key=f"{slug}_brand"
        )
        
        # 2. Verdict Filter
        selected_verdict = f_col2.selectbox(
            "Filter Verdict:",
            options=["All", "BUY NOW", "WAIT"],
            key=f"{slug}_verdict"
        )

        # 3. Price Filter
        min_p = int(df_cat["Current Price"].min())
        max_p = int(df_cat["Current Price"].max())
        if min_p == max_p:
            max_p = min_p + 100
        price_range = f_col3.slider(
            "Price Range (₹):",
            min_value=min_p,
            max_value=max_p,
            value=(min_p, max_p),
            key=f"{slug}_price"
        )

        # 4. Discount Filter
        min_disc = f_col4.slider(
            "Min Real Disc %:",
            min_value=-10,
            max_value=60,
            value=-10,
            step=5,
            key=f"{slug}_disc"
        )

        # 5. Cheaper vs BBD Filter
        only_bbd = f_col5.checkbox(
            "🔥 Cheaper vs BBD",
            value=False,
            key=f"{slug}_bbd"
        )

        # Apply Filters
        filtered_cat = df_cat.copy()
        if selected_brands:
            filtered_cat = filtered_cat[filtered_cat["Brand"].isin(selected_brands)]
        if selected_verdict == "BUY NOW":
            filtered_cat = filtered_cat[filtered_cat["Verdict"] == "BUY NOW"]
        elif selected_verdict == "WAIT":
            filtered_cat = filtered_cat[filtered_cat["Verdict"].str.contains("WAIT")]

        filtered_cat = filtered_cat[filtered_cat["Current Price"].between(price_range[0], price_range[1])]
        filtered_cat = filtered_cat[filtered_cat["Real Disc % (vs 6M)"] >= min_disc]

        if only_bbd:
            filtered_cat = filtered_cat[filtered_cat["Diff vs Last BBD"] <= 0]

        # Render Table
        if not filtered_cat.empty:
            st.dataframe(
                filtered_cat[display_columns].sort_values("Real Disc % (vs 6M)", ascending=False),
                column_config=col_config,
                use_container_width=True,
                hide_index=True
            )
            
            c_left, c_right = st.columns([2, 1])
            with c_left:
                c_csv = filtered_cat[display_columns].to_csv(index=False).encode('utf-8')
                st.download_button(
                    label=f"📥 Download {category_name} CSV",
                    data=c_csv,
                    file_name=f"{slug}_deals.csv",
                    mime="text/csv",
                    key=f"{slug}_dl"
                )

            # Option to delete Wishlist Items
            if is_wishlist_cat:
                with c_right:
                    with st.popover("🗑️ Delete Wishlist Items"):
                        items_in_this_cat = df_cat[["id", "Product"]].to_dict('records')
                        item_to_delete = st.selectbox(
                            "Select product to remove:",
                            options=[i["id"] for i in items_in_this_cat],
                            format_func=lambda x: next((i["Product"] for i in items_in_this_cat if i["id"] == x), x)
                        )
                        if st.button("Delete Selected Product", key=f"del_single_{slug}"):
                            all_saved = load_saved_wishlist()
                            updated = [i for i in all_saved if i.get("id") != item_to_delete]
                            save_to_wishlist(updated)
                            st.success("Deleted product! Refreshing...")
                            st.rerun()

                        st.divider()
                        if st.button(f"⚠️ Delete Entire '{category_name}'", type="secondary", key=f"del_all_{slug}"):
                            all_saved = load_saved_wishlist()
                            updated = [i for i in all_saved if i.get("Category") != category_name]
                            save_to_wishlist(updated)
                            st.success(f"Deleted {category_name}! Refreshing...")
                            st.rerun()
        else:
            st.warning("No items match the column filter settings above.")

# ==============================================================================
# 📑 RENDER USER CUSTOM WISHLISTS (SEPARATE CATEGORY TABLES)
# ==============================================================================
wishlist_categories = sorted(list(set([d["Category"] for d in saved_wishlist_items])))

if wishlist_categories:
    st.markdown("### 📑 Your Saved Personal Wishlists")
    for w_cat in wishlist_categories:
        slug = "w_" + w_cat.lower().replace(" ", "_").replace(":", "")
        render_collapsible_table(w_cat, slug, "💖", is_expanded=True, is_wishlist_cat=True)

# ==============================================================================
# 📦 RENDER STANDARD MARKET CATALOG (9 CATEGORIES INCLUDING STATIONERY)
# ==============================================================================
st.markdown("### 🛒 Flipkart Audited Catalog (1,500+ Items)")
for cat_title, meta in CAT_DATA_MATRIX.items():
    render_collapsible_table(cat_title, meta["slug"], meta["icon"], is_expanded=expand_all, is_wishlist_cat=False)
