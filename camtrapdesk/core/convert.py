"""変換係: 中継先で検証済みのカードフォルダにある AVI などを、順に H.264 の mp4 へ変換する（任意機能）。

流れ: SD → 中継（元のまま検証コピー）→ ここで mp4 に変換して元ファイルを消す → NAS へは mp4 だけ送る。
変換が終わるまでそのカードの NAS 転送は待つ（ledger.pending_nas が convert_pending を見る）。
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable

from ..config import Settings
from . import ledger as L
from .ffmpeg import convert_to_mp4, detect_encoder, find_ffmpeg
from .hashing import hash_file
from .roots import resolve_staging_dir


class Converter:
    def __init__(self, get_settings: Callable[[], Settings], ledger: L.Ledger,
                 on_status: Callable[[str], None], on_log: Callable[[str], None],
                 on_ingest_done: Callable[[int], None] | None = None):
        self.get_settings = get_settings
        self.ledger = ledger
        self.on_status = on_status
        self.on_log = on_log
        self.on_ingest_done = on_ingest_done
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.paused = False
        self.current: str | None = None       # 変換中の "season/dir"
        self.encoder: str | None = None
        self._failed: set[tuple[int, str]] = set()   # (ingest_id, name) 起動中は再試行しない
        self._reported_missing = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="convert", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    def busy(self) -> bool:
        return self.current is not None

    # ------------------------------------------------------------------
    def _loop(self) -> None:
        while not self._stop.is_set():
            wait = 15.0
            try:
                wait = self._tick()
            except Exception as e:  # pragma: no cover
                self.on_status(f"変換でエラー: {e}")
                self.on_log(f"変換でエラー: {e}")
            self._wake.wait(wait)
            self._wake.clear()

    def _tick(self) -> float:
        s = self.get_settings()
        pending = self.ledger.pending_convert()
        if not s.convert_enabled:
            if pending:
                # 変換をオフにしたので、残っていた分は元のまま NAS へ送る
                for ing in pending:
                    self.ledger.set_convert_pending(ing.id, 0)
                    self.on_log(f"[{ing.card_id}] 変換はオフなので元のファイルのまま NAS へ送ります")
                if self.on_ingest_done:
                    self.on_ingest_done(0)
            self.on_status("")
            return 30.0
        if self.paused:
            self.on_status("変換: 一時停止中")
            return 5.0
        if not pending:
            self.on_status("変換: 待機中（未変換なし）")
            return 15.0
        ffmpeg = find_ffmpeg(s.ffmpeg_path)
        if not ffmpeg:
            self.on_status(f"変換: ffmpeg が見つかりません（{len(pending)}件待機）。設定で ffmpeg の場所を指定してください")
            if not self._reported_missing:
                self._reported_missing = True
                self.on_log("ffmpeg が見つからないため変換を始められません。設定の「動画の変換」で場所を指定するか、変換をオフにしてください")
            return 60.0
        if self.encoder is None:
            self.on_status("変換: 使えるエンコーダを調べています…")
            self.encoder = detect_encoder(ffmpeg, s.convert_encoder)
            self.on_log(f"動画の変換に {self.encoder} を使います" + ("（CPU）" if self.encoder == "libx264" else "（ハードウェア）"))
        for ing in pending:
            src_dir = resolve_staging_dir(ing, s, self.ledger)
            if not src_dir or not os.path.isdir(src_dir):
                continue
            self._convert_ingest(ing, src_dir, ffmpeg, s)
            return 0.5
        self.on_status(f"変換: 中継先が見つかりません（SSDを取り外し中？）。{len(pending)}件待機")
        return 30.0

    def _targets(self, ing: L.Ingest, s: Settings) -> list[L.FileRec]:
        exts = {e.lower().lstrip(".") for e in s.convert_extensions}
        out = []
        for f in self.ledger.files_for(ing.id):
            if f.converted or f.deleted_at or (ing.id, f.name) in self._failed:
                continue
            ext = f.name.rsplit(".", 1)[-1].lower() if "." in f.name else ""
            if ext in exts:
                out.append(f)
        return out

    def _convert_ingest(self, ing: L.Ingest, src_dir: str, ffmpeg: str, s: Settings) -> None:
        label = f"{ing.season_folder}/{ing.dir_name}" if ing.season_folder else ing.dir_name
        files = self._targets(ing, s)
        if files:
            self.current = label
            total = len(files)
            done = 0
            existing = {f.name for f in self.ledger.files_for(ing.id)}
            lock = threading.Lock()

            def work(f: L.FileRec) -> None:
                nonlocal done
                if self._stop.is_set():
                    return
                with lock:
                    self.on_status(f"変換: {label} {f.name}（{done + 1}/{total}、{self.encoder}）")
                ok = self._convert_file(ing, src_dir, f, ffmpeg, s, existing)
                with lock:
                    done += 1
                    if not ok:
                        self._failed.add((ing.id, f.name))

            with ThreadPoolExecutor(max_workers=max(1, int(s.convert_parallel)), thread_name_prefix="ffmpeg") as pool:
                list(pool.map(work, files))
            self.current = None
            if self._stop.is_set():
                return
        remaining = len(self._targets(ing, s))
        if remaining:
            self.ledger.set_convert_pending(ing.id, remaining)
            return
        # 全部済んだ（失敗したものは元のまま残す）→ NAS 転送へ
        failed = [f.name for f in self.ledger.files_for(ing.id) if not f.converted and (ing.id, f.name) in self._failed]
        self.ledger.set_convert_pending(ing.id, 0)
        self.ledger.write_manifest(ing.id, src_dir)
        n_conv = sum(1 for f in self.ledger.files_for(ing.id) if f.converted)
        note = f"。変換できなかった {len(failed)} ファイルは元のまま送ります" if failed else ""
        self.ledger.event("convert_done", f"{label}: {n_conv} files → mp4" + (f", {len(failed)} failed" if failed else ""), ing.card_id)
        self.on_log(f"[{ing.card_id}] mp4 への変換完了（{n_conv}ファイル）{note}")
        if self.on_ingest_done:
            self.on_ingest_done(ing.id)

    def _convert_file(self, ing: L.Ingest, src_dir: str, f: L.FileRec, ffmpeg: str, s: Settings,
                      existing: set[str]) -> bool:
        src = os.path.join(src_dir, f.name)
        stem = f.name.rsplit(".", 1)[0] if "." in f.name else f.name
        new_name = stem + ".mp4"
        if new_name in existing and new_name != f.name:
            new_name = stem + "_h264.mp4"
        dst = os.path.join(src_dir, new_name)
        part = dst + ".part"
        if not os.path.isfile(src):
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                # 前回、改名まで済んで台帳の更新前に止まった
                h = hash_file(dst)
                self.ledger.set_file_converted(ing.id, f.name, new_name, os.path.getsize(dst), h, f.hash)
                return True
            self.on_log(f"[{ing.card_id}] 変換元が見つかりません: {src}")
            return False
        t0 = time.time()
        ok, msg = convert_to_mp4(ffmpeg, src, part, self.encoder or "libx264", int(s.convert_crf))
        if not ok:
            try:
                os.remove(part)
            except OSError:
                pass
            self.on_log(f"[{ing.card_id}] {f.name} の変換に失敗: {msg}（元のまま残します）")
            return False
        try:
            st = os.stat(src)
            os.utime(part, (st.st_atime, st.st_mtime))   # 撮影日時の手がかりになる更新日時を引き継ぐ
            os.replace(part, dst)
        except OSError as e:
            self.on_log(f"[{ing.card_id}] {f.name} の変換結果を置けません: {e}")
            return False
        size = os.path.getsize(dst)
        h = hash_file(dst)
        self.ledger.set_file_converted(ing.id, f.name, new_name, size, h, f.hash)
        try:
            os.remove(src)
        except OSError as e:
            self.on_log(f"[{ing.card_id}] 変換後に元ファイルを消せません: {src} ({e})")
        dt = time.time() - t0
        self.on_log(f"[{ing.card_id}] {f.name} → {new_name}（{f.size/1e6:.0f} MB → {size/1e6:.0f} MB、{dt:.0f}秒、{msg}）")
        return True
