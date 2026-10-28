"""ffmpeg の探索・エンコーダの自動選択・動画の変換（AVI などを H.264 の mp4 に）。

GPU は必須ではない。NVIDIA（nvenc）、Intel 内蔵（qsv）、AMD（amf）、Mac（videotoolbox）の
ハードウェアエンコーダがあれば使い、無ければ CPU（libx264）で変換する。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass

# 候補のエンコーダ（上から順に試す）
HW_ENCODERS = ["h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"]
CPU_ENCODER = "libx264"

_lock = threading.Lock()
_ffmpeg_cache: dict[str, str | None] = {}
_encoder_cache: dict[str, str] = {}


def _creationflags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def ffmpeg_candidates(configured: str = "") -> list[str]:
    """探す順: 設定 → アプリのフォルダ（ffmpeg.exe、tools/ffmpeg.exe、tools/ffmpeg/bin/ffmpeg.exe）→ imageio-ffmpeg 同梱 → PATH。"""
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    cands: list[str] = []
    if configured:
        cands.append(configured)
        if os.path.isdir(configured):
            cands.append(os.path.join(configured, exe))
            cands.append(os.path.join(configured, "bin", exe))
    if getattr(sys, "frozen", False):
        bases = [os.path.dirname(sys.executable), getattr(sys, "_MEIPASS", "")]
    else:
        bases = [os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))]
    for base in [b for b in bases if b]:
        cands.append(os.path.join(base, exe))
        cands.append(os.path.join(base, "tools", exe))
        cands.append(os.path.join(base, "tools", "ffmpeg", exe))
        cands.append(os.path.join(base, "tools", "ffmpeg", "bin", exe))
    try:
        import imageio_ffmpeg  # type: ignore
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception as e:  # 同梱バイナリが無い環境など
        cands.append(f"(imageio-ffmpeg: {e})")
    found = shutil.which("ffmpeg")
    cands.append(found or "(PATH に ffmpeg なし)")
    return cands


_works_cache: dict[str, bool] = {}


def ffmpeg_works(path: str) -> bool:
    """その ffmpeg が実際に起動するか（途中で切れたダウンロードなど、壊れたファイルを除くため）。"""
    with _lock:
        if path in _works_cache:
            return _works_cache[path]
    ok = False
    try:
        if os.path.isfile(path):
            r = _run([path, "-version"], timeout=30)
            ok = r.returncode == 0 and "ffmpeg" in (r.stdout or "").lower()
    except (OSError, subprocess.TimeoutExpired, ValueError):
        ok = False
    with _lock:
        _works_cache[path] = ok
    return ok


def forget_ffmpeg() -> None:
    """ffmpeg を置き直した後に、探索結果を忘れて探し直させる。"""
    with _lock:
        _ffmpeg_cache.clear()
        _works_cache.clear()
        _encoder_cache.clear()
        _encoder_report.clear()


def find_ffmpeg(configured: str = "") -> str | None:
    """ffmpeg の実行ファイルを探す（見つからなければ None）。壊れたファイルは飛ばす。結果はプロセス内で覚える。"""
    key = configured or ""
    with _lock:
        if key in _ffmpeg_cache:
            return _ffmpeg_cache[key]
    result = None
    for c in ffmpeg_candidates(configured):
        if c and not c.startswith("(") and os.path.isfile(c) and ffmpeg_works(c):
            result = c
            break
    with _lock:
        if result is not None:
            _ffmpeg_cache[key] = result   # 見つからなかった場合は覚えない（後から置けば次の確認で見つかる）
    return result


def candidate_status(path: str) -> str:
    """確認画面用: 候補の状態を短く説明する。"""
    if not path or path.startswith("("):
        return path
    if not os.path.isfile(path):
        return f"{path}  … 無し"
    size = os.path.getsize(path)
    if ffmpeg_works(path):
        return f"{path}  … 使えます（{size/1e6:.0f} MB）"
    return f"{path}  … 壊れています（{size/1e6:.1f} MB。ダウンロードが途中で切れた可能性。置き直してください）"


# ----------------------------------------------------------------------------
# ダウンロード
# ----------------------------------------------------------------------------

FFMPEG_WIN_URL = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
FFMPEG_MAC_URL = "https://evermeet.cx/ffmpeg/getrelease/zip"


def app_tools_dir() -> str:
    """ffmpeg を置く場所: アプリのフォルダの tools（配布版は exe と同じ場所の tools）。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(sys.executable)
    else:
        base = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "tools")


def download_ffmpeg(dest_dir: str | None = None, on_progress=None, stop: "threading.Event | None" = None) -> tuple[bool, str]:
    """公式ビルドの ffmpeg を取ってきて dest_dir に置く。Windows は BtbN の GPL ビルド（libx264・nvenc・qsv・amf 入り）、
    Mac は evermeet.cx の静的ビルド。on_progress(受け取ったバイト数, 全体のバイト数または 0)。"""
    import tempfile
    import urllib.request
    import zipfile

    dest_dir = dest_dir or app_tools_dir()
    if sys.platform == "win32":
        url, members = FFMPEG_WIN_URL, {"ffmpeg.exe": "/bin/ffmpeg.exe", "ffprobe.exe": "/bin/ffprobe.exe"}
    elif sys.platform == "darwin":
        url, members = FFMPEG_MAC_URL, {"ffmpeg": "ffmpeg"}
    else:
        return False, "Linux では配布元のパッケージ（apt install ffmpeg など）を使ってください"
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        return False, f"置き場所を作れません: {dest_dir} ({e})"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip", dir=dest_dir)
    tmp_path = tmp.name
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "camtrapdesk"})
        with urllib.request.urlopen(req, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            while True:
                if stop is not None and stop.is_set():
                    return False, "中止しました"
                chunk = resp.read(1024 * 256)
                if not chunk:
                    break
                tmp.write(chunk)
                got += len(chunk)
                if on_progress:
                    on_progress(got, total)
        tmp.close()
        if total and got != total:
            return False, f"ダウンロードが途中で切れました（{got/1e6:.0f} / {total/1e6:.0f} MB）。もう一度試してください"
        with zipfile.ZipFile(tmp_path) as z:
            names = z.namelist()
            for out_name, suffix in members.items():
                hit = next((n for n in names if n.endswith(suffix)), None)
                if hit is None:
                    if out_name.startswith("ffprobe"):
                        continue
                    return False, f"取ってきたファイルに {out_name} がありません"
                out_path = os.path.join(dest_dir, out_name)
                with z.open(hit) as src, open(out_path + ".tmp", "wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
                os.replace(out_path + ".tmp", out_path)
                if sys.platform != "win32":
                    os.chmod(out_path, 0o755)
    except Exception as e:
        return False, f"ダウンロードに失敗しました: {e}"
    finally:
        try:
            tmp.close()
        except Exception:
            pass
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    forget_ffmpeg()
    exe = os.path.join(dest_dir, "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
    if not ffmpeg_works(exe):
        return False, f"置いた ffmpeg が起動しません: {exe}"
    return True, exe


def ffprobe_path(ffmpeg: str) -> str | None:
    d, name = os.path.split(ffmpeg)
    probe = os.path.join(d, name.replace("ffmpeg", "ffprobe"))
    if os.path.isfile(probe):
        return probe
    return shutil.which("ffprobe")


def _run(args: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, creationflags=_creationflags(),
                          encoding="utf-8", errors="replace")


@dataclass
class MediaInfo:
    duration: float      # 秒
    width: int = 0
    height: int = 0
    vcodec: str = ""
    has_audio: bool = False


def probe(ffmpeg: str, path: str) -> MediaInfo | None:
    """長さ・解像度・コーデックを調べる。ffprobe が無ければ ffmpeg の出力から長さだけ読む。"""
    fp = ffprobe_path(ffmpeg)
    if fp:
        r = _run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=120)
        if r.returncode == 0:
            try:
                data = json.loads(r.stdout)
                dur = float(data.get("format", {}).get("duration") or 0.0)
                info = MediaInfo(duration=dur)
                for st in data.get("streams", []):
                    if st.get("codec_type") == "video" and not info.vcodec:
                        info.vcodec = st.get("codec_name", "")
                        info.width = int(st.get("width") or 0)
                        info.height = int(st.get("height") or 0)
                        if not dur and st.get("duration"):
                            info.duration = float(st["duration"])
                    elif st.get("codec_type") == "audio":
                        info.has_audio = True
                return info
            except (ValueError, TypeError):
                return None
        return None
    # ffprobe 無し: ffmpeg -i の stderr から Duration を読む
    r = _run([ffmpeg, "-hide_banner", "-i", path], timeout=120)
    import re
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
    if not m:
        return None
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return MediaInfo(duration=dur, has_audio="Audio:" in r.stderr)


def _quality_args(encoder: str, crf: int) -> list[str]:
    """エンコーダごとの「画質固定」指定。数字はおおむね libx264 の CRF 相当（小さいほど高画質・大きい）。"""
    if encoder == "libx264":
        return ["-preset", "veryfast", "-crf", str(crf)]
    if encoder == "h264_nvenc":
        return ["-preset", "p4", "-rc", "vbr", "-cq", str(crf), "-b:v", "0"]
    if encoder == "h264_qsv":
        return ["-global_quality", str(crf), "-look_ahead", "0"]
    if encoder == "h264_amf":
        return ["-rc", "cqp", "-qp_i", str(crf), "-qp_p", str(crf)]
    if encoder == "h264_videotoolbox":
        # VideoToolbox は 1〜100 の品質（大きいほど高画質）。CRF 23 ≒ 60 程度
        q = max(1, min(100, int(round(100 - crf * 1.75))))
        return ["-q:v", str(q)]
    return ["-crf", str(crf)]


_encoder_report: dict[str, list[tuple[str, bool, str]]] = {}


def _try_encoder(ffmpeg: str, encoder: str) -> tuple[bool, str]:
    """テスト映像を実際にエンコードして、このPCで使えるか確かめる。(使える, 理由) を返す。
    小さすぎる映像だとハードウェアエンコーダが「解像度が小さい」で失敗するので、普通の大きさで試す。"""
    args = [ffmpeg, "-v", "error", "-y", "-nostdin", "-f", "lavfi", "-i", "testsrc=duration=0.5:size=640x360:rate=10",
            "-c:v", encoder, *_quality_args(encoder, 23), "-pix_fmt", "yuv420p", "-f", "null", "-"]
    try:
        r = _run(args, timeout=90)
    except subprocess.TimeoutExpired:
        return False, "テストが時間内に終わりません"
    except OSError as e:
        return False, f"起動できません: {e}"
    if r.returncode == 0:
        return True, "使えます"
    err = [ln.strip() for ln in (r.stderr or "").strip().splitlines() if ln.strip()]
    # 最初の行に本当の理由（ドライバが無い、解像度、など）が出る。エンコーダ名の付いた行を優先する
    for ln in err:
        if f"[{encoder}" in ln:
            return False, ln.split("] ", 1)[-1]
    return False, (err[0] if err else f"終了コード {r.returncode}")


def _available_encoders(ffmpeg: str) -> set[str]:
    r = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=60)
    available = set()
    for line in (r.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            available.add(parts[1])
    return available


def detect_encoder(ffmpeg: str, preferred: str = "auto") -> str:
    """使えるエンコーダを選ぶ。preferred が auto 以外ならそれを試し、だめなら libx264。試した結果は encoder_report で見られる。"""
    key = f"{ffmpeg}|{preferred}"
    with _lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    available = _available_encoders(ffmpeg)
    cands = HW_ENCODERS if preferred == "auto" else [preferred]
    chosen = CPU_ENCODER
    report: list[tuple[str, bool, str]] = []
    for enc in cands:
        if enc not in available:
            report.append((enc, False, "この ffmpeg には入っていません"))
            continue
        ok, why = _try_encoder(ffmpeg, enc)
        report.append((enc, ok, why))
        if ok:
            chosen = enc
            break
    if chosen == CPU_ENCODER:
        report.append((CPU_ENCODER, CPU_ENCODER in available, "CPU で変換します" if CPU_ENCODER in available else "libx264 も入っていません"))
    with _lock:
        _encoder_cache[key] = chosen
        _encoder_report[key] = report
    return chosen


def encoder_report(ffmpeg: str, preferred: str = "auto") -> list[tuple[str, bool, str]]:
    """detect_encoder が試した各エンコーダの結果 [(名前, 使えるか, 理由)]。"""
    detect_encoder(ffmpeg, preferred)
    with _lock:
        return list(_encoder_report.get(f"{ffmpeg}|{preferred}", []))


def _run_cancellable(args: list[str], timeout: float, stop: "threading.Event | None") -> tuple[int, str]:
    """ffmpeg を走らせ、stop が立ったら途中で止める。(終了コード, stderr) を返す。中止は -1、時間切れは -2。"""
    import time as _t
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=_creationflags())
    buf: list[bytes] = []
    # stderr はパイプが詰まらないよう別スレッドで読み切る（-v error なので量は少ない）
    reader = threading.Thread(target=lambda: buf.append(p.stderr.read() if p.stderr else b""), daemon=True)
    reader.start()
    t0 = _t.time()
    code: int | None = None
    while code is None:
        try:
            code = p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            if stop is not None and stop.is_set():
                p.kill()
                p.wait()
                code = -1
            elif _t.time() - t0 > timeout:
                p.kill()
                p.wait()
                code = -2
    reader.join(timeout=5)
    err = b"".join(buf).decode("utf-8", errors="replace")
    return code, err


def convert_to_mp4(ffmpeg: str, src: str, dst: str, encoder: str, crf: int = 23,
                   threads: int = 0, stop: "threading.Event | None" = None) -> tuple[bool, str]:
    """src を H.264/AAC の mp4 として dst に書く。dst は .part などの一時名で呼び、呼び出し側で改名する。
    終わったら長さを照合して (成功, メッセージ) を返す。stop（threading.Event）が立つと ffmpeg を止めて失敗を返す。"""
    info = probe(ffmpeg, src)
    if info is None:
        return False, "元の動画を読み取れません（壊れている可能性）"
    args = [ffmpeg, "-v", "error", "-y", "-nostdin", "-i", src, "-map", "0:v:0", "-c:v", encoder,
            *_quality_args(encoder, crf), "-pix_fmt", "yuv420p"]
    if info.has_audio:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    if threads and encoder == "libx264":
        args += ["-threads", str(threads)]
    args += ["-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", dst]
    try:
        code, stderr = _run_cancellable(args, max(600.0, info.duration * 20 + 120), stop)
    except OSError as e:
        return False, f"ffmpeg を実行できません: {e}"
    if code == -1:
        return False, "中止しました"
    if code == -2:
        return False, "変換が時間内に終わりませんでした"
    if code != 0:
        err = stderr.strip().splitlines()
        return False, "ffmpeg エラー: " + (err[-1] if err else f"code {code}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False, "変換後のファイルが空です"
    out = probe(ffmpeg, dst)
    if out is None:
        return False, "変換後のファイルを読み取れません"
    if info.duration > 0 and abs(out.duration - info.duration) > max(1.0, info.duration * 0.02):
        return False, f"変換後の長さが合いません（元 {info.duration:.1f}s / 変換後 {out.duration:.1f}s）"
    return True, f"{encoder}"
