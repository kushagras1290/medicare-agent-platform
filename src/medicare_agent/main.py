from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import Select, func, select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from medicare_agent.audit import verify_audit_chain
from medicare_agent.auth import require_reviewer, require_service
from medicare_agent.caller import CallerClient
from medicare_agent.config import OidcBrowserConfig, Settings, get_settings
from medicare_agent.db import get_engine, get_session
from medicare_agent.governance import validate_release_manifest
from medicare_agent.logging_config import configure_logging
from medicare_agent.models import (
    FollowUpCase,
    OutboxEvent,
    ProviderCall,
    WorkerHeartbeat,
    as_utc,
)
from medicare_agent.provider import ModelGatewayClient
from medicare_agent.schemas import (
    AgentRunRequest,
    ApiEnvelope,
    AuditVerificationResponse,
    CaseStatus,
    ListCasesResponse,
    ReviewRequest,
)
from medicare_agent.service import (
    CASE_AGGREGATE,
    AgentService,
    DomainError,
    case_response,
    dispatch_one,
    list_cases,
    review_case,
    run_maintenance,
)

configure_logging()
logger = logging.getLogger(__name__)

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
# Probes, Prometheus and the service-key worker routes are called over plain HTTP inside the
# backend network (container healthcheck, workers, scraper). Browser-facing reviewer routes and
# the console itself still require HTTPS as reported by a trusted proxy.
HTTPS_EXEMPT_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})
HTTPS_EXEMPT_PREFIXES = ("/api/v1/agent/", "/api/v1/outbox/")
MONITORED_WORKERS = ("automation", "dispatch")
MISSING_HEARTBEAT_AGE_SECONDS = 1_000_000_000

REQUESTS = Counter("medicare_http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("medicare_http_request_seconds", "HTTP latency", ["method", "path"])
PENDING_REVIEWS = Gauge("medicare_pending_reviews", "Cases currently waiting for review")
OLDEST_PENDING_AGE = Gauge(
    "medicare_oldest_pending_review_age_seconds", "Age of the oldest pending review"
)
CASES_BY_STATUS = Gauge("medicare_cases", "Cases by workflow status", ["status"])
DEAD_LETTERS = Gauge("medicare_outbox_dead_letters", "Outbox events in dead-letter state")
OUTBOX_PROCESSING = Gauge("medicare_outbox_processing", "Outbox events currently leased")
PROVIDER_FAILURES = Gauge("medicare_provider_failures", "Persisted failed provider calls")
WORKER_HEARTBEAT_AGE = Gauge(
    "medicare_worker_heartbeat_age_seconds", "Seconds since worker heartbeat", ["worker"]
)
AUDIT_FAILURES = Counter(
    "medicare_audit_verification_failures_total", "Detected audit-chain verification failures"
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    if settings.reviewer_auth_mode == "api_key":
        settings.reviewers()
    else:
        settings.oidc_browser_config()
    settings.hosts()
    if settings.environment in {"staging", "production"}:
        if settings.release_approvals_path is None:
            raise RuntimeError("Release approvals manifest path is required")
        validate_release_manifest(
            settings.release_approvals_path,
            expected_environment=settings.environment,
            expected_sha256=settings.release_approvals_sha256,
        )
    engine = get_engine()
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    logger.info(
        "application started",
        extra={"event": "startup", "environment": settings.environment, "synthetic_only": True},
    )
    yield
    engine.dispose()


app = FastAPI(
    title="MediCare Human-in-the-Loop Agent",
    version="0.1.0",
    description="Synthetic-only decision-support workflow. Not approved for clinical use.",
    lifespan=lifespan,
)
app.add_middleware(TrustedHostMiddleware, allowed_hosts=get_settings().hosts())

package_dir = Path(__file__).parent
app.mount("/static", StaticFiles(directory=package_dir / "static"), name="static")
templates = Jinja2Templates(directory=package_dir / "templates")


def _https_exempt(path: str) -> bool:
    return path in HTTPS_EXEMPT_PATHS or path.startswith(HTTPS_EXEMPT_PREFIXES)


def _browser_oidc(settings: Settings) -> OidcBrowserConfig | None:
    if settings.reviewer_auth_mode != "oidc":
        return None
    try:
        return settings.oidc_browser_config()
    except ValueError:
        return None


def _content_security_policy(settings: Settings) -> str:
    oidc = _browser_oidc(settings)
    connect_src = "'self'" if oidc is None else f"'self' {oidc.token_origin}"
    return (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        f"img-src 'self' data:; connect-src {connect_src}; frame-ancestors 'none'; "
        "base-uri 'none'; form-action 'self'"
    )


def _request_id(request: Request) -> str:
    supplied = request.headers.get("X-Request-ID", "")
    return supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())


def _error_response(status_code: int, code: str, message: str, request_id: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ApiEnvelope(success=False, error={"code": code, "message": message}).model_dump(),
        headers={"X-Request-ID": request_id},
    )


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = _request_id(request)
    start = time.perf_counter()
    settings = get_settings()
    # Uvicorn rewrites the scheme from X-Forwarded-Proto only for FORWARDED_ALLOW_IPS, so the
    # header cannot be spoofed by arbitrary clients.
    scheme = request.url.scheme
    if settings.enforce_https and scheme != "https" and not _https_exempt(request.url.path):
        return _error_response(400, "HTTPS_REQUIRED", "HTTPS is required", request_id)
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled request failure", extra={"request_id": request_id, "event": "request_failed"}
        )
        return _error_response(500, "INTERNAL_ERROR", "Internal server error", request_id)
    latency = time.perf_counter() - start
    route = request.scope.get("route")
    path_label = getattr(route, "path", "unmatched")
    REQUESTS.labels(request.method, path_label, response.status_code).inc()
    LATENCY.labels(request.method, path_label).observe(latency)
    response.headers["X-Request-ID"] = request_id
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    response.headers["Content-Security-Policy"] = _content_security_policy(settings)
    if scheme == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    logger.info(
        "request completed",
        extra={
            "request_id": request_id,
            "event": "request_completed",
            "method": request.method,
            "path": path_label,
            "status": response.status_code,
            "latency_ms": round(latency * 1000),
        },
    )
    return response


@app.exception_handler(DomainError)
async def domain_error_handler(_: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiEnvelope(
            success=False, error={"code": exc.code, "message": str(exc)}
        ).model_dump(),
    )


@app.exception_handler(StarletteHTTPException)
async def http_error_handler(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content=ApiEnvelope(
            success=False,
            error={"code": f"HTTP_{exc.status_code}", "message": message},
        ).model_dump(),
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content=ApiEnvelope(
            success=False,
            error={"code": "VALIDATION_ERROR", "message": "Request validation failed"},
        ).model_dump(),
    )


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def dashboard(request: Request) -> HTMLResponse:
    settings = get_settings()
    oidc = _browser_oidc(settings)
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "synthetic_only": True,
            "auth_mode": settings.reviewer_auth_mode,
            "oidc": oidc,
        },
    )


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "alive"}


@app.get("/health/ready")
def ready(session: Annotated[Session, Depends(get_session)]) -> dict[str, str | bool]:
    session.execute(text("SELECT 1"))
    return {
        "status": "ready",
        "synthetic_only": True,
        "human_review_required": True,
        "reviewer_auth_mode": get_settings().reviewer_auth_mode,
    }


def _age_seconds(value: datetime | None) -> float:
    if value is None:
        return 0.0
    return max(0.0, (datetime.now(UTC) - as_utc(value)).total_seconds())


def _count(session: Session, statement: Select[Any]) -> int:
    return int(session.scalar(statement) or 0)


@app.get("/metrics", include_in_schema=False)
def metrics(
    _: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    status_counts = dict(
        session.execute(
            select(FollowUpCase.status, func.count()).group_by(FollowUpCase.status)
        ).all()
    )
    for status in CaseStatus:
        CASES_BY_STATUS.labels(status.value).set(status_counts.get(status.value, 0))
    oldest = session.scalar(
        select(func.min(FollowUpCase.created_at)).where(
            FollowUpCase.status == CaseStatus.PENDING_REVIEW
        )
    )
    PENDING_REVIEWS.set(status_counts.get(CaseStatus.PENDING_REVIEW.value, 0))
    OLDEST_PENDING_AGE.set(_age_seconds(oldest))
    DEAD_LETTERS.set(
        _count(
            session,
            select(func.count())
            .select_from(OutboxEvent)
            .where(OutboxEvent.status == "dead_letter"),
        )
    )
    OUTBOX_PROCESSING.set(
        _count(
            session,
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "processing"),
        )
    )
    PROVIDER_FAILURES.set(
        _count(
            session,
            select(func.count()).select_from(ProviderCall).where(ProviderCall.status == "failed"),
        )
    )
    heartbeats = {
        heartbeat.worker_name: heartbeat
        for heartbeat in session.scalars(select(WorkerHeartbeat)).all()
    }
    for worker in MONITORED_WORKERS:
        heartbeat = heartbeats.get(worker)
        WORKER_HEARTBEAT_AGE.labels(worker).set(
            _age_seconds(heartbeat.last_seen_at) if heartbeat else MISSING_HEARTBEAT_AGE_SECONDS
        )
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/v1/agent/runs", response_model=ApiEnvelope, status_code=201)
def run_agent(
    body: AgentRunRequest,
    actor_id: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiEnvelope:
    service = AgentService(session, ModelGatewayClient(settings))
    case = service.run(body.customer_id, body.idempotency_key, actor_id)
    return ApiEnvelope(success=True, data=case.model_dump(mode="json"))


@app.post("/api/v1/agent/cases/{case_id}/retry", response_model=ApiEnvelope)
def retry_agent_run(
    case_id: str,
    actor_id: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiEnvelope:
    service = AgentService(session, ModelGatewayClient(settings))
    case = service.retry(case_id, actor_id)
    return ApiEnvelope(success=True, data=case.model_dump(mode="json"))


@app.post("/api/v1/agent/maintenance", response_model=ApiEnvelope)
def maintenance(
    actor_id: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiEnvelope:
    result = run_maintenance(session, settings, actor_id)
    return ApiEnvelope(success=True, data=result.model_dump(mode="json"))


@app.get("/api/v1/cases", response_model=ApiEnvelope)
def get_cases(
    _: Annotated[str, Depends(require_reviewer)],
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[CaseStatus | None, Query()] = None,
    order: Annotated[Literal["oldest", "newest"], Query()] = "oldest",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiEnvelope:
    page = list_cases(
        session, status=status, limit=limit, offset=offset, oldest_first=order == "oldest"
    )
    response = ListCasesResponse(
        items=[case_response(item) for item in page.items],
        total=page.total,
        limit=limit,
        offset=offset,
    )
    return ApiEnvelope(success=True, data=response.model_dump(mode="json"))


@app.get("/api/v1/cases/{case_id}", response_model=ApiEnvelope)
def get_case(
    case_id: str,
    _: Annotated[str, Depends(require_reviewer)],
    session: Annotated[Session, Depends(get_session)],
) -> ApiEnvelope:
    case = session.get(FollowUpCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail="Case not found")
    return ApiEnvelope(
        success=True, data=case_response(case, include_snapshot=True).model_dump(mode="json")
    )


@app.post("/api/v1/cases/{case_id}/reviews", response_model=ApiEnvelope)
def review(
    case_id: str,
    body: ReviewRequest,
    reviewer_id: Annotated[str, Depends(require_reviewer)],
    session: Annotated[Session, Depends(get_session)],
) -> ApiEnvelope:
    result = review_case(session, case_id, body, reviewer_id)
    return ApiEnvelope(success=True, data=result.model_dump(mode="json"))


@app.post("/api/v1/outbox/dispatch-one", response_model=ApiEnvelope)
def dispatch(
    actor_id: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ApiEnvelope:
    result = dispatch_one(session, actor_id, CallerClient(settings))
    return ApiEnvelope(success=True, data=result.model_dump(mode="json"))


@app.get("/api/v1/cases/{case_id}/audit-verification", response_model=ApiEnvelope)
def verify_audit(
    case_id: str,
    _: Annotated[str, Depends(require_reviewer)],
    session: Annotated[Session, Depends(get_session)],
) -> ApiEnvelope:
    exists = session.scalar(select(FollowUpCase.id).where(FollowUpCase.id == case_id))
    if not exists:
        raise HTTPException(status_code=404, detail="Case not found")
    verification = verify_audit_chain(session, CASE_AGGREGATE, case_id)
    if not verification.valid:
        AUDIT_FAILURES.inc()
        logger.error(
            "audit verification failed",
            extra={"event": "audit_failed", "case_id": case_id, "reason": verification.reason},
        )
    response = AuditVerificationResponse(
        case_id=case_id,
        valid=verification.valid,
        sealed=verification.sealed,
        event_count=verification.event_count,
        reason=verification.reason,
    )
    return ApiEnvelope(success=True, data=response.model_dump(mode="json"))
