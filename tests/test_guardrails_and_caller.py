from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medicare_agent.guardrails import evaluate_proposal
from medicare_agent.schemas import (
    AgentProposal,
    ContactChannel,
    CustomerSnapshot,
    RiskTier,
    RuleAssessment,
)
from services.caller import main as caller_service
from services.caller.main import CallerSettings, app, settings


def snapshot(*, consent: str = "synthetic_opted_in") -> CustomerSnapshot:
    now = datetime(2026, 10, 1, tzinfo=UTC)
    return CustomerSnapshot(
        customer_id="C00001",
        age=62,
        sex_at_birth="female",
        conditions=["preventive_follow_up"],
        missed_appointment=False,
        consent_status=consent,
        last_contact_at=now,
        measured_at=now,
        bp_systolic_mm_hg=120,
        bp_diastolic_mm_hg=75,
        heart_rate_bpm=74,
        spo2_pct=97,
        temperature_c=36.8,
        respiratory_rate_bpm=16,
        weight_kg=67,
        bmi=24.5,
        creatinine_mg_dl=0.9,
        egfr_ml_min_1_73m2=82,
        glucose_mg_dl=103,
        hemoglobin_g_dl=13.4,
        sodium_mmol_l=139,
        potassium_mmol_l=4.2,
        phq9_score=3,
    )


def test_guardrail_blocks_diagnostic_claim_and_embedded_phone_number() -> None:
    result = evaluate_proposal(
        AgentProposal(
            summary="This confirms you have a serious disease.",
            recommended_action="Call +1 (555) 123-4567 for a routine follow-up.",
            contact_channel=ContactChannel.PHONE,
            reasoning_bullets=["Synthetic test evidence"],
            requires_human_review=True,
        ),
        RuleAssessment(tier=RiskTier.REVIEW, reasons=["synthetic_review"]),
        snapshot(),
    )
    assert "guardrail_diagnostic_claim" in result.holds
    assert "guardrail_embedded_contact_target" in result.holds


def test_guardrail_requires_urgent_escalation_language() -> None:
    result = evaluate_proposal(
        AgentProposal(
            summary="Synthetic urgent record needs review.",
            recommended_action="Arrange a routine follow-up when convenient.",
            contact_channel=ContactChannel.PHONE,
            reasoning_bullets=["Low synthetic oxygen observation"],
            requires_human_review=True,
        ),
        RuleAssessment(tier=RiskTier.URGENT, reasons=["spo2_urgent"]),
        snapshot(),
    )
    assert "guardrail_urgent_escalation_missing" in result.holds


def test_caller_service_requires_auth_human_approval_and_idempotency() -> None:
    client = TestClient(app)
    payload = {
        "case_id": "case-1",
        "customer_id": "C00001",
        "channel": "phone",
        "message": "Place a synthetic follow-up call after reviewer approval.",
        "synthetic": True,
        "approved_by_human": True,
    }
    assert client.post("/v1/calls", json=payload).status_code == 401
    headers = {
        "X-Caller-Key": settings.caller_service_api_key.get_secret_value(),
        "Idempotency-Key": "event-1",
    }
    first = client.post("/v1/calls", json=payload, headers=headers)
    second = client.post("/v1/calls", json=payload, headers=headers)
    assert first.status_code == 200
    assert first.json()["external_id"] == second.json()["external_id"]


def test_caller_http_adapter_uses_https_secret_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        CallerSettings(caller_mode="http", caller_provider_api_key="p" * 40)
    external = CallerSettings(
        caller_mode="http",
        caller_service_api_key="s" * 40,
        caller_provider_url="https://voice.example.test/v1/calls",
        caller_provider_api_key="p" * 40,
    )
    seen: dict[str, str] = {}

    def provider_call(url: str, **kwargs: object) -> httpx.Response:
        seen["url"] = url
        headers = kwargs["headers"]
        assert isinstance(headers, dict)
        seen["idempotency"] = str(headers["Idempotency-Key"])
        return httpx.Response(
            200,
            json={
                "external_id": "provider-call-123",
                "status": "accepted",
                "provider": "external-voice-service",
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(caller_service, "settings", external)
    monkeypatch.setattr(caller_service.httpx, "post", provider_call)
    response = TestClient(app).post(
        "/v1/calls",
        headers={"X-Caller-Key": "s" * 40, "Idempotency-Key": "event-http-1"},
        json={
            "case_id": "case-1",
            "customer_id": "C00001",
            "channel": "phone",
            "message": "Place a synthetic external follow-up call after approval.",
            "synthetic": True,
            "approved_by_human": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["external_id"] == "provider-call-123"
    assert seen == {
        "url": "https://voice.example.test/v1/calls",
        "idempotency": "event-http-1",
    }
