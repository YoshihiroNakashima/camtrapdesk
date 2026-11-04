import os
import threading
import time

from openpyxl import Workbook

from camtrapdesk.config import Settings
from camtrapdesk.core import ledger as L
from camtrapdesk.core.cards import detect_cards, normalize_card_id
from camtrapdesk.core.clear import check_card, delete_files
from camtrapdesk.core.fieldnote import build_mapping, format_station, load_fieldnote
from camtrapdesk.core.hashing import hash_file
from camtrapdesk.core.ingest import IngestManager, S_DONE, S_ERROR, S_SKIPPED
from camtrapdesk.core.nas_sync import NasSyncer
from camtrapdesk.core.rename import A_BLOCKED, A_RENAME, A_UNMAPPED, apply_plan, build_plan
from camtrapdesk.core.volumes import list_volumes

from conftest import make_card, nakashima_settings


def run_ingest(env, timeout=30):
    s, ledger = env["settings"], env["ledger"]
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append, armed=True)
    mgr.start()
    t0 = time.time()
    while time.time() - t0 < timeout:
        mgr.rescan_now()
        jobs = list(mgr.jobs.values())
        if jobs and all(j.state in (S_DONE, S_ERROR, S_SKIPPED) for j in jobs):
            break
        time.sleep(0.2)
    mgr.stop()
    return mgr, logs


def test_normalize_card_id():
    assert normalize_card_id("012") == "12"
    assert normalize_card_id(" 264 ") == "264"
    assert normalize_card_id("6_650") == "650"
    assert normalize_card_id("5_315") == "315"
    assert normalize_card_id("BRW_573") == "573"
    assert normalize_card_id("brw-07a") == "BRW07A"


def test_detect_and_ingest(env):
    cards = env["cards"]
    make_card(cards, "264", n_files=3)
    make_card(cards, "110", n_files=2)
    vols = list_volumes(scan_roots=[str(cards)])
    assert len(vols) == 2
    found = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)
    assert {c.card_id for c in found} == {"264", "110"}

    mgr, logs = run_ingest(env)
    states = {j.card.card_id: j.state for j in mgr.jobs.values()}
    assert states == {"264": S_DONE, "110": S_DONE}, logs
    dest = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(dest)) == ["IMG_0000.MP4", "IMG_0001.MP4", "IMG_0002.MP4", "manifest.json"]
    ing = env["ledger"].latest_ingest_for_card("264")
    assert ing.status == L.STATUS_STAGED
    files = env["ledger"].files_for(ing.id)
    assert all(f.hash and f.staging_ok for f in files)
    src_hash = hash_file(os.path.join(str(cards), "vol_264", "DCIM", "100_BTCF", "IMG_0000.MP4"))
    assert files[0].hash == src_hash

    # 2回目: 取り込み済みとしてスキップ
    mgr2, logs2 = run_ingest(env)
    states2 = {j.card.card_id: j.state for j in mgr2.jobs.values()}
    assert states2 == {"264": S_SKIPPED, "110": S_SKIPPED}


def test_ingest_small_file_is_only_a_warning(env):
    cards = env["cards"]
    root, _ = make_card(cards, "300", n_files=2)
    with open(os.path.join(root, "DCIM", "100_BTCF", "BAD.MP4"), "wb") as f:
        f.write(b"x" * 10)
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE
    assert any("小さいファイル" in w for w in job.card.warnings)


def test_ingest_rejects_single_video_card(env):
    """動画が1枚しかないカードはカメラ不調の疑いとしてエラーにし、コピーしない。"""
    make_card(env["cards"], "301", n_files=1)
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_ERROR and "1枚" in job.message
    assert not os.path.exists(env["dest"] / "N260710" / "301")


def test_nas_sync_and_clear(env):
    cards = env["cards"]
    make_card(cards, "264", n_files=2)
    run_ingest(env)
    s, ledger = env["settings"], env["ledger"]
    status = []
    syncer = NasSyncer(lambda: s, ledger, on_status=status.append, on_log=status.append)
    # 直接 tick を回す
    for _ in range(5):
        syncer._tick()
        if not ledger.pending_nas():
            break
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_SYNCED
    nas_dir = env["nas"] / "N260710" / "264"
    assert sorted(os.listdir(nas_dir)) == ["IMG_0000.MP4", "IMG_0001.MP4", "manifest.json"]
    assert all(f.nas_ok for f in ledger.files_for(ing.id))
    # 既定では中継のコピーは残す（控え）
    assert not ing.staging_cleared_at
    assert (env["dest"] / "N260710" / "264").is_dir()

    # 削除判定
    vols = list_volumes(scan_roots=[str(cards)])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)[0]
    v = check_card(card, ledger, s)
    assert v.ok, v.reasons
    assert len(v.deletable) == 2
    n, errs = delete_files(v, ledger, log=lambda m: None)
    assert n == 2 and not errs
    assert os.listdir(os.path.join(str(cards), "vol_264", "DCIM", "100_BTCF")) == []
    assert os.path.exists(os.path.join(str(cards), "vol_264", "id.txt"))
    assert ledger.latest_ingest_for_card("264") is None   # cleared
    assert ledger.latest_ingest_for_card("264", include_cleared=True).cleared_at


def test_clear_requires_nas(env):
    cards = env["cards"]
    make_card(cards, "264", n_files=2)
    run_ingest(env)
    s, ledger = env["settings"], env["ledger"]
    vols = list_volumes(scan_roots=[str(cards)])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)[0]
    v = check_card(card, ledger, s)
    assert not v.ok and any("NAS" in r for r in v.reasons)


def make_fieldnote(path, rows):
    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws.append(["rows", "Date", "Time", "Season", "State", "Station", "Type", "InstallationSD", "RetrievalSD", "Remarks"])
    for i, r in enumerate(rows, 1):
        ws.append([i, "2026/7/10", None, r["season"], r.get("state", "継続"), r["station"], r.get("type", "N"),
                   r.get("inst"), r.get("ret"), r.get("remarks")])
    wb.save(path)


def test_format_station():
    assert format_station("482", "BSN", 4) == "BSN0482"
    assert format_station("BSN730", "BSN", 4) == "BSN0730"
    assert format_station("_045", "BSN", 4) == "BSN0045"
    assert format_station("482", "", 4) == "0482"        # 接頭辞なし → 数字だけ
    assert format_station("482", "", 0) == "482"
    assert format_station("0482", "ST", 0) == "ST0482"   # 桁数 0 → 番号は書かれたまま
    assert format_station("Ridge-03", "", 0) == "Ridge-03"
    assert format_station("Ridge-A", "BSN", 4) == "Ridge-A"   # 番号でないものはそのまま
    assert format_station("MUC0727", "BSN", 4) == "MUC0727"


def test_fieldnote_mapping_and_rename(env):
    s, ledger = env["settings"], env["ledger"]
    fn_path = env["tmp"] / "fieldnote.xlsx"
    make_fieldnote(fn_path, [
        {"season": 260424, "station": 482, "inst": 319, "ret": 10},
        {"season": 260424, "station": 980, "inst": 555, "ret": 11},
        {"season": 260710, "station": 482, "inst": 260, "ret": 319},
        {"season": 260710, "station": 980, "inst": 555, "ret": 319},   # 重複 SD
        {"season": 260710, "station": 592, "inst": 74, "ret": 318},
        {"season": 260710, "station": 735, "inst": 560, "ret": 295},
        {"season": 260710, "station": 999, "inst": 1, "ret": 777, "type": "W"},
    ])
    fn = load_fieldnote(str(fn_path), s)
    assert fn.distinct("Type")[0] == "N"
    assert sorted(fn.distinct("Season")) == ["260424", "260710"]
    assert len(fn.filter([("Type", "N"), ("Season", "260710")])) == 4
    assert len(fn.filter([("season", "260710.0")])) == 5      # 列名の大文字小文字、数値表記の違いは無視
    assert fn.filter([("NoSuchColumn", "x")]) == []
    m = build_mapping(fn, [("Type", "N"), ("Season", "260710")], s)
    assert m.condition_text == "Type = N、Season = 260710"
    by = {e.station_folder: e for e in m.entries}
    assert by["BSN0592"].card_id == "318" and not by["BSN0592"].errors
    assert by["BSN0482"].errors and by["BSN0980"].errors        # 重複は両方保留
    assert m.issues

    # フォルダを用意: 318, 295 は取り込み済み。777 は別Type。
    cards = env["cards"]
    make_card(cards, "318", n_files=2)
    make_card(cards, "295", n_files=2)
    make_card(cards, "319", n_files=2)
    run_ingest(env)
    (env["dest"] / "N260710" / "UNKNOWN").mkdir()
    plan = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    actions = {i.card_id: i.action for i in plan.items}
    # NAS 未転送なので syncing になるはず
    from camtrapdesk.core.rename import A_SYNCING
    assert actions["318"] == A_SYNCING
    # NAS を済ませてから
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    plan = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    actions = {i.card_id: i.action for i in plan.items}
    assert actions["318"] == A_RENAME and actions["295"] == A_RENAME
    assert actions["319"] == A_BLOCKED
    assert actions["UNKNOWN"] == A_UNMAPPED
    assert {i.card_id: (i.in_staging, i.in_nas) for i in plan.items}["318"] == (True, True)
    n, errs = apply_plan(plan, ledger, log=lambda m: None)
    assert n == 2 and not errs, errs
    assert (env["dest"] / "N260710" / "BSN0592").is_dir()
    assert (env["nas"] / "N260710" / "BSN0592").is_dir()
    ing = ledger.latest_ingest_for_card("318")
    assert ing.dir_name == "BSN0592" and ing.station == "BSN0592"
    man = L.Ledger.read_manifest(str(env["dest"] / "N260710" / "BSN0592"))
    assert man["station"] == "BSN0592" and man["renamed_from"] == "318"
    # 2回目のドライランは「変更済み」
    plan2 = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    from camtrapdesk.core.rename import A_ALREADY
    assert {i.card_id: i.action for i in plan2.items}["318"] == A_ALREADY


def test_real_fieldnote_if_present():
    path = "/mnt/user-data/uploads/14_最新のカメラデータ/野帳写し.xlsx"
    if not os.path.exists(path):
        return
    s = nakashima_settings()
    fn = load_fieldnote(path, s)
    assert "N" in fn.distinct("Type")
    m = build_mapping(fn, [("Type", "N"), ("Season", "260710")], s, season_folder="N260710")
    assert m.season_folder == "N260710"
    assert len(m.entries) == 148
    dup = [e for e in m.entries if e.errors]
    assert len(dup) == 20   # 10組の重複
    ok = [e for e in m.entries if not e.errors]
    assert all(e.station_folder.startswith("BSN") and len(e.station_folder) == 7 for e in ok)


def test_fieldnote_tolerant_headers(tmp_path):
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active; ws.title = "Sheet1"
    ws.append(["Date", "Time", "Season", "State", "Statuion", "Type", "InstallationSD", "RetrievalSD"])
    ws.append(["2026/10/17", None, "N261016", "継続", 690, "N", 269, 263])
    ws.append(["2026/10/17", None, "N261016", "継続", 617, "W", 640, 315])
    p = tmp_path / "dna.xlsx"; wb.save(p)
    s = nakashima_settings()
    fn = load_fieldnote(str(p), s)
    assert any("Statuion" in n for n in fn.notes)
    assert fn.distinct("Season") == ["N261016"]
    m = build_mapping(fn, [("Type", "N"), ("Season", "N261016")], s, season_folder="N261016")
    assert m.season_folder == "N261016"          # 変更するフォルダは野帳の Season とは別に指定する
    assert [e.station_folder for e in m.entries] == ["BSN0690"]
    assert m.entries[0].card_id == "263"
    # 列が本当に無いときは、ファイルの列一覧を含むエラー
    s2 = nakashima_settings(col_retrieval_sd="KaishuSD")
    try:
        load_fieldnote(str(p), s2)
        assert False
    except ValueError as e:
        assert "KaishuSD" in str(e) and "RetrievalSD" in str(e)


def test_rename_matches_prefixed_card_id(env):
    """id.txt が "6_650" で野帳の RetrievalSD が 650 でも対応づく。"""
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "6_650", n_files=2)
    run_ingest(env)
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    fn_path = env["tmp"] / "fn.xlsx"
    make_fieldnote(fn_path, [{"season": "N260710", "station": 1266, "ret": 650}])
    fn = load_fieldnote(str(fn_path), s)
    m = build_mapping(fn, [("Type", "N"), ("Season", "N260710")], s)
    plan = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    it = {i.card_id: i for i in plan.items}["650"]
    assert it.action == A_RENAME and it.src_dir_name == "6_650" and it.dst_dir_name == "BSN1266"


def test_interrupted_ingest_resumes(env):
    """途中で止まった取り込み（status=copying）が残っていても、次回は再開して完了する。"""
    ledger = env["ledger"]
    make_card(env["cards"], "7_100", n_files=3)
    run_ingest(env)
    ing = ledger.latest_ingest_for_card("100")
    # 中断を模擬: 台帳を copying に戻し、保存先の最後のファイルを消して .part を残す
    ledger.update_ingest(ing.id, status=L.STATUS_COPYING, staging_verified_at=None, finished_at=None)
    d = env["dest"] / "N260710" / "7_100"
    os.remove(d / "IMG_0002.MP4"); os.remove(d / "manifest.json")
    (d / "IMG_0002.MP4.part").write_bytes(b"xx")
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE, logs
    assert any("再開" in m for m in logs)
    assert sorted(os.listdir(d)) == ["IMG_0000.MP4", "IMG_0001.MP4", "IMG_0002.MP4", "manifest.json"]
    new = ledger.latest_ingest_for_card("100")
    assert new.id != ing.id and new.status == L.STATUS_STAGED
    assert ledger.get_ingest(ing.id).status == L.STATUS_ERROR


def test_assign_id_to_card_without_idtxt(env):
    """id.txt の無いカードは一覧（others）に出て、ID を付けると取り込める。"""
    s, ledger = env["settings"], env["ledger"]
    root, _ = make_card(env["cards"], "999", n_files=2)
    os.remove(os.path.join(root, "id.txt"))
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append)
    mgr._scan_once()
    assert not mgr.jobs and len(mgr.others) == 1
    key, (vol, reason) = next(iter(mgr.others.items()))
    assert "id.txt がありません" in reason
    ok, msg = mgr.assign_card_id(key, "6_999")
    assert ok, msg
    assert open(os.path.join(root, "id.txt"), encoding="utf-8").read() == "6_999\n"
    mgr._scan_once()
    assert not mgr.others and len(mgr.jobs) == 1
    assert next(iter(mgr.jobs.values())).card.card_id == "6_999"
    # 既に id.txt があるカードには書かない
    assert mgr.assign_card_id(key, "x")[0] is False


def test_rename_works_when_only_nas_copy_remains(env):
    """中継のコピーが（NAS検証後の自動削除で）無くても、NAS 側のフォルダだけ地点名に変えられる。"""
    s, ledger = env["settings"], env["ledger"]
    s.staging_cleanup_after_nas = True
    make_card(env["cards"], "318", n_files=2)
    run_ingest(env)
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    assert not (env["dest"] / "N260710" / "318").exists()
    fn_path = env["tmp"] / "fn.xlsx"
    make_fieldnote(fn_path, [{"season": 260710, "station": 592, "ret": 318}])
    m = build_mapping(load_fieldnote(str(fn_path), s), [("Season", "260710")], s)
    plan = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    it = {i.card_id: i for i in plan.items}["318"]
    assert it.action == A_RENAME and (it.in_staging, it.in_nas) == (False, True)
    n, errs = apply_plan(plan, ledger, log=lambda m: None)
    assert n == 1 and not errs, errs
    assert (env["nas"] / "N260710" / "BSN0592").is_dir()
    assert ledger.latest_ingest_for_card("318").dir_name == "BSN0592"
    # 削除判定は NAS 側の実体で行う
    vols = list_volumes(scan_roots=[str(env["cards"])])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)[0]
    v = check_card(card, ledger, s)
    assert v.ok, v.reasons
    assert v.checked_destination == str(env["nas"] / "N260710" / "BSN0592")


def test_leftover_staging_copy_is_cleaned_later(env):
    """自動削除を後から有効にしても、NAS 検証済みで中継に残っている分は待機中に消される。"""
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "264", n_files=2)
    run_ingest(env)
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    assert (env["dest"] / "N260710" / "264").is_dir()
    s.staging_cleanup_after_nas = True
    syncer._tick()   # 未転送なし → 残りの掃除
    assert not (env["dest"] / "N260710" / "264").exists()
    assert ledger.latest_ingest_for_card("264").staging_cleared_at


def test_ingest_waits_for_staging_space(env, monkeypatch):
    """中継の空きが足りないときはコピーせずに待ち、空いたら続きから始める。"""
    from camtrapdesk.core import ingest as I
    monkeypatch.setattr(I, "SPACE_WAIT_SEC", 0.1)
    free = {"v": 10_000}            # まずは足りない（カードは 2×200KB、余裕は 0）
    monkeypatch.setattr(I, "free_bytes", lambda path: free["v"])
    s, ledger = env["settings"], env["ledger"]
    s.min_free_gb = 0
    make_card(env["cards"], "264", n_files=2)
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append, armed=True)
    mgr.start()
    mgr.rescan_now()
    t0 = time.time()
    while time.time() - t0 < 10:
        if any(j.state == I.S_WAITING for j in mgr.jobs.values()):
            break
        time.sleep(0.05)
    job = next(iter(mgr.jobs.values()))
    assert job.state == I.S_WAITING, (job.state, job.message)
    assert not (env["dest"] / "N260710" / "264").exists()
    assert mgr.active_count() == 0      # 空き待ちは「取り込み中」に数えない（NAS転送を止めない）
    free["v"] = 10**12
    t0 = time.time()
    while time.time() - t0 < 10 and job.state != S_DONE:
        time.sleep(0.05)
    mgr.stop()
    assert job.state == S_DONE, (job.state, job.message)
    assert ledger.latest_ingest_for_card("264").status == L.STATUS_STAGED


def _wait_until(pred, timeout=10):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


def test_switch_staging_ssd_while_waiting(env, monkeypatch):
    """中継がいっぱいで空き待ちのとき、設定の中継先を別のSSDに変えると、そちらへ取り込みが続く。"""
    from camtrapdesk.core import ingest as I
    from camtrapdesk.core import roots
    monkeypatch.setattr(I, "SPACE_WAIT_SEC", 0.1)
    s, ledger = env["settings"], env["ledger"]
    s.min_free_gb = 0
    ssd_a, ssd_b = env["dest"], env["tmp"] / "ssd_b"
    ssd_b.mkdir()
    monkeypatch.setattr(I, "free_bytes", lambda path: 1_000 if os.path.normpath(path) == os.path.normpath(str(ssd_a)) else 10**12)
    make_card(env["cards"], "264", n_files=2)
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=lambda m: None, armed=True)
    mgr.start()
    mgr.rescan_now()
    assert _wait_until(lambda: any(j.state == I.S_WAITING for j in mgr.jobs.values()))
    job = next(iter(mgr.jobs.values()))
    s.dest_root = str(ssd_b)          # 画面の「中継先を変更…」に相当
    assert _wait_until(lambda: job.state == S_DONE), (job.state, job.message)
    mgr.stop()
    ing = ledger.latest_ingest_for_card("264")
    assert ing.dest_root == str(ssd_b) and ing.status == L.STATUS_STAGED
    assert (ssd_b / "N260710" / "264" / "IMG_0001.MP4").is_file()
    assert not (ssd_a / "N260710" / "264").exists()
    # 中継先には目印が置かれ、台帳にそのIDが入る
    assert ing.dest_root_id and roots.read_root_id(str(ssd_b)) == ing.dest_root_id


def test_no_space_mid_copy_resumes_after_space_returns(env, monkeypatch):
    """コピー中に中継がいっぱいになったら、エラーで止めずに待って、空いたら続きから再開する。"""
    import errno
    from camtrapdesk.core import ingest as I
    monkeypatch.setattr(I, "SPACE_WAIT_SEC", 0.1)
    s, ledger = env["settings"], env["ledger"]
    s.min_free_gb = 0
    state = {"fail": True}
    real = I.copy_with_hash

    def flaky(src, dst, prog=None):
        if state["fail"] and dst.endswith("IMG_0001.MP4"):
            state["fail"] = False
            with open(dst + ".part", "wb") as f:
                f.write(b"partial")
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst, prog)

    monkeypatch.setattr(I, "copy_with_hash", flaky)
    make_card(env["cards"], "264", n_files=3)
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append, armed=True)
    mgr.start()
    mgr.rescan_now()
    job = None
    assert _wait_until(lambda: bool(mgr.jobs))
    job = next(iter(mgr.jobs.values()))
    assert _wait_until(lambda: job.state == S_DONE), (job.state, job.message, logs)
    mgr.stop()
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_STAGED
    d = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(d)) == ["IMG_0000.MP4", "IMG_0001.MP4", "IMG_0002.MP4", "manifest.json"]
    assert any("再開" in m for m in logs)
    errs = [i for i in ledger.ingests() if i.status == L.STATUS_ERROR]
    assert len(errs) == 1 and "再開" in (errs[0].message or "")   # 中断した記録は再開済みの印が付く


def test_nas_sync_finds_staging_after_drive_letter_change(env, monkeypatch):
    """中継SSDを抜き差ししてパスが変わっても、目印で見つけ直してNAS転送を続ける。抜いている間はエラーにせず待つ。"""
    from camtrapdesk.core import roots
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "264", n_files=2)
    run_ingest(env)
    ing = ledger.latest_ingest_for_card("264")
    assert ing.dest_root_id
    # SSD を抜いた: 中継先フォルダごと別の場所へ（= ドライブ文字が変わった）
    moved = env["tmp"] / "mounted_elsewhere" / "stage"
    moved.parent.mkdir()
    os.rename(str(env["dest"]), str(moved))
    monkeypatch.setattr(roots, "MISS_RETRY_SEC", 0.0)
    monkeypatch.setattr(roots, "volume_bases", lambda exclude=(): [])   # まだどこにも見えない
    status = []
    syncer = NasSyncer(lambda: s, ledger, on_status=status.append, on_log=status.append)
    syncer._tick()
    assert ledger.latest_ingest_for_card("264").status == L.STATUS_STAGED   # エラーにしない
    assert any("見つかりません" in m for m in status)
    # 繋ぎ直した（別の場所に見える）
    monkeypatch.setattr(roots, "volume_bases", lambda exclude=(): [str(env["tmp"] / "mounted_elsewhere")])
    for _ in range(5):
        syncer._tick()
        if not ledger.pending_nas():
            break
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_SYNCED
    assert ing.dest_root == str(moved)          # 台帳のパスも直っている
    assert (env["nas"] / "N260710" / "264" / "IMG_0001.MP4").is_file()


def test_generic_fieldnote_without_season_and_type(env):
    """Season/Type 列が無い野帳、整形ルール無し、シーズンフォルダ無しでも一通り動く（他の研究室向けの既定）。"""
    from openpyxl import Workbook
    s = Settings(dest_root=str(env["dest"]), nas_root=str(env["nas"]), season_folder="",
                 scan_roots=[str(env["cards"])], concurrency=2, poll_interval_sec=0.2, min_free_gb=0,
                 broken_check=False)   # make_card の .MP4 は中身がでたらめなので
    s.station_prefix, s.station_digits = "", 0    # 接頭辞もゼロ埋めも無し → 野帳の値そのまま
    env["settings"] = s
    ledger = env["ledger"]
    make_card(env["cards"], "12", n_files=2)
    mgr, logs = run_ingest(env)
    assert {j.state for j in mgr.jobs.values()} == {S_DONE}, logs
    assert (env["dest"] / "12" / "IMG_0000.MP4").is_file()     # シーズンフォルダ無し → 保存先の直下
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    assert (env["nas"] / "12" / "IMG_0000.MP4").is_file()

    wb = Workbook(); ws = wb.active
    ws.append(["Station", "RetrievalSD", "Remarks"])
    ws.append(["Ridge-03", 12, ""])
    ws.append(["Creek 7", 99, ""])
    p = env["tmp"] / "simple.xlsx"; wb.save(p)
    fn = load_fieldnote(str(p), s)
    assert fn.columns == ["Station", "RetrievalSD", "Remarks"]
    m = build_mapping(fn, [], s)                                   # 条件なし → 全行
    assert m.condition_text.startswith("（絞り込みなし")
    bad = build_mapping(fn, [("Season", "1")], s)
    assert not bad.entries and any("ありません" in i for i in bad.issues)
    assert m.season_folder == "" and [e.station_folder for e in m.entries] == ["Ridge-03", "Creek 7"]
    plan = build_plan(str(env["dest"]), str(env["nas"]), m, ledger)
    acts = {i.card_id: i.action for i in plan.items}
    assert acts["12"] == A_RENAME
    from camtrapdesk.core.rename import A_MISSING
    assert acts["99"] == A_MISSING
    n, errs = apply_plan(plan, ledger, log=lambda m: None)
    assert n == 1 and not errs, errs
    assert (env["dest"] / "Ridge-03").is_dir() and (env["nas"] / "Ridge-03").is_dir()
    # 台帳を消して manifest から再構築できる（シーズンフォルダ無しの配置）
    ledger2 = L.Ledger(str(env["tmp"] / "ledger2.sqlite"))
    assert ledger2.rebuild_from_manifests(str(env["dest"]), str(env["nas"])) == 1
    assert ledger2.find_by_dir("", "Ridge-03").station == "Ridge-03"


def test_default_settings_are_not_user_specific():
    s = Settings()
    assert s.fieldnote_path == "" and s.volume_label_prefix == "" and s.rename_filters == []
    assert (s.station_prefix, s.station_digits) == ("BSN", 4)   # 既定は BSN + 4桁。接頭辞を空にすると数字だけ
    assert format_station(" 0482 ", "", 4) == "0482"
    # 旧形式の設定（Type ごとの規則）からの読み替え
    old = {"type_rules": {"N": {"season_prefix": "N", "station_prefix": "ST", "station_pad": 3}}, "default_type": "N"}
    s2 = Settings.from_dict(old)
    assert (s2.station_prefix, s2.station_digits) == ("ST", 3)


def test_other_camera_folder_falls_back_to_dcim(env):
    """設定の対象フォルダ（DCIM/100_BTCF）が無い別機種のカードでも、DCIM 以下を探して取り込める。"""
    make_card(env["cards"], "77", n_files=2, subdir="DCIM/101RECNX")
    vols = list_volumes(scan_roots=[str(env["cards"])])
    found = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)
    assert len(found) == 1 and found[0].n_files == 2 and not found[0].problems
    assert any("DCIM 以下" in w for w in found[0].warnings)


def make_avi(path, seconds=0.5):
    """テスト用の小さな MJPEG の AVI を ffmpeg で作る。ffmpeg が無ければ None。"""
    import subprocess
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    ff = find_ffmpeg()
    if not ff:
        return None
    r = subprocess.run([ff, "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=96x64:rate=10",
                        "-c:v", "mjpeg", "-q:v", "5", "-an", str(path)], capture_output=True)
    return path if r.returncode == 0 else None


def test_convert_avi_to_mp4_in_staging(env):
    """変換を有効にすると、SD → 中継 は AVI のまま検証コピーし、中継で mp4 に変換して AVI を消し、NAS へは mp4 だけ送る。
    SD の削除判定は元の AVI の名前・サイズで行える。"""
    import pytest
    from camtrapdesk.core.convert import Converter
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    s, ledger = env["settings"], env["ledger"]
    s.convert_enabled = True
    s.convert_encoder = "libx264"
    s.convert_parallel = 1
    s.media_extensions = ["mp4", "avi"]
    cards = env["cards"]
    root, _names = make_card(cards, "264", n_files=1)          # IMG_0000.MP4（変換対象外）
    media = os.path.join(root, "DCIM", "100_BTCF")
    assert make_avi(os.path.join(media, "IMG_0001.AVI")) and make_avi(os.path.join(media, "IMG_0002.AVI"))
    avi_size = os.path.getsize(os.path.join(media, "IMG_0001.AVI"))
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE and "変換" in job.message, (job.message, logs)
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_STAGED and ing.convert_pending == 2
    d = env["dest"] / "N260710" / "264"
    assert (d / "IMG_0001.AVI").is_file()
    assert ledger.pending_nas() == []                             # 変換が終わるまで NAS 転送は待つ

    status = []
    conv = Converter(lambda: s, ledger, on_status=status.append, on_log=logs.append)
    for _ in range(5):
        conv._tick()
        if not ledger.pending_convert():
            break
    ing = ledger.latest_ingest_for_card("264")
    assert ing.convert_pending == 0 and ing.convert_done_at
    assert sorted(os.listdir(d)) == ["IMG_0000.MP4", "IMG_0001.mp4", "IMG_0002.mp4", "manifest.json"]
    recs = {f.name: f for f in ledger.files_for(ing.id)}
    r = recs["IMG_0001.mp4"]
    assert r.converted and r.src_name == "IMG_0001.AVI" and r.src_size == avi_size and r.src_hash and r.hash
    assert r.size == os.path.getsize(d / "IMG_0001.mp4") and r.hash == hash_file(str(d / "IMG_0001.mp4"))
    assert not recs["IMG_0000.MP4"].converted
    man = L.Ledger.read_manifest(str(d))
    assert {f["name"] for f in man["files"]} == {"IMG_0000.MP4", "IMG_0001.mp4", "IMG_0002.mp4"}
    assert [f for f in man["files"] if f["name"] == "IMG_0001.mp4"][0]["src_name"] == "IMG_0001.AVI"

    # NAS へは mp4 だけ
    assert len(ledger.pending_nas()) == 1
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=logs.append)
    while ledger.pending_nas():
        syncer._tick()
    assert sorted(os.listdir(env["nas"] / "N260710" / "264")) == ["IMG_0000.MP4", "IMG_0001.mp4", "IMG_0002.mp4", "manifest.json"]

    # 同じカードを挿し直しても「取り込み済み」（元の名前・サイズで判定）
    mgr2, logs2 = run_ingest(env)
    assert next(iter(mgr2.jobs.values())).state == S_SKIPPED, logs2

    # SD の削除判定: カード上は AVI、台帳は mp4 でも照合できる
    vols = list_volumes(scan_roots=[str(cards)])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4", "avi"], False, 1024)[0]
    v = check_card(card, ledger, s, verify_hash=True)
    assert v.ok, v.reasons
    assert sorted(v.deletable) == ["IMG_0000.MP4", "IMG_0001.AVI", "IMG_0002.AVI"]
    n, errs = delete_files(v, ledger, log=lambda m: None)
    assert n == 3 and not errs
    assert ledger.latest_ingest_for_card("264") is None   # cleared


def test_convert_off_releases_pending_cards(env):
    """変換を後からオフにすると、変換待ちだった分は元のまま NAS へ送られる。"""
    from camtrapdesk.core.convert import Converter
    s, ledger = env["settings"], env["ledger"]
    s.convert_enabled = True
    s.media_extensions = ["avi"]
    root, _ = make_card(cards_dir := env["cards"], "9", n_files=0)
    media = os.path.join(root, "DCIM", "100_BTCF")
    for n in ("A.AVI", "B.AVI"):
        with open(os.path.join(media, n), "wb") as f:
            f.write(b"x" * 5000)
    run_ingest(env)
    ing = ledger.latest_ingest_for_card("9")
    assert ing.convert_pending == 2 and ledger.pending_nas() == []
    s.convert_enabled = False
    Converter(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)._tick()
    assert ledger.latest_ingest_for_card("9").convert_pending == 0 and len(ledger.pending_nas()) == 1


def test_lost_id_file_is_suggested_from_volume_serial(env, monkeypatch):
    """id.txt を消してしまったカードは、前回取り込んだときのボリュームシリアルから ID の候補が出る。"""
    import dataclasses
    from camtrapdesk.core import ingest as I
    s, ledger = env["settings"], env["ledger"]
    root, _ = make_card(env["cards"], "650", n_files=2)
    real = I.list_volumes

    def with_serial(scan_roots=(), exclude=()):
        return [dataclasses.replace(v, serial="1A2B-3C4D") if v.root.rstrip("/\\").endswith("vol_650") else v
                for v in real(scan_roots, exclude)]

    monkeypatch.setattr(I, "list_volumes", with_serial)
    mgr, _ = run_ingest(env)
    assert ledger.card_id_for_serial("1A2B-3C4D")[0] == "650"
    # id.txt を消す（フォーマットではないのでシリアルは同じ）
    os.remove(os.path.join(root, "id.txt"))
    mgr2 = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=lambda m: None, armed=True)
    mgr2.rescan_now()
    assert not mgr2.jobs
    (key, (v, reason)), = mgr2.others.items()
    assert mgr2.suggested_ids[key] == "650" and "前回このカードは ID 650" in reason
    ok, _msg = mgr2.assign_card_id(key, mgr2.suggested_ids[key])
    assert ok
    mgr2.rescan_now()
    assert {j.card.card_id for j in mgr2.jobs.values()} == {"650"}
    mgr2.stop()


def test_cards_wait_until_start_is_confirmed(env):
    """起動直後は、挿したカードを勝手にコピーしない。「取り込みを開始」（arm）で始まり、以後は自動で始まる。"""
    from camtrapdesk.core import ingest as I
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "1", n_files=2)
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append)
    mgr.start()
    mgr.rescan_now()
    job = next(iter(mgr.jobs.values()))
    assert job.state == I.S_DETECTED and "取り込みを開始" in job.message
    time.sleep(0.5)
    assert job.state == I.S_DETECTED and not (env["dest"] / "N260710" / "1").exists()
    assert mgr.arm() == 1
    assert _wait_until(lambda: job.state == S_DONE), (job.state, job.message)
    make_card(env["cards"], "2", n_files=2)          # 開始後に挿したカードは自動で始まる
    mgr.rescan_now()
    job2 = [j for j in mgr.jobs.values() if j.card.card_id == "2"][0]
    assert _wait_until(lambda: job2.state == S_DONE), (job2.state, job2.message)
    mgr.disarm()
    make_card(env["cards"], "3", n_files=2)
    mgr.rescan_now()
    job3 = [j for j in mgr.jobs.values() if j.card.card_id == "3"][0]
    time.sleep(0.5)
    assert job3.state == I.S_DETECTED
    mgr.stop()


# ---------------------------------------------------------------- 保存済みの一括変換
def _batch_env_tree(env):
    """NAS に、manifest の無い古い取り込み（旧Rコード風）と、台帳にある取り込みを作る。"""
    import pytest
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    nas = env["nas"]
    old = nas / "N250101" / "BSN0001"
    old.mkdir(parents=True)
    assert make_avi(old / "IMG_0001.AVI") and make_avi(old / "IMG_0002.AVI")
    (old / "IMG_0003.MP4").write_bytes(b"m" * 3000)          # mp4 は触らない
    (old / "IMG_0009.mp4.part").write_bytes(b"p" * 10)        # 前回の途中（消す）
    (old / "@eaDir").mkdir()                                   # Synology のサムネイル（入らない）
    assert make_avi(old / "@eaDir" / "X.AVI")
    (nas / "#recycle").mkdir()
    assert make_avi(nas / "#recycle" / "Y.AVI")
    return old


def test_batch_convert_scan_and_run(env):
    """保存済みフォルダ以下の AVI を見つけて mp4 に変換し、元を消す。mp4・@eaDir・#recycle は触らない。"""
    from camtrapdesk.core import batch_convert as B
    s, ledger = env["settings"], env["ledger"]
    s.convert_encoder = "libx264"
    old = _batch_env_tree(env)
    avi_sizes = {n: os.path.getsize(old / n) for n in ("IMG_0001.AVI", "IMG_0002.AVI")}
    res = B.scan(str(env["nas"]), s, ledger)
    assert not res.issues and res.n_parts_removed == 1 and not (old / "IMG_0009.mp4.part").exists()
    assert [f.dir for f in res.folders] == [str(old)]
    assert res.n_files == 2 and res.total_bytes == sum(avi_sizes.values())
    fp = res.folders[0]
    assert not fp.has_manifest and fp.ingest_id is None and fp.n_other_media == 1
    assert [(t.name, t.out_name, t.action) for t in fp.targets] == [("IMG_0001.AVI", "IMG_0001.mp4", B.A_CONVERT),
                                                                     ("IMG_0002.AVI", "IMG_0002.mp4", B.A_CONVERT)]
    logs, status = [], []
    done = []
    bc = B.BatchConverter(lambda: s, ledger, on_status=status.append, on_log=logs.append, on_done=done.append)
    assert bc.start(res, parallel=2)
    bc.wait(120)
    assert not bc.running and done and done[0].n_done == 2 and done[0].n_failed == 0, logs
    assert sorted(os.listdir(old)) == ["@eaDir", "IMG_0001.mp4", "IMG_0002.mp4", "IMG_0003.MP4"]
    assert (old / "@eaDir" / "X.AVI").is_file() and (env["nas"] / "#recycle" / "Y.AVI").is_file()
    st = bc.snapshot()
    assert st.bytes_in_done == sum(avi_sizes.values()) and st.bytes_out_done == sum(
        os.path.getsize(old / n) for n in ("IMG_0001.mp4", "IMG_0002.mp4"))
    # 更新日時は元を引き継ぐ
    assert abs(os.path.getmtime(old / "IMG_0001.mp4") - fp.targets[0].mtime) < 2
    # もう一度走査すると対象なし
    res2 = B.scan(str(env["nas"]), s, ledger)
    assert res2.n_files == 0 and res2.folders == []
    assert any(e["kind"] == "batch_convert" for e in ledger.recent_events())


def test_batch_convert_updates_manifest_and_ledger(env):
    """台帳にある取り込み（NAS で検証済み）の NAS 側を変換すると、manifest と台帳が mp4 に合う。
    中継側の控えを変換しても台帳は触らず、manifest だけ合わせる。"""
    import pytest
    from camtrapdesk.core import batch_convert as B
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    s, ledger = env["settings"], env["ledger"]
    s.convert_encoder = "libx264"
    s.media_extensions = ["mp4", "avi"]
    root, _ = make_card(env["cards"], "264", n_files=1)
    media = os.path.join(root, "DCIM", "100_BTCF")
    assert make_avi(os.path.join(media, "IMG_0001.AVI"))
    run_ingest(env)
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_SYNCED
    nas_dir = env["nas"] / "N260710" / "264"
    staging_dir = env["dest"] / "N260710" / "264"
    old_hash = {f.name: f.hash for f in ledger.files_for(ing.id)}["IMG_0001.AVI"]

    # NAS 側（正本）
    res = B.scan(str(env["nas"]), s, ledger)
    fp = res.folders[0]
    assert fp.has_manifest and fp.ingest_id == ing.id and fp.ledger_authoritative and not fp.skipped_reason
    assert fp.targets[0].old_hash == old_hash
    bc = B.BatchConverter(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    bc.start(res, parallel=1)
    bc.wait(120)
    assert sorted(os.listdir(nas_dir)) == ["IMG_0000.MP4", "IMG_0001.mp4", "manifest.json"]
    recs = {f.name: f for f in ledger.files_for(ing.id)}
    r = recs["IMG_0001.mp4"]
    assert r.converted and r.src_name == "IMG_0001.AVI" and r.src_hash == old_hash and r.nas_ok
    assert r.size == os.path.getsize(nas_dir / "IMG_0001.mp4") and r.hash == hash_file(str(nas_dir / "IMG_0001.mp4"))
    assert ledger.get_ingest(ing.id).convert_done_at
    man = L.Ledger.read_manifest(str(nas_dir))
    mf = {f["name"]: f for f in man["files"]}
    assert mf["IMG_0001.mp4"]["src_name"] == "IMG_0001.AVI" and mf["IMG_0001.mp4"]["src_hash"] == old_hash
    assert mf["IMG_0001.mp4"]["hash"] == r.hash and man["batch_converted_at"] and man["total_bytes"] == sum(f["size"] for f in man["files"])

    # 中継側（控え）: manifest は合わせるが台帳は触らない
    res2 = B.scan(str(env["dest"]), s, ledger)
    fp2 = res2.folders[0]
    assert fp2.ingest_id == ing.id and not fp2.ledger_authoritative
    bc2 = B.BatchConverter(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    bc2.start(res2, parallel=1)
    bc2.wait(120)
    assert sorted(os.listdir(staging_dir)) == ["IMG_0000.MP4", "IMG_0001.mp4", "manifest.json"]
    man2 = L.Ledger.read_manifest(str(staging_dir))
    assert {f["name"] for f in man2["files"]} == {"IMG_0000.MP4", "IMG_0001.mp4"}
    assert {f.name: f for f in ledger.files_for(ing.id)}["IMG_0001.mp4"].hash == r.hash   # 台帳は NAS 側のまま

    # SD の削除判定は元の AVI の名前・サイズで通る
    vols = list_volumes(scan_roots=[str(env["cards"])])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4", "avi"], False, 1024)[0]
    v = check_card(card, ledger, s)
    assert v.ok, v.reasons


def test_batch_convert_skips_folders_in_flight_and_finishes_leftovers(env):
    """NAS 転送がまだの取り込みは対象外。変換後に元を消す前に止まった状態（AVI と同じ更新日時の mp4）は元を消すだけ。"""
    import pytest
    import shutil
    from camtrapdesk.core import batch_convert as B
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    s, ledger = env["settings"], env["ledger"]
    s.convert_encoder = "libx264"
    s.media_extensions = ["mp4", "avi"]
    root, _ = make_card(env["cards"], "264", n_files=1)
    media = os.path.join(root, "DCIM", "100_BTCF")
    assert make_avi(os.path.join(media, "IMG_0001.AVI"))
    run_ingest(env)                      # staged、NAS はまだ
    res = B.scan(str(env["dest"]), s, ledger)
    assert len(res.folders) == 1 and "NAS への転送" in res.folders[0].skipped_reason and res.n_files == 0

    # 途中で止まった形: 変換済み mp4（同じ更新日時）と元の AVI が両方ある
    d = env["nas"] / "N250101" / "BSN0002"
    d.mkdir(parents=True)
    assert make_avi(d / "A.AVI")
    mt = os.path.getmtime(d / "A.AVI")
    tmp = env["tmp"] / "tmpconv"
    tmp.mkdir()
    assert make_avi(tmp / "tmp.AVI")
    bc0 = B.BatchConverter(lambda: s, None, on_status=lambda m: None, on_log=lambda m: None)
    bc0.start(B.scan(str(tmp), s, None), parallel=1)
    bc0.wait(120)
    shutil.move(str(tmp / "tmp.mp4"), str(d / "A.mp4"))
    os.utime(d / "A.mp4", (mt, mt))
    # 別のカメラの IMG.MP4 と同じ番号の AVI → _h264 名に逃がす
    assert make_avi(d / "B.AVI")
    (d / "B.mp4").write_bytes(b"other" * 100)
    res2 = B.scan(str(d), s, None)
    t = {x.name: x for x in res2.folders[0].targets}
    assert t["A.AVI"].action == B.A_FINISH and t["A.AVI"].out_name == "A.mp4"
    assert t["B.AVI"].action == B.A_CONVERT and t["B.AVI"].out_name == "B_h264.mp4"
    logs = []
    bc = B.BatchConverter(lambda: s, None, on_status=lambda m: None, on_log=logs.append)
    bc.start(res2, parallel=1)
    bc.wait(120)
    assert sorted(os.listdir(d)) == ["A.mp4", "B.mp4", "B_h264.mp4"], logs
    assert bc.snapshot().n_finished_only == 1 and bc.snapshot().n_done == 2


def test_batch_convert_stop(env):
    """中止すると ffmpeg を止め、途中の .part は残さず、元ファイルはそのまま。"""
    import pytest
    from camtrapdesk.core import batch_convert as B
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    s = env["settings"]
    s.convert_encoder = "libx264"
    d = env["nas"] / "slow"
    d.mkdir()
    for i in range(4):
        assert make_avi(d / f"S{i}.AVI", seconds=6)
    res = B.scan(str(d), s, None)
    bc = B.BatchConverter(lambda: s, None, on_status=lambda m: None, on_log=lambda m: None)
    # 速いマシンでも確実に途中で止めるため、最初の 1 本が終わった時点で中止する
    done_once = threading.Event()
    orig = B.BatchConverter._convert_one

    def hooked(self, fp, t, ffmpeg):
        r = orig(self, fp, t, ffmpeg)
        if not done_once.is_set():
            done_once.set()
            self.stop()
        return r

    B.BatchConverter._convert_one = hooked
    try:
        bc.start(res, parallel=1)
        bc.wait(60)
    finally:
        B.BatchConverter._convert_one = orig
    st = bc.snapshot()
    assert st.stopped and 1 <= st.n_done < 4
    names = os.listdir(d)
    assert not any(n.endswith(".part") for n in names)
    assert sum(1 for n in names if n.endswith(".AVI")) + st.n_done == 4


def test_batch_convert_to_separate_folder_keeps_originals(env):
    """保存先を別フォルダにすると、相対パスを保って mp4 を書き、元の AVI は残り、manifest も台帳も触らない。"""
    from camtrapdesk.core import batch_convert as B
    s, ledger = env["settings"], env["ledger"]
    s.convert_encoder = "libx264"
    old = _batch_env_tree(env)
    out = env["tmp"] / "converted"
    # 保存先が同じ・中にある場合は断る
    assert B.scan(str(env["nas"]), s, ledger, out_root=str(env["nas"])).issues
    assert B.scan(str(env["nas"]), s, ledger, out_root=str(env["nas"] / "sub")).issues
    res = B.scan(str(env["nas"]), s, ledger, out_root=str(out))
    assert not res.issues and not res.in_place and res.n_files == 2
    fp = res.folders[0]
    assert fp.out_dir == str(out / os.path.relpath(str(old), str(env["nas"]))) and not fp.has_manifest and not fp.ledger_authoritative
    assert (old / "IMG_0009.mp4.part").exists()          # 置き換えではないので元側の .part も触らない
    logs, done = [], []
    bc = B.BatchConverter(lambda: s, ledger, on_status=lambda m: None, on_log=logs.append, on_done=done.append)
    assert bc.start(res, parallel=1)
    bc.wait(120)
    assert done and done[0].n_done == 2 and done[0].n_failed == 0, logs
    assert "残して" in done[0].message
    assert (old / "IMG_0001.AVI").is_file() and (old / "IMG_0002.AVI").is_file()      # 元はそのまま
    out_dir = out / os.path.relpath(str(old), str(env["nas"]))
    assert sorted(os.listdir(out_dir)) == ["IMG_0001.mp4", "IMG_0002.mp4"]
    assert abs(os.path.getmtime(out_dir / "IMG_0001.mp4") - fp.targets[0].mtime) < 2
    # もう一度走査すると、出力先に変換済みがあるので対象なし（元は消さない）
    res2 = B.scan(str(env["nas"]), s, ledger, out_root=str(out))
    assert res2.n_files == 0 and all(t.action == B.A_SKIP for f in res2.folders for t in f.targets)
    assert (old / "IMG_0001.AVI").is_file()


def test_broken_ffmpeg_binary_is_skipped(tmp_path, monkeypatch):
    """途中で切れたダウンロードなど、起動しない ffmpeg は候補から外す（WinError 193 で全体が止まらないように）。"""
    from camtrapdesk.core import ffmpeg as F
    F.forget_ffmpeg()
    bad = tmp_path / "ffmpeg"
    bad.write_bytes(b"\x00" * 2_000_000)
    bad.chmod(0o755)
    assert not F.ffmpeg_works(str(bad))
    assert "壊れています" in F.candidate_status(str(bad))
    real = F.find_ffmpeg()
    F.forget_ffmpeg()
    found = F.find_ffmpeg(str(bad))        # 設定で壊れたものを指しても、次の候補（本物）へ進む
    assert found != str(bad) and found == real
    F.forget_ffmpeg()


def test_probe_without_ffprobe(monkeypatch, tmp_path):
    """ffprobe が無い環境（imageio-ffmpeg 同梱の ffmpeg だけ）でも、長さ・コーデック・解像度が読める。"""
    import pytest
    from camtrapdesk.core import ffmpeg as F
    ff = F.find_ffmpeg()
    if not ff:
        pytest.skip("ffmpeg が無い")
    p = make_avi(tmp_path / "x.avi", seconds=1)
    assert p
    monkeypatch.setattr(F, "ffprobe_path", lambda ffmpeg: None)
    info = F.probe(ff, str(p))
    assert info and info.vcodec == "mjpeg" and (info.width, info.height) == (96, 64) and abs(info.duration - 1.0) < 0.3


# ----------------------------------------------------------------------------
# 壊れたファイル（スキャンと修復が出るカード）
# ----------------------------------------------------------------------------

def test_check_media_file_structural(tmp_path):
    """ffmpeg 無しでも分かる壊れ方: 0 バイト、途中で切れた AVI、終端の無い JPEG、moov の無い mp4。"""
    import struct
    from camtrapdesk.core.mediacheck import check_media_file
    (tmp_path / "zero.avi").write_bytes(b"")
    assert "0 バイト" in check_media_file(str(tmp_path / "zero.avi"))
    body = b"x" * 1000
    ok_avi = b"RIFF" + struct.pack("<I", len(body) + 4) + b"AVI " + body
    (tmp_path / "ok.avi").write_bytes(ok_avi)
    assert check_media_file(str(tmp_path / "ok.avi")) is None
    (tmp_path / "cut.avi").write_bytes(ok_avi[:600])                      # ヘッダのサイズより短い
    assert "途中で切れて" in check_media_file(str(tmp_path / "cut.avi"))
    (tmp_path / "unfinished.avi").write_bytes(b"RIFF" + struct.pack("<I", 0) + b"AVI " + body)
    assert "完了していません" in check_media_file(str(tmp_path / "unfinished.avi"))
    # 1 GB 超の AVI は最初の RIFF が実サイズより小さい（OpenDML）→ 正常
    (tmp_path / "odml.avi").write_bytes(b"RIFF" + struct.pack("<I", 100) + b"AVI " + body)
    assert check_media_file(str(tmp_path / "odml.avi")) is None
    (tmp_path / "ok.jpg").write_bytes(b"\xff\xd8" + b"j" * 100 + b"\xff\xd9")
    assert check_media_file(str(tmp_path / "ok.jpg")) is None
    (tmp_path / "cut.jpg").write_bytes(b"\xff\xd8" + b"j" * 100)
    assert "終端" in check_media_file(str(tmp_path / "cut.jpg"))
    ftyp = struct.pack(">I", 16) + b"ftyp" + b"isom" + struct.pack(">I", 0)
    moov = struct.pack(">I", 8) + b"moov"
    mdat = struct.pack(">I", 8 + 500) + b"mdat" + b"m" * 500
    (tmp_path / "ok.mp4").write_bytes(ftyp + moov + mdat)
    assert check_media_file(str(tmp_path / "ok.mp4")) is None
    (tmp_path / "nomoov.mp4").write_bytes(ftyp + mdat)
    assert "moov" in check_media_file(str(tmp_path / "nomoov.mp4"))
    (tmp_path / "cut.mp4").write_bytes((ftyp + moov + mdat)[:-100])
    assert "途中で切れて" in check_media_file(str(tmp_path / "cut.mp4"))


def _broken_env(env, action="delete"):
    """良い AVI 2 本と、壊れた 3 ファイル（途中で切れた AVI、0 バイト、終端の無い JPEG）を持つカード。"""
    import pytest
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    s = env["settings"]
    s.broken_check = True
    s.broken_action = action
    s.media_extensions = ["mp4", "avi", "jpg"]
    cards = env["cards"]
    root, _ = make_card(cards, "264", n_files=0)
    media = os.path.join(root, "DCIM", "100_BTCF")
    assert make_avi(os.path.join(media, "IMG_0001.AVI")) and make_avi(os.path.join(media, "IMG_0002.AVI"))
    good = open(os.path.join(media, "IMG_0001.AVI"), "rb").read()
    with open(os.path.join(media, "IMG_0003.AVI"), "wb") as f:
        f.write(good[: len(good) // 2])          # 途中で切れた
    open(os.path.join(media, "IMG_0004.AVI"), "wb").close()   # 0 バイト
    with open(os.path.join(media, "IMG_0005.JPG"), "wb") as f:
        f.write(b"\xff\xd8" + b"j" * 300)        # 終端マーカー無し
    return root, media


def test_broken_files_are_removed_after_copy_and_deleted_from_card(env):
    """壊れたファイルはいったん全部コピーしてから中継で見つけて消す。NAS には送らず、カードからは削除できる。"""
    s, ledger = env["settings"], env["ledger"]
    root, media = _broken_env(env)
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE and "壊れていた 3 ファイル" in job.message, (job.message, logs)
    ing = ledger.latest_ingest_for_card("264")
    d = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(d)) == ["IMG_0001.AVI", "IMG_0002.AVI", "manifest.json"]
    recs = {f.name: f for f in ledger.files_for(ing.id)}
    assert recs["IMG_0003.AVI"].broken and "途中で切れて" in recs["IMG_0003.AVI"].broken
    assert recs["IMG_0004.AVI"].broken and recs["IMG_0005.JPG"].broken
    assert not recs["IMG_0001.AVI"].broken and recs["IMG_0001.AVI"].staging_ok
    man = L.Ledger.read_manifest(str(d))
    assert man["n_files"] == 2 and {b["name"] for b in man["broken_files"]} == {"IMG_0003.AVI", "IMG_0004.AVI", "IMG_0005.JPG"}
    # NAS へは良いファイルだけ
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    assert ledger.latest_ingest_for_card("264").status == L.STATUS_SYNCED
    assert sorted(os.listdir(env["nas"] / "N260710" / "264")) == ["IMG_0001.AVI", "IMG_0002.AVI", "manifest.json"]
    # 削除判定: 壊れたファイルも消す（保存先には無いが）
    vols = list_volumes(scan_roots=[str(env["cards"])])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], s.media_extensions, False, 0)[0]
    v = check_card(card, ledger, s)
    assert v.ok, v.reasons
    assert sorted(v.deletable) == ["IMG_0001.AVI", "IMG_0002.AVI", "IMG_0003.AVI", "IMG_0004.AVI", "IMG_0005.JPG"]
    assert sorted(v.broken) == ["IMG_0003.AVI", "IMG_0004.AVI", "IMG_0005.JPG"]
    n, errs = delete_files(v, ledger, log=lambda m: None)
    assert n == 5 and not errs
    assert os.listdir(media) == []
    assert ledger.latest_ingest_for_card("264", include_cleared=True).cleared_at
    # 台帳を manifest から作り直しても壊れた記録が残る
    ledger2 = L.Ledger(str(env["tmp"] / "ledger2.sqlite"))
    assert ledger2.rebuild_from_manifests(str(env["nas"]), str(env["nas"])) == 1
    ing2 = ledger2.latest_ingest_for_card("264")
    assert sum(1 for f in ledger2.files_for(ing2.id) if f.broken) == 3


def test_broken_files_can_be_kept_in_subfolder(env):
    s, ledger = env["settings"], env["ledger"]
    _broken_env(env, action="keep")
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE and "_破損" in job.message, (job.message, logs)
    d = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(d)) == ["IMG_0001.AVI", "IMG_0002.AVI", "_破損", "manifest.json"]
    assert sorted(os.listdir(d / "_破損")) == ["IMG_0003.AVI", "IMG_0004.AVI", "IMG_0005.JPG"]
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    assert sorted(os.listdir(env["nas"] / "N260710" / "264")) == ["IMG_0001.AVI", "IMG_0002.AVI", "manifest.json"]


def test_broken_files_are_not_converted(env):
    """変換が有効でも、壊れた AVI は変換対象にならない（良いものだけ mp4 になる）。"""
    from camtrapdesk.core.convert import Converter
    s, ledger = env["settings"], env["ledger"]
    _broken_env(env)
    s.convert_enabled = True
    s.convert_encoder = "libx264"
    s.convert_parallel = 1
    mgr, logs = run_ingest(env)
    ing = ledger.latest_ingest_for_card("264")
    assert ing.convert_pending == 2, logs
    conv = Converter(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    for _ in range(5):
        conv._tick()
        if not ledger.pending_convert():
            break
    assert not ledger.pending_convert()
    d = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(d)) == ["IMG_0001.mp4", "IMG_0002.mp4", "manifest.json"]


# ----------------------------------------------------------------------------
# csv の野帳と、設置記録との照合
# ----------------------------------------------------------------------------

def test_csv_fieldnote(tmp_path):
    s = nakashima_settings()
    p = tmp_path / "note.csv"
    p.write_text("Season,Station,InstallationSD,RetrievalSD,Remarks\n260710,482,260,319,\n260710,592,74,318,ok\n",
                 encoding="cp932")
    fn = load_fieldnote(str(p), s)
    assert fn.sheets == [] and fn.sheet == ""
    assert [r.station for r in fn.rows] == ["482", "592"] and fn.rows[1].retrieval_sd == "318"
    assert fn.distinct("Season") == ["260710"]
    m = build_mapping(fn, [("Season", "260710")], s)
    assert {e.station_folder: e.card_id for e in m.entries} == {"BSN0482": "319", "BSN0592": "318"}
    # 回収SD の列が無い表でも、設置記録としてなら読める
    q = tmp_path / "install.csv"
    q.write_text("Station\tInstallationSD\n482\t319\n", encoding="utf-8-sig")
    fn2 = load_fieldnote(str(q), s, require_retrieval=False)
    assert fn2.rows[0].station == "482" and fn2.rows[0].get("InstallationSD") == "319"
    import pytest
    with pytest.raises(ValueError):
        load_fieldnote(str(q), s)


def test_installation_cross_check(tmp_path):
    """前の調査回の行（地点 → 設置SD）と、今回の行（地点 → 回収SD）を突き合わせ、合わない行に注意を付ける。"""
    from camtrapdesk.core.fieldnote import cross_check_installation
    s = nakashima_settings()
    fn_path = tmp_path / "fieldnote.xlsx"
    make_fieldnote(fn_path, [
        {"season": 260424, "station": 482, "inst": 319, "ret": 10},
        {"season": 260424, "station": 980, "inst": 555, "ret": 11},
        {"season": 260424, "station": 123, "inst": 900, "ret": 12},    # 今回の回収行が無い
        {"season": 260710, "station": 482, "inst": 260, "ret": 319},   # 一致
        {"season": 260710, "station": 980, "inst": 556, "ret": 11},    # 不一致（設置は 555）
        {"season": 260710, "station": 592, "inst": 74, "ret": 318},    # 設置記録に無い地点
    ])
    fn = load_fieldnote(str(fn_path), s)
    m = build_mapping(fn, [("Season", "260710")], s)
    res = cross_check_installation(m, fn, [("Season", "260424")], s)
    assert (res.n_install_rows, res.n_match, res.n_mismatch, res.n_no_record) == (3, 1, 1, 1)
    by = {e.station_folder: e for e in m.entries}
    assert not by["BSN0482"].warnings
    assert any("555" in w and "一致しません" in w for w in by["BSN0980"].warnings)
    assert any("設置記録にこの地点がありません" in w for w in by["BSN0592"].warnings)
    assert any("BSN0123" in x and "回収行がありません" in x for x in m.issues)
    assert any("一致しない行が 1 件" in x for x in m.issues)
    # 列や条件が無ければ照合せず理由を残す
    s2 = nakashima_settings(col_installation_sd="NoSuchColumn")
    m2 = build_mapping(fn, [("Season", "260710")], s2)
    res2 = cross_check_installation(m2, fn, [], s2)
    assert res2.n_checked == 0 and any("NoSuchColumn" in x for x in m2.issues)
    m3 = build_mapping(fn, [("Season", "260710")], s)
    res3 = cross_check_installation(m3, fn, [("Season", "999999")], s)
    assert res3.n_install_rows == 0 and any("条件に合う行がありません" in x for x in m3.issues)


# ----------------------------------------------------------------------------
# カード上で読めないファイル（「ファイルまたはディレクトリが壊れている」）と修復
# ----------------------------------------------------------------------------

def test_unreadable_file_on_card_is_skipped_not_fatal(env, monkeypatch):
    """コピー中にカード側の読み取りエラー（EIO）が出ても、そのファイルだけ壊れた扱いにしてカードの残りは取り込む。"""
    import errno
    from camtrapdesk.core import ingest as ING
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "264", n_files=3)
    real = ING.copy_with_hash

    def flaky(src, dst, progress=None):
        if src.endswith("IMG_0001.MP4"):
            raise OSError(errno.EIO, "ファイルまたはディレクトリが壊れているため、読み取ることができません")
        return real(src, dst, progress)

    monkeypatch.setattr(ING, "copy_with_hash", flaky)
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE and job.n_broken == 1, (job.message, logs)
    assert "修復をおすすめ" in job.message
    ing = ledger.latest_ingest_for_card("264")
    recs = {f.name: f for f in ledger.files_for(ing.id)}
    assert recs["IMG_0001.MP4"].broken and "読めません" in recs["IMG_0001.MP4"].broken
    assert recs["IMG_0000.MP4"].staging_ok and recs["IMG_0002.MP4"].staging_ok
    d = env["dest"] / "N260710" / "264"
    assert sorted(os.listdir(d)) == ["IMG_0000.MP4", "IMG_0002.MP4", "manifest.json"]
    assert not list(d.glob("*.part"))


def test_unreadable_file_at_detection_is_recorded(env, monkeypatch):
    """検出の時点で stat できないファイルは、カード全体のエラーにせず、壊れたファイルとして記録だけ残す。"""
    import errno
    from camtrapdesk.core import cards as C
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "264", n_files=3)
    real_stat = os.stat

    def bad_stat(p, *a, **k):
        if str(p).endswith("IMG_0002.MP4"):
            raise OSError(errno.EIO, "ファイルまたはディレクトリが壊れているため、読み取ることができません")
        return real_stat(p, *a, **k)

    monkeypatch.setattr(C.os, "stat", bad_stat)
    vols = list_volumes(scan_roots=[str(env["cards"])])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)[0]
    assert not card.problems and any("読めない" in w for w in card.warnings)
    assert [f.name for f in card.files if f.error] == ["IMG_0002.MP4"]
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_DONE and job.n_broken == 1, (job.message, logs)
    ing = ledger.latest_ingest_for_card("264")
    assert {f.name: bool(f.broken) for f in ledger.files_for(ing.id)} == {"IMG_0000.MP4": False, "IMG_0001.MP4": False, "IMG_0002.MP4": True}
    assert sorted(os.listdir(env["dest"] / "N260710" / "264")) == ["IMG_0000.MP4", "IMG_0001.MP4", "manifest.json"]
    # 削除判定では、読めなかったファイルも削除対象（保存先には無い）
    monkeypatch.undo()
    syncer = NasSyncer(lambda: s, ledger, on_status=lambda m: None, on_log=lambda m: None)
    while ledger.pending_nas():
        syncer._tick()
    vols = list_volumes(scan_roots=[str(env["cards"])])
    card = detect_cards(vols, "id.txt", ["DCIM/100_BTCF"], ["mp4"], False, 1024)[0]
    v = check_card(card, ledger, s)
    assert v.ok and sorted(v.deletable) == ["IMG_0000.MP4", "IMG_0001.MP4", "IMG_0002.MP4"] and v.broken == ["IMG_0002.MP4"]


def test_repair_helpers(tmp_path):
    from camtrapdesk.core.repair import build_repair_script, chkdsk_target, is_corruption_error, summarize_repair_log
    from camtrapdesk.core.volumes import Volume
    import errno
    v1 = Volume(key="\\\\?\\Volume{abc}\\", root="F:\\", display="F:", label="SD1", kind="removable")
    v2 = Volume(key="\\\\?\\Volume{def}\\", root="\\\\?\\Volume{def}\\", display="(レター無し)", label="SD2", kind="removable")
    assert chkdsk_target(v1) == "F:" and chkdsk_target(v2) == "\\\\?\\Volume{def}\\"
    script = build_repair_script(["F:", "\\\\?\\Volume{def}\\"], "C:\\x\\repair.log", "C:\\x\\repair.done")
    assert 'chkdsk "F:" /f /x' in script and 'chkdsk "\\\\?\\Volume{def}\\" /f /x' in script
    assert script.count("=== chkdsk") == 2 and "repair.done" in script and "\r\n" in script
    assert is_corruption_error(OSError(errno.EIO, "x"))
    e = OSError(22, "x"); e.winerror = 1392
    assert is_corruption_error(e)
    assert not is_corruption_error(OSError(errno.ENOENT, "x")) and not is_corruption_error(ValueError())
    log = "camtrapdesk repair\n\n=== chkdsk F: ===\nファイル システムの種類は FAT32 です。\nWindows はファイル システムを検査し、問題を修復しました。\n[exit 0]\n"
    s = summarize_repair_log(log)
    assert "F:: Windows はファイル システムを検査し、問題を修復しました。" in s and "F:: [exit 0]" in s


# ----------------------------------------------------------------------------
# 途中で止める
# ----------------------------------------------------------------------------

def test_stop_ingest_midway_and_resume(env):
    """コピー中に止めると「停止」になり、書きかけは消え、やり直すと検証済みのファイルを飛ばして続きから完了する。"""
    from camtrapdesk.core.ingest import S_STOPPED
    s, ledger = env["settings"], env["ledger"]
    s.concurrency = 1
    make_card(env["cards"], "264", n_files=6, size=3_000_000)
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append, armed=True)
    hold = threading.Event()
    stopped = threading.Event()
    real = mgr._ingest_card

    # 2 ファイル目のコピーが終わった時点で止める（on_update の中から）
    def on_update(job):
        if job.done_files >= 2 and not stopped.is_set():
            stopped.set()
            mgr.cancel_jobs()
    mgr.on_update = on_update
    mgr.start()
    t0 = time.time()
    while time.time() - t0 < 30:
        mgr.rescan_now()
        jobs = list(mgr.jobs.values())
        if jobs and jobs[0].state == S_STOPPED:
            break
        time.sleep(0.1)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_STOPPED and "続きから" in job.message, (job.state, job.message, logs)
    d = env["dest"] / "N260710" / "264"
    assert not list(d.glob("*.part"))
    done_before = sorted(p.name for p in d.iterdir() if p.suffix == ".MP4")
    assert 1 <= len(done_before) < 6
    ing = ledger.latest_ingest_for_card("264")
    assert ing.status == L.STATUS_ERROR and ing.message == "停止"
    # やり直す → 続きから
    mgr.enqueue(job.key)
    t0 = time.time()
    while time.time() - t0 < 30 and job.state != S_DONE:
        time.sleep(0.1)
    mgr.stop()
    assert job.state == S_DONE, (job.state, job.message, logs)
    assert any("再開" in m for m in logs)
    ing2 = ledger.latest_ingest_for_card("264")
    assert ing2.status == L.STATUS_STAGED and ing2.id != ing.id
    assert sorted(p.name for p in d.iterdir() if p.suffix == ".MP4") == [f"IMG_{i:04d}.MP4" for i in range(6)]


def test_cancel_queued_job_before_it_starts(env):
    from camtrapdesk.core.ingest import S_QUEUED, S_STOPPED
    s, ledger = env["settings"], env["ledger"]
    make_card(env["cards"], "264", n_files=2)
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=lambda m: None, armed=False)
    mgr.rescan_now()      # start() していないのでプールは無く、enqueue しても走らない
    key = next(iter(mgr.jobs))
    mgr.enqueue(key)
    assert mgr.jobs[key].state == S_QUEUED
    assert mgr.cancel_jobs([key]) == 1
    assert mgr.jobs[key].state == S_STOPPED and mgr.jobs[key].cancel
    mgr.start()
    mgr._run_job(key)     # 始まる前に cancel を見て何もしない
    assert mgr.jobs[key].state == S_STOPPED and ledger.latest_ingest_for_card("264") is None
    mgr.stop()
