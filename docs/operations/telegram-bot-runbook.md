# Telegram bot runbook (mxbox)

The production service is `fstec-monitor.service` under `/srv/code/bots/fstec-monitor`.
This runbook is intentionally limited to the bot service. Do not modify DNS, system trust stores, reverse proxies, or certificates during a bot rollout.

## Safe rollout

1. Build and test the branch locally:

   ```bash
   uv sync --extra dev
   uv run pytest -q
   uv run ruff check .
   uv run python -m compileall -q src tests
   uv build --wheel
   ```

2. Copy only explicitly reviewed bot files to the service checkout. Never use `rsync --delete`; preserve the remote virtualenv, database, object store, and environment file.

3. Confirm the existing environment still contains the intended bot settings, including `FSTEC_TLS_VERIFY=false`. Do not print the token or dump the complete environment.

4. Restart and inspect only the bot unit:

   ```bash
   sudo systemctl restart fstec-monitor.service
   sudo systemctl is-active fstec-monitor.service
   sudo journalctl -u fstec-monitor.service -n 100 --no-pager
   ```

## Smoke checks

- `/start` shows one role-appropriate menu.
- User menu cannot start scans, view admin errors, or change global settings.
- Admin can open status, start one scan, see one edited progress card, stop it, and retry after failure/cancellation.
- A second start while running returns the current progress instead of starting another task.
- New, removed, restored, content-changed, and attachment-changed documents produce the expected digest once.
- A personal ignored category suppresses only that user’s delivery.
- FSTEC fetch failures become bounded journal entries and a short rate-limited admin notice; tokens and exception details never appear in Telegram.

## Rollback

Stop the unit, restore the last reviewed bot files from the deployment artifact, restart the unit, and inspect the journal. Do not delete the database or object store as a rollback technique.
