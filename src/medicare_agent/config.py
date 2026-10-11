from __future__ import annotations

import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Validated runtime configuration with fail-closed production gates."""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    database_url: str = "sqlite:///./data/medicare.db"
    database_url_file: Path | None = None
    service_api_key: SecretStr = SecretStr("local-service-key-change-me-32-chars")
    service_api_key_file: Path | None = None
    previous_service_api_keys: str = ""
    reviewer_api_keys: str = "reviewer-demo:local-reviewer-key-change-me-32-chars"
    reviewer_api_keys_file: Path | None = None
    reviewer_auth_mode: Literal["api_key", "oidc"] = "api_key"
    oidc_issuer: str | None = None
    oidc_audience: str | None = None
    oidc_jwks_url: str | None = None
    oidc_required_scope: str = "medicare:review"
    oidc_require_mfa: bool = True
    model_gateway_url: str = "http://127.0.0.1:8091"
    model_gateway_api_key: SecretStr = SecretStr("local-gateway-key-change-me-32-chars")
    model_gateway_api_key_file: Path | None = None
    model_gateway_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    model_gateway_max_retries: int = Field(default=2, ge=0, le=5)
    caller_service_url: str = "http://127.0.0.1:8092"
    caller_service_api_key: SecretStr = SecretStr("local-caller-key-change-me-32-chars")
    caller_service_api_key_file: Path | None = None
    caller_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    caller_max_retries: int = Field(default=2, ge=0, le=5)
    caller_max_attempts: int = Field(default=5, ge=1, le=20)
    api_internal_url: str = "http://127.0.0.1:8000"
    worker_poll_seconds: float = Field(default=5.0, ge=0.5, le=60.0)
    automation_batch_size: int = Field(default=20, ge=1, le=100)
    allow_real_patient_data: bool = False
    allow_live_dispatch: bool = False
    require_human_review: bool = True
    release_approvals_path: Path | None = None
    release_approvals_sha256: str | None = None
    retention_days: int = Field(default=365, ge=30, le=3650)
    allowed_hosts: str = "127.0.0.1,localhost,testserver"
    enforce_https: bool = False

    @staticmethod
    def _read_file(path: Path, label: str) -> str:
        try:
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                raise ValueError(f"{label} must reference a small readable file")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Unable to read {label}") from exc
        if not value:
            raise ValueError(f"{label} cannot be empty")
        return value

    @model_validator(mode="after")
    def enforce_safety_gates(self) -> Settings:
        if self.database_url_file:
            self.database_url = self._read_file(self.database_url_file, "DATABASE_URL_FILE")
        if self.service_api_key_file:
            self.service_api_key = SecretStr(
                self._read_file(self.service_api_key_file, "SERVICE_API_KEY_FILE")
            )
        if self.reviewer_api_keys_file:
            self.reviewer_api_keys = self._read_file(
                self.reviewer_api_keys_file, "REVIEWER_API_KEYS_FILE"
            )
        if self.model_gateway_api_key_file:
            self.model_gateway_api_key = SecretStr(
                self._read_file(self.model_gateway_api_key_file, "MODEL_GATEWAY_API_KEY_FILE")
            )
        if self.caller_service_api_key_file:
            self.caller_service_api_key = SecretStr(
                self._read_file(self.caller_service_api_key_file, "CALLER_SERVICE_API_KEY_FILE")
            )
        if self.allow_real_patient_data:
            raise ValueError("Real-patient data is not supported by this synthetic demo build")
        if self.allow_live_dispatch:
            raise ValueError("Live dispatch is not implemented or approved")
        if not self.require_human_review:
            raise ValueError("Human review cannot be disabled")
        if self.environment in {"staging", "production"}:
            secrets_to_check = (
                self.service_api_key.get_secret_value(),
                self.model_gateway_api_key.get_secret_value(),
                self.caller_service_api_key.get_secret_value(),
            )
            if any("change-me" in value or len(value) < 32 for value in secrets_to_check):
                raise ValueError(
                    "Staging/production requires non-default secrets of at least 32 chars"
                )
            if self.database_url.startswith("sqlite"):
                raise ValueError("Staging/production requires a managed PostgreSQL database")
            if self.reviewer_auth_mode != "oidc":
                raise ValueError("Staging/production requires OIDC reviewer authentication")
            if not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)):
                raise ValueError("OIDC issuer, audience and JWKS URL are required")
            if not all(
                (
                    self.database_url_file,
                    self.service_api_key_file,
                    self.model_gateway_api_key_file,
                    self.caller_service_api_key_file,
                )
            ):
                raise ValueError("Staging/production secrets must be file-mounted")
            if not self.enforce_https:
                raise ValueError("Staging/production requires HTTPS enforcement")
            if self.release_approvals_path is None:
                raise ValueError("Staging/production requires a release approvals manifest")
            if not self.release_approvals_sha256 or len(self.release_approvals_sha256) != 64:
                raise ValueError("Staging/production requires the approvals manifest SHA-256")
        return self

    def service_keys(self) -> list[str]:
        keys = [self.service_api_key.get_secret_value()]
        keys.extend(key.strip() for key in self.previous_service_api_keys.split(",") if key.strip())
        if any(len(key) < 32 for key in keys):
            raise ValueError("Every service key must contain at least 32 characters")
        return keys

    def reviewers(self) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for item in self.reviewer_api_keys.split(","):
            if not item.strip():
                continue
            reviewer_id, separator, token = item.partition(":")
            if not separator or not reviewer_id.strip() or len(token.strip()) < 16:
                raise ValueError("REVIEWER_API_KEYS must contain reviewer_id:token entries")
            parsed[reviewer_id.strip()] = token.strip()
        if not parsed:
            raise ValueError("At least one reviewer API key is required")
        return parsed

    def authenticate_reviewer(self, token: str) -> str | None:
        for reviewer_id, expected in self.reviewers().items():
            if secrets.compare_digest(token, expected):
                return reviewer_id
        return None

    def hosts(self) -> list[str]:
        hosts = [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]
        if not hosts or "*" in hosts:
            raise ValueError("ALLOWED_HOSTS must contain explicit hosts")
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
