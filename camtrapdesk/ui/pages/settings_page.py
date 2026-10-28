"""設定画面。"""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from ...core.fieldnote import format_station
from ..context import AppContext
from ..widgets import PathRow


class SettingsPage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        s = ctx.settings
        outer = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        inner = QWidget()
        lay = QVBoxLayout(inner)
        scroll.setWidget(inner)
        outer.addWidget(scroll, 1)

        # --- 保存先 ---
        g1 = QGroupBox("保存先")
        f1 = QFormLayout(g1)
        f1.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.dest_root = PathRow(s.dest_root, "dir", placeholder=r"例: D:\CamTrap（中継HDD/SSD。NASを直接指定してもよい）")
        self.nas_root = PathRow(s.nas_root, "dir", placeholder=r"例: \\DS2422\camtrap（空ならNAS転送なし）")
        f1.addRow("中継先（または最終保存先）", self.dest_root)
        f1.addRow("NASの最終保存先", self.nas_root)
        f1.addRow("", QLabel("取り込み先のフォルダ名（調査回や調査地などの区切り）は「1. 取り込み」画面で、野帳のファイルは「2. 地点名に変更」画面で指定します。"))
        lay.addWidget(g1)

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
        f2.addRow("", self.media_recursive)
        f2.addRow("対象の拡張子", self.media_ext)
        f2.addRow("動画がこの枚数未満のカードはカメラ不調としてエラー（コピーしない）", self.min_count)
        f2.addRow("このサイズ未満（バイト）のファイルがあれば注意表示（コピーは行う）", self.min_size)
        f2.addRow("IDファイル名", self.id_file)
        lay.addWidget(g2)

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
        f3.addRow("同時にコピーするカード枚数", self.concurrency)
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
        g5 = QGroupBox("動画の変換（任意）: AVI などを H.264 の mp4 にしてから NAS へ送る")
        f5 = QFormLayout(g5)
        f5.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.conv_enabled = QCheckBox("変換する（SD → 中継 は元のまま検証コピーし、中継の上で mp4 に変換して元ファイルを消す）")
        self.conv_enabled.setChecked(s.convert_enabled)
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
        f5.addRow("", self.conv_enabled)
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
        self.tail_match = QCheckBox("カードIDは末尾の数字で照合する（id.txt の 6_650 と野帳の 650 を同じとみなす）")
        self.tail_match.setChecked(s.card_id_match_tail_number)
        cols = QHBoxLayout()
        for lbl, w in (("地点名の列", self.col_station), ("回収したカードIDの列", self.col_ret)):
            cols.addWidget(QLabel(lbl))
            cols.addWidget(w)
        cols.addStretch(1)
        f4.addRow("野帳の列名", cols)
        note = QLabel("この2列だけ必須。どの行を使うか（Season や Type など）は「2. 地点名に変更」画面で列と値を指定して絞り込みます。大文字小文字や綴りの揺れは自動で読み替えます。")
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

        # --- 保存 ---
        hb = QHBoxLayout()
        save = QPushButton("設定を保存")
        save.clicked.connect(self.save)
        hb.addStretch(1)
        hb.addWidget(save)
        lay.addLayout(hb)
        lay.addStretch(1)

    def _check_ffmpeg(self) -> None:
        from ...core.ffmpeg import candidate_status, detect_encoder, ffmpeg_candidates, find_ffmpeg, forget_ffmpeg
        from PySide6.QtWidgets import QApplication
        forget_ffmpeg()
        path = find_ffmpeg(self.ffmpeg_path.text())
        if not path:
            tried = "\n".join("  " + candidate_status(c) for c in ffmpeg_candidates(self.ffmpeg_path.text()))
            self.ffmpeg_result.setText("使える ffmpeg が見つかりません。「ffmpeg をダウンロードして入れる」を押すか、上の欄で場所を指定してください。\n探した場所:\n" + tried)
            return
        self.ffmpeg_result.setText(f"{path} を確認中…")
        QApplication.processEvents()
        enc = detect_encoder(path, self.conv_encoder.currentData() or "auto")
        kind = "CPU" if enc == "libx264" else "ハードウェア"
        self.ffmpeg_result.setText(f"ffmpeg: {path}\n使うエンコーダ: {enc}（{kind}）")

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

    def save(self) -> None:
        s = self.ctx.settings
        s.dest_root = self.dest_root.text()
        s.nas_root = self.nas_root.text()
        s.media_subdirs = self._split(self.media_subdirs.text()) or ["DCIM/100_BTCF"]
        s.media_recursive = self.media_recursive.isChecked()
        s.media_extensions = [e.lstrip(".").lower() for e in self._split(self.media_ext.text())] or ["mp4"]
        s.min_file_size_bytes = int(self.min_size.value())
        s.min_file_count = int(self.min_count.value())
        s.id_file_name = self.id_file.text().strip() or "id.txt"
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
        s.card_id_match_tail_number = self.tail_match.isChecked()
        s.station_prefix = self.station_prefix.text().strip()
        s.station_digits = int(self.station_digits.value())
        s.convert_enabled = self.conv_enabled.isChecked()
        s.convert_extensions = [e.lstrip(".").lower() for e in self._split(self.conv_ext.text())] or ["avi"]
        s.convert_crf = int(self.conv_crf.value())
        s.convert_encoder = self.conv_encoder.currentData() or "auto"
        s.convert_parallel = int(self.conv_parallel.value())
        s.ffmpeg_path = self.ffmpeg_path.text()
        self.ctx.save()
        QMessageBox.information(self, "設定", "保存しました。同時コピー枚数の変更はアプリの再起動後に反映されます。")
