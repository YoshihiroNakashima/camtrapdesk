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
    # --- 動画の変換（任意）: 中継先で AVI などを H.264 の mp4 にしてから NAS へ送る ---
    convert_enabled: bool = False
    convert_extensions: list[str] = field(default_factory=lambda: ["avi"])   # 変換する拡張子（mp4 は対象外）
    convert_crf: int = 23            # 画質（libx264 の CRF 相当。18 高画質〜28 小さい。23 が標準）
    convert_encoder: str = "auto"    # auto / libx264 / h264_nvenc / h264_qsv / h264_amf / h264_videotoolbox
    convert_parallel: int = 2        # 同時に変換する本数
    ffmpeg_path: str = ""            # 空なら自動（アプリ同梱 → imageio-ffmpeg → PATH）
    batch_convert_in_place: bool = True   # 「動画を mp4 に変換」: 同じ場所に置き換える（False なら別フォルダへ保存して元を残す）
    batch_convert_out_root: str = ""      # 別フォルダへ保存するときの保存先
    # --- 野帳 ---
    fieldnote_path: str = field(default_factory=_default_fieldnote_path)
    fieldnote_sheet: str = ""      # 空なら最初のシート
    col_station: str = "Station"           # 地点名の列
    col_retrieval_sd: str = "RetrievalSD"  # 回収したカードの ID の列
    card_id_match_tail_number: bool = True   # id.txt の "6_650" と野帳の 650 を同一視する
    # 地点名（フォルダ名）の付け方: 接頭辞＋番号をゼロ埋め。例: BSN + 4桁 → 482 → BSN0482。接頭辞が空なら数字だけ（0482）
    station_prefix: str = "BSN"
    station_digits: int = 4                  # 0 ならゼロ埋めしない
    rename_filters: list[list[str]] = field(default_factory=list)   # 「地点名に変更」で前回使った絞り込み [[列, 値], ...]
    # --- ボリューム検出 ---
    scan_roots: list[str] = field(default_factory=list)   # 追加の検索ルート（テスト・Linux用）
    exclude_volumes: list[str] = field(default_factory=list)
    volume_label_prefix: str = ""
    slot_names: dict[str, str] = field(default_factory=dict)   # ボリューム識別子（リーダーのスロット）→ 人が付けた名前

    # ------------------------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Settings":
        s = cls()
        for k, v in d.items():
            if k == "type_rules" and isinstance(v, dict):
                # 旧形式（Type ごとの規則）からの移行: 最初の規則の接頭辞と桁数を引き継ぐ
                for rule in v.values():
                    if isinstance(rule, dict):
                        s.station_prefix = str(rule.get("station_prefix", s.station_prefix))
                        s.station_digits = int(rule.get("station_pad", s.station_digits) or 0)
                        break
            elif hasattr(s, k) and not k.startswith("_"):
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
