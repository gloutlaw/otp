import asyncio
import logging
import time
from datetime import datetime
from html import escape

from aiogram import F, Bot, Dispatcher
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import (
    BotCommand,
    BotSubscriptionUpdated,
    CallbackQuery,
    ErrorEvent,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
    PreCheckoutQuery,
)

from replica import admin, billing_ui, payments
from replica.access import owner_of
from replica.billing import entitlements as ent
from replica.billing.entitlements import Denied, Existing, Reserved
from replica.catalog import (
    DEFAULT_CHAT_MODEL,
    PREMIUM_TIER,
    PRODUCTS,
    TERMS_VERSION,
    ChatModel,
    chat_model_by_slug,
)
from replica.chat import (
    CHAT_MODEL_ID,
    CONTINUE,
    HISTORY_MESSAGES,
    IMPROVE,
    SHORTEN,
    SMART_MODEL_ID,
    Budget,
    ChatError,
    Completion,
    ConfigError,
    RetryableError,
    append_turn,
    budget_for,
    build_messages,
    clip_reply,
    complete,
    fit_history,
    trim_history,
)
from replica.descriptions import DESCRIPTIONS, SHORT_DESCRIPTIONS
from replica.live import watch_code
from replica.machine import Back, Choose, Closed, Next, model_changed_text, normalize_action, step
from replica.quota import LOCAL_TZ
from replica.screens import (
    ACTION_CALLBACKS,
    HISTORY_PLACEHOLDER,
    body_buttons,
    buttons,
    render,
    to_html,
)
from replica.store import Store

logger = logging.getLogger(__name__)

RATE_LIMITED_TEXT = "Слишком много запросов. Подождите минуту и попробуйте снова."
UNAVAILABLE_TEXT = "Пока недоступно."
TOO_LONG_TEXT = billing_ui.TEXT_TOO_LONG
NOTHING_TO_REVISE_TEXT = billing_ui.TEXT_NOTHING_TO_REVISE
HISTORY_CLEARED_TEXT = "История очищена."
CLEAR_HISTORY_LABEL = "🗑 Очистить"
COLLAPSING_ACTIONS = frozenset({"a:home", "a:improve", "a:shorten", "a:smart", "a:cont"})
HISTORY_SHOWN = 10
HISTORY_QUESTION_CHARS = 120
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60.0
MAX_INPUT_CHARS = 2000
MAX_PATH_DEPTH = 20
SWEEP_INTERVAL_SECONDS = 60
RECONCILE_INTERVAL_SECONDS = 600
BOT_COMMANDS = (
    ("start", "Главное меню"),
    ("help", "Справка"),
    ("models", "Модели"),
    ("profile", "Профиль"),
    ("premium", "Тарифы"),
    ("limits", "Лимиты"),
    ("support", "Поддержка"),
    ("terms", "Условия"),
    ("paysupport", "Поддержка по оплате"),
)

_store: Store | None = globals().get("_store")
_owner_id: int | None = globals().get("_owner_id")


def configure(store: Store, owner_id: int | None) -> None:
    global _store, _owner_id
    _store = store
    _owner_id = owner_id


def _keep_dict(name: str) -> dict:
    existing = globals().get(name)
    if isinstance(existing, dict):
        return existing
    return {}


_chat_path: dict[int, list[str]] = _keep_dict("_chat_path")
_model_requests: dict[int, list[float]] = _keep_dict("_model_requests")


def _prune(store: dict[int, list[float]], window: float, current: float) -> None:
    stale = [key for key, times in store.items() if all(current - sent_at >= window for sent_at in times)]
    for key in stale:
        del store[key]


def within_rate_limit(chat_id: int, now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    _prune(_model_requests, RATE_LIMIT_WINDOW_SECONDS, current)
    recent = [
        sent_at for sent_at in _model_requests.get(chat_id, []) if current - sent_at < RATE_LIMIT_WINDOW_SECONDS
    ]
    allowed = len(recent) < RATE_LIMIT_REQUESTS
    if allowed:
        recent.append(current)
    if recent:
        _model_requests[chat_id] = recent
    return allowed


def serves_chat(chat_type: str | None) -> bool:
    return chat_type == "private"


def plan_status(user_id: int, now: datetime) -> str:
    return billing_ui.status_text(ent.status_for(_store, user_id, now), now)


def history_html(user_id: int) -> str:
    questions = _store.recent_questions(user_id, HISTORY_SHOWN)
    if not questions:
        return "Запросов пока нет."
    return "\n".join(f"• {escape(question[:HISTORY_QUESTION_CHARS])}" for question in questions)


def _button(label: str, callback_data: str, style: str | None = "primary") -> InlineKeyboardButton:
    return InlineKeyboardButton(text=label, callback_data=callback_data, style=style)


def keyboard(screen_id: str, hidden: frozenset[str] = frozenset()) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    pair: list[InlineKeyboardButton] = []
    body_count = len(body_buttons(screen_id))
    for index, button in enumerate(buttons(screen_id)):
        if button.label in hidden:
            continue
        if index == body_count and pair:
            rows.append(pair)
            pair = []
        data = ACTION_CALLBACKS.get(button.label, f"n:{screen_id}:{index}")
        markup = _button(button.label, data, button.style)
        if len(button.label) > 16:
            if pair:
                rows.append(pair)
                pair = []
            rows.append([markup])
            continue
        pair.append(markup)
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def clicked_button(data: str) -> tuple[str, str] | None:
    if not data.startswith("n:"):
        return None
    try:
        _, screen_id, index_text = data.split(":", 2)
        label = buttons(screen_id)[int(index_text)].label
    except (ValueError, IndexError, KeyError):
        return None
    return screen_id, label


def screen_after(screen_id: str, action: str) -> str:
    outcome = step(screen_id, action)
    if isinstance(outcome, Next):
        return outcome.screen_id
    return screen_id


def current_screen(chat_id: int) -> str:
    path = _chat_path.get(chat_id)
    return path[-1] if path else "start"


def remember(chat_id: int, screen_id: str) -> None:
    if screen_id == "start":
        _chat_path[chat_id] = ["start"]
        return
    path = _chat_path.setdefault(chat_id, ["start"])
    if path[-1] != screen_id:
        path.append(screen_id)
        del path[:-MAX_PATH_DEPTH]


def previous_screen(chat_id: int) -> str:
    path = _chat_path.setdefault(chat_id, ["start"])
    if len(path) > 1:
        path.pop()
    return path[-1]


async def show(message: Message, screen_id: str, bot_name: str, support_contact: str, user_id: int) -> None:
    now = datetime.now(LOCAL_TZ)
    remember(message.chat.id, screen_id)
    status = ent.status_for(_store, user_id, now)
    text = to_html(
        render(
            screen_id,
            bot_name=bot_name,
            support_contact=support_contact,
            status=billing_ui.limits_text(status, now) if screen_id == "limits" else billing_ui.status_text(status, now),
            history=HISTORY_PLACEHOLDER,
            model=effective_chat_model(user_id, now).title,
        )
    )
    if screen_id == "history":
        text = text.replace(HISTORY_PLACEHOLDER, history_html(user_id))
    markup = keyboard(screen_id, billing_ui.hidden_labels(screen_id, status))
    await message.answer(text, reply_markup=markup, parse_mode=ParseMode.HTML)


def answer_keyboard(expanded: bool = False, continuable: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if continuable:
        rows.append([_button("Продолжить", "a:cont", "success")])
    if not expanded:
        rows.append([_button("Инструменты", "a:tools")])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    rows.extend(
        [
            [_button("Улучшить", "a:improve", "success"), _button("Сократить", "a:shorten", "success")],
            [_button("Ответить умнее", "a:smart", "success"), _button("Главное меню", "a:home", None)],
            [_button("Скрыть", "a:hide", None)],
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def retry_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[_button("🔁 Повторить", "a:retry")]])


def _continuable(chat_id: int, message_id: int) -> bool:
    row = _store.get_answer(chat_id, message_id)
    return bool(row and row["truncated"])


async def set_answer_keyboard(message: Message, expanded: bool, continuable: bool = False) -> None:
    try:
        await message.edit_reply_markup(reply_markup=answer_keyboard(expanded, continuable))
    except TelegramBadRequest:
        return


async def alert_owner(bot: Bot, text: str) -> None:
    if _owner_id is None:
        return
    try:
        await bot.send_message(_owner_id, f"⚠️ {text}")
    except Exception as exc:
        logger.warning("owner alert failed: %s", type(exc).__name__)


async def _complete(api_key: str, model_id: str, messages: list[dict[str, str]], budget: Budget) -> Completion:
    try:
        return await asyncio.to_thread(complete, api_key, model_id, messages, budget)
    except RetryableError:
        return await asyncio.to_thread(complete, api_key, model_id, messages, budget)


async def _deny(message: Message, denied: Denied, user_id: int, now: datetime) -> None:
    if denied.reason == "blocked":
        await message.answer(billing_ui.TEXT_BLOCKED)
        return
    if denied.reason == "no_access":
        await message.answer(billing_ui.paywall_text(), reply_markup=billing_ui.paywall_markup())
        return
    status = ent.status_for(_store, user_id, now)
    free = status.tier == "free"
    quota = status.cheap.quota if status.cheap is not None else 0
    resets_at = denied.decision.resets_at if denied.decision is not None else None
    text = billing_ui.denied_text(denied.kind, free, resets_at, quota, 0, now)
    await message.answer(text, reply_markup=billing_ui.denied_markup(denied.kind, free))


async def _existing(message: Message, existing: Existing, key: str) -> None:
    if key.startswith("msg:"):
        return
    if existing.status == "reserved":
        await message.answer(billing_ui.TEXT_ALREADY_THINKING)
    elif key.split(":")[-1] == "smart":
        await message.answer(billing_ui.TEXT_SMART_ALREADY)
    else:
        await message.answer(billing_ui.TEXT_ALREADY_DONE)


async def reply_with(
    message: Message,
    bot_name: str,
    api_key: str,
    user_text: str,
    context: list[dict[str, str]] | None = None,
    *,
    user_id: int,
    smart: bool = False,
    mode: str | None = None,
    idem_key: str | None = None,
) -> None:
    chat_id = message.chat.id
    mode = mode or ("smart" if smart else ("chat" if context is None else "revise"))
    if not api_key:
        await alert_owner(message.bot, "Модель недоступна: не задан ключ провайдера")
        await message.answer(billing_ui.TEXT_UNAVAILABLE)
        return
    if len(user_text) > MAX_INPUT_CHARS:
        await message.answer(TOO_LONG_TEXT)
        return
    if not within_rate_limit(chat_id):
        await message.answer(RATE_LIMITED_TEXT)
        return
    now = datetime.now(LOCAL_TZ)
    stamp = int(now.timestamp())
    key = idem_key or f"msg:{chat_id}:{message.message_id}"
    choice = effective_chat_model(user_id, now)
    premium = mode in ("chat", "revise") and choice.tier == PREMIUM_TIER
    smart_route = mode == "smart" or premium
    result = ent.reserve(_store, user_id, "smart" if smart_route else "cheap", key, now)
    if isinstance(result, Existing):
        await _existing(message, result, key)
        return
    if isinstance(result, Denied):
        await _deny(message, result, user_id, now)
        return
    assert isinstance(result, Reserved)
    budget = budget_for("free" if result.source == "free" else "paid", smart_route)
    if mode == "smart":
        model_id = SMART_MODEL_ID
    elif mode in ("chat", "revise"):
        model_id = choice.slug
    else:
        model_id = CHAT_MODEL_ID
    if mode == "chat":
        base = _store.recent_turns(user_id, HISTORY_MESSAGES)
        seen = trim_history(base, budget.history_chars)
    else:
        base = list(context or [])
        seen = trim_history(base, budget.history_chars)
        if mode == "smart":
            seen = fit_history(bot_name, seen, user_text)
    messages = build_messages(bot_name, seen, user_text)
    await message.bot.send_chat_action(chat_id, ChatAction.TYPING)
    try:
        completion = await _complete(api_key, model_id, messages, budget)
    except ConfigError:
        ent.release(_store, key, "config")
        await alert_owner(message.bot, "Модель недоступна: провайдер отклонил запрос")
        await message.answer(billing_ui.TEXT_UNAVAILABLE)
        return
    except RetryableError:
        ent.release(_store, key, "overloaded")
        await _pending_error(message, user_id, billing_ui.TEXT_BUSY, base, mode, user_text, stamp)
        return
    except ChatError:
        ent.release(_store, key, "failed")
        await _pending_error(message, user_id, billing_ui.TEXT_FAILED, base, mode, user_text, stamp)
        return
    truncated = completion.finish_reason == "length"
    reply = clip_reply(completion.text)
    display = clip_reply(f"{reply}\n\n{billing_ui.TEXT_TRUNCATED}") if truncated else reply
    ent.commit(
        _store, key, cost=completion.cost_usd, prompt=completion.prompt_tokens,
        completion=completion.completion_tokens, reasoning=completion.reasoning_tokens,
        model_id=model_id, finish_reason=completion.finish_reason,
        latency_ms=completion.latency_ms, generation_id=completion.generation_id,
    )
    if mode == "chat":
        _store.add_turn(user_id, "user", user_text, stamp)
        _store.add_turn(user_id, "assistant", reply, stamp)
        thread = append_turn(seen, user_text, reply, budget.history_chars)
    elif mode == "smart":
        thread = append_turn(base, user_text, reply, budget.history_chars)
    else:
        thread = base[:-1] + [{"role": "assistant", "content": reply}]
    sent = await message.answer(display, reply_markup=answer_keyboard(continuable=truncated))
    _store.save_answer(
        chat_id=chat_id, message_id=sent.message_id, user_id=user_id, thread=thread,
        mode=None, pending_text=None, truncated=truncated, now=stamp,
    )


async def _pending_error(message: Message, user_id: int, text: str, base: list[dict[str, str]], mode: str,
                         user_text: str, stamp: int) -> None:
    sent = await message.answer(text, reply_markup=retry_keyboard())
    _store.save_answer(
        chat_id=message.chat.id, message_id=sent.message_id, user_id=user_id, thread=base,
        mode=mode, pending_text=user_text, truncated=False, now=stamp,
    )


async def answer_chat(message: Message, bot_name: str, api_key: str, user_id: int) -> None:
    await reply_with(message, bot_name, api_key, (message.text or "").strip(), user_id=user_id)


def _thread(chat_id: int, message_id: int) -> list[dict[str, str]]:
    row = _store.get_answer(chat_id, message_id)
    return row["thread"] if row else []


async def revise(message: Message, bot_name: str, api_key: str, instruction: str, user_id: int, action: str) -> None:
    thread = _thread(message.chat.id, message.message_id)
    if not thread or thread[-1]["role"] != "assistant":
        await message.answer(NOTHING_TO_REVISE_TEXT)
        return
    await reply_with(
        message, bot_name, api_key, instruction, thread, user_id=user_id, mode="revise",
        idem_key=f"cb:{message.chat.id}:{message.message_id}:{action}",
    )


async def smart_answer(message: Message, bot_name: str, api_key: str, user_id: int) -> None:
    thread = _thread(message.chat.id, message.message_id)
    if len(thread) < 2 or thread[-1]["role"] != "assistant":
        await message.answer(NOTHING_TO_REVISE_TEXT)
        return
    question = thread[-2]["content"]
    await reply_with(
        message, bot_name, api_key, question, thread[:-2], user_id=user_id, smart=True,
        idem_key=f"cb:{message.chat.id}:{message.message_id}:smart",
    )


async def retry_answer(message: Message, bot_name: str, api_key: str, user_id: int) -> None:
    row = _store.get_answer(message.chat.id, message.message_id)
    if row is None or row["pending_text"] is None:
        await message.answer(NOTHING_TO_REVISE_TEXT)
        return
    await reply_with(
        message, bot_name, api_key, row["pending_text"], row["thread"], user_id=user_id, mode=row["mode"],
        idem_key=f"cb:{message.chat.id}:{message.message_id}:retry",
    )


async def clear_history(message: Message, user_id: int) -> None:
    _store.clear_turns(user_id)
    await message.answer(HISTORY_CLEARED_TEXT)


def effective_chat_model(user_id: int, now: datetime) -> ChatModel:
    slug = _store.chat_model(user_id)
    choice = chat_model_by_slug(slug) if slug else None
    if choice is None:
        return DEFAULT_CHAT_MODEL
    if choice.tier == PREMIUM_TIER and ent.status_for(_store, user_id, now).tier != "pro":
        return DEFAULT_CHAT_MODEL
    return choice


async def choose_model(message: Message, user_id: int, slug: str, now: datetime) -> None:
    choice = chat_model_by_slug(slug)
    if choice is None:
        await message.answer(UNAVAILABLE_TEXT)
        return
    if choice.tier == PREMIUM_TIER and ent.status_for(_store, user_id, now).tier != "pro":
        await message.answer(billing_ui.model_locked_text(choice.title), reply_markup=billing_ui.paywall_markup())
        return
    _store.set_chat_model(user_id, slug, int(now.timestamp()))
    await message.answer(to_html(model_changed_text(choice.title)), parse_mode=ParseMode.HTML)


async def handle_message(
    message: Message,
    bot_name: str,
    support_contact: str,
    api_key: str,
    user_id: int,
) -> None:
    text = message.text or ""
    if not text.strip():
        return
    if normalize_action(text) in admin.ADMIN_COMMANDS:
        await admin.handle_admin(message, text, user_id, _store, _owner_id, datetime.now(LOCAL_TZ))
        return
    if not text.startswith("/"):
        await answer_chat(message, bot_name, api_key, user_id)
        return
    chat_id = message.chat.id
    screen_id = screen_after(current_screen(chat_id), text)
    await show(message, screen_id, bot_name, support_contact, user_id)


async def handle_billing_action(message: Message, data: str, user_id: int, bot_name: str, support_contact: str) -> None:
    now = datetime.now(LOCAL_TZ)
    if data in ("p:tariffs", "p:packs", "p:terms"):
        await show(message, data.split(":", 1)[1], bot_name, support_contact, user_id)
        return
    if data == "p:dismiss":
        return
    if data == "p:cancel":
        await message.answer(billing_ui.TEXT_CANCELLED)
        try:
            await message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest:
            pass
        return
    if data == "p:check":
        await payments.check_payment(message, message.bot, _store, user_id, now)
        return
    if data.startswith("p:buy:"):
        code = data.split(":", 2)[2]
        if code in PRODUCTS:
            await payments.start_purchase(message, _store, user_id, code, now, accepted=False)
        return
    if data.startswith("p:accept:"):
        code = data.split(":", 2)[2]
        if code in PRODUCTS:
            _store.accept_terms(user_id, TERMS_VERSION, int(now.timestamp()))
            await payments.start_purchase(message, _store, user_id, code, now, accepted=True)
        return
    if data in ("p:renew:off", "p:renew:on"):
        text = await payments.set_renewal(message.bot, _store, user_id, data.endswith(":on"), now)
        await message.answer(text)


async def handle_callback(
    query: CallbackQuery,
    bot_name: str,
    support_contact: str,
    api_key: str = "",
) -> None:
    await query.answer()
    message = query.message
    data = query.data or ""
    user_id = query.from_user.id
    if message is None:
        return
    if data == "a:tools":
        await set_answer_keyboard(message, expanded=True, continuable=_continuable(message.chat.id, message.message_id))
        return
    if data == "a:hide":
        await set_answer_keyboard(message, expanded=False, continuable=_continuable(message.chat.id, message.message_id))
        return
    if data in COLLAPSING_ACTIONS:
        await set_answer_keyboard(message, expanded=False, continuable=_continuable(message.chat.id, message.message_id))
    if data == "a:home":
        await show(message, "start", bot_name, support_contact, user_id)
        return
    if data == "a:improve":
        await revise(message, bot_name, api_key, IMPROVE, user_id, "improve")
        return
    if data == "a:shorten":
        await revise(message, bot_name, api_key, SHORTEN, user_id, "shorten")
        return
    if data == "a:cont":
        await revise(message, bot_name, api_key, CONTINUE, user_id, "continue")
        return
    if data == "a:smart":
        await smart_answer(message, bot_name, api_key, user_id)
        return
    if data == "a:retry":
        await retry_answer(message, bot_name, api_key, user_id)
        return
    if data.startswith("p:"):
        await handle_billing_action(message, data, user_id, bot_name, support_contact)
        return
    clicked = clicked_button(data)
    if clicked is None:
        return
    screen_id, label = clicked
    if screen_id == "history" and label == CLEAR_HISTORY_LABEL:
        await clear_history(message, user_id)
        return
    outcome = step(screen_id, label)
    if isinstance(outcome, Choose):
        await choose_model(message, user_id, outcome.model_id, datetime.now(LOCAL_TZ))
        return
    if isinstance(outcome, Back):
        await show(message, previous_screen(message.chat.id), bot_name, support_contact, user_id)
        return
    if isinstance(outcome, Closed):
        await message.answer(UNAVAILABLE_TEXT)
        return
    if not isinstance(outcome, Next):
        return
    await show(message, outcome.screen_id, bot_name, support_contact, user_id)


async def handle_error(event: ErrorEvent) -> bool:
    logger.error(
        "Unhandled %s while processing update %s",
        type(event.exception).__name__,
        event.update.update_id,
    )
    return True


def bot_commands() -> list[BotCommand]:
    return [BotCommand(command=name, description=description) for name, description in BOT_COMMANDS]


async def set_descriptions(bot: Bot) -> None:
    for language_code, text in DESCRIPTIONS.items():
        await bot.set_my_description(description=text, language_code=language_code)
    for language_code, text in SHORT_DESCRIPTIONS.items():
        await bot.set_my_short_description(short_description=text, language_code=language_code)


def make_dispatcher(bot_name: str, support_contact: str, api_key: str) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.errors.register(handle_error)

    @dispatcher.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        await payments.handle_pre_checkout(
            bot, _store, query.id, query.from_user.id, query.invoice_payload, datetime.now(LOCAL_TZ),
        )

    @dispatcher.message(F.successful_payment)
    async def on_successful_payment(message: Message, bot: Bot) -> None:
        await payments.handle_successful_payment(message, bot, _store, datetime.now(LOCAL_TZ))

    @dispatcher.message(F.refunded_payment)
    async def on_refunded_payment(message: Message, bot: Bot) -> None:
        charge_id = message.refunded_payment.telegram_payment_charge_id
        await payments.handle_refunded_payment(bot, _store, message.from_user.id, charge_id, datetime.now(LOCAL_TZ))

    @dispatcher.subscription()
    async def on_subscription(event: BotSubscriptionUpdated, bot: Bot) -> None:
        await payments.handle_subscription_update(
            bot, _store, event.user.id, event.invoice_payload, event.state, datetime.now(LOCAL_TZ),
        )

    @dispatcher.message()
    async def on_message(message: Message) -> None:
        if not serves_chat(message.chat.type) or message.from_user is None:
            return
        await handle_message(message, bot_name, support_contact, api_key, message.from_user.id)

    @dispatcher.callback_query()
    async def on_callback(query: CallbackQuery) -> None:
        chat_type = query.message.chat.type if query.message is not None else None
        if not serves_chat(chat_type):
            return
        await handle_callback(query, bot_name, support_contact, api_key)

    return dispatcher


async def maintenance(bot: Bot, store: Store) -> None:
    last_reconcile = float("-inf")
    while True:
        try:
            now = datetime.now(LOCAL_TZ)
            for notice in ent.sweep(store, now):
                await payments.maintenance_notice(bot, notice)
            if time.monotonic() - last_reconcile >= RECONCILE_INTERVAL_SECONDS:
                last_reconcile = time.monotonic()
                await payments.reconcile(bot, store, now)
        except Exception:
            logger.exception("maintenance step failed")
        await asyncio.sleep(SWEEP_INTERVAL_SECONDS)


async def run(
    token: str,
    bot_name: str,
    support_contact: str,
    api_key: str,
    allowed: tuple[int, ...],
    store: Store,
    dev: bool = False,
) -> None:
    configure(store, owner_of(allowed))
    bot = Bot(token)
    await bot.set_my_commands(bot_commands())
    await set_descriptions(bot)
    dispatcher = make_dispatcher(bot_name, support_contact, api_key)
    watcher = asyncio.create_task(watch_code()) if dev else None
    worker = asyncio.create_task(maintenance(bot, store))
    try:
        await dispatcher.start_polling(bot)
    finally:
        worker.cancel()
        if watcher is not None:
            watcher.cancel()
