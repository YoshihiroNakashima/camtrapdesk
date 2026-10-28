"""保存済みの動画の一括変換: NAS や中継にすでにあるフォルダを指定して、中の AVI などを後から mp4 に変換し、元を消す。

取り込み時の変換（convert.py）が無かった頃に取り込んだ分や、旧 R コードで取り込んだ分の容量を減らすための機能。
フォルダ構造は問わず、指定したフォルダ以下を再帰的に探す（NAS の保存先ごと、区切りフォルダ、地点フォルダのどれでもよい）。
変換は同じフォルダの中で行い（.part に書いて検証してから改名）、元ファイルは変換後に消す。
manifest.json があるフォルダは manifest も更新し、台帳に記録があって今変換しているのがその正本（NAS、NAS が無ければ中継）なら台帳も合わせる。
取り込み・NAS 転送・取り込み時の変換が途中のフォルダは対象外にする（裏の処理と取り合いにならないように）。
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from ..config import Settings
from . import ledger as L
from .ffmpeg import convert_to_mp4, detect_encoder, find_ffmpeg, probe
from .hashing import hash_file

PART_SUFFIX = ".part"
SKIP_DIR_PREFIXES = (".", "@", "#", "$")          # @eaDir（Synology のサムネイル）、#recycle、$RECYCLE.BIN など
SKIP_DIR_NAMES = {"system volume information", "synoreport"}

A_CONVERT = "convert"   # 変換する
A_FINISH = "finish"     # 変換済みの mp4 が既にある（前回、元を消す前に止まった）→ 元を消すだけ
A_SKIP = "skip"         # 変換しない（理由付き）


@dataclass
class Target:
    dir: str
    name: str
    size: int
    mtime: float
    out_name: str
    action: str = A_CONVERT
    reason: str = ""
    old_hash: str | None = None     # manifest に書かれていた元ファイルのハッシュ


@dataclass
class FolderPlan:
    dir: str
    targets: list[Target] = field(default_factory=list)
    has_manifest: bool = False
    ingest_id: int | None = None
    ledger_authoritative: bool = False   # この場所がその取り込みの正本（台帳も更新する）
    skipped_reason: str = ""             # フォルダごと対象外にした理由
    n_other_media: int = 0               # 変換対象でない動画（mp4 など）の数
    # 実行時の集計
    n_done: int = 0
    n_failed: int = 0
    bytes_in: int = 0
    bytes_out: int = 0

    @property
    def rel(self) -> str:
        return self.dir

    @property
    def n_targets(self) -> int:
        return sum(1 for t in self.targets if t.action != A_SKIP)

    @property
    def n_skipped(self) -> int:
        return sum(1 for t in self.targets if t.action == A_SKIP)

    @property
    def total_bytes(self) -> int:
        return sum(t.size for t in self.targets if t.action != A_SKIP)


@dataclass
class ScanResult:
    root: str
    folders: list[FolderPlan] = field(default_factory=list)
    n_dirs_seen: int = 0
    n_parts_removed: int = 0
    issues: list[str] = field(default_factory=list)

    @property
    def n_files(self) -> int:
        return sum(f.n_targets for f in self.folders if not f.skipped_reason)

    @property
    def total_bytes(self) -> int:
        return sum(f.total_bytes for f in self.folders if not f.skipped_reason)

    @property
    def n_skipped_files(self) -> int:
        return sum(f.n_skipped for f in self.folders) + sum(len(f.targets) for f in self.folders if f.skipped_reason)


def fmt_bytes(n: float) -> str:
    return f"{n/1e9:.1f} GB" if n >= 1e9 else f"{n/1e6:.0f} MB"


def _same_path(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    return os.path.normcase(os.path.normpath(os.path.abspath(a))) == os.path.normcase(os.path.normpath(os.path.abspath(b)))


def _skip_dir(name: str) -> bool:
    return name.startswith(SKIP_DIR_PREFIXES) or name.lower() in SKIP_DIR_NAMES


def _durations_match(a: float, b: float) -> bool:
    return a <= 0 or b <= 0 or abs(a - b) <= max(1.0, a * 0.02)


def _is_prior_output(src_path: str, src_mtime: float, out_path: str, ffmpeg: str | None) -> bool:
    """同じ名前の mp4 が「このアプリが前回作ったもの」か。変換時に元の更新日時を引き継ぐので、それが一致し、
    かつ H.264 で長さも合えば前回の出力とみなす（カメラが作った別ファイルを上書きしないため）。"""
    try:
        st = os.stat(out_path)
    except OSError:
        return False
    if st.st_size <= 0 or abs(st.st_mtime - src_mtime) > 2.0:
        return False
    if not ffmpeg:
        return True
    a, b = probe(ffmpeg, src_path), probe(ffmpeg, out_path)
    return bool(a and b and b.vcodec == "h264" and _durations_match(a.duration, b.duration))


def plan_folder(d: str, names: list[str], exts: set[str], media_exts: set[str],
                manifest: dict | None, ffmpeg: str | None = None) -> FolderPlan:
    """1 つのフォルダの中で、どのファイルをどの名前に変換するかを決める。"""
    fp = FolderPlan(dir=d, has_manifest=manifest is not None)
    lower = {n.lower(): n for n in names}
    man_hash: dict[str, str | None] = {}
    if manifest:
        for f in manifest.get("files", []):
            if isinstance(f, dict) and f.get("name"):
                man_hash[str(f["name"])] = f.get("hash")
    for n in sorted(names):
        if n.endswith(PART_SUFFIX) or n.lower() == L.MANIFEST_NAME:
            continue
        ext = n.rsplit(".", 1)[-1].lower() if "." in n else ""
        if ext not in exts:
            if ext in media_exts:
                fp.n_other_media += 1
            continue
        p = os.path.join(d, n)
        try:
            st = os.stat(p)
        except OSError as e:
            fp.targets.append(Target(d, n, 0, 0.0, "", A_SKIP, f"読めません: {e}"))
            continue
        stem = n.rsplit(".", 1)[0] if "." in n else n
        t = Target(d, n, st.st_size, st.st_mtime, stem + ".mp4", old_hash=man_hash.get(n))
        if st.st_size == 0:
            t.action, t.reason = A_SKIP, "サイズが 0 のファイル"
        else:
            for cand in (stem + ".mp4", stem + "_h264.mp4"):
                existing = lower.get(cand.lower())
                if existing is None:
                    t.out_name = cand
                    break
                if _is_prior_output(p, st.st_mtime, os.path.join(d, existing), ffmpeg):
                    t.out_name, t.action = existing, A_FINISH
                    t.reason = "変換済みの mp4 が既にあるので、元ファイルを消すだけ"
                    break
            else:
                t.action, t.reason = A_SKIP, f"{stem}.mp4 も {stem}_h264.mp4 も既にあります（別のファイル）"
        fp.targets.append(t)
    return fp


def scan(root: str, settings: Settings, ledger: L.Ledger | None = None,
         stop: threading.Event | None = None, on_progress: Callable[[int, int], None] | None = None,
         remove_parts: bool = True) -> ScanResult:
    """root 以下を再帰的に調べて、変換の計画を作る。on_progress(見たフォルダ数, 見つけた対象ファイル数)。"""
    res = ScanResult(root=root)
    if not root or not os.path.isdir(root):
        res.issues.append(f"フォルダが見つかりません: {root}")
        return res
    exts = {e.lower().lstrip(".") for e in settings.convert_extensions} or {"avi"}
    media_exts = {e.lower().lstrip(".") for e in settings.media_extensions}
    ffmpeg = find_ffmpeg(settings.ffmpeg_path)
    n_found = 0
    for d, dirs, files in os.walk(root, topdown=True, onerror=lambda e: res.issues.append(f"読めないフォルダ: {e}")):
        if stop is not None and stop.is_set():
            res.issues.append("走査を中止しました")
            break
        dirs[:] = sorted(x for x in dirs if not _skip_dir(x))
        res.n_dirs_seen += 1
        names = [n for n in files if not n.endswith(".mp4" + PART_SUFFIX)]
        if not any(("." in n and n.rsplit(".", 1)[-1].lower() in exts) for n in names):
            if on_progress:
                on_progress(res.n_dirs_seen, n_found)
            continue
        manifest = L.Ledger.read_manifest(d) if L.MANIFEST_NAME in names else None
        fp = plan_folder(d, names, exts, media_exts, manifest, ffmpeg)
        if manifest and ledger is not None:
            _attach_ledger(fp, manifest, ledger, settings)
        if remove_parts and not fp.skipped_reason:
            # 前回の途中のファイル。取り込み時の変換が動いているフォルダ（対象外）のものは触らない
            for n in files:
                if n.endswith(".mp4" + PART_SUFFIX):
                    try:
                        os.remove(os.path.join(d, n))
                        res.n_parts_removed += 1
                    except OSError:
                        pass
        res.folders.append(fp)
        n_found += fp.n_targets if not fp.skipped_reason else 0
        if on_progress:
            on_progress(res.n_dirs_seen, n_found)
    return res


def _attach_ledger(fp: FolderPlan, manifest: dict, ledger: L.Ledger, settings: Settings) -> None:
    """manifest から台帳の記録を探し、この場所が正本かどうかと、取り込み途中かどうかを決める。"""
    season = str(manifest.get("season_folder") or "")
    dir_name = str(manifest.get("dir_name") or os.path.basename(fp.dir))
    ing = ledger.find_by_dir(season, dir_name)
    if ing is None:
        return
    is_nas = _same_path(fp.dir, ing.nas_dir)
    is_staging = _same_path(fp.dir, ing.staging_dir)
    if not is_nas and not is_staging:
        return   # 同名の別フォルダ（別の保存先に置いたコピーなど）
    fp.ingest_id = ing.id
    fp.ledger_authoritative = is_nas or (is_staging and not ing.nas_root)
    if ing.cleared_at is None:
        if ing.status == L.STATUS_COPYING:
            fp.skipped_reason = "カードからのコピー中"
        elif ing.convert_pending:
            fp.skipped_reason = "取り込み時の変換が途中"
        elif ing.status in (L.STATUS_STAGED, L.STATUS_ERROR) and ing.nas_root and not ing.nas_verified_at:
            fp.skipped_reason = "NAS への転送がまだ（転送が終わってから変換してください）"


@dataclass
class BatchStats:
    n_total: int = 0
    n_done: int = 0
    n_failed: int = 0
    n_finished_only: int = 0      # 元を消すだけで済んだもの
    bytes_total: int = 0
    bytes_in_done: int = 0
    bytes_out_done: int = 0
    started_at: float = 0.0
    finished_at: float = 0.0
    current: dict[str, str] = field(default_factory=dict)   # スレッド名 → 変換中のファイル
    message: str = ""
    stopped: bool = False

    @property
    def saved_bytes(self) -> int:
        return max(0, self.bytes_in_done - self.bytes_out_done)

    @property
    def frac(self) -> float:
        return self.bytes_in_done / self.bytes_total if self.bytes_total else (1.0 if self.n_total == 0 else 0.0)

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return end - self.started_at if self.started_at else 0.0

    @property
    def eta_sec(self) -> float | None:
        if not self.bytes_in_done or self.finished_at:
            return None
        rate = self.bytes_in_done / max(1e-6, self.elapsed)
        return (self.bytes_total - self.bytes_in_done) / rate if rate > 0 else None


class BatchConverter:
    """計画（ScanResult）を受け取り、バックグラウンドで順に変換する。1 度に 1 つの計画だけ走らせる。"""

    def __init__(self, get_settings: Callable[[], Settings], ledger: L.Ledger | None,
                 on_status: Callable[[str], None], on_log: Callable[[str], None],
                 on_done: Callable[[BatchStats], None] | None = None):
        self.get_settings = get_settings
        self.ledger = ledger
        self.on_status = on_status
        self.on_log = on_log
        self.on_done = on_done
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.paused = False
        self.stats = BatchStats()
        self.plan: ScanResult | None = None
        self.encoder: str | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, plan: ScanResult, parallel: int = 2, crf: int | None = None, encoder: str | None = None) -> bool:
        if self.running:
            return False
        self.plan = plan
        self._stop.clear()
        self.paused = False
        folders = [f for f in plan.folders if not f.skipped_reason]
        self.stats = BatchStats(n_total=sum(f.n_targets for f in folders), bytes_total=sum(f.total_bytes for f in folders),
                                started_at=time.time())
        s = self.get_settings()
        self._parallel = max(1, int(parallel))
        self._crf = int(crf if crf is not None else s.convert_crf)
        self._encoder_pref = encoder or s.convert_encoder
        self._thread = threading.Thread(target=self._run, args=(folders,), name="batch-convert", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()

    def wait(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # ------------------------------------------------------------------
    def _run(self, folders: list[FolderPlan]) -> None:
        st = self.stats
        try:
            s = self.get_settings()
            ffmpeg = find_ffmpeg(s.ffmpeg_path)
            if not ffmpeg:
                st.message = "ffmpeg が見つかりません。設定の「動画の変換」で場所を指定してください"
                self.on_status(st.message)
                self.on_log(st.message)
                return
            self.on_status("使えるエンコーダを調べています…")
            self.encoder = detect_encoder(ffmpeg, self._encoder_pref)
            self.on_log(f"変換に {self.encoder} を使います" + ("（CPU）" if self.encoder == "libx264" else "（ハードウェア）")
                        + f"、画質 CRF {self._crf}、同時 {self._parallel} 本")
            for fp in folders:
                if self._stop.is_set():
                    break
                self._convert_folder(fp, ffmpeg)
        except Exception as e:  # pragma: no cover
            st.message = f"一括変換でエラー: {e}"
            self.on_log(st.message)
        finally:
            st.finished_at = time.time()
            st.stopped = self._stop.is_set()
            st.current.clear()
            summary = (f"{'中止' if st.stopped else '完了'}: {st.n_done}/{st.n_total} ファイル、"
                       f"{fmt_bytes(st.bytes_in_done)} → {fmt_bytes(st.bytes_out_done)}（{fmt_bytes(st.saved_bytes)} 空き）")
            if st.n_failed:
                summary += f"、失敗 {st.n_failed}（元のまま残しました）"
            st.message = summary
            self.on_status(summary)
            self.on_log(summary)
            if self.ledger is not None and self.plan is not None:
                try:
                    self.ledger.event("batch_convert", f"{self.plan.root}: {summary}")
                except Exception:  # pragma: no cover
                    pass
            if self.on_done:
                self.on_done(st)

    def _wait_if_paused(self) -> None:
        while self.paused and not self._stop.is_set():
            self.on_status("一時停止中")
            time.sleep(0.5)

    def _wait_for_space(self, d: str, need: int) -> bool:
        """変換先に need バイトの空きができるまで待つ。中止されたら False。"""
        while not self._stop.is_set():
            try:
                free = shutil.disk_usage(d).free
            except OSError:
                return True   # 分からなければ進める（失敗は変換側で拾う）
            if free >= need:
                return True
            self.on_status(f"空き待ち: {d} の空きが {fmt_bytes(free)} しかありません（{fmt_bytes(need)} 必要）。30 秒ごとに確認します")
            for _ in range(60):
                if self._stop.is_set():
                    return False
                time.sleep(0.5)
        return False

    def _convert_folder(self, fp: FolderPlan, ffmpeg: str) -> None:
        targets = [t for t in fp.targets if t.action != A_SKIP]
        if not targets:
            return
        results: list[tuple[Target, int, str | None]] = []   # (元, 変換後サイズ, 変換後ハッシュ)
        lock = self._lock

        def work(t: Target) -> None:
            if self._stop.is_set():
                return
            self._wait_if_paused()
            if self._stop.is_set():
                return
            tn = threading.current_thread().name
            with lock:
                self.stats.current[tn] = os.path.join(os.path.basename(fp.dir), t.name)
            self._status_line()
            try:
                r = self._convert_one(fp, t, ffmpeg)
            finally:
                with lock:
                    self.stats.current.pop(tn, None)
            with lock:
                if r is None:
                    self.stats.n_failed += 1
                    fp.n_failed += 1
                else:
                    size_out, h = r
                    results.append((t, size_out, h))
                    self.stats.n_done += 1
                    self.stats.bytes_in_done += t.size
                    self.stats.bytes_out_done += size_out
                    fp.n_done += 1
                    fp.bytes_in += t.size
                    fp.bytes_out += size_out
                    if t.action == A_FINISH:
                        self.stats.n_finished_only += 1
            self._status_line()

        with ThreadPoolExecutor(max_workers=self._parallel, thread_name_prefix="batch-ffmpeg") as pool:
            list(pool.map(work, targets))
        if results:
            self._finalize_folder(fp, results)

    def snapshot(self) -> BatchStats:
        """画面表示用に、集計のコピーを返す（スレッドをまたいで読んでも安全）。"""
        import dataclasses
        with self._lock:
            return dataclasses.replace(self.stats, current=dict(self.stats.current))

    def _status_line(self) -> None:
        st = self.snapshot()
        cur = "、".join(sorted(st.current.values()))
        eta = st.eta_sec
        eta_s = f"、残り約 {eta/60:.0f} 分" if eta is not None and eta > 0 else ""
        self.on_status(f"{st.n_done}/{st.n_total} ファイル（{fmt_bytes(st.bytes_in_done)}/{fmt_bytes(st.bytes_total)}、"
                       f"{fmt_bytes(st.saved_bytes)} 空き{eta_s}）" + (f"　変換中: {cur}" if cur else ""))

    def _convert_one(self, fp: FolderPlan, t: Target, ffmpeg: str) -> tuple[int, str | None] | None:
        """1 ファイルを変換して元を消す。(変換後サイズ, ハッシュ) を返す。失敗は None（元はそのまま）。"""
        src = os.path.join(fp.dir, t.name)
        dst = os.path.join(fp.dir, t.out_name)
        part = dst + PART_SUFFIX
        label = os.path.join(os.path.basename(fp.dir), t.name)
        if not os.path.isfile(src):
            if os.path.isfile(dst) and os.path.getsize(dst) > 0:
                return os.path.getsize(dst), (hash_file(dst) if fp.has_manifest else None)
            self.on_log(f"{label}: 元ファイルが見つかりません")
            return None
        if t.action == A_FINISH:
            # 前回、変換後に元を消す前に止まった。長さが合えば元を消すだけ。合わなければ既存は触らず別名に変換する
            a, b = probe(ffmpeg, src), probe(ffmpeg, dst)
            if a and b and b.vcodec == "h264" and _durations_match(a.duration, b.duration):
                try:
                    os.remove(src)
                except OSError as e:
                    self.on_log(f"{label}: 元ファイルを消せません: {e}")
                    return None
                self.on_log(f"{label}: 変換済みの {t.out_name} があったので元ファイルだけ消しました")
                return os.path.getsize(dst), (hash_file(dst) if fp.has_manifest else None)
            stem = t.name.rsplit(".", 1)[0] if "." in t.name else t.name
            alt = stem + "_h264.mp4"
            if t.out_name.lower() == alt.lower() or os.path.exists(os.path.join(fp.dir, alt)):
                self.on_log(f"{label}: 既存の {t.out_name} は変換結果ではないようで、別名も使えないので残します")
                return None
            self.on_log(f"{label}: 既存の {t.out_name} は変換結果ではないようなので {alt} に変換します")
            t.out_name = alt
            dst = os.path.join(fp.dir, alt)
            part = dst + PART_SUFFIX
        if not self._wait_for_space(fp.dir, t.size + 256 * 1024 * 1024):
            return None
        t0 = time.time()
        ok, msg = convert_to_mp4(ffmpeg, src, part, self.encoder or "libx264", self._crf, stop=self._stop)
        if not ok:
            try:
                os.remove(part)
            except OSError:
                pass
            if not self._stop.is_set():
                self.on_log(f"{label}: 変換に失敗: {msg}（元のまま残します）")
            return None
        try:
            os.utime(part, (t.mtime, t.mtime))   # 撮影日時の手がかりになる更新日時を引き継ぐ
            os.replace(part, dst)
        except OSError as e:
            self.on_log(f"{label}: 変換結果を置けません: {e}")
            try:
                os.remove(part)
            except OSError:
                pass
            return None
        size = os.path.getsize(dst)
        h = hash_file(dst) if fp.has_manifest else None
        try:
            os.remove(src)
        except OSError as e:
            self.on_log(f"{label}: 変換後に元ファイルを消せません: {e}（次回の走査で消します）")
        dt = time.time() - t0
        self.on_log(f"{label} → {t.out_name}（{t.size/1e6:.0f} MB → {size/1e6:.0f} MB、{dt:.0f}秒、{self.encoder}）")
        return size, h

    def _finalize_folder(self, fp: FolderPlan, results: list[tuple[Target, int, str | None]]) -> None:
        """manifest と台帳をこのフォルダの実体に合わせる。"""
        if fp.has_manifest:
            try:
                update_manifest(fp.dir, [(t.name, t.out_name, size, h, t.old_hash) for t, size, h in results])
            except OSError as e:
                self.on_log(f"{fp.dir}: manifest.json を更新できません: {e}")
        if fp.ledger_authoritative and fp.ingest_id is not None and self.ledger is not None:
            recs = {f.name: f for f in self.ledger.files_for(fp.ingest_id)}
            n = 0
            for t, size, h in results:
                r = recs.get(t.name)
                if r is None or h is None:
                    continue   # 台帳では既に別の名前（他方のコピーを先に変換した等）
                self.ledger.set_file_converted(fp.ingest_id, t.name, t.out_name, size, h, r.hash)
                n += 1
            if n:
                self.ledger.update_ingest(fp.ingest_id, convert_done_at=L.now_iso())
        summary = f"{fp.dir}: {fp.n_done} ファイル変換（{fmt_bytes(fp.bytes_in)} → {fmt_bytes(fp.bytes_out)}）"
        if fp.n_failed:
            summary += f"、失敗 {fp.n_failed}"
        self.on_log(summary)


def update_manifest(directory: str, changes: list[tuple[str, str, int, str | None, str | None]]) -> None:
    """manifest.json のファイル項目を変換後の実体に合わせる。changes: (元の名前, 変換後の名前, サイズ, ハッシュ, 元のハッシュ)。
    台帳と同じ形式（name/size/hash を変換後に、元の情報は src_*）。"""
    m = L.Ledger.read_manifest(directory)
    if not m:
        return
    files = m.get("files")
    if not isinstance(files, list):
        return
    by_name = {str(f.get("name")): f for f in files if isinstance(f, dict)}
    for src_name, out_name, size, h, old_hash in changes:
        f = by_name.get(src_name)
        if f is None:
            continue
        f.update({"src_name": f.get("src_name") or src_name, "src_size": f.get("src_size") if f.get("src_size") is not None else f.get("size"),
                  "src_hash": f.get("src_hash") or old_hash or f.get("hash"),
                  "name": out_name, "size": size, "hash": h, "converted": True})
    m["n_files"] = len(files)
    m["total_bytes"] = sum(int(f.get("size") or 0) for f in files if isinstance(f, dict))
    m["batch_converted_at"] = L.now_iso()
    p = os.path.join(directory, L.MANIFEST_NAME)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(m, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
