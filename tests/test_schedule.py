from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from fstec_monitor.schedule import (
    DAILY_MIDNIGHT,
    DAILY_NOON,
    DISABLED,
    EVERY_TWO_HOURS,
    next_scheduled_at,
    schedule_label,
)

MOSCOW = ZoneInfo("Europe/Moscow")


def test_daily_noon_rolls_to_next_local_noon():
    now = datetime(2026, 8, 17, 13, 5, tzinfo=MOSCOW)

    assert next_scheduled_at(DAILY_NOON, now) == datetime(2026, 8, 18, 12, tzinfo=MOSCOW)


def test_daily_noon_at_1159_runs_same_day():
    now = datetime(2026, 8, 17, 11, 59, tzinfo=MOSCOW)

    assert next_scheduled_at(DAILY_NOON, now) == datetime(2026, 8, 17, 12, tzinfo=MOSCOW)


def test_daily_midnight_and_two_hour_modes():
    now = datetime(2026, 8, 17, 10, 35, tzinfo=MOSCOW)

    assert next_scheduled_at(DAILY_MIDNIGHT, now) == datetime(2026, 8, 18, 0, tzinfo=MOSCOW)
    assert next_scheduled_at(EVERY_TWO_HOURS, now) == datetime(2026, 8, 17, 12, tzinfo=MOSCOW)


def test_disabled_mode_has_no_next_run():
    now = datetime(2026, 8, 17, 10, 35, tzinfo=MOSCOW)

    assert next_scheduled_at(DISABLED, now) is None


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError, match="Unknown schedule mode"):
        next_scheduled_at("unknown", datetime.now(MOSCOW))


def test_schedule_labels_are_user_facing():
    assert schedule_label(DAILY_NOON) == "раз в сутки в 12:00"
    assert schedule_label(DISABLED) == "автозапуск выключен"
