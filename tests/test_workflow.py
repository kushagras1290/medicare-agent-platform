from __future__ import annotations

import json
from collections.abc import Callable

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from medicare_agent.audit import verify_audit_chain
from medicare_agent.caller import CallerClient
from medicare_agent.models import AuditEvent, FollowUpCase, OutboxEvent
from medicare_agent.provider import ModelGatewayClient
from medicare_agent.schemas import ReviewRequest
from medicare_agent.service import AgentService, DomainError, dispatch_one, review_case


def test_agent_run_is_idempotent_and_audited(
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
    assert session.scalar(select(func.count()).select_from(FollowUpCase)) == 1
    assert verify_audit_chain(session, "follow_up_case", first.id)


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
    result = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Missing consent evidence requires a human-controlled hold.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert result.case.status == "human_escalated"
    assert result.dispatch_queued is False


@pytest.mark.parametrize("mode", ["fail", "malformed"])
def test_provider_failures_are_visible_and_do_not_create_proposals(
    mode: str,
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001")
    gateway = gateway_factory(fail=mode == "fail", malformed=mode == "malformed")
    result = AgentService(session, gateway).run("C00001", f"provider-{mode}", "worker")
    assert result.status == "provider_failed"
    assert result.proposal is None
    assert result.provider_error


def test_human_approval_queues_and_dispatches_mock_event(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "approve-flow-1", "worker")
    reviewed = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Synthetic routine case is suitable for mock follow-up.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert reviewed.case.status == "approved"
    assert reviewed.dispatch_queued is True

    dispatched = dispatch_one(session, "dispatch-worker", caller_factory())
    assert dispatched.processed is True
    assert dispatched.status == "delivered"
    assert session.get(FollowUpCase, case.id).status == "dispatched"  # type: ignore[union-attr]
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.case_id == case.id))
    assert event is not None and event.external_id and event.external_id.startswith("mock-call-")
    assert verify_audit_chain(session, "follow_up_case", case.id)


def test_urgent_case_never_enters_dispatch(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
) -> None:
    add_customer("C00001", urgent=True)
    case = AgentService(session, gateway_factory()).run("C00001", "urgent-flow-1", "worker")
    result = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Escalate this urgent synthetic case to a human.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert result.case.status == "human_escalated"
    assert result.dispatch_queued is False
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
    result = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Route policy violation for human investigation.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert result.case.status == "human_escalated"
    assert not result.dispatch_queued


def test_caller_outage_is_retried_without_marking_case_dispatched(
    session: Session,
    add_customer: Callable[..., object],
    gateway_factory: Callable[..., ModelGatewayClient],
    caller_factory: Callable[..., CallerClient],
) -> None:
    add_customer("C00001")
    case = AgentService(session, gateway_factory()).run("C00001", "caller-outage-1", "worker")
    reviewed = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Approve only the synthetic provider-backed caller test.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert reviewed.case.status == "approved"

    result = dispatch_one(session, "dispatch-worker", caller_factory(fail=True))
    assert result.status == "retry_scheduled"
    stored_case = session.get(FollowUpCase, case.id)
    event = session.scalar(select(OutboxEvent).where(OutboxEvent.case_id == case.id))
    assert stored_case is not None and stored_case.status == "approved"
    assert event is not None and event.status == "pending" and event.last_error


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
    result = review_case(
        session,
        case.id,
        ReviewRequest(
            decision="approve",
            rationale="Synthetic urgent case must remain with a human.",
            expected_version=case.version,
        ),
        "reviewer-1",
    )
    assert result.case.status == "human_escalated"
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
    assert verify_audit_chain(session, "follow_up_case", case.id) is False
