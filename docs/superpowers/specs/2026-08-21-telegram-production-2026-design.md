# Telegram Bot Production 2026 Design

## Goal

Evolve `fstec-monitor` into a quiet, reliable, testable Telegram monitor while preserving document discovery, new/removed/restored document events, content/attachment comparisons, Markdown reports, scan progress, cancellation, schedules, admin access control, and per-user category filters.

## Constraints

- Work only on the bot; do not change DNS, certificates, or unrelated services.
- Preserve `FSTEC_TLS_VERIFY=false` behavior for the FSTEC fetch path.
- Keep polling on mxbox unless a measured operational reason requires a migration.
- Do not add Mini App, payments, or streaming without a concrete user-facing need.
- Preserve backward-compatible database data and use additive migration-safe changes.
- No secrets in source, callback data, logs, tests, or review artifacts.

## Architecture

Keep the existing lightweight HTTP Bot API transport initially, but split responsibilities behind small interfaces:

- `telegram/api.py`: Telegram request/response transport, timeouts, retry classification, callback acknowledgement, edit/delete/send primitives.
- `telegram/keyboards.py`: one canonical admin/user/settings/progress keyboard schema and callback identifiers.
- `telegram/rendering.py`: compact HTML/Markdown-safe screens, progress cards, error/status summaries, and report formatting.
- `telegram/handlers.py`: command and callback routing with role checks and idempotent state transitions.
- `services/scan_service.py`: single-flight scan orchestration, progress events, cancellation, retry semantics, and lifecycle cleanup.
- `services/notification_service.py`: recipient filtering, delivery idempotency, quiet error policy, and digest/report delivery.
- Existing crawler/parser/repository modules remain domain-oriented; handlers stop opening sessions or embedding business rules directly.

The current `TelegramBot` becomes a composition façade during migration so existing entry points and tests remain compatible. Each extraction is behavior-preserving and can be reverted independently.

## UX

Use a single compact reply keyboard for the main menu and inline keyboards for contextual actions. User controls expose only updates, personal filters, help, and navigation. Admin controls expose status, changes, scan controls, errors, users, global filters, and schedule/settings. Every screen has `⬅️ Назад` or `🏠 Главное меню` where applicable. Callback data is versioned, bounded, validated against the current user and state, and always acknowledged.

Long reports continue to use Markdown attachments and concise Telegram summaries. Rich-message/streaming capabilities are assessed but only enabled when Bot API support and deployment compatibility are verified; no AI-generated or long-running token stream exists in this domain, so streaming is intentionally not used. A digest renderer may provide one compact aggregated notification when the existing event set warrants it; individual spammy messages remain suppressed.

## Scan lifecycle

Model scan state as explicit transitions: `idle -> running -> completed|failed|cancelled`. A second start while running returns the existing progress card and never starts a second crawler. Stop is idempotent and waits for the worker to finish cleanup. Retry is available only after failed/cancelled scans and creates a fresh run. Progress updates are throttled, edit one saved message, and include stage, completed/total, percentage, errors, elapsed time, and controls. All background tasks have done callbacks and exception reporting without recursively notifying the bot about Telegram transport failures.

## Errors, delivery, and observability

Separate domain/fetch/parser/storage errors from Telegram transport errors. Log structured context (scan id, document URL hash, event id, attempt) with secrets redacted. User-visible errors are short, temporary, and deduplicated. Admin error views read persisted error events; transport failures are logged and rate-limited rather than sent back through the same failing channel. Delivery rows remain the source of truth for per-recipient idempotency.

## Testing and rollout

Use TDD for each behavior change: failing regression test, minimal implementation, green targeted test, then full suite/lint/compile. Add tests for keyboard uniqueness/callback validation, permissions, scan single-flight/retry/cancel, progress edit throttling, notification dedup/filtering, document add/remove/restore, parser/diff/Markdown output, retry/timeout classification, and deployment invariants. Run Gortex impact before every mutation and detect/tests/guards/contract after it. Deploy only explicit bot files to mxbox, restart the bot service, inspect recent logs and smoke-test status/scan controls; DNS and certificate state are not changed.

## Out of scope

No Mini App, payments, inline mode, webhook migration, or rich-message streaming is planned unless implementation evidence shows the current bot cannot satisfy a concrete flow with standard Bot API messages and keyboards.
