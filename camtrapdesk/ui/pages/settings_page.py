"""設定画面。"""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

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
        f1.addRow("", QLabel("調査回（シーズン）ごとのフォルダ名は「1. 取り込み」画面で、野帳のファイルは「2. 地点名に変更」画面で指定します。"))
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

        # --- 野帳と地点名 ---
        g4 = QGroupBox("野帳の列名と地点名の付け方")
        f4 = QFormLayout(g4)
        f4.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.col_season = QLineEdit(s.col_season)
        self.col_station = QLineEdit(s.col_station)
        self.col_type = QLineEdit(s.col_type)
        self.col_ret = QLineEdit(s.col_retrieval_sd)
        self.tail_match = QCheckBox("カードIDは末尾の数字で照合する（id.txt の 6_650 と野帳の 650 を同じとみなす）")
        self.tail_match.setChecked(s.card_id_match_tail_number)
        cols = QHBoxLayout()
        for lbl, w in (("Season", self.col_season), ("Station", self.col_station), ("Type", self.col_type),
                       ("回収SD", self.col_ret)):
            cols.addWidget(QLabel(lbl))
            cols.addWidget(w)
        f4.addRow("野帳の列名", cols)
        note = QLabel("Station と 回収SD は必須。Season と Type は野帳に無くてもよい（無ければ全行が対象）。大文字小文字や綴りの揺れは自動で読み替えます。")
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
        s.col_season = self.col_season.text().strip() or "Season"
        s.col_station = self.col_station.text().strip() or "Station"
        s.col_type = self.col_type.text().strip() or "Type"
        s.col_retrieval_sd = self.col_ret.text().strip() or "RetrievalSD"
        s.card_id_match_tail_number = self.tail_match.isChecked()
        s.station_prefix = self.station_prefix.text().strip()
        s.station_digits = int(self.station_digits.value())
        self.ctx.save()
        QMessageBox.information(self, "設定", "保存しました。同時コピー枚数の変更はアプリの再起動後に反映されます。")
