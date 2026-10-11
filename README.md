# MediCare Agent Platform

A synthetic-only follow-up workflow with automated case preparation and mandatory human review.

The worker calls the agent API over HTTP. The API applies deterministic safety rules, calls a separate model-gateway service, runs deterministic output guardrails, and places the proposal in a reviewer queue. Nothing reaches the caller service until a human approves the case.

> This is an engineering demonstration, not a clinical system. It is not approved for real patient data, diagnosis, treatment decisions or patient contact. The runtime rejects real-data and live-dispatch modes.

## Flow

```text
automation worker -> agent API -> model gateway
                         |               |
                  deterministic <-------'
                    guardrails
                         |
                    human review
                         |
                 transactional outbox
                         |
              authenticated caller service
```

The model drafts a bounded proposal. Deterministic code owns the risk tier, consent gate, output guardrails, review transition, audit trail and dispatch decision.

## Included

- 10,000 reproducible synthetic customers and 15 measurements per customer.
- FastAPI control plane and separate HTTP model gateway.
- Mandatory approve, hold or reject review with stale-write protection.
- Versioned consent evidence and non-bypassable safety holds.
- Idempotent case creation, transactional outbox and a separate authenticated caller service.
- Hash-linked audit events.
- Local reviewer keys plus production OIDC/JWT, reviewer scope and MFA enforcement.
- File-mounted secrets and overlapping service-key rotation.
- PostgreSQL/SQLite, Alembic, Docker Compose and non-root containers.
- Protected Prometheus metrics, alert rules and worker heartbeats.
- Retention, release-gate, backup/restore and load-test tooling.

The generated measurements are blood pressure, heart rate, oxygen saturation, temperature, respiratory rate, weight, BMI, creatinine, eGFR, glucose, hemoglobin, sodium, potassium and PHQ-9.

## Reviewer console

The console keeps the queue compact and moves case evidence, guardrail holds, the proposal and the decision controls into a focused review drawer.

![MediCare review queue](docs/assets/review-queue.png)

![MediCare case review drawer](docs/assets/review-drawer.png)

## Local run

Requirements: Python 3.12–3.14 and `uv`.

```powershell
cd D:\Interview\Sapient\medicare-agent-platform
Copy-Item .env.example .env
uv sync --all-extras
uv run python scripts/generate_synthetic_data.py
uv run alembic upgrade head
uv run medicare bootstrap --csv data/synthetic_customers.csv
```

Start the gateway:

```powershell
uv run uvicorn services.model_gateway.main:app --host 127.0.0.1 --port 8091
```

Start the caller service in another terminal:

```powershell
uv run uvicorn services.caller.main:app --host 127.0.0.1 --port 8092
```

Start the API in another terminal:

```powershell
uv run uvicorn medicare_agent.main:app --host 127.0.0.1 --port 8000
```

Run one automated batch through the service boundary:

```powershell
uv run python -m medicare_agent.worker automation --once
```

Open `http://127.0.0.1:8000`, use the reviewer token from `REVIEWER_API_KEYS`, inspect a case and record a decision. Process an approved caller event with:

```powershell
uv run medicare dispatch-one
```

## Containers

```powershell
docker compose up --build
```

This starts PostgreSQL, migration/bootstrap, model gateway, caller service, API and both workers. The dashboard is available at `http://127.0.0.1:8000`.

The production override removes inline application secrets and requires OIDC, HTTPS, explicit hosts and an approved release manifest:

```powershell
docker compose -f compose.yaml -f compose.production.yaml config
```

Secret names and required release evidence are documented in [Release gates](docs/RELEASE_GATES.md). The override is a deployment contract, not proof that an organization has supplied those approvals.

## Model gateway

The default `MODEL_GATEWAY_MODE=stub` exercises the real HTTP path without an external provider. To use the OpenAI adapter, configure only the gateway:

```env
MODEL_GATEWAY_MODE=openai
OPENAI_API_KEY_FILE=/run/secrets/openai_api_key
OPENAI_MODEL=gpt-5-mini
```

The response is constrained by JSON Schema and validated again before reaching the workflow. External provider access was not used as evidence for this repository.

## Caller integration

The included caller runs in `mock` mode and proves the complete approval-to-service path without contacting a person. To connect an owned voice service, keep the internal caller credential separate and configure the caller container with:

```env
CALLER_MODE=http
CALLER_PROVIDER_URL=https://voice.example.com/v1/calls
CALLER_PROVIDER_API_KEY_FILE=/run/secrets/caller_provider_api_key
```

The external service must accept the `CallerRequest` JSON contract and return `external_id`, `status: accepted` and `provider`. The caller forwards the outbox event ID as `Idempotency-Key`. A provider error returns `502`; the dispatch worker retries with bounded exponential backoff and dead-letters the event after the configured attempt limit. Real phone numbers are deliberately absent, and the runtime still permits synthetic records only.

## Operations

```powershell
# Status and audit
uv run medicare stats
uv run medicare verify-audit <case-id>

# Preview retention; --execute is deliberately explicit
uv run medicare retention-sweep
uv run medicare retention-sweep --execute

# Validate a release manifest and its pinned digest
uv run medicare release-gate --manifest approvals.json --environment production --sha256 <digest>

# Local SQLite backup and integrity check
uv run python scripts/sqlite_backup.py --source data/medicare.db --output output/backups/medicare.db

# PostgreSQL logical backup and isolated restore drill
powershell -File scripts/postgres_backup_restore_drill.ps1
```

Prometheus and Alertmanager definitions are under `ops/`. Production alert routing must point to an organization-owned incident channel; the checked-in Alertmanager receiver is intentionally a local visible sink.

## Checks

```powershell
uv lock --check
uv run ruff format --check .
uv run ruff check .
uv run mypy src services scripts
uv run pytest --cov=medicare_agent --cov-branch --cov-report=term-missing
uv run python scripts/generate_synthetic_data.py --validate-only
uv run pip-audit -r audit-requirements.txt
docker compose config --quiet
```

The executed results and remaining external approvals are recorded in [Verification](docs/VERIFICATION.md).

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Security](docs/SECURITY.md)
- [Operations](docs/OPERATIONS.md)
- [Release gates](docs/RELEASE_GATES.md)
- [Data dictionary](docs/DATA_DICTIONARY.md)

Clinical approval, legal/privacy approval, provider contracts and permission for real-patient use cannot be granted by code. Production startup requires structured evidence for the applicable organizational gates, while real-patient data and live dispatch remain hard-disabled in this build.
