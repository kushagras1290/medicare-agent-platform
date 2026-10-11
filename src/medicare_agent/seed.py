from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from medicare_agent.models import ConsentEvidence, Customer, Observation


class SeedError(RuntimeError):
    pass


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def seed_csv(session: Session, path: Path, *, expected_rows: int = 10_000) -> int:
    if (session.scalar(select(func.count()).select_from(Customer)) or 0) > 0:
        raise SeedError("Database already contains customers; refusing a non-idempotent reseed")
    customers: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    consent_evidence: list[dict[str, object]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            customer_id = row["customer_id"]
            customers.append(
                {
                    "id": customer_id,
                    "age": int(row["age"]),
                    "sex_at_birth": row["sex_at_birth"],
                    "conditions_json": json.dumps(row["conditions"].split("|")),
                    "missed_appointment": row["missed_appointment"] == "true",
                    "consent_status": row["consent_status"],
                    "last_contact_at": parse_datetime(row["last_contact_at"]),
                    "synthetic": True,
                }
            )
            observations.append(
                {
                    "customer_id": customer_id,
                    "measured_at": parse_datetime(row["measured_at"]),
                    "bp_systolic_mm_hg": float(row["bp_systolic_mm_hg"]),
                    "bp_diastolic_mm_hg": float(row["bp_diastolic_mm_hg"]),
                    "heart_rate_bpm": float(row["heart_rate_bpm"]),
                    "spo2_pct": float(row["spo2_pct"]),
                    "temperature_c": float(row["temperature_c"]),
                    "respiratory_rate_bpm": float(row["respiratory_rate_bpm"]),
                    "weight_kg": float(row["weight_kg"]),
                    "bmi": float(row["bmi"]),
                    "creatinine_mg_dl": float(row["creatinine_mg_dl"]),
                    "egfr_ml_min_1_73m2": float(row["egfr_ml_min_1_73m2"]),
                    "glucose_mg_dl": float(row["glucose_mg_dl"]),
                    "hemoglobin_g_dl": float(row["hemoglobin_g_dl"]),
                    "sodium_mmol_l": float(row["sodium_mmol_l"]),
                    "potassium_mmol_l": float(row["potassium_mmol_l"]),
                    "phq9_score": int(row["phq9_score"]),
                }
            )
            consent_evidence.append(
                {
                    "customer_id": customer_id,
                    "purpose": "mock_follow_up",
                    "version": 1,
                    "status": row["consent_status"],
                    "source": "synthetic_dataset_v1",
                    "captured_at": parse_datetime(row["measured_at"]),
                    "expires_at": None,
                    "synthetic": True,
                }
            )
    if len(customers) != expected_rows:
        raise SeedError(f"Expected {expected_rows} CSV rows, found {len(customers)}")
    try:
        session.bulk_insert_mappings(Customer, customers)
        session.bulk_insert_mappings(Observation, observations)
        session.bulk_insert_mappings(ConsentEvidence, consent_evidence)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return len(customers)
