from __future__ import annotations

import csv
import json
import sys
from collections.abc import Callable
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from sqlalchemy.orm import Session

from medicare_agent import cli, worker
from medicare_agent.config import Settings
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


def write_one_row(path: Path) -> None:
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
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerow(row)


def test_seed_csv_is_atomic_and_refuses_reseed(session: Session, tmp_path: Path) -> None:
    path = tmp_path / "one.csv"
    write_one_row(path)
    assert seed_csv(session, path, expected_rows=1) == 1
    with pytest.raises(SeedError, match="already contains"):
        seed_csv(session, path, expected_rows=1)


def test_seed_csv_rejects_wrong_count_and_rolls_back_insert_failure(
    monkeypatch: pytest.MonkeyPatch, session: Session, tmp_path: Path
) -> None:
    path = tmp_path / "one.csv"
    write_one_row(path)
    with pytest.raises(SeedError, match="Expected 2"):
        seed_csv(session, path, expected_rows=2)

    monkeypatch.setattr(
        session, "bulk_insert_mappings", lambda *_: (_ for _ in ()).throw(RuntimeError("insert"))
    )
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


def test_worker_automation_and_dispatch_call_the_api(
    monkeypatch: pytest.MonkeyPatch,
    session: Session,
    settings: Settings,
    add_customer: Callable[..., object],
) -> None:
    add_customer("C00001", missed=True)
    monkeypatch.setattr(worker, "SessionLocal", lambda: SessionContext(session))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        if request.url.path.endswith("dispatch-one"):
            return httpx.Response(200, json={"data": {"processed": False}})
        body = json.loads(request.content)
        assert body["customer_id"] == "C00001"
        return httpx.Response(201, json={"success": True, "data": {}})

    original_client = httpx.Client
    original_post = httpx.post
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        worker.httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=transport, base_url=kwargs.get("base_url", "http://test")
        ),
    )
    monkeypatch.setattr(
        worker.httpx,
        "post",
        lambda url, **kwargs: original_client(transport=transport).post(url, **kwargs),
    )

    assert worker.automation_tick() == 1
    assert worker.dispatch_tick() is False
    assert requests == ["/api/v1/agent/runs", "/api/v1/outbox/dispatch-one"]
    monkeypatch.setattr(worker.httpx, "post", original_post)


def test_worker_main_once(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(worker, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "automation_tick", lambda: 2)
    heartbeats: list[tuple[str, int]] = []
    monkeypatch.setattr(
        worker, "heartbeat", lambda mode, processed: heartbeats.append((mode, processed))
    )
    monkeypatch.setattr(sys, "argv", ["worker", "automation", "--once"])
    worker.main()
    assert heartbeats == [("automation", 2)]


def test_worker_failure_heartbeat_stop_and_poll_loop(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
    session: Session,
    add_customer: Callable[..., object],
) -> None:
    add_customer("C00001", missed=True)
    monkeypatch.setattr(worker, "SessionLocal", lambda: SessionContext(session))
    monkeypatch.setattr(worker, "get_settings", lambda: settings)

    class FailingClient:
        def __enter__(self):
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def post(self, *_: object, **__: object) -> None:
            raise httpx.ConnectError("offline")

    monkeypatch.setattr(worker.httpx, "Client", lambda **_: FailingClient())
    assert worker.automation_tick() == 0
    monkeypatch.setattr(
        worker.httpx,
        "post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(httpx.ConnectError("offline")),
    )
    assert worker.dispatch_tick() is False

    heartbeats: list[tuple[str, int]] = []
    monkeypatch.setattr(
        worker,
        "record_worker_heartbeat",
        lambda _session, mode, *, processed: heartbeats.append((mode, processed)),
    )
    worker.heartbeat("dispatch", 3)
    assert heartbeats == [("dispatch", 3)]
    worker.stop_worker()
    assert worker.stopping is True

    worker.stopping = False
    monkeypatch.setattr(worker, "dispatch_tick", lambda: True)
    monkeypatch.setattr(worker, "heartbeat", lambda *_: None)
    monkeypatch.setattr(worker.time, "sleep", lambda _: worker.stop_worker())
    monkeypatch.setattr(sys, "argv", ["worker", "dispatch"])
    worker.main()
    assert worker.stopping is True
    worker.stopping = False


def test_cli_http_commands_delegate(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, dict[str, Any] | None]] = []
    monkeypatch.setattr(cli, "_post", lambda path, payload=None: calls.append((path, payload)))
    monkeypatch.setattr(
        sys,
        "argv",
        ["medicare", "run-agent", "C00001", "--idempotency-key", "cli-key-001"],
    )
    cli.main()
    monkeypatch.setattr(sys, "argv", ["medicare", "dispatch-one"])
    cli.main()
    assert calls[0][0] == "/api/v1/agent/runs"
    assert calls[1][0] == "/api/v1/outbox/dispatch-one"


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
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli.httpx, "post", lambda *_args, **_kwargs: response)
    cli._post("/test", {"ok": True})
    assert '"success": true' in capsys.readouterr().out

    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(session))
    monkeypatch.setattr(cli, "seed_csv", lambda *_args, **_kwargs: 1)
    csv_path = tmp_path / "data.csv"
    monkeypatch.setattr(sys, "argv", ["medicare", "seed", "--csv", str(csv_path)])
    cli.main()

    monkeypatch.setattr(session, "scalar", lambda *_: 0)
    monkeypatch.setattr(sys, "argv", ["medicare", "bootstrap", "--csv", str(csv_path)])
    cli.main()
    monkeypatch.setattr(session, "scalar", lambda *_: 5)
    cli.main()

    monkeypatch.setattr(cli, "verify_audit_chain", lambda *_: True)
    monkeypatch.setattr(sys, "argv", ["medicare", "verify-audit", "case-1"])
    cli.main()

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

    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "SessionLocal", lambda: SessionContext(session))
    monkeypatch.setattr(
        cli,
        "retention_sweep",
        lambda *_args, **_kwargs: SimpleNamespace(eligible_cases=1, deleted_cases=0),
    )
    monkeypatch.setattr(sys, "argv", ["medicare", "retention-sweep"])
    cli.main()
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
