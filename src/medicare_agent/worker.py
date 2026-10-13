from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from medicare_agent.config import Settings, get_settings
from medicare_agent.db import open_session
from medicare_agent.http_retry import is_retryable_status
from medicare_agent.logging_config import configure_logging
from medicare_agent.models import Customer, Observation
from medicare_agent.operations import record_worker_heartbeat

configure_logging()
logger = logging.getLogger(__name__)

STOP_EVENT = threading.Event()
WORKER_MODES = ("automation", "dispatch")
REQUEST_TIMEOUT_MARGIN_SECONDS = 15.0
MAX_TRACKED_FAILURES = 1000
FAILURE_BACKOFF_BASE_SECONDS = 60.0
FAILURE_BACKOFF_MAX_SECONDS = 3600.0
PERMANENT_FAILURE_BACKOFF_SECONDS = 86400.0
BLOCKED_DISPATCH_STATUSES = frozenset({403, 409})
RUNS_PATH = "/api/v1/agent/runs"
MAINTENANCE_PATH = "/api/v1/agent/maintenance"
DISPATCH_PATH = "/api/v1/outbox/dispatch-one"


class WorkerApiError(RuntimeError):
    pass


def stop_worker(*_: object) -> None:
    STOP_EVENT.set()


class ServiceApi:
    """Thin service-key client for the agent API envelope."""

    def __init__(self, client: httpx.Client, service_key: str) -> None:
        self.client = client
        self.service_key = service_key

    def post(self, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        response = self.client.post(path, json=payload, headers={"X-Service-Key": self.service_key})
        response.raise_for_status()
        try:
            data = response.json()["data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise WorkerApiError(f"Malformed API envelope from {path}") from exc
        if not isinstance(data, dict):
            raise WorkerApiError(f"Malformed API envelope from {path}")
        return data


@dataclass
class FailureTracker:
    """Backs off customers whose runs keep failing so they cannot block the batch head."""

    clock: Callable[[], float] = time.monotonic
    entries: dict[str, tuple[int, float]] = field(default_factory=dict)

    def blocked(self) -> list[str]:
        now = self.clock()
        return [key for key, (_, until) in self.entries.items() if until > now]

    def record(self, key: str, *, permanent: bool) -> None:
        attempts = self.entries.get(key, (0, 0.0))[0] + 1
        delay = (
            PERMANENT_FAILURE_BACKOFF_SECONDS
            if permanent
            else min(
                FAILURE_BACKOFF_MAX_SECONDS, FAILURE_BACKOFF_BASE_SECONDS * 2 ** (attempts - 1)
            )
        )
        if key not in self.entries and len(self.entries) >= MAX_TRACKED_FAILURES:
            soonest = min(self.entries, key=lambda item: self.entries[item][1])
            del self.entries[soonest]
        self.entries[key] = (attempts, self.clock() + delay)

    def clear(self, key: str) -> None:
        self.entries.pop(key, None)


def _is_permanent(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.HTTPStatusError) and not is_retryable_status(
        exc.response.status_code
    )


def automation_candidates(settings: Settings, excluded: list[str]) -> list[tuple[str, datetime]]:
    query = (
        select(Customer.id, Observation.measured_at)
        .join(Observation, Observation.customer_id == Customer.id)
        .where(
            Customer.missed_appointment.is_(True),
            Customer.follow_up_started_at.is_(None),
        )
    )
    if excluded:
        query = query.where(Customer.id.not_in(excluded))
    with open_session() as session:
        rows = session.execute(query.order_by(Customer.id).limit(settings.automation_batch_size))
        return [(customer_id, measured_at) for customer_id, measured_at in rows.all()]


def recover_and_retry(api: ServiceApi) -> int:
    """Fail stale provider runs and retry failed ones whose backoff has elapsed."""

    try:
        data = api.post(MAINTENANCE_PATH)
    except (httpx.HTTPError, WorkerApiError):
        logger.exception("maintenance call failed", extra={"event": "maintenance_failed"})
        return 0
    handled = int(data.get("recovered_stale_runs", 0))
    for case_id in data.get("retry_case_ids", []):
        try:
            api.post(f"/api/v1/agent/cases/{case_id}/retry")
            handled += 1
        except (httpx.HTTPError, WorkerApiError):
            logger.exception(
                "provider retry failed", extra={"event": "retry_failed", "case_id": case_id}
            )
    return handled


def automation_tick(api: ServiceApi, settings: Settings, tracker: FailureTracker) -> int:
    processed = recover_and_retry(api)
    for customer_id, measured_at in automation_candidates(settings, tracker.blocked()):
        key = f"auto-{customer_id}-{measured_at.date().isoformat()}"
        try:
            api.post(RUNS_PATH, {"customer_id": customer_id, "idempotency_key": key})
        except httpx.HTTPError as exc:
            tracker.record(customer_id, permanent=_is_permanent(exc))
            logger.exception(
                "automation service call failed",
                extra={"event": "automation_call_failed", "customer_id": customer_id},
            )
        except WorkerApiError:
            tracker.record(customer_id, permanent=False)
            logger.exception(
                "automation response malformed",
                extra={"event": "automation_call_failed", "customer_id": customer_id},
            )
        else:
            tracker.clear(customer_id)
            processed += 1
    return processed


def dispatch_tick(api: ServiceApi, settings: Settings) -> int:
    """Drain the outbox up to the batch limit instead of sending one event per poll."""

    processed = 0
    while processed < settings.dispatch_batch_limit and not STOP_EVENT.is_set():
        try:
            data = api.post(DISPATCH_PATH)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in BLOCKED_DISPATCH_STATUSES:
                # The API dead-letters a blocked event before answering, so keep draining.
                logger.warning("dispatch blocked", extra={"event": "dispatch_blocked"})
                processed += 1
                continue
            logger.exception("dispatch service call failed", extra={"event": "dispatch_failed"})
            break
        except (httpx.HTTPError, WorkerApiError):
            logger.exception("dispatch service call failed", extra={"event": "dispatch_failed"})
            break
        if not data.get("processed"):
            break
        processed += 1
    return processed


def heartbeat(mode: str, processed: int, status: str = "ok") -> None:
    with open_session() as session:
        record_worker_heartbeat(session, mode, processed=processed, status=status)


def run_once(
    mode: str, api: ServiceApi, settings: Settings, tracker: FailureTracker
) -> tuple[int, str]:
    try:
        if mode == "automation":
            return automation_tick(api, settings, tracker), "ok"
        return dispatch_tick(api, settings), "ok"
    except Exception:
        # Top-level guard: one bad tick (database blip, unexpected payload) must not kill the
        # process and send it into a restart loop.
        logger.exception("worker tick failed", extra={"event": "worker_tick_failed", "mode": mode})
        return 0, "error"


def request_timeout_seconds(settings: Settings, mode: str) -> float:
    budget = (
        settings.model_gateway_budget_seconds()
        if mode == "automation"
        else settings.caller_budget_seconds()
    )
    return budget + REQUEST_TIMEOUT_MARGIN_SECONDS


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=WORKER_MODES)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)
    STOP_EVENT.clear()
    signal.signal(signal.SIGINT, stop_worker)
    signal.signal(signal.SIGTERM, stop_worker)
    settings = get_settings()
    tracker = FailureTracker()
    with httpx.Client(
        base_url=settings.api_internal_url,
        timeout=request_timeout_seconds(settings, args.mode),
    ) as client:
        api = ServiceApi(client, settings.service_api_key.get_secret_value())
        while not STOP_EVENT.is_set():
            work, status = run_once(args.mode, api, settings, tracker)
            try:
                heartbeat(args.mode, work, status)
            except SQLAlchemyError:
                logger.exception("heartbeat failed", extra={"event": "heartbeat_failed"})
            logger.info(
                "worker tick",
                extra={"event": "worker_tick", "mode": args.mode, "processed": work},
            )
            if args.once:
                break
            # Event.wait returns immediately on SIGTERM, unlike time.sleep.
            STOP_EVENT.wait(settings.worker_poll_seconds)


if __name__ == "__main__":  # pragma: no cover - exercised through the container command
    main()
