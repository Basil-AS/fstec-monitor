# UX меню и права Telegram-пользователей Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Дать администратору расширенное красивое меню, обычным пользователям — только чтение/уведомления, и доставлять события каждому одобренному чату.

**Architecture:** Reply UI строится из двух наборов текстовых действий и переводит labels в существующие slash-команды. Права остаются серверной проверкой в `handle` и `handle_callback`. Доставка отделяется от общего флага `Event.notified` таблицей `EventDelivery`, а персональные категории — таблицей `UserIgnoredCategory`.

**Tech Stack:** Python 3.11+, asyncio, SQLAlchemy, SQLite/PostgreSQL, pytest.

## Global Constraints

- Обычный пользователь не запускает сканирование и не управляет ошибками, настройками, игнором или доступом.
- Все одобренные пользователи получают автоматические сводки изменений.
- Slash-команды сохраняются как обратная совместимость, но не показываются на reply-клавиатуре.
- Токены и содержимое `.env` не выводятся в логи.

---

### Task 1: Текстовые меню, permissions и персональный ignore

**Files:** `src/fstec_monitor/telegram_bot.py`, `src/fstec_monitor/models.py`, `src/fstec_monitor/db.py`, `tests/test_telegram_bot.py`

- [x] Написать failing-тесты на красивые уникальные labels, user keyboard, mapping labels и запрет admin-команд обычному пользователю.
- [x] Запустить focused tests и увидеть RED.
- [x] Добавить `user_keyboard`, label mappings, расширенное admin menu и whitelist пользовательских команд; подключить персональный список категорий пользователя и notification toggle в settings.
- [x] Запустить focused tests и получить GREEN.
- [x] Commit `feat(bot): add role-specific Telegram menus`.

### Task 2: Персональная доставка событий

**Files:** `src/fstec_monitor/models.py`, `src/fstec_monitor/db.py`, `src/fstec_monitor/notify.py`, `tests/test_notify.py`

- [x] Написать failing-тест на доставку одного события администратору и двум approved чатам с независимыми delivery records.
- [x] Запустить focused test и увидеть RED.
- [x] Добавить `EventDelivery`, SQLite migration и fan-out сводок по получателям; `Event.notified` выставлять только после доставки всем текущим получателям.
- [x] Запустить focused tests и получить GREEN.
- [x] Commit `feat(notify): fan out updates to approved users`.

### Task 3: Проверка и remote rollout

**Files:** `README.md`, `tests/test_deployment_config.py`

- [x] Обновить документацию по ролям и кнопкам.
- [x] Запустить полный local suite, Ruff и compileall.
- [ ] Синхронизировать изменённые файлы на `mxbox`, перезапустить `fstec-monitor.service`, проверить unit, journal, БД и безопасный smoke-test UI.
- [ ] Зафиксировать результаты и оставшиеся внешние сетевые блокеры без отключения TLS.
