"""取り込み画面: カードを挿すと自動検出・コピー・検証。NAS転送の状況も表示する。"""

from __future__ import annotations

import os
import sys
import time

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QPlainTextEdit, QProgressBar, QPushButton, QSplitter, QVBoxLayout, QWidget,
                               QMessageBox)

from ...core import ingest as I
from ..context import AppContext
from ..widgets import COLORS, PathRow, enable_sorting, human_size, make_table, refill_sorted, set_row, volume_sort_key


class IngestPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._dirty = True
        self._nas_frac = 0.0
        s = ctx.settings

        root = QVBoxLayout(self)

        # --- 上段: 保存先（ここで指定する。変えたらすぐ保存され、「保存」ボタンは無い） ---
        box = QGroupBox("保存先（ここで指定します。変更はすぐ反映されます）")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        hd = QHBoxLayout()
        self.dest_row = PathRow(s.dest_root, "dir", placeholder=r"例: D:\CamTrap（中継の SSD/HDD。NAS を直接指定してもよい）")
        self.dest_row.changed.connect(self._dest_changed)
        self.dest_label = QLabel()
        hd.addWidget(self.dest_row, 1)
        hd.addWidget(self.dest_label)
        form.addRow("保存先（中継）", hd)
        self.nas_row = PathRow(s.nas_root, "dir", placeholder=r"例: \\DS2422\camtrap　空なら NAS 転送なし（上の保存先が最終保存先）")
        self.nas_row.changed.connect(self._nas_changed)
        form.addRow("NAS の最終保存先（任意）", self.nas_row)
        self.season_edit = QLineEdit(s.season_folder)
        self.season_edit.setPlaceholderText("任意。同じ調査を繰り返すなら調査回ごと（例: N260710）、一度きりなら調査地名（例: 保尊崎）。空なら保存先の直下")
        self.season_edit.editingFinished.connect(self._season_changed)
        form.addRow("取り込み先のフォルダ名（調査の区切り）", self.season_edit)
        self.convert_chk = QCheckBox("AVI などを H.264 の mp4 に変換してから NAS へ送る（画質やエンコーダは「設定」）")
        self.convert_chk.setChecked(s.convert_enabled)
        self.convert_chk.toggled.connect(self._convert_toggled)
        form.addRow("動画の変換", self.convert_chk)
        root.addWidget(box)

        # --- 操作 ---
        ops = QHBoxLayout()
        self.arm_btn = QPushButton("取り込みを開始")
        self.arm_btn.setToolTip("保存先・フォルダ名・NAS を確認してから、挿さっているカードを始めます。以後、挿したカードは自動で始まります")
        self.arm_btn.clicked.connect(self._arm_clicked)
        self.stop_btn = QPushButton("取り込みを停止")
        self.stop_btn.setToolTip("一覧で選んだカード（選んでいなければ全部）のコピーを止めます。止めたカードは「やり直す」で検証済みのファイルを飛ばして続きから再開できます")
        self.stop_btn.clicked.connect(self._stop_clicked)
        self.start_btn = QPushButton("エラー・停止・スキップ分をやり直す")
        self.start_btn.clicked.connect(self._start_all)
        self.rescan_btn = QPushButton("再スキャン")
        self.rescan_btn.clicked.connect(lambda: self.ctx.ingest.rescan_now())
        self.clear_btn = QPushButton("完了分を一覧から消す")
        self.clear_btn.clicked.connect(self._clear_done)
        self.diag_btn = QPushButton("診断")
        self.diag_btn.setToolTip("Windows が認識している全ボリュームと id.txt の状態を一覧にします")
        self.diag_btn.clicked.connect(self._diagnostics)
        self.repair_btn = QPushButton("カードを修復（chkdsk）…")
        self.repair_btn.setToolTip("壊れたファイルのあったカードや「読めません」のカードを、取り込みが終わってから Windows の chkdsk でまとめて修復します")
        self.repair_btn.clicked.connect(lambda: self.open_repair())
        for w in (self.arm_btn, self.stop_btn, self.start_btn, self.rescan_btn, self.clear_btn, self.diag_btn, self.repair_btn):
            ops.addWidget(w)
        ops.addStretch(1)
        self.summary_label = QLabel("")
        ops.addWidget(self.summary_label)
        root.addLayout(ops)

        # --- 表とログ ---
        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["No.", "ドライブ", "カードID", "ファイル数", "容量", "状態", "進捗", "メッセージ"])
        self.table.setToolTip("見出しをクリックすると並べ替え（No. は読んだ順の通し番号）。行を選んで「取り込みを停止」でそのカードだけ止められます")
        enable_sorting(self.table, 0)
        split.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(2000)
        split.addWidget(self.log)
        split.setSizes([400, 160])
        root.addWidget(split, 1)

        # --- 変換・NAS ---
        nas_box = QGroupBox("裏で続く処理（mp4 への変換、NASへの転送）")
        nv = QVBoxLayout(nas_box)
        cl = QHBoxLayout()
        self.convert_status = QLabel("")
        self.convert_pause = QPushButton("変換を一時停止")
        self.convert_pause.setCheckable(True)
        self.convert_pause.setToolTip("進行中の ffmpeg をすぐ止めます（書きかけは消す）。再開するとそのファイルからやり直します")
        self.convert_pause.toggled.connect(self._convert_pause_toggled)
        self.convert_abandon = QPushButton("変換を取りやめる…")
        self.convert_abandon.setToolTip("残っている変換をやめ、未変換のファイルは元のまま NAS へ送ります")
        self.convert_abandon.clicked.connect(self._convert_abandon)
        cl.addWidget(self.convert_status, 2)
        cl.addWidget(self.convert_pause)
        cl.addWidget(self.convert_abandon)
        self.convert_row = QWidget()
        self.convert_row.setLayout(cl)
        self.convert_row.setVisible(False)
        nv.addWidget(self.convert_row)
        nl = QHBoxLayout()
        nv.addLayout(nl)
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
        ctx.ingest_batch_done.connect(self._on_batch_done)
        ctx.nas_status.connect(self._on_nas_status)
        ctx.nas_progress.connect(self._on_nas_progress)
        ctx.convert_status.connect(self._on_convert_status)
        ctx.settings_changed.connect(self._refresh_labels)
        ctx.settings_changed.connect(self._refresh_arm_button)
        self._refresh_arm_button()
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
        if self.dest_row.text() != s.dest_root:
            self.dest_row.setText(s.dest_root)
        if self.nas_row.text() != s.nas_root:
            self.nas_row.setText(s.nas_root)
        if self.convert_chk.isChecked() != s.convert_enabled:
            self.convert_chk.blockSignals(True)
            self.convert_chk.setChecked(s.convert_enabled)
            self.convert_chk.blockSignals(False)
        if not s.dest_root:
            text, style = "← 未設定。ここで指定してください", "color: #b00;"
        else:
            free = I.free_bytes(s.dest_root)
            if free is not None:
                text, style = f"空き {human_size(free)}", ""
                if s.nas_root and s.staging_cleanup_after_nas:
                    text += "（NASで検証済みの分は自動で消して空けます）"
            else:
                text, style = "見つかりません（取り外し中？）", "color: #b00;"
        self.dest_label.setText(text)
        self.dest_label.setStyleSheet(style)
        self.dest_label.setToolTip("いっぱいになったら別の SSD を繋いでここを変えると、続きからそちらへ取り込みます")
        if self.season_edit.text() != s.season_folder:
            self.season_edit.setText(s.season_folder)

    def _dest_changed(self, path: str) -> None:
        s = self.ctx.settings
        if path and not os.path.isdir(path):
            QMessageBox.warning(self, "保存先", f"フォルダが見つかりません:\n{path}\n\n接続を確認するか、「参照…」で選び直してください。")
        if path != s.dest_root:
            s.dest_root = path
            self.ctx.save()
            self._append_log(f"保存先を {path or '（未設定）'} にしました")
        self._ensure_season_dir()
        self._refresh_labels()

    def _nas_changed(self, path: str) -> None:
        s = self.ctx.settings
        if path and not os.path.isdir(path):
            QMessageBox.warning(self, "NAS", f"フォルダが見つかりません:\n{path}\n\nNAS に接続されているか確認してください（取り込みは続けられます。転送は接続されてから始まります）。")
        if path != s.nas_root:
            s.nas_root = path
            self.ctx.save()
            self._append_log(f"NAS の最終保存先を {path or '（なし）'} にしました")
        self._refresh_labels()

    def _convert_toggled(self, on: bool) -> None:
        s = self.ctx.settings
        if on != s.convert_enabled:
            s.convert_enabled = on
            self.ctx.save()
            self._append_log("動画の変換を" + ("オンにしました（中継の上で mp4 にしてから NAS へ送ります）" if on else "オフにしました"))

    def _season_changed(self) -> None:
        v = self.season_edit.text().strip()
        if v != self.ctx.settings.season_folder:
            self.ctx.settings.season_folder = v
            self.ctx.save()
        self._ensure_season_dir()

    def _ensure_season_dir(self) -> None:
        """保存先に区切りのフォルダを先に作っておく（名前変更画面などから見えるように）。"""
        s = self.ctx.settings
        if s.dest_root and s.season_folder.strip() and os.path.isdir(s.dest_root):
            try:
                os.makedirs(os.path.join(s.dest_root, s.season_folder), exist_ok=True)
            except OSError as e:
                self._append_log(f"取り込み先のフォルダを作成できません: {e}")

    def _arm_clicked(self) -> None:
        ing = self.ctx.ingest
        if ing.armed:
            ing.disarm()
            self._refresh_arm_button()
            self._append_log("新しいカードの自動開始を止めました（進行中のコピーは続きます）")
            return
        s = self.ctx.settings
        if not s.dest_root:
            QMessageBox.warning(self, "取り込みを開始", "保存先が未設定です。この画面の上の「保存先（中継）」で指定してください。")
            self.dest_row.edit.setFocus()
            return
        if not os.path.isdir(s.dest_root):
            QMessageBox.warning(self, "取り込みを開始", f"保存先が見つかりません:\n{s.dest_root}\n\n接続を確認するか、別の場所を指定してください。")
            return
        free = I.free_bytes(s.dest_root) if s.dest_root else None
        waiting = [j for j in ing.jobs.values() if j.state == I.S_DETECTED and not j.card.problems]
        total = sum(j.card.total_bytes for j in waiting)
        lines = [f"保存先（中継）: {s.dest_root}" + (f"　空き {human_size(free)}" if free is not None else "　（見つかりません）"),
                 f"取り込み先のフォルダ: {s.season_folder.strip() or '（保存先の直下）'}",
                 f"NAS: {s.nas_root or '（転送しない）'}",
                 f"動画の変換: {'する（' + ', '.join(s.convert_extensions) + ' → mp4）' if s.convert_enabled else 'しない'}",
                 "",
                 f"いま開始待ちのカード: {len(waiting)} 枚（{human_size(total)}）。"
                 "開始すると、これ以後に挿したカードも自動で始まります。\n\nこの内容で始めますか？"]
        if QMessageBox.question(self, "取り込みを開始", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        self._ensure_season_dir()
        n = ing.arm()
        self._refresh_arm_button()
        self._append_log(f"取り込みを開始（{n} 枚）。以後、挿したカードは自動で始まります")

    def _refresh_arm_button(self) -> None:
        armed = self.ctx.ingest.armed
        self.arm_btn.setText("自動開始を止める" if armed else "取り込みを開始")
        self.arm_btn.setStyleSheet("" if armed else "font-weight: bold;")

    def _nas_pause_toggled(self, on: bool) -> None:
        self.ctx.nas.paused = on
        self.ctx.nas.wake()
        self.nas_pause.setText("NAS転送を再開" if on else "NAS転送を一時停止")
        self._append_log("NAS転送を一時停止しました（送っている途中のファイルは止めて、再開時にやり直します）" if on else "NAS転送を再開しました")

    def _start_all(self) -> None:
        for key, job in list(self.ctx.ingest.jobs.items()):
            if job.state in (I.S_DETECTED, I.S_ERROR, I.S_SKIPPED, I.S_STOPPED) and not job.card.problems:
                self.ctx.ingest.enqueue(key)

    def _selected_keys(self) -> list[str]:
        rows = {i.row() for i in self.table.selectedIndexes()}
        keys = []
        for r in rows:
            it = self.table.item(r, 0)
            key = it.data(Qt.ItemDataRole.UserRole) if it else None
            if key:
                keys.append(key)
        return keys

    def _select_keys(self, keys: set[str]) -> None:
        if not keys:
            return
        self.table.clearSelection()
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it and it.data(Qt.ItemDataRole.UserRole) in keys:
                self.table.selectRow(r)

    def _stop_clicked(self) -> None:
        ing = self.ctx.ingest
        busy = (I.S_QUEUED, I.S_WAITING, I.S_COPYING, I.S_VERIFYING)
        selected = [k for k in self._selected_keys() if k in ing.jobs and ing.jobs[k].state in busy]
        if selected:
            keys, what = selected, f"選んだ {len(selected)} 枚"
        else:
            keys = [k for k, j in ing.jobs.items() if j.state in busy]
            what = f"進行中・待機中の全部（{len(keys)} 枚）"
        if not keys:
            QMessageBox.information(self, "取り込みを停止", "止めるカードがありません（コピー中・待機中のカードが無い）。")
            return
        msg = (f"{what}のコピーを止めます。書きかけのファイルは消し、検証が済んだファイルは残ります。\n"
               "止めたカードは「エラー・停止・スキップ分をやり直す」で続きから再開できます。\n")
        if not selected:
            msg += "以後に挿したカードの自動開始も止めます。\n"
        msg += "\n止めますか？"
        if QMessageBox.question(self, "取り込みを停止", msg) != QMessageBox.StandardButton.Yes:
            return
        if not selected:
            ing.disarm()
            self._refresh_arm_button()
        n = ing.cancel_jobs(keys)
        self._append_log(f"取り込みを停止しました（{n} 枚）")

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
        from ...core.repair import notification_senders
        senders = notification_senders()
        if senders:
            text += "\n\nWindows の通知の送信元（設定 > 通知 に出るもの。Enabled=0 はオフ）:\n" + "\n".join(
                f"  {name}: {'オフ' if en == 0 else ('オン' if en == 1 else '既定')}" for name, en in senders)
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
        # 読めないファイルのあったカードと Windows が修復を勧めるカードは、いまのうちに修復を勧める（データは取り込み済みなので安全）
        broken = [j for j in self.ctx.ingest.jobs.values()
                  if j.state == I.S_DONE and (j.n_broken or j.card.needs_repair) and not j.repair_offered]
        if broken and self.ctx.settings.repair_after_ingest and sys.platform == "win32":
            for j in broken:
                j.repair_offered = True
            ids = ", ".join(j.card.card_id for j in broken[:10]) + ("…" if len(broken) > 10 else "")
            msg = (f"{len(broken)} 枚のカードは修復が必要なようです（壊れたファイル、または Windows の警告）: {ids}\n"
                   "データは取り込み済みです。いまこれらのカードを Windows の chkdsk で修復しますか？\n"
                   "（管理者の許可を求める画面が 1 回出ます。あとで「カードを修復（chkdsk）…」からもできます）")
            if QMessageBox.question(self, "カードの修復", msg) == QMessageBox.StandardButton.Yes:
                self.open_repair({j.card.volume.key for j in broken})

    def open_repair(self, preselect: set[str] | None = None) -> None:
        from ..repair_dialog import RepairDialog
        if self.ctx.ingest.active_count():
            QMessageBox.information(self, "カードを修復", "コピー中のカードがあります。取り込みが終わってから修復してください（終わったカードだけ選べます）。")
        RepairDialog(self.ctx, self, preselect=preselect).exec()

    def _on_convert_status(self, msg: str) -> None:
        self.convert_status.setText(msg)
        self.convert_row.setVisible(bool(msg))

    def _convert_pause_toggled(self, on: bool) -> None:
        self.ctx.converter.pause(on)
        self.convert_pause.setText("変換を再開" if on else "変換を一時停止")
        self._append_log("mp4 への変換を一時停止しました（進行中のファイルは止めて、再開時にやり直します）" if on else "mp4 への変換を再開しました")

    def _convert_abandon(self) -> None:
        n = self.ctx.converter.pending_count()
        msg = (f"残っている変換（{n} 枚のカード）を取りやめます。進行中の ffmpeg は止め、未変換のファイルは元のまま NAS へ送ります。\n"
               "今後のカードも変換しないなら、上の「動画の変換」のチェックも外してください。\n\n取りやめますか？")
        if QMessageBox.question(self, "変換を取りやめる", msg) != QMessageBox.StandardButton.Yes:
            return
        self.ctx.converter.abandon()
        if self.convert_pause.isChecked():
            self.convert_pause.blockSignals(True)
            self.convert_pause.setChecked(False)
            self.convert_pause.setText("変換を一時停止")
            self.convert_pause.blockSignals(False)
        self._append_log(f"mp4 への変換を取りやめました（{n} 枚分）。未変換のファイルは元のまま NAS へ送ります")

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
        selected = set(self._selected_keys())
        jobs = sorted(self.ctx.ingest.jobs.values(), key=lambda j: j.seq)
        others = sorted(self.ctx.ingest.others.values(), key=lambda o: volume_sort_key(o[0]))
        n_done = n_active = n_err = n_stopped = 0
        with refill_sorted(self.table):
            self._fill_rows(jobs, others)
            for job in jobs:
                st = job.state
                if st == I.S_DONE:
                    n_done += 1
                elif st in (I.S_QUEUED, I.S_WAITING, I.S_COPYING, I.S_VERIFYING):
                    n_active += 1
                elif st == I.S_ERROR:
                    n_err += 1
                elif st == I.S_STOPPED:
                    n_stopped += 1
        self._select_keys(selected)
        n_noid = sum(1 for v, _r in others if v.kind != "unreadable")
        n_unread = sum(1 for v, _r in others if v.kind == "unreadable")
        extra = ""
        if n_noid:
            extra += f" / IDなし {n_noid}"
        if n_unread:
            extra += f" / 読めません {n_unread}"
        if n_stopped:
            extra += f" / 停止 {n_stopped}"
        self.summary_label.setText(f"検出 {len(jobs)} 枚 / 処理中 {n_active} / 完了 {n_done} / エラー {n_err}{extra}"
                                   f"　（この起動で読んだ通算 {self.ctx.ingest.seq_count} 枚）")

    def _fill_rows(self, jobs, others) -> None:
        self.table.setRowCount(len(jobs) + len(others))
        for r, job in enumerate(jobs):
            st = job.state
            color = {I.S_DONE: COLORS["ok"], I.S_ERROR: COLORS["err"], I.S_SKIPPED: COLORS["muted"],
                     I.S_COPYING: COLORS["info"], I.S_VERIFYING: COLORS["info"], I.S_REMOVED: COLORS["warn"],
                     I.S_WAITING: COLORS["warn"], I.S_STOPPED: COLORS["warn"]}.get(st)
            prog = f"{job.progress*100:.0f}%" if st in (I.S_COPYING, I.S_VERIFYING) else ("100%" if st == I.S_DONE else "")
            spd = job.speed_mbps
            if prog and spd:
                prog += f" ({spd:.0f} MB/s)"
            set_row(self.table, r, [str(job.seq), job.card.volume.display, job.card.card_id, str(job.card.n_files),
                                    human_size(job.card.total_bytes), I.STATE_LABELS.get(st, st), prog, job.message],
                    color)
            self.table.item(r, 0).setData(Qt.ItemDataRole.UserRole, job.key)
        for i, (v, reason) in enumerate(others):
            r = len(jobs) + i
            if v.kind == "unreadable":
                state, color = "読めません", COLORS["err"]
            else:
                state, color = "IDなし", COLORS["warn"]
                reason = reason + "。左の「カードにIDを付ける」で番号を付けてください"
            set_row(self.table, r, ["", v.display, "", "", "", state, "", f"{v.label}: {reason}"], color)
