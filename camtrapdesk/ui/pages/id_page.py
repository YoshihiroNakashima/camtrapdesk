"""最初に一度だけ: 新しいSDカードにID（id.txt）を付ける画面。任意でボリューム名もIDに合わせる。"""

from __future__ import annotations

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton,
                               QTableWidgetItem, QVBoxLayout, QWidget)

from ...core.volume_label import label_too_long, proposed_label, set_volume_label
from ..context import AppContext
from ..widgets import COLORS, make_table, set_row


class IdPage(QWidget):
    COL_INPUT = 4

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.rows: list[tuple[str, object, str, str]] = []   # (key, volume, 現在のID, 状態)
        root = QVBoxLayout(self)
        info = QLabel(
            "<b>新しいSDカードを使い始めるときに一度だけ行います。</b><br>"
            "カードの直下に id.txt（カードの番号）を書き込みます。番号はカードに貼ったラベルと同じにしてください。"
            "一度付ければ、以後はカードを挿すだけで番号が読み取られ、野帳の回収SD列と照合されます。<br>"
            "すでにIDがあるカードには何もしません（書き換えたいときはカードの id.txt を直接編集してください）。<br>"
            "id.txt を消してしまったカードは、以前の取り込みで覚えたボリュームの番号から前回のIDを候補に入れます"
            "（フォーマットした場合は番号が変わるので、カードのラベルを見て入力してください）。")
        info.setWordWrap(True)
        root.addWidget(info)

        ops = QHBoxLayout()
        self.scan_btn = QPushButton("挿さっているカードを表示")
        self.scan_btn.clicked.connect(self.scan)
        self.apply_btn = QPushButton("入力したIDを書き込む")
        self.apply_btn.clicked.connect(self.apply)
        ops.addWidget(self.scan_btn)
        ops.addWidget(self.apply_btn)
        ops.addSpacing(24)
        self.label_chk = QCheckBox("ボリューム名（エクスプローラーに出るカードの名前）もIDに合わせる")
        self.label_chk.setToolTip("任意。取り込みには不要ですが、エクスプローラーでどのカードか見分けやすくなります。FAT32 の制限で11文字まで")
        self.label_prefix = QLineEdit(ctx.settings.volume_label_prefix)
        self.label_prefix.setPlaceholderText("接頭辞（任意）")
        self.label_prefix.setMaximumWidth(120)
        ops.addWidget(self.label_chk)
        ops.addWidget(self.label_prefix)
        ops.addStretch(1)
        root.addLayout(ops)

        self.table = make_table(["スロット", "ボリューム名", "現在のID", "状態", "付けるID（ここに入力）", "結果"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.AllEditTriggers)
        root.addWidget(self.table, 1)
        # 画面を開いている間は、カードの抜き差しに合わせて一覧を自動で更新する（入力途中の ID は残す）
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(lambda: self.scan(auto=True))
        self._keys: tuple = ()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.scan(auto=True)
        self.timer.start()

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def _pending_edits(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for i, (key, _v, cid, _state) in enumerate(self.rows):
            if cid:
                continue
            it = self.table.item(i, self.COL_INPUT)
            if it is not None and it.text().strip():
                out[key] = it.text()
        return out

    def scan(self, auto: bool = False) -> None:
        """挿さっているカードを一覧にする。auto=True は自動更新（抜き差しが無ければ何もしない）。"""
        if not auto:
            self.ctx.ingest.rescan_now()
        rows: list[tuple[str, object, str, str]] = []
        for key, job in self.ctx.ingest.jobs.items():
            rows.append((key, job.card.volume, job.card.card_id, "ID あり"))
        suggested = dict(self.ctx.ingest.suggested_ids)
        for key, (v, reason) in self.ctx.ingest.others.items():
            if v.kind == "unreadable":
                state = "読めません: " + reason
            elif key in suggested:
                state = f"ID なし（前回は {suggested[key]}。候補を入れました）"
            else:
                state = "ID なし"
            rows.append((key, v, "", state))
        rows.sort(key=lambda r: self.ctx.slot_sort_key(r[1]))
        keys = tuple((k, cid, state) for k, _v, cid, state in rows)
        if auto and keys == self._keys:
            return
        edits = self._pending_edits()
        self._keys = keys
        self.rows = rows
        self.table.setRowCount(len(rows))
        for i, (key, v, cid, state) in enumerate(rows):
            color = COLORS["ok"] if cid else (COLORS["err"] if v.kind == "unreadable" else COLORS["warn"])
            prefill = "" if cid else edits.get(key, suggested.get(key, ""))
            set_row(self.table, i, [self.ctx.slot_label(v), v.label, cid, state, prefill, ""], color)
            it = self.table.item(i, self.COL_INPUT)
            if cid or v.kind == "unreadable":
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
            else:
                it.setFlags(it.flags() | Qt.ItemFlag.ItemIsEditable)
        if not rows and not auto:
            QMessageBox.information(self, "カードにIDを付ける", "カードが挿さっていません。")

    def apply(self) -> None:
        if not self.rows:
            self.scan()
            if not self.rows:
                return
        prefix = self.label_prefix.text().strip()
        if prefix != self.ctx.settings.volume_label_prefix:
            self.ctx.settings.volume_label_prefix = prefix
            self.ctx.save()
        todo: list[tuple[int, str, object, str]] = []
        for i, (key, v, cid, _state) in enumerate(self.rows):
            if cid or v.kind == "unreadable":
                continue
            it = self.table.item(i, self.COL_INPUT)
            new = it.text().strip() if it else ""
            if new:
                todo.append((i, key, v, new))
        relabel = self.label_chk.isChecked()
        if not todo and not relabel:
            QMessageBox.information(self, "カードにIDを付ける", "「付けるID」が入力されたカードがありません。")
            return
        n_written = 0
        for i, key, v, new in todo:
            ok, msg = self.ctx.ingest.assign_card_id(key, new)
            self.table.setItem(i, 5, QTableWidgetItem(msg))
            if ok:
                n_written += 1
                self.rows[i] = (key, v, new, "ID あり")
                self.table.setItem(i, 2, QTableWidgetItem(new))
                self.table.setItem(i, 3, QTableWidgetItem("ID あり（いま書き込み）"))
        if relabel:
            for i, (key, v, cid, _state) in enumerate(self.rows):
                if not cid or getattr(v, "kind", "") == "unreadable":
                    continue
                label = proposed_label(prefix, cid)
                if v.label == label:
                    continue
                if label_too_long(label):
                    self.table.setItem(i, 5, QTableWidgetItem(f"ボリューム名 '{label}' は11文字を超えるため変更しません"))
                    continue
                ok, msg = set_volume_label(v, label)
                self.table.setItem(i, 5, QTableWidgetItem(f"ボリューム名 → {label}: {msg}"))
        self.ctx.ingest.rescan_now()
        self._keys = ()
        if todo:
            QMessageBox.information(self, "カードにIDを付ける",
                                    f"{n_written} 枚に ID を書き込みました。「取り込み」画面に戻ると、挿したままで取り込みが始まります。")
