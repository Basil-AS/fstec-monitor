from __future__ import annotations

from datetime import datetime, time, timedelta

DAILY_NOON = "daily_noon"
DAILY_MIDNIGHT = "daily_midnight"
EVERY_TWO_HOURS = "every_two_hours"
DISABLED = "disabled"

SCHEDULE_MODES = (DAILY_NOON, DAILY_MIDNIGHT, EVERY_TWO_HOURS, DISABLED)

_LABELS = {
    DAILY_NOON: "раз в сутки в 12:00",
    DAILY_MIDNIGHT: "раз в сутки в 00:00",
    EVERY_TWO_HOURS: "каждые 2 часа",
    DISABLED: "автозапуск выключен",
}


def schedule_label(mode: str) -> str:
    try:
        return _LABELS[mode]
    except KeyError as exc:
        raise ValueError(f"Unknown schedule mode: {mode}") from exc


def next_scheduled_at(mode: str, now: datetime) -> datetime | None:
    if mode not in SCHEDULE_MODES:
        raise ValueError(f"Unknown schedule mode: {mode}")
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if mode == DISABLED:
        return None
    if mode == EVERY_TWO_HOURS:
        next_hour = ((now.hour // 2) + 1) * 2
        candidate_date = now.date()
        if next_hour >= 24:
            candidate_date += timedelta(days=1)
            next_hour = 0
        return datetime.combine(candidate_date, time(next_hour), tzinfo=now.tzinfo)

    target_hour = 12 if mode == DAILY_NOON else 0
    candidate = datetime.combine(now.date(), time(target_hour), tzinfo=now.tzinfo)
    if candidate <= now:
        candidate += timedelta(days=1)
    return candidate
