from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from medicare_agent.guardrails import evaluate_patient_message
from medicare_agent.schemas import AgentProposal, GatewayProposalRequest, RiskTier, RuleAssessment
from services.model_gateway import main as gateway
from tests.factories import make_proposal, make_snapshot

GATEWAY_HEADERS = {"X-Gateway-Key": gateway.settings.model_gateway_api_key.get_secret_value()}


def request_for(tier: RiskTier, case_id: str = "case-1") -> GatewayProposalRequest:
    return GatewayProposalRequest(
        case_id=case_id,
        snapshot=make_snapshot(),
        rule_assessment=RuleAssessment(tier=tier, reasons=[f"{tier.value}_reason"]),
    )


@pytest.fixture
def fresh_cache(monkeypatch: pytest.MonkeyPatch) -> gateway.IdempotencyCache[Any]:
    cache: gateway.IdempotencyCache[Any] = gateway.IdempotencyCache(max_entries=16, ttl_seconds=60)
    monkeypatch.setattr(gateway, "cache", cache)
    return cache


def test_gateway_settings_fail_closed(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="HTTPS"):
        gateway.GatewaySettings(openai_base_url="http://api.openai.test/v1")
    with pytest.raises(ValidationError, match="file-mounted"):
        gateway.GatewaySettings(environment="production")
    weak = tmp_path / "weak"
    weak.write_text("local-gateway-key-change-me-32-chars", encoding="utf-8")
    with pytest.raises(ValidationError, match="non-default"):
        gateway.GatewaySettings(environment="production", model_gateway_api_key_file=weak)
    strong = tmp_path / "strong"
    strong.write_text("g" * 40, encoding="utf-8")
    with pytest.raises(ValidationError, match="OpenAI keys"):
        gateway.GatewaySettings(
            environment="production",
            model_gateway_api_key_file=strong,
            model_gateway_mode="openai",
        )
    openai_key = tmp_path / "openai"
    openai_key.write_text("sk-test-" + "o" * 32, encoding="utf-8")
    loaded = gateway.GatewaySettings(
        environment="production",
        model_gateway_api_key_file=strong,
        model_gateway_mode="openai",
        openai_api_key_file=openai_key,
    )
    assert loaded.openai_api_key.get_secret_value().startswith("sk-test-")


@pytest.mark.parametrize("tier", list(RiskTier))
def test_stub_proposals_keep_patient_messages_neutral(tier: RiskTier) -> None:
    proposal = gateway.stub_proposal(request_for(tier))
    assert evaluate_patient_message(proposal.patient_message) == []
    assert proposal.patient_message != proposal.recommended_action


def test_strict_schema_drops_unsupported_keywords_and_converts_const() -> None:
    schema = gateway.strict_json_schema(AgentProposal.model_json_schema())
    serialized = json.dumps(schema)
    for keyword in ("minLength", "maxLength", "minItems", "maxItems", '"title"'):
        assert keyword not in serialized
    assert schema["properties"]["requires_human_review"]["enum"] == [True]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"])


def test_endpoint_requires_auth_and_replays_idempotent_requests(
    fresh_cache: gateway.IdempotencyCache[Any],
) -> None:
    client = TestClient(gateway.app)
    body = request_for(RiskTier.ROUTINE).model_dump(mode="json")
    assert client.post("/v1/proposals", json=body).status_code == 401
    assert client.get("/health/live").json()["mode"] == "stub"

    no_key = client.post("/v1/proposals", json=body, headers=GATEWAY_HEADERS)
    assert no_key.status_code == 200

    headers = {**GATEWAY_HEADERS, "Idempotency-Key": "case-1"}
    first = client.post("/v1/proposals", json=body, headers=headers)
    replay = client.post("/v1/proposals", json=body, headers=headers)
    assert first.json()["request_id"] == replay.json()["request_id"]

    other = request_for(RiskTier.REVIEW).model_dump(mode="json")
    conflict = client.post("/v1/proposals", json=other, headers=headers)
    assert conflict.status_code == 409
    too_long = client.post(
        "/v1/proposals", json=body, headers={**GATEWAY_HEADERS, "Idempotency-Key": "x" * 129}
    )
    assert too_long.status_code == 400


def test_idempotency_cache_expires_and_evicts() -> None:
    now = [0.0]
    cache: gateway.IdempotencyCache[str] = gateway.IdempotencyCache(
        max_entries=2, ttl_seconds=10, clock=lambda: now[0]
    )
    calls: list[str] = []

    def compute(value: str) -> str:
        calls.append(value)
        return value

    assert cache.run("a", "fp", lambda: compute("a1")) == "a1"
    assert cache.run("a", "fp", lambda: compute("a2")) == "a1"
    now[0] = 11
    assert cache.run("a", "fp", lambda: compute("a3")) == "a3"
    cache.run("b", "fp", lambda: compute("b1"))
    cache.run("c", "fp", lambda: compute("c1"))
    assert cache.run("a", "fp", lambda: compute("a4")) == "a4"
    assert calls == ["a1", "a3", "b1", "c1", "a4"]


def _openai_settings() -> gateway.GatewaySettings:
    return gateway.GatewaySettings(model_gateway_mode="openai", openai_api_key="sk-" + "t" * 40)


def test_openai_mode_uses_strict_schema_and_reports_failures(
    monkeypatch: pytest.MonkeyPatch, fresh_cache: gateway.IdempotencyCache[Any]
) -> None:
    proposal = make_proposal()
    captured: dict[str, Any] = {}

    def responses_api(url: str, **kwargs: Any) -> httpx.Response:
        captured["url"] = url
        captured["payload"] = kwargs["json"]
        captured["timeout"] = kwargs["timeout"]
        return httpx.Response(
            200,
            json={
                "id": "resp_1",
                "output": [
                    {"content": [{"type": "reasoning"}]},
                    {"content": [{"type": "output_text", "text": proposal.model_dump_json()}]},
                ],
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(gateway, "settings", _openai_settings())
    monkeypatch.setattr(gateway.httpx, "post", responses_api)
    client = TestClient(gateway.app)
    body = request_for(RiskTier.ROUTINE).model_dump(mode="json")
    response = client.post("/v1/proposals", json=body, headers=GATEWAY_HEADERS)
    assert response.status_code == 200
    assert response.json()["provider"] == "openai"
    assert response.json()["request_id"] == "resp_1"
    assert captured["url"].endswith("/responses")
    assert captured["timeout"] == 30.0
    schema = captured["payload"]["text"]["format"]["schema"]
    assert "minLength" not in json.dumps(schema)

    monkeypatch.setattr(
        gateway.httpx,
        "post",
        lambda url, **_: httpx.Response(
            200, json={"output": []}, request=httpx.Request("POST", url)
        ),
    )
    assert client.post("/v1/proposals", json=body, headers=GATEWAY_HEADERS).status_code == 502

    monkeypatch.setattr(gateway, "settings", gateway.GatewaySettings(model_gateway_mode="openai"))
    assert client.post("/v1/proposals", json=body, headers=GATEWAY_HEADERS).status_code == 503
