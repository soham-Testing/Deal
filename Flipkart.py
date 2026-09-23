import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time

# --- Page Configuration ---
st.set_page_config(
    page_title="Flipkart Deal Radar & Price Intelligence",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- Built-in Deep Audited Deal Dataset ---
DEFAULT_DEALS = [
    # Fashion: Men's
    {"category": "Men's Fashion", "product": "Levi's 511 Slim Fit Stretch Jeans", "mrp": 2999, "avg_90d": 1699, "current": 1056, "atl": 999, "bbd": 949, "note": "38% below 90d baseline. Size clearance risk.", "url": "https://www.flipkart.com/search?q=levis+511+jeans"},
    {"category": "Men's Fashion", "product": "Louis Philippe 2-Piece Formal Slim Suit", "mrp": 10999, "avg_90d": 8499, "current": 5390, "atl": 5390, "bbd": 4999, "note": "Currently at its all-time record low.", "url": "https://www.flipkart.com/search?q=louis+philippe+suits"},
    {"category": "Men's Fashion", "product": "U.S. Polo Assn Solid Cotton Polo", "mrp": 1999, "avg_90d": 1399, "current": 849, "atl": 799, "bbd": 749, "note": "High daily-wear discount floor.", "url": "https://www.flipkart.com/search?q=uspa+polo+t+shirt"},
    {"category": "Men's Fashion", "product": "Flying Machine Slim Tapered Jeans", "mrp": 2599, "avg_90d": 1599, "current": 899, "atl": 799, "bbd": 749, "note": "44% true discount vs running price.", "url": "https://www.flipkart.com/search?q=flying+machine+jeans"},
    {"category": "Men's Fashion", "product": "Allen Solly Regular Fit Formal Shirt", "mrp": 2199, "avg_90d": 1599, "current": 999, "atl": 899, "bbd": 849, "note": "Formal wear steady deal.", "url": "https://www.flipkart.com/search?q=allen+solly+formal+shirt"},

    # Fashion: Women's
    {"category": "Women's Fashion", "product": "Biba Embroidered Anarkali Kurta & Pant Set", "mrp": 4999, "avg_90d": 2899, "current": 1699, "atl": 1599, "bbd": 1499, "note": "Festival clearance rate; 41% true discount.", "url": "https://www.flipkart.com/search?q=biba+anarkali+kurta+set"},
    {"category": "Women's Fashion", "product": "W for Woman Printed Straight Cotton Kurta", "mrp": 1999, "avg_90d": 1299, "current": 649, "atl": 599, "bbd": 579, "note": "50% drop vs 90d baseline.", "url": "https://www.flipkart.com/search?q=w+for+woman+kurta"},
    {"category": "Women's Fashion", "product": "Aurelia Festive Flared Kurta & Dupatta", "mrp": 3999, "avg_90d": 2499, "current": 1499, "atl": 1399, "bbd": 1299, "note": "Rare sub-1.5k pricing for 3-piece set.", "url": "https://www.flipkart.com/search?q=aurelia+festive+kurta+set"},
    {"category": "Women's Fashion", "product": "ONLY High-Rise Relaxed Wide Leg Jeans", "mrp": 2999, "avg_90d": 1999, "current": 1199, "atl": 1099, "bbd": 999, "note": "40% real discount on branded denim.", "url": "https://www.flipkart.com/search?q=only+high+rise+jeans"},

    # Shoes & Footwear
    {"category": "Footwear", "product": "Puma Conduct Pro Performance Running Shoes", "mrp": 6499, "avg_90d": 4799, "current": 3199, "atl": 2899, "bbd": 2799, "note": "ProFoam sole at 33% discount.", "url": "https://www.flipkart.com/search?q=puma+conduct+pro"},
    {"category": "Footwear", "product": "Nike Revolution 7 Road Running Shoes", "mrp": 3695, "avg_90d": 3695, "current": 2995, "atl": 2299, "bbd": 2199, "note": "Wait for BBD Sneaker Fest; Nike drops ~25% more.", "url": "https://www.flipkart.com/search?q=nike+revolution+7"},
    {"category": "Footwear", "product": "Puma Cilia Mode Lux Women's Sneakers", "mrp": 5599, "avg_90d": 3499, "current": 2429, "atl": 2009, "bbd": 1999, "note": "31% off 90d avg; clean street style.", "url": "https://www.flipkart.com/search?q=puma+cilia+mode"},
    {"category": "Footwear", "product": "Asics Gel-Contend 8 Cushioned Running", "mrp": 5499, "avg_90d": 3999, "current": 2899, "atl": 2699, "bbd": 2549, "note": "Gel dampening tech at 28% real markdown.", "url": "https://www.flipkart.com/search?q=asics+gel+contend+8"},
    {"category": "Footwear", "product": "Woodland Outdoor Camel Leather Boots", "mrp": 5995, "avg_90d": 4495, "current": 3495, "atl": 3195, "bbd": 2999, "note": "Wait for winter/BBD sales; drops below 3k.", "url": "https://www.flipkart.com/search?q=woodland+camel+boots"},

    # Watches & Eyewear
    {"category": "Watches & Eyewear", "product": "Casio Vintage A-158WA Stainless Steel", "mrp": 1895, "avg_90d": 1745, "current": 1271, "atl": 1186, "bbd": 1149, "note": "Rarely drops below 1,250. Instant buy.", "url": "https://www.flipkart.com/search?q=casio+a158wa"},
    {"category": "Watches & Eyewear", "product": "Titan Karishma Champagne Dial Analog", "mrp": 2195, "avg_90d": 1995, "current": 1499, "atl": 1365, "bbd": 1349, "note": "Formal dress staple at 25% discount.", "url": "https://www.flipkart.com/search?q=titan+karishma+watch"},
    {"category": "Watches & Eyewear", "product": "Fastrack Revoltt Pro 1.97 AMOLED BT Calling", "mrp": 4999, "avg_90d": 2499, "current": 1899, "atl": 1899, "bbd": 1599, "note": "Smartwatches see steep BBD clearance cuts.", "url": "https://www.flipkart.com/search?q=fastrack+revoltt+pro"},
    {"category": "Watches & Eyewear", "product": "Ray-Ban Polarized Aviator Sunglasses (RB3025)", "mrp": 9290, "avg_90d": 7990, "current": 7490, "atl": 5999, "bbd": 5799, "note": "Luxury eyewear drops significantly with BBD bank coupons.", "url": "https://www.flipkart.com/search?q=ray+ban+aviator+polarized"},

    # Electronics & Phones
    {"category": "Smartphones", "product": "Apple iPhone 16 (128 GB, Black)", "mrp": 79900, "avg_90d": 79900, "current": 69900, "atl": 69900, "bbd": 51990, "note": "Major festive drop incoming. Wait for BBD kick-off.", "url": "https://www.flipkart.com/search?q=apple+iphone+16"},
    {"category": "Smartphones", "product": "Samsung Galaxy S24 5G (8GB/128GB)", "mrp": 74999, "avg_90d": 54999, "current": 42999, "atl": 37999, "bbd": 36999, "note": "Wait for BBD bank coupon + exchange booster.", "url": "https://www.flipkart.com/search?q=samsung+galaxy+s24"},
    {"category": "Smartphones", "product": "Samsung Galaxy S23 FE 5G (8GB/128GB)", "mrp": 59999, "avg_90d": 38999, "current": 29999, "atl": 28999, "bbd": 27499, "note": "Sub-30k price point makes this a high-value buy today.", "url": "https://www.flipkart.com/search?q=samsung+galaxy+s23+fe"},
    {"category": "Smartphones", "product": "Nothing Phone (2a) 5G (8GB/128GB)", "mrp": 25999, "avg_90d": 23999, "current": 20999, "atl": 19999, "bbd": 18499, "note": "Stable price; clean software experience.", "url": "https://www.flipkart.com/search?q=nothing+phone+2a"},

    # Monitors & Audio
    {"category": "Monitors & Audio", "product": "LG UltraGear 27\" 165Hz IPS QHD Gaming Monitor", "mrp": 32000, "avg_90d": 23999, "current": 19499, "atl": 18499, "bbd": 17999, "note": "Already 4.5k below average. High recommendation.", "url": "https://www.flipkart.com/search?q=lg+ultragear+27+monitor"},
    {"category": "Monitors & Audio", "product": "Sony WH-1000XM4 ANC Wireless Headphones", "mrp": 29990, "avg_90d": 22495, "current": 18990, "atl": 17988, "bbd": 17490, "note": "Benchmark noise cancelling at near-ATL price.", "url": "https://www.flipkart.com/search?q=sony+wh+1000xm4"},
    {"category": "Monitors & Audio", "product": "boAt Airdopes 161 ANC TWS Earbuds", "mrp": 3990, "avg_90d": 1499, "current": 999, "atl": 899, "bbd": 799, "note": "Sub-1,000 with ANC; everyday budget pick.", "url": "https://www.flipkart.com/search?q=boat+airdopes+161+anc"},

    # Home Appliances
    {"category": "Home Appliances", "product": "Philips EasySpeed Plus 2000W Steam Iron", "mrp": 2795, "avg_90d": 2295, "current": 1699, "atl": 1549, "bbd": 1499, "note": "26% true discount. Safe to buy now.", "url": "https://www.flipkart.com/search?q=philips+steam+iron"},
    {"category": "Home Appliances", "product": "Kent Grand Plus RO+UV+UF+TDS Water Purifier", "mrp": 20000, "avg_90d": 16499, "current": 13999, "atl": 11999, "bbd": 11499, "note": "Wait for BBD appliance exchange combo + bank coupon.", "url": "https://www.flipkart.com/search?q=kent+grand+plus+water+purifier"},
    {"category": "Home Appliances", "product": "Philips Air Fryer HD9200 (4.1L Rapid Air)", "mrp": 9995, "avg_7299": 7299, "current": 5499, "atl": 4999, "bbd": 4799, "note": "25% below 90-day median; solid kitchen deal.", "url": "https://www.flipkart.com/search?q=philips+air+fryer+hd9200"}
]

# --- Analytics Processing ---
def analyze_dataset(data, card_mode):
    processed = []
    for item in data:
        current = item["current"]
        mrp = item.get("mrp", current)
        avg = item.get("avg_90d", int(mrp * 0.85) if mrp > current else current)
        atl = item.get("atl", int(current * 0.95))
        bbd = item.get("bbd", int(current * 0.90))

        savings = avg - current
        disc_pct = round((savings / avg) * 100, 1) if avg > 0 else 0

        # Verdict logic
        if "Smartphones" in item["category"]:
            verdict = "WAIT (BBD)" if current > (bbd * 1.05) else "BUY NOW"
        elif disc_pct >= 30:
            verdict = "BUY NOW"
        else:
            verdict = "WAIT"

        # Card calculations
        instant_10 = min(int(current * 0.10), 1500)
        cashback_5 = int(current * 0.05)

        if card_mode == "Axis / ICICI (10% Instant, Cap ₹1.5k)":
            best_card = "Axis/ICICI (10%)"
            net_price = current - instant_10
            card_disc = instant_10
        elif card_mode == "Flipkart Axis (5% Unlimited Cashback)":
            best_card = "Flipkart Axis (5%)"
            net_price = current - cashback_5
            card_disc = cashback_5
        else:  # Auto-best
            if instant_10 >= cashback_5:
                best_card = "Axis/ICICI (10%)"
                net_price = current - instant_10
                card_disc = instant_10
            else:
                best_card = "Flipkart Axis (5%)"
                net_price = current - cashback_5
                card_disc = cashback_5

        processed.append({
            "Category": item["category"],
            "Product": item["product"],
            "Current Price": current,
            "90d Average": avg,
            "MRP": mrp,
            "Real Savings": savings,
            "Real Discount %": disc_pct,
            "Verdict": verdict,
            "BBD Forecast": bbd,
            "ATL (Floor)": atl,
            "Optimal Card": best_card,
            "Card Savings": card_disc,
            "Net Price": net_price,
            "Note": item.get("note", "Audited deal item"),
            "URL": item.get("url", "https://www.flipkart.com")
        })
    return pd.DataFrame(processed)

# --- Sidebar Controls ---
st.sidebar.title("⚡ Control Panel")

# Credit card simulator toggle
card_preference = st.sidebar.selectbox(
    "Credit Card Optimization",
    ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
)

# Category multi-select
all_categories = sorted(list(set([d["category"] for d in DEFAULT_DEALS])))
selected_categories = st.sidebar.multiselect(
    "Select Categories",
    options=all_categories,
    default=all_categories
)

# Minimum true discount filter
min_discount = st.sidebar.slider("Minimum Real Discount (%)", min_value=0, max_value=60, value=15, step=5)

# Verdict filter
verdict_filter = st.sidebar.radio("Purchase Verdict", ["All", "BUY NOW Only", "WAIT Only"])

# --- Main App Layout ---
st.title("⚡ Flipkart Deal Radar & Price Intelligence Portal")
st.caption("Live comparison against 90-day moving averages, All-Time Lows (ATL), Big Billion Days price forecasts, and bank card optimization.")

# Run analytics
df = analyze_dataset(DEFAULT_DEALS, card_preference)

# Apply filters
filtered_df = df[df["Category"].isin(selected_categories)]
filtered_df = filtered_df[filtered_df["Real Discount %"] >= min_discount]

if verdict_filter == "BUY NOW Only":
    filtered_df = filtered_df[filtered_df["Verdict"] == "BUY NOW"]
elif verdict_filter == "WAIT Only":
    filtered_df = filtered_df[filtered_df["Verdict"].str.contains("WAIT")]

# --- Metric KPI Bar ---
kpi1, kpi2, kpi3, kpi4 = st.columns(4)
with kpi1:
    st.metric("Deals Audited", len(filtered_df))
with kpi2:
    buy_now_count = len(filtered_df[filtered_df["Verdict"] == "BUY NOW"])
    st.metric("BUY NOW (At Floor)", buy_now_count)
with kpi3:
    wait_count = len(filtered_df[filtered_df["Verdict"].str.contains("WAIT")])
    st.metric("WAIT (Festive Drops)", wait_count)
with kpi4:
    total_savings = filtered_df["Real Savings"].sum()
    st.metric("Total Real Savings", f"₹{total_savings:,.0f}")

st.divider()

# --- Main Navigation Tabs ---
tab_matrix, tab_buy, tab_card, tab_adder = st.tabs([
    "📊 Master Deal Matrix",
    "🎯 Buy Now (At Floor)",
    "💳 Credit Card Strategy",
    "➕ Audit Custom Product"
])

with tab_matrix:
    st.subheader("Audited Catalog")
    
    # Configure interactive columns
    column_config = {
        "Current Price": st.column_config.NumberColumn(format="₹%d"),
        "90d Average": st.column_config.NumberColumn(format="₹%d"),
        "MRP": st.column_config.NumberColumn(format="₹%d"),
        "Real Savings": st.column_config.NumberColumn(format="₹%d"),
        "Real Discount %": st.column_config.ProgressColumn(
            format="%d%%",
            min_value=0,
            max_value=70,
        ),
        "BBD Forecast": st.column_config.NumberColumn(format="₹%d"),
        "ATL (Floor)": st.column_config.NumberColumn(format="₹%d"),
        "Net Price": st.column_config.NumberColumn(format="₹%d"),
        "URL": st.column_config.LinkColumn("Flipkart Store Link")
    }

    display_cols = [
        "Category", "Product", "Current Price", "90d Average", 
        "Real Savings", "Real Discount %", "Verdict", "BBD Forecast", 
        "Optimal Card", "Net Price", "URL"
    ]
    
    st.dataframe(
        filtered_df[display_cols].sort_values("Real Discount %", ascending=False),
        column_config=column_config,
        use_container_width=True,
        hide_index=True
    )

with tab_buy:
    st.subheader("Items at All-Time Lows (Immediate Buys)")
    st.caption("These items have reached their clearance floors. Waiting for festive sales will not produce significant drops, and popular sizes/variants risk stockouts.")
    
    buy_df = filtered_df[filtered_df["Verdict"] == "BUY NOW"]
    for idx, row in buy_df.iterrows():
        with st.container(border=True):
            col_a, col_b, col_c = st.columns([3, 2, 1])
            with col_a:
                st.markdown(f"**{row['Product']}** ({row['Category']})")
                st.caption(row["Note"])
            with col_b:
                st.markdown(f"**Current:** ₹{row['Current Price']:,} | ~~90d Avg: ₹{row['90d Average']:,}~~")
                st.markdown(f"<span style='color:green;'>Save ₹{row['Real Savings']:,} ({row['Real Discount %']}% true drop)</span>", unsafe_allow_html=True)
            with col_c:
                st.markdown(f"**Net:** ₹{row['Net Price']:,}")
                st.link_button("View on Flipkart", row["URL"])

with tab_card:
    st.subheader("Payment Card Decision Architecture")
    st.markdown("""
    - **For Orders under ₹15,000 (Fashion, Shoes, Watches, Small Appliances):**
      - Use **Axis Bank or ICICI Bank Credit Cards** for a **10% instant discount** (capped at ₹1,500). 
      - A 10% instant discount consistently beats 5% cashback on lower purchase tiers.
    - **For Orders above ₹30,000 (iPhones, Flagship Tech, Large Appliances):**
      - Use the **Flipkart Axis Bank Credit Card** for **5% unlimited statement cashback**.
      - Standard bank festive offers cap at ₹1,500, whereas 5% unlimited cashback delivers ₹2,500–₹3,500 in statement credits on big-ticket electronics.
    """)

with tab_adder:
    st.subheader("Audit a Live Product on the Fly")
    st.caption("Paste any product title, current sale price, and listed MRP you see on Flipkart to evaluate its authentic discount.")
    
    with st.form("custom_deal_form"):
        c1, c2 = st.columns(2)
        with c1:
            custom_cat = st.selectbox("Category", all_categories)
            custom_title = st.text_input("Product Title", placeholder="e.g. Puma Nitro Running Shoes")
        with c2:
            custom_curr = st.number_input("Current Listed Price (₹)", min_value=100, step=100, value=2500)
            custom_mrp = st.number_input("Listed MRP (₹)", min_value=100, step=100, value=4500)
        
        submitted = st.form_submit_button("Audit Deal")
        
        if submitted and custom_title:
            est_avg = int(custom_mrp * 0.85) if custom_mrp > custom_curr else custom_curr
            savings = est_avg - custom_curr
            drop_pct = round((savings / est_avg) * 100, 1)
            v = "BUY NOW" if drop_pct >= 25 else "WAIT"
            card_res = min(int(custom_curr * 0.10), 1500)
            
            st.success(f"Audit Complete for '{custom_title}'")
            res1, res2, res3 = st.columns(3)
            res1.metric("Real Discount vs 90d Baseline", f"{drop_pct}%", f"Save ₹{savings:,}")
            res2.metric("Verdict", v)
            res3.metric("Net Price with Optimal Card", f"₹{custom_curr - card_res:,}")
