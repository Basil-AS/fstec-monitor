# Telegram UX toolkit design

## Goal

Make fstec-monitor behave like a small stateful Telegram application: one
reusable navigation screen per chat, safe tail-aware edits, compact callbacks,
and one evolving progress card for long operations.

## Reference patterns

DubnaCams_bot separates message tracking from rendering. It remembers the
latest chat message and menu, edits only a current tail message, treats media
as persistent context, cleans callback triggers defensively, and settles all
admin notification copies. fstec-monitor will reuse those semantics in Python
without importing camera or recording domain logic.

## Boundaries

The existing `MessageLifecycleManager` remains the sole stateful message
lifecycle owner. `telegram/ux/messages.py` delegates to it and adds typed
references, stale/media policy, and multi-copy settlement. Rendering and
keyboard builders remain pure. Scanning, storage, scheduling, and permissions
stay outside the UX package.

## Components

- `models.py`: immutable view/message/pagination models and message kinds.
- `callbacks.py`: bounded `namespace:action:argument...` codec with strict
  validation and replay/stale generation metadata.
- `messages.py`: `MessageLedger` façade over the existing lifecycle manager,
  plus `NotificationSettlement` for fan-out decisions.
- `keyboards.py`: pure navigation, pagination, and result action builders.
- `progress.py`: operation-key guarded progress card façade over the existing
  lifecycle/coalescer.
- `navigation.py`: typed payload/context wrapper over `NavigationStack`.
- `errors.py`: safe classification of Telegram edit failures and recovery
  policy; no business logic.

## Compatibility

Existing `telegram.callbacks`, keyboards, navigation, and handlers remain
compatible. New APIs are deliberately small adapters so migration can happen
incrementally. Legacy callback decoding remains available for old buttons;
new toolkit callbacks use the namespaced codec.

## Safety invariants

1. Persistent messages and `.md` files are never removed by UI cleanup.
2. Media callback messages receive markup cleanup only; they are not edited as
   text and are not deleted.
3. A stale/non-tail UI is never edited above the chat tail.
4. Cleanup failures do not fail the business operation.
5. Progress operations are single-flight per `(chat_id, operation_key)`.
6. Settling an admin notification is idempotent and removes action buttons from
   every known copy.

## Verification

Tests exercise fallback behavior, media and stale callbacks, pagination,
single-flight progress, notification settlement, and markdown preservation.
The existing Telegram suite remains a compatibility gate.
