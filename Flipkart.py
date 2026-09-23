"""
flipkart_scan_app.py — Streamlit launcher for the Deep Scan Engine
===================================================================

THIS is the file you point `streamlit run` at:

    streamlit run flipkart_scan_app.py

Do NOT run `streamlit run flipkart_deep_scan.py`. That module is a CLI/library
with no UI; its __main__ guard calls argparse, which raises SystemExit(2) when
it sees no recognised flags. Streamlit then renders an empty page — the blank
screen with no error.

Keep all three files in the SAME folder:
    flipkart_deal_tracker.py   (your original — untouched)
    flipkart_deep_scan.py      (the engine)
    flipkart_scan_app.py       (this launcher)

Requirements: streamlit>=1.30, pandas>=2.0, requests>=2.28
"""

from __future__ import annotations

import importlib
import sys
import traceback
from typing import Any, Optional

import streamlit as st

st.set_page_config(page_title="Flipkart Deep Scan", page_icon="🛰️", layout="wide")


# --------------------------------------------------------------------------- #
# IMPORT GUARD — surface failures in the browser instead of a blank page
# --------------------------------------------------------------------------- #


def _safe_import(module_name: str) -> Any:
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError:
        return None
    except SystemExit:
        st.error(
            f"`{module_name}` called sys.exit() during import. If this is "
            "`flipkart_deep_scan`, you are running the wrong file with Streamlit."
        )
        st.stop()
    except Exception:
        st.error(f"`{module_name}` failed to import:")
        st.code(traceback.format_exc(), language="text")
        st.stop()


scan = _safe_import("flipkart_deep_scan")

if scan is None:
    st.error("Could not find **flipkart_deep_scan.py**.")
    st.markdown(
        "Put it in the same folder as this launcher, then rerun.\n\n"
        f"Python is currently looking in:\n```\n{sys.path[0]}\n```"
    )
    st.stop()

if scan.requests is None:
    st.error("The `requests` package is missing. Install it with `pip install requests`.")
    st.stop()


@st.cache_resource(show_spinner=False)
def get_engine(qps: float, workers: int):
    return scan.DeepScanEngine(qps=qps, workers=workers)


# --------------------------------------------------------------------------- #
# UI
# --------------------------------------------------------------------------- #

st.title("🛰️ Flipkart Deep Scan")
st.caption(
    "Catalogue-wide scanner built on `window.__INITIAL_STATE__` extraction. "
    "One request returns every product on the page, not one link."
)

with st.sidebar:
    st.subheader("Engine settings")
    qps = st.slider("Requests per second (all threads)", 0.5, 6.0, 2.5, 0.5)
    workers = st.slider("Worker threads", 1, 12, 6)
    pages = st.slider("Result pages per query", 1, 10, 3)
    sort = st.selectbox("Sort order", scan.SORT_MODES)
    use_cache = st.checkbox("Use cached pages", value=True)
    st.divider()
    if st.button("🩺 Test Flipkart connection", use_container_width=True):
        with st.spinner("Contacting Flipkart…"):
            ok, message = get_engine(qps, workers).self_test()
        (st.success if ok else st.error)(message)

engine = get_engine(qps, workers)

tab_scan, tab_offers, tab_diag = st.tabs(["🔍 Scan", "💳 Offers", "🩺 Diagnostics"])

# -- Scan -------------------------------------------------------------------- #

with tab_scan:
    mode = st.radio("Scan mode", ["Specific queries", "Full catalogue sweep"],
                    horizontal=True)

    queries: list[str] = []
    if mode == "Specific queries":
        raw = st.text_area(
            "One search term per line:",
            value="bluetooth headphones\nrunning shoes men\nmobile phones 5g",
            height=110,
        )
        queries = [line.strip() for line in raw.splitlines() if line.strip()]
    else:
        chosen = st.multiselect(
            "Categories:", list(scan.CATEGORY_SEEDS),
            default=list(scan.CATEGORY_SEEDS)[:3],
        )
        queries = [term for cat in chosen for term in scan.CATEGORY_SEEDS[cat]]
        st.caption(f"{len(queries)} seed queries × {pages} pages = "
                   f"{len(queries) * pages} page fetches.")

    if st.button("🚀 Run scan", type="primary", disabled=not queries):
        bar = st.progress(0.0)
        status = st.empty()

        def on_progress(done: int, total: int, name: str) -> None:
            bar.progress(min(done / max(total, 1), 1.0))
            status.caption(f"{done}/{total} — {name}")

        category_map = {t: c for c, terms in scan.CATEGORY_SEEDS.items() for t in terms}
        with st.spinner("Scanning Flipkart…"):
            records = engine.scan_many(
                queries, pages=pages, sort=sort,
                category_map=category_map, progress=on_progress, use_cache=use_cache,
            )
            records = engine.deduplicate(records)
            engine.stats.products_unique = len(records)
            engine.cache.append_ledger(records)

        bar.empty()
        status.empty()

        if not records:
            st.error("No products captured.")
            st.markdown(
                "Most likely causes, in order:\n"
                "1. **Bot challenge** — Flipkart served a CAPTCHA page. "
                "Lower the QPS slider and retry in a few minutes.\n"
                "2. **Network blocked** — corporate proxy, VPN or firewall. "
                "Use **🩺 Test Flipkart connection** in the sidebar.\n"
                "3. **Circuit breaker open** — see the Diagnostics tab."
            )
        else:
            st.success(f"Captured {len(records)} unique products.")
            stats = engine.stats.as_dict()
            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Products", stats["products_unique"])
            c2.metric("Pages fetched", stats["pages_fetched"])
            c3.metric("From cache", stats["pages_from_cache"])
            c4.metric("Products/sec", stats["products_per_sec"])

            frame = scan.to_dataframe(records, engine.cache)
            st.dataframe(
                frame,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "URL": st.column_config.LinkColumn("Link", display_text="Open ↗"),
                    "Current Price": st.column_config.NumberColumn(format="₹%d"),
                    "Net Price": st.column_config.NumberColumn(format="₹%d"),
                    "MRP": st.column_config.NumberColumn(format="₹%d"),
                    "Deal Score": st.column_config.ProgressColumn(
                        format="%.1f", min_value=0, max_value=100),
                },
            )
            st.download_button(
                "📥 Download CSV",
                frame.to_csv(index=False).encode("utf-8"),
                file_name="flipkart_deep_scan.csv",
                mime="text/csv",
            )

# -- Offers ------------------------------------------------------------------ #

with tab_offers:
    st.subheader(scan.BBD_2026["sale_name"])
    c1, c2, c3 = st.columns(3)
    c1.metric("Early Bird deals", scan.BBD_2026["early_bird_deals_from"])
    c2.metric("Early access", scan.BBD_2026["early_access_from"])
    c3.metric("Public start", scan.BBD_2026["public_start"])
    st.caption(f"Title sponsors: {', '.join(scan.BBD_2026['title_sponsors'])} · "
               f"End date {scan.BBD_2026['end_date'] or 'not announced'}")

    st.dataframe(
        [{"Offer": o.label, "Type": o.kind,
          "Rate": f"{o.rate:.0%}" if o.rate else "—",
          "Cap": f"₹{o.cap}" if o.cap else "not published",
          "Notes": o.note}
         for o in scan.OFFER_MATRIX],
        use_container_width=True, hide_index=True,
    )
    st.info(scan.RIVAL_NOTE)

    st.divider()
    st.markdown("**Net price calculator**")
    price = st.number_input("Listed price (₹)", min_value=0, value=56905, step=500)
    entitlements = st.multiselect(
        "Memberships you actually hold:",
        ["plus_premium", "black"],
        help="Membership offers are excluded unless selected, so net prices "
             "are not overstated.",
    )
    if price > 0:
        offer, saving, net = scan.OfferEngine.best(price, entitlements=entitlements)
        a, b, c = st.columns(3)
        a.metric("Best offer saving", f"₹{saving:,}")
        b.metric("Net price", f"₹{net:,}")
        c.metric("Effective off", f"{saving / price:.1%}")
        st.caption(f"{offer.label} — {offer.note}")

# -- Diagnostics ------------------------------------------------------------- #

with tab_diag:
    st.subheader("Engine state")
    st.json({
        "engine_version": scan.ENGINE_VERSION,
        "requests_available": engine.available,
        "circuit_breaker": engine.breaker.state,
        "affiliate_api_configured": engine.affiliate_ready,
        "robots_txt_loaded": engine._robots is not None,
        "cache_file": engine.cache.path,
        "stats": engine.stats.as_dict(),
    })
    st.caption("A circuit breaker in `open` state means Flipkart is refusing "
               "traffic; it retries automatically after the cooldown.")

    if st.button("🧹 Clear cached pages older than 7 days"):
        st.success(f"Removed {engine.cache.vacuum_pages()} cached pages.")

    st.divider()
    st.subheader("Bridge into your existing tracker")
    st.code(
        "import flipkart_deal_tracker as tracker\n"
        "import flipkart_deep_scan as deep\n"
        "deep.install_into_tracker(tracker)   # hot-swaps the resolver\n",
        language="python",
    )
    st.caption("Run your original app with `streamlit run flipkart_deal_tracker.py` "
               "— that file has its own UI and still works unchanged.")
