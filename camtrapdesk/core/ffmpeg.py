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
import time
from dataclasses import dataclass
from typing import Callable

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
    # ffprobe 無し（imageio-ffmpeg 同梱の ffmpeg だけの環境など）: ffmpeg -i の stderr から読む
    r = _run([ffmpeg, "-hide_banner", "-i", path], timeout=120)
    import re
    err = r.stderr or ""
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not m:
        return None
    dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    info = MediaInfo(duration=dur, has_audio=bool(re.search(r"Stream #\d+:\d+.*?: Audio:", err)))
    v = re.search(r"Stream #\d+:\d+.*?: Video: (\w+)[^\n]*?(\d{2,5})x(\d{2,5})", err)
    if v:
        info.vcodec, info.width, info.height = v.group(1).lower(), int(v.group(2)), int(v.group(3))
    return info


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


STALL_SEC = 90.0   # 出力ファイルがこの秒数まったく増えなければ、ffmpeg が固まったとみなして止める


def _run_cancellable(args: list[str], timeout: float, stop: "threading.Event | None",
                     watch_path: str | None = None, stall_sec: float = STALL_SEC) -> tuple[int, str]:
    """ffmpeg を走らせ、stop が立ったら途中で止める。(終了コード, stderr) を返す。
    中止は -1、時間切れは -2、出力が増えずに固まったら -3（watch_path を見張る。GPU デコードが固まる事例があるため）。"""
    import time as _t
    p = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                         creationflags=_creationflags())
    buf: list[bytes] = []
    # stderr はパイプが詰まらないよう別スレッドで読み切る（-v error なので量は少ない）
    reader = threading.Thread(target=lambda: buf.append(p.stderr.read() if p.stderr else b""), daemon=True)
    reader.start()
    t0 = _t.time()
    last_size, last_growth = -1, t0
    code: int | None = None
    while code is None:
        try:
            code = p.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            now = _t.time()
            if stop is not None and stop.is_set():
                p.kill()
                p.wait()
                code = -1
            elif now - t0 > timeout:
                p.kill()
                p.wait()
                code = -2
            elif watch_path:
                try:
                    size = os.path.getsize(watch_path)
                except OSError:
                    size = -1
                if size != last_size:
                    last_size, last_growth = size, now
                elif now - last_growth > stall_sec:
                    p.kill()
                    p.wait()
                    code = -3
    reader.join(timeout=5)
    err = b"".join(buf).decode("utf-8", errors="replace")
    return code, err


# GPU でのデコード（MJPEG → GPU 上で直接エンコード）。使えなければ CPU デコードに戻す。(エンコーダ, 元コーデック) → 使えるか
_gpu_decode_ok: dict[tuple[str, str], bool] = {}
# GPU デコードが「エラー」で使えなかった回数（連続）。1 本だけ対応外の JPEG だったときに全体を CPU に落とさないため、
# エラーはそのファイルだけ CPU に切り替え、連続して多いときだけ以後を CPU にする。固まった（停止）ときは即座に以後 CPU
_gpu_decode_fail_streak: dict[tuple[str, str], int] = {}
GPU_DECODE_GIVE_UP = 5
# ハードウェアエンコーダが固まった回数（2 回以上なら、変換係はそのセッション中は CPU に切り替える）
encoder_timeouts: dict[str, int] = {}
ENCODER_GIVE_UP = 2


def encoder_unreliable(encoder: str) -> bool:
    return encoder != "libx264" and encoder_timeouts.get(encoder, 0) >= ENCODER_GIVE_UP

# 元コーデックごとの GPU デコードの入力オプション（-i の前に置く）
_GPU_DECODE = {
    "h264_nvenc": {"mjpeg": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", "mjpeg_cuvid"],
                   "h264": ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-c:v", "h264_cuvid"]},
    "h264_qsv": {"mjpeg": ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", "mjpeg_qsv"],
                 "h264": ["-hwaccel", "qsv", "-hwaccel_output_format", "qsv", "-c:v", "h264_qsv"]},
}


def gpu_decode_available(encoder: str, vcodec: str) -> bool | None:
    """この組み合わせで GPU デコードが使えたか（None はまだ試していない）。"""
    return _gpu_decode_ok.get((encoder, vcodec))


def _build_args(ffmpeg: str, src: str, dst: str, encoder: str, crf: int, info: "MediaInfo", threads: int,
                gpu_decode: bool) -> list[str]:
    args = [ffmpeg, "-v", "error", "-y", "-nostdin"]
    if gpu_decode:
        args += _GPU_DECODE[encoder][info.vcodec]
    args += ["-i", src, "-map", "0:v:0", "-c:v", encoder, *_quality_args(encoder, crf)]
    if not gpu_decode:
        args += ["-pix_fmt", "yuv420p"]   # GPU 上のフレームはそのままエンコーダへ渡す（変換は GPU 側）
    if info.has_audio:
        args += ["-map", "0:a:0?", "-c:a", "aac", "-b:a", "96k"]
    else:
        args += ["-an"]
    if threads and encoder == "libx264":
        args += ["-threads", str(threads)]
    args += ["-map_metadata", "0", "-movflags", "+faststart", "-f", "mp4", dst]
    return args


def convert_to_mp4(ffmpeg: str, src: str, dst: str, encoder: str, crf: int = 23,
                   threads: int = 0, stop: "threading.Event | None" = None) -> tuple[bool, str]:
    """src を H.264/AAC の mp4 として dst に書く。dst は .part などの一時名で呼び、呼び出し側で改名する。
    終わったら長さを照合して (成功, メッセージ) を返す。stop（threading.Event）が立つと ffmpeg を止めて失敗を返す。

    ハードウェアエンコーダ（nvenc / qsv）のときは、デコードも GPU で行う経路をまず試す。
    MJPEG のデコードと色変換は CPU では重く、エンコードだけ GPU にしても速くならないため。
    GPU デコードが使えない（非対応の JPEG 形式など）ときは、その組み合わせを覚えて以後は CPU デコードにする。"""
    info = probe(ffmpeg, src)
    if info is None:
        return False, "元の動画を読み取れません（壊れている可能性）"
    can_gpu = encoder in _GPU_DECODE and info.vcodec in _GPU_DECODE[encoder]
    use_gpu = can_gpu and _gpu_decode_ok.get((encoder, info.vcodec), True)
    timeout = max(600.0, info.duration * 20 + 120)
    # 試す順: GPU デコード＋指定エンコーダ → CPU デコード＋指定エンコーダ → CPU（libx264）。
    # 固まった・時間切れになったら次へ進む（GPU 側が固まる事例があるため）。
    attempts: list[tuple[bool, str]] = []
    if use_gpu:
        attempts.append((True, encoder))
    attempts.append((False, encoder))
    if encoder != "libx264":
        attempts.append((False, "libx264"))
    code, stderr = 1, ""
    used_gpu, used_enc = False, encoder
    notes: list[str] = []
    for i, (gpu, enc) in enumerate(attempts):
        args = _build_args(ffmpeg, src, dst, enc, crf, info, threads, gpu)
        try:
            code, stderr = _run_cancellable(args, timeout, stop, watch_path=dst)
        except OSError as e:
            return False, f"ffmpeg を実行できません: {e}"
        if code == -1:
            return False, "中止しました"
        if code == 0:
            used_gpu, used_enc = gpu, enc
            if gpu:
                _gpu_decode_ok[(encoder, info.vcodec)] = True
                _gpu_decode_fail_streak[(encoder, info.vcodec)] = 0
            break
        try:
            os.remove(dst)
        except OSError:
            pass
        hung = code in (-2, -3)
        if gpu:
            key = (encoder, info.vcodec)
            if hung:
                # 固まった: 以後は CPU デコード＋GPU エンコードにする
                _gpu_decode_ok[key] = False
                notes.append("GPU デコードが固まったので CPU デコードでやり直し")
            else:
                # このファイルは GPU で読めない（対応外の JPEG など）。このファイルだけ CPU に切り替える。
                # 連続して何本も失敗するなら、そもそも使えない環境なので以後は試さない
                _gpu_decode_fail_streak[key] = _gpu_decode_fail_streak.get(key, 0) + 1
                if _gpu_decode_fail_streak[key] >= GPU_DECODE_GIVE_UP:
                    _gpu_decode_ok[key] = False
                notes.append("GPU デコードがこのファイルに使えないので CPU デコードでやり直し")
            continue
        if hung and enc != "libx264":
            encoder_timeouts[enc] = encoder_timeouts.get(enc, 0) + 1
            notes.append(f"{enc} が固まったので CPU（libx264）でやり直し")
            continue
        if hung:
            return False, "変換が時間内に終わりませんでした" + ("（" + "、".join(notes) + "）" if notes else "")
        if enc != "libx264" and i + 1 < len(attempts):
            notes.append(f"{enc} がエラーだったので CPU（libx264）でやり直し")
            continue
        break
    if code != 0:
        err = stderr.strip().splitlines()
        return False, "ffmpeg エラー: " + (err[-1] if err else f"code {code}") + ("（" + "、".join(notes) + "）" if notes else "")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False, "変換後のファイルが空です"
    out = probe(ffmpeg, dst)
    if out is None:
        return False, "変換後のファイルを読み取れません"
    if info.duration > 0 and abs(out.duration - info.duration) > max(1.0, info.duration * 0.02):
        return False, f"変換後の長さが合いません（元 {info.duration:.1f}s / 変換後 {out.duration:.1f}s）"
    return True, f"{used_enc}" + ("（デコードも GPU）" if used_gpu else "") + ("。" + "、".join(notes) if notes else "")


# ----------------------------------------------------------------------------
# 変換が遅いときの診断: どこに時間がかかっているかを 1 本の動画で測る
# ----------------------------------------------------------------------------

def _timed(args: list[str], timeout: float = 1800.0) -> tuple[float, int, str]:
    """ffmpeg を走らせて (秒, 終了コード, stderr) を返す。"""
    t0 = time.time()
    r = _run(args, timeout=timeout)
    return time.time() - t0, r.returncode, r.stderr or ""


def _stream_details(ffmpeg: str, path: str) -> dict:
    """ffprobe があれば画素形式・fps・ビットレート・音声も読む。"""
    out: dict = {}
    fp = ffprobe_path(ffmpeg)
    if not fp:
        return out
    try:
        r = _run([fp, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path], timeout=120)
        d = json.loads(r.stdout or "{}")
    except Exception:
        return out
    for st in d.get("streams", []):
        if st.get("codec_type") == "video" and "vcodec" not in out:
            fr = st.get("avg_frame_rate") or st.get("r_frame_rate") or "0/1"
            try:
                num, den = fr.split("/")
                fps = float(num) / float(den) if float(den) else 0.0
            except Exception:
                fps = 0.0
            out.update(vcodec=st.get("codec_name"), pix_fmt=st.get("pix_fmt"), width=st.get("width"),
                       height=st.get("height"), fps=fps, nb_frames=st.get("nb_frames"),
                       vbitrate=st.get("bit_rate"))
        elif st.get("codec_type") == "audio" and "acodec" not in out:
            out.update(acodec=st.get("codec_name"), asample_rate=st.get("sample_rate"))
    fmt = d.get("format", {})
    out.update(format=fmt.get("format_name"), bitrate=fmt.get("bit_rate"))
    return out


def diagnose_conversion(ffmpeg: str, src: str, encoder: str = "auto", crf: int = 23,
                        on_progress: Callable[[str], None] | None = None) -> str:
    """1 本の動画について、読み出し・デコード・色変換・エンコードにそれぞれ何秒かかるかを測り、原因の見当を付ける。"""
    import shutil
    import tempfile
    lines: list[str] = []

    def say(msg: str) -> None:
        lines.append(msg)
        if on_progress:
            on_progress(msg)

    info = probe(ffmpeg, src)
    if info is None:
        return "元の動画を読み取れません（壊れている可能性）"
    det = _stream_details(ffmpeg, src)
    size = os.path.getsize(src)
    dur = info.duration or 0.0
    say(f"ファイル: {src}")
    say(f"  {size/1e6:.0f} MB、{dur:.1f} 秒、{info.width}x{info.height}、映像 {det.get('vcodec') or info.vcodec}"
        + (f"（{det.get('pix_fmt')}）" if det.get("pix_fmt") else "")
        + (f"、{det.get('fps'):.1f} fps" if det.get("fps") else "")
        + (f"、音声 {det.get('acodec')}" if det.get("acodec") else "、音声なし")
        + (f"、{int(det['bitrate'])/1e6:.0f} Mbps" if det.get("bitrate") else ""))
    enc = detect_encoder(ffmpeg, encoder)
    say(f"  エンコーダ: {enc}（設定 {encoder}）")
    for ln in hardware_summary().splitlines():
        say("  " + ln)

    # 1) 読み出しだけ（ディスク／NAS の速さ）
    t0 = time.time()
    n = 0
    with open(src, "rb", buffering=0) as f:
        while True:
            b = f.read(8 << 20)
            if not b:
                break
            n += len(b)
    t_read = time.time() - t0
    say(f"1) ファイルを読むだけ: {t_read:.1f} 秒（{n/1e6/max(t_read, 1e-6):.0f} MB/s）")

    # 2) デコードだけ（映像のみ）
    t_dec, code, err = _timed([ffmpeg, "-hide_banner", "-v", "warning", "-nostdin", "-i", src, "-map", "0:v:0",
                               "-f", "null", "-"])
    warn = [ln.strip() for ln in err.splitlines() if ln.strip()]
    say(f"2) デコードだけ（CPU）: {t_dec:.1f} 秒（{dur/max(t_dec, 1e-6):.1f} 倍速）" + ("" if code == 0 else f"　終了コード {code}"))
    noninter = any("non-interleaved" in w.lower() for w in warn)
    for w in warn[:5]:
        say(f"   ffmpeg: {w}")

    # 3) デコード＋yuv420p への色変換
    t_pix, code, _ = _timed([ffmpeg, "-v", "error", "-nostdin", "-i", src, "-map", "0:v:0", "-pix_fmt", "yuv420p",
                             "-f", "null", "-"])
    say(f"3) デコード＋色変換: {t_pix:.1f} 秒")

    tmpdir = tempfile.mkdtemp(prefix="camtrapdesk_diag_")
    try:
        # 4) いつもの変換（CPU デコード＋選んだエンコーダ）
        dst = os.path.join(tmpdir, "a.mp4")
        t_enc, code, err = _timed(_build_args(ffmpeg, src, dst, enc, crf, info, 0, False))
        out_size = os.path.getsize(dst) if os.path.isfile(dst) else 0
        say(f"4) 変換（CPU デコード＋{enc}）: {t_enc:.1f} 秒（{dur/max(t_enc, 1e-6):.1f} 倍速、{out_size/1e6:.0f} MB）"
            + ("" if code == 0 else f"　失敗: {err.strip().splitlines()[-1] if err.strip() else code}"))
        # 5) GPU デコード
        t_gpu = None
        if enc in _GPU_DECODE and info.vcodec in _GPU_DECODE[enc]:
            dst2 = os.path.join(tmpdir, "b.mp4")
            t_gpu, code, err = _timed(_build_args(ffmpeg, src, dst2, enc, crf, info, 0, True))
            say(f"5) 変換（GPU デコード＋{enc}）: {t_gpu:.1f} 秒（{dur/max(t_gpu, 1e-6):.1f} 倍速）"
                + ("" if code == 0 else f"　使えません: {err.strip().splitlines()[-1] if err.strip() else code}"))
            if code != 0:
                t_gpu = None
        # 6) CPU（libx264）との比較
        t_x264 = None
        if enc != "libx264":
            dst3 = os.path.join(tmpdir, "c.mp4")
            t_x264, code, _ = _timed(_build_args(ffmpeg, src, dst3, "libx264", crf, info, 0, False))
            say(f"6) 変換（CPU のみ libx264 veryfast）: {t_x264:.1f} 秒（{dur/max(t_x264, 1e-6):.1f} 倍速）")
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    # 見立て
    say("")
    say("見立て:")
    if t_read > 0.5 * t_enc and n / 1e6 / max(t_read, 1e-6) < 60:
        say(f" - 読み出しが遅い（{n/1e6/max(t_read, 1e-6):.0f} MB/s）。NAS や HDD、USB の SD カードから直接変換していませんか。中継の SSD 上で変換すると速くなります")
    if noninter:
        say(" - ffmpeg が「Non-interleaved AVI」と言っています。映像と音声が交互に並んでいない（索引が無い）AVI で、読むときに行ったり来たりするため"
            "遅くなります。カメラの書き方の問題で、ローカルの SSD 上なら影響は小さくなります")
    if t_dec > 0.6 * t_enc:
        say(f" - 時間の大半はデコード（MJPEG の展開）です（デコードだけで {t_dec:.1f} 秒）。夜間の赤外線映像のようなノイズの多い動画は JPEG が大きく、展開に時間がかかります。"
            + ("GPU デコードが使えるので、そちらを使えば速くなります" if t_gpu and t_gpu < t_enc * 0.7 else
               "GPU デコードが使えない（または速くない）環境です。同時に変換する本数を増やすと全体の時間は短くなります"))
    if t_pix - t_dec > 0.3 * t_enc:
        say(f" - 色変換（{det.get('pix_fmt')} → yuv420p）に {t_pix - t_dec:.1f} 秒かかっています")
    if t_enc - t_pix > 0.5 * t_enc:
        say(f" - エンコード自体に時間がかかっています（{t_enc - t_pix:.1f} 秒）。"
            + ("GPU のエンコーダを使うか、" if enc == "libx264" else "")
            + "設定の CRF を上げる（例: 26）と速く小さくなります")
    if t_gpu is not None and t_gpu < t_enc * 0.7:
        say(f" - GPU デコードの経路なら {t_gpu:.1f} 秒です。普段の変換でこれが使われていないなら、一度 GPU デコードに失敗して CPU に切り替わった"
            "ままかもしれません（アプリを起動し直すと再び試します）")
    if max(t_enc, t_dec) < max(2.0, dur * 0.5):
        say(" - この 1 本は十分速く変換できています。普段遅いのは、取り込みの検証（ハッシュ計算）や NAS 転送と同時に走って CPU を奪い合っているか、"
            "別の動画が重いかのどちらかです。取り込みが終わってから変換するか、同時に変換する本数を減らしてみてください")
    return "\n".join(lines)


def hardware_summary() -> str:
    """CPU のコア数と GPU の名前（分かる範囲で）。同時に変換する本数の目安も添える。"""
    import platform
    cores = os.cpu_count() or 0
    phys = None
    gpu = ""
    try:
        r = _run(["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"], timeout=10)
        if r.returncode == 0 and r.stdout.strip():
            gpu = "NVIDIA " + r.stdout.strip().splitlines()[0].replace(",", "（ドライバ") + "）"
    except Exception:
        pass
    if sys.platform == "win32":
        try:
            r = _run(["powershell", "-NoProfile", "-Command",
                      "(Get-CimInstance Win32_Processor | Select-Object -First 1).NumberOfCores; "
                      "(Get-CimInstance Win32_Processor | Select-Object -First 1).Name; "
                      "(Get-CimInstance Win32_VideoController | ForEach-Object { $_.Name }) -join ' / '"], timeout=20)
            lines = [x.strip() for x in (r.stdout or "").splitlines() if x.strip()]
            if len(lines) >= 1 and lines[0].isdigit():
                phys = int(lines[0])
            cpu_name = lines[1] if len(lines) >= 2 else platform.processor()
            if not gpu and len(lines) >= 3:
                gpu = lines[2]
        except Exception:
            cpu_name = platform.processor()
    else:
        cpu_name = platform.processor() or platform.machine()
    core_txt = f"{phys} コア（論理 {cores}）" if phys else f"論理 {cores} コア"
    base = phys or max(1, cores // 2)
    if gpu and "nvidia" in gpu.lower():
        rec = "GPU（nvenc）なら 3〜4 本。取り込みと同時なら 2 本"
    else:
        rec = f"CPU だけなら {min(4, max(2, base // 4))} 本程度。取り込みと同時なら 2 本"
    return f"CPU: {cpu_name} {core_txt}\nGPU: {gpu or '（見つかりません、または NVIDIA 以外）'}\n同時に変換する本数の目安: {rec}"
