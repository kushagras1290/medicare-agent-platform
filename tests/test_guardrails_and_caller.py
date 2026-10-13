from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medicare_agent.guardrails import evaluate_patient_message, evaluate_proposal
from medicare_agent.schemas import ContactChannel, RiskTier, RuleAssessment
from services.caller import main as caller_service
from services.caller.main import CallerSettings, app, settings
from tests.factories import make_proposal, make_snapshot

CALLER_PAYLOAD = {
    "case_id": "case-1",
    "customer_id": "C00001",
    "channel": "phone",
    "message": "Place a synthetic follow-up call after reviewer approval.",
    "synthetic": True,
    "approved_by_human": True,
}


def test_guardrail_blocks_diagnostic_claim_and_embedded_phone_number() -> None:
    result = evaluate_proposal(
        make_proposal(
            summary="This confirms you have a serious disease.",
            recommended_action="Call +1 (555) 123-4567 for a routine follow-up.",
            contact_channel=ContactChannel.PHONE,
            reasoning_bullets=["Synthetic test evidence"],
        ),
        RuleAssessment(tier=RiskTier.REVIEW, reasons=["synthetic_review"]),
        make_snapshot(),
    )
    assert "guardrail_diagnostic_claim" in result.holds
    assert "guardrail_embedded_contact_target" in result.holds


def test_guardrail_requires_urgent_escalation_language() -> None:
    result = evaluate_proposal(
        make_proposal(
            summary="Synthetic urgent record needs review.",
            recommended_action="Arrange a routine follow-up when convenient.",
            contact_channel=ContactChannel.PHONE,
            reasoning_bullets=["Low synthetic oxygen observation"],
        ),
        RuleAssessment(tier=RiskTier.URGENT, reasons=["spo2_urgent"]),
        make_snapshot(),
    )
    assert "guardrail_urgent_escalation_missing" in result.holds


@pytest.mark.parametrize(
    "text",
    [
        "Last measured 2026-10-12 with blood pressure 150/95 noted.",
        "Readings were BP 150 120 95 and SpO2 97 on review.",
        "Follow-up planned for 12.10.2026 at the portal.",
    ],
)
def test_dates_and_measurements_are_not_contact_targets(text: str) -> None:
    assert evaluate_patient_message(text) == []


@pytest.mark.parametrize(
    ("text", "hold"),
    [
        ("Please take double your insulin tonight.", "guardrail_treatment_instruction"),
        ("Discontinue your lisinopril until the visit.", "guardrail_treatment_instruction"),
        ("Stop taking the tablets for now.", "guardrail_treatment_instruction"),
        ("Call 555.123.4567 to confirm the visit.", "guardrail_embedded_contact_target"),
        ("Book online at https://clinic.example.test now.", "guardrail_embedded_contact_target"),
        ("Email care.team@clinic.example to confirm.", "guardrail_embedded_contact_target"),
    ],
)
def test_patient_message_guardrails_catch_unsafe_text(text: str, hold: str) -> None:
    assert hold in evaluate_patient_message(text)


def test_patient_message_is_scanned_inside_proposals() -> None:
    result = evaluate_proposal(
        make_proposal(patient_message="Please reduce your medication dose before the visit."),
        RuleAssessment(tier=RiskTier.ROUTINE, reasons=["routine"]),
        make_snapshot(),
    )
    assert "guardrail_treatment_instruction" in result.holds


def test_caller_service_requires_auth_human_approval_and_idempotency() -> None:
    client = TestClient(app)
    assert client.post("/v1/calls", json=CALLER_PAYLOAD).status_code == 401
    headers = {
        "X-Caller-Key": settings.caller_service_api_key.get_secret_value(),
        "Idempotency-Key": "event-1",
    }
    first = client.post("/v1/calls", json=CALLER_PAYLOAD, headers=headers)
    second = client.post("/v1/calls", json=CALLER_PAYLOAD, headers=headers)
    assert first.status_code == 200
    assert first.json()["external_id"] == second.json()["external_id"]
    unapproved = client.post(
        "/v1/calls", json={**CALLER_PAYLOAD, "approved_by_human": False}, headers=headers
    )
    assert unapproved.status_code == 422
    no_key = client.post(
        "/v1/calls",
        json=CALLER_PAYLOAD,
        headers={"X-Caller-Key": headers["X-Caller-Key"]},
    )
    assert no_key.status_code == 400
    assert client.get("/health/live").json() == {"status": "alive", "mode": "mock"}


def test_caller_http_mode_requires_https_key_and_sandbox_attestation() -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        CallerSettings(caller_mode="http", caller_provider_api_key="p" * 40)
    with pytest.raises(ValidationError, match="provider API key"):
        CallerSettings(caller_mode="http", caller_provider_url="https://voice.example.test")
    with pytest.raises(ValidationError, match="SYNTHETIC_SANDBOX"):
        CallerSettings(
            caller_mode="http",
            caller_provider_url="https://voice.example.test",
            caller_provider_api_key="p" * 40,
        )


def test_caller_production_settings_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="file-mounted"):
        CallerSettings(environment="production")
    weak = tmp_path / "weak"
    weak.write_text("local-caller-key-change-me-32-chars", encoding="utf-8")
    with pytest.raises(ValidationError, match="non-default"):
        CallerSettings(environment="production", caller_service_api_key_file=weak)
    strong = tmp_path / "strong"
    strong.write_text("s" * 40, encoding="utf-8")
    provider = tmp_path / "provider"
    provider.write_text("p" * 40, encoding="utf-8")
    loaded = CallerSettings(
        environment="production",
        caller_service_api_key_file=strong,
        caller_provider_api_key_file=provider,
    )
    assert loaded.caller_service_api_key.get_secret_value() == "s" * 40
    assert loaded.caller_provider_api_key is not None


def test_caller_http_adapter_uses_https_secret_and_idempotency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    external = CallerSettings(
        caller_mode="http",
        caller_service_api_key="s" * 40,
        caller_provider_url="https://voice.example.test/v1/calls",
        caller_provider_api_key="p" * 40,
        caller_provider_is_synthetic_sandbox=True,
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
    headers = {"X-Caller-Key": "s" * 40, "Idempotency-Key": "event-http-1"}
    response = TestClient(app).post("/v1/calls", headers=headers, json=CALLER_PAYLOAD)
    assert response.status_code == 200
    assert response.json()["external_id"] == "provider-call-123"
    assert seen == {
        "url": "https://voice.example.test/v1/calls",
        "idempotency": "event-http-1",
    }

    def provider_down(url: str, **_: object) -> httpx.Response:
        raise httpx.ConnectError("down", request=httpx.Request("POST", url))

    monkeypatch.setattr(caller_service.httpx, "post", provider_down)
    failed = TestClient(app).post("/v1/calls", headers=headers, json=CALLER_PAYLOAD)
    assert failed.status_code == 502

    unconfigured = CallerSettings.model_construct(
        caller_mode="http",
        caller_service_api_key=external.caller_service_api_key,
        caller_provider_url=None,
        caller_provider_api_key=None,
    )
    monkeypatch.setattr(caller_service, "settings", unconfigured)
    missing = TestClient(app).post("/v1/calls", headers=headers, json=CALLER_PAYLOAD)
    assert missing.status_code == 503
