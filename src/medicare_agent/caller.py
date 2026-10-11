from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from medicare_agent.config import Settings
from medicare_agent.schemas import CallerRequest, CallerResponse


class CallerUnavailableError(RuntimeError):
    pass


@dataclass(frozen=True)
class CallerResult:
    response: CallerResponse
    latency_ms: int


class CallerClient:
    """Authenticated client for the post-approval outbound caller service."""

    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def place_call(self, request: CallerRequest, idempotency_key: str) -> CallerResult:
        started = time.perf_counter()
        last_error = "unknown caller error"
        for attempt in range(self.settings.caller_max_retries + 1):
            try:
                with httpx.Client(
                    base_url=self.settings.caller_service_url,
                    timeout=self.settings.caller_timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(
                        "/v1/calls",
                        content=request.model_dump_json(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Caller-Key": self.settings.caller_service_api_key.get_secret_value(),
                            "Idempotency-Key": idempotency_key,
                        },
                    )
                if response.status_code < 500:
                    response.raise_for_status()
                    parsed = CallerResponse.model_validate(response.json())
                    return CallerResult(parsed, round((time.perf_counter() - started) * 1000))
                last_error = f"caller HTTP {response.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if attempt < self.settings.caller_max_retries:
                time.sleep(0.2 * (2**attempt))
        raise CallerUnavailableError(last_error)
