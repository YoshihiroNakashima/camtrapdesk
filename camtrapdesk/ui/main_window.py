"""メインウィンドウ: 左のナビゲーションと右の画面。"""

from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QMainWindow, QStackedWidget,
                               QSystemTrayIcon, QVBoxLayout, QWidget)

from .. import __version__
from .context import AppContext
from .pages.batch_convert_page import BatchConvertPage
from .pages.clear_page import ClearPage
from .pages.id_page import IdPage
from .pages.ingest_page import IngestPage
from .pages.ledger_page import LedgerPage
from .pages.rename_page import RenamePage
from .pages.settings_page import SettingsPage
from .pages.slot_page import SlotPage


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
        self.nav.setFixedWidth(230)
        self.nav.setIconSize(QSize(16, 16))
        self.stack = QStackedWidget()
        lay.addWidget(self.nav)
        lay.addWidget(self.stack, 1)
        self.setCentralWidget(central)

        self._add_header("最初に一度だけ")
        self._add_page("カードにIDを付ける", IdPage(ctx), "新しいSDカードに番号（id.txt）を書き込みます")
        self._add_header("リーダーを挿し替えたら")
        self._add_page("スロットに名前を付ける", SlotPage(ctx), "リーダーの差し口に名前を付けて、ドライブ文字に頼らず見分けます")
        self._add_header("移行のたびに（この順で）")
        self._add_page("1. 取り込み", IngestPage(ctx), "挿したカードを保存先にコピーして検証し、NASへ転送します")
        self._add_page("2. 地点名に変更", RenamePage(ctx), "野帳の回収SD列をもとに、カードIDのフォルダ名を地点名にします")
        self._add_page("3. カードを消す", ClearPage(ctx), "保存先（NAS）と照合できたカードの動画だけを削除します")
        self._add_header("保存済みのデータ（任意）")
        self._add_page("動画を mp4 に変換", BatchConvertPage(ctx), "NAS や中継にすでにあるフォルダの AVI を後から mp4 に変換して容量を減らします")
        self._add_header("記録・設定")
        self._add_page("台帳", LedgerPage(ctx), "取り込みの記録")
        self._add_page("設定", SettingsPage(ctx))

        self.nav.currentRowChanged.connect(self._nav_changed)
        self.nav.setCurrentRow(self.page_index(IngestPage))

        self.status_nas = QLabel("")
        self.statusBar().addPermanentWidget(self.status_nas)
        ctx.nas_status.connect(self.status_nas.setText)

        self.tray: QSystemTrayIcon | None = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(_app_icon(), self)
            self.tray.setToolTip("camtrapdesk")
            self.tray.show()

    def _add_header(self, text: str) -> None:
        if self.nav.count():
            gap = QListWidgetItem("")
            gap.setFlags(Qt.ItemFlag.NoItemFlags)
            gap.setSizeHint(QSize(0, 10))
            self.nav.addItem(gap)
            self.stack.addWidget(QWidget())
        it = QListWidgetItem(text)
        it.setFlags(Qt.ItemFlag.NoItemFlags)
        f = it.font()
        f.setBold(True)
        it.setFont(f)
        self.nav.addItem(it)
        self.stack.addWidget(QWidget())

    def _add_page(self, text: str, page: QWidget, tooltip: str = "") -> None:
        it = QListWidgetItem("   " + text)
        if tooltip:
            it.setToolTip(tooltip)
        self.nav.addItem(it)
        self.stack.addWidget(page)

    def page_index(self, cls: type) -> int:
        """ページのクラスからナビゲーションの行番号を得る。"""
        for i in range(self.stack.count()):
            if isinstance(self.stack.widget(i), cls):
                return i
        return -1

    def page(self, cls: type) -> QWidget:
        return self.stack.widget(self.page_index(cls))

    def show_page(self, cls: type) -> None:
        self.nav.setCurrentRow(self.page_index(cls))

    def _nav_changed(self, row: int) -> None:
        if row >= 0:
            self.stack.setCurrentIndex(row)

    def notify(self, title: str, text: str) -> None:
        self.statusBar().showMessage(f"{title}: {text}", 15000)
        if self.tray:
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 10000)

    def closeEvent(self, e) -> None:  # noqa: N802
        from PySide6.QtWidgets import QMessageBox
        from ..core import ingest as I
        active = [j for j in self.ctx.ingest.jobs.values() if j.state in (I.S_QUEUED, I.S_COPYING, I.S_VERIFYING)]
        if active or self.ctx.nas.current or self.ctx.converter.current or self.ctx.batch.running:
            parts = []
            if active:
                parts.append(f"カードのコピーが {len(active)} 枚進行中")
            if self.ctx.converter.current:
                parts.append(f"mp4 への変換中（{self.ctx.converter.current}）")
            if self.ctx.nas.current:
                parts.append(f"NASへの転送中（{self.ctx.nas.current}）")
            if self.ctx.batch.running:
                st = self.ctx.batch.snapshot()
                parts.append(f"保存済み動画の mp4 への変換中（{st.n_done}/{st.n_total} ファイル）")
            msg = "、".join(parts) + "です。\n終了すると途中のファイルは次回起動時にやり直します。終了しますか？"
            if QMessageBox.question(self, "終了の確認", msg) != QMessageBox.StandardButton.Yes:
                e.ignore()
                return
        self.ctx.stop_background()
        super().closeEvent(e)
