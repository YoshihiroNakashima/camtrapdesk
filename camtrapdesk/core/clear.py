"""削除（clear）: 挿入中のカードを台帳と突合し、検証済みのファイルだけを削除する。

二つのやり方がある。
 - 台帳と照合（check_card）: このPCで取り込んだばかりのカード。台帳の記録と保存先の実体で確かめる。
 - 指定したフォルダと照合（check_card_in_folder）: 過去のカードや別のPCで取り込んだカード。動画の置き場所
   （調査回のフォルダなど）を指定し、その中のどこかのフォルダにカードの全ファイルが同じ名前・サイズで
   （manifest.json があればそれで、任意でハッシュでも）あることを確かめる。
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from . import ledger as L
from .cards import Card, normalize_card_id
from .hashing import hash_file
from .roots import resolve_staging_dir


@dataclass
class ClearVerdict:
    card: Card
    ingest: L.Ingest | None
    ok: bool = False                      # 削除してよい
    deletable: list[str] = field(default_factory=list)    # 削除するファイル名
    broken: list[str] = field(default_factory=list)       # そのうち、取り込み時に壊れていて保存先には無いもの
    extra: list[str] = field(default_factory=list)        # 台帳に無い（残す）ファイル
    reasons: list[str] = field(default_factory=list)      # 削除しない理由・注意
    checked_destination: str = ""
    mode: str = "ledger"                                  # ledger: 台帳と照合 / folder: 指定フォルダと照合

    @property
    def deletable_bytes(self) -> int:
        names = set(self.deletable)
        return sum(f.size for f in self.card.files if f.name in names)


def check_card(card: Card, ledger: L.Ledger, s: Settings, verify_hash: bool = False,
               allow_offline_destination: bool = False) -> ClearVerdict:
    v = ClearVerdict(card=card, ingest=None)
    ing = ledger.latest_ingest_for_card(card.card_id_norm)
    v.ingest = ing
    if ing is None:
        old = ledger.latest_ingest_for_card(card.card_id_norm, include_cleared=True)
        if old and old.cleared_at:
            v.reasons.append(f"削除済みです（{old.cleared_at}、{old.season_folder}/{old.dir_name}）")
        else:
            v.reasons.append("台帳に取り込みの記録がありません")
        if not card.files:
            v.reasons.append("カードに対象ファイルはありません")
        return v
    if ing.status == L.STATUS_ERROR:
        v.reasons.append(f"取り込みがエラーで終わっています: {ing.message}")
        return v
    if ing.status == L.STATUS_COPYING:
        v.reasons.append("取り込みが完了していません")
        return v
    requires_nas = bool(ing.nas_root) and s.delete_requires_nas
    if requires_nas and ing.status != L.STATUS_SYNCED:
        v.reasons.append("NASへの転送・検証がまだ終わっていません")
        return v

    if ing.convert_pending:
        v.reasons.append(f"mp4 への変換がまだ終わっていません（{ing.convert_pending}ファイル）")
        return v
    recs = {f.card_name: f for f in ledger.files_for(ing.id) if not f.deleted_at}   # カード上の名前で引く
    mismatch = 0
    n_broken = 0
    for f in card.files:
        r = recs.get(f.name)
        if r is None:
            v.extra.append(f.name)
            continue
        if r.broken:
            # 取り込み時に壊れていると分かったファイル。保存先には無いが、カードからは消してよい
            v.broken.append(f.name)
            v.deletable.append(f.name)
            n_broken += 1
            continue
        if r.card_size != f.size:
            mismatch += 1
            v.reasons.append(f"サイズ不一致: {f.name}（カード {f.size} / 台帳 {r.card_size}）")
            continue
        if verify_hash and r.card_hash:
            if hash_file(f.src_path) != r.card_hash:
                mismatch += 1
                v.reasons.append(f"ハッシュ不一致: {f.name}")
                continue
        v.deletable.append(f.name)
    if v.extra:
        v.reasons.append(f"台帳に無いファイルが{len(v.extra)}件あります（削除しません）")
    if n_broken:
        v.reasons.append(f"取り込み時に壊れていた {n_broken} ファイルも消します（保存先にはありません）")

    # 最終保存先（NASがあればNAS、無ければ中継先）に実体があるか
    if ing.nas_root and ing.status == L.STATUS_SYNCED:
        dest_dir = ing.nas_dir or ""
    else:
        dest_dir = resolve_staging_dir(ing, s, ledger) or ing.staging_dir
    v.checked_destination = dest_dir
    if os.path.isdir(dest_dir):
        missing = []
        for name in v.deletable:
            r = recs[name]
            if r.broken:
                continue
            p = os.path.join(dest_dir, r.name)   # 変換済みなら mp4 の名前とサイズで確かめる
            if not os.path.isfile(p) or os.path.getsize(p) != r.size:
                missing.append(name)
        if missing:
            v.reasons.append(f"保存先にファイルがありません/サイズ不一致: {missing[0]} ほか{len(missing)}件")
            v.deletable = [n for n in v.deletable if n not in set(missing)]
            v.ok = False
            return v
    else:
        if not allow_offline_destination:
            v.reasons.append(f"保存先に接続できません: {dest_dir}")
            return v
        v.reasons.append(f"保存先に接続できないため台帳の記録のみで判定: {dest_dir}")

    if mismatch:
        v.ok = False
        return v
    if not v.deletable:
        v.reasons.append("削除対象のファイルがありません（既に削除済み？）")
        return v
    v.ok = True
    return v


def delete_files(v: ClearVerdict, ledger: L.Ledger, log=print) -> tuple[int, list[str]]:
    """判定で OK になったファイルだけを削除する。id.txt などは触らない。"""
    if not v.ok or (v.ingest is None and v.mode != "folder"):
        return 0, ["削除条件を満たしていません"]
    names = set(v.deletable)
    deleted: list[str] = []
    errors: list[str] = []
    for f in v.card.files:
        if f.name not in names:
            continue
        try:
            os.remove(f.src_path)
            deleted.append(f.name)
        except OSError as e:
            errors.append(f"{f.name}: {e}")
    if v.ingest is not None:
        ledger.mark_files_deleted(v.ingest.id, deleted)
        if deleted and not errors:
            remaining = [f for f in ledger.files_for(v.ingest.id) if not f.deleted_at]
            if not remaining:
                ledger.update_ingest(v.ingest.id, cleared_at=L.now_iso())
        ledger.event("clear", f"{v.card.card_id}: {len(deleted)} files deleted" + (f", {len(errors)} errors" if errors else ""),
                     v.card.card_id)
    else:
        ledger.event("clear_folder", f"{v.card.card_id}: {len(deleted)} files deleted after matching {v.checked_destination}"
                     + (f", {len(errors)} errors" if errors else ""), v.card.card_id)
    log(f"[{v.card.card_id}] {len(deleted)}ファイルを削除" + (f"（エラー{len(errors)}件）" if errors else "")
        + (f"　照合先: {v.checked_destination}" if v.mode == "folder" else ""))
    return len(deleted), errors


# ----------------------------------------------------------------------------
# 指定したフォルダと照合する
# ----------------------------------------------------------------------------

SKIP_DIRS = {"system volume information", "$recycle.bin", "#recycle", "@eadir", "_破損"}


@dataclass
class FolderIndex:
    """動画の置き場所（調査回のフォルダなど）の中身の索引。フォルダごとのファイル名→サイズと、manifest.json。"""
    root: str
    folders: dict[str, dict[str, tuple[int, str]]] = field(default_factory=dict)   # フォルダ → {小文字の名前: (サイズ, 実際の名前)}
    manifests: dict[str, dict] = field(default_factory=dict)           # フォルダ → manifest
    n_files: int = 0
    built_at: float = 0.0
    error: str = ""

    @property
    def n_folders(self) -> int:
        return len(self.folders)


def build_folder_index(root: str, max_depth: int = 3, stop: Callable[[], bool] | None = None,
                       on_progress: Callable[[int, int], None] | None = None) -> FolderIndex:
    """root 以下（深さ max_depth まで）の全ファイルを名前とサイズで索引にする。NAS でも数十秒で終わる。"""
    idx = FolderIndex(root=root, built_at=time.time())
    if not root or not os.path.isdir(root):
        idx.error = f"フォルダが見つかりません: {root or '（未指定）'}"
        return idx
    base_depth = os.path.normpath(root).count(os.sep)
    for dirpath, dirnames, filenames in os.walk(root):
        if stop and stop():
            idx.error = "中止しました"
            break
        depth = os.path.normpath(dirpath).count(os.sep) - base_depth
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS and not d.startswith(".")] if depth < max_depth else []
        files: dict[str, tuple[int, str]] = {}
        for n in filenames:
            if n == L.MANIFEST_NAME:
                m = L.Ledger.read_manifest(dirpath)
                if m:
                    idx.manifests[dirpath] = m
                continue
            try:
                files[n.lower()] = (os.path.getsize(os.path.join(dirpath, n)), n)
            except OSError:
                continue
        if files:
            idx.folders[dirpath] = files
            idx.n_files += len(files)
        if on_progress:
            on_progress(len(idx.folders), idx.n_files)
    return idx


def _stem(name: str) -> str:
    return name.rsplit(".", 1)[0] if "." in name else name


def check_card_in_folder(card: Card, index: FolderIndex, verify_hash: bool = False,
                         accept_converted_by_name: bool = False) -> ClearVerdict:
    """カードの全ファイルが、索引のどこかのフォルダに同じ名前・サイズで（manifest があれば manifest の記録と）あれば削除OK。
    accept_converted_by_name: manifest の無いフォルダで、同じ名前の mp4（変換後）があるものも一致とみなす（サイズは比べられない）。"""
    v = ClearVerdict(card=card, ingest=None, mode="folder", checked_destination=index.root)
    if index.error:
        v.reasons.append(index.error)
        return v
    if not card.files:
        v.reasons.append("カードに対象ファイルはありません")
        return v
    norm = card.card_id_norm
    lower_names = {f.name.lower(): f for f in card.files}

    # 1) manifest に同じカードIDがあるフォルダ（camtrapdesk で取り込んだもの）
    for folder, m in index.manifests.items():
        if normalize_card_id(m.get("card_id") or "") != norm:
            continue
        entries: dict[str, dict] = {}
        for e in m.get("files", []) or []:
            entries[(e.get("src_name") or e.get("name") or "").lower()] = e
        broken = {(b.get("name") or "").lower(): b for b in (m.get("broken_files") or [])}
        files_here = index.folders.get(folder, {})
        missing: list[str] = []
        mismatch: list[str] = []
        for f in card.files:
            key = f.name.lower()
            if key in broken:
                v.broken.append(f.name)
                v.deletable.append(f.name)
                continue
            e = entries.get(key)
            if e is None:
                v.extra.append(f.name)
                continue
            src_size = e.get("src_size") if e.get("converted") else e.get("size")
            if src_size is not None and int(src_size) != f.size:
                mismatch.append(f"{f.name}（カード {f.size} / 記録 {src_size}）")
                continue
            dest_name = (e.get("name") or "").lower()
            if dest_name not in files_here or (e.get("size") is not None and files_here[dest_name][0] != int(e["size"])):
                missing.append(e.get("name") or f.name)
                continue
            if verify_hash:
                want = e.get("src_hash") if e.get("converted") else e.get("hash")
                if want and hash_file(f.src_path) != want:
                    mismatch.append(f"{f.name}（ハッシュ不一致）")
                    continue
            v.deletable.append(f.name)
        v.checked_destination = folder
        v.reasons.append(f"照合先: {folder}（manifest のカードID {m.get('card_id')}" + (f"、地点 {m.get('station')}" if m.get("station") else "") + "）")
        if missing:
            v.reasons.append(f"保存先にファイルがありません/サイズ不一致: {missing[0]} ほか{len(missing)}件")
        if mismatch:
            v.reasons.append(f"カードの内容が記録と違います: {mismatch[0]} ほか{len(mismatch)}件")
        if v.extra:
            v.reasons.append(f"記録に無いファイルが{len(v.extra)}件あります（削除しません）")
        if v.broken:
            v.reasons.append(f"取り込み時に壊れていた {len(v.broken)} ファイルも消します")
        v.ok = not missing and not mismatch and bool(v.deletable)
        return v

    # 2) manifest が無い: 同じ名前・サイズのファイルが全部そろっているフォルダを探す
    best: tuple[int, str] | None = None
    best_by_name: dict[str, str] = {}
    for folder, files_here in index.folders.items():
        n = 0
        for key, f in lower_names.items():
            hit = files_here.get(key)
            if hit is not None and hit[0] == f.size:
                n += 1
            elif accept_converted_by_name and (_stem(key) + ".mp4") in files_here:
                n += 1
        if best is None or n > best[0]:
            best = (n, folder)
        if n == len(card.files):
            break
    if best is None or best[0] == 0:
        v.reasons.append(f"{index.root} の中に、このカードのファイルと同じ名前・サイズのものが見つかりません")
        return v
    n, folder = best
    files_here = index.folders[folder]
    v.checked_destination = folder
    if n < len(card.files):
        v.reasons.append(f"一致するフォルダがありません（最も近い {folder} に {n}/{len(card.files)} ファイル）")
        return v
    converted_only = 0
    for f in card.files:
        key = f.name.lower()
        hit = files_here.get(key)
        if hit is not None and hit[0] == f.size:
            if verify_hash and hash_file(f.src_path) != hash_file(os.path.join(folder, hit[1])):
                v.reasons.append(f"ハッシュ不一致: {f.name}")
                return v
            v.deletable.append(f.name)
        else:
            converted_only += 1
            v.deletable.append(f.name)
    v.reasons.append(f"照合先: {folder}（名前とサイズ{'とハッシュ' if verify_hash else ''}が全 {len(card.files)} ファイル一致）"
                     + (f"。うち {converted_only} 件は mp4 に変換済みのものを名前だけで照合" if converted_only else ""))
    v.ok = True
    return v
