"""ファイルのハッシュ計算と、ハッシュを取りながらのコピー。

xxHash (xxh3_128) が使えればそれを、無ければ BLAKE2b(16バイト) を使う。
どちらで計算したかはハッシュ文字列の接頭辞で区別する。
"""

from __future__ import annotations

import hashlib
import os
from typing import Callable

try:
    import xxhash  # type: ignore

    def _new_hasher():
        return "xxh3:", xxhash.xxh3_128()
except Exception:  # pragma: no cover - 環境依存
    def _new_hasher():
        return "b2:", hashlib.blake2b(digest_size=16)

CHUNK = 4 * 1024 * 1024

Progress = Callable[[int], None]   # 処理したバイト数（累積）を受け取る


def hash_file(path: str, progress: Progress | None = None) -> str:
    prefix, h = _new_hasher()
    done = 0
    with open(path, "rb", buffering=0) as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
    return prefix + h.hexdigest()


class CopyCancelled(RuntimeError):
    """should_stop が True を返したので途中でやめた（.part は消してある）。"""


def copy_with_hash(src: str, dst: str, progress: Progress | None = None,
                   should_stop: Callable[[], bool] | None = None) -> tuple[int, str]:
    """src を dst にコピーしながらハッシュを計算する。更新日時もコピーする。
    should_stop が True を返したら 4 MB 単位の切れ目でやめ、書きかけの .part を消して CopyCancelled を投げる。"""
    prefix, h = _new_hasher()
    done = 0
    tmp = dst + ".part"
    with open(src, "rb", buffering=0) as fi, open(tmp, "wb", buffering=0) as fo:
        while True:
            if should_stop is not None and should_stop():
                fo.close()
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise CopyCancelled("停止されました")
            chunk = fi.read(CHUNK)
            if not chunk:
                break
            fo.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done)
        fo.flush()
        try:
            os.fsync(fo.fileno())
        except OSError:
            pass
    st = os.stat(src)
    os.replace(tmp, dst)
    try:
        os.utime(dst, (st.st_atime, st.st_mtime))
    except OSError:
        pass
    return done, prefix + h.hexdigest()
