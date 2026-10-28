"""リーダーを挿し替えたら: カードリーダーの差し口（スロット）に名前を付ける画面。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QAbstractItemView, QHBoxLayout, QLabel, QMessageBox, QPushButton, QVBoxLayout, QWidget

from ..context import AppContext
from ..widgets import COLORS, make_table, set_row


class SlotPage(QWidget):
    COL_INPUT = 3

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.vols: list[tuple[object, str]] = []   # (volume, カードID)
        root = QVBoxLayout(self)
        info = QLabel(
            "<b>カードリーダーを初めて繋いだとき、別のUSBポートに挿し替えたときに行います。</b><br>"
            "差し口（スロット）ごとに名前を付けておくと、ドライブ文字（E:, F:, …）が変わっても取り込み画面に同じ名前で表示され、"
            "どのカードがどこに入っているか分かります。<br>"
            "やり方: 各スロットにカードを1枚ずつ挿し、表のカードIDを手がかりに、そのスロットの名前（例: 1-A、1-B、… や U1-3-B）を入力して保存します。"
            "名前の最後の区切りより前（1 や U1-3）が同じリーダーとみなされ、コピーが1台のリーダーに偏らないよう順番が組まれます。")
        info.setWordWrap(True)
        root.addWidget(info)

        ops = QHBoxLayout()
        self.scan_btn = QPushButton("挿さっているカードを表示")
        self.scan_btn.clicked.connect(self.scan)
        self.apply_btn = QPushButton("名前を保存")
        self.apply_btn.clicked.connect(self.apply)
        self.clear_btn = QPushButton("保存されている名前をすべて消す")
        self.clear_btn.clicked.connect(self.clear_all)
        ops.addWidget(self.scan_btn)
        ops.addWidget(self.apply_btn)
        ops.addWidget(self.clear_btn)
        ops.addStretch(1)
        root.addLayout(ops)

        self.table = make_table(["ドライブ", "カードID", "現在の名前", "スロット名（ここに入力）"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        root.addWidget(self.table, 1)
        self.count_label = QLabel("")
        root.addWidget(self.count_label)
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self.ctx.settings.slot_names)
        self.count_label.setText(f"保存されているスロット名: {n} 個")

    def scan(self) -> None:
        self.ctx.ingest.rescan_now()
        vols: list[tuple[object, str]] = []
        for j in self.ctx.ingest.jobs.values():
            vols.append((j.card.volume, j.card.card_id))
        for v, _r in self.ctx.ingest.others.values():
            vols.append((v, ""))
        vols.sort(key=lambda t: self.ctx.slot_sort_key(t[0]))
        self.vols = vols
        self.table.setRowCount(len(vols))
        for i, (v, cid) in enumerate(vols):
            cur = self.ctx.settings.slot_names.get(v.key, "")
            set_row(self.table, i, [v.display, cid, cur, cur], COLORS["ok"] if cur else None)
            it = self.table.item(i, self.COL_INPUT)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
        if not vols:
            QMessageBox.information(self, "スロットに名前を付ける", "カードが挿さっているスロットがありません。各スロットにカードを1枚ずつ挿してください。")

    def apply(self) -> None:
        if not self.vols:
            QMessageBox.information(self, "スロットに名前を付ける", "まず「挿さっているカードを表示」を押してください。")
            return
        names = dict(self.ctx.settings.slot_names)
        changed = 0
        for i, (v, _cid) in enumerate(self.vols):
            it = self.table.item(i, self.COL_INPUT)
            val = it.text().strip() if it else ""
            if val != names.get(v.key, ""):
                changed += 1
            if val:
                names[v.key] = val
            else:
                names.pop(v.key, None)
        self.ctx.settings.slot_names = names
        self.ctx.save()
        self._refresh_count()
        self.scan()
        QMessageBox.information(self, "スロットに名前を付ける", f"{changed} 個の名前を保存しました。")

    def clear_all(self) -> None:
        if QMessageBox.question(self, "スロットの名前", "保存されているスロット名をすべて消します。よろしいですか？") \
                != QMessageBox.StandardButton.Yes:
            return
        self.ctx.settings.slot_names = {}
        self.ctx.save()
        self._refresh_count()
        if self.vols:
            self.scan()
