"""カードIDのフォルダ名を地点名に変更する（ドライラン→適用）。"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

from . import ledger as L
from .cards import normalize_card_id
from .fieldnote import Mapping, MapEntry

A_RENAME = "rename"            # 変更する
A_ALREADY = "already"          # 既に地点名になっている
A_MISSING = "missing"          # 野帳にあるがフォルダが無い
A_UNMAPPED = "unmapped"        # フォルダはあるが野帳に無い
A_BLOCKED = "blocked"          # 野帳側の問題（重複など）で変更しない
A_TARGET_EXISTS = "target_exists"
A_SYNCING = "syncing"          # NAS転送中なので今は変更しない

ACTION_LABELS = {
    A_RENAME: "変更",
    A_ALREADY: "変更済み",
    A_MISSING: "フォルダ無し",
    A_UNMAPPED: "野帳に無い",
    A_BLOCKED: "保留（要確認）",
    A_TARGET_EXISTS: "変更先が既に存在",
    A_SYNCING: "NAS転送中",
}


@dataclass
class PlanItem:
    action: str
    card_id: str
    src_dir_name: str          # 現在のフォルダ名（無ければ ""）
    dst_dir_name: str          # 変更後のフォルダ名（無ければ ""）
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    entry: MapEntry | None = None
    ingest_id: int | None = None
    in_nas: bool = False


@dataclass
class RenamePlan:
    root: str                  # 中継先（または最終保存先）のルート
    nas_root: str | None
    season_folder: str
    items: list[PlanItem]
    issues: list[str]

    @property
    def season_dir(self) -> str:
        return os.path.join(self.root, self.season_folder)

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)


def build_plan(root: str, nas_root: str | None, mapping: Mapping, ledger: L.Ledger | None) -> RenamePlan:
    season_dir = os.path.join(root, mapping.season_folder)
    items: list[PlanItem] = []
    issues = list(mapping.issues)
    if not os.path.isdir(season_dir):
        try:
            os.makedirs(season_dir, exist_ok=True)
            issues.append(f"シーズンフォルダを作成しました（まだ取り込んだカードはありません）: {season_dir}")
        except OSError as e:
            issues.append(f"シーズンフォルダを作成できません: {season_dir} ({e})")
            return RenamePlan(root, nas_root, mapping.season_folder, items, issues)

    existing = {d for d in os.listdir(season_dir) if os.path.isdir(os.path.join(season_dir, d))}
    # フォルダ名 → 正規化カードID（manifest があればその card_id を優先）
    dir_by_card: dict[str, str] = {}
    dir_station: dict[str, str] = {}   # 既に地点名になっているフォルダ: dir → card_id
    for d in existing:
        m = L.Ledger.read_manifest(os.path.join(season_dir, d))
        cid = (m or {}).get("card_id") or d
        norm = normalize_card_id(cid)
        if norm in dir_by_card and dir_by_card[norm] != d:
            issues.append(f"同じカードID({cid})のフォルダが複数あります: {dir_by_card[norm]}, {d}")
        dir_by_card.setdefault(norm, d)
        if (m or {}).get("station"):
            dir_station[d] = cid

    used_targets: set[str] = set()
    for e in mapping.entries:
        d = dir_by_card.get(e.card_id_norm) if e.card_id_norm else None
        ing = None
        if ledger and d:
            ing = ledger.find_by_dir(mapping.season_folder, d)
        item = PlanItem(action=A_RENAME, card_id=e.card_id, src_dir_name=d or "",
                        dst_dir_name=e.station_folder, warnings=list(e.warnings), entry=e,
                        ingest_id=ing.id if ing else None)
        if e.errors:
            item.action = A_BLOCKED
            item.reason = "; ".join(e.errors)
        elif d is None:
            item.action = A_MISSING
            item.reason = "このカードIDのフォルダが見つかりません（未取り込み、または別の名前）"
        elif d == e.station_folder:
            item.action = A_ALREADY
        elif e.station_folder in existing or e.station_folder in used_targets:
            item.action = A_TARGET_EXISTS
            item.reason = f"{e.station_folder} というフォルダが既にあります"
        elif ing and ing.status == L.STATUS_STAGED and ing.nas_root and not ing.nas_verified_at:
            item.action = A_SYNCING
            item.reason = "NASへの転送が終わってから変更します"
        if item.action == A_RENAME:
            used_targets.add(e.station_folder)
            if nas_root:
                item.in_nas = os.path.isdir(os.path.join(nas_root, mapping.season_folder, d))
        items.append(item)

    mapped_dirs = {i.src_dir_name for i in items if i.src_dir_name}
    for d in sorted(existing - mapped_dirs):
        cid = dir_station.get(d, d)
        items.append(PlanItem(action=A_UNMAPPED, card_id=cid, src_dir_name=d, dst_dir_name="",
                              reason="野帳のこの Season に対応する行がありません" if d not in dir_station
                              else "変更済み（野帳の別行か、以前の変更）"))
    return RenamePlan(root, nas_root, mapping.season_folder, items, issues)


def apply_plan(plan: RenamePlan, ledger: L.Ledger | None, log=print) -> tuple[int, list[str]]:
    """計画のうち action==rename の項目を実行する。戻り値は (成功数, エラー一覧)。"""
    done = 0
    errors: list[str] = []
    for it in plan.items:
        if it.action != A_RENAME:
            continue
        src = os.path.join(plan.season_dir, it.src_dir_name)
        dst = os.path.join(plan.season_dir, it.dst_dir_name)
        try:
            if os.path.exists(dst):
                raise FileExistsError(dst)
            os.rename(src, dst)
        except OSError as e:
            errors.append(f"{it.src_dir_name} → {it.dst_dir_name}: {e}")
            continue
        nas_ok = True
        if plan.nas_root and it.in_nas:
            nsrc = os.path.join(plan.nas_root, plan.season_folder, it.src_dir_name)
            ndst = os.path.join(plan.nas_root, plan.season_folder, it.dst_dir_name)
            try:
                if os.path.exists(ndst):
                    raise FileExistsError(ndst)
                os.rename(nsrc, ndst)
            except OSError as e:
                nas_ok = False
                errors.append(f"NAS側 {it.src_dir_name} → {it.dst_dir_name}: {e}（中継側は変更済み）")
        if ledger and it.ingest_id:
            ledger.update_ingest(it.ingest_id, dir_name=it.dst_dir_name, station=it.dst_dir_name)
            ledger.write_manifest(it.ingest_id, dst, {"renamed_from": it.src_dir_name})
            if plan.nas_root and it.in_nas and nas_ok:
                ledger.write_manifest(it.ingest_id, os.path.join(plan.nas_root, plan.season_folder, it.dst_dir_name),
                                      {"renamed_from": it.src_dir_name})
            ledger.event("rename", f"{plan.season_folder}: {it.src_dir_name} → {it.dst_dir_name}", it.card_id)
        else:
            # 台帳に無いフォルダ（旧Rコードで取り込んだもの等）も manifest だけは残す
            m = L.Ledger.read_manifest(dst) or {}
            m.update({"card_id": it.card_id, "station": it.dst_dir_name, "renamed_from": it.src_dir_name})
            try:
                import json
                with open(os.path.join(dst, L.MANIFEST_NAME), "w", encoding="utf-8") as f:
                    json.dump(m, f, ensure_ascii=False, indent=1)
            except OSError:
                pass
        log(f"{it.src_dir_name} → {it.dst_dir_name}")
        done += 1
    return done, errors
