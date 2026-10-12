"""Pure renderers for compact Telegram status screens."""

from __future__ import annotations

import html
import re
from html import escape


def progress_bar(percent: int, width: int = 10) -> str:
    percent = max(0, min(100, int(percent)))
    filled = round(width * percent / 100)
    return "█" * filled + "░" * (width - filled)


def escape_html(value: object) -> str:
    """Escape dynamic values before placing them in Telegram HTML."""
    return escape(str(value), quote=False)


def has_html_markup(value: str) -> bool:
    return "<b>" in value or "</b>" in value or "<i>" in value or "</i>" in value


def render_scan_progress(progress) -> str:
    percent = max(0, min(100, int(getattr(progress, "percent", 0))))
    stage = escape_html(str(getattr(progress, "stage", "Проверка"))[:120])
    completed = max(0, int(getattr(progress, "completed", 0)))
    total = max(0, int(getattr(progress, "total", 0)))
    errors = max(0, int(getattr(progress, "errors", 0)))
    last_error = escape_html(str(getattr(progress, "last_error", ""))[:180])
    error_line = f"\nПоследняя ошибка: {last_error}" if last_error else ""
    return f"🔍 <b>Проверка каталога</b>\n{progress_bar(percent)} {percent}%\n{stage}\nДокументы: {completed}/{total}\nОшибки: {errors}{error_line}"


def render_error_notice(context: str, error_id: int | str | None = None) -> str:
    safe_context = str(context).strip()[:80] or "операция"
    suffix = f" · ID {error_id}" if isinstance(error_id, int) else ""
    return f"⚠️ Не удалось выполнить: {html.escape(safe_context)}.{suffix}\nПодробности доступны в журнале сервера."


def markdown_to_telegram_html(markdown: str) -> str:
    """Render the small Markdown subset used by screens as safe Telegram HTML."""
    rendered: list[str] = []
    for line in markdown.splitlines():
        if line.startswith("### "):
            rendered.append(f"<b>{html.escape(line[4:])}</b>")
        elif line.startswith("## "):
            rendered.append(f"<b>{html.escape(line[3:])}</b>")
        elif line.startswith("# "):
            rendered.append(f"<b>{html.escape(line[2:])}</b>")
        elif line.startswith(("- ", "* ")):
            rendered.append("• " + html.escape(line[2:]))
        else:
            escaped = html.escape(line)
            escaped = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", escaped)
            escaped = re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
            rendered.append(escaped)
    return "\n".join(rendered)
