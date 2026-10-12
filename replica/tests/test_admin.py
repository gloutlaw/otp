import asyncio
from datetime import datetime, timedelta

import pytest

from replica import admin
from replica.billing import entitlements as ent
from replica.catalog import PRODUCTS
from replica.quota import LOCAL_TZ, to_ts
from replica.store import Store

OWNER = 1
NOW = datetime(2026, 10, 14, 12, tzinfo=LOCAL_TZ)


class FakeBot:
    def __init__(self) -> None:
        self.refunds: list[tuple] = []

    async def refund_star_payment(self, user_id: int, telegram_payment_charge_id: str, **_) -> bool:
        self.refunds.append((user_id, telegram_payment_charge_id))
        return True

    async def edit_user_star_subscription(self, **_) -> bool:
        return True


class FakeMessage:
    def __init__(self) -> None:
        self.bot = FakeBot()
        self.replies: list[str] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.replies.append(text)


@pytest.fixture
def store(tmp_path):
    db = Store(tmp_path / "replica.db")
    yield db
    db.close()


def run(store: Store, text: str, user_id: int = OWNER, message: FakeMessage | None = None) -> FakeMessage:
    message = message or FakeMessage()
    asyncio.run(admin.handle_admin(message, text, user_id, store, OWNER, NOW))
    return message


def test_non_owner_gets_no_answer(store) -> None:
    message = run(store, "/grant 61 pro", user_id=99)
    assert message.replies == []
    assert ent.status_for(store, 61, NOW).tier == "free"


def test_owner_grants_a_plan_with_days(store) -> None:
    message = run(store, "/grant 61 pro 30")
    assert message.replies[-1] == "Тариф Pro выдан: 61 на 30 дн"
    status = ent.status_for(store, 61, NOW)
    assert status.tier == "pro"
    assert status.tier_until == NOW + timedelta(days=30)


def test_owner_grants_without_days_as_a_cycle(store) -> None:
    message = run(store, "/grant 61 start")
    assert message.replies[-1] == "Тариф Старт выдан: 61 без срока"
    assert ent.status_for(store, 61, NOW).smart.quota == PRODUCTS["start"].smart


def test_grant_format_errors_get_usage_hint(store) -> None:
    assert run(store, "/grant 61 platinum").replies[-1] == admin.GRANT_USAGE_TEXT
    assert run(store, "/grant 61 pro -3").replies[-1] == admin.GRANT_USAGE_TEXT
    assert run(store, "/grant x pro").replies[-1] == admin.GRANT_USAGE_TEXT


def test_grant_is_written_to_the_audit_log(store) -> None:
    run(store, "/grant 61 pro 30")
    rows = store._all("SELECT action, target_user_id FROM admin_audit")
    assert [(row["action"], row["target_user_id"]) for row in rows] == [("grant", 61)]


def test_revoke_returns_to_free(store) -> None:
    run(store, "/grant 61 pro")
    run(store, "/revoke 61")
    assert ent.status_for(store, 61, NOW).tier == "free"


def test_addpack_grants_a_smart_pack_for_ninety_days_by_default(store) -> None:
    run(store, "/addpack 61 smart 50")
    status = ent.status_for(store, 61, NOW)
    assert [pack.remaining for pack in status.packs] == [50]
    assert status.packs[0].until == NOW + timedelta(days=90)


def test_block_and_unblock_change_status(store) -> None:
    run(store, "/block 61")
    assert store.user_status(61) == "blocked"
    run(store, "/unblock 61")
    assert store.user_status(61) == "active"


def test_refund_needs_confirmation_and_then_refunds(store) -> None:
    from replica import payments

    payload = "inv:admin-test"
    store.invoice_create(invoice_id="admin-test", user_id=61, product="pack_smart_100", stars=219,
                         payload=payload, now=to_ts(NOW), expires_at=to_ts(NOW + timedelta(minutes=30)))
    payments.apply_payment(store, user_id=61, charge_id="p9", payload=payload, stars=219, recurring=False,
                           first_recurring=False, expires_ts=None, source="update", raw="{}", now=NOW)
    prompt = run(store, "/refund p9")
    assert "yes" in prompt.replies[-1]
    assert store.payment_get("p9")["status"] == "applied"
    done = run(store, "/refund p9 yes")
    assert done.bot.refunds == [(61, "p9")]
    assert store.payment_get("p9")["status"] == "refunded"


def test_refund_of_unknown_charge_says_so(store) -> None:
    assert run(store, "/refund nope yes").replies[-1] == "Платёж не найден."


def test_reports_render_without_data(store) -> None:
    assert "Аномалий нет." in run(store, "/anomalies").replies[-1]
    assert "Пользователей:" in run(store, "/stats").replies[-1]
    assert "За 30 дн" in run(store, "/costs").replies[-1]
    assert "Платежей нет." in run(store, "/payments 61").replies[-1]


def test_user_card_shows_plan_and_balance(store) -> None:
    run(store, "/grant 61 pro")
    text = run(store, "/user 61").replies[-1]
    assert "Пользователь 61" in text
    assert "Pro" in text
