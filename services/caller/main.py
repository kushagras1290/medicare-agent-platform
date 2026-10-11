from __future__ import annotations

import hashlib
import secrets
from pathlib import Path
from typing import Annotated, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from medicare_agent.schemas import CallerRequest, CallerResponse


class CallerSettings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    caller_mode: Literal["mock", "http"] = "mock"
    caller_service_api_key: SecretStr = SecretStr("local-caller-key-change-me-32-chars")
    caller_service_api_key_file: Path | None = None
    caller_provider_url: str | None = None
    caller_provider_api_key: SecretStr | None = None
    caller_provider_api_key_file: Path | None = None
    caller_provider_timeout_seconds: float = 20.0

    @model_validator(mode="after")
    def load_secret(self) -> CallerSettings:
        if self.caller_service_api_key_file:
            try:
                value = self.caller_service_api_key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError("Unable to read CALLER_SERVICE_API_KEY_FILE") from exc
            if not value:
                raise ValueError("CALLER_SERVICE_API_KEY_FILE cannot be empty")
            self.caller_service_api_key = SecretStr(value)
        if self.caller_provider_api_key_file:
            try:
                value = self.caller_provider_api_key_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ValueError("Unable to read CALLER_PROVIDER_API_KEY_FILE") from exc
            if not value:
                raise ValueError("CALLER_PROVIDER_API_KEY_FILE cannot be empty")
            self.caller_provider_api_key = SecretStr(value)
        if self.caller_mode == "http":
            if not self.caller_provider_url or not self.caller_provider_url.startswith("https://"):
                raise ValueError("HTTP caller mode requires an HTTPS CALLER_PROVIDER_URL")
            if self.caller_provider_api_key is None:
                raise ValueError("HTTP caller mode requires a provider API key")
        return self


settings = CallerSettings()
app = FastAPI(title="MediCare synthetic caller", version="0.1.0")


def authorize(x_caller_key: Annotated[str | None, Header()] = None) -> None:
    if not x_caller_key or not secrets.compare_digest(
        x_caller_key, settings.caller_service_api_key.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="Invalid caller credentials")


class HealthResponse(BaseModel):
    status: str
    mode: str


@app.get("/health/live", response_model=HealthResponse)
def live() -> HealthResponse:
    return HealthResponse(status="alive", mode=settings.caller_mode)


@app.post("/v1/calls", response_model=CallerResponse, dependencies=[Depends(authorize)])
def place_call(
    request: CallerRequest,
    idempotency_key: Annotated[str | None, Header()] = None,
) -> CallerResponse:
    if not idempotency_key or len(idempotency_key) > 128:
        raise HTTPException(status_code=400, detail="A bounded idempotency key is required")
    if request.synthetic is not True or request.approved_by_human is not True:
        raise HTTPException(status_code=403, detail="Synthetic human-approved calls only")
    if settings.caller_mode == "http":
        assert settings.caller_provider_url
        assert settings.caller_provider_api_key
        try:
            response = httpx.post(
                settings.caller_provider_url,
                content=request.model_dump_json(),
                headers={
                    "Authorization": (
                        f"Bearer {settings.caller_provider_api_key.get_secret_value()}"
                    ),
                    "Content-Type": "application/json",
                    "Idempotency-Key": idempotency_key,
                },
                timeout=settings.caller_provider_timeout_seconds,
            )
            response.raise_for_status()
            return CallerResponse.model_validate(response.json())
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(status_code=502, detail="Caller provider request failed") from exc
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:20]
    return CallerResponse(
        external_id=f"mock-call-{digest}",
        status="accepted",
        provider="deterministic-caller-stub",
    )
