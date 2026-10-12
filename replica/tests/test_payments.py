import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramAPIError
from aiogram.types import StarTransaction, TransactionPartnerUser, User

from replica import billing_ui, payments
from replica.billing import entitlements as ent
from replica.catalog import PERIOD, TERMS_VERSION
from replica.quota import LOCAL_TZ, to_ts
from replica.store import Store

USER = 7
NOW = datetime(2026, 10, 14, 12, tzinfo=LOCAL_TZ)


class FakeError(TelegramAPIError):
    def __init__(self, text: str) -> None:
        super().__init__(method=None, message=text)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.transactions: list[StarTransaction] = []
        self.refund_error: Exception | None = None

    async def refund_star_payment(self, user_id: int, telegram_payment_charge_id: str, **_) -> bool:
        self.calls.append(("refund", user_id, telegram_payment_charge_id))
        if self.refund_error is not None:
            raise self.refund_error
        return True

    async def edit_user_star_subscription(self, user_id: int, telegram_payment_charge_id: str, is_canceled: bool, **_) -> bool:
        self.calls.append(("edit", user_id, telegram_payment_charge_id, is_canceled))
        return True

    async def answer_pre_checkout_query(self, query_id: str, ok: bool, error_message: str | None = None, **_) -> bool:
        self.calls.append(("precheckout", ok, error_message))
        return True

    async def send_message(self, chat_id: int, text: str, reply_markup=None, **_) -> None:
        self.calls.append(("send", chat_id, text))

    async def create_invoice_link(self, **kwargs) -> str:
        self.calls.append(("invoice_link", kwargs["subscription_period"], kwargs["payload"]))
        return "https://t.me/$invoice"

    async def send_invoice(self, **kwargs) -> None:
        self.calls.append(("send_invoice", kwargs["payload"]))

    async def get_star_transactions(self, offset: int = 0, limit: int = 100, **_) -> SimpleNamespace:
        return SimpleNamespace(transactions=self.transactions[offset : offset + limit])


class FakeMessage:
    def __init__(self, bot: FakeBot, user_id: int = USER) -> None:
        self.bot = bot
        self.chat = SimpleNamespace(id=user_id)
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[tuple[str, object]] = []

    async def answer(self, text: str, reply_markup=None, parse_mode=None) -> None:
        self.replies.append((text, reply_markup))


@pytest.fixture
def store(tmp_path) -> Store:
    db = Store(tmp_path / "replica.db")
    yield db
    db.close()


def invoice(store: Store, code: str, now: datetime = NOW, user_id: int = USER, suffix: str = "a") -> str:
    invoice_id = f"inv{suffix}{code}"
    payload = f"inv:{invoice_id}"
    product = payments.PRODUCTS[code]
    store.invoice_create(
        invoice_id=invoice_id, user_id=user_id, product=code, stars=product.stars, payload=payload,
        now=to_ts(now), expires_at=to_ts(now + timedelta(minutes=30)),
    )
    return payload


def pay(store: Store, payload: str, charge: str, now: datetime = NOW, recurring: bool = False,
        first: bool = True, expires: datetime | None = None, user_id: int = USER):
    stars = 0
    return payments.apply_payment(
        store, user_id=user_id, charge_id=charge, payload=payload, stars=stars, recurring=recurring,
        first_recurring=first, expires_ts=None if expires is None else to_ts(expires), source="update",
        raw="{}", now=now,
    )


def test_first_subscription_payment_activates_the_plan_once(store) -> None:
    payload = invoice(store, "pro")
    outcome = pay(store, payload, "c1")
    assert outcome.kind == "subscribed"
    assert outcome.code == "pro"
    status = ent.status_for(store, USER, NOW)
    assert status.tier == "pro"
    assert status.cheap.quota == 500
    assert status.smart.quota == 150
    assert store.invoice_by_payload(payload)["status"] == "paid"
    again = pay(store, payload, "c1")
    assert again.kind == "duplicate"
    assert ent.status_for(store, USER, NOW).smart.quota == 150


def test_renewal_opens_a_new_period_from_the_old_end(store) -> None:
    payload = invoice(store, "start")
    pay(store, payload, "c1")
    old_end = NOW + PERIOD
    outcome = pay(store, payload, "c2", now=old_end - timedelta(hours=1), recurring=True, first=False,
                  expires=old_end + PERIOD)
    assert outcome.kind == "renewed"
    sub = store.sub_active(USER)
    assert sub["period_start"] == to_ts(old_end)
    assert sub["period_end"] == to_ts(old_end + PERIOD)
    assert store.payment_get("c2")["status"] == "applied"
    assert store.event_add(sub["id"], "renewed", "update", "c2", "{}", to_ts(NOW)) is False


def test_renewal_after_grace_restarts_from_the_payment_time(store) -> None:
    payload = invoice(store, "start")
    pay(store, payload, "c1")
    late = NOW + PERIOD + timedelta(days=2)
    ent.sweep(store, late)
    outcome = pay(store, payload, "c2", now=late, recurring=True, first=False)
    assert outcome.kind == "renewed"
    assert store.sub_active(USER)["period_start"] == to_ts(late)


def test_upgrade_from_start_supersedes_it_and_asks_to_cancel_its_renewal(store) -> None:
    start_payload = invoice(store, "start", suffix="s")
    pay(store, start_payload, "start-charge")
    pro_payload = invoice(store, "pro", suffix="p")
    outcome = pay(store, pro_payload, "pro-charge", now=NOW + timedelta(days=1))
    assert outcome.kind == "subscribed"
    assert outcome.superseded_charge == "start-charge"
    assert store.sub_active(USER)["plan"] == "pro"
    start_sub = store.sub_by_payload(start_payload)
    assert start_sub["status"] == "superseded"
    assert ent.status_for(store, USER, NOW + timedelta(days=1)).smart.quota == 150


def test_second_identical_subscription_is_refunded_not_stacked(store) -> None:
    first = invoice(store, "start", suffix="1")
    pay(store, first, "c1")
    second = invoice(store, "start", suffix="2")
    outcome = pay(store, second, "c2")
    assert outcome.kind == "refund"
    assert store.payment_get("c2")["status"] == "refund_pending"
    assert store.sub_active(USER)["first_charge_id"] == "c1"


def test_pack_is_granted_and_marked_applied(store) -> None:
    payload = invoice(store, "pack_smart_100")
    outcome = pay(store, payload, "p1")
    assert outcome.kind == "applied"
    assert [pack.remaining for pack in ent.status_for(store, USER, NOW).packs] == [100]


def test_payment_with_unknown_invoice_is_recorded_as_failed(store) -> None:
    outcome = pay(store, "inv:nope", "c-x")
    assert outcome.kind == "failed"
    assert store.payment_get("c-x")["status"] == "failed_apply"
    assert ent.status_for(store, USER, NOW).tier == "free"


def test_precheckout_rejects_another_users_invoice(store) -> None:
    payload = invoice(store, "pro", user_id=99)
    assert payments.precheckout_error(store, USER, payload, NOW) == billing_ui.TEXT_TECH


def test_precheckout_rejects_stale_and_duplicate_and_pro_to_start(store) -> None:
    payload = invoice(store, "start", suffix="x")
    late = NOW + timedelta(hours=1)
    assert payments.precheckout_error(store, USER, payload, late) == billing_ui.TEXT_STALE
    pay(store, invoice(store, "start", suffix="y"), "c1")
    again = invoice(store, "start", suffix="z")
    assert payments.precheckout_error(store, USER, again, NOW) == billing_ui.duplicate_text("start")
    pay(store, invoice(store, "pro", suffix="q"), "c2", now=NOW + timedelta(minutes=1))
    start_again = invoice(store, "start", suffix="w")
    assert payments.precheckout_error(store, USER, start_again, NOW) == billing_ui.TEXT_PRO_BLOCKS_START


def test_precheckout_lock_blocks_a_second_purchase_for_two_minutes(store) -> None:
    first = invoice(store, "pack_smart_100", suffix="1")
    second = invoice(store, "pack_smart_100", suffix="2")
    assert payments.precheckout_error(store, USER, first, NOW) is None
    assert payments.precheckout_error(store, USER, second, NOW + timedelta(minutes=1)) == billing_ui.TEXT_TECH
    assert payments.precheckout_error(store, USER, second, NOW + timedelta(minutes=3)) is None


def test_pre_checkout_accepts_a_renewal_of_an_existing_subscription(store) -> None:
    payload = invoice(store, "start")
    pay(store, payload, "c1")
    assert payments.precheckout_error(store, USER, payload, NOW + PERIOD) is None


def test_pre_checkout_handler_answers_ok_or_error(store) -> None:
    bot = FakeBot()
    payload = invoice(store, "pro")
    asyncio.run(payments.handle_pre_checkout(bot, store, "q1", USER, payload, NOW))
    assert bot.calls[-1] == ("precheckout", True, None)
    asyncio.run(payments.handle_pre_checkout(bot, store, "q2", USER, "inv:missing", NOW))
    assert bot.calls[-1] == ("precheckout", False, billing_ui.TEXT_TECH)


def test_refund_revokes_access_and_is_idempotent(store) -> None:
    payload = invoice(store, "pro")
    pay(store, payload, "c1")
    assert payments.apply_refund(store, "c1", NOW) is True
    assert payments.apply_refund(store, "c1", NOW) is False
    assert store.sub_by_charge("c1")["status"] == "refunded"
    assert ent.status_for(store, USER, NOW).tier == "free"


def test_refund_of_a_pack_removes_only_the_pack(store) -> None:
    pay(store, invoice(store, "pack_smart_100"), "p1")
    ent.grant_admin(store, user_id=USER, plan="start", now=NOW, days=None, actor="1")
    payments.apply_refund(store, "p1", NOW)
    status = ent.status_for(store, USER, NOW)
    assert status.packs == ()
    assert status.tier == "start"


def test_refund_charge_calls_telegram_and_cancels_the_subscription(store) -> None:
    bot = FakeBot()
    pay(store, invoice(store, "start"), "c1")
    text = asyncio.run(payments.refund_charge(bot, store, "c1", NOW))
    assert text == "Возврат выполнен, доступ отозван."
    assert ("refund", USER, "c1") in bot.calls
    assert ("edit", USER, "c1", True) in bot.calls


def test_refund_already_done_at_telegram_is_treated_as_success(store) -> None:
    bot = FakeBot()
    bot.refund_error = FakeError("CHARGE_ALREADY_REFUNDED")
    pay(store, invoice(store, "pack_smart_100"), "p1")
    text = asyncio.run(payments.refund_charge(bot, store, "p1", NOW))
    assert text == "Возврат выполнен, доступ отозван."
    assert store.payment_get("p1")["status"] == "refunded"


def test_refund_failure_keeps_the_payment_and_access(store) -> None:
    bot = FakeBot()
    bot.refund_error = FakeError("BOT_ERROR")
    pay(store, invoice(store, "pack_smart_100"), "p1")
    text = asyncio.run(payments.refund_charge(bot, store, "p1", NOW))
    assert "не принял" in text
    assert store.payment_get("p1")["status"] == "applied"


def test_subscription_update_canceled_turns_renewal_off(store) -> None:
    bot = FakeBot()
    pay(store, invoice(store, "start"), "c1")
    asyncio.run(payments.handle_subscription_update(bot, store, USER, store.sub_active(USER)["invoice_payload"], "canceled", NOW))
    assert store.sub_active(USER)["auto_renew"] == 0
    asyncio.run(payments.handle_subscription_update(bot, store, USER, store.sub_active(USER)["invoice_payload"], "active", NOW))
    assert store.sub_active(USER)["auto_renew"] == 1


def test_failed_renewal_notifies_the_user(store) -> None:
    bot = FakeBot()
    payload = invoice(store, "start")
    pay(store, payload, "c1")
    asyncio.run(payments.handle_subscription_update(bot, store, USER, payload, "failed", NOW))
    sent = [call for call in bot.calls if call[0] == "send"]
    assert sent and "Не удалось продлить" in sent[0][2]


def test_reconcile_applies_a_missed_payment_and_is_idempotent(store) -> None:
    bot = FakeBot()
    payload = invoice(store, "pro")
    user = User(id=USER, is_bot=False, first_name="u")
    bot.transactions = [
        StarTransaction(
            id="missed-1",
            amount=600,
            date=NOW,
            source=TransactionPartnerUser(transaction_type="invoice_payment", user=user, invoice_payload=payload,
                                          subscription_period=2592000),
        ),
    ]
    outcomes = asyncio.run(payments.reconcile(bot, store, NOW))
    assert [item.kind for item in outcomes] == ["subscribed"]
    assert store.payment_get("missed-1")["source"] == "reconcile"
    assert asyncio.run(payments.reconcile(bot, store, NOW)) == []
    assert ent.status_for(store, USER, NOW).tier == "pro"


def test_reconcile_ignores_outgoing_and_foreign_transactions(store) -> None:
    bot = FakeBot()
    user = User(id=USER, is_bot=False, first_name="u")
    bot.transactions = [
        StarTransaction(id="out", amount=-300, date=NOW,
                        source=TransactionPartnerUser(transaction_type="invoice_payment", user=user, invoice_payload="inv:x")),
        StarTransaction(id="other", amount=10, date=NOW,
                        source=TransactionPartnerUser(transaction_type="invoice_payment", user=user, invoice_payload="foreign")),
    ]
    assert asyncio.run(payments.reconcile(bot, store, NOW)) == []
    assert store.payment_get("out") is None
    assert store.payment_get("other") is None


def test_start_purchase_asks_for_consent_first(store) -> None:
    bot = FakeBot()
    message = FakeMessage(bot)
    asyncio.run(payments.start_purchase(message, store, USER, "pro", NOW, accepted=False))
    text, markup = message.replies[-1]
    assert "/terms" in text
    assert not bot.calls
    assert store.terms_version(USER) is None


def test_accepted_terms_issue_a_subscription_link_with_a_period(store) -> None:
    bot = FakeBot()
    message = FakeMessage(bot)
    store.accept_terms(USER, TERMS_VERSION, to_ts(NOW))
    asyncio.run(payments.start_purchase(message, store, USER, "pro", NOW, accepted=False))
    kinds = [call[0] for call in bot.calls]
    assert kinds == ["invoice_link"]
    assert bot.calls[0][1] == 2592000
    assert bot.calls[0][2].startswith("inv:")


def test_duplicate_plan_is_refused_before_an_invoice_is_made(store) -> None:
    bot = FakeBot()
    pay(store, invoice(store, "start", suffix="1"), "c1")
    message = FakeMessage(bot)
    store.accept_terms(USER, TERMS_VERSION, to_ts(NOW))
    asyncio.run(payments.start_purchase(message, store, USER, "start", NOW, accepted=False))
    assert message.replies[-1][0] == billing_ui.duplicate_text("start")
    assert not bot.calls


def test_pack_purchase_sends_a_single_chat_invoice(store) -> None:
    bot = FakeBot()
    message = FakeMessage(bot)
    store.accept_terms(USER, TERMS_VERSION, to_ts(NOW))
    asyncio.run(payments.start_purchase(message, store, USER, "pack_smart_100", NOW, accepted=False))
    assert [call[0] for call in bot.calls] == ["send_invoice"]


def test_renewal_toggle_calls_telegram_and_updates_the_row(store) -> None:
    bot = FakeBot()
    pay(store, invoice(store, "start"), "c1")
    text = asyncio.run(payments.set_renewal(bot, store, USER, False, NOW))
    assert ("edit", USER, "c1", True) in bot.calls
    assert store.sub_active(USER)["auto_renew"] == 0
    assert "Продление выключено" in text


def test_payload_fits_telegram_limit(store) -> None:
    payload = invoice(store, "pro")
    assert len(payload) <= 128
    assert json.loads(json.dumps({"p": payload}))["p"] == payload
