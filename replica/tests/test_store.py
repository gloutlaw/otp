import sqlite3
import time
from datetime import datetime

import pytest

from replica.quota import LOCAL_TZ
from replica.store import DEFAULT_PLAN, Store


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=LOCAL_TZ)


def ts(day: int, hour: int = 12) -> int:
    return int(at(day, hour).timestamp())


def test_new_user_starts_on_free_with_free_and_trial_buckets(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    assert store.user_status(7) == "active"
    sources = sorted(row["source"] for row in store.active_entitlements(7, None, ts(14)))
    assert sources == ["free", "trial"]
    assert DEFAULT_PLAN == "free"
    store.close()


def test_ensure_user_is_idempotent(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    store.ensure_user(7, ts(15))
    assert len(store.active_entitlements(7, None, ts(15))) == 2
    store.close()


def test_reservation_blocks_a_second_key_on_the_same_bucket(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    bucket = next(row for row in store.active_entitlements(7, "cheap", ts(14)))
    store.usage_reserve(user_id=7, kind="cheap", key="k1", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14))
    assert store.usage_count(bucket["id"], ts(14, 0)) == 1
    assert store.usage_by_key("k1")["status"] == "reserved"
    store.close()


def test_release_frees_the_key_for_reuse(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    bucket = store.active_entitlements(7, "cheap", ts(14))[0]
    store.usage_reserve(user_id=7, kind="cheap", key="k1", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14))
    store.usage_release("k1", "failed")
    assert store.usage_count(bucket["id"], ts(14, 0)) == 0
    assert store.usage_by_key("k1") is None
    store.usage_reserve(user_id=7, kind="cheap", key="k1", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14))
    store.close()


def test_stale_reservations_are_released(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    bucket = store.active_entitlements(7, "cheap", ts(14))[0]
    store.usage_reserve(user_id=7, kind="cheap", key="old", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14, 8))
    assert store.usage_release_stale(ts(14, 9)) == 1
    assert store.usage_by_key("old") is None
    store.close()


def test_usage_totals_count_only_committed_rows(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.ensure_user(7, ts(14))
    bucket = store.active_entitlements(7, "cheap", ts(14))[0]
    store.usage_reserve(user_id=7, kind="cheap", key="a", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14))
    store.usage_commit("a", cost=0.0021, prompt=300, completion=120, reasoning=0, model_id="m",
                       finish_reason="stop", latency_ms=10, generation_id="g")
    store.usage_reserve(user_id=7, kind="cheap", key="b", bucket_id=bucket["id"], window_start=ts(14, 0), now=ts(14))
    count, cost, prompt, completion = store.usage_totals(7)
    assert (count, prompt, completion) == (1, 300, 120)
    assert cost == pytest.approx(0.0021)
    store.close()


def test_one_active_subscription_per_user(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.sub_create(user_id=7, plan="start", payload="inv:a", first_charge="c1", start=ts(1), end=ts(31), now=ts(1))
    with pytest.raises(sqlite3.IntegrityError):
        store.sub_create(user_id=7, plan="pro", payload="inv:b", first_charge="c2", start=ts(1), end=ts(31), now=ts(1))
    store.sub_update(store.sub_active(7)["id"], ts(2), status="superseded")
    store.sub_create(user_id=7, plan="pro", payload="inv:b", first_charge="c2", start=ts(2), end=ts(31), now=ts(2))
    assert store.sub_active(7)["plan"] == "pro"
    store.close()


def test_payment_charge_id_is_unique(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    args = dict(charge_id="c1", user_id=7, invoice_id=None, product="start", stars=300, recurring=False,
                source="update", status="applied", subscription_id=None, raw_json="{}", now=ts(1))
    assert store.payment_add(**args) is True
    assert store.payment_add(**args) is False
    store.close()


def test_subscription_event_is_unique_per_source_and_ref(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    assert store.event_add(None, "renewed", "update", "c1", "{}", ts(1)) is True
    assert store.event_add(None, "renewed", "update", "c1", "{}", ts(1)) is False
    store.close()


def test_turns_are_read_back_in_order(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.add_turn(7, "user", "первый", ts(13))
    store.add_turn(7, "assistant", "ответ", ts(13))
    store.add_turn(7, "user", "второй", ts(13))
    assert store.recent_turns(7, 12) == [
        {"role": "user", "content": "первый"},
        {"role": "assistant", "content": "ответ"},
        {"role": "user", "content": "второй"},
    ]
    assert store.recent_questions(7, 10) == ["второй", "первый"]
    store.close()


def test_recent_turns_survive_reopen_and_respect_limit(tmp_path) -> None:
    path = tmp_path / "replica.db"
    store = Store(path)
    for index in range(5):
        store.add_turn(7, "user", f"вопрос {index}", ts(13))
    store.close()
    reopened = Store(path)
    assert [item["content"] for item in reopened.recent_turns(7, 2)] == ["вопрос 3", "вопрос 4"]
    reopened.close()


def test_clear_turns_removes_only_that_user(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.add_turn(7, "user", "мой", ts(13))
    store.add_turn(8, "user", "чужой", ts(13))
    store.clear_turns(7)
    assert store.recent_turns(7, 12) == []
    assert store.recent_turns(8, 12) == [{"role": "user", "content": "чужой"}]
    store.close()


def test_answers_are_stored_in_the_database(tmp_path) -> None:
    path = tmp_path / "replica.db"
    store = Store(path)
    thread = [{"role": "user", "content": "вопрос"}, {"role": "assistant", "content": "ответ"}]
    store.save_answer(chat_id=7, message_id=3, user_id=7, thread=thread, mode=None, pending_text=None,
                      truncated=True, now=ts(13))
    store.close()
    reopened = Store(path)
    row = reopened.get_answer(7, 3)
    assert row["thread"] == thread
    assert row["truncated"] == 1
    reopened.close()


def test_legacy_prototype_database_is_migrated(tmp_path) -> None:
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE users (user_id INTEGER PRIMARY KEY, plan TEXT NOT NULL, created_at INTEGER NOT NULL);
        CREATE TABLE usage (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            kind TEXT NOT NULL,
            created_at INTEGER NOT NULL
        );
        INSERT INTO users(user_id, plan, created_at) VALUES (7, 'pro', 0), (8, 'start', 0), (9, 'free', 0);
        INSERT INTO usage(user_id, kind, created_at) VALUES (7, 'cheap', 0);
        """
    )
    db.commit()
    db.close()
    later = int(time.time()) + 60
    store = Store(path)
    assert store.usage_totals(7) == (1, 0.0, 0, 0)
    pro_cheap = [row for row in store.active_entitlements(7, "cheap", later) if row["source"] == "admin"]
    pro_smart = [row for row in store.active_entitlements(7, "smart", later) if row["source"] == "admin"]
    assert pro_cheap[0]["quota"] == 500
    assert pro_smart[0]["quota"] == 150
    assert [row["quota"] for row in store.active_entitlements(8, "cheap", later) if row["source"] == "admin"] == [400]
    assert not [row for row in store.active_entitlements(9, None, later) if row["source"] == "admin"]
    store.close()
    again = Store(path)
    assert len([row for row in again.active_entitlements(7, "cheap", later) if row["source"] == "admin"]) == 1
    again.close()
