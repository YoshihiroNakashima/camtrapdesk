"""保存済みの動画を mp4 に変換: NAS や中継にすでにあるフォルダを指定し、中の AVI などを後から一括で mp4 にして元を消す。"""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter, QVBoxLayout, QWidget)

from ...core import batch_convert as B
from ..context import AppContext
from ..widgets import COLORS, PathRow, human_size, make_table, set_row


class BatchConvertPage(QWidget):
    _scan_done = Signal(object)        # ScanResult（走査スレッドから）
    _scan_progress = Signal(int, int)  # 見たフォルダ数, 見つけた対象数

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.plan: B.ScanResult | None = None
        self._scan_thread: threading.Thread | None = None
        self._scan_stop = threading.Event()
        s = ctx.settings

        root = QVBoxLayout(self)
        info = QLabel("フォルダを指定して、その中（サブフォルダも含む）の AVI などを H.264 の mp4 に変換します。NAS や中継の保存先に限らず、"
                      "PC のどのフォルダでも使えます。保存先は「同じ場所に置き換える（元の AVI を消して容量を減らす）」か、"
                      "「別のフォルダに保存する（フォルダ構造を保って mp4 を書き、元はそのまま残す）」を選べます。"
                      "mp4 はそのまま、取り込み・NAS 転送が途中のフォルダは対象外です。")
        info.setWordWrap(True)
        root.addWidget(info)

        box = QGroupBox("対象と設定")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        h1 = QHBoxLayout()
        self.path_row = PathRow(s.nas_root or s.dest_root, "dir", placeholder=r"例: \\DS2422\camtrap\N260710 や Z:\BOSO\MOVIE\PRM_N")
        self.nas_btn = QPushButton("NASの保存先")
        self.nas_btn.setToolTip("設定の「NASの最終保存先」を入れます")
        self.nas_btn.clicked.connect(lambda: self.path_row.setText(self.ctx.settings.nas_root))
        self.dest_btn = QPushButton("中継先")
        self.dest_btn.setToolTip("設定の「中継先（または最終保存先）」を入れます")
        self.dest_btn.clicked.connect(lambda: self.path_row.setText(self.ctx.settings.dest_root))
        h1.addWidget(self.path_row, 1)
        h1.addWidget(self.nas_btn)
        h1.addWidget(self.dest_btn)
        form.addRow("変換するフォルダ", h1)

        out_box = QVBoxLayout()
        self.inplace_radio = QRadioButton("同じ場所に置き換える（変換した mp4 を同じフォルダに置き、元の AVI を消す。不可逆）")
        self.out_radio = QRadioButton("別のフォルダに保存する（元の AVI はそのまま残す）")
        self.inplace_radio.setChecked(s.batch_convert_in_place)
        self.out_radio.setChecked(not s.batch_convert_in_place)
        self.out_row = PathRow(s.batch_convert_out_root, "dir", placeholder=r"例: D:\converted（変換するフォルダからの相対パスを保って mp4 を書きます）")
        self.out_row.setEnabled(not s.batch_convert_in_place)
        self.out_radio.toggled.connect(self.out_row.setEnabled)
        out_box.addWidget(self.inplace_radio)
        h_out = QHBoxLayout()
        h_out.addWidget(self.out_radio)
        h_out.addWidget(self.out_row, 1)
        out_box.addLayout(h_out)
        form.addRow("保存先", out_box)

        h2 = QHBoxLayout()
        self.ext_edit = QLineEdit(", ".join(s.convert_extensions))
        self.ext_edit.setMaximumWidth(160)
        self.ext_edit.setToolTip("変換する拡張子（カンマ区切り）。mp4 は対象にしません")
        self.crf = QSpinBox()
        self.crf.setRange(15, 35)
        self.crf.setValue(int(s.convert_crf))
        self.crf.setToolTip("小さいほど高画質で大きいファイル。23 が標準（MJPEG の AVI なら 10分の1 程度）、26 でさらに半分")
        self.parallel = QSpinBox()
        self.parallel.setRange(1, 8)
        self.parallel.setValue(int(s.convert_parallel))
        self.parallel.setToolTip("同時に変換する本数。GPU があれば 2〜4、CPU だけなら 1〜2 が目安。NAS 上で変換するときはネットワークの速さも効きます")
        self.encoder_label = QLabel("")
        h2.addWidget(QLabel("拡張子"))
        h2.addWidget(self.ext_edit)
        h2.addWidget(QLabel("画質（CRF）"))
        h2.addWidget(self.crf)
        h2.addWidget(QLabel("同時に変換する本数"))
        h2.addWidget(self.parallel)
        h2.addWidget(self.encoder_label, 1)
        form.addRow("変換の設定", h2)
        root.addWidget(box)

        ops = QHBoxLayout()
        self.scan_btn = QPushButton("対象を調べる")
        self.scan_btn.setToolTip("フォルダ以下を走査して、変換するファイルと容量を一覧にします（まだ何も変えません）")
        self.scan_btn.clicked.connect(self.scan)
        self.start_btn = QPushButton("変換を開始")
        self.start_btn.setEnabled(False)
        self.start_btn.clicked.connect(self.start)
        self.pause_btn = QPushButton("一時停止")
        self.pause_btn.setCheckable(True)
        self.pause_btn.setEnabled(False)
        self.pause_btn.toggled.connect(self._pause_toggled)
        self.stop_btn = QPushButton("中止")
        self.stop_btn.setEnabled(False)
        self.stop_btn.setToolTip("変換中のファイルを止めて終わります（止めたファイルは元のまま残り、次回また対象になります）")
        self.stop_btn.clicked.connect(self.stop)
        for w in (self.scan_btn, self.start_btn, self.pause_btn, self.stop_btn):
            ops.addWidget(w)
        ops.addStretch(1)
        self.summary_label = QLabel("")
        ops.addWidget(self.summary_label)
        root.addLayout(ops)

        pl = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.bar = QProgressBar()
        self.bar.setRange(0, 1000)
        self.bar.setTextVisible(True)
        pl.addWidget(self.status_label, 2)
        pl.addWidget(self.bar, 1)
        root.addLayout(pl)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["フォルダ", "対象", "容量", "変換済み", "失敗", "備考"])
        split.addWidget(self.table)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        split.addWidget(self.log)
        split.setSizes([380, 160])
        root.addWidget(split, 1)

        self._scan_done.connect(self._on_scan_done)
        self._scan_progress.connect(self._on_scan_progress)
        ctx.batch_status.connect(self.status_label.setText)
        ctx.batch_log.connect(self._append_log)
        ctx.batch_done.connect(self._on_done)
        ctx.settings_changed.connect(self._refresh_encoder_label)
        self._refresh_encoder_label()
        self.timer = QTimer(self)
        self.timer.setInterval(1000)
        self.timer.timeout.connect(self._refresh_progress)
        self.timer.start()

    # ------------------------------------------------------------------
    def _append_log(self, msg: str) -> None:
        self.log.appendPlainText(time.strftime("%H:%M:%S ") + msg)

    def _refresh_encoder_label(self) -> None:
        s = self.ctx.settings
        enc = {"auto": "自動", "libx264": "CPU（libx264）", "h264_nvenc": "NVIDIA", "h264_qsv": "Intel 内蔵",
               "h264_amf": "AMD", "h264_videotoolbox": "Mac"}.get(s.convert_encoder, s.convert_encoder)
        self.encoder_label.setText(f"エンコーダ: {enc}（「設定」の動画の変換で変更）")

    def _settings_for_scan(self):
        """拡張子だけこの画面の値で上書きした設定（走査用）。"""
        import dataclasses
        exts = [e.strip().lstrip(".").lower() for e in self.ext_edit.text().split(",") if e.strip()]
        exts = [e for e in exts if e != "mp4"] or ["avi"]
        return dataclasses.replace(self.ctx.settings, convert_extensions=exts)

    # --------------------------------------------------------------- 走査
    def scan(self) -> None:
        if self.ctx.batch.running:
            QMessageBox.information(self, "変換中", "変換が終わるか中止してから、もう一度調べてください。")
            return
        root = self.path_row.text()
        if not root or not os.path.isdir(root):
            QMessageBox.warning(self, "フォルダ", "変換するフォルダが見つかりません。NAS に接続できているか確認してください。")
            return
        if self._scan_thread and self._scan_thread.is_alive():
            return
        s = self._settings_for_scan()
        from ...core.ffmpeg import find_ffmpeg
        if not find_ffmpeg(s.ffmpeg_path):
            QMessageBox.warning(self, "ffmpeg", "ffmpeg が見つかりません。「設定」の動画の変換で場所を指定してください。")
            return
        out_root = "" if self.inplace_radio.isChecked() else self.out_row.text()
        if not self.inplace_radio.isChecked() and not out_root:
            QMessageBox.warning(self, "保存先", "別のフォルダに保存する場合は、保存先のフォルダを指定してください。")
            return
        st = self.ctx.settings
        if st.batch_convert_in_place != self.inplace_radio.isChecked() or st.batch_convert_out_root != self.out_row.text():
            st.batch_convert_in_place = self.inplace_radio.isChecked()
            st.batch_convert_out_root = self.out_row.text()
            self.ctx.save()
        self.plan = None
        self.start_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.table.setRowCount(0)
        self.summary_label.setText("")
        self.bar.setValue(0)
        self.bar.setFormat("")
        self.status_label.setText(f"{root} を調べています…")
        self._append_log(f"走査を開始: {root}（拡張子: {', '.join(s.convert_extensions)}）" + (f" → 保存先 {out_root}" if out_root else "（同じ場所に置き換え）"))
        self._scan_stop.clear()

        def work() -> None:
            try:
                res = B.scan(root, s, self.ctx.ledger, stop=self._scan_stop, on_progress=self._scan_progress.emit,
                             out_root=out_root)
            except Exception as e:  # pragma: no cover
                res = B.ScanResult(root=root, issues=[f"走査でエラー: {e}"])
            self._scan_done.emit(res)

        self._scan_thread = threading.Thread(target=work, name="batch-scan", daemon=True)
        self._scan_thread.start()

    def _on_scan_progress(self, n_dirs: int, n_found: int) -> None:
        self.status_label.setText(f"調べています… フォルダ {n_dirs} 個、対象 {n_found} ファイル")

    def _on_scan_done(self, res: B.ScanResult) -> None:
        self.plan = res
        self.scan_btn.setEnabled(True)
        self._show_plan(res)
        for i in res.issues:
            self._append_log("注意: " + i)
        if res.n_parts_removed:
            self._append_log(f"前回の途中のファイル（.part）を {res.n_parts_removed} 個消しました")
        n_skip_dirs = sum(1 for f in res.folders if f.skipped_reason)
        msg = f"対象 {res.n_files} ファイル（{human_size(res.total_bytes)}）、{len(res.folders) - n_skip_dirs} フォルダ"
        if n_skip_dirs:
            msg += f"。対象外のフォルダ {n_skip_dirs}"
        if res.n_skipped_files:
            msg += f"、対象外のファイル {res.n_skipped_files}"
        self.status_label.setText(msg + "。「変換を開始」で始めます" if res.n_files else msg + "。変換するものはありません")
        self._append_log(msg)
        self.start_btn.setEnabled(res.n_files > 0)

    def _show_plan(self, res: B.ScanResult) -> None:
        root = res.root
        self.table.setRowCount(len(res.folders))
        for r, fp in enumerate(res.folders):
            rel = os.path.relpath(fp.dir, root) if fp.dir != root else "."
            notes = []
            if fp.skipped_reason:
                notes.append("対象外: " + fp.skipped_reason)
            if fp.out_dir:
                notes.append("→ " + fp.out_dir)
            if fp.has_manifest:
                notes.append("manifest あり" + ("・台帳も更新" if fp.ledger_authoritative else ""))
            n_finish = sum(1 for t in fp.targets if t.action == B.A_FINISH)
            if n_finish:
                notes.append(f"変換済みの mp4 が既にあるもの {n_finish}（元を消すだけ）")
            skipped = [t for t in fp.targets if t.action == B.A_SKIP]
            if skipped:
                notes.append(f"対象外 {len(skipped)}: " + "; ".join(f"{t.name}（{t.reason}）" for t in skipped[:3]) + ("…" if len(skipped) > 3 else ""))
            if fp.n_other_media:
                notes.append(f"mp4 など {fp.n_other_media}（そのまま）")
            color = COLORS["muted"] if fp.skipped_reason else (COLORS["warn"] if skipped else None)
            if fp.n_failed:
                color = COLORS["err"]
            elif fp.n_done and fp.n_done >= fp.n_targets:
                color = COLORS["ok"]
            set_row(self.table, r, [rel, str(fp.n_targets), human_size(fp.total_bytes), str(fp.n_done) if fp.n_done else "",
                                    str(fp.n_failed) if fp.n_failed else "", "; ".join(notes)], color)

    # --------------------------------------------------------------- 実行
    def start(self) -> None:
        plan = self.plan
        if not plan or not plan.n_files:
            return
        if self.ctx.batch.running:
            return
        if self.ctx.converter.busy():
            QMessageBox.information(self, "変換中", "取り込み時の変換が動いています。終わってから始めてください。")
            return
        if plan.in_place:
            how = "変換した mp4 を同じフォルダに置き、元のファイルは消します。この操作は元に戻せません。"
        else:
            how = f"変換した mp4 を {plan.out_root} の下に（フォルダ構造を保って）書きます。元のファイルはそのまま残します。"
        lines = [f"フォルダ: {plan.root}",
                 f"対象: {plan.n_files} ファイル（{human_size(plan.total_bytes)}）",
                 f"画質 CRF {int(self.crf.value())}、同時 {int(self.parallel.value())} 本",
                 "",
                 how,
                 "MJPEG の AVI なら容量は 10 分の 1 程度になり、見た目はほぼ変わりません。",
                 "", "始めますか？"]
        if QMessageBox.question(self, "変換の実行", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        ok = self.ctx.batch.start(plan, parallel=int(self.parallel.value()), crf=int(self.crf.value()))
        if not ok:
            return
        self.start_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.pause_btn.setEnabled(True)
        self.stop_btn.setEnabled(True)
        self._append_log(f"変換を開始: {plan.n_files} ファイル（{human_size(plan.total_bytes)}）")

    def _pause_toggled(self, on: bool) -> None:
        self.ctx.batch.paused = on
        self.pause_btn.setText("再開" if on else "一時停止")

    def stop(self) -> None:
        if not self.ctx.batch.running:
            return
        if QMessageBox.question(self, "中止", "変換を中止しますか？ 変換中のファイルは元のまま残ります（変換が終わった分はそのままです）。") \
                != QMessageBox.StandardButton.Yes:
            return
        self.ctx.batch.stop()
        self.pause_btn.setChecked(False)
        self.stop_btn.setEnabled(False)

    def _on_done(self, stats: B.BatchStats) -> None:
        self.pause_btn.setChecked(False)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.scan_btn.setEnabled(True)
        self._refresh_progress()
        if self.plan:
            self._show_plan(self.plan)
        win = self.window()
        if hasattr(win, "notify"):
            win.notify("mp4 への変換", stats.message)
        # 続きがあれば（中止・失敗）もう一度調べると残りが対象になる
        self.start_btn.setEnabled(False)

    def _refresh_progress(self) -> None:
        b = self.ctx.batch
        if not b.plan:
            return
        st = b.snapshot()
        if not st.started_at:
            return
        self.bar.setValue(int(st.frac * 1000))
        self.bar.setFormat(f"{st.n_done}/{st.n_total} ファイル {st.frac*100:.0f}%")
        if b.running and self.plan is b.plan:
            self._show_plan(self.plan)
        in_place = not (b.plan and b.plan.out_root)
        saved = (f"空いた容量 {human_size(st.saved_bytes)}" if in_place else f"出力 {human_size(st.bytes_out_done)}") if st.bytes_in_done else ""
        el = f"経過 {st.elapsed/60:.0f} 分" if st.elapsed >= 60 else ""
        self.summary_label.setText("　".join(x for x in (saved, el) if x))
