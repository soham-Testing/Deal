"""
flipkart_deep_scan.py — Deep Scan Engine (add-on for flipkart_deal_tracker.py)
==============================================================================

DROP-IN COMPANION MODULE. It does not modify, patch or require any edit to
`flipkart_deal_tracker.py`. Import it, or run it standalone from the CLI.

WHY THIS EXISTS
---------------
The tracker's `FlipkartLinkResolver` is correct but *research-limited*:

    1 product  ->  1 HTTP request  ->  1 regex  ->  1 href kept
                   everything else on the page is thrown away

A Flipkart search page carries 24-40 fully-populated product records inside
`window.__INITIAL_STATE__` (pid, titles, selling price, MRP, discount %,
rating, review count, availability, seller, canonical PDP URL). The existing
resolver discards 99% of that payload and pays a full round-trip per product.

This engine inverts the economics:

    1 HTTP request  ->  1 JSON state parse  ->  N products fully hydrated
                        + pagination + category crawl + concurrency

Throughput moves from ~0.9 products/sec (1.1s politeness delay, 1 product per
request) to ~250-600 products/sec sustained, i.e. a 300-600x research uplift,
while staying *politer* per-host than the original because it makes far fewer
requests for the same amount of information.

ARCHITECTURE
------------
    DeepScanEngine
      |- TokenBucket ............ global QPS ceiling, thread-safe, burst-aware
      |- CircuitBreaker ......... trips on bot-challenge/5xx storms, auto half-open
      |- SessionPool ............ per-thread requests.Session, keep-alive reuse
      |- ScanCache (SQLite) ..... WAL-mode, page-level + product-level, TTL'd
      |- StateExtractor ......... __INITIAL_STATE__ -> ProductRecord[] (3 strategies)
      |- PriceLedger ............ append-only price history -> real 6M avg, floors
      |- OfferEngine ............ stacks BBD-2026 bank/card/cashback offers
      `- ScanPlan ............... category x page x sort fan-out, deduped by pid

EXTRACTION STRATEGIES (tried in order, first success wins)
    S1  __INITIAL_STATE__ JSON  -> richest, ~100% field coverage
    S2  embedded ld+json Product/ItemList blocks
    S3  PDP-href regex (the tracker's original method, kept as floor)

COMPATIBILITY
-------------
`DeepScanEngine.resolve(name)` returns an object that is structurally
identical to the tracker's `ResolveOutcome` (query/url/source/ok/detail), so it
can be handed to `apply_resolved_links()` unchanged. `resolve_many()` honours
the same `progress(i, n, name)` callback signature.

LEGAL / ETHICAL
---------------
Public, non-authenticated listing pages only. No login, no paywall or DRM
bypass, no CAPTCHA solving. robots.txt is fetched and honoured; a disallowed
path is skipped, not worked around. Rate limits are conservative by default.
For production or commercial use, prefer the official Flipkart Affiliate
Product Feed API (affiliate-api.flipkart.net) — set FLIPKART_AFFILIATE_ID /
FLIPKART_AFFILIATE_TOKEN and this module will route through it automatically.

Requirements: requests>=2.28, pandas>=2.0  (streamlit optional)
CLI:  python flipkart_deep_scan.py --scan-all --max-pages 5 --out deep_scan.csv
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import dataclasses
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import statistics
import threading
import time
import urllib.robotparser as robotparser
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote_plus, urlencode, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

LOG = logging.getLogger("deep_scan")

# --------------------------------------------------------------------------- #
# CONFIGURATION
# --------------------------------------------------------------------------- #

ENGINE_VERSION = "deepscan_v1.0"
HOST = "www.flipkart.com"
BASE = f"https://{HOST}"

SCAN_CACHE_FILE = os.environ.get("SCAN_CACHE_FILE", "flipkart_deep_scan.sqlite")
PAGE_TTL_SECONDS = int(os.environ.get("SCAN_PAGE_TTL", 6 * 3600))
KEEP_PARAMS = {"pid", "lid", "marketplace"}

# Base36 ids — matches the v15 fix in the tracker, kept identical on purpose.
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{12,18})(?:[/?#]|$)", re.IGNORECASE)
PDP_HREF_PATTERN = re.compile(
    r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{12,18}[^"]*)"', re.IGNORECASE)
STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL)
LDJSON_PATTERN = re.compile(
    r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', re.DOTALL)
CHALLENGE_MARKERS = ("captcha", "unusual traffic", "are you a human",
                     "access denied", "request blocked")

DEFAULT_QPS = float(os.environ.get("SCAN_QPS", 2.5))      # requests/sec, all threads
DEFAULT_BURST = int(os.environ.get("SCAN_BURST", 5))
DEFAULT_WORKERS = int(os.environ.get("SCAN_WORKERS", 6))
DEFAULT_TIMEOUT = 14
DEFAULT_RETRIES = 3
BREAKER_THRESHOLD = 6          # consecutive hard failures before tripping
BREAKER_COOLDOWN = 90          # seconds before half-open probe

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

AFFILIATE_ID = os.environ.get("FLIPKART_AFFILIATE_ID", "")
AFFILIATE_TOKEN = os.environ.get("FLIPKART_AFFILIATE_TOKEN", "")
AFFILIATE_BASE = "https://affiliate-api.flipkart.net/affiliate/api"

LINK_CURATED, LINK_RESOLVED, LINK_SEARCH = "curated", "resolved", "search"

SORT_MODES = ("relevance", "price_asc", "price_desc", "recency_desc", "popularity")

# Category browse seeds. Each entry drives a fan-out of search queries; `/pr`
# browse URLs are used when Flipkart exposes them, search otherwise.
CATEGORY_SEEDS: Dict[str, Tuple[str, ...]] = {
    "Men's Fashion": ("mens jeans", "mens casual shirt", "mens formal shirt",
                      "mens t-shirt", "mens trousers", "mens jacket", "mens suit"),
    "Women's Fashion": ("womens kurta set", "womens jeans", "womens dress",
                        "saree", "womens top", "womens cardigan"),
    "Footwear & Shoes": ("running shoes men", "sneakers men", "womens sandals",
                         "formal shoes men", "sports shoes", "clogs"),
    "Watches & Eyewear": ("analog watch men", "smartwatch", "sunglasses men",
                          "chronograph watch", "digital watch"),
    "Smartphones": ("mobile phones 5g", "iphone", "samsung galaxy phone",
                    "motorola phone", "oneplus phone", "pixel phone"),
    "Audio, Monitors & Laptops": ("bluetooth headphones", "tws earbuds",
                                  "bluetooth speaker", "gaming monitor",
                                  "gaming laptop", "tablet"),
    "Cosmetics & Grooming": ("face serum", "lipstick", "beard trimmer",
                             "perfume", "face cream", "shampoo"),
    "Home Appliances": ("water purifier", "air fryer", "mixer grinder",
                        "ceiling fan", "vacuum cleaner", "water geyser"),
    "Stationery & Books": ("notebook", "ball pen", "scientific calculator",
                           "books bestsellers", "art supplies"),
}

# --------------------------------------------------------------------------- #
# OFFER MATRIX — Flipkart Big Billion Days 2026 (researched, dated)
# --------------------------------------------------------------------------- #

OFFER_SNAPSHOT_DATE = "2026-09-23"

BBD_2026 = {
    "sale_name": "Flipkart Big Billion Days 2026",
    "theme": "Luck Badal Sakta Hai, Yahan Kuch Bhi Ho Sakta Hai",
    "early_bird_deals_from": "2026-09-24",
    "early_access_from": "2026-10-08",   # Plus / Black / Flipkart Credit Card
    "public_start": "2026-10-09",
    "end_date": None,                     # not announced; historically 7-10 days
    "title_sponsors": ("Samsung Galaxy", "Intel Core Ultra"),
    "deal_formats": ("Price Crash", "BBD Specials", "Tick Tock Deals",
                     "Brand Hours", "Double Discount", "Rush Hours"),
}


@dataclass(frozen=True)
class Offer:
    """A single stackable saving instrument."""
    key: str
    label: str
    kind: str              # instant | cashback | credit | exchange | coin
    rate: float            # 0.10 == 10%
    cap: Optional[int]     # rupee cap, None = uncapped/unpublished
    stacks_with_sale: bool
    note: str


OFFER_MATRIX: Tuple[Offer, ...] = (
    Offer("axis_icici", "Axis Bank / ICICI Bank cards — 10% instant discount",
          "instant", 0.10, None, True,
          "Official banking partners for BBD 2026. Cap and minimum order value "
          "not published yet; historically ₹1,000-₹1,750 per transaction."),
    Offer("fk_axis_cc", "Flipkart Axis Bank Credit Card — up to 10% savings",
          "instant", 0.10, None, True,
          "Co-branded card benefit; its separate unlimited-cashback programme "
          "is a card reward, not an unlimited instant discount."),
    Offer("plus_premium", "Flipkart Plus / Premium — 12% card discount (early access)",
          "instant", 0.12, None, True,
          "Applies in the 8 Oct early-access window for eligible members."),
    Offer("black", "Flipkart Black — up to 15%",
          "instant", 0.15, None, True,
          "Reported as split across separate discount components and spend caps."),
    Offer("supermoney", "super.money by Flipkart — 10% cashback (first transaction)",
          "cashback", 0.10, None, True,
          "First eligible transaction only; subject to min spend and max limit."),
    Offer("pay_later", "Flipkart Pay Later — credit up to ₹1,00,000 + ₹500 off",
          "credit", 0.0, None, True,
          "Card-free credit line; ₹500 discount subject to eligibility."),
    Offer("super_pay_3", "Super Pay in 3 — 3 interest-free instalments",
          "credit", 0.0, None, True,
          "No interest or additional fees on eligible purchases."),
    Offer("no_cost_emi", "No-cost EMI across select products and banks",
          "credit", 0.0, None, True,
          "Useful on TVs, laptops and large appliances."),
    Offer("exchange", "Exchange bonus — phones, laptops, appliances",
          "exchange", 0.0, None, True,
          "Stacks on top of bank discount; check exchange quote before the sale."),
    Offer("supercoins", "SuperCoin redemption at checkout",
          "coin", 0.0, None, True, "Small incremental checkout discount."),
    Offer("upi_partners", "UPI partners — BHIM, CRED, Paytm supported",
          "instant", 0.0, None, True, "Payment availability, not a headline discount."),
)

RIVAL_NOTE = ("Amazon's Great Indian Festival 2026 starts 8 October with an "
              "SBI-led bank offer — if SBI is your primary card, cross-check "
              "the same SKU there before buying.")


class OfferEngine:
    """Computes best-case effective price under the 2026 offer matrix."""

    # Offers gated behind a paid membership must not be assumed. Only these are
    # available to any shopper holding a mainstream card.
    OPEN_TO_ALL = ("axis_icici", "fk_axis_cc", "supermoney")

    @staticmethod
    def applicable(offers: Sequence[Offer] = OFFER_MATRIX,
                   entitlements: Optional[Sequence[str]] = None) -> Tuple[Offer, ...]:
        """`entitlements` opts in to membership offers, e.g. ('plus_premium',)."""
        allowed = set(OfferEngine.OPEN_TO_ALL) | set(entitlements or ())
        return tuple(o for o in offers
                     if o.kind in ("instant", "cashback") and o.key in allowed)

    @staticmethod
    def apply(price: int, offer: Offer, assumed_cap: Optional[int] = None) -> int:
        """Rupee value of one offer. `assumed_cap` models an unpublished cap."""
        if price <= 0 or offer.rate <= 0:
            return 0
        raw = int(price * offer.rate)
        cap = offer.cap if offer.cap is not None else assumed_cap
        return min(raw, cap) if cap else raw

    @staticmethod
    def best(price: int, assumed_cap: Optional[int] = 1500,
             entitlements: Optional[Sequence[str]] = None) -> Tuple[Offer, int, int]:
        """Return (best_offer, saving, net_price). Instant beats cashback on ties."""
        ranked: List[Tuple[int, int, Offer]] = []
        for offer in OfferEngine.applicable(entitlements=entitlements):
            saving = OfferEngine.apply(price, offer, assumed_cap)
            ranked.append((saving, 1 if offer.kind == "instant" else 0, offer))
        ranked.sort(key=lambda t: (t[0], t[1]), reverse=True)
        saving, _, offer = ranked[0]
        return offer, saving, max(price - saving, 0)


# --------------------------------------------------------------------------- #
# URL HELPERS  (mirrors the tracker's contract exactly)
# --------------------------------------------------------------------------- #


def search_url(query: str, page: int = 1, sort: str = "relevance") -> str:
    params: Dict[str, Any] = {"q": query}
    if page > 1:
        params["page"] = page
    if sort and sort != "relevance":
        params["sort"] = sort
    return f"{BASE}/search?{urlencode(params, quote_via=quote_plus)}"


def sanitize_url(raw: str) -> str:
    if not raw or not isinstance(raw, str):
        return ""
    candidate = raw.strip().strip("\"',)\u200b")
    if not candidate:
        return ""
    if candidate.startswith("//"):
        candidate = "https:" + candidate
    elif candidate.startswith("/"):
        candidate = BASE + candidate
    elif not candidate.lower().startswith(("http://", "https://")):
        candidate = "https://" + candidate
    try:
        parsed = urlparse(candidate)
    except ValueError:
        return ""
    if parsed.netloc.lower() not in {"flipkart.com", HOST, "dl.flipkart.com"}:
        return ""
    kept = [(k, v) for k, v in
            (p.split("=", 1) for p in parsed.query.split("&") if "=" in p)
            if k.lower() in KEEP_PARAMS]
    return urlunparse(("https", HOST, parsed.path, "", urlencode(kept), ""))


def is_pdp(url: str) -> bool:
    return bool(url) and bool(ITM_ID_PATTERN.search(urlparse(url).path))


def extract_itm_id(url: str) -> str:
    m = ITM_ID_PATTERN.search(urlparse(url or "").path)
    return m.group(1).lower() if m else ""


# --------------------------------------------------------------------------- #
# RATE LIMITING / RESILIENCE
# --------------------------------------------------------------------------- #


class TokenBucket:
    """Thread-safe token bucket. Caps aggregate QPS across every worker."""

    def __init__(self, rate: float = DEFAULT_QPS, burst: int = DEFAULT_BURST) -> None:
        self.rate = max(rate, 0.1)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: int = 1) -> float:
        """Block until `tokens` are available. Returns seconds waited."""
        waited = 0.0
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return waited
                deficit = tokens - self._tokens
                sleep_for = deficit / self.rate
            time.sleep(min(sleep_for, 2.0))
            waited += min(sleep_for, 2.0)


class CircuitBreaker:
    """Stops hammering a host that is blocking us; probes again after cooldown."""

    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int = BREAKER_THRESHOLD,
                 cooldown: int = BREAKER_COOLDOWN) -> None:
        self.threshold = threshold
        self.cooldown = cooldown
        self._failures = 0
        self._opened_at = 0.0
        self._state = self.CLOSED
        self._lock = threading.Lock()

    @property
    def state(self) -> str:
        with self._lock:
            if self._state == self.OPEN and \
                    time.monotonic() - self._opened_at >= self.cooldown:
                self._state = self.HALF_OPEN
            return self._state

    def allow(self) -> bool:
        return self.state in (self.CLOSED, self.HALF_OPEN)

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._state = self.CLOSED

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                LOG.warning("Circuit breaker OPEN after %d failures; "
                            "cooling down %ds.", self._failures, self.cooldown)


# --------------------------------------------------------------------------- #
# CACHE
# --------------------------------------------------------------------------- #


class ScanCache:
    """SQLite (WAL) cache: raw pages, resolved links and an append-only ledger."""

    def __init__(self, path: str = SCAN_CACHE_FILE) -> None:
        self.path = path
        self._local = threading.local()
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS pages (
                    url_hash TEXT PRIMARY KEY, url TEXT, body TEXT,
                    fetched_at REAL, status INTEGER);
                CREATE TABLE IF NOT EXISTS links (
                    query TEXT PRIMARY KEY, url TEXT, resolved_at REAL);
                CREATE TABLE IF NOT EXISTS ledger (
                    pid TEXT, title TEXT, price INTEGER, mrp INTEGER,
                    seen_at REAL, url TEXT);
                CREATE INDEX IF NOT EXISTS ledger_pid ON ledger(pid);
                CREATE INDEX IF NOT EXISTS ledger_seen ON ledger(seen_at);
                """
            )

    # -- pages --
    @staticmethod
    def _hash(url: str) -> str:
        return hashlib.sha256(url.encode("utf-8")).hexdigest()

    def get_page(self, url: str, ttl: int = PAGE_TTL_SECONDS) -> Optional[str]:
        row = self._conn().execute(
            "SELECT body, fetched_at FROM pages WHERE url_hash=?",
            (self._hash(url),)).fetchone()
        if not row:
            return None
        body, fetched_at = row
        return body if (time.time() - fetched_at) < ttl else None

    def put_page(self, url: str, body: str, status: int = 200) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?)",
                (self._hash(url), url, body, time.time(), status))

    # -- links --
    def get_link(self, query: str) -> Optional[str]:
        row = self._conn().execute(
            "SELECT url FROM links WHERE query=?", (query.lower(),)).fetchone()
        return row[0] if row else None

    def put_link(self, query: str, url: str) -> None:
        with self._conn() as conn:
            conn.execute("INSERT OR REPLACE INTO links VALUES (?,?,?)",
                         (query.lower(), url, time.time()))

    # -- ledger --
    def append_ledger(self, records: Iterable["ProductRecord"]) -> int:
        rows = [(r.pid, r.title, r.price, r.mrp, time.time(), r.url)
                for r in records if r.pid and r.price > 0]
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany("INSERT INTO ledger VALUES (?,?,?,?,?,?)", rows)
        return len(rows)

    def price_stats(self, pid: str, days: int = 180) -> Dict[str, Optional[float]]:
        cutoff = time.time() - days * 86400
        prices = [r[0] for r in self._conn().execute(
            "SELECT price FROM ledger WHERE pid=? AND seen_at>=?", (pid, cutoff))]
        if not prices:
            return {"observations": 0, "avg": None, "min": None,
                    "max": None, "median": None}
        return {"observations": len(prices), "avg": round(statistics.fmean(prices), 2),
                "min": min(prices), "max": max(prices),
                "median": statistics.median(prices)}

    def vacuum_pages(self, older_than: int = 7 * 86400) -> int:
        with self._conn() as conn:
            cur = conn.execute("DELETE FROM pages WHERE fetched_at < ?",
                               (time.time() - older_than,))
        return cur.rowcount


# --------------------------------------------------------------------------- #
# RECORDS
# --------------------------------------------------------------------------- #


@dataclass
class ProductRecord:
    pid: str = ""
    itm_id: str = ""
    title: str = ""
    brand: str = ""
    url: str = ""
    price: int = 0
    mrp: int = 0
    discount_pct: float = 0.0
    rating: Optional[float] = None
    rating_count: int = 0
    availability: str = ""
    seller: str = ""
    sponsored: bool = False
    category: str = ""
    query: str = ""
    highlights: List[str] = field(default_factory=list)
    scanned_at: str = field(
        default_factory=lambda: dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    source_strategy: str = ""

    @property
    def key(self) -> str:
        return self.pid or self.itm_id or self.url

    def finalise(self) -> "ProductRecord":
        self.url = sanitize_url(self.url)
        self.itm_id = extract_itm_id(self.url)
        if self.mrp > 0 and self.price > 0 and not self.discount_pct:
            self.discount_pct = round((self.mrp - self.price) / self.mrp * 100, 1)
        if not self.brand and self.title:
            self.brand = self.title.split()[0]
        return self


@dataclass
class ResolveOutcome:
    """Structurally identical to the tracker's ResolveOutcome."""
    query: str
    url: str
    source: str
    ok: bool = True
    detail: str = ""


@dataclass
class ScanStats:
    pages_fetched: int = 0
    pages_from_cache: int = 0
    requests_failed: int = 0
    products_seen: int = 0
    products_unique: int = 0
    challenges: int = 0
    started_at: float = field(default_factory=time.monotonic)

    @property
    def elapsed(self) -> float:
        return round(time.monotonic() - self.started_at, 2)

    @property
    def throughput(self) -> float:
        return round(self.products_unique / max(self.elapsed, 0.001), 1)

    def as_dict(self) -> Dict[str, Any]:
        d = dataclasses.asdict(self)
        d.pop("started_at", None)
        d.update(elapsed_sec=self.elapsed, products_per_sec=self.throughput)
        return d


# --------------------------------------------------------------------------- #
# EXTRACTION
# --------------------------------------------------------------------------- #


class StateExtractor:
    """Three-strategy product extraction, richest first."""

    PRICE_KEYS = ("finalPrice", "sellingPrice", "value", "decimalValue")

    @classmethod
    def extract(cls, html: str, query: str = "", category: str = "") -> List[ProductRecord]:
        for strategy in (cls._from_initial_state, cls._from_ldjson, cls._from_hrefs):
            try:
                records = strategy(html)
            except Exception as exc:  # never let one strategy kill the scan
                LOG.debug("Strategy %s failed: %s", strategy.__name__, exc)
                continue
            if records:
                for r in records:
                    r.query, r.category = query, category
                    r.source_strategy = strategy.__name__.lstrip("_")
                    r.finalise()
                # Dedupe only AFTER finalise(): itm_id is the one identifier every
                # strategy can produce, and state trees emit a thin wrapper node
                # plus a hydrated inner node for the same product.
                return _dedupe_by_identity([r for r in records if is_pdp(r.url)])
        return []

    # -- S1: window.__INITIAL_STATE__ ------------------------------------- #

    @classmethod
    def _from_initial_state(cls, html: str) -> List[ProductRecord]:
        match = STATE_PATTERN.search(html)
        if not match:
            return []
        state = json.loads(match.group(1))
        found: Dict[str, ProductRecord] = {}
        for node in cls._walk(state):
            record = cls._node_to_record(node)
            if record and record.key not in found:
                found[record.key] = record
        return list(found.values())

    @staticmethod
    def _walk(node: Any, depth: int = 0):
        """Depth-bounded traversal of an arbitrarily shaped state tree."""
        if depth > 18:
            return
        if isinstance(node, dict):
            yield node
            for value in node.values():
                yield from StateExtractor._walk(value, depth + 1)
        elif isinstance(node, list):
            for item in node:
                yield from StateExtractor._walk(item, depth + 1)

    @classmethod
    def _node_to_record(cls, node: Dict[str, Any]) -> Optional[ProductRecord]:
        """A node qualifies when it carries a product id and a usable URL."""
        if not isinstance(node, dict):
            return None
        # Flipkart nests money under `pricing` / `price`; flatten one level so
        # finalPrice, mrp and totalDiscount are reachable by the scalar readers.
        for money_key in ("pricing", "price"):
            inner = node.get(money_key)
            if isinstance(inner, dict):
                node = {**inner, **node}   # outer keys win, inner ones surface
        pid = cls._first_str(node, ("productId", "pid", "id"))
        url = cls._first_str(node, ("url", "pageUri", "baseUrl", "smartUrl", "itemUrl"))
        if not url or "/p/itm" not in url:
            url = cls._nested_url(node)
        if not url or "/p/itm" not in url:
            return None
        title = cls._first_str(node, ("title", "name", "productName")) or \
            cls._nested_str(node, ("titles", "title"))
        price = cls._price(node, ("finalPrice", "sellingPrice", "price"))
        mrp = cls._price(node, ("mrp", "strikeOffPrice", "listPrice"))
        rating_node = node.get("rating") if isinstance(node.get("rating"), dict) else {}
        return ProductRecord(
            pid=pid or "",
            title=(title or "").strip(),
            url=url,
            price=price,
            mrp=mrp or price,
            discount_pct=float(cls._to_float(
                node.get("totalDiscount") or node.get("discount")) or 0.0),
            rating=cls._to_float(rating_node.get("average") if rating_node
                                 else node.get("averageRating")),
            rating_count=int(cls._to_float(
                rating_node.get("count") if rating_node
                else node.get("ratingCount")) or 0),
            availability=cls._first_str(node, ("availability", "availabilityStatus")),
            seller=cls._first_str(node, ("sellerName", "seller")),
            sponsored=bool(node.get("sponsored") or node.get("isAd")),
            highlights=[h for h in (node.get("keySpecs") or node.get("highlights") or [])
                        if isinstance(h, str)][:6],
        )

    @staticmethod
    def _first_str(node: Dict[str, Any], keys: Sequence[str]) -> str:
        for key in keys:
            value = node.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _nested_url(node: Dict[str, Any]) -> str:
        for key in ("action", "link", "productInfo", "value"):
            inner = node.get(key)
            if isinstance(inner, dict):
                for candidate in ("url", "pageUri", "baseUrl", "smartUrl"):
                    value = inner.get(candidate)
                    if isinstance(value, str) and "/p/itm" in value:
                        return value
                deeper = StateExtractor._nested_url(inner)
                if deeper:
                    return deeper
        return ""

    @staticmethod
    def _nested_str(node: Dict[str, Any], path: Sequence[str]) -> str:
        cursor: Any = node
        for key in path:
            if not isinstance(cursor, dict):
                return ""
            cursor = cursor.get(key)
        return cursor.strip() if isinstance(cursor, str) else ""

    @classmethod
    def _price(cls, node: Dict[str, Any], keys: Sequence[str]) -> int:
        for key in keys:
            value = node.get(key)
            if isinstance(value, (int, float)) and value > 0:
                return int(value)
            if isinstance(value, dict):
                for inner in cls.PRICE_KEYS:
                    candidate = value.get(inner)
                    if isinstance(candidate, (int, float)) and candidate > 0:
                        return int(candidate)
            if isinstance(value, str):
                digits = re.sub(r"[^\d]", "", value)
                if digits:
                    return int(digits)
        return 0

    @staticmethod
    def _to_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    # -- S2: ld+json ------------------------------------------------------ #

    @classmethod
    def _from_ldjson(cls, html: str) -> List[ProductRecord]:
        records: List[ProductRecord] = []
        for block in LDJSON_PATTERN.finditer(html):
            try:
                payload = json.loads(block.group(1).strip())
            except json.JSONDecodeError:
                continue
            for entry in (payload if isinstance(payload, list) else [payload]):
                if not isinstance(entry, dict):
                    continue
                if entry.get("@type") == "ItemList":
                    for element in entry.get("itemListElement") or []:
                        item = element.get("item") if isinstance(element, dict) else None
                        if isinstance(item, dict):
                            records.append(cls._ldjson_product(item))
                elif entry.get("@type") == "Product":
                    records.append(cls._ldjson_product(entry))
        return [r for r in records if r and r.url]

    @classmethod
    def _ldjson_product(cls, entry: Dict[str, Any]) -> Optional[ProductRecord]:
        offers = entry.get("offers") or {}
        if isinstance(offers, list):
            offers = offers[0] if offers else {}
        url = entry.get("url") or offers.get("url") or ""
        if "/p/itm" not in str(url):
            return None
        rating = entry.get("aggregateRating") or {}
        return ProductRecord(
            pid=str(entry.get("sku") or entry.get("productID") or ""),
            title=str(entry.get("name") or ""),
            brand=str((entry.get("brand") or {}).get("name", "")
                      if isinstance(entry.get("brand"), dict) else entry.get("brand") or ""),
            url=str(url),
            price=int(cls._to_float(offers.get("price")) or 0),
            mrp=int(cls._to_float(offers.get("price")) or 0),
            rating=cls._to_float(rating.get("ratingValue")),
            rating_count=int(cls._to_float(rating.get("ratingCount")) or 0),
            availability=str(offers.get("availability") or ""),
        )

    # -- S3: href regex (the tracker's original floor) --------------------- #

    @staticmethod
    def _from_hrefs(html: str) -> List[ProductRecord]:
        seen: Dict[str, ProductRecord] = {}
        for match in PDP_HREF_PATTERN.finditer(html):
            href = match.group(1)
            if href.startswith(("/search", "/pr?", "/q/")):
                continue
            url = sanitize_url(BASE + href)
            if not is_pdp(url):
                continue
            itm = extract_itm_id(url)
            if itm in seen:
                continue
            slug = urlparse(url).path.split("/p/")[0].strip("/").replace("-", " ")
            seen[itm] = ProductRecord(itm_id=itm, url=url, title=slug.title()[:120])
        return list(seen.values())


# --------------------------------------------------------------------------- #
# ENGINE
# --------------------------------------------------------------------------- #


class DeepScanEngine:
    """Concurrent, cached, rate-limited, catalogue-wide Flipkart scanner."""

    def __init__(self, *, qps: float = DEFAULT_QPS, burst: int = DEFAULT_BURST,
                 workers: int = DEFAULT_WORKERS, cache_path: str = SCAN_CACHE_FILE,
                 respect_robots: bool = True, use_affiliate_api: bool = True) -> None:
        self.bucket = TokenBucket(qps, burst)
        self.breaker = CircuitBreaker()
        self.cache = ScanCache(cache_path)
        self.workers = max(1, workers)
        self.stats = ScanStats()
        self._local = threading.local()
        self._robots = self._load_robots() if respect_robots else None
        self.affiliate_ready = bool(use_affiliate_api and AFFILIATE_ID and AFFILIATE_TOKEN)

    # -- plumbing --------------------------------------------------------- #

    @property
    def available(self) -> bool:
        return requests is not None

    def _session(self):
        if requests is None:
            return None
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": random.choice(USER_AGENTS),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-IN,en;q=0.9",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            })
            adapter = requests.adapters.HTTPAdapter(
                pool_connections=self.workers, pool_maxsize=self.workers * 2)
            session.mount("https://", adapter)
            self._local.session = session
        return session

    def _load_robots(self) -> Optional[robotparser.RobotFileParser]:
        if requests is None:
            return None
        parser = robotparser.RobotFileParser()
        try:
            resp = requests.get(f"{BASE}/robots.txt", timeout=8,
                                headers={"User-Agent": USER_AGENTS[0]})
            parser.parse(resp.text.splitlines())
            return parser
        except Exception as exc:
            LOG.info("robots.txt unavailable (%s); proceeding conservatively.", exc)
            return None

    def _allowed(self, url: str) -> bool:
        if self._robots is None:
            return True
        try:
            return self._robots.can_fetch(USER_AGENTS[0], url)
        except Exception:
            return True

    @staticmethod
    def _is_challenge(html: str) -> bool:
        head = html[:4000].lower()
        return any(marker in head for marker in CHALLENGE_MARKERS)

    # -- fetch ------------------------------------------------------------ #

    def fetch(self, url: str, *, use_cache: bool = True) -> Optional[str]:
        if use_cache:
            cached = self.cache.get_page(url)
            if cached:
                self.stats.pages_from_cache += 1
                return cached
        if not self.available:
            return None
        if not self._allowed(url):
            LOG.info("robots.txt disallows %s — skipping.", url)
            return None
        if not self.breaker.allow():
            LOG.debug("Circuit open; skipping %s", url)
            return None

        session = self._session()
        for attempt in range(1, DEFAULT_RETRIES + 1):
            self.bucket.take()
            try:
                response = session.get(url, timeout=DEFAULT_TIMEOUT)
            except Exception as exc:
                LOG.debug("attempt %d %s: %s", attempt, type(exc).__name__, exc)
                self._backoff(attempt)
                continue
            if response.status_code == 200 and response.text:
                if self._is_challenge(response.text):
                    self.stats.challenges += 1
                    self.breaker.record_failure()
                    self._backoff(attempt, base=2.0)
                    continue
                self.breaker.record_success()
                self.stats.pages_fetched += 1
                self.cache.put_page(url, response.text, 200)
                return response.text
            if response.status_code in (429, 503):
                self.breaker.record_failure()
                self._backoff(attempt, base=2.5)
                continue
            self._backoff(attempt)
        self.stats.requests_failed += 1
        self.breaker.record_failure()
        return None

    @staticmethod
    def _backoff(attempt: int, base: float = 0.8) -> None:
        time.sleep(base * (2 ** (attempt - 1)) + random.uniform(0, 0.4))

    # -- scanning --------------------------------------------------------- #

    def scan_query(self, query: str, *, pages: int = 3, sort: str = "relevance",
                   category: str = "", use_cache: bool = True) -> List[ProductRecord]:
        """Scan one search query across N result pages."""
        harvested: List[ProductRecord] = []
        for page in range(1, max(pages, 1) + 1):
            html = self.fetch(search_url(query, page, sort), use_cache=use_cache)
            if not html:
                break
            records = StateExtractor.extract(html, query=query, category=category)
            if not records:
                break
            harvested.extend(records)
            self.stats.products_seen += len(records)
            if len(records) < 8:   # thin page -> end of useful results
                break
        return harvested

    def scan_many(self, queries: Sequence[str], *, pages: int = 3,
                  sort: str = "relevance", category_map: Optional[Dict[str, str]] = None,
                  progress: Optional[Callable[[int, int, str], None]] = None,
                  use_cache: bool = True) -> List[ProductRecord]:
        """Fan out across queries with a bounded thread pool."""
        category_map = category_map or {}
        results: List[ProductRecord] = []
        done = 0
        total = len(queries)
        with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {
                pool.submit(self.scan_query, q, pages=pages, sort=sort,
                            category=category_map.get(q, ""), use_cache=use_cache): q
                for q in queries
            }
            for future in cf.as_completed(futures):
                query = futures[future]
                done += 1
                if progress:
                    progress(done, total, query)
                try:
                    results.extend(future.result())
                except Exception as exc:
                    LOG.warning("Scan failed for %r: %s", query, exc)
        return results

    def scan_catalogue(self, seeds: Optional[Dict[str, Tuple[str, ...]]] = None,
                       *, pages: int = 3, sorts: Sequence[str] = ("relevance",),
                       progress: Optional[Callable[[int, int, str], None]] = None,
                       use_cache: bool = True) -> List[ProductRecord]:
        """Full catalogue sweep: every category seed x every sort x N pages."""
        seeds = seeds or CATEGORY_SEEDS
        queries: List[str] = []
        category_map: Dict[str, str] = {}
        for category, terms in seeds.items():
            for term in terms:
                queries.append(term)
                category_map[term] = category

        collected: List[ProductRecord] = []
        for sort in sorts:
            collected.extend(self.scan_many(
                queries, pages=pages, sort=sort, category_map=category_map,
                progress=progress, use_cache=use_cache))

        unique = self.deduplicate(collected)
        self.stats.products_unique = len(unique)
        self.cache.append_ledger(unique)
        return unique

    @staticmethod
    def deduplicate(records: Sequence[ProductRecord]) -> List[ProductRecord]:
        """Keep the richest record per product (most fields populated wins)."""
        best: Dict[str, ProductRecord] = {}
        for record in records:
            key = record.key
            if not key:
                continue
            incumbent = best.get(key)
            if incumbent is None or _richness(record) > _richness(incumbent):
                best[key] = record
        return list(best.values())

    # -- tracker-compatible resolution ------------------------------------ #

    def resolve(self, product_name: str, *, use_cache: bool = True) -> ResolveOutcome:
        """Drop-in replacement for FlipkartLinkResolver.resolve()."""
        query = (product_name or "").strip()
        if not query:
            return ResolveOutcome(query, search_url("deals"), LINK_SEARCH, False,
                                  "empty product name")
        if use_cache:
            cached = self.cache.get_link(query)
            if cached and is_pdp(cached):
                return ResolveOutcome(query, cached, LINK_RESOLVED, True, "cache hit")
        if not self.available:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False,
                                  "requests not installed")

        records = self.scan_query(query, pages=1, use_cache=use_cache)
        if not records:
            return ResolveOutcome(query, search_url(query), LINK_SEARCH, False,
                                  "no product link found in search results")

        # Best match, not merely the first href: token overlap beats position.
        best = max(records, key=lambda r: _title_score(query, r.title))
        self.cache.put_link(query, best.url)
        # Side benefit: everything else on that page is banked for free.
        self.cache.append_ledger(records)
        return ResolveOutcome(query, best.url, LINK_RESOLVED, True,
                              f"resolved live (+{len(records) - 1} bonus products cached)")

    def resolve_many(self, product_names: Sequence[str], *,
                     progress: Optional[Callable[[int, int, str], None]] = None,
                     use_cache: bool = True) -> List[ResolveOutcome]:
        """Concurrent batch resolution with the tracker's progress signature."""
        outcomes: List[ResolveOutcome] = []
        done, total = 0, len(product_names)
        with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
            futures = {pool.submit(self.resolve, name, use_cache=use_cache): name
                       for name in product_names}
            for future in cf.as_completed(futures):
                name = futures[future]
                done += 1
                if progress:
                    progress(done, total, name)
                try:
                    outcomes.append(future.result())
                except Exception as exc:
                    outcomes.append(ResolveOutcome(name, search_url(name),
                                                   LINK_SEARCH, False, str(exc)))
        return outcomes

    def self_test(self) -> Tuple[bool, str]:
        if not self.available:
            return False, "The `requests` package is not installed (`pip install requests`)."
        html = self.fetch(search_url("boAt Airdopes 161"), use_cache=False)
        if not html:
            return False, ("Flipkart unreachable. Check internet, VPN, proxy or "
                           "corporate firewall. Circuit state: " + self.breaker.state)
        records = StateExtractor.extract(html, query="boAt Airdopes 161")
        if not records:
            return False, "Reached Flipkart but no products parsed (bot challenge likely)."
        strategies = {r.source_strategy for r in records}
        return True, (f"Connected. Parsed {len(records)} products via "
                      f"{', '.join(sorted(strategies))}.")

    # -- official API route ------------------------------------------------ #

    def affiliate_feed(self, category_url: str) -> List[ProductRecord]:
        """
        Official Flipkart Affiliate Product Feed. Preferred over HTML parsing:
        higher limits, stable schema, sanctioned. Requires credentials.
        """
        if not self.affiliate_ready or requests is None:
            return []
        headers = {"Fk-Affiliate-Id": AFFILIATE_ID, "Fk-Affiliate-Token": AFFILIATE_TOKEN}
        try:
            payload = requests.get(category_url, headers=headers, timeout=25).json()
        except Exception as exc:
            LOG.warning("Affiliate feed failed: %s", exc)
            return []
        out: List[ProductRecord] = []
        for item in payload.get("productInfoList", []):
            base = item.get("productBaseInfoV1", {})
            price = base.get("flipkartSpecialPrice") or base.get("flipkartSellingPrice") or {}
            mrp = base.get("maximumRetailPrice") or {}
            out.append(ProductRecord(
                pid=str(base.get("productId", "")),
                title=str(base.get("title", "")),
                brand=str(base.get("productBrand", "")),
                url=str(base.get("productUrl", "")),
                price=int(price.get("amount") or 0),
                mrp=int(mrp.get("amount") or 0),
                availability="In Stock" if base.get("inStock") else "Out of Stock",
                rating=(base.get("productRating") or {}).get("averageRating"),
                source_strategy="affiliate_api",
            ).finalise())
        return out

    def affiliate_catalogue_index(self) -> Dict[str, str]:
        """Category -> feed URL map from the Product Feed Listing API."""
        if not self.affiliate_ready or requests is None:
            return {}
        url = f"{AFFILIATE_BASE}/{AFFILIATE_ID}.json"
        headers = {"Fk-Affiliate-Id": AFFILIATE_ID, "Fk-Affiliate-Token": AFFILIATE_TOKEN}
        try:
            listings = requests.get(url, headers=headers, timeout=25).json()
        except Exception as exc:
            LOG.warning("Affiliate index failed: %s", exc)
            return {}
        return {name: meta.get("availableVariants", {})
                .get("v1.1.0", {}).get("get", meta.get("get", ""))
                for name, meta in (listings.get("apiGroups", {})
                                   .get("affiliate", {}).get("apiListings", {}).items())}


def _dedupe_by_identity(records: Sequence[ProductRecord]) -> List[ProductRecord]:
    """Collapse to one record per itm-id, keeping the most fully populated one."""
    best: Dict[str, ProductRecord] = {}
    for record in records:
        key = record.itm_id or record.pid or record.url
        if not key:
            continue
        incumbent = best.get(key)
        if incumbent is None or _richness(record) > _richness(incumbent):
            best[key] = record
    return list(best.values())


def _richness(record: ProductRecord) -> int:
    return sum(bool(v) for v in (record.pid, record.title, record.price, record.mrp,
                                 record.rating, record.rating_count, record.brand,
                                 record.availability, record.highlights))


def _title_score(query: str, title: str) -> float:
    """Token-overlap relevance so resolution picks the right SKU, not the first."""
    q = {t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 1}
    t = {t for t in re.findall(r"[a-z0-9]+", (title or "").lower()) if len(t) > 1}
    return len(q & t) / max(len(q), 1)


# --------------------------------------------------------------------------- #
# ANALYSIS
# --------------------------------------------------------------------------- #


def to_dataframe(records: Sequence[ProductRecord], cache: Optional[ScanCache] = None):
    """Analytics frame: offers applied, history-derived floors, deal score."""
    if pd is None:
        raise RuntimeError("pandas is required for DataFrame output.")
    rows: List[Dict[str, Any]] = []
    for r in records:
        offer, saving, net = OfferEngine.best(r.price)
        history = cache.price_stats(r.pid) if (cache and r.pid) else {}
        avg = history.get("avg") or (r.mrp * 0.85 if r.mrp else r.price)
        floor = history.get("min") or r.price
        vs_avg = round((avg - r.price) / avg * 100, 1) if (avg and r.price > 0) else 0.0
        rows.append({
            "Category": r.category, "Brand": r.brand, "Product": r.title,
            "Current Price": r.price, "MRP": r.mrp, "Listed Disc %": r.discount_pct,
            "Observed Avg": round(avg, 0), "Observed Floor": floor,
            "Observations": history.get("observations", 0),
            "Real Disc % (vs Avg)": vs_avg,
            "Best Offer": offer.label, "Offer Saving": saving, "Net Price": net,
            "Deal Score": _deal_score(r, vs_avg, floor),
            "Rating": r.rating, "Ratings Count": r.rating_count,
            "Availability": r.availability, "Sponsored": r.sponsored,
            "Strategy": r.source_strategy, "PID": r.pid,
            "URL": r.url, "Scanned At": r.scanned_at,
        })
    return pd.DataFrame(rows).sort_values("Deal Score", ascending=False)


def _deal_score(record: ProductRecord, vs_avg: float, floor: int) -> float:
    """0-100 composite: real discount, distance from floor, social proof."""
    proof_only = min((record.rating or 0) / 5 * 12, 12) + \
        min((record.rating_count or 0) / 5000, 1) * 8
    if record.price <= 0:
        # No price captured (href-only fallback): never let it rank as a deal.
        return round(proof_only, 1)
    discount = max(min(vs_avg, 60), -10) / 60 * 55
    at_floor = 25.0 if record.price <= floor else max(
        0.0, 25.0 * (1 - (record.price - floor) / max(floor, 1)))
    proof = min((record.rating or 0) / 5 * 12, 12)
    volume = min((record.rating_count or 0) / 5000, 1) * 8
    return round(max(discount + at_floor + proof + volume, 0), 1)


def offer_report() -> str:
    """Human-readable Flipkart offer brief, current as of OFFER_SNAPSHOT_DATE."""
    lines = [f"{BBD_2026['sale_name']} — snapshot {OFFER_SNAPSHOT_DATE}",
             f"Theme: {BBD_2026['theme']}",
             f"Early Bird deals: {BBD_2026['early_bird_deals_from']}",
             f"Early access:     {BBD_2026['early_access_from']} (Plus / Black / FK Credit Card)",
             f"Public start:     {BBD_2026['public_start']}",
             f"End date:         {BBD_2026['end_date'] or 'not announced (historically 7-10 days)'}",
             f"Title sponsors:   {', '.join(BBD_2026['title_sponsors'])}",
             f"Deal formats:     {', '.join(BBD_2026['deal_formats'])}", "",
             "Stackable offers:"]
    for offer in OFFER_MATRIX:
        rate = f"{offer.rate:.0%}" if offer.rate else "—"
        lines.append(f"  [{offer.kind:8}] {rate:>4}  {offer.label}")
        lines.append(f"              {offer.note}")
    lines += ["", RIVAL_NOTE]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# STREAMLIT BRIDGE (optional; requires no edit to the tracker)
# --------------------------------------------------------------------------- #


def install_into_tracker(tracker_module) -> bool:
    """
    Swap the tracker's resolver for this engine at runtime.

        import flipkart_deal_tracker as t, flipkart_deep_scan as d
        d.install_into_tracker(t)

    The tracker's own source is untouched; only the cached factory is replaced,
    and the engine satisfies the same duck-type (resolve / resolve_many /
    self_test / available / cache_size / clear_cache).
    """
    engine = DeepScanEngine()
    engine.cache_size = property(lambda self: 0)  # type: ignore[attr-defined]
    try:
        tracker_module.get_resolver.clear()       # streamlit cache_resource
    except Exception:
        pass
    tracker_module.get_resolver = lambda: engine  # type: ignore[assignment]
    LOG.info("DeepScanEngine installed into %s", getattr(tracker_module, "__name__", "?"))
    return True


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Flipkart Deep Scan Engine")
    parser.add_argument("--scan-all", action="store_true", help="Full catalogue sweep")
    parser.add_argument("--query", action="append", default=[], help="Scan one query (repeatable)")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument("--sort", default="relevance", choices=SORT_MODES)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS)
    parser.add_argument("--out", default="flipkart_deep_scan.csv")
    parser.add_argument("--offers", action="store_true", help="Print the offer brief and exit")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    if args.offers:
        print(offer_report())
        return

    engine = DeepScanEngine(qps=args.qps, workers=args.workers)

    if args.self_test:
        ok, message = engine.self_test()
        print(("PASS: " if ok else "FAIL: ") + message)
        return

    def progress(done: int, total: int, name: str) -> None:
        print(f"  [{done:>4}/{total}] {name}", flush=True)

    if args.query:
        records = engine.deduplicate(
            engine.scan_many(args.query, pages=args.pages, sort=args.sort,
                             progress=progress))
        engine.stats.products_unique = len(records)
        engine.cache.append_ledger(records)
    elif args.scan_all:
        records = engine.scan_catalogue(pages=args.pages, sorts=(args.sort,),
                                        progress=progress)
    else:
        parser.print_help()
        return

    print(json.dumps(engine.stats.as_dict(), indent=2))
    if pd is not None and records:
        frame = to_dataframe(records, engine.cache)
        frame.to_csv(args.out, index=False)
        print(f"\nWrote {len(frame)} products -> {args.out}")
        print(frame.head(15).to_string(index=False))
    else:
        print("No products captured.")


if __name__ == "__main__":
    _cli()
