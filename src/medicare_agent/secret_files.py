from __future__ import annotations

from pathlib import Path

MAX_SECRET_FILE_BYTES = 64 * 1024
MIN_SECRET_LENGTH = 32
DEFAULT_SECRET_MARKER = "change-me"  # noqa: S105 - placeholder marker, not a credential


def read_secret_file(path: Path, label: str) -> str:
    """Read a small, non-empty secret from a mounted file."""

    try:
        if not path.is_file() or path.stat().st_size > MAX_SECRET_FILE_BYTES:
            raise ValueError(f"{label} must reference a small readable file")
        value = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Unable to read {label}") from exc
    if not value:
        raise ValueError(f"{label} cannot be empty")
    return value


def is_weak_secret(value: str) -> bool:
    return DEFAULT_SECRET_MARKER in value or len(value) < MIN_SECRET_LENGTH
