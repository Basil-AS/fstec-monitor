# Interactive Daily Schedule Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Telegram bot a convenient, resource-safe administrator console with a persistent once-daily 12:00 schedule and verified behavior.

**Architecture:** Add a small persistent `BotSetting` key/value table for the selected schedule, isolate schedule calculations in a pure module, and keep Telegram callback handling in `telegram_bot.py`. The bot will wait for the next local scheduled time instead of scanning immediately; systemd will also be changed to a daily 12:00 timer.

**Tech Stack:** Python 3.11, SQLAlchemy, SQLite/PostgreSQL-compatible ORM, httpx Telegram Bot API, pytest, Ruff, systemd.

## Global Constraints

- Default automatic mode is once daily at 12:00 local server time.
- Manual `/scan` remains available and never starts a duplicate concurrent scan.
- Schedule choices persist in SQLite and survive process restarts.
- Administrator authorization is required for settings and operational commands.
- Existing history and object-store behavior must remain unchanged.
- Verify with pytest, Ruff, Python compilation, systemd unit inspection, and diff checks.

### Task 1: Add persistent schedule settings and pure scheduler

**Files:**
- Modify: `src/fstec_monitor/models.py`
- Modify: `src/fstec_monitor/db.py`
- Create: `src/fstec_monitor/schedule.py`
- Test: `tests/test_schedule.py`

**Interfaces:**
- `ScheduleMode` values: `daily_noon`, `daily_midnight`, `every_two_hours`, `disabled`.
- `next_scheduled_at(mode: str, now: datetime) -> datetime | None` returns a timezone-aware local datetime.
- `schedule_label(mode: str) -> str` returns the Russian user-facing label.
- `BotSetting(key: str, value: str)` stores persistent settings.

- [x] **Step 1: Write failing scheduler tests** for all four modes, including a time after 12:00 rolling to the next day and disabled mode returning `None`.
- [x] **Step 2: Run `pytest tests/test_schedule.py -q`** and confirm the new imports/functions fail.
- [x] **Step 3: Add `BotSetting` and idempotent SQLite migration** in the existing `init_db()` pattern.
- [x] **Step 4: Implement pure schedule labels and next-run calculation** using local timezone-aware datetimes.
- [x] **Step 5: Run the focused tests and commit** with `feat(schedule): persist bot schedule settings`.

### Task 2: Make the bot scheduler safe and expose settings state

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/config.py`
- Test: `tests/test_telegram_bot.py`

**Interfaces:**
- `TelegramBot.get_schedule_mode() -> str` reads the DB setting and falls back to `daily_noon`.
- `TelegramBot.set_schedule_mode(mode: str) -> None` validates and persists one of the four modes.
- `TelegramBot.settings_text() -> str` includes the mode and calculated next run.
- `admin_keyboard()` includes `⚙️ Настройки` and `🔍 Проверить сейчас` buttons.

- [x] **Step 1: Add failing tests** for keyboard labels, schedule persistence, settings text, and `scan_is_running()` protection.
- [x] **Step 2: Run focused bot tests** and confirm failures.
- [x] **Step 3: Implement DB-backed getters/setters** and add schedule information to `/status`.
- [x] **Step 4: Remove unconditional `self.start_scan()` from `run()`** and calculate the next local scheduled time; support disabled mode without a polling busy-loop.
- [x] **Step 5: Run focused tests and commit** with `feat(bot): add safe persistent schedule controls`.

### Task 3: Add interactive Telegram menu and callbacks

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_telegram_bot.py`

**Interfaces:**
- `settings_keyboard() -> dict` returns inline buttons for all schedule modes and main-menu navigation.
- Callback data format: `settings:set:<mode>` and `menu:main`.
- `/settings` is administrator-only and renders `settings_text()` with `settings_keyboard()`.

- [x] **Step 1: Write failing tests** for `/start`, `/settings`, callback mode selection, unknown callback data, and non-admin rejection.
- [x] **Step 2: Run focused tests to confirm failures.**
- [x] **Step 3: Implement the inline keyboard and callback dispatch**, always answering callback queries and safely ignoring malformed data.
- [x] **Step 4: Improve `/help`, `/start`, `/scan`, `/status`, and error responses** so the menu is discoverable and operations report state clearly.
- [x] **Step 5: Run the full Telegram test module and commit** with `feat(bot): add interactive admin menu`.

### Task 4: Align deployment schedule and documentation

**Files:**
- Modify: `systemd/fstec-monitor.timer`
- Modify: `README.md`
- Test: `tests/test_deployment_config.py`

- [x] **Step 1: Write tests** asserting the timer has `OnCalendar=*-*-* 12:00:00` and documentation describes daily 12:00 behavior.
- [x] **Step 2: Update the timer description/calendar** and replace stale two-hour documentation.
- [x] **Step 3: Run deployment tests and commit** with `fix(deploy): schedule monitor daily at noon`.

### Task 5: Full verification and behavior audit

**Files:**
- Modify: `tests/test_telegram_bot.py` or focused test files only if coverage gaps remain.

- [x] **Step 1: Run `pytest -q`** and inspect every failure rather than masking errors.
- [x] **Step 2: Run `ruff check src tests` and `python -m compileall -q src tests`.**
- [x] **Step 3: Run `git diff --check`, inspect `git diff --stat`, and search for stale two-hour/default-immediate-start references.
- [x] **Step 4: Record verification evidence, update the implementation plan, and prepare the branch for review/PR without pushing or merging automatically.
