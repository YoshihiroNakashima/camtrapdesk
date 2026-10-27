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

@dataclass
class VolumeDiag:
    """診断用: Windows が列挙した全ボリュームの生の情報。"""
    guid: str
    drive_type: int
    mounts: list[str]
    label: str
    fs: str
    info_ok: bool
    error: int
    included: bool
    reason: str


def _windows_enumerate() -> list[VolumeDiag]:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
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
    DRIVE_REMOTE, DRIVE_CDROM = 4, 5
    SEM_FAILCRITICALERRORS = 0x0001   # "ディスクを挿入してください" のダイアログを出さない
    old_mode = k32.SetErrorMode(SEM_FAILCRITICALERRORS)
    system_drive = os.environ.get("SystemDrive", "C:").upper()
    out: list[VolumeDiag] = []
    buf = ctypes.create_unicode_buffer(1024)
    h = k32.FindFirstVolumeW(buf, len(buf))
    if h == INVALID_HANDLE_VALUE or h is None:
        k32.SetErrorMode(old_mode)
        return out
    try:
        while True:
            guid_path = buf.value  # 例: \\?\Volume{...}\
            dtype = k32.GetDriveTypeW(guid_path)
            names = ctypes.create_unicode_buffer(8192)
            retlen = wintypes.DWORD(0)
            mounts: list[str] = []
            if k32.GetVolumePathNamesForVolumeNameW(guid_path, names, len(names), ctypes.byref(retlen)):
                mounts = [m for m in names[: retlen.value].split("\x00") if m]
            label_buf = ctypes.create_unicode_buffer(261)
            fs_buf = ctypes.create_unicode_buffer(261)
            serial = wintypes.DWORD(0)
            maxcomp = wintypes.DWORD(0)
            flags = wintypes.DWORD(0)
            ok = bool(k32.GetVolumeInformationW(guid_path, label_buf, 261, ctypes.byref(serial),
                                                ctypes.byref(maxcomp), ctypes.byref(flags), fs_buf, 261))
            err = 0 if ok else ctypes.get_last_error()
            is_system = any(m.upper().startswith(system_drive + "\\") for m in mounts)
            if dtype in (DRIVE_REMOTE, DRIVE_CDROM):
                included, reason = False, "ネットワーク/光学ドライブ"
            elif is_system:
                included, reason = False, "システムドライブ"
            elif not ok:
                included, reason = False, f"ボリューム情報を取得できません (Win32 error {err})"
            else:
                included, reason = True, ""
            out.append(VolumeDiag(guid=guid_path, drive_type=dtype, mounts=mounts, label=label_buf.value,
                                  fs=fs_buf.value, info_ok=ok, error=err, included=included, reason=reason))
            if not k32.FindNextVolumeW(h, buf, len(buf)):
                break
    finally:
        k32.FindVolumeClose(h)
        k32.SetErrorMode(old_mode)
    return out


def _windows_volumes() -> list[Volume]:
    DRIVE_REMOVABLE, DRIVE_FIXED = 2, 3
    results: list[Volume] = []
    for d in _windows_enumerate():
        if not d.included:
            continue
        display = d.mounts[0].rstrip("\\") if d.mounts else "(レター無し)"
        kind = "removable" if d.drive_type == DRIVE_REMOVABLE else ("fixed" if d.drive_type == DRIVE_FIXED else "unknown")
        # アクセスはレターがあればレター経由（エクスプローラーと同じ見え方）、無ければGUIDパス
        root = d.mounts[0] if d.mounts else d.guid
        results.append(Volume(key=d.guid, root=root, display=display, label=d.label, kind=kind))
    return results


def diagnostics(scan_roots: Iterable[str] = (), exclude: Iterable[str] = (), id_file_name: str = "id.txt") -> str:
    """全ボリュームの状態を人が読める形で返す（取り込み画面の「診断」）。"""
    lines: list[str] = []
    ex = {os.path.normcase(os.path.normpath(e)) for e in exclude if e}
    if sys.platform == "win32":
        diags = _windows_enumerate()
        lines.append(f"Windows が列挙したボリューム: {len(diags)} 個")
        for d in diags:
            mounts = ", ".join(m.rstrip("\\") for m in d.mounts) if d.mounts else "(レター無し)"
            state = "対象" if d.included else f"対象外: {d.reason}"
            root = d.mounts[0] if d.mounts else d.guid
            if os.path.normcase(os.path.normpath(root)) in ex or d.guid in ex:
                state = "対象外: 除外設定（保存先/NAS/除外ボリューム）"
            idtxt = ""
            if d.included:
                p = os.path.join(root, id_file_name)
                try:
                    with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
                        first = f.readline().strip()
                    idtxt = f"{id_file_name}='{first}'" if first else f"{id_file_name} は空"
                except FileNotFoundError:
                    idtxt = f"{id_file_name} なし"
                except OSError as e:
                    idtxt = f"{id_file_name} 読み取りエラー: {e}"
            lines.append(f"  {mounts:14s} {d.label:16s} type={d.drive_type} fs={d.fs:6s} {state} {idtxt}  {d.guid}")
    else:
        for v in list_volumes(scan_roots, exclude):
            lines.append(f"  {v.display:14s} {v.label:16s} {v.kind} {v.root}")
    return "\n".join(lines)


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
