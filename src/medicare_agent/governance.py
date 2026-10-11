from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

REQUIRED_RELEASE_GATES = frozenset(
    {
        "clinical_policy",
        "privacy_legal",
        "security_review",
        "identity_access",
        "backup_restore",
        "monitoring_alerting",
        "incident_response",
        "consent_governance",
        "provider_contracts",
    }
)

EvidenceText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=3, max_length=500)
]


class Approval(BaseModel):
    model_config = ConfigDict(extra="forbid")

    gate: EvidenceText
    status: Literal["approved", "pending", "rejected"]
    owner: EvidenceText
    approval_id: EvidenceText
    approved_at: datetime | None = None
    expires_at: datetime | None = None
    evidence_uri: EvidenceText


class ReleaseManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    release_id: EvidenceText
    environment: Literal["staging", "production"]
    generated_at: datetime
    approvals: list[Approval] = Field(min_length=1, max_length=30)


class ReleaseGateError(RuntimeError):
    pass


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_release_manifest(
    path: Path,
    *,
    expected_environment: str,
    expected_sha256: str | None = None,
    now: datetime | None = None,
) -> ReleaseManifest:
    if not path.is_file() or path.stat().st_size > 256 * 1024:
        raise ReleaseGateError("Release approvals manifest is missing or too large")
    digest = file_sha256(path)
    if expected_sha256 and digest != expected_sha256.lower():
        raise ReleaseGateError("Release approvals manifest SHA-256 does not match")
    manifest = ReleaseManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if manifest.environment != expected_environment:
        raise ReleaseGateError("Release approvals manifest targets a different environment")
    approvals = {approval.gate: approval for approval in manifest.approvals}
    if len(approvals) != len(manifest.approvals):
        raise ReleaseGateError("Release approvals manifest contains duplicate gates")
    missing = sorted(REQUIRED_RELEASE_GATES - approvals.keys())
    if missing:
        raise ReleaseGateError(f"Release approvals are missing: {', '.join(missing)}")
    current = now or datetime.now(UTC)
    invalid: list[str] = []
    for gate in sorted(REQUIRED_RELEASE_GATES):
        approval = approvals[gate]
        if approval.status != "approved" or approval.approved_at is None:
            invalid.append(f"{gate}:not-approved")
            continue
        approved_at = approval.approved_at
        if approved_at.tzinfo is None:
            invalid.append(f"{gate}:timezone-missing")
        elif approved_at > current:
            invalid.append(f"{gate}:future-approval")
        if approval.expires_at is not None and (
            approval.expires_at.tzinfo is None or approval.expires_at <= current
        ):
            invalid.append(f"{gate}:expired")
        if not (
            approval.evidence_uri.startswith("https://") or approval.evidence_uri.startswith("urn:")
        ):
            invalid.append(f"{gate}:invalid-evidence-uri")
    if invalid:
        raise ReleaseGateError(f"Release approvals failed: {', '.join(invalid)}")
    return manifest
