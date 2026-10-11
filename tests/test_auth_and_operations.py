from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from sqlalchemy.orm import Session

from medicare_agent import auth
from medicare_agent.config import Settings
from medicare_agent.models import FollowUpCase, WorkerHeartbeat
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

    with pytest.raises(HTTPException) as error:
        auth._authenticate_oidc("not-a-jwt", settings)
    assert error.value.status_code == 401


def test_worker_heartbeat_upserts(session: Session) -> None:
    record_worker_heartbeat(session, "automation", processed=4)
    record_worker_heartbeat(session, "automation", processed=7)
    heartbeat = session.get(WorkerHeartbeat, "automation")
    assert heartbeat is not None
    assert heartbeat.processed == 7


def test_retention_is_dry_run_by_default_and_requires_execute(session: Session) -> None:
    old = datetime.now(UTC) - timedelta(days=500)
    case = FollowUpCase(
        customer_id="C00001",
        idempotency_key="retention-case-1",
        status="rejected",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
        created_at=old,
        updated_at=old,
    )
    session.add(case)
    session.commit()
    preview = retention_sweep(
        session, before=datetime.now(UTC) - timedelta(days=365), execute=False
    )
    assert preview.eligible_cases == 1
    assert session.get(FollowUpCase, case.id) is not None
    result = retention_sweep(session, before=datetime.now(UTC) - timedelta(days=365), execute=True)
    assert result.deleted_cases == 1
    assert session.get(FollowUpCase, case.id) is None
