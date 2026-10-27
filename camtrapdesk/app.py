"""アプリの起動。"""

from __future__ import annotations

import sys

from PySide6.QtWidgets import QApplication

from . import APP_NAME
from .ui.context import AppContext
from .ui.main_window import MainWindow


def run(argv: list[str] | None = None) -> int:
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setOrganizationName("camtrapdesk")
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    ctx.start_background()
    return app.exec()
