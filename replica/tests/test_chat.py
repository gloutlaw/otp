import urllib.error

import pytest

import replica.chat as chat
from replica.chat import (
    CHAT_MODEL_ID,
    CHEAP_BUDGET,
    FREE_BUDGET,
    HISTORY_CHAR_LIMIT,
    SMART_BUDGET,
    SMART_MODEL_ID,
    ChatError,
    ConfigError,
    RetryableError,
    append_turn,
    budget_for,
    build_messages,
    complete,
    estimated_tokens,
    fit_history,
    parse_completion,
    request_body,
    trim_history,
)


def test_short_history_keeps_every_turn():
    history = append_turn([], "вопрос", "ответ")
    assert history == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ]


def test_long_history_is_trimmed_by_characters_in_whole_turns():
    long_text = "а" * 3000
    history: list[dict[str, str]] = []
    for _ in range(3):
        history = append_turn(history, long_text, long_text)
    assert sum(len(item["content"]) for item in history) <= HISTORY_CHAR_LIMIT
    assert history[0]["role"] == "user"
    assert len(history) % 2 == 0


def test_trim_uses_the_limit_it_is_given():
    history = [{"role": "user", "content": "а" * 1000}, {"role": "assistant", "content": "б" * 1000}] * 3
    assert len(trim_history(history, 4500)) > len(trim_history(history, 2000))
    assert sum(len(item["content"]) for item in trim_history(history, 2000)) <= 2000


def test_chat_model_body_turns_reasoning_off() -> None:
    body = request_body(CHAT_MODEL_ID, [], CHEAP_BUDGET)
    assert body["model"] == "anthropic/claude-haiku-5.5"
    assert body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == 700


def test_smart_model_body_uses_low_effort_and_bigger_budget() -> None:
    body = request_body(SMART_MODEL_ID, [], SMART_BUDGET)
    assert body["model"] == "openai/gpt-6.1-sol"
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == 1000


def test_free_plan_gets_the_short_budget() -> None:
    assert budget_for("free", False) == chat.FREE_BUDGET
    assert request_body(CHAT_MODEL_ID, [], FREE_BUDGET)["max_tokens"] == 500
    assert FREE_BUDGET.history_chars == 4500


def test_paid_plans_get_the_cheap_budget_and_pro_smart_the_smart_one() -> None:
    assert budget_for("start", False) == chat.CHEAP_BUDGET
    assert budget_for("pro", False) == chat.CHEAP_BUDGET
    assert budget_for("pro", True) == chat.SMART_BUDGET
    assert SMART_BUDGET.history_chars == 12000


def test_parse_completion_reads_text_and_usage() -> None:
    body = {
        "choices": [{"message": {"content": " ответ "}}],
        "usage": {"cost": 0.0021, "prompt_tokens": 300, "completion_tokens": 120},
    }
    completion = parse_completion(body)
    assert completion.text == "ответ"
    assert completion.cost_usd == pytest.approx(0.0021)
    assert completion.prompt_tokens == 300
    assert completion.completion_tokens == 120


def test_parse_completion_without_usage_gives_zeros() -> None:
    completion = parse_completion({"choices": [{"message": {"content": "ответ"}}]})
    assert completion.cost_usd == 0.0
    assert completion.prompt_tokens == 0
    assert completion.completion_tokens == 0


def test_parse_completion_rejects_empty_answer() -> None:
    with pytest.raises(chat.ChatError):
        parse_completion({"choices": [{"message": {"content": "   "}}]})


def test_finish_reason_reasoning_and_generation_id_are_read() -> None:
    body = {
        "id": "gen-1",
        "choices": [{"message": {"content": "ответ"}, "finish_reason": "length"}],
        "usage": {"completion_tokens": 5, "completion_tokens_details": {"reasoning_tokens": 3}},
    }
    completion = parse_completion(body)
    assert completion.finish_reason == "length"
    assert completion.reasoning_tokens == 3
    assert completion.generation_id == "gen-1"


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(chat.API_URL, code, "error", None, None)


def test_auth_rejection_is_a_config_error(monkeypatch) -> None:
    def rejected(*args, **kwargs):
        raise _http_error(401)

    monkeypatch.setattr(chat.urllib.request, "urlopen", rejected)
    with pytest.raises(chat.ConfigError):
        complete("key", CHAT_MODEL_ID, [], CHEAP_BUDGET)


def test_overload_and_network_errors_are_retryable(monkeypatch) -> None:
    for error in (_http_error(429), _http_error(503), urllib.error.URLError("down")):

        def failing(*args, error=error, **kwargs):
            raise error

        monkeypatch.setattr(chat.urllib.request, "urlopen", failing)
        with pytest.raises(chat.RetryableError):
            complete("key", CHAT_MODEL_ID, [], CHEAP_BUDGET)


def test_missing_key_is_a_config_error() -> None:
    with pytest.raises(chat.ConfigError):
        complete("", CHAT_MODEL_ID, [], CHEAP_BUDGET)


def test_other_client_errors_are_plain_chat_errors(monkeypatch) -> None:
    def bad_request(*args, **kwargs):
        raise _http_error(400)

    monkeypatch.setattr(chat.urllib.request, "urlopen", bad_request)
    with pytest.raises(chat.ChatError) as info:
        complete("key", CHAT_MODEL_ID, [], CHEAP_BUDGET)
    assert not isinstance(info.value, (chat.RetryableError, chat.ConfigError))


def test_smart_input_is_trimmed_to_the_token_estimate() -> None:
    history: list[dict[str, str]] = []
    for _ in range(6):
        history.append({"role": "user", "content": "а" * 1500})
        history.append({"role": "assistant", "content": "б" * 1500})
    fitted = fit_history("Бот", history, "вопрос", token_limit=2000)
    assert estimated_tokens(build_messages("Бот", fitted, "вопрос")) <= 2000
    assert len(fitted) == 2
    assert fit_history("Бот", history, "вопрос", token_limit=50) == []
