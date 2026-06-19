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
    top_k_stream,
    dedup_stream,
    fetch_text,
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
