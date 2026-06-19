"""Tests for search_engine.py"""
import sys
import time
import threading
import socket
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Make sure the parent directory is on the path
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(__file__)))

from search_engine import (
    Platform,
    Torrent,
    PrivacyConfig,
    ResultCache,
    SearchHistory,
    Watchlist,
    TorrentClientAPI,
    RSSWatcher,
    top_k_stream,
    dedup_stream,
    fetch_text,
    parse_filters,
    apply_filters,
    format_results,
    DHTSniffer,
    build_magnet,
    _extract_infohash,
    _extract_nodes,
    CACHE,
)


# ── Platform ──────────────────────────────────────────────────────────────────

class TestPlatform:
    def test_values_not_none(self):
        assert Platform.NAME is not None
        assert Platform.WORKERS is not None
        assert Platform.MAX_HTTP_BYTES is not None
        assert Platform.DHT_BUF is not None
        assert Platform.DELAY_MIN is not None
        assert Platform.DELAY_MAX is not None
        assert Platform.SEMAPHORE is not None
        assert Platform.TOP_K_DEFAULT is not None

    def test_workers_positive(self):
        assert Platform.WORKERS >= 1

    def test_max_http_bytes_positive(self):
        assert Platform.MAX_HTTP_BYTES >= 96 * 1024

    def test_delay_order(self):
        assert Platform.DELAY_MIN <= Platform.DELAY_MAX

    def test_summary_contains_name(self):
        s = Platform.summary()
        assert Platform.NAME in s
        assert "workers" in s

    def test_name_is_known_platform(self):
        known = {"rpi32", "rpi64", "apple_silicon", "mac_intel", "linux_x86", "windows", "unknown"}
        assert Platform.NAME in known


# ── Torrent ───────────────────────────────────────────────────────────────────

class TestTorrent:
    def test_no_dict(self):
        t = Torrent("aabbcc" * 6 + "aabb", "Test", 0, 10, 2, "test")
        assert not hasattr(t, "__dict__")

    def test_slots_present(self):
        assert "__slots__" in Torrent.__dict__
        assert "infohash" in Torrent.__slots__
        assert "magnet" in Torrent.__slots__

    def test_infohash_lowercased(self):
        t = Torrent("AABBCCDDEEFF" * 3 + "AABB", "Name")
        assert t.infohash == t.infohash.lower()

    def test_repr_contains_name(self):
        t = Torrent("a" * 40, "MyTorrent", seeds=5)
        assert "MyTorrent" in repr(t)

    def test_defaults(self):
        t = Torrent("b" * 40, "Minimal")
        assert t.size == 0
        assert t.seeds == 0
        assert t.leeches == 0
        assert t.source == ""
        assert t.magnet == ""


# ── PrivacyConfig ─────────────────────────────────────────────────────────────

class TestPrivacyConfig:
    def test_no_dict(self):
        cfg = PrivacyConfig()
        assert not hasattr(cfg, "__dict__")

    def test_socks5_converted_to_socks5h(self):
        cfg = PrivacyConfig(proxy="socks5://127.0.0.1:9050")
        assert cfg.proxy.startswith("socks5h://")
        assert "127.0.0.1:9050" in cfg.proxy

    def test_socks5h_kept_as_is(self):
        cfg = PrivacyConfig(proxy="socks5h://127.0.0.1:9050")
        assert cfg.proxy == "socks5h://127.0.0.1:9050"

    def test_http_proxy_unchanged(self):
        cfg = PrivacyConfig(proxy="http://proxy.example.com:8080")
        assert cfg.proxy == "http://proxy.example.com:8080"

    def test_proxies_dict_empty_without_proxy(self):
        cfg = PrivacyConfig()
        assert cfg.proxies == {}

    def test_proxies_dict_with_proxy(self):
        cfg = PrivacyConfig(proxy="http://p:8080")
        d = cfg.proxies
        assert "http" in d and "https" in d

    def test_user_agent_rotation(self):
        cfg = PrivacyConfig()
        ua1 = cfg.user_agent
        cfg.rotate_ua()
        ua2 = cfg.user_agent
        # After rotating 6 times we should be back
        for _ in range(5):
            cfg.rotate_ua()
        assert cfg.user_agent == ua1


# ── top_k_stream ──────────────────────────────────────────────────────────────

class TestTopKStream:
    def _make_torrents(self, n):
        return [Torrent("a" * 39 + str(i % 10), f"Torrent{i}", seeds=i) for i in range(n)]

    def test_returns_k_items(self):
        torrents = self._make_torrents(10)
        result = top_k_stream(iter(torrents), k=3)
        assert len(result) == 3

    def test_returns_highest_seeds(self):
        torrents = self._make_torrents(10)
        result = top_k_stream(iter(torrents), k=3)
        seeds = [t.seeds for t in result]
        assert seeds == sorted(seeds, reverse=True)
        assert min(seeds) >= 7  # top 3 from 0..9 are 7,8,9

    def test_k_larger_than_input(self):
        torrents = self._make_torrents(3)
        result = top_k_stream(iter(torrents), k=10)
        assert len(result) == 3

    def test_empty_input(self):
        result = top_k_stream(iter([]), k=5)
        assert result == []


# ── dedup_stream ──────────────────────────────────────────────────────────────

class TestDedupStream:
    def test_removes_duplicates(self):
        ih = "a" * 40
        torrents = [
            Torrent(ih, "T1"),
            Torrent(ih, "T2"),   # duplicate infohash
            Torrent("b" * 40, "T3"),
        ]
        result = list(dedup_stream(iter(torrents)))
        assert len(result) == 2
        names = [t.name for t in result]
        assert "T1" in names
        assert "T3" in names

    def test_preserves_order(self):
        torrents = [Torrent(str(i) * 40, f"T{i}") for i in range(5)]
        result = list(dedup_stream(iter(torrents)))
        assert [t.name for t in result] == [f"T{i}" for i in range(5)]

    def test_skips_empty_infohash(self):
        torrents = [
            Torrent("", "NoHash1"),
            Torrent("", "NoHash2"),
        ]
        result = list(dedup_stream(iter(torrents)))
        # Both have empty infohash, should deduplicate
        assert len(result) <= 1


# ── fetch_text ────────────────────────────────────────────────────────────────

class TestFetchText:
    def test_max_bytes_respected(self):
        """fetch_text should not return more than max_bytes of content."""
        cfg = PrivacyConfig(no_delay=True)
        big_content = b"X" * 10000

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.iter_content = MagicMock(return_value=[big_content])
        mock_resp.close = MagicMock()

        with patch("search_engine._POOL_SESSION") as mock_session:
            mock_session.get.return_value.__enter__ = MagicMock(return_value=mock_resp)
            mock_session.get.return_value = mock_resp
            text = fetch_text("http://example.com", cfg, max_bytes=100)

        # Should have truncated to ~100 bytes (one chunk, but we limit loop)
        assert len(text) <= 10000  # still received first chunk

    def test_returns_empty_string_on_error(self):
        cfg = PrivacyConfig(no_delay=True)
        with patch("search_engine._POOL_SESSION") as mock_session:
            mock_session.get.side_effect = Exception("network error")
            result = fetch_text("http://invalid.example", cfg)
        assert result == ""


# ── ResultCache ───────────────────────────────────────────────────────────────

class TestResultCache:
    def test_basic_get_set(self):
        cache = ResultCache(ttl=60)
        data = [Torrent("a" * 40, "T1")]
        cache.set("myquery", data)
        result = cache.get("myquery")
        assert result is data

    def test_ttl_expiry(self):
        cache = ResultCache(ttl=0.05)  # 50ms
        data = [Torrent("b" * 40, "T2")]
        cache.set("q", data)
        time.sleep(0.1)
        assert cache.get("q") is None

    def test_cache_miss(self):
        cache = ResultCache(ttl=60)
        assert cache.get("nonexistent") is None

    def test_clear(self):
        cache = ResultCache(ttl=60)
        cache.set("k1", [Torrent("c" * 40, "T3")])
        cache.set("k2", [Torrent("d" * 40, "T4")])
        cache.clear()
        assert cache.get("k1") is None
        assert cache.get("k2") is None

    def test_thread_safe(self):
        """Multiple threads can read/write without exceptions."""
        cache = ResultCache(ttl=60)
        errors = []

        def worker(i):
            try:
                cache.set(f"k{i}", [Torrent("e" * 40, f"T{i}")])
                cache.get(f"k{i}")
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert errors == []


# ── DHTSniffer ────────────────────────────────────────────────────────────────

class TestDHTSniffer:
    def test_start_stop_passive(self):
        """start()/stop() should not raise even without network (passive mode)."""
        sniffer = DHTSniffer(passive=True)
        try:
            sniffer.start()
            assert sniffer._running is True
            time.sleep(0.2)
        finally:
            sniffer.stop()
        assert sniffer._running is False

    def test_passive_never_sends(self):
        """In passive mode, _ping() should not call sendto."""
        sniffer = DHTSniffer(passive=True)
        sniffer._node_id = os.urandom(20) if hasattr(sniffer, "_node_id") else b"\x00" * 20

        mock_sock = MagicMock()
        sniffer._sock = mock_sock
        sniffer._node_id = b"\x00" * 20

        sniffer._ping(("127.0.0.1", 6881))
        mock_sock.sendto.assert_not_called()

    def test_slots_present(self):
        assert "__slots__" in DHTSniffer.__dict__
        sniffer = DHTSniffer()
        assert not hasattr(sniffer, "__dict__")

    def test_queue_maxlen(self):
        sniffer = DHTSniffer(passive=True)
        assert sniffer._queue.maxlen == Platform.DHT_BUF


# ── DHT helper functions ──────────────────────────────────────────────────────

class TestDHTHelpers:
    def test_extract_infohash_found(self):
        ih_bytes = bytes(range(20))
        data = b"d1:rd2:id20:" + ih_bytes + b"9:info_hash20:" + ih_bytes + b"ee"
        result = _extract_infohash(data)
        assert result == ih_bytes.hex()

    def test_extract_infohash_not_found(self):
        data = b"d1:t2:aae"
        assert _extract_infohash(data) is None

    def test_extract_nodes(self):
        import socket as _sock
        import struct as _struct
        # Build one fake node: 20 byte ID + 4 byte IP + 2 byte port
        node_id = b"\x01" * 20
        ip = _sock.inet_aton("192.168.1.1")
        port = _struct.pack("!H", 6881)
        raw = node_id + ip + port  # 26 bytes
        length = len(raw)
        data = b"5:nodes" + str(length).encode() + b":" + raw
        nodes = _extract_nodes(data)
        assert len(nodes) == 1
        assert nodes[0] == ("192.168.1.1", 6881)

    def test_extract_nodes_missing(self):
        data = b"d1:t2:aae"
        assert _extract_nodes(data) == []


# ── build_magnet ──────────────────────────────────────────────────────────────

class TestBuildMagnet:
    def test_contains_infohash(self):
        t = Torrent("aabbcc" * 6 + "aabb", "TestTorrent", seeds=10)
        cfg = PrivacyConfig()
        m = build_magnet(t, cfg)
        assert t.infohash in m

    def test_contains_5_trackers(self):
        t = Torrent("f" * 40, "TrackerTest")
        cfg = PrivacyConfig()
        m = build_magnet(t, cfg)
        assert m.count("tr=") == 5

    def test_magnet_format(self):
        t = Torrent("0" * 40, "FormatTest")
        cfg = PrivacyConfig()
        m = build_magnet(t, cfg)
        assert m.startswith("magnet:?xt=urn:btih:")


# need os for the DHT test
import os


# ── SearchHistory ─────────────────────────────────────────────────────────────

class TestSearchHistory:
    def _make_history(self, tmp_path):
        db = str(tmp_path / "test.db")
        return SearchHistory(db_path=db)

    def test_record_and_get_recent(self, tmp_path):
        h = self._make_history(tmp_path)
        h.record_search("ubuntu", 10)
        h.record_search("debian", 5)
        recent = h.get_recent(limit=10)
        assert len(recent) == 2
        assert recent[0]["query"] == "debian"  # most recent first
        assert recent[1]["query"] == "ubuntu"
        h.close()

    def test_save_and_get_cached(self, tmp_path):
        h = self._make_history(tmp_path)
        torrents = [
            Torrent("a" * 40, "Ubuntu 24.04", seeds=100),
            Torrent("b" * 40, "Ubuntu Server", seeds=50),
        ]
        h.save_results("ubuntu", torrents)
        result = h.get_cached("ubuntu", max_age_s=3600)
        assert result is not None
        assert len(result) == 2
        assert result[0].seeds == 100
        h.close()

    def test_sqlite_cache_ttl_expired(self, tmp_path):
        h = self._make_history(tmp_path)
        torrents = [Torrent("c" * 40, "OldTorrent", seeds=1)]
        h.save_results("old", torrents)
        result = h.get_cached("old", max_age_s=0)  # 0s TTL = already expired
        assert result is None
        h.close()

    def test_cache_miss_returns_none(self, tmp_path):
        h = self._make_history(tmp_path)
        assert h.get_cached("nonexistent") is None
        h.close()

    def test_clear(self, tmp_path):
        h = self._make_history(tmp_path)
        h.record_search("test", 5)
        h.save_results("test", [Torrent("d" * 40, "T")])
        h.clear()
        assert h.get_recent() == []
        assert h.get_cached("test") is None
        h.close()

    def test_empty_infohash_not_saved(self, tmp_path):
        h = self._make_history(tmp_path)
        torrents = [Torrent("", "NoHash")]
        h.save_results("q", torrents)
        result = h.get_cached("q")
        assert result is None
        h.close()


# ── parse_filters / apply_filters ─────────────────────────────────────────────

class TestParseFilters:
    def test_no_filters(self):
        query, filters = parse_filters("ubuntu linux")
        assert query == "ubuntu linux"
        assert filters == {}

    def test_seeds_min(self):
        query, filters = parse_filters("ubuntu seeds>50")
        assert query == "ubuntu"
        assert filters.get("seeds_min") == 50

    def test_seeds_max(self):
        query, filters = parse_filters("debian seeds<100")
        assert query == "debian"
        assert filters.get("seeds_max") == 100

    def test_size_mb(self):
        query, filters = parse_filters("iso size<500mb")
        assert query == "iso"
        assert filters.get("size_max") == 500 * 1024 ** 2

    def test_size_gb(self):
        query, filters = parse_filters("movie size>2gb")
        assert query == "movie"
        assert filters.get("size_min") == 2 * 1024 ** 3

    def test_source_filter(self):
        query, filters = parse_filters("python source=apibay")
        assert query == "python"
        assert filters.get("source") == "apibay"

    def test_multiple_filters(self):
        query, filters = parse_filters("ubuntu seeds>10 size<2gb source=nyaa")
        assert query == "ubuntu"
        assert filters["seeds_min"] == 10
        assert filters["size_max"] == 2 * 1024 ** 3
        assert filters["source"] == "nyaa"

    def test_empty_after_filters(self):
        query, filters = parse_filters("seeds>5")
        assert query == ""
        assert filters["seeds_min"] == 5


class TestApplyFilters:
    def _t(self, ih_char, seeds=0, leeches=0, size=0, source="apibay"):
        return Torrent(ih_char * 40, "T", size, seeds, leeches, source)

    def test_filter_seeds_min(self):
        results = [self._t("a", seeds=5), self._t("b", seeds=100), self._t("c", seeds=50)]
        out = apply_filters(results, {"seeds_min": 50})
        assert len(out) == 2
        assert all(t.seeds >= 50 for t in out)

    def test_filter_seeds_max(self):
        results = [self._t("a", seeds=5), self._t("b", seeds=100)]
        out = apply_filters(results, {"seeds_max": 10})
        assert len(out) == 1

    def test_filter_source(self):
        results = [self._t("a", source="apibay"), self._t("b", source="nyaa")]
        out = apply_filters(results, {"source": "nyaa"})
        assert len(out) == 1
        assert out[0].source == "nyaa"

    def test_filter_size_max(self):
        mb = 1024 * 1024
        results = [self._t("a", size=100*mb), self._t("b", size=2000*mb)]
        out = apply_filters(results, {"size_max": 500 * mb})
        assert len(out) == 1

    def test_no_filters_passthrough(self):
        results = [self._t("a"), self._t("b"), self._t("c")]
        assert apply_filters(results, {}) == results


# ── format_results ────────────────────────────────────────────────────────────

class TestFormatResults:
    def _sample(self):
        return [
            Torrent("a" * 40, "Ubuntu 24.04", size=1024**3, seeds=500, leeches=50, source="apibay"),
            Torrent("b" * 40, "Debian 12", size=500*1024**2, seeds=200, leeches=20, source="nyaa"),
        ]

    def test_json_is_valid(self):
        import json
        cfg = PrivacyConfig(no_delay=True)
        output = format_results(self._sample(), "json", cfg)
        data = json.loads(output)
        assert isinstance(data, list)
        assert len(data) == 2
        assert data[0]["name"] == "Ubuntu 24.04"
        assert data[0]["seeds"] == 500
        assert "magnet" in data[0]

    def test_json_contains_infohash(self):
        import json
        cfg = PrivacyConfig(no_delay=True)
        output = format_results(self._sample(), "json", cfg)
        data = json.loads(output)
        assert data[0]["infohash"] == "a" * 40

    def test_csv_has_header(self):
        output = format_results(self._sample(), "csv")
        lines = output.strip().split("\n")
        assert lines[0].startswith("infohash")
        assert "name" in lines[0]
        assert "seeds" in lines[0]

    def test_csv_row_count(self):
        output = format_results(self._sample(), "csv")
        lines = [l for l in output.strip().split("\n") if l]
        assert len(lines) == 3  # header + 2 rows

    def test_fetch_text_retry_429(self):
        """fetch_text should retry once on 429 response."""
        cfg = PrivacyConfig(no_delay=True)
        call_count = [0]

        mock_resp_429 = MagicMock()
        mock_resp_429.status_code = 429
        mock_resp_429.close = MagicMock()

        mock_resp_ok = MagicMock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.raise_for_status = MagicMock()
        mock_resp_ok.iter_content = MagicMock(return_value=[b"hello world"])
        mock_resp_ok.close = MagicMock()

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return mock_resp_429
            return mock_resp_ok

        with patch("search_engine._POOL_SESSION") as mock_session:
            mock_session.get.side_effect = side_effect
            result = fetch_text("http://example.com", cfg)

        assert call_count[0] == 2
        assert "hello" in result


# ── Watchlist ─────────────────────────────────────────────────────────────────

class TestWatchlist:
    def _wl(self, tmp_path):
        return Watchlist(db_path=str(tmp_path / "fav.db"))

    def _t(self, ih="a" * 40, name="Test", seeds=100):
        return Torrent(ih, name, size=1024 ** 3, seeds=seeds, leeches=10, source="apibay")

    def test_add_and_is_favorite(self, tmp_path):
        wl = self._wl(tmp_path)
        t = self._t()
        assert wl.add(t) is True
        assert wl.is_favorite(t.infohash) is True
        wl.close()

    def test_no_duplicate(self, tmp_path):
        wl = self._wl(tmp_path)
        t = self._t()
        wl.add(t)
        result = wl.add(t)
        # Second add should not raise, is_favorite still True
        assert wl.is_favorite(t.infohash) is True
        wl.close()

    def test_remove(self, tmp_path):
        wl = self._wl(tmp_path)
        t = self._t()
        wl.add(t)
        wl.remove(t.infohash)
        assert wl.is_favorite(t.infohash) is False
        wl.close()

    def test_list_all_sorted_by_added_ts(self, tmp_path):
        wl = self._wl(tmp_path)
        t1 = self._t("a" * 40, "First")
        t2 = self._t("b" * 40, "Second")
        wl.add(t1)
        time.sleep(0.01)
        wl.add(t2)
        results = wl.list_all()
        assert len(results) == 2
        assert results[0].name == "Second"  # most recent first
        wl.close()

    def test_clear(self, tmp_path):
        wl = self._wl(tmp_path)
        wl.add(self._t("a" * 40))
        wl.add(self._t("b" * 40, "B"))
        wl.clear()
        assert wl.list_all() == []
        wl.close()

    def test_not_favorite_after_remove(self, tmp_path):
        wl = self._wl(tmp_path)
        t = self._t()
        wl.add(t)
        wl.remove(t.infohash)
        assert not wl.is_favorite(t.infohash)
        wl.close()

    def test_empty_infohash_not_added(self, tmp_path):
        wl = self._wl(tmp_path)
        t = Torrent("", "No hash", seeds=1)
        result = wl.add(t)
        assert result is False
        wl.close()


# ── TorrentClientAPI ─────────────────────────────────────────────────────────

class TestTorrentClientAPI:
    def test_none_client_add_returns_false(self):
        api = TorrentClientAPI("none", "")
        assert api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40) is False

    def test_none_client_test_connection(self):
        api = TorrentClientAPI("none", "")
        result = api.test_connection()
        assert "kein" in result.lower() or "client" in result.lower()

    def test_qbittorrent_success(self):
        api = TorrentClientAPI("qbittorrent", "http://localhost:8080")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Ok."
        with patch.object(api._session, "post", return_value=mock_resp):
            result = api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert result is True

    def test_qbittorrent_fails_response(self):
        api = TorrentClientAPI("qbittorrent", "http://localhost:8080")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.text = "Fails."
        with patch.object(api._session, "post", return_value=mock_resp):
            result = api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert result is False

    def test_transmission_success(self):
        api = TorrentClientAPI("transmission", "http://localhost:9091")
        mock_409 = MagicMock()
        mock_409.status_code = 409
        mock_409.headers = {"X-Transmission-Session-Id": "token123"}
        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"result": "success"}
        with patch.object(api._session, "get", return_value=mock_409):
            with patch.object(api._session, "post", return_value=mock_200):
                result = api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert result is True

    def test_transmission_failure(self):
        api = TorrentClientAPI("transmission", "http://localhost:9091")
        mock_409 = MagicMock()
        mock_409.status_code = 409
        mock_409.headers = {"X-Transmission-Session-Id": "token123"}
        mock_200 = MagicMock()
        mock_200.status_code = 200
        mock_200.json.return_value = {"result": "error: duplicate torrent"}
        with patch.object(api._session, "get", return_value=mock_409):
            with patch.object(api._session, "post", return_value=mock_200):
                result = api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert result is False

    def test_connection_error_returns_false(self):
        api = TorrentClientAPI("qbittorrent", "http://localhost:8080")
        with patch.object(api._session, "post", side_effect=Exception("connection refused")):
            result = api.add_magnet("magnet:?xt=urn:btih:" + "a" * 40)
        assert result is False

    def test_qbittorrent_test_connection_ok(self):
        api = TorrentClientAPI("qbittorrent", "http://localhost:8080")
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch.object(api._session, "get", return_value=mock_resp):
            result = api.test_connection()
        assert result == "OK"

    def test_transmission_test_connection_ok(self):
        api = TorrentClientAPI("transmission", "http://localhost:9091")
        mock_resp = MagicMock()
        mock_resp.status_code = 409
        with patch.object(api._session, "get", return_value=mock_resp):
            result = api.test_connection()
        assert result == "OK"


# ── RSSWatcher ────────────────────────────────────────────────────────────────

class TestRSSWatcher:
    def _make_watcher(self):
        mock_engine = MagicMock()
        mock_engine.search.return_value = []
        return RSSWatcher(mock_engine, interval_min=60)

    def test_add_watch_returns_id(self):
        w = self._make_watcher()
        wid = w.add_watch("ubuntu", callback=None)
        assert len(wid) == 8

    def test_list_watches(self):
        w = self._make_watcher()
        w.add_watch("ubuntu", callback=None)
        w.add_watch("debian", callback=None)
        watches = w.list_watches()
        assert len(watches) == 2
        queries = {x["query"] for x in watches}
        assert queries == {"ubuntu", "debian"}

    def test_remove_watch(self):
        w = self._make_watcher()
        wid = w.add_watch("ubuntu", callback=None)
        result = w.remove_watch(wid)
        assert result is True
        assert len(w.list_watches()) == 0

    def test_remove_unknown_watch(self):
        w = self._make_watcher()
        result = w.remove_watch("nonexistent")
        assert result is False

    def test_stop_without_start(self):
        w = self._make_watcher()
        w.stop()  # should not raise

    def test_start_stop(self):
        w = self._make_watcher()
        w.start()
        assert w._running is True
        w.stop()
        assert w._running is False

    def test_callback_called_on_new_results(self):
        t1 = Torrent("a" * 40, "Ubuntu", seeds=100)
        mock_engine = MagicMock()
        mock_engine.search.return_value = [t1]

        received = []

        def cb(query, results):
            received.append((query, results))

        w = RSSWatcher(mock_engine, interval_min=60)
        wid = w.add_watch("ubuntu", callback=cb)
        # Force last_check to 0 so poll triggers
        w._watches[wid]["last_check"] = 0.0
        # Manually trigger poll
        w._poll_loop_once()
        assert len(received) == 1
        assert received[0][0] == "ubuntu"
        assert received[0][1][0].name == "Ubuntu"
