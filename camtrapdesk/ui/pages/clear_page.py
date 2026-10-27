"""削除画面: 挿入中のカードを台帳と突合し、検証済みのファイルだけを削除する。"""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit, QPushButton,
                               QSplitter, QVBoxLayout, QWidget)

from ...core.cards import detect_cards
from ...core.clear import ClearVerdict, check_card, delete_files
from ...core.volumes import list_volumes
from ..context import AppContext
from ..widgets import COLORS, human_size, make_table, set_row


class ClearPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.verdicts: list[ClearVerdict] = []

        root = QVBoxLayout(self)
        info = QLabel("挿入中のカードについて、台帳（取り込み・検証の記録）と照合し、一致したファイルだけを削除します。"
                      "id.txt は残します。台帳に無いファイルは削除しません。")
        info.setWordWrap(True)
        root.addWidget(info)
        ops = QHBoxLayout()
        self.check_btn = QPushButton("挿入中のカードを判定")
        self.check_btn.clicked.connect(self.check)
        self.hash_chk = QCheckBox("ハッシュも再計算して照合（時間がかかります）")
        self.offline_chk = QCheckBox("保存先に接続できないときも台帳の記録だけで許可する")
        self.delete_btn = QPushButton("判定OKのカードの動画を削除")
        self.delete_btn.setEnabled(False)
        self.delete_btn.clicked.connect(self.delete)
        for w in (self.check_btn, self.hash_chk, self.offline_chk, self.delete_btn):
            ops.addWidget(w)
        ops.addStretch(1)
        root.addLayout(ops)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["スロット", "カードID", "判定", "取り込み先", "削除対象", "容量", "残す", "理由・注意"])
        split.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        split.addWidget(self.log)
        split.setSizes([400, 140])
        root.addWidget(split, 1)

    def check(self) -> None:
        s = self.ctx.settings
        exclude = list(s.exclude_volumes) + [p for p in (s.dest_root, s.nas_root) if p]
        vols = list_volumes(s.scan_roots, exclude)
        cards = detect_cards(vols, s.id_file_name, s.media_subdirs, s.media_extensions, s.media_recursive, 0)
        cards.sort(key=lambda c: self.ctx.slot_sort_key(c.volume))
        self.verdicts = [check_card(c, self.ctx.ledger, s, verify_hash=self.hash_chk.isChecked(),
                                    allow_offline_destination=self.offline_chk.isChecked()) for c in cards]
        self.table.setRowCount(len(self.verdicts))
        n_ok = 0
        for r, v in enumerate(self.verdicts):
            ing = v.ingest
            where = f"{ing.season_folder}/{ing.dir_name}" if ing else ""
            set_row(self.table, r, [self.ctx.slot_label(v.card.volume), v.card.card_id, "削除OK" if v.ok else "削除しない", where,
                                    str(len(v.deletable)), human_size(v.deletable_bytes), str(len(v.extra)),
                                    "; ".join(v.reasons)], COLORS["ok"] if v.ok else COLORS["err"])
            n_ok += 1 if v.ok else 0
        self.delete_btn.setEnabled(n_ok > 0)
        self.log.appendPlainText(f"{len(self.verdicts)} 枚を判定: 削除OK {n_ok} 枚")

    def delete(self) -> None:
        targets = [v for v in self.verdicts if v.ok]
        if not targets:
            return
        n_files = sum(len(v.deletable) for v in targets)
        size = sum(v.deletable_bytes for v in targets)
        msg = (f"{len(targets)} 枚のカードから {n_files} ファイル（{human_size(size)}）を削除します。\n"
               "この操作は元に戻せません。保存先に検証済みのコピーがあることは台帳で確認済みです。\n\n実行しますか？")
        if QMessageBox.question(self, "削除の実行", msg) != QMessageBox.StandardButton.Yes:
            return
        total = 0
        for v in targets:
            n, errs = delete_files(v, self.ctx.ledger, log=self.log.appendPlainText)
            total += n
            for e in errs:
                self.log.appendPlainText("  エラー: " + e)
        self.log.appendPlainText(f"合計 {total} ファイルを削除しました。")
        self.check()
