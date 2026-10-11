from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


def backup_and_verify(source: Path, destination: Path, *, force: bool = False) -> dict[str, object]:
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination:
        raise ValueError("Backup destination must differ from the source database")
    if destination.exists() and not force:
        raise FileExistsError("Backup already exists; use --force to replace this exact file")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as backup_db:
        source_db.backup(backup_db)
        integrity = backup_db.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise RuntimeError("SQLite backup integrity check failed")
        counts = {
            table: backup_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]  # noqa: S608
            for table in ("customers", "observations", "follow_up_cases", "audit_events")
        }
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    return {
        "source": str(source),
        "backup": str(destination),
        "sha256": digest,
        "integrity_check": "ok",
        "row_counts": counts,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and verify a local SQLite backup")
    parser.add_argument("--source", type=Path, default=Path("data/medicare.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(backup_and_verify(args.source, args.output, force=args.force), indent=2))


if __name__ == "__main__":
    main()
