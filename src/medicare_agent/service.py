from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from medicare_agent.audit import append_audit
from medicare_agent.caller import CallerClient, CallerUnavailableError
from medicare_agent.guardrails import evaluate_proposal
from medicare_agent.models import (
    ConsentEvidence,
    Customer,
    FollowUpCase,
    HumanReview,
    Observation,
    OutboxEvent,
    ProviderCall,
    utcnow,
)
from medicare_agent.provider import ModelGatewayClient, ProviderUnavailableError
from medicare_agent.risk_rules import assess
from medicare_agent.schemas import (
    AgentProposal,
    CallerRequest,
    CaseResponse,
    CustomerSnapshot,
    DispatchResponse,
    GatewayProposalRequest,
    ReviewRequest,
    ReviewResponse,
)

logger = logging.getLogger(__name__)


class DomainError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _loads_list(value: str) -> list[str]:
    parsed = json.loads(value)
    return [str(item) for item in parsed]


def case_response(case: FollowUpCase) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        customer_id=case.customer_id,
        status=case.status,
        rule_tier=case.rule_tier,
        rule_reasons=_loads_list(case.rule_reasons_json),
        policy_holds=_loads_list(case.policy_holds_json),
        proposal=AgentProposal.model_validate_json(case.proposal_json)
        if case.proposal_json
        else None,
        provider_error=case.provider_error,
        version=case.version,
        created_at=case.created_at,
        updated_at=case.updated_at,
    )


def _snapshot(
    customer: Customer,
    observation: Observation,
    *,
    consent_status: Literal["synthetic_opted_in", "synthetic_withheld"] | None = None,
) -> CustomerSnapshot:
    return CustomerSnapshot(
        customer_id=customer.id,
        age=customer.age,
        sex_at_birth=customer.sex_at_birth,
        conditions=json.loads(customer.conditions_json),
        missed_appointment=customer.missed_appointment,
        consent_status=consent_status or customer.consent_status,
        last_contact_at=customer.last_contact_at,
        measured_at=observation.measured_at,
        bp_systolic_mm_hg=observation.bp_systolic_mm_hg,
        bp_diastolic_mm_hg=observation.bp_diastolic_mm_hg,
        heart_rate_bpm=observation.heart_rate_bpm,
        spo2_pct=observation.spo2_pct,
        temperature_c=observation.temperature_c,
        respiratory_rate_bpm=observation.respiratory_rate_bpm,
        weight_kg=observation.weight_kg,
        bmi=observation.bmi,
        creatinine_mg_dl=observation.creatinine_mg_dl,
        egfr_ml_min_1_73m2=observation.egfr_ml_min_1_73m2,
        glucose_mg_dl=observation.glucose_mg_dl,
        hemoglobin_g_dl=observation.hemoglobin_g_dl,
        sodium_mmol_l=observation.sodium_mmol_l,
        potassium_mmol_l=observation.potassium_mmol_l,
        phq9_score=observation.phq9_score,
    )


class AgentService:
    def __init__(self, session: Session, gateway: ModelGatewayClient) -> None:
        self.session = session
        self.gateway = gateway

    def run(self, customer_id: str, idempotency_key: str, actor_id: str) -> CaseResponse:
        existing = self.session.scalar(
            select(FollowUpCase).where(FollowUpCase.idempotency_key == idempotency_key)
        )
        if existing:
            if existing.customer_id != customer_id:
                raise DomainError(
                    "IDEMPOTENCY_CONFLICT",
                    "The idempotency key is already associated with another customer",
                    409,
                )
            return case_response(existing)

        row = self.session.execute(
            select(Customer, Observation)
            .join(Observation, Observation.customer_id == Customer.id)
            .where(Customer.id == customer_id)
        ).one_or_none()
        if row is None:
            raise DomainError("CUSTOMER_NOT_FOUND", "Synthetic customer not found", 404)
        customer, observation = row
        if not customer.synthetic:
            raise DomainError(
                "REAL_DATA_BLOCKED", "Only synthetic customer records are accepted", 403
            )

        consent = self.session.scalar(
            select(ConsentEvidence)
            .where(
                ConsentEvidence.customer_id == customer.id,
                ConsentEvidence.purpose == "mock_follow_up",
            )
            .order_by(ConsentEvidence.version.desc())
            .limit(1)
        )
        consent_holds: list[str] = []
        effective_consent: Literal["synthetic_opted_in", "synthetic_withheld"] = (
            "synthetic_opted_in"
            if customer.consent_status == "synthetic_opted_in"
            else "synthetic_withheld"
        )
        if consent is None:
            effective_consent = "synthetic_withheld"
            consent_holds.append("consent_evidence_missing")
        elif consent.status != "synthetic_opted_in":
            effective_consent = "synthetic_withheld"
            consent_holds.append("consent_not_granted")
        elif consent.expires_at is not None and consent.expires_at <= utcnow():
            effective_consent = "synthetic_withheld"
            consent_holds.append("consent_evidence_expired")

        snapshot = _snapshot(customer, observation, consent_status=effective_consent)
        rule_assessment = assess(snapshot)
        rule_assessment.policy_holds = sorted(set([*rule_assessment.policy_holds, *consent_holds]))
        case = FollowUpCase(
            customer_id=customer.id,
            idempotency_key=idempotency_key,
            status="provider_pending",
            rule_tier=rule_assessment.tier.value,
            rule_reasons_json=json.dumps(rule_assessment.reasons),
            policy_holds_json=json.dumps(rule_assessment.policy_holds),
        )
        self.session.add(case)
        try:
            self.session.flush()
        except IntegrityError as exc:
            self.session.rollback()
            winner = self.session.scalar(
                select(FollowUpCase).where(FollowUpCase.idempotency_key == idempotency_key)
            )
            if winner and winner.customer_id == customer_id:
                return case_response(winner)
            raise DomainError("IDEMPOTENCY_CONFLICT", "Concurrent request conflict", 409) from exc
        append_audit(
            self.session,
            aggregate_type="follow_up_case",
            aggregate_id=case.id,
            event_type="agent_run_started",
            actor_type="service",
            actor_id=actor_id,
            data={
                "customer_id": customer.id,
                "rule_assessment": rule_assessment.model_dump(mode="json"),
            },
        )
        self.session.commit()

        try:
            result = self.gateway.propose(
                GatewayProposalRequest(
                    case_id=case.id, snapshot=snapshot, rule_assessment=rule_assessment
                )
            )
        except ProviderUnavailableError as exc:
            case.status = "provider_failed"
            case.provider_error = str(exc)[:300]
            case.version += 1
            self.session.add(
                ProviderCall(
                    case_id=case.id,
                    provider="model-gateway",
                    model="unknown",
                    status="failed",
                    latency_ms=0,
                    error_code="PROVIDER_UNAVAILABLE",
                )
            )
            append_audit(
                self.session,
                aggregate_type="follow_up_case",
                aggregate_id=case.id,
                event_type="agent_run_failed",
                actor_type="service",
                actor_id=actor_id,
                data={"error_code": "PROVIDER_UNAVAILABLE"},
            )
            self.session.commit()
            logger.warning(
                "model gateway unavailable", extra={"event": "provider_failed", "case_id": case.id}
            )
            return case_response(case)

        guardrails = evaluate_proposal(result.response.proposal, rule_assessment, snapshot)
        policy_holds = rule_assessment.policy_holds + guardrails.holds
        case.policy_holds_json = json.dumps(sorted(set(policy_holds)))
        case.proposal_json = result.response.proposal.model_dump_json()
        case.provider_error = None
        case.status = "pending_review"
        case.version += 1
        self.session.add(
            ProviderCall(
                case_id=case.id,
                provider=result.response.provider,
                model=result.response.model,
                status="succeeded",
                latency_ms=result.latency_ms,
                request_id=result.response.request_id,
            )
        )
        append_audit(
            self.session,
            aggregate_type="follow_up_case",
            aggregate_id=case.id,
            event_type="agent_proposal_created",
            actor_type="model_gateway",
            actor_id=result.response.provider,
            data={
                "model": result.response.model,
                "proposal": result.response.proposal.model_dump(mode="json"),
                "policy_holds": sorted(set(policy_holds)),
                "guardrail_checks": guardrails.checks,
            },
        )
        self.session.commit()
        return case_response(case)


def review_case(
    session: Session,
    case_id: str,
    request: ReviewRequest,
    reviewer_id: str,
) -> ReviewResponse:
    case = session.get(FollowUpCase, case_id)
    if case is None:
        raise DomainError("CASE_NOT_FOUND", "Follow-up case not found", 404)
    if case.status != "pending_review":
        raise DomainError(
            "INVALID_STATE", f"Case cannot be reviewed from status {case.status}", 409
        )
    if case.version != request.expected_version:
        raise DomainError("VERSION_CONFLICT", "Case changed; refresh before reviewing", 409)

    holds = _loads_list(case.policy_holds_json)
    dispatch_queued = False
    if request.decision == "reject":
        new_status = "rejected"
    elif request.decision == "hold":
        new_status = "on_hold"
    elif holds:
        new_status = "human_escalated"
    else:
        new_status = "approved"

    transition = cast(
        CursorResult[Any],
        session.execute(
            update(FollowUpCase)
            .where(
                FollowUpCase.id == case.id,
                FollowUpCase.status == "pending_review",
                FollowUpCase.version == request.expected_version,
            )
            .values(status=new_status, version=FollowUpCase.version + 1, updated_at=utcnow())
        ),
    )
    if transition.rowcount != 1:
        session.rollback()
        raise DomainError("VERSION_CONFLICT", "Case changed; refresh before reviewing", 409)

    session.add(
        HumanReview(
            case_id=case.id,
            reviewer_id=reviewer_id,
            decision=request.decision,
            rationale=request.rationale,
            case_version=request.expected_version,
        )
    )

    if new_status == "approved":
        proposal = AgentProposal.model_validate_json(case.proposal_json or "{}")
        session.add(
            OutboxEvent(
                case_id=case.id,
                event_type="mock_follow_up_requested",
                payload_json=json.dumps(
                    {
                        "customer_id": case.customer_id,
                        "channel": proposal.contact_channel.value,
                        "message": proposal.recommended_action,
                        "synthetic": True,
                    },
                    sort_keys=True,
                ),
            )
        )
        dispatch_queued = True
    append_audit(
        session,
        aggregate_type="follow_up_case",
        aggregate_id=case.id,
        event_type="human_review_recorded",
        actor_type="human_reviewer",
        actor_id=reviewer_id,
        data={
            "decision": request.decision,
            "rationale": request.rationale,
            "new_status": new_status,
            "dispatch_queued": dispatch_queued,
        },
    )
    session.commit()
    session.refresh(case)
    return ReviewResponse(
        case=case_response(case), decision=request.decision, dispatch_queued=dispatch_queued
    )


def dispatch_one(
    session: Session,
    actor_id: str,
    caller: CallerClient,
) -> DispatchResponse:
    candidates = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending", OutboxEvent.available_at <= utcnow())
        .order_by(OutboxEvent.available_at, OutboxEvent.id)
        .limit(5)
    ).all()
    claimed: OutboxEvent | None = None
    for candidate in candidates:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(OutboxEvent)
                .where(OutboxEvent.id == candidate.id, OutboxEvent.status == "pending")
                .values(
                    status="processing",
                    claimed_at=utcnow(),
                    attempts=OutboxEvent.attempts + 1,
                )
            ),
        )
        if result.rowcount == 1:
            session.commit()
            claimed = session.get(OutboxEvent, candidate.id)
            break
        session.rollback()
    if claimed is None:
        return DispatchResponse(processed=False, status="empty")

    case = session.get(FollowUpCase, claimed.case_id)
    if case is None or case.status != "approved" or _loads_list(case.policy_holds_json):
        claimed.status = "dead_letter"
        claimed.last_error = "Safety invariant failed: case is not dispatchable"
        session.commit()
        raise DomainError("DISPATCH_BLOCKED", "Safety invariant blocked dispatch", 409)

    payload = json.loads(claimed.payload_json)
    if payload.get("synthetic") is not True:
        claimed.status = "dead_letter"
        claimed.last_error = "Non-synthetic payload blocked"
        session.commit()
        raise DomainError("REAL_DISPATCH_BLOCKED", "Only synthetic mock dispatch is supported", 403)

    try:
        call = caller.place_call(
            CallerRequest(
                case_id=case.id,
                customer_id=case.customer_id,
                channel=payload["channel"],
                message=payload["message"],
                synthetic=True,
                approved_by_human=True,
            ),
            idempotency_key=claimed.id,
        )
    except CallerUnavailableError as exc:
        claimed.last_error = str(exc)[:300]
        if claimed.attempts >= caller.settings.caller_max_attempts:
            claimed.status = "dead_letter"
            status = "dead_letter"
        else:
            claimed.status = "pending"
            claimed.available_at = utcnow() + timedelta(seconds=min(300, 2**claimed.attempts))
            status = "retry_scheduled"
        append_audit(
            session,
            aggregate_type="follow_up_case",
            aggregate_id=case.id,
            event_type="caller_service_failed",
            actor_type="service",
            actor_id=actor_id,
            data={"outbox_event_id": claimed.id, "status": status},
        )
        session.commit()
        return DispatchResponse(
            processed=True,
            outbox_event_id=claimed.id,
            case_id=case.id,
            status=status,
        )

    claimed.status = "delivered"
    claimed.delivered_at = utcnow()
    claimed.external_id = call.response.external_id
    claimed.last_error = None
    case.status = "dispatched"
    case.version += 1
    append_audit(
        session,
        aggregate_type="follow_up_case",
        aggregate_id=case.id,
        event_type="caller_dispatch_completed",
        actor_type="service",
        actor_id=actor_id,
        data={
            "outbox_event_id": claimed.id,
            "external_id": claimed.external_id,
            "channel": payload["channel"],
            "provider": call.response.provider,
            "latency_ms": call.latency_ms,
        },
    )
    session.commit()
    return DispatchResponse(
        processed=True,
        outbox_event_id=claimed.id,
        case_id=case.id,
        status="delivered",
    )


@dataclass(frozen=True)
class CasePage:
    items: list[FollowUpCase]
    total: int


def list_cases(session: Session, *, status: str | None, limit: int, offset: int) -> CasePage:
    filters = [FollowUpCase.status == status] if status else []
    total = session.scalar(select(func.count()).select_from(FollowUpCase).where(*filters)) or 0
    items = list(
        session.scalars(
            select(FollowUpCase)
            .where(*filters)
            .order_by(FollowUpCase.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    )
    return CasePage(items=items, total=total)
