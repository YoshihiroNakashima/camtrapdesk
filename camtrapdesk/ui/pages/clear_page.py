"""削除画面: 挿入中のカードを台帳と突合し、検証済みのファイルだけを削除する。

全部の移行が終わってからまとめて消す使い方が多いので、
 - カードを挿すと自動で照合して、消せるか／消せない理由を一覧に出す
 - 「照合して、挿さっているカードを消す」の 1 ボタンで、消せるものを消し、消せなかったものを理由付きで知らせる
 - 台帳から「このフォルダに取り込んだカードのうち、まだ消していないもの」を出し、消し忘れを見つけられる
"""

from __future__ import annotations

import threading
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from ...core import ledger as L
from ...core.cards import detect_cards
from ...core.clear import ClearVerdict, check_card, delete_files
from ...core.volumes import list_volumes
from ..context import AppContext
from ..widgets import COLORS, enable_sorting, human_size, make_table, refill_sorted, set_row, volume_sort_key


class ClearPage(QWidget):
    _checked = Signal(object, bool)   # (verdicts, then_delete) 照合スレッドから

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.verdicts: list[ClearVerdict] = []
        self._checking = False
        self._keys_seen: tuple = ()
        self._dirty = True

        root = QVBoxLayout(self)
        info = QLabel("挿さっているカードを台帳（取り込み・検証の記録）と自動で照合し、保存先（NAS を使うなら NAS）に正しくコピーされていると"
                      "確認できたファイルだけを消します。id.txt は残します。台帳に無いファイル（取り込み後に撮ったものなど）は消しません。"
                      "下の「取り込んだカードの消し忘れ」で、まだ消していないカードを確かめられます。")
        info.setWordWrap(True)
        root.addWidget(info)
        ops = QHBoxLayout()
        self.delete_btn = QPushButton("照合して、挿さっているカードを消す")
        self.delete_btn.setStyleSheet("font-weight: bold;")
        self.delete_btn.setToolTip("挿さっているカードをもう一度照合してから、消せるカードの動画を消します。消せないカードは理由を表示します")
        self.delete_btn.clicked.connect(self.delete_all)
        self.check_btn = QPushButton("照合し直す")
        self.check_btn.clicked.connect(lambda: self.check(then_delete=False))
        self.repair_btn = QPushButton("カードを修復（chkdsk）…")
        self.repair_btn.setToolTip("壊れていて消せないファイルがあるカードを Windows の chkdsk で修復します")
        self.repair_btn.clicked.connect(self._open_repair)
        self.hash_chk = QCheckBox("ハッシュも再計算して照合（時間がかかります）")
        self.offline_chk = QCheckBox("保存先に接続できないときも台帳の記録だけで許可する")
        for w in (self.delete_btn, self.check_btn, self.repair_btn, self.hash_chk, self.offline_chk):
            ops.addWidget(w)
        ops.addStretch(1)
        self.summary_label = QLabel("")
        ops.addWidget(self.summary_label)
        root.addLayout(ops)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["ドライブ", "カードID", "判定", "取り込み先", "削除対象", "容量", "残す", "理由・注意"])
        enable_sorting(self.table, 1)
        split.addWidget(self.table)

        # 台帳から見た消し忘れ
        box = QGroupBox("取り込んだカードの消し忘れ（台帳から）")
        bl = QVBoxLayout(box)
        hb = QHBoxLayout()
        self.folder_combo = QComboBox()
        self.folder_combo.setMinimumWidth(220)
        self.folder_combo.currentIndexChanged.connect(lambda _i: self.refresh_pending())
        self.folder_refresh = QPushButton("更新")
        self.folder_refresh.clicked.connect(self.refresh_folders)
        hb.addWidget(QLabel("取り込み先のフォルダ"))
        hb.addWidget(self.folder_combo)
        hb.addWidget(self.folder_refresh)
        hb.addStretch(1)
        bl.addLayout(hb)
        self.pending_label = QLabel("")
        self.pending_label.setWordWrap(True)
        self.pending_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        bl.addWidget(self.pending_label)
        split.addWidget(box)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        split.addWidget(self.log)
        split.setSizes([360, 120, 120])
        root.addWidget(split, 1)

        self._checked.connect(self._on_checked)
        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self._tick)
        ctx.settings_changed.connect(self.refresh_folders)
        ctx.nas_status.connect(self._nas_status)
        self._nas_idle = True
        self.refresh_folders()

    def _nas_status(self, msg: str) -> None:
        # NAS 転送が一段落したら（転送中 → 待機中）、挿さっているカードを照合し直す
        idle = "未転送なし" in msg
        if idle and not self._nas_idle:
            self._dirty = True
        self._nas_idle = idle

    # ------------------------------------------------------------- 自動更新
    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refresh_folders()
        self._dirty = True
        self.timer.start()

    def hideEvent(self, e) -> None:  # noqa: N802
        self.timer.stop()
        super().hideEvent(e)

    def _present_keys(self) -> tuple:
        ing = self.ctx.ingest
        return tuple(sorted(list(ing.jobs) + list(ing.others)))

    def _tick(self) -> None:
        keys = self._present_keys()
        if keys != self._keys_seen:
            self._keys_seen = keys
            self._dirty = True
        if self._dirty and not self._checking:
            self.check(then_delete=False, auto=True)

    # ----------------------------------------------------------------- 照合
    def _detect(self):
        s = self.ctx.settings
        exclude = list(s.exclude_volumes) + [p for p in (s.dest_root, s.nas_root) if p]
        vols = list_volumes(s.scan_roots, exclude)
        cards = detect_cards(vols, s.id_file_name, s.media_subdirs, s.media_extensions, s.media_recursive, 0)
        cards.sort(key=lambda c: volume_sort_key(c.volume))
        return cards

    def check(self, then_delete: bool = False, auto: bool = False) -> None:
        """挿さっているカードを照合する（NAS 上のファイルを確かめるので別スレッドで）。"""
        if self._checking:
            return
        self._checking = True
        self._dirty = False
        if not auto:
            self.summary_label.setText("照合中…")
        s = self.ctx.settings
        verify_hash = self.hash_chk.isChecked()
        offline = self.offline_chk.isChecked()
        ledger = self.ctx.ledger

        def work() -> None:
            try:
                cards = self._detect()
                verdicts = [check_card(c, ledger, s, verify_hash=verify_hash, allow_offline_destination=offline) for c in cards]
            except Exception as e:  # pragma: no cover
                verdicts = []
                self.ctx.log_message.emit(f"カードの照合でエラー: {e}")
            self._checked.emit(verdicts, then_delete)

        threading.Thread(target=work, name="clear-check", daemon=True).start()

    def _on_checked(self, verdicts: list, then_delete: bool) -> None:
        self._checking = False
        self.verdicts = verdicts
        n_ok = sum(1 for v in verdicts if v.ok)
        with refill_sorted(self.table):
            self._fill_rows(verdicts)
        self.summary_label.setText(f"挿入中 {len(verdicts)} 枚: 消せる {n_ok} / 消せない {len(verdicts) - n_ok}"
                                   + "　" + time.strftime("%H:%M:%S"))
        if then_delete:
            self._confirm_and_delete()

    def _fill_rows(self, verdicts: list) -> None:
        self.table.setRowCount(len(verdicts))
        for r, v in enumerate(verdicts):
            ing = v.ingest
            where = f"{ing.season_folder}/{ing.dir_name}" if ing else ""
            if v.ok:
                color = COLORS["ok"]
            elif any("削除済み" in x for x in v.reasons):
                color = COLORS["muted"]   # もう消してあるカード
            else:
                color = COLORS["err"]
            set_row(self.table, r, [v.card.volume.display, v.card.card_id, "削除OK" if v.ok else "削除しない", where,
                                    str(len(v.deletable)), human_size(v.deletable_bytes), str(len(v.extra)),
                                    "; ".join(v.reasons)], color)

    # ----------------------------------------------------------------- 削除
    def delete_all(self) -> None:
        """照合し直してから、消せるカードをすべて消す。"""
        if not self._present_keys() and not self.verdicts:
            QMessageBox.information(self, "カードを消す", "カードが挿さっていません。")
            return
        self.summary_label.setText("照合中…")
        self.check(then_delete=True)

    def _confirm_and_delete(self) -> None:
        targets = [v for v in self.verdicts if v.ok]
        others = [v for v in self.verdicts if not v.ok]
        if not targets:
            lines = ["消せるカードがありません。"]
            if others:
                lines.append("")
                lines += [f"{v.card.card_id}: {'; '.join(v.reasons)}" for v in others[:20]]
                if len(others) > 20:
                    lines.append(f"（ほか {len(others) - 20} 枚）")
            QMessageBox.information(self, "カードを消す", "\n".join(lines))
            return
        n_files = sum(len(v.deletable) for v in targets)
        size = sum(v.deletable_bytes for v in targets)
        msg = (f"挿さっている {len(self.verdicts)} 枚のうち {len(targets)} 枚から {n_files} ファイル（{human_size(size)}）を削除します。\n"
               "保存先に検証済みのコピーがあることは台帳と実物で確認済みです。この操作は元に戻せません。")
        if others:
            msg += f"\n\n残り {len(others)} 枚は消しません（理由は表の「理由・注意」）。"
        msg += "\n\n実行しますか？"
        if QMessageBox.question(self, "削除の実行", msg) != QMessageBox.StandardButton.Yes:
            return
        total = 0
        failed: list[str] = []
        for v in targets:
            n, errs = delete_files(v, self.ctx.ledger, log=self.log.appendPlainText)
            total += n
            for e in errs:
                self.log.appendPlainText("  エラー: " + e)
            if errs:
                failed.append(v.card.card_id)
        self.log.appendPlainText(f"合計 {total} ファイルを削除しました。")
        self.refresh_pending()
        self._dirty = True
        # 結果をはっきり知らせる（消していないカードがあれば、その理由も）
        lines = [f"{len(targets)} 枚から {total} ファイルを削除しました。"]
        if failed:
            lines.append(f"削除でエラーになったカード: {', '.join(failed)}（ログを確認）。"
                         "「ファイルまたはディレクトリが壊れている」なら「カードを修復（chkdsk）…」で直してからもう一度消してください")
        if others:
            lines.append("")
            lines.append(f"消していないカード {len(others)} 枚:")
            lines += [f"  {v.card.card_id}: {'; '.join(v.reasons)}" for v in others[:20]]
            if len(others) > 20:
                lines.append(f"  （ほか {len(others) - 20} 枚）")
            lines.append("")
            lines.append("NAS への転送がまだのカードは、転送が終わってからもう一度押してください。")
        pend = self._pending_summary()
        if pend:
            lines.append("")
            lines.append(pend)
        QMessageBox.information(self, "削除の結果", "\n".join(lines))
        self.check(then_delete=False)

    def _open_repair(self) -> None:
        from ..repair_dialog import RepairDialog
        pre = {v.card.volume.key for v in self.verdicts if not v.ok and any("壊れて" in r or "エラー" in r for r in v.reasons)}
        RepairDialog(self.ctx, self, preselect=pre or None).exec()

    # ---------------------------------------------------------- 消し忘れ
    def refresh_folders(self) -> None:
        folders = self.ctx.ledger.season_folders()
        cur = self.ctx.settings.season_folder.strip()
        prev = self.folder_combo.currentData()
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        for f in folders:
            self.folder_combo.addItem(f or "（保存先の直下）", f)
        want = prev if prev is not None else cur
        idx = self.folder_combo.findData(want)
        self.folder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.folder_combo.blockSignals(False)
        self.refresh_pending()

    def _pending_summary(self) -> str:
        folder = self.folder_combo.currentData()
        if folder is None:
            return ""
        cards = self.ctx.ledger.cards_in_folder(folder)
        if not cards:
            return ""
        cleared = [c for c in cards if c.cleared_at]
        pending = [c for c in cards if not c.cleared_at]
        name = folder or "（保存先の直下）"
        text = f"{name}: 取り込んだカード {len(cards)} 枚、削除済み {len(cleared)} 枚、まだ消していない {len(pending)} 枚"
        if pending:
            not_synced = [c for c in pending if c.nas_root and c.status != L.STATUS_SYNCED]
            ids = ", ".join(c.card_id for c in pending[:60])
            if len(pending) > 60:
                ids += f" …（ほか {len(pending) - 60} 枚）"
            text += f"\nまだ消していないカード: {ids}"
            if not_synced:
                text += f"\nそのうち NAS への転送・検証が終わっていないもの: {len(not_synced)} 枚（" + \
                        ", ".join(c.card_id for c in not_synced[:30]) + ("…" if len(not_synced) > 30 else "") + "）"
        else:
            text += "。このフォルダのカードはすべて消してあります"
        return text

    def refresh_pending(self) -> None:
        self.pending_label.setText(self._pending_summary() or "台帳に取り込みの記録がありません")
