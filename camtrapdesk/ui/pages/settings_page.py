"""設定画面: 細かい項目だけ。値を変えるとそのまま保存される（「保存」ボタンは無い）。

保存先・NAS・フォルダ名・変換のオンオフは「1. 取り込み」画面、野帳のファイルと絞り込みは「2. 地点名に変更」画面で指定する。
"""

from __future__ import annotations

import time

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QRadioButton, QScrollArea, QSpinBox, QVBoxLayout,
                               QWidget)

from ...core.fieldnote import format_station
from ..context import AppContext
from ..widgets import PathRow


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self._loading = True
        s = ctx.settings
        outer = QVBoxLayout(self)
        top = QLabel("ここにあるのは普段は変えなくてよい項目です。値を変えるとその場で保存されます（「保存」ボタンはありません）。"
                     "保存先・NAS・フォルダ名・動画の変換のオンオフは「1. 取り込み」画面で、野帳のファイルは「2. 地点名に変更」画面で指定します。")
        top.setWordWrap(True)
        outer.addWidget(top)
        self.saved_label = QLabel("")
        self.saved_label.setStyleSheet("color: #2e7d5a;")
        outer.addWidget(self.saved_label)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # --- カード内の対象 ---
        g2 = QGroupBox("SDカード内の対象")
        f2 = QFormLayout(g2)
        f2.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.media_subdirs = QLineEdit(", ".join(s.media_subdirs))
        self.media_recursive = QCheckBox("サブフォルダも含める")
        self.media_recursive.setChecked(s.media_recursive)
        self.media_ext = QLineEdit(", ".join(s.media_extensions))
        self.min_size = QSpinBox()
        self.min_size.setRange(0, 1_000_000_000)
        self.min_size.setValue(s.min_file_size_bytes)
        self.min_count = QSpinBox()
        self.min_count.setRange(0, 1000)
        self.min_count.setValue(s.min_file_count)
        self.id_file = QLineEdit(s.id_file_name)
        f2.addRow("対象フォルダ（カンマ区切り）", self.media_subdirs)
        f2.addRow("", QLabel("既定は Browning（DCIM/100_BTCF）。そのフォルダが無いカードは DCIM 以下をすべて探すので、他社のカメラでもたいていそのままで動きます"))
        f2.addRow("", self.media_recursive)
        f2.addRow("対象の拡張子", self.media_ext)
        f2.addRow("動画がこの枚数未満のカードはカメラ不調としてエラー（コピーしない）", self.min_count)
        f2.addRow("このサイズ未満（バイト）のファイルがあれば注意表示（コピーは行う）", self.min_size)
        f2.addRow("IDファイル名", self.id_file)
        lay.addWidget(g2)

        # --- 壊れたファイル ---
        g6 = QGroupBox("壊れたファイル（Windows で「スキャンして修復しますか」と出るカード）")
        f6 = QFormLayout(g6)
        f6.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        note6 = QLabel("カメラは電圧低下などで動画を途中までしか書けないことがあります。そのダイアログは「スキャンせずに続行」（閉じるだけでもよい）で、"
                       "カードはそのまま取り込めます。取り込みでは全ファイルをコピーしてから中継の上で壊れたものを見つけ、取り除きます。"
                       "壊れたファイルは NAS には送らず、「3. カードを消す」でカードからも消えます。")
        note6.setWordWrap(True)
        f6.addRow(note6)
        self.broken_check = QCheckBox("コピー後に壊れた動画・画像を調べて取り除く（推奨）")
        self.broken_check.setChecked(s.broken_check)
        self.broken_delete = QRadioButton("中継から消す（既定）")
        self.broken_keep = QRadioButton("カードのフォルダ内の _破損 に移して残す（確認したいとき）")
        (self.broken_keep if s.broken_action == "keep" else self.broken_delete).setChecked(True)
        hb6 = QHBoxLayout()
        hb6.addWidget(self.broken_delete)
        hb6.addWidget(self.broken_keep)
        hb6.addStretch(1)
        f6.addRow("", self.broken_check)
        f6.addRow("壊れたファイルの扱い", hb6)
        lay.addWidget(g6)

        # --- 挙動 ---
        g3 = QGroupBox("取り込み・転送・削除の挙動")
        f3 = QFormLayout(g3)
        f3.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        self.concurrency.setValue(s.concurrency)
        self.verify = QCheckBox("コピー後に読み戻してハッシュ照合する（推奨）")
        self.verify.setChecked(s.verify_readback)
        self.nas_verify = QCheckBox("NASへ転送後も読み戻してハッシュ照合する（推奨）")
        self.nas_verify.setChecked(s.nas_verify_readback)
        self.nas_wait = QCheckBox("カードの取り込み中はNAS転送を待たせる（中継がHDDのとき。SSDなら外す）")
        self.nas_wait.setToolTip("HDD は読み書きを同時にすると遅くなるので、取り込みが終わってから NAS へ送ります。SSD ならこの制限は不要です")
        self.nas_wait.setChecked(s.nas_wait_while_ingesting)
        self.cleanup = QCheckBox("NASで検証済みになったカードは中継のコピーを自動で消す（通常はオフ）")
        self.cleanup.setToolTip("オフなら中継にも控えが残ります。中継の容量が足りず、別のSSDに切り替えるのも難しいときだけオンにしてください")
        self.cleanup.setChecked(s.staging_cleanup_after_nas)
        self.min_free = QDoubleSpinBox()
        self.min_free.setRange(0, 10000)
        self.min_free.setDecimals(0)
        self.min_free.setSuffix(" GB")
        self.min_free.setValue(s.min_free_gb)
        self.del_req_nas = QCheckBox("削除はNASで検証済みのカードに限る（NASを使う場合）")
        self.del_req_nas.setChecked(s.delete_requires_nas)
        self.poll = QDoubleSpinBox()
        self.poll.setRange(0.5, 60)
        self.poll.setValue(s.poll_interval_sec)
        self.scan_roots = QLineEdit(", ".join(s.scan_roots))
        self.exclude = QLineEdit(", ".join(s.exclude_volumes))
        f3.addRow("同時にコピーするカード枚数（変更はアプリの再起動後に反映）", self.concurrency)
        f3.addRow("", self.verify)
        f3.addRow("", self.nas_verify)
        f3.addRow("", self.nas_wait)
        f3.addRow("", self.cleanup)
        self.min_free.setToolTip("中継の空きが「そのカードの容量＋この余裕」を下回るカードは「空き待ち」になり、別のSSDへの切り替えや NAS 転送で空くのを待ちます")
        f3.addRow("中継の空きの余裕（これを下回ったら「空き待ち」）", self.min_free)
        f3.addRow("", self.del_req_nas)
        f3.addRow("カード検出の間隔（秒）", self.poll)
        f3.addRow("追加の検索フォルダ（カンマ区切り、通常は空）", self.scan_roots)
        f3.addRow("除外するボリューム（カンマ区切り）", self.exclude)
        lay.addWidget(g3)

        # --- 動画の変換 ---
        g5 = QGroupBox("動画の変換の詳細（オンオフは「1. 取り込み」画面）")
        f5 = QFormLayout(g5)
        f5.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.conv_ext = QLineEdit(", ".join(s.convert_extensions))
        self.conv_ext.setMaximumWidth(200)
        self.conv_crf = QSpinBox()
        self.conv_crf.setRange(15, 35)
        self.conv_crf.setValue(int(s.convert_crf))
        self.conv_crf.setToolTip("小さいほど高画質で大きいファイル。23 が標準（MJPEG の AVI なら 10分の1 程度）、26 でさらに半分")
        self.conv_encoder = QComboBox()
        for code, label in (("auto", "自動（GPU や内蔵エンコーダがあれば使う。無ければ CPU）"), ("libx264", "CPU（libx264）"),
                            ("h264_nvenc", "NVIDIA（nvenc）"), ("h264_qsv", "Intel 内蔵（QuickSync）"),
                            ("h264_amf", "AMD（AMF）"), ("h264_videotoolbox", "Mac（VideoToolbox）")):
            self.conv_encoder.addItem(label, code)
        i = self.conv_encoder.findData(s.convert_encoder)
        self.conv_encoder.setCurrentIndex(i if i >= 0 else 0)
        self.conv_parallel = QSpinBox()
        self.conv_parallel.setRange(1, 8)
        self.conv_parallel.setValue(int(s.convert_parallel))
        self.ffmpeg_path = PathRow(s.ffmpeg_path, "file", placeholder="空なら自動で探す（アプリ同梱 → PATH）",
                                   file_filter="ffmpeg (ffmpeg.exe ffmpeg);;すべて (*)")
        self.ffmpeg_check = QPushButton("ffmpeg とエンコーダを確認")
        self.ffmpeg_check.clicked.connect(self._check_ffmpeg)
        self.ffmpeg_dl = QPushButton("ffmpeg をダウンロードして入れる")
        self.ffmpeg_dl.setToolTip("公式ビルド（Windows は約 200 MB、GitHub から）を取ってきて、アプリの tools フォルダに置きます。Mac は evermeet.cx の静的ビルド")
        self.ffmpeg_dl.clicked.connect(self._download_ffmpeg)
        self.ffmpeg_result = QLabel("")
        self.ffmpeg_result.setWordWrap(True)
        hc = QHBoxLayout()
        hc.addWidget(QLabel("変換する拡張子"))
        hc.addWidget(self.conv_ext)
        hc.addWidget(QLabel("画質（CRF）"))
        hc.addWidget(self.conv_crf)
        hc.addWidget(QLabel("同時に変換する本数"))
        hc.addWidget(self.conv_parallel)
        hc.addStretch(1)
        f5.addRow("", hc)
        f5.addRow("エンコーダ", self.conv_encoder)
        f5.addRow("ffmpeg の場所", self.ffmpeg_path)
        hf = QHBoxLayout()
        hf.addWidget(self.ffmpeg_check)
        hf.addWidget(self.ffmpeg_dl)
        hf.addWidget(self.ffmpeg_result, 1)
        f5.addRow("", hf)
        lay.addWidget(g5)

        # --- 野帳と地点名 ---
        g4 = QGroupBox("野帳の列名と地点名の付け方")
        f4 = QFormLayout(g4)
        f4.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.col_station = QLineEdit(s.col_station)
        self.col_ret = QLineEdit(s.col_retrieval_sd)
        self.col_inst = QLineEdit(s.col_installation_sd)
        self.tail_match = QCheckBox("カードIDは末尾の数字で照合する（id.txt の 6_650 と野帳の 650 を同じとみなす）")
        self.tail_match.setChecked(s.card_id_match_tail_number)
        cols = QHBoxLayout()
        for lbl, w in (("地点名の列", self.col_station), ("回収したカードIDの列", self.col_ret), ("設置したカードIDの列（照合用、任意）", self.col_inst)):
            cols.addWidget(QLabel(lbl))
            cols.addWidget(w)
        cols.addStretch(1)
        f4.addRow("野帳の列名", cols)
        note = QLabel("地点名と回収SDの2列だけ必須。どの行を使うか（Season や Type など）は「2. 地点名に変更」画面で列と値を指定して絞り込みます。"
                      "大文字小文字や綴りの揺れは自動で読み替えます。設置SDの列は、設置記録との照合（同じ画面）を使うときだけ要ります。")
        note.setWordWrap(True)
        f4.addRow("", note)
        f4.addRow("", self.tail_match)
        self.station_prefix = QLineEdit(s.station_prefix)
        self.station_prefix.setMaximumWidth(120)
        self.station_prefix.setPlaceholderText("空なら数字だけ")
        self.station_digits = QSpinBox()
        self.station_digits.setRange(0, 10)
        self.station_digits.setValue(int(s.station_digits))
        self.station_example = QLabel("")
        hs = QHBoxLayout()
        hs.addWidget(QLabel("接頭辞"))
        hs.addWidget(self.station_prefix)
        hs.addWidget(QLabel("番号の桁数（0 ならそのまま）"))
        hs.addWidget(self.station_digits)
        hs.addWidget(self.station_example)
        hs.addStretch(1)
        f4.addRow("地点名（フォルダ名）の付け方", hs)
        self.station_prefix.textChanged.connect(self._update_example)
        self.station_digits.valueChanged.connect(self._update_example)
        self._update_example()
        lay.addWidget(g4)
        lay.addStretch(1)

        # 値を変えたらそのまま保存する
        for w in (self.media_subdirs, self.media_ext, self.id_file, self.scan_roots, self.exclude, self.conv_ext,
                  self.col_station, self.col_ret, self.col_inst, self.station_prefix):
            w.editingFinished.connect(self._autosave)
        for w in (self.media_recursive, self.broken_check, self.broken_delete, self.broken_keep, self.verify,
                  self.nas_verify, self.nas_wait, self.cleanup, self.del_req_nas, self.tail_match):
            w.toggled.connect(self._autosave)
        for w in (self.min_size, self.min_count, self.concurrency, self.conv_crf, self.conv_parallel, self.station_digits):
            w.valueChanged.connect(self._autosave)
        for w in (self.min_free, self.poll):
            w.valueChanged.connect(self._autosave)
        self.conv_encoder.currentIndexChanged.connect(self._autosave)
        self.ffmpeg_path.changed.connect(self._autosave)
        self._loading = False

    def _check_ffmpeg(self) -> None:
        from ...core.ffmpeg import candidate_status, detect_encoder, ffmpeg_candidates, find_ffmpeg, forget_ffmpeg
        from PySide6.QtWidgets import QApplication
        forget_ffmpeg()
        path = find_ffmpeg(self.ffmpeg_path.text())
        if not path:
            tried = "\n".join("  " + candidate_status(c) for c in ffmpeg_candidates(self.ffmpeg_path.text()))
            self.ffmpeg_result.setText("使える ffmpeg が見つかりません。「ffmpeg をダウンロードして入れる」を押すか、上の欄で場所を指定してください。\n探した場所:\n" + tried)
            return
        self.ffmpeg_result.setText(f"{path} を確認中…（各エンコーダで短い試し変換をします）")
        QApplication.processEvents()
        from ...core.ffmpeg import encoder_report
        pref = self.conv_encoder.currentData() or "auto"
        enc = detect_encoder(path, pref)
        kind = "CPU" if enc == "libx264" else "ハードウェア"
        lines = [f"ffmpeg: {path}", f"使うエンコーダ: {enc}（{kind}）", "試した結果:"]
        for name, ok, why in encoder_report(path, pref):
            lines.append(f"  {name}: {'○ ' if ok else '× '}{why}")
        if enc == "libx264" and pref == "auto":
            lines.append("GPU があるのに × のときは、グラフィックドライバを最新にするか、理由の行を知らせてください")
        self.ffmpeg_result.setText("\n".join(lines))

    def _download_ffmpeg(self) -> None:
        import threading
        from PySide6.QtCore import QTimer
        from PySide6.QtWidgets import QProgressDialog
        from ...core.ffmpeg import app_tools_dir, download_ffmpeg
        dest = app_tools_dir()
        dlg = QProgressDialog(f"ffmpeg を取ってきています…\n置き場所: {dest}", "中止", 0, 1000, self)
        dlg.setWindowTitle("ffmpeg のダウンロード")
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        state = {"got": 0, "total": 0, "done": None}
        stop = threading.Event()

        def prog(got: int, total: int) -> None:
            state["got"], state["total"] = got, total

        def work() -> None:
            state["done"] = download_ffmpeg(dest, on_progress=prog, stop=stop)

        threading.Thread(target=work, name="ffmpeg-dl", daemon=True).start()
        timer = QTimer(self)

        def tick() -> None:
            if dlg.wasCanceled():
                stop.set()
            got, total = state["got"], state["total"]
            if total:
                dlg.setValue(int(got * 1000 / total))
                dlg.setLabelText(f"ffmpeg を取ってきています… {got/1e6:.0f} / {total/1e6:.0f} MB\n置き場所: {dest}")
            else:
                dlg.setLabelText(f"ffmpeg を取ってきています… {got/1e6:.0f} MB\n置き場所: {dest}")
            if state["done"] is not None:
                timer.stop()
                dlg.close()
                ok, msg = state["done"]
                if ok:
                    self.ffmpeg_path.setText("")
                    self._autosave()
                    self._check_ffmpeg()
                    QMessageBox.information(self, "ffmpeg", f"ffmpeg を入れました:\n{msg}")
                else:
                    self.ffmpeg_result.setText(msg)
                    QMessageBox.warning(self, "ffmpeg", msg)

        timer.timeout.connect(tick)
        timer.start(200)

    def _update_example(self) -> None:
        pre = self.station_prefix.text().strip()
        d = int(self.station_digits.value())
        self.station_example.setText(f"例: 野帳の 482 → {format_station('482', pre, d)}、BSN730 → {format_station('BSN730', pre, d)}、Ridge-A → Ridge-A")

    @staticmethod
    def _split(text: str) -> list[str]:
        return [x.strip() for x in text.split(",") if x.strip()]

    def _autosave(self, *_args) -> None:
        if self._loading:
            return
        if self.save():
            self.saved_label.setText(time.strftime("%H:%M:%S") + " 保存しました")

    def save(self) -> bool:
        """画面の値を設定に写して保存する。戻り値は何かが変わったか。"""
        s = self.ctx.settings
        before = s.to_dict()
        s.media_subdirs = self._split(self.media_subdirs.text()) or ["DCIM/100_BTCF"]
        s.media_recursive = self.media_recursive.isChecked()
        s.media_extensions = [e.lstrip(".").lower() for e in self._split(self.media_ext.text())] or ["mp4"]
        s.min_file_size_bytes = int(self.min_size.value())
        s.min_file_count = int(self.min_count.value())
        s.id_file_name = self.id_file.text().strip() or "id.txt"
        s.broken_check = self.broken_check.isChecked()
        s.broken_action = "keep" if self.broken_keep.isChecked() else "delete"
        s.concurrency = int(self.concurrency.value())
        s.verify_readback = self.verify.isChecked()
        s.nas_verify_readback = self.nas_verify.isChecked()
        s.delete_requires_nas = self.del_req_nas.isChecked()
        s.nas_wait_while_ingesting = self.nas_wait.isChecked()
        s.staging_cleanup_after_nas = self.cleanup.isChecked()
        s.min_free_gb = float(self.min_free.value())
        s.poll_interval_sec = float(self.poll.value())
        s.scan_roots = self._split(self.scan_roots.text())
        s.exclude_volumes = self._split(self.exclude.text())
        s.col_station = self.col_station.text().strip() or "Station"
        s.col_retrieval_sd = self.col_ret.text().strip() or "RetrievalSD"
        s.col_installation_sd = self.col_inst.text().strip() or "InstallationSD"
        s.card_id_match_tail_number = self.tail_match.isChecked()
        s.station_prefix = self.station_prefix.text().strip()
        s.station_digits = int(self.station_digits.value())
        s.convert_extensions = [e.lstrip(".").lower() for e in self._split(self.conv_ext.text())] or ["avi"]
        s.convert_crf = int(self.conv_crf.value())
        s.convert_encoder = self.conv_encoder.currentData() or "auto"
        s.convert_parallel = int(self.conv_parallel.value())
        s.ffmpeg_path = self.ffmpeg_path.text()
        if s.to_dict() == before:
            return False
        self.ctx.save()
        return True
