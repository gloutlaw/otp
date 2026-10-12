import asyncio
from pathlib import Path

from collect_bots import render_exchange
from collect_branches import open_client
from collect_images import BOT, Session, retry_flood

OUTPUT = Path(__file__).resolve().parent / "internet-probe.txt"
QUESTION = "Есть ли у тебя доступ к интернету и можешь ли ты узнать текущий курс валюты? Ответь одним предложением."


async def main() -> None:
    client = await open_client()
    try:
        entity = await retry_flood(lambda: client.get_entity(BOT))
        session = Session(client, entity)
        sent = await session.send(QUESTION)
        final, _seen = await session.watch(sent.id, 75, 8, 12)
        lines = render_exchange("Вопрос о доступе к интернету", QUESTION, final)
        OUTPUT.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    finally:
        await client.disconnect()
    print(f"Записано {OUTPUT.name}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
