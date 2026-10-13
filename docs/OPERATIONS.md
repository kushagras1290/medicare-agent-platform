# Operations

## Health and telemetry

- `GET /health/live`: process liveness.
- `GET /health/ready`: database readiness and active reviewer-auth mode.
- `GET /metrics`: Prometheus text, protected by a service bearer token.
- JSON request logs and `X-Request-ID` support request tracing without record payloads.

Metrics cover request rate/latency, cases by status, pending-review count and age, provider failures, dead letters, leased outbox events, audit failures and worker heartbeat age. `ops/alerts.yml` turns those into safety and reliability alerts. The local Alertmanager receiver only keeps alerts visible; production must use an owned on-call route.

## Backup and recovery

SQLite is local-only. `scripts/sqlite_backup.py` uses the online backup API, runs `PRAGMA integrity_check`, records table counts and reports a SHA-256 digest.

For PostgreSQL, `scripts/postgres_backup_restore_drill.ps1` creates a custom-format logical backup, restores it into the isolated `medicare_restore_drill` database, compares customer and consent-evidence counts, reports the digest and removes the temporary restore database. Production also needs provider-managed encrypted point-in-time recovery.

## Retention

`medicare retention-sweep` previews closed cases (`dispatched`, `rejected`, `closed`, and `provider_failed` with no attempts left) older than `RETENTION_DAYS`. `--execute` is required to delete a bounded batch. The sweep removes reviews, provider metadata, outbox records and the corresponding case audit chain and seal before deleting the case, then records the deleted case IDs in a `retention_sweep` audit event. Active, held, escalated, pending and still-retryable cases are not eligible. Customer follow-up markers are kept, so a purged case never re-triggers automated contact.

The command covers this synthetic schema. A real deployment would need an approved record schedule, legal holds and subject-rights workflow before real data could be enabled; this build does not enable it.

## Rollout

1. Validate the pinned release manifest.
2. Run migrations as a separate job.
3. Start gateway and API with workers stopped.
4. Check OIDC, readiness, metrics and a synthetic review.
5. Start one automation worker and one dispatch worker.

The caller service runs in deterministic `mock` mode by default. An owned external voice service uses `CALLER_MODE=http`, an HTTPS `CALLER_PROVIDER_URL` and a file-mounted provider key. Keep the internal API-to-caller key separate from the external provider key. Caller `5xx`, timeouts and malformed responses surface as `502`; the transactional outbox schedules a bounded retry and moves exhausted events to `dead_letter`.
6. Confirm heartbeats and alert delivery before scaling.

Rollback application images independently of the database. Prefer a forward-fix migration once data has been written. Stop workers before investigating an unsafe rule, broken audit chain or dispatch invariant.

## Incident actions

- **Gateway outage:** failed runs are retried automatically with backoff up to `PROVIDER_MAX_ATTEMPTS`; stop new automation if failures persist. No fallback proposal is allowed. `medicare retry-case <id>` retries one case manually.
- **Stuck/dead-letter event:** leased events are reclaimed automatically after `OUTBOX_LEASE_SECONDS`. For dead letters, stop dispatch workers and inspect the case, holds and idempotency key. Never create a replacement event by hand.
- **Audit failure:** stop mutations for the affected aggregate and preserve database/log evidence.
- **Credential exposure:** remove the compromised key after an overlap rotation, restart its clients and review request IDs.
- **Bad rule release:** stop workers, roll back the image, preserve cases for review and add a regression fixture.
- **Suspected data exposure:** isolate access, preserve evidence, invoke the organization’s privacy and incident teams, and do not use the retention job to destroy evidence.
