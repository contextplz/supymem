"""Account/device-scoped native execution receipts; never an execution grant."""
import json

from desktop_runtime.offline import connect


def _db():
    conn = connect()
    conn.execute("""CREATE TABLE IF NOT EXISTS native_executions (
        gateway TEXT NOT NULL, account_id TEXT NOT NULL, device_id TEXT NOT NULL,
        order_id TEXT NOT NULL, venture_id TEXT NOT NULL, receipt TEXT NOT NULL,
        PRIMARY KEY (gateway, account_id, device_id, order_id))""")
    conn.commit()
    return conn


def probe():
    """Prove that the durable journal can acquire a write lock without saving a run."""
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("ROLLBACK")


def create(scope, venture_id, order_id, *, resume=False):
    with _db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        prior = conn.execute("SELECT receipt FROM native_executions WHERE gateway=? AND account_id=? AND device_id=? AND order_id=?",
                             (*scope, order_id)).fetchone()
        if resume and prior:
            receipt = json.loads(prior[0])
            evidence = receipt.get("evidence") or {}
            if (receipt.get("effect_outcome_unconfirmed") or receipt.get("state") in {
                    "admitting_action", "running_coding", "running_terminal", "deploying",
                    "closing", "reconciliation_required", "interrupted_reconciliation_required"}
                    or receipt.get("state") == "acting" and receipt.get("tool") != "ai_loop"
                    or (evidence.get("inventory") or {}).get("processes_stopped") is False):
                raise ValueError("native_execution_requires_reconciliation")
            return
        if prior and json.loads(prior[0]).get("state") != "not_started":
            raise ValueError("native_execution_already_recorded")
        conn.execute("INSERT OR REPLACE INTO native_executions VALUES (?,?,?,?,?,?)", (*scope, order_id, venture_id,
            json.dumps({"order_id": order_id, "state": "starting", "task_verified": False})))


def save(scope, venture_id, order_id, receipt):
    raw = json.dumps(receipt, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    # Product plans are <=32 KiB UTF-8 plus bounded iteration receipts/events
    # and process custody. Preserve them without changing other executors' cap.
    ceiling = 256000 if receipt.get("product_profile") == "web_app_vercel_v1" else 65536
    if len(raw.encode()) > ceiling:
        raise ValueError("native_receipt_too_large")
    with _db() as conn:
        result = conn.execute("UPDATE native_executions SET receipt=? WHERE gateway=? AND account_id=? AND device_id=? AND order_id=? AND venture_id=?",
                              (raw, *scope, order_id, venture_id))
        if result.rowcount != 1:
            raise ValueError("native_execution_missing")


def read(scope, venture_id, order_id):
    with _db() as conn:
        row = conn.execute("SELECT receipt FROM native_executions WHERE gateway=? AND account_id=? AND device_id=? AND order_id=? AND venture_id=?",
                           (*scope, order_id, venture_id)).fetchone()
    return json.loads(row[0]) if row else None


def read_for_cancelled_recovery(scope, venture_id, order_id, original_device_id):
    """Read one old-device journal row for the same account and exact order.

    The server must first identify the original device on a cancelled order.
    This is only evidence retrieval; it cannot create or replay an execution.
    """
    if not original_device_id or original_device_id == scope[2]:
        return None
    return read((scope[0], scope[1], original_device_id), venture_id, order_id)
