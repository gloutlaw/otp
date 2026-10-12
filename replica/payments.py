import json
import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import LabeledPrice, Message, TransactionPartnerUser

from replica import billing_ui
from replica.billing import entitlements as ent
from replica.catalog import (
    CURRENCY,
    GRACE,
    INVOICE_TTL,
    PERIOD,
    PERIOD_SECONDS,
    PRECHECKOUT_LOCK,
    PRODUCTS,
    SUBSCRIPTION,
    TERMS_VERSION,
)
from replica.quota import from_ts, to_ts
from replica.store import Store

logger = logging.getLogger(__name__)

RECONCILE_PAGE = 100
RECONCILE_MAX_PAGES = 50


@dataclass(frozen=True)
class Outcome:
    kind: str
    user_id: int
    charge_id: str = ""
    code: str = ""
    subscription_id: int | None = None
    superseded_charge: str | None = None


def purchase_refusal(store: Store, user_id: int, code: str) -> str | None:
    product = PRODUCTS[code]
    if product.kind != SUBSCRIPTION:
        return None
    active = store.sub_active(user_id)
    if active is None:
        return None
    if active["plan"] == product.code:
        return billing_ui.duplicate_text(product.code)
    if active["plan"] == "pro" and product.code == "start":
        return billing_ui.TEXT_PRO_BLOCKS_START
    return None


def create_invoice(store: Store, user_id: int, code: str, now: datetime) -> tuple[str, str]:
    invoice_id = uuid4().hex
    payload = f"inv:{invoice_id}"
    product = PRODUCTS[code]
    store.invoice_create(
        invoice_id=invoice_id, user_id=user_id, product=code, stars=product.stars, payload=payload,
        now=to_ts(now), expires_at=to_ts(now + INVOICE_TTL),
    )
    return invoice_id, payload


def precheckout_error(store: Store, user_id: int, payload: str, now: datetime) -> str | None:
    invoice = store.invoice_by_payload(payload)
    if invoice is None or invoice["user_id"] != user_id:
        return billing_ui.TEXT_TECH
    if invoice["status"] == "paid" and store.sub_by_payload(payload) is not None:
        return None
    if invoice["status"] not in ("created", "prechecked"):
        return billing_ui.TEXT_STALE
    if invoice["status"] == "created" and to_ts(now) >= invoice["expires_at"]:
        return billing_ui.TEXT_STALE
    code = invoice["product_code"]
    refusal = purchase_refusal(store, user_id, code)
    if refusal is not None:
        return refusal
    other = store.invoice_prechecked_since(user_id, to_ts(now - PRECHECKOUT_LOCK), invoice["invoice_id"])
    if other is not None:
        return billing_ui.TEXT_TECH
    store.invoice_set_status(invoice["invoice_id"], "prechecked", to_ts(now))
    return None


def _payment_raw(**fields: object) -> str:
    return json.dumps(fields, ensure_ascii=False, sort_keys=True)


def apply_payment(
    store: Store, *, user_id: int, charge_id: str, payload: str, stars: int, recurring: bool,
    first_recurring: bool, expires_ts: int | None, source: str, raw: str, now: datetime,
) -> Outcome:
    now_ts = to_ts(now)
    with store.transaction():
        if store.payment_get(charge_id) is not None:
            return Outcome("duplicate", user_id, charge_id)
        store.ensure_user(user_id, now_ts)
        if recurring and not first_recurring:
            return _apply_renewal(store, user_id, charge_id, payload, stars, expires_ts, source, raw, now)
        invoice = store.invoice_by_payload(payload)
        if invoice is None or invoice["user_id"] != user_id:
            store.payment_add(
                charge_id=charge_id, user_id=user_id, invoice_id=None, product=None, stars=stars,
                recurring=recurring, source=source, status="failed_apply", subscription_id=None,
                raw_json=raw, now=now_ts,
            )
            return Outcome("failed", user_id, charge_id)
        code = invoice["product_code"]
        product = PRODUCTS[code]
        if product.kind != SUBSCRIPTION:
            store.payment_add(
                charge_id=charge_id, user_id=user_id, invoice_id=invoice["invoice_id"], product=code,
                stars=stars, recurring=False, source=source, status="applied", subscription_id=None,
                raw_json=raw, now=now_ts,
            )
            ent.grant_pack(store, user_id=user_id, code=code, charge_id=charge_id, now=now)
            store.invoice_set_status(invoice["invoice_id"], "paid", now_ts)
            return Outcome("applied", user_id, charge_id, code=code)
        existing = store.sub_active(user_id)
        # Pro is never stacked: a second Start under Pro, or a repeated plan, is refunded.
        if existing is not None and (existing["plan"] == code or existing["plan"] == "pro"):
            store.payment_add(
                charge_id=charge_id, user_id=user_id, invoice_id=invoice["invoice_id"], product=code,
                stars=stars, recurring=recurring, source=source, status="refund_pending", subscription_id=None,
                raw_json=raw, now=now_ts,
            )
            return Outcome("refund", user_id, charge_id, code=code)
        superseded = None
        if existing is not None:
            store.sub_update(existing["id"], now_ts, status="superseded", auto_renew=0)
            ent.revoke_subscription(store, existing["id"], "superseded", now)
            superseded = existing["first_charge_id"]
        end = _period_end(now, expires_ts)
        sub_id = store.sub_create(
            user_id=user_id, plan=code, payload=payload, first_charge=charge_id,
            start=now_ts, end=to_ts(end), now=now_ts,
        )
        ent.grant_subscription(
            store, user_id=user_id, plan=code, subscription_id=sub_id, charge_id=charge_id,
            start=now, end=end, now=now,
        )
        store.payment_add(
            charge_id=charge_id, user_id=user_id, invoice_id=invoice["invoice_id"], product=code,
            stars=stars, recurring=recurring, source=source, status="applied", subscription_id=sub_id,
            raw_json=raw, now=now_ts,
        )
        store.invoice_set_status(invoice["invoice_id"], "paid", now_ts)
        return Outcome("subscribed", user_id, charge_id, code=code, subscription_id=sub_id, superseded_charge=superseded)


def _period_end(start: datetime, expires_ts: int | None) -> datetime:
    if expires_ts is not None:
        return from_ts(expires_ts)
    return start + PERIOD


def _apply_renewal(store: Store, user_id: int, charge_id: str, payload: str, stars: int,
                   expires_ts: int | None, source: str, raw: str, now: datetime) -> Outcome:
    now_ts = to_ts(now)
    sub = store.sub_by_payload(payload)
    if sub is None or sub["user_id"] != user_id:
        sub = store.sub_active(user_id)
    if sub is None:
        store.payment_add(
            charge_id=charge_id, user_id=user_id, invoice_id=None, product=None, stars=stars,
            recurring=True, source=source, status="failed_apply", subscription_id=None, raw_json=raw, now=now_ts,
        )
        return Outcome("failed", user_id, charge_id)
    previous_end = from_ts(sub["period_end"])
    start = previous_end if previous_end >= now - GRACE else now
    end = _period_end(start, expires_ts)
    if store.sub_active(user_id) is not None and sub["status"] != "active":
        store.payment_add(
            charge_id=charge_id, user_id=user_id, invoice_id=None, product=sub["plan"], stars=stars,
            recurring=True, source=source, status="refund_pending", subscription_id=sub["id"], raw_json=raw, now=now_ts,
        )
        return Outcome("refund", user_id, charge_id, code=sub["plan"])
    store.sub_update(sub["id"], now_ts, status="active", auto_renew=1, period_start=to_ts(start), period_end=to_ts(end))
    ent.grant_subscription(
        store, user_id=user_id, plan=sub["plan"], subscription_id=sub["id"], charge_id=charge_id,
        start=start, end=end, now=now,
    )
    store.payment_add(
        charge_id=charge_id, user_id=user_id, invoice_id=None, product=sub["plan"], stars=stars,
        recurring=True, source=source, status="applied", subscription_id=sub["id"], raw_json=raw, now=now_ts,
    )
    store.event_add(sub["id"], "renewed", source, charge_id, raw, now_ts)
    return Outcome("renewed", user_id, charge_id, code=sub["plan"], subscription_id=sub["id"])


def apply_refund(store: Store, charge_id: str, now: datetime) -> bool:
    with store.transaction():
        payment = store.payment_get(charge_id)
        if payment is None or payment["status"] == "refunded":
            return False
        store.payment_set_status(charge_id, "refunded", to_ts(now))
        ent.revoke_payment(store, charge_id, now)
        sub = store.sub_by_charge(charge_id)
        if sub is not None:
            store.sub_update(sub["id"], to_ts(now), status="refunded", auto_renew=0)
            ent.revoke_subscription(store, sub["id"], "refund_revoke", now)
        return True


async def _cancel_renewal(bot: Bot, user_id: int, charge_id: str) -> None:
    try:
        await bot.edit_user_star_subscription(user_id=user_id, telegram_payment_charge_id=charge_id, is_canceled=True)
    except TelegramAPIError as exc:
        logger.error("cancel renewal failed: %s", type(exc).__name__)


async def refund_charge(bot: Bot, store: Store, charge_id: str, now: datetime) -> str:
    payment = store.payment_get(charge_id)
    if payment is None:
        return "Платёж не найден."
    if payment["status"] == "refunded":
        return "Платёж уже возвращён."
    user_id = payment["user_id"]
    try:
        await bot.refund_star_payment(user_id=user_id, telegram_payment_charge_id=charge_id)
    except TelegramAPIError as exc:
        if "CHARGE_ALREADY_REFUNDED" not in str(exc):
            logger.error("refund failed: %s", type(exc).__name__)
            return "Telegram не принял возврат. Проверьте платёж позже."
    apply_refund(store, charge_id, now)
    sub = store.sub_by_charge(charge_id)
    if sub is not None:
        await _cancel_renewal(bot, user_id, charge_id)
    return "Возврат выполнен, доступ отозван."


async def handle_refunded_payment(bot: Bot, store: Store, user_id: int, charge_id: str, now: datetime) -> None:
    if not apply_refund(store, charge_id, now):
        return
    if store.sub_by_charge(charge_id) is not None:
        await _cancel_renewal(bot, user_id, charge_id)
    await _notify(bot, user_id, billing_ui.refund_text())


async def handle_successful_payment(message: Message, bot: Bot, store: Store, now: datetime) -> None:
    payment = message.successful_payment
    user_id = message.from_user.id
    raw = _payment_raw(
        charge=payment.telegram_payment_charge_id, total=payment.total_amount, recurring=payment.is_recurring,
        first=payment.is_first_recurring, expires=payment.subscription_expiration_date,
    )
    outcome = apply_payment(
        store, user_id=user_id, charge_id=payment.telegram_payment_charge_id, payload=payment.invoice_payload,
        stars=payment.total_amount, recurring=bool(payment.is_recurring),
        first_recurring=bool(payment.is_first_recurring),
        expires_ts=payment.subscription_expiration_date, source="update", raw=raw, now=now,
    )
    await _settle(bot, store, outcome, now, reply_to=message)


async def _settle(bot: Bot, store: Store, outcome: Outcome, now: datetime, reply_to: Message | None = None) -> None:
    if outcome.superseded_charge:
        await _cancel_renewal(bot, outcome.user_id, outcome.superseded_charge)
    if outcome.kind == "refund":
        await _refund_duplicate(bot, store, outcome, now)
        return
    if reply_to is None:
        return
    if outcome.kind in ("subscribed", "applied"):
        status = ent.status_for(store, outcome.user_id, now)
        await reply_to.answer(billing_ui.success_text(outcome.code, status, now))
    elif outcome.kind == "failed":
        await reply_to.answer(billing_ui.TEXT_RECEIVED)
    elif outcome.kind == "duplicate":
        await reply_to.answer(billing_ui.TEXT_ALREADY_PAID)


async def _refund_duplicate(bot: Bot, store: Store, outcome: Outcome, now: datetime) -> None:
    try:
        await bot.refund_star_payment(user_id=outcome.user_id, telegram_payment_charge_id=outcome.charge_id)
    except TelegramAPIError as exc:
        logger.error("duplicate refund failed: %s", type(exc).__name__)
        return
    store.payment_set_status(outcome.charge_id, "refunded", to_ts(now))
    await _notify(bot, outcome.user_id, billing_ui.TEXT_DUPLICATE_REFUNDED)


async def handle_pre_checkout(bot: Bot, store: Store, query_id: str, user_id: int, payload: str, now: datetime) -> None:
    error = precheckout_error(store, user_id, payload, now)
    if error is None:
        await bot.answer_pre_checkout_query(query_id, ok=True)
    else:
        await bot.answer_pre_checkout_query(query_id, ok=False, error_message=error)


async def handle_subscription_update(bot: Bot, store: Store, user_id: int, payload: str, state: str, now: datetime) -> None:
    sub = store.sub_by_payload(payload) or store.sub_active(user_id)
    if sub is None:
        return
    ref = f"{sub['id']}:{state}:{to_ts(now) // 60}"
    if not store.event_add(sub["id"], state, "update", ref, _payment_raw(state=state), to_ts(now)):
        return
    if state == "canceled":
        store.sub_update(sub["id"], to_ts(now), auto_renew=0)
    elif state == "active" and sub["status"] == "active":
        store.sub_update(sub["id"], to_ts(now), auto_renew=1)
    elif state == "failed":
        until = from_ts(sub["period_end"])
        await _notify(
            bot, user_id, billing_ui.notice_text("failed", sub["plan"], until),
            reply_markup=billing_ui.notice_markup("failed"),
        )


async def reconcile(bot: Bot, store: Store, now: datetime, only_user: int | None = None) -> list[Outcome]:
    outcomes: list[Outcome] = []
    offset = 0
    for _ in range(RECONCILE_MAX_PAGES):
        page = await bot.get_star_transactions(offset=offset, limit=RECONCILE_PAGE)
        items = page.transactions
        for tx in items:
            source = tx.source
            if not isinstance(source, TransactionPartnerUser) or tx.amount <= 0:
                continue
            if not (source.invoice_payload or "").startswith("inv:"):
                continue
            if only_user is not None and source.user.id != only_user:
                continue
            if store.payment_get(tx.id) is not None:
                continue
            payload = source.invoice_payload
            outcome = apply_payment(
                store, user_id=source.user.id, charge_id=tx.id, payload=payload, stars=tx.amount,
                recurring=source.subscription_period is not None,
                first_recurring=store.sub_by_payload(payload) is None,
                expires_ts=None, source="reconcile", raw=_payment_raw(charge=tx.id, amount=tx.amount), now=now,
            )
            await _settle(bot, store, outcome, now)
            if outcome.kind in ("subscribed", "applied"):
                status = ent.status_for(store, outcome.user_id, now)
                await _notify(bot, outcome.user_id, billing_ui.success_text(outcome.code, status, now))
            outcomes.append(outcome)
        if len(items) < RECONCILE_PAGE:
            break
        offset += len(items)
    return outcomes


async def start_purchase(message: Message, store: Store, user_id: int, code: str, now: datetime, accepted: bool) -> None:
    refusal = purchase_refusal(store, user_id, code)
    if refusal is not None:
        await message.answer(refusal)
        return
    if not accepted and store.terms_version(user_id) != TERMS_VERSION:
        active = store.sub_active(user_id)
        upgrading = active is not None and active["plan"] == "start" and code == "pro"
        await message.answer(billing_ui.consent_text(code, upgrading), reply_markup=billing_ui.consent_markup(code))
        return
    product = PRODUCTS[code]
    _, payload = create_invoice(store, user_id, code, now)
    description = (
        "Подписка на 30 дней, продлевается автоматически" if product.kind == SUBSCRIPTION else "Разовый пакет, действует 90 дней"
    )
    prices = [LabeledPrice(label=product.title, amount=product.stars)]
    if product.kind == SUBSCRIPTION:
        link = await message.bot.create_invoice_link(
            title=product.title, description=description, payload=payload, currency=CURRENCY,
            prices=prices, subscription_period=PERIOD_SECONDS,
        )
        await message.answer(billing_ui.invoice_text(code), reply_markup=billing_ui.invoice_markup(link, product.stars))
        return
    await message.bot.send_invoice(
        chat_id=message.chat.id, title=product.title, description=description, payload=payload,
        currency=CURRENCY, prices=prices,
    )
    await message.answer(billing_ui.pack_waiting_text(code), reply_markup=billing_ui.invoice_markup(None, product.stars))


async def check_payment(message: Message, bot: Bot, store: Store, user_id: int, now: datetime) -> None:
    outcomes = await reconcile(bot, store, now, only_user=user_id)
    applied = [item for item in outcomes if item.kind in ("subscribed", "applied", "renewed")]
    if applied:
        status = ent.status_for(store, user_id, now)
        await message.answer(billing_ui.success_text(applied[-1].code, status, now))
        return
    await message.answer(billing_ui.TEXT_NOT_FOUND)


async def set_renewal(bot: Bot, store: Store, user_id: int, enabled: bool, now: datetime) -> str:
    sub = store.sub_active(user_id)
    if sub is None:
        return billing_ui.TEXT_NO_SUBSCRIPTION
    try:
        await bot.edit_user_star_subscription(
            user_id=user_id, telegram_payment_charge_id=sub["first_charge_id"], is_canceled=not enabled,
        )
    except TelegramAPIError as exc:
        logger.error("renewal toggle failed: %s", type(exc).__name__)
        return "Не удалось изменить продление. Попробуйте позже."
    store.sub_update(sub["id"], to_ts(now), auto_renew=int(enabled))
    return billing_ui.renew_text(enabled, from_ts(sub["period_end"]))


async def maintenance_notice(bot: Bot, notice: ent.Notice) -> None:
    text = billing_ui.notice_text(notice.kind, notice.plan, notice.until)
    markup = billing_ui.notice_markup(notice.kind)
    await _notify(bot, notice.user_id, text, reply_markup=markup)


async def _notify(bot: Bot, user_id: int, text: str, reply_markup=None) -> None:
    try:
        await bot.send_message(user_id, text, reply_markup=reply_markup)
    except TelegramAPIError as exc:
        logger.error("notify failed: %s", type(exc).__name__)
