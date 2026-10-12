from dataclasses import dataclass
from datetime import timedelta

SUBSCRIPTION = "subscription"
PACK = "pack"

WINDOW_DAY = "day_local"
WINDOW_WEEK = "week_of_period"
WINDOW_PERIOD = "period"
WINDOW_CYCLE = "cycle30"

PERIOD = timedelta(days=30)
PERIOD_SECONDS = int(PERIOD.total_seconds())
GRACE = timedelta(hours=2)
REMINDER_BEFORE = timedelta(days=3)
PACK_LIFETIME = timedelta(days=90)
INVOICE_TTL = timedelta(minutes=30)
PRECHECKOUT_LOCK = timedelta(minutes=2)
RESERVATION_TTL = timedelta(minutes=3)
ANSWER_TTL = timedelta(days=30)

TERMS_VERSION = "2026-10-12"
CURRENCY = "XTR"
STAR_USD = 0.012
FREE_CHEAP_PER_DAY = 10
TRIAL_SMART = 3
SMART_INPUT_TOKENS = 4000

PLAN_TITLES = {"free": "Бесплатный", "start": "Старт", "pro": "Pro", "trial": "пробный"}


@dataclass(frozen=True)
class Product:
    code: str
    title: str
    stars: int
    kind: str
    cheap: int = 0
    smart: int = 0


PRODUCTS: dict[str, Product] = {
    "start": Product("start", "Старт", 300, SUBSCRIPTION, cheap=400, smart=20),
    "pro": Product("pro", "Pro", 600, SUBSCRIPTION, cheap=500, smart=150),
    "pack_smart_100": Product("pack_smart_100", "+100 умных ответов", 219, PACK, smart=100),
}

SUBSCRIPTION_PLANS = ("start", "pro")

CHEAP_TIER = "cheap"
PREMIUM_TIER = "premium"


@dataclass(frozen=True)
class ChatModel:
    slug: str
    title: str
    tier: str


CHAT_MODELS: tuple[ChatModel, ...] = (
    ChatModel("anthropic/claude-haiku-5.5", "Claude Haiku 5.5", CHEAP_TIER),
    ChatModel("deepseek/deepseek-v4.1-flash", "DeepSeek V4.1 Flash", CHEAP_TIER),
    ChatModel("google/gemini-3.8-flash", "Gemini 3.8 Flash", PREMIUM_TIER),
    ChatModel("x-ai/grok-4.7", "Grok 4.7", PREMIUM_TIER),
    ChatModel("moonshotai/kimi-k3", "Kimi K3", PREMIUM_TIER),
    ChatModel("openai/gpt-6.1-sol", "GPT-6.1 Sol", PREMIUM_TIER),
    ChatModel("anthropic/claude-sonnet-5.5", "Claude Sonnet 5.5", PREMIUM_TIER),
    ChatModel("anthropic/claude-opus-5.5", "Claude Opus 5.5", PREMIUM_TIER),
)
DEFAULT_CHAT_MODEL = CHAT_MODELS[0]


def chat_model_by_slug(slug: str) -> ChatModel | None:
    return next((model for model in CHAT_MODELS if model.slug == slug), None)


def chat_model_by_title(title: str) -> ChatModel | None:
    return next((model for model in CHAT_MODELS if model.title == title), None)
