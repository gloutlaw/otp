import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "collector"))

from probe_identity import MORE

SOURCE = Path(__file__).resolve().parent / "model-compare.txt"
BLIND_TEXT = Path(__file__).resolve().parent / "blind-review.txt"
BLIND_KEY = Path(__file__).resolve().parent / "blind-key.json"
BLIND_SCORES = Path(__file__).resolve().parent / "blind-scores.json"
SAMPLE = (1, 3, 5, 7, 10, 12, 15, 18, 20, 22, 23, 25, 27, 30, 33, 35, 37, 40, 44, 48)
SEED = 20261011
LABELS = "ABCDEFG"
IDENTITY_QUESTIONS = ("18", "37")
GEMINI_ID = "google/gemini-2.5-flash-lite"
THRESHOLD_AVG = 3.5


def parse_answers(text: str) -> dict[str, dict[int, str]]:
    answers: dict[str, dict[int, str]] = {}
    parts = re.split(r"^=== (.+) ===$", text, flags=re.M)
    for model, body in zip(parts[1::2], parts[2::2]):
        per_question: dict[int, str] = {}
        for index, question in enumerate(MORE, start=1):
            header = f"\n{index}. {question}\n"
            start = body.find(header)
            if start < 0:
                raise SystemExit(f"Нет вопроса {index} в блоке {model}")
            start += len(header)
            end = len(body)
            if index < len(MORE):
                next_header = body.find(f"\n{index + 1}. {MORE[index]}\n", start)
                if next_header >= 0:
                    end = next_header
            raw = body[start:end]
            raw = re.sub(r"\nпровал: [^\n]*\s*$", "", raw.rstrip())
            per_question[index] = raw.strip()
        answers[model] = per_question
    return answers


def build_key(models: list[str]) -> dict:
    questions: dict[str, dict[str, str]] = {}
    for index in SAMPLE:
        order = list(models)
        random.Random(SEED + index).shuffle(order)
        questions[str(index)] = {LABELS[position]: model for position, model in enumerate(order)}
    return {"seed": SEED, "sample": list(SAMPLE), "questions": questions}


def main() -> None:
    if not SOURCE.is_file():
        raise SystemExit("Нет collector/model-compare.txt, сначала запустите тест.")
    answers = parse_answers(SOURCE.read_text(encoding="utf-8"))
    models = list(answers)
    key = build_key(models)

    lines = [
        "Слепая оценка русского. Имена моделей скрыты.",
        "Для каждого ответа поставьте оценку от 1 до 5 и отметьте, есть ли смысловой провал (да / нет).",
        "Ответы зависят от предыдущих реплик того же диалога, это нормально.",
        "",
    ]
    for index in SAMPLE:
        lines.append(f"## Вопрос {index}")
        lines.append("")
        lines.append(MORE[index - 1])
        lines.append("")
        for label, model in key["questions"][str(index)].items():
            lines.append(f"### {label}")
            lines.append(answers[model][index])
            lines.append("Оценка: ___  Провал по смыслу: ___")
            lines.append("")

    BLIND_TEXT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    BLIND_KEY.write_text(json.dumps(key, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Записано {BLIND_TEXT.name}: {len(SAMPLE)} вопросов, {len(SAMPLE) * len(models)} ответов")


def report() -> None:
    key = json.loads(BLIND_KEY.read_text(encoding="utf-8"))
    scores = json.loads(BLIND_SCORES.read_text(encoding="utf-8"))["scores"]
    stats: dict[str, dict] = {}
    for question, labels in key["questions"].items():
        for label, model in labels.items():
            score, fail = scores[question][label]
            entry = stats.setdefault(model, {"scores": [], "fails": 0, "identity_fails": []})
            entry["scores"].append(score)
            if fail:
                entry["fails"] += 1
                if question in IDENTITY_QUESTIONS:
                    entry["identity_fails"].append(question)
    print(f"{'модель':34} {'средняя':>8} {'провалы':>8} {'провалы на 18/37':>18}")
    for model, entry in sorted(stats.items(), key=lambda item: -sum(item[1]["scores"]) / len(item[1]["scores"])):
        average = sum(entry["scores"]) / len(entry["scores"])
        identity = ",".join(entry["identity_fails"]) or "-"
        print(f"{model:34} {average:8.2f} {entry['fails']:8d} {identity:>18}")
    gemini = stats.get(GEMINI_ID)
    if gemini:
        average = sum(gemini["scores"]) / len(gemini["scores"])
        passed = average >= THRESHOLD_AVG and not gemini["identity_fails"]
        print(f"\nGemini: средняя {average:.2f}, порог {THRESHOLD_AVG}, решение: {'переключаем' if passed else 'не переключаем'}")


if __name__ == "__main__":
    if sys.argv[1:] == ["report"]:
        report()
    else:
        main()
