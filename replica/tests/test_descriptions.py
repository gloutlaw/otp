import asyncio

import replica.telegram as telegram
from replica.descriptions import (
    DESCRIPTION_EN,
    DESCRIPTION_RU,
    DESCRIPTIONS,
    SHORT_DESCRIPTION_EN,
    SHORT_DESCRIPTION_RU,
    SHORT_DESCRIPTIONS,
)

TELEGRAM_DESCRIPTION_LIMIT = 512


def test_descriptions_fit_the_telegram_limit() -> None:
    assert len(DESCRIPTION_RU) <= TELEGRAM_DESCRIPTION_LIMIT
    assert len(DESCRIPTION_EN) <= TELEGRAM_DESCRIPTION_LIMIT


def test_default_and_russian_share_the_russian_text() -> None:
    assert DESCRIPTIONS[None] == DESCRIPTION_RU
    assert DESCRIPTIONS["ru"] == DESCRIPTION_RU
    assert DESCRIPTIONS["en"] == DESCRIPTION_EN


def test_free_message_claim_is_not_promised() -> None:
    assert "10 сообщений" not in DESCRIPTION_RU
    assert "Stars" not in DESCRIPTION_EN


def test_short_descriptions_fit_the_telegram_limit() -> None:
    assert len(SHORT_DESCRIPTION_RU) <= 120
    assert len(SHORT_DESCRIPTION_EN) <= 120


def test_set_descriptions_sets_each_language_and_the_default() -> None:
    descriptions: list[tuple[str | None, str]] = []
    short_descriptions: list[tuple[str | None, str]] = []

    class FakeBot:
        async def set_my_description(self, description: str, language_code: str | None = None) -> None:
            descriptions.append((language_code, description))

        async def set_my_short_description(
            self, short_description: str, language_code: str | None = None
        ) -> None:
            short_descriptions.append((language_code, short_description))

    asyncio.run(telegram.set_descriptions(FakeBot()))
    assert sorted(descriptions, key=lambda item: str(item[0])) == sorted(
        DESCRIPTIONS.items(), key=lambda item: str(item[0])
    )
    assert sorted(short_descriptions, key=lambda item: str(item[0])) == sorted(
        SHORT_DESCRIPTIONS.items(), key=lambda item: str(item[0])
    )
