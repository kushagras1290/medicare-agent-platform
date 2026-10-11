from __future__ import annotations

import argparse
import logging
import signal
import time

import httpx
from sqlalchemy import exists, select

from medicare_agent.config import get_settings
from medicare_agent.db import SessionLocal
from medicare_agent.logging_config import configure_logging
from medicare_agent.models import Customer, FollowUpCase, Observation
from medicare_agent.operations import record_worker_heartbeat

configure_logging()
logger = logging.getLogger(__name__)
stopping = False


def stop_worker(*_: object) -> None:
    global stopping
    stopping = True


def headers() -> dict[str, str]:
    settings = get_settings()
    return {"X-Service-Key": settings.service_api_key.get_secret_value()}


def automation_tick() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        rows = session.execute(
            select(Customer.id, Observation.measured_at)
            .join(Observation, Observation.customer_id == Customer.id)
            .where(
                Customer.missed_appointment.is_(True),
                ~exists().where(FollowUpCase.customer_id == Customer.id),
            )
            .order_by(Customer.id)
            .limit(settings.automation_batch_size)
        ).all()
    processed = 0
    with httpx.Client(base_url=settings.api_internal_url, timeout=60.0) as client:
        for customer_id, measured_at in rows:
            key = f"auto-{customer_id}-{measured_at.date().isoformat()}"
            try:
                response = client.post(
                    "/api/v1/agent/runs",
                    headers=headers(),
                    json={"customer_id": customer_id, "idempotency_key": key},
                )
                response.raise_for_status()
                processed += 1
            except httpx.HTTPError:
                logger.exception(
                    "automation service call failed",
                    extra={"event": "automation_call_failed", "customer_id": customer_id},
                )
    return processed


def dispatch_tick() -> bool:
    settings = get_settings()
    try:
        response = httpx.post(
            f"{settings.api_internal_url.rstrip('/')}/api/v1/outbox/dispatch-one",
            headers=headers(),
            timeout=30.0,
        )
        response.raise_for_status()
        return bool(response.json()["data"]["processed"])
    except httpx.HTTPError:
        logger.exception("dispatch service call failed", extra={"event": "dispatch_call_failed"})
        return False


def heartbeat(mode: str, processed: int) -> None:
    with SessionLocal() as session:
        record_worker_heartbeat(session, mode, processed=processed)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["automation", "dispatch"])
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    signal.signal(signal.SIGINT, stop_worker)
    signal.signal(signal.SIGTERM, stop_worker)
    settings = get_settings()
    while not stopping:
        work = automation_tick() if args.mode == "automation" else int(dispatch_tick())
        heartbeat(args.mode, work)
        logger.info(
            "worker tick", extra={"event": "worker_tick", "mode": args.mode, "processed": work}
        )
        if args.once:
            break
        time.sleep(settings.worker_poll_seconds)


if __name__ == "__main__":  # pragma: no cover - exercised through the container command
    main()
