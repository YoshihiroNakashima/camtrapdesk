"""地点名に変更: 野帳を読み、使う行を「列 = 値」で絞り込み、変更するフォルダを選んで、ドライラン→実行。
任意で、設置時の記録（地点名・設置SD）と回収SDが合うかも照合する。"""

from __future__ import annotations

import os
from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from ...core import rename as R
from ...core.fieldnote import (Condition, Fieldnote, build_mapping, cross_check_installation, is_csv,
                               load_fieldnote)
from ..context import AppContext
from ..widgets import COLORS, PathRow, make_table, set_row

TABLE_FILTER = "表 (*.xlsx *.xlsm *.csv *.tsv);;Excel (*.xlsx *.xlsm);;CSV (*.csv *.tsv *.txt);;すべて (*)"


class ConditionRow(QWidget):
    """「列 = 値」の 1 行。列は表の見出しから選び、値はその列にある値から選ぶ（手入力も可）。"""

    def __init__(self, owner: "ConditionList", parent=None):
        super().__init__(parent)
        self.owner = owner
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.col = QComboBox()
        self.col.setEditable(True)
        self.col.setMinimumWidth(160)
        self.col.setToolTip("表の列名（見出し）")
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
        self.val.currentTextChanged.connect(lambda _t: self.owner.changed())
        self.rm.clicked.connect(lambda: self.owner.remove_condition(self))

    def set_columns(self, columns: list[str]) -> None:
        cur = self.col.currentText()
        self.col.blockSignals(True)
        self.col.clear()
        self.col.addItems(columns)
        self.col.setCurrentText(cur)
        self.col.blockSignals(False)
        self._col_changed(self.col.currentText())

    def _col_changed(self, name: str) -> None:
        fn = self.owner.get_fieldnote()
        cur = self.val.currentText()
        self.val.blockSignals(True)
        self.val.clear()
        if fn and name.strip():
            self.val.addItems(fn.distinct(name))
        self.val.setCurrentText(cur)
        self.val.blockSignals(False)
        self.owner.changed()

    def condition(self) -> Condition:
        return (self.col.currentText().strip(), self.val.currentText().strip())

    def set(self, col: str, val: str) -> None:
        self.col.setCurrentText(col)
        self._col_changed(col)
        self.val.setCurrentText(val)


class ConditionList(QWidget):
    """「列 ＝ 値」の条件の並び＋追加ボタン＋該当行数。"""

    def __init__(self, get_fieldnote: Callable[[], Fieldnote | None], on_change: Callable[[], None] | None = None,
                 parent=None):
        super().__init__(parent)
        self.get_fieldnote = get_fieldnote
        self.on_change = on_change
        self.rows: list[ConditionRow] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.area = QVBoxLayout()
        lay.addLayout(self.area)
        h = QHBoxLayout()
        self.add_btn = QPushButton("＋ 条件を追加")
        self.add_btn.clicked.connect(lambda: self.add())
        self.match_label = QLabel("")
        h.addWidget(self.add_btn)
        h.addWidget(self.match_label, 1)
        lay.addLayout(h)

    def add(self, col: str = "", val: str = "") -> ConditionRow:
        row = ConditionRow(self)
        fn = self.get_fieldnote()
        if fn:
            row.set_columns(fn.columns)
        if col:
            row.set(col, val)
        self.rows.append(row)
        self.area.addWidget(row)
        self.changed()
        return row

    def remove_condition(self, row: ConditionRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
        self.area.removeWidget(row)
        row.deleteLater()
        self.changed()

    def clear(self) -> None:
        for r in list(self.rows):
            self.remove_condition(r)

    def conditions(self) -> list[Condition]:
        return [r.condition() for r in self.rows if r.condition()[0]]

    def set_conditions(self, conds: list[tuple[str, str]]) -> None:
        self.clear()
        for c, v in conds:
            self.add(c, v)
        if not conds:
            self.add()

    def refresh_columns(self) -> None:
        fn = self.get_fieldnote()
        for r in self.rows:
            r.set_columns(fn.columns if fn else [])

    def changed(self) -> None:
        fn = self.get_fieldnote()
        if not fn:
            self.match_label.setText("")
        else:
            conds = self.conditions()
            bad = [c for c, _v in conds if fn.resolve_column(c) is None]
            if bad:
                self.match_label.setText(f"列 '{bad[0]}' がありません")
            else:
                n = len(fn.filter(conds))
                self.match_label.setText(f"条件に合う行: {n} / {len(fn.rows)}" + ("（条件なし＝全行）" if not conds else ""))
        if self.on_change:
            self.on_change()


class RenamePage(QWidget):
    def __init__(self, ctx: AppContext, parent=None):
        super().__init__(parent)
        self.ctx = ctx
        self.fieldnote: Fieldnote | None = None
        self.install_fn: Fieldnote | None = None
        self.plan: R.RenamePlan | None = None
        s = ctx.settings

        root = QVBoxLayout(self)
        box = QGroupBox("野帳と対象")
        form = QFormLayout(box)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)

        # 野帳: パス＋参照＋読み込み（1 行）。パスを選んだら自動で読む
        h1 = QHBoxLayout()
        self.path_row = PathRow(s.fieldnote_path, mode="file", placeholder="野帳の Excel か csv（例: 野帳写し.xlsx）",
                                file_filter=TABLE_FILTER)
        self.path_row.changed.connect(self._path_edited)
        self.load_btn = QPushButton("読み込む")
        self.load_btn.clicked.connect(self.load_fieldnote)
        self.sheet_combo = QComboBox()
        self.sheet_combo.setToolTip("シート")
        self.sheet_combo.currentTextChanged.connect(self._sheet_changed)
        h1.addWidget(self.path_row, 1)
        h1.addWidget(self.load_btn)
        h1.addWidget(QLabel("シート"))
        h1.addWidget(self.sheet_combo)
        form.addRow("野帳（Excel / csv）", h1)
        self.fn_label = QLabel("まだ読み込んでいません")
        self.fn_label.setWordWrap(True)
        form.addRow("", self.fn_label)

        # 絞り込み条件
        cond_box = QVBoxLayout()
        self.conds = ConditionList(lambda: self.fieldnote)
        cond_box.addWidget(self.conds)
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

        # 設置記録との照合（任意）
        ibox = QGroupBox("設置記録との照合（任意）: 回収SDが、設置時に記録した設置SDと合っているか確かめる")
        iform = QFormLayout(ibox)
        iform.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.install_chk = QCheckBox("照合する（合わない行には注意が付きます。変更は止めません）")
        self.install_chk.setChecked(s.install_check)
        self.install_chk.toggled.connect(self._install_toggled)
        iform.addRow("", self.install_chk)
        self.install_widgets = QWidget()
        iv = QVBoxLayout(self.install_widgets)
        iv.setContentsMargins(0, 0, 0, 0)
        hi = QHBoxLayout()
        self.install_path = PathRow(s.install_path, mode="file",
                                    placeholder="空なら野帳と同じファイル。設置時の表（csv / Excel）を別に持っていればそれを指定",
                                    file_filter=TABLE_FILTER)
        self.install_path.changed.connect(self._install_path_edited)
        self.install_sheet = QComboBox()
        self.install_sheet.setToolTip("シート")
        self.install_sheet.currentTextChanged.connect(self._install_sheet_changed)
        hi.addWidget(QLabel("設置記録"))
        hi.addWidget(self.install_path, 1)
        hi.addWidget(QLabel("シート"))
        hi.addWidget(self.install_sheet)
        iv.addLayout(hi)
        self.install_label = QLabel("")
        self.install_label.setWordWrap(True)
        iv.addWidget(self.install_label)
        self.install_conds = ConditionList(lambda: self.install_fn)
        iv.addWidget(self.install_conds)
        ihint = QLabel("設置記録の行を「列 ＝ 値」で絞り込みます（野帳と同じファイルなら、たとえば前の調査回: Season ＝ 260410）。"
                       f"地点名の列は野帳と同じ、設置SDの列は設定の「設置したカードIDの列」（今は {s.col_installation_sd}）。"
                       "地点ごとに設置SD と今回の回収SD を比べ、違えば表の注意に出します。設置記録にあるのに回収行が無い地点も挙げます。")
        ihint.setWordWrap(True)
        iv.addWidget(ihint)
        iform.addRow(self.install_widgets)
        root.addWidget(ibox)
        self.install_widgets.setVisible(s.install_check)

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

    def _path_edited(self, p: str) -> None:
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
            QMessageBox.warning(self, "野帳", "野帳の Excel か csv ファイルを指定してください。")
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
        self.sheet_combo.setEnabled(not is_csv(path))
        self.sheet_combo.blockSignals(False)
        msg = f"読み込みました: {len(fn.rows)} 行。列: {', '.join(fn.columns)}"
        if fn.notes:
            msg += "\n" + "\n".join(" - " + n for n in fn.notes)
        self.fn_label.setText(msg)
        self._restore_conditions()
        self.conds.changed()
        self.apply_btn.setEnabled(False)
        if self.install_chk.isChecked():
            self.load_install()

    # ------------------------------------------------------------- 絞り込み
    def add_condition(self, col: str = "", val: str = "") -> ConditionRow:
        return self.conds.add(col, val)

    def conditions(self) -> list[Condition]:
        return self.conds.conditions()

    @property
    def match_label(self) -> QLabel:
        return self.conds.match_label

    def _restore_conditions(self) -> None:
        """前回の条件を復元する。無ければ、野帳に Season 列があり変更するフォルダ名に合う値があれば、それを初期条件にする。"""
        fn = self.fieldnote
        if not fn:
            return
        self.conds.clear()
        saved = [tuple(x) for x in self.ctx.settings.rename_filters if isinstance(x, (list, tuple)) and len(x) == 2]
        usable = [(c, v) for c, v in saved if fn.resolve_column(c)]
        if usable:
            for c, v in usable:
                self.conds.add(fn.resolve_column(c) or c, v)
            return
        season_col = fn.resolve_column("Season")
        folder = (self._folder() or self.ctx.settings.season_folder).strip()
        if season_col and folder:
            for v in fn.distinct(season_col):
                if v and (v == folder or folder.endswith(v) or v.endswith(folder)):
                    self.conds.add(season_col, v)
                    return
        self.conds.add()   # 空の 1 行（列を選ぶだけで使える）

    def update_match_count(self) -> None:
        self.conds.changed()

    # --------------------------------------------------------------- 設置記録
    def _install_toggled(self, on: bool) -> None:
        self.install_widgets.setVisible(on)
        s = self.ctx.settings
        if on != s.install_check:
            s.install_check = on
            self.ctx.save()
        if on and self.install_fn is None and self.fieldnote is not None:
            self.load_install()

    def _install_path_edited(self, p: str) -> None:
        s = self.ctx.settings
        if p != s.install_path:
            s.install_path = p
            s.install_sheet = ""
            self.ctx.save()
        self.load_install()

    def _install_sheet_changed(self, _t: str) -> None:
        if self.install_fn and self.install_sheet.currentText() and self.install_sheet.currentText() != self.install_fn.sheet:
            self.load_install()

    def install_source(self) -> tuple[str, str | None]:
        """設置記録のファイルとシート（空なら野帳と同じ）。"""
        s = self.ctx.settings
        own = self.install_path.text()
        path = own or self.path_row.text()
        sheet = self.install_sheet.currentText() or s.install_sheet or None
        if not sheet and not own and self.fieldnote and self.fieldnote.path == path:
            sheet = self.fieldnote.sheet   # 野帳と同じファイルで、シートを選んでいなければ野帳と同じシート
        return path, sheet

    def load_install(self) -> None:
        path, sheet = self.install_source()
        if not path:
            self.install_label.setText("設置記録のファイルを指定してください（野帳を読み込んでいれば同じファイルを使います）")
            return
        try:
            fn = load_fieldnote(path, self.ctx.settings, sheet, require_retrieval=False)
        except Exception as e:
            self.install_fn = None
            self.install_label.setText(f"設置記録を読み込めませんでした: {e}")
            return
        self.install_fn = fn
        s = self.ctx.settings
        if fn.sheet != s.install_sheet:
            s.install_sheet = fn.sheet
            self.ctx.save()
        self.install_sheet.blockSignals(True)
        self.install_sheet.clear()
        self.install_sheet.addItems(fn.sheets)
        self.install_sheet.setCurrentText(fn.sheet)
        self.install_sheet.setEnabled(not is_csv(path))
        self.install_sheet.blockSignals(False)
        col = fn.resolve_column(s.col_installation_sd)
        self.install_label.setText(f"読み込みました: {len(fn.rows)} 行（{os.path.basename(path)}"
                                   + (f" / {fn.sheet}" if fn.sheet else "") + "）"
                                   + (f"。設置SDの列: {col}" if col else f"。列 '{s.col_installation_sd}' が無いので照合できません（設定で列名を合わせてください）"))
        saved = [tuple(x) for x in s.install_filters if isinstance(x, (list, tuple)) and len(x) == 2]
        usable = [(fn.resolve_column(c) or c, v) for c, v in saved if fn.resolve_column(c)]
        self.install_conds.set_conditions(usable)

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
            QMessageBox.warning(self, "フォルダ", "変更するフォルダの場所（保存先かNAS）が設定されていません。「1. 取り込み」画面で指定してください。")
            return
        s = self.ctx.settings
        saved = [list(c) for c in conds]
        changed = False
        if saved != s.rename_filters:
            s.rename_filters = saved
            changed = True
        mapping = build_mapping(self.fieldnote, conds, s, season_folder=self._folder())
        check_text = ""
        if self.install_chk.isChecked():
            if self.install_fn is None:
                self.load_install()
            iconds = self.install_conds.conditions()
            isaved = [list(c) for c in iconds]
            if isaved != s.install_filters:
                s.install_filters = isaved
                changed = True
            if self.install_fn is not None:
                res = cross_check_installation(mapping, self.install_fn, iconds, s)
                check_text = res.summary
            else:
                mapping.issues.append("設置記録を読み込めていないので照合していません")
        if changed:
            self.ctx.save()
        plan = R.build_plan(root, nas_root, mapping, self.ctx.ledger)
        self.plan = plan
        self._show_plan(plan, mapping.condition_text, check_text)
        self.apply_btn.setEnabled(plan.count(R.A_RENAME) > 0)

    def _show_plan(self, plan: R.RenamePlan, cond_text: str = "", check_text: str = "") -> None:
        order = {R.A_RENAME: 0, R.A_BLOCKED: 1, R.A_TARGET_EXISTS: 2, R.A_SYNCING: 3, R.A_MISSING: 4,
                 R.A_UNMAPPED: 5, R.A_ALREADY: 6}
        items = sorted(plan.items, key=lambda i: (order.get(i.action, 9), 0 if i.warnings else 1, i.dst_dir_name or i.src_dir_name))
        self.table.setRowCount(len(items))
        for r, it in enumerate(items):
            color = {R.A_RENAME: COLORS["ok"], R.A_ALREADY: COLORS["muted"], R.A_MISSING: COLORS["warn"],
                     R.A_UNMAPPED: COLORS["warn"], R.A_BLOCKED: COLORS["err"],
                     R.A_TARGET_EXISTS: COLORS["err"], R.A_SYNCING: COLORS["info"]}.get(it.action)
            if it.action == R.A_RENAME and it.warnings:
                color = COLORS["warn"]
            e = it.entry
            note = "; ".join(filter(None, [it.reason] + it.warnings))
            label = R.ACTION_LABELS.get(it.action, it.action)
            if it.action == R.A_RENAME and it.warnings:
                label += "（注意あり）"
            set_row(self.table, r, [label, it.card_id, it.src_dir_name,
                                    it.dst_dir_name, it.where, str(e.row_no) if e else "", e.date if e else "",
                                    e.state if e else "", note], color)
        lines = [f"使う行: {cond_text}" if cond_text else "",
                 f"変更するフォルダ: {plan.season_dir}" + (f"　／　NAS: {plan.nas_season_dir}" if plan.nas_season_dir else ""),
                 f"変更 {plan.count(R.A_RENAME)} / 保留 {plan.count(R.A_BLOCKED)} / 変更先が既存 {plan.count(R.A_TARGET_EXISTS)}"
                 f" / NAS転送中 {plan.count(R.A_SYNCING)} / フォルダ無し {plan.count(R.A_MISSING)}"
                 f" / 野帳に無い {plan.count(R.A_UNMAPPED)} / 変更済み {plan.count(R.A_ALREADY)}"]
        if check_text:
            lines.append(check_text)
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
            msg += f"\nそのうち {warn} 個には注意が付いています（設置記録との不一致など）。表の「理由・注意」を確かめましたか？"
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
