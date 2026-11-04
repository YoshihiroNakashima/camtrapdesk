"""取り込み（ingest）: 挿入されたカードを検出し、並列にコピーして検証し、台帳に記録する。

GUI に依存しない。進捗はコールバックで通知するので、UI 側で Qt のシグナルに載せ替える。
"""

from __future__ import annotations

import errno
import os
import shutil
import threading
import time
import traceback
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from . import ledger as L
from .cards import Card, detect_cards, read_card_id_with_reason, write_card_id
from .hashing import CopyCancelled, copy_with_hash, hash_file
from .roots import ensure_root_id, resolve_staging_dir
from .volumes import Volume, list_volumes

# ジョブの状態
S_DETECTED = "detected"      # 検出済み（自動開始オフのとき）
S_QUEUED = "queued"          # 待機中
S_WAITING = "waiting"        # 中継の空き待ち
S_COPYING = "copying"
S_VERIFYING = "verifying"
S_DONE = "done"              # 中継先で検証済み（抜いてよい）
S_SKIPPED = "skipped"        # 取り込み済みなどで何もしなかった
S_ERROR = "error"
S_REMOVED = "removed"        # 処理中に抜かれた
S_STOPPED = "stopped"        # 途中で止めた（やり直すと検証済みのファイルは飛ばして続きから）

STATE_LABELS = {
    S_DETECTED: "検出（開始待ち）",
    S_QUEUED: "待機中",
    S_WAITING: "空き待ち",
    S_COPYING: "コピー中",
    S_VERIFYING: "検証中",
    S_DONE: "完了（抜いてOK）",
    S_SKIPPED: "スキップ",
    S_ERROR: "エラー",
    S_REMOVED: "抜かれました",
    S_STOPPED: "停止（やり直すと続きから）",
}


SPACE_WAIT_SEC = 10.0   # 中継の空き待ちの再確認間隔


class NoSpaceError(RuntimeError):
    """コピー中に中継がいっぱいになった（待って続きから再開する）。"""


def free_bytes(path: str) -> int | None:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return None


def _gb(n: int) -> str:
    return f"{n/1e9:.1f} GB"


@dataclass
class CardJob:
    key: str
    card: Card
    state: str = S_DETECTED
    message: str = ""
    done_bytes: int = 0
    total_bytes: int = 0
    done_files: int = 0
    ingest_id: int | None = None
    n_broken: int = 0            # 壊れていた（読めなかった／途中で切れていた）ファイル数
    cancel: bool = False         # 停止が求められた（待機中なら始めない、コピー中なら切れ目でやめる）
    repair_offered: bool = False # 一括完了時に修復を勧めたか（同じカードに二度聞かない）
    started: float = field(default_factory=time.time)
    copy_started: float | None = None
    finished: float | None = None

    @property
    def speed_mbps(self) -> float | None:
        """コピー開始からの平均速度（MB/s）。"""
        if not self.copy_started:
            return None
        end = self.finished or time.time()
        dt = end - self.copy_started
        return (self.done_bytes / 1e6 / dt) if dt > 0.5 else None

    @property
    def progress(self) -> float:
        return (self.done_bytes / self.total_bytes) if self.total_bytes else (1.0 if self.state == S_DONE else 0.0)


class IngestManager:
    """ボリュームを監視し、カードを検出して取り込むバックグラウンド処理。"""

    _instances: "weakref.WeakSet[IngestManager]" = weakref.WeakSet()   # テストや終了処理で止め忘れを拾うため

    def __init__(self, get_settings: Callable[[], Settings], ledger: L.Ledger,
                 on_update: Callable[[CardJob], None], on_log: Callable[[str], None],
                 on_batch_done: Callable[[int], None] | None = None,
                 armed: bool = False):
        self.get_settings = get_settings
        # armed: 「取り込みを開始」で保存先などを確認してもらってから、挿したカードを自動で始める。
        # 起動直後は False で、検出したカードは「開始待ち」で止まる（設定が違うまま勝手にコピーしないため）
        self.armed = armed
        self.ledger = ledger
        self.on_update = on_update
        self.on_log = on_log
        self.on_batch_done = on_batch_done
        self.jobs: dict[str, CardJob] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._active = 0
        self._batch_counted = True
        self._skipped_reported: set[str] = set()   # id.txt が無い等で対象外にしたボリューム（1回だけ記録）
        IngestManager._instances.add(self)
        self.scan_paused = False                           # カードの修復（chkdsk）中は走査を止める
        self.others: dict[str, tuple[Volume, str]] = {}   # カードとして扱えないリムーバブルボリュームと理由
        self.suggested_ids: dict[str, str] = {}           # id.txt の無いボリューム → 前回の ID（ボリュームシリアルから）
        self._remembered: set[tuple[str, str]] = set()    # 今回の起動で台帳に書いた (シリアル, ID)

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        s = self.get_settings()
        self._pool = ThreadPoolExecutor(max_workers=max(1, int(s.concurrency)), thread_name_prefix="ingest")
        self._thread = threading.Thread(target=self._poll_loop, name="ingest-poll", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._pool:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def rescan_now(self) -> None:
        self._scan_once()

    def active_count(self) -> int:
        with self._lock:
            return sum(1 for j in self.jobs.values() if j.state in (S_QUEUED, S_COPYING, S_VERIFYING))

    def arm(self) -> int:
        """自動開始を有効にし、いま開始待ちのカードをすべて始める。戻り値は始めた枚数。"""
        self.armed = True
        n = 0
        with self._lock:
            keys = [k for k, j in self.jobs.items() if j.state == S_DETECTED and not j.card.problems]
        for k in keys:
            self.enqueue(k)
            n += 1
        return n

    def disarm(self) -> None:
        """新しく挿したカードの自動開始を止める（進行中のコピーは続く）。"""
        self.armed = False

    def cancel_jobs(self, keys: "list[str] | None" = None) -> int:
        """待機中・コピー中のカードを止める（None なら全部）。コピー中のものは 4 MB の切れ目でやめ、書きかけは消す。
        止めたカードは「停止」になり、「やり直す」で検証済みのファイルを飛ばして続きから再開できる。戻り値は止めた枚数。"""
        n = 0
        with self._lock:
            targets = [self.jobs[k] for k in (keys if keys is not None else list(self.jobs)) if k in self.jobs]
            for job in targets:
                if job.state in (S_QUEUED, S_WAITING, S_COPYING, S_VERIFYING):
                    job.cancel = True
                    n += 1
                    if job.state in (S_QUEUED, S_WAITING):
                        # まだ始まっていない: その場で「停止」にする（_run_job は始まる前に cancel を見て抜ける）
                        job.state = S_STOPPED
                        job.message = "停止しました（「やり直す」で始めます）"
        for job in targets:
            self.on_update(job)
        return n

    def enqueue(self, key: str) -> None:
        with self._lock:
            job = self.jobs.get(key)
            if not job or job.state not in (S_DETECTED, S_ERROR, S_SKIPPED, S_STOPPED):
                return
            job.state = S_QUEUED
            job.cancel = False
            job.message = ""
            job.done_bytes = 0
            job.done_files = 0
            self._batch_counted = False
        self.on_update(job)
        if self._pool:
            self._pool.submit(self._run_job, key)

    def assign_card_id(self, key: str, card_id: str) -> tuple[bool, str]:
        """id.txt の無いカードに ID を書き込み、次のスキャンで取り込めるようにする。"""
        with self._lock:
            item = self.others.get(key)
        if not item:
            return False, "対象のボリュームが見つかりません"
        v, _reason = item
        ok, msg = write_card_id(v, self.get_settings().id_file_name, card_id)
        if ok:
            self.ledger.event("assign_id", f"{v.display} ({v.label}) に ID {card_id.strip()} を書き込み", card_id.strip())
            self.on_log(f"ボリューム {v.display} に {self.get_settings().id_file_name} = {card_id.strip()} を書き込みました")
            with self._lock:
                self.others.pop(key, None)
                self._skipped_reported.discard(key)
        return ok, msg

    def remove_job(self, key: str) -> None:
        with self._lock:
            self.jobs.pop(key, None)

    # ----------------------------------------------------------------- poll
    def _poll_loop(self) -> None:
        while not self._stop.is_set():
            try:
                if not self.scan_paused:
                    self._scan_once()
            except Exception as e:  # pragma: no cover
                self.on_log(f"スキャンでエラー: {e}")
            self._stop.wait(max(0.5, float(self.get_settings().poll_interval_sec)))

    def _scan_once(self) -> None:
        s = self.get_settings()
        exclude = list(s.exclude_volumes)
        for p in (s.dest_root, s.nas_root):
            if p:
                exclude.append(p)
        vols = list_volumes(s.scan_roots, exclude)
        cards = detect_cards(vols, s.id_file_name, s.media_subdirs, s.media_extensions,
                             s.media_recursive, s.min_file_size_bytes, list_files=True,
                             min_file_count=s.min_file_count)
        present = {c.volume.key for c in cards}
        # ID のあるカードは、ボリュームシリアルと ID の対応を覚えておく（id.txt が消えたときの候補に使う）
        for c in cards:
            if c.volume.serial and (c.volume.serial, c.card_id) not in self._remembered:
                self.ledger.remember_card_volume(c.volume.serial, c.card_id, c.volume.label)
                self._remembered.add((c.volume.serial, c.card_id))
        # カードとして扱えなかったリムーバブルボリュームは、理由付きで一覧に出し、ログにも1回だけ書く
        others: dict[str, tuple[Volume, str]] = {}
        suggested: dict[str, str] = {}
        for v in vols:
            if v.key in present:
                continue
            if v.kind == "unreadable":
                reason = f"Windows が読み取れません（{v.error}）。別のスロットに挿し直すか、カードの破損・未フォーマットを疑ってください"
            elif v.kind in ("removable", "virtual"):
                _cid, reason = read_card_id_with_reason(v, s.id_file_name)
                known = self.ledger.card_id_for_serial(v.serial) if v.serial else None
                if known:
                    suggested[v.key] = known[0]
                    reason += f"。前回このカードは ID {known[0]} でした（{known[1][:10]}）。「カードにIDを付ける」に候補が入ります"
            else:
                continue
            others[v.key] = (v, reason)
            if v.key not in self._skipped_reported:
                self._skipped_reported.add(v.key)
                self.on_log(f"ボリューム {v.display}（{v.label or 'ラベル無し'}）は対象外: {reason}")
        with self._lock:
            self.others = others
            self.suggested_ids = suggested
        self._skipped_reported &= {v.key for v in vols}
        new_jobs: list[CardJob] = []
        with self._lock:
            # 抜かれたカード
            for key, job in list(self.jobs.items()):
                if key not in present:
                    if job.state in (S_COPYING, S_VERIFYING, S_QUEUED, S_WAITING):
                        job.state = S_REMOVED
                        job.message = "処理中にカードが抜かれました"
                        self.on_update(job)
                        self.on_log(f"[{job.card.card_id}] 処理中にカードが抜かれました")
                    self.jobs.pop(key, None)
            # 新しいカード
            for c in cards:
                if c.volume.key in self.jobs:
                    continue
                job = CardJob(key=c.volume.key, card=c, total_bytes=c.total_bytes)
                if c.problems:
                    job.state = S_ERROR
                    job.message = "; ".join(c.problems)
                elif c.warnings:
                    job.message = "注意: " + "; ".join(c.warnings)
                self.jobs[c.volume.key] = job
                new_jobs.append(job)
        for job in new_jobs:
            self.on_update(job)
            if job.state == S_ERROR:
                self.on_log(f"[{job.card.card_id}] {job.message}")
            elif self.armed:
                self.enqueue(job.key)
            else:
                job.message = "「取り込みを開始」を押すと始まります（保存先とフォルダ名を確認してから）"
                self.on_update(job)
                self.on_log(f"[{job.card.card_id}] 検出（{job.card.n_files}ファイル, {job.card.total_bytes/1e9:.2f} GB）。開始待ち")
        self._check_batch_done()

    def _check_batch_done(self) -> None:
        with self._lock:
            if self._batch_counted:
                return
            active = [j for j in self.jobs.values() if j.state in (S_QUEUED, S_WAITING, S_COPYING, S_VERIFYING)]
            if active:
                return
            self._batch_counted = True
            n = sum(1 for j in self.jobs.values() if j.state == S_DONE)
        if self.on_batch_done:
            self.on_batch_done(n)

    # ------------------------------------------------------------------ job
    def _set(self, job: CardJob, state: str | None = None, message: str | None = None) -> None:
        with self._lock:
            if state:
                job.state = state
            if message is not None:
                job.message = message
        self.on_update(job)

    def _run_job(self, key: str) -> None:
        with self._lock:
            job = self.jobs.get(key)
        if not job or job.state != S_QUEUED or job.cancel:
            return
        try:
            while True:
                try:
                    self._ingest_card(job)
                    break
                except CopyCancelled:
                    # 途中で止めた: 記録は中断扱い。検証済みのファイルは台帳に残るので、やり直すと続きから
                    if job.ingest_id:
                        self.ledger.update_ingest(job.ingest_id, status=L.STATUS_ERROR, message="停止",
                                                  finished_at=L.now_iso())
                    self.ledger.event("stopped", f"{job.card.card_id}: {job.done_files}/{job.card.n_files} files", job.card.card_id)
                    job.copy_started = None
                    self._set(job, S_STOPPED, f"停止しました（{job.done_files}/{job.card.n_files} ファイルまで検証済み。「やり直す」で続きから）")
                    self.on_log(f"[{job.card.card_id}] 停止しました（{job.done_files}/{job.card.n_files} ファイルまで）。「エラー・停止・スキップ分をやり直す」で続きから再開します")
                    break
                except NoSpaceError as e:
                    # 中継がいっぱい: 記録を中断扱いにして待ち、空いたら（または中継先が変わったら）続きから
                    if job.ingest_id:
                        self.ledger.update_ingest(job.ingest_id, status=L.STATUS_ERROR, message=str(e),
                                                  finished_at=L.now_iso())
                    self.ledger.event("no_space", f"{job.card.card_id}: {e}", job.card.card_id)
                    self.on_log(f"[{job.card.card_id}] {e}。NAS転送で空くか、取り込み画面の保存先を別のSSDにすると続きから再開します")
                    job.ingest_id = None
                    job.done_bytes = 0
                    job.done_files = 0
                    job.copy_started = None
                    self._set(job, S_WAITING, f"{e}。空くか中継先が変わるのを待っています")
                    self._stop.wait(SPACE_WAIT_SEC)
                    if self._stop.is_set():
                        return
                    with self._lock:
                        if self.jobs.get(key) is not job or job.state != S_WAITING or job.cancel:
                            return
        except Exception as e:
            self.ledger.event("error", f"{job.card.card_id}: {e}", job.card.card_id)
            if job.ingest_id:
                self.ledger.update_ingest(job.ingest_id, status=L.STATUS_ERROR, message=str(e), finished_at=L.now_iso())
            self._set(job, S_ERROR, f"失敗: {e}")
            self.on_log(f"[{job.card.card_id}] 失敗: {e}\n{traceback.format_exc(limit=3)}")
        finally:
            job.finished = time.time()
            self._check_batch_done()

    def _wait_for_space(self, job: CardJob, s: Settings, need_bytes: int) -> bool:
        """中継の空きが「このカード＋他のコピー中のカードの残り＋余裕」に届くまで待つ。
        届けばジョブをコピー中にして True。カードが抜かれた・停止されたら False。"""
        margin = int(max(0.0, float(s.min_free_gb)) * 1e9)
        waited = False
        while not self._stop.is_set():
            free = free_bytes(s.dest_root)
            with self._lock:
                if job.state in (S_REMOVED, S_STOPPED) or job.cancel or self.jobs.get(job.key) is not job:
                    return False
                reserved = sum(max(0, j.total_bytes - j.done_bytes) for j in self.jobs.values()
                               if j is not job and j.state in (S_COPYING, S_VERIFYING))
                if free is not None and free - reserved >= need_bytes + margin:
                    job.state = S_COPYING   # ここで予約に数えられるようにする
                    if waited:
                        job.message = "中継に空きができたのでコピーを始めます"
                    break
                job.state = S_WAITING
                if free is None:
                    job.message = (f"保存先 {s.dest_root} が見つかりません（取り外し中？）。"
                                   "接続し直すか、取り込み画面の保存先を別の場所にすると再開します")
                else:
                    job.message = (f"保存先の空き待ち（空き {_gb(free)}、他のコピーで {_gb(reserved)} 使用予定、"
                                   f"このカード {_gb(need_bytes)}＋余裕 {_gb(margin)}）。"
                                   "NAS転送で空くか、取り込み画面の保存先を別のSSDにすると再開します")
            self.on_update(job)
            if not waited:
                waited = True
                self.on_log(f"[{job.card.card_id}] " + ("中継先が見つからないため待機" if free is None
                                                         else f"中継の空きが足りないため待機（空き {_gb(free)}）"))
            self._stop.wait(SPACE_WAIT_SEC)
        else:
            return False
        self.on_update(job)
        return True

    def _ingest_card(self, job: CardJob) -> None:
        s = self.get_settings()
        card = job.card
        cid = card.card_id
        if not s.dest_root:
            self._set(job, S_ERROR, "保存先が未設定です。取り込み画面の上の欄で指定してください")
            return
        if card.problems:
            self._set(job, S_ERROR, "; ".join(card.problems))
            return
        if not card.files:
            self._set(job, S_SKIPPED, "対象ファイルがありません")
            return

        # 前回取り込み済み（削除未実施）のカードを検出
        prev = self.ledger.latest_ingest_for_card(card.card_id_norm)
        if prev and prev.status in (L.STATUS_STAGED, L.STATUS_SYNCED) and not prev.cleared_at:
            prev_files = {(f.card_name, f.card_size) for f in self.ledger.files_for(prev.id) if not f.deleted_at}
            cur_files = {(f.name, f.size) for f in card.files}
            if prev_files and cur_files <= prev_files:
                self._set(job, S_SKIPPED,
                          f"取り込み済みです（{prev.season_folder}/{prev.dir_name}、{prev.finished_at}）。削除は「削除」タブから")
                self.on_log(f"[{cid}] 取り込み済みのためスキップ")
                return

        season_folder = s.season_folder.strip()
        dir_name = cid
        while True:
            dest_root = s.dest_root
            dest_dir = os.path.join(dest_root, season_folder, dir_name)
            existing_hashes: dict[str, tuple[int, str]] = {}
            if os.path.isdir(dest_root):
                # 既存フォルダ: 同名ファイルがあれば、中断の再開として扱えるか manifest で判定する
                existing_manifest = L.Ledger.read_manifest(dest_dir) if os.path.isdir(dest_dir) else None
                if existing_manifest:
                    for f in existing_manifest.get("files", []):
                        if f.get("hash"):
                            existing_hashes[f["name"]] = (int(f["size"]), f["hash"])
                # 途中で止まった前回の取り込み（アプリ終了・停電・中継がいっぱい）は、台帳に残るハッシュで再開する
                if prev and prev.status in (L.STATUS_COPYING, L.STATUS_ERROR) and not prev.cleared_at:
                    prev_dir = resolve_staging_dir(prev, s, self.ledger, search=False)
                    if prev_dir and os.path.normcase(prev_dir) == os.path.normcase(dest_dir):
                        for f in self.ledger.files_for(prev.id):
                            if f.hash and f.staging_ok and f.name not in existing_hashes:
                                existing_hashes[f.name] = (f.size, f.hash)
                        self.ledger.update_ingest(prev.id, status=L.STATUS_ERROR, message="中断されたため、新しい取り込みで再開")
                        self.on_log(f"[{cid}] 前回中断した取り込みを再開します（検証済み {len(existing_hashes)} ファイルは再利用）")
                if os.path.isdir(dest_dir):
                    for n in os.listdir(dest_dir):
                        if n.endswith(".part"):
                            try:
                                os.remove(os.path.join(dest_dir, n))
                            except OSError:
                                pass
                    present = {n for n in os.listdir(dest_dir) if n != L.MANIFEST_NAME}
                    clash = [f.name for f in card.files if f.name in present and f.name not in existing_hashes]
                    if clash:
                        self._set(job, S_ERROR, f"保存先に同名ファイルが既にあります（{clash[0]} ほか{len(clash)}件）。上書きしません")
                        return
            # 中継の空きが足りなければ待つ（NAS転送で空く／別のSSDを中継先に設定する、のどちらでも再開できる）
            need = card.total_bytes
            for name, (size_, _h) in existing_hashes.items():
                if os.path.isfile(os.path.join(dest_dir, name)):
                    need -= size_
            if not self._wait_for_space(job, s, max(0, need)):
                return
            if s.dest_root == dest_root:
                break
            self.on_log(f"[{cid}] 中継先が変わったので {s.dest_root} に取り込みます")
        os.makedirs(dest_dir, exist_ok=True)
        root_id = ensure_root_id(dest_root)

        ingest_id = self.ledger.create_ingest(cid, card.card_id_norm, season_folder, dir_name, dest_root,
                                              s.nas_root or None, card.volume.key, card.volume.label,
                                              card.n_files, card.total_bytes, dest_root_id=root_id)
        job.ingest_id = ingest_id
        self.ledger.add_files(ingest_id, [(f.name, f.size, f.mtime, f.src_path) for f in card.files])
        self.ledger.event("ingest_start", f"{os.path.join(season_folder, dir_name)} {card.n_files} files", cid)
        self._set(job, S_COPYING, f"コピー中 0/{card.n_files}")
        self.on_log(f"[{cid}] コピー開始 → {dest_dir}（{card.n_files}ファイル, {card.total_bytes/1e9:.2f} GB）")

        base_done = 0
        job.copy_started = time.time()
        broken: list[tuple[str, str]] = []   # (ファイル名, 理由)。カードから読めなかったもの
        for i, f in enumerate(card.files, 1):
            if self._stop.is_set():
                raise RuntimeError("停止されました")
            if job.cancel:
                raise CopyCancelled("停止されました")
            dst = os.path.join(dest_dir, f.name)
            if f.error:
                # 検出の時点でカード上で読めなかったファイル: コピーせず、壊れたものとして記録だけ残す
                broken.append((f.name, f.error))
                self.ledger.set_file_broken(ingest_id, f.name, f.error)
                self.on_log(f"[{cid}] {f.name}: {f.error}。飛ばして続けます")
                job.done_files = i
                continue
            # 再開: 既に検証済みのファイルは、読み戻してハッシュが一致すれば飛ばす
            if f.name in existing_hashes and os.path.isfile(dst) and os.path.getsize(dst) == f.size:
                size_, h = existing_hashes[f.name]
                if size_ == f.size and hash_file(dst) == h:
                    self.ledger.set_file_hash(ingest_id, f.name, h, True)
                    base_done += f.size
                    job.done_bytes = base_done
                    job.done_files = i
                    self._set(job, message=f"コピー中 {i}/{card.n_files}（再開）")
                    continue

            def prog(n: int, base=base_done):
                job.done_bytes = base + n
                self.on_update(job)

            try:
                nbytes, h = copy_with_hash(f.src_path, dst, prog, should_stop=lambda: job.cancel or self._stop.is_set())
            except OSError as e:
                if e.errno == errno.ENOSPC:
                    raise NoSpaceError("保存先がいっぱいになりました") from e
                if _is_source_read_error(e, f.src_path):
                    # カード上で壊れていて読めないファイル: このファイルだけ諦めて先へ進む（カード全体は止めない）
                    _remove_quietly(dst + ".part")
                    reason = f"カードから読めません（{e.strerror or e}）"
                    broken.append((f.name, reason))
                    self.ledger.set_file_broken(ingest_id, f.name, reason)
                    self.on_log(f"[{cid}] {f.name}: {reason}。飛ばして続けます")
                    base_done += f.size
                    job.done_bytes = base_done
                    job.done_files = i
                    continue
                raise RuntimeError(f"{f.name} のコピーに失敗: {e}") from e
            if nbytes != f.size:
                raise RuntimeError(f"{f.name}: サイズ不一致（元 {f.size} / コピー {nbytes}）")
            ok = True
            if s.verify_readback:
                self._set(job, S_VERIFYING, f"検証中 {i}/{card.n_files}")
                h2 = hash_file(dst)
                ok = (h2 == h)
                if not ok:
                    raise RuntimeError(f"{f.name}: 読み戻しハッシュ不一致")
            self.ledger.set_file_hash(ingest_id, f.name, h, ok)
            base_done += f.size
            job.done_bytes = base_done
            job.done_files = i
            self._set(job, S_COPYING, f"コピー中 {i}/{card.n_files}")

        # 壊れたファイル（途中までしか書けなかった動画など）を中継の上で調べ、取り除く
        if s.broken_check:
            self._set(job, S_VERIFYING, "壊れたファイルがないか確認中")
            broken += self._remove_broken(ingest_id, dest_dir, card, s, skip={n for n, _r in broken})

        ts = L.now_iso()
        broken_names = {n for n, _r in broken}
        good_files = [f for f in card.files if f.name not in broken_names]
        job.n_broken = len(broken)
        # 変換（AVI → mp4）が有効なら、対象ファイル数を記録しておく。NAS 転送は変換が終わってから
        n_convert = 0
        if s.convert_enabled:
            exts = {e.lower().lstrip(".") for e in s.convert_extensions}
            n_convert = sum(1 for f in good_files if ("." in f.name and f.name.rsplit(".", 1)[-1].lower() in exts))
        self.ledger.update_ingest(ingest_id, status=L.STATUS_STAGED, finished_at=ts, staging_verified_at=ts,
                                  message="中継先で検証済み" + (f"（壊れた {len(broken)} ファイルを除く）" if broken else ""),
                                  convert_pending=n_convert)
        self.ledger.write_manifest(ingest_id, dest_dir)
        self.ledger.event("ingest_done", f"{season_folder}/{dir_name}" + (f", {len(broken)} broken" if broken else ""), cid)
        notes = []
        if n_convert:
            notes.append(f"mp4 への変換（{n_convert}ファイル）")
        if s.nas_root:
            notes.append("NASへの転送")
        nas_note = "。" + "と".join(notes) + "はバックグラウンドで続きます" if notes else ""
        broken_note = ""
        if broken:
            n_unread = sum(1 for _n, r in broken if r.startswith("カード"))   # カード上で読めなかったもの
            where = "中継から消しました" if s.broken_action != "keep" else "_破損 フォルダに移しました"
            broken_note = f"。壊れていた {len(broken)} ファイルは{where}（カードを消すときに一緒に消えます"
            broken_note += "。読めないファイルがあったので、取り込みが終わったらカードの修復をおすすめします）" if n_unread else "）"
        self._set(job, S_DONE, f"完了: {len(good_files)}ファイル検証済み{broken_note}{nas_note}")
        self.on_log(f"[{cid}] 完了（{len(good_files)}ファイル, {card.total_bytes/1e9:.2f} GB"
                    + (f"、壊れたファイル {len(broken)} 件" if broken else "") + "）")

    def _remove_broken(self, ingest_id: int, dest_dir: str, card: Card, s: Settings,
                       skip: set[str]) -> list[tuple[str, str]]:
        """中継にコピーしたファイルのうち壊れているものを見つけ、設定に従って消すか _破損 に移す。戻り値は (名前, 理由)。"""
        from .ffmpeg import find_ffmpeg
        from .mediacheck import BROKEN_DIR, check_media_file, is_checkable
        ffmpeg = find_ffmpeg(s.ffmpeg_path)
        out: list[tuple[str, str]] = []
        for f in card.files:
            if f.name in skip or not is_checkable(f.name):
                continue
            p = os.path.join(dest_dir, f.name)
            reason = check_media_file(p, ffmpeg)
            if not reason:
                continue
            if s.broken_action == "keep":
                bdir = os.path.join(dest_dir, BROKEN_DIR)
                try:
                    os.makedirs(bdir, exist_ok=True)
                    os.replace(p, os.path.join(bdir, f.name))
                except OSError as e:
                    self.on_log(f"[{card.card_id}] {f.name} を {BROKEN_DIR} に移せません: {e}")
                    continue
            else:
                _remove_quietly(p)
            self.ledger.set_file_broken(ingest_id, f.name, reason)
            self.on_log(f"[{card.card_id}] 壊れたファイル: {f.name}（{reason}）")
            out.append((f.name, reason))
        if out:
            self.ledger.event("broken", f"{card.card_id}: " + ", ".join(n for n, _r in out), card.card_id)
        return out


def _is_source_read_error(e: OSError, src_path: str) -> bool:
    """コピー中の OSError が、カード側（読み取り）で起きたものか。"""
    from .repair import is_corruption_error
    fn = getattr(e, "filename", None) or ""
    if fn and os.path.normcase(os.path.abspath(fn)) == os.path.normcase(os.path.abspath(src_path)):
        return True
    return is_corruption_error(e)


def _remove_quietly(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass
