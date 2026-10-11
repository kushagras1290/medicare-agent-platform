from __future__ import annotations

import json
import logging
import sys
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from medicare_agent import auth, db
from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings
from medicare_agent.governance import (
    REQUIRED_RELEASE_GATES,
    ReleaseGateError,
    validate_release_manifest,
)
from medicare_agent.logging_config import JsonFormatter
from medicare_agent.models import ConsentEvidence, Customer, FollowUpCase, Observation, OutboxEvent
from medicare_agent.provider import ModelGatewayClient
from medicare_agent.schemas import ApiEnvelope, ReviewRequest
from medicare_agent.service import AgentService, DomainError, dispatch_one, review_case


class SessionContext(AbstractContextManager[Session]):
    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *_: object) -> None:
        return None


def test_oidc_subject_and_reviewer_credential_edges(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    auth._jwks_client.cache_clear()
    assert auth._jwks_client("https://id.example.test/jwks").uri == "https://id.example.test/jwks"

    oidc = settings.model_copy(
        update={
            "reviewer_auth_mode": "oidc",
            "oidc_issuer": "https://id.example.test/",
            "oidc_audience": "medicare-api",
            "oidc_jwks_url": "https://id.example.test/jwks",
        }
    )
    monkeypatch.setattr(
        auth,
        "_jwks_client",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda __: SimpleNamespace(key="unused")
        ),
    )
    monkeypatch.setattr(
        auth.jwt,
        "decode",
        lambda *_args, **_kwargs: {
            "sub": " ",
            "scope": "medicare:review",
            "amr": ["mfa"],
        },
    )
    with pytest.raises(HTTPException, match="subject"):
        auth._authenticate_oidc("token", oidc)
    with pytest.raises(HTTPException, match="bearer"):
        auth.require_reviewer(oidc, None)
    with pytest.raises(HTTPException, match="bearer"):
        auth.require_reviewer(oidc, HTTPAuthorizationCredentials(scheme="Basic", credentials="x"))
    monkeypatch.setattr(auth, "_authenticate_oidc", lambda token, _: f"subject:{token}")
    assert (
        auth.require_reviewer(
            oidc, HTTPAuthorizationCredentials(scheme="Bearer", credentials="valid")
        )
        == "subject:valid"
    )


def test_config_all_production_fail_closed_branches(tmp_path: Path) -> None:
    valid = {
        "environment": "production",
        "database_url": "postgresql+psycopg://user:pass@db/app",
        "service_api_key": "s" * 40,
        "model_gateway_api_key": "g" * 40,
        "caller_service_api_key": "c" * 40,
        "reviewer_auth_mode": "oidc",
        "oidc_issuer": "https://id.example.test/",
        "oidc_audience": "medicare-api",
        "oidc_jwks_url": "https://id.example.test/jwks",
        "database_url_file": tmp_path / "database",
        "service_api_key_file": tmp_path / "service",
        "model_gateway_api_key_file": tmp_path / "gateway",
        "caller_service_api_key_file": tmp_path / "caller",
        "enforce_https": True,
        "release_approvals_path": tmp_path / "approvals",
        "release_approvals_sha256": "a" * 64,
    }
    Path(str(valid["database_url_file"])).write_text(str(valid["database_url"]), encoding="utf-8")
    for key, value in (("service", "s"), ("gateway", "g"), ("caller", "c")):
        (tmp_path / key).write_text(value * 40, encoding="utf-8")

    cases = [
        ({"reviewer_auth_mode": "api_key"}, "OIDC reviewer"),
        ({"oidc_issuer": None}, "issuer"),
        ({"database_url_file": None}, "file-mounted"),
        ({"enforce_https": False}, "HTTPS"),
        ({"release_approvals_path": None}, "approvals manifest"),
        ({"release_approvals_sha256": "short"}, "SHA-256"),
    ]
    for overrides, message in cases:
        with pytest.raises(ValueError, match=message):
            Settings(**(valid | dict(overrides)))

    reviewer_file = tmp_path / "reviewers"
    reviewer_file.write_text("alice:alice-reviewer-token-long-enough", encoding="utf-8")
    assert Settings(reviewer_api_keys_file=reviewer_file).reviewers()["alice"]
    assert Settings(reviewer_api_keys=",alice:alice-reviewer-token-long-enough,").reviewers()[
        "alice"
    ]
    with pytest.raises(ValueError, match="At least one"):
        Settings(reviewer_api_keys=",").reviewers()


def test_config_secret_file_io_failures(tmp_path: Path) -> None:
    oversized = tmp_path / "oversized"
    oversized.write_bytes(b"x" * (64 * 1024 + 1))
    with pytest.raises(ValueError, match="small readable"):
        Settings._read_file(oversized, "TEST_FILE")

    class BrokenPath:
        def is_file(self) -> bool:
            return True

        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_size=1)

        def read_text(self, *, encoding: str) -> str:
            raise OSError("denied")

    with pytest.raises(ValueError, match="Unable to read"):
        Settings._read_file(BrokenPath(), "TEST_FILE")  # type: ignore[arg-type]


def test_database_dependency_commits_no_policy_and_rolls_back_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock()
    context = MagicMock()
    context.__enter__.return_value = fake
    context.__exit__.return_value = None
    monkeypatch.setattr(db, "SessionLocal", lambda: context)
    dependency = db.get_session()
    assert next(dependency) is fake
    dependency.close()

    dependency = db.get_session()
    assert next(dependency) is fake
    with pytest.raises(RuntimeError, match="boom"):
        dependency.throw(RuntimeError("boom"))
    fake.rollback.assert_called_once()


def test_json_formatter_includes_exception() -> None:
    try:
        raise RuntimeError("log failure")
    except RuntimeError:
        record = logging.LogRecord("test", logging.ERROR, __file__, 1, "failed", (), sys.exc_info())
    parsed = json.loads(JsonFormatter().format(record))
    assert "RuntimeError: log failure" in parsed["exception"]


def _approval_payload(now: datetime) -> dict[str, Any]:
    return {
        "release_id": "release-edge",
        "environment": "production",
        "generated_at": now.isoformat(),
        "approvals": [
            {
                "gate": gate,
                "status": "approved",
                "owner": "release owner",
                "approval_id": f"approval-{gate}",
                "approved_at": now.isoformat(),
                "expires_at": (now + timedelta(days=1)).isoformat(),
                "evidence_uri": f"urn:test:{gate}",
            }
            for gate in sorted(REQUIRED_RELEASE_GATES)
        ],
    }


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("approved_at", "2026-10-11T10:00:00", "timezone-missing"),
        ("approved_at", "2026-10-13T10:00:00+00:00", "future-approval"),
        ("expires_at", "2026-10-10T10:00:00+00:00", "expired"),
        ("evidence_uri", "file:///approval", "invalid-evidence-uri"),
    ],
)
def test_release_manifest_rejects_bad_approval_metadata(
    tmp_path: Path, field: str, value: str, message: str
) -> None:
    now = datetime(2026, 10, 11, 10, tzinfo=UTC)
    payload = _approval_payload(now)
    payload["approvals"][0][field] = value
    path = tmp_path / "approvals.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match=message):
        validate_release_manifest(path, expected_environment="production", now=now)


def test_api_envelope_rejects_ambiguous_shape() -> None:
    with pytest.raises(ValueError, match="Successful responses"):
        ApiEnvelope(success=True, error={"code": "bad", "message": "bad"})


def test_service_missing_real_and_consent_edge_paths(
    session: Session,
    add_customer,
    gateway_factory,
) -> None:
    service = AgentService(session, gateway_factory())
    with pytest.raises(DomainError, match="not found"):
        service.run("C99999", "missing-customer-1", "worker")

    add_customer("C00001", consent="synthetic_withheld")
    withheld = service.run("C00001", "withheld-consent-1", "worker")
    assert "consent_not_granted" in withheld.policy_holds

    add_customer("C00002")
    consent = session.query(ConsentEvidence).filter_by(customer_id="C00002").one()
    consent.expires_at = datetime.now(UTC) - timedelta(days=1)
    session.commit()
    expired = service.run("C00002", "expired-consent-1", "worker")
    assert "consent_evidence_expired" in expired.policy_holds

    customer = session.get(Customer, "C00002")
    assert customer is not None
    session.execute(text("PRAGMA ignore_check_constraints = ON"))
    customer.synthetic = False
    session.commit()
    with pytest.raises(DomainError, match="synthetic"):
        service.run("C00002", "real-data-1", "worker")


def test_concurrent_insert_conflict_paths(gateway_factory) -> None:
    customer = Customer(
        id="C00001",
        age=50,
        sex_at_birth="female",
        conditions_json='["test"]',
        missed_appointment=False,
        consent_status="synthetic_opted_in",
        last_contact_at=datetime.now(UTC),
        synthetic=True,
    )
    observation = Observation(
        customer_id="C00001",
        measured_at=datetime.now(UTC),
        bp_systolic_mm_hg=120,
        bp_diastolic_mm_hg=75,
        heart_rate_bpm=70,
        spo2_pct=97,
        temperature_c=36.8,
        respiratory_rate_bpm=16,
        weight_kg=70,
        bmi=25,
        creatinine_mg_dl=1,
        egfr_ml_min_1_73m2=80,
        glucose_mg_dl=100,
        hemoglobin_g_dl=13,
        sodium_mmol_l=140,
        potassium_mmol_l=4,
        phq9_score=2,
    )
    consent = ConsentEvidence(
        customer_id="C00001",
        purpose="mock_follow_up",
        version=1,
        status="synthetic_opted_in",
        source="test",
        captured_at=datetime.now(UTC),
        synthetic=True,
    )
    winner = FollowUpCase(
        id="winner-case",
        customer_id="C00001",
        idempotency_key="race-key-1",
        status="provider_pending",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
        provider_error=None,
        version=1,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    session = MagicMock()
    session.scalar.side_effect = [None, consent, winner]
    session.execute.return_value.one_or_none.return_value = (customer, observation)
    session.flush.side_effect = IntegrityError("insert", {}, Exception("race"))
    result = AgentService(session, gateway_factory()).run("C00001", "race-key-1", "worker")
    assert result.customer_id == "C00001"

    session = MagicMock()
    session.scalar.side_effect = [None, consent, None]
    session.execute.return_value.one_or_none.return_value = (customer, observation)
    session.flush.side_effect = IntegrityError("insert", {}, Exception("race"))
    with pytest.raises(DomainError, match="Concurrent"):
        AgentService(session, gateway_factory()).run("C00001", "race-key-2", "worker")


def test_review_and_dispatch_defensive_states(
    session: Session,
    add_customer,
    gateway_factory,
    caller_factory,
) -> None:
    with pytest.raises(DomainError, match="not found"):
        review_case(
            session,
            "missing",
            ReviewRequest(
                decision="hold", rationale="Missing case cannot be reviewed.", expected_version=1
            ),
            "reviewer",
        )
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "reject-path-1", "worker")
    rejected = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="reject",
            rationale="Reject this synthetic proposal after review.",
            expected_version=case.version,
        ),
        "reviewer",
    )
    assert rejected.case.status == "rejected"
    with pytest.raises(DomainError, match="cannot be reviewed"):
        review_case(
            session,
            case.id,
            ReviewRequest(
                decision="hold",
                rationale="A second decision is invalid.",
                expected_version=case.version + 1,
            ),
            "reviewer",
        )

    approved = FollowUpCase(
        customer_id="C00001",
        idempotency_key="unsafe-payload-1",
        status="approved",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
    )
    session.add(approved)
    session.flush()
    event = OutboxEvent(
        case_id=approved.id,
        event_type="mock_follow_up_requested",
        payload_json=json.dumps({"synthetic": False, "channel": "phone", "message": "unsafe"}),
    )
    session.add(event)
    session.commit()
    with pytest.raises(DomainError, match="synthetic mock"):
        dispatch_one(session, "worker", caller_factory())
    assert event.status == "dead_letter"


def test_caller_dead_letters_after_max_attempt(
    session: Session, add_customer, gateway_factory, caller_factory
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "dead-letter-caller-1", "worker")
    review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Approve synthetic caller failure test.",
            expected_version=case.version,
        ),
        "reviewer",
    )
    caller = caller_factory(fail=True)
    caller.settings.caller_max_attempts = 1
    result = dispatch_one(session, "worker", caller)
    assert result.status == "dead_letter"


def test_claim_lost_to_another_dispatcher_returns_empty(caller_factory) -> None:
    candidate = SimpleNamespace(id="event-1")
    session = MagicMock()
    session.scalars.return_value.all.return_value = [candidate]
    session.execute.return_value.rowcount = 0
    result = dispatch_one(session, "worker", caller_factory())
    assert result.status == "empty"
    session.rollback.assert_called_once()


@pytest.mark.parametrize("client_type", ["model", "caller"])
def test_http_clients_retry_server_errors(
    client_type: str, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        if client_type == "model":
            return httpx.Response(
                200,
                json={
                    "proposal": {
                        "summary": "Synthetic retry response was successfully generated.",
                        "recommended_action": (
                            "Route this synthetic follow-up through a human reviewer."
                        ),
                        "contact_channel": "portal",
                        "reasoning_bullets": ["synthetic retry evidence"],
                        "requires_human_review": True,
                    },
                    "provider": "retry-test",
                    "model": "retry-test",
                },
            )
        return httpx.Response(
            200,
            json={"external_id": "mock-call-retry", "status": "accepted", "provider": "retry-test"},
        )

    monkeypatch.setattr("time.sleep", lambda _: None)
    retry_settings = settings.model_copy(
        update={"model_gateway_max_retries": 1, "caller_max_retries": 1}
    )
    transport = httpx.MockTransport(handler)
    if client_type == "model":
        from medicare_agent.schemas import (
            CustomerSnapshot,
            GatewayProposalRequest,
            RiskTier,
            RuleAssessment,
        )

        snapshot = CustomerSnapshot(
            customer_id="C00001",
            age=50,
            sex_at_birth="female",
            conditions=["test"],
            missed_appointment=False,
            consent_status="synthetic_opted_in",
            last_contact_at=datetime.now(UTC),
            measured_at=datetime.now(UTC),
            bp_systolic_mm_hg=120,
            bp_diastolic_mm_hg=75,
            heart_rate_bpm=70,
            spo2_pct=97,
            temperature_c=36.8,
            respiratory_rate_bpm=16,
            weight_kg=70,
            bmi=25,
            creatinine_mg_dl=1,
            egfr_ml_min_1_73m2=80,
            glucose_mg_dl=100,
            hemoglobin_g_dl=13,
            sodium_mmol_l=140,
            potassium_mmol_l=4,
            phq9_score=2,
        )
        ModelGatewayClient(retry_settings, transport).propose(
            GatewayProposalRequest(
                case_id="case-1",
                snapshot=snapshot,
                rule_assessment=RuleAssessment(tier=RiskTier.ROUTINE, reasons=["test"]),
            )
        )
    else:
        from medicare_agent.schemas import CallerRequest

        CallerClient(retry_settings, transport).place_call(
            CallerRequest(
                case_id="case-1",
                customer_id="C00001",
                channel="phone",
                message="Synthetic approved retry call.",
                synthetic=True,
                approved_by_human=True,
            ),
            "event-1",
        )
    assert attempts == 2
