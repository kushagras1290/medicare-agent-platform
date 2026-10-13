from __future__ import annotations

import time

import httpx

BACKOFF_BASE_SECONDS = 0.2
RETRYABLE_CLIENT_STATUS_CODES = frozenset({408, 425, 429})


class HttpCallError(RuntimeError):
    pass


class RetriesExhaustedError(HttpCallError):
    """Every attempt failed with a transport error or a retryable status."""


class NonRetryableResponseError(HttpCallError):
    """The peer rejected the request; repeating it unchanged cannot succeed."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code


def is_retryable_status(status_code: int) -> bool:
    return status_code >= 500 or status_code in RETRYABLE_CLIENT_STATUS_CODES


def backoff_seconds(attempt: int) -> float:
    return float(BACKOFF_BASE_SECONDS * (2**attempt))


def retry_budget_seconds(timeout_seconds: float, max_retries: int) -> float:
    """Approximate worst-case wall time for one call including retries and backoff."""

    sleeps = sum(backoff_seconds(attempt) for attempt in range(max_retries))
    return timeout_seconds * (max_retries + 1) + sleeps


def post_with_retries(
    client: httpx.Client,
    path: str,
    *,
    content: str,
    headers: dict[str, str],
    max_retries: int,
) -> httpx.Response:
    """POST with bounded retries on transport errors, 5xx, 408, 425 and 429 only."""

    last_error = "no attempt made"
    for attempt in range(max_retries + 1):
        try:
            response = client.post(path, content=content, headers=headers)
        except httpx.TransportError as exc:
            last_error = f"{type(exc).__name__}: {str(exc)[:180]}"
        else:
            if response.is_success:
                return response
            if not is_retryable_status(response.status_code):
                raise NonRetryableResponseError(response.status_code)
            last_error = f"HTTP {response.status_code}"
        if attempt < max_retries:
            time.sleep(backoff_seconds(attempt))
    raise RetriesExhaustedError(last_error)
