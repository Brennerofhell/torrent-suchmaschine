#!/usr/bin/env python3
"""PyQt6 GUI – Torrent Search Engine."""

import sys
import time
import argparse

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QTabWidget,
    QVBoxLayout, QHBoxLayout, QFormLayout,
    QLineEdit, QPushButton, QTableWidget, QTableWidgetItem,
    QHeaderView, QProgressBar, QGroupBox, QLabel,
    QSpinBox, QDoubleSpinBox, QComboBox, QCheckBox,
    QSplitter, QMenu, QAbstractItemView,
)
from PyQt6.QtCore import Qt, QThread, pyqtSignal

import search_engine as _se
from search_engine import (
    TorrentSearchEngine, PrivacyConfig, Platform, Torrent,
    apply_filters, parse_filters, build_magnet, enable_doh,
)

# ═══════════════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════════════

class _NumericItem(QTableWidgetItem):
    """QTableWidgetItem that sorts by a numeric UserRole value."""

    def __lt__(self, other):
        try:
            a = self.data(Qt.ItemDataRole.UserRole) or 0
            b = other.data(Qt.ItemDataRole.UserRole) or 0
            return a < b
        except Exception:
            return super().__lt__(other)


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH WORKER
# ═══════════════════════════════════════════════════════════════════════════════

class SearchWorker(QThread):
    """Runs TorrentSearchEngine.search() in a background thread."""

    results_ready = pyqtSignal(list)
    error = pyqtSignal(str)

    def __init__(self, engine: TorrentSearchEngine, query: str, filters: dict) -> None:
        super().__init__()
        self._engine = engine
        self._query = query
        self._filters = filters

    def run(self) -> None:
        try:
            results = self._engine.search(self._query)
            if self._filters:
                results = apply_filters(results, self._filters)
            self.results_ready.emit(results)
        except Exception as exc:
            self.error.emit(str(exc))


# ═══════════════════════════════════════════════════════════════════════════════
# FILTER PANEL
# ═══════════════════════════════════════════════════════════════════════════════

class FilterPanel(QGroupBox):
    """Left-side filter panel with seeds, size, and source filters."""

    _UNITS = {"MB": 1024 ** 2, "GB": 1024 ** 3}
    _SOURCES = ["Alle", "apibay", "nyaa", "eztv", "tgx", "1337x",
                "torrentproject", "btdigg", "dht", "limetorrents"]

    def __init__(self, parent=None) -> None:
        super().__init__("Filter", parent)
        layout = QFormLayout(self)
        layout.setVerticalSpacing(6)

        self._seeds_min = QSpinBox()
        self._seeds_min.setRange(0, 999_999)
        layout.addRow("Seeds min:", self._seeds_min)

        self._seeds_max = QSpinBox()
        self._seeds_max.setRange(0, 999_999)
        self._seeds_max.setValue(999_999)
        layout.addRow("Seeds max:", self._seeds_max)

        self._size_min_val = QDoubleSpinBox()
        self._size_min_val.setRange(0, 99_999)
        self._size_min_val.setDecimals(1)
        self._size_min_unit = QComboBox()
        self._size_min_unit.addItems(["MB", "GB"])
        size_min_row = self._make_row(self._size_min_val, self._size_min_unit)
        layout.addRow("Größe min:", size_min_row)

        self._size_max_val = QDoubleSpinBox()
        self._size_max_val.setRange(0, 99_999)
        self._size_max_val.setDecimals(1)
        self._size_max_unit = QComboBox()
        self._size_max_unit.addItems(["MB", "GB"])
        size_max_row = self._make_row(self._size_max_val, self._size_max_unit)
        layout.addRow("Größe max:", size_max_row)

        self._source = QComboBox()
        self._source.addItems(self._SOURCES)
        layout.addRow("Quelle:", self._source)

        btn_row = QWidget()
        btn_lay = QHBoxLayout(btn_row)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        self.btn_apply = QPushButton("Anwenden")
        self._btn_reset = QPushButton("Zurücksetzen")
        btn_lay.addWidget(self.btn_apply)
        btn_lay.addWidget(self._btn_reset)
        layout.addRow(btn_row)

        self._btn_reset.clicked.connect(self.reset)
        self.setMinimumWidth(200)
        self.setMaximumWidth(240)

    @staticmethod
    def _make_row(spin, combo) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(spin)
        lay.addWidget(combo)
        return w

    def reset(self) -> None:
        self._seeds_min.setValue(0)
        self._seeds_max.setValue(999_999)
        self._size_min_val.setValue(0)
        self._size_max_val.setValue(0)
        self._source.setCurrentIndex(0)

    def get_filters(self) -> dict:
        """Return filter dict compatible with apply_filters()."""
        filters: dict = {}

        seeds_min = self._seeds_min.value()
        seeds_max = self._seeds_max.value()
        if seeds_min > 0:
            filters["seeds_min"] = seeds_min
        if seeds_max < 999_999:
            filters["seeds_max"] = seeds_max

        size_min = self._size_min_val.value()
        if size_min > 0:
            filters["size_min"] = int(size_min * self._UNITS[self._size_min_unit.currentText()])

        size_max = self._size_max_val.value()
        if size_max > 0:
            filters["size_max"] = int(size_max * self._UNITS[self._size_max_unit.currentText()])

        source = self._source.currentText()
        if source != "Alle":
            filters["source"] = source.lower()

        return filters


# ═══════════════════════════════════════════════════════════════════════════════
# RESULTS TABLE
# ═══════════════════════════════════════════════════════════════════════════════

_COLUMNS = ["#", "Name", "Seeds", "Leeches", "Größe", "Quelle"]


class ResultsTable(QTableWidget):
    """Sortable table showing search results. Double-click copies magnet link."""

    def __init__(self, status_fn, cfg_getter, parent=None) -> None:
        super().__init__(0, len(_COLUMNS), parent)
        self._status_fn = status_fn
        self._cfg_getter = cfg_getter
        self._results: list = []

        self.setHorizontalHeaderLabels(_COLUMNS)
        hdr = self.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSortIndicatorShown(True)
        self.setColumnWidth(0, 40)
        self.setColumnWidth(2, 70)
        self.setColumnWidth(3, 70)
        self.setColumnWidth(4, 90)
        self.setColumnWidth(5, 110)

        self.setSortingEnabled(True)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setAlternatingRowColors(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.verticalHeader().setVisible(False)

        self.cellDoubleClicked.connect(self._on_double_click)
        self.customContextMenuRequested.connect(self._on_context_menu)

    def populate(self, results: list) -> None:
        self.setSortingEnabled(False)
        self.setRowCount(0)
        self._results = list(results)

        for orig_idx, t in enumerate(results):
            row = self.rowCount()
            self.insertRow(row)

            num = _NumericItem(str(orig_idx + 1))
            num.setData(Qt.ItemDataRole.UserRole, orig_idx)
            num.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            name = QTableWidgetItem(t.name)

            seeds = _NumericItem(f"{t.seeds:,}")
            seeds.setData(Qt.ItemDataRole.UserRole, t.seeds)
            seeds.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            leeches = _NumericItem(f"{t.leeches:,}")
            leeches.setData(Qt.ItemDataRole.UserRole, t.leeches)
            leeches.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            if t.size >= 1024 ** 3:
                size_str = f"{t.size / 1024**3:.1f} GB"
            elif t.size > 0:
                size_str = f"{t.size / 1024**2:.1f} MB"
            else:
                size_str = "–"
            size = _NumericItem(size_str)
            size.setData(Qt.ItemDataRole.UserRole, t.size)
            size.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

            source = QTableWidgetItem(t.source)

            self.setItem(row, 0, num)
            self.setItem(row, 1, name)
            self.setItem(row, 2, seeds)
            self.setItem(row, 3, leeches)
            self.setItem(row, 4, size)
            self.setItem(row, 5, source)

        self.setSortingEnabled(True)
        self.sortByColumn(2, Qt.SortOrder.DescendingOrder)

    def _orig_index(self, row: int) -> int:
        item = self.item(row, 0)
        if item is None:
            return -1
        idx = item.data(Qt.ItemDataRole.UserRole)
        return idx if idx is not None else -1

    def _copy_magnet(self, row: int) -> None:
        idx = self._orig_index(row)
        if 0 <= idx < len(self._results):
            t = self._results[idx]
            magnet = build_magnet(t, self._cfg_getter())
            QApplication.clipboard().setText(magnet)
            self._status_fn(f"Magnet-Link kopiert: {t.name[:60]}", 4000)

    def _copy_infohash(self, row: int) -> None:
        idx = self._orig_index(row)
        if 0 <= idx < len(self._results):
            ih = self._results[idx].infohash
            QApplication.clipboard().setText(ih)
            self._status_fn(f"Infohash kopiert: {ih}", 3000)

    def _on_double_click(self, row: int, _col: int) -> None:
        self._copy_magnet(row)

    def _on_context_menu(self, pos) -> None:
        row = self.rowAt(pos.y())
        if row < 0:
            return
        menu = QMenu(self)
        act_magnet = menu.addAction("Magnet-Link kopieren")
        act_hash = menu.addAction("Infohash kopieren")
        chosen = menu.exec(self.mapToGlobal(pos))
        if chosen == act_magnet:
            self._copy_magnet(row)
        elif chosen == act_hash:
            self._copy_infohash(row)


# ═══════════════════════════════════════════════════════════════════════════════
# SEARCH TAB
# ═══════════════════════════════════════════════════════════════════════════════

class SearchTab(QWidget):
    """Main search interface: search bar + filter panel + results table."""

    def __init__(self, engine_getter, cfg_getter, status_fn, parent=None) -> None:
        super().__init__(parent)
        self._engine_getter = engine_getter
        self._status_fn = status_fn
        self._worker: SearchWorker = None

        root = QVBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(4)

        # ── Search bar ────────────────────────────────────────────────────────
        bar = QWidget()
        bar_lay = QHBoxLayout(bar)
        bar_lay.setContentsMargins(0, 0, 0, 0)
        self._search_input = QLineEdit()
        self._search_input.setPlaceholderText(
            "Suchbegriff… z.B.  ubuntu seeds>50 size<2GB source=apibay"
        )
        self._search_input.returnPressed.connect(self._start_search)
        self._search_btn = QPushButton("🔎  Suchen")
        self._search_btn.setMinimumWidth(110)
        self._search_btn.clicked.connect(self._start_search)
        bar_lay.addWidget(self._search_input)
        bar_lay.addWidget(self._search_btn)
        root.addWidget(bar)

        # ── Splitter: filter left | results right ─────────────────────────────
        splitter = QSplitter(Qt.Orientation.Horizontal)
        self._filter_panel = FilterPanel()
        self._filter_panel.btn_apply.clicked.connect(self._start_search)
        self._results_table = ResultsTable(
            status_fn=status_fn,
            cfg_getter=cfg_getter,
        )
        splitter.addWidget(self._filter_panel)
        splitter.addWidget(self._results_table)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([210, 700])
        root.addWidget(splitter, stretch=1)

        # ── Progress bar ──────────────────────────────────────────────────────
        self._progress = QProgressBar()
        self._progress.setRange(0, 0)
        self._progress.setMaximumHeight(6)
        self._progress.hide()
        root.addWidget(self._progress)

    def set_query(self, query: str) -> None:
        self._search_input.setText(query)

    def _start_search(self) -> None:
        raw = self._search_input.text().strip()
        if not raw:
            return

        clean_query, inline_filters = parse_filters(raw)
        panel_filters = self._filter_panel.get_filters()
        merged = {**panel_filters, **inline_filters}

        if not clean_query:
            self._status_fn("Bitte einen Suchbegriff eingeben.", 3000)
            return

        if self._worker and self._worker.isRunning():
            self._worker.quit()

        self._search_btn.setEnabled(False)
        self._progress.show()
        self._status_fn(f"Suche nach '{clean_query}'…")

        self._worker = SearchWorker(self._engine_getter(), clean_query, merged)
        self._worker.results_ready.connect(self._on_results)
        self._worker.error.connect(self._on_error)
        self._worker.finished.connect(self._on_finished)
        self._worker.start()

    def _on_results(self, results: list) -> None:
        self._results_table.populate(results)
        count = len(results)
        self._status_fn(
            f"{count} Ergebnisse{'  (Doppelklick → Magnet-Link kopieren)' if count else ''}",
            6000,
        )

    def _on_error(self, msg: str) -> None:
        self._status_fn(f"Fehler: {msg}", 8000)

    def _on_finished(self) -> None:
        self._search_btn.setEnabled(True)
        self._progress.hide()


# ═══════════════════════════════════════════════════════════════════════════════
# HISTORY TAB
# ═══════════════════════════════════════════════════════════════════════════════

class HistoryTab(QWidget):
    """Shows search history from SQLite. Double-click re-runs the search."""

    def __init__(self, search_fn, parent=None) -> None:
        super().__init__(parent)
        self._search_fn = search_fn

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Zeitpunkt", "Suchanfrage", "Treffer"])
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setAlternatingRowColors(True)
        self._table.verticalHeader().setVisible(False)
        self._table.cellDoubleClicked.connect(self._on_double_click)
        layout.addWidget(self._table)

        btn_clear = QPushButton("Verlauf löschen")
        btn_clear.setMaximumWidth(160)
        btn_clear.clicked.connect(self._clear)
        layout.addWidget(btn_clear)

    def refresh(self) -> None:
        entries = _se.HISTORY.get_recent(limit=50)
        self._table.setRowCount(0)
        for entry in entries:
            ts_str = time.strftime("%d.%m.%Y %H:%M", time.localtime(entry["ts"]))
            row = self._table.rowCount()
            self._table.insertRow(row)
            self._table.setItem(row, 0, QTableWidgetItem(ts_str))
            self._table.setItem(row, 1, QTableWidgetItem(entry["query"]))
            count_item = QTableWidgetItem(str(entry.get("count") or 0))
            count_item.setTextAlignment(
                Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            )
            self._table.setItem(row, 2, count_item)

    def _on_double_click(self, row: int, _col: int) -> None:
        item = self._table.item(row, 1)
        if item:
            self._search_fn(item.text())

    def _clear(self) -> None:
        _se.HISTORY.clear()
        _se.CACHE.clear()
        self._table.setRowCount(0)


# ═══════════════════════════════════════════════════════════════════════════════
# SETTINGS TAB
# ═══════════════════════════════════════════════════════════════════════════════

class SettingsTab(QWidget):
    """Configuration panel: proxy, DoH, delays, DHT, top-k."""

    def __init__(self, on_save, parent=None) -> None:
        super().__init__(parent)
        self._on_save = on_save

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)

        form_box = QGroupBox("Verbindung & Datenschutz")
        form_lay = QFormLayout(form_box)
        form_lay.setVerticalSpacing(8)

        self._proxy = QLineEdit()
        self._proxy.setPlaceholderText("socks5://127.0.0.1:9050  (leer = kein Proxy)")
        form_lay.addRow("Proxy:", self._proxy)

        self._doh = QCheckBox("DNS-over-HTTPS aktivieren (Cloudflare)")
        form_lay.addRow(self._doh)

        self._delays = QCheckBox("Zufällige Delays zwischen Requests")
        self._delays.setChecked(False)
        form_lay.addRow(self._delays)

        self._dht_sec = QSpinBox()
        self._dht_sec.setRange(0, 60)
        self._dht_sec.setSuffix(" s  (0 = deaktiviert)")
        form_lay.addRow("DHT-Laufzeit:", self._dht_sec)

        self._top_k = QSpinBox()
        self._top_k.setRange(10, 500)
        self._top_k.setValue(Platform.TOP_K_DEFAULT)
        form_lay.addRow("Max. Ergebnisse (Top-K):", self._top_k)

        layout.addWidget(form_box)

        save_btn = QPushButton("Einstellungen speichern")
        save_btn.setMaximumWidth(200)
        save_btn.clicked.connect(self._save)
        layout.addWidget(save_btn)

        layout.addStretch()

        platform_lbl = QLabel(Platform.summary())
        platform_lbl.setStyleSheet("color: gray; font-size: 11px;")
        layout.addWidget(platform_lbl)

        warn = Platform.warn_if_needed()
        if warn:
            warn_lbl = QLabel(warn)
            warn_lbl.setStyleSheet("color: darkorange; font-weight: bold; font-size: 11px;")
            layout.addWidget(warn_lbl)

    def _save(self) -> None:
        proxy = self._proxy.text().strip()
        cfg = PrivacyConfig(
            proxy=proxy,
            use_doh=self._doh.isChecked(),
            no_delay=not self._delays.isChecked(),
        )
        if cfg.use_doh:
            enable_doh()
        self._on_save(cfg, self._top_k.value(), self._dht_sec.value())

    def get_config(self) -> tuple:
        """Return (PrivacyConfig, top_k, dht_sec) without saving."""
        proxy = self._proxy.text().strip()
        cfg = PrivacyConfig(
            proxy=proxy,
            use_doh=self._doh.isChecked(),
            no_delay=not self._delays.isChecked(),
        )
        return cfg, self._top_k.value(), self._dht_sec.value()


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN WINDOW
# ═══════════════════════════════════════════════════════════════════════════════

class MainWindow(QMainWindow):
    def __init__(self, no_delay: bool = False) -> None:
        super().__init__()
        self._cfg = PrivacyConfig(no_delay=no_delay)
        self._engine = TorrentSearchEngine(cfg=self._cfg, top_k=Platform.TOP_K_DEFAULT)

        self.setWindowTitle("Torrent Search Engine")
        self.setMinimumSize(900, 600)
        self.resize(1100, 700)

        self._tabs = QTabWidget()
        self.setCentralWidget(self._tabs)

        self._search_tab = SearchTab(
            engine_getter=lambda: self._engine,
            cfg_getter=lambda: self._cfg,
            status_fn=self.statusBar().showMessage,
        )
        self._history_tab = HistoryTab(search_fn=self._search_from_history)
        self._settings_tab = SettingsTab(on_save=self._apply_settings)

        self._tabs.addTab(self._search_tab, "🔎  Suche")
        self._tabs.addTab(self._history_tab, "📋  Verlauf")
        self._tabs.addTab(self._settings_tab, "⚙  Einstellungen")
        self._tabs.currentChanged.connect(self._on_tab_changed)

        self.statusBar().showMessage("Bereit  –  " + Platform.summary())

    def _on_tab_changed(self, idx: int) -> None:
        if self._tabs.widget(idx) is self._history_tab:
            self._history_tab.refresh()

    def _search_from_history(self, query: str) -> None:
        self._tabs.setCurrentWidget(self._search_tab)
        self._search_tab.set_query(query)
        self._search_tab._start_search()

    def _apply_settings(self, cfg: PrivacyConfig, top_k: int, dht_sec: int) -> None:
        self._cfg = cfg
        self._engine.close()
        self._engine = TorrentSearchEngine(cfg=cfg, top_k=top_k, dht_seconds=dht_sec)
        self.statusBar().showMessage("Einstellungen gespeichert.", 3000)

    def closeEvent(self, event) -> None:
        self._engine.close()
        _se.HISTORY.close()
        event.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════════

def main() -> None:
    parser = argparse.ArgumentParser(description="Torrent Search Engine – PyQt6 GUI")
    parser.add_argument(
        "--no-delay",
        action="store_true",
        dest="no_delay",
        help="Delays zwischen Requests deaktivieren",
    )
    args = parser.parse_args()

    app = QApplication(sys.argv)
    app.setApplicationName("Torrent Search Engine")
    win = MainWindow(no_delay=args.no_delay)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
