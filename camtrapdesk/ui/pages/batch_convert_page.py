"""保存済みの動画を mp4 に変換: NAS や中継にすでにあるフォルダを指定し、中の AVI などを後から一括で mp4 にして元を消す。"""

from __future__ import annotations

import os
import threading
import time

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QRadioButton, QSpinBox, QSplitter,
                               QVBoxLayout, QWidget)

from ...core import batch_convert as B
from ..context import AppContext
from ..widgets import COLORS, PathRow, human_size, make_table, set_row


class BatchConvertPage(QWidget):
    _scan_done = Signal(object)        # list[ScanResult]（走査スレッドから）
    _scan_progress = Signal(int, int)  # 見たフォルダ数, 見つけた対象数

    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.plan: B.ScanResult | None = None          # いま変換中（または次に始める）のフォルダの計画
        self.plans: list[B.ScanResult] = []            # 走査した全フォルダの計画（並べた順）
        self._queue: list[B.ScanResult] = []           # まだ変換していないフォルダ
        self._run_totals = {"done": 0, "failed": 0, "in": 0, "out": 0, "folders": 0}
        self._scan_thread: threading.Thread | None = None
        self._scan_stop = threading.Event()
        s = ctx.settings

        root = QVBoxLayout(self)
        info = QLabel("フォルダを指定して、その中（サブフォルダも含む）の AVI などを H.264 の mp4 に変換します。フォルダは複数並べられ、"
                      "上から順に、一つが終わったら次へ進みます。NAS や中継の保存先に限らず PC のどのフォルダでも使えます。"
                      "保存先は「同じ場所に置き換える（元の AVI を消して容量を減らす）」か、「別のフォルダに保存する（フォルダ構造を保って mp4 を書き、"
                      "元はそのまま残す）」を選べます。mp4 はそのまま、取り込み・NAS 転送が途中のフォルダは対象外です。")
        info.setWordWrap(True)
        root.addWidget(info)

        box = QGroupBox("対象と設定")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        fbox = QVBoxLayout()
        h1 = QHBoxLayout()
        self.path_row = PathRow("", "dir", placeholder=r"例: \\DS2422\camtrap\N260710 や Z:\BOSO\MOVIE\PRM_N（入れて「追加」。参照で選ぶとそのまま追加）")
        self.path_row.changed.connect(lambda _p: self._add_folder())
        self.add_btn = QPushButton("追加")
        self.add_btn.clicked.connect(lambda: self._add_folder())
        self.nas_btn = QPushButton("NASの保存先を追加")
        self.nas_btn.setToolTip("設定の「NASの最終保存先」を一覧に足します")
        self.nas_btn.clicked.connect(lambda: self._add_folder(self.ctx.settings.nas_root))
        self.dest_btn = QPushButton("中継先を追加")
        self.dest_btn.setToolTip("設定の「中継先（または最終保存先）」を一覧に足します")
        self.dest_btn.clicked.connect(lambda: self._add_folder(self.ctx.settings.dest_root))
        h1.addWidget(self.path_row, 1)
        h1.addWidget(self.add_btn)
        h1.addWidget(self.nas_btn)
        h1.addWidget(self.dest_btn)
        fbox.addLayout(h1)
        h1b = QHBoxLayout()
        self.folder_list = QListWidget()
        self.folder_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.folder_list.setMaximumHeight(110)
        self.folder_list.setToolTip("この順に変換します。複数選んで「外す」、「上へ」「下へ」で順番を変えられます")
        for f in s.batch_convert_roots:
            self.folder_list.addItem(f)
        side = QVBoxLayout()
        self.rm_btn = QPushButton("外す")
        self.rm_btn.clicked.connect(self._remove_folders)
        self.up_btn = QPushButton("上へ")
        self.up_btn.clicked.connect(lambda: self._move_folder(-1))
        self.down_btn = QPushButton("下へ")
        self.down_btn.clicked.connect(lambda: self._move_folder(1))
        self.clear_btn = QPushButton("全部外す")
        self.clear_btn.clicked.connect(self._clear_folders)
        for w in (self.rm_btn, self.up_btn, self.down_btn, self.clear_btn):
            side.addWidget(w)
        side.addStretch(1)
        h1b.addWidget(self.folder_list, 1)
        h1b.addLayout(side)
        fbox.addLayout(h1b)
        form.addRow("変換するフォルダ（上から順）", fbox)

        out_box = QVBoxLayout()
        self.inplace_radio = QRadioButton("同じ場所に置き換える（変換した mp4 を同じフォルダに置き、元の AVI を消す。不可逆）")
        self.out_radio = QRadioButton("別のフォルダに保存する（元の AVI はそのまま残す）")
        self.inplace_radio.setChecked(s.batch_convert_in_place)
        self.out_radio.setChecked(not s.batch_convert_in_place)
        self.out_row = PathRow(s.batch_convert_out_root, "dir", placeholder=r"例: D:\converted（変換するフォルダからの相対パスを保って mp4 を書きます。フォルダが複数なら、その名前のサブフォルダに分けます）")
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
        self.parallel.setValue(int(s.batch_convert_parallel))
        self.parallel.setToolTip("この画面の一括変換で同時に変換する本数（取り込み時の変換の本数は「設定」で別に決めます）。"
                                 "他に何も動いていなければ、GPU があれば 4、CPU だけなら 2〜3 が目安。NAS 上で変換するときはネットワークの速さも効きます")
        self.parallel.valueChanged.connect(self._parallel_changed)
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
        self.diag_btn = QPushButton("1 本試して診断…")
        self.diag_btn.setToolTip("動画を 1 本選び、読み出し・デコード・色変換・エンコードにそれぞれ何秒かかるかを測って、遅い原因の見当を付けます")
        self.diag_btn.clicked.connect(self.diagnose)
        for w in (self.scan_btn, self.start_btn, self.pause_btn, self.stop_btn, self.diag_btn):
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
        ctx.batch_status.connect(self._set_status)
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

    # --------------------------------------------------------- フォルダの一覧
    def folders(self) -> list[str]:
        return [self.folder_list.item(i).text() for i in range(self.folder_list.count())]

    def _save_folders(self) -> None:
        st = self.ctx.settings
        if st.batch_convert_roots != self.folders():
            st.batch_convert_roots = self.folders()
            self.ctx.save()

    def _add_folder(self, path: str | None = None) -> None:
        p = (path if isinstance(path, str) else self.path_row.text()).strip()
        if not p:
            return
        key = os.path.normcase(os.path.abspath(p))
        if any(os.path.normcase(os.path.abspath(f)) == key for f in self.folders()):
            self.path_row.setText("")
            return
        if not os.path.isdir(p):
            QMessageBox.warning(self, "フォルダ", f"フォルダが見つかりません:\n{p}\n\nNAS に接続できているか確認してください。")
            return
        self.folder_list.addItem(p)
        self.path_row.setText("")
        self._save_folders()

    def _remove_folders(self) -> None:
        for it in self.folder_list.selectedItems():
            self.folder_list.takeItem(self.folder_list.row(it))
        self._save_folders()

    def _clear_folders(self) -> None:
        self.folder_list.clear()
        self._save_folders()

    def _move_folder(self, delta: int) -> None:
        rows = sorted(self.folder_list.row(it) for it in self.folder_list.selectedItems())
        if not rows:
            return
        if delta > 0:
            rows.reverse()
        for r in rows:
            nr = r + delta
            if nr < 0 or nr >= self.folder_list.count():
                return
            it = self.folder_list.takeItem(r)
            self.folder_list.insertItem(nr, it)
            it.setSelected(True)
        self._save_folders()

    # --------------------------------------------------------------- 走査
    def scan(self) -> None:
        if self.ctx.batch.running:
            QMessageBox.information(self, "変換中", "変換が終わるか中止してから、もう一度調べてください。")
            return
        if self.path_row.text():
            self._add_folder()
        roots = self.folders()
        if not roots:
            QMessageBox.warning(self, "フォルダ", "変換するフォルダを一覧に追加してください。")
            self.path_row.edit.setFocus()
            return
        missing = [r for r in roots if not os.path.isdir(r)]
        if missing:
            QMessageBox.warning(self, "フォルダ", "見つからないフォルダがあります（NAS に接続できているか確認してください）:\n" + "\n".join(missing))
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
        if st.batch_convert_parallel != int(self.parallel.value()):
            st.batch_convert_parallel = int(self.parallel.value())
            self.ctx.save()
        if st.batch_convert_in_place != self.inplace_radio.isChecked() or st.batch_convert_out_root != self.out_row.text():
            st.batch_convert_in_place = self.inplace_radio.isChecked()
            st.batch_convert_out_root = self.out_row.text()
            self.ctx.save()
        self.plan = None
        self.plans = []
        self._queue = []
        self.start_btn.setEnabled(False)
        self.scan_btn.setEnabled(False)
        self.table.setRowCount(0)
        self.summary_label.setText("")
        self.bar.setValue(0)
        self.bar.setFormat("")
        self.status_label.setText(f"{roots[0]} を調べています…")
        self._append_log(f"走査を開始: {len(roots)} フォルダ（拡張子: {', '.join(s.convert_extensions)}）"
                         + (f" → 保存先 {out_root}" if out_root else "（同じ場所に置き換え）"))
        self._scan_stop.clear()

        def out_for(root: str) -> str:
            if not out_root:
                return ""
            if len(roots) == 1:
                return out_root
            return os.path.join(out_root, os.path.basename(os.path.normpath(root)) or "root")   # フォルダごとに分けて衝突を避ける

        def work() -> None:
            results: list[B.ScanResult] = []
            for root in roots:
                if self._scan_stop.is_set():
                    break
                try:
                    res = B.scan(root, s, self.ctx.ledger, stop=self._scan_stop, on_progress=self._scan_progress.emit,
                                 out_root=out_for(root))
                except Exception as e:  # pragma: no cover
                    res = B.ScanResult(root=root, issues=[f"走査でエラー: {e}"])
                results.append(res)
            self._scan_done.emit(results)

        self._scan_thread = threading.Thread(target=work, name="batch-scan", daemon=True)
        self._scan_thread.start()

    def _on_scan_progress(self, n_dirs: int, n_found: int) -> None:
        self.status_label.setText(f"調べています… フォルダ {n_dirs} 個、対象 {n_found} ファイル")

    def _on_scan_done(self, results: list) -> None:
        self.plans = list(results)
        self.plan = self.plans[0] if self.plans else None
        self.scan_btn.setEnabled(True)
        self._show_plans()
        n_files = n_bytes = n_dirs = n_skip_dirs = n_skip_files = 0
        for res in self.plans:
            for i in res.issues:
                self._append_log(f"注意（{os.path.basename(os.path.normpath(res.root))}）: " + i)
            if res.n_parts_removed:
                self._append_log(f"{res.root}: 前回の途中のファイル（.part）を {res.n_parts_removed} 個消しました")
            n_files += res.n_files
            n_bytes += res.total_bytes
            n_skip_dirs += sum(1 for f in res.folders if f.skipped_reason)
            n_dirs += sum(1 for f in res.folders if not f.skipped_reason)
            n_skip_files += res.n_skipped_files
            self._append_log(f"{res.root}: 対象 {res.n_files} ファイル（{human_size(res.total_bytes)}）")
        msg = f"{len(self.plans)} フォルダで対象 {n_files} ファイル（{human_size(n_bytes)}）、サブフォルダ {n_dirs}"
        if n_skip_dirs:
            msg += f"。対象外のフォルダ {n_skip_dirs}"
        if n_skip_files:
            msg += f"、対象外のファイル {n_skip_files}"
        self.status_label.setText(msg + "。「変換を開始」で上から順に始めます" if n_files else msg + "。変換するものはありません")
        self._append_log(msg)
        self.start_btn.setEnabled(n_files > 0)

    def _show_plans(self) -> None:
        rows: list[tuple[B.ScanResult, B.FolderPlan]] = [(res, fp) for res in self.plans for fp in res.folders]
        many = len(self.plans) > 1
        self.table.setRowCount(len(rows))
        for r, (res, fp) in enumerate(rows):
            self._fill_row(r, res.root, fp, prefix=(os.path.basename(os.path.normpath(res.root)) + " / ") if many else "",
                           active=(res is self.ctx.batch.plan and self.ctx.batch.running))

    def _show_plan(self, res: B.ScanResult) -> None:
        """互換用: 全部を描き直す。"""
        self._show_plans()

    def _fill_row(self, r: int, root: str, fp: B.FolderPlan, prefix: str = "", active: bool = False) -> None:
        if True:
            rel = os.path.relpath(fp.dir, root) if fp.dir != root else "."
            rel = (prefix + rel) if rel != "." else (prefix.rstrip(" /") or ".")
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
            elif active and fp.n_done:
                color = COLORS["info"]
            set_row(self.table, r, [rel, str(fp.n_targets), human_size(fp.total_bytes), str(fp.n_done) if fp.n_done else "",
                                    str(fp.n_failed) if fp.n_failed else "", "; ".join(notes)], color)

    # --------------------------------------------------------------- 実行
    def start(self) -> None:
        todo = [p for p in self.plans if p.n_files]
        if not todo:
            return
        if self.ctx.batch.running:
            return
        if self.ctx.settings.convert_enabled and self.ctx.converter.busy():
            cur = self.ctx.converter.current or ""
            st = self.ctx.converter.last_status or ""
            QMessageBox.information(self, "変換中",
                                    f"取り込んだカードの mp4 への変換が裏で動いています（{cur}）。\n{st}\n\n"
                                    "終わってから始めてください（取り込み画面の「裏で続く処理」で進み具合が分かります。"
                                    "取り込み画面の「動画の変換」を外すと、残りは元のまま NAS へ送って止まります）。")
            return
        first = todo[0]
        if first.in_place:
            how = "変換した mp4 を同じフォルダに置き、元のファイルは消します。この操作は元に戻せません。"
        else:
            out = self.out_row.text()
            how = f"変換した mp4 を {out} の下に（フォルダ構造を保って）書きます。元のファイルはそのまま残します。"
        n_files = sum(p.n_files for p in todo)
        n_bytes = sum(p.total_bytes for p in todo)
        lines = ([f"フォルダ（この順に）:"] + [f"  {p.root}（{p.n_files} ファイル、{human_size(p.total_bytes)}）" for p in todo[:8]]
                 + ([f"  …ほか {len(todo) - 8} フォルダ"] if len(todo) > 8 else [])
                 + [f"合計: {n_files} ファイル（{human_size(n_bytes)}）",
                    f"画質 CRF {int(self.crf.value())}、同時 {int(self.parallel.value())} 本",
                    "",
                    how,
                    "MJPEG の AVI なら容量は 10 分の 1 程度になり、見た目はほぼ変わりません。",
                    "", "始めますか？"])
        if QMessageBox.question(self, "変換の実行", "\n".join(lines)) != QMessageBox.StandardButton.Yes:
            return
        self._queue = list(todo)
        self._run_totals = {"done": 0, "failed": 0, "in": 0, "out": 0, "folders": 0}
        self._append_log(f"変換を開始: {len(todo)} フォルダ、{n_files} ファイル（{human_size(n_bytes)}）")
        self._start_next()

    def _start_next(self) -> bool:
        """キューの先頭のフォルダの変換を始める。始めたら True。"""
        while self._queue:
            plan = self._queue.pop(0)
            self.plan = plan
            n = self._run_totals["folders"] + 1
            total = n + len(self._queue)
            self._append_log(f"[{n}/{total}] {plan.root} を変換します（{plan.n_files} ファイル）")
            if self.ctx.batch.start(plan, parallel=int(self.parallel.value()), crf=int(self.crf.value())):
                self.start_btn.setEnabled(False)
                self.scan_btn.setEnabled(False)
                self.pause_btn.setEnabled(True)
                self.stop_btn.setEnabled(True)
                return True
        return False

    def _pause_toggled(self, on: bool) -> None:
        self.ctx.batch.paused = on
        self.pause_btn.setText("再開" if on else "一時停止")

    def stop(self) -> None:
        if not self.ctx.batch.running:
            return
        if QMessageBox.question(self, "中止", "変換を中止しますか？ 変換中のファイルは元のまま残ります（変換が終わった分はそのままです）。") \
                != QMessageBox.StandardButton.Yes:
            return
        remaining = len(self._queue)
        self._queue = []
        if remaining:
            self._append_log(f"残り {remaining} フォルダは始めずに終わります")
        self.ctx.batch.stop()
        self.pause_btn.setChecked(False)
        self.stop_btn.setEnabled(False)

    def _on_done(self, stats: B.BatchStats) -> None:
        tot = self._run_totals
        tot["done"] += stats.n_done
        tot["failed"] += stats.n_failed
        tot["in"] += stats.bytes_in_done
        tot["out"] += stats.bytes_out_done
        tot["folders"] += 1
        self._refresh_progress()
        self._show_plans()
        if not stats.stopped and self._queue:
            self.pause_btn.setChecked(False)
            if self._start_next():
                return
        self.pause_btn.setChecked(False)
        self.pause_btn.setEnabled(False)
        self.stop_btn.setEnabled(False)
        self.scan_btn.setEnabled(True)
        self._queue = []
        if tot["folders"] > 1:
            msg = (f"{'中止' if stats.stopped else '完了'}: {tot['folders']} フォルダ、{tot['done']} ファイル、"
                   f"{human_size(tot['in'])} → {human_size(tot['out'])}" + (f"、失敗 {tot['failed']}" if tot["failed"] else ""))
            self._append_log("=== " + msg + " ===")
        else:
            msg = stats.message
        win = self.window()
        if hasattr(win, "notify"):
            win.notify("mp4 への変換", msg)
        # 続きがあれば（中止・失敗）もう一度調べると残りが対象になる
        self.start_btn.setEnabled(False)

    def _set_status(self, text: str) -> None:
        """変換側からの 1 行。フォルダが複数のときは「フォルダ i/n」を頭に付ける。"""
        n_total = self._run_totals["folders"] + (1 if self.ctx.batch.running else 0) + len(self._queue)
        if n_total > 1 and self.ctx.batch.running:
            i = self._run_totals["folders"] + 1
            name = os.path.basename(os.path.normpath(self.plan.root)) if self.plan else ""
            text = f"[フォルダ {i}/{n_total} {name}] {text}"
        self.status_label.setText(text)

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
            self._show_plans()
        in_place = not (b.plan and b.plan.out_root)
        saved = (f"空いた容量 {human_size(st.saved_bytes)}" if in_place else f"出力 {human_size(st.bytes_out_done)}") if st.bytes_in_done else ""
        el = f"経過 {st.elapsed/60:.0f} 分" if st.elapsed >= 60 else ""
        self.summary_label.setText("　".join(x for x in (saved, el) if x))

    def _parallel_changed(self, v: int) -> None:
        st = self.ctx.settings
        if st.batch_convert_parallel != int(v):
            st.batch_convert_parallel = int(v)
            self.ctx.save()

    # ------------------------------------------------------------------ 診断
    def diagnose(self) -> None:
        """動画を 1 本選んで、どこに時間がかかっているかを測る。"""
        import threading
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QDialog, QDialogButtonBox, QFileDialog, QPlainTextEdit, QVBoxLayout
        from ...core.ffmpeg import diagnose_conversion, find_ffmpeg
        s = self.ctx.settings
        ffmpeg = find_ffmpeg(s.ffmpeg_path)
        if not ffmpeg:
            QMessageBox.warning(self, "診断", "ffmpeg が見つかりません。設定の「動画の変換の詳細」で場所を指定するかダウンロードしてください。")
            return
        start_dir = self.path_row.text() or (self.folders()[0] if self.folders() else "")
        path, _ = QFileDialog.getOpenFileName(self, "診断する動画を 1 本選ぶ", start_dir,
                                              "動画 (*.avi *.AVI *.mp4 *.MP4 *.mov);;すべて (*)")
        if not path:
            return
        dlg = QDialog(self)
        dlg.setWindowTitle("変換の診断")
        dlg.resize(820, 520)
        lay = QVBoxLayout(dlg)
        box = QPlainTextEdit()
        box.setReadOnly(True)
        box.setPlainText("測っています（同じ動画を数回変換するので、動画の長さの数倍かかります）…\n")
        lay.addWidget(box)
        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        btns.rejected.connect(dlg.reject)
        btns.accepted.connect(dlg.accept)
        lay.addWidget(btns)
        lines: list[str] = []
        result: dict = {}

        def work() -> None:
            try:
                result["text"] = diagnose_conversion(ffmpeg, path, s.convert_encoder, int(s.convert_crf), on_progress=lines.append)
            except Exception as e:  # pragma: no cover
                result["text"] = "診断でエラー: " + str(e)

        threading.Thread(target=work, name="convert-diag", daemon=True).start()
        timer = QTimer(dlg)
        shown = {"n": 0}

        def tick() -> None:
            if len(lines) > shown["n"]:
                box.appendPlainText("\n".join(lines[shown["n"]:]))
                shown["n"] = len(lines)
            if "text" in result:
                timer.stop()
                box.setPlainText(result["text"])
                self.log.appendPlainText("=== 変換の診断 ===\n" + result["text"])

        timer.timeout.connect(tick)
        timer.start(300)
        dlg.exec()

