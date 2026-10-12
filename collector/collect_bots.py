import asyncio
import os
import struct
import subprocess
import zlib
from dataclasses import dataclass, field
from pathlib import Path

import segno
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.tl.functions.users import GetFullUserRequest
from telethon.errors import (
    DataInvalidError,
    FloodWaitError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SendCodeUnavailableError,
    SessionPasswordNeededError,
)
from telethon.tl import types
from telethon.tl.custom import Message

ROOT = Path(__file__).resolve().parent
PAUSE_AFTER_ACTION_SECONDS = 3
PAUSE_BETWEEN_ACTIONS_SECONDS = 2
RECENT_MESSAGES = 10
MAX_DEPTH = 2

LINK_TYPES = (types.InlineButtonTypeUrl, types.InlineButtonTypeUrlAuth)
WEB_TYPES = (types.InlineButtonTypeWebView, types.ButtonTypeSimpleWebView)


@dataclass
class ButtonNote:
    label: str
    detail: str = ""


@dataclass
class Section:
    title: str
    texts: list[str] = field(default_factory=list)
    buttons: list[ButtonNote] = field(default_factory=list)
    children: list["Section"] = field(default_factory=list)


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise SystemExit(
            f"В .env нет {name}. Скопируйте .env.example в .env и заполните ключи с my.telegram.org."
        )
    return value


def load_bots(path: Path) -> list[str]:
    if not path.exists():
        raise SystemExit("Нет файла bots.txt. Добавьте юзернеймы ботов, по одному на строку.")

    bots: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        name = raw_line.strip()
        if not name or name.startswith("#"):
            continue
        if not name.startswith("@"):
            name = f"@{name}"
        bots.append(name)
    return bots


def describe_sent_code(sent) -> str:
    code_type = sent.type
    length = getattr(code_type, "length", None)
    suffix = f" Цифр в коде: {length}." if isinstance(length, int) else ""
    if isinstance(code_type, types.auth.SentCodeTypeApp):
        text = (
            "Код отправлен внутри приложения Telegram, в служебный чат «Telegram». "
            "Это не SMS и не код со страницы my.telegram.org."
        )
    elif isinstance(code_type, (
        types.auth.SentCodeTypeSms,
        types.auth.SentCodeTypeFirebaseSms,
        types.auth.SentCodeTypeFragmentSms,
        types.auth.SentCodeTypeSmsPhrase,
        types.auth.SentCodeTypeSmsWord,
    )):
        text = "Код отправлен по SMS на номер, который вы ввели."
    elif isinstance(code_type, types.auth.SentCodeTypeCall):
        text = "Telegram продиктует код входящим звонком."
    elif isinstance(code_type, (types.auth.SentCodeTypeFlashCall, types.auth.SentCodeTypeMissedCall)):
        text = "Код — это последние цифры входящего или пропущенного звонка."
    elif isinstance(code_type, types.auth.SentCodeTypeEmailCode):
        text = "Код отправлен на почту, привязанную к аккаунту."
    elif isinstance(code_type, types.auth.SentCodeTypeSetUpEmailRequired):
        text = "Telegram просит сначала привязать почту в официальном приложении. Код входа пока не отправлен."
    else:
        text = "Telegram отправил код. Проверьте служебный чат «Telegram», уведомления и SMS."
    return text + suffix


def show_qr(url: str) -> Path:
    qr_path = ROOT / "login-qr.svg"
    segno.make(url).save(str(qr_path), scale=8)
    subprocess.run(["open", str(qr_path)], check=False)
    return qr_path


async def ask_cloud_password(client: TelegramClient) -> None:
    print("QR принят. Дальше нужен пароль двухфакторной защиты. Введите его в эту консоль.", flush=True)
    password = input("Пароль двухфакторной защиты: ")
    await client.sign_in(password=password)
    print("Вход выполнен.", flush=True)


async def sign_in_with_qr(client: TelegramClient) -> None:
    qr_path = ROOT / "login-qr.svg"
    print(
        "На телефоне откройте Telegram с этим аккаунтом: Настройки → Устройства → Подключить устройство. "
        "Наведите камеру на QR-код. Код держится на экране около полутора минут.",
        flush=True,
    )
    try:
        try:
            qr_login = await client.qr_login()
        except SessionPasswordNeededError:
            await ask_cloud_password(client)
            return

        for _ in range(3):
            show_qr(qr_login.url)
            try:
                await qr_login.wait(timeout=90)
                print("Вход по QR выполнен.", flush=True)
                return
            except SessionPasswordNeededError:
                await ask_cloud_password(client)
                return
            except TimeoutError:
                print("QR истёк. Открываю новый.", flush=True)
                try:
                    await qr_login.recreate()
                except SessionPasswordNeededError:
                    await ask_cloud_password(client)
                    return
    finally:
        qr_path.unlink(missing_ok=True)

    raise SystemExit("Не дождался сканирования QR. Запустите скрипт снова.")


async def sign_in_with_code(client: TelegramClient) -> None:
    phone = input("Номер телефона в международном формате: ").strip()
    try:
        sent = await client.send_code_request(phone)
    except FloodWaitError as exc:
        raise SystemExit(
            f"Telegram временно не отправляет новые коды. Подождите {exc.seconds} секунд и запустите скрипт снова."
        ) from exc

    print(describe_sent_code(sent), flush=True)
    print("Если сообщения нет, нажмите Enter без кода. Если SMS недоступен, скрипт откроет QR.", flush=True)

    for _ in range(3):
        code = input("Код из Telegram (или пустой Enter, чтобы отправить ещё раз): ").strip()
        if not code:
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                raise SystemExit(
                    f"Telegram временно не отправляет новые коды. Подождите {exc.seconds} секунд и запустите скрипт снова."
                ) from exc
            except SendCodeUnavailableError:
                print(
                    "SMS для этого номера недоступен. Код только внутри приложения Telegram. Переключаюсь на QR.",
                    flush=True,
                )
                await sign_in_with_qr(client)
                return
            print(describe_sent_code(sent), flush=True)
            continue
        try:
            await client.sign_in(phone, code)
            return
        except SessionPasswordNeededError:
            password = input("Пароль двухфакторной защиты: ")
            await client.sign_in(password=password)
            return
        except PhoneCodeInvalidError:
            print("Код не подошёл. Введите его ещё раз или нажмите Enter для новой отправки.", flush=True)
        except PhoneCodeExpiredError:
            print("Код устарел. Запрашиваю новый.", flush=True)
            try:
                sent = await client.send_code_request(phone)
            except FloodWaitError as exc:
                raise SystemExit(
                    f"Telegram временно не отправляет новые коды. Подождите {exc.seconds} секунд и запустите скрипт снова."
                ) from exc
            except SendCodeUnavailableError:
                print(
                    "SMS для этого номера недоступен. Код только внутри приложения Telegram. Переключаюсь на QR.",
                    flush=True,
                )
                await sign_in_with_qr(client)
                return
            print(describe_sent_code(sent), flush=True)

    raise SystemExit("Не удалось войти. Подождите несколько минут и запустите скрипт снова.")


async def ensure_signed_in(client: TelegramClient) -> None:
    await client.connect()
    if await client.is_user_authorized():
        return

    print(
        "1 — войти QR-кодом с телефона, где этот аккаунт уже открыт\n"
        "2 — ввести номер и цифровой код из чата «Telegram»",
        flush=True,
    )
    choice = input("Выбор [1]: ").strip()
    if choice == "2":
        await sign_in_with_code(client)
        return
    await sign_in_with_qr(client)


def button_action(button) -> tuple[str, str, str]:
    raw_type = getattr(button.button, "type", None)
    if raw_type is None:
        return "other", " [не нажималась]", ""

    url = getattr(raw_type, "url", None)
    if isinstance(raw_type, LINK_TYPES):
        detail = f" -> {url}" if url else ""
        return "link", detail, ""
    if isinstance(raw_type, WEB_TYPES):
        detail = " [Web App не открывался]"
        if url:
            detail = f"{detail} {url}"
        return "webapp", detail, ""
    if isinstance(raw_type, types.ButtonTypeRequestPhone):
        return "phone", " [не нажималась: запрос телефона]", ""
    if isinstance(raw_type, types.ButtonTypeRequestGeoLocation):
        return "geo", " [не нажималась: запрос геолокации]", ""
    if isinstance(raw_type, types.InlineButtonTypeBuy):
        return "pay", " [не нажималась: оплата]", ""
    if isinstance(raw_type, types.InlineButtonTypeCallback):
        if raw_type.requires_password:
            return "other", " [не нажималась: нужен пароль]", ""
        data = raw_type.data or b""
        return "click", "", "cb:" + data.hex()
    if isinstance(raw_type, types.ButtonTypeDefault):
        return "click", "", "text:" + button.text

    return "other", " [не нажималась]", ""


def flat_buttons(message) -> list:
    if not message or not message.buttons:
        return []
    return [button for row in message.buttons for button in row]


def snapshot(messages: list) -> tuple[list[str], object | None]:
    texts: list[str] = []
    menu = None
    for message in messages:
        text = (message.text or "").strip()
        if text:
            texts.append(text)
        if message.buttons:
            menu = message
    return texts, menu


def screen_key(texts: list[str], labels: list[str]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return (tuple(texts), tuple(labels))


def button_labels(menu) -> list[str]:
    return [button.text for button in flat_buttons(menu)]


def extract_alert(result) -> str:
    if isinstance(result, Message):
        return ""
    alert = getattr(result, "message", None)
    if isinstance(alert, str):
        return alert.strip()
    return ""


async def incoming_after(client, entity, after_id: int) -> list:
    await asyncio.sleep(PAUSE_AFTER_ACTION_SECONDS)
    batch = await client.get_messages(entity, limit=RECENT_MESSAGES)
    fresh = [message for message in batch if not message.out and message.id > after_id]
    if fresh:
        fresh.reverse()
        return fresh

    edited = [message for message in batch if not message.out and message.id == after_id]
    edited.reverse()
    return edited


async def explore(client, entity, messages: list, title: str, depth: int, visited: set[str]) -> Section:
    texts, menu = snapshot(messages)
    if not texts:
        texts = ["[Бот не прислал текст]"]

    section = Section(title=title, texts=texts)
    if menu is None:
        return section

    buttons = flat_buttons(menu)
    before = screen_key(texts, button_labels(menu))

    for button in buttons:
        kind, detail, key = button_action(button)
        if kind == "click" and key in visited:
            section.buttons.append(ButtonNote(button.text, " [уже открыта]"))
            continue

        section.buttons.append(ButtonNote(button.text, detail))
        if kind != "click" or depth >= MAX_DEPTH:
            continue

        visited.add(key)
        print(f"Открываю: {button.text}", flush=True)
        try:
            result = await button.click()
        except Exception as exc:
            section.children.append(Section(button.text, [f"[Ошибка]: {exc}"]))
            await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
            continue

        alert = extract_alert(result)
        child_messages = await incoming_after(client, entity, menu.id)
        child_texts, child_menu = snapshot(child_messages)
        child_key = screen_key(child_texts, button_labels(child_menu))
        if child_key == before:
            texts = [alert] if alert else ["[Экран не изменился]"]
            section.children.append(Section(button.text, texts))
        else:
            child = await explore(client, entity, child_messages, button.text, depth + 1, visited)
            if alert and alert not in child.texts:
                child.texts.insert(0, alert)
            section.children.append(child)
        await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)

    return section


def render_section(section: Section, depth: int) -> list[str]:
    indent = "  " * depth
    lines = [f"{indent}{section.title}"]
    if section.texts:
        lines.append(f"{indent}Текст:")
        for text in section.texts:
            for line in text.splitlines() or [""]:
                lines.append(f"{indent}  {line}")
    if section.buttons:
        lines.append(f"{indent}Кнопки:")
        for button in section.buttons:
            lines.append(f"{indent}- {button.label}{button.detail}")
    for child in section.children:
        lines.append("")
        lines.extend(render_section(child, depth + 1))
    return lines


CHAT_MODEL_LABELS = {
    "Auto",
    "GPT-5 mini",
    "Gemini Flash",
    "DeepSeek V4",
    "GPT-5.5",
    "GPT-5.4",
    "GPT-5.4 mini",
    "Claude Opus",
    "Claude Sonnet",
    "Gemini Pro",
    "Gemini 3.1 Pro · скоро",
}
NAVIGATION_LABELS = {"Назад", "Главное меню", "Закрыть"}
DESTRUCTIVE_LABELS = {"Очистить", "🗑 Очистить"}
COMMAND_PATHS = (
    ("Premium", "Premium"),
    ("Premium", "AI Pass"),
    ("Premium", "Ultimate"),
    ("Premium", "Генерации"),
    ("Premium", "Докупить запросы"),
    ("Профиль", "Изменить имя"),
    ("Рефералы", "Статистика"),
    ("Рефералы", "Моя ссылка"),
    ("Рефералы", "Награды"),
    ("Рефералы", "Баланс"),
    ("История", "🔁 Повторить"),
    ("Лимиты", "Открыть тарифы"),
    ("AI-центр", "Режим AI"),
    ("AI-центр", "Стиль AI"),
    ("AI-центр", "Формат ответов"),
    ("AI-центр", "AI Provider"),
    ("AI-центр", "Auto Routing"),
    ("AI-центр", "Персонализация"),
    ("Создать AI агента", "Создать AI агента"),
    ("Создать AI агента", "Мои агенты"),
    ("Создать AI агента", "Подключённые чаты"),
    ("Модели", "GPT-5.4"),
    ("Модели", "GPT-5.4 mini"),
    ("Модели", "Claude Opus"),
    ("Модели", "Claude Sonnet"),
    ("Модели", "Gemini Pro"),
    ("Модели", "Gemini 3.1 Pro · скоро"),
    ("Модели", "DeepSeek V4"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "🖼 GPT Image"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "🍌 Nano Banana 2"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "🌙 Seedream 4.5"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "🌌 FLUX.2 Pro"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "GPT Image 2"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "🍌 Nano Banana Pro"),
    ("Генерации", "Изображения", "🤖 Выбрать модель", "Fast Image"),
    ("Генерации", "Изображения", "🖼 Качество: 1K", "2K · купленные"),
    ("Генерации", "Изображения", "🖼 Качество: 1K", "4K · купленные"),
    ("Генерации", "Изображения", "🖼 Качество: 1K", "1K"),
    ("Генерации", "Изображения", "📐 Формат: 1:1", "📐 3:4"),
    ("Генерации", "Изображения", "📐 Формат: 1:1", "📐 4:3"),
    ("Генерации", "Изображения", "📐 Формат: 1:1", "1:1"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Аватары"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Логотипы"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Постеры"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Товары / реклама"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Соцсети"),
    ("Генерации", "Изображения", "💡 Популярные промты", "Арт / стиль"),
    ("Генерации", "Изображения", "✨ Создать изображение"),
)
REPLY_ACTIONS = ("✨ Улучшить", "✂️ Сократить")
SLOW_LABELS = {"🔁 Повторить", "✨ Создать изображение", "✨ Улучшить", "✂️ Сократить"}
GAP_PATHS = (
    ("Premium",),
    ("Генерации", "Видео"),
    ("Генерации", "Документы"),
    ("Генерации", "Музыка"),
    ("Профиль",),
    ("Рефералы",),
    ("История",),
    ("Лимиты",),
    ("AI-центр",),
    ("Создать AI агента",),
    ("Генерации", "Изображения", "🤖 Выбрать модель"),
    ("Генерации", "Изображения", "🖼 Качество: 1K"),
    ("Генерации", "Изображения", "📐 Формат: 1:1"),
    ("Генерации", "Изображения", "💡 Популярные промты"),
    ("Генерации", "Изображения", "ℹ️ Инструкция"),
)


def find_button(message, label: str):
    buttons = flat_buttons(message)
    for button in buttons:
        if button.text == label:
            return button
    prefix = label.split(":", 1)[0].strip()
    if prefix != label:
        for button in buttons:
            if button.text.startswith(prefix):
                return button
    for button in buttons:
        if label in button.text:
            return button
    return None


def skip_reason(button) -> str:
    text = button.text
    if text in NAVIGATION_LABELS:
        return " [служебная, не открывалась]"
    if text in DESTRUCTIVE_LABELS:
        return " [удаление, не нажималась]"
    if "⭐" in text or text.startswith("Купить"):
        return " [оплата, не нажималась]"
    kind, detail, _key = button_action(button)
    if kind != "click":
        return detail
    return ""


def is_stale_click(exc: Exception) -> bool:
    return isinstance(exc, DataInvalidError) or "Encrypted data invalid" in str(exc)


async def reload_message(client, entity, message_id: int):
    loaded = await client.get_messages(entity, ids=message_id)
    if isinstance(loaded, list):
        return loaded[0] if loaded else None
    return loaded


async def message_with_label(client, entity, label: str, prefer_id: int | None, min_id: int):
    if prefer_id is not None:
        current = await reload_message(client, entity, prefer_id)
        if current is not None and current.id >= min_id and find_button(current, label):
            return current

    batch = await client.get_messages(entity, limit=RECENT_MESSAGES)
    matches = [
        message
        for message in batch
        if not message.out and message.id >= min_id and find_button(message, label)
    ]
    if not matches:
        return None
    matches.sort(key=lambda message: message.id)
    return matches[-1]


async def screen_after_click(client, entity, clicked_id: int) -> list:
    await asyncio.sleep(PAUSE_AFTER_ACTION_SECONDS)
    batch = await client.get_messages(entity, limit=RECENT_MESSAGES)
    newer = [message for message in batch if not message.out and message.id > clicked_id]
    newer.sort(key=lambda message: message.id)
    if newer:
        return newer

    edited = await reload_message(client, entity, clicked_id)
    if edited is not None and not edited.out:
        return [edited]
    return []


async def click_label(client, entity, screen, label: str, min_id: int):
    last_error = ""
    for _attempt in range(2):
        current = await message_with_label(client, entity, label, getattr(screen, "id", None), min_id)
        if current is None:
            return None, "", f"[Кнопка не найдена: {label}]"

        button = find_button(current, label)
        reason = skip_reason(button)
        if reason:
            return None, "", f"[{label} не нажималась:{reason}]"

        try:
            result = await button.click()
        except FloodWaitError:
            raise
        except Exception as exc:
            last_error = str(exc)
            if is_stale_click(exc):
                await asyncio.sleep(PAUSE_AFTER_ACTION_SECONDS)
                continue
            return None, "", f"[Ошибка]: {exc}"

        messages = await screen_after_click(client, entity, current.id)
        alert = extract_alert(result)
        return messages, alert, ""

    return None, "", f"[Ошибка]: {last_error}"


def section_from_messages(title: str, messages: list, alert: str) -> Section:
    texts = []
    menu = None
    for message in messages:
        text = (message.text or "").strip()
        if text and text not in texts:
            texts.append(text)
        if message.buttons:
            menu = message
    if alert and alert not in texts:
        texts.insert(0, alert)
    if not texts:
        texts = ["[Бот не прислал текст]"]
    notes = [ButtonNote(button.text, skip_reason(button)) for button in flat_buttons(menu)]
    return Section(title, texts, notes)


async def open_gap(client, entity, steps: tuple[str, ...]) -> Section:
    title = " → ".join(steps)
    print(f"Открываю: {title}", flush=True)
    try:
        sent = await client.send_message(entity, "/start")
    except FloodWaitError:
        raise
    except Exception as exc:
        return Section(title, [f"[Ошибка]: {exc}"])

    messages = await screen_after_click(client, entity, sent.id)
    screen = next((message for message in reversed(messages) if message.buttons), None)
    if screen is None and messages:
        screen = messages[-1]
    if screen is None:
        return Section(title, ["[Бот не прислал текст]"])

    floor_id = screen.id
    for index, label in enumerate(steps):
        clicked_from = floor_id
        opened, alert, error = await click_label(client, entity, screen, label, floor_id)
        if error:
            return Section(title, [error])
        if not opened:
            return Section(title, ["[Бот не прислал текст]"])
        if index == len(steps) - 1 and label in SLOW_LABELS:
            slower = await wait_for_replies(client, entity, clicked_from)
            if slower:
                opened = slower
        screen = next((message for message in reversed(opened) if message.buttons), opened[-1])
        floor_id = screen.id
        if index == len(steps) - 1:
            section = section_from_messages(title, opened, alert)
            await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
            return section
        await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)

    return Section(title, ["[Бот не прислал текст]"])


async def fill_gaps(client, username: str) -> str:
    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        return f"=== ДОБОР: {username} ===\n[Ошибка при подключении]: {exc}\n\n"

    sections = []
    for steps in GAP_PATHS:
        try:
            sections.append(await open_gap(client, entity, steps))
        except FloodWaitError as exc:
            sections.append(Section(" → ".join(steps), [
                f"[Ошибка]: Telegram просит подождать {exc.seconds} секунд",
            ]))
            break

    lines = [f"=== ДОБОР: {username} ===", ""]
    for index, section in enumerate(sections):
        if index:
            lines.append("")
        lines.extend(render_section(section, 0))
    return "\n".join(lines).rstrip() + "\n\n"


async def analyze_bot(client, username: str) -> str:
    try:
        entity = await client.get_entity(username)
        sent = await client.send_message(entity, "/start")
        messages = await incoming_after(client, entity, sent.id)
        section = await explore(client, entity, messages, "/start", 0, set())
        lines = [f"=== БОТ: {username} ===", ""]
        lines.extend(render_section(section, 0))
        return "\n".join(lines).rstrip() + "\n\n"
    except Exception as exc:
        return f"=== БОТ: {username} ===\n[Ошибка при подключении]: {exc}\n\n"


PROBE_TEXT = "Коротко объясни одним предложением, что ты умеешь."
PROBE_WAIT_SECONDS = 45
PROBE_QUIET_SECONDS = 5
CAT_IMAGE = ROOT / "probe-cat.png"


def write_cat_png(path: Path) -> None:
    size = 48

    def pixel(x: int, y: int) -> bytes:
        def dist(cx: float, cy: float) -> float:
            return ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5

        if dist(14, 10) < 8 and y < 16 and x < 22:
            return b"\xe8\x8a\x3a"
        if dist(34, 10) < 8 and y < 16 and x > 26:
            return b"\xe8\x8a\x3a"
        if dist(24, 26) < 16:
            if dist(18, 24) < 2 or dist(30, 24) < 2:
                return b"\x2b\x2b\x2b"
            if dist(24, 30) < 2:
                return b"\xd4\x5d\x4a"
            return b"\xf0\xa0\x55"
        return b"\xf7\xef\xd8"

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + b"".join(pixel(x, y) for x in range(size)) for y in range(size))
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def attachment_note(message) -> str:
    if message.photo:
        return "[Бот прислал фото]"
    if getattr(message, "video", None):
        return "[Бот прислал видео]"
    if getattr(message, "voice", None) or getattr(message, "audio", None):
        return "[Бот прислал аудио]"
    if message.document:
        return "[Бот прислал файл]"
    if message.sticker:
        return "[Бот прислал стикер]"
    return ""


def render_exchange(title: str, sent_note: str, messages: list) -> list[str]:
    lines = [title, "Отправлено:", f"  {sent_note}"]
    if not messages:
        lines.extend(["Ответ:", "  [Бот не ответил за отведённое время]"])
        return lines

    lines.append("Ответ:")
    for message in messages:
        text = (message.text or "").strip()
        if text:
            for line in text.splitlines():
                lines.append(f"  {line}")
        note = attachment_note(message)
        if note:
            lines.append(f"  {note}")
        for button in flat_buttons(message):
            lines.append(f"  Кнопка: {button.text}{skip_reason(button)}")
    return lines


async def wait_for_replies(client, entity, after_id: int) -> list:
    deadline = asyncio.get_running_loop().time() + PROBE_WAIT_SECONDS
    last_change = asyncio.get_running_loop().time()
    signature: tuple = ()
    collected: list = []
    while asyncio.get_running_loop().time() < deadline:
        batch = await client.get_messages(entity, limit=RECENT_MESSAGES)
        fresh = [message for message in batch if not message.out and message.id > after_id]
        fresh.sort(key=lambda message: message.id)
        current = tuple(
            (message.id, (message.text or "").strip(), bool(message.photo), bool(message.document))
            for message in fresh
        )
        if current != signature:
            signature = current
            collected = fresh
            last_change = asyncio.get_running_loop().time()
        if collected and asyncio.get_running_loop().time() - last_change >= PROBE_QUIET_SECONDS:
            break
        await asyncio.sleep(2)
    return collected


async def probe_chat(client, username: str) -> str:
    write_cat_png(CAT_IMAGE)
    try:
        entity = await client.get_entity(username)
        print("Отправляю фото котика...", flush=True)
        photo = await client.send_file(entity, CAT_IMAGE, force_document=False)
        photo_replies = await wait_for_replies(client, entity, photo.id)
        await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)

        print("Отправляю текст...", flush=True)
        text_message = await client.send_message(entity, PROBE_TEXT)
        text_replies = await wait_for_replies(client, entity, text_message.id)
    except Exception as exc:
        return f"=== ПРОБА: {username} ===\n[Ошибка]: {exc}\n\n"

    lines = [f"=== ПРОБА: {username} ===", ""]
    lines.extend(render_exchange("Фото", "фото котика", photo_replies))
    lines.append("")
    lines.extend(render_exchange("Текст", PROBE_TEXT, text_replies))
    return "\n".join(lines).rstrip() + "\n\n"


async def listed_commands(client, entity) -> list[tuple[str, str]]:
    full = await client(GetFullUserRequest(entity))
    info = getattr(full.full_user, "bot_info", None)
    if info is None:
        return []
    infos = info if isinstance(info, list) else [info]
    found: list[tuple[str, str]] = []
    for item in infos:
        for command in getattr(item, "commands", None) or []:
            name = command.command
            if not name.startswith("/"):
                name = "/" + name
            found.append((name, getattr(command, "description", "") or ""))
    return found


async def send_slash(client, entity, command: str, description: str) -> Section:
    print(f"Команда: {command}", flush=True)
    try:
        sent = await client.send_message(entity, command)
    except FloodWaitError:
        raise
    except Exception as exc:
        return Section(command, [f"[Ошибка]: {exc}"])
    replies = await wait_for_replies(client, entity, sent.id)
    section = section_from_messages(command, replies, "")
    if description and description not in section.texts:
        section.texts.insert(0, description)
    await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
    return section


async def click_reply_action(client, entity, label: str) -> Section:
    print(f"Открываю: {label}", flush=True)
    batch = await client.get_messages(entity, limit=20)
    host = next((message for message in batch if not message.out and find_button(message, label)), None)
    if host is None:
        return Section(label, [f"[Кнопка не найдена: {label}]"])
    opened, alert, error = await click_label(client, entity, host, label, host.id)
    if error:
        return Section(label, [error])
    replies = await wait_for_replies(client, entity, host.id)
    await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
    return section_from_messages(label, replies or opened or [], alert)


async def sweep_commands(client, username: str) -> str:
    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        return f"=== КОМАНДЫ: {username} ===\n[Ошибка при подключении]: {exc}\n\n"

    sections: list[Section] = []
    stopped = False
    try:
        commands = await listed_commands(client, entity)
    except Exception as exc:
        commands = []
        sections.append(Section("Список команд", [f"[Ошибка]: {exc}"]))
    if not any(name == "/help" for name, _description in commands):
        commands.append(("/help", ""))

    for name, description in commands:
        if name == "/start":
            continue
        try:
            sections.append(await send_slash(client, entity, name, description))
        except FloodWaitError as exc:
            sections.append(Section(name, [f"[Ошибка]: Telegram просит подождать {exc.seconds} секунд"]))
            stopped = True
            break

    if not stopped:
        for steps in COMMAND_PATHS:
            try:
                sections.append(await open_gap(client, entity, steps))
            except FloodWaitError as exc:
                sections.append(Section(" → ".join(steps), [
                    f"[Ошибка]: Telegram просит подождать {exc.seconds} секунд",
                ]))
                stopped = True
                break

    if not stopped:
        for label in REPLY_ACTIONS:
            try:
                sections.append(await click_reply_action(client, entity, label))
            except FloodWaitError as exc:
                sections.append(Section(label, [f"[Ошибка]: Telegram просит подождать {exc.seconds} секунд"]))
                break

    lines = [f"=== КОМАНДЫ: {username} ===", ""]
    for index, section in enumerate(sections):
        if index:
            lines.append("")
        lines.extend(render_section(section, 0))
    return "\n".join(lines).rstrip() + "\n\n"


DIALOG_PROMPTS = (
    ("Вопрос", "Сколько будет 17 умножить на 3?"),
    ("Текст", "Напиши два предложения о пользе прогулки."),
    ("Рассуждение", "Что логичнее взять в дождь: зонт или солнечные очки? Ответь одним предложением."),
)
WIZARD_ANSWERS = (
    "Тестовый помощник",
    "Коротко отвечает на учебные вопросы",
    "Спокойный и деловой",
)


def has_star_invoice(messages: list) -> bool:
    for message in messages:
        if "⭐" in (message.text or ""):
            return True
        for button in flat_buttons(message):
            if skip_reason(button).startswith(" [оплата"):
                return True
    return False


def is_chat_link(button) -> bool:
    text = button.text.lower()
    return "подключ" in text or "привяз" in text


async def send_when_ready(client, entity, text: str):
    while True:
        try:
            return await client.send_message(entity, text)
        except FloodWaitError as exc:
            print(f"Telegram просит подождать {exc.seconds} с.", flush=True)
            await asyncio.sleep(exc.seconds + 1)


async def run_dialog(client, username: str) -> str:
    try:
        entity = await client.get_entity(username)
    except Exception as exc:
        return f"=== ДИАЛОГ: {username} ===\n[Ошибка при подключении]: {exc}\n\n"

    lines = [f"=== ДИАЛОГ: {username} ===", ""]
    try:
        print("Сбрасываю ожидание имени агента...", flush=True)
        await send_when_ready(client, entity, "/start")
        await asyncio.sleep(PAUSE_AFTER_ACTION_SECONDS)

        for title, prompt in DIALOG_PROMPTS:
            print(f"Диалог: {title}", flush=True)
            sent = await send_when_ready(client, entity, prompt)
            replies = await wait_for_replies(client, entity, sent.id)
            if lines[-1] != "":
                lines.append("")
            lines.extend(render_exchange(title, prompt, replies))
            if has_star_invoice(replies):
                lines.extend(["", "Дальше счёт в звёздах. Оплату не подтверждаю."])
                return "\n".join(lines).rstrip() + "\n\n"
            await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)

        for label in REPLY_ACTIONS:
            print(f"Кнопка ответа: {label}", flush=True)
            while True:
                try:
                    section = await click_reply_action(client, entity, label)
                    break
                except FloodWaitError as exc:
                    print(f"Telegram просит подождать {exc.seconds} с.", flush=True)
                    await asyncio.sleep(exc.seconds + 1)
            lines.append("")
            lines.extend(render_section(section, 0))
            if any("⭐" in text for text in section.texts):
                lines.extend(["", "Дальше счёт в звёздах. Оплату не подтверждаю."])
                return "\n".join(lines).rstrip() + "\n\n"

        print("Мастер агента...", flush=True)
        while True:
            try:
                wizard = await open_gap(client, entity, ("Создать AI агента", "Создать AI агента"))
                break
            except FloodWaitError as exc:
                print(f"Telegram просит подождать {exc.seconds} с.", flush=True)
                await asyncio.sleep(exc.seconds + 1)
        lines.append("")
        lines.extend(render_section(wizard, 0))
        if any("⭐" in text for text in wizard.texts):
            lines.extend(["", "Дальше счёт в звёздах. Оплату не подтверждаю."])
            return "\n".join(lines).rstrip() + "\n\n"

        for answer in WIZARD_ANSWERS:
            print(f"Ответ мастеру: {answer}", flush=True)
            sent = await send_when_ready(client, entity, answer)
            replies = await wait_for_replies(client, entity, sent.id)
            lines.append("")
            lines.extend(render_exchange("Мастер", answer, replies))
            if has_star_invoice(replies):
                lines.extend(["", "Дальше счёт в звёздах. Оплату не подтверждаю."])
                break
            if any(is_chat_link(button) for message in replies for button in flat_buttons(message)):
                lines.extend(["", "Дальше привязка чата. Её не подтверждаю."])
                break
            joined = "\n".join((message.text or "") for message in replies).lower()
            if "подключ" in joined and "чат" in joined:
                lines.extend(["", "Дальше привязка чата. Её не подтверждаю."])
                break
            await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
    except Exception as exc:
        lines.extend(["", f"[Ошибка]: {exc}"])

    return "\n".join(lines).rstrip() + "\n\n"


async def analyze_bots() -> None:
    load_dotenv(ROOT / ".env")
    api_id_raw = require_env("API_ID")
    api_hash = require_env("API_HASH")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("API_ID в .env должен быть числом без кавычек.") from exc

    bots = load_bots(ROOT / "bots.txt")
    if not bots:
        raise SystemExit("bots.txt пуст. Добавьте юзернеймы ботов, по одному на строку.")

    results: list[str] = []
    client = TelegramClient(str(ROOT / "session_name"), api_id, api_hash)
    try:
        await ensure_signed_in(client)
        for username in bots:
            print(f"Веду диалог: {username}...", flush=True)
            results.append(await run_dialog(client, username))
            await asyncio.sleep(PAUSE_BETWEEN_ACTIONS_SECONDS)
    finally:
        await client.disconnect()

    output_path = ROOT / "bots_functionality.txt"
    existing = output_path.read_text(encoding="utf-8") if output_path.exists() else ""
    if existing and not existing.endswith("\n"):
        existing += "\n"
    separator = "\n" if existing else ""
    output_path.write_text(existing + separator + "".join(results), encoding="utf-8")
    print(f"Готово. Диалог дописан в {output_path.name}", flush=True)


if __name__ == "__main__":
    asyncio.run(analyze_bots())
