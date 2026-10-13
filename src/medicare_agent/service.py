from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from medicare_agent.audit import append_audit
from medicare_agent.caller import CallerClient, CallerError, CallerRejectedError
from medicare_agent.config import Settings
from medicare_agent.guardrails import evaluate_patient_message, evaluate_proposal
from medicare_agent.models import (
    ConsentEvidence,
    Customer,
    FollowUpCase,
    HumanReview,
    Observation,
    OutboxEvent,
    ProviderCall,
    as_utc,
    utcnow,
)
from medicare_agent.provider import ModelGatewayClient, ProviderError
from medicare_agent.risk_rules import assess
from medicare_agent.schemas import (
    AgentProposal,
    CallerRequest,
    CaseResponse,
    CaseStatus,
    CustomerSnapshot,
    DispatchResponse,
    GatewayProposalRequest,
    MaintenanceResponse,
    ReviewRequest,
    ReviewResponse,
    RuleAssessment,
)

logger = logging.getLogger(__name__)

CASE_AGGREGATE = "follow_up_case"
CONSENT_PURPOSE = "mock_follow_up"
REVIEWABLE_STATUSES = frozenset({CaseStatus.PENDING_REVIEW, CaseStatus.ON_HOLD})
STALE_PROVIDER_ERROR = "Provider run did not finish before the stale-run threshold"
LEASE_EXPIRED_ERROR = "Dispatch lease expired before the caller result was recorded"
MAINTENANCE_BATCH_SIZE = 100
LEASE_RECLAIM_BATCH_SIZE = 50
CLAIM_CANDIDATES = 5
MAX_OUTBOX_BACKOFF_SECONDS = 300

ConsentStatus = Literal["synthetic_opted_in", "synthetic_withheld"]


class DomainError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


def _loads_list(value: str) -> list[str]:
    parsed = json.loads(value)
    return [str(item) for item in parsed]


def _rowcount(result: Any) -> int:
    return cast(CursorResult[Any], result).rowcount


def case_response(case: FollowUpCase, *, include_snapshot: bool = False) -> CaseResponse:
    return CaseResponse(
        id=case.id,
        customer_id=case.customer_id,
        status=CaseStatus(case.status),
        rule_tier=case.rule_tier,
        rule_reasons=_loads_list(case.rule_reasons_json),
        policy_holds=_loads_list(case.policy_holds_json),
        proposal=AgentProposal.model_validate_json(case.proposal_json)
        if case.proposal_json
        else None,
        provider_error=case.provider_error,
        provider_attempts=case.provider_attempts,
        version=case.version,
        # SQLite hands back naive datetimes; emit explicit UTC so clients never read local time.
        created_at=as_utc(case.created_at),
        updated_at=as_utc(case.updated_at),
        snapshot=CustomerSnapshot.model_validate_json(case.snapshot_json)
        if include_snapshot and case.snapshot_json
        else None,
    )


def _snapshot(
    customer: Customer,
    observation: Observation,
    *,
    consent_status: ConsentStatus | None = None,
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


@dataclass(frozen=True)
class PreparedRun:
    customer: Customer
    snapshot: CustomerSnapshot
    assessment: RuleAssessment


def _effective_consent(session: Session, customer: Customer) -> tuple[ConsentStatus, list[str]]:
    consent = session.scalar(
        select(ConsentEvidence)
        .where(
            ConsentEvidence.customer_id == customer.id,
            ConsentEvidence.purpose == CONSENT_PURPOSE,
        )
        .order_by(ConsentEvidence.version.desc())
        .limit(1)
    )
    effective: ConsentStatus = (
        "synthetic_opted_in"
        if customer.consent_status == "synthetic_opted_in"
        else "synthetic_withheld"
    )
    if consent is None:
        return "synthetic_withheld", ["consent_evidence_missing"]
    if consent.status != "synthetic_opted_in":
        return "synthetic_withheld", ["consent_not_granted"]
    if consent.expires_at is not None and as_utc(consent.expires_at) <= utcnow():
        return "synthetic_withheld", ["consent_evidence_expired"]
    return effective, []


def _prepare(session: Session, customer_id: str) -> PreparedRun:
    row = session.execute(
        select(Customer, Observation)
        .join(Observation, Observation.customer_id == Customer.id)
        .where(Customer.id == customer_id)
    ).one_or_none()
    if row is None:
        raise DomainError("CUSTOMER_NOT_FOUND", "Synthetic customer not found", 404)
    customer, observation = row
    if not customer.synthetic:
        raise DomainError("REAL_DATA_BLOCKED", "Only synthetic customer records are accepted", 403)
    effective_consent, consent_holds = _effective_consent(session, customer)
    snapshot = _snapshot(customer, observation, consent_status=effective_consent)
    assessment = assess(snapshot)
    assessment.policy_holds = sorted({*assessment.policy_holds, *consent_holds})
    return PreparedRun(customer=customer, snapshot=snapshot, assessment=assessment)


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

        prepared = _prepare(self.session, customer_id)
        case = FollowUpCase(
            customer_id=prepared.customer.id,
            idempotency_key=idempotency_key,
            status=CaseStatus.PROVIDER_PENDING,
            rule_tier=prepared.assessment.tier.value,
            rule_reasons_json=json.dumps(prepared.assessment.reasons),
            policy_holds_json=json.dumps(prepared.assessment.policy_holds),
            snapshot_json=prepared.snapshot.model_dump_json(),
            provider_attempts=1,
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
        if prepared.customer.follow_up_started_at is None:
            prepared.customer.follow_up_started_at = utcnow()
        append_audit(
            self.session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case.id,
            event_type="agent_run_started",
            actor_type="service",
            actor_id=actor_id,
            data={
                "customer_id": prepared.customer.id,
                "attempt": 1,
                "rule_assessment": prepared.assessment.model_dump(mode="json"),
            },
        )
        self.session.commit()
        return self._request_proposal(case, prepared, actor_id)

    def retry(self, case_id: str, actor_id: str) -> CaseResponse:
        case = self.session.get(FollowUpCase, case_id)
        if case is None:
            raise DomainError("CASE_NOT_FOUND", "Follow-up case not found", 404)
        if case.status != CaseStatus.PROVIDER_FAILED:
            raise DomainError(
                "INVALID_STATE",
                f"Only provider_failed cases can be retried, not {case.status}",
                409,
            )
        if case.provider_attempts >= self.gateway.settings.provider_max_attempts:
            raise DomainError("RETRY_EXHAUSTED", "Provider attempts are exhausted", 409)

        prepared = _prepare(self.session, case.customer_id)
        attempt = case.provider_attempts + 1
        transition = self.session.execute(
            update(FollowUpCase)
            .where(
                FollowUpCase.id == case.id,
                FollowUpCase.status == CaseStatus.PROVIDER_FAILED,
                FollowUpCase.version == case.version,
            )
            .values(
                status=CaseStatus.PROVIDER_PENDING,
                version=FollowUpCase.version + 1,
                provider_attempts=attempt,
                provider_error=None,
                rule_tier=prepared.assessment.tier.value,
                rule_reasons_json=json.dumps(prepared.assessment.reasons),
                policy_holds_json=json.dumps(prepared.assessment.policy_holds),
                snapshot_json=prepared.snapshot.model_dump_json(),
                updated_at=utcnow(),
            )
        )
        if _rowcount(transition) != 1:
            self.session.rollback()
            raise DomainError("VERSION_CONFLICT", "Case changed while scheduling a retry", 409)
        append_audit(
            self.session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case.id,
            event_type="agent_run_retried",
            actor_type="service",
            actor_id=actor_id,
            data={
                "attempt": attempt,
                "rule_assessment": prepared.assessment.model_dump(mode="json"),
            },
        )
        self.session.commit()
        self.session.refresh(case)
        return self._request_proposal(case, prepared, actor_id)

    def _request_proposal(
        self, case: FollowUpCase, prepared: PreparedRun, actor_id: str
    ) -> CaseResponse:
        try:
            result = self.gateway.propose(
                GatewayProposalRequest(
                    case_id=case.id,
                    snapshot=prepared.snapshot,
                    rule_assessment=prepared.assessment,
                )
            )
        except ProviderError as exc:
            self._record_provider_failure(case, exc, actor_id)
            return case_response(case)

        proposal = result.response.proposal
        guardrails = evaluate_proposal(proposal, prepared.assessment, prepared.snapshot)
        policy_holds = sorted({*prepared.assessment.policy_holds, *guardrails.holds})
        case.policy_holds_json = json.dumps(policy_holds)
        case.proposal_json = proposal.model_dump_json()
        case.provider_error = None
        case.status = CaseStatus.PENDING_REVIEW
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
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case.id,
            event_type="agent_proposal_created",
            actor_type="model_gateway",
            actor_id=result.response.provider,
            data={
                "model": result.response.model,
                "proposal": proposal.model_dump(mode="json"),
                "policy_holds": policy_holds,
                "guardrail_checks": guardrails.checks,
            },
        )
        self.session.commit()
        return case_response(case)

    def _record_provider_failure(
        self, case: FollowUpCase, error: ProviderError, actor_id: str
    ) -> None:
        case.status = CaseStatus.PROVIDER_FAILED
        case.provider_error = str(error)[:300]
        case.version += 1
        self.session.add(
            ProviderCall(
                case_id=case.id,
                provider="model-gateway",
                model="unknown",
                status="failed",
                latency_ms=0,
                error_code=error.code,
            )
        )
        append_audit(
            self.session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case.id,
            event_type="agent_run_failed",
            actor_type="service",
            actor_id=actor_id,
            data={"error_code": error.code, "attempt": case.provider_attempts},
        )
        self.session.commit()
        logger.warning(
            "model gateway call failed",
            extra={"event": "provider_failed", "case_id": case.id, "error_code": error.code},
        )


def recover_stale_provider_runs(
    session: Session,
    *,
    cutoff: datetime,
    actor_id: str,
    limit: int = MAINTENANCE_BATCH_SIZE,
) -> int:
    """Fail runs left in provider_pending by a crash so the retry path can pick them up."""

    case_ids = session.scalars(
        select(FollowUpCase.id)
        .where(
            FollowUpCase.status == CaseStatus.PROVIDER_PENDING,
            FollowUpCase.updated_at < cutoff,
        )
        .order_by(FollowUpCase.updated_at)
        .limit(limit)
    ).all()
    recovered = 0
    for case_id in case_ids:
        transition = session.execute(
            update(FollowUpCase)
            .where(
                FollowUpCase.id == case_id,
                FollowUpCase.status == CaseStatus.PROVIDER_PENDING,
                FollowUpCase.updated_at < cutoff,
            )
            .values(
                status=CaseStatus.PROVIDER_FAILED,
                provider_error=STALE_PROVIDER_ERROR,
                version=FollowUpCase.version + 1,
                updated_at=utcnow(),
            )
        )
        if _rowcount(transition) != 1:
            session.rollback()
            continue
        session.add(
            ProviderCall(
                case_id=case_id,
                provider="model-gateway",
                model="unknown",
                status="failed",
                latency_ms=0,
                error_code="PROVIDER_STALE",
            )
        )
        append_audit(
            session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case_id,
            event_type="agent_run_recovered",
            actor_type="service",
            actor_id=actor_id,
            data={"error_code": "PROVIDER_STALE"},
        )
        session.commit()
        recovered += 1
    if recovered:
        logger.warning(
            "stale provider runs recovered",
            extra={"event": "provider_stale_recovered", "recovered": recovered},
        )
    return recovered


def retry_candidates(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    limit: int = MAINTENANCE_BATCH_SIZE,
) -> list[str]:
    """Failed runs with attempts left whose exponential backoff has elapsed."""

    rows = session.execute(
        select(FollowUpCase.id, FollowUpCase.provider_attempts, FollowUpCase.updated_at)
        .where(
            FollowUpCase.status == CaseStatus.PROVIDER_FAILED,
            FollowUpCase.provider_attempts < settings.provider_max_attempts,
        )
        .order_by(FollowUpCase.updated_at)
        .limit(limit)
    ).all()
    eligible: list[str] = []
    for case_id, attempts, updated_at in rows:
        delay = settings.provider_retry_delay_seconds * 2 ** max(attempts - 1, 0)
        if as_utc(updated_at) + timedelta(seconds=delay) <= now:
            eligible.append(case_id)
    return eligible


def run_maintenance(session: Session, settings: Settings, actor_id: str) -> MaintenanceResponse:
    now = utcnow()
    recovered = recover_stale_provider_runs(
        session,
        cutoff=now - timedelta(seconds=settings.provider_stale_seconds),
        actor_id=actor_id,
    )
    return MaintenanceResponse(
        recovered_stale_runs=recovered,
        retry_case_ids=retry_candidates(session, settings, now=now),
    )


def _next_status(current: str, decision: str, holds: list[str]) -> CaseStatus:
    if current in REVIEWABLE_STATUSES:
        if decision == "approve":
            return CaseStatus.HUMAN_ESCALATED if holds else CaseStatus.APPROVED
        if decision == "reject":
            return CaseStatus.REJECTED
        if decision == "hold":
            return CaseStatus.ON_HOLD
        raise DomainError("INVALID_DECISION", "Only escalated cases can be closed", 409)
    if current == CaseStatus.HUMAN_ESCALATED:
        if decision == "close":
            return CaseStatus.CLOSED
        raise DomainError(
            "INVALID_DECISION",
            "Escalated cases are handled outside automation and can only be closed",
            409,
        )
    raise DomainError("INVALID_STATE", f"Case cannot be reviewed from status {current}", 409)


def _approved_message(case: FollowUpCase, request: ReviewRequest) -> str:
    if not case.proposal_json:
        raise DomainError("PROPOSAL_MISSING", "The case has no proposal to approve", 409)
    if request.patient_message is None:
        return AgentProposal.model_validate_json(case.proposal_json).patient_message
    message_holds = evaluate_patient_message(request.patient_message)
    if message_holds:
        raise DomainError(
            "MESSAGE_GUARDRAIL",
            f"The edited patient message failed guardrails: {', '.join(message_holds)}",
            422,
        )
    return request.patient_message


def review_case(
    session: Session,
    case_id: str,
    request: ReviewRequest,
    reviewer_id: str,
) -> ReviewResponse:
    case = session.get(FollowUpCase, case_id)
    if case is None:
        raise DomainError("CASE_NOT_FOUND", "Follow-up case not found", 404)
    previous_status = case.status
    holds = _loads_list(case.policy_holds_json)
    new_status = _next_status(previous_status, request.decision, holds)
    if case.version != request.expected_version:
        raise DomainError("VERSION_CONFLICT", "Case changed; refresh before reviewing", 409)
    message = _approved_message(case, request) if new_status == CaseStatus.APPROVED else None

    transition = session.execute(
        update(FollowUpCase)
        .where(
            FollowUpCase.id == case.id,
            FollowUpCase.status == previous_status,
            FollowUpCase.version == request.expected_version,
        )
        .values(status=new_status, version=FollowUpCase.version + 1, updated_at=utcnow())
    )
    if _rowcount(transition) != 1:
        session.rollback()
        raise DomainError("VERSION_CONFLICT", "Case changed; refresh before reviewing", 409)

    session.add(
        HumanReview(
            case_id=case.id,
            reviewer_id=reviewer_id,
            decision=request.decision,
            rationale=request.rationale,
            approved_message=message,
            case_version=request.expected_version,
        )
    )
    dispatch_queued = message is not None
    if message is not None:
        proposal = AgentProposal.model_validate_json(case.proposal_json or "")
        session.add(
            OutboxEvent(
                case_id=case.id,
                event_type="mock_follow_up_requested",
                payload_json=json.dumps(
                    {
                        "customer_id": case.customer_id,
                        "channel": proposal.contact_channel.value,
                        "message": message,
                        "synthetic": True,
                    },
                    sort_keys=True,
                ),
            )
        )
    append_audit(
        session,
        aggregate_type=CASE_AGGREGATE,
        aggregate_id=case.id,
        event_type="human_review_recorded",
        actor_type="human_reviewer",
        actor_id=reviewer_id,
        data={
            "decision": request.decision,
            "rationale": request.rationale,
            "previous_status": previous_status,
            "new_status": new_status.value,
            "dispatch_queued": dispatch_queued,
            "patient_message_edited": request.patient_message is not None,
            "approved_message": message,
        },
    )
    session.commit()
    session.refresh(case)
    return ReviewResponse(
        case=case_response(case), decision=request.decision, dispatch_queued=dispatch_queued
    )


def _reclaim_expired_leases(session: Session, settings: Settings, actor_id: str) -> None:
    """Return events whose dispatcher died mid-call to the queue; the caller is idempotent."""

    cutoff = utcnow() - timedelta(seconds=settings.outbox_lease_seconds)
    expired = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == "processing", OutboxEvent.claimed_at < cutoff)
        .order_by(OutboxEvent.claimed_at)
        .limit(LEASE_RECLAIM_BATCH_SIZE)
    ).all()
    for event in expired:
        exhausted = event.attempts >= settings.caller_max_attempts
        new_status = "dead_letter" if exhausted else "pending"
        result = session.execute(
            update(OutboxEvent)
            .where(
                OutboxEvent.id == event.id,
                OutboxEvent.status == "processing",
                OutboxEvent.claimed_at == event.claimed_at,
            )
            .values(status=new_status, available_at=utcnow(), last_error=LEASE_EXPIRED_ERROR)
        )
        if _rowcount(result) != 1:
            session.rollback()
            continue
        append_audit(
            session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=event.case_id,
            event_type="outbox_lease_expired",
            actor_type="service",
            actor_id=actor_id,
            data={"outbox_event_id": event.id, "status": new_status},
        )
        session.commit()


def _claim_next(session: Session) -> OutboxEvent | None:
    candidates = session.scalars(
        select(OutboxEvent)
        .where(OutboxEvent.status == "pending", OutboxEvent.available_at <= utcnow())
        .order_by(OutboxEvent.available_at, OutboxEvent.id)
        .limit(CLAIM_CANDIDATES)
    ).all()
    for candidate in candidates:
        result = session.execute(
            update(OutboxEvent)
            .where(OutboxEvent.id == candidate.id, OutboxEvent.status == "pending")
            .values(
                status="processing",
                claimed_at=utcnow(),
                attempts=OutboxEvent.attempts + 1,
            )
        )
        if _rowcount(result) == 1:
            session.commit()
            # Reload the claimed row so attempts and status reflect the committed lease.
            session.refresh(candidate)
            return candidate
        session.rollback()
    return None


def _block_dispatch(
    session: Session,
    event: OutboxEvent,
    actor_id: str,
    *,
    reason: str,
    code: str,
    message: str,
    status_code: int,
) -> DomainError:
    event.status = "dead_letter"
    event.last_error = reason
    append_audit(
        session,
        aggregate_type=CASE_AGGREGATE,
        aggregate_id=event.case_id,
        event_type="dispatch_blocked",
        actor_type="service",
        actor_id=actor_id,
        data={"outbox_event_id": event.id, "reason": reason},
    )
    session.commit()
    logger.error(
        "dispatch blocked by safety invariant",
        extra={"event": "dispatch_blocked", "case_id": event.case_id, "error_code": code},
    )
    return DomainError(code, message, status_code)


def dispatch_one(
    session: Session,
    actor_id: str,
    caller: CallerClient,
) -> DispatchResponse:
    settings = caller.settings
    _reclaim_expired_leases(session, settings, actor_id)
    claimed = _claim_next(session)
    if claimed is None:
        return DispatchResponse(processed=False, status="empty")

    case = session.get(FollowUpCase, claimed.case_id)
    if case is None or case.status != CaseStatus.APPROVED or _loads_list(case.policy_holds_json):
        raise _block_dispatch(
            session,
            claimed,
            actor_id,
            reason="Safety invariant failed: case is not dispatchable",
            code="DISPATCH_BLOCKED",
            message="Safety invariant blocked dispatch",
            status_code=409,
        )

    payload = json.loads(claimed.payload_json)
    if payload.get("synthetic") is not True:
        raise _block_dispatch(
            session,
            claimed,
            actor_id,
            reason="Non-synthetic payload blocked",
            code="REAL_DISPATCH_BLOCKED",
            message="Only synthetic mock dispatch is supported",
            status_code=403,
        )

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
    except CallerError as exc:
        claimed.last_error = str(exc)[:300]
        permanent = isinstance(exc, CallerRejectedError)
        if permanent or claimed.attempts >= settings.caller_max_attempts:
            claimed.status = "dead_letter"
            status = "dead_letter"
        else:
            claimed.status = "pending"
            claimed.available_at = utcnow() + timedelta(
                seconds=min(MAX_OUTBOX_BACKOFF_SECONDS, 2**claimed.attempts)
            )
            status = "retry_scheduled"
        append_audit(
            session,
            aggregate_type=CASE_AGGREGATE,
            aggregate_id=case.id,
            event_type="caller_service_failed",
            actor_type="service",
            actor_id=actor_id,
            data={"outbox_event_id": claimed.id, "status": status, "permanent": permanent},
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
    case.status = CaseStatus.DISPATCHED
    case.version += 1
    append_audit(
        session,
        aggregate_type=CASE_AGGREGATE,
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


def list_cases(
    session: Session,
    *,
    status: str | None,
    limit: int,
    offset: int,
    oldest_first: bool = True,
) -> CasePage:
    filters = [FollowUpCase.status == status] if status else []
    total = session.scalar(select(func.count()).select_from(FollowUpCase).where(*filters)) or 0
    ordering = (
        (FollowUpCase.created_at.asc(), FollowUpCase.id.asc())
        if oldest_first
        else (FollowUpCase.created_at.desc(), FollowUpCase.id.desc())
    )
    items = list(
        session.scalars(
            select(FollowUpCase).where(*filters).order_by(*ordering).limit(limit).offset(offset)
        )
    )
    return CasePage(items=items, total=total)
