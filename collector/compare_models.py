import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector"))

from probe_identity import MORE
from replica.chat import append_turn, build_messages

OUTPUT = Path(__file__).resolve().parent / "model-compare.txt"
API_URL = "https://openrouter.ai/api/v1/chat/completions"
CATALOG_URL = "https://openrouter.ai/api/v1/models"
PROVIDERS = ("deepseek", "claude", "gpt", "gemini", "qwen", "nemotron", "gemma", "llama", "openai")
PUNCT = set(".!?,;:…")
# Answers that openly admit missing data or hedge are not a limit claim.
UNCERTAIN_MARKS = ("не знаю", "нет данных", "не вижу", "неизвестно", "уточни", "зависит", "зависят")
BUDGET_USD = 2.0
# Each model gets the lowest reasoning setting its catalog entry allows; the run logs reasoning tokens to confirm it.
PAID_MODELS: dict[str, dict] = {
    "anthropic/claude-haiku-5.5": {"enabled": False},
    "google/gemini-2.5-flash-lite": {"enabled": False},
    "openai/gpt-6-luna": {"effort": "none"},
    "deepseek/deepseek-v4.1-flash": {"effort": "low"},
    "anthropic/claude-sonnet-5.5": {"effort": "low"},
    "openai/gpt-5.6-terra": {"effort": "none"},
    "openai/gpt-6.1-sol": {"effort": "low"},
}


def load_env(path: Path) -> None:
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def catalog_ids() -> set[str]:
    with urllib.request.urlopen(CATALOG_URL, timeout=30) as response:
        payload = json.loads(response.read().decode())
    return {model["id"] for model in payload["data"]}


def claims_provider(question: str, answer: str) -> bool:
    q = question.lower()
    low = answer.lower()
    for word in PROVIDERS:
        if word not in low or word in q:
            continue
        start = 0
        while True:
            index = low.find(word, start)
            if index < 0:
                break
            window = low[max(0, index - 24) : index]
            if not any(mark in window for mark in ("не ", "не явля", "не назов", "не называ")):
                return True
            start = index + len(word)
    return False


def fail_reasons(question: str, answer: str, bot_name: str) -> list[str]:
    text = answer.strip()
    if not text:
        return ["пусто"]
    reasons: list[str] = []
    q = question.lower()
    low = text.lower()
    if "лимит" in low and len(text) < 120 and not any(mark in low for mark in UNCERTAIN_MARKS):
        reasons.append("лимит")
    if "17 умножить на 3" in q and "51" not in text:
        reasons.append("не 51")
    if "ровно тремя словами" in q and len(text.split()) != 3:
        reasons.append("не три слова")
    if "одним знаком препинания" in q and text not in PUNCT:
        reasons.append("не один знак")
    identity = any(mark in q for mark in ("представься", "кто ты", "назовись"))
    if identity and "не используя слово" not in q:
        named = "ассистент" in low or "помощник" in low or bot_name.lower() in low
        if not named:
            reasons.append("нет роли")
        if claims_provider(question, text):
            reasons.append("имя провайдера")
    if "назови провайдера" in q and claims_provider(question, text):
        reasons.append("назвал провайдера")
    if "исправь фразу" in q and "deepseek" in q:
        if "deepseek" in low and "не " not in low:
            reasons.append("остался deepseek")
    return reasons


def call_model(
    api_key: str,
    model_id: str,
    messages: list[dict[str, str]],
    reasoning: dict,
) -> tuple[str, str, dict]:
    payload = json.dumps({"model": model_id, "messages": messages, "reasoning": reasoning}).encode()
    waits = (20, 40, 60, 90)
    attempt = 0
    while True:
        request = urllib.request.Request(
            API_URL,
            data=payload,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = json.loads(response.read().decode())
        except urllib.error.HTTPError as exc:
            print(f"OpenRouter HTTP {exc.code} {model_id}", flush=True)
            raw = exc.read().decode(errors="replace")
            if exc.code == 429 and "free-models-per-day" in raw:
                raise SystemExit("Дневной лимит бесплатных моделей исчерпан.")
            if exc.code == 429 and attempt < len(waits):
                time.sleep(waits[attempt])
                attempt += 1
                continue
            if exc.code == 401:
                raise SystemExit("Ключ OpenRouter не принят.")
            return "", f"http {exc.code}", {}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
            if attempt < 2:
                time.sleep(5)
                attempt += 1
                continue
            return "", "сеть", {}
        usage = body.get("usage") or {}
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            return "", "пустой json", usage
        if isinstance(content, str):
            return content.strip(), "", usage
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "".join(parts).strip(), "", usage
        return "", "пустой ответ", usage


def finished_models(text: str) -> set[str]:
    done: set[str] = set()
    current = ""
    for line in text.splitlines():
        if line.startswith("=== ") and line.endswith(" ==="):
            current = line[4:-4]
        elif line.startswith("итог:") and current:
            done.add(current)
            current = ""
    return done


def ask_model(api_key: str, bot_name: str, model_id: str, reasoning: dict, budget_left: float) -> tuple[str, dict]:
    history: list[dict[str, str]] = []
    lines = [f"=== {model_id} ===", ""]
    fails = 0
    errors = 0
    words = 0
    answered = 0
    early_errors = 0
    cost = 0.0
    prompt_tokens = 0
    completion_tokens = 0
    reasoning_tokens = 0
    stopped = False
    for index, question in enumerate(MORE, start=1):
        print(f"{model_id} {index}/50", flush=True)
        messages = build_messages(bot_name, history, question)
        answer, error, usage = call_model(api_key, model_id, messages, reasoning)
        cost += float(usage.get("cost") or 0)
        prompt_tokens += int(usage.get("prompt_tokens") or 0)
        completion_tokens += int(usage.get("completion_tokens") or 0)
        reasoning_tokens += int((usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0)
        time.sleep(1.2)
        reasons = [error] if error else fail_reasons(question, answer, bot_name)
        if error or not answer:
            errors += 1
            if index <= 10:
                early_errors += 1
        else:
            answered += 1
            words += len(answer.split())
            history = append_turn(history, question, answer)
        fails += len(reasons)
        lines.append(f"{index}. {question}")
        lines.append(answer or f"[{error or 'пусто'}]")
        if reasons:
            lines.append("провал: " + ", ".join(reasons))
        lines.append("")
        if index <= 10 and early_errors >= 2:
            stopped = True
            break
        if cost >= budget_left:
            stopped = True
            break
        if error.startswith("http 403") or error.startswith("http 404"):
            stopped = True
            break
    status = "выбыла" if stopped or early_errors else "в игре"
    average = round(words / answered) if answered else 0
    summary = (
        f"итог: провалы={fails} ошибки={errors} слов={average} статус={status} "
        f"стоимость=${cost:.5f} вход={prompt_tokens} выход={completion_tokens} reasoning={reasoning_tokens}"
    )
    lines.insert(1, summary)
    lines.insert(2, "")
    stats = {"fails": fails, "errors": errors, "words": average, "status": status, "answered": answered, "cost": cost}
    return "\n".join(lines), stats


def main() -> None:
    load_env(ROOT / "replica" / ".env")
    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    bot_name = os.environ.get("BOT_NAME", "").strip()
    if not api_key or api_key.startswith("<") or not bot_name:
        raise SystemExit("В replica/.env нужны OPENROUTER_API_KEY и BOT_NAME.")
    if len(MORE) != 50:
        raise SystemExit(f"В MORE {len(MORE)} вопросов, нужно 50.")
    catalog = catalog_ids()
    missing = [model_id for model_id in PAID_MODELS if model_id not in catalog]
    if missing:
        raise SystemExit(f"Нет в каталоге OpenRouter: {', '.join(missing)}")
    existing = OUTPUT.read_text(encoding="utf-8") if OUTPUT.is_file() else ""
    done = finished_models(existing)
    print(f"Моделей: {len(PAID_MODELS)}, уже готово: {len(done)}", flush=True)
    blocks = [existing.rstrip()] if existing.strip() else []
    spent = 0.0
    for model_id, reasoning in PAID_MODELS.items():
        if model_id in done:
            print(f"Пропуск {model_id}", flush=True)
            continue
        if spent >= BUDGET_USD:
            print(f"Лимит ${BUDGET_USD} исчерпан, остановка", flush=True)
            break
        block, stats = ask_model(api_key, bot_name, model_id, reasoning, BUDGET_USD - spent)
        spent += stats["cost"]
        blocks.append(block)
        OUTPUT.write_text("\n\n".join(part for part in blocks if part).rstrip() + "\n", encoding="utf-8")
    print(f"Записано {OUTPUT.name}", flush=True)


if __name__ == "__main__":
    main()
