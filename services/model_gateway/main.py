from __future__ import annotations

import secrets
import uuid
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from medicare_agent.schemas import (
    AgentProposal,
    ContactChannel,
    GatewayProposalRequest,
    GatewayProposalResponse,
    RiskTier,
)


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    model_gateway_mode: Literal["stub", "openai"] = "stub"
    model_gateway_api_key: SecretStr = SecretStr("local-gateway-key-change-me-32-chars")
    model_gateway_api_key_file: Path | None = None
    openai_api_key: SecretStr = SecretStr("")
    openai_api_key_file: Path | None = None
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"

    @staticmethod
    def _read_secret(path: Path, label: str) -> SecretStr:
        try:
            if not path.is_file() or path.stat().st_size > 64 * 1024:
                raise ValueError(f"{label} must reference a small readable file")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ValueError(f"Unable to read {label}") from exc
        if not value:
            raise ValueError(f"{label} cannot be empty")
        return SecretStr(value)

    @model_validator(mode="after")
    def load_file_secrets(self) -> GatewaySettings:
        if self.model_gateway_api_key_file:
            self.model_gateway_api_key = self._read_secret(
                self.model_gateway_api_key_file, "MODEL_GATEWAY_API_KEY_FILE"
            )
        if self.openai_api_key_file:
            self.openai_api_key = self._read_secret(self.openai_api_key_file, "OPENAI_API_KEY_FILE")
        return self


settings = GatewaySettings()
app = FastAPI(title="MediCare Model Gateway", version="0.1.0")


def authorize(x_gateway_key: Annotated[str | None, Header()] = None) -> None:
    if not x_gateway_key or not secrets.compare_digest(
        x_gateway_key, settings.model_gateway_api_key.get_secret_value()
    ):
        raise HTTPException(status_code=401, detail="Invalid gateway credentials")


def stub_proposal(request: GatewayProposalRequest) -> AgentProposal:
    tier = request.rule_assessment.tier
    if tier == RiskTier.URGENT:
        action = (
            "Pause automation and route this synthetic case to an authorized human reviewer "
            "immediately."
        )
        channel = ContactChannel.NONE
    elif tier == RiskTier.REVIEW:
        action = (
            "Have a human reviewer assess the flagged synthetic measurements before approving "
            "any mock follow-up."
        )
        channel = ContactChannel.PHONE
    else:
        action = (
            "Queue a routine mock portal follow-up after a human reviewer confirms the proposal."
        )
        channel = ContactChannel.PORTAL
    return AgentProposal(
        summary=(
            f"Synthetic case {request.snapshot.customer_id} is classified as {tier.value} "
            "by deterministic demo rules."
        ),
        recommended_action=action,
        contact_channel=channel,
        reasoning_bullets=request.rule_assessment.reasons[:6],
        requires_human_review=True,
    )


SYSTEM_PROMPT = """You draft synthetic follow-up proposals for a human reviewer.
You are not a clinician and must not diagnose, prescribe, change medication, or bypass review.
Use only the supplied synthetic record and deterministic rule result.
Keep identifiers as customer IDs.
Every proposal must require human review. For urgent or held cases, choose contact_channel 'none'
and recommend immediate human escalation without patient-facing instructions."""


def _extract_output_text(payload: dict[str, Any]) -> str:
    for output in payload.get("output", []):
        for content in output.get("content", []):
            if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                return str(content["text"])
    raise ValueError("OpenAI response did not contain output_text")


def openai_proposal(request: GatewayProposalRequest) -> tuple[AgentProposal, str | None]:
    api_key = settings.openai_api_key.get_secret_value()
    if not api_key:
        raise HTTPException(status_code=503, detail="OPENAI_API_KEY is not configured")
    schema = AgentProposal.model_json_schema()
    payload = {
        "model": settings.openai_model,
        "input": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": request.model_dump_json()},
        ],
        "text": {
            "format": {
                "type": "json_schema",
                "name": "agent_proposal",
                "strict": True,
                "schema": schema,
            }
        },
    }
    try:
        response = httpx.post(
            f"{settings.openai_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=45.0,
        )
        response.raise_for_status()
        data = response.json()
        proposal = AgentProposal.model_validate_json(_extract_output_text(data))
        return proposal, response.headers.get("x-request-id") or data.get("id")
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Upstream model call failed: {type(exc).__name__}"
        ) from exc


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive", "mode": settings.model_gateway_mode}


@app.post("/v1/proposals", response_model=GatewayProposalResponse)
def create_proposal(
    request: GatewayProposalRequest,
    _: Annotated[None, Depends(authorize)],
) -> GatewayProposalResponse:
    if settings.model_gateway_mode == "stub":
        proposal = stub_proposal(request)
        return GatewayProposalResponse(
            proposal=proposal,
            provider="deterministic-http-stub",
            model="stub-v1",
            request_id=str(uuid.uuid4()),
        )
    proposal, request_id = openai_proposal(request)
    return GatewayProposalResponse(
        proposal=proposal,
        provider="openai",
        model=settings.openai_model,
        request_id=request_id,
    )
