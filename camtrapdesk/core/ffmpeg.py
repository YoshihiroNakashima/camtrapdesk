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


def find_ffmpeg(configured: str = "") -> str | None:
    """ffmpeg の実行ファイルを探す: 設定 → アプリと同じフォルダ → imageio-ffmpeg 同梱 → PATH。"""
    key = configured or ""
    with _lock:
        if key in _ffmpeg_cache:
            return _ffmpeg_cache[key]
    exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
    cands: list[str] = []
    if configured:
        cands.append(configured)
        if os.path.isdir(configured):
            cands.append(os.path.join(configured, exe))
    base = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    cands.append(os.path.join(base, exe))
    cands.append(os.path.join(base, "tools", exe))
    try:
        import imageio_ffmpeg  # type: ignore
        cands.append(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        pass
    found = shutil.which("ffmpeg")
    if found:
        cands.append(found)
    result = None
    for c in cands:
        if c and os.path.isfile(c) and os.access(c, os.X_OK):
            result = c
            break
    with _lock:
        _ffmpeg_cache[key] = result
    return result


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


def _try_encoder(ffmpeg: str, encoder: str) -> bool:
    """小さなテスト映像を実際にエンコードして、このPCで使えるか確かめる。"""
    args = [ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=0.3:size=128x72:rate=10",
            "-c:v", encoder, *_quality_args(encoder, 23), "-pix_fmt", "yuv420p", "-f", "null",
            "NUL" if sys.platform == "win32" else "/dev/null"]
    try:
        r = _run(args, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


def detect_encoder(ffmpeg: str, preferred: str = "auto") -> str:
    """使えるエンコーダを選ぶ。preferred が auto 以外ならそれを試し、だめなら libx264。"""
    key = f"{ffmpeg}|{preferred}"
    with _lock:
        if key in _encoder_cache:
            return _encoder_cache[key]
    r = _run([ffmpeg, "-hide_banner", "-encoders"], timeout=60)
    available = set()
    for line in r.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].startswith("V"):
            available.add(parts[1])
    cands = HW_ENCODERS if preferred == "auto" else [preferred]
    chosen = CPU_ENCODER
    for enc in cands:
        if enc in available and _try_encoder(ffmpeg, enc):
            chosen = enc
            break
    with _lock:
        _encoder_cache[key] = chosen
    return chosen


def convert_to_mp4(ffmpeg: str, src: str, dst: str, encoder: str, crf: int = 23,
                   threads: int = 0) -> tuple[bool, str]:
    """src を H.264/AAC の mp4 として dst に書く。dst は .part などの一時名で呼び、呼び出し側で改名する。
    終わったら長さを照合して (成功, メッセージ) を返す。"""
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
        r = _run(args, timeout=max(600.0, info.duration * 20 + 120))
    except subprocess.TimeoutExpired:
        return False, "変換が時間内に終わりませんでした"
    except OSError as e:
        return False, f"ffmpeg を実行できません: {e}"
    if r.returncode != 0:
        err = (r.stderr or "").strip().splitlines()
        return False, "ffmpeg エラー: " + (err[-1] if err else f"code {r.returncode}")
    if not os.path.isfile(dst) or os.path.getsize(dst) == 0:
        return False, "変換後のファイルが空です"
    out = probe(ffmpeg, dst)
    if out is None:
        return False, "変換後のファイルを読み取れません"
    if info.duration > 0 and abs(out.duration - info.duration) > max(1.0, info.duration * 0.02):
        return False, f"変換後の長さが合いません（元 {info.duration:.1f}s / 変換後 {out.duration:.1f}s）"
    return True, f"{encoder}"
