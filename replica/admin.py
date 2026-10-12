from datetime import datetime, timedelta

from aiogram.types import Message

from replica import billing_ui, payments
from replica.billing import entitlements as ent
from replica.catalog import PLAN_TITLES, STAR_USD
from replica.quota import LOCAL_TZ, to_ts
from replica.store import Store

ADMIN_COMMANDS = frozenset(
    {"/user", "/grant", "/revoke", "/addpack", "/payments", "/refund", "/block", "/unblock", "/stats", "/costs", "/anomalies"}
)
GRANT_PLANS = ("free", "start", "pro")
GRANT_USAGE_TEXT = "Формат: /grant <id> <free|start|pro> [дни]"
USAGE_TEXT = (
    "Команды: /user <id>, /grant <id> <free|start|pro> [дни], /revoke <id>, /addpack <id> smart <N> [дни], "
    "/payments <id>, /refund <charge_id> [yes], /block <id>, /unblock <id>, /stats, /costs [дни], /anomalies"
)
ANOMALY_SHARE = 0.5
ANOMALY_HOURLY_REQUESTS = 100


def _parse_id(value: str) -> int | None:
    return int(value) if value.isdigit() else None


async def handle_admin(message: Message, text: str, user_id: int, store: Store, owner_id: int | None, now: datetime) -> None:
    if owner_id is None or user_id != owner_id:
        return
    parts = text.split()
    command = parts[0].split("@", 1)[0]
    args = parts[1:]
    actor = owner_id
    stamp = to_ts(now)
    if command == "/user":
        target = _parse_id(args[0]) if args else None
        if target is None:
            await message.answer("Формат: /user <id>")
            return
        await message.answer(_user_card(store, target, now))
        return
    if command == "/grant":
        await _grant(message, store, args, actor, now)
        return
    if command == "/revoke":
        target = _parse_id(args[0]) if args else None
        if target is None:
            await message.answer("Формат: /revoke <id>")
            return
        ent.grant_admin(store, user_id=target, plan="free", now=now, days=None, actor=str(actor))
        store.audit_add(actor, "revoke", target, {}, stamp)
        await message.answer(f"Админ-тариф снят: {target}")
        return
    if command == "/addpack":
        await _addpack(message, store, args, actor, now)
        return
    if command == "/payments":
        target = _parse_id(args[0]) if args else None
        if target is None:
            await message.answer("Формат: /payments <id>")
            return
        await message.answer(_payments_text(store, target))
        return
    if command == "/refund":
        await _refund(message, store, args, actor, now)
        return
    if command in ("/block", "/unblock"):
        target = _parse_id(args[0]) if args else None
        if target is None:
            await message.answer(f"Формат: {command} <id>")
            return
        status = "blocked" if command == "/block" else "active"
        store.set_user_status(target, status, stamp)
        store.audit_add(actor, command[1:], target, {}, stamp)
        await message.answer(f"Статус {target}: {status}")
        return
    if command == "/stats":
        await message.answer(_stats_text(store, now))
        return
    if command == "/costs":
        days = int(args[0]) if args and args[0].isdigit() else 30
        await message.answer(_costs_text(store, now, days))
        return
    if command == "/anomalies":
        await message.answer(_anomalies_text(store, now))
        return
    await message.answer(USAGE_TEXT)


async def _grant(message: Message, store: Store, args: list[str], actor: int, now: datetime) -> None:
    if len(args) not in (2, 3) or _parse_id(args[0]) is None or args[1] not in GRANT_PLANS:
        await message.answer(GRANT_USAGE_TEXT)
        return
    if len(args) == 3 and not (args[2].isdigit() and int(args[2]) > 0):
        await message.answer(GRANT_USAGE_TEXT)
        return
    target = int(args[0])
    days = int(args[2]) if len(args) == 3 else None
    ent.grant_admin(store, user_id=target, plan=args[1], now=now, days=days, actor=str(actor))
    store.audit_add(actor, "grant", target, {"plan": args[1], "days": days}, to_ts(now))
    term = f" на {days} дн" if days else " без срока"
    await message.answer(f"Тариф {PLAN_TITLES[args[1]]} выдан: {target}{term}")


async def _addpack(message: Message, store: Store, args: list[str], actor: int, now: datetime) -> None:
    if len(args) < 3 or _parse_id(args[0]) is None or args[1] != "smart" or not args[2].isdigit():
        await message.answer("Формат: /addpack <id> smart <N> [дни]")
        return
    days = int(args[3]) if len(args) > 3 and args[3].isdigit() else 90
    target = int(args[0])
    ent.grant_admin_pack(store, user_id=target, smart=int(args[2]), days=days, now=now)
    store.audit_add(actor, "addpack", target, {"smart": int(args[2]), "days": days}, to_ts(now))
    await message.answer(f"Пакет выдан: {target}, умных {args[2]} на {days} дн")


async def _refund(message: Message, store: Store, args: list[str], actor: int, now: datetime) -> None:
    if not args:
        await message.answer("Формат: /refund <charge_id> [yes]")
        return
    charge_id = args[0]
    payment = store.payment_get(charge_id)
    if payment is None:
        await message.answer("Платёж не найден.")
        return
    if len(args) < 2 or args[1] != "yes":
        await message.answer(
            f"Вернуть {payment['stars']} ⭐ пользователю {payment['user_id']}? Подтвердите: /refund {charge_id} yes"
        )
        return
    result = await payments.refund_charge(message.bot, store, charge_id, now)
    store.audit_add(actor, "refund", payment["user_id"], {"stars": payment["stars"]}, to_ts(now))
    await message.answer(result)


def _user_card(store: Store, target: int, now: datetime) -> str:
    status = ent.status_for(store, target, now)
    count, cost, _, _ = store.usage_totals(target)
    lines = [
        f"Пользователь {target}",
        f"Статус: {store.user_status(target)}",
        billing_ui.status_text(status, now),
        f"Запросов всего: {count}, расход: ${cost:.4f}",
    ]
    return "\n".join(lines)


def _payments_text(store: Store, target: int) -> str:
    rows = store.payments_recent(target, 10)
    if not rows:
        return "Платежей нет."
    lines = []
    for row in rows:
        when = datetime.fromtimestamp(row["received_at"], LOCAL_TZ).strftime("%d.%m %H:%M")
        lines.append(f"{when} · {row['stars']} ⭐ · {row['product_code'] or '—'} · {row['status']} · {row['telegram_payment_charge_id']}")
    return "\n".join(lines)


def _stats_text(store: Store, now: datetime) -> str:
    since7 = to_ts(now - timedelta(days=7))
    since30 = to_ts(now - timedelta(days=30))
    week = store.payments_since(since7)
    month = store.payments_since(since30)
    refunds_week = sum(1 for row in week if row["status"] == "refunded")
    refunds_month = sum(1 for row in month if row["status"] == "refunded")
    applied_week = [row for row in week if row["status"] in ("applied", "refunded")]
    applied_month = [row for row in month if row["status"] in ("applied", "refunded")]
    return "\n".join(
        [
            f"Пользователей: {len(store.user_ids())}",
            f"7 дн: оплат {len(applied_week)}, {sum(row['stars'] for row in applied_week)} ⭐, возвратов {refunds_week}",
            f"30 дн: оплат {len(applied_month)}, {sum(row['stars'] for row in applied_month)} ⭐, возвратов {refunds_month}",
        ]
    )


def _costs_text(store: Store, now: datetime, days: int) -> str:
    since = to_ts(now - timedelta(days=days))
    models = store.cost_by_model(since)
    cost = sum(row["cost"] for row in models)
    requests = sum(row["requests"] for row in models)
    stars = sum(row["stars"] for row in _stars_by_user(store, since).values())
    revenue = stars * STAR_USD
    lines = [f"За {days} дн: запросов {requests}, расход ${cost:.4f}, выручка ${revenue:.2f} ({stars} ⭐)"]
    for row in models:
        lines.append(f"{row['model_id'] or 'нет модели'}: {row['requests']} зап., ${row['cost']:.4f}")
    if revenue > 0:
        lines.append(f"Маржа по звёздам: {(revenue - cost) / revenue:.0%}")
    return "\n".join(lines)


def _stars_by_user(store: Store, since: int) -> dict[int, dict[str, int]]:
    result: dict[int, dict[str, int]] = {}
    for row in store.payments_since(since):
        if row["status"] not in ("applied", "refunded"):
            continue
        bucket = result.setdefault(row["user_id"], {"stars": 0})
        bucket["stars"] += row["stars"]
    return result


def _anomalies_text(store: Store, now: datetime) -> str:
    since30 = to_ts(now - timedelta(days=30))
    since_hour = to_ts(now - timedelta(hours=1))
    stars = _stars_by_user(store, since30)
    flagged: list[str] = []
    for row in store.cost_by_user(since30):
        revenue = stars.get(row["user_id"], {"stars": 0})["stars"] * STAR_USD
        if revenue > 0 and row["cost"] > revenue * ANOMALY_SHARE:
            flagged.append(f"{row['user_id']}: расход ${row['cost']:.4f} при выручке ${revenue:.2f}")
    for row in store.cost_by_user(since_hour):
        if row["requests"] > ANOMALY_HOURLY_REQUESTS:
            flagged.append(f"{row['user_id']}: {row['requests']} запросов за час")
    return "\n".join(flagged) if flagged else "Аномалий нет."
