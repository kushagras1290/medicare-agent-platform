from __future__ import annotations

import httpx
import pytest

from medicare_agent.http_retry import (
    NonRetryableResponseError,
    RetriesExhaustedError,
    is_retryable_status,
    post_with_retries,
    retry_budget_seconds,
)


def client_for(responses: list[httpx.Response | Exception]) -> tuple[httpx.Client, list[int]]:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        item = responses[len(attempts) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return httpx.Client(base_url="http://peer.test", transport=httpx.MockTransport(handler)), (
        attempts
    )


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("time.sleep", lambda _: None)


def test_client_errors_are_not_retried() -> None:
    client, attempts = client_for([httpx.Response(401), httpx.Response(200)])
    with pytest.raises(NonRetryableResponseError) as error:
        post_with_retries(client, "/x", content="{}", headers={}, max_retries=3)
    assert error.value.status_code == 401
    assert len(attempts) == 1


def test_transient_failures_are_retried_until_exhausted() -> None:
    request = httpx.Request("POST", "http://peer.test/x")
    client, attempts = client_for(
        [httpx.ConnectError("down", request=request), httpx.Response(429), httpx.Response(503)]
    )
    with pytest.raises(RetriesExhaustedError, match="HTTP 503"):
        post_with_retries(client, "/x", content="{}", headers={}, max_retries=2)
    assert len(attempts) == 3


def test_retryable_status_classification_and_budget() -> None:
    assert is_retryable_status(500) and is_retryable_status(429) and is_retryable_status(408)
    assert not is_retryable_status(400) and not is_retryable_status(409)
    assert retry_budget_seconds(10, 0) == 10
    assert retry_budget_seconds(10, 2) == pytest.approx(30.6)
