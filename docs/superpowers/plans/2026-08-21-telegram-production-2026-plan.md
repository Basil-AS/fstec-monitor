# Telegram Production 2026 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the FSTEC Telegram monitor quiet, idempotent, observable, testable, and easier to evolve while preserving current document monitoring and reporting behavior.

**Architecture:** Incrementally extract transport, UI, scan orchestration, and notification responsibilities from `telegram_bot.py` behind small typed modules. Keep the current polling entry point and façade during migration; only change the internal boundaries and behavior after regression tests prove compatibility.

**Tech Stack:** Python 3.11+, asyncio, httpx, SQLAlchemy 2, Pydantic Settings, pytest, Ruff, Telegram Bot API via current HTTP adapter, SQLite/PostgreSQL-compatible persistence.

## Global Constraints

- Work only in `fstec-monitor`; never touch DNS or certificates.
- Preserve the FSTEC TLS bypass setting and polling deployment on mxbox.
- Do not stage or overwrite the user-owned `uv.lock` change in the original checkout.
- No secrets in source, callback data, logs, tests, commits, or PR text.
- Use Gortex impact before mutations and detect/tests/guards/contract after each mutation.
- Every behavior change follows TDD: failing regression test, minimal fix, green targeted test, then refactor.
- No Mini App, payments, or streaming without a demonstrated requirement; standard messages remain the fallback.

---

### Task 1: Establish reliability contracts and canonical Telegram UI

**Files:**
- Create: `src/fstec_monitor/telegram/callbacks.py`
- Create: `src/fstec_monitor/telegram/keyboards.py`
- Create: `src/fstec_monitor/telegram/rendering.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_telegram_ui.py`
- Test: `tests/test_telegram_bot.py`

**Interfaces:**
- `callbacks.py` provides `encode_callback(action: str, value: str = "") -> str` and `decode_callback(data: str) -> tuple[str, str] | None`; invalid versions/unknown actions return `None`.
- `keyboards.py` provides `admin_keyboard()`, `user_keyboard()`, `settings_keyboard(...)`, and `scan_keyboard(...)`, each returning one canonical Telegram reply/inline markup with unique labels and callback data.
- `rendering.py` provides `progress_bar(percent: int, width: int = 10) -> str`, `render_scan_progress(progress) -> str`, and `render_error_notice(context, error_id) -> str`.

- [ ] Write tests proving all visible labels are unique, user markup excludes admin controls, callbacks round-trip and reject malformed/oversized data, progress renders `0%`/`100%`, and error notices contain no exception secrets.
- [ ] Run `uv run pytest tests/test_telegram_ui.py -q`; confirm it fails because the modules/contracts do not exist.
- [ ] Implement the smallest pure helpers and route existing keyboard builders through them.
- [ ] Run targeted tests, then existing Telegram tests.
- [ ] Refactor duplicate labels and callback literals only after tests are green.
- [ ] Run Gortex `detect`, `get_test_targets`, `check_guards`, and `contracts` for changed symbols.
- [ ] Commit `feat(telegram): add canonical safe UI contracts`.

### Task 2: Make scan lifecycle single-flight and durable at the service boundary

**Files:**
- Create: `src/fstec_monitor/services/__init__.py`
- Create: `src/fstec_monitor/services/scan_service.py`
- Modify: `src/fstec_monitor/crawler.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Test: `tests/test_scan_service.py`
- Test: `tests/test_crawler.py`

**Interfaces:**
- `ScanState` is an immutable snapshot with `state`, `stage`, `completed`, `total`, `errors`, `started_at`, `finished_at`, `last_error`, and `run_id`.
- `ScanService.start(trigger: str = "manual") -> StartResult` returns `started`, `already_running`, or `rejected` with the current snapshot.
- `ScanService.stop() -> bool` is idempotent; `ScanService.retry() -> StartResult` only starts after terminal failure/cancellation.
- `ScanService.subscribe(callback) -> Callable[[], None]` receives throttled snapshots and never leaks background tasks.

- [ ] Write failing tests for duplicate starts, idempotent stop, retry-after-failure, cancellation cleanup, task exception observation, and monotonic progress.
- [ ] Run the targeted tests and verify expected failures.
- [ ] Extract the current in-memory task/event logic into `ScanService` while preserving `run_monitor` callback behavior.
- [ ] Add explicit scan run identifiers to logs and user-visible cards; keep existing `ScanRun` persistence.
- [ ] Route `TelegramBot.start_scan`, `stop_scan`, and progress refresh through the service façade.
- [ ] Run targeted crawler/service tests and full tests.
- [ ] Run Gortex impact/detect/tests/guards/contract and commit `feat(scan): centralize single-flight lifecycle`.

### Task 3: Separate Telegram transport, handlers, and notification delivery

**Files:**
- Create: `src/fstec_monitor/telegram/api.py`
- Create: `src/fstec_monitor/telegram/handlers.py`
- Create: `src/fstec_monitor/services/notification_service.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Modify: `src/fstec_monitor/notify.py`
- Test: `tests/test_telegram_api.py`
- Test: `tests/test_notification_service.py`
- Test: `tests/test_telegram_bot.py`

**Interfaces:**
- `TelegramApi.call(method, payload, *, timeout_class="normal")` classifies definite 4xx errors separately from unknown 429/5xx/transport outcomes and never logs token-bearing URLs.
- `NotificationService.deliver_pending(session_factory, send, *, now=None) -> DeliverySummary` applies access, enabled, per-user ignore filters, and `EventDelivery` idempotency before sending.
- `handlers.py` exposes role-aware command/callback dispatchers that always acknowledge callbacks and use edit-in-place for contextual screens.

- [ ] Add failing tests for Telegram timeout classification, retry policy, callback acknowledgement, no recursive transport alerts, and duplicate delivery suppression.
- [ ] Implement transport and notification boundaries with dependency injection; retain the façade for compatibility.
- [ ] Move command/callback branching out of the façade in small slices, keeping exact existing commands as aliases.
- [ ] Replace exception-text user messages with stable short notices plus admin event IDs.
- [ ] Run targeted and full tests, then Gortex post-change checks.
- [ ] Commit `refactor(bot): isolate transport handlers and delivery policy`.

### Task 4: Improve parsing/document lifecycle and reports without changing semantics

**Files:**
- Modify: `src/fstec_monitor/crawler.py`
- Modify: `src/fstec_monitor/parser.py`
- Modify: `src/fstec_monitor/normalize.py`
- Modify: `src/fstec_monitor/reports.py`
- Test: `tests/test_crawler.py`
- Test: `tests/test_parser.py`
- Test: `tests/test_reports.py`

- [ ] Add failing regressions for new document, removal, restoration, markup-only change, attachment replacement, malformed HTML/PDF, and Markdown size splitting.
- [ ] Run the focused tests to verify red state.
- [ ] Make lifecycle reconciliation explicit and idempotent; ensure latest snapshots are retained and removed documents do not repeat events.
- [ ] Make report rendering bounded, deterministic, and safe for Telegram Markdown/HTML entity limits.
- [ ] Run parser/crawler/report tests and full suite.
- [ ] Run Gortex impact/detect/tests/guards/contract and commit `fix(crawler): harden document lifecycle and reports`.

### Task 5: Admin/user settings, permissions, and error observability

**Files:**
- Modify: `src/fstec_monitor/models.py`
- Modify: `src/fstec_monitor/db.py`
- Modify: `src/fstec_monitor/config.py`
- Modify: `src/fstec_monitor/telegram_bot.py`
- Create: `tests/test_settings_permissions.py`
- Modify: `tests/test_access.py`

- [ ] Add failing tests for admin-only scan/error/user/settings actions, user-only updates/ignore actions, default settings, and safe error redaction.
- [ ] Add only backward-compatible columns/indexes if required; keep `create_all`/deployment compatibility and document migration implications.
- [ ] Implement consistent role checks at handler boundaries and personal notification/category settings without exposing global admin state.
- [ ] Add bounded structured logging fields and rate-limited admin error summaries.
- [ ] Run all tests and Gortex checks; commit `feat(bot): harden roles settings and observability`.

### Task 6: CI, deployment smoke checks, and documentation

**Files:**
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `README.md`
- Create: `docs/operations/telegram-bot-runbook.md`
- Test: `tests/test_deployment_config.py`

- [ ] Add failing tests for required dev setup, no-secret configuration, polling mode, TLS bypass preservation, and explicit mxbox service commands.
- [ ] Make CI run dependency sync, tests, Ruff, compileall, and package validation with caching/concurrency appropriate to this small Python project.
- [ ] Document start/status/log/rollback commands for mxbox without DNS/certificate operations and with explicit no-delete deployment rules.
- [ ] Run full local verification and Gortex quality/architecture/PR review.
- [ ] Commit `ci: harden bot verification and operations docs`.

### Task 7: Review, PR, CI, deployment, and cleanup

**Files:**
- No new production files; review all changed files and generated docs.

- [ ] Run `uv run pytest -q`, `uv run ruff check .`, `uv run python -m compileall -q src tests`, package build, diff-check, secret scan, and startup smoke test.
- [ ] Run Gortex `detect_changes`, `review_pack`, `pr_risk`, `suggest_reviewers`, architecture/quality audit, tests, guards, and contract checks.
- [ ] Request independent code review; apply all valid critical/important findings and record decisions for disputed findings.
- [ ] Push the feature branch, create a focused PR, inspect every CI job, and fix failures to green.
- [ ] Use GitHub review-comment tooling and receiving-code-review workflow for all actionable comments.
- [ ] Deploy only explicit bot files to mxbox, restart `fstec-monitor.service`, inspect logs, and smoke-test `/start`, menu navigation, scan progress, duplicate scan, stop, retry, changes, and personal ignore.
- [ ] Merge only after green CI and review, then use finishing-a-development-branch to clean the worktree/branch safely; never alter the original dirty checkout.
