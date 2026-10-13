from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from medicare_agent.audit import verify_audit_chain
from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings
from medicare_agent.models import (
    AuditEvent,
    Customer,
    FollowUpCase,
    HumanReview,
    OutboxEvent,
    ProviderCall,
)
from medicare_agent.provider import ModelGatewayClient
from medicare_agent.schemas import CaseResponse, ReviewRequest
from medicare_agent.service import (
    AgentService,
    DomainError,
    case_response,
    dispatch_one,
    list_cases,
    recover_stale_provider_runs,
    retry_candidates,
    review_case,
    run_maintenance,
)


def approve(session: Session, case: CaseResponse, **extra: object) -> object:
    return review_case(
        session,
        case.id,
        ReviewRequest.model_validate(
            {
                "decision": "approve",
                "rationale": "Synthetic routine case is suitable for mock follow-up.",
                "expected_version": case.version,
                **extra,
            }
        ),
        "reviewer-1",
    )


def test_agent_run_is_idempotent_audited_and_marks_customer(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    service = AgentService(session, gateway_factory())
    first = service.run("C00001", "stable-key-0001", "worker")
    second = service.run("C00001", "stable-key-0001", "worker")

    assert first.id == second.id
    assert first.status == "pending_review"
    assert first.rule_tier == "routine"
    assert first.provider_attempts == 1
    assert session.scalar(select(func.count()).select_from(FollowUpCase)) == 1
    assert verify_audit_chain(session, "follow_up_case", first.id).valid
    customer = session.get(Customer, "C00001")
    assert customer is not None and customer.follow_up_started_at is not None
    stored = session.get(FollowUpCase, first.id)
    assert stored is not None
    assert case_response(stored, include_snapshot=True).snapshot is not None

    marker = customer.follow_up_started_at
    manual = service.run("C00001", "manual-rerun-0001", "operator")
    assert manual.id != first.id
    session.refresh(customer)
    assert customer.follow_up_started_at == marker


def test_idempotency_key_cannot_cross_customers(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    add_customer("C00002")
    service = AgentService(session, gateway_factory())
    service.run("C00001", "stable-key-0002", "worker")
    with pytest.raises(DomainError, match="another customer") as error:
        service.run("C00002", "stable-key-0002", "worker")
    assert error.value.status_code == 409


def test_missing_consent_evidence_adds_a_non_bypassable_hold(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    from medicare_agent.models import ConsentEvidence

    add_customer("C00001")
    session.query(ConsentEvidence).delete()
    session.commit()
    case = AgentService(session, gateway_factory()).run(
        "C00001", "missing-consent-evidence", "worker"
    )
    assert "consent_evidence_missing" in case.policy_holds
    result = approve(session, case)
    assert result.case.status == "human_escalated"  # type: ignore[attr-defined]
    assert result.dispatch_queued is False  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("options", "error_code"),
    [
        ({"fail": True}, "PROVIDER_UNAVAILABLE"),
        ({"malformed": True}, "PROVIDER_REJECTED"),
        ({"status_code": 401}, "PROVIDER_REJECTED"),
    ],
)
def test_provider_failures_are_visible_and_do_not_create_proposals(
    options: dict[str, object],
    error_code: str,
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    result = AgentService(session, gateway_factory(**options)).run(
        "C00001", f"provider-{error_code}-{len(options)}", "worker"
    )
    assert result.status == "provider_failed"
    assert result.proposal is None
    assert result.provider_error
    call = session.scalar(select(ProviderCall).where(ProviderCall.case_id == result.id))
    assert call is not None and call.error_code == error_code


def test_failed_provider_run_can_be_retried_until_exhausted(
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    failing = AgentService(session, gateway_factory(fail=True))
    case = failing.run("C00001", "retry-flow-0001", "worker")
    assert case.status == "provider_failed"

    retried = failing.retry(case.id, "worker")
    assert retried.status == "provider_failed"
    assert retried.provider_attempts == 2

    succeeded = AgentService(session, gateway_factory()).retry(case.id, "worker")
    assert succeeded.status == "pending_review"
    assert succeeded.provider_attempts == 3
    assert verify_audit_chain(session, "follow_up_case", case.id).valid

    with pytest.raises(DomainError, match="Only provider_failed") as invalid:
        failing.retry(case.id, "worker")
    assert invalid.value.status_code == 409
    with pytest.raises(DomainError, match="not found"):
        failing.retry("missing-case", "worker")

    stored = session.get(FollowUpCase, case.id)
    assert stored is not None
    stored.status = "provider_failed"
    session.commit()
    with pytest.raises(DomainError, match="exhausted"):
        failing.retry(case.id, "worker")
    assert settings.provider_max_attempts == 3


def test_retry_loses_race_to_concurrent_transition(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    service = AgentService(session, gateway_factory(fail=True))
    case = service.run("C00001", "retry-race-0001", "worker")
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    with factory() as stale_session:
        stale = AgentService(stale_session, gateway_factory())
        loaded = stale_session.get(FollowUpCase, case.id)
        assert loaded is not None
        service.retry(case.id, "worker")
        loaded.status = "provider_failed"
        with pytest.raises(DomainError, match="Case changed") as error:
            stale.retry(case.id, "worker")
        assert error.value.code == "VERSION_CONFLICT"


def test_stale_provider_runs_are_recovered_and_scheduled_for_retry(
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
) -> None:
    add_customer("C00001")
    old = datetime.now(UTC) - timedelta(hours=2)
    stuck = FollowUpCase(
        customer_id="C00001",
        idempotency_key="stuck-run-0001",
        status="provider_pending",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
        provider_attempts=1,
        created_at=old,
        updated_at=old,
    )
    session.add(stuck)
    session.commit()

    recovered = recover_stale_provider_runs(
        session, cutoff=datetime.now(UTC) - timedelta(minutes=10), actor_id="worker"
    )
    assert recovered == 1
    session.refresh(stuck)
    assert stuck.status == "provider_failed"
    assert verify_audit_chain(session, "follow_up_case", stuck.id).valid

    # Backoff has not elapsed for a case that was just failed.
    assert retry_candidates(session, settings, now=datetime.now(UTC)) == []
    later = datetime.now(UTC) + timedelta(seconds=settings.provider_retry_delay_seconds + 1)
    assert retry_candidates(session, settings, now=later) == [stuck.id]

    result = run_maintenance(session, settings, "worker")
    assert result.recovered_stale_runs == 0


def test_stale_recovery_skips_case_changed_by_another_worker(
    session: Session, add_customer: Callable[..., object]
) -> None:
    add_customer("C00001")
    old = datetime.now(UTC) - timedelta(hours=2)
    stuck = FollowUpCase(
        customer_id="C00001",
        idempotency_key="stuck-run-0002",
        status="provider_pending",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
        created_at=old,
        updated_at=old,
    )
    session.add(stuck)
    session.commit()
    original_scalars = session.scalars

    def scalars_then_finish(statement: object) -> object:
        result = original_scalars(statement)  # type: ignore[call-overload]
        stuck.status = "pending_review"
        session.commit()
        return result

    session.scalars = scalars_then_finish  # type: ignore[method-assign]
    try:
        recovered = recover_stale_provider_runs(
            session, cutoff=datetime.now(UTC) - timedelta(minutes=10), actor_id="worker"
        )
    finally:
        session.scalars = original_scalars  # type: ignore[method-assign]
    assert recovered == 0


def test_human_approval_queues_and_dispatches_mock_event(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "approve-flow-1", "worker")
    reviewed = approve(session, case)
    assert reviewed.case.status == "approved"  # type: ignore[attr-defined]
    assert reviewed.dispatch_queued is True  # type: ignore[attr-defined]
    review = session.scalar(select(HumanReview).where(HumanReview.case_id == case.id))
    assert review is not None and review.approved_message == case.proposal.patient_message  # type: ignore[union-attr]

    dispatched = dispatch_one(session, "dispatch-worker", caller_factory())
    assert dispatched.processed is True
    assert dispatched.status == "delivered"
    assert session.get(FollowUpCase, case.id).status == "dispatched"  # type: ignore[union-attr]
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.case_id == case.id))
    assert event is not None and event.external_id and event.external_id.startswith("mock-call-")
    payload = json.loads(event.payload_json)
    assert payload["message"] == case.proposal.patient_message  # type: ignore[union-attr]
    assert payload["message"] != case.proposal.recommended_action  # type: ignore[union-attr]
    assert verify_audit_chain(session, "follow_up_case", case.id).valid


def test_reviewer_edit_must_pass_message_guardrails(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "edit-message-1", "worker")
    with pytest.raises(DomainError, match="guardrail_embedded_contact_target") as error:
        approve(session, case, patient_message="Please call us back at 555-123-4567 today.")
    assert error.value.status_code == 422
    stored = session.get(FollowUpCase, case.id)
    assert stored is not None and stored.status == "pending_review"


def test_approval_without_a_proposal_is_rejected(
    session: Session, add_customer: Callable[..., object]
) -> None:
    add_customer("C00001")
    case = FollowUpCase(
        customer_id="C00001",
        idempotency_key="no-proposal-1",
        status="pending_review",
        rule_tier="routine",
        rule_reasons_json='["test"]',
        policy_holds_json="[]",
    )
    session.add(case)
    session.commit()
    with pytest.raises(DomainError, match="no proposal"):
        approve(session, case_response(case))


def test_on_hold_cases_can_be_re_reviewed_and_escalations_closed(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    add_customer("C00002", urgent=True)
    routine = AgentService(session, gateway_factory()).run("C00001", "hold-flow-1", "worker")
    held = review_case(
        session,
        routine.id,
        ReviewRequest(
            decision="hold", rationale="Waiting for more synthetic context.", expected_version=2
        ),
        "reviewer-1",
    )
    assert held.case.status == "on_hold"
    with pytest.raises(DomainError, match="Only escalated cases can be closed"):
        review_case(
            session,
            routine.id,
            ReviewRequest(
                decision="close", rationale="Close is not valid for held cases.", expected_version=3
            ),
            "reviewer-1",
        )
    resumed = approve(session, held.case)
    assert resumed.case.status == "approved"  # type: ignore[attr-defined]

    urgent = AgentService(session, gateway_factory()).run("C00002", "close-flow-1", "worker")
    escalated = approve(session, urgent)
    assert escalated.case.status == "human_escalated"  # type: ignore[attr-defined]
    with pytest.raises(DomainError, match="can only be closed"):
        approve(session, escalated.case)  # type: ignore[attr-defined]
    closed = review_case(
        session,
        urgent.id,
        ReviewRequest(
            decision="close",
            rationale="Clinician handled the synthetic escalation offline.",
            expected_version=escalated.case.version,  # type: ignore[attr-defined]
        ),
        "reviewer-1",
    )
    assert closed.case.status == "closed"
    assert closed.dispatch_queued is False


def test_urgent_case_never_enters_dispatch(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001", urgent=True)
    case = AgentService(session, gateway_factory()).run("C00001", "urgent-flow-1", "worker")
    result = approve(session, case)
    assert result.case.status == "human_escalated"  # type: ignore[attr-defined]
    assert result.dispatch_queued is False  # type: ignore[attr-defined]
    assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_model_treatment_instruction_adds_non_bypassable_hold(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory(prohibited=True)).run(
        "C00001", "policy-scan-1", "worker"
    )
    assert "guardrail_treatment_instruction" in case.policy_holds
    result = approve(session, case)
    assert result.case.status == "human_escalated"  # type: ignore[attr-defined]
    assert not result.dispatch_queued  # type: ignore[attr-defined]


def test_caller_outage_is_retried_without_marking_case_dispatched(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "caller-outage-1", "worker")
    approve(session, case)

    result = dispatch_one(session, "dispatch-worker", caller_factory(fail=True))
    assert result.status == "retry_scheduled"
    stored_case = session.get(FollowUpCase, case.id)
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.case_id == case.id))
    assert stored_case is not None and stored_case.status == "approved"
    assert event is not None and event.status == "pending" and event.last_error
    assert event.attempts == 1


def test_caller_rejection_dead_letters_immediately(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "caller-reject-1", "worker")
    approve(session, case)
    result = dispatch_one(session, "dispatch-worker", caller_factory(status_code=422))
    assert result.status == "dead_letter"


def test_expired_dispatch_leases_are_reclaimed_or_dead_lettered(
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    add_customer("C00002")
    first = AgentService(session, gateway_factory()).run("C00001", "lease-flow-1", "worker")
    second = AgentService(session, gateway_factory()).run("C00002", "lease-flow-2", "worker")
    approve(session, first)
    approve(session, second)
    expired_at = datetime.now(UTC) - timedelta(seconds=settings.outbox_lease_seconds + 60)
    events = {event.case_id: event for event in session.scalars(select(OutboxEvent)).all()}
    events[first.id].status = "processing"
    events[first.id].claimed_at = expired_at
    events[first.id].attempts = 1
    events[second.id].status = "processing"
    events[second.id].claimed_at = expired_at
    events[second.id].attempts = settings.caller_max_attempts
    session.commit()

    result = dispatch_one(session, "dispatch-worker", caller_factory())
    assert result.status == "delivered"
    assert result.case_id == first.id
    session.refresh(events[second.id])
    assert events[second.id].status == "dead_letter"


def test_lease_reclaim_skips_event_completed_concurrently(
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "lease-race-1", "worker")
    approve(session, case)
    event = session.scalar(select(OutboxEvent))
    assert event is not None
    event.status = "processing"
    event.claimed_at = datetime.now(UTC) - timedelta(seconds=settings.outbox_lease_seconds + 60)
    session.commit()
    original_execute = session.execute

    def execute_after_completion(statement: object, *args: object, **kwargs: object) -> object:
        if "outbox_events" in str(statement) and "UPDATE" in str(statement):
            session.execute = original_execute  # type: ignore[method-assign]
            original_execute(
                OutboxEvent.__table__.update().values(status="delivered")  # type: ignore[attr-defined]
            )
        return original_execute(statement, *args, **kwargs)  # type: ignore[arg-type]

    session.execute = execute_after_completion  # type: ignore[method-assign]
    result = dispatch_one(session, "dispatch-worker", caller_factory())
    assert result.status == "empty"


def test_stale_review_version_is_rejected(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "stale-review-1", "worker")
    with pytest.raises(DomainError, match="refresh") as error:
        review_case(
            session,
            case.id,
            ReviewRequest(
                decision="hold",
                rationale="This is based on a stale browser version.",
                expected_version=case.version - 1,
            ),
            "reviewer-1",
        )
    assert error.value.status_code == 409


def test_two_loaded_reviewer_sessions_cannot_both_decide(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "two-reviewers-1", "worker")
    factory = sessionmaker(bind=session.get_bind(), expire_on_commit=False)
    with factory() as reviewer_a, factory() as reviewer_b:
        loaded_a = reviewer_a.get(FollowUpCase, case.id)
        stale_b = reviewer_b.get(FollowUpCase, case.id)
        assert loaded_a is not None
        assert stale_b is not None
        request = ReviewRequest(
            decision="hold",
            rationale="First reviewer owns the synthetic decision.",
            expected_version=case.version,
        )
        first = review_case(reviewer_a, case.id, request, "reviewer-a")
        assert first.case.status == "on_hold"
        with pytest.raises(DomainError, match="refresh") as error:
            review_case(reviewer_b, case.id, request, "reviewer-b")
        assert error.value.status_code == 409


def test_injected_outbox_for_non_approved_case_is_dead_lettered(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001", urgent=True)
    case = AgentService(session, gateway_factory()).run("C00001", "dispatch-bypass-1", "worker")
    result = approve(session, case)
    assert result.case.status == "human_escalated"  # type: ignore[attr-defined]
    injected = OutboxEvent(
        case_id=case.id,
        event_type="mock_follow_up_requested",
        payload_json=json.dumps({"synthetic": True, "channel": "portal"}),
    )
    session.add(injected)
    session.commit()
    with pytest.raises(DomainError, match="blocked dispatch"):
        dispatch_one(session, "malicious-worker", caller_factory())
    session.refresh(injected)
    assert injected.status == "dead_letter"
    blocked = session.scalar(
        select(AuditEvent).where(
            AuditEvent.aggregate_id == case.id, AuditEvent.event_type == "dispatch_blocked"
        )
    )
    assert blocked is not None


def test_audit_tampering_is_detected(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "tamper-test-1", "worker")
    event = session.scalar(
        select(AuditEvent).where(AuditEvent.aggregate_id == case.id).order_by(AuditEvent.created_at)
    )
    assert event is not None
    event.data_json = json.dumps({"tampered": True})
    session.commit()
    verification = verify_audit_chain(session, "follow_up_case", case.id)
    assert verification.valid is False
    assert verification.reason == "hash_mismatch"


def test_list_cases_orders_oldest_first_by_default(
    session: Session, add_customer: Callable[..., object]
) -> None:
    add_customer("C00001")
    now = datetime.now(UTC)
    for index in range(3):
        session.add(
            FollowUpCase(
                id=f"case-{index}",
                customer_id="C00001",
                idempotency_key=f"order-key-{index}",
                status="pending_review",
                rule_tier="routine",
                rule_reasons_json='["test"]',
                policy_holds_json="[]",
                created_at=now + timedelta(minutes=index),
                updated_at=now,
            )
        )
    session.commit()
    oldest = list_cases(session, status="pending_review", limit=10, offset=0)
    newest = list_cases(session, status=None, limit=10, offset=0, oldest_first=False)
    assert [case.id for case in oldest.items] == ["case-0", "case-1", "case-2"]
    assert [case.id for case in newest.items] == ["case-2", "case-1", "case-0"]
    assert oldest.total == 3
