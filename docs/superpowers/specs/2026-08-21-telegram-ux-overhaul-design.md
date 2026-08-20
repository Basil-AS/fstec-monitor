# Telegram UX Overhaul Design

## Goal

Turn the bot into a stateful Telegram UI that reuses one screen message per private chat, keeps the chat compact, and preserves persistent reports and Markdown documents.

## Chosen approach

Keep the existing hand-rolled Bot API polling transport and `FSTEC_TLS_VERIFY=false` deployment contract. Add a small Telegram presentation layer rather than migrating the whole runtime to aiogram. The presentation layer owns screen lifecycle; handlers translate updates into commands and call application services; services do scanning, settings, permissions and reports.

A `MessageLifecycleManager` stores one `ScreenSession` per chat. It supports `show_screen`, `edit_screen`, `replace_screen`, `show_progress`, `show_temporary`, `publish_persistent`, and `close_screen`. Screen edits are serialized per chat, skip identical payloads, and recover by sending a replacement when Telegram reports that the original message is missing. Temporary messages are tracked and deleted or replaced; persistent reports/files are never passed to cleanup.

A `NavigationStack` stores logical screen names and payloads (`main`, `settings`, `scan`, `filters`, `admin`, `changes`). Back pops the stack and edits the same screen message. Every callback is acknowledged before dispatch. Callback intent includes a stable operation key; scan start uses a single-flight guard and duplicate intents only refresh the current screen.

Progress is rendered by one `ScanProgressCard`. Updates are coalesced by a monotonic interval and only the latest state is sent. Completion replaces the progress card in place, while a requested report or Markdown file is delivered as persistent content. Error screens are recoverable: retry/back edits the same screen and clears the transient error.

## Message policy

- Persistent: report result, document result, generated `.md` and important final operation result. Never deleted by UI cleanup.
- Screen: main menu, settings, scan, filters and admin screens. Edit the current screen message.
- Temporary: acknowledgement, loading, retryable errors and progress. Coalesce, replace or delete.
- A missing/deleted UI message is recoverable by creating exactly one new screen message and updating the session pointer.

## Telegram capabilities

Use standard Bot API 10.x `answerCallbackQuery`, `editMessageText`, `editMessageReplyMarkup`, `deleteMessage`, inline keyboards and link-preview suppression. Rich/Markdown skills inform compact structured rendering and digest formatting; the current transport does not expose a verified `sendRichMessageDraft` endpoint, so native Rich/draft/streaming APIs are not added. Streaming is not applicable because the bot does not generate token streams. `aiogram-dialog` is evaluated but not introduced: the existing flows are finite button-driven screens, and a second state framework would increase compatibility risk. The logical navigation stack provides the relevant window/dialog behavior without changing polling or deployment.

## Reliability and safety

Message API errors distinguish `message is not modified`, missing messages and transient timeouts. Missing-message recovery does not reset scan state. Progress edits never create messages. Per-chat locks prevent edit/delete races. The lifecycle cleanup API rejects `.md` paths and has a regression test proving Markdown files remain byte-for-byte intact. No DNS, certificate, trust-store or database destructive changes are allowed.

## Verification

Add focused UX tests for screen reuse, five-step navigation, back/cancel, stale keyboard removal, progress throttling, retry/error replacement, persistent result retention, duplicate callback single-flight, callback acknowledgement, edit/delete races, `message is not modified`, deleted UI recovery, Telegram timeouts, and Markdown preservation. Run pytest, Ruff, compileall, wheel build, architecture/cycle/dead-code audits, then deploy only fstec-monitor.service on mxbox and inspect its fresh journal.
