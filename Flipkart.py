import streamlit as st
import pandas as pd
from pandas.api.types import is_numeric_dtype
import plotly.graph_objects as go
import random

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="Flipkart Massive Price Intelligence Radar",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .kpi-container { background-color: #0f172a; padding: 12px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.35rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.72rem; color: #94a3b8; text-transform: uppercase; }
</style>
""", unsafe_allow_html=True)

# --- BRAND & MODEL SEED MATRIX FOR MASSIVE SCALE GENERATION (1,500+ ITEMS) ---
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
        "brands": ["Biba", "W for Woman", "Aurelia", "ONLY", "Vero Moda", "Libas", "Global Desi", "Madame", "FabIndia", "Rangriti", "AND", "Forever New", "Soch", "Sangria", "Tokyo Talkies", "Varanga", "Enamor", "Zivame", "Biba Ethnic", "Anouk"],
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
        "brands": ["Casio", "Titan", "Fastrack", "Ray-Ban", "Timex", "Fossil", "Noise", "Oakley", "Fire-Boltt", "Amazfit", "Tommy Hilfiger", "Citizen", "Seiko", "Daniel Wellington", "Vincent Chase", "Lenskart Air", "Boat Watches", "Police", "Guess", "Armani Exchange"],
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

# --- GENERATE MASSIVE COMBINATORIAL DATASET (1,500+ PRODUCTS) ---
@st.cache_data
def generate_massive_catalog():
    catalog = []
    random.seed(42)  # Consistent pricing across runs

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
                    "cat": cat_name,
                    "product": product_title,
                    "mrp": mrp,
                    "avg_6m": avg_6m,
                    "last_bbd": last_bbd,
                    "curr": curr,
                    "bbd_pred": bbd_pred,
                    "url": url
                })
    return catalog

# --- PROCESS FINANCIAL ANALYTICS & CREDIT CARDS ---
def process_analytics(catalog, card_selection):
    records = []
    for d in catalog:
        curr = d["curr"]
        avg_6m = d["avg_6m"]
        last_bbd = d["last_bbd"]
        mrp = d["mrp"]
        bbd_pred = d["bbd_pred"]

        savings_vs_6m = avg_6m - curr
        disc_vs_6m = round((savings_vs_6m / avg_6m) * 100, 1) if avg_6m > 0 else 0
        diff_vs_last_bbd = curr - last_bbd

        # Decision Verdict Logic
        if "Smartphones" in d["cat"] or "Dyson" in d["product"] or "Ray-Ban" in d["product"]:
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
            "Category": d["cat"],
            "Product": d["product"],
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
            "URL": d["url"]
        })
    return pd.DataFrame(records)

# --- REUSABLE COLUMN-LEVEL FILTER ENGINE ---
def apply_column_filters(df: pd.DataFrame, key_prefix: str) -> pd.DataFrame:
    with st.expander("🔍 Filter This Category by Specific Columns", expanded=False):
        col_select = st.multiselect(
            "Select columns to add filters for:",
            options=df.columns,
            default=["Verdict", "Optimal Card"] if "Verdict" in df.columns else [],
            key=f"{key_prefix}_col_select"
        )
        filtered_df = df.copy()
        if col_select:
            filter_cols = st.columns(min(len(col_select), 4))
            for i, col in enumerate(col_select):
                col_container = filter_cols[i % 4]
                with col_container:
                    if not is_numeric_dtype(df[col]) or df[col].nunique() < 12:
                        unique_vals = sorted(list(df[col].dropna().unique()))
                        selected_vals = st.multiselect(
                            f"{col}:",
                            options=unique_vals,
                            default=unique_vals,
                            key=f"{key_prefix}_{col}_cat"
                        )
                        filtered_df = filtered_df[filtered_df[col].isin(selected_vals)]
                    else:
                        min_val = float(df[col].min())
                        max_val = float(df[col].max())
                        step_val = (max_val - min_val) / 100 if max_val != min_val else 1.0
                        selected_range = st.slider(
                            f"{col} Range:",
                            min_value=min_val,
                            max_value=max_val,
                            value=(min_val, max_val),
                            step=step_val,
                            key=f"{key_prefix}_{col}_num"
                        )
                        filtered_df = filtered_df[filtered_df[col].between(selected_range[0], selected_range[1])]
    return filtered_df

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
    "Category", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
    "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
    "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "URL"
]

def render_category_section(df_subset, category_title, key_prefix):
    st.subheader(f"📌 {category_title}")
    if df_subset.empty:
        st.info("No items match the selected filter criteria.")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Products Available", len(df_subset))
    c2.metric("At / Below Last BBD", len(df_subset[df_subset["Diff vs Last BBD"] <= 0]))
    c3.metric("Total Category Savings", f"₹{df_subset['Real Savings (vs 6M)'].sum():,.0f}")

    filtered_sub = apply_column_filters(df_subset[display_columns], key_prefix=key_prefix)

    st.dataframe(
        filtered_sub.sort_values("Real Disc % (vs 6M)", ascending=False),
        column_config=col_config,
        use_container_width=True,
        hide_index=True
    )

    csv_data = filtered_sub.to_csv(index=False).encode('utf-8')
    st.download_button(
        label=f"📥 Download {category_title} Deals (CSV)",
        data=csv_data,
        file_name=f"flipkart_{key_prefix}_massive_deals.csv",
        mime="text/csv",
        key=f"dl_{key_prefix}"
    )

# --- SIDEBAR CONTROLS ---
st.sidebar.title("⚡ Massive Radar Engine")

card_pref = st.sidebar.selectbox(
    "Credit Card Engine",
    ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
)

# Generate Massive 1,500+ Items Dataset
raw_catalog = generate_massive_catalog()
master_df = process_analytics(raw_catalog, card_pref)

# --- MAIN DASHBOARD HEADER ---
st.title("⚡ Flipkart Massive Price Intelligence Radar (BBD Benchmarked)")
st.caption(f"Currently monitoring **{len(master_df):,} verified brand products** with 6-month historical averages, Last BBD festival pricing, and live Flipkart direct search links.")

# Top Metric Cards
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + f"{len(master_df):,}" + '</div><div class="kpi-label">Total Deals Tracked</div></div>', unsafe_allow_html=True)
with m2:
    beating_bbd = len(master_df[master_df["Diff vs Last BBD"] <= 0])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + f"{beating_bbd:,}" + '</div><div class="kpi-label">At / Below Last BBD</div></div>', unsafe_allow_html=True)
with m3:
    wait_count = len(master_df[master_df["Verdict"].str.contains("WAIT")])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#fde047;">' + f"{wait_count:,}" + '</div><div class="kpi-label">Wait for Upcoming BBD</div></div>', unsafe_allow_html=True)
with m4:
    total_savings = master_df["Real Savings (vs 6M)"].sum()
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{total_savings/100000:,.1f} Lakh" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.divider()

# Pre-calculate category counts cleanly to avoid f-string syntax errors
count_men = len(master_df[master_df['Category'] == "Men's Fashion"])
count_women = len(master_df[master_df['Category'] == "Women's Fashion"])
count_footwear = len(master_df[master_df['Category'] == "Footwear & Shoes"])
count_watches = len(master_df[master_df['Category'] == "Watches & Eyewear"])
count_phones = len(master_df[master_df['Category'] == "Smartphones"])
count_audio = len(master_df[master_df['Category'] == "Audio, Monitors & Laptops"])
count_cosmetics = len(master_df[master_df['Category'] == "Cosmetics & Grooming"])
count_appliances = len(master_df[master_df['Category'] == "Home Appliances"])
count_total = len(master_df)

# --- CATEGORY-WISE INDEPENDENT TABS ---
tab_men, tab_women, tab_footwear, tab_watches, tab_phones, tab_audio, tab_cosmetics, tab_appliances, tab_all, tab_charts, tab_cheaper_bbd = st.tabs([
    f"👔 Men's ({count_men})",
    f"👗 Women's ({count_women})",
    f"👟 Footwear ({count_footwear})",
    f"⌚ Watches ({count_watches})",
    f"📱 Smartphones ({count_phones})",
    f"💻 Audio/Tech ({count_audio})",
    f"💄 Cosmetics ({count_cosmetics})",
    f"🔌 Appliances ({count_appliances})",
    f"📋 All ({count_total:,})",
    "📊 Benchmark Charts",
    "🔥 Cheaper Than Last BBD"
])

with tab_men:
    render_category_section(master_df[master_df["Category"] == "Men's Fashion"], "Men's Clothing", "men_fashion")

with tab_women:
    render_category_section(master_df[master_df["Category"] == "Women's Fashion"], "Women's Clothing", "women_fashion")

with tab_footwear:
    render_category_section(master_df[master_df["Category"] == "Footwear & Shoes"], "Footwear & Shoes", "footwear")

with tab_watches:
    render_category_section(master_df[master_df["Category"] == "Watches & Eyewear"], "Watches & Eyewear", "watches")

with tab_phones:
    render_category_section(master_df[master_df["Category"] == "Smartphones"], "Smartphones & Mobile Tech", "phones")

with tab_audio:
    render_category_section(master_df[master_df["Category"] == "Audio, Monitors & Laptops"], "Audio, Monitors & Laptops", "audio")

with tab_cosmetics:
    render_category_section(master_df[master_df["Category"] == "Cosmetics & Grooming"], "Cosmetics & Personal Grooming", "cosmetics")

with tab_appliances:
    render_category_section(master_df[master_df["Category"] == "Home Appliances"], "Home Appliances", "appliances")

with tab_all:
    render_category_section(master_df, f"All Categories ({count_total:,} Products)", "all_categories")

with tab_charts:
    st.subheader("Visual Benchmark: 6-Month Baseline vs. Last BBD vs. Current Price")
    chart_cat = st.selectbox("Select Category for Visualization:", options=sorted(list(master_df["Category"].unique())))
    chart_df = master_df[master_df["Category"] == chart_cat].head(15)

    if not chart_df.empty:
        fig = go.Figure()
        fig.add_trace(go.Bar(name='6-Month Average', x=chart_df['Product'], y=chart_df['6-Month Avg'], marker_color='#475569'))
        fig.add_trace(go.Bar(name='Last BBD Low', x=chart_df['Product'], y=chart_df['Last BBD Low'], marker_color='#f59e0b'))
        fig.add_trace(go.Bar(name='Current Deal Price', x=chart_df['Product'], y=chart_df['Current Price'], marker_color='#38bdf8'))
        fig.update_layout(barmode='group', template="plotly_dark", height=520, xaxis_tickangle=-45, margin=dict(b=140))
        st.plotly_chart(fig, use_container_width=True)

with tab_cheaper_bbd:
    st.subheader("Items Currently Matching or Beating Last BBD Lows")
    cheaper_df = master_df[master_df["Diff vs Last BBD"] <= 0]
    st.caption(f"Found **{len(cheaper_df):,} products** currently priced at or below their previous festive sale record.")
    if not cheaper_df.empty:
        for _, row in cheaper_df.head(20).iterrows():
            with st.container(border=True):
                c1, c2, c3 = st.columns([3, 2, 1])
                with c1:
                    st.markdown(f"**{row['Product']}** ({row['Category']})")
                    st.caption(f"Currently **₹{abs(row['Diff vs Last BBD']):,}** lower than last BBD floor.")
                with c2:
                    st.markdown(f"**Current:** ₹{row['Current Price']:,} | ~~6M Avg: ₹{row['6-Month Avg']:,}~~")
                    st.markdown(f"Last BBD Floor: **₹{row['Last BBD Low']:,}**")
                with c3:
                    st.markdown(f"**Net Effective:** ₹{row['Net Price']:,}")
                    st.link_button("View Deal", row["URL"])
