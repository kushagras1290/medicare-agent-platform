from __future__ import annotations

from collections.abc import Callable, Generator

from sqlalchemy.orm import Session
from starlette.testclient import TestClient

from medicare_agent import main
from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings, get_settings
from medicare_agent.db import get_session
from medicare_agent.main import app
from medicare_agent.provider import ModelGatewayClient


def test_dashboard_and_health_are_available(
    session: Session,
    settings: Settings,
) -> None:
    def override_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        dashboard = client.get("/")
        assert dashboard.status_code == 200
        assert "Simulation only" in dashboard.text
        ready = client.get("/health/ready")
        assert ready.status_code == 200
        assert ready.json()["human_review_required"] is True
    finally:
        app.dependency_overrides.clear()


def test_https_enforcement_fails_closed(
    monkeypatch,
    session: Session,
    settings: Settings,
) -> None:
    def override_session() -> Generator[Session, None, None]:
        yield session

    secure_settings = settings.model_copy(update={"enforce_https": True})
    monkeypatch.setattr(main, "get_settings", lambda: secure_settings)
    app.dependency_overrides[get_session] = override_session
    client = TestClient(app)
    try:
        response = client.get("/health/live")
        assert response.status_code == 400
        assert response.json()["error"]["code"] == "HTTPS_REQUIRED"
        allowed = client.get("/health/live", headers={"X-Forwarded-Proto": "https"})
        assert allowed.status_code == 200
        assert "Strict-Transport-Security" in allowed.headers
    finally:
        app.dependency_overrides.clear()


def test_reviewer_routes_reject_missing_key(
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
) -> None:
    add_customer("C00001")

    def override_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        response = client.get("/api/v1/cases")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "HTTP_401"
        allowed = client.get(
            "/api/v1/cases",
            headers={"X-Reviewer-Key": "test-reviewer-key-at-least-32-characters"},
        )
        assert allowed.status_code == 200
        assert allowed.json()["success"] is True
    finally:
        app.dependency_overrides.clear()


def test_service_routes_reject_reviewer_key(session: Session, settings: Settings) -> None:
    def override_session() -> Generator[Session, None, None]:
        yield session

    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    try:
        response = client.post(
            "/api/v1/outbox/dispatch-one",
            headers={"X-Reviewer-Key": "test-reviewer-key-at-least-32-characters"},
        )
        assert response.status_code == 401
        assert response.json()["success"] is False
        metrics_denied = client.get("/metrics")
        assert metrics_denied.status_code == 401
        metrics = client.get(
            "/metrics", headers={"Authorization": "Bearer test-service-key-at-least-32-characters"}
        )
        assert metrics.status_code == 200
        assert "medicare_pending_reviews" in metrics.text
    finally:
        app.dependency_overrides.clear()


def test_full_api_workflow_uses_role_boundaries_and_envelopes(
    monkeypatch,
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")

    def override_session() -> Generator[Session, None, None]:
        yield session

    gateway = gateway_factory()
    caller = caller_factory()
    monkeypatch.setattr(main, "ModelGatewayClient", lambda _: gateway)
    monkeypatch.setattr(main, "CallerClient", lambda _: caller)
    app.dependency_overrides[get_session] = override_session
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app)
    service_headers = {"X-Service-Key": "test-service-key-at-least-32-characters"}
    reviewer_headers = {"X-Reviewer-Key": "test-reviewer-key-at-least-32-characters"}
    try:
        created = client.post(
            "/api/v1/agent/runs",
            headers=service_headers,
            json={"customer_id": "C00001", "idempotency_key": "api-workflow-001"},
        )
        assert created.status_code == 201
        case = created.json()["data"]
        assert case["status"] == "pending_review"

        detail = client.get(f"/api/v1/cases/{case['id']}", headers=reviewer_headers)
        assert detail.status_code == 200
        audit = client.get(
            f"/api/v1/cases/{case['id']}/audit-verification", headers=reviewer_headers
        )
        assert audit.json()["data"]["valid"] is True

        review = client.post(
            f"/api/v1/cases/{case['id']}/reviews",
            headers=reviewer_headers,
            json={
                "decision": "approve",
                "rationale": "Reviewed synthetic routine evidence for mock follow-up.",
                "expected_version": case["version"],
            },
        )
        assert review.status_code == 200
        assert review.json()["data"]["dispatch_queued"] is True

        dispatched = client.post("/api/v1/outbox/dispatch-one", headers=service_headers)
        assert dispatched.json()["data"]["status"] == "delivered"
        empty = client.post("/api/v1/outbox/dispatch-one", headers=service_headers)
        assert empty.json()["data"]["status"] == "empty"

        missing = client.get("/api/v1/cases/not-a-case", headers=reviewer_headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "HTTP_404"

        invalid = client.post(
            "/api/v1/agent/runs",
            headers=service_headers,
            json={"customer_id": "not-valid", "idempotency_key": "short"},
        )
        assert invalid.status_code == 422
        assert invalid.json()["error"]["code"] == "VALIDATION_ERROR"
    finally:
        app.dependency_overrides.clear()
