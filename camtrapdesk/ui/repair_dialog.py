"""カードの修復（chkdsk）のダイアログ。挿さっているカードから選んで、管理者として 1 回でまとめて修復する。"""

from __future__ import annotations

import os
import sys
import threading

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QLabel, QListWidget, QListWidgetItem,
                               QMessageBox, QPlainTextEdit, QVBoxLayout, QWidget)

from ..config import config_dir
from ..core import ingest as I
from ..core.repair import run_repair, summarize_repair_log
from .context import AppContext


def _card_rows(ctx: AppContext) -> list[tuple[object, str, bool]]:
    """(ボリューム, 表示, 既定で選ぶか)。壊れたファイルのあったカード、エラーのカード、読めないボリュームは既定で選ぶ。"""
    rows: list[tuple[object, str, bool]] = []
    busy = (I.S_QUEUED, I.S_WAITING, I.S_COPYING, I.S_VERIFYING)
    for job in sorted(ctx.ingest.jobs.values(), key=lambda j: j.card.volume.display):
        if job.state in busy:
            continue
        v = job.card.volume
        pick = job.n_broken > 0 or job.state == I.S_ERROR
        note = f"壊れたファイル {job.n_broken} 件" if job.n_broken else ("エラー" if job.state == I.S_ERROR else "")
        rows.append((v, f"{v.display}　ID {job.card.card_id}　{note}".rstrip("　"), pick))
    for v, reason in sorted(ctx.ingest.others.values(), key=lambda o: o[0].display):
        pick = v.kind == "unreadable"
        rows.append((v, f"{v.display}　{v.label or 'ラベル無し'}　{'読めません' if pick else 'IDなし'}", pick))
    return rows


class RepairDialog(QDialog):
    def __init__(self, ctx: AppContext, parent: QWidget | None = None, preselect: set[str] | None = None):
        super().__init__(parent)
        self.ctx = ctx
        self.setWindowTitle("カードを修復（chkdsk）")
        self.resize(640, 520)
        lay = QVBoxLayout(self)
        info = QLabel("選んだカードを Windows の chkdsk /f でまとめて修復します（壊れたファイルの切り詰め、失われた領域の回収）。"
                      "管理者の許可を求める画面（UAC）が 1 回出るので「はい」を押してください。取り込みが終わっていないカードは選べません。"
                      "修復中はカードの走査を止めます。修復で復元された断片はカードのルートに FILE0000.CHK などの名前で残ることがあります"
                      "（不要なら消してよい）。")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.list = QListWidget()
        for v, text, pick in _card_rows(ctx):
            it = QListWidgetItem(text)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            on = pick if preselect is None else (v.key in preselect)
            it.setCheckState(Qt.CheckState.Checked if on else Qt.CheckState.Unchecked)
            it.setData(Qt.ItemDataRole.UserRole, v)
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setPlaceholderText("chkdsk の結果がここに出ます")
        lay.addWidget(self.log, 1)
        self.btns = QDialogButtonBox()
        self.run_btn = self.btns.addButton("選んだカードを修復", QDialogButtonBox.ButtonRole.AcceptRole)
        self.close_btn = self.btns.addButton("閉じる", QDialogButtonBox.ButtonRole.RejectRole)
        self.run_btn.clicked.connect(self.run)
        self.close_btn.clicked.connect(self.reject)
        lay.addWidget(self.btns)
        self._result: tuple[bool, str] | None = None
        if sys.platform != "win32":
            self.run_btn.setEnabled(False)
            self.log.setPlainText("カードの修復（chkdsk）は Windows でだけ行えます。Mac では「ディスクユーティリティ」の First Aid を使ってください。")

    def selected(self) -> list:
        out = []
        for i in range(self.list.count()):
            it = self.list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def run(self) -> None:
        vols = self.selected()
        if not vols:
            QMessageBox.information(self, "カードを修復", "修復するカードを選んでください。")
            return
        self.run_btn.setEnabled(False)
        self.close_btn.setEnabled(False)
        self.log.setPlainText("修復を始めます。管理者の許可を求める画面（UAC）が出たら「はい」を押してください…")
        self.ctx.ingest.scan_paused = True
        work_dir = os.path.join(str(config_dir()), "repair")

        def work() -> None:
            self._result = run_repair(vols, work_dir, on_log=self.ctx.log_message.emit)

        threading.Thread(target=work, name="repair", daemon=True).start()
        timer = QTimer(self)

        def tick() -> None:
            if self._result is None:
                return
            timer.stop()
            self.ctx.ingest.scan_paused = False
            ok, log = self._result
            self._result = None
            summary = summarize_repair_log(log) if ok else ""
            self.log.setPlainText(("修復が終わりました。\n\n" + (summary + "\n\n---- chkdsk の出力 ----\n" if summary else "") + log)
                                  if ok else ("修復できませんでした: " + log))
            self.ctx.log_message.emit("カードの修復" + ("が終わりました: " + ", ".join(v.display for v in vols) if ok else "は行えませんでした: " + log.splitlines()[0]))
            self.ctx.ledger.event("repair", ("ok: " if ok else "failed: ") + ", ".join(f"{v.display}({v.label})" for v in vols))
            self.run_btn.setEnabled(True)
            self.close_btn.setEnabled(True)
            self.ctx.ingest.rescan_now()

        timer.timeout.connect(tick)
        timer.start(500)
