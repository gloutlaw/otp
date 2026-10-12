import asyncio
import logging
import os
import sys
from pathlib import Path

from aiogram import Bot

from support.bot import Desk, build_dispatcher
from support.store import Store

ROOT = Path(__file__).resolve().parent
DATA_PATH = ROOT / "data" / "support.db"
REPLICA_DB_PATH = ROOT.parent / "replica" / "data" / "replica.db"


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
        raise SystemExit(f"Задайте {name} в support/.env")
    return value


def owner_id_from_env() -> int:
    raw = require_env("OWNER_ID")
    if not raw.isdigit():
        raise SystemExit("OWNER_ID в support/.env: числовой Telegram ID.")
    return int(raw)


async def run(token: str, desk: Desk) -> None:
    bot = Bot(token)
    await build_dispatcher(desk).start_polling(bot)


def main() -> None:
    load_env_file(ROOT / ".env")
    token = require_env("SUPPORT_BOT_TOKEN")
    logging.basicConfig(level=logging.INFO)
    token_filter = TokenMaskFilter(token)
    for handler in logging.getLogger().handlers:
        handler.addFilter(token_filter)
    desk = Desk(owner_id=owner_id_from_env(), store=Store(DATA_PATH), replica_db=REPLICA_DB_PATH)
    try:
        asyncio.run(run(token, desk))
    finally:
        desk.store.close()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
