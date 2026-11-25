"""Persisted one-time consent per adapter (LD-04 / doc 91 §3)."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class ConsentRecord:
    adapter: str
    version: str
    consented_at: str


def _db_path() -> Path:
    override = os.environ.get("CODING_AGENT_CONSENT_DB", "").strip()
    if override:
        return Path(override)
    data = os.environ.get("COMPANY_OS_DATA_DIR", "").strip()
    if data:
        root = Path(data)
    else:
        # packages/coding_agents -> repo root
        root = Path(__file__).resolve().parents[2] / "data"
    root.mkdir(parents=True, exist_ok=True)
    return root / "coding_agent_consent.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS adapter_consent (
            adapter TEXT NOT NULL,
            version TEXT NOT NULL,
            consented_at TEXT NOT NULL,
            PRIMARY KEY (adapter, version)
        )
        """
    )
    conn.commit()
    return conn


def has_consent(adapter: str, version: str | None = None) -> bool:
    with _connect() as conn:
        if version:
            row = conn.execute(
                "SELECT 1 FROM adapter_consent WHERE adapter=? AND version=?",
                (adapter, version),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT 1 FROM adapter_consent WHERE adapter=? LIMIT 1",
                (adapter,),
            ).fetchone()
        return row is not None


def record_consent(adapter: str, version: str = "unknown") -> ConsentRecord:
    rec = ConsentRecord(
        adapter=adapter,
        version=version,
        consented_at=datetime.now(timezone.utc).isoformat(),
    )
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO adapter_consent(adapter, version, consented_at)
            VALUES (?, ?, ?)
            ON CONFLICT(adapter, version) DO UPDATE SET consented_at=excluded.consented_at
            """,
            (rec.adapter, rec.version, rec.consented_at),
        )
        conn.commit()
    return rec


def require_consent(adapter: str) -> None:
    # Platform-operated paths: no third-party ToS click-through.
    if adapter in {"hermes", "opencode", "openhands_builder", "inhouse", "inhouse_stub"}:
        return
    if not has_consent(adapter):
        raise PermissionError(f"consent required for adapter {adapter}")


def clear_consents() -> None:
    path = _db_path()
    if path.exists():
        path.unlink()
