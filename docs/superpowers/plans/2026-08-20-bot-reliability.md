# Надёжный Telegram-бот и корректное отслеживание документов Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Устранить Telegram-спам и сделать обнаружение изменений документов, ошибок и зависаний предсказуемым.

**Architecture:** Сохранить текущие SQLite/SQLAlchemy и crawler, но вынести политику уведомлений и UI в небольшие чистые функции. Жизненный цикл документа обновляется после полного discovery, а фоновые задачи и HTTP-клиенты получают явные границы и диагностику.

**Tech Stack:** Python 3.11+, asyncio, httpx, SQLAlchemy, pytest, Ruff.

## Global Constraints

- Уведомлять только о содержательных изменениях и подтверждённых удалениях.
- Не скрывать исключения и не удалять тестовые проверки.
- Не менять пользовательское незакоммиченное изменение в `uv.lock`.
- Все изменения делать на ветке `agent/fix/bot-reliability`.

---

### Task 1: Единая модель Telegram UI

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_telegram_bot.py`

- [ ] **Step 1: Write failing tests** for unique command names, one canonical keyboard action per command, and consistent back/cancel callbacks for settings, scan, errors, users and ignore screens.
- [ ] **Step 2: Run** `pytest tests/test_telegram_bot.py -q` and verify the new assertions fail against the current duplicated definitions.
- [ ] **Step 3: Implement** shared action constants/builders and route reply text plus command menu through them; preserve existing command names while removing duplicate visible actions.
- [ ] **Step 4: Run** the focused tests and verify they pass.
- [ ] **Step 5: Commit** `fix(bot): unify Telegram menus and callbacks`.

### Task 2: Дедупликация и сводка уведомлений

**Files:**
- Modify: `src/fstec_monitor/notify.py`, `src/fstec_monitor/crawler.py`, `src/fstec_monitor/models.py`
- Test: `tests/test_notify.py` (create)

- [ ] **Step 1: Write failing tests** for suppressing markup/304 events, grouping identical errors per run, and notifying each meaningful event once.
- [ ] **Step 2: Run** `pytest tests/test_notify.py -q` and verify expected failures.
- [ ] **Step 3: Implement** stable event fingerprints, per-run error grouping, and an explicit meaningful-event allowlist; keep failed deliveries pending for a later retry.
- [ ] **Step 4: Run** focused notification tests and then `pytest tests/test_telegram_bot.py -q`.
- [ ] **Step 5: Commit** `fix(notify): deduplicate operational noise`.

### Task 3: Обнаружение новых, удалённых и восстановленных документов

**Files:**
- Modify: `src/fstec_monitor/crawler.py`, `src/fstec_monitor/models.py`
- Test: `tests/test_crawler.py`

- [ ] **Step 1: Write failing tests** for first missing run, confirmed removal, no repeated removal event, and restoration without a false new-document event.
- [ ] **Step 2: Run** the focused crawler tests and verify the missing-document assertions fail.
- [ ] **Step 3: Implement** full-discovery reconciliation using `missing_runs`, a two-run confirmation threshold, and explicit restore state transitions.
- [ ] **Step 4: Run** crawler tests and verify existing scan-run accounting remains correct.
- [ ] **Step 5: Commit** `fix(crawler): track document removal and restoration`.

### Task 4: Ошибки, зависания и жизненный цикл фоновых задач

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`, `src/fstec_monitor/http.py`, `src/fstec_monitor/cli.py`
- Test: `tests/test_telegram_bot.py`, `tests/test_http.py` (create)

- [ ] **Step 1: Write failing tests** for a failed background scan being consumed and reported once, bounded retry without post-final sleep, and clean client closure on cancellation.
- [ ] **Step 2: Run** focused tests and verify failures are caused by current task/retry behavior.
- [ ] **Step 3: Implement** done-callback/result consumption, cancellation-aware polling, bounded backoff, and structured run logging with redacted URLs.
- [ ] **Step 4: Run** focused tests plus all existing tests.
- [ ] **Step 5: Commit** `fix(bot): make background scans and retries bounded`.

### Task 5: Интеграционная проверка и эксплуатационные документы

**Files:**
- Modify: `README.md`, `Makefile`, `.github/workflows/ci.yml` (create)
- Test: `tests/test_integration_flow.py` (create)

- [ ] **Step 1: Write an integration test** that runs a fake catalog through new, unchanged, changed, missing and restored states and asserts the resulting events/notifications.
- [ ] **Step 2: Run** the integration test and verify it fails until all state transitions are wired together.
- [ ] **Step 3: Add** one-command test/lint/compile CI and document the notification policy, removal grace period, log fields and recovery commands.
- [ ] **Step 4: Run** `pytest -q`, `python -m compileall -q src tests`, and `ruff check .` (or the project-local equivalent) and record exact results.
- [ ] **Step 5: Commit** `test: verify bot lifecycle and add CI checks`.
