import os
import random

import pytest

from camtrapdesk.config import Settings, TypeRule
from camtrapdesk.core.ledger import Ledger

# 中島研の野帳の整形ルール（Type N → N260710 / BSN0482）。既定は「そのまま」なので、テストでは明示する
NAKASHIMA_RULES = {"N": TypeRule("N", "BSN", 4)}


def nakashima_settings(**kw) -> Settings:
    return Settings(type_rules=dict(NAKASHIMA_RULES), default_type="N", **kw)


def make_card(cards_dir, card_id, n_files=3, size=200_000, subdir="DCIM/100_BTCF", seed=0):
    root = os.path.join(cards_dir, f"vol_{card_id}")
    media = os.path.join(root, *subdir.split("/"))
    os.makedirs(media, exist_ok=True)
    with open(os.path.join(root, "id.txt"), "w", encoding="utf-8") as f:
        f.write(f"{card_id}\n")
    rnd = random.Random(seed + int(card_id))
    names = []
    for i in range(n_files):
        name = f"IMG_{i:04d}.MP4"
        with open(os.path.join(media, name), "wb") as f:
            f.write(rnd.randbytes(size))
        names.append(name)
    return root, names


@pytest.fixture
def env(tmp_path):
    cards = tmp_path / "cards"
    dest = tmp_path / "staging"
    nas = tmp_path / "nas"
    cards.mkdir(); dest.mkdir(); nas.mkdir()
    s = nakashima_settings(dest_root=str(dest), nas_root=str(nas), season_folder="N260710",
                           scan_roots=[str(cards)], concurrency=2, poll_interval_sec=0.2)
    ledger = Ledger(str(tmp_path / "ledger.sqlite"))
    return {"tmp": tmp_path, "cards": cards, "dest": dest, "nas": nas, "settings": s, "ledger": ledger}
