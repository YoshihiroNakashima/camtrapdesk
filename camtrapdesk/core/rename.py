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

    primary_label: str = "中継"
    secondary_label: str = "NAS"

    @property
    def where(self) -> str:
        return "・".join([x for x, ok in ((self.primary_label, self.in_staging), (self.secondary_label, self.in_nas)) if ok])


@dataclass
class RenamePlan:
    """変更するフォルダ（カードIDのフォルダが並んでいる場所）と、あれば対になる場所（中継と NAS の他方）。"""
    primary_dir: str               # 変更するフォルダ
    secondary_dir: str | None      # 対になる場所（中継 ⇔ NAS）。同じ相対位置にあれば一緒に変える
    season_folder: str             # 台帳上の区切りフォルダ名（primary_dir の名前。保存先の直下なら ""）
    items: list[PlanItem]
    issues: list[str]
    primary_label: str = "中継"
    secondary_label: str = "NAS"

    # 旧名との互換
    @property
    def season_dir(self) -> str:
        return self.primary_dir

    @property
    def nas_season_dir(self) -> str | None:
        return self.secondary_dir

    def count(self, action: str) -> int:
        return sum(1 for i in self.items if i.action == action)


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _under(path: str, root: str) -> str | None:
    """path が root の中なら相対パス（root 自身なら ""）、外なら None。"""
    if not path or not root:
        return None
    a = os.path.normcase(os.path.abspath(path))
    r = os.path.normcase(os.path.abspath(root))
    if a == r:
        return ""
    if a.startswith(r.rstrip("\\/") + os.sep):
        return os.path.relpath(os.path.abspath(path), os.path.abspath(root))
    return None


def counterpart_dir(directory: str, dest_root: str | None, nas_root: str | None) -> tuple[str | None, str, str]:
    """directory が保存先（中継）か NAS の中にあれば、他方の同じ相対位置を返す。(対のフォルダ, 自分の名前, 対の名前)。"""
    if _same_path(dest_root, nas_root):
        nas_root = None
    for root, other, label, other_label in ((dest_root, nas_root, "中継", "NAS"), (nas_root, dest_root, "NAS", "中継")):
        rel = _under(directory, root) if root else None
        if rel is not None:
            if other:
                cand = os.path.join(other, rel) if rel else other
                return (cand if os.path.isdir(cand) else None), label, other_label
            return None, label, other_label
    return None, "指定の場所", ""


def season_folder_of(directory: str, dest_root: str | None, nas_root: str | None) -> str:
    """台帳の season_folder に当たる名前。保存先か NAS の直下のフォルダならその名前、ルート自身なら ""、それ以外はフォルダ名。"""
    for root in (dest_root, nas_root):
        rel = _under(directory, root) if root else None
        if rel is not None:
            return rel.replace("\\", "/")
    return os.path.basename(os.path.normpath(directory))


def build_plan(root: str, nas_root: str | None, mapping: Mapping, ledger: L.Ledger | None) -> RenamePlan:
    """保存先（中継）と NAS の区切りフォルダ（mapping.season_folder）を対象に計画を立てる（従来の入口）。"""
    if _same_path(root, nas_root):
        nas_root = None
    if not root and nas_root:
        root, nas_root = nas_root, None
    if not root:
        plan = RenamePlan("", None, mapping.season_folder, [], list(mapping.issues))
        plan.issues.append("変更するフォルダの場所（保存先かNAS）が設定されていません")
        return plan
    primary = os.path.join(root, mapping.season_folder) if mapping.season_folder else root
    secondary = (os.path.join(nas_root, mapping.season_folder) if mapping.season_folder else nas_root) if nas_root else None
    if not os.path.isdir(primary):
        try:
            os.makedirs(primary, exist_ok=True)
        except OSError as e:
            plan = RenamePlan(primary, secondary, mapping.season_folder, [], list(mapping.issues))
            plan.issues.append(f"フォルダを作成できません: {primary} ({e})")
            return plan
        created = True
    else:
        created = False
    plan = build_plan_for_dir(primary, secondary, mapping, ledger, season_folder=mapping.season_folder)
    if created:
        plan.issues.insert(0, f"フォルダを作成しました（まだ取り込んだカードはありません）: {primary}")
    return plan


def build_plan_for_dir(primary_dir: str, secondary_dir: str | None, mapping: Mapping, ledger: L.Ledger | None,
                       season_folder: str | None = None, primary_label: str = "中継",
                       secondary_label: str = "NAS") -> RenamePlan:
    """カードIDのフォルダが並んでいるフォルダ（どこにあってもよい）を対象に計画を立てる。
    secondary_dir（中継と NAS の他方）があれば、同じ名前のフォルダは一緒に変える。"""
    items: list[PlanItem] = []
    issues = list(mapping.issues)
    if secondary_dir and (_same_path(primary_dir, secondary_dir) or not os.path.isdir(secondary_dir)):
        secondary_dir = None
    if season_folder is None:
        season_folder = os.path.basename(os.path.normpath(primary_dir))
    plan = RenamePlan(primary_dir, secondary_dir, season_folder, items, issues, primary_label, secondary_label)
    if not primary_dir or not os.path.isdir(primary_dir):
        issues.append(f"フォルダが見つかりません: {primary_dir or '（未指定）'}")
        return plan

    # フォルダ名 → どこにあるか
    locations: dict[str, dict[str, bool]] = {}
    for key, sdir in (("staging", primary_dir), ("nas", secondary_dir)):
        if not sdir or not os.path.isdir(sdir):
            continue
        try:
            names = os.listdir(sdir)
        except OSError as e:
            issues.append(f"フォルダを読めません: {sdir} ({e})")
            continue
        for d in names:
            if os.path.isdir(os.path.join(sdir, d)) and not d.startswith("."):
                locations.setdefault(d, {"staging": False, "nas": False})[key] = True
    existing = set(locations)

    def manifest_of(d: str):
        for key, sdir in (("staging", primary_dir), ("nas", secondary_dir)):
            if sdir and locations[d][key]:
                m = L.Ledger.read_manifest(os.path.join(sdir, d))
                if m:
                    return m
        return None

    # フォルダ名 → 正規化カードID（manifest があればその card_id を優先）
    dir_by_card: dict[str, str] = {}
    dir_station: dict[str, str] = {}   # 既に地点名になっているフォルダ: dir → card_id
    manifests: dict[str, dict] = {}
    for d in sorted(existing):
        m = manifest_of(d)
        if m:
            manifests[d] = m
        cid = (m or {}).get("card_id") or d
        norm = normalize_card_id(cid)
        if norm in dir_by_card and dir_by_card[norm] != d:
            issues.append(f"同じカードID({cid})のフォルダが複数あります: {dir_by_card[norm]}, {d}")
        dir_by_card.setdefault(norm, d)
        if (m or {}).get("station"):
            dir_station[d] = cid

    def find_ingest(d: str):
        if not ledger:
            return None
        m = manifests.get(d) or {}
        ing = None
        if m.get("season_folder") is not None and m.get("dir_name"):
            ing = ledger.find_by_dir(m["season_folder"], m["dir_name"]) or ledger.find_by_dir(m["season_folder"], d)
        return ing or ledger.find_by_dir(season_folder, d)

    used_targets: set[str] = set()
    for e in mapping.entries:
        d = dir_by_card.get(e.card_id_norm) if e.card_id_norm else None
        ing = find_ingest(d) if d else None
        item = PlanItem(action=A_RENAME, card_id=e.card_id, src_dir_name=d or "",
                        dst_dir_name=e.station_folder, warnings=list(e.warnings), entry=e,
                        ingest_id=ing.id if ing else None, primary_label=primary_label, secondary_label=secondary_label)
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
                              reason="野帳の使う行に対応するカードIDがありません" if d not in dir_station
                              else "変更済み（野帳の別行か、以前の変更）",
                              in_staging=locations[d]["staging"], in_nas=locations[d]["nas"],
                              primary_label=primary_label, secondary_label=secondary_label))
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
            targets.append((plan.primary_label + "側", plan.primary_dir))
        if it.in_nas and plan.secondary_dir:
            targets.append((plan.secondary_label + "側", plan.secondary_dir))
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
            ledger.event("rename", f"{plan.primary_dir}: {it.src_dir_name} → {it.dst_dir_name}", it.card_id)
        else:
            for dst in renamed:
                _write_manifest_fallback(dst, it.card_id, it.dst_dir_name, it.src_dir_name)
        log(f"{it.src_dir_name} → {it.dst_dir_name}（{it.where}）")
        done += 1
    return done, errors
