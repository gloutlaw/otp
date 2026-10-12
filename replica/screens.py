import re
from dataclasses import dataclass

from replica.catalog import CHAT_MODELS, DEFAULT_CHAT_MODEL, PRODUCTS, TERMS_VERSION

PURCHASE_START_LABEL = f"Старт — {PRODUCTS['start'].stars} ⭐"
PURCHASE_PRO_LABEL = f"Pro — {PRODUCTS['pro'].stars} ⭐"
PURCHASE_PACK_LABEL = f"Купить +100 умных — {PRODUCTS['pack_smart_100'].stars} ⭐"
RENEW_OFF_LABEL = "Отменить продление"
RENEW_ON_LABEL = "Включить продление"


@dataclass(frozen=True)
class Button:
    label: str
    target: str | None = None
    closed: bool = False
    style: str | None = "primary"


@dataclass(frozen=True)
class Screen:
    text: str
    buttons: tuple[Button, ...]


DEFAULT_STATUS = (
    "💎 **Ваш тариф: Бесплатный**\n"
    "\n"
    "📊 **Обычные ответы**\n"
    "├ Осталось: **10 из 10**\n"
    "└ 🔄 Обновление через 24 ч. (UTC+3)\n"
    "\n"
    "🧠 **Умные ответы**\n"
    "├ Осталось: **3 из 3**\n"
    "└ пробные"
)
DEFAULT_HISTORY = "Запросов пока нет."
DEFAULT_MODEL = DEFAULT_CHAT_MODEL.title
HISTORY_PLACEHOLDER = "{history}"
BACK = "__back__"
NO_NAV_SCREENS = frozenset({"start", "ai"})

ACTION_CALLBACKS = {
    PURCHASE_START_LABEL: "p:buy:start",
    PURCHASE_PRO_LABEL: "p:buy:pro",
    PURCHASE_PACK_LABEL: "p:buy:pack_smart_100",
    RENEW_OFF_LABEL: "p:renew:off",
    RENEW_ON_LABEL: "p:renew:on",
}


def _home(label: str = "Главное меню") -> Button:
    return Button(label, target="start", style=None)


def _back(label: str = "Назад") -> Button:
    return Button(label, target=BACK, style=None)


def _deeper(label: str) -> Button:
    return Button(label)


def _closed(label: str) -> Button:
    return Button(label, closed=True)


def _action(label: str) -> Button:
    return Button(label)


SCREENS: dict[str, Screen] = {
    "start": Screen(
        text=(
            "🤖 **{bot_name}**\n"
            "__Ваш личный AI-ассистент__\n"
            "\n"
            "{status}\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "✨ Выберите нужный раздел в меню ниже или просто отправьте свою задачу сообщением — {bot_name} поможет!"
        ),
        buttons=(
            Button("🧠 Модели", target="models"),
            Button("👤 Профиль", target="profile"),
            Button("💎 Тарифы", target="tariffs", style="success"),
            Button("🕘 История", target="history"),
            Button("📊 Лимиты", target="limits"),
        ),
    ),
    "generations": Screen(
        text=(
            "**Генерации**\n"
            "\n"
            "Картинки, видео и документы пока не подключены.\n"
            "Текстовые запросы считаются в разделе «Лимиты»."
        ),
        buttons=(
            _closed("Купить генерации"),
        ),
    ),
    "models": Screen(
        text=(
            "🧠 **Модели**\n"
            "\n"
            "Текущая модель: **{model}**\n"
            "Обычные ответы тратят обычный запрос. Дорогие модели тратят умный запрос и доступны в Pro.\n"
            "\n"
            "Выберите модель."
        ),
        buttons=tuple(_deeper(model.title) for model in CHAT_MODELS),
    ),
    "profile": Screen(
        text=(
            "👤 **Мой тариф**\n"
            "\n"
            "{status}"
        ),
        buttons=(
            _closed("Изменить имя"),
            Button("Тарифы", target="tariffs"),
            _action(RENEW_OFF_LABEL),
            _action(RENEW_ON_LABEL),
        ),
    ),
    "tariffs": Screen(
        text=(
            "**💎 Тарифы**\n"
            "\n"
            "{status}\n"
            "\n"
            "**Бесплатный** — 0 ⭐\n"
            "• 10 обычных ответов в день\n"
            "• 3 пробных умных ответа\n"
            "\n"
            "**Старт** — 300 ⭐ / 30 дней\n"
            "• 400 обычных ответов в неделю\n"
            "• 20 умных ответов за период\n"
            "\n"
            "**Pro** — 600 ⭐ / 30 дней\n"
            "• 500 обычных ответов в неделю\n"
            "• 150 умных ответов за период\n"
            "\n"
            "Подписка продлевается автоматически каждые 30 дней, отменить можно в любой момент — доступ сохранится до конца оплаченного периода."
        ),
        buttons=(
            _action(PURCHASE_START_LABEL),
            _action(PURCHASE_PRO_LABEL),
            Button("📦 Пакеты", target="packs"),
            Button("Условия", target="terms"),
        ),
    ),
    "packs": Screen(
        text=(
            "**📦 Пакеты**\n"
            "\n"
            "Разовая покупка, без подписки.\n"
            "\n"
            "**+100 умных ответов** — 219 ⭐\n"
            "Действуют 90 дней с момента покупки. Расходуются после лимитов тарифа."
        ),
        buttons=(
            _action(PURCHASE_PACK_LABEL),
        ),
    ),
    "referrals": Screen(
        text=(
            "👥 **Рефералы**\n"
            "\n"
            "Рефералы пока не подключены."
        ),
        buttons=(),
    ),
    "history": Screen(
        text=(
            "🕘 **История запросов**\n"
            "\n"
            "{history}\n"
            "\n"
            "Последние вопросы, новые сверху."
        ),
        buttons=(
            _deeper("🔁 Повторить"),
            Button("🗑 Очистить", closed=True, style="danger"),
        ),
    ),
    "limits": Screen(
        text=(
            "{status}\n"
            "\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "\n"
            "📌 **Как работают лимиты?**\n"
            "\n"
            "• Лимиты подписки рассчитываются в рамках 30-дневного периода и недель внутри него.\n"
            "• Бесплатные сутки рассчитываются по времени UTC+3.\n"
            "• Каждый умный ответ расходует один умный запрос.\n"
            "• Сначала используются запросы, включённые в тариф, затем — запросы из дополнительных пакетов."
        ),
        buttons=(
            Button("Открыть тарифы", target="tariffs"),
            Button("📦 Пакеты", target="packs"),
        ),
    ),
    "ai_center": Screen(
        text=(
            "⚙️ **Центр управления AI**\n"
            "\n"
            "Модель выбирается в разделе «Модели».\n"
            "Настройки стиля и маршрутизации пока не подключены."
        ),
        buttons=(),
    ),
    "agents": Screen(
        text=(
            "🤖 **AI агенты**\n"
            "\n"
            "Агенты пока не подключены."
        ),
        buttons=(),
    ),
    "help": Screen(
        text=(
            "📚 **FAQ**\n"
            "\n"
            "**Как пользоваться?**\n"
            "Отправьте сообщение в чат. Ответ можно улучшить или сократить: нажмите «Инструменты» под ним.\n"
            "\n"
            "**Что значит «Ответить умнее»?**\n"
            "Кнопка в «Инструментах» пересчитывает ответ сильной моделью и тратит умный запрос.\n"
            "\n"
            "**Как работают лимиты?**\n"
            "Подписка даёт лимиты на 30 дней, внутри них — недельные окна. Бесплатные ответы обновляются каждые сутки по UTC+3. "
            "Подробности в разделе «Лимиты».\n"
            "\n"
            "**Как получить больше?**\n"
            "Тарифы — /premium, пакеты — в разделе «Пакеты». Условия — /terms, вопросы по оплате — /paysupport."
        ),
        buttons=(),
    ),
    "terms": Screen(
        text=(
            "**📜 Условия**\n"
            "\n"
            "**Что продаётся.** Доступ к ответам AI в пределах лимитов тарифа или пакета.\n"
            "\n"
            "**Цены и периоды.** Старт — 300 ⭐, Pro — 600 ⭐, период 30 дней. Пакет «+100 умных» — 219 ⭐, действует 90 дней. Цены в Telegram Stars.\n"
            "\n"
            "**Автопродление.** Подписка продлевается каждые 30 дней. Продление можно выключить в разделе «Профиль»; доступ сохраняется до конца оплаченного периода.\n"
            "\n"
            "**Возвраты.** Возврат оформляется через /paysupport. Возврат отзывает доступ, полученный этим платежом. Уже использованные ответы не возвращаются.\n"
            "\n"
            "**Ограничения.** Ответы AI могут быть неточными. Проверяйте важные решения самостоятельно.\n"
            "\n"
            "**Данные.** Сообщения передаются сторонним AI-провайдерам для генерации ответа.\n"
            "\n"
            "**Поддержка.** {support_contact}\n"
            "\n"
            "Версия условий: " + TERMS_VERSION
        ),
        buttons=(),
    ),
    "paysupport": Screen(
        text=(
            "**💳 Поддержка по оплате**\n"
            "\n"
            "Опишите проблему одним сообщением или напишите {support_contact}. Укажите дату и сумму платежа. Отвечаем в течение 24 часов.\n"
            "\n"
            "Поддержка Telegram и @BotSupport по покупкам в этом боте помочь не могут."
        ),
        buttons=(),
    ),
    "support": Screen(
        text=(
            "**🛟 Поддержка**\n"
            "\n"
            "Если у вас возникли вопросы, напишите:\n"
            "\n"
            "👉 **{support_contact}**"
        ),
        buttons=(
            _deeper("🤖 AI Чат"),
        ),
    ),
    "ai": Screen(
        text=("Добро пожаловать!\n\nСоздано при поддержке {support_contact}"),
        buttons=(),
    ),
}

COMMANDS = {
    "/start": "start",
    "/help": "help",
    "/models": "models",
    "/premium": "tariffs",
    "/profile": "profile",
    "/support": "support",
    "/limits": "limits",
    "/terms": "terms",
    "/paysupport": "paysupport",
    "/ai": "ai",
}


def button_on(screen_id: str, label: str) -> Button | None:
    screen = SCREENS.get(screen_id)
    if screen is None:
        return None
    for button in buttons(screen_id):
        if button.label == label:
            return button
    return None


def body_buttons(screen_id: str) -> tuple[Button, ...]:
    return SCREENS[screen_id].buttons


def buttons(screen_id: str) -> tuple[Button, ...]:
    body = body_buttons(screen_id)
    if screen_id in NO_NAV_SCREENS:
        return body
    return body + (_back(), _home())


def render(
    screen_id: str,
    *,
    bot_name: str,
    support_contact: str,
    status: str | None = None,
    history: str | None = None,
    model: str | None = None,
) -> str:
    text = SCREENS[screen_id].text
    return (
        text.replace("{bot_name}", bot_name)
        .replace("{support_contact}", support_contact)
        .replace("{status}", status or DEFAULT_STATUS)
        .replace("{history}", history or DEFAULT_HISTORY)
        .replace("{model}", model or DEFAULT_MODEL)
    )


def to_html(text: str) -> str:
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
    return re.sub(r"__(.+?)__", r"<i>\1</i>", escaped)
