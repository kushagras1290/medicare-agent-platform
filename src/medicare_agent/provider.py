from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from medicare_agent.config import Settings
from medicare_agent.schemas import GatewayProposalRequest, GatewayProposalResponse


class ProviderUnavailableError(RuntimeError):
    pass


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
        last_error = "unknown provider error"
        for attempt in range(self.settings.model_gateway_max_retries + 1):
            try:
                with httpx.Client(
                    base_url=self.settings.model_gateway_url,
                    timeout=self.settings.model_gateway_timeout_seconds,
                    transport=self.transport,
                ) as client:
                    response = client.post(
                        "/v1/proposals",
                        content=request.model_dump_json(),
                        headers={
                            "Content-Type": "application/json",
                            "X-Gateway-Key": self.settings.model_gateway_api_key.get_secret_value(),
                            "Idempotency-Key": request.case_id,
                        },
                    )
                if response.status_code < 500:
                    response.raise_for_status()
                    parsed = GatewayProposalResponse.model_validate(response.json())
                    return ProviderResult(parsed, round((time.perf_counter() - start) * 1000))
                last_error = f"gateway HTTP {response.status_code}"
            except (httpx.HTTPError, ValueError) as exc:
                last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
            if attempt < self.settings.model_gateway_max_retries:
                time.sleep(0.2 * (2**attempt))
        raise ProviderUnavailableError(last_error)
