#!/usr/bin/env python3
"""Torrent Search Engine – single-file, resource-optimised."""

# ── Stdlib ──────────────────────────────────────────────────────────────────
import argparse
import gc
import heapq
import os
import platform as _platform_mod
import random
import re
import socket
import struct
import sys
import threading
import time
import weakref
from collections import deque
from concurrent.futures import ThreadPoolExecutor, wait as _fut_wait
from urllib.parse import quote_plus

# ── Third-party ──────────────────────────────────────────────────────────────
import requests
from requests.adapters import HTTPAdapter

try:
    import feedparser as _feedparser
    HAS_FEEDPARSER = True
except ImportError:
    HAS_FEEDPARSER = False

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

try:
    import psutil as _psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False

# ═══════════════════════════════════════════════════════════════════════════════
# PLATFORM DETECTION
# ═══════════════════════════════════════════════════════════════════════════════

class Platform:
    """Auto-detects hardware and sets resource limits accordingly."""

    NAME: str = "unknown"
    WORKERS: int = 4
    MAX_HTTP_BYTES: int = 256 * 1024
    DHT_BUF: int = 250
    DELAY_MIN: float = 0.5
    DELAY_MAX: float = 2.0
    SEMAPHORE: int = 4
    TOP_K_DEFAULT: int = 50

    @classmethod
    def _detect(cls) -> None:
        machine = _platform_mod.machine().lower()
        system = _platform_mod.system().lower()
        node_bits = _platform_mod.architecture()[0]  # '32bit' or '64bit'

        # Raspberry Pi detection
        is_rpi = False
        try:
            with open("/proc/cpuinfo") as f:
                cpuinfo = f.read().lower()
            is_rpi = "raspberry pi" in cpuinfo or "bcm" in cpuinfo
        except OSError:
            pass

        if is_rpi and node_bits == "32bit":
            cls.NAME = "rpi32"
            cls.WORKERS = 2
            cls.MAX_HTTP_BYTES = 96 * 1024
            cls.DHT_BUF = 50
            cls.DELAY_MIN = 1.0
            cls.DELAY_MAX = 3.0
            cls.SEMAPHORE = 2
            cls.TOP_K_DEFAULT = 20
        elif is_rpi:
            cls.NAME = "rpi64"
            cls.WORKERS = 6
            cls.MAX_HTTP_BYTES = 256 * 1024
            cls.DHT_BUF = 250
            cls.DELAY_MIN = 0.5
            cls.DELAY_MAX = 2.0
            cls.SEMAPHORE = 4
            cls.TOP_K_DEFAULT = 50
        elif system == "darwin" and machine == "arm64":
            cls.NAME = "apple_silicon"
            cls.WORKERS = 20
            cls.MAX_HTTP_BYTES = 512 * 1024
            cls.DHT_BUF = 1000
            cls.DELAY_MIN = 0.1
            cls.DELAY_MAX = 0.5
            cls.SEMAPHORE = 10
            cls.TOP_K_DEFAULT = 100
        elif system == "darwin":
            cls.NAME = "mac_intel"
            cls.WORKERS = 12
            cls.MAX_HTTP_BYTES = 512 * 1024
            cls.DHT_BUF = 500
            cls.DELAY_MIN = 0.2
            cls.DELAY_MAX = 1.0
            cls.SEMAPHORE = 6
            cls.TOP_K_DEFAULT = 100
        elif system == "windows":
            cls.NAME = "windows"
            cls.WORKERS = 12
            cls.MAX_HTTP_BYTES = 512 * 1024
            cls.DHT_BUF = 500
            cls.DELAY_MIN = 0.2
            cls.DELAY_MAX = 1.0
            cls.SEMAPHORE = 6
            cls.TOP_K_DEFAULT = 100
        else:
            # Linux x86 / generic
            cls.NAME = "linux_x86"
            cls.WORKERS = 12
            cls.MAX_HTTP_BYTES = 512 * 1024
            cls.DHT_BUF = 500
            cls.DELAY_MIN = 0.2
            cls.DELAY_MAX = 1.0
            cls.SEMAPHORE = 6
            cls.TOP_K_DEFAULT = 100

    @classmethod
    def summary(cls) -> str:
        return (
            f"Platform={cls.NAME}  workers={cls.WORKERS}  "
            f"max_http={cls.MAX_HTTP_BYTES//1024}KB  "
            f"dht_buf={cls.DHT_BUF}  top_k={cls.TOP_K_DEFAULT}"
        )

    @classmethod
    def warn_if_needed(cls) -> str:
        if not HAS_PSUTIL:
            return ""
        try:
            mem = _psutil.virtual_memory()
            if mem.available < 256 * 1024 * 1024:
                return f"WARNING: Only {mem.available // (1024*1024)}MB RAM available – consider --top-k 20"
        except Exception:
            pass
        return ""


Platform._detect()


# ═══════════════════════════════════════════════════════════════════════════════
# DATA MODELS
# ═══════════════════════════════════════════════════════════════════════════════

class Torrent:
    __slots__ = ("infohash", "name", "size", "seeds", "leeches", "source", "magnet")

    def __init__(
        self,
        infohash: str,
        name: str,
        size: int = 0,
        seeds: int = 0,
        leeches: int = 0,
        source: str = "",
        magnet: str = "",
    ) -> None:
        self.infohash = infohash.lower().strip()
        self.name = name
        self.size = size
        self.seeds = seeds
        self.leeches = leeches
        self.source = source
        self.magnet = magnet

    def __repr__(self) -> str:
        size_mb = self.size / (1024 * 1024) if self.size else 0
        return (
            f"[{self.source}] {self.name[:60]}  "
            f"S:{self.seeds} L:{self.leeches}  {size_mb:.1f}MB"
        )


_USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:123.0) Gecko/20100101 Firefox/123.0",
]

_REFERERS = [
    "https://www.google.com/",
    "https://duckduckgo.com/",
    "https://www.bing.com/",
    "https://search.yahoo.com/",
    "https://www.ecosia.org/",
]


class PrivacyConfig:
    __slots__ = ("proxy", "use_doh", "no_delay", "ua_index", "passive_dht")

    def __init__(
        self,
        proxy: str = "",
        use_doh: bool = False,
        no_delay: bool = False,
        passive_dht: bool = True,
    ) -> None:
        # Force socks5h:// for DNS-leak protection
        if proxy and proxy.startswith("socks5://"):
            proxy = "socks5h://" + proxy[len("socks5://"):]
        self.proxy = proxy
        self.use_doh = use_doh
        self.no_delay = no_delay
        self.ua_index = random.randint(0, len(_USER_AGENTS) - 1)
        self.passive_dht = passive_dht

    @property
    def user_agent(self) -> str:
        return _USER_AGENTS[self.ua_index % len(_USER_AGENTS)]

    def rotate_ua(self) -> None:
        self.ua_index = (self.ua_index + 1) % len(_USER_AGENTS)

    @property
    def proxies(self) -> dict:
        if self.proxy:
            return {"http": self.proxy, "https": self.proxy}
        return {}


# ═══════════════════════════════════════════════════════════════════════════════
# GLOBALS / POOL
# ═══════════════════════════════════════════════════════════════════════════════

_adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8)
_POOL_SESSION = requests.Session()
_POOL_SESSION.mount("http://", _adapter)
_POOL_SESSION.mount("https://", _adapter)

_HTTP_SEM = threading.Semaphore(Platform.SEMAPHORE)

POOL = ThreadPoolExecutor(max_workers=Platform.WORKERS, thread_name_prefix="ts")

# ═══════════════════════════════════════════════════════════════════════════════
# RESULT CACHE  (weakref + TTL=300s)
# ═══════════════════════════════════════════════════════════════════════════════

class _CacheEntry:
    """Holder so WeakValueDictionary can hold results."""
    __slots__ = ("data", "ts", "__weakref__")

    def __init__(self, data: list, ts: float) -> None:
        self.data = data
        self.ts = ts


class ResultCache:
    def __init__(self, ttl: float = 300.0) -> None:
        self._ttl = ttl
        self._weak: weakref.WeakValueDictionary = weakref.WeakValueDictionary()
        self._strong: dict = {}   # keeps strong refs alive until TTL
        self._lock = threading.Lock()

    def get(self, key: str):
        with self._lock:
            entry = self._weak.get(key)
            if entry is None:
                return None
            if time.monotonic() - entry.ts > self._ttl:
                self._strong.pop(key, None)
                return None
            return entry.data

    def set(self, key: str, data: list) -> None:
        with self._lock:
            entry = _CacheEntry(data, time.monotonic())
            self._strong[key] = entry
            self._weak[key] = entry

    def clear(self) -> None:
        with self._lock:
            self._strong.clear()


CACHE = ResultCache()


# ═══════════════════════════════════════════════════════════════════════════════
# SQLITE HISTORY
# ═══════════════════════════════════════════════════════════════════════════════

import sqlite3 as _sqlite3

_HISTORY_SCHEMA = """
CREATE TABLE IF NOT EXISTS searches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    ts REAL NOT NULL,
    result_count INTEGER
);
CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    query TEXT NOT NULL,
    infohash TEXT NOT NULL,
    name TEXT,
    size INTEGER,
    seeds INTEGER,
    leeches INTEGER,
    source TEXT,
    ts REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_results_query ON results(query);
"""


class SearchHistory:
    def __init__(self, db_path: str = "~/.torrent_search.db") -> None:
        self._path = os.path.expanduser(db_path)
        self._lock = threading.Lock()
        self._conn: _sqlite3.Connection = None
        self._open()

    def _open(self) -> None:
        try:
            self._conn = _sqlite3.connect(self._path, check_same_thread=False)
            self._conn.executescript(_HISTORY_SCHEMA)
            self._conn.commit()
        except Exception:
            self._conn = None

    def record_search(self, query: str, result_count: int) -> None:
        if not self._conn:
            return
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO searches (query, ts, result_count) VALUES (?, ?, ?)",
                    (query, time.time(), result_count),
                )
                self._conn.commit()
            except Exception:
                pass

    def save_results(self, query: str, results: list) -> None:
        if not self._conn:
            return
        ts = time.time()
        rows = [
            (query, t.infohash, t.name, t.size, t.seeds, t.leeches, t.source, ts)
            for t in results
            if t.infohash
        ]
        if not rows:
            return
        with self._lock:
            try:
                # Delete old entries for same query before saving new ones
                self._conn.execute("DELETE FROM results WHERE query = ?", (query,))
                self._conn.executemany(
                    "INSERT INTO results (query, infohash, name, size, seeds, leeches, source, ts) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    rows,
                )
                self._conn.commit()
            except Exception:
                pass

    def get_recent(self, limit: int = 20) -> list:
        if not self._conn:
            return []
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT query, ts, result_count FROM searches "
                    "ORDER BY ts DESC LIMIT ?",
                    (limit,),
                )
                return [
                    {"query": r[0], "ts": r[1], "count": r[2]} for r in cur.fetchall()
                ]
            except Exception:
                return []

    def get_cached(self, query: str, max_age_s: int = 3600):
        if not self._conn:
            return None
        cutoff = time.time() - max_age_s
        with self._lock:
            try:
                cur = self._conn.execute(
                    "SELECT infohash, name, size, seeds, leeches, source FROM results "
                    "WHERE query = ? AND ts > ? ORDER BY seeds DESC",
                    (query, cutoff),
                )
                rows = cur.fetchall()
                if not rows:
                    return None
                return [
                    Torrent(r[0], r[1] or "", r[2] or 0, r[3] or 0, r[4] or 0, r[5] or "")
                    for r in rows
                ]
            except Exception:
                return None

    def clear(self) -> None:
        if not self._conn:
            return
        with self._lock:
            try:
                self._conn.execute("DELETE FROM searches")
                self._conn.execute("DELETE FROM results")
                self._conn.commit()
            except Exception:
                pass

    def close(self) -> None:
        if self._conn:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None


HISTORY = SearchHistory()


# ═══════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ═══════════════════════════════════════════════════════════════════════════════

_DOH_CACHE: dict = {}
_DOH_LOCK = threading.Lock()
_doh_original_getaddrinfo = socket.getaddrinfo


def _doh_getaddrinfo(host, port, *args, **kwargs):
    with _DOH_LOCK:
        cached_ip = _DOH_CACHE.get(host)
    if cached_ip:
        return _doh_original_getaddrinfo(cached_ip, port, *args, **kwargs)
    try:
        resp = requests.get(
            "https://cloudflare-dns.com/dns-query",
            params={"name": host, "type": "A"},
            headers={"Accept": "application/dns-json"},
            timeout=5,
        )
        for answer in resp.json().get("Answer", []):
            if answer.get("type") == 1:
                ip = answer["data"]
                with _DOH_LOCK:
                    _DOH_CACHE[host] = ip
                return _doh_original_getaddrinfo(ip, port, *args, **kwargs)
    except Exception:
        pass
    return _doh_original_getaddrinfo(host, port, *args, **kwargs)


def enable_doh() -> None:
    socket.getaddrinfo = _doh_getaddrinfo


def fetch_text(
    url: str,
    cfg: PrivacyConfig,
    max_bytes: int = 0,
    timeout: int = 10,
) -> str:
    """Streaming fetch – never loads more than max_bytes into RAM."""
    if max_bytes <= 0:
        max_bytes = Platform.MAX_HTTP_BYTES
    with _HTTP_SEM:
        if not cfg.no_delay:
            time.sleep(random.uniform(Platform.DELAY_MIN, Platform.DELAY_MAX))
        cfg.rotate_ua()
        headers = {
            "User-Agent": cfg.user_agent,
            "Referer": random.choice(_REFERERS),
            "Accept-Language": "en-US,en;q=0.9",
        }
        last_exc = None
        for attempt in range(2):
            try:
                resp = _POOL_SESSION.get(
                    url,
                    headers=headers,
                    proxies=cfg.proxies,
                    stream=True,
                    timeout=timeout,
                    verify=True,
                )
                if resp.status_code in (429, 503) and attempt == 0:
                    resp.close()
                    if not cfg.no_delay:
                        time.sleep(2)
                    continue
                resp.raise_for_status()
                chunks = []
                total = 0
                for chunk in resp.iter_content(chunk_size=4096):
                    if chunk:
                        chunks.append(chunk)
                        total += len(chunk)
                        if total >= max_bytes:
                            break
                resp.close()
                return b"".join(chunks).decode("utf-8", errors="replace")
            except Exception as exc:
                last_exc = exc
                if attempt == 0:
                    continue
        return ""


def dedup_stream(iterable):
    """Yield torrents, skipping already-seen infohashes."""
    seen: set = set()
    for t in iterable:
        if t.infohash and t.infohash not in seen:
            seen.add(t.infohash)
            yield t


def top_k_stream(iterable, k: int) -> list:
    """Return top-k torrents by seeds using a min-heap O(n log k)."""
    heap = []  # (seeds, index, torrent) – index breaks ties
    idx = 0
    for t in iterable:
        heapq.heappush(heap, (t.seeds, idx, t))
        idx += 1
        if len(heap) > k:
            heapq.heappop(heap)
    # Sort descending by seeds
    result = [item[2] for item in sorted(heap, key=lambda x: -x[0])]
    return result


_PUBLIC_TRACKERS = [
    "udp://tracker.openbittorrent.com:80/announce",
    "udp://opentracker.i2p.rocks:6969/announce",
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://open.stealth.si:80/announce",
]


def build_magnet(t: Torrent, cfg: PrivacyConfig) -> str:
    """Build a magnet link with 5 public trackers."""
    ih = t.infohash
    name_enc = quote_plus(t.name)
    trackers = "&".join(f"tr={quote_plus(tr)}" for tr in _PUBLIC_TRACKERS)
    return f"magnet:?xt=urn:btih:{ih}&dn={name_enc}&{trackers}"


def _ram_mb() -> float:
    if HAS_PSUTIL:
        try:
            return _psutil.Process().memory_info().rss / (1024 * 1024)
        except Exception:
            pass
    return 0.0


_SIZE_UNITS = {"kb": 1024, "mb": 1024**2, "gb": 1024**3, "tb": 1024**4}

_FILTER_RE = re.compile(
    r"""
    (?P<key>seeds|leeches|size|source)   # field name
    (?P<op>[<>=])                        # operator
    (?P<val>\S+)                         # value (no spaces)
    """,
    re.VERBOSE | re.IGNORECASE,
)


def parse_filters(raw_query: str) -> tuple:
    """Split 'ubuntu seeds>50 size<2GB' → ('ubuntu', {'seeds_min':50, 'size_max':2GB_bytes})."""
    filters: dict = {}
    clean_parts = []
    for token in raw_query.split():
        m = _FILTER_RE.fullmatch(token)
        if not m:
            clean_parts.append(token)
            continue
        key = m.group("key").lower()
        op = m.group("op")
        val_str = m.group("val").lower()

        if key in ("seeds", "leeches"):
            try:
                n = int(val_str)
            except ValueError:
                clean_parts.append(token)
                continue
            suffix = "_min" if op in (">", "=") else "_max"
            filters[key + suffix] = n

        elif key == "size":
            unit = 1
            for unit_str, unit_bytes in _SIZE_UNITS.items():
                if val_str.endswith(unit_str):
                    val_str = val_str[: -len(unit_str)]
                    unit = unit_bytes
                    break
            try:
                n = float(val_str) * unit
            except ValueError:
                clean_parts.append(token)
                continue
            suffix = "_min" if op in (">", "=") else "_max"
            filters["size" + suffix] = int(n)

        elif key == "source":
            if op == "=":
                filters["source"] = val_str

    return " ".join(clean_parts).strip(), filters


def apply_filters(results: list, filters: dict) -> list:
    """Filter results list according to parsed filter dict."""
    if not filters:
        return results
    out = []
    for t in results:
        if "seeds_min" in filters and t.seeds < filters["seeds_min"]:
            continue
        if "seeds_max" in filters and t.seeds > filters["seeds_max"]:
            continue
        if "leeches_min" in filters and t.leeches < filters["leeches_min"]:
            continue
        if "leeches_max" in filters and t.leeches > filters["leeches_max"]:
            continue
        if "size_min" in filters and t.size < filters["size_min"]:
            continue
        if "size_max" in filters and t.size > filters["size_max"] and t.size > 0:
            continue
        if "source" in filters and t.source.lower() != filters["source"]:
            continue
        out.append(t)
    return out


def format_results(results: list, fmt: str, cfg: "PrivacyConfig" = None) -> str:
    """Serialise results to JSON or CSV string."""
    import json
    import csv
    import io

    if fmt == "json":
        rows = []
        for t in results:
            rows.append({
                "infohash": t.infohash,
                "name": t.name,
                "size": t.size,
                "seeds": t.seeds,
                "leeches": t.leeches,
                "source": t.source,
                "magnet": build_magnet(t, cfg) if cfg else "",
            })
        return json.dumps(rows, indent=2, ensure_ascii=False)

    elif fmt == "csv":
        buf = io.StringIO()
        fieldnames = ["infohash", "name", "size", "seeds", "leeches", "source"]
        w = csv.DictWriter(buf, fieldnames=fieldnames, lineterminator="\n")
        w.writeheader()
        for t in results:
            w.writerow({
                "infohash": t.infohash,
                "name": t.name,
                "size": t.size,
                "seeds": t.seeds,
                "leeches": t.leeches,
                "source": t.source,
            })
        return buf.getvalue()

    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# STREAM SOURCES
# ═══════════════════════════════════════════════════════════════════════════════

def _stream_apibay(query: str, cfg: PrivacyConfig):
    """Method 1 – TPB/apibay JSON API."""
    url = f"https://apibay.org/q.php?q={quote_plus(query)}&cat=0"
    text = fetch_text(url, cfg, timeout=12)
    if not text:
        return
    try:
        import json
        items = json.loads(text)
        if not isinstance(items, list):
            return
        for item in items:
            ih = item.get("info_hash", "")
            name = item.get("name", "")
            if not ih or name in ("No results returned", ""):
                continue
            try:
                size = int(item.get("size", 0))
            except (ValueError, TypeError):
                size = 0
            try:
                seeds = int(item.get("seeders", 0))
            except (ValueError, TypeError):
                seeds = 0
            try:
                leeches = int(item.get("leechers", 0))
            except (ValueError, TypeError):
                leeches = 0
            yield Torrent(ih, name, size, seeds, leeches, "apibay")
    except Exception:
        return


def _stream_nyaa(query: str, cfg: PrivacyConfig):
    """Method 2 – Nyaa RSS feed."""
    if not HAS_FEEDPARSER:
        return
    url = f"https://nyaa.si/?page=rss&q={quote_plus(query)}"
    text = fetch_text(url, cfg, timeout=12)
    if not text:
        return
    try:
        feed = _feedparser.parse(text)
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "")
            # Extract infohash from magnet or torrent link
            ih = ""
            magnet = entry.get("nyaa_infohash", "") or entry.get("id", "")
            m = re.search(r"btih:([0-9a-fA-F]{40})", link + magnet)
            if m:
                ih = m.group(1)
            if not ih:
                continue
            try:
                seeds = int(entry.get("nyaa_seeders", 0))
            except (ValueError, TypeError):
                seeds = 0
            try:
                leeches = int(entry.get("nyaa_leechers", 0))
            except (ValueError, TypeError):
                leeches = 0
            yield Torrent(ih, title, 0, seeds, leeches, "nyaa")
    except Exception:
        return


def _fetch_rss_feed(args):
    """Helper for parallel RSS fetching."""
    url, cfg, source = args
    if not HAS_FEEDPARSER:
        return []
    text = fetch_text(url, cfg, timeout=10)
    if not text:
        return []
    results = []
    try:
        feed = _feedparser.parse(text)
        for entry in feed.entries:
            title = entry.get("title", "")
            link = entry.get("link", "") or entry.get("id", "")
            enclosure = ""
            if entry.get("enclosures"):
                enclosure = entry["enclosures"][0].get("href", "")
            combined = link + enclosure
            m = re.search(r"btih:([0-9a-fA-F]{40})", combined, re.I)
            if not m:
                m = re.search(r"/([0-9a-fA-F]{40})", combined, re.I)
            if not m:
                continue
            ih = m.group(1)
            results.append(Torrent(ih, title, 0, 0, 0, source))
    except Exception:
        pass
    return results


_RSS_FEEDS = [
    ("https://eztv.re/ezrss.xml", "eztv"),
    ("https://torrentgalaxy.to/rss.xml", "tgx"),
    ("https://showrss.info/other/all.rss", "showrss"),
]


def _stream_rss(query: str, cfg: PrivacyConfig):
    """Method 3 – Parallel RSS: EZTV + TorrentGalaxy + Showrss."""
    args_list = [(url, cfg, src) for url, src in _RSS_FEEDS]
    futs = [POOL.submit(_fetch_rss_feed, a) for a in args_list]
    done, _ = _fut_wait(futs, timeout=15)
    for fut in done:
        try:
            for t in fut.result():
                if query.lower() in t.name.lower():
                    yield t
        except Exception:
            pass


def _stream_scraping(query: str, cfg: PrivacyConfig):
    """Method 4 – BeautifulSoup scraping: LimeTorrents + 1337x mirror."""
    if not HAS_BS4:
        return

    def _scrape_limetorrents(q):
        url = f"https://www.limetorrents.lol/search/all/{quote_plus(q)}/"
        text = fetch_text(url, cfg, timeout=12)
        if not text:
            return []
        results = []
        try:
            soup = BeautifulSoup(text, "lxml")
            for row in soup.select("table.table2 tr"):
                cells = row.find_all("td")
                if len(cells) < 4:
                    continue
                a_tag = cells[0].find("a", href=True)
                if not a_tag:
                    continue
                name = a_tag.get_text(strip=True)
                href = a_tag["href"]
                m = re.search(r"/([0-9a-fA-F]{40})", href)
                if not m:
                    continue
                ih = m.group(1)
                seeds_text = cells[-2].get_text(strip=True).replace(",", "")
                leeches_text = cells[-1].get_text(strip=True).replace(",", "")
                try:
                    seeds = int(seeds_text)
                except ValueError:
                    seeds = 0
                try:
                    leeches = int(leeches_text)
                except ValueError:
                    leeches = 0
                results.append(Torrent(ih, name, 0, seeds, leeches, "limetorrents"))
        except Exception:
            pass
        return results

    def _scrape_1337x(q):
        mirrors = [
            "https://1337x.to",
            "https://1337x.st",
        ]
        for mirror in mirrors:
            url = f"{mirror}/search/{quote_plus(q)}/1/"
            text = fetch_text(url, cfg, timeout=12)
            if not text:
                continue
            results = []
            try:
                soup = BeautifulSoup(text, "lxml")
                for row in soup.select("table.table-list tbody tr"):
                    cells = row.find_all("td")
                    if len(cells) < 4:
                        continue
                    a_tags = cells[0].find_all("a", href=True)
                    name = ""
                    href = ""
                    for a in a_tags:
                        if "/torrent/" in a["href"]:
                            name = a.get_text(strip=True)
                            href = a["href"]
                            break
                    if not name:
                        continue
                    # seeds/leeches in cells[1] and cells[2]
                    try:
                        seeds = int(cells[1].get_text(strip=True).replace(",", ""))
                    except ValueError:
                        seeds = 0
                    try:
                        leeches = int(cells[2].get_text(strip=True).replace(",", ""))
                    except ValueError:
                        leeches = 0
                    results.append((href, name, seeds, leeches))
                # Fetch detail pages for first 5 results to get real infohash
                final = []
                for detail_href, name, seeds, leeches in results[:5]:
                    ih = ""
                    try:
                        detail_url = mirror + detail_href
                        detail_text = fetch_text(detail_url, cfg, max_bytes=32 * 1024, timeout=10)
                        m = re.search(r"btih:([0-9a-fA-F]{40})", detail_text, re.I)
                        if m:
                            ih = m.group(1)
                    except Exception:
                        pass
                    if ih:
                        final.append(Torrent(ih, name, 0, seeds, leeches, "1337x"))
                if final:
                    return final
            except Exception:
                continue
        return []

    fut_lime = POOL.submit(_scrape_limetorrents, query)
    fut_1337 = POOL.submit(_scrape_1337x, query)
    done, _ = _fut_wait([fut_lime, fut_1337], timeout=20)
    for fut in done:
        try:
            for t in fut.result():
                yield t
        except Exception:
            pass


def _stream_torrentproject(query: str, cfg: PrivacyConfig):
    """Method 5 – TorrentProject JSON API + BTDigg fallback."""
    import json

    # TorrentProject
    url = f"https://torrentproject2.se/?t={quote_plus(query)}&out=json&orderby=seeders"
    text = fetch_text(url, cfg, timeout=12)
    yielded = 0
    if text:
        try:
            data = json.loads(text)
            total = data.get("total_found", 0)
            for key, val in data.items():
                if key in ("total_found",) or not isinstance(val, dict):
                    continue
                ih = val.get("torrent_hash", "")
                name = val.get("title", "")
                if not ih or not name:
                    continue
                try:
                    seeds = int(val.get("seeds", 0))
                except (ValueError, TypeError):
                    seeds = 0
                try:
                    leeches = int(val.get("leechs", 0))
                except (ValueError, TypeError):
                    leeches = 0
                yield Torrent(ih, name, 0, seeds, leeches, "torrentproject")
                yielded += 1
        except Exception:
            pass

    # BTDigg fallback if nothing found
    if yielded == 0 and HAS_BS4:
        url2 = f"https://btdig.com/search?q={quote_plus(query)}&p=0&order=1"
        text2 = fetch_text(url2, cfg, timeout=12)
        if text2:
            try:
                soup = BeautifulSoup(text2, "lxml")
                for item in soup.select(".one_result"):
                    a = item.select_one(".torrent_name a")
                    if not a:
                        continue
                    name = a.get_text(strip=True)
                    href = a.get("href", "")
                    m = re.search(r"([0-9a-fA-F]{40})", href)
                    if not m:
                        continue
                    ih = m.group(1)
                    yield Torrent(ih, name, 0, 0, 0, "btdigg")
            except Exception:
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# DHT SNIFFER
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_infohash(data: bytes):
    marker = b"9:info_hash20:"
    idx = data.find(marker)
    if idx == -1:
        return None
    start = idx + len(marker)
    if start + 20 <= len(data):
        return data[start : start + 20].hex()
    return None


def _extract_nodes(data: bytes) -> list:
    """Return list of (ip_str, port) from bencoded nodes string."""
    marker = b"5:nodes"
    idx = data.find(marker)
    if idx == -1:
        return []
    try:
        pos = idx + len(marker)
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        raw = data[colon + 1 : colon + 1 + length]
        nodes = []
        for i in range(0, len(raw) - 25, 26):
            ip = socket.inet_ntoa(raw[i + 20 : i + 24])
            port = struct.unpack("!H", raw[i + 24 : i + 26])[0]
            if port > 0:
                nodes.append((ip, port))
        return nodes
    except (ValueError, IndexError, struct.error):
        return []


class DHTSniffer:
    __slots__ = ("_sock", "_thread", "_running", "_node_id", "_port", "_queue", "passive")

    def __init__(self, passive: bool = True) -> None:
        self._sock = None
        self._thread = None
        self._running = False
        self._node_id = b""
        self._port = 0
        self._queue: deque = deque(maxlen=Platform.DHT_BUF)
        self.passive = passive

    def start(self) -> None:
        self._node_id = os.urandom(20)
        self._port = random.randint(10000, 60000)
        self._running = True
        self._queue.clear()

        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.settimeout(1.0)
        try:
            self._sock.bind(("0.0.0.0", self._port))
        except OSError:
            # If port is in use, let OS assign one
            self._sock.bind(("0.0.0.0", 0))
            self._port = self._sock.getsockname()[1]

        self._thread = threading.Thread(
            target=self._loop, name="dht-sniffer", daemon=True
        )
        self._thread.start()

        # Bootstrap: send find_node to router.bittorrent.com
        try:
            bootstrap_host = socket.gethostbyname("router.bittorrent.com")
            self._ping((bootstrap_host, 6881))
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass

    def _ping(self, addr: tuple) -> None:
        """Send a find_node query to bootstrap into the DHT."""
        if self.passive:
            return
        tid = os.urandom(2)
        # Minimal bencoded find_node query
        nid = self._node_id
        target = os.urandom(20)
        msg = (
            b"d1:ad2:id20:" + nid + b"6:target20:" + target + b"e"
            b"1:q9:find_node1:t2:" + tid + b"1:y1:qe"
        )
        try:
            self._sock.sendto(msg, addr)
        except Exception:
            pass

    def _loop(self) -> None:
        while self._running:
            try:
                data, addr = self._sock.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                break

            # Extract infohash from announce_peer messages
            ih = _extract_infohash(data)
            if ih:
                self._queue.append(ih)

            # Discover new nodes (passive: just collect, don't respond)
            nodes = _extract_nodes(data)
            if nodes and not self.passive:
                # Only ping new nodes if not in passive mode
                for ip, port in nodes[:3]:
                    try:
                        self._ping((ip, port))
                    except Exception:
                        pass

    def stream(self):
        """Yield Torrent stubs from observed DHT infohashes."""
        seen: set = set()
        deadline = time.monotonic() + 5  # collect for up to 5s
        while time.monotonic() < deadline or self._running:
            while self._queue:
                ih = self._queue.popleft()
                if ih not in seen:
                    seen.add(ih)
                    yield Torrent(ih, f"DHT:{ih[:16]}...", source="dht")
            time.sleep(0.1)
            if not self._running:
                break


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN ENGINE
# ═══════════════════════════════════════════════════════════════════════════════

class TorrentSearchEngine:
    def __init__(
        self,
        cfg: PrivacyConfig = None,
        dht_seconds: int = 0,
        top_k: int = 0,
    ) -> None:
        self.cfg = cfg or PrivacyConfig()
        self.dht_seconds = dht_seconds
        self.top_k = top_k or Platform.TOP_K_DEFAULT
        self._dht: DHTSniffer = None

    def search(self, query: str) -> list:
        # 1. RAM cache (300s)
        cached = CACHE.get(query)
        if cached is not None:
            print(f"  [cache hit] {len(cached)} Ergebnisse")
            return cached

        # 2. SQLite cache (1h)
        db_cached = HISTORY.get_cached(query, max_age_s=3600)
        if db_cached:
            print(f"  [db cache hit] {len(db_cached)} Ergebnisse")
            CACHE.set(query, db_cached)
            return db_cached

        ram_before = _ram_mb()

        methods = [
            _stream_apibay,
            _stream_nyaa,
            _stream_rss,
            _stream_scraping,
            _stream_torrentproject,
        ]

        shared_buf = []
        buf_lock = threading.Lock()

        def run_method(fn):
            try:
                for t in fn(query, self.cfg):
                    with buf_lock:
                        shared_buf.append(t)
            except Exception:
                pass

        futs = [POOL.submit(run_method, fn) for fn in methods]

        # DHT collection
        dht_fut = None
        if self.dht_seconds > 0:
            if self._dht is None:
                self._dht = DHTSniffer(passive=self.cfg.passive_dht)
                self._dht.start()

            def collect_dht():
                deadline = time.monotonic() + self.dht_seconds
                while time.monotonic() < deadline:
                    while self._dht._queue:
                        ih = self._dht._queue.popleft()
                        with buf_lock:
                            shared_buf.append(
                                Torrent(ih, f"DHT:{ih[:16]}...", source="dht")
                            )
                    time.sleep(0.2)

            dht_fut = POOL.submit(collect_dht)
            futs.append(dht_fut)

        _fut_wait(futs, timeout=20)

        pipeline = dedup_stream(iter(shared_buf))
        results = top_k_stream(pipeline, self.top_k)

        gc.collect()

        ram_after = _ram_mb()
        if ram_before > 0:
            print(f"  RAM: {ram_before:.1f}MB → {ram_after:.1f}MB")

        CACHE.set(query, results)
        HISTORY.record_search(query, len(results))
        HISTORY.save_results(query, results)
        return results

    def close(self) -> None:
        if self._dht:
            self._dht.stop()


# ═══════════════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════════════

def privacy_wizard() -> PrivacyConfig:
    print("\n=== Privacy-Wizard ===")
    proxy = ""
    use_doh = False
    no_delay = False
    passive_dht = True

    ans = input("Proxy verwenden? (socks5://host:port oder leer) > ").strip()
    if ans:
        proxy = ans

    ans = input("DNS-over-HTTPS (Cloudflare) aktivieren? [j/N] > ").strip().lower()
    use_doh = ans in ("j", "y", "ja", "yes")

    ans = input("Delays zwischen Requests deaktivieren? [j/N] > ").strip().lower()
    no_delay = ans in ("j", "y", "ja", "yes")

    ans = input("DHT passiv-Modus (nur lauschen, nie antworten)? [J/n] > ").strip().lower()
    passive_dht = ans not in ("n", "no", "nein")

    cfg = PrivacyConfig(proxy=proxy, use_doh=use_doh, no_delay=no_delay, passive_dht=passive_dht)
    if use_doh:
        enable_doh()
        print("DoH aktiviert (Cloudflare)")
    print(f"Proxy: {cfg.proxy or 'keiner'}  DoH: {cfg.use_doh}  Delays: {not cfg.no_delay}  DHT-passiv: {cfg.passive_dht}")
    return cfg


def _get_prompt() -> str:
    ram_str = ""
    cpu_str = ""
    if HAS_PSUTIL:
        try:
            proc = _psutil.Process()
            ram_mb = proc.memory_info().rss / (1024 * 1024)
            cpu = _psutil.cpu_percent(interval=None)
            ram_str = f"RAM {ram_mb:.0f}MB"
            cpu_str = f" CPU {cpu:.0f}%"
        except Exception:
            pass
    if ram_str:
        return f"🔎 Suche [{ram_str}{cpu_str}]> "
    return "🔎 Suche> "


def run_cli(args) -> None:
    cfg = PrivacyConfig(
        proxy=args.proxy or "",
        use_doh=args.doh,
        no_delay=args.no_delay,
        passive_dht=not args.dht_sec,
    )
    if cfg.use_doh:
        enable_doh()

    top_k = args.top_k or Platform.TOP_K_DEFAULT
    dht_seconds = args.dht_sec

    engine = TorrentSearchEngine(cfg=cfg, dht_seconds=dht_seconds, top_k=top_k)

    print("\n" + "=" * 60)
    print("  Torrent Search Engine")
    print(f"  {Platform.summary()}")
    warn = Platform.warn_if_needed()
    if warn:
        print(f"  {warn}")
    print("=" * 60)
    print("  Befehle: privacy | cache | ram | history | history clear | q")
    print("  Filter-Syntax: 'ubuntu seeds>50 size<2GB source=apibay'")
    print("  Nummer eingeben nach Suche für Magnet-Link\n")

    last_results: list = []

    while True:
        try:
            prompt = _get_prompt()
            query = input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            print("\nBeenden…")
            break

        if not query:
            continue
        if query.lower() in ("q", "quit", "exit", "beenden"):
            print("Auf Wiedersehen!")
            break
        if query.lower() == "privacy":
            cfg = privacy_wizard()
            engine.cfg = cfg
            continue
        if query.lower() == "cache":
            CACHE.clear()
            print("RAM-Cache geleert.")
            continue
        if query.lower() == "history clear":
            HISTORY.clear()
            CACHE.clear()
            print("Suchverlauf und Cache geleert.")
            continue
        if query.lower() == "history":
            recent = HISTORY.get_recent(limit=20)
            if not recent:
                print("  Kein Suchverlauf vorhanden.")
            else:
                print(f"\n  Letzte {len(recent)} Suchen:\n")
                for i, entry in enumerate(recent, 1):
                    ts_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(entry["ts"]))
                    print(f"  {i:3d}. {ts_str}  {entry['query']:<40s}  {entry['count'] or 0:4d} Ergebnisse")
                print()
            continue
        if query.lower() == "ram":
            if HAS_PSUTIL:
                try:
                    proc = _psutil.Process()
                    rss = proc.memory_info().rss / (1024 * 1024)
                    vm = _psutil.virtual_memory()
                    print(f"Prozess: {rss:.1f}MB  System verfügbar: {vm.available//(1024*1024)}MB")
                except Exception:
                    print("psutil nicht verfügbar")
            else:
                print("psutil nicht installiert")
            continue

        # Check if input is a number -> show magnet link
        if query.isdigit():
            idx = int(query) - 1
            if 0 <= idx < len(last_results):
                t = last_results[idx]
                magnet = build_magnet(t, cfg)
                print(f"\nMagnet-Link #{query}:\n{magnet}\n")
            else:
                print(f"Ungültige Nummer (1–{len(last_results)})")
            continue

        # Parse filters from query
        clean_query, filters = parse_filters(query)
        if not clean_query:
            print("  Bitte einen Suchbegriff eingeben.")
            continue
        if filters:
            active = ", ".join(f"{k}={v}" for k, v in filters.items())
            print(f"  Filter aktiv: {active}")

        # Perform search
        print(f"\nSuche nach: {clean_query!r}")
        results = engine.search(clean_query)
        if filters:
            results = apply_filters(results, filters)
        last_results = results

        if not results:
            print("  Keine Ergebnisse gefunden.\n")
            continue

        print(f"\n  {len(results)} Ergebnisse:\n")
        for i, t in enumerate(results, 1):
            size_str = f"{t.size/(1024*1024):.1f}MB" if t.size else "?MB"
            print(f"  {i:3d}. [{t.source:15s}] {t.name[:55]:<55s}  "
                  f"S:{t.seeds:<5d} L:{t.leeches:<5d} {size_str}")
        print("\n  (Nummer eingeben für Magnet-Link)\n")

    engine.close()
    HISTORY.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Torrent-Suchmaschine – privacy-first, ressourcen-optimiert"
    )
    parser.add_argument(
        "--proxy",
        metavar="socks5://host:port",
        default="",
        help="SOCKS5/HTTP-Proxy (socks5h:// wird automatisch gesetzt)",
    )
    parser.add_argument(
        "--doh",
        action="store_true",
        help="DNS-over-HTTPS via Cloudflare aktivieren",
    )
    parser.add_argument(
        "--no-delay",
        action="store_true",
        dest="no_delay",
        help="Zufällige Delays zwischen Requests deaktivieren",
    )
    parser.add_argument(
        "--dht-sec",
        type=int,
        default=0,
        metavar="SEKUNDEN",
        dest="dht_sec",
        help="DHT-Sniffer für N Sekunden mitlaufen lassen (0 = deaktiviert)",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=0,
        metavar="K",
        dest="top_k",
        help=f"Maximalanzahl Ergebnisse (Standard: {Platform.TOP_K_DEFAULT})",
    )
    parser.add_argument(
        "--query",
        metavar="SUCHBEGRIFF",
        default="",
        help="Suchanfrage für nicht-interaktiven Modus (kombinierbar mit --json/--csv)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Ergebnisse als JSON auf stdout ausgeben (nicht-interaktiv)",
    )
    parser.add_argument(
        "--csv",
        action="store_true",
        dest="output_csv",
        help="Ergebnisse als CSV auf stdout ausgeben (nicht-interaktiv)",
    )

    args = parser.parse_args()

    # Non-interactive batch mode
    if args.query and (args.output_json or args.output_csv):
        cfg = PrivacyConfig(
            proxy=args.proxy or "",
            use_doh=args.doh,
            no_delay=args.no_delay,
        )
        if cfg.use_doh:
            enable_doh()
        engine = TorrentSearchEngine(
            cfg=cfg,
            dht_seconds=args.dht_sec,
            top_k=args.top_k or Platform.TOP_K_DEFAULT,
        )
        clean_query, filters = parse_filters(args.query)
        results = engine.search(clean_query)
        if filters:
            results = apply_filters(results, filters)
        fmt = "json" if args.output_json else "csv"
        print(format_results(results, fmt, cfg))
        engine.close()
        HISTORY.close()
        return

    run_cli(args)


if __name__ == "__main__":
    main()
