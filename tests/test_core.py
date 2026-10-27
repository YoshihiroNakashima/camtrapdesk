import os
import time

from openpyxl import Workbook

from camtrapdesk.config import Settings, TypeRule
from camtrapdesk.core import ledger as L
from camtrapdesk.core.cards import detect_cards, normalize_card_id
from camtrapdesk.core.clear import check_card, delete_files
from camtrapdesk.core.fieldnote import build_mapping, format_station, load_fieldnote
from camtrapdesk.core.hashing import hash_file
from camtrapdesk.core.ingest import IngestManager, S_DONE, S_ERROR, S_SKIPPED
from camtrapdesk.core.nas_sync import NasSyncer
from camtrapdesk.core.rename import A_BLOCKED, A_RENAME, A_UNMAPPED, apply_plan, build_plan
from camtrapdesk.core.volumes import list_volumes

from conftest import make_card


def run_ingest(env, timeout=30):
    s, ledger = env["settings"], env["ledger"]
    logs = []
    mgr = IngestManager(lambda: s, ledger, on_update=lambda j: None, on_log=logs.append)
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


def test_ingest_rejects_bad_card(env):
    cards = env["cards"]
    root, _ = make_card(cards, "300", n_files=2)
    # 異常サイズのファイル
    with open(os.path.join(root, "DCIM", "100_BTCF", "BAD.MP4"), "wb") as f:
        f.write(b"x" * 10)
    mgr, logs = run_ingest(env)
    job = next(iter(mgr.jobs.values()))
    assert job.state == S_ERROR and "異常サイズ" in job.message


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
    make_card(cards, "264", n_files=1)
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
    rule = TypeRule("N", "BSN", 4)
    assert format_station("482", rule) == "BSN0482"
    assert format_station("BSN730", rule) == "BSN0730"
    assert format_station("_045", rule) == "BSN0045"
    assert format_station("MUC0727", rule) == "MUC0727"


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
    assert fn.types()[0] == "N"
    assert fn.seasons("N") == ["260710", "260424"]
    m = build_mapping(fn, "N", "260710", s)
    by = {e.station_folder: e for e in m.entries}
    assert by["BSN0592"].card_id == "318" and not by["BSN0592"].errors
    assert by["BSN0482"].errors and by["BSN0980"].errors        # 重複は両方保留
    assert any("前回設置SD(555" in w for w in by["BSN0980"].warnings)  # 555 vs 319
    assert m.issues

    # フォルダを用意: 318, 295 は取り込み済み。777 は別Type。
    cards = env["cards"]
    make_card(cards, "318", n_files=1)
    make_card(cards, "295", n_files=1)
    make_card(cards, "319", n_files=1)
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
    s = Settings()
    fn = load_fieldnote(path, s)
    assert "N" in fn.types()
    m = build_mapping(fn, "N", "260710", s)
    assert m.season_folder == "N260710"
    assert len(m.entries) == 148
    dup = [e for e in m.entries if e.errors]
    assert len(dup) == 20   # 10組の重複
    ok = [e for e in m.entries if not e.errors]
    assert all(e.station_folder.startswith("BSN") and len(e.station_folder) == 7 for e in ok)
