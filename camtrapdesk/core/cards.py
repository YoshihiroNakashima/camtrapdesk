"""ボリュームからSDカード（id.txt を持つもの）を検出し、対象ファイルを列挙する。"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from .volumes import Volume


# カードIDの照合方法。True なら末尾の数字だけで照合する（id.txt の "6_650" と野帳の 650 を同じとみなす）。
# 設定から AppContext が切り替える。
MATCH_BY_TAIL_NUMBER = True


def normalize_card_id(raw: str) -> str:
    """照合用のカードID。

    既定（末尾の数字で照合）: "6_650" → "650", "650" → "650", "012" → "12"。
    末尾に数字が無いもの、または照合を厳密にした場合は、英数字のみ大文字にして比べる。"""
    s = (raw or "").strip()
    if MATCH_BY_TAIL_NUMBER:
        m = re.search(r"(\d+)\s*$", s)
        if m:
            return str(int(m.group(1)))
    if s.isdigit():
        return str(int(s))
    return re.sub(r"[^A-Za-z0-9]", "", s).upper()


def read_card_id_with_reason(volume: Volume, id_file_name: str) -> tuple[str | None, str]:
    """id.txt の1行目を読む。戻り値は (カードID, 理由)。読めないときは ID が None で理由が入る。"""
    p = volume.join(id_file_name)
    try:
        with open(p, "r", encoding="utf-8-sig", errors="replace") as f:
            first = f.readline()
    except FileNotFoundError:
        return None, f"{id_file_name} がありません"
    except OSError as e:
        return None, f"{id_file_name} を読めません ({e.strerror or e})"
    s = first.strip().replace("\x00", "")
    if not s:
        return None, f"{id_file_name} の1行目が空です"
    return s, "ok"


def read_card_id(volume: Volume, id_file_name: str) -> str | None:
    return read_card_id_with_reason(volume, id_file_name)[0]


def write_card_id(volume: Volume, id_file_name: str, card_id: str) -> tuple[bool, str]:
    """id.txt を持たないカードに ID を書き込む（カードへ書く唯一の操作）。既にあれば上書きしない。"""
    card_id = (card_id or "").strip()
    if not card_id:
        return False, "IDが空です"
    if volume.kind == "unreadable":
        return False, "このボリュームは読み書きできません"
    p = volume.join(id_file_name)
    if os.path.exists(p):
        return False, f"{id_file_name} は既にあります"
    try:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(card_id + "\n")
    except OSError as e:
        return False, f"書き込めません: {e.strerror or e}"
    return True, "書き込みました"


@dataclass
class MediaFile:
    src_path: str
    rel_dir: str      # media_subdir からの相対ディレクトリ（"" なら直下）
    name: str         # ファイル名
    size: int
    mtime: float

    @property
    def dest_name(self) -> str:
        return self.name


@dataclass
class Card:
    volume: Volume
    card_id: str                  # id.txt の内容（そのまま）
    card_id_norm: str             # 照合用
    files: list[MediaFile] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)   # 取り込みを止めるべき問題

    @property
    def total_bytes(self) -> int:
        return sum(f.size for f in self.files)

    @property
    def n_files(self) -> int:
        return len(self.files)


def list_media_files(volume: Volume, media_subdirs: list[str], extensions: list[str],
                     recursive: bool, min_size: int) -> tuple[list[MediaFile], list[str]]:
    """設定に従って対象ファイルを列挙する。戻り値は (ファイル一覧, 問題点)。"""
    exts = {e.lower().lstrip(".") for e in extensions}
    files: list[MediaFile] = []
    problems: list[str] = []
    found_dir = False
    for sub in media_subdirs:
        base = volume.join(*[p for p in re.split(r"[\\/]+", sub) if p])
        if not os.path.isdir(base):
            continue
        found_dir = True
        if recursive:
            walker = os.walk(base)
        else:
            try:
                names = sorted(os.listdir(base))
            except OSError as e:
                problems.append(f"フォルダを読めません: {sub} ({e})")
                continue
            walker = [(base, [], names)]
        for dirpath, _dirs, names in walker:
            rel_dir = os.path.relpath(dirpath, base)
            rel_dir = "" if rel_dir == "." else rel_dir.replace("\\", "/")
            for n in sorted(names):
                ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
                if ext not in exts:
                    continue
                p = os.path.join(dirpath, n)
                try:
                    st = os.stat(p)
                except OSError as e:
                    problems.append(f"読めないファイル: {n} ({e})")
                    continue
                if not os.path.isfile(p):
                    continue
                if st.st_size < min_size:
                    problems.append(f"異常サイズ(<{min_size}B): {n}")
                files.append(MediaFile(src_path=p, rel_dir=rel_dir, name=n, size=st.st_size, mtime=st.st_mtime))
    if not found_dir:
        problems.append("対象フォルダが見つかりません: " + ", ".join(media_subdirs))
    # 平坦化した際の同名衝突
    seen: dict[str, str] = {}
    for f in files:
        if f.name in seen and seen[f.name] != f.rel_dir:
            problems.append(f"同名ファイルが複数フォルダにあります: {f.name}")
        seen[f.name] = f.rel_dir
    return files, problems


def detect_cards(volumes: list[Volume], id_file_name: str, media_subdirs: list[str],
                 extensions: list[str], recursive: bool, min_size: int,
                 list_files: bool = True) -> list[Card]:
    """id.txt を持つボリュームをカードとして返す。"""
    cards: list[Card] = []
    for v in volumes:
        if v.kind == "unreadable":
            continue
        cid = read_card_id(v, id_file_name)
        if not cid:
            continue
        card = Card(volume=v, card_id=cid, card_id_norm=normalize_card_id(cid))
        if list_files:
            card.files, card.problems = list_media_files(v, media_subdirs, extensions, recursive, min_size)
        cards.append(card)
    # 同じIDのカードが複数挿さっている場合は両方止める
    counts: dict[str, int] = {}
    for c in cards:
        counts[c.card_id_norm] = counts.get(c.card_id_norm, 0) + 1
    for c in cards:
        if counts[c.card_id_norm] > 1:
            c.problems.append(f"同じID({c.card_id})のカードが{counts[c.card_id_norm]}枚挿さっています")
    return cards
