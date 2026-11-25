"""Private task billing checkpoints over the shared wallet recovery protocol."""
import hashlib
import json
from contextlib import nullcontext

from sqlalchemy import select

from app.credits.errors import CreditsUnavailable
from app.db.task_models import Task, TaskEvent, TaskRun, TaskStep

FORMAT = "task_billing_v1"
FIELDS = ("source", "reservation_id", "account_id", "estimate_micro", "workspace_id", "venture_id", "purpose",
          "model", "provider", "request_id", "task_invocation", "actual_micro", "provider_actual_micro",
          "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens")
BINDING_FIELDS = ("source", "reservation_id", "account_id", "estimate_micro", "workspace_id", "venture_id", "purpose",
                  "model", "provider", "request_id", "task_invocation")
UNSUPPORTED_RECEIPT_FIELDS = frozenset({"call_binding", "catalog_model", "actual_catalog_model", "returned_model", "output_error"})


def _invalid():
    return CreditsUnavailable("The saved billing receipt needs reconciliation before this result can be applied.")


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _receipt_binding(usage):
    return {key: usage[key] for key in BINDING_FIELDS if key in usage}


def _usage(value):
    if not isinstance(value, dict) or UNSUPPORTED_RECEIPT_FIELDS.intersection(value) or any(key not in value for key in FIELDS):
        raise _invalid()
    if value.get("definitive_unspent") is not None and value.get("definitive_unspent") is not False:
        raise _invalid()  # Confirmed rejection cleanup cannot prove a successful response.
    result = {key: value[key] for key in FIELDS}
    if not isinstance(result["source"], str) or result["source"] not in {"platform", "byok", "free"}:
        raise _invalid()
    for key in ("estimate_micro", "actual_micro", "provider_actual_micro", "prompt_tokens", "completion_tokens", "cached_tokens", "total_tokens"):
        if type(result[key]) is not int or result[key] < 0:
            raise _invalid()
    if (result["total_tokens"] != result["prompt_tokens"] + result["completion_tokens"]
            or result["cached_tokens"] > result["prompt_tokens"]):
        raise _invalid()
    for key in ("workspace_id", "purpose", "request_id"):
        if not isinstance(result[key], str) or not 1 <= len(result[key]) <= 512:
            raise _invalid()
    for key in ("venture_id", "reservation_id", "account_id", "model", "provider", "task_invocation"):
        if result[key] is not None and (not isinstance(result[key], str) or not 1 <= len(result[key]) <= 512):
            raise _invalid()
    if result["source"] == "platform":
        if not result["reservation_id"] or not result["account_id"] or result["estimate_micro"] < 1:
            raise _invalid()
    elif result["reservation_id"] is not None:
        raise _invalid()  # BYOK/free never invent a wallet hold.
    if result["source"] == "free" and (result["actual_micro"] or result["provider_actual_micro"]):
        raise _invalid()
    return result


def _binding(usage, *, workspace_id, venture_id, purpose, request_id, run_id, step_id, session=None):
    if (usage["workspace_id"], usage["venture_id"], usage["purpose"], usage["request_id"]) != (
            workspace_id, venture_id, purpose, request_id):
        raise _invalid()
    if (run_id is None) != (step_id is None):
        raise _invalid()
    event_id = task_id = None
    if run_id is None:
        if usage["task_invocation"] is not None:
            raise _invalid()
    else:
        invocation = usage["task_invocation"]
        if not isinstance(invocation, str) or len(invocation) > 128:
            raise _invalid()
        from app.state import db  # Current explicit test/runtime factory, never ambient run authority.
        with (nullcontext(session) if session is not None else db()()) as reader, reader.no_autoflush:
            run, step = reader.get(TaskRun, run_id), reader.get(TaskStep, step_id)
            task = reader.get(Task, run.task_id) if run else None
            events = reader.scalars(select(TaskEvent).where(TaskEvent.kind == "call_reserved",
                TaskEvent.data_json.contains(json.dumps(invocation), autoescape=True))).all()
            matches = []
            for event in events:
                try:
                    data = json.loads(event.data_json)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict) and data.get("invocation") == invocation:
                    matches.append((event, data))
            if (not task or not run or not step or step.run_id != run.id
                    or (task.workspace_id, task.venture_id) != (workspace_id, venture_id) or len(matches) != 1):
                raise _invalid()
            event, data = matches[0]
            if ((event.task_id, event.run_id, event.step_id) != (task.id, run.id, step.id)
                    or type(data.get("cost_micro")) is not int or data["cost_micro"] != usage["estimate_micro"]):
                raise _invalid()
            if "call_binding" in data:
                raise _invalid()
            task_id, event_id = task.id, event.id
    return {**_receipt_binding(usage), "run_id": run_id, "step_id": step_id,
            "task_id": task_id, "reserved_event_id": event_id}


def capture(usage, *, workspace_id, venture_id, purpose, request_id, run_id=None, step_id=None, session=None):
    """Capture confirmed metering data before meter exit; no wallet claim yet."""
    normalized = _usage(usage)
    binding = _binding(normalized, workspace_id=workspace_id, venture_id=venture_id, purpose=purpose,
        request_id=request_id, run_id=run_id, step_id=step_id, session=session)
    result = {"format": FORMAT, "usage": normalized, "binding": binding}
    return {**result, "sha256": _hash(result)}


def _validate_checkpoint(checkpoint, *, workspace_id, venture_id, purpose, request_id, run_id=None, step_id=None, session=None):
    if not isinstance(checkpoint, dict) or set(checkpoint) != {"format", "usage", "binding", "sha256"}:
        raise _invalid()
    try:
        expected = capture(checkpoint["usage"], workspace_id=workspace_id, venture_id=venture_id,
            purpose=purpose, request_id=request_id, run_id=run_id, step_id=step_id, session=session)
        if checkpoint != expected:
            raise _invalid()
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        raise _invalid() from exc
    return expected


def validate_saved(checkpoint, *, workspace_id, venture_id, purpose, request_id, run_id=None, step_id=None, session=None):
    """Read-only proof for an applicable result, including original Task attribution."""
    return _validate_checkpoint(checkpoint, workspace_id=workspace_id, venture_id=venture_id,
        purpose=purpose, request_id=request_id, run_id=run_id, step_id=step_id, session=session)


def reconcile(checkpoint, *, workspace_id, venture_id, purpose, request_id, run_id=None, step_id=None):
    """No transport or repricing. Shared recovery proves both wallet and Task settlement."""
    expected = _validate_checkpoint(checkpoint, workspace_id=workspace_id, venture_id=venture_id,
        purpose=purpose, request_id=request_id, run_id=run_id, step_id=step_id)
    try:
        from app.credits.gate import reconcile_receipt
        charged = reconcile_receipt(expected["usage"], _receipt_binding(expected["binding"]))
    except (AttributeError, ImportError, KeyError, TypeError, ValueError) as exc:
        raise _invalid() from exc
    if type(charged) is not int or charged < 0 or expected["usage"]["source"] != "platform" and charged != 0:
        raise _invalid()
    return charged
