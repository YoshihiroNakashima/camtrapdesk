"""取り込み画面: カードを挿すと自動検出・コピー・検証。NAS転送の状況も表示する。"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget,
                               QMessageBox)

from ...core import ingest as I
from ..context import AppContext
from ..widgets import COLORS, human_size, make_table, set_row


class IngestPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._dirty = True
        self._nas_frac = 0.0
        s = ctx.settings

        root = QVBoxLayout(self)

        # --- 上段: 設定の要点 ---
        box = QGroupBox("取り込み先")
        form = QFormLayout(box)
        self.season_edit = QLineEdit(s.season_folder)
        self.season_edit.setPlaceholderText("任意。例: 2026-10 や N260710。取り込んだカードはこのフォルダの下に入ります（空なら保存先の直下）")
        self.season_edit.editingFinished.connect(self._season_changed)
        form.addRow("調査回（シーズン）のフォルダ名", self.season_edit)
        self.dest_label = QLabel()
        form.addRow("保存先（中継）", self.dest_label)
        self.nas_label = QLabel()
        form.addRow("NAS", self.nas_label)
        root.addWidget(box)

        # --- 操作 ---
        ops = QHBoxLayout()
        self.auto_chk = QCheckBox("カードを挿したら自動で開始")
        self.auto_chk.setChecked(s.auto_start_ingest)
        self.auto_chk.toggled.connect(self._auto_toggled)
        self.pause_btn = QPushButton("一時停止")
        self.pause_btn.setCheckable(True)
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.start_btn = QPushButton("検出済みをすべて開始")
        self.start_btn.clicked.connect(self._start_all)
        self.rescan_btn = QPushButton("再スキャン")
        self.rescan_btn.clicked.connect(lambda: self.ctx.ingest.rescan_now())
        self.clear_btn = QPushButton("完了分を一覧から消す")
        self.clear_btn.clicked.connect(self._clear_done)
        self.diag_btn = QPushButton("診断")
        self.diag_btn.setToolTip("Windows が認識している全ボリュームと id.txt の状態を一覧にします")
        self.diag_btn.clicked.connect(self._diagnostics)
        for w in (self.auto_chk, self.pause_btn, self.start_btn, self.rescan_btn, self.clear_btn, self.diag_btn):
            ops.addWidget(w)
        ops.addStretch(1)
        self.summary_label = QLabel("")
        ops.addWidget(self.summary_label)
        root.addLayout(ops)

        # --- 表とログ ---
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["スロット", "カードID", "ファイル数", "容量", "状態", "進捗", "メッセージ"])
        split.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        split.addWidget(self.log)
        split.setSizes([400, 160])
        root.addWidget(split, 1)

        # --- NAS ---
        nas_box = QGroupBox("NASへの転送（バックグラウンド）")
        nl = QHBoxLayout(nas_box)
        self.nas_status = QLabel("NAS転送: 待機中")
        self.nas_bar = QProgressBar()
        self.nas_bar.setRange(0, 1000)
        self.nas_bar.setTextVisible(True)
        self.nas_pause = QPushButton("NAS転送を一時停止")
        self.nas_pause.setCheckable(True)
        self.nas_pause.toggled.connect(self._nas_pause_toggled)
        nl.addWidget(self.nas_status, 2)
        nl.addWidget(self.nas_bar, 1)
        nl.addWidget(self.nas_pause)
        root.addWidget(nas_box)

        # --- シグナル ---
        ctx.job_updated.connect(self._on_job_updated)
        ctx.log_message.connect(self._append_log)
        ctx.batch_done.connect(self._on_batch_done)
        ctx.nas_status.connect(self._on_nas_status)
        ctx.nas_progress.connect(self._on_nas_progress)
        ctx.settings_changed.connect(self._refresh_labels)
        self._refresh_labels()

        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self._refresh_table)
        self.timer.start()
        self.label_timer = QTimer(self)
        self.label_timer.setInterval(5000)
        self.label_timer.timeout.connect(self._refresh_labels)
        self.label_timer.start()
        self._others_sig: tuple = ()

    # ------------------------------------------------------------------ slots
    def _refresh_labels(self) -> None:
        s = self.ctx.settings
        dest = s.dest_root or "（未設定。「設定」で保存先を指定してください）"
        free = I.free_bytes(s.dest_root) if s.dest_root else None
        if free is not None:
            dest += f"　空き {human_size(free)}"
            if s.nas_root and s.staging_cleanup_after_nas:
                dest += "（NASで検証済みの分は自動で消して空けます）"
        elif s.dest_root:
            dest += "　（見つかりません。取り外し中？）"
        dest += "　※変更は「設定」で（いっぱいになったら別のSSDを繋いで設定を変えると続きから取り込みます）"
        self.dest_label.setText(dest)
        self.nas_label.setText(s.nas_root or "（NAS転送なし。保存先が最終保存先になります）")
        if self.season_edit.text() != s.season_folder:
            self.season_edit.setText(s.season_folder)

    def _season_changed(self) -> None:
        v = self.season_edit.text().strip()
        if v != self.ctx.settings.season_folder:
            self.ctx.settings.season_folder = v
            self.ctx.save()
        self._ensure_season_dir()

    def _ensure_season_dir(self) -> None:
        """保存先にシーズンフォルダを先に作っておく（名前変更画面などから見えるように）。"""
        s = self.ctx.settings
        if s.dest_root and s.season_folder.strip() and os.path.isdir(s.dest_root):
            try:
                os.makedirs(os.path.join(s.dest_root, s.season_folder), exist_ok=True)
            except OSError as e:
                self._append_log(f"シーズンフォルダを作成できません: {e}")

    def _auto_toggled(self, on: bool) -> None:
        self.ctx.settings.auto_start_ingest = on
        self.ctx.save()

    def _pause_toggled(self, on: bool) -> None:
        self.ctx.ingest.paused = on
        self.pause_btn.setText("一時停止中（押すと再開）" if on else "一時停止")

    def _nas_pause_toggled(self, on: bool) -> None:
        self.ctx.nas.paused = on
        self.ctx.nas.wake()
        self.nas_pause.setText("NAS転送を再開" if on else "NAS転送を一時停止")

    def _start_all(self) -> None:
        for key, job in list(self.ctx.ingest.jobs.items()):
            if job.state in (I.S_DETECTED, I.S_ERROR, I.S_SKIPPED) and not job.card.problems:
                self.ctx.ingest.enqueue(key)

    def _clear_done(self) -> None:
        for key, job in list(self.ctx.ingest.jobs.items()):
            if job.state in (I.S_DONE, I.S_SKIPPED):
                self.ctx.ingest.remove_job(key)
        self._dirty = True

    def _on_job_updated(self, job) -> None:
        self._dirty = True

    def _diagnostics(self) -> None:
        from ...core.volumes import diagnostics
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout
        s = self.ctx.settings
        exclude = list(s.exclude_volumes) + [p for p in (s.dest_root, s.nas_root) if p]
        try:
            text = diagnostics(s.scan_roots, exclude, s.id_file_name)
        except Exception as e:  # pragma: no cover
            text = f"診断でエラー: {e}"
        jobs = ", ".join(f"{j.card.volume.display}={j.card.card_id}" for j in self.ctx.ingest.jobs.values())
        text += f"\n\nアプリが検出中のカード ({len(self.ctx.ingest.jobs)}): {jobs}"
        dlg = QDialog(self)
        dlg.setWindowTitle("ボリューム診断")
        dlg.resize(1100, 600)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit(text)
        box.setReadOnly(True)
        box.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(box)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        self._append_log("診断:\n" + text)
        dlg.exec()

    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(time.strftime("%H:%M:%S ") + msg)

    def _on_batch_done(self, n: int) -> None:
        from PySide6.QtWidgets import QApplication
        QApplication.beep()
        self._append_log(f"=== バッチ完了: {n}枚のカードが「抜いてOK」になりました ===")
        win = self.window()
        if hasattr(win, "notify"):
            win.notify("取り込み完了", f"{n}枚のカードが完了しました。入れ替えてください。")

    def _on_nas_status(self, msg: str) -> None:
        self.nas_status.setText(msg)
        if "待機中" in msg:
            self.nas_bar.setValue(0)

    def _on_nas_progress(self, name: str, frac: float) -> None:
        self.nas_bar.setValue(int(frac * 1000))
        self.nas_bar.setFormat(f"{name} {frac*100:.0f}%")

    # ----------------------------------------------------------------- table
    def _refresh_table(self) -> None:
        sig = tuple(sorted(self.ctx.ingest.others))
        if sig != self._others_sig:
            self._others_sig = sig
            self._dirty = True
        if not self._dirty:
            return
        self._dirty = False
        jobs = sorted(self.ctx.ingest.jobs.values(), key=lambda j: self.ctx.slot_sort_key(j.card.volume))
        others = sorted(self.ctx.ingest.others.values(), key=lambda o: self.ctx.slot_sort_key(o[0]))
        self.table.setRowCount(len(jobs) + len(others))
        n_done = n_active = n_err = 0
        for r, job in enumerate(jobs):
            st = job.state
            color = {I.S_DONE: COLORS["ok"], I.S_ERROR: COLORS["err"], I.S_SKIPPED: COLORS["muted"],
                     I.S_COPYING: COLORS["info"], I.S_VERIFYING: COLORS["info"], I.S_REMOVED: COLORS["warn"],
                     I.S_WAITING: COLORS["warn"]}.get(st)
            prog = f"{job.progress*100:.0f}%" if st in (I.S_COPYING, I.S_VERIFYING) else ("100%" if st == I.S_DONE else "")
            spd = job.speed_mbps
            if prog and spd:
                prog += f" ({spd:.0f} MB/s)"
            set_row(self.table, r, [self.ctx.slot_label(job.card.volume), job.card.card_id, str(job.card.n_files),
                                    human_size(job.card.total_bytes), I.STATE_LABELS.get(st, st), prog, job.message],
                    color)
            if st == I.S_DONE:
                n_done += 1
            elif st in (I.S_QUEUED, I.S_WAITING, I.S_COPYING, I.S_VERIFYING):
                n_active += 1
            elif st == I.S_ERROR:
                n_err += 1
        n_noid = n_unread = 0
        for i, (v, reason) in enumerate(others):
            r = len(jobs) + i
            if v.kind == "unreadable":
                state, color = "読めません", COLORS["err"]
                n_unread += 1
            else:
                state, color = "IDなし", COLORS["warn"]
                n_noid += 1
                reason = reason + "。左の「カードにIDを付ける」で番号を付けてください"
            set_row(self.table, r, [self.ctx.slot_label(v), "", "", "", state, "", f"{v.label}: {reason}"], color)
        extra = ""
        if n_noid:
            extra += f" / IDなし {n_noid}"
        if n_unread:
            extra += f" / 読めません {n_unread}"
        self.summary_label.setText(f"検出 {len(jobs)} 枚 / 処理中 {n_active} / 完了 {n_done} / エラー {n_err}{extra}")
