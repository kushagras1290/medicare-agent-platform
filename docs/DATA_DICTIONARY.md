# Synthetic Dataset Contract

`data/synthetic_customers.csv` contains 10,000 deterministic, non-identifiable synthetic customers. The generator seed, snapshot, SHA-256 and validation counts are recorded in the adjacent manifest.

## Metadata (not part of the 15 measurements)

`customer_id`, `age`, `sex_at_birth`, pipe-separated `conditions`, `missed_appointment`, synthetic contact-consent state, last-contact timestamp and observation timestamp.

## Fifteen clinical-style parameters

1. `bp_systolic_mm_hg`
2. `bp_diastolic_mm_hg`
3. `heart_rate_bpm`
4. `spo2_pct`
5. `temperature_c`
6. `respiratory_rate_bpm`
7. `weight_kg`
8. `bmi`
9. `creatinine_mg_dl`
10. `egfr_ml_min_1_73m2`
11. `glucose_mg_dl`
12. `hemoglobin_g_dl`
13. `sodium_mmol_l`
14. `potassium_mmol_l`
15. `phq9_score`

Values are correlated enough to exercise software paths but are not a validated population model, epidemiological sample, or clinical benchmark. Rare deterministic edge rows are deliberately included for urgent and data-quality gate tests.

## Operational records

- `consent_evidence`: versioned synthetic evidence for the `mock_follow_up` purpose. Missing, withheld or expired evidence creates a dispatch-blocking hold.
- `follow_up_cases`: deterministic risk result, model proposal, policy holds and state version.
- `human_reviews`: reviewer identity, decision, rationale and reviewed case version.
- `provider_calls`: provider/model, status, latency and request ID; request payloads are not stored.
- `outbox_events`: idempotent mock-delivery request and status.
- `audit_events`: hash-linked aggregate events.
- `worker_heartbeats`: latest worker timestamp, status and processed count without customer payloads.
