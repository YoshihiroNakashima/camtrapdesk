"""小さな共通ウィジェットと書式ヘルパー。"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QFileDialog, QHBoxLayout, QLineEdit, QPushButton, QTableWidget,
                               QTableWidgetItem, QWidget, QHeaderView)


def natural_key(text: str) -> tuple:
    """「F:」「NO NAME」「vol_12」などを、数字は数の大小で並べるためのキー。"""
    import re
    return tuple((0, int(x)) if x.isdigit() else (1, x.lower()) for x in re.split(r"(\d+)", text or "") if x)


def volume_sort_key(volume) -> tuple:
    """ボリュームの表示名（ドライブ文字など）で並べる。"""
    return natural_key(getattr(volume, "display", "") or "")


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} TB"


class PathRow(QWidget):
    """パス入力欄＋「参照」ボタン。入力を終えた（Enter・フォーカスが外れた・参照で選んだ）ときに changed を出す。"""

    changed = Signal(str)

    def __init__(self, text: str = "", mode: str = "dir", parent=None, placeholder: str = "",
                 file_filter: str = "Excel (*.xlsx *.xlsm);;すべて (*)"):
        super().__init__(parent)
        self.mode = mode
        self.file_filter = file_filter
        self._last = (text or "").strip()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(text)
        self.edit.setPlaceholderText(placeholder)
        self.edit.editingFinished.connect(self._maybe_changed)
        btn = QPushButton("参照…")
        btn.clicked.connect(self.browse)
        lay.addWidget(self.edit, 1)
        lay.addWidget(btn)

    def text(self) -> str:
        return self.edit.text().strip()

    def setText(self, t: str) -> None:
        self._last = (t or "").strip()
        self.edit.setText(t)

    def _maybe_changed(self) -> None:
        t = self.text()
        if t != self._last:
            self._last = t
            self.changed.emit(t)

    def browse(self) -> None:
        if self.mode == "dir":
            p = QFileDialog.getExistingDirectory(self, "フォルダを選択", self.text() or "")
        else:
            p, _ = QFileDialog.getOpenFileName(self, "ファイルを選択", self.text() or "", self.file_filter)
        if p:
            self.edit.setText(p)
            self._maybe_changed()


COLORS = {
    "ok": QColor(214, 243, 220),
    "warn": QColor(255, 243, 205),
    "err": QColor(252, 219, 219),
    "info": QColor(222, 235, 250),
    "muted": QColor(235, 235, 235),
}


def make_table(headers: list[str], stretch_last: bool = True) -> QTableWidget:
    t = QTableWidget(0, len(headers))
    t.setHorizontalHeaderLabels(headers)
    t.verticalHeader().setVisible(False)
    t.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    t.setAlternatingRowColors(True)
    h = t.horizontalHeader()
    h.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    if stretch_last:
        h.setStretchLastSection(True)
    return t


def set_row(t: QTableWidget, row: int, values: list[str], color: QColor | None = None) -> None:
    for col, v in enumerate(values):
        item = QTableWidgetItem(str(v))
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if color is not None:
            item.setBackground(color)
        t.setItem(row, col, item)
