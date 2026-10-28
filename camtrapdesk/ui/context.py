"""アプリ全体で共有する状態: 設定、台帳、取り込み・NAS転送のバックグラウンド処理、Qt シグナル。"""

from __future__ import annotations

import os

from PySide6.QtCore import QObject, Signal

from ..config import Settings, config_dir, load_settings, save_settings
from ..core.convert import Converter
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
    convert_status = Signal(str)
    settings_changed = Signal()

    def __init__(self):
        super().__init__()
        self.settings: Settings = load_settings()
        self.ledger = Ledger(os.path.join(config_dir(), "ledger.sqlite"))
        self._apply_match_mode()
        self.ingest = IngestManager(self.get_settings, self.ledger,
                                    on_update=self.job_updated.emit,
                                    on_log=self.log_message.emit,
                                    on_batch_done=self.batch_done.emit,
                                    reader_group=self.reader_group)
        self.nas = NasSyncer(self.get_settings, self.ledger,
                             on_status=self.nas_status.emit,
                             on_log=self.log_message.emit,
                             on_progress=self.nas_progress.emit,
                             is_ingest_busy=lambda: self.ingest.active_count() > 0)
        self.converter = Converter(self.get_settings, self.ledger,
                                   on_status=self.convert_status.emit,
                                   on_log=self.log_message.emit,
                                   on_ingest_done=lambda _iid: self.nas.wake())
        # 取り込みが1枚終わるたびに変換係と NAS 転送係を起こす（待ち時間なしで始める）
        self.job_updated.connect(self._maybe_wake_nas)

    def _maybe_wake_nas(self, job: CardJob) -> None:
        from ..core.ingest import S_DONE
        if job.state == S_DONE:
            self.converter.wake()
            self.nas.wake()

    def get_settings(self) -> Settings:
        return self.settings

    def slot_label(self, volume) -> str:
        """スロットに名前が付いていれば「名前 (K:)」、無ければドライブ表示だけ。"""
        name = self.settings.slot_names.get(volume.key)
        return f"{name} ({volume.display})" if name else volume.display

    def reader_group(self, volume) -> str:
        """スロット名「U1-3-B」の最後の区切りより前（U1-3）をリーダーの識別として使う。名前が無ければ空。"""
        import re
        name = self.settings.slot_names.get(volume.key, "")
        if not name:
            return ""
        parts = re.split(r"[-_/ ]", name)
        return "-".join(parts[:-1]) if len(parts) > 1 else name

    def slot_sort_key(self, volume) -> tuple:
        import re
        name = self.settings.slot_names.get(volume.key, "")
        parts = [(0, int(x)) if x.isdigit() else (1, x.lower()) for x in re.split(r"(\d+)", name) if x]
        return (0 if name else 1, parts, volume.display)

    def save(self) -> None:
        save_settings(self.settings)
        self._apply_match_mode()
        self.converter.encoder = None   # エンコーダの指定が変わったかもしれないので選び直す
        self.converter.wake()
        self.settings_changed.emit()

    def _apply_match_mode(self) -> None:
        from ..core import cards
        if cards.MATCH_BY_TAIL_NUMBER != self.settings.card_id_match_tail_number:
            cards.MATCH_BY_TAIL_NUMBER = self.settings.card_id_match_tail_number
        self.ledger.renormalize_card_ids()

    def start_background(self) -> None:
        self.ingest.start()
        self.converter.start()
        self.nas.start()

    def stop_background(self) -> None:
        self.ingest.stop()
        self.converter.stop()
        self.nas.stop()
