from dataclasses import dataclass
from datetime import datetime, timedelta

from replica.catalog import (
    ANSWER_TTL,
    GRACE,
    PACK,
    PACK_LIFETIME,
    PRODUCTS,
    REMINDER_BEFORE,
    RESERVATION_TTL,
    SUBSCRIPTION,
    WINDOW_CYCLE,
    WINDOW_PERIOD,
    WINDOW_WEEK,
)
from replica.quota import Bucket, Decision, Window, decide, from_ts, to_ts, window_for
from replica.store import Store

PAID_CHEAP_SOURCES = ("subscription", "admin")
SMART_ACCESS_SOURCES = ("subscription", "admin", "pack")
SOURCE_PRIORITY = {"subscription": 0, "admin": 1, "pack": 2, "trial": 3, "free": 4}


@dataclass(frozen=True)
class Reserved:
    key: str
    kind: str
    source: str
    bucket_id: int
    remaining: int


@dataclass(frozen=True)
class Denied:
    kind: str
    reason: str
    decision: Decision | None


@dataclass(frozen=True)
class Existing:
    key: str
    status: str


@dataclass(frozen=True)
class Line:
    remaining: int
    quota: int
    resets_at: datetime | None
    window: str


@dataclass(frozen=True)
class Pack:
    remaining: int
    until: datetime | None


@dataclass(frozen=True)
class Status:
    tier: str
    tier_until: datetime | None
    auto_renew: bool
    subscribed: bool
    cheap: Line | None
    smart: Line | None
    packs: tuple[Pack, ...]
    trial_left: int


@dataclass(frozen=True)
class Notice:
    user_id: int
    kind: str
    plan: str
    until: datetime


def bucket_of(row: dict) -> Bucket:
    until = row["valid_until"]
    return Bucket(
        window=row["window"],
        quota=row["quota"],
        valid_from=from_ts(row["valid_from"]),
        valid_until=None if until is None else from_ts(until),
    )


def ordered_buckets(store: Store, user_id: int, kind: str, now: datetime) -> list[dict]:
    rows = store.active_entitlements(user_id, kind, to_ts(now))
    if kind == "cheap" and any(row["source"] in PAID_CHEAP_SOURCES for row in rows):
        rows = [row for row in rows if row["source"] != "free"]
    if kind == "smart" and any(row["source"] in SMART_ACCESS_SOURCES for row in rows):
        rows = [row for row in rows if row["source"] != "trial"]
    rows.sort(key=lambda row: (row["valid_until"] is None, row["valid_until"] or 0, SOURCE_PRIORITY[row["source"]]))
    return rows


def current_window(store: Store, row: dict, now: datetime) -> tuple[Window, int] | None:
    window = window_for(bucket_of(row), now)
    if window is None:
        return None
    used = store.usage_count(row["id"], to_ts(window.start))
    return window, used


def reserve(store: Store, user_id: int, kind: str, key: str, now: datetime) -> Reserved | Denied | Existing:
    with store.transaction():
        store.ensure_user(user_id, to_ts(now))
        existing = store.usage_by_key(key)
        if existing is not None:
            return Existing(key=key, status=existing["status"])
        if store.user_status(user_id) == "blocked":
            return Denied(kind=kind, reason="blocked", decision=None)
        rows = ordered_buckets(store, user_id, kind, now)
        earliest = None
        for row in rows:
            current = current_window(store, row, now)
            if current is None:
                continue
            window, used = current
            decision = decide(used, window)
            if decision.allowed:
                store.usage_reserve(
                    user_id=user_id, kind=kind, key=key, bucket_id=row["id"],
                    window_start=to_ts(window.start), now=to_ts(now),
                )
                return Reserved(key=key, kind=kind, source=row["source"], bucket_id=row["id"], remaining=decision.remaining - 1)
            if decision.resets_at is not None and (earliest is None or decision.resets_at < earliest.resets_at):
                earliest = decision
        if kind == "smart" and not any(row["source"] in SMART_ACCESS_SOURCES for row in rows):
            return Denied(kind=kind, reason="no_access", decision=None)
        return Denied(kind=kind, reason="quota", decision=earliest)


def commit(store: Store, key: str, *, cost: float, prompt: int, completion: int, reasoning: int,
           model_id: str, finish_reason: str | None, latency_ms: int, generation_id: str | None) -> None:
    store.usage_commit(
        key, cost=cost, prompt=prompt, completion=completion, reasoning=reasoning, model_id=model_id,
        finish_reason=finish_reason, latency_ms=latency_ms, generation_id=generation_id,
    )


def release(store: Store, key: str, error_code: str) -> None:
    store.usage_release(key, error_code)


def grant_subscription(store: Store, *, user_id: int, plan: str, subscription_id: int, charge_id: str,
                       start: datetime, end: datetime, now: datetime) -> list[int]:
    product = PRODUCTS[plan]
    ids = [
        store.add_entitlement(
            user_id=user_id, plan=plan, source=SUBSCRIPTION, source_id=subscription_id, kind="cheap",
            window=WINDOW_WEEK, quota=product.cheap, valid_from=to_ts(start), valid_until=to_ts(end),
            payment_id=charge_id, now=to_ts(now),
        )
    ]
    if product.smart:
        ids.append(
            store.add_entitlement(
                user_id=user_id, plan=plan, source=SUBSCRIPTION, source_id=subscription_id, kind="smart",
                window=WINDOW_PERIOD, quota=product.smart, valid_from=to_ts(start), valid_until=to_ts(end),
                payment_id=charge_id, now=to_ts(now),
            )
        )
    return ids


def grant_pack(store: Store, *, user_id: int, code: str, charge_id: str, now: datetime) -> list[int]:
    product = PRODUCTS[code]
    return [
        store.add_entitlement(
            user_id=user_id, plan=code, source=PACK, source_id=None, kind="smart", window=WINDOW_PERIOD,
            quota=product.smart, valid_from=to_ts(now), valid_until=to_ts(now + PACK_LIFETIME),
            payment_id=charge_id, now=to_ts(now),
        )
    ]


def revoke_subscription(store: Store, subscription_id: int, reason: str, now: datetime) -> None:
    ids = store.entitlement_ids(source=SUBSCRIPTION, source_id=subscription_id)
    store.revoke_entitlements(ids, reason, "subscription", str(subscription_id), to_ts(now))


def revoke_payment(store: Store, charge_id: str, now: datetime) -> None:
    ids = store.entitlement_ids(payment_id=charge_id)
    store.revoke_entitlements(ids, "refund_revoke", "payment", charge_id, to_ts(now))


def grant_admin(store: Store, *, user_id: int, plan: str, now: datetime, days: int | None, actor: str) -> None:
    with store.transaction():
        ids = [
            row["id"]
            for row in store.active_entitlements(user_id, None, to_ts(now))
            if row["source"] == "admin" and not _is_pack(row)
        ]
        store.revoke_entitlements(ids, "admin_adjust", "admin", actor, to_ts(now), actor)
        if plan == "free":
            return
        product = PRODUCTS[plan]
        until = None if days is None else now + timedelta(days=days)
        store.add_entitlement(
            user_id=user_id, plan=plan, source="admin", source_id=None, kind="cheap", window=WINDOW_WEEK,
            quota=product.cheap, valid_from=to_ts(now), valid_until=None if until is None else to_ts(until),
            payment_id=None, now=to_ts(now),
        )
        store.add_entitlement(
            user_id=user_id, plan=plan, source="admin", source_id=None, kind="smart",
            window=WINDOW_PERIOD if until is not None else WINDOW_CYCLE, quota=product.smart,
            valid_from=to_ts(now), valid_until=None if until is None else to_ts(until),
            payment_id=None, now=to_ts(now),
        )


def _is_pack(row: dict) -> bool:
    return row["source"] == PACK or row["plan"] == "pack"


def grant_admin_pack(store: Store, *, user_id: int, smart: int, days: int, now: datetime) -> int:
    until = now + timedelta(days=days)
    return store.add_entitlement(
        user_id=user_id, plan="pack", source="admin", source_id=None, kind="smart", window=WINDOW_PERIOD,
        quota=smart, valid_from=to_ts(now), valid_until=to_ts(until), payment_id=None, now=to_ts(now),
    )


def status_for(store: Store, user_id: int, now: datetime) -> Status:
    with store.transaction():
        store.ensure_user(user_id, to_ts(now))
        sub = store.sub_active(user_id)
        rows = store.active_entitlements(user_id, None, to_ts(now))
        admin_plans = [row for row in rows if row["source"] == "admin" and not _is_pack(row)]
        if sub is not None:
            tier, tier_until, auto_renew = sub["plan"], from_ts(sub["period_end"]), bool(sub["auto_renew"])
        elif admin_plans:
            tier = admin_plans[0]["plan"]
            until = admin_plans[0]["valid_until"]
            tier_until, auto_renew = (None if until is None else from_ts(until)), False
        else:
            tier, tier_until, auto_renew = "free", None, False
        cheap = _line_for(store, ordered_buckets(store, user_id, "cheap", now), now)
        smart_all = ordered_buckets(store, user_id, "smart", now)
        smart = _line_for(
            store,
            [row for row in smart_all if row["source"] != "trial" and not _is_pack(row)],
            now,
        )
        packs = []
        for row in smart_all:
            if not _is_pack(row):
                continue
            current = current_window(store, row, now)
            if current is not None:
                window, used = current
                packs.append(Pack(remaining=max(window.quota - used, 0), until=bucket_of(row).valid_until))
        trial_left = 0
        for row in smart_all:
            if row["source"] == "trial":
                current = current_window(store, row, now)
                if current is not None:
                    window, used = current
                    trial_left += max(window.quota - used, 0)
    return Status(
        tier=tier, tier_until=tier_until, auto_renew=auto_renew, subscribed=sub is not None,
        cheap=cheap, smart=smart, packs=tuple(packs), trial_left=trial_left,
    )


def _line_for(store: Store, rows: list[dict], now: datetime) -> Line | None:
    for row in rows:
        current = current_window(store, row, now)
        if current is None:
            continue
        window, used = current
        return Line(
            remaining=max(window.quota - used, 0), quota=window.quota,
            resets_at=window.end, window=row["window"],
        )
    return None


def sweep(store: Store, now: datetime) -> list[Notice]:
    notices: list[Notice] = []
    with store.transaction():
        store.usage_release_stale(to_ts(now - RESERVATION_TTL))
        store.prune_answers(to_ts(now - ANSWER_TTL))
        store.invoices_expire(to_ts(now))
        store.expire_entitlements(to_ts(now))
        for sub in store.subs_past_end(to_ts(now - GRACE)):
            store.sub_update(sub["id"], to_ts(now), status="expired", auto_renew=0)
            ids = store.entitlement_ids(source=SUBSCRIPTION, source_id=sub["id"])
            store.revoke_entitlements(ids, "expire", "subscription", str(sub["id"]), to_ts(now))
            notices.append(Notice(sub["user_id"], "expired", sub["plan"], from_ts(sub["period_end"])))
        for sub in store.subs_reminder_due(to_ts(now), to_ts(now + REMINDER_BEFORE)):
            store.sub_update(sub["id"], to_ts(now), reminder_sent=1)
            notices.append(Notice(sub["user_id"], "reminder", sub["plan"], from_ts(sub["period_end"])))
    return notices

