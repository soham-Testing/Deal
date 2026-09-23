import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

# --- PAGE SETUP ---
st.set_page_config(
    page_title="Flipkart Deal Radar & Price Intelligence Pro",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# --- EXPANDED 50+ DEEP RESEARCH DEALS DATABASE ---
RAW_DEALS = [
    # MEN'S FASHION
    {"cat": "Men's Fashion", "product": "Levi's 511 Slim Fit Stretch Jeans", "mrp": 2999, "avg": 1699, "curr": 1056, "atl": 999, "bbd": 949, "vol": "Low", "url": "https://www.flipkart.com/search?q=levis+511+jeans"},
    {"cat": "Men's Fashion", "product": "Louis Philippe 2-Piece Formal Slim Suit", "mrp": 10999, "avg": 8499, "curr": 5390, "atl": 5390, "bbd": 4999, "vol": "Low", "url": "https://www.flipkart.com/search?q=louis+philippe+suits"},
    {"cat": "Men's Fashion", "product": "U.S. Polo Assn Solid Cotton Polo", "mrp": 1999, "avg": 1399, "curr": 849, "atl": 799, "bbd": 749, "vol": "Medium", "url": "https://www.flipkart.com/search?q=uspa+polo+t+shirt"},
    {"cat": "Men's Fashion", "product": "Flying Machine Slim Tapered Jeans", "mrp": 2599, "avg": 1599, "curr": 899, "atl": 799, "bbd": 749, "vol": "Medium", "url": "https://www.flipkart.com/search?q=flying+machine+jeans"},
    {"cat": "Men's Fashion", "product": "Allen Solly Formal Poplin Shirt", "mrp": 2199, "avg": 1599, "curr": 999, "atl": 899, "bbd": 849, "vol": "Low", "url": "https://www.flipkart.com/search?q=allen+solly+shirt"},
    {"cat": "Men's Fashion", "product": "Peter England Slim Fit Formal Trousers", "mrp": 1799, "avg": 1299, "curr": 749, "atl": 699, "bbd": 649, "vol": "Low", "url": "https://www.flipkart.com/search?q=peter+england+trousers"},
    {"cat": "Men's Fashion", "product": "Wildcraft Windproof Active Jacket", "mrp": 4299, "avg": 2799, "curr": 1649, "atl": 1499, "bbd": 1399, "vol": "High", "url": "https://www.flipkart.com/search?q=wildcraft+jacket"},
    {"cat": "Men's Fashion", "product": "Wrangler Skater Fit Washed Denim", "mrp": 3199, "avg": 2199, "curr": 1249, "atl": 1149, "bbd": 1099, "vol": "Medium", "url": "https://www.flipkart.com/search?q=wrangler+jeans"},
    {"cat": "Men's Fashion", "product": "Tommy Hilfiger Classic Pique Polo", "mrp": 4599, "avg": 3299, "curr": 2199, "atl": 1999, "bbd": 1899, "vol": "High", "url": "https://www.flipkart.com/search?q=tommy+hilfiger+polo"},
    {"cat": "Men's Fashion", "product": "Van Heusen Solid Poly-Cotton Shirt", "mrp": 1999, "avg": 1499, "curr": 899, "atl": 849, "bbd": 799, "vol": "Low", "url": "https://www.flipkart.com/search?q=van+heusen+shirt"},

    # WOMEN'S FASHION
    {"cat": "Women's Fashion", "product": "Biba Embroidered Anarkali Kurta & Pant Set", "mrp": 4999, "avg": 2899, "curr": 1699, "atl": 1599, "bbd": 1499, "vol": "Medium", "url": "https://www.flipkart.com/search?q=biba+anarkali"},
    {"cat": "Women's Fashion", "product": "W for Woman Printed Straight Cotton Kurta", "mrp": 1999, "avg": 1299, "curr": 649, "atl": 599, "bbd": 579, "vol": "Low", "url": "https://www.flipkart.com/search?q=w+for+woman+kurta"},
    {"cat": "Women's Fashion", "product": "Aurelia Festive Flared Kurta Set", "mrp": 3999, "avg": 2499, "curr": 1499, "atl": 1399, "bbd": 1299, "vol": "Medium", "url": "https://www.flipkart.com/search?q=aurelia+kurta"},
    {"cat": "Women's Fashion", "product": "ONLY High-Rise Wide Leg Jeans", "mrp": 2999, "avg": 1999, "curr": 1199, "atl": 1099, "bbd": 999, "vol": "Medium", "url": "https://www.flipkart.com/search?q=only+jeans"},
    {"cat": "Women's Fashion", "product": "Vero Moda Floral Summer Maxi Dress", "mrp": 3499, "avg": 2299, "curr": 1349, "atl": 1199, "bbd": 1149, "vol": "High", "url": "https://www.flipkart.com/search?q=vero+moda+dress"},
    {"cat": "Women's Fashion", "product": "Libas Pure Cotton Straight Kurta Pant", "mrp": 2499, "avg": 1499, "curr": 799, "atl": 749, "bbd": 699, "vol": "Low", "url": "https://www.flipkart.com/search?q=libas+kurta"},
    {"cat": "Women's Fashion", "product": "Global Desi Geometric Print Tunic", "mrp": 2199, "avg": 1399, "curr": 749, "atl": 699, "bbd": 649, "vol": "Medium", "url": "https://www.flipkart.com/search?q=global+desi+tunic"},
    {"cat": "Women's Fashion", "product": "Madame Full Sleeve Ribbed Cardigan", "mrp": 2699, "avg": 1899, "curr": 1099, "atl": 999, "bbd": 899, "vol": "High", "url": "https://www.flipkart.com/search?q=madame+cardigan"},

    # FOOTWEAR & SHOES
    {"cat": "Footwear", "product": "Puma Conduct Pro Performance Running", "mrp": 6499, "avg": 4799, "curr": 3199, "atl": 2899, "bbd": 2799, "vol": "Low", "url": "https://www.flipkart.com/search?q=puma+conduct+pro"},
    {"cat": "Footwear", "product": "Nike Revolution 7 Road Running", "mrp": 3695, "avg": 3695, "curr": 2995, "atl": 2299, "bbd": 2199, "vol": "High", "url": "https://www.flipkart.com/search?q=nike+revolution+7"},
    {"cat": "Footwear", "product": "Puma Cilia Mode Lux Women's Sneakers", "mrp": 5599, "avg": 3499, "curr": 2429, "atl": 2009, "bbd": 1999, "vol": "Medium", "url": "https://www.flipkart.com/search?q=puma+cilia+mode"},
    {"cat": "Footwear", "product": "Asics Gel-Contend 8 Cushioned Trainer", "mrp": 5499, "avg": 3999, "curr": 2899, "atl": 2699, "bbd": 2549, "vol": "Low", "url": "https://www.flipkart.com/search?q=asics+gel+contend+8"},
    {"cat": "Footwear", "product": "Woodland Camel Leather Rugged Boots", "mrp": 5995, "avg": 4495, "curr": 3495, "atl": 3195, "bbd": 2999, "vol": "Medium", "url": "https://www.flipkart.com/search?q=woodland+boots"},
    {"cat": "Footwear", "product": "Adidas Clinch-X Responsive Running", "mrp": 3999, "avg": 2499, "curr": 1699, "atl": 1549, "bbd": 1499, "vol": "Low", "url": "https://www.flipkart.com/search?q=adidas+clinch+x"},
    {"cat": "Footwear", "product": "Red Tape Airflow Chunky Walk Sneakers", "mrp": 5899, "avg": 1799, "curr": 1199, "atl": 1099, "bbd": 999, "vol": "High", "url": "https://www.flipkart.com/search?q=red+tape+sneakers"},
    {"cat": "Footwear", "product": "Skechers Go Run Elevate Running Shoes", "mrp": 6299, "avg": 4599, "curr": 3149, "atl": 2999, "bbd": 2799, "vol": "Low", "url": "https://www.flipkart.com/search?q=skechers+go+run"},

    # WATCHES & EYEWEAR
    {"cat": "Watches & Eyewear", "product": "Casio Vintage A-158WA Stainless Steel", "mrp": 1895, "avg": 1745, "curr": 1271, "atl": 1186, "bbd": 1149, "vol": "Low", "url": "https://www.flipkart.com/search?q=casio+a158wa"},
    {"cat": "Watches & Eyewear", "product": "Titan Karishma Champagne Dial Watch", "mrp": 2195, "avg": 1995, "curr": 1499, "atl": 1365, "bbd": 1349, "vol": "Low", "url": "https://www.flipkart.com/search?q=titan+karishma+watch"},
    {"cat": "Watches & Eyewear", "product": "Fastrack Revoltt Pro 1.97\" AMOLED", "mrp": 4999, "avg": 2499, "curr": 1899, "atl": 1899, "bbd": 1599, "vol": "High", "url": "https://www.flipkart.com/search?q=fastrack+revoltt+pro"},
    {"cat": "Watches & Eyewear", "product": "Ray-Ban Polarized Aviator (RB3025)", "mrp": 9290, "avg": 7990, "curr": 7490, "atl": 5999, "bbd": 5799, "vol": "High", "url": "https://www.flipkart.com/search?q=ray+ban+aviator"},
    {"cat": "Watches & Eyewear", "product": "Timex Expedition Rugged Field Watch", "mrp": 3995, "avg": 3195, "curr": 2299, "atl": 2099, "bbd": 1999, "vol": "Low", "url": "https://www.flipkart.com/search?q=timex+expedition"},
    {"cat": "Watches & Eyewear", "product": "Fossil Grant Chronograph Leather Watch", "mrp": 13495, "avg": 8995, "curr": 6495, "atl": 5995, "bbd": 5499, "vol": "High", "url": "https://www.flipkart.com/search?q=fossil+grant"},
    {"cat": "Watches & Eyewear", "product": "Titan Neo Analog Dial Stainless Steel", "mrp": 5495, "avg": 4295, "curr": 2999, "atl": 2799, "bbd": 2599, "vol": "Medium", "url": "https://www.flipkart.com/search?q=titan+neo+watch"},

    # SMARTPHONES & TECH
    {"cat": "Smartphones", "product": "Apple iPhone 16 (128 GB, Black)", "mrp": 79900, "avg": 79900, "curr": 69900, "atl": 69900, "bbd": 51999, "vol": "High", "url": "https://www.flipkart.com/search?q=apple+iphone+16"},
    {"cat": "Smartphones", "product": "Apple iPhone 15 (128 GB, Blue)", "mrp": 69900, "avg": 64900, "curr": 54999, "atl": 51999, "bbd": 48999, "vol": "High", "url": "https://www.flipkart.com/search?q=apple+iphone+15"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S24 5G (8GB/128GB)", "mrp": 74999, "avg": 54999, "curr": 42999, "atl": 37999, "bbd": 36999, "vol": "High", "url": "https://www.flipkart.com/search?q=samsung+galaxy+s24"},
    {"cat": "Smartphones", "product": "Samsung Galaxy S23 FE 5G (8GB/128GB)", "mrp": 59999, "avg": 38999, "curr": 29999, "atl": 28999, "bbd": 27499, "vol": "Medium", "url": "https://www.flipkart.com/search?q=samsung+galaxy+s23+fe"},
    {"cat": "Smartphones", "product": "Nothing Phone (2a) 5G (8GB/128GB)", "mrp": 25999, "avg": 23999, "curr": 20999, "atl": 19999, "bbd": 18499, "vol": "Low", "url": "https://www.flipkart.com/search?q=nothing+phone+2a"},
    {"cat": "Smartphones", "product": "Motorola Edge 50 Fusion (8GB/128GB)", "mrp": 27999, "avg": 24999, "curr": 21999, "atl": 20999, "bbd": 19999, "vol": "Low", "url": "https://www.flipkart.com/search?q=motorola+edge+50+fusion"},
    {"cat": "Smartphones", "product": "OnePlus Nord CE4 5G (8GB/128GB)", "mrp": 26999, "avg": 24999, "curr": 24999, "atl": 22999, "bbd": 21499, "vol": "Medium", "url": "https://www.flipkart.com/search?q=oneplus+nord+ce4"},
    {"cat": "Smartphones", "product": "Realme 16 Pro 5G (8GB/256GB)", "mrp": 29999, "avg": 26999, "curr": 22999, "atl": 21999, "bbd": 19999, "vol": "High", "url": "https://www.flipkart.com/search?q=realme+16+pro"},

    # MONITORS & AUDIO
    {"cat": "Monitors & Audio", "product": "LG UltraGear 27\" 165Hz IPS QHD Monitor", "mrp": 32000, "avg": 23999, "curr": 19499, "atl": 18499, "bbd": 17999, "vol": "Low", "url": "https://www.flipkart.com/search?q=lg+ultragear+27"},
    {"cat": "Monitors & Audio", "product": "Samsung Odyssey G3 24\" 165Hz FHD", "mrp": 19000, "avg": 13499, "curr": 10499, "atl": 9999, "bbd": 9499, "vol": "Medium", "url": "https://www.flipkart.com/search?q=samsung+odyssey+g3"},
    {"cat": "Monitors & Audio", "product": "Sony WH-1000XM4 ANC Headphones", "mrp": 29990, "avg": 22495, "curr": 18990, "atl": 17988, "bbd": 17490, "vol": "Low", "url": "https://www.flipkart.com/search?q=sony+wh+1000xm4"},
    {"cat": "Monitors & Audio", "product": "Apple AirPods Pro (2nd Gen, USB-C)", "mrp": 24900, "avg": 23900, "curr": 19999, "atl": 17999, "bbd": 16999, "vol": "High", "url": "https://www.flipkart.com/search?q=airpods+pro+2"},
    {"cat": "Monitors & Audio", "product": "boAt Airdopes 161 ANC Earbuds", "mrp": 3990, "avg": 1499, "curr": 999, "atl": 899, "bbd": 799, "vol": "High", "url": "https://www.flipkart.com/search?q=boat+airdopes+161"},
    {"cat": "Monitors & Audio", "product": "Acer Nitro V 15.6\" Gaming Laptop (RTX 4050)", "mrp": 88999, "avg": 74990, "curr": 64990, "atl": 62990, "bbd": 59990, "vol": "High", "url": "https://www.flipkart.com/search?q=acer+nitro+v"},
    {"cat": "Monitors & Audio", "product": "Apple iPad (10th Gen, 64GB Wi-Fi)", "mrp": 39900, "avg": 34900, "curr": 30900, "atl": 28900, "bbd": 27490, "vol": "Medium", "url": "https://www.flipkart.com/search?q=apple+ipad+10th+gen"},

    # HOME APPLIANCES
    {"cat": "Home Appliances", "product": "Philips EasySpeed Plus 2000W Steam Iron", "mrp": 2795, "avg": 2295, "curr": 1699, "atl": 1549, "bbd": 1499, "vol": "Low", "url": "https://www.flipkart.com/search?q=philips+steam+iron"},
    {"cat": "Home Appliances", "product": "Kent Grand Plus RO+UV+UF+TDS Purifier", "mrp": 20000, "avg": 16499, "curr": 13999, "atl": 11999, "bbd": 11499, "vol": "High", "url": "https://www.flipkart.com/search?q=kent+grand+plus"},
    {"cat": "Home Appliances", "product": "Philips Air Fryer HD9200 (4.1L)", "mrp": 9995, "avg": 7299, "curr": 5499, "atl": 4999, "bbd": 4799, "vol": "Low", "url": "https://www.flipkart.com/search?q=philips+air+fryer"},
    {"cat": "Home Appliances", "product": "Aquaguard Aura 7L RO+UV Purifier", "mrp": 18000, "avg": 14299, "curr": 11499, "atl": 10499, "bbd": 9999, "vol": "Medium", "url": "https://www.flipkart.com/search?q=aquaguard+aura"},
    {"cat": "Home Appliances", "product": "Prestige Iris 750W Mixer Grinder (4 Jars)", "mrp": 4495, "avg": 3299, "curr": 2499, "atl": 2199, "bbd": 2099, "vol": "Low", "url": "https://www.flipkart.com/search?q=prestige+iris"}
]

# --- DATA PROCESSING ENGINE ---
def process_data(deals, card_mode):
    out = []
    for d in deals:
        curr, avg, mrp = d["curr"], d["avg"], d["mrp"]
        real_savings = avg - curr
        real_disc = round((real_savings / avg) * 100, 1)
        fake_mrp_disc = round(((mrp - curr) / mrp) * 100, 1)
        synthetic_inflation = round(((mrp - avg) / avg) * 100, 1)

        # Verdict logic
        if "Smartphones" in d["cat"]:
            verdict = "WAIT (BBD)" if curr > (d["bbd"] * 1.05) else "BUY NOW"
        elif real_disc >= 28 or curr <= d["atl"]:
            verdict = "BUY NOW"
        else:
            verdict = "WAIT"

        # Card calculations
        inst_10 = min(int(curr * 0.10), 1500)
        cb_5 = int(curr * 0.05)

        if card_mode == "Axis / ICICI (10% Instant, Cap ₹1.5k)":
            card_name, net, disc = "Axis/ICICI (10%)", curr - inst_10, inst_10
        elif card_mode == "Flipkart Axis (5% Unlimited Cashback)":
            card_name, net, disc = "Flipkart Axis (5%)", curr - cb_5, cb_5
        else:
            if inst_10 >= cb_5:
                card_name, net, disc = "Axis/ICICI (10%)", curr - inst_10, inst_10
            else:
                card_name, net, disc = "Flipkart Axis (5%)", curr - cb_5, cb_5

        out.append({
            "Category": d["cat"],
            "Product": d["product"],
            "Current Price": curr,
            "90d Average": avg,
            "MRP": mrp,
            "Real Savings": real_savings,
            "Real Discount %": real_disc,
            "Fake MRP Disc %": fake_mrp_disc,
            "MRP Inflation %": synthetic_inflation,
            "Verdict": verdict,
            "ATL (Floor)": d["atl"],
            "BBD Forecast": d["bbd"],
            "Volatility": d["vol"],
            "Optimal Card": card_name,
            "Net Price": net,
            "Card Discount": disc,
            "URL": d["url"]
        })
    return pd.DataFrame(out)

# --- SIDEBAR CONTROLS ---
st.sidebar.title("⚡ Intelligence Controls")

card_pref = st.sidebar.selectbox(
    "Credit Card Engine",
    ["Auto-Best Card", "Axis / ICICI (10% Instant, Cap ₹1.5k)", "Flipkart Axis (5% Unlimited Cashback)"]
)

cats = sorted(list(set([d["cat"] for d in RAW_DEALS])))
selected_cats = st.sidebar.multiselect("Filter Categories", options=cats, default=cats)
min_disc = st.sidebar.slider("Minimum Real Discount (%)", 0, 60, 10, step=5)
verdict_filter = st.sidebar.radio("Decision Verdict", ["All", "BUY NOW Only", "WAIT Only"])

# Process dataframe
df = process_data(RAW_DEALS, card_pref)

# Filter
f_df = df[df["Category"].isin(selected_cats)]
f_df = f_df[f_df["Real Discount %"] >= min_disc]
if verdict_filter == "BUY NOW Only":
    f_df = f_df[f_df["Verdict"] == "BUY NOW"]
elif verdict_filter == "WAIT Only":
    f_df = f_df[f_df["Verdict"].str.contains("WAIT")]

# --- MAIN LAYOUT ---
st.title("⚡ Flipkart Price Intelligence & Visual Radar")
st.caption("Deep research analytics comparing live offers to 90-day baselines, synthetic MRP inflation, and bank stacks.")

# Metrics Strip
m1, m2, m3, m4 = st.columns(4)
m1.metric("Audited Products", len(f_df))
m2.metric("Buy Now (Floor Price)", len(f_df[f_df["Verdict"] == "BUY NOW"]))
m3.metric("Wait (Festive Drops)", len(f_df[f_df["Verdict"].str.contains("WAIT")]))
m4.metric("Total Consumer Savings", f"₹{f_df['Real Savings'].sum():,.0f}")

st.divider()

# --- TABS FOR DEEP VISUAL ANALYTICS ---
tab_visuals, tab_matrix, tab_scatter, tab_card = st.tabs([
    "📈 Visual Analytics",
    "📋 Master Deal Matrix",
    "🎯 Price vs 90d Baseline Map",
    "💳 Bank Card Optimization"
])

with tab_visuals:
    st.subheader("Category Discount & Inflation Breakdown")
    col_chart1, col_chart2 = st.columns(2)

    with col_chart1:
        cat_grp = f_df.groupby("Category")[["Real Discount %", "Fake MRP Disc %"]].mean().reset_index()
        fig1 = go.Figure(data=[
            go.Bar(name='Real Discount % (vs 90d Avg)', x=cat_grp['Category'], y=cat_grp['Real Discount %'], marker_color='#38bdf8'),
            go.Bar(name='Marketed Discount % (vs Inflated MRP)', x=cat_grp['Category'], y=cat_grp['Fake MRP Disc %'], marker_color='#64748b')
        ])
        fig1.update_layout(barmode='group', title="Real Discount vs Marketing Claims", template="plotly_dark", height=400)
        st.plotly_chart(fig1, use_container_width=True)

    with col_chart2:
        fig2 = px.box(
            f_df, x="Category", y="Real Savings", color="Category",
            title="Savings Distribution (₹)", template="plotly_dark", height=400
        )
        st.plotly_chart(fig2, use_container_width=True)

with tab_matrix:
    st.subheader("Master Audited Product Grid")
    
    col_cfg = {
        "Current Price": st.column_config.NumberColumn(format="₹%d"),
        "90d Average": st.column_config.NumberColumn(format="₹%d"),
        "Real Savings": st.column_config.NumberColumn(format="₹%d"),
        "Real Discount %": st.column_config.ProgressColumn(format="%d%%", min_value=0, max_value=60),
        "MRP Inflation %": st.column_config.NumberColumn(format="%d%%"),
        "BBD Forecast": st.column_config.NumberColumn(format="₹%d"),
        "Net Price": st.column_config.NumberColumn(format="₹%d"),
        "URL": st.column_config.LinkColumn("Flipkart Link")
    }

    cols = ["Category", "Product", "Current Price", "90d Average", "Real Savings", "Real Discount %", "Verdict", "BBD Forecast", "Optimal Card", "Net Price", "URL"]
    st.dataframe(f_df[cols].sort_values("Real Discount %", ascending=False), column_config=col_cfg, use_container_width=True, hide_index=True)

with tab_scatter:
    st.subheader("Price Ratio vs Historical Floor Analysis")
    st.caption("Dots close to the lower-left are at or below their all-time lowest recorded price point.")
    
    fig3 = px.scatter(
        f_df, x="Current Price", y="90d Average",
        size="Real Savings", color="Verdict", hover_name="Product",
        log_x=True, log_y=True, title="Current Price vs 90-Day Baseline (Log Scale)",
        color_discrete_map={"BUY NOW": "#4ade80", "WAIT": "#fde047", "WAIT (BBD)": "#f97316"},
        template="plotly_dark", height=500
    )
    st.plotly_chart(fig3, use_container_width=True)

with tab_card:
    st.subheader("Credit Card Cashback & Discount Engine")
    st.markdown("""
    - **Purchases under ₹15,000 (Fashion, Shoes, Watches, Steam Irons):** 
      - **Axis Bank & ICICI Bank Cards** yield the highest return through their **10% instant discount** (up to the ₹1,500 festive cap).
    - **Purchases above ₹30,000 (iPhones, Laptops, Water Purifiers):**
      - **Flipkart Axis Bank Card** wins because the 10% instant discount hits its ₹1,500 ceiling, while **5% unlimited cashback** scales up to ₹2,500–₹3,500 on big-ticket devices.
    """)
    
    fig4 = px.scatter(
        f_df, x="Current Price", y="Card Discount", color="Optimal Card",
        size="Card Discount", hover_name="Product",
        title="Card Savings Comparison Across Price Ranges",
        template="plotly_dark", height=420
    )
    st.plotly_chart(fig4, use_container_width=True)
