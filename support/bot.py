import logging
import re
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import Message, User

from replica.quota import LOCAL_TZ, PLAN_TITLES
from support import texts
from support.replica_db import plan_of
from support.store import DIRECT_SOURCE, Store, Ticket

logger = logging.getLogger(__name__)

MAX_INPUT_CHARS = 2000
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
OWNER_COMMANDS = frozenset({"open", "close", "reply", "ban", "unban", "who"})
SOURCE_PATTERN = re.compile(r"[\w-]{1,32}")
OTHER_SOURCE = "other"


@dataclass(frozen=True)
class Desk:
    owner_id: int
    store: Store
    replica_db: Path


def now_epoch() -> int:
    return int(datetime.now(LOCAL_TZ).timestamp())


def clock_text(epoch: int) -> str:
    return datetime.fromtimestamp(epoch, LOCAL_TZ).strftime("%d.%m %H:%M")


def body_of(message: Message) -> str:
    return message.text or message.caption or ""


def command_of(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    head = text.split(maxsplit=1)[0]
    return head[1:].split("@", 1)[0].lower()


def target_id(text: str) -> int | None:
    parts = text.split()
    if len(parts) < 2 or not parts[1].isdigit():
        return None
    return int(parts[1])


# Only plain identifiers are stored; arbitrary text from a link is not trusted as a source label.
def source_from_start(text: str) -> str:
    parts = text.split(maxsplit=1)
    if len(parts) < 2:
        return DIRECT_SOURCE
    arg = parts[1].strip()
    return arg if SOURCE_PATTERN.fullmatch(arg) else OTHER_SOURCE


def card_text(ticket: Ticket, user: User, now: int) -> str:
    return texts.CARD.format(
        ticket_id=ticket.id,
        user_id=user.id,
        name=user.full_name,
        username=f"@{user.username}" if user.username else "нет",
        source=ticket.source,
        time=clock_text(now),
    )


async def try_deliver(delivery: Awaitable[object]) -> str | None:
    try:
        await delivery
    except TelegramForbiddenError:
        return texts.NOT_DELIVERED_BLOCKED
    except TelegramAPIError as exc:
        logger.error("Delivery to user failed: %s", type(exc).__name__)
        return texts.NOT_DELIVERED
    return None


async def on_user_start(message: Message, desk: Desk) -> None:
    if message.from_user is None or desk.store.is_banned(message.from_user.id):
        return
    desk.store.remember_source(message.from_user.id, source_from_start(message.text or ""), now_epoch())
    await message.answer(texts.START)


async def on_user_message(message: Message, bot: Bot, desk: Desk) -> None:
    user = message.from_user
    if user is None:
        return
    body = body_of(message)
    if command_of(body) in OWNER_COMMANDS:
        return
    if desk.store.is_banned(user.id):
        return
    if len(body) > MAX_INPUT_CHARS:
        await message.answer(texts.TOO_LONG.format(limit=MAX_INPUT_CHARS))
        return
    now = now_epoch()
    if not desk.store.take_rate_slot(user.id, now, RATE_LIMIT_REQUESTS, RATE_LIMIT_WINDOW_SECONDS):
        await message.answer(texts.RATE_LIMITED)
        return

    ticket = desk.store.open_ticket_for(user.id)
    is_new = ticket is None
    if ticket is None:
        ticket = desk.store.create_ticket(user.id, desk.store.source_of(user.id), now)
        card = await bot.send_message(desk.owner_id, card_text(ticket, user, now))
        desk.store.add_relay(card.message_id, ticket.id, user.id)

    copied = await bot.copy_message(
        chat_id=desk.owner_id,
        from_chat_id=message.chat.id,
        message_id=message.message_id,
    )
    desk.store.add_relay(copied.message_id, ticket.id, user.id)
    if is_new:
        await message.answer(texts.ACK.format(ticket_id=ticket.id))


async def on_user_edit(message: Message, bot: Bot, desk: Desk) -> None:
    user = message.from_user
    if user is None or desk.store.is_banned(user.id):
        return
    body = body_of(message)
    ticket = desk.store.open_ticket_for(user.id)
    if ticket is None or not body or len(body) > MAX_INPUT_CHARS:
        return
    notice = await bot.send_message(desk.owner_id, texts.EDITED.format(ticket_id=ticket.id, body=body))
    desk.store.add_relay(notice.message_id, ticket.id, user.id)


async def on_owner_message(message: Message, bot: Bot, desk: Desk) -> None:
    body = body_of(message)
    name = command_of(body)
    now = now_epoch()
    if name in ("start", "help"):
        await message.answer(texts.OWNER_HELP)
    elif name == "open":
        await message.answer(_open_list(desk.store))
    elif name == "reply":
        await _owner_reply(message, bot, desk, body)
    elif name in OWNER_COMMANDS:
        await _owner_user_command(message, bot, desk, name, body, now)
    elif name is not None:
        await message.answer(texts.OWNER_HELP)
    else:
        await _owner_reply_to_ticket(message, bot, desk)


def _open_list(store: Store) -> str:
    tickets = store.list_open()
    if not tickets:
        return texts.NO_OPEN_TICKETS
    return "\n".join(
        texts.OPEN_LINE.format(
            ticket_id=ticket.id,
            user_id=ticket.user_id,
            source=ticket.source,
            time=clock_text(ticket.created_at),
        )
        for ticket in tickets
    )


async def _owner_reply(message: Message, bot: Bot, desk: Desk, body: str) -> None:
    parts = body.split(maxsplit=2)
    if len(parts) < 3 or not parts[1].isdigit():
        await message.answer(texts.REPLY_USAGE)
        return
    error = await try_deliver(bot.send_message(int(parts[1]), parts[2]))
    await message.answer(error or texts.SENT)


async def _owner_reply_to_ticket(message: Message, bot: Bot, desk: Desk) -> None:
    # Replies route by the id of the message in the operator chat, not by user id.
    if message.reply_to_message is not None:
        route = desk.store.route(message.reply_to_message.message_id)
        if route is not None:
            _, user_id = route
            error = await try_deliver(
                bot.copy_message(chat_id=user_id, from_chat_id=desk.owner_id, message_id=message.message_id)
            )
            await message.answer(error or texts.SENT)
            return
    await message.answer(texts.OWNER_HINT)


async def _owner_user_command(
    message: Message,
    bot: Bot,
    desk: Desk,
    name: str,
    body: str,
    now: int,
) -> None:
    user_id = target_id(body)
    if user_id is None:
        await message.answer(texts.USER_ID_REQUIRED)
        return
    if name == "close":
        ticket = desk.store.close_open_ticket(user_id, now)
        if ticket is None:
            await message.answer(texts.NO_OPEN_TICKET)
            return
        await message.answer(texts.CLOSED_FOR_OWNER.format(ticket_id=ticket.id))
        error = await try_deliver(bot.send_message(user_id, texts.CLOSED.format(ticket_id=ticket.id)))
        if error is not None:
            await message.answer(error)
    elif name == "ban":
        desk.store.ban(user_id, now)
        desk.store.close_open_ticket(user_id, now)
        await message.answer(texts.BANNED.format(user_id=user_id))
    elif name == "unban":
        done = desk.store.unban(user_id)
        await message.answer((texts.UNBANNED if done else texts.NOT_BANNED).format(user_id=user_id))
    elif name == "who":
        plan = plan_of(desk.replica_db, user_id)
        if plan is None:
            await message.answer(texts.NO_REPLICA_RECORD)
        else:
            await message.answer(texts.WHO.format(user_id=user_id, plan=PLAN_TITLES.get(plan, plan)))


def build_dispatcher(desk: Desk) -> Dispatcher:
    private = F.chat.type == ChatType.PRIVATE
    is_owner = F.from_user.id == desk.owner_id
    is_user = F.from_user.id != desk.owner_id

    router = Router()
    router.message.filter(private)
    router.edited_message.filter(private)
    router.message.register(on_owner_message, is_owner)
    router.message.register(on_user_start, is_user, CommandStart())
    router.message.register(on_user_message, is_user)
    router.edited_message.register(on_user_edit, is_user)

    dispatcher = Dispatcher()
    dispatcher["desk"] = desk
    dispatcher.include_router(router)
    return dispatcher
