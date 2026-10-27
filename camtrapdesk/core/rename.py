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
    in_staging: bool = False   # 中継側にフォルダがある
    in_nas: bool = False       # NAS側にフォルダがある

    @property
    def where(self) -> str:
        return "・".join([x for x, ok in (("中継", self.in_staging), ("NAS", self.in_nas)) if ok])


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

    @property
    def nas_season_dir(self) -> str | None:
        return os.path.join(self.nas_root, self.season_folder) if self.nas_root else None

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def build_plan(root: str, nas_root: str | None, mapping: Mapping, ledger: L.Ledger | None) -> RenamePlan:
    """中継先と NAS の両方を見て計画を立てる。中継のコピーが（NAS検証後の自動削除で）無くても NAS 側だけ変更できる。"""
    items: list[PlanItem] = []
    issues = list(mapping.issues)
    if _same_path(root, nas_root):
        nas_root = None
    if not root and nas_root:
        root, nas_root = nas_root, None
    plan = RenamePlan(root, nas_root or None, mapping.season_folder, items, issues)
    if not root:
        issues.append("変更するフォルダの場所（保存先かNAS）が設定されていません")
        return plan
    season_dir = plan.season_dir
    if not os.path.isdir(season_dir):
        try:
            os.makedirs(season_dir, exist_ok=True)
            issues.append(f"シーズンフォルダを作成しました（まだ取り込んだカードはありません）: {season_dir}")
        except OSError as e:
            issues.append(f"シーズンフォルダを作成できません: {season_dir} ({e})")
            return plan

    # フォルダ名 → どこにあるか
    locations: dict[str, dict[str, bool]] = {}
    for key, sdir in (("staging", season_dir), ("nas", plan.nas_season_dir)):
        if not sdir or not os.path.isdir(sdir):
            continue
        for d in os.listdir(sdir):
            if os.path.isdir(os.path.join(sdir, d)):
                locations.setdefault(d, {"staging": False, "nas": False})[key] = True
    existing = set(locations)

    def manifest_of(d: str):
        for key, sdir in (("staging", season_dir), ("nas", plan.nas_season_dir)):
            if sdir and locations[d][key]:
                m = L.Ledger.read_manifest(os.path.join(sdir, d))
                if m:
                    return m
        return None

    # フォルダ名 → 正規化カードID（manifest があればその card_id を優先）
    dir_by_card: dict[str, str] = {}
    dir_station: dict[str, str] = {}   # 既に地点名になっているフォルダ: dir → card_id
    for d in sorted(existing):
        m = manifest_of(d)
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
        if d:
            item.in_staging = locations[d]["staging"]
            item.in_nas = locations[d]["nas"]
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
        items.append(item)

    mapped_dirs = {i.src_dir_name for i in items if i.src_dir_name}
    for d in sorted(existing - mapped_dirs):
        cid = dir_station.get(d, d)
        items.append(PlanItem(action=A_UNMAPPED, card_id=cid, src_dir_name=d, dst_dir_name="",
                              reason="野帳のこの Season に対応する行がありません" if d not in dir_station
                              else "変更済み（野帳の別行か、以前の変更）",
                              in_staging=locations[d]["staging"], in_nas=locations[d]["nas"]))
    return plan


def _write_manifest_fallback(dst: str, card_id: str, station: str, renamed_from: str) -> None:
    """台帳に無いフォルダ（旧Rコードで取り込んだもの等）にも manifest だけは残す。"""
    m = L.Ledger.read_manifest(dst) or {}
    m.update({"card_id": card_id, "station": station, "renamed_from": renamed_from})
    try:
        import json
        with open(os.path.join(dst, L.MANIFEST_NAME), "w", encoding="utf-8") as f:
            json.dump(m, f, ensure_ascii=False, indent=1)
    except OSError:
        pass


def apply_plan(plan: RenamePlan, ledger: L.Ledger | None, log=print) -> tuple[int, list[str]]:
    """計画のうち action==rename の項目を実行する。中継と NAS の両方にあれば両方変える。戻り値は (成功数, エラー一覧)。"""
    done = 0
    errors: list[str] = []
    for it in plan.items:
        if it.action != A_RENAME:
            continue
        renamed: list[str] = []   # 変更できた場所のフォルダパス
        targets: list[tuple[str, str]] = []
        if it.in_staging:
            targets.append(("中継側", plan.season_dir))
        if it.in_nas and plan.nas_season_dir:
            targets.append(("NAS側", plan.nas_season_dir))
        for label, sdir in targets:
            src = os.path.join(sdir, it.src_dir_name)
            dst = os.path.join(sdir, it.dst_dir_name)
            try:
                if os.path.exists(dst):
                    raise FileExistsError(dst)
                os.rename(src, dst)
                renamed.append(dst)
            except OSError as e:
                note = "（他方は変更済み）" if renamed else ""
                errors.append(f"{label} {it.src_dir_name} → {it.dst_dir_name}: {e}{note}")
        if not renamed:
            continue
        if ledger and it.ingest_id:
            ledger.update_ingest(it.ingest_id, dir_name=it.dst_dir_name, station=it.dst_dir_name)
            for dst in renamed:
                ledger.write_manifest(it.ingest_id, dst, {"renamed_from": it.src_dir_name})
            ledger.event("rename", f"{plan.season_folder}: {it.src_dir_name} → {it.dst_dir_name}", it.card_id)
        else:
            for dst in renamed:
                _write_manifest_fallback(dst, it.card_id, it.dst_dir_name, it.src_dir_name)
        log(f"{it.src_dir_name} → {it.dst_dir_name}（{it.where}）")
        done += 1
    return done, errors
