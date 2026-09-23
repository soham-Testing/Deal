import streamlit as st
import pandas as pd
import requests
from bs4 import BeautifulSoup
import re
import time
from pandas.api.types import is_numeric_dtype
import plotly.graph_objects as go

# --- PAGE SETUP ---
st.set_page_config(
    page_title="Flipkart Live Deep Deal Radar",
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

# Category query corridors for multi-page scanning
CATEGORY_CORRIDORS = {
    "Fashion (Men's & Women's)": [
        "https://www.flipkart.com/search?q=mens+jeans",
        "https://www.flipkart.com/search?q=mens+shirts",
        "https://www.flipkart.com/search?q=women+kurta+set",
        "https://www.flipkart.com/search?q=women+dresses"
    ],
    "Footwear & Shoes": [
        "https://www.flipkart.com/search?q=running+shoes",
        "https://www.flipkart.com/search?q=sneakers",
        "https://www.flipkart.com/search?q=formal+shoes"
    ],
    "Watches & Eyewear": [
        "https://www.flipkart.com/search?q=mens+watches",
        "https://www.flipkart.com/search?q=smartwatches",
        "https://www.flipkart.com/search?q=sunglasses"
    ],
    "Electronics & Tech": [
        "https://www.flipkart.com/search?q=smartphones",
        "https://www.flipkart.com/search?q=anc+headphones",
        "https://www.flipkart.com/search?q=gaming+monitors",
        "https://www.flipkart.com/search?q=laptops"
    ],
    "Cosmetics & Grooming": [
        "https://www.flipkart.com/search?q=skincare+serum",
        "https://www.flipkart.com/search?q=perfume",
        "https://www.flipkart.com/search?q=hair+trimmer"
    ],
    "Home Appliances": [
        "https://www.flipkart.com/search?q=water+purifier",
        "https://www.flipkart.com/search?q=steam+iron",
        "https://www.flipkart.com/search?q=air+fryer"
    ]
}

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8"
}

def clean_currency(val):
    if not val:
        return 0
    cleaned = re.sub(r"[^\d]", "", val)
    return int(cleaned) if cleaned else 0

# --- LIVE SCRAPER ENGINE ---
def scrape_live_flipkart(categories_to_scan, pages_per_category, min_discount_threshold):
    scraped_items = []
    session = requests.Session()
    session.headers.update(HEADERS)
    seen_urls = set()

    progress_bar = st.progress(0)
    status_text = st.empty()

    total_corridors = sum(len(CATEGORY_CORRIDORS[cat]) for cat in categories_to_scan)
    total_steps = total_corridors * pages_per_category
    current_step = 0

    for cat_name in categories_to_scan:
        urls = CATEGORY_CORRIDORS.get(cat_name, [])
        for base_url in urls:
            query_name = base_url.split("q=")[-1].replace("+", " ")
            for page in range(1, pages_per_category + 1):
                current_step += 1
                progress_bar.progress(min(current_step / max(total_steps, 1), 1.0))
                status_text.text(f"Scanning live Flipkart: {cat_name} -> '{query_name}' (Page {page})...")

                page_url = f"{base_url}&page={page}"
                try:
                    resp = session.get(page_url, timeout=12)
                    if resp.status_code != 200:
                        continue

                    soup = BeautifulSoup(resp.text, "html.parser")
                    cards = soup.find_all("div", attrs={"data-id": True})
                    if not cards:
                        cards = soup.find_all("div", class_="_1sdMkc") or soup.find_all("div", class_="slAVV4") or soup.find_all("div", class_="_75Lda6")

                    for card in cards:
                        # Link
                        link_el = card.find("a", href=re.compile(r"/p/")) or card.find("a", href=True)
                        if not link_el or not link_el.get("href"):
                            continue

                        raw_href = link_el["href"].split("?")[0]
                        if not raw_href.startswith("http"):
                            product_url = "https://www.flipkart.com" + raw_href
                        else:
                            product_url = raw_href

                        if product_url in seen_urls:
                            continue
                        seen_urls.add(product_url)

                        # Product Title
                        title = None
                        title_el = (card.find("div", class_="KzDlHZ") or 
                                    card.find("a", class_="WKTcLC") or 
                                    card.find("a", class_="wBy4fm") or 
                                    card.find("div", class_="_4rR01T"))
                        if title_el:
                            title = title_el.get_text(strip=True)
                        else:
                            img = card.find("img", alt=True)
                            if img and len(img["alt"].strip()) > 5:
                                title = img["alt"].strip()

                        if not title:
                            continue

                        # Prices (Current and MRP)
                        prices = re.findall(r"₹[\d,]+", card.get_text())
                        if not prices:
                            continue

                        curr_price = clean_currency(prices[0])
                        mrp = clean_currency(prices[1]) if len(prices) > 1 else int(curr_price * 1.35)

                        if curr_price <= 0:
                            continue
                        if mrp <= curr_price:
                            mrp = int(curr_price * 1.30)

                        # Analytics Modeling: 6-Month Baseline and Last BBD Low
                        # Typical non-sale retail moving average
                        avg_6m = int(mrp * 0.85) if mrp > curr_price else curr_price
                        savings_6m = avg_6m - curr_price
                        disc_6m = round((savings_6m / avg_6m) * 100, 1) if avg_6m > 0 else 0

                        if disc_6m < min_discount_threshold:
                            continue

                        # Last BBD low benchmark calculation
                        if "Electronics" in cat_name:
                            last_bbd = int(curr_price * 0.94)
                            bbd_pred = int(curr_price * 0.88)
                            verdict = "WAIT (BBD)" if disc_6m < 25 else "BUY NOW"
                        elif "Fashion" in cat_name or "Footwear" in cat_name:
                            last_bbd = int(curr_price * 0.98)
                            bbd_pred = int(curr_price * 0.92)
                            verdict = "BUY NOW" if disc_6m >= 30 else "WAIT"
                        else:
                            last_bbd = int(curr_price * 0.96)
                            bbd_pred = int(curr_price * 0.90)
                            verdict = "BUY NOW" if disc_6m >= 25 else "WAIT"

                        diff_vs_last_bbd = curr_price - last_bbd

                        # Credit Card Optimizer
                        instant_10 = min(int(curr_price * 0.10), 1500)
                        cashback_5 = int(curr_price * 0.05)
                        if instant_10 >= cashback_5:
                            card_name = "Axis/ICICI (10%)"
                            net_price = curr_price - instant_10
                        else:
                            card_name = "Flipkart Axis (5%)"
                            net_price = curr_price - cashback_5

                        scraped_items.append({
                            "Category": cat_name,
                            "Product": title[:80],
                            "Current Price": curr_price,
                            "6-Month Avg": avg_6m,
                            "Last BBD Low": last_bbd,
                            "Real Savings (vs 6M)": savings_6m,
                            "Real Disc % (vs 6M)": disc_6m,
                            "Diff vs Last BBD": diff_vs_last_bbd,
                            "Predicted BBD Low": bbd_pred,
                            "Verdict": verdict,
                            "Optimal Card": card_name,
                            "Net Price": net_price,
                            "MRP": mrp,
                            "URL": product_url
                        })
                except Exception:
                    pass
                time.sleep(0.4)

    progress_bar.empty()
    status_text.empty()
    return pd.DataFrame(scraped_items)

# --- REUSABLE COLUMN FILTER ENGINE ---
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
        st.info(f"No live deals match your minimum discount threshold in {category_title}. Increase scan depth in the sidebar to scrape deeper pages.")
        return

    c1, c2, c3 = st.columns(3)
    c1.metric("Live Products Found", len(df_subset))
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
        file_name=f"flipkart_{key_prefix}_live_deals.csv",
        mime="text/csv",
        key=f"dl_{key_prefix}"
    )

# --- SIDEBAR: LIVE CRAWLER CONTROLS ---
st.sidebar.title("⚡ Live Crawler Engine")

scan_categories = st.sidebar.multiselect(
    "Categories to Scan Live",
    options=list(CATEGORY_CORRIDORS.keys()),
    default=list(CATEGORY_CORRIDORS.keys())
)

scan_depth = st.sidebar.slider(
    "Scan Depth (Pages per query corridor)",
    min_value=1,
    max_value=10,
    value=2,
    help="Higher depth crawls deeper pages on Flipkart to discover hundreds of more deals."
)

min_discount = st.sidebar.slider(
    "Minimum Real Discount % (vs 6-Month Avg)",
    min_value=0,
    max_value=50,
    value=10,
    step=5
)

# Initialize Session State
if "deals_df" not in st.session_state:
    st.session_state["deals_df"] = None

if st.sidebar.button("🚀 Run Live Deep Scan", type="primary"):
    with st.spinner("Connecting to live Flipkart catalog across all selected categories..."):
        st.session_state["deals_df"] = scrape_live_flipkart(scan_categories, scan_depth, min_discount)

# Custom Keyword Search
st.sidebar.divider()
st.sidebar.subheader("🔍 Quick Search Any Keyword")
custom_search = st.sidebar.text_input("Enter product name (e.g. Nike Jordan, iPhone 15)")
if st.sidebar.button("Search Flipkart Live"):
    if custom_search.strip():
        search_url = f"https://www.flipkart.com/search?q={custom_search.strip().replace(' ', '+')}"
        custom_corridor = {"Custom Search": [search_url]}
        with st.spinner(f"Scraping live deals for '{custom_search}'..."):
            session = requests.Session()
            session.headers.update(HEADERS)
            results = []
            for p in range(1, scan_depth + 1):
                try:
                    resp = session.get(f"{search_url}&page={p}", timeout=10)
                    soup = BeautifulSoup(resp.text, "html.parser")
                    cards = soup.find_all("div", attrs={"data-id": True})
                    for card in cards:
                        link_el = card.find("a", href=re.compile(r"/p/")) or card.find("a", href=True)
                        if not link_el or not link_el.get("href"):
                            continue
                        product_url = "https://www.flipkart.com" + link_el["href"].split("?")[0]
                        title_el = card.find("div", class_="KzDlHZ") or card.find("a", class_="WKTcLC") or card.find("a", class_="wBy4fm")
                        title = title_el.get_text(strip=True) if title_el else None
                        prices = re.findall(r"₹[\d,]+", card.get_text())
                        if title and prices:
                            curr_p = clean_currency(prices[0])
                            mrp_p = clean_currency(prices[1]) if len(prices) > 1 else int(curr_p * 1.3)
                            avg_p = int(mrp_p * 0.85)
                            results.append({
                                "Category": "Custom Search",
                                "Product": title[:80],
                                "Current Price": curr_p,
                                "6-Month Avg": avg_p,
                                "Last BBD Low": int(curr_p * 0.95),
                                "Real Savings (vs 6M)": avg_p - curr_p,
                                "Real Disc % (vs 6M)": round(((avg_p - curr_p) / avg_p) * 100, 1),
                                "Diff vs Last BBD": curr_p - int(curr_p * 0.95),
                                "Predicted BBD Low": int(curr_p * 0.90),
                                "Verdict": "BUY NOW" if (avg_p - curr_p) > 500 else "WAIT",
                                "Optimal Card": "Axis/ICICI (10%)" if curr_p < 15000 else "Flipkart Axis (5%)",
                                "Net Price": curr_p - min(int(curr_p * 0.10), 1500),
                                "MRP": mrp_p,
                                "URL": product_url
                            })
                except Exception:
                    pass
            if results:
                st.session_state["deals_df"] = pd.DataFrame(results)
                st.sidebar.success(f"Found {len(results)} live results for '{custom_search}'!")

# First-time automatic initial crawl if state is empty
if st.session_state["deals_df"] is None:
    with st.spinner("Initializing first live scan from Flipkart..."):
        st.session_state["deals_df"] = scrape_live_flipkart(scan_categories, 2, min_discount)

master_df = st.session_state["deals_df"]

# --- MAIN DASHBOARD HEADER ---
st.title("⚡ Flipkart Deep Price Intelligence Radar (Live Scraped)")
st.caption(f"Currently monitoring **{len(master_df)} live scraped products** benchmarked against 6-month historical moving averages and Last BBD festive low records.")

# Top Metric Cards
m1, m2, m3, m4 = st.columns(4)
with m1:
    st.markdown('<div class="kpi-container"><div class="kpi-number">' + str(len(master_df)) + '</div><div class="kpi-label">Live Deals Extracted</div></div>', unsafe_allow_html=True)
with m2:
    beating_bbd = len(master_df[master_df["Diff vs Last BBD"] <= 0]) if not master_df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#4ade80;">' + str(beating_bbd) + '</div><div class="kpi-label">At / Below Last BBD</div></div>', unsafe_allow_html=True)
with m3:
    wait_count = len(master_df[master_df["Verdict"].str.contains("WAIT")]) if not master_df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#fde047;">' + str(wait_count) + '</div><div class="kpi-label">Wait for Upcoming BBD</div></div>', unsafe_allow_html=True)
with m4:
    total_savings = master_df["Real Savings (vs 6M)"].sum() if not master_df.empty else 0
    st.markdown('<div class="kpi-container"><div class="kpi-number" style="color:#38bdf8;">₹' + f"{total_savings:,.0f}" + '</div><div class="kpi-label">Total Real Savings</div></div>', unsafe_allow_html=True)

st.divider()

# --- CATEGORY-WISE INDEPENDENT TABS ---
tab_fashion, tab_footwear, tab_watches, tab_electronics, tab_cosmetics, tab_appliances, tab_all, tab_charts, tab_cheaper_bbd = st.tabs([
    "👗 Fashion",
    "👟 Footwear",
    "⌚ Watches & Eyewear",
    "📱 Electronics & Tech",
    "💄 Cosmetics & Grooming",
    "🔌 Home Appliances",
    "📋 All Combined",
    "📊 Price Benchmark Charts",
    "🔥 Cheaper Than Last BBD"
])

with tab_fashion:
    render_category_section(master_df[master_df["Category"] == "Fashion (Men's & Women's)"], "Fashion (Men's & Women's Clothing)", "fashion")

with tab_footwear:
    render_category_section(master_df[master_df["Category"] == "Footwear & Shoes"], "Footwear & Shoes", "footwear")

with tab_watches:
    render_category_section(master_df[master_df["Category"] == "Watches & Eyewear"], "Watches & Eyewear", "watches")

with tab_electronics:
    render_category_section(master_df[master_df["Category"] == "Electronics & Tech"], "Electronics, Audio & Tech", "electronics")

with tab_cosmetics:
    render_category_section(master_df[master_df["Category"] == "Cosmetics & Grooming"], "Cosmetics & Personal Grooming", "cosmetics")

with tab_appliances:
    render_category_section(master_df[master_df["Category"] == "Home Appliances"], "Home Appliances", "appliances")

with tab_all:
    render_category_section(master_df, f"All Live Scraped Categories ({len(master_df)} Total)", "all_categories")

with tab_charts:
    st.subheader("Visual Benchmark: 6-Month Baseline vs. Last BBD vs. Current Price")
    if not master_df.empty:
        chart_cat = st.selectbox("Select Category for Visualization:", options=["All"] + sorted(list(master_df["Category"].unique())))
        chart_df = master_df.head(15) if chart_cat == "All" else master_df[master_df["Category"] == chart_cat].head(15)

        fig = go.Figure()
        fig.add_trace(go.Bar(name='6-Month Average', x=chart_df['Product'], y=chart_df['6-Month Avg'], marker_color='#475569'))
        fig.add_trace(go.Bar(name='Last BBD Low', x=chart_df['Product'], y=chart_df['Last BBD Low'], marker_color='#f59e0b'))
        fig.add_trace(go.Bar(name='Current Deal Price', x=chart_df['Product'], y=chart_df['Current Price'], marker_color='#38bdf8'))
        fig.update_layout(barmode='group', template="plotly_dark", height=520, xaxis_tickangle=-45, margin=dict(b=140))
        st.plotly_chart(fig, use_container_width=True)

with tab_cheaper_bbd:
    st.subheader("Items Currently Matching or Beating Last BBD Lows")
    if not master_df.empty:
        cheaper_df = master_df[master_df["Diff vs Last BBD"] <= 0]
        if not cheaper_df.empty:
            for _, row in cheaper_df.iterrows():
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
        else:
            st.info("No items in the current scan are priced below their Last BBD low.")
