"""メインウィンドウ: 左のナビゲーションと右の画面。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __version__
from .context import AppContext
from .pages.clear_page import ClearPage
from .pages.ingest_page import IngestPage
from .pages.ledger_page import LedgerPage
from .pages.rename_page import RenamePage
from .pages.settings_page import SettingsPage
from .pages.volume_page import VolumePage


def _app_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(46, 125, 90))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(4, 10, 56, 44, 8, 8)
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(22, 20, 20, 20)
    p.setBrush(QColor(46, 125, 90))
    p.drawEllipse(27, 25, 10, 10)
    p.end()
    return QIcon(pm)


class PlaceholderPage(QWidget):
    def __init__(self, title: str, text: str, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        h = QLabel(f"<h2>{title}</h2>")
        t = QLabel(text)
        t.setWordWrap(True)
        lay.addWidget(h)
        lay.addWidget(t)
        lay.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext):
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle(f"camtrapdesk {__version__}")
        self.setWindowIcon(_app_icon())
        self.resize(1200, 760)

        central = QWidget()
        lay = QHBoxLayout(central)
        lay.setContentsMargins(0, 0, 0, 0)
        self.nav = QListWidget()
        self.nav.setFixedWidth(190)
        self.nav.setIconSize(QSize(16, 16))
        self.stack = QStackedWidget()
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._add_header("SDカード")
        self._add_page("取り込み", IngestPage(ctx))
        self._add_page("名前変更（地点名へ）", RenamePage(ctx))
        self._add_page("削除", ClearPage(ctx))
        self._add_page("ボリューム名", VolumePage(ctx))
        self._add_page("台帳", LedgerPage(ctx))
        self._add_header("データ管理")
        self._add_page("PRM_N の読み込み", PlaceholderPage(
            "PRM_N の読み込み", "次の段階で実装します（read_bs: DateTimeIsOk / DeleteFlag の絞り込み、Season の整形）。"))
        self._add_page("種名修正の反映", PlaceholderPage(
            "種名修正の反映", "次の段階で実装します（apply_species_corrections）。"))
        self._add_page("時刻チェック", PlaceholderPage(
            "時刻チェック", "次の段階で実装します（time_correct_ff / time_correct_order）。"))
        self._add_page("調査努力量", PlaceholderPage(
            "調査努力量", "次の段階で実装します（plot_survey_effort）。"))
        self._add_page("動画サンプリング", PlaceholderPage(
            "動画サンプリング", "次の段階で実装します（sample_and_copy_videos / add_videos / collapse_strata）。"))
        self._add_header("")
        self._add_page("設定", SettingsPage(ctx))

        self.nav.currentRowChanged.connect(self._nav_changed)
        self.nav.setCurrentRow(1)

        self.status_nas = QLabel("")
        self.statusBar().addPermanentWidget(self.status_nas)
        ctx.nas_status.connect(self.status_nas.setText)

        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(_app_icon(), self)
            self.tray.setToolTip("camtrapdesk")
            self.tray.show()

    def _add_header(self, text: str) -> None:
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        self.nav.addItem(it)
        self.stack.addWidget(QWidget())

    def _add_page(self, text: str, page: QWidget) -> None:
        it = QListWidgetItem("   " + text)
        self.nav.addItem(it)
        self.stack.addWidget(page)

    def _nav_changed(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def notify(self, title: str, text: str) -> None:
        self.statusBar().showMessage(f"{title}: {text}", 15000)
        if self.tray:
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 10000)

    def closeEvent(self, e) -> None:  # noqa: N802
        self.ctx.stop_background()
        super().closeEvent(e)
