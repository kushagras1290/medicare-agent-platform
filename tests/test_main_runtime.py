from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from medicare_agent import main
from medicare_agent.audit import AuditVerification
from medicare_agent.config import Settings
from medicare_agent.service import DomainError


class FakeConnection:
    def __enter__(self) -> FakeConnection:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def execute(self, *_: object) -> None:
        return None


class FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def connect(self) -> FakeConnection:
        return FakeConnection()

    def dispose(self) -> None:
        self.disposed = True


def run_lifespan() -> None:
    async def run() -> None:
        async with main.lifespan(main.app):
            pass

    asyncio.run(run())


def production_namespace(approvals: Path | None) -> SimpleNamespace:
    return SimpleNamespace(
        reviewer_auth_mode="oidc",
        environment="production",
        release_approvals_path=approvals,
        release_approvals_sha256="a" * 64,
        hosts=lambda: ["medicare.example.test"],
        oidc_browser_config=lambda: None,
    )


def test_lifespan_validates_local_and_production_startup(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, tmp_path: Path
) -> None:
    local_engine = FakeEngine()
    monkeypatch.setattr(main, "get_engine", lambda: local_engine)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    run_lifespan()
    assert local_engine.disposed is True

    approvals = tmp_path / "approvals.json"
    approvals.write_text("{}", encoding="utf-8")
    validated: list[Path] = []
    monkeypatch.setattr(main, "get_settings", lambda: production_namespace(approvals))
    monkeypatch.setattr(
        main,
        "validate_release_manifest",
        lambda path, **_: validated.append(path),
    )
    production_engine = FakeEngine()
    monkeypatch.setattr(main, "get_engine", lambda: production_engine)
    run_lifespan()
    assert validated == [approvals]
    assert production_engine.disposed is True

    monkeypatch.setattr(main, "get_settings", lambda: production_namespace(None))
    with pytest.raises(RuntimeError, match="manifest path"):
        run_lifespan()


def test_request_middleware_returns_enveloped_500_for_unhandled_error(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/failure",
            "raw_path": b"/failure",
            "query_string": b"",
            "headers": [(b"x-request-id", b"trace-500")],
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        }
    )

    async def fail(_: Request) -> None:
        raise RuntimeError("route failed")

    response = asyncio.run(main.request_context(request, fail))
    assert response.status_code == 500
    assert response.headers["X-Request-ID"] == "trace-500"
    assert json.loads(bytes(response.body))["error"]["code"] == "INTERNAL_ERROR"


def test_handlers_age_and_audit_failure_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    response = asyncio.run(
        main.domain_error_handler(
            MagicMock(), DomainError("TEST_FAILURE", "synthetic failure", 409)
        )
    )
    assert response.status_code == 409
    assert main._age_seconds(None) == 0
    naive_utc = datetime.now().astimezone().astimezone(main.UTC).replace(tzinfo=None)
    assert main._age_seconds(naive_utc - timedelta(seconds=1)) > 0
    assert main._age_seconds(datetime.now().astimezone() + timedelta(seconds=5)) == 0

    session = MagicMock()
    session.scalar.return_value = None
    with pytest.raises(HTTPException, match="not found"):
        main.verify_audit("missing", "reviewer", session)
    session.scalar.return_value = "case-1"
    monkeypatch.setattr(
        main,
        "verify_audit_chain",
        lambda *_: AuditVerification(False, False, 3, "head_mismatch"),
    )
    result = main.verify_audit("case-1", "reviewer", session)
    assert result.data == {
        "case_id": "case-1",
        "valid": False,
        "sealed": False,
        "event_count": 3,
        "reason": "head_mismatch",
    }
