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
from medicare_agent.schemas import GatewayProposalRequest, GatewayProposalResponse


class ProviderError(RuntimeError):
    code = "PROVIDER_ERROR"


class ProviderUnavailableError(ProviderError):
    """Transport failures or retryable statuses persisted through every attempt."""

    code = "PROVIDER_UNAVAILABLE"


class ProviderRejectedError(ProviderError):
    """The gateway refused the request or returned a body outside the contract."""

    code = "PROVIDER_REJECTED"


@dataclass(frozen=True)
class ProviderResult:
    response: GatewayProposalResponse
    latency_ms: int


class ModelGatewayClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    def propose(self, request: GatewayProposalRequest) -> ProviderResult:
        start = time.perf_counter()
        with httpx.Client(
            base_url=self.settings.model_gateway_url,
            timeout=self.settings.model_gateway_timeout_seconds,
            transport=self.transport,
        ) as client:
            try:
                response = post_with_retries(
                    client,
                    "/v1/proposals",
                    content=request.model_dump_json(),
                    headers={
                        "Content-Type": "application/json",
                        "X-Gateway-Key": self.settings.model_gateway_api_key.get_secret_value(),
                        "Idempotency-Key": request.case_id,
                    },
                    max_retries=self.settings.model_gateway_max_retries,
                )
            except NonRetryableResponseError as exc:
                raise ProviderRejectedError(f"gateway {exc}") from exc
            except RetriesExhaustedError as exc:
                raise ProviderUnavailableError(f"gateway {exc}") from exc
        try:
            parsed = GatewayProposalResponse.model_validate(response.json())
        except ValueError as exc:
            raise ProviderRejectedError(
                f"gateway response invalid: {type(exc).__name__}: {str(exc)[:160]}"
            ) from exc
        return ProviderResult(parsed, round((time.perf_counter() - start) * 1000))
