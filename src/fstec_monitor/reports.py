from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .models import Event


def fmt_dt(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z") if value else "нет данных"


def event_report(event: Event, title: str = "Документ", url: str = "") -> str:
    return "\n".join(
        (
            "Отчёт об изменении ФСТЭК Monitor",
            f"Событие #{event.id}",
            f"Время: {fmt_dt(event.created_at)}",
            f"Тип: {event.kind}",
            f"Важность: {event.severity}",
            f"Документ: {title}",
            f"URL: {url or 'нет данных'}",
            "",
            "Кратко:",
            event.summary,
            "",
            "Подробности / old-new diff:",
            event.details or "нет подробностей",
            "",
        )
    )


def safe_filename(name: str, suffix: str = ".txt") -> str:
    clean = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in name).strip("._")
    return (clean[:80] or "fstec-report") + suffix


def report_path(directory: Path, event: Event, title: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    return directory / safe_filename(f"event-{event.id}-{title}")
