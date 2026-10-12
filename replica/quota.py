import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from replica.catalog import WINDOW_CYCLE, WINDOW_DAY, WINDOW_PERIOD, WINDOW_WEEK

LOCAL_TZ = timezone(timedelta(hours=3))
DAY = timedelta(days=1)
WEEK = timedelta(days=7)
CYCLE = timedelta(days=30)


@dataclass(frozen=True)
class Bucket:
    window: str
    quota: int
    valid_from: datetime
    valid_until: datetime | None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime | None
    quota: int


@dataclass(frozen=True)
class Decision:
    allowed: bool
    remaining: int
    resets_at: datetime | None


def from_ts(value: int) -> datetime:
    return datetime.fromtimestamp(value, LOCAL_TZ)


def to_ts(value: datetime) -> int:
    return int(value.timestamp())


def day_start(now: datetime) -> datetime:
    local = now.astimezone(LOCAL_TZ)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def _blocks(bucket: Bucket, now: datetime, size: timedelta, prorate: bool) -> Window:
    index = (now - bucket.valid_from) // size
    start = bucket.valid_from + size * index
    end = start + size
    quota = bucket.quota
    if bucket.valid_until is not None and end > bucket.valid_until:
        end = bucket.valid_until
        if prorate:
            quota = math.ceil(bucket.quota * (end - start) / size)
    return Window(start=start, end=end, quota=quota)


def window_for(bucket: Bucket, now: datetime) -> Window | None:
    if bucket.valid_until is not None and now >= bucket.valid_until:
        return None
    if bucket.window == WINDOW_DAY:
        start = day_start(now)
        return Window(start=start, end=start + DAY, quota=bucket.quota)
    if now < bucket.valid_from:
        return None
    if bucket.window == WINDOW_WEEK:
        return _blocks(bucket, now, WEEK, prorate=True)
    if bucket.window == WINDOW_CYCLE:
        return _blocks(bucket, now, CYCLE, prorate=False)
    if bucket.window == WINDOW_PERIOD:
        return Window(start=bucket.valid_from, end=bucket.valid_until, quota=bucket.quota)
    raise ValueError(f"unknown window: {bucket.window}")


def decide(used: int, window: Window) -> Decision:
    remaining = max(window.quota - used, 0)
    return Decision(allowed=remaining > 0, remaining=remaining, resets_at=window.end)


def duration_text(delta: timedelta) -> str:
    total = max(int(delta.total_seconds()), 0)
    days, rest = divmod(total, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"{days} дн {hours} ч"
    if hours:
        return f"{hours} ч {minutes} мин"
    return f"{max(minutes, 1)} мин"


def reset_text(moment: datetime, now: datetime) -> str:
    local = moment.astimezone(LOCAL_TZ)
    return f"через {duration_text(moment - now)} ({local:%d.%m, %H:%M} UTC+3)"
