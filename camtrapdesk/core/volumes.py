"""接続中のボリューム（SDカード候補）を列挙する。

Windows ではドライブレターに依存せず、ボリュームGUIDパス（``\\\\?\\Volume{...}\\``）で
列挙・アクセスする。26文字を超えて文字が割り当てられないカードも扱える。
macOS は /Volumes、Linux は /media, /run/media, /mnt を見る。
加えて settings.scan_roots に指定したフォルダの直下のサブフォルダを
「仮想ボリューム」として扱う（テストや、カードを手動でマウントした場合用）。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Volume:
    key: str          # 安定した識別子（Windows: GUIDパス、他: マウントパス）
    root: str         # ファイルアクセスに使うルートパス（末尾に区切り文字を含む）
    display: str      # 画面表示用（例: "F:" / "NO NAME" / フォルダ名）
    label: str        # ボリュームラベル
    kind: str         # "removable" / "fixed" / "virtual" / "unknown"

    def join(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)


# ----------------------------------------------------------------------------
# Windows
# ----------------------------------------------------------------------------

def _windows_volumes() -> list[Volume]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k32.FindFirstVolumeW.restype = wintypes.HANDLE
    k32.FindFirstVolumeW.argtypes = [wintypes.LPWSTR, wintypes.DWORD]
    k32.FindNextVolumeW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD]
    k32.FindVolumeClose.argtypes = [wintypes.HANDLE]
    k32.GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    k32.GetDriveTypeW.restype = wintypes.UINT
    k32.GetVolumePathNamesForVolumeNameW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k32.GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD), wintypes.LPWSTR, wintypes.DWORD]

    INVALID_HANDLE_VALUE = wintypes.HANDLE(-1).value
    DRIVE_REMOVABLE, DRIVE_FIXED, DRIVE_REMOTE, DRIVE_CDROM = 2, 3, 4, 5
    # エラーダイアログ（"ディスクを挿入してください"）を出さない
    SEM_FAILCRITICALERRORS = 0x0001
    old_mode = k32.SetErrorMode(SEM_FAILCRITICALERRORS)

    system_drive = os.environ.get("SystemDrive", "C:").upper()
    results: list[Volume] = []
    buf = ctypes.create_unicode_buffer(1024)
    h = k32.FindFirstVolumeW(buf, len(buf))
    if h == INVALID_HANDLE_VALUE or h is None:
        k32.SetErrorMode(old_mode)
        return results
    try:
        while True:
            guid_path = buf.value  # 例: \\?\Volume{...}\
            dtype = k32.GetDriveTypeW(guid_path)
            if dtype in (DRIVE_REMOTE, DRIVE_CDROM):
                pass
            else:
                # マウントポイント（ドライブレター or フォルダ）
                names = ctypes.create_unicode_buffer(4096)
                retlen = wintypes.DWORD(0)
                mounts: list[str] = []
                if k32.GetVolumePathNamesForVolumeNameW(guid_path, names, len(names), ctypes.byref(retlen)):
                    raw = names[: retlen.value]
                    mounts = [m for m in raw.split("\x00") if m]
                is_system = any(m.upper().startswith(system_drive + "\\") for m in mounts)
                # ラベルとファイルシステム
                label_buf = ctypes.create_unicode_buffer(261)
                fs_buf = ctypes.create_unicode_buffer(261)
                serial = wintypes.DWORD(0)
                maxcomp = wintypes.DWORD(0)
                flags = wintypes.DWORD(0)
                ok = k32.GetVolumeInformationW(guid_path, label_buf, 261, ctypes.byref(serial),
                                               ctypes.byref(maxcomp), ctypes.byref(flags), fs_buf, 261)
                if ok and not is_system:
                    label = label_buf.value
                    display = mounts[0].rstrip("\\") if mounts else "(レター無し)"
                    kind = "removable" if dtype == DRIVE_REMOVABLE else ("fixed" if dtype == DRIVE_FIXED else "unknown")
                    # アクセスはレターがあればレター経由（エクスプローラーと同じ見え方）、無ければGUIDパス
                    root = mounts[0] if mounts else guid_path
                    results.append(Volume(key=guid_path, root=root, display=display, label=label, kind=kind))
            if not k32.FindNextVolumeW(h, buf, len(buf)):
                break
    finally:
        k32.FindVolumeClose(h)
        k32.SetErrorMode(old_mode)
    return results


# ----------------------------------------------------------------------------
# macOS / Linux
# ----------------------------------------------------------------------------

def _dir_volumes(base_dirs: Iterable[str], kind: str) -> list[Volume]:
    out: list[Volume] = []
    for base in base_dirs:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for name in entries:
            p = os.path.join(base, name)
            if not os.path.isdir(p):
                continue
            if kind != "virtual" and not os.path.ismount(p):
                continue
            real = os.path.realpath(p)
            if real == "/":
                continue  # 起動ボリュームへのシンボリックリンク
            out.append(Volume(key=p, root=p.rstrip(os.sep) + os.sep, display=name, label=name, kind=kind))
    return out


def _macos_volumes() -> list[Volume]:
    return _dir_volumes(["/Volumes"], "removable")


def _linux_volumes() -> list[Volume]:
    user = os.environ.get("USER", "")
    bases = ["/media", f"/media/{user}", f"/run/media/{user}", "/mnt"]
    vols = _dir_volumes(bases, "removable")
    # /media/<user>/xxx は /media の走査で /media/<user> 自体がマウントでないため拾われない。上のリストで対応。
    seen = set()
    uniq = []
    for v in vols:
        if v.key not in seen:
            seen.add(v.key)
            uniq.append(v)
    return uniq


def list_volumes(scan_roots: Iterable[str] = (), exclude: Iterable[str] = ()) -> list[Volume]:
    """接続中のボリューム候補を返す。scan_roots の直下サブフォルダは仮想ボリュームとして追加する。"""
    if sys.platform == "win32":
        vols = _windows_volumes()
    elif sys.platform == "darwin":
        vols = _macos_volumes()
    else:
        vols = _linux_volumes()
    vols += _dir_volumes(list(scan_roots), "virtual")
    ex = {os.path.normcase(os.path.normpath(e)) for e in exclude if e}
    return [v for v in vols if os.path.normcase(os.path.normpath(v.root)) not in ex and v.key not in ex]
