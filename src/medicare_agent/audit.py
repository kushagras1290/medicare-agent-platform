from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from medicare_agent.config import get_settings
from medicare_agent.models import AuditEvent, AuditHead, utcnow

GENESIS_HASH = "0" * 64
LEGACY_HASH_VERSION = 1
HMAC_HASH_VERSION = 2


class AuditChainError(RuntimeError):
    pass


@dataclass(frozen=True)
class AuditVerification:
    valid: bool
    sealed: bool
    event_count: int
    reason: str | None = None


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def canonical_timestamp(value: datetime) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return f"{value.isoformat(timespec='microseconds')}Z"


def _default_key() -> bytes:
    return get_settings().audit_key()


def _material(
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    data: Any,
    previous_hash: str,
    created_at: datetime,
) -> bytes:
    return canonical_json(
        {
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "event_type": event_type,
            "actor_type": actor_type,
            "actor_id": actor_id,
            "data": data,
            "previous_hash": previous_hash,
            "created_at": canonical_timestamp(created_at),
        }
    ).encode("utf-8")


def _digest(material: bytes, hash_version: int, key: bytes) -> str:
    if hash_version == LEGACY_HASH_VERSION:
        return hashlib.sha256(material).hexdigest()
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _seal(key: bytes, aggregate_type: str, aggregate_id: str, count: int, head_hash: str) -> str:
    message = f"{aggregate_type}\x1f{aggregate_id}\x1f{count}\x1f{head_hash}".encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def _event_digest(event: AuditEvent, previous_hash: str, key: bytes) -> str:
    material = _material(
        aggregate_type=event.aggregate_type,
        aggregate_id=event.aggregate_id,
        event_type=event.event_type,
        actor_type=event.actor_type,
        actor_id=event.actor_id,
        data=json.loads(event.data_json),
        previous_hash=previous_hash,
        created_at=event.created_at,
    )
    return _digest(material, event.hash_version, key)


def _load_events(session: Session, aggregate_type: str, aggregate_id: str) -> list[AuditEvent]:
    return list(
        session.scalars(
            select(AuditEvent).where(
                AuditEvent.aggregate_type == aggregate_type,
                AuditEvent.aggregate_id == aggregate_id,
            )
        )
    )


def _ordered_chain(events: list[AuditEvent]) -> list[AuditEvent]:
    """Order events by their hash links rather than timestamps, which can tie or be edited."""

    successors: dict[str, AuditEvent] = {}
    for event in events:
        if event.previous_hash in successors:
            raise AuditChainError("chain_fork")
        successors[event.previous_hash] = event
    ordered: list[AuditEvent] = []
    cursor = GENESIS_HASH
    while cursor in successors:
        event = successors.pop(cursor)
        ordered.append(event)
        cursor = event.event_hash
    if successors:
        raise AuditChainError("unlinked_events")
    return ordered


def _lock_head(session: Session, aggregate_type: str, aggregate_id: str) -> AuditHead | None:
    return session.get(AuditHead, (aggregate_type, aggregate_id), with_for_update=True)


def append_audit(
    session: Session,
    *,
    aggregate_type: str,
    aggregate_id: str,
    event_type: str,
    actor_type: str,
    actor_id: str,
    data: dict[str, Any],
    key: bytes | None = None,
) -> AuditEvent:
    """Append a keyed, hash-linked event and reseal the aggregate head in the same transaction."""

    audit_key = key if key is not None else _default_key()
    head = _lock_head(session, aggregate_type, aggregate_id)
    if head is None:
        existing = _ordered_chain(_load_events(session, aggregate_type, aggregate_id))
        previous_hash = existing[-1].event_hash if existing else GENESIS_HASH
        count = len(existing)
    else:
        previous_hash = head.head_hash
        count = head.event_count
    created_at = utcnow()
    data_json = canonical_json(data)
    material = _material(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        data=json.loads(data_json),
        previous_hash=previous_hash,
        created_at=created_at,
    )
    event = AuditEvent(
        aggregate_type=aggregate_type,
        aggregate_id=aggregate_id,
        event_type=event_type,
        actor_type=actor_type,
        actor_id=actor_id,
        data_json=data_json,
        previous_hash=previous_hash,
        event_hash=_digest(material, HMAC_HASH_VERSION, audit_key),
        hash_version=HMAC_HASH_VERSION,
        created_at=created_at,
    )
    session.add(event)
    new_count = count + 1
    seal = _seal(audit_key, aggregate_type, aggregate_id, new_count, event.event_hash)
    if head is None:
        session.add(
            AuditHead(
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                event_count=new_count,
                head_hash=event.event_hash,
                seal=seal,
            )
        )
    else:
        head.event_count = new_count
        head.head_hash = event.event_hash
        head.seal = seal
    # The session does not autoflush; flushing keeps a second append in this transaction
    # chained to this event instead of re-reading a stale head.
    session.flush()
    return event


def verify_audit_chain(
    session: Session,
    aggregate_type: str,
    aggregate_id: str,
    *,
    key: bytes | None = None,
) -> AuditVerification:
    audit_key = key if key is not None else _default_key()
    events = _load_events(session, aggregate_type, aggregate_id)
    try:
        ordered = _ordered_chain(events)
    except AuditChainError as exc:
        return AuditVerification(False, False, len(events), str(exc))
    previous_hash = GENESIS_HASH
    for event in ordered:
        expected = _event_digest(event, previous_hash, audit_key)
        if not hmac.compare_digest(event.event_hash, expected):
            return AuditVerification(False, False, len(events), "hash_mismatch")
        previous_hash = event.event_hash

    head = session.get(AuditHead, (aggregate_type, aggregate_id))
    if head is None:
        if any(event.hash_version != LEGACY_HASH_VERSION for event in ordered):
            return AuditVerification(False, False, len(events), "missing_head")
        return AuditVerification(True, False, len(events))
    expected_seal = _seal(audit_key, aggregate_type, aggregate_id, len(ordered), previous_hash)
    if (
        head.event_count != len(ordered)
        or head.head_hash != previous_hash
        or not hmac.compare_digest(head.seal, expected_seal)
    ):
        return AuditVerification(False, False, len(events), "head_mismatch")
    return AuditVerification(True, True, len(events))
