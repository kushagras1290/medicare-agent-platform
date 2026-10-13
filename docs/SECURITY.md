# Security Model

## Trust boundaries

- Workers authenticate to the API with rotatable service keys.
- Reviewers use local keys only in local/test; staging and production require an OIDC bearer token with `medicare:review` (in `scope` or `scp`) and an MFA claim. The console obtains it with the authorization-code flow and PKCE; the CSP allows only the token endpoint origin in addition to `self`.
- The API calls the model gateway and caller service with separate credentials.
- PostgreSQL, model gateway and caller service are on the internal Compose network.
- Model, provider and browser inputs are untrusted. Deterministic code owns all state changes.

## Implemented controls

- Synthetic-only database and startup gates; real data and live dispatch are rejected.
- File-mounted production secrets and overlap rotation for service keys.
- Fixed `RS256` OIDC validation with issuer, audience, expiry, subject, scope and MFA checks.
- Strict request/model schemas, deterministic output guardrails, bounded retries and provider timeouts.
- Consent evidence, policy holds and mandatory human review.
- Optimistic concurrency, idempotency and transactional outbox.
- Keyed (HMAC-SHA256), sealed audit chains with fork, edit and truncation detection.
- Explicit hosts, HTTPS enforcement for browser routes, forwarded headers trusted only from `FORWARDED_ALLOW_IPS`, validated request IDs, CSP, HSTS on HTTPS, no-store responses and frame denial.
- The model gateway and caller service refuse default or inline keys in staging/production; caller HTTP mode requires an explicit synthetic-sandbox attestation.
- Non-root image, separated networks, protected metrics and minimal CI permissions.
- No names, email addresses, phone numbers, postal addresses or free-text clinical notes in the dataset.

## Threat review

| Threat | Control | External work still required |
|---|---|---|
| Model manipulation | Proposal-only model, schema validation, deterministic guardrails | Continuous adversarial evaluation |
| Unauthorized review | OIDC scope, MFA, issuer/audience checks | IdP policy and access recertification |
| Duplicate or stale action | Idempotency, case version, unique outbox | Queue replay exercises |
| Provider outage | Timeout, retry, visible failure, no fallback | On-call provider escalation |
| Audit tampering | Keyed, sealed hash chain and alert counter | WORM/SIEM export and external anchoring of chain heads |
| Secret exposure | File mounts, role separation, overlap rotation | Managed secret store and rotation policy |
| Data disclosure | Synthetic minimized dataset | DPIA, DLP, KMS and approved retention for any future real data |
| Outbound abuse | Human approval, consent/hold checks, service auth, idempotent caller contract | Approved contact provider and destination verification |

This repository is not HIPAA, GDPR, SOC 2 or clinical-certification evidence. No deployed penetration test, cloud IAM review or clinician validation is claimed.
