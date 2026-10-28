"""GUI のスモークテスト（オフスクリーン）。取り込み→NAS→名前変更→削除を画面経由で一巡させる。"""

import os
import time

import pytest

pytest.importorskip("PySide6")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

from camtrapdesk.config import save_settings  # noqa: E402
from camtrapdesk.core import ingest as I  # noqa: E402
from camtrapdesk.core import ledger as L  # noqa: E402

from conftest import make_card, nakashima_settings  # noqa: E402
from test_core import make_fieldnote  # noqa: E402


def _pump(app, seconds):
    t0 = time.time()
    while time.time() - t0 < seconds:
        app.processEvents()
        time.sleep(0.02)


def test_gui_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("CAMTRAPDESK_HOME", str(tmp_path / "home"))
    cards = tmp_path / "cards"; cards.mkdir()
    dest = tmp_path / "staging"; dest.mkdir()
    nas = tmp_path / "nas"; nas.mkdir()
    fn_path = tmp_path / "fieldnote.xlsx"
    make_fieldnote(fn_path, [
        {"season": 260710, "station": 592, "inst": 74, "ret": 318},
        {"season": 260710, "station": 735, "inst": 560, "ret": 295},
    ])
    make_card(cards, "318", n_files=2)
    make_card(cards, "295", n_files=2)

    s = nakashima_settings(dest_root=str(dest), nas_root=str(nas), season_folder="N260710", scan_roots=[str(cards)],
                           fieldnote_path=str(fn_path), concurrency=2, poll_interval_sec=0.2)
    save_settings(s)

    app = QApplication.instance() or QApplication([])
    from camtrapdesk.ui.context import AppContext
    from camtrapdesk.ui.main_window import MainWindow
    from camtrapdesk.ui.pages.clear_page import ClearPage
    from camtrapdesk.ui.pages.id_page import IdPage
    from camtrapdesk.ui.pages.ingest_page import IngestPage
    from camtrapdesk.ui.pages.ledger_page import LedgerPage
    from camtrapdesk.ui.pages.rename_page import RenamePage
    from camtrapdesk.ui.pages.settings_page import SettingsPage
    from camtrapdesk.ui.pages.slot_page import SlotPage
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    ctx.start_background()

    # 起動直後は開始待ち。「取り込みを開始」（確認ダイアログで Yes）で始まる
    ingest_page = win.page(IngestPage)
    for _ in range(50):
        _pump(app, 0.2)
        if len(ctx.ingest.jobs) == 2:
            break
    assert all(j.state == I.S_DETECTED for j in ctx.ingest.jobs.values())
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    ingest_page._arm_clicked()
    assert ctx.ingest.armed and ingest_page.arm_btn.text() == "自動開始を止める"

    for _ in range(100):
        _pump(app, 0.2)
        jobs = list(ctx.ingest.jobs.values())
        if len(jobs) == 2 and all(j.state == I.S_DONE for j in jobs):
            break
    assert all(j.state == I.S_DONE for j in ctx.ingest.jobs.values())
    _pump(app, 0.8)   # 表の更新タイマー（0.5秒）を待つ
    assert ingest_page.table.rowCount() == 2
    # NAS 転送が終わるまで
    for _ in range(100):
        _pump(app, 0.2)
        if not ctx.ledger.pending_nas():
            break
    assert all(i.status == L.STATUS_SYNCED for i in ctx.ledger.ingests())
    win.grab().save(str(tmp_path / "ingest.png"))

    # 名前変更
    rename_page = win.page(RenamePage)
    win.show_page(RenamePage)
    rename_page.load_fieldnote()
    # 初期条件: 変更するフォルダ N260710 に合う Season = 260710 が自動で入る
    assert rename_page.conditions() == [("Season", "260710")]
    rename_page.add_condition("Type", "N")
    assert rename_page.conditions() == [("Season", "260710"), ("Type", "N")]
    assert "条件に合う行: 2 / 2" in rename_page.match_label.text()
    rename_page.dry_run()
    assert ctx.settings.rename_filters == [["Season", "260710"], ["Type", "N"]]
    assert rename_page.plan and rename_page.plan.count("rename") == 2
    rename_page.apply()
    # 中継と NAS の両方が地点名になる（中継のコピーは既定で残す）
    assert (dest / "N260710" / "BSN0592").is_dir() and (nas / "N260710" / "BSN0592").is_dir()
    assert (dest / "N260710" / "BSN0735").is_dir() and (nas / "N260710" / "BSN0735").is_dir()
    win.grab().save(str(tmp_path / "rename.png"))

    # 削除
    clear_page = win.page(ClearPage)
    win.show_page(ClearPage)
    clear_page.check()
    assert all(v.ok for v in clear_page.verdicts)
    clear_page.delete()
    assert os.listdir(cards / "vol_318" / "DCIM" / "100_BTCF") == []
    win.grab().save(str(tmp_path / "clear.png"))

    # 残りの画面も開く（ID付け・スロット名は挿さっているカードを一覧にする）
    for cls in (LedgerPage, SettingsPage, IdPage, SlotPage):
        win.show_page(cls)
        _pump(app, 0.2)
    win.page(IdPage).scan()
    assert win.page(IdPage).table.rowCount() == 2
    win.page(SlotPage).scan()
    assert win.page(SlotPage).table.rowCount() == 2
    win.close()
    _pump(app, 0.2)


def test_gui_batch_convert_page(tmp_path, monkeypatch):
    """「動画を mp4 に変換」画面: フォルダを調べて一覧にし、確認ダイアログの Yes で変換し、終わると表が更新される。"""
    from camtrapdesk.core.ffmpeg import find_ffmpeg
    from test_core import make_avi
    if not find_ffmpeg():
        pytest.skip("ffmpeg が無い")
    monkeypatch.setenv("CAMTRAPDESK_HOME", str(tmp_path / "home"))
    nas = tmp_path / "nas"
    d = nas / "N250101" / "BSN0007"
    d.mkdir(parents=True)
    assert make_avi(d / "IMG_0001.AVI") and make_avi(d / "IMG_0002.AVI")
    (d / "IMG_0003.MP4").write_bytes(b"m" * 2000)
    s = nakashima_settings(dest_root=str(tmp_path / "staging"), nas_root=str(nas), convert_encoder="libx264",
                           scan_roots=[str(tmp_path / "cards")], poll_interval_sec=0.2)
    (tmp_path / "staging").mkdir(); (tmp_path / "cards").mkdir()
    save_settings(s)

    app = QApplication.instance() or QApplication([])
    from camtrapdesk.ui.context import AppContext
    from camtrapdesk.ui.main_window import MainWindow
    from camtrapdesk.ui.pages.batch_convert_page import BatchConvertPage
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    page = win.page(BatchConvertPage)
    win.show_page(BatchConvertPage)
    assert page.path_row.text() == str(nas)     # 既定は NAS の保存先
    page.scan()
    for _ in range(100):
        _pump(app, 0.1)
        if page.plan is not None:
            break
    assert page.plan is not None and page.plan.n_files == 2
    assert page.table.rowCount() == 1 and page.table.item(0, 1).text() == "2"
    assert page.start_btn.isEnabled()
    win.grab().save(str(tmp_path / "batch_scan.png"))

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    page.start()
    assert ctx.batch.running or ctx.batch.snapshot().finished_at
    for _ in range(300):
        _pump(app, 0.2)
        if not ctx.batch.running:
            break
    _pump(app, 1.2)   # 完了シグナルと表の更新
    assert not ctx.batch.running
    assert sorted(os.listdir(d)) == ["IMG_0001.mp4", "IMG_0002.mp4", "IMG_0003.MP4"]
    assert page.table.item(0, 3).text() == "2" and not page.start_btn.isEnabled()
    assert "完了" in page.status_label.text()
    win.grab().save(str(tmp_path / "batch_done.png"))
    win.close()
    _pump(app, 0.2)
