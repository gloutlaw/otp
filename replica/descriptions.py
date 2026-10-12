DESCRIPTION_RU = """Личный AI-помощник MewTon.

Напишите задачу в чат:
— объяснить сложное простыми словами;
— написать или поправить текст;
— перевести и проверить формулировку;
— помочь с кодом и таблицами.

Улучшить, сократить или пересчитать ответ сильнее — в «Инструментах» под ответом."""

DESCRIPTION_EN = """MewTon, a personal AI assistant.

Send a task in the chat:
— explain something complex in plain words;
— write or edit a text;
— translate and check the wording;
— help with code and spreadsheets.

Improve, shorten or re-answer more strongly via Tools under the answer."""

SHORT_DESCRIPTION_RU = "Личный AI-помощник MewTon. Напишите задачу в чат."
SHORT_DESCRIPTION_EN = "MewTon, a personal AI assistant. Send a task in the chat."

DEFAULT_LANGUAGE = None
DESCRIPTIONS: dict[str | None, str] = {
    DEFAULT_LANGUAGE: DESCRIPTION_RU,
    "ru": DESCRIPTION_RU,
    "en": DESCRIPTION_EN,
}
SHORT_DESCRIPTIONS: dict[str | None, str] = {
    DEFAULT_LANGUAGE: SHORT_DESCRIPTION_RU,
    "ru": SHORT_DESCRIPTION_RU,
    "en": SHORT_DESCRIPTION_EN,
}
