from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy import func, insert, select
from sqlalchemy.orm import Session

from medicare_agent.models import ConsentEvidence, Customer, Observation
from medicare_agent.schemas import CustomerSnapshot

CONSENT_PURPOSE = "mock_follow_up"
CONSENT_SOURCE = "synthetic_dataset_v1"


class SeedError(RuntimeError):
    pass


def parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _validated_row(row: dict[str, str], line_number: int) -> CustomerSnapshot:
    """Validate with the same contract the workflow uses, so a bad row fails at seed time."""

    try:
        return CustomerSnapshot.model_validate(
            {
                **row,
                "conditions": row["conditions"].split("|"),
                "missed_appointment": row["missed_appointment"] == "true",
                "last_contact_at": parse_datetime(row["last_contact_at"]),
                "measured_at": parse_datetime(row["measured_at"]),
            }
        )
    except (KeyError, ValueError, ValidationError) as exc:
        raise SeedError(f"Invalid synthetic CSV row at line {line_number}: {exc}") from exc


def seed_csv(session: Session, path: Path, *, expected_rows: int = 10_000) -> int:
    if (session.scalar(select(func.count()).select_from(Customer)) or 0) > 0:
        raise SeedError("Database already contains customers; refusing a non-idempotent reseed")
    customers: list[dict[str, object]] = []
    observations: list[dict[str, object]] = []
    consent_evidence: list[dict[str, object]] = []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for line_number, row in enumerate(reader, start=2):
            record = _validated_row(row, line_number)
            customers.append(
                {
                    "id": record.customer_id,
                    "age": record.age,
                    "sex_at_birth": record.sex_at_birth,
                    "conditions_json": json.dumps(record.conditions),
                    "missed_appointment": record.missed_appointment,
                    "consent_status": record.consent_status,
                    "last_contact_at": record.last_contact_at,
                    "synthetic": True,
                }
            )
            observations.append(
                {
                    "customer_id": record.customer_id,
                    **record.model_dump(
                        exclude={
                            "customer_id",
                            "age",
                            "sex_at_birth",
                            "conditions",
                            "missed_appointment",
                            "consent_status",
                            "last_contact_at",
                        }
                    ),
                }
            )
            consent_evidence.append(
                {
                    "customer_id": record.customer_id,
                    "purpose": CONSENT_PURPOSE,
                    "version": 1,
                    "status": record.consent_status,
                    "source": CONSENT_SOURCE,
                    "captured_at": record.measured_at,
                    "expires_at": None,
                    "synthetic": True,
                }
            )
    if len(customers) != expected_rows:
        raise SeedError(f"Expected {expected_rows} CSV rows, found {len(customers)}")
    try:
        session.execute(insert(Customer), customers)
        session.execute(insert(Observation), observations)
        session.execute(insert(ConsentEvidence), consent_evidence)
        session.commit()
    except Exception:
        session.rollback()
        raise
    return len(customers)
