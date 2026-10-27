"""NAS への転送係。中継先で検証済みのカードフォルダを順に NAS へコピーし、読み戻して検証する。

NAS に届かないときは待って再試行し、再起動後も台帳の未完了分から再開する。
"""

from __future__ import annotations

import os
import threading
from typing import Callable

from ..config import Settings
from . import ledger as L
from .hashing import copy_with_hash, hash_file


class NasSyncer:
    def __init__(self, get_settings: Callable[[], Settings], ledger: L.Ledger,
                 on_status: Callable[[str], None], on_log: Callable[[str], None],
                 on_progress: Callable[[str, float], None] | None = None,
                 is_ingest_busy: Callable[[], bool] | None = None):
        self.get_settings = get_settings
        self.is_ingest_busy = is_ingest_busy
        self.ledger = ledger
        self.on_status = on_status
        self.on_log = on_log
        self.on_progress = on_progress
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.current: str | None = None   # 転送中の "season/dir"
        self.last_error: str | None = None
        self.paused = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nas-sync", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = 15.0
            try:
                wait = self._tick()
            except Exception as e:  # pragma: no cover
                self.last_error = str(e)
                self.on_status(f"NAS転送でエラー: {e}")
                self.on_log(f"NAS転送でエラー: {e}")
            self._wake.wait(wait)
            self._wake.clear()

    def _tick(self) -> float:
        """1回分の処理を行い、次に起きるまでの秒数を返す。"""
        if self.paused:
            self.on_status("NAS転送: 一時停止中")
            return 5.0
        pending = self.ledger.pending_nas()
        if not pending:
            self.on_status("NAS転送: 待機中（未転送なし）")
            return 15.0
        if self.get_settings().nas_wait_while_ingesting and self.is_ingest_busy and self.is_ingest_busy():
            self.on_status(f"NAS転送: カードの取り込み中のため待機（{len(pending)}件）。取り込みが終わると再開します")
            return 5.0
        ing = pending[0]
        nas_root = ing.nas_root or ""
        if not nas_root or not os.path.isdir(nas_root):
            self.on_status(f"NAS転送: NASに接続できません（{nas_root}）。{len(pending)}件待機。30秒後に再試行")
            return 30.0
        self.current = f"{ing.season_folder}/{ing.dir_name}"
        self.on_status(f"NAS転送: {self.current} を転送中（残り{len(pending)}件）")
        self._sync_one(ing)
        self.current = None
        return 0.5   # 続けて次へ

    def _sync_one(self, ing: L.Ingest) -> None:
        s = self.get_settings()
        src_dir = ing.staging_dir
        dst_dir = ing.nas_dir
        assert dst_dir
        if not os.path.isdir(src_dir):
            self.ledger.update_ingest(ing.id, status=L.STATUS_ERROR, message=f"中継先フォルダが見つかりません: {src_dir}")
            self.on_log(f"[{ing.card_id}] 中継先フォルダが見つかりません: {src_dir}")
            return
        files = [f for f in self.ledger.files_for(ing.id) if not f.deleted_at]
        total = sum(f.size for f in files) or 1
        os.makedirs(dst_dir, exist_ok=True)
        done = 0
        for f in files:
            if self._stop.is_set():
                return
            src = os.path.join(src_dir, f.name)
            dst = os.path.join(dst_dir, f.name)
            if not os.path.isfile(src):
                self.ledger.update_ingest(ing.id, status=L.STATUS_ERROR, message=f"中継先にファイルがありません: {f.name}")
                self.on_log(f"[{ing.card_id}] 中継先にファイルがありません: {f.name}")
                return
            # 既に NAS にあり、サイズとハッシュが一致すればコピーしない（再開）
            if f.nas_ok and os.path.isfile(dst) and os.path.getsize(dst) == f.size:
                done += f.size
                continue
            if os.path.isfile(dst) and os.path.getsize(dst) == f.size and f.hash:
                if hash_file(dst) == f.hash:
                    self.ledger.set_file_nas_ok(ing.id, f.name, True)
                    done += f.size
                    self._prog(ing, done / total)
                    continue
            base = done

            def prog(n: int, base=base):
                self._prog(ing, (base + n) / total)

            try:
                _, h = copy_with_hash(src, dst, prog)
            except OSError as e:
                self.on_log(f"[{ing.card_id}] NASへのコピー失敗: {f.name} ({e})。後で再試行します")
                self.last_error = str(e)
                return
            if f.hash and h != f.hash:
                self.ledger.update_ingest(ing.id, status=L.STATUS_ERROR,
                                          message=f"NASへ転送したファイルのハッシュが中継先と不一致: {f.name}")
                self.on_log(f"[{ing.card_id}] 中継先のファイルが台帳のハッシュと一致しません: {f.name}")
                return
            if s.nas_verify_readback:
                h2 = hash_file(dst)
                if h2 != h:
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                    self.on_log(f"[{ing.card_id}] NAS上の読み戻しハッシュ不一致: {f.name}。再試行します")
                    return
            self.ledger.set_file_nas_ok(ing.id, f.name, True)
            done += f.size
            self._prog(ing, done / total)

        ts = L.now_iso()
        self.ledger.update_ingest(ing.id, status=L.STATUS_SYNCED, nas_verified_at=ts, message="NASで検証済み")
        self.ledger.write_manifest(ing.id, dst_dir)
        self.ledger.write_manifest(ing.id, src_dir)
        self.ledger.event("nas_done", f"{ing.season_folder}/{ing.dir_name} → {dst_dir}", ing.card_id)
        self.on_log(f"[{ing.card_id}] NAS転送・検証完了 → {dst_dir}")

    def _prog(self, ing: L.Ingest, frac: float) -> None:
        if self.on_progress:
            self.on_progress(f"{ing.season_folder}/{ing.dir_name}", frac)
