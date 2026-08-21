# Telegram UX Overhaul Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transfer DubnaCams-style tail-aware, edit-first Telegram UX to fstec-monitor while preserving reports, documents, data, polling, and deployment configuration.

**Architecture:** Extend the existing `MessageLifecycleManager` into the sole owner of per-chat tail/context/persistent/temporary message state. Keep raw Bot API/httpx, add a safe renderer, TGUX metadata instrumentation, chat actions, and thin handler orchestration. Store navigation payloads in stack frames and render scan/settings/filter flows in place.

**Tech Stack:** Python 3, asyncio, httpx, raw Telegram Bot API, SQLAlchemy, pytest, Ruff.

## Global Constraints

- Never delete existing or generated `.md` files or persistent Telegram media/document/report messages.
- Do not add a second lifecycle manager or migrate to aiogram.
- Preserve polling, `FSTEC_TLS_VERIFY=false`, existing config/data schema, and deployment paths.
- Do not touch DNS, certificates, trust stores, or unrelated services.
- TGUX logs contain metadata only: method, chat id, message id, screen, reason; no token, secrets, full text, or document content.

### Task 1: Tail-aware lifecycle and navigation state

**Files:**
- Modify: `src/fstec_monitor/telegram/lifecycle.py`
- Modify: `src/fstec_monitor/telegram/navigation.py`
- Test: `tests/test_telegram_lifecycle.py`, `tests/test_telegram_navigation.py`

- [ ] Add failing tests for `remember_message`, `remember_context_message`, tail checks, media classification, stale-screen replacement, deleted-screen recreation, persistent ID protection, and context payload Back behavior.
- [ ] Run the focused tests and verify they fail for the missing state/behavior.
- [ ] Extend `ScreenSession` with `last_message_id`, `current_screen`, `navigation_context`, `temporary_message_ids`, `persistent_message_ids`, and a generation token; add lifecycle methods corresponding to DubnaCams semantics.
- [ ] Make `show_screen` edit only an eligible current text tail; on stale/media/missing messages clean disposable UI safely and send exactly one new screen.
- [ ] Make cleanup idempotent and treat `message is not modified` as success; never delete media, documents, persistent IDs, or `.md` paths.
- [ ] Replace string-only navigation entries with immutable frames containing screen and payload/context; make `back()` restore the exact parent frame.
- [ ] Run focused lifecycle/navigation tests and commit `feat(telegram): make lifecycle tail-aware`.

### Task 2: Safe Telegram transport, renderer, instrumentation, and chat actions

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Create/modify: `src/fstec_monitor/telegram/rendering.py`
- Test: `tests/test_telegram_transport.py`, `tests/test_telegram_rendering.py`

- [ ] Add failing tests for HTML escaping, method metadata, `message is not modified`, missing-message errors, `send_chat_action`, and no sensitive text in TGUX records.
- [ ] Implement one transport wrapper with parse mode `HTML`, escaped dynamic values, reason/screen metadata, and configurable `FSTEC_TGUX_LOGGING` default-safe behavior.
- [ ] Add `send_chat_action(chat_id, action)` and use `upload_document` before report uploads; keep scan progress in the progress card.
- [ ] Normalize API response message IDs and error classification without exposing token URLs.
- [ ] Run focused tests, Ruff on changed files, and commit `feat(telegram): add safe transport instrumentation`.

### Task 3: Edit-first menus, toasts, pagination, and stale action replacement

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/telegram/keyboards.py`
- Modify: `src/fstec_monitor/telegram/callbacks.py`
- Test: `tests/test_telegram_bot.py`, `tests/test_telegram_keyboards.py`

- [ ] Add failing acceptance tests for `/start` plus five transitions, settings/category toggle toast + edit with zero sends, pagination by edit, callback-first ordering, and stale action replacement.
- [ ] Route all screen rendering through the lifecycle manager; remove temporary success/status sends where a callback toast or screen edit is sufficient.
- [ ] Add compact pagination controls and payload-preserving back/main navigation for changes, errors, users, and filters.
- [ ] Ensure callback actions answer before database/API work and repeated scan actions are idempotent.
- [ ] Run focused UX tests and commit `feat(telegram): make navigation edit-first`.

### Task 4: Morphing scan and persistent report/media behavior

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/telegram/lifecycle.py`
- Modify: `src/fstec_monitor/notify.py` only if transport integration requires it
- Test: `tests/test_telegram_bot.py`, `tests/test_notify.py`, `tests/test_markdown_preservation.py`

- [ ] Add failing tests for one scan message from preparation through progress/final, immediate first render, bounded edits, stop/retry race handling, and final action buttons.
- [ ] Render compact stage/count/percentage/bar/elapsed/error/finding data; flush final state once and close progress coalescers.
- [ ] Send persistent reports/documents separately, mark their IDs persistent, and remove only stale reply markup when necessary.
- [ ] Add explicit media callback tests proving `deleteMessage=0` and UI recovery below media.
- [ ] Run scan/notify/Markdown tests and commit `feat(telegram): morph scan and protect persistent results`.

### Task 5: Access decisions and full regression suite

**Files:**
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/telegram/lifecycle.py`
- Test: `tests/test_permissions.py`, `tests/test_telegram_bot.py`, new access lifecycle tests

- [ ] Add failing tests for approve/deny idempotency, notification copies closed, verdict toast, user notification, and stale buttons removed.
- [ ] Implement lifecycle-aware decision updates without duplicate admin messages or repeatable action buttons.
- [ ] Run full pytest, Ruff, compileall, wheel build, and inspect changed diff.
- [ ] Run Gortex detect/impact/tests/guards/contract and quality/architecture/cycle/dead-code review; fix actionable findings.
- [ ] Commit `test(telegram): cover production UX lifecycle`.

### Task 6: Review, PR, deployment, and verification

**Files:**
- Modify only files justified by review findings; never delete `.md`.

- [ ] Run independent code review plus Gortex PR review; address every actionable finding.
- [ ] Push branch and open a focused PR describing before/after API behavior and test evidence.
- [ ] Wait for CI, inspect failures, fix root causes, and repeat until green.
- [ ] File-only deploy exact changed paths to mxbox without `--delete`; preserve DNS/cert/trust/TLS settings; restart only `fstec-monitor.service`.
- [ ] Verify production SHA, import path, service health, restart count, journal errors, and TGUX instrumentation readiness.
- [ ] Run fresh verification commands and finish the branch according to repository policy.
