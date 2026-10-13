from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select

from medicare_agent.audit import verify_audit_chain
from medicare_agent.config import get_settings
from medicare_agent.db import open_session
from medicare_agent.governance import ReleaseGateError, file_sha256, validate_release_manifest
from medicare_agent.models import Customer, FollowUpCase
from medicare_agent.operations import retention_sweep
from medicare_agent.seed import seed_csv

CLI_TIMEOUT_MARGIN_SECONDS = 15.0


def _post(path: str, payload: dict[str, Any] | None = None) -> None:
    settings = get_settings()
    base_url = os.getenv("MEDICARE_API_URL", "http://127.0.0.1:8000")
    response = httpx.post(
        f"{base_url.rstrip('/')}{path}",
        headers={"X-Service-Key": settings.service_api_key.get_secret_value()},
        json=payload,
        timeout=settings.model_gateway_budget_seconds() + CLI_TIMEOUT_MARGIN_SECONDS,
    )
    response.raise_for_status()
    print(json.dumps(response.json(), indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="medicare")
    subparsers = parser.add_subparsers(dest="command", required=True)

    seed_parser = subparsers.add_parser("seed")
    seed_parser.add_argument("--csv", type=Path, default=Path("data/synthetic_customers.csv"))

    bootstrap_parser = subparsers.add_parser("bootstrap")
    bootstrap_parser.add_argument("--csv", type=Path, default=Path("data/synthetic_customers.csv"))

    run_parser = subparsers.add_parser("run-agent")
    run_parser.add_argument("customer_id")
    run_parser.add_argument("--idempotency-key", required=True)

    retry_parser = subparsers.add_parser("retry-case")
    retry_parser.add_argument("case_id")

    subparsers.add_parser("maintenance")
    subparsers.add_parser("dispatch-one")

    verify_parser = subparsers.add_parser("verify-audit")
    verify_parser.add_argument("case_id")

    subparsers.add_parser("stats")
    release_parser = subparsers.add_parser("release-gate")
    release_parser.add_argument("--manifest", type=Path, required=True)
    release_parser.add_argument("--environment", choices=["staging", "production"], required=True)
    release_parser.add_argument("--sha256")
    retention_parser = subparsers.add_parser("retention-sweep")
    retention_parser.add_argument("--before", help="ISO-8601 cutoff; defaults to RETENTION_DAYS")
    retention_parser.add_argument("--execute", action="store_true")
    retention_parser.add_argument("--batch-size", type=int, default=500)
    return parser


def _release_gate(args: argparse.Namespace) -> None:
    try:
        manifest = validate_release_manifest(
            args.manifest,
            expected_environment=args.environment,
            expected_sha256=args.sha256,
        )
    except ReleaseGateError as exc:
        print(json.dumps({"valid": False, "error": str(exc)}, indent=2))
        raise SystemExit(2) from exc
    print(
        json.dumps(
            {
                "valid": True,
                "release_id": manifest.release_id,
                "environment": manifest.environment,
                "sha256": file_sha256(args.manifest),
            },
            indent=2,
        )
    )


def _retention(args: argparse.Namespace) -> None:
    settings = get_settings()
    before = (
        datetime.fromisoformat(args.before.replace("Z", "+00:00"))
        if args.before
        else datetime.now(UTC) - timedelta(days=settings.retention_days)
    )
    if before.tzinfo is None:
        raise SystemExit("--before must include a timezone")
    if not 1 <= args.batch_size <= 10_000:
        raise SystemExit("--batch-size must be between 1 and 10000")
    with open_session() as session:
        result = retention_sweep(
            session,
            before=before,
            execute=args.execute,
            provider_max_attempts=settings.provider_max_attempts,
            batch_size=args.batch_size,
        )
    print(json.dumps(result.__dict__, indent=2))


def main() -> None:
    args = _parser().parse_args()

    if args.command == "seed":
        with open_session() as session:
            print(json.dumps({"seeded": seed_csv(session, args.csv)}))
    elif args.command == "bootstrap":
        with open_session() as session:
            existing = session.scalar(select(func.count()).select_from(Customer)) or 0
            seeded = 0 if existing else seed_csv(session, args.csv)
            print(json.dumps({"seeded": seeded, "existing": existing}))
    elif args.command == "run-agent":
        _post(
            "/api/v1/agent/runs",
            {"customer_id": args.customer_id, "idempotency_key": args.idempotency_key},
        )
    elif args.command == "retry-case":
        _post(f"/api/v1/agent/cases/{args.case_id}/retry")
    elif args.command == "maintenance":
        _post("/api/v1/agent/maintenance")
    elif args.command == "dispatch-one":
        _post("/api/v1/outbox/dispatch-one")
    elif args.command == "verify-audit":
        with open_session() as session:
            verification = verify_audit_chain(session, "follow_up_case", args.case_id)
            print(json.dumps(asdict(verification)))
    elif args.command == "stats":
        with open_session() as session:
            customers = session.scalar(select(func.count()).select_from(Customer)) or 0
            statuses = dict(
                session.execute(
                    select(FollowUpCase.status, func.count()).group_by(FollowUpCase.status)
                ).all()
            )
            print(json.dumps({"customers": customers, "cases": statuses}, indent=2))
    elif args.command == "release-gate":
        _release_gate(args)
    else:
        _retention(args)


if __name__ == "__main__":  # pragma: no cover - exercised through the console script
    main()
