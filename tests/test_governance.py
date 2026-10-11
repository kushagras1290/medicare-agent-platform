from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from medicare_agent.governance import (
    REQUIRED_RELEASE_GATES,
    ReleaseGateError,
    file_sha256,
    validate_release_manifest,
)


def write_manifest(path: Path, *, status: str = "approved") -> None:
    now = datetime.now(UTC)
    payload = {
        "release_id": "change-123",
        "environment": "production",
        "generated_at": now.isoformat(),
        "approvals": [
            {
                "gate": gate,
                "status": status,
                "owner": f"{gate} owner",
                "approval_id": f"approval-{gate}",
                "approved_at": now.isoformat() if status == "approved" else None,
                "expires_at": (now + timedelta(days=30)).isoformat(),
                "evidence_uri": f"urn:test:{gate}",
            }
            for gate in sorted(REQUIRED_RELEASE_GATES)
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_valid_release_manifest_requires_digest_and_all_approvals(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    write_manifest(path)
    manifest = validate_release_manifest(
        path, expected_environment="production", expected_sha256=file_sha256(path)
    )
    assert manifest.release_id == "change-123"


def test_release_manifest_fails_closed_for_pending_or_tampered_data(tmp_path: Path) -> None:
    path = tmp_path / "approvals.json"
    write_manifest(path, status="pending")
    with pytest.raises(ReleaseGateError, match="not-approved"):
        validate_release_manifest(path, expected_environment="production")
    expected = file_sha256(path)
    path.write_text(path.read_text(encoding="utf-8") + " ", encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="SHA-256"):
        validate_release_manifest(path, expected_environment="production", expected_sha256=expected)


def test_release_manifest_rejects_missing_wrong_environment_and_duplicate_gate(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(ReleaseGateError, match="missing"):
        validate_release_manifest(missing, expected_environment="production")

    path = tmp_path / "approvals.json"
    write_manifest(path)
    with pytest.raises(ReleaseGateError, match="different environment"):
        validate_release_manifest(path, expected_environment="staging")

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["approvals"].append(payload["approvals"][0])
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="duplicate"):
        validate_release_manifest(path, expected_environment="production")

    payload["approvals"] = payload["approvals"][1:-1]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ReleaseGateError, match="missing"):
        validate_release_manifest(path, expected_environment="production")
