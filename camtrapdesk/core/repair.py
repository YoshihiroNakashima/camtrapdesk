"""壊れた SD カードの扱い（Windows）。

1. 「ドライブをスキャンして修復しますか？」のダイアログを自動で「スキャンせずに続行」にする（ScanDialogDismisser）。
   このダイアログはファイルの読み書きを止めないので、閉じてしまえば取り込みはそのまま進む。
2. 取り込みが終わったあとで、壊れていたカードを chkdsk /f で修復する（run_repair）。
   chkdsk は管理者権限が要るので、1 回の UAC（「許可しますか」）で複数枚をまとめて直す。

「ファイルまたはディレクトリが壊れているため、読み取ることができません」（WinError 1392）は、ダイアログの
せいではなく、そのファイル（やフォルダ）の FAT 上のつながりが壊れていて読めないときに出る。取り込みでは
そのファイルだけ飛ばして残りを取り込み（ingest）、あとで chkdsk をかけるとカードが再び正常に使える。
"""

from __future__ import annotations

import os
import re
import sys
import threading
import time
from typing import Callable, Iterable

from .volumes import Volume

# ダイアログの文言（日本語と英語の Windows）
DIALOG_MARKERS = ("スキャンして修復", "scan and fix", "scan and repair")
CONTINUE_MARKERS = ("スキャンせずに続行", "continue without scanning")

_WINERROR_CORRUPT = {23, 1117, 1392, 6069}   # CRC / I/O デバイスエラー / ファイルまたはディレクトリが壊れている / 壊れたボリューム


def is_corruption_error(e: BaseException) -> bool:
    """カード側の破損が原因の OSError か。"""
    import errno
    if not isinstance(e, OSError):
        return False
    if getattr(e, "winerror", None) in _WINERROR_CORRUPT:
        return True
    return e.errno == errno.EIO


# ----------------------------------------------------------------------------
# 「スキャンして修復しますか」ダイアログを自動で閉じる
# ----------------------------------------------------------------------------

def _find_scan_dialogs() -> list[tuple[int, int, str]]:
    """開いている「スキャンして修復」ダイアログの (ダイアログ hwnd, 「スキャンせずに続行」ボタン hwnd, タイトル) を返す。"""
    if sys.platform != "win32":
        return []
    import ctypes
    from ctypes import wintypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def text_of(h: int) -> str:
        n = u32.GetWindowTextLengthW(h)
        buf = ctypes.create_unicode_buffer(n + 1)
        u32.GetWindowTextW(h, buf, n + 1)
        return buf.value

    def class_of(h: int) -> str:
        buf = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(h, buf, 256)
        return buf.value

    def children(h: int) -> list[int]:
        out: list[int] = []

        def cb(ch, _lp):
            out.append(ch)
            return True

        u32.EnumChildWindows(h, EnumWindowsProc(cb), 0)
        return out

    found: list[tuple[int, int, str]] = []

    def top(h, _lp):
        if not u32.IsWindowVisible(h) or class_of(h) != "#32770":
            return True
        kids = children(h)
        texts = [(k, text_of(k).strip()) for k in kids]
        blob = " ".join(t for _k, t in texts).lower()
        if not any(m.lower() in blob for m in DIALOG_MARKERS):
            return True
        for k, t in texts:
            tl = t.lower()
            if class_of(k) == "Button" and any(m.lower() in tl for m in CONTINUE_MARKERS):
                found.append((h, k, text_of(h)))
                break
        return True

    u32.EnumWindows(EnumWindowsProc(top), 0)
    return found


def dismiss_scan_dialogs() -> int:
    """見つけたダイアログで「スキャンせずに続行」を押す。押した数を返す。"""
    if sys.platform != "win32":
        return 0
    import ctypes
    u32 = ctypes.WinDLL("user32", use_last_error=True)
    BM_CLICK = 0x00F5
    n = 0
    for _dlg, btn, _title in _find_scan_dialogs():
        u32.SendMessageW(btn, BM_CLICK, 0, 0)
        n += 1
    return n


class ScanDialogDismisser:
    """バックグラウンドで 1 秒ごとにダイアログを探し、「スキャンせずに続行」を押す（Windows のみ）。"""

    def __init__(self, on_log: Callable[[str], None] | None = None, interval: float = 1.0):
        self.on_log = on_log
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.enabled = True
        self.n_dismissed = 0

    def start(self) -> None:
        if sys.platform != "win32" or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="scan-dialog", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self.enabled:
                try:
                    n = dismiss_scan_dialogs()
                except Exception:   # pragma: no cover - Windows API の都合で失敗しても取り込みは止めない
                    n = 0
                if n:
                    self.n_dismissed += n
                    if self.on_log:
                        self.on_log(f"Windows の「スキャンして修復しますか」を {n} 件、「スキャンせずに続行」で閉じました"
                                    "（壊れたファイルは取り込み後に取り除き、必要ならカードを修復します）")
            self._stop.wait(self.interval)


# ----------------------------------------------------------------------------
# chkdsk による修復
# ----------------------------------------------------------------------------

def chkdsk_target(volume: Volume) -> str:
    """chkdsk に渡すボリューム名。ドライブ文字があればそれ、無ければボリューム GUID パス。"""
    d = (volume.display or "").strip()
    if re.fullmatch(r"[A-Za-z]:", d):
        return d.upper()
    return volume.key


def build_repair_script(targets: Iterable[str], log_path: str, done_path: str) -> str:
    """複数のボリュームを順に chkdsk /f /x する cmd スクリプト。管理者として 1 回だけ起動する。
    FAT の「失われたチェーンをファイルに変換しますか」には y と答える（復元したファイルはルートの FILE0000.CHK になる）。"""
    lines = ["@echo off", "chcp 65001 > nul", f'echo camtrapdesk repair %date% %time% > "{log_path}"']
    for t in targets:
        lines.append(f'echo. >> "{log_path}"')
        lines.append(f'echo === chkdsk {t} === >> "{log_path}"')
        lines.append(f'echo y| chkdsk "{t}" /f /x >> "{log_path}" 2>&1')
        lines.append(f'echo [exit %errorlevel%] >> "{log_path}"')
    lines.append(f'echo done > "{done_path}"')
    return "\r\n".join(lines) + "\r\n"


def _run_elevated_and_wait(script_path: str, done_path: str, timeout: float) -> tuple[bool, str]:
    """cmd スクリプトを管理者として実行し、終わるまで待つ。UAC を断られたら (False, 理由)。"""
    import ctypes
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", wintypes.ULONG), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIcon", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    SEE_MASK_NOCLOSEPROCESS = 0x40
    SW_SHOWMINNOACTIVE = 7
    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = SEE_MASK_NOCLOSEPROCESS
    info.lpVerb = "runas"
    info.lpFile = "cmd.exe"
    info.lpParameters = f'/c "{script_path}"'
    info.nShow = SW_SHOWMINNOACTIVE
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    if not shell32.ShellExecuteExW(ctypes.byref(info)):
        err = ctypes.get_last_error()
        if err == 1223:   # ERROR_CANCELLED: UAC で「いいえ」
            return False, "管理者としての実行が許可されなかったので、修復していません"
        return False, f"修復を始められません（Win32 error {err}）"
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    t0 = time.time()
    try:
        if info.hProcess:
            while time.time() - t0 < timeout:
                if k32.WaitForSingleObject(info.hProcess, 1000) == 0:
                    break
            else:
                return False, "修復が時間内に終わりませんでした（chkdsk のウィンドウを確認してください）"
        else:
            while time.time() - t0 < timeout and not os.path.isfile(done_path):
                time.sleep(1.0)
    finally:
        if info.hProcess:
            k32.CloseHandle(info.hProcess)
    return True, ""


def run_repair(volumes: list[Volume], work_dir: str, on_log: Callable[[str], None] | None = None,
               timeout: float = 3600.0) -> tuple[bool, str]:
    """指定したカードをまとめて chkdsk /f で修復する（Windows のみ、UAC が 1 回出る）。戻り値は (成功, ログ)。"""
    if sys.platform != "win32":
        return False, "カードの修復（chkdsk）は Windows でだけ行えます。Mac では「ディスクユーティリティ」の First Aid を使ってください"
    if not volumes:
        return False, "修復するカードがありません"
    os.makedirs(work_dir, exist_ok=True)
    log_path = os.path.join(work_dir, "repair.log")
    done_path = os.path.join(work_dir, "repair.done")
    script_path = os.path.join(work_dir, "repair.cmd")
    for p in (log_path, done_path):
        try:
            os.remove(p)
        except OSError:
            pass
    targets = [chkdsk_target(v) for v in volumes]
    with open(script_path, "w", encoding="utf-8", newline="") as f:
        f.write(build_repair_script(targets, log_path, done_path))
    if on_log:
        on_log("カードの修復を始めます: " + ", ".join(f"{v.display}（{v.label or 'ラベル無し'}）" for v in volumes)
               + "。管理者の許可を求める画面（UAC）が出たら「はい」を押してください")
    ok, why = _run_elevated_and_wait(script_path, done_path, timeout)
    log = ""
    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as f:
            log = f.read()
    except OSError:
        pass
    if not ok:
        return False, why + ("\n\n" + log if log else "")
    if not os.path.isfile(done_path):
        return False, "修復のスクリプトが最後まで走りませんでした\n\n" + log
    return True, log or "（chkdsk の出力はありません）"


def summarize_repair_log(log: str) -> str:
    """chkdsk のログから、ボリュームごとの結果の要点だけを取り出す。"""
    out: list[str] = []
    cur = ""
    for line in log.splitlines():
        m = re.match(r"=== chkdsk (.+) ===", line.strip())
        if m:
            cur = m.group(1)
            continue
        s = line.strip()
        if not s:
            continue
        if re.search(r"エラー|修正|修復|problem|error|fixed|corrected|lost|失われ|converted|\[exit", s, re.IGNORECASE):
            out.append(f"{cur}: {s}" if cur else s)
    return "\n".join(out[:80])
