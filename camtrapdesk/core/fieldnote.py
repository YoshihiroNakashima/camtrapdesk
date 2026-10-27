"""野帳（Excel）の読み込みと、カードID→地点名の対応表づくり。"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, date
from typing import Any

from openpyxl import load_workbook

from ..config import Settings, TypeRule
from .cards import normalize_card_id


def _s(v: Any) -> str:
    """セルの値を文字列に。数値は整数表記、日付は ISO。"""
    if v is None:
        return ""
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    return str(v).strip()


@dataclass
class FieldnoteRow:
    row_no: int               # Excel の行番号（1始まり、見出しを含む）
    season: str
    station: str
    type_code: str
    retrieval_sd: str
    installation_sd: str
    date: str = ""
    state: str = ""
    remarks: str = ""


@dataclass
class Fieldnote:
    path: str
    sheet: str
    sheets: list[str]
    header: list[str]
    rows: list[FieldnoteRow]

    def types(self) -> list[str]:
        seen: dict[str, int] = {}
        for r in self.rows:
            if r.type_code:
                seen[r.type_code] = seen.get(r.type_code, 0) + 1
        return sorted(seen, key=lambda k: (-seen[k], k))

    def seasons(self, type_code: str | None = None) -> list[str]:
        vals = {r.season for r in self.rows if r.season and (type_code is None or r.type_code == type_code)}
        return sorted(vals, reverse=True)

    def select(self, type_code: str, season: str) -> list[FieldnoteRow]:
        return [r for r in self.rows if r.type_code == type_code and r.season == season]


def list_sheets(path: str) -> list[str]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        return list(wb.sheetnames)
    finally:
        wb.close()


def load_fieldnote(path: str, s: Settings, sheet: str | None = None) -> Fieldnote:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet_name = sheet or s.fieldnote_sheet or wb.sheetnames[0]
        if sheet_name not in wb.sheetnames:
            raise ValueError(f"シート '{sheet_name}' がありません。あるシート: {', '.join(wb.sheetnames)}")
        ws = wb[sheet_name]
        it = ws.iter_rows(values_only=True)
        header = [_s(h) for h in next(it)]
        idx = {h: i for i, h in enumerate(header) if h}
        need = {"season": s.col_season, "station": s.col_station, "type": s.col_type,
                "retrieval": s.col_retrieval_sd, "installation": s.col_installation_sd}
        missing = [c for c in need.values() if c not in idx]
        if missing:
            raise ValueError("野帳に列がありません: " + ", ".join(missing) + "（設定で列名を確認してください）")

        def get(row: tuple, name: str) -> str:
            i = idx.get(name)
            return _s(row[i]) if i is not None and i < len(row) else ""

        rows: list[FieldnoteRow] = []
        for n, row in enumerate(it, start=2):
            if row is None or all(v is None for v in row):
                continue
            rows.append(FieldnoteRow(
                row_no=n,
                season=get(row, s.col_season),
                station=get(row, s.col_station),
                type_code=get(row, s.col_type),
                retrieval_sd=get(row, s.col_retrieval_sd),
                installation_sd=get(row, s.col_installation_sd),
                date=get(row, "Date"),
                state=get(row, "State"),
                remarks=get(row, "Remarks"),
            ))
        return Fieldnote(path=path, sheet=sheet_name, sheets=list(wb.sheetnames), header=header, rows=rows)
    finally:
        wb.close()


# ----------------------------------------------------------------------------
# 名前の整形
# ----------------------------------------------------------------------------

def format_station(raw: str, rule: TypeRule) -> str:
    """地点名を整形する。数値なら接頭辞＋ゼロ埋め（482 → BSN0482）。
    文字列で末尾が数字なら末尾をゼロ埋め（BSN730 → BSN0730）、英字の接頭辞が無ければ接頭辞を付ける。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.isdigit():
        return f"{rule.station_prefix}{int(s):0{rule.station_pad}d}"
    m = re.match(r"^(.*?)(\d+)$", s)
    if m:
        head, num = m.group(1), m.group(2)
        padded = f"{int(num):0{rule.station_pad}d}"
        if re.search(r"[A-Za-z]", head):
            return f"{head}{padded}"
        return f"{rule.station_prefix}{padded}"
    return s


def format_season_folder(season: str, rule: TypeRule) -> str:
    s = (season or "").strip()
    if not s:
        return ""
    if s.startswith(rule.season_prefix):
        return s
    return f"{rule.season_prefix}{s}"


# ----------------------------------------------------------------------------
# 対応表
# ----------------------------------------------------------------------------

@dataclass
class MapEntry:
    card_id: str            # 野帳の値（文字列化）
    card_id_norm: str
    station_folder: str     # 整形後の地点名（フォルダ名）
    row_no: int
    date: str = ""
    state: str = ""
    remarks: str = ""
    prev_installation_sd: str = ""   # 同じ地点の前回設置SD（突合用）
    warnings: list[str] = field(default_factory=list)   # 変更は可能だが注意
    errors: list[str] = field(default_factory=list)     # この行は変更しない


@dataclass
class Mapping:
    type_code: str
    season: str
    season_folder: str
    entries: list[MapEntry]
    issues: list[str]       # 対応表全体に関する問題点

    def by_card(self) -> dict[str, list[MapEntry]]:
        d: dict[str, list[MapEntry]] = {}
        for e in self.entries:
            d.setdefault(e.card_id_norm, []).append(e)
        return d


def build_mapping(fn: Fieldnote, type_code: str, season: str, s: Settings,
                  use_installation: bool = False) -> Mapping:
    """選んだ Type と Season の行から、カードID→地点フォルダ名の対応表を作る。

    重複（同じカードIDが複数行、整形後の地点名が複数行）は該当行にエラーを付け、
    それ以外の行は変更可能なままにする。前回設置SDとの突合は警告として付ける。
    """
    rule = s.rule_for(type_code)
    rows = fn.select(type_code, season)
    season_folder = format_season_folder(season, rule)
    entries: list[MapEntry] = []
    issues: list[str] = []
    if not rows:
        issues.append(f"Type={type_code}, Season={season} の行がありません")
        return Mapping(type_code, season, season_folder, [], issues)

    # 前回設置SDの索引: (type, station整形後) → 時系列で最後の installation_sd（この season より前）
    prev_inst: dict[str, tuple[str, str]] = {}
    for r in fn.rows:
        if r.type_code != type_code or not r.installation_sd:
            continue
        if r.season >= season:
            continue
        st = format_station(r.station, rule)
        cur = prev_inst.get(st)
        if cur is None or r.season > cur[0]:
            prev_inst[st] = (r.season, r.installation_sd)

    for r in rows:
        sd = r.installation_sd if use_installation else r.retrieval_sd
        st = format_station(r.station, rule)
        e = MapEntry(card_id=sd, card_id_norm=normalize_card_id(sd) if sd else "", station_folder=st,
                     row_no=r.row_no, date=r.date, state=r.state, remarks=r.remarks)
        if not sd:
            e.errors.append("SD列が空")
        if not st:
            e.errors.append("Station列が空")
        pi = prev_inst.get(st)
        if pi:
            e.prev_installation_sd = pi[1]
            if sd and normalize_card_id(pi[1]) != normalize_card_id(sd):
                e.warnings.append(f"前回設置SD({pi[1]}, {pi[0]})と今回回収SD({sd})が不一致")
        entries.append(e)

    # 重複チェック
    by_card: dict[str, list[MapEntry]] = {}
    by_station: dict[str, list[MapEntry]] = {}
    for e in entries:
        if e.card_id_norm:
            by_card.setdefault(e.card_id_norm, []).append(e)
        if e.station_folder:
            by_station.setdefault(e.station_folder, []).append(e)
    for cid, es in by_card.items():
        if len(es) > 1:
            sts = ", ".join(x.station_folder for x in es)
            for x in es:
                x.errors.append(f"カードID {x.card_id} が{len(es)}行にあります（{sts}）")
            issues.append(f"カードID {es[0].card_id} が複数の地点に記録されています: {sts}（行 {', '.join(str(x.row_no) for x in es)}）")
    for st, es in by_station.items():
        if len(es) > 1:
            for x in es:
                x.errors.append(f"地点 {st} が{len(es)}行にあります")
            issues.append(f"地点 {st} が複数行にあります（行 {', '.join(str(x.row_no) for x in es)}）")
    return Mapping(type_code, season, season_folder, entries, issues)
