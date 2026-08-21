# Telegram UX toolkit implementation plan

1. Add typed UX models and strict callback codec with focused failing tests.
2. Add `MessageLedger` and settlement adapters over the existing lifecycle;
   cover edit/send/delete fallback and media/tail policy.
3. Add pure keyboard and navigation adapters; cover pagination and payload
   preservation.
4. Add guarded `ProgressMessage` façade and error classifier; cover one
   message lifecycle, throttling, cancellation, and concurrent starts.
5. Integrate compatibility exports into the existing Telegram package without
   changing polling, permissions, data storage, or FSTEC TLS behavior.
6. Run targeted and full verification, inspect diff, perform Gortex quality and
   architecture review, then push a PR and resolve CI/review findings.
7. Deploy only changed bot files to mxbox, restart only `fstec-monitor.service`,
   and verify import path, hashes, journal, and TGUX readiness.
