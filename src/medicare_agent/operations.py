from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from medicare_agent.models import (
    AuditEvent,
    FollowUpCase,
    HumanReview,
    OutboxEvent,
    ProviderCall,
    WorkerHeartbeat,
    utcnow,
)

CLOSED_CASE_STATUSES = frozenset({"dispatched", "rejected", "provider_failed"})


def record_worker_heartbeat(
    session: Session, worker_name: str, *, processed: int, status: str = "ok"
) -> None:
    heartbeat = session.get(WorkerHeartbeat, worker_name)
    if heartbeat is None:
        heartbeat = WorkerHeartbeat(
            worker_name=worker_name,
            last_seen_at=utcnow(),
            status=status,
            processed=processed,
            detail_json=json.dumps({"synthetic_only": True}),
        )
        session.add(heartbeat)
    else:
        heartbeat.last_seen_at = utcnow()
        heartbeat.status = status
        heartbeat.processed = processed
    session.commit()


@dataclass(frozen=True)
class RetentionResult:
    eligible_cases: int
    deleted_cases: int
    executed: bool


def retention_sweep(
    session: Session, *, before: datetime, execute: bool, batch_size: int = 500
) -> RetentionResult:
    case_ids = list(
        session.scalars(
            select(FollowUpCase.id)
            .where(
                FollowUpCase.status.in_(CLOSED_CASE_STATUSES),
                FollowUpCase.updated_at < before,
            )
            .order_by(FollowUpCase.updated_at, FollowUpCase.id)
            .limit(batch_size)
        )
    )
    if not execute or not case_ids:
        return RetentionResult(len(case_ids), 0, False)
    session.execute(delete(HumanReview).where(HumanReview.case_id.in_(case_ids)))
    session.execute(delete(ProviderCall).where(ProviderCall.case_id.in_(case_ids)))
    session.execute(delete(OutboxEvent).where(OutboxEvent.case_id.in_(case_ids)))
    session.execute(
        delete(AuditEvent).where(
            AuditEvent.aggregate_type == "follow_up_case",
            AuditEvent.aggregate_id.in_(case_ids),
        )
    )
    session.execute(delete(FollowUpCase).where(FollowUpCase.id.in_(case_ids)))
    session.commit()
    return RetentionResult(len(case_ids), len(case_ids), True)
