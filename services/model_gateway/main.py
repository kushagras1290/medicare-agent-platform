from __future__ import annotations

import hashlib
import secrets
import threading
import time
import uuid
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from medicare_agent.logging_config import configure_logging
from medicare_agent.schemas import (
    AgentProposal,
    ContactChannel,
    GatewayProposalRequest,
    GatewayProposalResponse,
    RiskTier,
)
from medicare_agent.secret_files import is_weak_secret, read_secret_file

IDEMPOTENCY_LOCK_STRIPES = 64
MAX_IDEMPOTENCY_KEY_LENGTH = 128
# Bounds are re-validated by Pydantic after the call; dropping them keeps the schema inside the
# JSON Schema subset that strict structured outputs accept.
STRICT_SCHEMA_DROPPED_KEYWORDS = frozenset(
    {"minLength", "maxLength", "minItems", "maxItems", "title", "default"}
)


class GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    environment: Literal["local", "test", "staging", "production"] = "local"
    model_gateway_mode: Literal["stub", "openai"] = "stub"
    model_gateway_api_key: SecretStr = SecretStr("local-gateway-key-change-me-32-chars")
    model_gateway_api_key_file: Path | None = None
    openai_api_key: SecretStr = SecretStr("")
    openai_api_key_file: Path | None = None
    openai_model: str = "gpt-5-mini"
    openai_base_url: str = "https://api.openai.com/v1"
    openai_timeout_seconds: float = Field(default=30.0, ge=1.0, le=110.0)
    idempotency_cache_size: int = Field(default=1024, ge=16, le=100_000)
    idempotency_ttl_seconds: float = Field(default=900.0, ge=60.0, le=86_400.0)

    @model_validator(mode="after")
    def load_file_secrets(self) -> GatewaySettings:
        if self.model_gateway_api_key_file:
            self.model_gateway_api_key = SecretStr(
                read_secret_file(self.model_gateway_api_key_file, "MODEL_GATEWAY_API_KEY_FILE")
            )
        if self.openai_api_key_file:
            self.openai_api_key = SecretStr(
                read_secret_file(self.openai_api_key_file, "OPENAI_API_KEY_FILE")
            )
        if not self.openai_base_url.startswith("https://"):
            raise ValueError("OPENAI_BASE_URL must use HTTPS")
        if self.environment in {"staging", "production"}:
            if not self.model_gateway_api_key_file:
                raise ValueError("Staging/production gateway keys must be file-mounted")
            if is_weak_secret(self.model_gateway_api_key.get_secret_value()):
                raise ValueError("Staging/production requires a non-default gateway key")
            if self.model_gateway_mode == "openai" and not self.openai_api_key_file:
                raise ValueError("Staging/production OpenAI keys must be file-mounted")
        return self


class IdempotencyConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class _CacheEntry[T]:
    fingerprint: str
    expires_at: float
    value: T


class IdempotencyCache[T]:
    """Replays the first result for an Idempotency-Key so client retries never pay twice.

    Striped locks serialize duplicates of one key while unrelated keys proceed in parallel.
    """

    def __init__(
        self,
        *,
        max_entries: int,
        ttl_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, _CacheEntry[T]] = OrderedDict()
        self._entries_lock = threading.Lock()
        self._stripes = [threading.Lock() for _ in range(IDEMPOTENCY_LOCK_STRIPES)]

    def _stripe(self, key: str) -> threading.Lock:
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return self._stripes[digest[0] % IDEMPOTENCY_LOCK_STRIPES]

    def _lookup(self, key: str, fingerprint: str) -> T | None:
        with self._entries_lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            if entry.expires_at <= self._clock():
                del self._entries[key]
                return None
            if entry.fingerprint != fingerprint:
                raise IdempotencyConflictError("Idempotency key reused with a different request")
            return entry.value

    def _store(self, key: str, fingerprint: str, value: T) -> None:
        with self._entries_lock:
            self._entries[key] = _CacheEntry(fingerprint, self._clock() + self._ttl_seconds, value)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def run(self, key: str, fingerprint: str, compute: Callable[[], T]) -> T:
        with self._stripe(key):
            cached = self._lookup(key, fingerprint)
            if cached is not None:
                return cached
            value = compute()
            self._store(key, fingerprint, value)
            return value


configure_logging()
settings = GatewaySettings()
cache: IdempotencyCache[GatewayProposalResponse] = IdempotencyCache(
    max_entries=settings.idempotency_cache_size, ttl_seconds=settings.idempotency_ttl_seconds
)
app = FastAPI(title="MediCare Model Gateway", version="0.1.0")


def authorize(x_gateway_key: Annotated[str | None, Header()] = None) -> None:
    if not x_gateway_key or not secrets.compare_digest(
        x_gateway_key.encode("utf-8"), settings.model_gateway_api_key.get_secret_value().encode()
    ):
        raise HTTPException(status_code=401, detail="Invalid gateway credentials")


def stub_proposal(request: GatewayProposalRequest) -> AgentProposal:
    tier = request.rule_assessment.tier
    if tier == RiskTier.URGENT:
        action = (
            "Pause automation and route this synthetic case to an authorized human reviewer "
            "immediately."
        )
        patient_message = (
            "No patient message is sent for this synthetic case; it is routed to a human "
            "reviewer for escalation."
        )
        channel = ContactChannel.NONE
    elif tier == RiskTier.REVIEW:
        action = (
            "Have a human reviewer assess the flagged synthetic measurements before approving "
            "any mock follow-up."
        )
        patient_message = (
            "This is a synthetic call from your care team to help you reschedule a missed "
            "follow-up visit."
        )
        channel = ContactChannel.PHONE
    else:
        action = (
            "Queue a routine mock portal follow-up after a human reviewer confirms the proposal."
        )
        patient_message = (
            "This is a synthetic reminder from your care team. Please use the patient portal "
            "to book a follow-up visit at a time that suits you."
        )
        channel = ContactChannel.PORTAL
    return AgentProposal(
        summary=(
            f"Synthetic case {request.snapshot.customer_id} is classified as {tier.value} "
            "by deterministic demo rules."
        ),
        recommended_action=action,
        patient_message=patient_message,
        contact_channel=channel,
        reasoning_bullets=request.rule_assessment.reasons[:6],
        requires_human_review=True,
    )


SYSTEM_PROMPT = """You draft synthetic follow-up proposals for a human reviewer.
You are not a clinician and must not diagnose, prescribe, change medication, or bypass review.
Use only the supplied synthetic record and deterministic rule result.
Keep identifiers as customer IDs.
recommended_action is guidance for staff. patient_message is the only text a patient may receive:
keep it to a short, neutral scheduling reminder of at most 320 characters with no clinical advice,
no measurements, no phone numbers, no links and no email addresses.
Every proposal must require human review. For urgent or held cases, choose contact_channel 'none'
and recommend immediate human escalation without patient-facing instructions."""


def strict_json_schema(node: Any) -> Any:
    """Convert a Pydantic schema into the subset accepted by strict structured outputs."""

    if isinstance(node, list):
        return [strict_json_schema(item) for item in node]
    if not isinstance(node, dict):
        return node
    converted: dict[str, Any] = {}
    for key, value in node.items():
        if key in STRICT_SCHEMA_DROPPED_KEYWORDS:
            continue
        if key == "const":
            converted["enum"] = [value]
            continue
        converted[key] = strict_json_schema(value)
    return converted


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
                "schema": strict_json_schema(AgentProposal.model_json_schema()),
            }
        },
    }
    try:
        response = httpx.post(
            f"{settings.openai_base_url.rstrip('/')}/responses",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=settings.openai_timeout_seconds,
        )
        response.raise_for_status()
        data = response.json()
        proposal = AgentProposal.model_validate_json(_extract_output_text(data))
        return proposal, response.headers.get("x-request-id") or data.get("id")
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(
            status_code=502, detail=f"Upstream model call failed: {type(exc).__name__}"
        ) from exc


def generate_proposal(request: GatewayProposalRequest) -> GatewayProposalResponse:
    if settings.model_gateway_mode == "stub":
        return GatewayProposalResponse(
            proposal=stub_proposal(request),
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


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive", "mode": settings.model_gateway_mode}


@app.post("/v1/proposals", response_model=GatewayProposalResponse)
def create_proposal(
    request: GatewayProposalRequest,
    _: Annotated[None, Depends(authorize)],
    idempotency_key: Annotated[str | None, Header()] = None,
) -> GatewayProposalResponse:
    if idempotency_key is None:
        return generate_proposal(request)
    if not idempotency_key or len(idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        raise HTTPException(status_code=400, detail="A bounded idempotency key is required")
    fingerprint = hashlib.sha256(request.model_dump_json().encode("utf-8")).hexdigest()
    try:
        return cache.run(idempotency_key, fingerprint, lambda: generate_proposal(request))
    except IdempotencyConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
