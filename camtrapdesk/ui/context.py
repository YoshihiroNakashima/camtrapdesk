"""アプリ全体で共有する状態: 設定、台帳、取り込み・NAS転送のバックグラウンド処理、Qt シグナル。"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal

from ..config import Settings, config_dir, load_settings, save_settings
from ..core.ingest import CardJob, IngestManager
from ..core.ledger import Ledger
from ..core.nas_sync import NasSyncer


class AppContext(QObject):
    # バックグラウンドスレッドから発火される（Qt がメインスレッドへキューする）
    job_updated = Signal(object)        # CardJob
    log_message = Signal(str)
    batch_done = Signal(int)
    nas_status = Signal(str)
    nas_progress = Signal(str, float)
    settings_changed = Signal()

    def __init__(self):
        super().__init__()
        self.settings: Settings = load_settings()
        self.ledger = Ledger(os.path.join(config_dir(), "ledger.sqlite"))
        self._apply_match_mode()
        self.ingest = IngestManager(self.get_settings, self.ledger,
                                    on_update=self.job_updated.emit,
                                    on_log=self.log_message.emit,
                                    on_batch_done=self.batch_done.emit)
        self.nas = NasSyncer(self.get_settings, self.ledger,
                             on_status=self.nas_status.emit,
                             on_log=self.log_message.emit,
                             on_progress=self.nas_progress.emit)
        # 取り込みが1枚終わるたびに NAS 転送係を起こす（待ち時間なしで転送を始める）
        self.job_updated.connect(self._maybe_wake_nas)

    def _maybe_wake_nas(self, job: CardJob) -> None:
        from ..core.ingest import S_DONE
        if job.state == S_DONE:
            self.nas.wake()

    def get_settings(self) -> Settings:
        return self.settings

    def save(self) -> None:
        save_settings(self.settings)
        self._apply_match_mode()
        self.settings_changed.emit()

    def _apply_match_mode(self) -> None:
        from ..core import cards
        if cards.MATCH_BY_TAIL_NUMBER != self.settings.card_id_match_tail_number:
            cards.MATCH_BY_TAIL_NUMBER = self.settings.card_id_match_tail_number
        self.ledger.renormalize_card_ids()

    def start_background(self) -> None:
        self.ingest.start()
        self.nas.start()

    def stop_background(self) -> None:
        self.ingest.stop()
        self.nas.stop()
