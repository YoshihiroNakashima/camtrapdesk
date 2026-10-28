"""名前変更画面: 野帳を読み、Type と Season を選び、ドライランを確認してからフォルダ名を地点名に変更する。"""

from __future__ import annotations

from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QVBoxLayout, QWidget, QSplitter)
from PySide6.QtCore import Qt

from ...core import rename as R
from ...core.fieldnote import Fieldnote, build_mapping, load_fieldnote
from ..context import AppContext
from ..widgets import COLORS, PathRow, make_table, set_row


class RenamePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.fieldnote: Fieldnote | None = None
        self.plan: R.RenamePlan | None = None
        s = ctx.settings

        root = QVBoxLayout(self)
        box = QGroupBox("野帳と対象")
        form = QFormLayout(box)
        self.path_row = PathRow(s.fieldnote_path, mode="file", placeholder="野帳写し.xlsx")
        self.path_row.edit.editingFinished.connect(self._path_edited)
        form.addRow("野帳（Excel）", self.path_row)
        h = QHBoxLayout()
        self.sheet_combo = QComboBox()
        self.load_btn = QPushButton("野帳を読み込む")
        self.load_btn.clicked.connect(self.load_fieldnote)
        h.addWidget(QLabel("シート"))
        h.addWidget(self.sheet_combo)
        h.addWidget(self.load_btn)
        h.addStretch(1)
        form.addRow("", h)
        h2 = QHBoxLayout()
        self.type_combo = QComboBox()
        self.type_combo.currentTextChanged.connect(self._type_changed)
        self.season_combo = QComboBox()
        h2.addWidget(QLabel("Type"))
        h2.addWidget(self.type_combo)
        h2.addWidget(QLabel("Season"))
        h2.addWidget(self.season_combo)
        h2.addWidget(QLabel("（野帳の回収SD列のカードIDを地点名に対応づけます）"))
        h2.addStretch(1)
        form.addRow("対象", h2)
        h3 = QHBoxLayout()
        self.dry_btn = QPushButton("ドライラン（確認）")
        self.dry_btn.clicked.connect(self.dry_run)
        self.apply_btn = QPushButton("変更を実行")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
        h3.addWidget(QLabel("保存先（中継）とNASの両方のフォルダを見て、ある方を変更します"))
        h3.addWidget(self.dry_btn)
        h3.addWidget(self.apply_btn)
        h3.addStretch(1)
        form.addRow("実行", h3)
        root.addWidget(box)

        split = QSplitter(Qt.Orientation.Vertical)
        self.table = make_table(["結果", "カードID", "現在のフォルダ", "変更後", "場所", "野帳行", "日付", "State", "理由・注意"])
        split.addWidget(self.table)
        self.issues = QPlainTextEdit()
        self.issues.setReadOnly(True)
        split.addWidget(self.issues)
        split.setSizes([420, 140])
        root.addWidget(split, 1)
        ctx.settings_changed.connect(self._settings_changed)

    def _settings_changed(self) -> None:
        if self.path_row.text() != self.ctx.settings.fieldnote_path:
            self.path_row.setText(self.ctx.settings.fieldnote_path)

    def _path_edited(self) -> None:
        p = self.path_row.text()
        if p != self.ctx.settings.fieldnote_path:
            self.ctx.settings.fieldnote_path = p
            self.ctx.save()

    # ------------------------------------------------------------------
    def load_fieldnote(self) -> None:
        path = self.path_row.text()
        if not path:
            QMessageBox.warning(self, "野帳", "野帳のExcelファイルを指定してください。")
            return
        try:
            sheet = self.sheet_combo.currentText() or None
            fn = load_fieldnote(path, self.ctx.settings, sheet)
        except Exception as e:
            QMessageBox.critical(self, "野帳の読み込み", f"読み込めませんでした:\n{e}")
            return
        self.fieldnote = fn
        if path != self.ctx.settings.fieldnote_path:
            self.ctx.settings.fieldnote_path = path
            self.ctx.save()
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        self.sheet_combo.addItems(fn.sheets)
        self.sheet_combo.setCurrentText(fn.sheet)
        self.sheet_combo.blockSignals(False)
        self.type_combo.blockSignals(True)
        self.type_combo.clear()
        for t in fn.types():
            self.type_combo.addItem(t if t else "（Type列なし）", t)
        default_type = self.ctx.settings.default_type
        if default_type in fn.types():
            self.type_combo.setCurrentIndex(fn.types().index(default_type))
        self.type_combo.setEnabled(fn.has_type)
        self.type_combo.blockSignals(False)
        self._type_changed(self.type_combo.currentText())
        msg = f"読み込みました: {len(fn.rows)} 行"
        if fn.has_type:
            msg += f"、Type: {', '.join(fn.types()[:8])}"
        if fn.notes:
            msg += "\n" + "\n".join(" - " + n for n in fn.notes)
        self.issues.setPlainText(msg)
        self.apply_btn.setEnabled(False)

    def _current_type(self) -> str:
        d = self.type_combo.currentData()
        return d if d is not None else self.type_combo.currentText()

    def _current_season(self) -> str:
        d = self.season_combo.currentData()
        return d if d is not None else self.season_combo.currentText()

    def _type_changed(self, _t: str) -> None:
        self.season_combo.clear()
        if not self.fieldnote:
            return
        t = self._current_type()
        seasons = self.fieldnote.seasons(t)
        for sn in seasons:
            self.season_combo.addItem(sn if sn else "（Season列なし: 全行）", sn)
        self.season_combo.setEnabled(self.fieldnote.has_season)
        # 取り込み中のシーズンがあればそれを選ぶ
        cur = self.ctx.settings.season_folder.strip()
        rule = self.ctx.settings.rule_for(t)
        for i, sn in enumerate(seasons):
            if sn and (sn == cur or rule.season_prefix + sn == cur):
                self.season_combo.setCurrentIndex(i)
                break

    def _root(self) -> tuple[str, str | None]:
        s = self.ctx.settings
        return s.dest_root, (s.nas_root or None)

    def dry_run(self) -> None:
        if not self.fieldnote:
            self.load_fieldnote()
            if not self.fieldnote:
                return
        t = self._current_type()
        season = self._current_season()
        if (self.fieldnote.has_type and not t) or (self.fieldnote.has_season and not season):
            QMessageBox.warning(self, "対象", "Type と Season を選んでください。")
            return
        root, nas_root = self._root()
        if not root and not nas_root:
            QMessageBox.warning(self, "フォルダ", "変更するフォルダの場所（保存先かNAS）が設定されていません。")
            return
        mapping = build_mapping(self.fieldnote, t, season, self.ctx.settings)
        plan = R.build_plan(root, nas_root, mapping, self.ctx.ledger)
        self.plan = plan
        self._show_plan(plan)
        self.apply_btn.setEnabled(plan.count(R.A_RENAME) > 0)

    def _show_plan(self, plan: R.RenamePlan) -> None:
        order = {R.A_RENAME: 0, R.A_BLOCKED: 1, R.A_TARGET_EXISTS: 2, R.A_SYNCING: 3, R.A_MISSING: 4,
                 R.A_UNMAPPED: 5, R.A_ALREADY: 6}
        items = sorted(plan.items, key=lambda i: (order.get(i.action, 9), i.dst_dir_name or i.src_dir_name))
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            color = {R.A_RENAME: COLORS["ok"], R.A_ALREADY: COLORS["muted"], R.A_MISSING: COLORS["warn"],
                     R.A_UNMAPPED: COLORS["warn"], R.A_BLOCKED: COLORS["err"],
                     R.A_TARGET_EXISTS: COLORS["err"], R.A_SYNCING: COLORS["info"]}.get(it.action)
            if it.action == R.A_RENAME and it.warnings:
                color = COLORS["warn"]
            e = it.entry
            note = "; ".join(filter(None, [it.reason] + it.warnings))
            set_row(self.table, r, [R.ACTION_LABELS.get(it.action, it.action), it.card_id, it.src_dir_name,
                                    it.dst_dir_name, it.where, str(e.row_no) if e else "", e.date if e else "",
                                    e.state if e else "", note], color)
        lines = [f"シーズンフォルダ: {plan.season_dir}" + (f"　／　NAS: {plan.nas_season_dir}" if plan.nas_season_dir else ""),
                 f"変更 {plan.count(R.A_RENAME)} / 保留 {plan.count(R.A_BLOCKED)} / 変更先が既存 {plan.count(R.A_TARGET_EXISTS)}"
                 f" / NAS転送中 {plan.count(R.A_SYNCING)} / フォルダ無し {plan.count(R.A_MISSING)}"
                 f" / 野帳に無い {plan.count(R.A_UNMAPPED)} / 変更済み {plan.count(R.A_ALREADY)}"]
        if plan.issues:
            lines.append("")
            lines.append("【野帳側で確認が必要な点】")
            lines += [" - " + x for x in plan.issues]
        self.issues.setPlainText("\n".join(lines))

    def apply(self) -> None:
        if not self.plan:
            return
        n = self.plan.count(R.A_RENAME)
        warn = sum(1 for i in self.plan.items if i.action == R.A_RENAME and i.warnings)
        msg = f"{n} 個のフォルダ名を地点名に変更します。"
        if warn:
            msg += f"\nそのうち {warn} 個には注意（前回設置SDとの不一致など）が付いています。"
        msg += "\n\n実行しますか？"
        if QMessageBox.question(self, "変更の実行", msg) != QMessageBox.StandardButton.Yes:
            return
        logs: list[str] = []
        done, errors = R.apply_plan(self.plan, self.ctx.ledger, log=logs.append)
        text = f"{done} 件を変更しました。\n" + "\n".join(logs[:50])
        if errors:
            text += "\n\n【エラー】\n" + "\n".join(errors)
        self.issues.setPlainText(text)
        self.apply_btn.setEnabled(False)
        self.dry_run()
