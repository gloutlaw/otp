import math
from datetime import datetime, timedelta

from replica.catalog import WINDOW_CYCLE, WINDOW_DAY, WINDOW_PERIOD, WINDOW_WEEK
from replica.quota import LOCAL_TZ, Bucket, decide, duration_text, window_for


def at(year: int, month: int, day: int, hour: int = 12) -> datetime:
    return datetime(year, month, day, hour, tzinfo=LOCAL_TZ)


def bucket(window: str, quota: int, start: datetime, end: datetime | None = None) -> Bucket:
    return Bucket(window=window, quota=quota, valid_from=start, valid_until=end)


def test_day_window_is_local_calendar_day() -> None:
    window = window_for(bucket(WINDOW_DAY, 10, at(2026, 10, 1)), at(2026, 10, 14, 23))
    assert window is not None
    assert window.start == at(2026, 10, 14, 0)
    assert window.end == at(2026, 10, 15, 0)
    assert window.quota == 10


def test_weekly_blocks_start_at_the_purchase_moment_not_on_monday() -> None:
    anchor = at(2026, 10, 20, 14)
    subscription = bucket(WINDOW_WEEK, 400, anchor, anchor + timedelta(days=30))
    window = window_for(subscription, anchor + timedelta(days=8))
    assert window is not None
    assert window.start == anchor + timedelta(days=7)
    assert window.end == anchor + timedelta(days=14)
    assert window.quota == 400


def test_last_partial_block_of_the_period_gets_a_proportional_quota() -> None:
    anchor = at(2026, 10, 20, 14)
    end = anchor + timedelta(days=30)
    pro = bucket(WINDOW_WEEK, 500, anchor, end)
    window = window_for(pro, anchor + timedelta(days=29))
    assert window is not None
    assert window.start == anchor + timedelta(days=28)
    assert window.end == end
    assert window.quota == math.ceil(500 * 2 / 7) == 143
    start = bucket(WINDOW_WEEK, 400, anchor, end)
    assert window_for(start, anchor + timedelta(days=29)).quota == math.ceil(400 * 2 / 7) == 115


def test_full_blocks_inside_the_period_keep_the_full_quota() -> None:
    anchor = at(2026, 10, 20, 14)
    pro = bucket(WINDOW_WEEK, 500, anchor, anchor + timedelta(days=30))
    for day in (0, 6, 7, 13, 14, 20, 21, 27):
        assert window_for(pro, anchor + timedelta(days=day)).quota == 500


def test_period_quota_is_a_single_window_until_the_end() -> None:
    anchor = at(2026, 10, 20, 14)
    end = anchor + timedelta(days=30)
    smart = bucket(WINDOW_PERIOD, 150, anchor, end)
    window = window_for(smart, anchor + timedelta(days=25))
    assert window is not None
    assert window.start == anchor
    assert window.end == end
    assert window.quota == 150


def test_expired_bucket_has_no_window() -> None:
    anchor = at(2026, 10, 20, 14)
    smart = bucket(WINDOW_PERIOD, 150, anchor, anchor + timedelta(days=30))
    assert window_for(smart, anchor + timedelta(days=30)) is None
    assert window_for(bucket(WINDOW_WEEK, 400, anchor, anchor + timedelta(days=30)), anchor + timedelta(days=31)) is None


def test_cycle_without_expiry_repeats_every_thirty_days() -> None:
    anchor = at(2026, 10, 1, 9)
    smart = bucket(WINDOW_CYCLE, 150, anchor, None)
    window = window_for(smart, anchor + timedelta(days=45))
    assert window is not None
    assert window.start == anchor + timedelta(days=30)
    assert window.end == anchor + timedelta(days=60)
    assert window.quota == 150


def test_decide_counts_down_and_reports_the_window_end() -> None:
    anchor = at(2026, 10, 20, 14)
    window = window_for(bucket(WINDOW_WEEK, 400, anchor, None), anchor)
    assert decide(399, window).allowed
    assert decide(399, window).remaining == 1
    denied = decide(400, window)
    assert not denied.allowed
    assert denied.remaining == 0
    assert denied.resets_at == anchor + timedelta(days=7)


def test_duration_text_is_human_readable() -> None:
    assert duration_text(timedelta(days=3, hours=4, minutes=10)) == "3 дн 4 ч"
    assert duration_text(timedelta(hours=6, minutes=40)) == "6 ч 40 мин"
    assert duration_text(timedelta(minutes=12)) == "12 мин"
    assert duration_text(timedelta(seconds=-5)) == "1 мин"
