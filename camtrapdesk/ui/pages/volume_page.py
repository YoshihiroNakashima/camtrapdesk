"""ボリューム名の変更画面（任意）: カードのボリュームラベルを「接頭辞＋カードID」にする。"""

from __future__ import annotations

from PySide6.QtWidgets import QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ...core.cards import detect_cards
from ...core.volume_label import label_too_long, proposed_label, set_volume_label
from ...core.volumes import list_volumes
from ..context import AppContext
from ..widgets import COLORS, make_table, set_row


class VolumePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.cards = []
        root = QVBoxLayout(self)
        info = QLabel("エクスプローラー上でカードを見分けやすくするため、ボリューム名をカードID付きに変更します（取り込みには不要な補助機能です）。")
        info.setWordWrap(True)
        root.addWidget(info)
        ops = QHBoxLayout()
        ops.addWidget(QLabel("接頭辞"))
        self.prefix = QLineEdit(ctx.settings.volume_label_prefix)
        self.prefix.setMaximumWidth(100)
        ops.addWidget(self.prefix)
        self.scan_btn = QPushButton("挿入中のカードを表示")
        self.scan_btn.clicked.connect(self.scan)
        self.apply_btn = QPushButton("名前を変更")
        self.apply_btn.clicked.connect(self.apply)
        ops.addWidget(self.scan_btn)
        ops.addWidget(self.apply_btn)
        ops.addStretch(1)
        root.addLayout(ops)
        self.table = make_table(["スロット", "現在の名前", "カードID", "変更後", "結果"])
        root.addWidget(self.table, 1)

    def scan(self) -> None:
        s = self.ctx.settings
        vols = list_volumes(s.scan_roots, list(s.exclude_volumes) + [p for p in (s.dest_root, s.nas_root) if p])
        self.cards = detect_cards(vols, s.id_file_name, s.media_subdirs, s.media_extensions, False, 0, list_files=False)
        pre = self.prefix.text()
        self.table.setRowCount(len(self.cards))
        for r, c in enumerate(self.cards):
            new = proposed_label(pre, c.card_id)
            note = "11文字を超えます" if label_too_long(new) else ("変更済み" if c.volume.label == new else "")
            set_row(self.table, r, [self.ctx.slot_label(c.volume), c.volume.label, c.card_id, new, note],
                    COLORS["muted"] if note else None)

    def apply(self) -> None:
        if not self.cards:
            self.scan()
        pre = self.prefix.text()
        if pre != self.ctx.settings.volume_label_prefix:
            self.ctx.settings.volume_label_prefix = pre
            self.ctx.save()
        todo = [c for c in self.cards if c.volume.label != proposed_label(pre, c.card_id)]
        if not todo:
            QMessageBox.information(self, "ボリューム名", "変更が必要なカードはありません。")
            return
        if QMessageBox.question(self, "ボリューム名", f"{len(todo)} 枚のボリューム名を変更します。よろしいですか？") \
                != QMessageBox.StandardButton.Yes:
            return
        for r, c in enumerate(self.cards):
            new = proposed_label(pre, c.card_id)
            if c.volume.label == new:
                continue
            ok, msg = set_volume_label(c.volume, new)
            set_row(self.table, r, [self.ctx.slot_label(c.volume), c.volume.label, c.card_id, new, msg],
                    COLORS["ok"] if ok else COLORS["err"])
