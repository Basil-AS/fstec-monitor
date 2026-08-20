"""Pure renderers for compact Telegram status screens."""

from __future__ import annotations


def progress_bar(percent: int, width: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def render_scan_progress(progress) -> str:
    percent = max(0, min(100, int(getattr(progress, "percent", 0))))
    stage = str(getattr(progress, "stage", "Проверка"))[:120]
    completed = max(0, int(getattr(progress, "completed", 0)))
    total = max(0, int(getattr(progress, "total", 0)))
    errors = max(0, int(getattr(progress, "errors", 0)))
    return f"🔍 <b>Проверка каталога</b>\n{progress_bar(percent)} {percent}%\n{stage}\nДокументы: {completed}/{total}\nОшибки: {errors}"


def render_error_notice(context: str, error_id: int | str | None = None) -> str:
    safe_context = str(context).strip()[:80] or "операция"
    suffix = f" · ID {error_id}" if isinstance(error_id, int) else ""
    return f"⚠️ Не удалось выполнить: {safe_context}.{suffix}\nОшибка записана в журнал."
