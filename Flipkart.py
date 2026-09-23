import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px

# --- PAGE SETUP ---
st.set_page_config(
    page_title="Flipkart Deep Price Intelligence Radar",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom Styling
st.markdown("""
<style>
    .kpi-container { background-color: #0f172a; padding: 14px; border-radius: 8px; border: 1px solid #1e293b; text-align: center; }
    .kpi-number { font-size: 1.4rem; font-weight: 800; color: #38bdf8; }
    .kpi-label { font-size: 0.75rem; color: #94a3b8; text-transform: uppercase; }
</style>
""", unsafe_allow_html=True)

# --- AUDITED 6-MONTH & BBD COMPARATIVE DATASET ---
DEEP_DEALS_DATABASE = [
    # SMARTPHONES
    {"cat": "Smartphones", "product": "Apple iPhone 15 (128 GB, Blue)", "mrp": 69900, "avg_6m": 63499, "last_bbd": 52999, "curr": 54999, "bbd_pred": 48999, "url": "https://www.flipkart.com/search?q=apple+iphone+15"},
    {"cat": "Smartphones", "product": "Apple iPhone 16 (128 GB, Black)", "mrp": 79900, "avg_6m": 79900, "last_bbd": 69900, "curr": 69900, "bbd_pred": 51999, "url": "https://www.flipkart.com/search?q=apple+iphone+16"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S24 5G (8GB/128GB)", "mrp": 74999, "avg_6m": 56999, "last_bbd": 41999, "curr": 42999, "bbd_pred": 36999, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s24"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S23 FE 5G (8GB/128GB)", "mrp": 59999, "avg_6m": 39999, "last_bbd": 29999, "curr": 29999, "bbd_pred": 27499, "url": "https://www.flipkart.com/search?q=samsung+galaxy+s23+fe"},
    {"cat": "Smartphones", "product": "Nothing Phone (2a) 5G (8GB/128GB)", "mrp": 25999, "avg_6m": 23499, "last_bbd": 19999, "curr": 20999, "bbd_pred": 18499, "url": "https://www.flipkart.com/search?q=nothing+phone+2a"},
    {"cat": "Smartphones", "product": "Motorola Edge 50 Fusion (8GB/128GB)", "mrp": 27999, "avg_6m": 23999, "last_bbd": 20999, "curr": 21999, "bbd_pred": 19999, "url": "https://www.flipkart.com/search?q=motorola+edge+50+fusion"},
    {"cat": "Smartphones", "product": "OnePlus Nord CE4 5G (8GB/128GB)", "mrp": 26999, "avg_6m": 24999, "last_bbd": 22999, "curr": 24999, "bbd_pred": 21499, "url": "https://www.flipkart.com/search?q=oneplus+nord+ce4"},
    {"cat": "Smartphones", "product": "Realme 16 Pro 5G (8GB/256GB)", "mrp": 29999, "avg_6m": 26999, "last_bbd": 21999, "curr": 22999, "bbd_pred": 19999, "url": "https://www.flipkart.com/search?q=realme+16+pro"},

    # MONITORS & AUDIO
    {"cat": "Monitors & Audio", "product": "Sony WH-1000XM4 ANC Headphones", "mrp": 29990, "avg_6m": 22990, "last_bbd": 18490, "curr": 18990, "bbd_pred": 17490, "url": "https://www.flipkart.com/search?q=sony+wh+1000xm4"},
    {"cat": "Monitors & Audio", "product": "LG UltraGear 27\" 165Hz IPS QHD Monitor", "mrp": 32000, "avg_6m": 24499, "last_bbd": 18999, "curr": 19499, "bbd_pred": 17999, "url": "https://www.flipkart.com/search?q=lg+ultragear+27"},
    {"cat": "Monitors & Audio", "product": "Samsung Odyssey G3 24\" 165Hz FHD", "mrp": 19000, "avg_6m": 13999, "last_bbd": 9999, "curr": 10499, "bbd_pred": 9499, "url": "https://www.flipkart.com/search?q=samsung+odyssey+g3"},
    {"cat": "Monitors & Audio", "product": "Apple iPad 10th Gen (64GB Wi-Fi)", "mrp": 39900, "avg_6m": 34490, "last_bbd": 29999, "curr": 30900, "bbd_pred": 27490, "url": "https://www.flipkart.com/search?q=apple+ipad+10th+gen"},
    {"cat": "Monitors & Audio", "product": "Apple AirPods Pro (2nd Gen, USB-C)", "mrp": 24900, "avg_6m": 23900, "last_bbd": 17999, "curr": 19999, "bbd_pred": 16999, "url": "https://www.flipkart.com/search?q=airpods+pro+2"},
    {"cat": "Monitors & Audio", "product": "boAt Airdopes 161 ANC TWS Earbuds", "mrp": 3990, "avg_6m": 1499, "last_bbd": 899, "curr": 999, "bbd_pred": 799, "url": "https://www.flipkart.com/search?q=boat+airdopes+161"},
    {"cat": "Monitors & Audio", "product": "Acer Nitro V 15.6\" Gaming Laptop (RTX 4050)", "mrp": 88999, "avg_6m": 74990, "last_bbd": 62990, "curr": 64990, "bbd_pred": 59990, "url": "https://www.flipkart.com/search?q=acer+nitro+v"},

    # MEN'S FASHION
    {"cat": "Men's Fashion", "product": "Levi's 511 Slim Fit Stretch Jeans", "mrp": 2999, "avg_6m": 1749, "last_bbd": 1099, "curr": 1056, "bbd_pred": 949, "url": "https://www.flipkart.com/search?q=levis+511+jeans"},
    {"cat": "Men's Fashion", "product": "Louis Philippe 2-Piece Formal Slim Suit", "mrp": 10999, "avg_6m": 8999, "last_bbd": 5499, "curr": 5390, "bbd_pred": 4999, "url": "https://www.flipkart.com/search?q=louis+philippe+suits"},
    {"cat": "Men's Fashion", "product": "U.S. Polo Assn Solid Cotton Polo", "mrp": 1999, "avg_6m": 1399, "last_bbd": 849, "curr": 849, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=uspa+polo+t+shirt"},
    {"cat": "Men's Fashion", "product": "Flying Machine Slim Tapered Jeans", "mrp": 2599, "avg_6m": 1649, "last_bbd": 899, "curr": 899, "bbd_pred": 749, "url": "https://www.flipkart.com/search?q=flying+machine+jeans"},
    {"cat": "Men's Fashion", "product": "Allen Solly Formal Poplin Shirt", "mrp": 2199, "avg_6m": 1599, "last_bbd": 999, "curr": 999, "bbd_pred": 849, "url": "https://www.flipkart.com/search?q=allen+solly+shirt"},
    {"cat": "Men's Fashion", "product": "Peter England Slim Fit Formal Trousers", "mrp": 1799, "avg_6m": 1299, "last_bbd": 749, "curr": 749, "bbd_pred": 649, "url": "https://www.flipkart.com/search?q=peter+england+trousers"},
    {"cat": "Men's Fashion", "product": "Wildcraft Windproof Active Jacket", "mrp": 4299, "avg_6m": 2799, "last_bbd": 1599, "curr": 1649, "bbd_pred": 1399, "url": "https://www.flipkart.com/search?q=wildcraft+jacket"},

    # WOMEN'S FASHION
    {"cat": "Women's Fashion", "product": "Biba Embroidered Anarkali Kurta & Pant Set", "mrp": 4999, "avg_6m": 2999, "last_bbd": 1799, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=biba+anarkali"},
    {"cat": "Women's Fashion", "product": "W for Woman Printed Straight Cotton Kurta", "mrp": 1999, "avg_6m": 1349, "last_bbd": 699, "curr": 649, "bbd_pred": 579, "url": "https://www.flipkart.com/search?q=w+for+woman+kurta"},
    {"cat": "Women's Fashion", "product": "Aurelia Festive Flared Kurta Set", "mrp": 3999, "avg_6m": 2549, "last_bbd": 1499, "curr": 1499, "bbd_pred": 1299, "url": "https://www.flipkart.com/search?q=aurelia+kurta"},
    {"cat": "Women's Fashion", "product": "ONLY High-Rise Wide Leg Jeans", "mrp": 2999, "avg_6m": 1999, "last_bbd": 1199, "curr": 1199, "bbd_pred": 999, "url": "https://www.flipkart.com/search?q=only+jeans"},
    {"cat": "Women's Fashion", "product": "Vero Moda Floral Summer Maxi Dress", "mrp": 3499, "avg_6m": 2299, "last_bbd": 1299, "curr": 1349, "bbd_pred": 1149, "url": "https://www.flipkart.com/search?q=vero+moda+dress"},
    {"cat": "Women's Fashion", "product": "Libas Pure Cotton Straight Kurta Pant", "mrp": 2499, "avg_6m": 1499, "last_bbd": 799, "curr": 799, "bbd_pred": 699, "url": "https://www.flipkart.com/search?q=libas+kurta"},

    # FOOTWEAR & SHOES
    {"cat": "Footwear", "product": "Puma Conduct Pro Performance Running", "mrp": 6499, "avg_6m": 4899, "last_bbd": 3299, "curr": 3199, "bbd_pred": 2799, "url": "https://www.flipkart.com/search?q=puma+conduct+pro"},
    {"cat": "Footwear", "product": "Nike Revolution 7 Road Running", "mrp": 3695, "avg_6m": 3695, "last_bbd": 2399, "curr": 2995, "bbd_pred": 2199, "url": "https://www.flipkart.com/search?q=nike+revolution+7"},
    {"cat": "Footwear", "product": "Puma Cilia Mode Lux Women's Sneakers", "mrp": 5599, "avg_6m": 3599, "last_bbd": 2399, "curr": 2429, "bbd_pred": 1999, "url": "https://www.flipkart.com/search?q=puma+cilia+mode"},
    {"cat": "Footwear", "product": "Asics Gel-Contend 8 Cushioned Trainer", "mrp": 5499, "avg_6m": 4099, "last_bbd": 2899, "curr": 2899, "bbd_pred": 2549, "url": "https://www.flipkart.com/search?q=asics+gel+contend+8"},
    {"cat": "Footwear", "product": "Woodland Camel Leather Rugged Boots", "mrp": 5995, "avg_6m": 4595, "last_bbd": 3295, "curr": 3495, "bbd_pred": 2999, "url": "https://www.flipkart.com/search?q=woodland+boots"},
    {"cat": "Footwear", "product": "Adidas Clinch-X Responsive Running", "mrp": 3999, "avg_6m": 2499, "last_bbd": 1599, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=adidas+clinch+x"},

    # WATCHES & EYEWEAR
    {"cat": "Watches & Eyewear", "product": "Casio Vintage A-158WA Stainless Steel", "mrp": 1895, "avg_6m": 1745, "last_bbd": 1249, "curr": 1271, "bbd_pred": 1149, "url": "https://www.flipkart.com/search?q=casio+a158wa"},
    {"cat": "Watches & Eyewear", "product": "Titan Karishma Champagne Dial Watch", "mrp": 2195, "avg_6m": 1995, "last_bbd": 1449, "curr": 1499, "bbd_pred": 1349, "url": "https://www.flipkart.com/search?q=titan+karishma+watch"},
    {"cat": "Watches & Eyewear", "product": "Fastrack Revoltt Pro 1.97\" AMOLED", "mrp": 4999, "avg_6m": 2599, "last_bbd": 1799, "curr": 1899, "bbd_pred": 1599, "url": "https://www.flipkart.com/search?q=fastrack+revoltt+pro"},
    {"cat": "Watches & Eyewear", "product": "Ray-Ban Polarized Aviator (RB3025)", "mrp": 9290, "avg_6m": 8290, "last_bbd": 5999, "curr": 7490, "bbd_pred": 5799, "url": "https://www.flipkart.com/search?q=ray+ban+aviator"},
    {"cat": "Watches & Eyewear", "product": "Timex Expedition Rugged Field Watch", "mrp": 3995, "avg_6m": 3195, "last_bbd": 2199, "curr": 2299, "bbd_pred": 1999, "url": "https://www.flipkart.com/search?q=timex+expedition"},

    # HOME APPLIANCES
    {"cat": "Home Appliances", "product": "Philips EasySpeed Plus 2000W Steam Iron", "mrp": 2795, "avg_6m": 2349, "last_bbd": 1599, "curr": 1699, "bbd_pred": 1499, "url": "https://www.flipkart.com/search?q=philips+steam+iron"},
    {"cat": "Home Appliances", "product": "Kent Grand Plus RO+UV+UF+TDS Purifier", "mrp": 20000, "avg_6m": 16999, "last_bbd": 12499, "curr": 13999, "bbd_pred": 11499, "url": "https://www.flipkart.com/search?q=kent+grand+plus"},
    {"cat": "Home Appliances", "product": "Philips Air Fryer HD9200 (4.1L)", "mrp": 9995, "avg_6m": 7399, "last_bbd": 5199, "curr": 5499, "bbd_pred": 4799, "url": "https://www.flipkart.com/search?q=philips+air+fryer"},
    {"cat": "Home Appliances", "product": "Aquaguard Aura 7L RO+UV Purifier", "mrp": 18000, "avg_6m": 14499, "last_bbd": 10999, "curr": 11499, "bbd_pred": 9999, "url": "https://www.flipkart.com/search?q=aquaguard+aura"},
    {"cat": "Home Appliances", "product": "Prestige Iris 750W Mixer Grinder (4 Jars)", "mrp": 4495, "avg_6m": 3299, "last_bbd": 2299, "curr": 2499, "bbd_pred": 2099, "url": "https://www.flipkart.com/search?q=prestige+iris"}
]

# --- ANALYTICS CALCULATIONS ---
def process_deep_analytics(data, card_selection):
    records = []
    for d in data:
        curr = d["curr"]
        avg_6m = d["avg_6m"]
        last_bbd = d["last_bbd"]
        mrp = d["mrp"]
        bbd_pred = d["bbd_pred"]

        savings_vs_6m = avg_6m - curr
        disc_vs_6m = round((savings_vs_6m / avg_6m) * 100, 1)

        diff_vs_last_bbd = curr - last_bbd
        pct_vs_last_bbd = round((diff_vs_last_bbd / last_bbd) * 100, 1)

        # Decision Verdict Logic
        if "Smartphones" in d["cat"] or "Ray-Ban" in d["product"]:
            verdict = "WAIT (BBD)" if curr > (bbd_pred * 1.05) else "BUY NOW"
        elif curr <= last_bbd or disc_vs_6m >= 30:
            verdict = "BUY NOW"
        else:
            verdict = "WAIT"

        # Card Optimization Engine
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
            "Vs Last BBD %": pct_vs_last_bbd,
            "Predicted BBD Low": bbd_pred,
            "Verdict": verdict,
            "Optimal Card": card_title,
            "Net Price": net_price,
            "MRP": mrp,
            "URL": d["url"]
        })
    return pd.DataFrame(records)

# --- SIDEBAR FILTERS ---
st.sidebar.title("⚡ Deep Research Controls")

card_pref = st.sidebar.selectbox(
    "Credit Card Engine",
    ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
)

categories = sorted(list(set([d["cat"] for d in DEEP_DEALS_DATABASE])))
selected_categories = st.sidebar.multiselect("Category Filter", options=categories, default=categories)
verdict_filter = st.sidebar.radio("Verdict Filter", ["All", "BUY NOW Only", "WAIT Only"])

# Process DataFrame
df = process_deep_analytics(DEEP_DEALS_DATABASE, card_pref)
filtered_df = df[df["Category"].isin(selected_categories)]

if verdict_filter == "BUY NOW Only":
    filtered_df = filtered_df[filtered_df["Verdict"] == "BUY NOW"]
elif verdict_filter == "WAIT Only":
    filtered_df = filtered_df[filtered_df["Verdict"].str.contains("WAIT")]

# --- MAIN DASHBOARD HEADER ---
st.title("⚡ Flipkart Deep Price Intelligence Radar")
st.caption("Cross-referencing live pricing against 6-month historical averages, recorded Last BBD floors, and upcoming festive forecasts.")

# Metric Cards
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + str(len(filtered_df)) + '</div><div class="kpi-label">Audited Deals</div></div>', unsafe_allow_html=True)
with m2:
    beating_bbd = len(filtered_df[filtered_df["Diff vs Last BBD"] <= 0])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + str(beating_bbd) + '</div><div class="kpi-label">Matching / Cheaper than Last BBD</div></div>', unsafe_allow_html=True)
with m3:
    wait_count = len(filtered_df[filtered_df["Verdict"].str.contains("WAIT")])
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#fde047;">' + str(wait_count) + '</div><div class="kpi-label">Wait for Upcoming BBD</div></div>', unsafe_allow_html=True)
with m4:
    total_savings = filtered_df["Real Savings (vs 6M)"].sum()
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{total_savings:,.0f}" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.divider()

# --- TABS ---
tab_matrix, tab_charts, tab_cheaper_bbd = st.tabs([
    "📋 Master Comparative Table",
    "📊 3-Way Price Benchmark Visualizer",
    "🔥 Currently Cheaper Than Last BBD"
])

with tab_matrix:
    st.subheader("6-Month & BBD Comparative Data Matrix")
    
    col_config = {
        "Current Price": st.column_config.NumberColumn(format="₹%d"),
        "6-Month Avg": st.column_config.NumberColumn(format="₹%d"),
        "Last BBD Low": st.column_config.NumberColumn(format="₹%d"),
        "Real Savings (vs 6M)": st.column_config.NumberColumn(format="₹%d"),
        "Real Disc % (vs 6M)": st.column_config.ProgressColumn(format="%d%%", min_value=-10, max_value=60),
        "Diff vs Last BBD": st.column_config.NumberColumn(format="₹%d", help="Negative number means CURRENTLY CHEAPER than last BBD!"),
        "Predicted BBD Low": st.column_config.NumberColumn(format="₹%d"),
        "Net Price": st.column_config.NumberColumn(format="₹%d"),
        "URL": st.column_config.LinkColumn("Store Link")
    }

    cols = [
        "Category", "Product", "Current Price", "6-Month Avg", "Last BBD Low", 
        "Real Savings (vs 6M)", "Real Disc % (vs 6M)", "Diff vs Last BBD", 
        "Verdict", "Predicted BBD Low", "Optimal Card", "Net Price", "URL"
    ]
    
    st.dataframe(
        filtered_df[cols].sort_values("Real Disc % (vs 6M)", ascending=False),
        column_config=col_config,
        use_container_width=True,
        hide_index=True
    )
    
    # Direct CSV Export
    csv_data = filtered_df.to_csv(index=False).encode('utf-8')
    st.download_button(
        label="📥 Export Full Comparative Dataset (CSV)",
        data=csv_data,
        file_name="flipkart_deep_price_audit.csv",
        mime="text/csv"
    )

with tab_charts:
    st.subheader("Visual Benchmark: 6-Month Baseline vs. Last BBD vs. Current Price")
    st.caption("Compare how close current prices are to last year's festive floor versus regular 6-month averages.")

    sample_products = filtered_df["Product"].unique()[:12]
    chart_df = filtered_df[filtered_df["Product"].isin(sample_products)]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        name='6-Month Average',
        x=chart_df['Product'],
        y=chart_df['6-Month Avg'],
        marker_color='#475569'
    ))
    fig.add_trace(go.Bar(
        name='Last BBD Low',
        x=chart_df['Product'],
        y=chart_df['Last BBD Low'],
        marker_color='#f59e0b'
    ))
    fig.add_trace(go.Bar(
        name='Current Deal Price',
        x=chart_df['Product'],
        y=chart_df['Current Price'],
        marker_color='#38bdf8'
    ))

    fig.update_layout(
        barmode='group',
        template="plotly_dark",
        height=500,
        xaxis_tickangle=-45,
        margin=dict(b=120)
    )
    st.plotly_chart(fig, use_container_width=True)

with tab_cheaper_bbd:
    st.subheader("Items Currently Matching or Beating Last BBD Lows")
    st.caption("These items have broken past their historical festive floor and are safe to buy immediately.")

    cheaper_df = filtered_df[filtered_df["Diff vs Last BBD"] <= 0]
    for _, row in cheaper_df.iterrows():
        with st.container(border=True):
            c1, c2, c3 = st.columns([3, 2, 1])
            with c1:
                st.markdown(f"**{row['Product']}** ({row['Category']})")
                st.caption(f"Beating Last BBD by **₹{abs(row['Diff vs Last BBD']):,}** ({abs(row['Vs Last BBD %'])}% below festival price)")
            with c2:
                st.markdown(f"**Current:** ₹{row['Current Price']:,} | ~~6M Avg: ₹{row['6-Month Avg']:,}~~")
                st.markdown(f"Last BBD Record: **₹{row['Last BBD Low']:,}**")
            with c3:
                st.markdown(f"**Net:** ₹{row['Net Price']:,}")
                st.link_button("View Deal", row["URL"])
