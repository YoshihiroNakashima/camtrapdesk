"""設定画面。"""

from __future__ import annotations

from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QScrollArea, QSpinBox, QTableWidget, QTableWidgetItem,
                               QVBoxLayout, QWidget, QHeaderView)

from ...config import TypeRule
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
        self.dest_root = PathRow(s.dest_root, "dir", placeholder=r"例: D:\CamTrap（中継HDD/SSD。NASを直接指定してもよい）")
        self.nas_root = PathRow(s.nas_root, "dir", placeholder=r"例: \\DS2422\camtrap（空ならNAS転送なし）")
        self.season_folder = QLineEdit(s.season_folder)
        f1.addRow("中継先（または最終保存先）", self.dest_root)
        f1.addRow("NASの最終保存先", self.nas_root)
        f1.addRow("現在のシーズンフォルダ名", self.season_folder)
        lay.addWidget(g1)

        # --- カード内の対象 ---
        g2 = QGroupBox("SDカード内の対象")
        f2 = QFormLayout(g2)
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
        f2.addRow("動画がこの枚数未満ならエラーにしてコピーしない（例: 2 → 1枚だけのカードは不調扱い）", self.min_count)
        f2.addRow("注意表示の閾値: このサイズ未満のファイルがあれば表示（バイト。コピーは行う）", self.min_size)
        f2.addRow("IDファイル名", self.id_file)
        lay.addWidget(g2)

        # --- 挙動 ---
        g3 = QGroupBox("取り込み・転送・削除の挙動")
        f3 = QFormLayout(g3)
        self.concurrency = QSpinBox()
        self.concurrency.setRange(1, 32)
        self.concurrency.setValue(s.concurrency)
        self.verify = QCheckBox("コピー後に読み戻してハッシュ照合する（推奨）")
        self.verify.setChecked(s.verify_readback)
        self.nas_verify = QCheckBox("NASへ転送後も読み戻してハッシュ照合する（推奨）")
        self.nas_verify.setChecked(s.nas_verify_readback)
        self.nas_wait = QCheckBox("カードの取り込み中はNAS転送を待たせる（中継HDDの読み書きの取り合いを避ける。推奨）")
        self.nas_wait.setChecked(s.nas_wait_while_ingesting)
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
        f3.addRow("", self.del_req_nas)
        f3.addRow("カード検出の間隔（秒）", self.poll)
        f3.addRow("追加の検索フォルダ（カンマ区切り、通常は空）", self.scan_roots)
        f3.addRow("除外するボリューム（カンマ区切り）", self.exclude)
        lay.addWidget(g3)

        # --- 野帳 ---
        g4 = QGroupBox("野帳（ファイルは「名前変更」画面で選びます）")
        f4 = QFormLayout(g4)
        self.sheet = QLineEdit(s.fieldnote_sheet)
        self.sheet.setPlaceholderText("空なら最初のシート")
        self.col_season = QLineEdit(s.col_season)
        self.col_station = QLineEdit(s.col_station)
        self.col_type = QLineEdit(s.col_type)
        self.col_ret = QLineEdit(s.col_retrieval_sd)
        self.default_type = QLineEdit(s.default_type)
        self.tail_match = QCheckBox("カードIDは末尾の数字で照合する（id.txt の 6_650 と野帳の 650 を同じとみなす）")
        self.tail_match.setChecked(s.card_id_match_tail_number)
        f4.addRow("シート名", self.sheet)
        f4.addRow("", self.tail_match)
        cols = QHBoxLayout()
        for lbl, w in (("Season", self.col_season), ("Station", self.col_station), ("Type", self.col_type),
                       ("回収SD", self.col_ret)):
            cols.addWidget(QLabel(lbl))
            cols.addWidget(w)
        f4.addRow("列名（大文字小文字や綴りの揺れは自動で読み替え）", cols)
        f4.addRow("既定の Type", self.default_type)
        self.rules = QTableWidget(0, 4)
        self.rules.setHorizontalHeaderLabels(["Type", "Seasonの接頭辞", "Stationの接頭辞", "ゼロ埋め桁数"])
        self.rules.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        for t, r in s.type_rules.items():
            self._add_rule_row(t, r)
        btns = QHBoxLayout()
        add = QPushButton("Type を追加")
        add.clicked.connect(lambda: self._add_rule_row("", TypeRule()))
        rm = QPushButton("選択行を削除")
        rm.clicked.connect(lambda: self.rules.removeRow(self.rules.currentRow()))
        btns.addWidget(add)
        btns.addWidget(rm)
        btns.addStretch(1)
        f4.addRow("フォルダ名の整形ルール", self.rules)
        f4.addRow("", btns)
        lay.addWidget(g4)

        # --- 保存 ---
        hb = QHBoxLayout()
        save = QPushButton("設定を保存")
        save.clicked.connect(self.save)
        hb.addStretch(1)
        hb.addWidget(save)
        lay.addLayout(hb)
        lay.addStretch(1)

    def _add_rule_row(self, t: str, r: TypeRule) -> None:
        row = self.rules.rowCount()
        self.rules.insertRow(row)
        for c, v in enumerate([t, r.season_prefix, r.station_prefix, str(r.station_pad)]):
            self.rules.setItem(row, c, QTableWidgetItem(v))

    @staticmethod
    def _split(text: str) -> list[str]:
        return [x.strip() for x in text.split(",") if x.strip()]

    def save(self) -> None:
        s = self.ctx.settings
        s.dest_root = self.dest_root.text()
        s.nas_root = self.nas_root.text()
        s.season_folder = self.season_folder.text().strip()
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
        s.poll_interval_sec = float(self.poll.value())
        s.scan_roots = self._split(self.scan_roots.text())
        s.exclude_volumes = self._split(self.exclude.text())
        s.fieldnote_sheet = self.sheet.text().strip()
        s.col_season = self.col_season.text().strip() or "Season"
        s.col_station = self.col_station.text().strip() or "Station"
        s.col_type = self.col_type.text().strip() or "Type"
        s.col_retrieval_sd = self.col_ret.text().strip() or "RetrievalSD"
        s.card_id_match_tail_number = self.tail_match.isChecked()
        s.default_type = self.default_type.text().strip() or "N"
        rules: dict[str, TypeRule] = {}
        for r in range(self.rules.rowCount()):
            def cell(c: int) -> str:
                it = self.rules.item(r, c)
                return it.text().strip() if it else ""
            t = cell(0)
            if not t:
                continue
            try:
                pad = int(cell(3) or "4")
            except ValueError:
                pad = 4
            rules[t] = TypeRule(season_prefix=cell(1), station_prefix=cell(2), station_pad=pad)
        if rules:
            s.type_rules = rules
        self.ctx.save()
        QMessageBox.information(self, "設定", "保存しました。同時コピー枚数の変更はアプリの再起動後に反映されます。")
