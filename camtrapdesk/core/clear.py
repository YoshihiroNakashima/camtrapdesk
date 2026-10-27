"""削除（clear）: 挿入中のカードを台帳と突合し、検証済みのファイルだけを削除する。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..config import Settings
from . import ledger as L
from .cards import Card
from .hashing import hash_file
from .roots import resolve_staging_dir


@dataclass
class ClearVerdict:
    card: Card
    ingest: L.Ingest | None
    ok: bool = False                      # 削除してよい
    deletable: list[str] = field(default_factory=list)    # 削除するファイル名
    extra: list[str] = field(default_factory=list)        # 台帳に無い（残す）ファイル
    reasons: list[str] = field(default_factory=list)      # 削除しない理由・注意
    checked_destination: str = ""

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

    recs = {f.name: f for f in ledger.files_for(ing.id) if not f.deleted_at}
    mismatch = 0
    for f in card.files:
        r = recs.get(f.name)
        if r is None:
            v.extra.append(f.name)
            continue
        if r.size != f.size:
            mismatch += 1
            v.reasons.append(f"サイズ不一致: {f.name}（カード {f.size} / 台帳 {r.size}）")
            continue
        if verify_hash and r.hash:
            if hash_file(f.src_path) != r.hash:
                mismatch += 1
                v.reasons.append(f"ハッシュ不一致: {f.name}")
                continue
        v.deletable.append(f.name)
    if v.extra:
        v.reasons.append(f"台帳に無いファイルが{len(v.extra)}件あります（削除しません）")

    # 最終保存先（NASがあればNAS、無ければ中継先）に実体があるか
    if ing.nas_root and ing.status == L.STATUS_SYNCED:
        dest_dir = ing.nas_dir or ""
    else:
        dest_dir = resolve_staging_dir(ing, s, ledger) or ing.staging_dir
    v.checked_destination = dest_dir
    if os.path.isdir(dest_dir):
        missing = []
        for name in v.deletable:
            p = os.path.join(dest_dir, name)
            if not os.path.isfile(p) or os.path.getsize(p) != recs[name].size:
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
    if not v.ok or v.ingest is None:
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
    ledger.mark_files_deleted(v.ingest.id, deleted)
    if deleted and not errors:
        remaining = [f for f in ledger.files_for(v.ingest.id) if not f.deleted_at]
        if not remaining:
            ledger.update_ingest(v.ingest.id, cleared_at=L.now_iso())
    ledger.event("clear", f"{v.card.card_id}: {len(deleted)} files deleted" + (f", {len(errors)} errors" if errors else ""),
                 v.card.card_id)
    log(f"[{v.card.card_id}] {len(deleted)}ファイルを削除" + (f"（エラー{len(errors)}件）" if errors else ""))
    return len(deleted), errors
