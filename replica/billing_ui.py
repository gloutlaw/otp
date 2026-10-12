from datetime import datetime

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from replica.billing.entitlements import Status
from replica.catalog import DEFAULT_CHAT_MODEL, PLAN_TITLES, PRODUCTS, TRIAL_SMART
from replica.quota import LOCAL_TZ, duration_text, reset_text
from replica.screens import (
    PURCHASE_PRO_LABEL,
    PURCHASE_START_LABEL,
    RENEW_OFF_LABEL,
    RENEW_ON_LABEL,
)

WINDOW_TITLES = {
    "day_local": "сегодня",
    "week_of_period": "на эту неделю",
    "period": "за период",
    "cycle30": "за 30 дней",
}

TEXT_TOO_LONG = "Сообщение длиннее 2000 символов. Сократите его или разбейте на части."
TEXT_BUSY = "Не получилось получить ответ — сервис сейчас перегружен. Запрос не списан. Попробуйте ещё раз."
TEXT_UNAVAILABLE = "Сервис временно недоступен, мы уже разбираемся. Запрос не списан."
TEXT_FAILED = "Не получилось получить ответ. Запрос не списан. Попробуйте ещё раз."
TEXT_TRUNCATED = "…(ответ сокращён)"
TEXT_BLOCKED = "Доступ к боту ограничен. Вопросы — /support."
TEXT_TECH = "Не удалось подтвердить заказ. Звёзды не списаны. Попробуйте через минуту."
TEXT_DUPLICATE_REFUNDED = "Этот платёж не применён: доступ уже активен. Звёзды возвращены."
TEXT_RECEIVED = "Оплата получена, активируем доступ. Если он не появится в течение минуты — напишите в /paysupport."
TEXT_ALREADY_PAID = "Оплата уже учтена, доступ активен."
TEXT_STALE = "Этот счёт устарел. Откройте «Тарифы» и создайте новый."
TEXT_PRO_BLOCKS_START = "У вас активен Pro. Старт не продаётся поверх Pro."
TEXT_NOT_FOUND = (
    "Пока не вижу оплату. Если звёзды списались — напишите в /paysupport, мы проверим вручную. "
    "Telegram и @BotSupport с покупками в этом боте помочь не смогут."
)
TEXT_CANCELLED = "Оплата отменена. Открыть тарифы можно в главном меню."
TEXT_NO_SUBSCRIPTION = "Активной подписки нет."
TEXT_SMART_ALREADY = "Умный ответ уже выше 👆"
TEXT_ALREADY_THINKING = "Уже думаю…"
TEXT_ALREADY_DONE = "Уже сделано выше 👆"
TEXT_NOTHING_TO_REVISE = "Пока нечего менять."
TEXT_TRUNCATED = "…(ответ сокращён)"


def _date(moment: datetime) -> str:
    return f"{moment.astimezone(LOCAL_TZ):%d.%m.%Y}"


def title_of(code: str) -> str:
    return PRODUCTS[code].title


def _plan_title(status: Status) -> str:
    return PLAN_TITLES.get(status.tier, status.tier)


def _dotted(text: str) -> str:
    return " ".join(word if word.isdigit() or word.endswith(".") else f"{word}." for word in text.split(" "))


def _clock(moment: datetime) -> str:
    local = moment.astimezone(LOCAL_TZ)
    return f"{local:%d.%m} в {local:%H:%M}"


def _tree(rows: list[str]) -> list[str]:
    if not rows:
        return []
    if len(rows) == 1:
        return [f"└ {rows[0]}"]
    return [f"├ {rows[0]}", *[f"├ {row}" for row in rows[1:-1]], f"└ {rows[-1]}"]


def _block(title: str, rows: list[str]) -> str:
    return "\n".join([title, *_tree(rows)])


def _update_detail(moment: datetime, now: datetime) -> str:
    span = _dotted(duration_text(moment - now))
    return f"🔄 Обновление через {span} — {_clock(moment)} (UTC+3)"


def _cheap_rows(status: Status, now: datetime, *, bullet: bool) -> list[str]:
    line = status.cheap
    if line is None:
        return []
    amount = f"{line.remaining} из {line.quota}"
    rows = [f"Доступно: {amount}" if bullet else f"Осталось: **{amount}**"]
    if line.resets_at is None:
        return rows
    if bullet:
        rows.append(f"Обновление: {_clock(line.resets_at)} (UTC+3)")
    else:
        rows.append(_update_detail(line.resets_at, now))
    return rows


def _smart_rows(status: Status, trial_total: int, *, bullet: bool) -> list[str]:
    if status.smart is not None:
        line = status.smart
        available = f"{line.remaining} из {line.quota}"
        rows = [f"Доступно: {available}" if bullet else f"Осталось: **{available}**"]
        if line.resets_at is not None:
            rows.append(f"Действуют до {_date(line.resets_at)}" if bullet else f"📅 Действуют до {_date(line.resets_at)}")
        elif when := WINDOW_TITLES.get(line.window, ""):
            rows.append(when)
        return rows
    if status.tier == "free":
        available = f"{status.trial_left} из {trial_total}"
        left = f"Доступно: {available}" if bullet else f"Осталось: **{available}**"
        return [left, "пробные"]
    return []


def _pack_blocks(status: Status, *, bullet: bool) -> list[str]:
    blocks = []
    for pack in status.packs:
        left = f"Доступно: {pack.remaining}" if bullet else f"Осталось: **{pack.remaining}**"
        rows = [left]
        if pack.until is not None:
            until = f"Действует до {_date(pack.until)}"
            rows.append(until if bullet else f"📅 {until}")
        title = "📦 **Пакет «+100 умных»**"
        if bullet:
            blocks.append("\n".join([title, *[f"• {row}" for row in rows]]))
        else:
            blocks.append(_block(title, rows))
    return blocks


def status_text(status: Status, now: datetime, trial_total: int = TRIAL_SMART) -> str:
    head = [f"💎 **Ваш тариф: {_plan_title(status)}**"]
    if status.subscribed:
        head.append("продление включено" if status.auto_renew else "продление выключено")
    blocks = ["\n".join(head)]
    if cheap := _cheap_rows(status, now, bullet=False):
        blocks.append(_block("📊 **Обычные ответы**", cheap))
    if smart := _smart_rows(status, trial_total, bullet=False):
        blocks.append(_block("🧠 **Умные ответы**", smart))
    blocks.extend(_pack_blocks(status, bullet=False))
    if status.packs or status.smart is not None:
        blocks.append("ℹ️ Сначала расходуются запросы, включённые в тариф, затем — из дополнительных пакетов.")
    return "\n\n".join(blocks)


def limits_text(status: Status, now: datetime, trial_total: int = TRIAL_SMART) -> str:
    blocks = [
        "📊 **Ваши лимиты**",
        f"💎 Текущий тариф: **{_plan_title(status)}**",
    ]
    if cheap := _cheap_rows(status, now, bullet=True):
        blocks.append("\n".join(["💬 **Обычные ответы**", *[f"• {row}" for row in cheap]]))
    if smart := _smart_rows(status, trial_total, bullet=True):
        blocks.append("\n".join(["🧠 **Умные ответы**", *[f"• {row}" for row in smart]]))
    blocks.extend(_pack_blocks(status, bullet=True))
    return "\n\n".join(blocks)


def hidden_labels(screen_id: str, status: Status) -> frozenset[str]:
    hidden: set[str] = set()
    if screen_id == "tariffs" and status.subscribed:
        if status.tier in ("start", "pro"):
            hidden.add(PURCHASE_START_LABEL)
        if status.tier == "pro":
            hidden.add(PURCHASE_PRO_LABEL)
    if screen_id == "profile":
        if not (status.subscribed and status.auto_renew):
            hidden.add(RENEW_OFF_LABEL)
        if not (status.subscribed and not status.auto_renew):
            hidden.add(RENEW_ON_LABEL)
    return frozenset(hidden)


def consent_text(code: str, upgrading_from_start: bool) -> str:
    text = (
        "Перед оплатой, пожалуйста, ознакомьтесь с условиями: /terms\n"
        "Коротко: оплата в Telegram Stars; подписка продлевается каждые 30 дней; "
        "возвраты — по правилам в /terms; вопросы по оплате — /paysupport."
    )
    if upgrading_from_start and code == "pro":
        text += "\n\nПри переходе со Старта его остаток сгорает, а продление Старта отменяется."
    return text


def consent_markup(code: str) -> InlineKeyboardMarkup:
    return _markup(
        [
            [("Принимаю и перехожу к оплате", f"p:accept:{code}", "success")],
            [("Условия полностью", "p:terms", "primary")],
            [("Отмена", "p:cancel", "danger")],
        ]
    )


def invoice_text(code: str) -> str:
    product = PRODUCTS[code]
    return (
        f"Счёт на «{product.title}» за {product.stars} ⭐ готов. Нажмите «Оплатить» ниже.\n"
        "После оплаты доступ включится автоматически — обычно за несколько секунд."
    )


def pack_waiting_text(code: str) -> str:
    product = PRODUCTS[code]
    return (
        f"Счёт на «{product.title}» за {product.stars} ⭐ отправлен выше. "
        "После оплаты доступ включится автоматически — обычно за несколько секунд."
    )


def invoice_markup(link: str | None, stars: int) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if link is not None:
        rows.append([InlineKeyboardButton(text=f"Оплатить {stars} ⭐", url=link, style="success")])
    rows.append([InlineKeyboardButton(text="Проверить оплату", callback_data="p:check", style="primary")])
    rows.append([InlineKeyboardButton(text="Отмена", callback_data="p:cancel", style="danger")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def success_text(code: str, status: Status, now: datetime) -> str:
    if code == "pack_smart_100":
        pack = status.packs[-1] if status.packs else None
        until = f", действует до {_date(pack.until)}" if pack and pack.until else ""
        remaining = sum(item.remaining for item in status.packs)
        return f"Пакет «+100 умных» добавлен{until}. Умных ответов сейчас в пакетах: {remaining}."
    product = PRODUCTS[code]
    until = _date(status.tier_until) if status.tier_until else ""
    return (
        f"Готово! Тариф {product.title} активен до {until}.\n"
        "Подписка продлится автоматически. Отменить продление можно в разделе «Профиль»."
    )


def duplicate_text(code: str) -> str:
    return f"У вас уже есть активный {title_of(code)}. Повторная оплата не нужна."


def paywall_text() -> str:
    return (
        "«Ответить умнее» использует более сильную модель — она лучше справляется со сложными вопросами, "
        "длинными текстами и рассуждениями.\n"
        "Доступно в Pro (150 за период), в Старте (20) или пакетом «+100 умных» за 219 ⭐."
    )


def model_locked_text(title: str) -> str:
    return (
        f"Модель «{title}» тратит умный запрос и доступна в Pro (150 за период). "
        f"Обычные ответы пока остаются на {DEFAULT_CHAT_MODEL.title}."
    )


def paywall_markup() -> InlineKeyboardMarkup:
    return _markup(
        [[("⭐ Тарифы", "p:tariffs", "primary"), ("📦 Пакет", "p:packs", "primary")], [("Не сейчас", "p:dismiss", None)]]
    )


def denied_text(kind: str, free: bool, resets_at: datetime | None, quota: int, remaining: int, now: datetime) -> str:
    if kind == "smart":
        when = f"Обновятся {_date(resets_at)}" if resets_at else "Пакеты не закончились"
        return f"Умные ответы в этом периоде закончились. {when} или можно докупить пакет «+100 умных» за 219 ⭐."
    if free:
        reset = reset_text(resets_at, now) if resets_at else ""
        return (
            f"На сегодня бесплатные ответы закончились ({quota} из {quota}). Новые появятся {reset}. "
            "Нужно больше уже сейчас? Тариф «Старт» — 400 ответов в неделю за 300 ⭐."
        )
    reset = reset_text(resets_at, now) if resets_at else ""
    return f"Лимит обычных ответов на эту неделю исчерпан ({quota} из {quota}). Обновится {reset}."


def denied_markup(kind: str, free: bool) -> InlineKeyboardMarkup:
    if kind == "smart":
        return _markup([[("📦 Пакет", "p:packs", "primary")], [("Ок", "p:dismiss", None)]])
    if free:
        return _markup([[("⭐ Тарифы", "p:tariffs", "primary")], [("Ок, подожду", "p:dismiss", None)]])
    return _markup([[("Ок", "p:dismiss", None)]])


def renew_text(enabled: bool, until: datetime | None) -> str:
    if enabled:
        return "Продление включено."
    if until is None:
        return TEXT_NO_SUBSCRIPTION
    return f"Продление выключено. Доступ сохранится до {_date(until)}."


def notice_text(kind: str, plan: str, until: datetime) -> str:
    title = PLAN_TITLES.get(plan, plan)
    if kind == "reminder":
        return (
            f"{title} действует до {_date(until)}. Продление выключено — после этой даты вы перейдёте на бесплатный тариф."
        )
    if kind == "failed":
        return f"Не удалось продлить {title} — на балансе недостаточно звёзд. Доступ сохраняется до {_date(until)}."
    return f"{title} закончился, вы на бесплатном тарифе: 10 ответов в день. Пакеты, если есть, продолжают работать."


def notice_markup(kind: str) -> InlineKeyboardMarkup | None:
    if kind == "reminder":
        return _markup([[("Включить продление", "p:renew:on", "success")]])
    if kind == "failed":
        return _markup([[("⭐ Тарифы", "p:tariffs", "primary")]])
    return None


def refund_text() -> str:
    return "Платёж возвращён. Доступ, полученный им, отозван."


def _markup(rows: list[list[tuple[str, str, str | None]]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=label, callback_data=data, style=style) for label, data, style in row]
            for row in rows
        ]
    )

