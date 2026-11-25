"""Account and gateway scoped outbox. Only an upstream acknowledgment delivers it.

Legacy unscoped rows are quarantined: their owner is unknown. An interrupted
send becomes uncertain and requires reconciliation instead of a blind retry.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from desktop_runtime.paths import data_root


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect() -> sqlite3.Connection:
    path = Path(data_root(create=True)) / "bridge_outbox.sqlite"
    conn = sqlite3.connect(str(path))
    path.chmod(0o600)
    conn.execute("PRAGMA secure_delete=ON")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS scoped_outbox (
        gateway TEXT NOT NULL, account_id TEXT NOT NULL, idempotency_key TEXT NOT NULL,
        method TEXT NOT NULL, path TEXT NOT NULL, body TEXT NOT NULL,
        created_at TEXT NOT NULL, sent_at TEXT, state TEXT NOT NULL DEFAULT 'queued',
        last_error TEXT, response_status INTEGER,
        PRIMARY KEY (gateway, account_id, idempotency_key)
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS scoped_cache (
        gateway TEXT NOT NULL, account_id TEXT NOT NULL, key TEXT NOT NULL,
        value TEXT NOT NULL, cached_at TEXT NOT NULL,
        PRIMARY KEY (gateway, account_id, key)
    )""")
    # Earlier versions saved authorization material in plaintext. Never reuse it.
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cache'").fetchone():
        conn.execute("DELETE FROM cache WHERE key='pkce_tokens'")
    conn.commit()
    return conn


def _scope(gateway: str, account_id: str) -> tuple[str, str]:
    if not gateway or not account_id:
        raise ValueError("verified_account_required")
    return gateway.rstrip("/"), account_id


def enqueue(method: str, path: str, body: Any, *, idempotency_key: str,
            gateway: str, account_id: str) -> dict[str, Any]:
    scope = _scope(gateway, account_id)
    payload = json.dumps(body, sort_keys=True, separators=(",", ":"))
    with connect() as conn:
        existing = conn.execute(
            "SELECT * FROM scoped_outbox WHERE gateway=? AND account_id=? AND idempotency_key=?",
            (*scope, idempotency_key),
        ).fetchone()
        if existing:
            if (existing["method"], existing["path"], existing["body"]) != (method, path, payload):
                raise ValueError("idempotency_conflict")
            return {"idempotency_key": idempotency_key, "replayed": True,
                    "already_sent": existing["sent_at"] is not None, "state": existing["state"]}
        conn.execute(
            "INSERT INTO scoped_outbox (gateway,account_id,idempotency_key,method,path,body,created_at) VALUES (?,?,?,?,?,?,?)",
            (*scope, idempotency_key, method, path, payload, _now()),
        )
    return {"idempotency_key": idempotency_key, "replayed": False, "already_sent": False, "state": "queued"}


def pending(*, gateway: str, account_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT idempotency_key,method,path,body,created_at,state,last_error,response_status FROM scoped_outbox "
            "WHERE gateway=? AND account_id=? AND sent_at IS NULL ORDER BY created_at",
            _scope(gateway, account_id),
        ).fetchall()
    return [{**dict(r), "body": json.loads(r["body"])} for r in rows]


def claim(idempotency_key: str, *, gateway: str, account_id: str) -> bool:
    with connect() as conn:
        result = conn.execute(
            "UPDATE scoped_outbox SET state='sending', last_error=NULL "
            "WHERE gateway=? AND account_id=? AND idempotency_key=? AND state IN ('queued','auth_required')",
            (*_scope(gateway, account_id), idempotency_key),
        )
    return result.rowcount == 1


def record_result(idempotency_key: str, *, gateway: str, account_id: str,
                  state: str, status: int | None = None, error: str | None = None) -> None:
    if state not in {"delivered", "auth_required", "uncertain", "rejected"}:
        raise ValueError("invalid_delivery_state")
    with connect() as conn:
        conn.execute(
            "UPDATE scoped_outbox SET state=?,response_status=?,last_error=?,sent_at=? "
            "WHERE gateway=? AND account_id=? AND idempotency_key=? AND state='sending'",
            (state, status, error, _now() if state == "delivered" else None,
             *_scope(gateway, account_id), idempotency_key),
        )


def recover_interrupted() -> None:
    with connect() as conn:
        conn.execute("UPDATE scoped_outbox SET state='uncertain',last_error='interrupted_delivery' WHERE state='sending'")


def put_cache(key: str, value: Any, *, gateway: str, account_id: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO scoped_cache VALUES (?,?,?,?,?)",
                     (*_scope(gateway, account_id), key, json.dumps(value), _now()))


def get_cache(key: str, *, gateway: str, account_id: str) -> Any | None:
    with connect() as conn:
        row = conn.execute("SELECT value FROM scoped_cache WHERE gateway=? AND account_id=? AND key=?",
                           (*_scope(gateway, account_id), key)).fetchone()
    return json.loads(row["value"]) if row else None
