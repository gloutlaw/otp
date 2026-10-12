import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import replica.telegram as telegram  # noqa: E402
from replica.store import Store  # noqa: E402


@pytest.fixture(autouse=True)
def bot_store(tmp_path, monkeypatch):
    store = Store(tmp_path / "replica.db")
    monkeypatch.setattr(telegram, "_store", store)
    monkeypatch.setattr(telegram, "_owner_id", 1)
    yield store
    store.close()
