import asyncio
import hashlib
import random
import re
from dataclasses import dataclass

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError

from collect_bots import (
    ROOT,
    ButtonNote,
    Section,
    attachment_note,
    extract_alert,
    flat_buttons,
    is_stale_click,
    render_exchange,
    render_section,
    require_env,
)

BOT = "@allai_robot"
OUTPUT = ROOT / "bots_functionality.txt"
BLOCK_TITLE = "=== ДОБОР КАРТИНОК ==="
PROMPT = "рыжий кот сидит на подоконнике, мягкий дневной свет"
CATEGORIES = ("Аватары", "Логотипы", "Постеры", "Товары / реклама", "Соцсети", "Арт / стиль")

FORBIDDEN = (
    "⭐",
    "купить",
    "пакет",
    "подключить",
    "сделать активным",
    "удалить",
    "очистить",
    "изменить",
    "оплат",
)
LAUNCH_WORDS = ("запустить", "подтвердить", "начать", "сгенерировать", "генерировать", "продолжить")
REQUEST_PATTERN = re.compile(r"опиш|описани|промпт|prompt|отправьте|пришлите|напишите", re.IGNORECASE)

RECENT = 15
REPLY_WAIT = 30
CREATE_WAIT = 45
RESULT_DEADLINE = 240
MEDIA_QUIET = 8
CONFIRM_QUIET = 10
TEXT_QUIET = 90


class StepError(Exception):
    pass


@dataclass
class Screen:
    messages: list
    alert: str = ""

    @property
    def menu(self):
        return next((message for message in reversed(self.messages) if message.buttons), None)


async def pause() -> None:
    await asyncio.sleep(random.uniform(4, 5))


async def retry_flood(factory):
    # Wait the exact time Telegram asks for and retry only the current step.
    while True:
        try:
            return await factory()
        except FloodWaitError as exc:
            print(f"Telegram просит подождать {exc.seconds} с. Жду и продолжаю.", flush=True)
            await asyncio.sleep(exc.seconds + 1)


def norm(text: str) -> str:
    return re.sub(r"^[^\w]+", "", text).strip()


def is_forbidden(label: str) -> bool:
    lowered = label.lower()
    return any(word in lowered for word in FORBIDDEN)


def find_button(message, label: str, prefix: bool = False):
    wanted = norm(label)
    buttons = flat_buttons(message)
    for button in buttons:
        if norm(button.text) == wanted:
            return button
    if prefix:
        for button in buttons:
            if norm(button.text).startswith(wanted):
                return button
    return None


def signature(message) -> tuple:
    labels = tuple(button.text for button in flat_buttons(message))
    return ((message.text or "").strip(), labels, bool(message.photo), bool(message.document))


def add_block(report: list[str], lines: list[str]) -> None:
    if report:
        report.append("")
    report.extend(lines)


def section_from(title: str, screen: Screen, extra: tuple[str, ...] = ()) -> Section:
    texts: list[str] = []
    if screen.alert:
        texts.append(f"[Всплывающий ответ бота]: {screen.alert}")
    for message in screen.messages:
        text = (message.text or "").strip()
        if text and text not in texts:
            texts.append(text)
        note = attachment_note(message)
        if note:
            texts.append(note)
    texts.extend(extra)
    if not texts:
        texts = ["[Экран не изменился, бот не прислал текст]"]
    menu = screen.menu
    notes = []
    if menu is not None:
        notes = [
            ButtonNote(button.text, " [не нажималась]" if is_forbidden(button.text) else "")
            for button in flat_buttons(menu)
        ]
    return Section(title, texts, notes)


def screen_text(screen: Screen) -> str:
    return "\n".join((message.text or "") for message in screen.messages)


def settings_facts(text: str) -> tuple[str, bool]:
    model = re.search(r"Модель:\s*([^\n]+)", text)
    quality = re.search(r"Качество:\s*(\S+)", text)
    ratio = re.search(r"Формат:\s*(\S+)", text)
    free = re.search(r"Бесплатные:\s*(\d+)\s*из\s*(\d+)", text)
    current = re.search(r"Для текущей модели:\s*(\d+)", text)
    parts = [
        f"модель — {model.group(1).strip() if model else '?'}",
        f"качество — {quality.group(1) if quality else '?'}",
        f"формат — {ratio.group(1) if ratio else '?'}",
        f"бесплатные — {free.group(1)} из {free.group(2)}" if free else "бесплатные — ?",
        f"для текущей модели — {current.group(1) if current else '?'}",
    ]
    ok = bool(
        model and "Fast Image" in model.group(1)
        and quality and quality.group(1) == "1K"
        and ratio and ratio.group(1) == "1:1"
    )
    return "Проверка настроек: " + "; ".join(parts) + ("" if ok else " [расхождение с Fast Image 1K 1:1]"), ok


def launch_button(messages: list):
    for message in messages:
        for button in flat_buttons(message):
            if is_forbidden(button.text):
                continue
            label = norm(button.text).lower()
            if any(label.startswith(word) for word in LAUNCH_WORDS):
                return message, button
    return None, None


class Session:
    def __init__(self, client: TelegramClient, entity) -> None:
        self.client = client
        self.entity = entity
        self.cur: int | None = None

    async def recent(self) -> list:
        return await retry_flood(lambda: self.client.get_messages(self.entity, limit=RECENT))

    async def reload(self, message_id: int):
        return await retry_flood(lambda: self.client.get_messages(self.entity, ids=message_id))

    async def top_id(self) -> int:
        return max((message.id for message in await self.recent()), default=0)

    async def send(self, text: str):
        print("Отправляю сообщение боту.", flush=True)
        return await retry_flood(lambda: self.client.send_message(self.entity, text))

    async def fresh_after(self, after_id: int) -> list:
        batch = await self.recent()
        return sorted((m for m in batch if not m.out and m.id > after_id), key=lambda m: m.id)

    async def watch(self, after_id: int, deadline_s: float, media_quiet: float, text_quiet: float):
        loop = asyncio.get_running_loop()
        started = loop.time()
        last_change = started
        current_sig: tuple = ()
        final: list = []
        seen: list[str] = []
        while loop.time() - started < deadline_s:
            await pause()
            fresh = await self.fresh_after(after_id)
            snapshot = tuple((message.id, *signature(message)) for message in fresh)
            if snapshot != current_sig:
                current_sig = snapshot
                final = fresh
                last_change = loop.time()
                for message in fresh:
                    text = (message.text or "").strip()
                    if text and text not in seen:
                        seen.append(text)
            if final:
                has_media = any(m.photo or m.document for m in final)
                if has_media:
                    quiet = media_quiet
                elif launch_button(final)[1] is not None:
                    quiet = CONFIRM_QUIET
                else:
                    quiet = text_quiet
                if loop.time() - last_change >= quiet:
                    break
        return final, seen

    async def start(self) -> None:
        print("Шаг: /start", flush=True)
        sent = await self.send("/start")
        replies, _seen = await self.watch(sent.id, REPLY_WAIT, 5, 5)
        menu = next((message for message in reversed(replies) if message.buttons), None)
        if menu is None:
            raise StepError("После /start нет сообщения с кнопками")
        self.cur = menu.id

    async def has(self, label: str, prefix: bool = False) -> bool:
        if self.cur is None:
            return False
        message = await self.reload(self.cur)
        return message is not None and find_button(message, label, prefix) is not None

    async def locate(self, label: str, prefix: bool):
        if self.cur is not None:
            message = await self.reload(self.cur)
            if message is not None and find_button(message, label, prefix):
                return message
        batch = await self.recent()
        for message in sorted(batch, key=lambda m: m.id, reverse=True):
            if not message.out and find_button(message, label, prefix):
                return message
        return None

    async def current_screen(self, alert: str = "") -> Screen:
        message = await self.reload(self.cur) if self.cur is not None else None
        return Screen([message] if message is not None else [], alert)

    async def collect_screen(self, host_id: int, before: tuple, top: int, alert: str, wait_s: float) -> Screen:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + wait_s

        async def snapshot() -> list:
            fresh = await self.fresh_after(top)
            host = await self.reload(host_id)
            changed = host is not None and signature(host) != before
            return ([host] if changed else []) + [m for m in fresh if m.id != host_id]

        messages = await snapshot()
        while not messages and not alert and loop.time() < deadline:
            await pause()
            messages = await snapshot()
        if messages:
            # One more read to catch the final edit of a message that changes in steps.
            await pause()
            messages = await snapshot() or messages

        menu = next((m for m in reversed(messages) if m.buttons), None)
        if menu is not None:
            self.cur = menu.id
        return Screen(messages, alert)

    async def click(self, label: str, prefix: bool = False, wait_s: float = REPLY_WAIT) -> Screen:
        for attempt in range(3):
            message = await self.locate(label, prefix)
            if message is None:
                raise StepError(f"Кнопка не найдена: {label}")
            button = find_button(message, label, prefix)
            if is_forbidden(button.text):
                raise StepError(f"Кнопка запрещена правилами и не нажималась: {button.text}")

            top = await self.top_id()
            before = signature(message)
            print(f"Нажимаю: {button.text}", flush=True)
            try:
                result = await retry_flood(lambda: button.click())
            except Exception as exc:
                if is_stale_click(exc) and attempt < 2:
                    await pause()
                    continue
                raise
            alert = extract_alert(result)
            await pause()
            return await self.collect_screen(message.id, before, top, alert, wait_s)
        raise StepError(f"Кнопка устарела и не нажалась: {label}")

    async def to_images(self) -> None:
        if await self.has("Создать изображение"):
            return
        if await self.has("Назад", prefix=True):
            await self.click("Назад", prefix=True)
            if await self.has("Создать изображение"):
                return
        await self.start()
        await self.click("Генерации")
        await self.click("Изображения")

    async def ensure(self, marker: str, opener) -> None:
        if await self.has(marker):
            return
        if not await self.has("Создать изображение") and await self.has("Назад", prefix=True):
            await self.click("Назад", prefix=True)
            if await self.has(marker):
                return
        await opener()

    async def generate(self, prompt: str):
        sent = await self.send(prompt)
        confirmed = False
        loop = asyncio.get_running_loop()
        started = loop.time()
        notes: list[str] = []
        while True:
            remaining = max(RESULT_DEADLINE - (loop.time() - started), 10)
            final, seen = await self.watch(sent.id, remaining, MEDIA_QUIET, TEXT_QUIET)
            has_media = any(m.photo or m.document for m in final)
            host, button = launch_button(final)
            if has_media or confirmed or button is None:
                return final, seen, notes
            text = "\n".join((m.text or "") for m in final)
            if "⭐" in text or not re.search(r"\b1\s+генерац", text):
                notes.append("Бот просит подтверждение, но стоимость не равна 1 бесплатной генерации. Не нажималась.")
                return final, seen, notes
            notes.append(f"Бот просил подтверждение. Нажата одна кнопка: {button.text}")
            self.cur = host.id
            await self.click(button.text)
            confirmed = True


async def choose_fast_image(session: Session, report: list[str]) -> None:
    title = "Генерации → Изображения → 🤖 Выбрать модель → Fast Image"
    await session.start()
    await session.click("Генерации")
    await session.click("Изображения")
    await session.click("Выбрать модель")
    screen = await session.click("Fast Image")
    if not await session.has("Создать изображение"):
        alert = screen.alert
        await session.to_images()
        screen = await session.current_screen(alert)
    facts, _ok = settings_facts(screen_text(screen))
    add_block(report, render_section(section_from(title, screen, (facts,)), 0))


async def read_categories(session: Session, report: list[str]) -> None:
    async def open_prompts() -> None:
        await session.to_images()
        await session.click("Популярные промты")

    await open_prompts()
    for category in CATEGORIES:
        title = f"Генерации → Изображения → 💡 Популярные промты → {category}"
        try:
            await session.ensure(category, open_prompts)
            screen = await session.click(category)
            add_block(report, render_section(section_from(title, screen), 0))
        except Exception as exc:
            add_block(report, render_section(Section(title, [f"[Ошибка]: {exc}"]), 0))


async def probe_quality_and_format(session: Session, report: list[str]) -> None:
    async def open_quality() -> None:
        await session.to_images()
        await session.click("Качество", prefix=True)

    async def open_format() -> None:
        await session.to_images()
        await session.click("Формат", prefix=True)

    await open_quality()
    for label in ("2K", "4K"):
        title = f"Генерации → Изображения → 🖼 Качество → {label}"
        try:
            await session.ensure("1K", open_quality)
            screen = await session.click(label, prefix=True)
            add_block(report, render_section(section_from(title, screen), 0))
        except Exception as exc:
            add_block(report, render_section(Section(title, [f"[Ошибка]: {exc}"]), 0))

    title = "Генерации → Изображения → 🖼 Качество → 1K (возврат)"
    await session.ensure("1K", open_quality)
    screen = await session.click("1K")
    add_block(report, render_section(section_from(title, screen), 0))

    await open_format()
    for label in ("3:4", "4:3"):
        title = f"Генерации → Изображения → 📐 Формат → {label}"
        try:
            await session.ensure("1:1", open_format)
            screen = await session.click(label)
            add_block(report, render_section(section_from(title, screen), 0))
        except Exception as exc:
            add_block(report, render_section(Section(title, [f"[Ошибка]: {exc}"]), 0))

    title = "Генерации → Изображения → 📐 Формат → 1:1 (возврат)"
    await session.ensure("1:1", open_format)
    screen = await session.click("1:1")
    add_block(report, render_section(section_from(title, screen), 0))

    await session.to_images()
    screen = await session.current_screen()
    facts, _ok = settings_facts(screen_text(screen))
    add_block(report, render_section(section_from("Экран «Изображения» после возврата 1K и 1:1", screen, (facts,)), 0))


async def create_image(session: Session, report: list[str]) -> None:
    title = "Генерации → Изображения → ✨ Создать изображение"
    await session.to_images()
    current = await session.current_screen()
    facts, ok = settings_facts(screen_text(current))
    if not ok:
        add_block(report, render_section(Section(title, [
            "Настройки не совпали с Fast Image 1K 1:1, запрос на картинку не отправлялся.",
            facts,
        ]), 0))
        return

    screen = await session.click("Создать изображение", wait_s=CREATE_WAIT)
    add_block(report, render_section(section_from(title, screen), 0))

    joined = screen_text(screen) + "\n" + screen.alert
    if "⭐" in joined or "недостаточно" in joined.lower():
        add_block(report, ["Бот показал оплату или нехватку генераций. Описание не отправлялось."])
        return
    if not REQUEST_PATTERN.search(joined):
        add_block(report, ["Бот не попросил описание. Промпт не отправлялся."])
        return

    final, seen, notes = await session.generate(PROMPT)
    lines = render_exchange("Описание картинки", PROMPT, final)
    final_texts = {(m.text or "").strip() for m in final}
    transient = [text for text in seen if text not in final_texts]
    if transient:
        lines.append("Промежуточные ответы (потом заменены или удалены):")
        lines.extend(f"  {line}" for text in transient for line in text.splitlines())
    lines.extend(notes)
    if any(m.photo for m in final):
        lines.append("Итог: пришло фото.")
    elif any(m.document for m in final):
        lines.append("Итог: пришёл файл.")
    else:
        lines.append("Итог: картинки нет, пришёл только текст.")
    add_block(report, lines)

    await session.start()
    screen = await session.click("Генерации")
    add_block(report, render_section(section_from("Генерации (экран после запроса на картинку)", screen), 0))


async def guard(report: list[str], title: str, coro) -> None:
    try:
        await coro
    except Exception as exc:
        print(f"Ошибка этапа: {exc}", flush=True)
        add_block(report, render_section(Section(title, [f"[Ошибка]: {exc}"]), 0))


def append_block(report: list[str]) -> None:
    existing = OUTPUT.read_bytes()
    digest = hashlib.sha256(existing).hexdigest()
    prefix = b""
    if not existing.endswith(b"\n"):
        prefix += b"\n"
    if not existing.endswith(b"\n\n"):
        prefix += b"\n"
    block = "\n".join([BLOCK_TITLE, ""] + report).rstrip() + "\n\n"
    with OUTPUT.open("ab") as handle:
        handle.write(prefix + block.encode("utf-8"))
    updated = OUTPUT.read_bytes()
    if hashlib.sha256(updated[: len(existing)]).hexdigest() != digest:
        raise SystemExit("Старое содержимое файла изменилось. Проверьте bots_functionality.txt.")
    print(f"Блок {BLOCK_TITLE} дописан в {OUTPUT.name}", flush=True)


async def main() -> None:
    load_dotenv(ROOT / ".env")
    api_id_raw = require_env("API_ID")
    api_hash = require_env("API_HASH")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("API_ID в .env должен быть числом без кавычек.") from exc

    report: list[str] = []
    client = TelegramClient(str(ROOT / "session_name"), api_id, api_hash, flood_sleep_threshold=0)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise SystemExit("Сессия не авторизована. Вход не выполняется: код и пароль не запрашиваются.")
        entity = await retry_flood(lambda: client.get_entity(BOT))
        session = Session(client, entity)

        phases = (
            ("Fast Image", choose_fast_image),
            ("Популярные промты", read_categories),
            ("Качество и формат", probe_quality_and_format),
            ("Создать изображение", create_image),
        )
        for title, phase in phases:
            await guard(report, title, phase(session, report))
    finally:
        await client.disconnect()
        if report:
            append_block(report)


if __name__ == "__main__":
    asyncio.run(main())
