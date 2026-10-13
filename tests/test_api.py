from __future__ import annotations

import json
from collections.abc import Callable, Generator, Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from medicare_agent import main
from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings, get_settings
from medicare_agent.db import get_session
from medicare_agent.main import app
from medicare_agent.models import OutboxEvent
from medicare_agent.provider import ModelGatewayClient
from tests.factories import REVIEWER_KEY, SERVICE_KEY

SERVICE_HEADERS = {"X-Service-Key": SERVICE_KEY}
REVIEWER_HEADERS = {"X-Reviewer-Key": REVIEWER_KEY}
OIDC_UPDATE = {
    "reviewer_auth_mode": "oidc",
    "oidc_issuer": "https://id.example.test/",
    "oidc_audience": "medicare-api",
    "oidc_jwks_url": "https://id.example.test/jwks",
    "oidc_client_id": "medicare-console",
    "oidc_authorization_url": "https://id.example.test/authorize",
    "oidc_token_url": "https://tokens.example.test/oauth/token",
}


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings
) -> Iterator[TestClient]:
    def override_session() -> Generator[Session, None, None]:
        yield session

    monkeypatch.setattr(main, "get_settings", lambda: settings)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def test_dashboard_and_health_are_available(client: TestClient) -> None:
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert "Simulation only" in dashboard.text
    assert 'data-auth-mode="api_key"' in dashboard.text
    assert "connect-src 'self';" in dashboard.headers["Content-Security-Policy"]
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["human_review_required"] is True


def test_oidc_dashboard_exposes_public_client_config_and_token_origin(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    oidc = settings.model_copy(update=OIDC_UPDATE)
    monkeypatch.setattr(main, "get_settings", lambda: oidc)
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert 'data-oidc-client-id="medicare-console"' in page.text
    assert 'id="sign-in"' in page.text
    assert "https://tokens.example.test" in page.headers["Content-Security-Policy"]

    misconfigured = settings.model_copy(
        update={**OIDC_UPDATE, "oidc_token_url": "http://tokens.example.test/token"}
    )
    monkeypatch.setattr(main, "get_settings", lambda: misconfigured)
    page = TestClient(app).get("/")
    assert "data-oidc-client-id" not in page.text
    assert "connect-src 'self';" in page.headers["Content-Security-Policy"]


def test_https_enforcement_covers_browser_routes_and_exempts_internal_routes(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings
) -> None:
    def override_session() -> Generator[Session, None, None]:
        yield session

    secure = settings.model_copy(update={"enforce_https": True})
    monkeypatch.setattr(main, "get_settings", lambda: secure)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: secure
    try:
        plain = TestClient(app)
        denied = plain.get("/")
        assert denied.status_code == 400
        assert denied.json()["error"]["code"] == "HTTPS_REQUIRED"
        # The forwarded header alone is not trusted; uvicorn rewrites the scheme only for
        # FORWARDED_ALLOW_IPS, so a spoofed header must not bypass the check.
        spoofed = plain.get("/api/v1/cases", headers={"X-Forwarded-Proto": "https"})
        assert spoofed.status_code == 400
        assert plain.get("/health/live").status_code == 200
        assert plain.get("/health/ready").status_code == 200
        assert plain.get("/metrics", headers=SERVICE_HEADERS).status_code == 200
        assert plain.post("/api/v1/outbox/dispatch-one", headers=SERVICE_HEADERS).status_code in {
            200,
            409,
        }
        secure_client = TestClient(app, base_url="https://testserver")
        allowed = secure_client.get("/api/v1/cases", headers=REVIEWER_HEADERS)
        assert allowed.status_code == 200
        assert "Strict-Transport-Security" in allowed.headers
    finally:
        app.dependency_overrides.clear()


def test_reviewer_routes_reject_missing_key(
    client: TestClient, add_customer: Callable[..., object]
) -> None:
    add_customer("C00001")
    response = client.get("/api/v1/cases")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "HTTP_401"
    allowed = client.get("/api/v1/cases", headers=REVIEWER_HEADERS)
    assert allowed.status_code == 200
    assert allowed.json()["success"] is True


def test_service_routes_reject_reviewer_key(client: TestClient) -> None:
    response = client.post("/api/v1/outbox/dispatch-one", headers=REVIEWER_HEADERS)
    assert response.status_code == 401
    assert response.json()["success"] is False
    assert client.post("/api/v1/agent/maintenance", headers=REVIEWER_HEADERS).status_code == 401
    metrics_denied = client.get("/metrics")
    assert metrics_denied.status_code == 401
    metrics = client.get("/metrics", headers={"Authorization": f"Bearer {SERVICE_KEY}"})
    assert metrics.status_code == 200
    assert "medicare_pending_reviews" in metrics.text
    assert 'medicare_cases{status="pending_review"}' in metrics.text
    assert "medicare_outbox_processing" in metrics.text


def test_request_ids_are_validated_and_unknown_routes_use_envelope(client: TestClient) -> None:
    kept = client.get("/health/live", headers={"X-Request-ID": "trace-123.abc"})
    assert kept.headers["X-Request-ID"] == "trace-123.abc"
    replaced = client.get("/health/live", headers={"X-Request-ID": "bad id\nInjected: yes"})
    assert replaced.headers["X-Request-ID"] != "bad id\nInjected: yes"
    assert len(replaced.headers["X-Request-ID"]) == 36
    missing = client.get("/does-not-exist")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "HTTP_404"


def test_full_api_workflow_uses_role_boundaries_and_envelopes(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    gateway = gateway_factory()
    caller = caller_factory()
    monkeypatch.setattr(main, "ModelGatewayClient", lambda _: gateway)
    monkeypatch.setattr(main, "CallerClient", lambda _: caller)

    created = client.post(
        "/api/v1/agent/runs",
        headers=SERVICE_HEADERS,
        json={"customer_id": "C00001", "idempotency_key": "api-workflow-001"},
    )
    assert created.status_code == 201
    case = created.json()["data"]
    assert case["status"] == "pending_review"
    assert case["snapshot"] is None

    detail = client.get(f"/api/v1/cases/{case['id']}", headers=REVIEWER_HEADERS)
    assert detail.status_code == 200
    assert detail.json()["data"]["snapshot"]["spo2_pct"] == 97
    audit = client.get(f"/api/v1/cases/{case['id']}/audit-verification", headers=REVIEWER_HEADERS)
    assert audit.json()["data"] == {
        "case_id": case["id"],
        "valid": True,
        "sealed": True,
        "event_count": 2,
        "reason": None,
    }

    newest = client.get(
        "/api/v1/cases", headers=REVIEWER_HEADERS, params={"order": "newest", "status": "approved"}
    )
    assert newest.json()["data"]["total"] == 0
    bad_status = client.get("/api/v1/cases", headers=REVIEWER_HEADERS, params={"status": "bogus"})
    assert bad_status.status_code == 422

    edited = "Synthetic reminder: please book your follow-up visit through the portal."
    review = client.post(
        f"/api/v1/cases/{case['id']}/reviews",
        headers=REVIEWER_HEADERS,
        json={
            "decision": "approve",
            "rationale": "Reviewed synthetic routine evidence for mock follow-up.",
            "expected_version": case["version"],
            "patient_message": edited,
        },
    )
    assert review.status_code == 200
    assert review.json()["data"]["dispatch_queued"] is True
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.case_id == case["id"]))
    assert event is not None and json.loads(event.payload_json)["message"] == edited

    dispatched = client.post("/api/v1/outbox/dispatch-one", headers=SERVICE_HEADERS)
    assert dispatched.json()["data"]["status"] == "delivered"
    empty = client.post("/api/v1/outbox/dispatch-one", headers=SERVICE_HEADERS)
    assert empty.json()["data"]["status"] == "empty"

    maintenance = client.post("/api/v1/agent/maintenance", headers=SERVICE_HEADERS)
    assert maintenance.json()["data"] == {"recovered_stale_runs": 0, "retry_case_ids": []}
    retry = client.post(f"/api/v1/agent/cases/{case['id']}/retry", headers=SERVICE_HEADERS)
    assert retry.status_code == 409
    assert retry.json()["error"]["code"] == "INVALID_STATE"

    missing = client.get("/api/v1/cases/not-a-case", headers=REVIEWER_HEADERS)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "HTTP_404"
    missing_audit = client.get(
        "/api/v1/cases/not-a-case/audit-verification", headers=REVIEWER_HEADERS
    )
    assert missing_audit.status_code == 404

    invalid = client.post(
        "/api/v1/agent/runs",
        headers=SERVICE_HEADERS,
        json={"customer_id": "not-valid", "idempotency_key": "short"},
    )
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"


def test_retry_endpoint_reruns_failed_provider_call(
    monkeypatch: pytest.MonkeyPatch,
    client: TestClient,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    gateways = iter([gateway_factory(fail=True), gateway_factory()])
    monkeypatch.setattr(main, "ModelGatewayClient", lambda _: next(gateways))
    failed = client.post(
        "/api/v1/agent/runs",
        headers=SERVICE_HEADERS,
        json={"customer_id": "C00001", "idempotency_key": "api-retry-0001"},
    ).json()["data"]
    assert failed["status"] == "provider_failed"
    retried = client.post(f"/api/v1/agent/cases/{failed['id']}/retry", headers=SERVICE_HEADERS)
    assert retried.status_code == 200
    assert retried.json()["data"]["status"] == "pending_review"
    assert retried.json()["data"]["provider_attempts"] == 2
