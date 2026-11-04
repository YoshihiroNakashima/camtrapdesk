"""壊れた動画・画像の検出。

カメラトラップは電圧の低下などで、ファイルを途中までしか書けないことがある。そういうカードは Windows で
「スキャンして修復しますか」と出るが、ファイル自体はそのまま読めるので、いったん全部コピーしてから
中継の上で調べ、壊れたものを取り除く（ingest がコピーの後に呼ぶ）。

ffmpeg が無くても分かる構造の確認（ヘッダのサイズと実サイズの食い違いなど）を先に行い、ffmpeg があれば
さらに長さが読めるかを確かめる。全部を復号するわけではないので数十ミリ秒で終わる。
"""

from __future__ import annotations

import os
import struct

VIDEO_EXTS = {"avi", "mp4", "mov", "m4v", "3gp"}
IMAGE_EXTS = {"jpg", "jpeg"}
BROKEN_DIR = "_破損"   # broken_action = keep のとき、カードフォルダの中にこの名前で置く（NAS には送らない）


def _ext(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _check_avi(path: str, size: int) -> str | None:
    with open(path, "rb") as f:
        head = f.read(12)
    if len(head) < 12:
        return "ファイルが短すぎます"
    if head[:4] != b"RIFF" or head[8:12] != b"AVI ":
        return "AVI の先頭（RIFF ヘッダ）が壊れています"
    riff = struct.unpack("<I", head[4:8])[0]
    if riff == 0:
        return "AVI ヘッダのサイズが 0（書き込みが完了していません）"
    # 1 GB を超える AVI は RIFF が複数に分かれる（最初の RIFF は 1 GB 分だけ）ので、実サイズより小さいのは正常。
    # ヘッダが実サイズより大きければ途中で切れている
    if riff + 8 > size:
        return f"途中で切れています（ヘッダ {riff + 8:,} バイト / 実サイズ {size:,} バイト）"
    return None


def _check_mp4(path: str, size: int) -> str | None:
    """トップレベルの box を辿り、moov（再生に必須の索引）があるか、box がファイルの外にはみ出していないかを見る。"""
    pos = 0
    seen_moov = False
    seen_any = False
    with open(path, "rb") as f:
        while pos + 8 <= size:
            f.seek(pos)
            hdr = f.read(8)
            if len(hdr) < 8:
                break
            n = struct.unpack(">I", hdr[:4])[0]
            typ = hdr[4:8]
            if n == 1:
                ext = f.read(8)
                if len(ext) < 8:
                    return "box のサイズが読めません（途中で切れています）"
                n = struct.unpack(">Q", ext)[0]
            elif n == 0:
                n = size - pos   # 最後の box
            if n < 8:
                return "mp4 の構造が壊れています"
            seen_any = True
            if typ == b"moov":
                seen_moov = True
            if pos + n > size:
                return f"途中で切れています（{typ.decode('latin1')} box がファイルの外まで続く）"
            pos += n
    if not seen_any:
        return "mp4 の構造が読めません"
    if not seen_moov:
        return "moov（再生に必要な索引）がありません。録画が完了していません"
    return None


def _check_jpeg(path: str, size: int) -> str | None:
    with open(path, "rb") as f:
        head = f.read(2)
        f.seek(max(0, size - 2))
        tail = f.read(2)
    if head != b"\xff\xd8":
        return "JPEG の先頭が壊れています"
    if tail != b"\xff\xd9":
        return "JPEG の終端マーカーがありません（途中で切れています）"
    return None


def check_media_file(path: str, ffmpeg: str | None = None) -> str | None:
    """壊れていれば理由の文字列、問題なければ None。"""
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return f"読めません: {e.strerror or e}"
    if size == 0:
        return "0 バイト"
    ext = _ext(os.path.basename(path))
    try:
        if ext == "avi":
            r = _check_avi(path, size)
        elif ext in ("mp4", "mov", "m4v", "3gp"):
            r = _check_mp4(path, size)
        elif ext in IMAGE_EXTS:
            r = _check_jpeg(path, size)
        else:
            r = None
    except OSError as e:
        return f"読めません: {e.strerror or e}"
    if r:
        return r
    if ffmpeg and ext in VIDEO_EXTS:
        try:
            from .ffmpeg import probe
            info = probe(ffmpeg, path)
        except Exception:   # ffmpeg の実行自体に失敗したら、構造の確認だけで判断する
            return None
        if info is None:
            return "ffmpeg が読めません（ヘッダが壊れています）"
        if info.duration <= 0:
            return "長さが 0 秒（録画が完了していません）"
    return None


def is_checkable(name: str) -> bool:
    return _ext(name) in VIDEO_EXTS | IMAGE_EXTS
