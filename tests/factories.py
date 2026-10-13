from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from medicare_agent.schemas import AgentProposal, ContactChannel, CustomerSnapshot

SAFE_PATIENT_MESSAGE = (
    "This is a synthetic reminder from your care team. Please book a follow-up visit."
)
SERVICE_KEY = "test-service-key-at-least-32-characters"
REVIEWER_KEY = "test-reviewer-key-at-least-32-characters"


def snapshot_values(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "customer_id": "C00001",
        "age": 62,
        "sex_at_birth": "female",
        "conditions": ["preventive_follow_up"],
        "missed_appointment": False,
        "consent_status": "synthetic_opted_in",
        "last_contact_at": datetime(2026, 9, 1, tzinfo=UTC),
        "measured_at": datetime(2026, 10, 1, tzinfo=UTC),
        "bp_systolic_mm_hg": 120,
        "bp_diastolic_mm_hg": 75,
        "heart_rate_bpm": 74,
        "spo2_pct": 97,
        "temperature_c": 36.8,
        "respiratory_rate_bpm": 16,
        "weight_kg": 67,
        "bmi": 24.5,
        "creatinine_mg_dl": 0.9,
        "egfr_ml_min_1_73m2": 82,
        "glucose_mg_dl": 103,
        "hemoglobin_g_dl": 13.4,
        "sodium_mmol_l": 139,
        "potassium_mmol_l": 4.2,
        "phq9_score": 3,
    }
    values.update(overrides)
    return values


def make_snapshot(**overrides: Any) -> CustomerSnapshot:
    return CustomerSnapshot.model_validate(snapshot_values(**overrides))


def make_proposal(**overrides: Any) -> AgentProposal:
    values: dict[str, Any] = {
        "summary": "Synthetic record reviewed through the isolated HTTP model gateway.",
        "recommended_action": "A human reviewer should approve a routine mock portal follow-up.",
        "patient_message": SAFE_PATIENT_MESSAGE,
        "contact_channel": ContactChannel.PORTAL,
        "reasoning_bullets": ["no_synthetic_demo_flags"],
        "requires_human_review": True,
    }
    values.update(overrides)
    return AgentProposal.model_validate(values)
