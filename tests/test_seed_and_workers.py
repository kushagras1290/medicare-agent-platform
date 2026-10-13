from __future__ import annotations

import csv
import json
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from medicare_agent import cli, worker
from medicare_agent.audit import AuditVerification
from medicare_agent.config import Settings
from medicare_agent.models import Customer
from medicare_agent.seed import SeedError, seed_csv

FIELDS = [
    "customer_id",
    "age",
    "sex_at_birth",
    "conditions",
    "missed_appointment",
    "consent_status",
    "last_contact_at",
    "measured_at",
    "bp_systolic_mm_hg",
    "bp_diastolic_mm_hg",
    "heart_rate_bpm",
    "spo2_pct",
    "temperature_c",
    "respiratory_rate_bpm",
    "weight_kg",
    "bmi",
    "creatinine_mg_dl",
    "egfr_ml_min_1_73m2",
    "glucose_mg_dl",
    "hemoglobin_g_dl",
    "sodium_mmol_l",
    "potassium_mmol_l",
    "phq9_score",
]


def write_one_row(path: Path, **overrides: str) -> None:
    row = {
        "customer_id": "C00001",
        "age": "50",
        "sex_at_birth": "female",
        "conditions": "hypertension|depression",
        "missed_appointment": "true",
        "consent_status": "synthetic_opted_in",
        "last_contact_at": "2026-09-01T00:00:00Z",
        "measured_at": "2026-10-01T00:00:00Z",
        "bp_systolic_mm_hg": "145",
        "bp_diastolic_mm_hg": "90",
        "heart_rate_bpm": "80",
        "spo2_pct": "96",
        "temperature_c": "36.8",
        "respiratory_rate_bpm": "17",
        "weight_kg": "70",
        "bmi": "26",
        "creatinine_mg_dl": "1.0",
        "egfr_ml_min_1_73m2": "75",
        "glucose_mg_dl": "110",
        "hemoglobin_g_dl": "13",
        "sodium_mmol_l": "139",
        "potassium_mmol_l": "4.2",
        "phq9_score": "8",
    }
    row.update(overrides)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(row)


def test_seed_csv_is_atomic_and_refuses_reseed(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "one.csv"
    write_one_row(path)
    assert seed_csv(session, path, expected_rows=1) == 1
    customer = session.get(Customer, "C00001")
    assert customer is not None and customer.follow_up_started_at is None
    with pytest.raises(SeedError, match="already contains"):
        seed_csv(session, path, expected_rows=1)


def test_seed_csv_validates_rows_against_the_workflow_contract(
    session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "invalid.csv"
    write_one_row(path, spo2_pct="49")
    with pytest.raises(SeedError, match="line 2"):
        seed_csv(session, path, expected_rows=1)
    write_one_row(path, consent_status="unknown")
    with pytest.raises(SeedError, match="line 2"):
        seed_csv(session, path, expected_rows=1)


def test_seed_csv_rejects_wrong_count_and_rolls_back_insert_failure(
    monkeypatch: pytest.MonkeyPatch, session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "one.csv"
    write_one_row(path)
    with pytest.raises(SeedError, match="Expected 2"):
        seed_csv(session, path, expected_rows=2)

    original_execute = session.execute
    calls = 0

    def fail_on_insert(statement: Any, *args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("insert")
        return original_execute(statement, *args, **kwargs)

    monkeypatch.setattr(session, "execute", fail_on_insert)
    with pytest.raises(RuntimeError, match="insert"):
        seed_csv(session, path, expected_rows=1)
    assert session.in_transaction() is False


class SessionContext(AbstractContextManager[Session]):
    def __init__(self, session: Session) -> None:
        self.session = session

    def __enter__(self) -> Session:
        return self.session

    def __exit__(self, *args: object) -> None:
        return None


def api_for(handler: Callable[[httpx.Request], httpx.Response]) -> worker.ServiceApi:
    client = httpx.Client(base_url="http://api.test", transport=httpx.MockTransport(handler))
    return worker.ServiceApi(client, "service-key")


def envelope(data: object, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json={"success": True, "data": data})


def test_failure_tracker_backs_off_evicts_and_clears() -> None:
    now = [1000.0]
    tracker = worker.FailureTracker(clock=lambda: now[0])
    tracker.record("C00001", permanent=False)
    tracker.record("C00001", permanent=False)
    assert tracker.entries["C00001"][0] == 2
    assert tracker.entries["C00001"][1] == 1000.0 + worker.FAILURE_BACKOFF_BASE_SECONDS * 2
    tracker.record("C00002", permanent=True)
    assert tracker.blocked() == ["C00001", "C00002"]
    now[0] += worker.FAILURE_BACKOFF_BASE_SECONDS * 2 + 1
    assert tracker.blocked() == ["C00002"]
    tracker.clear("C00002")
    assert tracker.blocked() == []

    full = worker.FailureTracker(clock=lambda: now[0])
    for index in range(worker.MAX_TRACKED_FAILURES):
        full.entries[f"C{index:05d}"] = (1, now[0] + index + 1)
    full.record("C99999", permanent=False)
    assert "C00000" not in full.entries
    assert len(full.entries) == worker.MAX_TRACKED_FAILURES


def test_service_api_rejects_malformed_envelopes() -> None:
    api = api_for(lambda _: httpx.Response(200, json={"success": True, "data": []}))
    with pytest.raises(worker.WorkerApiError):
        api.post("/x")
    api = api_for(lambda _: httpx.Response(200, text="not json"))
    with pytest.raises(worker.WorkerApiError):
        api.post("/x")


def test_automation_tick_runs_maintenance_retries_and_backs_off_failures(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    add_customer: Callable[..., Customer],
) -> None:
    add_customer("C00001", missed=True)
    add_customer("C00002", missed=True)
    add_customer("C00003", missed=True)
    add_customer("C00004", missed=True)
    handled = add_customer("C00005", missed=True)
    handled.follow_up_started_at = datetime.now(UTC)
    session.commit()
    monkeypatch.setattr(worker, "open_session", lambda: SessionContext(session))
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path == worker.MAINTENANCE_PATH:
            return envelope({"recovered_stale_runs": 1, "retry_case_ids": ["case-a", "case-b"]})
        if request.url.path.endswith("case-b/retry"):
            return httpx.Response(503)
        if request.url.path.endswith("/retry"):
            return envelope({})
        customer_id = json.loads(request.content)["customer_id"]
        if customer_id == "C00002":
            return httpx.Response(404)
        if customer_id == "C00003":
            return httpx.Response(503)
        if customer_id == "C00004":
            return httpx.Response(201, json={"success": True})
        return envelope({}, 201)

    tracker = worker.FailureTracker()
    processed = worker.automation_tick(api_for(handler), settings, tracker)
    assert processed == 1 + 1 + 1
    assert worker.RUNS_PATH in requests
    assert set(tracker.entries) == {"C00002", "C00003", "C00004"}
    assert tracker.entries["C00002"][1] - tracker.entries["C00003"][1] > 3600

    requests.clear()
    worker.automation_tick(api_for(handler), settings, tracker)
    run_bodies = [path for path in requests if path == worker.RUNS_PATH]
    assert len(run_bodies) == 1


def test_automation_tick_survives_maintenance_failure(
    monkeypatch: pytest.MonkeyPatch, session: Session, settings: Settings
) -> None:
    monkeypatch.setattr(worker, "open_session", lambda: SessionContext(session))
    api = api_for(lambda _: httpx.Response(500))
    assert worker.automation_tick(api, settings, worker.FailureTracker()) == 0


def test_dispatch_tick_drains_until_empty_and_handles_errors(settings: Settings) -> None:
    responses = iter(
        [
            envelope({"processed": True}),
            httpx.Response(409, json={"success": False}),
            envelope({"processed": True}),
            envelope({"processed": False}),
        ]
    )
    assert worker.dispatch_tick(api_for(lambda _: next(responses)), settings) == 3

    assert worker.dispatch_tick(api_for(lambda _: httpx.Response(500)), settings) == 0

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    assert worker.dispatch_tick(api_for(offline), settings) == 0

    limited = settings.model_copy(update={"dispatch_batch_limit": 2})
    assert worker.dispatch_tick(api_for(lambda _: envelope({"processed": True})), limited) == 2


def test_run_once_contains_tick_failures(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    api = api_for(lambda _: envelope({"processed": False}))
    assert worker.run_once("dispatch", api, settings, worker.FailureTracker()) == (0, "ok")

    def broken(*_: object) -> int:
        raise OperationalError("select", {}, Exception("database down"))

    monkeypatch.setattr(worker, "automation_tick", broken)
    assert worker.run_once("automation", api, settings, worker.FailureTracker()) == (0, "error")


def test_request_timeout_exceeds_downstream_budget(settings: Settings) -> None:
    assert worker.request_timeout_seconds(settings, "automation") > (
        settings.model_gateway_budget_seconds()
    )
    assert worker.request_timeout_seconds(settings, "dispatch") > settings.caller_budget_seconds()


def test_worker_main_once_and_heartbeat_failures(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, session: Session
) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "run_once", lambda *_: (2, "ok"))
    heartbeats: list[tuple[str, int, str]] = []
    monkeypatch.setattr(
        worker,
        "heartbeat",
        lambda mode, processed, status: heartbeats.append((mode, processed, status)),
    )
    worker.main(["automation", "--once"])
    assert heartbeats == [("automation", 2, "ok")]

    def failing_heartbeat(*_: object) -> None:
        raise OperationalError("update", {}, Exception("database down"))

    monkeypatch.setattr(worker, "heartbeat", failing_heartbeat)
    worker.main(["dispatch", "--once"])

    # Restore the real heartbeat() and exercise it against a recording persistence layer.
    monkeypatch.undo()
    recorded: list[tuple[str, int, str]] = []
    monkeypatch.setattr(worker, "open_session", lambda: SessionContext(session))
    monkeypatch.setattr(
        worker,
        "record_worker_heartbeat",
        lambda _session, mode, *, processed, status: recorded.append((mode, processed, status)),
    )
    worker.heartbeat("dispatch", 3, "error")
    assert recorded == [("dispatch", 3, "error")]


def test_worker_poll_loop_stops_on_signal(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "heartbeat", lambda *_: None)
    ticks: list[str] = []

    def tick(mode: str, *_: object) -> tuple[int, str]:
        ticks.append(mode)
        worker.stop_worker()
        return 1, "ok"

    monkeypatch.setattr(worker, "run_once", tick)
    worker.main(["dispatch"])
    assert ticks == ["dispatch"]
    assert worker.STOP_EVENT.is_set()
    worker.STOP_EVENT.clear()


def test_cli_http_commands_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(cli, "_post", lambda path, payload=None: calls.append((path, payload)))
    for argv in (
        ["medicare", "run-agent", "C00001", "--idempotency-key", "cli-key-001"],
        ["medicare", "dispatch-one"],
        ["medicare", "maintenance"],
        ["medicare", "retry-case", "case-1"],
    ):
        monkeypatch.setattr(sys, "argv", argv)
        cli.main()
    assert [path for path, _ in calls] == [
        "/api/v1/agent/runs",
        "/api/v1/outbox/dispatch-one",
        "/api/v1/agent/maintenance",
        "/api/v1/agent/cases/case-1/retry",
    ]


def test_cli_post_and_database_commands(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    response = httpx.Response(
        200, json={"success": True}, request=httpx.Request("POST", "http://test")
    )
    captured: dict[str, Any] = {}

    def fake_post(*_args: object, **kwargs: Any) -> httpx.Response:
        captured.update(kwargs)
        return response

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.httpx, "post", fake_post)
    cli._post("/test", {"ok": True})
    assert '"success": true' in capsys.readouterr().out
    assert captured["timeout"] > settings.model_gateway_budget_seconds()

    monkeypatch.setattr(cli, "open_session", lambda: SessionContext(session))
    monkeypatch.setattr(cli, "seed_csv", lambda *_args, **_kwargs: 1)
    csv_path = tmp_path / "data.csv"
    monkeypatch.setattr(sys, "argv", ["medicare", "seed", "--csv", str(csv_path)])
    cli.main()

    monkeypatch.setattr(session, "scalar", lambda *_: 0)
    monkeypatch.setattr(sys, "argv", ["medicare", "bootstrap", "--csv", str(csv_path)])
    cli.main()
    monkeypatch.setattr(session, "scalar", lambda *_: 5)
    cli.main()

    monkeypatch.setattr(
        cli, "verify_audit_chain", lambda *_: AuditVerification(True, True, 4, None)
    )
    monkeypatch.setattr(sys, "argv", ["medicare", "verify-audit", "case-1"])
    capsys.readouterr()
    cli.main()
    assert json.loads(capsys.readouterr().out) == {
        "valid": True,
        "sealed": True,
        "event_count": 4,
        "reason": None,
    }

    monkeypatch.setattr(
        session, "execute", lambda *_: SimpleNamespace(all=lambda: [("approved", 2)])
    )
    monkeypatch.setattr(sys, "argv", ["medicare", "stats"])
    cli.main()
    assert '"approved": 2' in capsys.readouterr().out


def test_cli_release_and_retention_branches(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: Session,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text("{}", encoding="utf-8")
    manifest = SimpleNamespace(release_id="release-1", environment="production")
    monkeypatch.setattr(cli, "validate_release_manifest", lambda *_args, **_kwargs: manifest)
    monkeypatch.setattr(cli, "file_sha256", lambda _: "a" * 64)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "medicare",
            "release-gate",
            "--manifest",
            str(manifest_path),
            "--environment",
            "production",
        ],
    )
    cli.main()
    assert '"valid": true' in capsys.readouterr().out

    monkeypatch.setattr(
        cli,
        "validate_release_manifest",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(cli.ReleaseGateError("pending")),
    )
    with pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2

    sweeps: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "open_session", lambda: SessionContext(session))
    monkeypatch.setattr(
        cli,
        "retention_sweep",
        lambda *_args, **kwargs: (
            sweeps.append(kwargs) or SimpleNamespace(eligible_cases=1, deleted_cases=0)
        ),
    )
    monkeypatch.setattr(sys, "argv", ["medicare", "retention-sweep"])
    cli.main()
    assert sweeps[0]["provider_max_attempts"] == settings.provider_max_attempts
    monkeypatch.setattr(
        sys,
        "argv",
        ["medicare", "retention-sweep", "--before", "2026-01-01T00:00:00+00:00", "--execute"],
    )
    cli.main()
    monkeypatch.setattr(
        sys, "argv", ["medicare", "retention-sweep", "--before", "2026-01-01T00:00:00"]
    )
    with pytest.raises(SystemExit, match="timezone"):
        cli.main()
    monkeypatch.setattr(sys, "argv", ["medicare", "retention-sweep", "--batch-size", "0"])
    with pytest.raises(SystemExit, match="batch-size"):
        cli.main()
