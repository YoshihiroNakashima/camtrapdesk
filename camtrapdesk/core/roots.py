"""中継先（保存先）のフォルダを、ドライブ文字が変わっても見つけられるようにする。

中継先フォルダの直下に目印ファイル（.camtrapdesk-root.json、固有のID入り）を置き、台帳にはそのIDも記録する。
SSD を抜き差ししてドライブ文字が変わったり、別の SSD に切り替えて元の SSD を後から繋いだりしても、
目印を探してフォルダを見つけ直す。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime
from typing import Iterable

MARKER_NAME = ".camtrapdesk-root.json"

_lock = threading.Lock()
_ensure_lock = threading.Lock()   # 同じフォルダに2つの目印を同時に作らないため
_cache: dict[str, str] = {}   # root_id → 最後に見つかったパス
_miss: dict[str, float] = {}  # root_id → 走査して見つからなかった時刻（しばらく走査しない）
MISS_RETRY_SEC = 60.0


def read_root_id(path: str) -> str | None:
    try:
        with open(os.path.join(path, MARKER_NAME), "r", encoding="utf-8") as f:
            data = json.load(f)
        rid = data.get("root_id")
        return str(rid) if rid else None
    except (OSError, ValueError, AttributeError):
        return None


def ensure_root_id(path: str) -> str | None:
    """目印が無ければ作る。作れない（読み取り専用など）場合は None。並列の取り込みから同時に呼ばれても1つしか作らない。"""
    with _ensure_lock:
        return _ensure_root_id(path)


def _ensure_root_id(path: str) -> str | None:
    rid = read_root_id(path)
    if rid:
        with _lock:
            _cache[rid] = path
        return rid
    rid = uuid.uuid4().hex
    data = {"app": "camtrapdesk", "root_id": rid, "created_at": datetime.now().isoformat(timespec="seconds"),
            "note": "camtrapdesk が中継先フォルダを見分けるための目印です。消さないでください。"}
    try:
        os.makedirs(path, exist_ok=True)
        tmp = os.path.join(path, MARKER_NAME + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(path, MARKER_NAME))
    except OSError:
        return None
    with _lock:
        _cache[rid] = path
    return rid


def _scan_for_marker(root_id: str, base: str, max_depth: int) -> str | None:
    """base 以下を max_depth 段まで下りて目印を探す（フォルダだけ見るので速い）。"""
    stack = [(base, 0)]
    while stack:
        d, depth = stack.pop()
        if read_root_id(d) == root_id:
            return d
        if depth >= max_depth:
            continue
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False) and not e.name.startswith((".", "$")) \
                                and e.name.lower() not in ("windows", "program files", "program files (x86)", "users"):
                            stack.append((e.path, depth + 1))
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def locate_root(root_id: str, hint_paths: Iterable[str] = (), search_bases: Iterable[str] = (),
                max_depth: int = 2) -> str | None:
    """root_id の目印があるフォルダを探す。まず候補（記録されたパス、設定の中継先、前回見つけた場所）を見て、
    無ければ接続中のボリュームを浅く走査する。"""
    with _lock:
        cached = _cache.get(root_id)
    for p in [cached, *hint_paths]:
        if p and os.path.isdir(p) and read_root_id(p) == root_id:
            with _lock:
                _cache[root_id] = p
            return p
    with _lock:
        if time.time() - _miss.get(root_id, 0.0) < MISS_RETRY_SEC:
            return None
    if callable(search_bases):
        search_bases = search_bases()
    for base in search_bases:
        if not base or not os.path.isdir(base):
            continue
        found = _scan_for_marker(root_id, base, max_depth)
        if found:
            with _lock:
                _cache[root_id] = found
                _miss.pop(root_id, None)
            return found
    with _lock:
        _miss[root_id] = time.time()
    return None


def volume_bases(exclude: Iterable[str] = ()) -> list[str]:
    """走査の起点にする接続中ボリュームのルート（NAS など除外したいものは exclude）。"""
    from .volumes import list_volumes
    ex = {os.path.normcase(os.path.normpath(e)) for e in exclude if e}
    bases = []
    for v in list_volumes():
        if v.kind == "unreadable":
            continue
        if os.path.normcase(os.path.normpath(v.root)) in ex:
            continue
        bases.append(v.root)
    return bases


def resolve_dest_root(ing, settings, ledger=None, search: bool = True) -> str | None:
    """取り込み記録の中継先フォルダの「今のパス」を返す。見つからなければ None（取り外し中など）。

    目印IDが記録されていれば、記録されたパス→設定の中継先→前回見つけた場所→接続中のボリュームの順に探し、
    別の場所で見つかれば台帳のパスも直しておく。目印IDが無い古い記録は、記録されたパスをそのまま使う。
    """
    rid = getattr(ing, "dest_root_id", None)
    if not rid:
        return ing.dest_root if os.path.isdir(ing.dest_root) else None
    hints = [ing.dest_root, getattr(settings, "dest_root", "")]
    bases = (lambda: volume_bases(exclude=[getattr(settings, "nas_root", "")])) if search else []
    found = locate_root(rid, hints, bases)
    if found and ledger is not None and os.path.normcase(os.path.normpath(found)) != os.path.normcase(os.path.normpath(ing.dest_root)):
        ledger.update_ingest(ing.id, dest_root=found)
        ing.dest_root = found
    return found


def resolve_staging_dir(ing, settings, ledger=None, search: bool = True) -> str | None:
    root = resolve_dest_root(ing, settings, ledger, search)
    if not root:
        return None
    return os.path.join(root, ing.season_folder, ing.dir_name)
