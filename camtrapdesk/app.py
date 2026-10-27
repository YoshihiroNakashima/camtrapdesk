"""アプリの起動。"""

from __future__ import annotations

import os
import sys
import traceback

from PySide6.QtWidgets import QApplication, QMessageBox

from . import APP_NAME
from .config import config_dir
from .ui.context import AppContext
from .ui.main_window import MainWindow


def _install_crash_hook() -> None:
    """未処理の例外をログに残し、ダイアログで知らせる（pythonw で起動するとコンソールが無いため）。"""
    log_path = os.path.join(config_dir(), "crash.log")

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(text + "\n")
        except OSError:
            pass
        try:
            QMessageBox.critical(None, "camtrapdesk エラー",
                                 f"予期しないエラーが起きました。ログ: {log_path}\n\n{text[-1500:]}")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc, tb)

    sys.excepthook = hook


def run(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("camtrapdesk")
    _install_crash_hook()
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    ctx.start_background()
    return app.exec()
