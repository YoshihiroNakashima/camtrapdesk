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

from conftest import make_card  # noqa: E402
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

    from camtrapdesk.config import Settings
    s = Settings(dest_root=str(dest), nas_root=str(nas), season_folder="N260710", scan_roots=[str(cards)],
                 fieldnote_path=str(fn_path), concurrency=2, poll_interval_sec=0.2)
    save_settings(s)

    app = QApplication.instance() or QApplication([])
    from camtrapdesk.ui.context import AppContext
    from camtrapdesk.ui.main_window import MainWindow
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    ctx.start_background()

    # 取り込みが終わるまで
    ingest_page = win.stack.widget(1)
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
    rename_page = win.stack.widget(2)
    win.nav.setCurrentRow(2)
    rename_page.load_fieldnote()
    assert rename_page.type_combo.currentText() == "N"
    assert rename_page.season_combo.currentText() == "260710"
    rename_page.dry_run()
    assert rename_page.plan and rename_page.plan.count("rename") == 2
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    rename_page.apply()
    assert (dest / "N260710" / "BSN0592").is_dir() and (nas / "N260710" / "BSN0735").is_dir()
    win.grab().save(str(tmp_path / "rename.png"))

    # 削除
    clear_page = win.stack.widget(3)
    win.nav.setCurrentRow(3)
    clear_page.check()
    assert all(v.ok for v in clear_page.verdicts)
    clear_page.delete()
    assert os.listdir(cards / "vol_318" / "DCIM" / "100_BTCF") == []
    win.grab().save(str(tmp_path / "clear.png"))

    # 台帳・設定画面も開く
    win.nav.setCurrentRow(5)
    _pump(app, 0.2)
    win.nav.setCurrentRow(win.nav.count() - 1)
    _pump(app, 0.2)
    win.close()
    _pump(app, 0.2)
