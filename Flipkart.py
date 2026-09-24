"""
exhaustive_scan.py — Aggressive Brand x Category Exhaustive Scanner
====================================================================

SELF-CONTAINED. Requires only `bbd_targets.py` (for the brand/category thesis)
plus requests + pandas. It does NOT import flipkart_deep_scan or deep_discovery,
so there is nothing to place side-by-side and nothing to break.

WHY THE PREVIOUS RUN CAPPED NEAR ~80 PRODUCTS
---------------------------------------------
Not a row limit — a DEDUPLICATION COLLAPSE. Three compounding causes:

  1. RESULT OVERLAP. `levis mens jeans`, `wrangler mens jeans` and
     `lee mens jeans` all return Flipkart's same high-relevance head inventory.
     1,258 queries collapsed to a few dozen unique products after dedupe.

  2. NO PAGINATION PRESSURE. Relevance sort returns the identical head page
     for every related query. Without forcing DISJOINT slices, extra queries
     add cost and no coverage.

  3. EARLY TERMINATION. A thin page (ad slots / layout widgets) aborted the
     walk before deeper pages were reached.

THE FIX: FORCED DISJOINT SHARDING
---------------------------------
Every query is exploded into slices that CANNOT return the same rows:

    brand x subcategory x PRICE BAND x sort-direction x page

Price bands are disjoint by construction, so `levis jeans ₹500-999` and
`levis jeans ₹1000-1999` share zero products. Each shard also gets its own
fresh pagination allowance, which is how you get past search-depth truncation.
Ascending AND descending sort on each shard harvests from both ends.

Expected yield: tens of thousands of unique products vs ~80, because coverage
now scales with SHARDS, not with query count.

Everything is persisted continuously to SQLite. Interrupt freely and rerun —
it resumes from the frontier and never re-fetches a completed shard.

LEGAL / ETHICAL
---------------
Public, unauthenticated listing pages only. robots.txt is fetched and obeyed.
No login, no CAPTCHA solving, no paywall or access-control bypass. Aggressive
here means THOROUGH COVERAGE (more disjoint slices, full pagination), never
abusive request rates — the token bucket and circuit breaker still govern
traffic. For genuinely complete catalogue data, use the official Flipkart
Affiliate Product Feed API.

Usage:
    python exhaustive_scan.py --plan
    python exhaustive_scan.py --run --hours 8
    python exhaustive_scan.py --run --p1-only --workers 8
    python exhaustive_scan.py --report
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import logging
import os
import random
import re
import sqlite3
import threading
import time
import urllib.robotparser as robotparser
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple
from urllib.parse import quote_plus, urlencode, urlparse, urlunparse

try:
    import requests
except ImportError:  # pragma: no cover
    requests = None

try:
    import pandas as pd
except ImportError:  # pragma: no cover
    pd = None

import bbd_targets as T

LOG = logging.getLogger("exhaustive")
SCANNER_VERSION = "exhaustive_v1.0"

HOST = "www.flipkart.com"
BASE = f"https://{HOST}"
STORE = os.environ.get("EXHAUSTIVE_DB", "exhaustive_scan.sqlite")

KEEP_PARAMS = {"pid", "lid", "marketplace"}
ITM_ID_PATTERN = re.compile(r"/p/(itm[0-9a-z]{12,18})(?:[/?#]|$)", re.IGNORECASE)
PDP_HREF_PATTERN = re.compile(
    r'href="(/[^"?#]{3,200}/p/itm[0-9a-z]{12,18}[^"]*)"', re.IGNORECASE)
STATE_PATTERN = re.compile(
    r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});?\s*</script>", re.DOTALL)
CHALLENGE_MARKERS = ("captcha", "unusual traffic", "are you a human",
                     "access denied", "request blocked")

# --------------------------------------------------------------------------- #
# SHARDING CONFIGURATION — this is what breaks the dedup collapse
# --------------------------------------------------------------------------- #

# Disjoint by construction. Narrow bands at the low end because fashion and
# beauty inventory is dense there; wide bands up top where listings thin out.
PRICE_BANDS: Tuple[Tuple[int, int], ...] = (
    (0, 199), (200, 349), (350, 499), (500, 699), (700, 899),
    (900, 1199), (1200, 1499), (1500, 1999), (2000, 2499), (2500, 2999),
    (3000, 3999), (4000, 4999), (5000, 6999), (7000, 9999),
    (10000, 14999), (15000, 24999), (25000, 49999), (50000, 9999999),
)

# Both directions per shard: harvests from each end of the same slice.
SORT_MODES: Tuple[str, ...] = ("price_asc", "price_desc")

DEFAULT_PAGES = 10          # per shard; shards are small so this is reachable
DEFAULT_QPS = 2.5
DEFAULT_BURST = 5
DEFAULT_WORKERS = 6
TIMEOUT = 15
RETRIES = 3
BREAKER_THRESHOLD = 8
BREAKER_COOLDOWN = 120

USER_AGENTS = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
)


# --------------------------------------------------------------------------- #
# URL HELPERS
# --------------------------------------------------------------------------- #


def search_url(query: str, page: int = 1, sort: str = "relevance",
               band: Optional[Tuple[int, int]] = None) -> str:
    params: Dict[str, Any] = {"q": query}
    if page > 1:
        params["page"] = page
    if sort and sort != "relevance":
        params["sort"] = sort
    url = f"{BASE}/search?{urlencode(params, quote_via=quote_plus)}"
    if band:
        low, high = band
        url += (f"&p%5B%5D=facets.price_range.from%3D{low}"
                f"&p%5B%5D=facets.price_range.to%3D{high}")
    return url


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
    match = ITM_ID_PATTERN.search(urlparse(url or "").path)
    return match.group(1).lower() if match else ""


# --------------------------------------------------------------------------- #
# TRAFFIC GOVERNORS
# --------------------------------------------------------------------------- #


class TokenBucket:
    def __init__(self, rate: float = DEFAULT_QPS, burst: int = DEFAULT_BURST) -> None:
        self.rate = max(rate, 0.1)
        self.capacity = max(burst, 1)
        self._tokens = float(self.capacity)
        self._updated = time.monotonic()
        self._lock = threading.Lock()

    def take(self, tokens: int = 1) -> None:
        while True:
            with self._lock:
                now = time.monotonic()
                self._tokens = min(self.capacity,
                                   self._tokens + (now - self._updated) * self.rate)
                self._updated = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                wait = (tokens - self._tokens) / self.rate
            time.sleep(min(wait, 2.0))


class CircuitBreaker:
    CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"

    def __init__(self, threshold: int = BREAKER_THRESHOLD,
                 cooldown: int = BREAKER_COOLDOWN) -> None:
        self.threshold, self.cooldown = threshold, cooldown
        self._failures, self._opened_at = 0, 0.0
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

    def success(self) -> None:
        with self._lock:
            self._failures, self._state = 0, self.CLOSED

    def failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._failures >= self.threshold:
                self._state = self.OPEN
                self._opened_at = time.monotonic()
                LOG.warning("Circuit OPEN after %d failures; cooling %ds.",
                            self._failures, self.cooldown)


# --------------------------------------------------------------------------- #
# PERSISTENT STORE — products + shard frontier
# --------------------------------------------------------------------------- #


@dataclass
class Product:
    itm_id: str = ""
    pid: str = ""
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
    category: str = ""
    matched_brand: str = ""
    shard: str = ""
    seen_at: str = field(
        default_factory=lambda: dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))

    def finalise(self) -> "Product":
        self.url = sanitize_url(self.url)
        self.itm_id = extract_itm_id(self.url)
        if self.mrp > 0 and self.price > 0 and not self.discount_pct:
            self.discount_pct = round((self.mrp - self.price) / self.mrp * 100, 1)
        if not self.brand and self.title:
            self.brand = self.title.split()[0]
        return self

    @property
    def key(self) -> str:
        return self.itm_id or self.pid or self.url


class Store:
    """SQLite store. Products are upserted; shards track scan progress."""

    def __init__(self, path: str = STORE) -> None:
        self.path = path
        self._local = threading.local()
        self._init()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, timeout=45, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS products (
                    itm_id TEXT PRIMARY KEY, pid TEXT, title TEXT, brand TEXT,
                    url TEXT, price INTEGER, mrp INTEGER, discount_pct REAL,
                    rating REAL, rating_count INTEGER, availability TEXT,
                    seller TEXT, category TEXT, matched_brand TEXT,
                    shard TEXT, seen_at TEXT);
                CREATE INDEX IF NOT EXISTS p_cat ON products(category);
                CREATE INDEX IF NOT EXISTS p_brand ON products(matched_brand);
                CREATE TABLE IF NOT EXISTS ledger (
                    itm_id TEXT, price INTEGER, seen_at REAL);
                CREATE INDEX IF NOT EXISTS l_itm ON ledger(itm_id);
                CREATE TABLE IF NOT EXISTS shards (
                    shard_id TEXT PRIMARY KEY, query TEXT, category TEXT,
                    brand TEXT, band_low INTEGER, band_high INTEGER, sort TEXT,
                    status TEXT DEFAULT 'pending', found INTEGER DEFAULT 0,
                    done_at REAL);
                CREATE INDEX IF NOT EXISTS s_status ON shards(status);
            """)

    # -- shards --
    def seed_shards(self, shards: Sequence[Tuple[str, str, str, str, int, int, str]]) -> int:
        with self._conn() as conn:
            conn.executemany(
                "INSERT OR IGNORE INTO shards "
                "(shard_id, query, category, brand, band_low, band_high, sort) "
                "VALUES (?,?,?,?,?,?,?)", shards)
        return len(shards)

    def pending_shards(self, limit: int = 200) -> List[Tuple]:
        return self._conn().execute(
            "SELECT shard_id, query, category, brand, band_low, band_high, sort "
            "FROM shards WHERE status='pending' LIMIT ?", (limit,)).fetchall()

    def complete_shard(self, shard_id: str, found: int) -> None:
        with self._conn() as conn:
            conn.execute("UPDATE shards SET status='done', found=?, done_at=? "
                         "WHERE shard_id=?", (found, time.time(), shard_id))

    def shard_stats(self) -> Dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) FROM shards GROUP BY status").fetchall()
        stats = {status: count for status, count in rows}
        stats["total"] = sum(stats.values())
        stats.setdefault("pending", 0)
        stats.setdefault("done", 0)
        return stats

    def reset_shards(self) -> None:
        with self._conn() as conn:
            conn.execute("DELETE FROM shards")

    # -- products --
    def upsert(self, products: Iterable[Product]) -> int:
        rows = [(p.itm_id, p.pid, p.title, p.brand, p.url, p.price, p.mrp,
                 p.discount_pct, p.rating, p.rating_count, p.availability,
                 p.seller, p.category, p.matched_brand, p.shard, p.seen_at)
                for p in products if p.itm_id]
        if not rows:
            return 0
        with self._conn() as conn:
            conn.executemany(
                "INSERT INTO products VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(itm_id) DO UPDATE SET price=excluded.price, "
                "mrp=excluded.mrp, discount_pct=excluded.discount_pct, "
                "rating=excluded.rating, rating_count=excluded.rating_count, "
                "availability=excluded.availability, seen_at=excluded.seen_at",
                rows)
            conn.executemany("INSERT INTO ledger VALUES (?,?,?)",
                             [(p.itm_id, p.price, time.time())
                              for p in products if p.itm_id and p.price > 0])
        return len(rows)

    def count(self) -> int:
        return self._conn().execute("SELECT COUNT(*) FROM products").fetchone()[0]

    def by_category(self) -> List[Tuple[str, int]]:
        return self._conn().execute(
            "SELECT category, COUNT(*) FROM products GROUP BY category "
            "ORDER BY 2 DESC").fetchall()

    def by_brand(self, limit: int = 40) -> List[Tuple[str, int]]:
        return self._conn().execute(
            "SELECT matched_brand, COUNT(*) FROM products "
            "WHERE matched_brand != '' GROUP BY matched_brand "
            "ORDER BY 2 DESC LIMIT ?", (limit,)).fetchall()

    def price_stats(self, itm_id: str) -> Dict[str, Any]:
        prices = [r[0] for r in self._conn().execute(
            "SELECT price FROM ledger WHERE itm_id=?", (itm_id,))]
        if not prices:
            return {"observations": 0, "median": None, "avg": None}
        ordered = sorted(prices)
        mid = len(ordered) // 2
        median = (ordered[mid] if len(ordered) % 2
                  else (ordered[mid - 1] + ordered[mid]) / 2)
        return {"observations": len(prices), "median": median,
                "avg": sum(prices) / len(prices), "min": min(prices)}

    def dataframe(self) -> "pd.DataFrame":
        if pd is None:
            raise RuntimeError("pandas required")
        return pd.read_sql_query("SELECT * FROM products", self._conn())


# --------------------------------------------------------------------------- #
# EXTRACTION
# --------------------------------------------------------------------------- #


class Extractor:
    @staticmethod
    def extract(html: str) -> List[Product]:
        products = Extractor._from_state(html)
        if not products:
            products = Extractor._from_hrefs(html)
        best: Dict[str, Product] = {}
        for product in products:
            product.finalise()
            if not is_pdp(product.url):
                continue
            key = product.key
            incumbent = best.get(key)
            if incumbent is None or _richness(product) > _richness(incumbent):
                best[key] = product
        return list(best.values())

    @staticmethod
    def _from_state(html: str) -> List[Product]:
        match = STATE_PATTERN.search(html)
        if not match:
            return []
        try:
            state = json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
        out: List[Product] = []
        for node in _walk(state):
            product = Extractor._node(node)
            if product:
                out.append(product)
        return out

    @staticmethod
    def _node(node: Dict[str, Any]) -> Optional[Product]:
        if not isinstance(node, dict):
            return None
        for money in ("pricing", "price"):
            inner = node.get(money)
            if isinstance(inner, dict):
                node = {**inner, **node}
        url = _first_str(node, ("url", "pageUri", "baseUrl", "smartUrl"))
        if "/p/itm" not in url:
            url = _nested_url(node)
        if "/p/itm" not in url:
            return None
        titles = node.get("titles")
        title = _first_str(node, ("title", "name", "productName")) or (
            titles.get("title", "") if isinstance(titles, dict) else "")
        rating = node.get("rating") if isinstance(node.get("rating"), dict) else {}
        return Product(
            pid=_first_str(node, ("productId", "pid", "id")),
            title=str(title).strip(), url=url,
            price=_money(node, ("finalPrice", "sellingPrice", "price")),
            mrp=_money(node, ("mrp", "strikeOffPrice", "listPrice")),
            discount_pct=float(_num(node.get("totalDiscount")
                                    or node.get("discount")) or 0.0),
            rating=_num(rating.get("average") if rating else node.get("averageRating")),
            rating_count=int(_num(rating.get("count") if rating
                                  else node.get("ratingCount")) or 0),
            availability=_first_str(node, ("availability", "availabilityStatus")),
            seller=_first_str(node, ("sellerName", "seller")),
        )

    @staticmethod
    def _from_hrefs(html: str) -> List[Product]:
        seen: Dict[str, Product] = {}
        for match in PDP_HREF_PATTERN.finditer(html):
            href = match.group(1)
            if href.startswith(("/search", "/pr?", "/q/")):
                continue
            url = sanitize_url(BASE + href)
            itm = extract_itm_id(url)
            if not itm or itm in seen:
                continue
            slug = urlparse(url).path.split("/p/")[0].strip("/").replace("-", " ")
            seen[itm] = Product(itm_id=itm, url=url, title=slug.title()[:130])
        return list(seen.values())


def _walk(node: Any, depth: int = 0):
    if depth > 18:
        return
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value, depth + 1)
    elif isinstance(node, list):
        for item in node:
            yield from _walk(item, depth + 1)


def _first_str(node: Dict[str, Any], keys: Sequence[str]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _nested_url(node: Dict[str, Any], depth: int = 0) -> str:
    if depth > 4:
        return ""
    for key in ("action", "link", "productInfo", "value"):
        inner = node.get(key)
        if isinstance(inner, dict):
            for candidate in ("url", "pageUri", "baseUrl", "smartUrl"):
                value = inner.get(candidate)
                if isinstance(value, str) and "/p/itm" in value:
                    return value
            deeper = _nested_url(inner, depth + 1)
            if deeper:
                return deeper
    return ""


def _money(node: Dict[str, Any], keys: Sequence[str]) -> int:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
        if isinstance(value, dict):
            for inner in ("finalPrice", "sellingPrice", "value", "decimalValue"):
                candidate = value.get(inner)
                if isinstance(candidate, (int, float)) and candidate > 0:
                    return int(candidate)
        if isinstance(value, str):
            digits = re.sub(r"[^\d]", "", value)
            if digits:
                return int(digits)
    return 0


def _num(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _richness(product: Product) -> int:
    return sum(bool(v) for v in (product.pid, product.title, product.price,
                                 product.mrp, product.rating, product.brand,
                                 product.availability))


# --------------------------------------------------------------------------- #
# SHARD PLAN — brand x subcategory x price band x sort
# --------------------------------------------------------------------------- #


def build_shards(*, priorities: Optional[Sequence[str]] = None,
                 bands: Sequence[Tuple[int, int]] = PRICE_BANDS,
                 sorts: Sequence[str] = SORT_MODES,
                 include_bare: bool = True
                 ) -> List[Tuple[str, str, str, str, int, int, str]]:
    """
    Explode the thesis into disjoint shards.

    A shard is (query, price band, sort). Two shards with different bands cannot
    return the same product, which is exactly what the previous design lacked.
    """
    shards: List[Tuple[str, str, str, str, int, int, str]] = []
    seen: Set[str] = set()

    for category in T.TARGET_CATEGORIES:
        if priorities and category.priority not in priorities:
            continue
        queries: List[Tuple[str, str]] = []
        if include_bare:
            for term in category.search_terms:
                queries.append((term, ""))
        for brand in category.all_brands:
            brand_key = brand.lower().replace("'", "")
            for term in category.search_terms:
                queries.append((f"{brand_key} {term}", brand))

        for query, brand in queries:
            for low, high in bands:
                for sort in sorts:
                    raw = f"{query}|{low}|{high}|{sort}"
                    shard_id = hashlib.sha1(raw.encode()).hexdigest()[:20]
                    if shard_id in seen:
                        continue
                    seen.add(shard_id)
                    shards.append((shard_id, query, category.name, brand,
                                   low, high, sort))
    return shards


def plan_summary(shards: Sequence[Tuple]) -> str:
    by_category: Dict[str, int] = {}
    for shard in shards:
        by_category[shard[2]] = by_category.get(shard[2], 0) + 1
    lines = ["Category                               Shards    Est. products",
             "-" * 66]
    for category in T.TARGET_CATEGORIES:
        count = by_category.get(category.name, 0)
        if count:
            lines.append(f"{category.name:<38} {count:>7}  {count * 12:>14,}")
    lines.append("-" * 66)
    lines.append(f"{'TOTAL':<38} {len(shards):>7}  {len(shards) * 12:>14,}")
    lines.append("")
    lines.append("Estimate assumes ~12 unique products per shard after overlap.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# SCANNER
# --------------------------------------------------------------------------- #


class ExhaustiveScanner:
    def __init__(self, *, qps: float = DEFAULT_QPS, workers: int = DEFAULT_WORKERS,
                 pages: int = DEFAULT_PAGES, store: Optional[Store] = None,
                 respect_robots: bool = True) -> None:
        self.bucket = TokenBucket(qps)
        self.breaker = CircuitBreaker()
        self.store = store or Store()
        self.workers = max(1, workers)
        self.pages = pages
        self._local = threading.local()
        self._robots = self._load_robots() if respect_robots else None
        self.fetched = 0
        self.failed = 0
        self.challenges = 0

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
            session.mount("https://", requests.adapters.HTTPAdapter(
                pool_connections=self.workers, pool_maxsize=self.workers * 2))
            self._local.session = session
        return session

    def _load_robots(self):
        if requests is None:
            return None
        parser = robotparser.RobotFileParser()
        try:
            response = requests.get(f"{BASE}/robots.txt", timeout=10,
                                    headers={"User-Agent": USER_AGENTS[0]})
            parser.parse(response.text.splitlines())
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

    def fetch(self, url: str) -> Optional[str]:
        if not self.available or not self._allowed(url) or not self.breaker.allow():
            return None
        session = self._session()
        for attempt in range(1, RETRIES + 1):
            self.bucket.take()
            try:
                response = session.get(url, timeout=TIMEOUT)
            except Exception:
                time.sleep(0.8 * attempt + random.uniform(0, 0.4))
                continue
            if response.status_code == 200 and response.text:
                if any(m in response.text[:4000].lower() for m in CHALLENGE_MARKERS):
                    self.challenges += 1
                    self.breaker.failure()
                    time.sleep(2.0 * attempt)
                    continue
                self.breaker.success()
                self.fetched += 1
                return response.text
            if response.status_code in (429, 503):
                self.breaker.failure()
                time.sleep(2.5 * attempt)
                continue
            time.sleep(0.8 * attempt)
        self.failed += 1
        self.breaker.failure()
        return None

    def scan_shard(self, shard: Tuple[str, str, str, str, int, int, str]
                   ) -> Tuple[str, List[Product]]:
        """Walk one disjoint slice to full depth."""
        shard_id, query, category, brand, low, high, sort = shard
        harvested: Dict[str, Product] = {}
        barren = 0

        for page in range(1, self.pages + 1):
            html = self.fetch(search_url(query, page, sort, (low, high)))
            if not html:
                break
            found = Extractor.extract(html)
            fresh = 0
            for product in found:
                if product.key in harvested:
                    continue
                product.category = category
                product.matched_brand = brand
                product.shard = f"{query}|{low}-{high}|{sort}"
                harvested[product.key] = product
                fresh += 1
            barren = barren + 1 if fresh == 0 else 0
            if barren >= 2:
                break
            if not self.breaker.allow():
                break

        return shard_id, list(harvested.values())

    def run(self, *, max_seconds: Optional[float] = None,
            max_products: Optional[int] = None,
            progress: Optional[Callable[[Dict[str, Any]], None]] = None) -> int:
        """Drain the shard frontier. Resumable; persists continuously."""
        deadline = (time.monotonic() + max_seconds) if max_seconds else None
        total_new = 0

        while True:
            if deadline and time.monotonic() > deadline:
                LOG.info("Time box reached.")
                break
            if max_products and self.store.count() >= max_products:
                LOG.info("Product target reached.")
                break
            batch = self.store.pending_shards(limit=self.workers * 8)
            if not batch:
                LOG.info("Frontier exhausted.")
                break
            if not self.breaker.allow():
                LOG.warning("Circuit open — pausing %ds.", BREAKER_COOLDOWN)
                time.sleep(BREAKER_COOLDOWN)
                continue

            with cf.ThreadPoolExecutor(max_workers=self.workers) as pool:
                futures = {pool.submit(self.scan_shard, s): s for s in batch}
                for future in cf.as_completed(futures):
                    shard = futures[future]
                    try:
                        shard_id, products = future.result()
                    except Exception as exc:
                        LOG.debug("shard failed: %s", exc)
                        continue
                    written = self.store.upsert(products)
                    self.store.complete_shard(shard_id, len(products))
                    total_new += written
                    if progress:
                        stats = self.store.shard_stats()
                        progress({"shard": shard[1][:40],
                                  "band": f"{shard[4]}-{shard[5]}",
                                  "found": len(products),
                                  "total_products": self.store.count(),
                                  "shards_done": stats["done"],
                                  "shards_left": stats["pending"]})
        return total_new


# --------------------------------------------------------------------------- #
# REPORTING
# --------------------------------------------------------------------------- #


def build_report(store: Store, out: str = "exhaustive_products.csv"
                 ) -> Optional["pd.DataFrame"]:
    if pd is None:
        print("pandas required for CSV export.")
        return None
    frame = store.dataframe()
    if frame.empty:
        print("No products captured yet.")
        return None

    medians, avgs, counts = [], [], []
    for itm in frame["itm_id"]:
        stats = store.price_stats(itm)
        medians.append(stats["median"])
        avgs.append(stats["avg"])
        counts.append(stats["observations"])
    frame["Observed Median"] = medians
    frame["Observed Avg"] = avgs
    frame["Observations"] = counts

    frame = frame.rename(columns={"title": "Product", "brand": "Brand",
                                  "price": "Current Price", "mrp": "MRP",
                                  "url": "URL"})
    scored = T.enrich_dataframe(frame)
    scored.to_csv(out, index=False)
    return scored


def print_report(store: Store) -> None:
    print(f"\nTotal unique products stored: {store.count():,}")
    stats = store.shard_stats()
    print(f"Shards: {stats['done']:,} done / {stats['pending']:,} pending "
          f"/ {stats['total']:,} total")
    print("\nBy category:")
    for category, count in store.by_category():
        print(f"  {category or '(unclassified)':<40} {count:>8,}")
    brands = store.by_brand(25)
    if brands:
        print("\nTop brands by product count:")
        for brand, count in brands:
            print(f"  {brand:<40} {count:>8,}")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def _cli() -> None:
    parser = argparse.ArgumentParser(
        description="Aggressive brand x category exhaustive Flipkart scanner")
    parser.add_argument("--plan", action="store_true", help="Show the shard plan")
    parser.add_argument("--run", action="store_true", help="Run / resume the scan")
    parser.add_argument("--report", action="store_true", help="Report + export CSV")
    parser.add_argument("--reset", action="store_true", help="Clear the shard frontier")
    parser.add_argument("--p1-only", action="store_true")
    parser.add_argument("--no-bare", action="store_true",
                        help="Brand-qualified queries only")
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--max-products", type=int, default=None)
    parser.add_argument("--pages", type=int, default=DEFAULT_PAGES)
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--qps", type=float, default=DEFAULT_QPS)
    parser.add_argument("--out", default="exhaustive_products.csv")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    priorities = (T.P1_AGGRESSIVE,) if args.p1_only else None
    store = Store()

    if args.reset:
        store.reset_shards()
        print("Shard frontier cleared.")
        return

    if args.plan:
        shards = build_shards(priorities=priorities, include_bare=not args.no_bare)
        print(plan_summary(shards))
        return

    if args.report:
        print_report(store)
        frame = build_report(store, args.out)
        if frame is not None:
            actionable = T.buy_list(frame)
            print(f"\nActionable (BUY / STRONG BUY): {len(actionable):,}")
            cols = [c for c in ("Product", "Brand", "Current Price",
                                "Effective Disc %", "Threshold %", "Buy Signal",
                                "Target Category")
                    if c in actionable.columns]
            if not actionable.empty:
                print(actionable[cols].head(40).to_string(index=False))
            print(f"\nWrote -> {args.out}")
        return

    if args.run:
        shards = build_shards(priorities=priorities, include_bare=not args.no_bare)
        seeded = store.seed_shards(shards)
        stats = store.shard_stats()
        print(plan_summary(shards))
        print(f"\nFrontier: {stats['pending']:,} pending of {stats['total']:,} "
              f"({seeded:,} defined this run)\n")

        scanner = ExhaustiveScanner(qps=args.qps, workers=args.workers,
                                    pages=args.pages, store=store)
        if not scanner.available:
            print("The `requests` package is required: pip install requests")
            return

        last = [0.0]

        def progress(info: Dict[str, Any]) -> None:
            now = time.time()
            if now - last[0] < 2.0:
                return
            last[0] = now
            print(f"   products={info['total_products']:>7,}  "
                  f"shards {info['shards_done']:,}/{info['shards_done'] + info['shards_left']:,}  "
                  f"+{info['found']:<3} {info['band']:<12} {info['shard']}",
                  flush=True)

        try:
            scanner.run(max_seconds=args.hours * 3600 if args.hours else None,
                        max_products=args.max_products, progress=progress)
        except KeyboardInterrupt:
            print("\nInterrupted — progress is saved. Rerun --run to resume.")

        print_report(store)
        print(f"\nFetched {scanner.fetched:,} pages, {scanner.failed:,} failures, "
              f"{scanner.challenges:,} challenges.")
        print("Run --report to score products and export the CSV.")
        return

    parser.print_help()


if __name__ == "__main__":
    _cli()
