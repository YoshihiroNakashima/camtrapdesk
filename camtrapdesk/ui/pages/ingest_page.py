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
        self.season_edit.setPlaceholderText("例: N260710（Type＋調査日。取り込んだカードはこのフォルダの下に入ります）")
        self.season_edit.editingFinished.connect(self._season_changed)
        form.addRow("シーズンフォルダ名", self.season_edit)
        self.dest_label = QLabel()
        self.dest_btn = QPushButton("中継先を変更…")
        self.dest_btn.setToolTip("中継のSSDがいっぱいになったら、別のSSDを繋いでここでそのフォルダを選ぶと、空き待ちのカードがそちらへ続きます")
        self.dest_btn.clicked.connect(self._change_dest)
        hd = QHBoxLayout()
        hd.addWidget(self.dest_label, 1)
        hd.addWidget(self.dest_btn)
        form.addRow("保存先（中継）", hd)
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
        self.assign_btn = QPushButton("IDの無いカードにIDを付ける…")
        self.assign_btn.setToolTip("id.txt が無いカードに ID を書き込みます（カードへの唯一の書き込み）")
        self.assign_btn.clicked.connect(self._assign_ids)
        self.slots_btn = QPushButton("スロットに名前を付ける…")
        self.slots_btn.setToolTip("リーダーの差し口ごとに名前（例: 1-A）を付けると、ドライブ文字が変わっても同じ名前で表示されます")
        self.slots_btn.clicked.connect(self._name_slots)
        self.diag_btn = QPushButton("診断")
        self.diag_btn.setToolTip("Windows が認識している全ボリュームと id.txt の状態を一覧にします")
        self.diag_btn.clicked.connect(self._diagnostics)
        for w in (self.auto_chk, self.pause_btn, self.start_btn, self.rescan_btn, self.clear_btn, self.assign_btn,
                  self.slots_btn, self.diag_btn):
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
        self.dest_label.setText(dest)
        self.nas_label.setText(s.nas_root or "（NAS転送なし。保存先が最終保存先になります）")
        if self.season_edit.text() != s.season_folder:
            self.season_edit.setText(s.season_folder)

    def _change_dest(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        s = self.ctx.settings
        d = QFileDialog.getExistingDirectory(self, "中継先のフォルダを選ぶ（例: 別のSSDの CamTrap フォルダ）", s.dest_root or "")
        if not d:
            return
        d = os.path.normpath(d)
        if d == s.dest_root:
            return
        s.dest_root = d
        self.ctx.save()
        self._ensure_season_dir()
        self._refresh_labels()
        waiting = sum(1 for j in self.ctx.ingest.jobs.values() if j.state == I.S_WAITING)
        note = f"\n空き待ちのカード {waiting} 枚は、次の確認（10秒以内）でこちらに取り込みを始めます。" if waiting else ""
        QMessageBox.information(self, "中継先", f"中継先を {d} にしました。{note}\n\n以前の中継先に残っている分のNAS転送は、そのSSDが繋がっているあいだに続きます。")

    def _season_changed(self) -> None:
        v = self.season_edit.text().strip()
        if v != self.ctx.settings.season_folder:
            self.ctx.settings.season_folder = v
            self.ctx.save()
        self._ensure_season_dir()

    def _ensure_season_dir(self) -> None:
        """保存先にシーズンフォルダを先に作っておく（名前変更画面などから見えるように）。"""
        s = self.ctx.settings
        if s.dest_root and s.season_folder and os.path.isdir(s.dest_root):
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
        if not self.ctx.settings.season_folder.strip():
            QMessageBox.warning(self, "シーズンフォルダ名", "取り込み先のシーズンフォルダ名を入力してください（例: N260710）。")
            return
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

    def _assign_ids(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView
        self.ctx.ingest.rescan_now()
        others = [(k, v, r) for k, (v, r) in self.ctx.ingest.others.items() if v.kind != "unreadable"]
        if not others:
            QMessageBox.information(self, "IDの付与", "id.txt の無いカードは挿さっていません。")
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("IDの無いカードにIDを付ける")
        dlg.resize(700, 320)
        lay = QVBoxLayout(dlg)
        info = QLabel("カードに貼ってある番号などを入力してください（例: 6_650）。空欄のカードには何もしません。"
                      "\n入力した ID は、そのカードの直下に id.txt として書き込まれます。")
        info.setWordWrap(True)
        lay.addWidget(info)
        tbl = QTableWidget(len(others), 4)
        tbl.setHorizontalHeaderLabels(["ボリューム", "ラベル", "状態", "付けるID（入力）"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, (k, v, r) in enumerate(others):
            for c, val in enumerate([v.display, v.label, r]):
                it = QTableWidgetItem(val)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(i, c, it)
            tbl.setItem(i, 3, QTableWidgetItem(""))
        lay.addWidget(tbl)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        results = []
        for i, (k, v, r) in enumerate(others):
            it = tbl.item(i, 3)
            cid = it.text().strip() if it else ""
            if not cid:
                continue
            ok, msg = self.ctx.ingest.assign_card_id(k, cid)
            results.append(f"{v.display}: {cid} → {msg}")
        if results:
            QMessageBox.information(self, "IDの付与", "\n".join(results))
        self.ctx.ingest.rescan_now()

    def _name_slots(self) -> None:
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QVBoxLayout, QTableWidget, QTableWidgetItem, QHeaderView
        self.ctx.ingest.rescan_now()
        vols = []
        for j in self.ctx.ingest.jobs.values():
            vols.append((j.card.volume, j.card.card_id))
        for v, _r in self.ctx.ingest.others.values():
            vols.append((v, ""))
        if not vols:
            QMessageBox.information(self, "スロットの名前", "カードが挿さっているスロットがありません。")
            return
        vols.sort(key=lambda t: self.ctx.slot_sort_key(t[0]))
        dlg = QDialog(self)
        dlg.setWindowTitle("スロットに名前を付ける")
        dlg.resize(760, 420)
        lay = QVBoxLayout(dlg)
        info = QLabel("いま挿さっているカードについて、そのカードが入っている差し口の名前を入力してください（例: 1-A、1-B、…）。"
                      "カードIDを手がかりに、どのリーダーのどのスロットかを確認して付けます。"
                      "\n名前はスロット（リーダーの差し口）に残り、カードを入れ替えても、ドライブ文字が変わっても同じ名前で表示されます。"
                      "リーダーを別のUSBポートに挿し替えると付け直しが必要です。")
        info.setWordWrap(True)
        lay.addWidget(info)
        tbl = QTableWidget(len(vols), 4)
        tbl.setHorizontalHeaderLabels(["ドライブ", "カードID", "現在の名前", "スロット名（入力）"])
        tbl.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for i, (v, cid) in enumerate(vols):
            cur = self.ctx.settings.slot_names.get(v.key, "")
            for c, val in enumerate([v.display, cid, cur]):
                it = QTableWidgetItem(val)
                it.setFlags(it.flags() & ~Qt.ItemFlag.ItemIsEditable)
                tbl.setItem(i, c, it)
            tbl.setItem(i, 3, QTableWidgetItem(cur))
        lay.addWidget(tbl)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        btns.accepted.connect(dlg.accept)
        btns.rejected.connect(dlg.reject)
        lay.addWidget(btns)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        names = dict(self.ctx.settings.slot_names)
        for i, (v, _cid) in enumerate(vols):
            it = tbl.item(i, 3)
            val = it.text().strip() if it else ""
            if val:
                names[v.key] = val
            else:
                names.pop(v.key, None)
        self.ctx.settings.slot_names = names
        self.ctx.save()
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
            set_row(self.table, r, [self.ctx.slot_label(v), "", "", "", state, "", f"{v.label}: {reason}"], color)
        extra = ""
        if n_noid:
            extra += f" / IDなし {n_noid}"
        if n_unread:
            extra += f" / 読めません {n_unread}"
        self.summary_label.setText(f"検出 {len(jobs)} 枚 / 処理中 {n_active} / 完了 {n_done} / エラー {n_err}{extra}")
