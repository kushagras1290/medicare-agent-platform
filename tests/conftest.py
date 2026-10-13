from __future__ import annotations

import json
import os
from collections.abc import Callable, Generator
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings
from medicare_agent.db import Base, enable_sqlite_foreign_keys
from medicare_agent.models import ConsentEvidence, Customer, Observation
from medicare_agent.provider import ModelGatewayClient
from tests.factories import REVIEWER_KEY, SAFE_PATIENT_MESSAGE, SERVICE_KEY, make_proposal


@pytest.fixture
def settings() -> Settings:
    return Settings(
        environment="test",
        database_url="sqlite://",
        service_api_key=SERVICE_KEY,
        reviewer_api_keys=f"reviewer-1:{REVIEWER_KEY}",
        audit_hmac_key="test-audit-key-at-least-32-characters",
        model_gateway_url="http://gateway.test",
        model_gateway_api_key="test-gateway-key-at-least-32-characters",
        model_gateway_max_retries=0,
        caller_service_url="http://caller.test",
        caller_service_api_key="test-caller-key-at-least-32-characters",
        caller_max_retries=0,
    )


def _test_engine() -> Engine:
    # TEST_DATABASE_URL runs the suite against a disposable PostgreSQL database; the default is
    # an in-memory SQLite database shared across the test's connections.
    url = os.getenv("TEST_DATABASE_URL")
    if url:
        return create_engine(url)
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    enable_sqlite_foreign_keys(engine)
    return engine


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    if not os.getenv("TEST_DATABASE_URL"):
        return
    skip = pytest.mark.skip(reason="relies on SQLite-only behaviour")
    for item in items:
        if "sqlite_only" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def session() -> Generator[Session, None, None]:
    engine = _test_engine()
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as db_session:
        yield db_session
    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def add_customer(session: Session) -> Callable[..., Customer]:
    def _add(
        customer_id: str,
        *,
        urgent: bool = False,
        consent: str = "synthetic_opted_in",
        missed: bool = False,
        bp_inversion: bool = False,
    ) -> Customer:
        customer = Customer(
            id=customer_id,
            age=62,
            sex_at_birth="female",
            conditions_json=json.dumps(["preventive_follow_up"]),
            missed_appointment=missed,
            consent_status=consent,
            last_contact_at=datetime(2026, 9, 1, tzinfo=UTC),
            synthetic=True,
        )
        observation = Observation(
            customer_id=customer_id,
            measured_at=datetime(2026, 10, 1, tzinfo=UTC),
            bp_systolic_mm_hg=120,
            bp_diastolic_mm_hg=125 if bp_inversion else 75,
            heart_rate_bpm=74,
            spo2_pct=86 if urgent else 97,
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
        consent_evidence = ConsentEvidence(
            customer_id=customer_id,
            purpose="mock_follow_up",
            version=1,
            status=consent,
            source="synthetic_test_fixture",
            captured_at=datetime(2026, 10, 1, tzinfo=UTC),
            synthetic=True,
        )
        session.add(customer)
        session.flush()
        session.add_all([observation, consent_evidence])
        session.commit()
        return customer

    return _add


@pytest.fixture
def gateway_factory(settings: Settings) -> Callable[..., ModelGatewayClient]:
    def _make(
        *,
        prohibited: bool = False,
        fail: bool = False,
        malformed: bool = False,
        status_code: int | None = None,
        patient_message: str = SAFE_PATIENT_MESSAGE,
    ) -> ModelGatewayClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if fail:
                raise httpx.ConnectError("simulated outage", request=request)
            assert (
                request.headers["X-Gateway-Key"]
                == settings.model_gateway_api_key.get_secret_value()
            )
            if status_code is not None:
                return httpx.Response(status_code)
            if malformed:
                return httpx.Response(200, json={"unexpected": True})
            action = (
                "Increase the dose immediately without waiting for a human review."
                if prohibited
                else "A human reviewer should approve a routine mock portal follow-up."
            )
            proposal = make_proposal(recommended_action=action, patient_message=patient_message)
            return httpx.Response(
                200,
                json={
                    "proposal": proposal.model_dump(mode="json"),
                    "provider": "test-provider",
                    "model": "test-model",
                    "request_id": "provider-request-1",
                },
            )

        return ModelGatewayClient(settings, transport=httpx.MockTransport(handler))

    return _make


@pytest.fixture
def caller_factory(settings: Settings) -> Callable[..., CallerClient]:
    def _make(*, fail: bool = False, status_code: int | None = None) -> CallerClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if fail:
                raise httpx.ConnectError("simulated caller outage", request=request)
            assert (
                request.headers["X-Caller-Key"]
                == settings.caller_service_api_key.get_secret_value()
            )
            assert request.headers["Idempotency-Key"]
            if status_code is not None:
                return httpx.Response(status_code)
            return httpx.Response(
                200,
                json={
                    "external_id": f"mock-call-{request.headers['Idempotency-Key']}",
                    "status": "accepted",
                    "provider": "test-caller",
                },
            )

        return CallerClient(settings, transport=httpx.MockTransport(handler))

    return _make
