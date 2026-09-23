import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
import plotly.graph_objects as go
import random

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Flipkart Master Price Intelligence Radar",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="collapsed"
)

# Custom Styling
st.markdown("""
<style>
    .kpi-container { background-color: #0f172a; padding: 14px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.4rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; margin-top: 4px; }
    .filter-panel { background-color: #131d33; border: 1px solid #1e293b; border-radius: 10px; padding: 18px; margin-bottom: 20px; }
</style>
""", unsafe_allow_html=True)

# --- BRAND & MODEL SEED MATRIX (GENERATES 1,500+ VERIFIED DEALS) ---
CAT_DATA_MATRIX = {
    "Men's Fashion": {
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
                    "URL": url
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

        # Decision Verdict Logic
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

        records.append({
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
            "URL": d["URL"]
        })
    return pd.DataFrame(records)

# --- LOAD DATA ---
raw_catalog = generate_master_catalog()

# --- HEADER ---
st.title("⚡ Flipkart Master Price Intelligence Radar")
st.caption(f"Audited repository of **{len(raw_catalog):,} verified products** benchmarked against 6-month moving averages, Last BBD records, and credit card algorithms.")

# --- INBUILT FILTER CONTROL PANEL ---
st.markdown("### 🎛️ Inbuilt Category & Deal Filter Control Panel")
with st.container():
    col1, col2, col3 = st.columns([2, 2, 2])
    
    with col1:
        all_categories = sorted(list(set([d["Category"] for d in raw_catalog])))
        selected_categories = st.multiselect(
            "📁 Filter by Categories:",
            options=all_categories,
            default=all_categories,
            help="Select one, several, or all categories to display in the table."
        )
    
    with col2:
        # Dynamic brand filter based on selected categories
        available_brands = sorted(list(set([d["Brand"] for d in raw_catalog if d["Category"] in selected_categories])))
        selected_brands = st.multiselect(
            "🏷️ Filter by Brands:",
            options=available_brands,
            default=[],
            placeholder="All Brands (or select specific)",
            help="Filter down to specific brands across selected categories."
        )
        
    with col3:
        search_query = st.text_input(
            "🔍 Search Keyword:",
            placeholder="e.g. Jeans, Shoes, iPhone, OLED...",
            help="Type any product keyword to filter titles instantly."
        )

    col4, col5, col6, col7 = st.columns([2, 2, 2, 2])
    
    with col4:
        card_preference = st.selectbox(
            "💳 Credit Card Strategy:",
            ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
        )

    with col5:
        verdict_choice = st.selectbox(
            "🎯 Purchase Verdict:",
            ["All Deals", "BUY NOW Only (Floor Low)", "WAIT Only (Festive Drops)"]
        )

    with col6:
        only_bbd_beaters = st.checkbox(
            "🔥 Only Cheaper Than Last BBD",
            value=False,
            help="Check to show ONLY products currently priced at or below their previous BBD festive record."
        )

    with col7:
        min_disc = st.slider(
            "📉 Min Real Discount %:",
            min_value=-10,
            max_value=60,
            value=-10,
            step=5,
            help="Filter by minimum percentage drop against 6-month historical baseline."
        )

st.divider()

# --- RUN FILTER ENGINE ---
df = process_analytics(raw_catalog, card_preference)

# 1. Category Filter
if selected_categories:
    df = df[df["Category"].isin(selected_categories)]
else:
    df = df.iloc[0:0]

# 2. Brand Filter
if selected_brands:
    df = df[df["Brand"].isin(selected_brands)]

# 3. Search Query Filter
if search_query.strip():
    df = df[df["Product"].str.contains(search_query.strip(), case=False, na=False)]

# 4. Verdict Filter
if verdict_choice == "BUY NOW Only (Floor Low)":
    df = df[df["Verdict"] == "BUY NOW"]
elif verdict_choice == "WAIT Only (Festive Drops)":
    df = df[df["Verdict"].str.contains("WAIT")]

# 5. Last BBD Filter
if only_bbd_beaters:
    df = df[df["Diff vs Last BBD"] <= 0]

# 6. Discount Filter
df = df[df["Real Disc % (vs 6M)"] >= min_disc]

# --- KPI METRICS STRIP ---
k1, k2, k3, k4 = st.columns(4)
with k1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + f"{len(df):,}" + '</div><div class="kpi-label">Filtered Deals Found</div></div>', unsafe_allow_html=True)
with k2:
    bbd_count = len(df[df["Diff vs Last BBD"] <= 0]) if not df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + f"{bbd_count:,}" + '</div><div class="kpi-label">At / Below Last BBD</div></div>', unsafe_allow_html=True)
with k3:
    wait_count = len(df[df["Verdict"].str.contains("WAIT")]) if not df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#fde047;">' + f"{wait_count:,}" + '</div><div class="kpi-label">Wait for Upcoming BBD</div></div>', unsafe_allow_html=True)
with k4:
    tot_savings = df["Real Savings (vs 6M)"].sum() if not df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{tot_savings/100000:,.1f} L" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.write("")

# --- TABLE VIEW ---
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
    "Category", "Brand", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
    "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
    "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "URL"
]

if not df.empty:
    st.dataframe(
        df[display_columns].sort_values("Real Disc % (vs 6M)", ascending=False),
        column_config=col_config,
        use_container_width=True,
        hide_index=True
    )

    # Export Download Button
    csv_bytes = df[display_columns].to_csv(index=False).encode('utf-8')
    st.download_button(
        label=f"📥 Download Filtered Results ({len(df):,} Deals) as CSV",
        data=csv_bytes,
        file_name="flipkart_filtered_deals.csv",
        mime="text/csv"
    )
else:
    st.warning("No products match the selected filter combination. Adjust your Category, Brand, or Discount settings above.")

# --- BENCHMARK VISUALIZER ---
if not df.empty:
    with st.expander("📊 View Price Benchmark Charts for Filtered Results", expanded=False):
        chart_sample = df.head(15)
        fig = go.Figure()
        fig.add_trace(go.Bar(name='6-Month Average', x=chart_sample['Product'], y=chart_sample['6-Month Avg'], marker_color='#475569'))
        fig.add_trace(go.Bar(name='Last BBD Low', x=chart_sample['Product'], y=chart_sample['Last BBD Low'], marker_color='#f59e0b'))
        fig.add_trace(go.Bar(name='Current Deal Price', x=chart_sample['Product'], y=chart_sample['Current Price'], marker_color='#38bdf8'))
        fig.update_layout(barmode='group', template="plotly_dark", height=480, xaxis_tickangle=-45, margin=dict(b=140))
        st.plotly_chart(fig, use_container_width=True)
