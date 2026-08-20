# Telegram UX Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace message-spamming Telegram flows with a stateful, reusable-screen UI while preserving polling, scan behavior, permissions, data and Markdown documents.

**Architecture:** Keep `TelegramBot` as a compatibility façade and polling owner. Move screen lifecycle and navigation into focused Telegram modules. Handlers dispatch typed intents to existing services; the lifecycle manager serializes per-chat screen edits and separates screen, temporary and persistent messages.

**Tech Stack:** Python 3.11+, hand-rolled Telegram Bot API polling, asyncio, httpx, SQLAlchemy, pytest, Ruff.

## Global Constraints

- Preserve polling and `FSTEC_TLS_VERIFY=false`.
- Do not change DNS, certificates or trust stores.
- PR #11 is baseline; do not rewrite its unrelated reliability changes.
- Never delete, empty, truncate or overwrite `*.md` during cleanup or lifecycle handling.
- No new message for a screen transition when the current screen message can be edited.
- A callback must be answered before long-running work is dispatched.
- Persistent reports and Markdown files are never managed by temporary/UI cleanup.
- Every production change starts with a failing focused test and ends with fresh verification.

---

### Task 1: Screen lifecycle primitives

**Files:**
- Create: `src/fstec_monitor/telegram/lifecycle.py`
- Create: `tests/test_telegram_lifecycle.py`

**Interfaces:**
- `ScreenSession(chat_id: int, message_id: int | None = None, screen: str = "main")`
- `MessageLifecycleManager.show_screen(chat_id, screen, text, markup=None) -> int | None`
- `MessageLifecycleManager.show_progress(chat_id, text, markup=None) -> int | None`
- `MessageLifecycleManager.publish_persistent(chat_id, text, markup=None) -> int | None`
- `MessageLifecycleManager.show_temporary(chat_id, text, ttl=8.0) -> int | None`
- `MessageLifecycleManager.close_screen(chat_id) -> None`
- injected transport protocol with `send`, `edit_message`, `delete_message`

- [ ] Write failing tests proving the second `show_screen` edits the first message, identical payloads do not edit, missing-message errors recreate one screen, and `publish_persistent` is not deleted by cleanup.
- [ ] Run `uv run pytest tests/test_telegram_lifecycle.py -q`; expect failures because lifecycle module is absent.
- [ ] Implement per-chat sessions and locks, edit-first behavior, replacement recovery and temporary task tracking.
- [ ] Add `is_not_modified`/`is_missing_message` classifiers and ensure timeout errors leave the session pointer intact.
- [ ] Run the focused tests green.
- [ ] Commit `feat(telegram): add reusable message lifecycle manager`.

### Task 2: Markdown preservation guard

**Files:**
- Modify: `src/fstec_monitor/telegram/lifecycle.py`
- Create: `tests/test_markdown_preservation.py`

- [ ] Write a failing regression test that creates a `.md`, runs lifecycle cleanup/temporary cleanup and asserts identical bytes remain.
- [ ] Run the test and verify the failure is caused by the missing explicit Markdown guard.
- [ ] Implement cleanup path validation that excludes `.md` objects and never opens them for truncation/overwrite.
- [ ] Run focused preservation tests green.
- [ ] Commit `fix(telegram): protect markdown documents from lifecycle cleanup`.

### Task 3: Navigation stack and canonical screens

**Files:**
- Create: `src/fstec_monitor/telegram/navigation.py`
- Modify: `src/fstec_monitor/telegram/keyboards.py`
- Modify: `src/fstec_monitor/telegram/rendering.py`
- Create: `tests/test_telegram_navigation.py`

**Interfaces:**
- `NavigationStack.push(screen, payload=None)`, `.back()`, `.reset()`, `.current`
- `Screen(name, text, markup)` immutable value object
- `main_screen(is_admin)`, `settings_screen(...)`, `scan_screen(...)`, `filters_screen(...)`
- callbacks remain backward-compatible (`menu:main`, `settings:*`, `scan:*`, `ignore:*`, `userignore:*`)

- [ ] Write failing tests for five screen transitions using one message id, back restoring the previous logical screen, and every non-main screen including main/back controls.
- [ ] Run focused tests red.
- [ ] Implement stack and screen renderers with compact structured Markdown/HTML-safe text and consistent emoji button labels.
- [ ] Remove duplicate keyboard definitions by making the canonical keyboard module the only source for screen controls; retain command aliases for compatibility.
- [ ] Run focused tests green and commit `feat(telegram): add stateful screen navigation`.

### Task 4: Callback acknowledgment and single-flight scan UX

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/telegram/lifecycle.py`
- Modify: `src/fstec_monitor/services/scan_service.py`
- Create: `tests/test_telegram_callback_ux.py`

- [ ] Write failing tests for callback acknowledgement before dispatch, duplicate `scan:run:confirm` not creating a second task, retry editing the existing scan screen, and stop/cancel preserving one screen.
- [ ] Run focused tests red.
- [ ] Route callback responses through lifecycle manager; answer expired callbacks safely; use stable intent keys and current screen message id.
- [ ] Keep scan orchestration outside screen rendering and make progress state single-flight.
- [ ] Run focused tests green and commit `feat(telegram): make scan callbacks single-flight and smooth`.

### Task 5: Coalesced progress and transient error lifecycle

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/crawler.py`
- Modify: `src/fstec_monitor/telegram/rendering.py`
- Create: `tests/test_telegram_progress_ux.py`

- [ ] Write failing tests proving 20 progress callbacks produce at most one edit within the throttle window, the latest progress is eventually rendered, `message is not modified` is harmless, and retry replaces a temporary error.
- [ ] Run focused tests red.
- [ ] Implement monotonic coalescing with one pending update task and one progress card (`Подготовка → загрузка → обработка → сравнение → готово`).
- [ ] Ensure completion edits/replaces the progress screen and only persistent report/file delivery creates durable messages.
- [ ] Run focused tests green and commit `feat(telegram): coalesce progress and recover transient errors`.

### Task 6: Thin handler façade and message policy integration

**Files:**
- Create: `src/fstec_monitor/telegram/handlers.py`
- Create: `src/fstec_monitor/telegram/transport.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Create: `tests/test_telegram_message_policy.py`

- [ ] Write failing tests that exercise `/start`, settings, scan, back and report flows through the façade and assert screen messages are edited/reused while reports remain persistent.
- [ ] Run focused tests red.
- [ ] Extract Bot API call/retry/error classification into transport and update `TelegramBot` to compose lifecycle + navigation + existing services.
- [ ] Move callback/command routing into thin handlers; leave compatibility methods delegating to them so existing tests and deployment entrypoint continue to work.
- [ ] Run focused tests green and commit `refactor(telegram): compose handlers around screen lifecycle`.

### Task 7: Full regression and architecture cleanup

**Files:**
- Modify: affected `src/fstec_monitor/telegram*.py`, `src/fstec_monitor/telegram/*.py`
- Modify: affected `tests/test_telegram_bot.py`, `tests/test_telegram_ui.py`
- Modify: `docs/operations/runbook.md` if lifecycle operations need documentation

- [ ] Run all UX tests and current regression suite; fix failures without removing coverage.
- [ ] Run Ruff and compileall; fix diagnostics.
- [ ] Run Gortex `detect_changes`, impact, test targets, guards, contract checks and architecture/quality audit.
- [ ] Run cycle/dead-code/security inspections; remove only proven dead duplicate code and never Markdown artifacts.
- [ ] Commit `refactor(telegram): complete stateful UX integration`.

### Task 8: Verification, PR, CI, production smoke and merge

**Files:**
- No source changes unless verification finds a defect.

- [ ] Run fresh `uv run pytest -q`, `uv run ruff check .`, `uv run python -m compileall -q src tests`, and `uv build --wheel`.
- [ ] Run independent review pass plus Gortex PR review; if linked-worktree cross-workspace blocks Gortex review, record the limitation and use graph audits, diff review and test evidence instead.
- [ ] Inspect `git diff`, secrets, and status; push branch and open PR against `main`.
- [ ] Wait for GitHub CI, inspect every check and fix root causes.
- [ ] Address review comments with technical verification; rerun all checks.
- [ ] Deploy only this bot to `mxbox`/`fstec-monitor.service`, preserving environment and remote data; verify active state, restart count, exit status and fresh journal.
- [ ] Merge PR only after green CI and review; preserve external worktree per host ownership.
