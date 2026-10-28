"""PyInstaller 用の入口。`pyinstaller tools/entry.py` でビルドする（build.yml 参照）。"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from camtrapdesk.__main__ import main  # noqa: E402

if __name__ == "__main__":
    main()
