from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from medicare_agent.audit import append_audit
from medicare_agent.models import (
    AuditEvent,
    AuditHead,
    FollowUpCase,
    HumanReview,
    OutboxEvent,
    ProviderCall,
    WorkerHeartbeat,
    utcnow,
)
from medicare_agent.schemas import CaseStatus

CASE_AGGREGATE = "follow_up_case"
RETENTION_AGGREGATE = "retention_sweep"
CLOSED_CASE_STATUSES = frozenset({CaseStatus.DISPATCHED, CaseStatus.REJECTED, CaseStatus.CLOSED})


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
    session: Session,
    *,
    before: datetime,
    execute: bool,
    provider_max_attempts: int,
    actor_id: str = "retention-cli",
    batch_size: int = 500,
) -> RetentionResult:
    """Delete one bounded batch of closed cases. Provider failures count as closed only when
    their retry budget is spent; otherwise the retry path still owns them."""

    closed = or_(
        FollowUpCase.status.in_(CLOSED_CASE_STATUSES),
        and_(
            FollowUpCase.status == CaseStatus.PROVIDER_FAILED,
            FollowUpCase.provider_attempts >= provider_max_attempts,
        ),
    )
    case_ids = list(
        session.scalars(
            select(FollowUpCase.id)
            .where(closed, FollowUpCase.updated_at < before)
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
            AuditEvent.aggregate_type == CASE_AGGREGATE,
            AuditEvent.aggregate_id.in_(case_ids),
        )
    )
    session.execute(
        delete(AuditHead).where(
            AuditHead.aggregate_type == CASE_AGGREGATE,
            AuditHead.aggregate_id.in_(case_ids),
        )
    )
    session.execute(delete(FollowUpCase).where(FollowUpCase.id.in_(case_ids)))
    # Deleting case audit chains must itself leave evidence of who deleted what and why.
    append_audit(
        session,
        aggregate_type=RETENTION_AGGREGATE,
        aggregate_id=str(uuid.uuid4()),
        event_type="retention_sweep_executed",
        actor_type="operator",
        actor_id=actor_id,
        data={"before": before.isoformat(), "deleted_case_ids": sorted(case_ids)},
    )
    session.commit()
    return RetentionResult(len(case_ids), len(case_ids), True)
