"""設定の読み書き。OSごとの設定ディレクトリに JSON で保存する。"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

from . import APP_NAME


def config_dir() -> Path:
    """OSごとの設定・台帳の保存先。環境変数 CAMTRAPDESK_HOME で上書きできる。"""
    override = os.environ.get("CAMTRAPDESK_HOME")
    if override:
        p = Path(override)
    elif sys.platform == "win32":
        p = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / APP_NAME
    elif sys.platform == "darwin":
        p = Path.home() / "Library" / "Application Support" / APP_NAME
    else:
        p = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def _default_fieldnote_path() -> str:
    return ""   # 野帳の場所は「地点名に変更」画面で選ぶ


@dataclass
class TypeRule:
    """野帳の Type ごとの、Season / Station のフォルダ名整形ルール。
    既定（接頭辞なし・ゼロ埋めなし）では野帳の値をそのままフォルダ名にする。
    例: Type N に season_prefix="N", station_prefix="BSN", station_pad=4 を設定すると 260710 → N260710、482 → BSN0482。"""
    season_prefix: str = ""
    station_prefix: str = ""
    station_pad: int = 0

    @property
    def is_plain(self) -> bool:
        return not self.season_prefix and not self.station_prefix and not self.station_pad


@dataclass
class Settings:
    # --- 取り込み先 ---
    dest_root: str = ""            # 中継（または直接NAS）の保存先ルート
    nas_root: str = ""             # NASの最終保存先。空ならNAS転送は行わない
    season_folder: str = ""        # 現在の取り込み先フォルダ名（例: N260710）
    # --- SDカード内の対象 ---
    media_subdirs: list[str] = field(default_factory=lambda: ["DCIM/100_BTCF"])
    media_recursive: bool = False
    media_extensions: list[str] = field(default_factory=lambda: ["mp4", "mov", "avi", "jpg", "jpeg", "thm"])
    min_file_size_bytes: int = 1024          # これより小さいファイルは注意として表示する（コピーは行う）
    min_file_count: int = 2                  # 動画がこの枚数未満ならカメラ不調とみなしてエラー（コピーしない）
    id_file_name: str = "id.txt"
    # --- 取り込みの挙動 ---
    concurrency: int = 4
    verify_readback: bool = True
    nas_verify_readback: bool = True
    delete_requires_nas: bool = True     # 削除にNAS検証済みを要求する（nas_root が空なら無視）
    nas_wait_while_ingesting: bool = True  # カードの取り込み中はNAS転送を待たせる（中継HDDの取り合いを避ける）
    staging_cleanup_after_nas: bool = False  # NASで検証済みになったカードは中継のコピーを消す（中継の容量を小さくしたいとき）
    min_free_gb: float = 20.0              # 中継の空きがカード容量＋この余裕を下回るなら、空くまでコピーを待たせる
    poll_interval_sec: float = 3.0
    auto_start_ingest: bool = True
    # --- 野帳 ---
    fieldnote_path: str = field(default_factory=_default_fieldnote_path)
    fieldnote_sheet: str = ""      # 空なら最初のシート
    col_season: str = "Season"
    col_station: str = "Station"
    col_type: str = "Type"
    col_retrieval_sd: str = "RetrievalSD"
    card_id_match_tail_number: bool = True   # id.txt の "6_650" と野帳の 650 を同一視する
    default_type: str = ""                   # 「地点名に変更」で最初に選ぶ Type（空なら野帳で最も多いもの）
    type_rules: dict[str, TypeRule] = field(default_factory=dict)   # Type ごとの整形ルール（無ければそのまま）
    # --- ボリューム検出 ---
    scan_roots: list[str] = field(default_factory=list)   # 追加の検索ルート（テスト・Linux用）
    exclude_volumes: list[str] = field(default_factory=list)
    volume_label_prefix: str = ""
    slot_names: dict[str, str] = field(default_factory=dict)   # ボリューム識別子（リーダーのスロット）→ 人が付けた名前

    # ------------------------------------------------------------------
    def rule_for(self, type_code: str) -> TypeRule:
        """Type の整形ルール。未設定なら野帳の値をそのまま使う。"""
        return self.type_rules.get(type_code) or self.type_rules.get("*") or TypeRule()

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["type_rules"] = {k: asdict(v) for k, v in self.type_rules.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        s = cls()
        for k, v in d.items():
            if k == "type_rules" and isinstance(v, dict):
                s.type_rules = {kk: TypeRule(**vv) for kk, vv in v.items()}
            elif hasattr(s, k):
                setattr(s, k, v)
        return s


def settings_path() -> Path:
    return config_dir() / "settings.json"


def load_settings() -> Settings:
    p = settings_path()
    if p.exists():
        try:
            return Settings.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except Exception:
            # 壊れた設定ファイルは退避して既定値で起動する
            p.rename(p.with_suffix(".json.broken"))
    return Settings()


def save_settings(s: Settings) -> None:
    p = settings_path()
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(s.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)
