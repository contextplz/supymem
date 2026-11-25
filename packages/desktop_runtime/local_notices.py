"""In-process native notices for Mac-seat pauses and task finishes.

Needs-you pauses and terminal Mac-task outcomes are local to this Mac and must
not wait on a gateway round-trip before the OS banner can fire. Delivery acks
for native and in-app channels are tracked independently so an in-app banner
cannot mask a native failure.
"""
from __future__ import annotations

import threading
import time
from uuid import uuid4

_lock = threading.Lock()
_pending: list[dict] = []
# Dedupe key -> notice id for recent pushes (needs-you / finish).
_recent: dict[str, float] = {}
_DEDUPE_SECONDS = 120.0


def _normalize_target(target_path: str | None) -> str | None:
    if not target_path:
        return None
    candidate = target_path.strip()
    if "?" in candidate or ".." in candidate:
        return None
    return candidate


def push(
    *,
    title: str,
    body: str,
    target_path: str | None = None,
    kind: str = "device_needs_you",
    dedupe_key: str | None = None,
    actor_id: str | None = None,
) -> str | None:
    """Enqueue a local notice. Returns None when a recent duplicate is skipped."""
    now = time.time()
    key = f"{actor_id or 'unattributed'}:" + (dedupe_key or f"{kind}:{title}:{body}:{target_path or ''}")
    with _lock:
        expired = [k for k, ts in _recent.items() if now - ts > _DEDUPE_SECONDS]
        for old in expired:
            _recent.pop(old, None)
        if key in _recent:
            return None
        notice_id = "not_local_" + uuid4().hex[:16]
        _pending.append({
            "id": notice_id,
            "actor_id": actor_id,
            "title": title[:120],
            "body": body[:400],
            "kind": kind[:80],
            "target_path": _normalize_target(target_path),
            "ts": now,
            "acked": False,
            "in_app_status": None,
            "native_status": None,
        })
        _recent[key] = now
        if len(_pending) > 40:
            del _pending[:-40]
        return notice_id


def push_needs_you(*, title: str, body: str, target_path: str | None = None,
                   order_id: str | None = None, actor_id: str | None = None) -> str | None:
    key = f"needs_you:{order_id or target_path or title}"
    return push(title=title, body=body, target_path=target_path,
                kind="device_needs_you", dedupe_key=key, actor_id=actor_id)


def push_task_finished(*, outcome: str, task_title: str, target_path: str | None = None,
                       order_id: str | None = None, actor_id: str | None = None) -> str | None:
    """Plain founder-facing finish notice with distinct review and failure states."""
    label = (task_title or "Mac task").strip()[:80] or "Mac task"
    if outcome == "verified":
        title = f"Mac task finished: {label}"
        body = "Supymem finished this Mac task."
        kind = "device_task_finished"
    elif outcome == "review_required":
        title = f"Review coding changes: {label}"
        body = "The coding process stopped. Review its changes and checks before accepting the task."
        kind = "device_task_review_required"
    elif outcome == "visual_review_required":
        title = f"Mac task result ready to review: {label}"
        body = "Supymem interpreted the selected window. Review its visible facts; the full task is not independently verified."
        kind = "device_task_review_required"
    elif outcome == "outcome_review_required":
        title = f"Mac task result ready to review: {label}"
        body = "Supymem reported a result. Review its answer and the source; the task is not independently verified."
        kind = "device_task_review_required"
    elif outcome in {"cancelled", "cancelled_or_expired"}:
        title = f"Mac task cancelled: {label}"
        body = "This Mac task was cancelled before it finished."
        kind = "device_task_cancelled"
    else:
        title = f"Mac task failed: {label}"
        body = "This Mac task did not finish cleanly. Open Supymem to review."
        kind = "device_task_failed"
        outcome = "failed"
    key = f"finish:{order_id or target_path or label}:{outcome}"
    return push(title=title, body=body, target_path=target_path, kind=kind, dedupe_key=key, actor_id=actor_id)


def pending(*, limit: int = 20, actor_id: str | None = None) -> list[dict]:
    """Return notices that still need at least one delivery channel."""
    with _lock:
        out = []
        for item in _pending:
            if actor_id is not None and item.get("actor_id") != actor_id:
                continue
            native = item.get("native_status")
            in_app = item.get("in_app_status")
            native_done = isinstance(native, str) and (
                native == "native_shown" or native.startswith("native_failed:")
            )
            in_app_done = in_app == "in_app_shown"
            if native_done and in_app_done:
                continue
            out.append(dict(item))
            if len(out) >= limit:
                break
        return out


def record_results(results: list[dict], *, actor_id: str | None = None) -> dict:
    """Record independent channel acks. Fully settled notices are removed."""
    if not isinstance(results, list):
        return {"recorded": 0, "settled": 0, "statuses": []}
    recorded = 0
    settled = 0
    statuses: list[dict] = []
    with _lock:
        by_id = {item.get("id"): item for item in _pending
                 if item.get("id") and (actor_id is None or item.get("actor_id") == actor_id)}
        for entry in results:
            if not isinstance(entry, dict):
                continue
            notice_id = entry.get("id")
            if not isinstance(notice_id, str) or notice_id not in by_id:
                continue
            item = by_id[notice_id]
            in_app = entry.get("in_app")
            native = entry.get("native")
            if in_app == "in_app_shown":
                item["in_app_status"] = "in_app_shown"
                recorded += 1
            if isinstance(native, str) and (
                native == "native_shown" or native.startswith("native_failed:")
            ):
                item["native_status"] = native
                recorded += 1
            statuses.append({
                "id": notice_id,
                "in_app_status": item.get("in_app_status"),
                "native_status": item.get("native_status"),
            })
            native_done = isinstance(item.get("native_status"), str) and (
                item["native_status"] == "native_shown"
                or item["native_status"].startswith("native_failed:")
            )
            in_app_done = item.get("in_app_status") == "in_app_shown"
            if native_done and in_app_done:
                _pending[:] = [row for row in _pending if row.get("id") != notice_id]
                settled += 1
    return {"recorded": recorded, "settled": settled, "statuses": statuses}


def ack(ids: list[str], *, actor_id: str | None = None) -> int:
    """Legacy full ack (both channels). Prefer record_results."""
    wanted = {value for value in ids if value}
    if not wanted:
        return 0
    with _lock:
        before = len(_pending)
        _pending[:] = [item for item in _pending if item.get("id") not in wanted or (actor_id is not None and item.get("actor_id") != actor_id)]
        return before - len(_pending)


def reset_for_tests() -> None:
    with _lock:
        _pending.clear()
        _recent.clear()
