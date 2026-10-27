"""ボリュームラベル（カードの名前）を「接頭辞＋カードID」に変更する（任意機能）。"""

from __future__ import annotations

import re
import subprocess
import sys

from .volumes import Volume


def proposed_label(prefix: str, card_id: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", card_id)
    return (prefix + cleaned).upper()


def label_too_long(label: str) -> bool:
    return len(label) > 11   # FAT32 の上限


def set_volume_label(volume: Volume, label: str) -> tuple[bool, str]:
    if label_too_long(label):
        return False, f"'{label}' は11文字を超えています（FAT32の制限）"
    if volume.kind == "virtual":
        return False, "仮想ボリュームの名前は変更できません"
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            root = volume.root if volume.root.endswith("\\") else volume.root + "\\"
            if not k32.SetVolumeLabelW(root, label):
                err = ctypes.get_last_error() or ctypes.GetLastError()
                return False, f"SetVolumeLabel 失敗 (code {err})"
            return True, "変更しました"
        if sys.platform == "darwin":
            r = subprocess.run(["diskutil", "rename", volume.root.rstrip("/"), label],
                               capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                return False, (r.stderr or r.stdout).strip()
            return True, "変更しました"
        return False, "このOSでは未対応です"
    except Exception as e:  # pragma: no cover
        return False, str(e)
