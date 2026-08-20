# Управление проверкой и прогресс Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать администратору единый экран состояния проверки с прогрессом, повтором, остановкой и защитой от параллельных запусков.

**Architecture:** Состояние scan хранится в `ScanProgress` внутри `TelegramBot`, а crawler сообщает прогресс через callback и получает `asyncio.Event` отмены. Telegram-карточка редактируется на месте и использует callback-кнопки, поэтому повторные нажатия не создают спам.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy, httpx, Telegram Bot API, pytest.

## Global Constraints

- Второй scan никогда не запускается поверх активного.
- Остановка не считается ошибкой и освобождает возможность нового запуска.
- Ошибка одного документа не завершает весь scan.
- Обычные пользователи не получают управление сканированием.
- Токены не попадают в тексты статуса и логи.

---

### Task 1: Модель состояния проверки

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_telegram_bot.py`

- [ ] Добавить failing-тесты для состояния idle/running/completed/failed/cancelled, форматирования прогресса и запрета второго запуска.
- [ ] Запустить focused tests и убедиться, что они падают по отсутствующему состоянию/методам.
- [ ] Реализовать `ScanProgress`, `scan_progress_text()`, `start_scan()` с сохранением последнего результата и `stop_scan()` через `asyncio.Event`.
- [ ] Запустить focused tests и получить GREEN.
- [ ] Зафиксировать коммит `feat(bot): track scan progress and lifecycle`.

### Task 2: Прогресс из crawler

**Files:**
- Modify: `src/fstec_monitor/crawler.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_crawler.py`

- [ ] Написать failing-тест на callbacks после discovery, после каждого URL и при отмене.
- [ ] Запустить focused tests и увидеть RED.
- [ ] Добавить callback `progress(stage, completed, total, errors)` и `cancel_event` в `run_monitor`; проверять отмену между worker-задачами и закрывать monitor в `finally`.
- [ ] Передать callback из `_scan_task`, обновлять объект состояния без блокировки event loop.
- [ ] Запустить crawler tests и получить GREEN.
- [ ] Зафиксировать коммит `feat(crawler): report progress and support cancellation`.

### Task 3: Telegram UX управления

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_telegram_bot.py`

- [ ] Написать failing-тесты на карточку статуса, callbacks `scan:status`, `scan:stop:confirm`, `scan:retry` и отсутствие второго запуска.
- [ ] Запустить focused tests и увидеть RED.
- [ ] Добавить inline-клавиатуры запуска/обновления/остановки/повтора, обработать подтверждение остановки и редактирование сообщения.
- [ ] Встроить карточку в `/status`, `📊 Статус` и `🔍 Проверить сейчас`; ограничить callbacks admin ID.
- [ ] Запустить focused tests и получить GREEN.
- [ ] Зафиксировать коммит `feat(bot): add scan control card`.

### Task 4: Ошибки, документация и remote rollout

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-08-21-scan-progress-control-design.md`
- Test: full suite

- [ ] Добавить в README описание состояний, кнопок, остановки и повторного запуска.
- [ ] Запустить полный `pytest`, Ruff и compileall.
- [ ] Синхронизировать только исходники бота на `mxbox` без `--delete`, перезапустить service и проверить `systemctl`, journal, live catalog и smoke callbacks.
- [ ] Зафиксировать remote результат и оставить незакрытые сетевые ошибки в журнале.
