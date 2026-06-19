"""Tests for gui.py – non-display-dependent parts run with offscreen platform."""

import os
import sys
import time
import pytest
from unittest.mock import MagicMock, patch

# Use offscreen Qt platform so tests work without a real display
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# ── QApplication fixture ──────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def app():
    from PyQt6.QtWidgets import QApplication
    existing = QApplication.instance()
    if existing:
        return existing
    return QApplication(sys.argv)


# ── Imports (after QApplication setup path is set) ───────────────────────────

from search_engine import Torrent, PrivacyConfig, Platform

# Import GUI classes
from gui import (
    SearchWorker,
    FilterPanel,
    ResultsTable,
    HistoryTab,
    SettingsTab,
    _NumericItem,
)


# ── _NumericItem ──────────────────────────────────────────────────────────────

class TestNumericItem:
    def test_sorts_numerically(self, app):
        from PyQt6.QtCore import Qt
        a = _NumericItem("5")
        a.setData(Qt.ItemDataRole.UserRole, 5)
        b = _NumericItem("100")
        b.setData(Qt.ItemDataRole.UserRole, 100)
        assert a < b
        assert not b < a

    def test_fallback_without_user_role(self, app):
        a = _NumericItem("alpha")
        b = _NumericItem("beta")
        # Should not raise
        _ = a < b


# ── FilterPanel ───────────────────────────────────────────────────────────────

class TestFilterPanel:
    def test_default_no_filters(self, app):
        panel = FilterPanel()
        f = panel.get_filters()
        assert f == {}

    def test_seeds_min(self, app):
        panel = FilterPanel()
        panel._seeds_min.setValue(50)
        f = panel.get_filters()
        assert f.get("seeds_min") == 50

    def test_seeds_max(self, app):
        panel = FilterPanel()
        panel._seeds_max.setValue(200)
        f = panel.get_filters()
        assert f.get("seeds_max") == 200

    def test_size_max_mb(self, app):
        panel = FilterPanel()
        panel._size_max_val.setValue(500)
        panel._size_max_unit.setCurrentText("MB")
        f = panel.get_filters()
        assert f.get("size_max") == int(500 * 1024 ** 2)

    def test_size_min_gb(self, app):
        panel = FilterPanel()
        panel._size_min_val.setValue(2)
        panel._size_min_unit.setCurrentText("GB")
        f = panel.get_filters()
        assert f.get("size_min") == int(2 * 1024 ** 3)

    def test_source_filter(self, app):
        panel = FilterPanel()
        panel._source.setCurrentText("apibay")
        f = panel.get_filters()
        assert f.get("source") == "apibay"

    def test_source_alle_not_in_filters(self, app):
        panel = FilterPanel()
        panel._source.setCurrentIndex(0)  # "Alle"
        f = panel.get_filters()
        assert "source" not in f

    def test_reset_clears_all(self, app):
        panel = FilterPanel()
        panel._seeds_min.setValue(100)
        panel._size_max_val.setValue(500)
        panel._source.setCurrentText("nyaa")
        panel.reset()
        assert panel.get_filters() == {}

    def test_multiple_filters(self, app):
        panel = FilterPanel()
        panel._seeds_min.setValue(10)
        panel._size_max_val.setValue(1)
        panel._size_max_unit.setCurrentText("GB")
        panel._source.setCurrentText("1337x")
        f = panel.get_filters()
        assert f["seeds_min"] == 10
        assert f["size_max"] == 1024 ** 3
        assert f["source"] == "1337x"


# ── ResultsTable ──────────────────────────────────────────────────────────────

class TestResultsTable:
    def _make_table(self, app):
        cfg = PrivacyConfig(no_delay=True)
        return ResultsTable(
            status_fn=lambda msg, ms=0: None,
            cfg_getter=lambda: cfg,
        )

    def _sample_torrents(self):
        return [
            Torrent("a" * 40, "Ubuntu 24.04", size=2 * 1024 ** 3, seeds=500, leeches=50, source="apibay"),
            Torrent("b" * 40, "Debian 12", size=700 * 1024 ** 2, seeds=200, leeches=20, source="nyaa"),
            Torrent("c" * 40, "Arch Linux", size=0, seeds=10, leeches=5, source="1337x"),
        ]

    def test_populate_sets_row_count(self, app):
        table = self._make_table(app)
        table.populate(self._sample_torrents())
        assert table.rowCount() == 3

    def test_populate_stores_results(self, app):
        table = self._make_table(app)
        torrents = self._sample_torrents()
        table.populate(torrents)
        assert len(table._results) == 3

    def test_populate_empty(self, app):
        table = self._make_table(app)
        table.populate([])
        assert table.rowCount() == 0

    def test_repopulate_clears_previous(self, app):
        table = self._make_table(app)
        table.populate(self._sample_torrents())
        table.populate([Torrent("d" * 40, "Single")])
        assert table.rowCount() == 1
        assert len(table._results) == 1

    def test_size_displayed_as_gb(self, app):
        from PyQt6.QtCore import Qt
        table = self._make_table(app)
        table.populate([Torrent("a" * 40, "Big", size=2 * 1024 ** 3)])
        # Find size column item
        found_gb = False
        for r in range(table.rowCount()):
            item = table.item(r, 4)
            if item and "GB" in item.text():
                found_gb = True
        assert found_gb

    def test_size_displayed_as_mb(self, app):
        table = self._make_table(app)
        table.populate([Torrent("b" * 40, "Medium", size=700 * 1024 ** 2)])
        found_mb = False
        for r in range(table.rowCount()):
            item = table.item(r, 4)
            if item and "MB" in item.text():
                found_mb = True
        assert found_mb

    def test_zero_size_shows_dash(self, app):
        table = self._make_table(app)
        table.populate([Torrent("c" * 40, "NoSize", size=0)])
        found_dash = False
        for r in range(table.rowCount()):
            item = table.item(r, 4)
            if item and item.text() == "–":
                found_dash = True
        assert found_dash

    def test_copy_magnet_calls_clipboard(self, app):
        from PyQt6.QtWidgets import QApplication as _QApp
        table = self._make_table(app)
        table.populate(self._sample_torrents())
        # find row with orig_index=0
        for r in range(table.rowCount()):
            if table._orig_index(r) == 0:
                table._copy_magnet(r)
                text = _QApp.clipboard().text()
                assert "magnet:?xt=urn:btih:" in text
                break


# ── SearchWorker ──────────────────────────────────────────────────────────────

def _run_worker(worker, app):
    """Start worker, wait for it, then flush the Qt event queue."""
    from PyQt6.QtWidgets import QApplication
    worker.start()
    worker.wait(5000)
    QApplication.processEvents()


class TestSearchWorker:
    def test_emits_results_ready(self, app):
        t1 = Torrent("a" * 40, "T1", seeds=100)
        t2 = Torrent("b" * 40, "T2", seeds=50)
        mock_engine = MagicMock()
        mock_engine.search.return_value = [t1, t2]

        received = []
        worker = SearchWorker(mock_engine, "ubuntu", {})
        worker.results_ready.connect(lambda r: received.extend(r))
        _run_worker(worker, app)

        assert len(received) == 2
        mock_engine.search.assert_called_once_with("ubuntu")

    def test_applies_filters(self, app):
        torrents = [
            Torrent("a" * 40, "T1", seeds=5),
            Torrent("b" * 40, "T2", seeds=200),
        ]
        mock_engine = MagicMock()
        mock_engine.search.return_value = torrents

        received = []
        worker = SearchWorker(mock_engine, "test", {"seeds_min": 100})
        worker.results_ready.connect(lambda r: received.extend(r))
        _run_worker(worker, app)

        assert len(received) == 1
        assert received[0].seeds == 200

    def test_emits_error_on_exception(self, app):
        mock_engine = MagicMock()
        mock_engine.search.side_effect = RuntimeError("network down")

        errors = []
        worker = SearchWorker(mock_engine, "fail", {})
        worker.error.connect(errors.append)
        _run_worker(worker, app)

        assert len(errors) == 1
        assert "network down" in errors[0]


# ── HistoryTab ────────────────────────────────────────────────────────────────

class TestHistoryTab:
    def test_refresh_loads_entries(self, app, tmp_path):
        from search_engine import SearchHistory
        import search_engine as _se
        old_history = _se.HISTORY
        _se.HISTORY = SearchHistory(db_path=str(tmp_path / "h.db"))
        _se.HISTORY.record_search("ubuntu", 10)
        _se.HISTORY.record_search("debian", 5)

        tab = HistoryTab(search_fn=lambda q: None)
        tab.refresh()
        assert tab._table.rowCount() == 2

        _se.HISTORY.close()
        _se.HISTORY = old_history

    def test_clear_empties_table(self, app, tmp_path):
        from search_engine import SearchHistory
        import search_engine as _se
        old_history = _se.HISTORY
        _se.HISTORY = SearchHistory(db_path=str(tmp_path / "h2.db"))
        _se.HISTORY.record_search("test", 3)

        tab = HistoryTab(search_fn=lambda q: None)
        tab.refresh()
        assert tab._table.rowCount() >= 1
        tab._clear()
        assert tab._table.rowCount() == 0

        _se.HISTORY.close()
        _se.HISTORY = old_history


# ── SettingsTab ───────────────────────────────────────────────────────────────

class TestSettingsTab:
    def test_get_config_default(self, app):
        received = []
        tab = SettingsTab(on_save=lambda cfg, k, d: received.append((cfg, k, d)))
        cfg, top_k, dht_sec = tab.get_config()
        assert isinstance(cfg, PrivacyConfig)
        assert top_k == Platform.TOP_K_DEFAULT
        assert dht_sec == 0

    def test_save_calls_callback(self, app):
        received = []
        tab = SettingsTab(on_save=lambda cfg, k, d: received.append((cfg, k, d)))
        tab._top_k.setValue(50)
        tab._dht_sec.setValue(10)
        tab._save()
        assert len(received) == 1
        _, top_k, dht_sec = received[0]
        assert top_k == 50
        assert dht_sec == 10

    def test_proxy_set(self, app):
        received = []
        tab = SettingsTab(on_save=lambda cfg, k, d: received.append(cfg))
        tab._proxy.setText("http://proxy.example.com:8080")
        tab._save()
        assert received[0].proxy == "http://proxy.example.com:8080"

    def test_socks5_converted_to_socks5h(self, app):
        received = []
        tab = SettingsTab(on_save=lambda cfg, k, d: received.append(cfg))
        tab._proxy.setText("socks5://127.0.0.1:9050")
        tab._save()
        assert received[0].proxy.startswith("socks5h://")

    def test_no_delay_default(self, app):
        received = []
        tab = SettingsTab(on_save=lambda cfg, k, d: received.append(cfg))
        tab._save()
        # Delays checkbox unchecked by default → no_delay=True
        assert received[0].no_delay is True
