"""地点名に変更: 野帳を読み、使う行を「列 = 値」で絞り込み、変更するフォルダを選んで、ドライラン→実行。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QPushButton, QSplitter, QVBoxLayout, QWidget)

from ...core import rename as R
from ...core.fieldnote import Condition, Fieldnote, build_mapping, load_fieldnote
from ..context import AppContext
from ..widgets import COLORS, PathRow, make_table, set_row


class ConditionRow(QWidget):
    """「列 = 値」の 1 行。列は野帳の見出しから選び、値はその列にある値から選ぶ（手入力も可）。"""

    def __init__(self, page: "RenamePage", parent=None):
        super().__init__(parent)
        self.page = page
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.col = QComboBox()
        self.col.setEditable(True)
        self.col.setMinimumWidth(160)
        self.col.setToolTip("野帳の列名（見出し）")
        self.val = QComboBox()
        self.val.setEditable(True)
        self.val.setMinimumWidth(180)
        self.val.setToolTip("その列の値。一覧から選ぶか入力")
        self.rm = QPushButton("×")
        self.rm.setFixedWidth(28)
        self.rm.setToolTip("この条件を外す")
        lay.addWidget(QLabel("列"))
        lay.addWidget(self.col)
        lay.addWidget(QLabel("＝"))
        lay.addWidget(self.val)
        lay.addWidget(self.rm)
        lay.addStretch(1)
        self.col.currentTextChanged.connect(self._col_changed)
        self.val.currentTextChanged.connect(lambda _t: self.page.update_match_count())
        self.rm.clicked.connect(lambda: self.page.remove_condition(self))

    def set_columns(self, columns: list[str]) -> None:
        cur = self.col.currentText()
        self.col.blockSignals(True)
        self.col.clear()
        self.col.addItems(columns)
        self.col.setCurrentText(cur)
        self.col.blockSignals(False)
        self._col_changed(self.col.currentText())

    def _col_changed(self, name: str) -> None:
        fn = self.page.fieldnote
        cur = self.val.currentText()
        self.val.blockSignals(True)
        self.val.clear()
        if fn and name.strip():
            self.val.addItems(fn.distinct(name))
        self.val.setCurrentText(cur)
        self.val.blockSignals(False)
        self.page.update_match_count()

    def condition(self) -> Condition:
        return (self.col.currentText().strip(), self.val.currentText().strip())

    def set(self, col: str, val: str) -> None:
        self.col.setCurrentText(col)
        self._col_changed(col)
        self.val.setCurrentText(val)


class RenamePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.fieldnote: Fieldnote | None = None
        self.plan: R.RenamePlan | None = None
        self.cond_rows: list[ConditionRow] = []
        s = ctx.settings

        root = QVBoxLayout(self)
        box = QGroupBox("野帳と対象")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # 野帳: パス＋参照＋読み込み（1 行）。パスを選んだら自動で読む
        h1 = QHBoxLayout()
        self.path_row = PathRow(s.fieldnote_path, mode="file", placeholder="野帳の Excel ファイル（例: 野帳写し.xlsx）")
        self.path_row.edit.editingFinished.connect(self._path_edited)
        self.load_btn = QPushButton("読み込む")
        self.load_btn.clicked.connect(self.load_fieldnote)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setToolTip("シート")
        self.sheet_combo.currentTextChanged.connect(self._sheet_changed)
        h1.addWidget(self.path_row, 1)
        h1.addWidget(self.load_btn)
        h1.addWidget(QLabel("シート"))
        h1.addWidget(self.sheet_combo)
        form.addRow("野帳（Excel）", h1)
        self.fn_label = QLabel("まだ読み込んでいません")
        self.fn_label.setWordWrap(True)
        form.addRow("", self.fn_label)

        # 絞り込み条件
        cond_box = QVBoxLayout()
        self.cond_area = QVBoxLayout()
        cond_box.addLayout(self.cond_area)
        hc = QHBoxLayout()
        self.add_cond_btn = QPushButton("＋ 条件を追加")
        self.add_cond_btn.clicked.connect(lambda: self.add_condition())
        self.match_label = QLabel("")
        hc.addWidget(self.add_cond_btn)
        hc.addWidget(self.match_label, 1)
        cond_box.addLayout(hc)
        hint = QLabel("野帳のどの行を使うかを「列 ＝ 値」で絞り込みます（複数はすべて満たす行）。例: Season ＝ 260710、Type ＝ N。"
                      "条件が無ければ全行を使います。使う行の「回収したカードIDの列」の値と、取り込んだフォルダ名（カードID）を対応づけます。")
        hint.setWordWrap(True)
        cond_box.addWidget(hint)
        form.addRow("使う行の絞り込み", cond_box)

        # 変更するフォルダ
        h4 = QHBoxLayout()
        self.folder_combo = QComboBox()
        self.folder_combo.setMinimumWidth(220)
        self.folder_combo.setToolTip("保存先（中継）と NAS にある区切りフォルダ（調査回や調査地）の一覧。通常は取り込み画面で指定した今回のフォルダ")
        self.folder_refresh = QPushButton("一覧を更新")
        self.folder_refresh.clicked.connect(self.refresh_folders)
        h4.addWidget(self.folder_combo)
        h4.addWidget(self.folder_refresh)
        h4.addWidget(QLabel("（この中のカードIDのフォルダを地点名にします。保存先とNASの両方を見て、ある方を変更）"))
        h4.addStretch(1)
        form.addRow("変更するフォルダ", h4)

        # 実行
        h3 = QHBoxLayout()
        self.dry_btn = QPushButton("ドライラン（確認）")
        self.dry_btn.clicked.connect(self.dry_run)
        self.apply_btn = QPushButton("変更を実行")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply)
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
        self.refresh_folders()
        self._loaded_once = False

    # ------------------------------------------------------------------ 野帳
    def _settings_changed(self) -> None:
        if self.path_row.text() != self.ctx.settings.fieldnote_path:
            self.path_row.setText(self.ctx.settings.fieldnote_path)
        self.refresh_folders()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refresh_folders()
        # 初めて開いたとき、野帳のパスが設定されていれば自動で読む
        if not self._loaded_once and self.path_row.text() and os.path.isfile(self.path_row.text()):
            self._loaded_once = True
            self.load_fieldnote()

    def _path_edited(self) -> None:
        p = self.path_row.text()
        if p != self.ctx.settings.fieldnote_path:
            self.ctx.settings.fieldnote_path = p
            self.ctx.save()
            if os.path.isfile(p):
                self.load_fieldnote()

    def _sheet_changed(self, _t: str) -> None:
        if self.fieldnote and self.sheet_combo.currentText() and self.sheet_combo.currentText() != self.fieldnote.sheet:
            self.load_fieldnote()

    def load_fieldnote(self) -> None:
        path = self.path_row.text()
        if not path:
            QMessageBox.warning(self, "野帳", "野帳のExcelファイルを指定してください。")
            return
        try:
            sheet = self.sheet_combo.currentText() or None
            fn = load_fieldnote(path, self.ctx.settings, sheet)
        except Exception as e:
            self.fn_label.setText(f"読み込めませんでした: {e}")
            QMessageBox.critical(self, "野帳の読み込み", f"読み込めませんでした:\n{e}")
            return
        self.fieldnote = fn
        s = self.ctx.settings
        if path != s.fieldnote_path or fn.sheet != s.fieldnote_sheet:
            s.fieldnote_path = path
            s.fieldnote_sheet = fn.sheet
            self.ctx.save()
        self.sheet_combo.blockSignals(True)
        self.sheet_combo.clear()
        self.sheet_combo.addItems(fn.sheets)
        self.sheet_combo.setCurrentText(fn.sheet)
        self.sheet_combo.blockSignals(False)
        msg = f"読み込みました: {len(fn.rows)} 行。列: {', '.join(fn.columns)}"
        if fn.notes:
            msg += "\n" + "\n".join(" - " + n for n in fn.notes)
        self.fn_label.setText(msg)
        self._restore_conditions()
        self.update_match_count()
        self.apply_btn.setEnabled(False)

    # ------------------------------------------------------------- 絞り込み
    def add_condition(self, col: str = "", val: str = "") -> ConditionRow:
        row = ConditionRow(self)
        if self.fieldnote:
            row.set_columns(self.fieldnote.columns)
        if col:
            row.set(col, val)
        self.cond_rows.append(row)
        self.cond_area.addWidget(row)
        self.update_match_count()
        return row

    def remove_condition(self, row: ConditionRow) -> None:
        if row in self.cond_rows:
            self.cond_rows.remove(row)
        self.cond_area.removeWidget(row)
        row.deleteLater()
        self.update_match_count()

    def clear_conditions(self) -> None:
        for r in list(self.cond_rows):
            self.remove_condition(r)

    def conditions(self) -> list[Condition]:
        return [r.condition() for r in self.cond_rows if r.condition()[0]]

    def _restore_conditions(self) -> None:
        """前回の条件を復元する。無ければ、野帳に Season 列があり変更するフォルダ名に合う値があれば、それを初期条件にする。"""
        fn = self.fieldnote
        if not fn:
            return
        self.clear_conditions()
        saved = [tuple(x) for x in self.ctx.settings.rename_filters if isinstance(x, (list, tuple)) and len(x) == 2]
        usable = [(c, v) for c, v in saved if fn.resolve_column(c)]
        if usable:
            for c, v in usable:
                self.add_condition(fn.resolve_column(c) or c, v)
            return
        season_col = fn.resolve_column("Season")
        folder = (self._folder() or self.ctx.settings.season_folder).strip()
        if season_col and folder:
            for v in fn.distinct(season_col):
                if v and (v == folder or folder.endswith(v) or v.endswith(folder)):
                    self.add_condition(season_col, v)
                    return
        self.add_condition()   # 空の 1 行（列を選ぶだけで使える）

    def update_match_count(self) -> None:
        fn = self.fieldnote
        if not fn:
            self.match_label.setText("")
            return
        conds = self.conditions()
        for c, _v in conds:
            if fn.resolve_column(c) is None:
                self.match_label.setText(f"列 '{c}' がありません")
                return
        n = len(fn.filter(conds))
        self.match_label.setText(f"条件に合う行: {n} / {len(fn.rows)}" + ("（条件なし＝全行）" if not conds else ""))

    # --------------------------------------------------------------- フォルダ
    def refresh_folders(self) -> None:
        """保存先と NAS にある区切りフォルダを一覧にし、取り込み画面で指定中のものを選ぶ。"""
        s = self.ctx.settings
        names: set[str] = set()
        for root in (s.dest_root, s.nas_root):
            if root and os.path.isdir(root):
                try:
                    for n in os.listdir(root):
                        p = os.path.join(root, n)
                        if os.path.isdir(p) and not n.startswith(".") and not os.path.isfile(os.path.join(p, "manifest.json")):
                            names.add(n)
                except OSError:
                    pass
        cur = s.season_folder.strip()
        if cur:
            names.add(cur)
        prev = self.folder_combo.currentData()
        self.folder_combo.blockSignals(True)
        self.folder_combo.clear()
        self.folder_combo.addItem("（保存先の直下）", "")
        for n in sorted(names, reverse=True):
            self.folder_combo.addItem(n, n)
        want = prev if prev else cur
        idx = self.folder_combo.findData(want)
        self.folder_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.folder_combo.blockSignals(False)

    def _folder(self) -> str:
        d = self.folder_combo.currentData()
        return d if d is not None else ""

    def _root(self) -> tuple[str, str | None]:
        s = self.ctx.settings
        return s.dest_root, (s.nas_root or None)

    # ------------------------------------------------------------------ 実行
    def dry_run(self) -> None:
        if not self.fieldnote:
            self.load_fieldnote()
            if not self.fieldnote:
                return
        conds = self.conditions()
        root, nas_root = self._root()
        if not root and not nas_root:
            QMessageBox.warning(self, "フォルダ", "変更するフォルダの場所（保存先かNAS）が設定されていません。")
            return
        saved = [list(c) for c in conds]
        if saved != self.ctx.settings.rename_filters:
            self.ctx.settings.rename_filters = saved
            self.ctx.save()
        mapping = build_mapping(self.fieldnote, conds, self.ctx.settings, season_folder=self._folder())
        plan = R.build_plan(root, nas_root, mapping, self.ctx.ledger)
        self.plan = plan
        self._show_plan(plan, mapping.condition_text)
        self.apply_btn.setEnabled(plan.count(R.A_RENAME) > 0)

    def _show_plan(self, plan: R.RenamePlan, cond_text: str = "") -> None:
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
        lines = [f"使う行: {cond_text}" if cond_text else "",
                 f"変更するフォルダ: {plan.season_dir}" + (f"　／　NAS: {plan.nas_season_dir}" if plan.nas_season_dir else ""),
                 f"変更 {plan.count(R.A_RENAME)} / 保留 {plan.count(R.A_BLOCKED)} / 変更先が既存 {plan.count(R.A_TARGET_EXISTS)}"
                 f" / NAS転送中 {plan.count(R.A_SYNCING)} / フォルダ無し {plan.count(R.A_MISSING)}"
                 f" / 野帳に無い {plan.count(R.A_UNMAPPED)} / 変更済み {plan.count(R.A_ALREADY)}"]
        if plan.issues:
            lines.append("")
            lines.append("【野帳側で確認が必要な点】")
            lines += [" - " + x for x in plan.issues]
        self.issues.setPlainText("\n".join(x for x in lines if x is not None))

    def apply(self) -> None:
        if not self.plan:
            return
        n = self.plan.count(R.A_RENAME)
        warn = sum(1 for i in self.plan.items if i.action == R.A_RENAME and i.warnings)
        msg = f"{n} 個のフォルダ名を地点名に変更します。"
        if warn:
            msg += f"\nそのうち {warn} 個には注意が付いています。"
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
