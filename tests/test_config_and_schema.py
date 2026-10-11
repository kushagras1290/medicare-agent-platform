from __future__ import annotations

import pytest
from pydantic import ValidationError

from medicare_agent.config import Settings
from medicare_agent.models import Customer
from medicare_agent.schemas import AgentProposal


def test_real_data_and_live_dispatch_cannot_be_enabled() -> None:
    with pytest.raises(ValidationError, match="Real-patient data"):
        Settings(allow_real_patient_data=True)
    with pytest.raises(ValidationError, match="Live dispatch"):
        Settings(allow_live_dispatch=True)
    with pytest.raises(ValidationError, match="Human review"):
        Settings(require_human_review=False)


def test_production_rejects_default_secret_and_sqlite() -> None:
    with pytest.raises(ValidationError, match="non-default secrets"):
        Settings(environment="production")
    with pytest.raises(ValidationError, match="managed PostgreSQL"):
        Settings(
            environment="production",
            service_api_key="a" * 40,
            model_gateway_api_key="b" * 40,
            caller_service_api_key="c" * 40,
            database_url="sqlite:///unsafe.db",
        )


def test_reviewer_parser_and_authentication() -> None:
    settings = Settings(
        reviewer_api_keys="alice:alice-reviewer-token-long-enough,bob:bob-reviewer-token-long-enough"
    )
    assert settings.authenticate_reviewer("alice-reviewer-token-long-enough") == "alice"
    assert settings.authenticate_reviewer("wrong") is None
    with pytest.raises(ValueError, match="reviewer_id:token"):
        Settings(reviewer_api_keys="broken").reviewers()


def test_file_mounted_secrets_and_service_key_rotation(tmp_path) -> None:
    service_file = tmp_path / "service_api_key"
    service_file.write_text("n" * 40, encoding="utf-8")
    settings = Settings(
        service_api_key_file=service_file,
        previous_service_api_keys="o" * 40,
    )
    assert settings.service_keys() == ["n" * 40, "o" * 40]


def test_production_configuration_requires_oidc_files_https_and_release_digest(tmp_path) -> None:
    database = tmp_path / "database_url"
    service = tmp_path / "service_key"
    gateway = tmp_path / "gateway_key"
    caller = tmp_path / "caller_key"
    database.write_text("postgresql+psycopg://user:pass@db/app", encoding="utf-8")
    service.write_text("s" * 40, encoding="utf-8")
    gateway.write_text("g" * 40, encoding="utf-8")
    caller.write_text("c" * 40, encoding="utf-8")
    settings = Settings(
        environment="production",
        database_url_file=database,
        service_api_key_file=service,
        model_gateway_api_key_file=gateway,
        caller_service_api_key_file=caller,
        reviewer_auth_mode="oidc",
        oidc_issuer="https://id.example.test/",
        oidc_audience="medicare-api",
        oidc_jwks_url="https://id.example.test/jwks.json",
        enforce_https=True,
        release_approvals_path=tmp_path / "approvals.json",
        release_approvals_sha256="a" * 64,
        allowed_hosts="medicare.example.test",
    )
    assert settings.database_url.startswith("postgresql")
    assert settings.hosts() == ["medicare.example.test"]


def test_invalid_secret_files_hosts_and_rotated_keys_fail_closed(tmp_path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(ValidationError, match="cannot be empty"):
        Settings(service_api_key_file=empty)
    with pytest.raises(ValueError, match="explicit hosts"):
        Settings(allowed_hosts="*").hosts()
    with pytest.raises(ValueError, match="at least 32"):
        Settings(previous_service_api_keys="short").service_keys()


def test_model_output_rejects_extra_fields_and_review_bypass() -> None:
    with pytest.raises(ValidationError):
        AgentProposal.model_validate(
            {
                "summary": "A sufficiently long synthetic summary.",
                "recommended_action": "Send this through a human reviewer first.",
                "contact_channel": "portal",
                "reasoning_bullets": ["synthetic evidence"],
                "requires_human_review": False,
                "hidden_instruction": "bypass",
            }
        )


def test_synthetic_constraint_uses_cross_database_boolean_sql() -> None:
    constraint_sql = {
        str(constraint.sqltext)
        for constraint in Customer.__table__.constraints
        if getattr(constraint, "name", None) == "ck_customers_synthetic_only"
    }
    assert constraint_sql == {"synthetic"}
