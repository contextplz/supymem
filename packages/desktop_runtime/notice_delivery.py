"""Bridge-local delivery status for remote (gateway) desktop notices.

Native and in-app channels are recorded independently. A notice is only
forwarded to the gateway ack (cleared) once both channels are terminal, so an
in-app banner cannot mask a native failure.
"""
from __future__ import annotations

import threading

_lock = threading.Lock()
# notice_id -> {"in_app_status": str|None, "native_status": str|None}
_status: dict[str, dict[str, str | None]] = {}


def annotate(item: dict) -> dict:
    notice_id = item.get("id")
    out = dict(item)
    if not isinstance(notice_id, str):
        return out
    with _lock:
        state = _status.get(notice_id) or {}
    out["in_app_status"] = state.get("in_app_status")
    out["native_status"] = state.get("native_status")
    return out


def is_settled(notice_id: str | None) -> bool:
    if not isinstance(notice_id, str):
        return False
    with _lock:
        state = _status.get(notice_id) or {}
    return _settled(state)


def _settled(state: dict) -> bool:
    native = state.get("native_status")
    in_app = state.get("in_app_status")
    native_done = isinstance(native, str) and (
        native == "native_shown" or native.startswith("native_failed:")
    )
    return native_done and in_app == "in_app_shown"


def record_results(results: list[dict]) -> dict:
    settled_ids: list[str] = []
    statuses: list[dict] = []
    with _lock:
        for entry in results:
            if not isinstance(entry, dict):
                continue
            notice_id = entry.get("id")
            if not isinstance(notice_id, str) or not notice_id:
                continue
            state = _status.setdefault(
                notice_id, {"in_app_status": None, "native_status": None})
            in_app = entry.get("in_app")
            native = entry.get("native")
            if in_app == "in_app_shown":
                state["in_app_status"] = "in_app_shown"
            if isinstance(native, str) and (
                native == "native_shown" or native.startswith("native_failed:")
            ):
                state["native_status"] = native
            statuses.append({
                "id": notice_id,
                "in_app_status": state.get("in_app_status"),
                "native_status": state.get("native_status"),
            })
            if _settled(state):
                settled_ids.append(notice_id)
                _status.pop(notice_id, None)
    return {"settled_ids": settled_ids, "statuses": statuses}


def force_settle(ids: list[str]) -> None:
    with _lock:
        for notice_id in ids:
            _status.pop(notice_id, None)


def reset_for_tests() -> None:
    with _lock:
        _status.clear()
