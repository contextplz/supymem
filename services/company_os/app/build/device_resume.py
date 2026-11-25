"""Durable human handoff signals bound to one owned, current execution and pause.

A queued or accepted signal grants no effect, extends no deadline and proves no
completed task. The native worker must reobserve the human gate before any I/O.
"""
import json
from sqlalchemy import select
from app.admin.authn import CallerContext
from app.build import device_invocations as calls, device_orders as orders
from app.db.models import VentureDocument
from app.db.supymem_models import NeedsYouRequest
from app.needs_you.policy import ALIASES, HUMAN_ONLY, BLOCKING, TITLES
from core.ids import new_id


class ResumeUnavailable(ValueError):
    pass


def _bound(body, pause):
    return (isinstance(pause, dict) and pause.get("execution_id") == body.get("execution_id")
        and pause.get("review_digest") == body.get("review_digest")
        and pause.get("device_id") == body.get("device_id"))


def register(venture_id, caller, order_id, *, pause_token, pause_kind):
    """Native-only pause registration; newer episodes invalidate older Continue cards."""
    pause_kind = (pause_kind or "").strip().lower()
    reason = ALIASES.get(pause_kind, pause_kind)
    if reason not in HUMAN_ONLY:
        raise ValueError("device_pause_kind_invalid")
    from app.needs_you import service
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        old = body.get("human_pause")
        if _bound(body, old) and old.get("pause_token") == pause_token:
            return {"request_id": old["request_id"], "pause_token": pause_token, "state": old["state"]}
        history = body.setdefault("human_pause_tokens", [])
        if pause_token in history or len(history) >= 64:
            raise ValueError("device_pause_superseded")
        prior = session.scalar(select(NeedsYouRequest).where(
            NeedsYouRequest.open_key == f"{venture_id}|device:{order_id}").with_for_update())
        now = orders.utcnow()
        # The engine's original report may have just created this same pause's
        # card. Reuse it instead of sending a second human email/notice. An old
        # episode's still-open card must instead expire before the new one exists.
        reuse = prior is not None and (not old or prior.id != old.get("request_id"))
        if prior and not reuse:
            prior.status, prior.open_key = "expired", None
        session.flush()
        if reuse:
            request = prior
            request.reason_code, request.title = reason, TITLES[reason]
            request.how_to_resume = "Finish the human step, then Continue. Supymem will recheck the same active Mac task."
        else:
            request = NeedsYouRequest(id=new_id("nyr"), workspace_id=body["workspace_id"], venture_id=venture_id,
                reason_code=reason, title=TITLES[reason], what_we_need="Complete this human step in the owned task window.",
                how_to_resume="Finish the human step, then Continue. Supymem will recheck the same active Mac task.",
                blocking_ref=f"device:{order_id}", severity="blocking" if reason in BLOCKING else "normal",
                status="open", open_key=f"{venture_id}|device:{order_id}", created_at=now, reminders_sent=0)
            session.add(request)
        history.append(pause_token)
        body["human_pause"] = {"pause_token": pause_token, "request_id": request.id,
            "execution_id": body["execution_id"], "review_digest": body["review_digest"],
            "device_id": body["device_id"], "state": "waiting", "created_at": now.isoformat()}
        row.provenance_json, row.updated_at = json.dumps(body), now
        session.commit()
        session.refresh(request)
        notice = service._row(request)
    if not reuse:
        service._deliver(notice, True)
    return {"request_id": notice["id"], "pause_token": pause_token, "state": "waiting"}


def lock_for_resolution(session, request, actor):
    """Match invocation lock order before locking the Needs-you row itself."""
    order_id = request.blocking_ref.split(":", 1)[1]
    saved = session.get(VentureDocument, order_id)
    if saved is None or saved.kind != orders.KIND or saved.venture_id != request.venture_id:
        raise ResumeUnavailable("device_resume_unavailable")
    data = json.loads(saved.provenance_json or "{}")
    if not actor or data.get("actor_id") != actor:
        raise ResumeUnavailable("device_resume_unavailable")
    caller = CallerContext(actor, "", "user", "/needs-you", "POST", 0, "internal-resolution",
        device_id=data.get("device_id"))
    try:
        row, body, account = calls._locked(session, request.venture_id, caller, order_id)
        calls._check_executing(session, request.venture_id, body, account, orders.utcnow())
    except (ValueError, TypeError, KeyError, PermissionError) as exc:
        raise ResumeUnavailable("device_resume_unavailable") from exc
    pause = body.get("human_pause")
    if not _bound(body, pause) or pause.get("request_id") != request.id or pause.get("state") not in {"waiting", "queued", "signal_accepted", "pause_loop_exited"}:
        raise ResumeUnavailable("device_resume_unavailable")
    return row, body


def queue(session, row, body, request, actor):
    pause = body["human_pause"]
    pause.update(state="queued", requested_at=orders.utcnow().isoformat(), requested_by=actor)
    row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
    return "queued"


def pending(venture_id, caller, order_id):
    with orders.db()() as session:
        _, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        pause = body.get("human_pause")
        if not _bound(body, pause) or pause.get("state") != "queued":
            return {"signal": None}
        return {"signal": {key: pause[key] for key in ("request_id", "pause_token", "execution_id", "review_digest", "device_id")}}


def acknowledge(venture_id, caller, order_id, *, pause_token, request_id, execution_id):
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        pause = body.get("human_pause")
        if (not _bound(body, pause) or pause.get("pause_token") != pause_token
                or pause.get("request_id") != request_id or pause.get("execution_id") != execution_id
                or pause.get("state") not in {"queued", "signal_accepted"}):
            raise ValueError("device_pause_superseded")
        pause.update(state="signal_accepted", accepted_at=pause.get("accepted_at") or orders.utcnow().isoformat())
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return {"state": "signal_accepted", "request_id": request_id}


def clear(venture_id, caller, order_id, *, pause_token):
    """Observed local exit clears the card; it does not assert task completion."""
    with orders.db()() as session:
        row, body, account = calls._locked(session, venture_id, caller, order_id)
        calls._check_executing(session, venture_id, body, account, orders.utcnow())
        pause = body.get("human_pause")
        if not _bound(body, pause) or pause.get("pause_token") != pause_token:
            raise ValueError("device_pause_superseded")
        request = session.get(NeedsYouRequest, pause["request_id"], with_for_update=True)
        if request and request.status == "open":
            request.status, request.open_key = "resolved", None
            request.resolved_at, request.resolved_by = orders.utcnow(), "device_observed_pause_exit"
        pause.update(state="pause_loop_exited", cleared_at=orders.utcnow().isoformat())
        row.provenance_json, row.updated_at = json.dumps(body), orders.utcnow()
        session.commit()
        return {"state": "pause_loop_exited"}
