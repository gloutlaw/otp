"""Fill the branch map of @allai_robot with the screens no earlier block covers.

Appends results to bots_functionality.txt under one block title and never
rewrites older blocks. Re-running skips steps whose title is already there.

Usage:
    python collect_branches.py --plan           # print steps, no Telegram
    python collect_branches.py limits           # read limits only, no file writes
    python collect_branches.py [group ...]      # run all or selected groups
"""

import asyncio
import io
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from dotenv import load_dotenv
from telethon import TelegramClient

from collect_bots import (
    ROOT,
    ButtonNote,
    Section,
    attachment_note,
    button_action,
    extract_alert,
    flat_buttons,
    is_stale_click,
    render_exchange,
    render_section,
    require_env,
)
from collect_images import (
    REPLY_WAIT,
    Screen,
    Session,
    StepError,
    find_button,
    norm,
    pause,
    retry_flood,
    screen_text,
    settings_facts,
    signature,
)

BOT = "@allai_robot"
OUTPUT = ROOT / "bots_functionality.txt"
BLOCK_TITLE = "=== ДОБОР ВЕТОК: @allai_robot ==="
QUESTION = "Сколько будет 17 умножить на 3?"
IMAGE_PROBE_TEXT = "проверочный текст, подтверждение не нажимается"

AWAIT = re.compile(
    r"Отправьте (новое )?имя|Опишите (изображение|роль)|Как назовём|шаг \d/\d|отправьте файл|Напишите, что|Введите|Пришлите",
    re.IGNORECASE,
)
TRAP = re.compile(r"Создание AI агента|шаг \d/\d|Отправьте новое имя|Проверка генерации", re.IGNORECASE)
TRANSIENT = re.compile(r"делаем короче|думаю|генерирует|подождите", re.IGNORECASE)
NOISE = re.compile(r"Создано при поддержке @StageControlBot")
BACK = re.compile(r"^(назад|к\s)")
NAV = re.compile(r"^(назад|к\s|главное меню|закрыть)")
LEAVE_ORDER = ("отмена", "назад", "главное меню")
ANNOTATION = re.compile(r"\s*\[[^\]]*\]\s*$")
MARKUP = re.compile(r"[*_`]")
NOT_CLICKABLE = ("link", "webapp", "phone", "geo", "pay")


class Abort(Exception):
    def __init__(self, message: str, lines: list[str] | None = None) -> None:
        super().__init__(message)
        self.lines = lines or []


def title_key(text: str) -> str:
    for line in (text or "").splitlines():
        cleaned = norm(MARKUP.sub("", line)).strip()
        if cleaned:
            return cleaned
    return ""


def label_key(label: str) -> str:
    text = norm(ANNOTATION.sub("", label)).lower()
    head = text.split(":", 1)[0]
    # "Качество: 1K" carries a value that changes; "1:1" is a bare ratio and stays whole.
    if ":" in text and re.search(r"[^\W\d_]", head):
        return head.strip()
    return text


def closed_reason(label: str, host_text: str = "") -> str:
    name = label_key(label)
    if "⭐" in label:
        return "оплата звёздами"
    if name.startswith(("купить", "докупить")) or "пакет" in name:
        return "покупка"
    if "подключить к чату" in name:
        return "привязка к чату"
    if "сделать активным" in name:
        return "активация"
    if "удалить" in name:
        return "удаление"
    if "очистить" in name:
        return "очистка"
    if "изменить имя" in name:
        return "смена имени"
    if name == "изменить":
        return "правка агента, близко к «Изменить имя»: решение за вами"
    if name == "создать ai агента" and "агентов:" in host_text.lower():
        return "новый агент"
    if "подтвердить" in name or name.startswith(("запустить", "сгенерировать")):
        return "запускает генерацию"
    if "использовать" in name:
        return "готовый промт, нажатие может сразу запустить генерацию"
    if re.match(r"^[24]k\b", name):
        return "2K и 4K без купленных генераций"
    return ""


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip())


def parse_known(text: str) -> dict[tuple[str, frozenset[str]], str]:
    known: dict[tuple[str, frozenset[str]], str] = {}
    lines = text.splitlines()
    index = 0
    while index < len(lines):
        if lines[index].strip() != "Текст:":
            index += 1
            continue
        indent = indent_of(lines[index])
        cursor = index + 1
        body: list[str] = []
        while cursor < len(lines) and (not lines[cursor].strip() or indent_of(lines[cursor]) > indent):
            body.append(lines[cursor].strip())
            cursor += 1
        labels: set[str] = set()
        if cursor < len(lines) and lines[cursor].strip() == "Кнопки:" and indent_of(lines[cursor]) == indent:
            cursor += 1
            while cursor < len(lines) and lines[cursor].strip().startswith("- "):
                labels.add(label_key(lines[cursor].strip()[2:]))
                cursor += 1
        title = title_key("\n".join(body))
        if title:
            known.setdefault((title, frozenset(labels)), title)
        index = max(cursor, index + 1)
    return known


class MapFile:
    def __init__(self, path) -> None:
        self.path = path
        self.snapshot = path.read_bytes()

    def text(self) -> str:
        return self.snapshot.decode("utf-8")

    def done_titles(self) -> set[str]:
        text = self.text()
        start = text.rfind(BLOCK_TITLE)
        if start < 0:
            return set()
        special = {"Текст:", "Кнопки:", "Отправлено:", "Ответ:"}
        return {
            line
            for line in text[start:].splitlines()
            if line and not line.startswith((" ", "- ", "=")) and line not in special
        }

    def append(self, lines: list[str]) -> None:
        current = self.path.read_bytes()
        if current != self.snapshot:
            raise SystemExit("Файл карты изменился во время работы. Остановка, чтобы ничего не затереть.")
        text = current.decode("utf-8")
        headers = [line for line in text.splitlines() if line.startswith("=== ")]
        in_block = bool(headers) and headers[-1] == BLOCK_TITLE
        prefix = ""
        if not text.endswith("\n"):
            prefix += "\n"
        if not in_block and not text.endswith("\n\n"):
            prefix += "\n"
        head = "" if in_block else BLOCK_TITLE + "\n\n"
        chunk = prefix + head + "\n".join(lines).rstrip() + "\n\n"
        with self.path.open("ab") as handle:
            handle.write(chunk.encode("utf-8"))
        updated = self.path.read_bytes()
        if updated[: len(current)] != current:
            raise SystemExit("Старое содержимое файла изменилось. Проверьте bots_functionality.txt.")
        self.snapshot = updated
        print(f"Дописано в карту: {lines[0]}", flush=True)


def join_parts(parts: list[list[str]]) -> list[str]:
    lines: list[str] = []
    for part in parts:
        if lines and part:
            lines.append("")
        lines.extend(part)
    return lines


def children(menu) -> list:
    host = menu.text or ""
    seen: set[str] = set()
    found = []
    for button in flat_buttons(menu):
        key = label_key(button.text)
        if key in seen or NAV.match(key) or closed_reason(button.text, host):
            continue
        seen.add(key)
        found.append(button)
    return found


class Walker(Session):
    def __init__(self, client: TelegramClient, entity, known: dict) -> None:
        super().__init__(client, entity)
        self.known = known
        self.titles: dict[tuple[str, ...], str] = {}
        self.text_pending = False
        self.deleted = False
        # Label keys of closed buttons the user explicitly allowed for the current step only.
        self.unlocked: set[str] = set()

    async def fresh_after(self, after_id: int) -> list:
        # The bot pushes this banner on its own timer; counting it as a reply ends the wait before the real edit lands.
        return [m for m in await super().fresh_after(after_id) if not NOISE.search(m.text or "")]

    async def cur_message(self):
        if self.cur is None:
            return None
        return await self.reload(self.cur)

    async def locate(self, label: str, prefix: bool):
        # Only the message we hold is trusted; scanning history could click a stale copy of the button.
        message = await self.cur_message()
        if message is not None and find_button(message, label, prefix):
            return message
        return None

    async def adopt(self, label: str, prefix: bool = False, limit: int = 30) -> None:
        batch = await retry_flood(lambda: self.client.get_messages(self.entity, limit=limit))
        for message in sorted(batch, key=lambda item: item.id, reverse=True):
            if not message.out and find_button(message, label, prefix):
                self.cur = message.id
                return
        raise StepError(f"Не нашёл свежее сообщение с кнопкой: {label}")

    async def settle(self, host_id: int, before: tuple, top: int, alert: str, deadline_s: float = 150, quiet_s: float = 12) -> Screen:
        loop = asyncio.get_running_loop()
        started = last_change = loop.time()
        seen_sig: tuple | None = None
        messages: list = []
        while loop.time() - started < deadline_s:
            await pause()
            fresh = await self.fresh_after(top)
            host = await self.reload(host_id)
            changed = host is not None and signature(host) != before
            current = ([host] if changed else []) + [m for m in fresh if m.id != host_id]
            sig = tuple((m.id, *signature(m)) for m in current)
            if sig != seen_sig:
                seen_sig = sig
                messages = current
                last_change = loop.time()
            transient = any(TRANSIENT.search(m.text or "") for m in messages)
            if messages and not transient and loop.time() - last_change >= quiet_s:
                break
            if not messages and alert and loop.time() - started > 10:
                break
        menu = next((m for m in reversed(messages) if m.buttons), None)
        if menu is not None:
            self.cur = menu.id
        return Screen(messages, alert)

    async def click(self, label: str, prefix: bool = False, wait_s: float = REPLY_WAIT, patient: bool = False) -> Screen:
        for attempt in range(3):
            message = await self.locate(label, prefix)
            if message is None:
                raise StepError(f"Кнопка не найдена на текущем экране: {label}")
            button = find_button(message, label, prefix)
            reason = closed_reason(button.text, message.text or "")
            if reason and label_key(button.text) not in self.unlocked:
                raise StepError(f"Закрытая кнопка не нажималась ({reason}): {button.text}")
            kind, _detail, _key = button_action(button)
            if kind in NOT_CLICKABLE:
                raise StepError(f"Кнопка не обычная ({kind}) и не нажималась: {button.text}")

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
            if patient:
                screen = await self.settle(message.id, before, top, alert)
            else:
                screen = await self.collect_screen(message.id, before, top, alert, wait_s)
            self.deleted = False
            if not screen.messages and not alert:
                self.deleted = await self.reload(message.id) is None
            if screen.messages:
                self.text_pending = bool(AWAIT.search(screen_text(screen)))
            return screen
        raise StepError(f"Кнопка устарела и не нажалась: {label}")

    async def on_screen(self, want: str, need: str, prefix: bool) -> bool:
        message = await self.cur_message()
        return message is not None and title_key(message.text or "") == want and find_button(message, need, prefix) is not None

    async def step_back(self, want: str, need: str, prefix: bool) -> bool:
        message = await self.cur_message()
        if message is None:
            return False
        back = next((b for b in flat_buttons(message) if BACK.match(label_key(b.text))), None)
        if back is None:
            return False
        await self.click(back.text)
        return await self.on_screen(want, need, prefix)

    async def walk(self, parent: tuple[str, ...]) -> None:
        await self.start()
        for label in parent:
            await self.click(label)
        message = await self.cur_message()
        if message is None:
            raise StepError(f"После перехода {' → '.join(parent) or '/start'} нет экрана")
        self.titles[parent] = title_key(message.text or "")

    async def reach(self, parent: tuple[str, ...], need: str, prefix: bool = False) -> None:
        want = self.titles.get(parent)
        if want is not None:
            if await self.on_screen(want, need, prefix):
                return
            if await self.step_back(want, need, prefix):
                return
        for attempt in range(2):
            # A slow edit can leave the old screen for a moment; one fresh walk fixes it.
            try:
                await self.walk(parent)
            except StepError:
                if attempt == 1:
                    raise
                await pause()
                continue
            if await self.has(need, prefix):
                return
            await pause()
        raise StepError(f"На экране {' → '.join(parent) or 'главное меню'} нет кнопки: {need}")

    async def visit(self, parent: tuple[str, ...], label: str, *, prefix: bool = False, wait_s: float = REPLY_WAIT, patient: bool = False) -> Screen:
        await self.reach(parent, label, prefix)
        return await self.click(label, prefix, wait_s, patient)

    async def leave_text_state(self) -> tuple[str, Screen]:
        message = await self.cur_message()
        if message is None:
            raise Abort("Не вижу экран, с которого можно выйти из ожидания текста. Текст дальше не отправляю.")
        buttons = flat_buttons(message)
        host = message.text or ""
        for wanted in LEAVE_ORDER:
            button = next(
                (b for b in buttons if label_key(b.text).startswith(wanted) and not closed_reason(b.text, host)),
                None,
            )
            if button is None:
                continue
            screen = await self.click(button.text)
            if self.text_pending:
                raise Abort("После выхода бот всё ещё ждёт текст. Дальше текст не отправляю.")
            return button.text, screen
        raise Abort("На экране ожидания текста нет кнопки выхода. Дальше текст не отправляю.")

    async def ensure_text_clean(self) -> None:
        if self.text_pending:
            await self.leave_text_state()

    async def ask(self, text: str, *, deadline: float = 90, quiet: float = 10, clean: bool = True) -> list:
        if clean:
            await self.ensure_text_clean()
        sent = await self.send(text)
        final, _seen = await self.watch(sent.id, deadline, 8, quiet)
        menu = next((m for m in reversed(final) if m.buttons), None)
        if menu is not None:
            self.cur = menu.id
        self.text_pending = False
        return final

    def annotation(self, button, host) -> str:
        reason = closed_reason(button.text, host.text or "")
        if reason:
            return f" [закрытая кнопка: {reason}, не нажималась]"
        kind, detail, _key = button_action(button)
        return detail if kind in NOT_CLICKABLE else ""

    def report(self, title: str, screen: Screen, *, full: bool = False):
        texts: list[str] = []
        if screen.alert:
            texts.append(f"[Всплывающий ответ бота]: {screen.alert}")
        fresh_menu = None
        fresh_any = False
        for message in screen.messages:
            text = (message.text or "").strip()
            labels = [button.text for button in flat_buttons(message)]
            key = (title_key(text), frozenset(label_key(label) for label in labels))
            if not full and key[0] and key in self.known:
                texts.append(f"[Исход]: открылся экран «{self.known[key]}», он уже в карте.")
                continue
            fresh_any = True
            if text and text not in texts:
                texts.append(text)
            note = attachment_note(message)
            if note:
                texts.append(note)
            if message.buttons:
                fresh_menu = message
            if key[0]:
                self.known.setdefault(key, key[0])
            if text and not message.buttons and not note:
                # A new text-only screen is treated as "the bot waits for text" until proven otherwise.
                self.text_pending = True
        if not texts:
            texts = [
                "[Исход]: бот удалил сообщение с кнопками, новый экран не открылся."
                if self.deleted
                else "[Исход]: бот сам не открыл экран, сообщение не изменилось."
            ]
        notes = [ButtonNote(b.text, self.annotation(b, fresh_menu)) for b in flat_buttons(fresh_menu)] if fresh_menu else []
        menu = next((m for m in reversed(screen.messages) if m.buttons), None)
        return render_section(Section(title, texts, notes), 0), menu, fresh_any


@dataclass(frozen=True)
class Step:
    group: str
    title: str
    run: Callable[[Walker], Awaitable[list[str]]]


def probe(group, parent, label, *, title=None, prefix=False, full=False, patient=False, wait=REPLY_WAIT) -> Step:
    name = title or " → ".join((*parent, label))

    async def run(walker: Walker) -> list[str]:
        screen = await walker.visit(parent, label, prefix=prefix, wait_s=wait, patient=patient)
        lines, _menu, _new = walker.report(name, screen, full=full)
        return lines

    return Step(group, name, run)


async def explore(walker: Walker, parent, label, title, depth, *, force_children=False) -> list[list[str]]:
    screen = await walker.visit(parent, label)
    lines, menu, is_new = walker.report(title, screen)
    parts = [lines]
    if depth and menu is not None and (is_new or force_children) and not walker.text_pending:
        for button in children(menu):
            parts.extend(await explore(walker, (*parent, label), button.text, f"{title} → {norm(button.text)}", depth - 1))
    return parts


def explore_step(group, parent, label, title, depth, *, force_children=False) -> Step:
    async def run(walker: Walker) -> list[str]:
        return join_parts(await explore(walker, parent, label, title, depth, force_children=force_children))

    return Step(group, title, run)


async def profile_name(walker: Walker) -> str:
    await walker.visit((), "Профиль")
    message = await walker.cur_message()
    match = re.search(r"Имя:\**\s*(\S+)", (message.text or "") if message else "")
    return match.group(1) if match else "?"


async def read_limits(walker: Walker) -> dict[str, str]:
    await walker.visit((), "Лимиты")
    message = await walker.cur_message()
    text = (message.text or "") if message else ""
    personal = re.search(r"Personal AI\**\s*Осталось:\s*(\d+)\s*/\s*(\d+)", text)
    agents = re.search(r"AI Agents\**\s*Осталось:\s*(\d+)\s*/\s*(\d+)", text)
    gens = re.search(r"Баланс:\s*(\d+)", text)
    return {
        "Personal AI": f"{personal.group(1)}/{personal.group(2)}" if personal else "?",
        "AI Agents": f"{agents.group(1)}/{agents.group(2)}" if agents else "?",
        "Генерации": gens.group(1) if gens else "?",
    }


async def question_step(walker: Walker) -> list[str]:
    before = await profile_name(walker)
    final = await walker.ask(QUESTION)
    lines = render_exchange("Вопрос (повтор после сброса мастера агента)", QUESTION, final)
    if TRAP.search("\n".join(m.text or "" for m in final)):
        raise Abort("Бот снова принял вопрос за шаг другого сценария. Дальше текст не отправляю.", lines)
    after = await profile_name(walker)
    print(f"Имя профиля {'не изменилось' if before == after else 'ИЗМЕНИЛОСЬ'}.", flush=True)
    if before != after:
        raise Abort("Имя профиля изменилось после текста. Остановка.", lines)
    return lines


async def shorten_step(walker: Walker) -> list[str]:
    await walker.adopt("Сократить", prefix=True)
    screen = await walker.click("Сократить", prefix=True, patient=True)
    lines, _menu, _new = walker.report("✂️ Сократить (итоговый ответ)", screen, full=True)
    return lines


async def to_confirm(walker: Walker, images: tuple[str, ...]) -> None:
    await walker.visit(images, "Создать изображение")
    prompt = await walker.cur_message()
    if prompt is None or title_key(prompt.text or "") != "Опишите изображение":
        # Sending text now would reach the Personal AI chat and spend a request.
        raise Abort("Бот не показал экран «Опишите изображение». Текст не отправляю.")
    sent = await walker.send(IMAGE_PROBE_TEXT)
    final, _seen = await walker.watch(sent.id, 90, 8, 10)
    menu = next((m for m in reversed(final) if m.buttons), None)
    if menu is None or title_key(menu.text or "") != "Проверка генерации":
        lines = render_exchange("Текст после «Создать изображение» (неожиданный ответ)", IMAGE_PROBE_TEXT, final)
        raise Abort("Бот не показал экран подтверждения. Дальше не иду.", lines)
    walker.cur = menu.id
    walker.text_pending = False


async def collecting(body: Callable[[list[list[str]]], Awaitable[None]]) -> list[str]:
    parts: list[list[str]] = []
    try:
        await body(parts)
    except Abort as stop:
        raise Abort(str(stop), join_parts([*parts, stop.lines])) from stop
    except Exception as exc:
        # Keep what was already collected: the state of the bot after a failure is unknown.
        raise Abort(f"Шаг оборвался: {exc}", join_parts(parts)) from exc
    return join_parts(parts)


async def image_flow(walker: Walker) -> list[str]:
    return await collecting(lambda parts: image_parts(walker, parts))


async def restore_image_defaults(walker: Walker) -> None:
    images = ("Генерации", "Изображения")
    for _ in range(3):
        await walker.reach(images, "Выбрать модель")
        text = (await walker.cur_message()).text or ""
        _facts, ok = settings_facts(text)
        if ok:
            print("Настройки изображений: Fast Image, 1K, 1:1.", flush=True)
            return
        model = re.search(r"Модель:\s*([^\n]+)", text)
        quality = re.search(r"Качество:\s*(\S+)", text)
        ratio = re.search(r"Формат:\s*(\S+)", text)
        if model is None or "Fast Image" not in model.group(1):
            await walker.visit(images, "Выбрать модель")
            await walker.click("Fast Image")
        elif quality and quality.group(1) != "1K":
            await walker.visit(images, "Качество", prefix=True)
            await walker.click("1K")
        elif ratio and ratio.group(1) != "1:1":
            await walker.visit(images, "Формат", prefix=True)
            await walker.click("1:1")
    raise Abort("Не смог вернуть Fast Image, 1K, 1:1. Проверьте настройки изображений вручную.")


async def use_prompt_flow(walker: Walker, category: str, name: str) -> list[str]:
    async def body(parts: list[list[str]]) -> None:
        parent = ("Генерации", "Изображения", "Популярные промты", category, name)
        title = f"{name} → ✨ Использовать промпт"
        # Allowed once by the user: it may spend one free generation.
        walker.unlocked.add("использовать промпт")
        try:
            screen = await walker.visit(parent, "Использовать промпт", patient=True)
        finally:
            walker.unlocked.discard("использовать промпт")
        lines, menu, _new = walker.report(title, screen, full=True)
        parts.append(lines)
        if menu is not None and title_key(menu.text or "") == "Проверка генерации":
            walker.cur = menu.id
            screen = await walker.click("Отмена")
            parts.append(walker.report(f"{title} → ✕ Отмена", screen)[0])
        elif walker.text_pending:
            label, screen = await walker.leave_text_state()
            parts.append(walker.report(f"{title} → {label}", screen)[0])
        await restore_image_defaults(walker)

    return await collecting(body)


DOC_ACTIONS = ("Краткое резюме", "Подробный конспект", "Структура / план")
REFUSAL = re.compile(r"куплен|недостаточно|⭐|Premium", re.IGNORECASE)


def make_doc() -> io.BytesIO:
    data = io.BytesIO("Проба раздела «Документы»: короткий текстовый файл.".encode("utf-8"))
    data.name = "probe.txt"
    return data


async def docs_actions_flow(walker: Walker, labels: tuple[str, ...] = DOC_ACTIONS, suffix: str = "") -> list[str]:
    async def body(parts: list[list[str]]) -> None:
        for label in labels:
            await walker.visit(("Генерации",), "Документы")
            sent = await retry_flood(lambda: walker.client.send_file(walker.entity, make_doc()))
            final, _seen = await walker.watch(sent.id, 60, 8, 12)
            menu = next((m for m in reversed(final) if m.buttons), None)
            if menu is None:
                raise StepError("После файла нет сообщения с кнопками")
            walker.cur = menu.id
            walker.text_pending = False
            screen = await walker.click(label, patient=True)
            parts.append(walker.report(f"Генерации → Документы → файл → {label}{suffix}", screen, full=True)[0])
            if not REFUSAL.search(screen_text(screen) + screen.alert):
                # The action ran instead of refusing: one spent attempt is enough, the rest share the branch.
                break

    return await collecting(body)


async def image_parts(walker: Walker, parts: list[list[str]]) -> None:
    images = ("Генерации", "Изображения")
    await walker.reach(images, "Создать изображение")
    current = await walker.cur_message()
    facts, ok = settings_facts((current.text or "") if current else "")
    if not ok:
        raise StepError(f"Настройки не Fast Image 1K 1:1, поток не запускался. {facts}")

    base = "Генерации → Изображения → ✨ Создать изображение"

    await walker.click("Создать изображение")
    screen = await walker.click("Отмена")
    parts.append(walker.report(f"{base} → ✕ Отмена", screen)[0])

    await to_confirm(walker, images)
    screen = await walker.click("Изменить промпт")
    parts.append(walker.report(f"{base} → текст → Проверка генерации → ✏️ Изменить промпт", screen)[0])
    if walker.text_pending:
        label, screen = await walker.leave_text_state()
        parts.append(walker.report(f"{base} → текст → Проверка генерации → ✏️ Изменить промпт → {label}", screen)[0])

    await to_confirm(walker, images)
    screen = await walker.click("Настройки")
    parts.append(walker.report(f"{base} → текст → Проверка генерации → Настройки", screen)[0])
    if walker.text_pending:
        await walker.leave_text_state()

    await to_confirm(walker, images)
    screen = await walker.click("Отмена")
    parts.append(walker.report(f"{base} → текст → Проверка генерации → ✕ Отмена", screen)[0])

    await walker.adopt("Создать ещё", limit=100)
    screen = await walker.click("Создать ещё", patient=True)
    parts.append(walker.report("Результат картинки → Создать ещё", screen, full=True)[0])
    if walker.text_pending:
        label, screen = await walker.leave_text_state()
        parts.append(walker.report(f"Результат картинки → Создать ещё → {label}", screen)[0])


async def docs_flow(walker: Walker) -> list[str]:
    await walker.visit(("Генерации",), "Документы")
    sent = await retry_flood(lambda: walker.client.send_file(walker.entity, make_doc()))
    final, _seen = await walker.watch(sent.id, 60, 8, 12)
    walker.text_pending = False
    return render_exchange("Генерации → Документы → отправка файла", "файл probe.txt, одна короткая строка", final)


async def support_flow(walker: Walker) -> list[str]:
    await walker.ask("/support")
    screen = await walker.click("AI Чат")
    return walker.report("/support → 🤖 AI Чат", screen)[0]


async def ai_command_flow(walker: Walker) -> list[str]:
    final = await walker.ask("/ai")
    return render_exchange("/ai (повтор, без постороннего сообщения)", "/ai", final)


CENTER = (
    ("Режим AI", "Сбалансированный", ("Быстрый", "Глубокий", "Креативный", "Аналитический"), True),
    ("Стиль AI", "Ассистент", ("Аналитик", "Наставник", "Креативный", "Технический"), False),
    ("Формат ответов", "Средние", ("Короткие", "Подробные"), True),
    ("AI Provider", "Авто", ("OpenAI", "Claude", "Gemini", "DeepSeek"), True),
    ("Auto Routing", "Умный", ("Экономный", "Быстрый", "Premium Auto"), True),
    ("Персонализация", "Выкл", ("Вкл",), True),
)

USE_PROMPT = ("Аватары", "Минималистичный аватар")

PROMPTS = (
    ("Аватары", ("Деловой портрет", "Кинематографичный неон", "Минималистичный аватар")),
    ("Логотипы", ("Минимал-знак", "Эмблема-бейдж", "Иконка приложения")),
    ("Постеры", ("Афиша события", "Типографика-минимал", "Киноплакат")),
    ("Товары / реклама", ("Студийный продукт", "Лайфстайл-сцена", "Рекламный баннер")),
    ("Соцсети", ("Обложка канала", "Фон для сторис", "Квадратный пост")),
    ("Арт / стиль", ("Акварель", "Киберпанк", "Флэт-иллюстрация")),
)

NAV_PROBES = (
    (("Генерации", "Видео"), "Назад"),
    (("Генерации", "Документы"), "Назад"),
    (("Генерации", "Музыка"), "Назад"),
    (("Генерации", "Изображения", "ℹ️ Инструкция"), "Назад к Изображениям"),
    (("Генерации", "Изображения", "Популярные промты", "Аватары"), "К категориям"),
    (("Генерации", "Изображения", "Популярные промты", "Аватары", "Деловой портрет"), "Назад"),
    (("Генерации", "Изображения", "Популярные промты", "Аватары", "Деловой портрет"), "Главное меню"),
    (("Генерации", "Изображения", "Популярные промты"), "Назад"),
    (("Генерации", "Изображения", "Выбрать модель"), "Назад"),
    (("Рефералы", "Статистика"), "Назад"),
    (("Рефералы",), "Назад"),
    (("История",), "Назад"),
    (("Лимиты",), "Назад"),
    (("Premium", "Premium"), "Назад"),
    (("Premium",), "Главное меню"),
    (("Рефералы",), "Главное меню"),
    (("Профиль",), "Главное меню"),
    (("AI-центр",), "Главное меню"),
    (("AI-центр", "Режим AI"), "Назад к настройкам"),
    (("Создать AI агента", "Мои агенты"), "AI агенты"),
)


def build_plan() -> list[Step]:
    steps: list[Step] = [
        probe(
            "chat",
            ("История", "Повторить"),
            "1. Привет",
            prefix=True,
            title="История → 🔁 Повторить → 1. Привет как дела ?",
            full=True,
            patient=True,
        ),
        Step("chat", "Вопрос (повтор после сброса мастера агента)", question_step),
        Step("chat", "✂️ Сократить (итоговый ответ)", shorten_step),
        Step("image", "Генерации → Изображения → ✨ Создать изображение → ✕ Отмена", image_flow),
        probe("models", ("Модели",), "GPT-5.5"),
        probe("models", ("Модели",), "Закрыть"),
        probe("models", ("Модели",), "Premium"),
        probe("models", ("Модели",), "Главное меню"),
        probe("models", ("Модели", "GPT-5.4"), "Назад к моделям"),
        probe("models", ("Модели", "GPT-5.4"), "Premium"),
        probe("models", ("Модели", "Gemini 3.1 Pro · скоро"), "Назад к моделям"),
        probe("profile", ("Профиль",), "Рефералы"),
        probe("profile", ("Профиль",), "Premium"),
        probe(
            "misc",
            ("Генерации", "Изображения", "Выбрать модель"),
            "Fast Image",
            title="Генерации → Изображения → 🤖 Выбрать модель → ✓ Fast Image (текущая)",
        ),
    ]
    for sub, current, others, reverts in CENTER:
        parent = ("AI-центр", sub)
        steps.append(probe("center", parent, current, title=f"AI-центр → {sub} → {current} (текущий)", wait=12))
        steps.extend(probe("center", parent, option, wait=12) for option in others)
        if reverts:
            steps.append(probe("center", parent, current, title=f"AI-центр → {sub} → {current} (возврат)", wait=12))
    steps.append(
        explore_step(
            "agents",
            ("Создать AI агента",),
            "Мои агенты",
            "Создать AI агента → Мои агенты (после сохранения агента)",
            2,
        )
    )
    prompts_root = ("Генерации", "Изображения", "Популярные промты")
    for category, names in PROMPTS:
        steps.extend(
            probe("prompts", (*prompts_root, category), name, full=True)
            for name in names
            if (category, name) != ("Аватары", "Деловой портрет")
        )
    steps.append(
        probe(
            "fix",
            ("AI-центр", "AI Provider"),
            "Gemini",
            title="AI-центр → AI Provider → Gemini (повтор, без постороннего сообщения)",
            wait=12,
        )
    )
    steps.append(
        probe(
            "fix",
            ("AI-центр", "AI Provider"),
            "Авто",
            title="AI-центр → AI Provider → Авто (возврат после повтора)",
            wait=12,
        )
    )
    steps.append(
        probe(
            "fix",
            ("Создать AI агента", "Мои агенты", "🤖 Тестовый помощник"),
            "Мои агенты",
            title="Карточка агента → Мои агенты (повтор, без постороннего сообщения)",
        )
    )
    steps.append(Step("fix", "/ai (повтор, без постороннего сообщения)", ai_command_flow))
    steps.append(
        Step("spend", f"{USE_PROMPT[1]} → ✨ Использовать промпт", lambda walker: use_prompt_flow(walker, *USE_PROMPT))
    )
    steps.append(Step("spend", "Генерации → Документы → файл → Краткое резюме", docs_actions_flow))
    steps.append(
        Step(
            "spend",
            "Генерации → Документы → файл → Структура / план (повтор, экран не успел обновиться)",
            lambda walker: docs_actions_flow(walker, ("Структура / план",), " (повтор, экран не успел обновиться)"),
        )
    )
    steps.append(Step("support", "/support → 🤖 AI Чат", support_flow))
    steps.append(Step("support", "/ai", ai_command_flow))
    steps.append(Step("docs", "Генерации → Документы → отправка файла", docs_flow))
    steps.extend(probe("nav", parent, label) for parent, label in NAV_PROBES)
    return steps


def select_steps(groups: list[str]) -> list[Step]:
    plan = build_plan()
    if not groups:
        return plan
    known_groups = {step.group for step in plan}
    unknown = [name for name in groups if name not in known_groups]
    if unknown:
        raise SystemExit(f"Нет таких групп: {', '.join(unknown)}. Есть: {', '.join(sorted(known_groups))}")
    return [step for step in plan if step.group in groups]


async def open_client() -> TelegramClient:
    load_dotenv(ROOT / ".env")
    api_id_raw = require_env("API_ID")
    api_hash = require_env("API_HASH")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise SystemExit("API_ID в .env должен быть числом без кавычек.") from exc
    # Threshold 0 makes Telethon raise every FloodWait, so retry_flood waits the exact time itself.
    client = TelegramClient(str(ROOT / "session_name"), api_id, api_hash, flood_sleep_threshold=0)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit("Сессия не авторизована. Вход не выполняется: код и пароль не запрашиваются.")
    return client


async def run_steps(steps: list[Step]) -> None:
    map_file = MapFile(OUTPUT)
    known = parse_known(map_file.text())
    done = map_file.done_titles()
    client = await open_client()
    failures: list[str] = []
    try:
        entity = await retry_flood(lambda: client.get_entity(BOT))
        walker = Walker(client, entity, known)
        before = await read_limits(walker)
        print(f"Лимиты до: {before}", flush=True)
        try:
            for step in steps:
                if step.title in done:
                    print(f"Пропуск, уже в карте: {step.title}", flush=True)
                    continue
                print(f"Шаг: {step.title}", flush=True)
                try:
                    lines = await step.run(walker)
                except Abort as stop:
                    if stop.lines:
                        map_file.append(stop.lines)
                    print(f"Остановка: {stop}", flush=True)
                    break
                except Exception as exc:
                    print(f"Ошибка шага «{step.title}»: {exc}", flush=True)
                    failures.append(step.title)
                    continue
                map_file.append(lines)
            await walker.ensure_text_clean()
        except Abort as stop:
            print(f"Остановка: {stop}", flush=True)
        after = await read_limits(walker)
        print(f"Лимиты после: {after}", flush=True)
    finally:
        await client.disconnect()
    if failures:
        print("Шаги с ошибкой, в карту не записаны:", flush=True)
        for title in failures:
            print(f"- {title}", flush=True)


async def show_limits() -> None:
    map_file = MapFile(OUTPUT)
    client = await open_client()
    try:
        entity = await retry_flood(lambda: client.get_entity(BOT))
        walker = Walker(client, entity, parse_known(map_file.text()))
        print(f"Лимиты: {await read_limits(walker)}", flush=True)
    finally:
        await client.disconnect()


def print_plan() -> None:
    done = MapFile(OUTPUT).done_titles()
    for step in build_plan():
        mark = "готово" if step.title in done else "ждёт"
        print(f"[{mark}] {step.group}: {step.title}")


def main() -> None:
    args = sys.argv[1:]
    if args == ["--plan"]:
        print_plan()
    elif args == ["limits"]:
        asyncio.run(show_limits())
    else:
        asyncio.run(run_steps(select_steps(args)))


if __name__ == "__main__":
    main()
