# FSTEC Monitor: production management release

## Goal

Give the administrator a compact Telegram control surface for document statistics,
change status, recent events, reports and stored old/new versions, while keeping
all downloaded versions until a strict 5 GiB application-data quota is reached.

## Design

- Keep the existing SQLite/object-store model and add quota enforcement before any
  new object is written. Existing objects are content-addressed and never duplicated.
- Expose `/status`, `/changes`, `/report <id>`, `/errors`, `/scan` and `/help`.
  Reports contain a short summary, detailed unified diff, hashes and server-side
  object keys; Telegram receives the report plus old/new files when their size is
  within the configured Bot API limit.
- Notify the administrator about new documents, meaningful content/attachment
  changes, scan failures and storage-quota failures. Mark presentation-only HTML
  changes as recorded but non-notifying noise.
- Keep polling responsive by running scans in a background task. Convert command,
  Telegram API, network and scan exceptions into logged diagnostics and a concise
  administrator message.
- Publish the implementation on an agent branch through a pull request into a
  newly-created `main`, merge it, create a release, then deploy exactly that main
  commit and verify the systemd service and Telegram API.

## Verification

Run unit tests, bytecode compilation and Ruff. On the host verify the active and
enabled service, database counts, zero unhandled pending events after a scan, the
5 GiB configuration, and a Telegram `/status` response to the administrator.
