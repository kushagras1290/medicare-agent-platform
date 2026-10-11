# Architecture and State Model

## Trust boundaries

1. The automation worker is a service client. It selects synthetic missed-appointment records and calls the API with a stable idempotency key.
2. The agent API is the control plane. It validates identity/input, reads the record, runs deterministic synthetic rules, persists state and calls the model gateway.
3. The model gateway is isolated. It receives a minimized synthetic snapshot and returns only a schema-valid proposal. It has no database or dispatch credentials.
4. A deterministic guardrail layer checks the untrusted proposal for treatment directions, diagnostic certainty, embedded contact targets, consent/channel conflicts, duplicate reasoning and missing urgent-escalation language. Failures become non-bypassable holds.
5. The reviewer console is the human decision boundary. Reviewer credentials cannot invoke service-only agent/dispatch endpoints.
6. The dispatcher reads only approved outbox events and calls the authenticated caller service with the event ID as its idempotency key. Policy-held, urgent, rejected, stale or non-synthetic events fail closed.

## Case state machine

```text
provider_pending
  |-- provider error --------------------> provider_failed
  `-- valid proposal --------------------> pending_review
                                              |-- reject --> rejected
                                              |-- hold ----> on_hold
                                              |-- approve + any hold --> human_escalated
                                              `-- approve + no holds -> approved
                                                                           |
                                                                     caller service
                                                                           v
                                                                      dispatched
```

Only `pending_review` accepts a review. The request must carry the current case version; the database transition compares the expected version, status and identifier atomically. Only `approved` plus an empty hold list can be delivered.

## Reliability properties

- Unique idempotency keys prevent duplicate agent runs.
- A unique outbox constraint permits one dispatch intent per case.
- Workers call the API rather than importing orchestration logic, exercising the deployed service boundary.
- Model and caller requests use bounded timeouts and retries. Model failure creates a visible terminal state; caller failure returns the event to the outbox with backoff and eventually dead-letters it.
- FastAPI `def` handlers deliberately match the synchronous SQLAlchemy layer and run in the framework thread pool. Long-running automation and dispatch live in independent workers, so blocking I/O is not placed on the event loop.
- Audit entries include the previous event hash and canonicalized content hash. This detects application-visible tampering; it is not a substitute for immutable external log storage.
- SQLite supports the local vertical slice. Multi-replica deployments require PostgreSQL.

## Scaling path

The current synchronous model call keeps the vertical slice understandable. At higher volume, route agent-run commands through a durable queue, keep the same idempotency key and state machine, use PostgreSQL row-level locking, export OpenTelemetry traces, and autoscale workers independently. Do not introduce those components until throughput/latency measurements justify them.
