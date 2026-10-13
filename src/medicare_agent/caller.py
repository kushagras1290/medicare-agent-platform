from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from medicare_agent.config import Settings
from medicare_agent.http_retry import (
    NonRetryableResponseError,
    RetriesExhaustedError,
    post_with_retries,
)
from medicare_agent.schemas import CallerRequest, CallerResponse


class CallerError(RuntimeError):
    pass


class CallerUnavailableError(CallerError):
    """Transient failure; the outbox schedules another attempt."""


class CallerRejectedError(CallerError):
    """Permanent failure; retrying the same request cannot succeed, so it is dead-lettered."""


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
        with httpx.Client(
            base_url=self.settings.caller_service_url,
            timeout=self.settings.caller_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = post_with_retries(
                    client,
                    "/v1/calls",
                    content=request.model_dump_json(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Caller-Key": self.settings.caller_service_api_key.get_secret_value(),
                        "Idempotency-Key": idempotency_key,
                    },
                    max_retries=self.settings.caller_max_retries,
                )
            except NonRetryableResponseError as exc:
                raise CallerRejectedError(f"caller {exc}") from exc
            except RetriesExhaustedError as exc:
                raise CallerUnavailableError(f"caller {exc}") from exc
        try:
            parsed = CallerResponse.model_validate(response.json())
        except ValueError as exc:
            # The call may have been accepted; retry under the same idempotency key to learn the
            # outcome instead of dead-lettering a possibly delivered event.
            raise CallerUnavailableError(
                f"caller response invalid: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc
        return CallerResult(parsed, round((time.perf_counter() - started) * 1000))
