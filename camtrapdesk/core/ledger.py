"""台帳（SQLite）と、カードフォルダごとの manifest.json。

台帳はPCごとに1ファイル。各カードフォルダに置く manifest.json が正本で、
台帳は manifest から再構築できる索引という位置づけ（rebuild_from_manifests）。
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable

MANIFEST_NAME = "manifest.json"

# ingests.status の遷移:
#   copying -> staged (中継先で検証済み) -> synced (NASで検証済み)
#   途中失敗は error。削除後は cleared_at が入る。
STATUS_COPYING = "copying"
STATUS_STAGED = "staged"
STATUS_SYNCED = "synced"
STATUS_ERROR = "error"

SCHEMA = """
CREATE TABLE IF NOT EXISTS ingests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    card_id TEXT NOT NULL,
    card_id_norm TEXT NOT NULL,
    season_folder TEXT NOT NULL,
    dir_name TEXT NOT NULL,
    dest_root TEXT NOT NULL,
    nas_root TEXT,
    source_volume TEXT,
    source_label TEXT,
    station TEXT,
    status TEXT NOT NULL,
    message TEXT,
    n_files INTEGER DEFAULT 0,
    total_bytes INTEGER DEFAULT 0,
    started_at TEXT,
    finished_at TEXT,
    staging_verified_at TEXT,
    nas_verified_at TEXT,
    cleared_at TEXT,
    staging_cleared_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_ingests_card ON ingests(card_id_norm);
CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ingest_id INTEGER NOT NULL REFERENCES ingests(id),
    name TEXT NOT NULL,
    size INTEGER NOT NULL,
    mtime REAL,
    hash TEXT,
    src_path TEXT,
    staging_ok INTEGER DEFAULT 0,
    nas_ok INTEGER DEFAULT 0,
    deleted_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_files_ingest ON files(ingest_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    card_id TEXT,
    message TEXT
);
"""


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Ingest:
    id: int
    card_id: str
    card_id_norm: str
    season_folder: str
    dir_name: str
    dest_root: str
    nas_root: str | None
    source_volume: str | None
    source_label: str | None
    station: str | None
    status: str
    message: str | None
    n_files: int
    total_bytes: int
    started_at: str | None
    finished_at: str | None
    staging_verified_at: str | None
    nas_verified_at: str | None
    cleared_at: str | None
    staging_cleared_at: str | None = None   # NAS検証後に中継のコピーを消した時刻

    @property
    def staging_dir(self) -> str:
        return os.path.join(self.dest_root, self.season_folder, self.dir_name)

    @property
    def nas_dir(self) -> str | None:
        if not self.nas_root:
            return None
        return os.path.join(self.nas_root, self.season_folder, self.dir_name)


@dataclass
class FileRec:
    id: int
    ingest_id: int
    name: str
    size: int
    mtime: float | None
    hash: str | None
    src_path: str | None
    staging_ok: bool
    nas_ok: bool
    deleted_at: str | None


class Ledger:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.RLock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """古い台帳に後から増えた列を足す。"""
        cols = {r["name"] for r in c.execute("PRAGMA table_info(ingests)")}
        if "staging_cleared_at" not in cols:
            c.execute("ALTER TABLE ingests ADD COLUMN staging_cleared_at TEXT")

    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        return c

    # ------------------------------------------------------------ events
    def event(self, kind: str, message: str, card_id: str | None = None) -> None:
        with self._lock, self._conn() as c:
            c.execute("INSERT INTO events(ts, kind, card_id, message) VALUES (?,?,?,?)",
                      (now_iso(), kind, card_id, message))

    def recent_events(self, limit: int = 200) -> list[sqlite3.Row]:
        with self._conn() as c:
            return list(c.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)))

    # ----------------------------------------------------------- ingests
    @staticmethod
    def _row_to_ingest(r: sqlite3.Row) -> Ingest:
        return Ingest(**{k: r[k] for k in r.keys()})

    def create_ingest(self, card_id: str, card_id_norm: str, season_folder: str, dir_name: str,
                      dest_root: str, nas_root: str | None, source_volume: str, source_label: str,
                      n_files: int, total_bytes: int) -> int:
        with self._lock, self._conn() as c:
            cur = c.execute(
                "INSERT INTO ingests(card_id, card_id_norm, season_folder, dir_name, dest_root, nas_root,"
                " source_volume, source_label, status, n_files, total_bytes, started_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, card_id_norm, season_folder, dir_name, dest_root, nas_root or None,
                 source_volume, source_label, STATUS_COPYING, n_files, total_bytes, now_iso()))
            return int(cur.lastrowid)

    def update_ingest(self, ingest_id: int, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE ingests SET {cols} WHERE id = ?", (*fields.values(), ingest_id))

    def get_ingest(self, ingest_id: int) -> Ingest | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM ingests WHERE id = ?", (ingest_id,)).fetchone()
            return self._row_to_ingest(r) if r else None

    def latest_ingest_for_card(self, card_id_norm: str, include_cleared: bool = False) -> Ingest | None:
        q = "SELECT * FROM ingests WHERE card_id_norm = ?"
        if not include_cleared:
            q += " AND cleared_at IS NULL"
        q += " ORDER BY id DESC LIMIT 1"
        with self._conn() as c:
            r = c.execute(q, (card_id_norm,)).fetchone()
            return self._row_to_ingest(r) if r else None

    def ingests(self, status: str | None = None, season_folder: str | None = None,
                limit: int = 1000) -> list[Ingest]:
        q = "SELECT * FROM ingests WHERE 1=1"
        args: list[Any] = []
        if status:
            q += " AND status = ?"
            args.append(status)
        if season_folder:
            q += " AND season_folder = ?"
            args.append(season_folder)
        q += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        with self._conn() as c:
            return [self._row_to_ingest(r) for r in c.execute(q, args)]

    def pending_nas(self) -> list[Ingest]:
        """中継先で検証済みだが NAS 未検証のもの。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ingests WHERE status = ? AND nas_root IS NOT NULL AND nas_root != ''"
                " AND nas_verified_at IS NULL AND cleared_at IS NULL ORDER BY id", (STATUS_STAGED,))
            return [self._row_to_ingest(r) for r in rows]

    def synced_with_staging_copy(self) -> list[Ingest]:
        """NAS で検証済みだが、中継のコピーをまだ消していないもの。"""
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM ingests WHERE status = ? AND nas_root IS NOT NULL AND nas_root != ''"
                " AND staging_cleared_at IS NULL ORDER BY id", (STATUS_SYNCED,))
            return [self._row_to_ingest(r) for r in rows]

    def find_by_dir(self, season_folder: str, dir_name: str) -> Ingest | None:
        with self._conn() as c:
            r = c.execute("SELECT * FROM ingests WHERE season_folder = ? AND dir_name = ?"
                          " ORDER BY id DESC LIMIT 1", (season_folder, dir_name)).fetchone()
            return self._row_to_ingest(r) if r else None

    # ------------------------------------------------------------- files
    def add_files(self, ingest_id: int, rows: Iterable[tuple[str, int, float, str | None]]) -> None:
        """rows: (name, size, mtime, src_path)"""
        with self._lock, self._conn() as c:
            c.executemany("INSERT INTO files(ingest_id, name, size, mtime, src_path) VALUES (?,?,?,?,?)",
                          [(ingest_id, n, s, m, p) for n, s, m, p in rows])

    def set_file_hash(self, ingest_id: int, name: str, hash_: str, staging_ok: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE files SET hash = ?, staging_ok = ? WHERE ingest_id = ? AND name = ?",
                      (hash_, 1 if staging_ok else 0, ingest_id, name))

    def set_file_nas_ok(self, ingest_id: int, name: str, ok: bool) -> None:
        with self._lock, self._conn() as c:
            c.execute("UPDATE files SET nas_ok = ? WHERE ingest_id = ? AND name = ?",
                      (1 if ok else 0, ingest_id, name))

    def mark_files_deleted(self, ingest_id: int, names: Iterable[str]) -> None:
        ts = now_iso()
        with self._lock, self._conn() as c:
            c.executemany("UPDATE files SET deleted_at = ? WHERE ingest_id = ? AND name = ?",
                          [(ts, ingest_id, n) for n in names])

    def files_for(self, ingest_id: int) -> list[FileRec]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM files WHERE ingest_id = ? ORDER BY name", (ingest_id,))
            return [FileRec(id=r["id"], ingest_id=r["ingest_id"], name=r["name"], size=r["size"],
                            mtime=r["mtime"], hash=r["hash"], src_path=r["src_path"],
                            staging_ok=bool(r["staging_ok"]), nas_ok=bool(r["nas_ok"]),
                            deleted_at=r["deleted_at"]) for r in rows]

    def renormalize_card_ids(self) -> int:
        """照合ルールの変更後に、全レコードの card_id_norm を計算し直す。"""
        from .cards import normalize_card_id
        n = 0
        with self._lock, self._conn() as c:
            rows = list(c.execute("SELECT id, card_id, card_id_norm FROM ingests"))
            for r in rows:
                new = normalize_card_id(r["card_id"])
                if new != r["card_id_norm"]:
                    c.execute("UPDATE ingests SET card_id_norm = ? WHERE id = ?", (new, r["id"]))
                    n += 1
        return n

    # ---------------------------------------------------------- manifest
    def write_manifest(self, ingest_id: int, directory: str, extra: dict[str, Any] | None = None) -> str:
        ing = self.get_ingest(ingest_id)
        if ing is None:
            raise ValueError("ingest not found")
        files = self.files_for(ingest_id)
        data: dict[str, Any] = {
            "app": "camtrapdesk",
            "manifest_version": 1,
            "card_id": ing.card_id,
            "season_folder": ing.season_folder,
            "dir_name": ing.dir_name,
            "station": ing.station,
            "source_volume": ing.source_volume,
            "source_label": ing.source_label,
            "started_at": ing.started_at,
            "finished_at": ing.finished_at,
            "staging_verified_at": ing.staging_verified_at,
            "nas_verified_at": ing.nas_verified_at,
            "staging_cleared_at": ing.staging_cleared_at,
            "n_files": len(files),
            "total_bytes": sum(f.size for f in files),
            "files": [{"name": f.name, "size": f.size, "mtime": f.mtime, "hash": f.hash} for f in files],
        }
        if extra:
            data.update(extra)
        os.makedirs(directory, exist_ok=True)
        p = os.path.join(directory, MANIFEST_NAME)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, p)
        return p

    @staticmethod
    def read_manifest(directory: str) -> dict[str, Any] | None:
        p = os.path.join(directory, MANIFEST_NAME)
        if not os.path.isfile(p):
            return None
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, json.JSONDecodeError):
            return None

    def rebuild_from_manifests(self, root: str, nas_root: str | None = None) -> int:
        """root/<season>/<dir>/manifest.json を走査し、台帳に無いものを登録する（別PCの台帳との統合用）。"""
        added = 0
        if not os.path.isdir(root):
            return 0
        for season in sorted(os.listdir(root)):
            sdir = os.path.join(root, season)
            if not os.path.isdir(sdir):
                continue
            for d in sorted(os.listdir(sdir)):
                ddir = os.path.join(sdir, d)
                m = self.read_manifest(ddir)
                if not m:
                    continue
                if self.find_by_dir(season, d):
                    continue
                from .cards import normalize_card_id
                iid = self.create_ingest(m.get("card_id", d), normalize_card_id(m.get("card_id", d)),
                                         season, d, root, nas_root, m.get("source_volume") or "",
                                         m.get("source_label") or "", m.get("n_files", 0), m.get("total_bytes", 0))
                self.add_files(iid, [(f["name"], f["size"], f.get("mtime") or 0.0, None) for f in m.get("files", [])])
                for f in m.get("files", []):
                    if f.get("hash"):
                        self.set_file_hash(iid, f["name"], f["hash"], True)
                status = STATUS_SYNCED if m.get("nas_verified_at") else STATUS_STAGED
                self.update_ingest(iid, status=status, station=m.get("station"),
                                   started_at=m.get("started_at"), finished_at=m.get("finished_at"),
                                   staging_verified_at=m.get("staging_verified_at"),
                                   nas_verified_at=m.get("nas_verified_at"),
                                   staging_cleared_at=m.get("staging_cleared_at"),
                                   message="manifest から再構築")
                added += 1
        return added
