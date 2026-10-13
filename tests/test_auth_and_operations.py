from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from medicare_agent import auth
from medicare_agent.audit import append_audit
from medicare_agent.config import Settings
from medicare_agent.models import AuditEvent, AuditHead, FollowUpCase, WorkerHeartbeat
from medicare_agent.operations import record_worker_heartbeat, retention_sweep


def test_oidc_verifies_signature_claims_scope_and_mfa(monkeypatch: pytest.MonkeyPatch) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    settings = Settings(
        reviewer_auth_mode="oidc",
        oidc_issuer="https://id.example.test/",
        oidc_audience="medicare-api",
        oidc_jwks_url="https://id.example.test/.well-known/jwks.json",
    )
    monkeypatch.setattr(
        auth,
        "_jwks_client",
        lambda _: SimpleNamespace(
            get_signing_key_from_jwt=lambda __: SimpleNamespace(key=public_key)
        ),
    )
    now = datetime.now(UTC)
    claims = {
        "sub": "reviewer-oidc-1",
        "iss": settings.oidc_issuer,
        "aud": settings.oidc_audience,
        "iat": now,
        "exp": now + timedelta(minutes=5),
        "scope": "openid medicare:review",
        "amr": ["pwd", "mfa"],
    }
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
    assert auth._authenticate_oidc(token, settings) == "reviewer-oidc-1"

    claims["scope"] = "openid"
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(HTTPException) as error:
        auth._authenticate_oidc(token, settings)
    assert error.value.status_code == 403

    claims["scope"] = "medicare:review"
    claims["amr"] = ["pwd"]
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
    with pytest.raises(HTTPException, match="MFA") as error:
        auth._authenticate_oidc(token, settings)
    assert error.value.status_code == 403

    claims["amr"] = ["mfa"]
    del claims["scope"]
    claims["scp"] = ["openid", "medicare:review"]
    token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
    assert auth._authenticate_oidc(token, settings) == "reviewer-oidc-1"

    with pytest.raises(HTTPException) as error:
        auth._authenticate_oidc("not-a-jwt", settings)
    assert error.value.status_code == 401

    unconfigured = settings.model_copy(update={"oidc_jwks_url": None})
    with pytest.raises(HTTPException) as error:
        auth._authenticate_oidc(token, unconfigured)
    assert error.value.status_code == 503


def test_worker_heartbeat_upserts(session: Session) -> None:
    record_worker_heartbeat(session, "automation", processed=4)
    record_worker_heartbeat(session, "automation", processed=7)
    heartbeat = session.get(WorkerHeartbeat, "automation")
    assert heartbeat is not None
    assert heartbeat.processed == 7


def _closed_case(case_id: str, status: str, *, attempts: int = 1) -> FollowUpCase:
    old = datetime.now(UTC) - timedelta(days=500)
    return FollowUpCase(
        id=case_id,
        customer_id="C00001",
        idempotency_key=f"retention-{case_id}",
        status=status,
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
        provider_attempts=attempts,
        created_at=old,
        updated_at=old,
    )


def test_retention_is_dry_run_by_default_and_requires_execute(
    session: Session, add_customer: Callable[..., object]
) -> None:
    add_customer("C00001")
    session.add_all(
        [
            _closed_case("rejected-case", "rejected"),
            _closed_case("closed-case", "closed"),
            _closed_case("exhausted-case", "provider_failed", attempts=3),
            _closed_case("retryable-case", "provider_failed", attempts=1),
            _closed_case("escalated-case", "human_escalated"),
        ]
    )
    session.commit()
    append_audit(
        session,
        aggregate_type="follow_up_case",
        aggregate_id="rejected-case",
        event_type="test_event",
        actor_type="service",
        actor_id="test",
        data={},
    )
    session.commit()
    cutoff = datetime.now(UTC) - timedelta(days=365)
    preview = retention_sweep(session, before=cutoff, execute=False, provider_max_attempts=3)
    assert preview.eligible_cases == 3
    assert session.get(FollowUpCase, "rejected-case") is not None

    result = retention_sweep(session, before=cutoff, execute=True, provider_max_attempts=3)
    assert result.deleted_cases == 3
    remaining = set(session.scalars(select(FollowUpCase.id)))
    assert remaining == {"retryable-case", "escalated-case"}
    assert session.get(AuditHead, ("follow_up_case", "rejected-case")) is None
    sweep_event = session.scalar(
        select(AuditEvent).where(AuditEvent.aggregate_type == "retention_sweep")
    )
    assert sweep_event is not None
    assert "rejected-case" in sweep_event.data_json
