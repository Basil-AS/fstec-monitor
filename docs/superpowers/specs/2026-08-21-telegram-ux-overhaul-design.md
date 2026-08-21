# Telegram UX Overhaul Design

## Goal

Make fstec-monitor feel like a stateful Telegram application: one reusable navigation
screen per chat, immediate callback feedback, tail-aware recovery, persistent reports
that survive navigation, and one morphing scan card instead of a stream of status
messages.

## Behavioral reference

DubnaCams_bot's useful semantics are transferred without copying its JavaScript:

- remember the latest known chat message and the current menu/context message;
- edit only a text menu that is still the chat tail;
- treat media/document messages as persistent content, never as disposable menus;
- close stale navigation UI before creating one fresh screen at the tail;
- use callback answers for short confirmations and errors;
- track all messages created by the bot so cleanup is scoped and auditable.

## Architecture

`MessageLifecycleManager` remains the only lifecycle owner. `ScreenSession` gains
tail metadata, navigation context, temporary IDs, persistent IDs, and a generation
counter. `show_screen` uses an edit-first decision tree: same current tail screen is
edited; a missing/deleted message is recreated; a stale or media callback never edits
history and instead cleans only disposable UI before sending one new screen. All
cleanup is media-safe and has an explicit Markdown/document preservation guard.

The Telegram transport remains raw Bot API/httpx. It gains one request wrapper that
emits opt-in `TGUX` metadata (method, chat, message id, screen, reason; never text or
secrets), escapes dynamic HTML values, handles `message is not modified` as a noop,
and exposes `send_chat_action`. Rich-message and streaming skills are evaluated but
not introduced for ordinary monitor screens: the bot has no token stream, and the
native edit lifecycle is the lower-latency compatible choice. Markdown reports remain
persistent document artifacts.

Navigation frames store screen name plus payload/context and parent. Back pops to the
exact originating frame, including pagination and event/report context. Main menu
resets only navigation UI and never deletes persistent results.

Handlers become orchestration only: answer callback first, mutate domain state, then
render the next screen or progress card. Settings/category toggles use toast + edit,
not temporary messages. Scan start/stop/retry is idempotent and renders preparation,
progress, and final state in one screen message. Long report generation advertises
`upload_document` via `sendChatAction`.

## UX rules

1. `/start` creates one UI message; menu transitions edit it whenever it is the current
   tail. A stale UI creates one replacement at the bottom.
2. UI messages are disposable; reports, documents, media, and Markdown files are not.
3. Every callback receives `answerCallbackQuery` before slow work.
4. Progress has an immediate first render and bounded coalesced edits with stage,
   counts, percentage, elapsed time, errors, and findings.
5. Pagination edits the same screen and uses compact previous/current/next controls.
6. Action buttons are replaced by the resulting state so stale actions cannot repeat.

## Testing

Transport/lifecycle fakes record exact Bot API calls. Regression tests cover tail and
media decisions, one-message five-transition navigation, callback toast ordering,
scan morphing, progress throttling, deleted-message recovery, no-op edits, persistent
Markdown safety, pagination, access decision cleanup, and upload-document chat action.
Existing crawler, notification, permissions, and data compatibility tests remain
green.

## Explicit non-goals

No aiogram migration, Mini App, payments, native rich drafts, or LLM streaming: none
is required by fstec-monitor's monitor flows. No DNS, certificate, trust-store, or
FSTEC TLS policy changes are part of this work.
