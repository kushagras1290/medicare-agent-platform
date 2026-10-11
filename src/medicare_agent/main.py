from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.middleware.trustedhost import TrustedHostMiddleware

from medicare_agent.audit import verify_audit_chain
from medicare_agent.auth import require_reviewer, require_service
from medicare_agent.caller import CallerClient
from medicare_agent.config import Settings, get_settings
from medicare_agent.db import engine, get_session
from medicare_agent.governance import validate_release_manifest
from medicare_agent.logging_config import configure_logging
from medicare_agent.models import FollowUpCase, OutboxEvent, ProviderCall, WorkerHeartbeat
from medicare_agent.provider import ModelGatewayClient
from medicare_agent.schemas import (
    AgentRunRequest,
    ApiEnvelope,
    ListCasesResponse,
    ReviewRequest,
)
from medicare_agent.service import (
    AgentService,
    DomainError,
    case_response,
    dispatch_one,
    list_cases,
    review_case,
)

configure_logging()
logger = logging.getLogger(__name__)

REQUESTS = Counter("medicare_http_requests_total", "HTTP requests", ["method", "path", "status"])
LATENCY = Histogram("medicare_http_request_seconds", "HTTP latency", ["method", "path"])
PENDING_REVIEWS = Gauge("medicare_pending_reviews", "Cases currently waiting for review")
OLDEST_PENDING_AGE = Gauge(
    "medicare_oldest_pending_review_age_seconds", "Age of the oldest pending review"
)
DEAD_LETTERS = Gauge("medicare_outbox_dead_letters", "Outbox events in dead-letter state")
PROVIDER_FAILURES = Gauge("medicare_provider_failures", "Persisted failed provider calls")
WORKER_HEARTBEAT_AGE = Gauge(
    "medicare_worker_heartbeat_age_seconds", "Seconds since worker heartbeat", ["worker"]
)
AUDIT_FAILURES = Counter(
    "medicare_audit_verification_failures_total", "Detected audit-chain verification failures"
)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    if settings.reviewer_auth_mode == "api_key":
        settings.reviewers()
    settings.hosts()
    if settings.environment in {"staging", "production"}:
        assert settings.release_approvals_path
        validate_release_manifest(
            settings.release_approvals_path,
            expected_environment=settings.environment,
            expected_sha256=settings.release_approvals_sha256,
        )
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


@app.middleware("http")
async def request_context(request: Request, call_next):  # type: ignore[no-untyped-def]
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    start = time.perf_counter()
    settings = get_settings()
    forwarded_proto = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    if settings.enforce_https and forwarded_proto != "https":
        return JSONResponse(
            status_code=400,
            content=ApiEnvelope(
                success=False,
                error={"code": "HTTPS_REQUIRED", "message": "HTTPS is required"},
            ).model_dump(),
        )
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "unhandled request failure", extra={"request_id": request_id, "event": "request_failed"}
        )
        raise
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
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; style-src 'self'; "
        "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'"
    )
    if forwarded_proto == "https":
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    logger.info(
        "request completed",
        extra={
            "request_id": request_id,
            "event": "request_completed",
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


@app.exception_handler(HTTPException)
async def http_error_handler(_: Request, exc: HTTPException) -> JSONResponse:
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
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"synthetic_only": True},
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
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return max(0.0, (datetime.now(UTC) - value).total_seconds())


@app.get("/metrics", include_in_schema=False)
def metrics(
    _: Annotated[str, Depends(require_service)],
    session: Annotated[Session, Depends(get_session)],
) -> Response:
    pending = (
        session.scalar(
            select(func.count())
            .select_from(FollowUpCase)
            .where(FollowUpCase.status == "pending_review")
        )
        or 0
    )
    oldest = session.scalar(
        select(func.min(FollowUpCase.created_at)).where(FollowUpCase.status == "pending_review")
    )
    dead_letters = (
        session.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.status == "dead_letter")
        )
        or 0
    )
    provider_failures = (
        session.scalar(
            select(func.count()).select_from(ProviderCall).where(ProviderCall.status == "failed")
        )
        or 0
    )
    PENDING_REVIEWS.set(pending)
    OLDEST_PENDING_AGE.set(_age_seconds(oldest))
    DEAD_LETTERS.set(dead_letters)
    PROVIDER_FAILURES.set(provider_failures)
    heartbeats = {
        heartbeat.worker_name: heartbeat
        for heartbeat in session.scalars(select(WorkerHeartbeat)).all()
    }
    for worker in ("automation", "dispatch"):
        heartbeat = heartbeats.get(worker)
        WORKER_HEARTBEAT_AGE.labels(worker).set(
            _age_seconds(heartbeat.last_seen_at) if heartbeat else 1_000_000_000
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


@app.get("/api/v1/cases", response_model=ApiEnvelope)
def get_cases(
    _: Annotated[str, Depends(require_reviewer)],
    session: Annotated[Session, Depends(get_session)],
    status: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ApiEnvelope:
    page = list_cases(session, status=status, limit=limit, offset=offset)
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
    return ApiEnvelope(success=True, data=case_response(case).model_dump(mode="json"))


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
) -> ApiEnvelope:
    result = dispatch_one(session, actor_id, CallerClient(get_settings()))
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
    valid = verify_audit_chain(session, "follow_up_case", case_id)
    if not valid:
        AUDIT_FAILURES.inc()
    return ApiEnvelope(success=True, data={"case_id": case_id, "valid": valid})
