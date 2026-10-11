from __future__ import annotations

from medicare_agent.schemas import CustomerSnapshot, RiskTier, RuleAssessment

# SYNTHETIC DEMO CONFIGURATION ONLY.
# These thresholds are not clinician-approved policy and must not be used for real patients.


def assess(snapshot: CustomerSnapshot) -> RuleAssessment:
    urgent: list[str] = []
    review: list[str] = []
    holds: list[str] = []

    if snapshot.bp_diastolic_mm_hg >= snapshot.bp_systolic_mm_hg:
        holds.append("data_quality_bp_inversion")
    if snapshot.spo2_pct < 90:
        urgent.append("synthetic_spo2_below_90")
    if snapshot.bp_systolic_mm_hg >= 180 or snapshot.bp_diastolic_mm_hg >= 120:
        urgent.append("synthetic_bp_extreme")
    if snapshot.phq9_score >= 20:
        urgent.append("synthetic_phq9_high_requires_human_screen")
    if snapshot.potassium_mmol_l < 2.8 or snapshot.potassium_mmol_l > 6.0:
        urgent.append("synthetic_potassium_extreme")

    if snapshot.missed_appointment:
        review.append("missed_appointment")
    if snapshot.egfr_ml_min_1_73m2 < 45 or snapshot.creatinine_mg_dl > 1.8:
        review.append("synthetic_renal_marker_review")
    if snapshot.glucose_mg_dl > 250:
        review.append("synthetic_glucose_review")
    if snapshot.hemoglobin_g_dl < 9:
        review.append("synthetic_hemoglobin_review")
    if snapshot.sodium_mmol_l < 130 or snapshot.sodium_mmol_l > 150:
        review.append("synthetic_sodium_review")
    if snapshot.spo2_pct < 94 or snapshot.respiratory_rate_bpm > 24:
        review.append("synthetic_respiratory_review")

    if snapshot.consent_status != "synthetic_opted_in":
        holds.append("synthetic_contact_consent_withheld")
    if urgent:
        holds.append("urgent_human_escalation_only")
        return RuleAssessment(
            tier=RiskTier.URGENT, reasons=urgent + review or urgent, policy_holds=sorted(set(holds))
        )
    if holds or review:
        return RuleAssessment(
            tier=RiskTier.REVIEW,
            reasons=review or ["data_quality_requires_review"],
            policy_holds=sorted(set(holds)),
        )
    return RuleAssessment(
        tier=RiskTier.ROUTINE, reasons=["no_synthetic_demo_flags"], policy_holds=[]
    )
