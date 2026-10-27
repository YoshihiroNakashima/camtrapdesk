"""台帳画面: 取り込みの記録と操作ログを見る。manifest からの再構築もここから。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QMessageBox, QPushButton, QSplitter,
                               QVBoxLayout, QWidget)

from ...core import ledger as L
from ..context import AppContext
from ..widgets import COLORS, human_size, make_table, set_row

STATUS_JA = {L.STATUS_COPYING: "コピー中", L.STATUS_STAGED: "中継先で検証済み",
             L.STATUS_SYNCED: "NASで検証済み", L.STATUS_ERROR: "エラー"}


class LedgerPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        root = QVBoxLayout(self)
        ops = QHBoxLayout()
        self.season_combo = QComboBox()
        self.season_combo.setEditable(True)
        self.season_combo.setPlaceholderText("Season で絞り込み")
        self.refresh_btn = QPushButton("更新")
        self.refresh_btn.clicked.connect(self.refresh)
        self.rebuild_btn = QPushButton("保存先の manifest から台帳を補完")
        self.rebuild_btn.clicked.connect(self.rebuild)
        self.path_label = QLabel(f"台帳ファイル: {ctx.ledger.path}")
        ops.addWidget(QLabel("Season"))
        ops.addWidget(self.season_combo)
        ops.addWidget(self.refresh_btn)
        ops.addWidget(self.rebuild_btn)
        ops.addStretch(1)
        ops.addWidget(self.path_label)
        root.addLayout(ops)
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["ID", "カードID", "Season", "フォルダ", "地点", "状態", "ファイル数", "容量",
                                 "取り込み", "NAS検証", "削除", "メッセージ"])
        split.addWidget(self.table)
        self.events = make_table(["時刻", "種類", "カードID", "内容"])
        split.addWidget(self.events)
        split.setSizes([380, 200])
        root.addWidget(split, 1)
        self.season_combo.currentTextChanged.connect(lambda _t: self.refresh())

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refresh()

    def refresh(self) -> None:
        season = self.season_combo.currentText().strip() or None
        rows = self.ctx.ledger.ingests(season_folder=season, limit=2000)
        seasons = sorted({r.season_folder for r in self.ctx.ledger.ingests(limit=5000)}, reverse=True)
        cur = self.season_combo.currentText()
        self.season_combo.blockSignals(True)
        self.season_combo.clear()
        self.season_combo.addItem("")
        self.season_combo.addItems(seasons)
        self.season_combo.setCurrentText(cur)
        self.season_combo.blockSignals(False)
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            color = {L.STATUS_SYNCED: COLORS["ok"], L.STATUS_STAGED: COLORS["info"],
                     L.STATUS_ERROR: COLORS["err"]}.get(r.status)
            if r.cleared_at:
                color = COLORS["muted"]
            set_row(self.table, i, [str(r.id), r.card_id, r.season_folder, r.dir_name, r.station or "",
                                    STATUS_JA.get(r.status, r.status), str(r.n_files), human_size(r.total_bytes),
                                    r.finished_at or r.started_at or "", r.nas_verified_at or "",
                                    r.cleared_at or "", r.message or ""], color)
        ev = self.ctx.ledger.recent_events(300)
        self.events.setRowCount(len(ev))
        for i, e in enumerate(ev):
            set_row(self.events, i, [e["ts"], e["kind"], e["card_id"] or "", e["message"] or ""])

    def rebuild(self) -> None:
        s = self.ctx.settings
        if not s.dest_root:
            QMessageBox.warning(self, "台帳", "保存先が設定されていません。")
            return
        n = self.ctx.ledger.rebuild_from_manifests(s.dest_root, s.nas_root or None)
        QMessageBox.information(self, "台帳", f"{n} 件を manifest から補完しました。")
        self.refresh()
