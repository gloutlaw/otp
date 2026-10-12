import asyncio
import logging
from datetime import datetime
from types import SimpleNamespace

import pytest

import replica.chat as chat_module
import replica.telegram as telegram
from replica import billing_ui
from replica.billing import entitlements as ent
from replica.catalog import TERMS_VERSION, TRIAL_SMART
from replica.chat import IMPROVE, SMART_MODEL_ID, Completion
from replica.quota import LOCAL_TZ
from replica.screens import buttons

USER = 1


class _Message:
    def __init__(self, chat_id: int, message_id: int, user_id: int | None = None) -> None:
        self.chat = SimpleNamespace(id=chat_id)
        self.from_user = SimpleNamespace(id=user_id if user_id is not None else chat_id)
        self.message_id = message_id
        self.bot = SimpleNamespace(send_chat_action=self._typing)
        self.replies: list[_Message] = []
        self.text = ""
        self.reply_markup = None
        self._next_id = message_id + 100

    async def _typing(self, chat_id: int, action: object) -> None:
        return None

    async def edit_reply_markup(self, reply_markup: object = None) -> None:
        self.reply_markup = reply_markup

    async def answer(self, text: str, reply_markup: object = None, parse_mode: object = None) -> "_Message":
        sent = _Message(self.chat.id, self._next_id, self.from_user.id)
        self._next_id += 1
        sent.text = text
        sent.reply_markup = reply_markup
        self.replies.append(sent)
        return sent


class _Query:
    def __init__(self, data: str, message: _Message, user_id: int = USER) -> None:
        self.data = data
        self.message = message
        self.from_user = SimpleNamespace(id=user_id)

    async def answer(self, *args, **kwargs) -> None:
        return None


def _done(text: str, cost: float = 0.0, prompt: int = 0, completion: int = 0, finish: str = "stop") -> Completion:
    return Completion(text=text, cost_usd=cost, prompt_tokens=prompt, completion_tokens=completion, finish_reason=finish)


def _now() -> datetime:
    return datetime.now(LOCAL_TZ)


def _labels(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def _press(chat_id: int, message: _Message, screen: str, label: str) -> None:
    index = next(i for i, button in enumerate(buttons(screen)) if button.label == label)
    asyncio.run(telegram.handle_callback(_Query(f"n:{screen}:{index}", message), "Бот", "@support", ""))


def _ask(message: _Message, text: str, user_id: int = USER, smart: bool = False) -> None:
    asyncio.run(telegram.reply_with(message, "Бот", "key", text, user_id=user_id, smart=smart))


def _grant(plan: str) -> None:
    ent.grant_admin(telegram._store, user_id=USER, plan=plan, now=_now(), days=None, actor="1")


def _spend_free(chat_id: int, count: int, monkeypatch) -> None:
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ", 0.001, 10, 5))
    for index in range(count):
        asyncio.run(telegram.reply_with(_Message(90_000 + chat_id * 100 + index, 1), "Бот", "key", "вопрос", user_id=USER))


def test_old_menu_button_opens_the_screen_that_drew_it() -> None:
    chat_id = 41
    telegram.remember(chat_id, "models")
    data = telegram.keyboard("start").inline_keyboard[0][0].callback_data
    message = _Message(chat_id, 1)
    asyncio.run(telegram.handle_callback(_Query(data, message), "Бот", "@support", ""))
    assert telegram.current_screen(chat_id) == "models"


def test_tools_button_expands_and_hide_collapses_the_same_message() -> None:
    message = _Message(63, 1)
    message.reply_markup = telegram.answer_keyboard()
    asyncio.run(telegram.handle_callback(_Query("a:tools", message), "Бот", "@support", ""))
    assert _labels(message.reply_markup) == [
        ["Улучшить", "Сократить"],
        ["Ответить умнее", "Главное меню"],
        ["Скрыть"],
    ]
    asyncio.run(telegram.handle_callback(_Query("a:hide", message), "Бот", "@support", ""))
    assert _labels(message.reply_markup) == [["Инструменты"]]


def test_back_walks_the_path_and_home_resets_it() -> None:
    chat_id = 65
    telegram._chat_path.pop(chat_id, None)
    message = _Message(chat_id, 1)
    _press(chat_id, message, "start", "📊 Лимиты")
    assert telegram.current_screen(chat_id) == "limits"
    _press(chat_id, message, "limits", "Открыть тарифы")
    assert telegram.current_screen(chat_id) == "tariffs"
    _press(chat_id, message, "tariffs", "Назад")
    assert telegram.current_screen(chat_id) == "limits"
    _press(chat_id, message, "limits", "Главное меню")
    assert telegram.current_screen(chat_id) == "start"


def test_action_inside_tools_collapses_the_answer_it_came_from(monkeypatch) -> None:
    chat_id = 64
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ"))
    chat = _Message(chat_id, 1)
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "вопрос", user_id=USER))
    answer = chat.replies[0]
    asyncio.run(telegram.handle_callback(_Query("a:tools", answer), "Бот", "@support", "key"))
    asyncio.run(telegram.handle_callback(_Query("a:improve", answer), "Бот", "@support", "key"))
    assert _labels(answer.reply_markup) == [["Инструменты"]]


def test_improve_revises_the_clicked_answer_not_a_later_one(monkeypatch) -> None:
    chat_id = 42
    prompts: list[list[dict[str, str]]] = []

    def fake_complete(api_key, model_id, messages, budget):
        prompts.append(messages)
        return _done(f"ответ {len(prompts)}")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    chat = _Message(chat_id, 1)
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "первый вопрос", user_id=USER))
    first = chat.replies[0]
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "второй вопрос", user_id=USER))
    asyncio.run(telegram.handle_callback(_Query("a:improve", first), "Бот", "@support", "key"))
    contents = [item["content"] for item in prompts[-1]]
    assert "ответ 1" in contents
    assert "ответ 2" not in contents
    assert contents[-1] == IMPROVE


def test_improve_does_not_store_the_instruction_as_a_user_turn(monkeypatch) -> None:
    chat_id = 43
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ"))
    chat = _Message(chat_id, 1)
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "вопрос", user_id=USER))
    asyncio.run(telegram.handle_callback(_Query("a:improve", chat.replies[0]), "Бот", "@support", "key"))
    assert IMPROVE not in [turn["content"] for turn in telegram._store.recent_turns(USER, 20)]


def test_rate_limit_stops_model_calls_after_the_limit(monkeypatch) -> None:
    chat_id = 43
    telegram._model_requests.pop(chat_id, None)
    calls: list[int] = []

    def fake_complete(*args):
        calls.append(1)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    chat = _Message(chat_id, 1)
    for index in range(telegram.RATE_LIMIT_REQUESTS + 1):
        asyncio.run(telegram.reply_with(_Message(chat_id, index + 1), "Бот", "key", "вопрос", user_id=USER))
    assert len(calls) == telegram.RATE_LIMIT_REQUESTS
    assert chat.replies == []


def test_rate_limit_window_resets_after_a_minute() -> None:
    chat_id = 44
    telegram._model_requests.pop(chat_id, None)
    for second in range(telegram.RATE_LIMIT_REQUESTS):
        assert telegram.within_rate_limit(chat_id, now=float(second))
    assert not telegram.within_rate_limit(chat_id, now=20.0)
    assert telegram.within_rate_limit(chat_id, now=telegram.RATE_LIMIT_WINDOW_SECONDS + 1.0)


def test_stale_rate_limit_keys_are_pruned() -> None:
    telegram._model_requests.clear()
    assert telegram.within_rate_limit(46, now=0.0)
    assert telegram.within_rate_limit(47, now=500.0)
    assert 46 not in telegram._model_requests
    assert 47 in telegram._model_requests


def test_long_input_does_not_reach_the_model(monkeypatch) -> None:
    calls: list[tuple] = []
    monkeypatch.setattr(telegram, "complete", lambda *args: calls.append(args) or _done("ответ"))
    chat = _Message(49, 1)
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "я" * (telegram.MAX_INPUT_CHARS + 1), user_id=USER))
    assert calls == []
    assert chat.replies[-1].text == telegram.TOO_LONG_TEXT


def test_only_private_chats_are_served() -> None:
    assert telegram.serves_chat("private")
    assert not telegram.serves_chat("group")
    assert not telegram.serves_chat("supergroup")
    assert not telegram.serves_chat("channel")
    assert not telegram.serves_chat(None)


def test_successful_answer_spends_one_cheap_request_and_records_cost(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ", 0.0021, 300, 120))
    _ask(_Message(50, 1), "вопрос")
    count, cost, prompt, completion = telegram._store.usage_totals(USER)
    assert count == 1
    assert cost == pytest.approx(0.0021)
    assert (prompt, completion) == (300, 120)


def test_failed_model_call_does_not_spend_quota(monkeypatch) -> None:
    def failing(*args):
        raise chat_module.ChatError("сбой")

    monkeypatch.setattr(telegram, "complete", failing)
    _ask(_Message(51, 1), "вопрос")
    assert telegram._store.usage_totals(USER)[0] == 0


def test_overload_shows_a_retry_button_and_retry_reuses_the_question(monkeypatch) -> None:
    state = {"calls": 0}

    def overloaded_then_ok(api_key, model_id, messages, budget):
        state["calls"] += 1
        if state["calls"] <= 2:
            raise chat_module.RetryableError("http 503")
        return _done("ответ после повтора")

    monkeypatch.setattr(telegram, "complete", overloaded_then_ok)
    chat = _Message(52, 1)
    _ask(chat, "вопрос про отпуск")
    error = chat.replies[-1]
    assert error.text == billing_ui.TEXT_BUSY
    assert _labels(error.reply_markup) == [["🔁 Повторить"]]
    assert telegram._store.usage_totals(USER)[0] == 0
    asyncio.run(telegram.handle_callback(_Query("a:retry", error), "Бот", "@support", "key"))
    assert error.replies[-1].text == "ответ после повтора"
    assert telegram._store.usage_totals(USER)[0] == 1


def test_config_error_hides_details_and_alerts_the_owner(monkeypatch) -> None:
    def rejected(*args):
        raise chat_module.ConfigError("http 401")

    alerts: list[str] = []

    async def alert(bot, text):
        alerts.append(text)

    monkeypatch.setattr(telegram, "complete", rejected)
    monkeypatch.setattr(telegram, "alert_owner", alert)
    chat = _Message(53, 1)
    _ask(chat, "вопрос")
    assert chat.replies[-1].text == billing_ui.TEXT_UNAVAILABLE
    assert alerts and "провайдер" in alerts[0]
    for banned in ("OPENROUTER", ".env", "Ключ", "бесплатной модели"):
        assert banned not in chat.replies[-1].text
    assert telegram._store.usage_totals(USER)[0] == 0


def test_missing_key_never_reaches_the_model(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(telegram, "complete", lambda *args: calls.append(1) or _done("ответ"))
    chat = _Message(54, 1)
    asyncio.run(telegram.reply_with(chat, "Бот", "", "вопрос", user_id=USER))
    assert calls == []
    assert chat.replies[-1].text == billing_ui.TEXT_UNAVAILABLE


def test_truncated_answer_says_so_and_offers_to_continue(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("обрыв", finish="length"))
    chat = _Message(55, 1)
    _ask(chat, "длинный вопрос")
    answer = chat.replies[-1]
    assert "ответ сокращён" in answer.text
    assert any("Продолжить" in text for row in _labels(answer.reply_markup) for text in row)


def test_continue_button_uses_the_revise_path(monkeypatch) -> None:
    seen: list[str] = []

    def fake_complete(api_key, model_id, messages, budget):
        seen.append(messages[-1]["content"])
        return _done("продолжение")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    chat = _Message(56, 1)
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("начало", finish="length"))
    _ask(chat, "длинный вопрос")
    monkeypatch.setattr(telegram, "complete", fake_complete)
    asyncio.run(telegram.handle_callback(_Query("a:cont", chat.replies[-1]), "Бот", "@support", "key"))
    assert seen and "Продолжи" in seen[-1]


def test_blocked_user_is_refused(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(telegram, "complete", lambda *args: calls.append(1) or _done("ответ"))
    telegram._store.set_user_status(USER, "blocked", int(_now().timestamp()))
    chat = _Message(57, 1)
    _ask(chat, "вопрос")
    assert calls == []
    assert chat.replies[-1].text == billing_ui.TEXT_BLOCKED


def test_exhausted_free_quota_shows_the_limit_with_tariffs_button(monkeypatch) -> None:
    _spend_free(58, 10, monkeypatch)
    calls: list[int] = []
    monkeypatch.setattr(telegram, "complete", lambda *args: calls.append(1) or _done("ответ"))
    chat = _Message(58, 2000)
    _ask(chat, "вопрос")
    assert calls == []
    assert "На сегодня бесплатные ответы закончились" in chat.replies[-1].text
    assert _labels(chat.replies[-1].reply_markup) == [["⭐ Тарифы"], ["Ок, подожду"]]


def test_smart_answer_without_access_shows_the_paywall(monkeypatch) -> None:
    _spend_free(59, 0, monkeypatch)
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ"))
    for index in range(TRIAL_SMART):
        asyncio.run(telegram.reply_with(_Message(59, 3000 + index), "Бот", "key", "вопрос", user_id=USER, smart=True))
    chat = _Message(59, 4000)
    asyncio.run(telegram.reply_with(chat, "Бот", "key", "вопрос", user_id=USER, smart=True))
    assert "«+100 умных»" in chat.replies[-1].text


def test_exhausted_trial_is_not_a_quota_message(monkeypatch) -> None:
    _spend_free(60, 0, monkeypatch)
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ"))
    for index in range(TRIAL_SMART):
        asyncio.run(telegram.reply_with(_Message(60, 3000 + index), "Бот", "key", "вопрос", user_id=USER, smart=True))
    assert ent.status_for(telegram._store, USER, _now()).trial_left == 0


def test_smart_answer_uses_the_smart_model_on_pro(monkeypatch) -> None:
    _grant("pro")
    models: list[str] = []

    def fake_complete(api_key, model_id, messages, budget):
        models.append(model_id)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    asyncio.run(telegram.reply_with(_Message(61, 1), "Бот", "key", "вопрос", user_id=USER, smart=True))
    assert models == [SMART_MODEL_ID]


def test_smart_button_recomputes_the_last_question_and_only_once(monkeypatch) -> None:
    _grant("pro")
    seen: list[tuple[str, list[dict[str, str]]]] = []

    def fake_complete(api_key, model_id, messages, budget):
        seen.append((model_id, messages))
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    chat = _Message(62, 1)
    _ask(chat, "первый вопрос")
    first = chat.replies[0]
    asyncio.run(telegram.handle_callback(_Query("a:smart", first), "Бот", "@support", "key"))
    asyncio.run(telegram.handle_callback(_Query("a:smart", first), "Бот", "@support", "key"))
    smart_calls = [item for item in seen if item[0] == SMART_MODEL_ID]
    assert len(smart_calls) == 1
    assert smart_calls[0][1][-1] == {"role": "user", "content": "первый вопрос"}
    assert first.replies[-1].text == billing_ui.TEXT_SMART_ALREADY


def test_free_plan_sends_the_short_budget(monkeypatch) -> None:
    budgets = []

    def fake_complete(api_key, model_id, messages, budget):
        budgets.append(budget)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    _ask(_Message(63, 1), "вопрос")
    assert budgets == [telegram.budget_for("free", False)]
    assert budgets[0].max_tokens == 500


def test_concurrent_requests_on_the_last_unit_call_the_model_once(monkeypatch) -> None:
    _spend_free(64, 9, monkeypatch)
    calls: list[int] = []

    def counting_complete(api_key, model_id, messages, budget):
        calls.append(1)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", counting_complete)

    async def burst() -> None:
        await asyncio.gather(
            *(telegram.reply_with(_Message(100 + index, 1), "Бот", "key", "вопрос", user_id=USER) for index in range(20))
        )

    asyncio.run(burst())
    assert len(calls) == 1


def test_history_is_loaded_from_the_database_after_restart(monkeypatch) -> None:
    chat_id = 57
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("первый ответ"))
    asyncio.run(telegram.reply_with(_Message(chat_id, 1), "Бот", "key", "первый вопрос", user_id=USER))
    prompts: list[list[dict[str, str]]] = []

    def fake_complete(api_key, model_id, messages, budget):
        prompts.append(messages)
        return _done("второй ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    asyncio.run(telegram.reply_with(_Message(chat_id, 2), "Бот", "key", "второй вопрос", user_id=USER))
    contents = [item["content"] for item in prompts[-1]]
    assert "первый вопрос" in contents
    assert "первый ответ" in contents


def test_answers_survive_a_cleared_memory_state(monkeypatch) -> None:
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("ответ"))
    chat = _Message(66, 1)
    _ask(chat, "вопрос")
    telegram._chat_path.clear()
    monkeypatch.setattr(telegram, "complete", lambda *args: _done("улучшено"))
    asyncio.run(telegram.handle_callback(_Query("a:improve", chat.replies[0]), "Бот", "@support", "key"))
    assert chat.replies[0].replies[-1].text == "улучшено"


def test_history_screen_lists_recent_questions_escaped() -> None:
    telegram._store.add_turn(USER, "user", "<b>вопрос</b> & тест", int(_now().timestamp()))
    text = telegram.history_html(USER)
    assert "&lt;b&gt;вопрос&lt;/b&gt; &amp; тест" in text


def test_history_screen_empty_state() -> None:
    assert telegram.history_html(USER) == "Запросов пока нет."


def test_clear_button_removes_turns() -> None:
    telegram._store.add_turn(USER, "user", "вопрос", int(_now().timestamp()))
    message = _Message(60, 1)
    asyncio.run(telegram.handle_callback(_Query("n:history:1", message), "Бот", "@support", "key"))
    assert telegram._store.recent_turns(USER, 12) == []
    assert message.replies[-1].text == telegram.HISTORY_CLEARED_TEXT


def test_closed_buttons_explain_that_the_feature_is_unavailable() -> None:
    message = _Message(70, 1)
    asyncio.run(telegram.handle_callback(_Query("n:profile:0", message), "Бот", "@support", "key"))
    assert message.replies[-1].text == telegram.UNAVAILABLE_TEXT


def test_status_shows_plan_and_remaining() -> None:
    status = telegram.plan_status(USER, _now())
    assert "Бесплатный" in status
    assert "Осталось: **10 из 10**" in status
    assert "пробные" in status


def test_bot_commands_match_the_menu() -> None:
    names = [command.command for command in telegram.bot_commands()]
    assert names == ["start", "help", "models", "profile", "premium", "limits", "support", "terms", "paysupport"]


def test_terms_and_paysupport_open_their_screens() -> None:
    message = _Message(72, 1)
    message.text = "/terms"
    asyncio.run(telegram.handle_message(message, "Бот", "@support", "key", USER))
    assert "Версия условий: " + TERMS_VERSION in message.replies[-1].text
    message = _Message(73, 1)
    message.text = "/paysupport"
    asyncio.run(telegram.handle_message(message, "Бот", "@support", "key", USER))
    assert "@support" in message.replies[-1].text


def test_error_handler_logs_type_and_update_id_only(caplog) -> None:
    event = SimpleNamespace(exception=ValueError("secret detail"), update=SimpleNamespace(update_id=7))
    with caplog.at_level(logging.ERROR, logger="replica.telegram"):
        assert asyncio.run(telegram.handle_error(event)) is True
    assert "ValueError" in caplog.text
    assert "update 7" in caplog.text
    assert "secret detail" not in caplog.text


def _usage_kinds() -> list[str]:
    return [row["kind"] for row in telegram._store._all("SELECT kind FROM usage WHERE status = 'committed'")]


def test_models_screen_has_no_close_button_and_lists_the_catalog() -> None:
    from replica.catalog import CHAT_MODELS
    from replica.screens import SCREENS

    labels = [button.label for button in SCREENS["models"].buttons]
    assert "Закрыть" not in labels
    assert labels == [model.title for model in CHAT_MODELS]


def test_free_user_cannot_pick_a_premium_model() -> None:
    chat = _Message(80, 1)
    _press(80, chat, "models", "Claude Sonnet 5.5")
    assert telegram._store.chat_model(USER) is None
    assert "доступна в Pro" in chat.replies[-1].text


def test_pro_user_model_choice_is_used_and_spends_smart_quota(monkeypatch) -> None:
    _grant("pro")
    models: list[str] = []

    def fake_complete(api_key, model_id, messages, budget):
        models.append(model_id)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    chat = _Message(81, 1)
    _press(81, chat, "models", "Claude Sonnet 5.5")
    assert telegram._store.chat_model(USER) == "anthropic/claude-sonnet-5.5"
    _ask(_Message(82, 1), "вопрос")
    assert models == ["anthropic/claude-sonnet-5.5"]
    assert _usage_kinds() == ["smart"]


def test_cheap_model_choice_spends_the_cheap_quota(monkeypatch) -> None:
    models: list[str] = []

    def fake_complete(api_key, model_id, messages, budget):
        models.append(model_id)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    _press(83, _Message(83, 1), "models", "DeepSeek V4.1 Flash")
    _ask(_Message(84, 1), "вопрос")
    assert models == ["deepseek/deepseek-v4.1-flash"]
    assert _usage_kinds() == ["cheap"]


def test_downgraded_user_falls_back_to_the_default_model(monkeypatch) -> None:
    _grant("pro")
    _press(85, _Message(85, 1), "models", "Claude Opus 5.5")
    ent.grant_admin(telegram._store, user_id=USER, plan="free", now=_now(), days=None, actor="1")
    models: list[str] = []

    def fake_complete(api_key, model_id, messages, budget):
        models.append(model_id)
        return _done("ответ")

    monkeypatch.setattr(telegram, "complete", fake_complete)
    _ask(_Message(86, 1), "вопрос")
    assert models == [telegram.DEFAULT_CHAT_MODEL.slug]


def test_cancel_payment_tells_the_user_and_removes_the_buttons() -> None:
    message = _Message(87, 1)
    message.reply_markup = billing_ui.invoice_markup("https://t.me/invoice", 600)
    asyncio.run(telegram.handle_callback(_Query("p:cancel", message), "Бот", "@support", ""))
    assert message.replies[-1].text == billing_ui.TEXT_CANCELLED
    assert message.reply_markup is None


def test_dismiss_does_not_say_the_payment_was_cancelled() -> None:
    message = _Message(88, 1)
    asyncio.run(telegram.handle_callback(_Query("p:dismiss", message), "Бот", "@support", ""))
    assert message.replies == []
