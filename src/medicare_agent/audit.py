from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from medicare_agent.models import AuditEvent, utcnow

GENESIS_HASH = "0" * 64


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return f"{value.isoformat(timespec='microseconds')}Z"


def append_audit(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    data: dict[str, Any],
) -> AuditEvent:
    previous = session.scalar(
        select(AuditEvent)
        .where(AuditEvent.aggregate_type == aggregate_type, AuditEvent.aggregate_id == aggregate_id)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(1)
    )
    created_at = utcnow()
    previous_hash = previous.event_hash if previous else GENESIS_HASH
    data_json = canonical_json(data)
    material = canonical_json(
        {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "data": json.loads(data_json),
            "previous_hash": previous_hash,
            "created_at": canonical_timestamp(created_at),
        }
    )
    event = AuditEvent(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        data_json=data_json,
        previous_hash=previous_hash,
        event_hash=hashlib.sha256(material.encode()).hexdigest(),
        created_at=created_at,
    )
    session.add(event)
    return event


def verify_audit_chain(session: Session, aggregate_type: str, aggregate_id: str) -> bool:
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.aggregate_type == aggregate_type, AuditEvent.aggregate_id == aggregate_id)
        .order_by(AuditEvent.created_at, AuditEvent.id)
    ).all()
    previous_hash = GENESIS_HASH
    for event in events:
        material = canonical_json(
            {
                "aggregate_type": event.aggregate_type,
                "aggregate_id": event.aggregate_id,
                "event_type": event.event_type,
                "actor_type": event.actor_type,
                "actor_id": event.actor_id,
                "data": json.loads(event.data_json),
                "previous_hash": previous_hash,
                "created_at": canonical_timestamp(event.created_at),
            }
        )
        if (
            event.previous_hash != previous_hash
            or event.event_hash != hashlib.sha256(material.encode()).hexdigest()
        ):
            return False
        previous_hash = event.event_hash
    return True
