from dataclasses import dataclass

from replica.catalog import DEFAULT_CHAT_MODEL, chat_model_by_title
from replica.screens import BACK, COMMANDS, button_on


@dataclass(frozen=True)
class Next:
    screen_id: str


@dataclass(frozen=True)
class Closed:
    screen_id: str


@dataclass(frozen=True)
class NotBuilt:
    screen_id: str


@dataclass(frozen=True)
class Choose:
    screen_id: str
    model_id: str | None


@dataclass(frozen=True)
class Back:
    screen_id: str


DEFAULT_MODEL_ID = DEFAULT_CHAT_MODEL.slug


def model_changed_text(title: str) -> str:
    return f"✅ **Модель изменена**\n\nТеперь используется {title}"


Outcome = Next | Closed | NotBuilt | Choose | Back


def normalize_action(action: str) -> str:
    text = action.strip()
    if not text.startswith("/"):
        return text
    head = text.split()[0]
    return head.split("@", 1)[0]


def step(screen_id: str, action: str) -> Outcome:
    normalized = normalize_action(action)
    command_target = COMMANDS.get(normalized)
    if command_target is not None:
        return Next(command_target)
    button = button_on(screen_id, normalized)
    if button is None:
        return NotBuilt(screen_id)
    if button.closed:
        return Closed(screen_id)
    if button.target == BACK:
        return Back(screen_id)
    if screen_id == "models" and button.target is None:
        choice = chat_model_by_title(button.label)
        return Choose(screen_id, choice.slug) if choice is not None else NotBuilt(screen_id)
    if button.target is None:
        return NotBuilt(screen_id)
    return Next(button.target)
