from datetime import datetime, timedelta

from replica.billing import entitlements as ent
from replica.billing.entitlements import Denied, Existing, Reserved
from replica.catalog import TRIAL_SMART
from replica.quota import LOCAL_TZ, to_ts
from replica.store import Store

USER = 7


def at(day: int, hour: int = 12) -> datetime:
    return datetime(2026, 10, day, hour, tzinfo=LOCAL_TZ)


def reserve_and_commit(store: Store, key: str, kind: str, now: datetime) -> Reserved:
    result = ent.reserve(store, USER, kind, key, now)
    assert isinstance(result, Reserved), result
    ent.commit(store, key, cost=0.001, prompt=10, completion=5, reasoning=0, model_id="m",
               finish_reason="stop", latency_ms=1, generation_id=None)
    return result


def make_subscription(store: Store, plan: str, start: datetime, payload: str = "inv:a", charge: str = "c1") -> int:
    end = start + timedelta(days=30)
    sub_id = store.sub_create(
        user_id=USER, plan=plan, payload=payload, first_charge=charge,
        start=to_ts(start), end=to_ts(end), now=to_ts(start),
    )
    ent.grant_subscription(store, user_id=USER, plan=plan, subscription_id=sub_id, charge_id=charge,
                           start=start, end=end, now=start)
    return sub_id


def test_free_user_gets_ten_cheap_requests_per_day(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    now = at(14)
    for index in range(10):
        reserved = reserve_and_commit(store, f"k{index}", "cheap", now)
        assert reserved.source == "free"
    denied = ent.reserve(store, USER, "cheap", "k10", now)
    assert isinstance(denied, Denied)
    assert denied.reason == "quota"
    assert denied.decision.resets_at == at(15, 0)
    store.close()


def test_free_quota_resets_at_local_midnight(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    for index in range(10):
        reserve_and_commit(store, f"k{index}", "cheap", at(14, 23))
    assert isinstance(ent.reserve(store, USER, "cheap", "late", at(14, 23)), Denied)
    assert isinstance(ent.reserve(store, USER, "cheap", "next", at(15, 0)), Reserved)
    store.close()


def test_trial_gives_three_smart_answers_then_paywall(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    for index in range(TRIAL_SMART):
        assert reserve_and_commit(store, f"s{index}", "smart", at(14)).source == "trial"
    denied = ent.reserve(store, USER, "smart", "s-last", at(14))
    assert isinstance(denied, Denied)
    assert denied.reason == "no_access"
    store.close()


def test_idempotency_key_returns_the_existing_row(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    first = ent.reserve(store, USER, "cheap", "cb:1:2:smart", at(14))
    assert isinstance(first, Reserved)
    second = ent.reserve(store, USER, "cheap", "cb:1:2:smart", at(14))
    assert second == Existing(key="cb:1:2:smart", status="reserved")
    ent.commit(store, "cb:1:2:smart", cost=0, prompt=0, completion=0, reasoning=0, model_id="m",
               finish_reason=None, latency_ms=0, generation_id=None)
    assert ent.reserve(store, USER, "cheap", "cb:1:2:smart", at(14)) == Existing(key="cb:1:2:smart", status="committed")
    store.close()


def test_released_reservation_gives_the_quota_back(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    for index in range(10):
        ent.reserve(store, USER, "cheap", f"k{index}", at(14))
    assert isinstance(ent.reserve(store, USER, "cheap", "extra", at(14)), Denied)
    ent.release(store, "k0", "failed")
    assert isinstance(ent.reserve(store, USER, "cheap", "extra", at(14)), Reserved)
    store.close()


def test_blocked_user_is_refused_before_any_bucket(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    store.set_user_status(USER, "blocked", to_ts(at(14)))
    denied = ent.reserve(store, USER, "cheap", "k", at(14))
    assert isinstance(denied, Denied)
    assert denied.reason == "blocked"
    store.close()


def test_subscription_cheap_quota_hides_the_free_bucket(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(20, 14)
    make_subscription(store, "start", start)
    ent.reserve(store, USER, "cheap", "k0", start)
    rows = ent.ordered_buckets(store, USER, "cheap", start)
    assert [row["source"] for row in rows] == ["subscription"]
    store.close()


def test_pro_weekly_quota_then_proportional_last_block(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(20, 14)
    make_subscription(store, "pro", start)
    for index in range(500):
        reserve_and_commit(store, f"w{index}", "cheap", start)
    denied = ent.reserve(store, USER, "cheap", "w-extra", start)
    assert isinstance(denied, Denied) and denied.reason == "quota"
    assert denied.decision.resets_at == start + timedelta(days=7)
    last_block = start + timedelta(days=29)
    for index in range(143):
        reserve_and_commit(store, f"l{index}", "cheap", last_block)
    assert isinstance(ent.reserve(store, USER, "cheap", "l-extra", last_block), Denied)
    store.close()


def test_pro_smart_is_capped_per_period_not_per_calendar_month(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(20, 14)
    make_subscription(store, "pro", start)
    for index in range(150):
        reserve_and_commit(store, f"s{index}", "smart", start + timedelta(days=index % 25))
    denied = ent.reserve(store, USER, "smart", "s-extra", start + timedelta(days=26))
    assert isinstance(denied, Denied) and denied.reason == "quota"
    assert denied.decision.resets_at == start + timedelta(days=30)
    store.close()


def test_subscription_smart_is_spent_before_a_pack(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(20, 14)
    make_subscription(store, "start", start)
    ent.grant_pack(store, user_id=USER, code="pack_smart_100", charge_id="p1", now=start)
    assert reserve_and_commit(store, "first", "smart", start).source == "subscription"
    for index in range(19):
        reserve_and_commit(store, f"more{index}", "smart", start)
    assert reserve_and_commit(store, "after", "smart", start).source == "pack"
    store.close()


def test_admin_grant_without_days_is_a_cycle_not_an_expiry(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    ent.grant_admin(store, user_id=USER, plan="pro", now=at(1), days=None, actor="1")
    status = ent.status_for(store, USER, at(1))
    assert status.tier == "pro"
    assert status.tier_until is None
    assert status.smart is not None and status.smart.quota == 150
    assert status.cheap is not None and status.cheap.quota == 500
    store.close()


def test_admin_grant_with_days_expires_and_revoke_restores_free(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    ent.grant_admin(store, user_id=USER, plan="start", now=at(1), days=3, actor="1")
    assert ent.status_for(store, USER, at(2)).tier == "start"
    assert ent.status_for(store, USER, at(5)).tier == "free"
    ent.grant_admin(store, user_id=USER, plan="pro", now=at(6), days=None, actor="1")
    ent.grant_admin(store, user_id=USER, plan="free", now=at(6), days=None, actor="1")
    assert ent.status_for(store, USER, at(6)).tier == "free"
    store.close()


def test_status_reports_pack_balance(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    ent.grant_pack(store, user_id=USER, code="pack_smart_100", charge_id="p1", now=at(1))
    status = ent.status_for(store, USER, at(2))
    assert [pack.remaining for pack in status.packs] == [100]
    assert status.packs[0].until == at(1) + timedelta(days=90)
    store.close()


def test_sweep_expires_a_lapsed_subscription_and_reports_it_once(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(1, 12)
    make_subscription(store, "pro", start)
    late = start + timedelta(days=30, hours=3)
    notices = ent.sweep(store, late)
    assert [(notice.kind, notice.plan) for notice in notices] == [("expired", "pro")]
    assert ent.sweep(store, late) == []
    assert ent.status_for(store, USER, late).tier == "free"
    store.close()


def test_sweep_sends_one_reminder_for_a_cancelled_renewal(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    start = at(1, 12)
    sub_id = make_subscription(store, "start", start)
    store.sub_update(sub_id, to_ts(start), auto_renew=0)
    soon = start + timedelta(days=27)
    notices = ent.sweep(store, soon)
    assert [notice.kind for notice in notices] == ["reminder"]
    assert ent.sweep(store, soon + timedelta(hours=1)) == []
    store.close()


def test_sweep_expires_stale_reservations_and_invoices(tmp_path) -> None:
    store = Store(tmp_path / "replica.db")
    ent.reserve(store, USER, "cheap", "stuck", at(14, 8))
    ent.sweep(store, at(14, 9))
    assert store.usage_by_key("stuck") is None
    store.close()
