from __future__ import annotations

from datetime import UTC, datetime

import pytest

from medicare_agent.risk_rules import assess
from medicare_agent.schemas import CustomerSnapshot, RiskTier


def snapshot(**overrides: object) -> CustomerSnapshot:
    values: dict[str, object] = {
        "customer_id": "C00001",
        "age": 42,
        "sex_at_birth": "female",
        "conditions": ["preventive_follow_up"],
        "missed_appointment": False,
        "consent_status": "synthetic_opted_in",
        "last_contact_at": datetime(2026, 9, 1, tzinfo=UTC),
        "measured_at": datetime(2026, 10, 1, tzinfo=UTC),
        "bp_systolic_mm_hg": 118,
        "bp_diastolic_mm_hg": 74,
        "heart_rate_bpm": 70,
        "spo2_pct": 98,
        "temperature_c": 36.7,
        "respiratory_rate_bpm": 16,
        "weight_kg": 64,
        "bmi": 23,
        "creatinine_mg_dl": 0.8,
        "egfr_ml_min_1_73m2": 92,
        "glucose_mg_dl": 96,
        "hemoglobin_g_dl": 13.8,
        "sodium_mmol_l": 139,
        "potassium_mmol_l": 4.1,
        "phq9_score": 2,
    }
    values.update(overrides)
    return CustomerSnapshot.model_validate(values)


def test_routine_snapshot_has_no_hold() -> None:
    result = assess(snapshot())
    assert result.tier == RiskTier.ROUTINE
    assert result.policy_holds == []


def test_missed_appointment_is_reviewed() -> None:
    result = assess(snapshot(missed_appointment=True))
    assert result.tier == RiskTier.REVIEW
    assert "missed_appointment" in result.reasons


def test_urgent_snapshot_is_held_for_human_only() -> None:
    result = assess(snapshot(spo2_pct=86))
    assert result.tier == RiskTier.URGENT
    assert "urgent_human_escalation_only" in result.policy_holds


def test_data_quality_and_consent_fail_closed() -> None:
    result = assess(
        snapshot(
            bp_systolic_mm_hg=100,
            bp_diastolic_mm_hg=110,
            consent_status="synthetic_withheld",
        )
    )
    assert result.tier == RiskTier.REVIEW
    assert set(result.policy_holds) == {
        "data_quality_bp_inversion",
        "synthetic_contact_consent_withheld",
    }


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"bp_systolic_mm_hg": 190}, "synthetic_bp_extreme"),
        ({"phq9_score": 24}, "synthetic_phq9_high_requires_human_screen"),
        ({"potassium_mmol_l": 6.5}, "synthetic_potassium_extreme"),
        ({"egfr_ml_min_1_73m2": 30}, "synthetic_renal_marker_review"),
        ({"glucose_mg_dl": 300}, "synthetic_glucose_review"),
        ({"hemoglobin_g_dl": 8}, "synthetic_hemoglobin_review"),
        ({"sodium_mmol_l": 126}, "synthetic_sodium_review"),
        ({"respiratory_rate_bpm": 28}, "synthetic_respiratory_review"),
    ],
)
def test_each_synthetic_rule_is_observable(overrides: dict[str, object], reason: str) -> None:
    assert reason in assess(snapshot(**overrides)).reasons
