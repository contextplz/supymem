"""Local provider spend in micro-dollars, without a wallet or payment service."""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
from pathlib import Path

from core.clock import utcnow
from cryptography.exceptions import InvalidTag

from app.core.local_mode import is_local
from app.credits.errors import CreditsUnavailable
from app.credits.units import usd_to_micro


class LocalSpendLimit(CreditsUnavailable):
    code = "local_spend_limit"

    def __init__(self):
        super().__init__("Your monthly spending limit stops this paid call. "
            "Open Keys and connections, then Your monthly spend cap, and raise or remove Monthly limit (USD) to continue.")


def _directory() -> Path:
    if not is_local():
        raise RuntimeError("local spend is only available in local mode")
    from desktop_runtime.paths import os_conventional_app_data_dir

    override = os.environ.get("COMPANY_OS_DATA_DIR", "").strip()
    return Path(override).expanduser() if override else os_conventional_app_data_dir()


def monthly_cap_usd_micro() -> int | None:
    try:
        from app.core.local_secrets import read_settings

        value = read_settings("spend")["values"].get("monthly_spend_cap")
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
            raise TypeError
        amount = Decimal(str(value))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return usd_to_micro(amount)
    except (OSError, AttributeError, ValueError, TypeError, InvalidOperation, InvalidTag) as exc:
        raise CreditsUnavailable("You need a valid monthly spending limit. "
            "Open Keys and connections, then Your monthly spend cap, and set a non-negative Monthly limit (USD) "
            "or remove the limit to continue.") from exc


@contextmanager
def _database():
    connection = None
    try:
        directory = _directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = directory / "local_spend.sqlite3"
        from app.db.session import _apply_sqlite_pragmas

        connection = sqlite3.connect(path, timeout=30)
        _apply_sqlite_pragmas(connection)
        path.chmod(0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("""CREATE TABLE IF NOT EXISTS calls (
            call_id TEXT PRIMARY KEY, month TEXT NOT NULL, task_id TEXT, run_id TEXT,
            workspace_id TEXT, venture_id TEXT, purpose TEXT NOT NULL, request_id TEXT,
            provider TEXT, model TEXT, estimate_usd_micro INTEGER NOT NULL,
            cost_usd_micro INTEGER, prompt_tokens INTEGER, completion_tokens INTEGER,
            cached_tokens INTEGER, status TEXT NOT NULL, receipt_json TEXT)""")
        with connection:
            yield connection
    except (OSError, sqlite3.Error) as exc:
        raise CreditsUnavailable("You cannot save your spending history right now. "
            "Check that your local data folder is writable and has free space before trying again.") from exc
    finally:
        if connection is not None:
            connection.close()


def _month() -> str:
    return utcnow().strftime("%Y-%m")


def _spent(connection, month: str) -> int:
    return connection.execute("SELECT COALESCE(SUM(COALESCE(cost_usd_micro, estimate_usd_micro)), 0) "
        "FROM calls WHERE month = ?", (month,)).fetchone()[0]


def begin(meter) -> None:
    if type(meter.estimate_micro) is not int or meter.estimate_micro < 0:
        raise ValueError("estimated cost must be a non-negative integer")
    from app.credits.gate import pending_model_receipt
    from app.tasks import usage

    if meter.model_binding is not None:
        from app.credits.metered import model_call_key
        meter.trace_id = model_call_key(workspace_id=meter.workspace_id, venture_id=meter.venture_id,
            purpose=meter.purpose, request_id=meter.request_id)
    context = usage._CONTEXT.get()
    task_id = None
    if context is not None:
        from app.db.task_models import TaskRun
        from app.state import db
        with db()() as session:
            run = session.get(TaskRun, context[0])
            if run is None:
                raise CreditsUnavailable("You cannot find this task. Open your task list before trying again.")
            task_id = run.task_id
    cap = monthly_cap_usd_micro()
    month = _month()
    with _database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if meter.model_binding is not None and connection.execute(
            "SELECT 1 FROM calls WHERE workspace_id IS ? AND venture_id IS ? AND purpose = ? AND request_id = ?",
            (meter.workspace_id, meter.venture_id, meter.purpose, meter.request_id)).fetchone():
            raise CreditsUnavailable("You already started this request. Check your task result before trying again.")
        if meter.estimate_micro > 0 and cap is not None and _spent(connection, month) + meter.estimate_micro > cap:
            raise LocalSpendLimit()
        connection.execute("INSERT INTO calls (call_id, month, task_id, run_id, workspace_id, venture_id, "
            "purpose, request_id, provider, model, estimate_usd_micro, status, receipt_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meter.trace_id, month, task_id, context[0] if context else None, meter.workspace_id,
             meter.venture_id, meter.purpose, meter.request_id, meter.provider, meter.model, meter.estimate_micro, "pending",
             json.dumps(pending_model_receipt(meter), allow_nan=False)))


def protect_outcome(meter) -> None:
    from app.credits.gate import pending_model_receipt

    with _database() as connection:
        changed = connection.execute("UPDATE calls SET receipt_json = ? WHERE call_id = ? AND status = 'pending'",
            (json.dumps(pending_model_receipt(meter), allow_nan=False), meter.trace_id)).rowcount
        if changed != 1:
            raise CreditsUnavailable("You cannot confirm this request in your spending history. "
                "Open Tasks and check the saved result before trying again.")


def finish(meter) -> None:
    from app.credits.gate import captured_receipt, pending_model_receipt

    receipt = captured_receipt(meter) if meter.recorded else None
    cost = receipt["provider_actual_micro"] if receipt else (0 if meter.definitive_failure else None)
    with _database() as connection:
        changed = connection.execute("UPDATE calls SET cost_usd_micro = ?, prompt_tokens = ?, completion_tokens = ?, "
            "cached_tokens = ?, status = ?, receipt_json = ? WHERE call_id = ? AND status IN ('pending', 'unknown')",
            (cost, meter.prompt_tokens, meter.completion_tokens, meter.cached_tokens,
             "confirmed" if receipt else "unspent" if cost == 0 else "unknown",
             json.dumps(receipt or pending_model_receipt(meter), allow_nan=False), meter.trace_id)).rowcount
        if changed != 1:
            row = connection.execute("SELECT status, receipt_json FROM calls WHERE call_id = ?", (meter.trace_id,)).fetchone()
            if row is not None and row["status"] == "confirmed" and (not meter.recorded or
                    json.dumps(_canonical_receipt(json.loads(row["receipt_json"])), sort_keys=True, allow_nan=False)
                    == json.dumps(_canonical_receipt(receipt), sort_keys=True, allow_nan=False)):
                return
            raise CreditsUnavailable("You cannot find this call in your spending history. "
                "Check your local data folder before trying again.")


def _canonical_receipt(usage: dict) -> dict:
    receipt = dict(usage)
    if receipt.get("model") and receipt.get("provenance") == "provider_usage":
        receipt.pop("provenance")
        receipt.pop("output", None)
    if "actual_micro" in receipt:
        receipt.setdefault("provider_actual_micro", receipt["actual_micro"])
    if receipt.get("model_binding") is not None:
        receipt.setdefault("provider_output_error", None)
    elif receipt.get("model") is None:
        for name in ("model_binding", "request_digest", "token_ceiling"):
            if receipt.get(name) is None or name == "token_ceiling" and type(receipt[name]) is int and receipt[name] == 0:
                receipt.pop(name, None)
    return receipt


def reconcile(usage: dict) -> int:
    usage = dict(usage)
    with _database() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute("SELECT status, cost_usd_micro, receipt_json FROM calls WHERE call_id = ?",
            (usage.get("local_call_id"),)).fetchone()
        expected = dict(usage)
        unspent = expected.pop("definitive_unspent", None) is True
        cap = expected.pop("cap_at_reservation", False) if unspent else False
        if unspent:
            settled = expected.pop("settled", True)
            charged = expected.pop("credits_micro", 0)
            if settled is not True or type(charged) is not int or charged != 0:
                raise CreditsUnavailable("You cannot confirm this saved result. "
                    "Open Tasks and check the saved result before trying this request again.")
        stored = _canonical_receipt(json.loads(row["receipt_json"])) if row is not None and row["receipt_json"] is not None else None
        supplied = _canonical_receipt(expected if unspent else usage)
        if stored is not None and stored.get("model_binding") is None and stored.get("request_digest") is None:
            supplied = {name: stored[name] for name in ("model_binding", "request_digest", "token_ceiling")
                if name in stored} | supplied
        if (not unspent and stored is not None and (stored.get("model_binding") is not None
                or stored.get("model") and usage.get("provenance") == "provider_usage")
                and row["status"] in {"pending", "unknown"} and row["cost_usd_micro"] is None):
            from app.credits.gate import _check_model_projection, _costs, _counts

            if stored.get("model_binding") is not None:
                _check_model_projection(supplied)
            else:
                _counts(supplied, required=True)
                _costs(supplied)
            projected = stored | {name: supplied[name] for name in (
                "actual_micro", "provider_actual_micro", "prompt_tokens", "completion_tokens", "total_tokens",
                "cached_tokens", "provider_receipt", "billing_output_error", "reasoning_tokens",
                "cap_at_reservation", "provider_output_error") if name in supplied}
            if json.dumps(projected, sort_keys=True, allow_nan=False) != json.dumps(supplied, sort_keys=True, allow_nan=False):
                raise CreditsUnavailable("You cannot confirm this saved result against your spending history. "
                    "Open Tasks and check the saved result before trying again.")
            cost = supplied["provider_actual_micro"]
            connection.execute("UPDATE calls SET status = 'confirmed', cost_usd_micro = ?, prompt_tokens = ?, "
                "completion_tokens = ?, cached_tokens = ?, receipt_json = ? WHERE call_id = ?",
                (cost, supplied["prompt_tokens"], supplied["completion_tokens"], supplied["cached_tokens"],
                 json.dumps(supplied, allow_nan=False), supplied["local_call_id"]))
            row = dict(row) | {"status": "confirmed", "cost_usd_micro": cost}
            stored = supplied
        if (stored is None
                or type(cap) is not bool
                or unspent and (row["status"] != "unspent" or row["cost_usd_micro"] != 0)
                or not unspent and (row["status"] != "confirmed" or type(supplied.get("provider_actual_micro")) is not int
                    or supplied["provider_actual_micro"] != row["cost_usd_micro"])
                or json.dumps(stored, sort_keys=True, allow_nan=False) != json.dumps(supplied, sort_keys=True, allow_nan=False)):
            raise CreditsUnavailable("You cannot confirm this saved result against your spending history. "
                "Check your task result before trying again.")
    return 0


def recover(key: str) -> dict | None:
    from app.credits.gate import reconcile_receipt

    with _database() as connection:
        row = connection.execute("SELECT status, receipt_json FROM calls WHERE call_id = ?", (key,)).fetchone()
    if row is None:
        return None
    if row["status"] == "unspent":
        usage = json.loads(row["receipt_json"])
        reconcile_receipt(usage | {"definitive_unspent": True})
        return {"credits_micro": 0, "output_usable": False, "outcome": "rejected"}
    if row["status"] != "confirmed":
        raise CreditsUnavailable("You cannot confirm whether this request finished. "
            "Check your provider's usage history before trying again.")
    usage = json.loads(row["receipt_json"])
    reconcile_receipt(usage)
    return {"credits_micro": 0, "output_usable": not usage["billing_output_error"], "outcome": "confirmed"}


def snapshot(*, workspace_id: str | None = None, venture_id: str | None = None, run_id: str | None = None) -> dict:
    from app.credits.spend import category

    cap = monthly_cap_usd_micro()
    month = _month()
    where = "month = ?"
    parameters = [month]
    for name, value in (("workspace_id", workspace_id), ("venture_id", venture_id)):
        if value is not None:
            where += f" AND {name} = ?"
            parameters.append(value)
    if run_id is not None:
        where += " AND (run_id = ? OR request_id = ? OR substr(request_id, 1, length(?) + 1) = ? || ':')"
        parameters.extend([run_id] * 4)
    with _database() as connection:
        rows = connection.execute("SELECT task_id, run_id, workspace_id, venture_id, purpose, "
            "COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, COALESCE(SUM(completion_tokens), 0) AS completion_tokens, "
            "COALESCE(SUM(cached_tokens), 0) AS cached_tokens, "
            "SUM(COALESCE(cost_usd_micro, estimate_usd_micro)) AS cost_usd_micro, "
            "SUM(CASE WHEN cost_usd_micro IS NULL THEN 1 ELSE 0 END) AS unconfirmed_calls "
            f"FROM calls WHERE {where} GROUP BY task_id, run_id, workspace_id, venture_id, purpose", parameters).fetchall()
        features = {}
        for row in rows:
            feature = category(row["purpose"])
            features[feature] = features.get(feature, 0) + row["cost_usd_micro"]
        return {"month": month, "month_cost_usd_micro": sum(features.values()),
            "monthly_cap_usd_micro": cap, "by_feature_usd_micro": features, "tasks": [dict(row) for row in rows]}


def check_limit(estimate_usd_micro: int) -> None:
    if type(estimate_usd_micro) is not int or estimate_usd_micro < 0:
        raise ValueError("estimated cost must be a non-negative integer")
    cap = monthly_cap_usd_micro()
    with _database() as connection:
        if estimate_usd_micro > 0 and cap is not None and _spent(connection, _month()) + estimate_usd_micro > cap:
            raise LocalSpendLimit()


def run_cost(workspace_id: str, venture_id: str, run_ids: set[str]) -> int:
    if not run_ids:
        return 0
    with _database() as connection:
        rows = connection.execute("SELECT run_id, request_id, COALESCE(cost_usd_micro, estimate_usd_micro) AS cost "
            "FROM calls WHERE workspace_id = ? AND venture_id = ?", (workspace_id, venture_id)).fetchall()
    return sum(row["cost"] for row in rows if row["run_id"] in run_ids or any(
        row["request_id"] == run_id or (row["request_id"] or "").startswith(run_id + ":") for run_id in run_ids))
