import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CopyMessage, SendMessage

from support import texts
from support.bot import (
    MAX_INPUT_CHARS,
    RATE_LIMIT_REQUESTS,
    Desk,
    build_dispatcher,
    on_owner_message,
    on_user_edit,
    on_user_message,
    on_user_start,
)
from support.store import Store

OWNER = 1
USER = 2


class FakeBot:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.copied: list[tuple[int, int, int, int]] = []
        self.blocked: set[int] = set()
        self._last_id = 500

    def _new_id(self) -> int:
        self._last_id += 1
        return self._last_id

    async def send_message(self, chat_id: int, text: str, **kwargs) -> SimpleNamespace:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(method=SendMessage(chat_id=chat_id, text=text), message="Forbidden")
        self.sent.append((chat_id, text))
        return SimpleNamespace(message_id=self._new_id())

    async def copy_message(self, chat_id: int, from_chat_id: int, message_id: int, **kwargs) -> SimpleNamespace:
        if chat_id in self.blocked:
            raise TelegramForbiddenError(
                method=CopyMessage(chat_id=chat_id, from_chat_id=from_chat_id, message_id=message_id),
                message="Forbidden",
            )
        new_id = self._new_id()
        self.copied.append((chat_id, from_chat_id, message_id, new_id))
        return SimpleNamespace(message_id=new_id)


class FakeMessage:
    def __init__(self, user_id: int, message_id: int, text: str | None = None, reply_to=None) -> None:
        self.from_user = SimpleNamespace(id=user_id, full_name="Иван Петров", username=None)
        self.chat = SimpleNamespace(id=user_id, type="private")
        self.message_id = message_id
        self.text = text
        self.caption = None
        self.reply_to_message = reply_to
        self.replies: list[str] = []

    async def answer(self, text: str, **kwargs) -> None:
        self.replies.append(text)


@pytest.fixture
def desk(tmp_path: Path):
    store = Store(tmp_path / "support.db")
    yield Desk(owner_id=OWNER, store=store, replica_db=tmp_path / "missing.db")
    store.close()


@pytest.fixture
def bot() -> FakeBot:
    return FakeBot()


def run(coro) -> None:
    asyncio.run(coro)


def user_says(bot: FakeBot, desk: Desk, text: str, message_id: int = 10) -> FakeMessage:
    message = FakeMessage(USER, message_id, text)
    run(on_user_message(message, bot, desk))
    return message


def owner_replies_to(bot: FakeBot, desk: Desk, owner_message_id: int, reply_to_id: int, text: str) -> FakeMessage:
    message = FakeMessage(OWNER, owner_message_id, text, reply_to=SimpleNamespace(message_id=reply_to_id))
    run(on_owner_message(message, bot, desk))
    return message


def owner_command(bot: FakeBot, desk: Desk, text: str) -> FakeMessage:
    message = FakeMessage(OWNER, 800, text)
    run(on_owner_message(message, bot, desk))
    return message


def last_user_copy_id(bot: FakeBot) -> int:
    return bot.copied[-1][3]


def test_first_message_opens_ticket_and_is_acked_once(bot, desk) -> None:
    first = user_says(bot, desk, "Не работает оплата")
    ticket_id = desk.store.open_ticket_for(USER).id
    assert first.replies == [texts.ACK.format(ticket_id=ticket_id)]
    assert [chat for chat, _ in bot.sent] == [OWNER]
    assert bot.copied[-1][:2] == (OWNER, USER)

    second = user_says(bot, desk, "Ещё вопрос", message_id=11)
    assert second.replies == []
    assert len(bot.sent) == 1
    assert len(bot.copied) == 2


def test_owner_reply_goes_to_the_ticket_user(bot, desk) -> None:
    user_says(bot, desk, "Вопрос")
    reply = owner_replies_to(bot, desk, 900, last_user_copy_id(bot), "Ответ")
    assert bot.copied[-1][:2] == (USER, OWNER)
    assert reply.replies == [texts.SENT]


def test_replying_to_the_card_also_routes_to_the_user(bot, desk) -> None:
    user_says(bot, desk, "Вопрос")
    card_id = bot.copied[-1][3] - 1
    reply = owner_replies_to(bot, desk, 900, card_id, "Ответ")
    assert bot.copied[-1][:2] == (USER, OWNER)
    assert reply.replies == [texts.SENT]


def test_owner_reply_to_unknown_message_gets_a_hint(bot, desk) -> None:
    reply = owner_replies_to(bot, desk, 900, 12345, "Ответ")
    assert reply.replies == [texts.OWNER_HINT]
    assert bot.copied == []


def test_owner_command_from_user_is_ignored(bot, desk) -> None:
    message = FakeMessage(USER, 10, "/close 2")
    run(on_user_message(message, bot, desk))
    assert message.replies == []
    assert bot.sent == []
    assert bot.copied == []


def test_banned_user_gets_no_answer(bot, desk) -> None:
    desk.store.ban(USER, now=1)
    message = user_says(bot, desk, "Привет")
    assert message.replies == []
    assert bot.sent == []
    assert bot.copied == []


def test_rate_limit_stops_the_extra_message(bot, desk) -> None:
    for index in range(RATE_LIMIT_REQUESTS):
        user_says(bot, desk, f"вопрос {index}", message_id=index + 10)
    blocked = user_says(bot, desk, "ещё один", message_id=99)
    assert blocked.replies == [texts.RATE_LIMITED]
    assert len(bot.copied) == RATE_LIMIT_REQUESTS


def test_too_long_message_is_rejected_without_relay(bot, desk) -> None:
    message = user_says(bot, desk, "а" * (MAX_INPUT_CHARS + 1))
    assert message.replies == [texts.TOO_LONG.format(limit=MAX_INPUT_CHARS)]
    assert bot.copied == []
    assert bot.sent == []


def test_blocked_user_is_reported_to_owner(bot, desk) -> None:
    user_says(bot, desk, "Вопрос")
    bot.blocked.add(USER)
    reply = owner_replies_to(bot, desk, 900, last_user_copy_id(bot), "Ответ")
    assert reply.replies == [texts.NOT_DELIVERED_BLOCKED]


def test_close_notifies_user_and_next_message_opens_new_ticket(bot, desk) -> None:
    user_says(bot, desk, "Первый вопрос")
    first_id = desk.store.open_ticket_for(USER).id
    owner_command(bot, desk, f"/close {USER}")
    assert (USER, texts.CLOSED.format(ticket_id=first_id)) in bot.sent

    reopened = user_says(bot, desk, "Второй вопрос", message_id=20)
    new_id = desk.store.open_ticket_for(USER).id
    assert new_id != first_id
    assert reopened.replies == [texts.ACK.format(ticket_id=new_id)]


def test_ban_closes_open_ticket(bot, desk) -> None:
    user_says(bot, desk, "Вопрос")
    owner_command(bot, desk, f"/ban {USER}")
    assert desk.store.open_ticket_for(USER) is None
    assert desk.store.is_banned(USER)


def test_who_without_replica_record_says_so(bot, desk) -> None:
    assert owner_command(bot, desk, f"/who {USER}").replies == [texts.NO_REPLICA_RECORD]


def test_plain_owner_message_without_reply_gets_a_hint(bot, desk) -> None:
    message = owner_command(bot, desk, "просто текст")
    assert message.replies == [texts.OWNER_HINT]
    assert bot.copied == []


def test_start_with_replica_link_remembers_source(bot, desk) -> None:
    message = FakeMessage(USER, 10, "/start replica")
    run(on_user_start(message, desk))
    assert desk.store.source_of(USER) == "replica"
    assert message.replies == [texts.START]


def test_edited_message_is_forwarded_to_open_ticket(bot, desk) -> None:
    user_says(bot, desk, "Вопрос")
    run(on_user_edit(FakeMessage(USER, 10, "Исправил вопрос"), bot, desk))
    ticket_id = desk.store.open_ticket_for(USER).id
    assert bot.sent[-1] == (OWNER, texts.EDITED.format(ticket_id=ticket_id, body="Исправил вопрос"))


def test_dispatcher_builds_with_desk_injected(desk) -> None:
    dispatcher = build_dispatcher(desk)
    assert dispatcher["desk"] is desk
