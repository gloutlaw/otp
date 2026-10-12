def parse_user_ids(raw: str) -> tuple[int, ...]:
    items = [item.strip() for item in raw.split(",") if item.strip()]
    try:
        ids = [int(item) for item in items]
    except ValueError as exc:
        raise SystemExit("ALLOWED_USER_IDS в replica/.env: числа через запятую.") from exc
    return tuple(dict.fromkeys(ids))


def owner_of(allowed: tuple[int, ...]) -> int | None:
    return allowed[0] if allowed else None
