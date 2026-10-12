import asyncio
import logging
import os
import sys
from pathlib import Path

from replica.access import parse_user_ids
from replica.store import Store
from replica.telegram import run

DATA_PATH = Path(__file__).resolve().parent / "data" / "replica.db"


class TokenMaskFilter(logging.Filter):
    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self._mask(record.getMessage())
        record.args = ()
        # Formatter reuses a cached exc_text, so the traceback must be masked here.
        if record.exc_info:
            record.exc_text = self._mask(logging.Formatter().formatException(record.exc_info))
        if record.stack_info:
            record.stack_info = self._mask(record.stack_info)
        return True

    def _mask(self, text: str) -> str:
        return text.replace(self._token, "<TOKEN>") if self._token else text


def load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value or value.startswith("<"):
        raise SystemExit(f"Задайте {name} в replica/.env")
    return value


def main() -> None:
    load_env_file(Path(__file__).resolve().parent / ".env")
    token = require_env("BOT_TOKEN")
    logging.basicConfig(level=logging.INFO)
    token_filter = TokenMaskFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(token_filter)
    bot_name = require_env("BOT_NAME")
    support_contact = require_env("SUPPORT_CONTACT")
    api_key = require_env("OPENROUTER_API_KEY")
    allowed = parse_user_ids(os.environ.get("ALLOWED_USER_IDS", ""))
    if not allowed:
        print("ALLOWED_USER_IDS пуст: владелец не задан, команда /grant недоступна.", flush=True)
    dev = "--no-dev" not in sys.argv[1:]
    store = Store(DATA_PATH)
    try:
        asyncio.run(run(token, bot_name, support_contact, api_key, allowed, store, dev))
    finally:
        store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
