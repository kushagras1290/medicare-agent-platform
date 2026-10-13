from __future__ import annotations

import secrets
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from medicare_agent.http_retry import retry_budget_seconds
from medicare_agent.secret_files import MIN_SECRET_LENGTH, is_weak_secret, read_secret_file

STALE_RUN_MARGIN_SECONDS = 30.0
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


@dataclass(frozen=True)
class OidcBrowserConfig:
    """Public OIDC values the reviewer console needs for an authorization-code + PKCE login."""

    client_id: str
    authorization_url: str
    token_url: str
    scope: str

    @property
    def token_origin(self) -> str:
        parts = urlsplit(self.token_url)
        return f"{parts.scheme}://{parts.netloc}"


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
    oidc_client_id: str | None = None
    oidc_authorization_url: str | None = None
    oidc_token_url: str | None = None
    audit_hmac_key: SecretStr = SecretStr("local-audit-key-change-me-32-chars")
    audit_hmac_key_file: Path | None = None
    model_gateway_url: str = "http://127.0.0.1:8091"
    model_gateway_api_key: SecretStr = SecretStr("local-gateway-key-change-me-32-chars")
    model_gateway_api_key_file: Path | None = None
    model_gateway_timeout_seconds: float = Field(default=40.0, ge=1.0, le=120.0)
    model_gateway_max_retries: int = Field(default=2, ge=0, le=5)
    provider_max_attempts: int = Field(default=3, ge=1, le=10)
    provider_retry_delay_seconds: float = Field(default=60.0, ge=5.0, le=3600.0)
    provider_stale_seconds: float = Field(default=600.0, ge=60.0, le=86400.0)
    caller_service_url: str = "http://127.0.0.1:8092"
    caller_service_api_key: SecretStr = SecretStr("local-caller-key-change-me-32-chars")
    caller_service_api_key_file: Path | None = None
    caller_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    caller_max_retries: int = Field(default=2, ge=0, le=5)
    caller_max_attempts: int = Field(default=5, ge=1, le=20)
    outbox_lease_seconds: float = Field(default=300.0, ge=30.0, le=3600.0)
    dispatch_batch_limit: int = Field(default=25, ge=1, le=500)
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
        return read_secret_file(path, label)

    def _load_file_secrets(self) -> None:
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
        if self.audit_hmac_key_file:
            self.audit_hmac_key = SecretStr(
                self._read_file(self.audit_hmac_key_file, "AUDIT_HMAC_KEY_FILE")
            )
        if self.model_gateway_api_key_file:
            self.model_gateway_api_key = SecretStr(
                self._read_file(self.model_gateway_api_key_file, "MODEL_GATEWAY_API_KEY_FILE")
            )
        if self.caller_service_api_key_file:
            self.caller_service_api_key = SecretStr(
                self._read_file(self.caller_service_api_key_file, "CALLER_SERVICE_API_KEY_FILE")
            )

    def _enforce_timing_invariants(self) -> None:
        if self.provider_stale_seconds < self.model_gateway_budget_seconds() + (
            STALE_RUN_MARGIN_SECONDS
        ):
            raise ValueError(
                "PROVIDER_STALE_SECONDS must exceed the model gateway timeout and retry budget"
            )
        if self.outbox_lease_seconds < self.caller_budget_seconds() + STALE_RUN_MARGIN_SECONDS:
            raise ValueError("OUTBOX_LEASE_SECONDS must exceed the caller timeout and retry budget")

    def _enforce_deployed_gates(self) -> None:
        secrets_to_check = (
            self.service_api_key.get_secret_value(),
            self.model_gateway_api_key.get_secret_value(),
            self.caller_service_api_key.get_secret_value(),
            self.audit_hmac_key.get_secret_value(),
        )
        if any(is_weak_secret(value) for value in secrets_to_check):
            raise ValueError("Staging/production requires non-default secrets of at least 32 chars")
        if self.database_url.startswith("sqlite"):
            raise ValueError("Staging/production requires a managed PostgreSQL database")
        if self.reviewer_auth_mode != "oidc":
            raise ValueError("Staging/production requires OIDC reviewer authentication")
        if not all((self.oidc_issuer, self.oidc_audience, self.oidc_jwks_url)):
            raise ValueError("OIDC issuer, audience and JWKS URL are required")
        self.oidc_browser_config()
        if not all(
            (
                self.database_url_file,
                self.service_api_key_file,
                self.model_gateway_api_key_file,
                self.caller_service_api_key_file,
                self.audit_hmac_key_file,
            )
        ):
            raise ValueError("Staging/production secrets must be file-mounted")
        if not self.enforce_https:
            raise ValueError("Staging/production requires HTTPS enforcement")
        if self.release_approvals_path is None:
            raise ValueError("Staging/production requires a release approvals manifest")
        if not self.release_approvals_sha256 or len(self.release_approvals_sha256) != 64:
            raise ValueError("Staging/production requires the approvals manifest SHA-256")

    @model_validator(mode="after")
    def enforce_safety_gates(self) -> Settings:
        self._load_file_secrets()
        if self.allow_real_patient_data:
            raise ValueError("Real-patient data is not supported by this synthetic demo build")
        if self.allow_live_dispatch:
            raise ValueError("Live dispatch is not implemented or approved")
        if not self.require_human_review:
            raise ValueError("Human review cannot be disabled")
        self._enforce_timing_invariants()
        if self.environment in {"staging", "production"}:
            self._enforce_deployed_gates()
        return self

    def model_gateway_budget_seconds(self) -> float:
        return retry_budget_seconds(
            self.model_gateway_timeout_seconds, self.model_gateway_max_retries
        )

    def caller_budget_seconds(self) -> float:
        return retry_budget_seconds(self.caller_timeout_seconds, self.caller_max_retries)

    def _is_secure_oidc_url(self, url: str) -> bool:
        # OAuth permits plain HTTP only on the loopback interface; allow it for local/test IdPs.
        if url.startswith("https://"):
            return True
        host = urlsplit(url).hostname
        return (
            self.environment in {"local", "test"}
            and url.startswith("http://")
            and host in LOOPBACK_HOSTS
        )

    def oidc_browser_config(self) -> OidcBrowserConfig:
        client_id = self.oidc_client_id
        authorization_url = self.oidc_authorization_url
        token_url = self.oidc_token_url
        if not client_id or not authorization_url or not token_url:
            raise ValueError("OIDC client ID, authorization URL and token URL are required")
        if not all(self._is_secure_oidc_url(url) for url in (authorization_url, token_url)):
            raise ValueError("OIDC authorization and token URLs must use HTTPS")
        return OidcBrowserConfig(
            client_id=client_id,
            authorization_url=authorization_url,
            token_url=token_url,
            scope=f"openid {self.oidc_required_scope}",
        )

    def audit_key(self) -> bytes:
        return self.audit_hmac_key.get_secret_value().encode("utf-8")

    def service_keys(self) -> list[str]:
        keys = [self.service_api_key.get_secret_value()]
        keys.extend(key.strip() for key in self.previous_service_api_keys.split(",") if key.strip())
        if any(len(key) < MIN_SECRET_LENGTH for key in keys):
            raise ValueError("Every service key must contain at least 32 characters")
        return keys

    def reviewers(self) -> dict[str, str]:
        parsed: dict[str, str] = {}
        for item in self.reviewer_api_keys.split(","):
            if not item.strip():
                continue
            reviewer_id, separator, token = item.partition(":")
            if not separator or not reviewer_id.strip() or len(token.strip()) < MIN_SECRET_LENGTH:
                raise ValueError(
                    "REVIEWER_API_KEYS must contain reviewer_id:token entries "
                    "with tokens of at least 32 characters"
                )
            parsed[reviewer_id.strip()] = token.strip()
        if not parsed:
            raise ValueError("At least one reviewer API key is required")
        return parsed

    def authenticate_reviewer(self, token: str) -> str | None:
        # Compare against every key so response time does not reveal which entry matched.
        matched: str | None = None
        for reviewer_id, expected in self.reviewers().items():
            if secrets.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
                matched = reviewer_id
        return matched

    def hosts(self) -> list[str]:
        hosts = [host.strip() for host in self.allowed_hosts.split(",") if host.strip()]
        if not hosts or "*" in hosts:
            raise ValueError("ALLOWED_HOSTS must contain explicit hosts")
        return hosts


@lru_cache
def get_settings() -> Settings:
    return Settings()
