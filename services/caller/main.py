from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from medicare_agent.logging_config import configure_logging
from medicare_agent.schemas import CallerRequest, CallerResponse
from medicare_agent.secret_files import is_weak_secret, read_secret_file

MAX_IDEMPOTENCY_KEY_LENGTH = 128


class CallerSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    environment: Literal["local", "test", "staging", "production"] = "local"
    caller_mode: Literal["mock", "http"] = "mock"
    caller_service_api_key: SecretStr = SecretStr("local-caller-key-change-me-32-chars")
    caller_service_api_key_file: Path | None = None
    caller_provider_url: str | None = None
    caller_provider_api_key: SecretStr | None = None
    caller_provider_api_key_file: Path | None = None
    caller_provider_timeout_seconds: float = Field(default=20.0, ge=1.0, le=60.0)
    # Live dispatch is disabled platform-wide. HTTP mode may only target a provider sandbox that
    # never reaches a real person, and the operator must attest to that explicitly.
    caller_provider_is_synthetic_sandbox: bool = False

    @model_validator(mode="after")
    def load_secret(self) -> CallerSettings:
        if self.caller_service_api_key_file:
            self.caller_service_api_key = SecretStr(
                read_secret_file(self.caller_service_api_key_file, "CALLER_SERVICE_API_KEY_FILE")
            )
        if self.caller_provider_api_key_file:
            self.caller_provider_api_key = SecretStr(
                read_secret_file(self.caller_provider_api_key_file, "CALLER_PROVIDER_API_KEY_FILE")
            )
        if self.caller_mode == "http":
            if not self.caller_provider_url or not self.caller_provider_url.startswith("https://"):
                raise ValueError("HTTP caller mode requires an HTTPS CALLER_PROVIDER_URL")
            if self.caller_provider_api_key is None:
                raise ValueError("HTTP caller mode requires a provider API key")
            if not self.caller_provider_is_synthetic_sandbox:
                raise ValueError(
                    "HTTP caller mode requires CALLER_PROVIDER_IS_SYNTHETIC_SANDBOX=true; "
                    "live patient dispatch is not approved"
                )
        if self.environment in {"staging", "production"}:
            if not self.caller_service_api_key_file:
                raise ValueError("Staging/production caller keys must be file-mounted")
            if is_weak_secret(self.caller_service_api_key.get_secret_value()):
                raise ValueError("Staging/production requires a non-default caller key")
        return self


configure_logging()
settings = CallerSettings()
app = FastAPI(title="MediCare synthetic caller", version="0.1.0")


def authorize(x_caller_key: Annotated[str | None, Header()] = None) -> None:
    if not x_caller_key or not secrets.compare_digest(
        x_caller_key.encode("utf-8"), settings.caller_service_api_key.get_secret_value().encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid caller credentials")


class HealthResponse(BaseModel):
    status: str
    mode: str


@app.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="alive", mode=settings.caller_mode)


def _forward_to_provider(request: CallerRequest, idempotency_key: str) -> CallerResponse:
    provider_url = settings.caller_provider_url
    provider_key = settings.caller_provider_api_key
    if not provider_url or provider_key is None:
        raise HTTPException(status_code=503, detail="Caller provider is not configured")
    try:
        response = httpx.post(
            provider_url,
            content=request.model_dump_json(),
            headers={
                "Authorization": f"Bearer {provider_key.get_secret_value()}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            timeout=settings.caller_provider_timeout_seconds,
        )
        response.raise_for_status()
        return CallerResponse.model_validate(response.json())
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(status_code=502, detail="Caller provider request failed") from exc


@app.post("/v1/calls", response_model=CallerResponse, dependencies=[Depends(authorize)])
def place_call(
    request: CallerRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> CallerResponse:
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(status_code=400, detail="A bounded idempotency key is required")
    # CallerRequest types synthetic and approved_by_human as Literal[True], so a request that is
    # not synthetic or not human-approved is rejected with 422 before reaching this handler.
    if settings.caller_mode == "http":
        return _forward_to_provider(request, idempotency_key)
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
    return CallerResponse(
        external_id=f"mock-call-{digest}",
        status="accepted",
        provider="deterministic-caller-stub",
    )
