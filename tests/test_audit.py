from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete
from sqlalchemy.orm import Session

from medicare_agent.audit import (
    GENESIS_HASH,
    LEGACY_HASH_VERSION,
    AuditChainError,
    append_audit,
    canonical_json,
    canonical_timestamp,
    verify_audit_chain,
)
from medicare_agent.models import AuditEvent, AuditHead

KEY = b"k" * 40
AGGREGATE = ("follow_up_case", "case-1")


def append(session: Session, event_type: str, *, key: bytes = KEY) -> AuditEvent:
    event = append_audit(
        session,
        aggregate_type=AGGREGATE[0],
        aggregate_id=AGGREGATE[1],
        event_type=event_type,
        actor_type="service",
        actor_id="test",
        data={"event": event_type},
        key=key,
    )
    session.commit()
    return event


def legacy_event(previous_hash: str, event_type: str) -> AuditEvent:
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    material = canonical_json(
        {
            "aggregate_type": AGGREGATE[0],
            "aggregate_id": AGGREGATE[1],
            "event_type": event_type,
            "actor_type": "service",
            "actor_id": "legacy",
            "data": {},
            "previous_hash": previous_hash,
            "created_at": canonical_timestamp(created_at),
        }
    )
    return AuditEvent(
        aggregate_type=AGGREGATE[0],
        aggregate_id=AGGREGATE[1],
        event_type=event_type,
        actor_type="service",
        actor_id="legacy",
        data_json=json.dumps({}),
        previous_hash=previous_hash,
        event_hash=hashlib.sha256(material.encode()).hexdigest(),
        hash_version=LEGACY_HASH_VERSION,
        created_at=created_at,
    )


def test_chain_is_keyed_sealed_and_linked_within_one_transaction(session: Session) -> None:
    first = append_audit(
        session,
        aggregate_type=AGGREGATE[0],
        aggregate_id=AGGREGATE[1],
        event_type="first",
        actor_type="service",
        actor_id="test",
        data={},
        key=KEY,
    )
    second = append_audit(
        session,
        aggregate_type=AGGREGATE[0],
        aggregate_id=AGGREGATE[1],
        event_type="second",
        actor_type="service",
        actor_id="test",
        data={},
        key=KEY,
    )
    session.commit()
    assert first.previous_hash == GENESIS_HASH
    assert second.previous_hash == first.event_hash
    verification = verify_audit_chain(session, *AGGREGATE, key=KEY)
    assert (verification.valid, verification.sealed, verification.event_count) == (True, True, 2)
    wrong_key = verify_audit_chain(session, *AGGREGATE, key=b"x" * 40)
    assert wrong_key.valid is False and wrong_key.reason == "hash_mismatch"


def test_truncating_the_newest_event_is_detected(session: Session) -> None:
    append(session, "first")
    last = append(session, "second")
    session.execute(delete(AuditEvent).where(AuditEvent.id == last.id))
    session.commit()
    verification = verify_audit_chain(session, *AGGREGATE, key=KEY)
    assert verification.valid is False
    assert verification.reason == "head_mismatch"


def test_deleting_the_head_of_a_keyed_chain_is_detected(session: Session) -> None:
    append(session, "first")
    session.execute(delete(AuditHead))
    session.commit()
    verification = verify_audit_chain(session, *AGGREGATE, key=KEY)
    assert verification.reason == "missing_head"


def test_unlinked_events_are_detected_and_block_appends(session: Session) -> None:
    append(session, "first")
    session.add(legacy_event("f" * 64, "orphan"))
    session.commit()
    assert verify_audit_chain(session, *AGGREGATE, key=KEY).reason == "unlinked_events"
    session.execute(delete(AuditHead))
    session.commit()
    with pytest.raises(AuditChainError, match="unlinked_events"):
        append(session, "after-orphan")
    session.rollback()


def test_fork_detection_without_database_constraint() -> None:
    from medicare_agent.audit import _ordered_chain

    a = legacy_event(GENESIS_HASH, "a")
    b = legacy_event(GENESIS_HASH, "b")
    with pytest.raises(AuditChainError, match="chain_fork"):
        _ordered_chain([a, b])


def test_legacy_chains_verify_unsealed_and_are_sealed_on_next_append(session: Session) -> None:
    first = legacy_event(GENESIS_HASH, "legacy-1")
    session.add(first)
    session.commit()
    second = legacy_event(first.event_hash, "legacy-2")
    session.add(second)
    session.commit()
    legacy = verify_audit_chain(session, *AGGREGATE, key=KEY)
    assert (legacy.valid, legacy.sealed, legacy.event_count) == (True, False, 2)

    appended = append(session, "modern")
    assert appended.previous_hash == second.event_hash
    sealed = verify_audit_chain(session, *AGGREGATE, key=KEY)
    assert (sealed.valid, sealed.sealed, sealed.event_count) == (True, True, 3)


def test_empty_aggregate_verifies_as_unsealed(session: Session) -> None:
    verification = verify_audit_chain(session, "follow_up_case", "nothing-here", key=KEY)
    assert (verification.valid, verification.sealed, verification.event_count) == (
        True,
        False,
        0,
    )


def test_default_key_comes_from_settings(session: Session) -> None:
    append_audit(
        session,
        aggregate_type="follow_up_case",
        aggregate_id="case-default",
        event_type="default-key",
        actor_type="service",
        actor_id="test",
        data={},
    )
    session.commit()
    assert verify_audit_chain(session, "follow_up_case", "case-default").valid
