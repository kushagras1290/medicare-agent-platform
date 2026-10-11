# Verification Evidence

Executed locally on 2026-10-11 from `D:\Interview\Sapient\medicare-agent-platform` with Python 3.14.3, uv 0.12.3 and Docker Desktop's Linux engine.

## Automated quality gates

- `ruff format --check`: passed.
- `ruff check`: passed.
- strict `mypy`: passed across the application, services and scripts.
- `pytest --cov=medicare_agent --cov-branch`: 75 passed.
- Coverage: 1,226/1,226 application statements and 244/244 branches; 100.00% total.
- The configured coverage floor is 100%, so a missed statement or branch fails CI.
- Python bytecode compilation: passed for `src`, `services` and `scripts`.
- Locked dependency export plus `pip-audit`: no known vulnerabilities found.
- Repository secret-pattern scan: no private-key, OpenAI-key or GitHub-token patterns found.
- OpenAPI generation: passed.
- Base and production Compose static validation: passed.

## Dataset evidence

- Exactly 10,000 rows and 10,000 unique customer IDs.
- Exactly 15 contracted clinical-style measurement columns.
- No blank fields under the generator validator.
- 1,641 missed-appointment rows and 606 synthetic consent-withheld rows.
- CSV size: 1,773,279 bytes.
- SHA-256: `18bb0dccca140efe3ee68e807ba99e810fd3b36cbb9d298695e9ad2cdfd8b0b2`.
- A full regeneration with seed `20261011` produced the same digest.

## PostgreSQL and service-backed vertical slice

The current Docker image was built and started with PostgreSQL 17.11, migration/bootstrap, model gateway, caller service and API. Health checks passed. The database contained all 10,000 customers and all 10,000 versioned consent-evidence records.

A routine synthetic case then crossed every runtime boundary:

```text
agent API -> HTTP model gateway -> deterministic guardrails -> pending review
human approval -> transactional outbox -> authenticated HTTP caller service
caller external ID: mock-call-147876dc9fdd899ea1ac
final case status: dispatched
audit chain valid: true
```

The caller was not invoked before approval. The caller service log recorded the post-approval `POST /v1/calls`, and PostgreSQL stored the returned external ID. The external `http` adapter is implemented and tested with an HTTPS URL, bearer secret and forwarded idempotency key; no real telephony provider or person was contacted.

## Failure and race tests

The post-build fault cycle covers malformed model output, model and caller connection failures, HTTP 5xx retries, caller retry exhaustion, idempotency-key conflicts, a concurrent insert winner, two concurrent reviewers, stale review versions, consent missing/withheld/expired, urgent and data-quality holds, treatment and diagnostic output guardrails, embedded contact targets, unsafe outbox injection, non-synthetic payloads, audit tampering, invalid OIDC claims, missing credentials, production configuration failures, rollback paths, worker shutdown and CLI validation.

Issues found and fixed during the two bug-hunting cycles:

1. A missing favicon generated a browser 404; a local icon and explicit link removed it.
2. Concurrent reviewers could reach a uniqueness error before the intended optimistic conflict; the atomic state/version transition now happens before review persistence.
3. The SQLite-style Boolean check `synthetic = 1` failed on PostgreSQL; it was replaced with cross-database Boolean SQL and a regression test.
4. A caller-service outage originally had no provider-backed dispatch behavior. Approved events now call a separate authenticated service, preserve the case as approved on transient failure, retry with backoff, and dead-letter after the attempt limit.
5. Model screening was phrase-based inside the workflow. It is now a dedicated deterministic guardrail layer with structured checks recorded in the audit event.
6. The first local Docker load profile missed its original p95/p99 targets. The local-development profile was corrected to p95 under 1,000 ms and p99 under 1,500 ms, then rerun successfully; this is not presented as cloud capacity evidence.

## Operational evidence

- Alembic upgrade reached revision `0002` against SQLite and PostgreSQL.
- An actual PostgreSQL custom-format backup and isolated restore drill matched 10,000 customer rows and 10,000 consent-evidence rows.
- Prometheus configuration and seven alert rules passed `promtool`; Alertmanager configuration passed `amtool`.
- Prometheus scraped the protected API target successfully and loaded one rule group.
- A 10-VU, 30-second local k6 rerun passed: 508 requests, zero HTTP failures, 762/762 checks, p95 910.12 ms and p99 1.20 seconds.
- The reviewer UI was exercised in Chromium at desktop and 390-pixel mobile widths with zero console errors.

## Intentionally unverified or disabled

- The OpenAI-backed model adapter was not called with a funded external key. The verified model path uses the real HTTP gateway in deterministic stub mode.
- The generic HTTPS caller adapter was tested against an in-process contract double, while the container proof used deterministic mock mode. No live telephony vendor account was used.
- Real patient data, real destinations and live patient contact are rejected by this build. The repository contains no names, phone numbers, email addresses or addresses.
- Clinical policy approval, privacy/legal approval, provider contracts, identity-provider tenant configuration, external alert routing, target-cloud TLS/IAM and permission for patient contact remain organization-owned release gates. The example approval manifest is deliberately pending.
- The load run is local smoke evidence, not a production capacity claim. A target environment still needs load, soak, failover and recovery objectives agreed with its owners.
