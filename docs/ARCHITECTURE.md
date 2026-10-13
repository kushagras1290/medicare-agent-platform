# Architecture and State Model

## Trust boundaries

1. The automation worker is a service client. It selects synthetic missed-appointment records and calls the API with a stable idempotency key.
2. The agent API is the control plane. It validates identity/input, reads the record, runs deterministic synthetic rules, persists state and calls the model gateway.
3. The model gateway is isolated. It receives a minimized synthetic snapshot and returns only a schema-valid proposal. It has no database or dispatch credentials.
4. A deterministic guardrail layer checks the untrusted proposal for treatment directions, diagnostic certainty, embedded contact targets, consent/channel conflicts, duplicate reasoning and missing urgent-escalation language. Failures become non-bypassable holds.
5. The reviewer console is the human decision boundary. It shows the evidence snapshot the model saw and the patient-facing message, which is separate from staff guidance; reviewer edits to that message pass the same content guardrails. Reviewer credentials cannot invoke service-only agent/dispatch endpoints.
6. The dispatcher reads only approved outbox events and calls the authenticated caller service with the event ID as its idempotency key. Policy-held, urgent, rejected, stale or non-synthetic events fail closed.

## Case state machine

```text
provider_pending <---- retry (attempts left, backoff elapsed) ----.
  |-- provider error / stale run ------> provider_failed ---------'
  `-- valid proposal --------------------> pending_review / on_hold
                                              |-- reject --> rejected
                                              |-- hold ----> on_hold (re-reviewable)
                                              |-- approve + any hold --> human_escalated
                                              |                              `-- close --> closed
                                              `-- approve + no holds -> approved
                                                                           |
                                                                     caller service
                                                                           v
                                                                      dispatched
```

`pending_review` and `on_hold` accept approve, hold or reject. `human_escalated` accepts only
`close`, recorded after offline handling; it can never enter dispatch. Runs left in
`provider_pending` longer than `PROVIDER_STALE_SECONDS` are failed by the maintenance endpoint, and
failed runs are retried up to `PROVIDER_MAX_ATTEMPTS` with exponential backoff. Every review request must carry the current case version; the database transition compares the expected version, status and identifier atomically. Only `approved` plus an empty hold list can be delivered.

## Reliability properties

- Unique idempotency keys prevent duplicate agent runs.
- A unique outbox constraint permits one dispatch intent per case. A dispatcher that dies mid-call leaves a lease that is reclaimed after `OUTBOX_LEASE_SECONDS`; the caller's idempotency key makes the repeat safe.
- `customers.follow_up_started_at` records that automation has handled a customer. Retention deletes cases but never this marker, so purged customers are not contacted again.
- Workers call the API rather than importing orchestration logic, exercising the deployed service boundary.
- Model and caller requests use bounded timeouts and retry only transport errors, `5xx`, `408`, `425` and `429`; other `4xx` responses fail immediately. Model failure creates a visible, retryable state; transient caller failure returns the event to the outbox with backoff, and permanent rejection or exhausted attempts dead-letter it. Configuration validation keeps stale-run and lease thresholds above the retry budgets.
- FastAPI `def` handlers deliberately match the synchronous SQLAlchemy layer and run in the framework thread pool. Long-running automation and dispatch live in independent workers, so blocking I/O is not placed on the event loop.
- Audit entries are HMAC-SHA256 hashes over canonical content and the previous hash, keyed by `AUDIT_HMAC_KEY`. A per-aggregate head row stores a keyed seal over the event count and newest hash, and a unique index on the chain link prevents forks. Verification walks hash links rather than timestamps and detects edits, forks, unlinked events and truncation. Pre-0003 events verify as legacy, unsealed chains until their next append. This is not a substitute for immutable external log storage.
- SQLite supports the local vertical slice. Multi-replica deployments require PostgreSQL.

## Scaling path

The current synchronous model call keeps the vertical slice understandable. At higher volume, route agent-run commands through a durable queue, keep the same idempotency key and state machine, use PostgreSQL row-level locking, export OpenTelemetry traces, and autoscale workers independently. Do not introduce those components until throughput/latency measurements justify them.
