"""Read-only, venture-scoped booking receipts from your connected accounts."""
from datetime import datetime
import json
import logging

from sqlalchemy import or_, select

from app.db.models import ContentOAuthState, GtmContact, GtmEmailEvent, GtmTouch, Person, Venture, Workspace
from core.clock import as_utc
from core.copy import strip_emdash

log = logging.getLogger(__name__)
PROVIDERS = {"cal": "Cal.com", "calendly": "Calendly", "google_calendar": "Google Calendar"}
READ_TOOLS = {
    "cal": {"CAL_GET_BOOKINGS"},
    "calendly": {"CALENDLY_GET_CURRENT_USER", "CALENDLY_LIST_EVENTS", "CALENDLY_LIST_EVENT_INVITEES"},
    "google_calendar": {"GOOGLECALENDAR_LIST_CALENDARS", "GOOGLECALENDAR_FIND_EVENT"},
}


def capture_gmail_reply_headers(venture_id, message_id, headers, *, email_account_id, reply_message_id):
    from app.connections.mailboxes import list_read_mailboxes, MailboxUnavailable
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    kept = {key.casefold(): value for key, value in headers.items() if key.casefold() == "content-type"}
    if not kept or "text/calendar" not in str(kept).casefold():
        return
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None or not any(ref["mailbox_id"] == email_account_id
                for ref in list_read_mailboxes(session, venture.workspace_id)):
            raise MailboxUnavailable("mailbox_unavailable")
        touch = session.scalar(select(GtmTouch).where(GtmTouch.workspace_id == venture.workspace_id,
            GtmTouch.venture_id == venture_id, GtmTouch.direction == "in", GtmTouch.message_id == reply_message_id))
        if touch:
            key = "booking-headers:" + touch.id
            if session.scalar(select(GtmEmailEvent.id).where(GtmEmailEvent.idempotency_key == key)):
                return
            session.add(GtmEmailEvent(id=new_id("gtmee"), workspace_id=touch.workspace_id, contact_id=touch.contact_id,
                email_account_id=email_account_id, event_type="booking_reply_headers", provider_message_id=message_id,
                idempotency_key=key, metadata_json=json.dumps({"reply_id": touch.id, "reply_headers": kept,
                    "subject": headers.get("subject", "")}), created_at=utcnow()))
            session.commit()


def _call(account, slug, arguments):
    from app.credits import metered
    from app.content import composio_social
    from app.gtm.supymem import composio_gmail
    from app.tasks import usage

    if slug not in READ_TOOLS[account["platform"]]:
        raise ValueError("booking_tool_not_read_only")
    from app.core.local_mode import is_local
    if is_local():
        from app.gtm.local_mail import enabled
        if not enabled("connected_apps"):
            raise ValueError("Open Find customers and mark your recorded outreach as booked, or enable Connected apps in Keys and connections and connect your calendar on Connections.")
    usage.before_tool()
    with metered.charge_sync(workspace_id=account["workspace_id"], purpose="connections.bookings",
            provider="composio", estimate_micro=0, payer="founder") as meter:
        if is_local():
            version = composio_social._toolkit_version(account["platform"])
            body = {"user_id": account["provider_user_id"], "connected_account_id": account["connected_account_id"],
                "arguments": arguments, "version": version}
            if version == "latest":
                body["dangerously_skip_version_check"] = True
            reply = composio_social._request("POST", f"/tools/execute/{slug}", json_body=body, timeout=60)
        else:
            reply = composio_gmail.execute_tool(slug, user_id=account["provider_user_id"],
                connected_account_id=account["connected_account_id"], arguments=arguments,
                toolkit_version=composio_social._toolkit_version(account["platform"]))
        meter.record_micro(0)
    if not isinstance(reply, dict) or reply.get("successful") is False:
        raise ValueError("booking_read_failed")
    data = reply.get("data", reply)
    while isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
        data = data["data"]
    if not isinstance(data, (dict, list)):
        raise ValueError("booking_read_invalid")
    return data


def _pages(account, slug, arguments, key):
    token, seen = None, set()
    # shortcut: at most 1000 rows per list, increase this bound if larger calendars need checking.
    for page in range(10):
        params = dict(arguments)
        if account["platform"] == "cal":
            params.update(take=100, skip=page * 100)
        elif token:
            params["pageToken" if account["platform"] == "google_calendar" else "page_token"] = token
        data = _call(account, slug, params)
        rows = data if isinstance(data, list) else data.get(key)
        if rows is None and account["platform"] == "google_calendar":
            rows = data.get("calendar_data" if slug == "GOOGLECALENDAR_LIST_CALENDARS" else "event_data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError("booking_list_invalid")
        yield from rows
        if account["platform"] == "cal":
            more = data.get("hasMore") if isinstance(data, dict) else None
            if more is False or len(rows) < 100 and more is not True:
                return
        else:
            pagination = data.get("pagination") or {}
            token = data.get("nextPageToken") or pagination.get("next_page_token")
            if not token:
                return
            if token in seen:
                raise ValueError("booking_pagination_repeated")
            seen.add(token)
    raise ValueError("booking_list_incomplete")


def _time(value):
    if isinstance(value, dict):
        value = value.get("dateTime")
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return as_utc(parsed) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _events(account, start, end):
    platform = account["platform"]
    if platform == "google_calendar":
        calendars = list(_pages(account, "GOOGLECALENDAR_LIST_CALENDARS", {"max_results": 100}, "items"))
        if any(not isinstance(calendar.get("id"), str) or not calendar["id"] for calendar in calendars):
            raise ValueError("booking_calendar_missing")
        rows = (row for calendar in calendars if not calendar.get("deleted") for row in _pages(account,
            "GOOGLECALENDAR_FIND_EVENT", {"calendar_id": calendar["id"], "timeMin": start.isoformat(),
            "timeMax": end.isoformat(), "max_results": 100, "single_events": True}, "items"))
    elif platform == "calendly":
        profile = _call(account, "CALENDLY_GET_CURRENT_USER", {})
        user = (profile.get("resource") or {}).get("uri")
        if not isinstance(user, str) or not user:
            raise ValueError("booking_user_missing")
        rows = _pages(account, "CALENDLY_LIST_EVENTS", {"user": user, "status": "active",
            "min_start_time": start.isoformat(), "max_start_time": end.isoformat(), "count": 100}, "collection")
    else:
        rows = (row for status in ("upcoming", "past") for row in _pages(account, "CAL_GET_BOOKINGS",
            {"status": status, "afterStart": start.isoformat(), "beforeStart": end.isoformat()}, "bookings"))
    for row in rows:
        if row.get("cancelled") or row.get("canceled") or str(row.get("status", "")).lower() not in {
                "", "confirmed", "accepted", "active", "upcoming", "past"}:
            continue
        starts = _time(row.get("start") or row.get("startTime") or row.get("start_time"))
        ends = _time(row.get("end") or row.get("endTime") or row.get("end_time"))
        if not starts or not ends or not start <= starts < end or ends <= starts:
            continue
        if platform == "calendly":
            uri = row.get("uri")
            if not isinstance(uri, str) or not uri:
                raise ValueError("booking_event_missing")
            attendees = list(_pages(account, "CALENDLY_LIST_EVENT_INVITEES",
                {"event": uri, "status": "active", "count": 100}, "collection"))
        else:
            attendees = row.get("attendees") or []
        if not isinstance(attendees, list):
            raise ValueError("booking_attendees_invalid")
        emails = set()
        for attendee in attendees:
            if not isinstance(attendee, dict) or attendee.get("self"):
                continue
            if str(attendee.get("responseStatus", "accepted")).lower() != "accepted":
                continue
            if str(attendee.get("status", "active")).lower() not in {"active", "accepted"}:
                continue
            email = attendee.get("email")
            if isinstance(email, str) and email.strip():
                emails.add(email.strip().lower())
        title = row.get("summary") or row.get("title") or row.get("name") or "Your customer call"
        yield {"title": strip_emdash(title[:200]) if isinstance(title, str) else "Your customer call",
            "attendees": sorted(emails), "starts_at": starts.isoformat(), "ends_at": ends.isoformat()}


def read_bookings(session, venture_id, start, end, *, feature_flag, person_id=None):
    from app.credits.policy import get_policy
    from app.content.oauth.callbacks import _verified_connection
    from app.connections import toolkits

    from app.core.local_mode import is_local
    if is_local():
        from app.gtm.local_mail import enabled
        if not enabled("connected_apps"):
            return {"connected": False, "available": False, "bookings": [],
                "next_step": "Open Find customers and mark the call as booked on your recorded outreach, or enable Connected apps in Keys and connections and connect your calendar on Connections."}
    else:
        flags = get_policy("feature_flags")
        if (feature_flag not in {"experiments_autopilot_v1", "goals_v1"}
            or not isinstance(flags, dict) or flags.get(feature_flag) is not True):
            return None
    start, end = as_utc(start), as_utc(end)
    if start >= end:
        raise ValueError("booking_window_invalid")
    venture = session.get(Venture, venture_id)
    workspace = session.get(Workspace, venture.workspace_id) if venture else None
    person = session.get(Person, person_id or workspace.owner_id) if workspace else None
    if not workspace or not person or person.id != workspace.owner_id:
        raise ValueError("booking_owner_missing")
    receipts = session.scalars(select(ContentOAuthState).where(
        ContentOAuthState.workspace_id == workspace.id, ContentOAuthState.platform.in_(PROVIDERS),
        ContentOAuthState.auth_method == "composio", ContentOAuthState.actor_subject == person.id,
        ContentOAuthState.provider_user_id.in_([person.id, f"content:{workspace.id}"]),
        ContentOAuthState.connected_account_id.is_not(None), ContentOAuthState.auth_config_id.is_not(None),
        ContentOAuthState.consumed_at.is_not(None),
    ).order_by(ContentOAuthState.created_at.desc(), ContentOAuthState.id.desc())).all()
    latest = {}
    for receipt in receipts:
        latest.setdefault(receipt.connected_account_id, receipt)
    accounts = [{column.name: getattr(receipt, column.name) for column in ContentOAuthState.__table__.columns}
        for receipt in latest.values() if receipt.outcome == "connected"]
    if not accounts:
        preferred = next((row.platform for row in latest.values()), None)
        preferred = preferred or next((slug for slug in ("cal", "calendly") if toolkits.auth_config_id(slug)), "google_calendar")
        return {"connected": False, "available": False, "bookings": [], "connection_action": {
            "label": "Connect " + PROVIDERS[preferred], "action": "connections.connect",
            "payload": {"query": ("Reconnect " if latest else "Connect ") + PROVIDERS[preferred]}}}
    related = select(GtmTouch.id).where(GtmTouch.contact_id == GtmContact.id,
        GtmTouch.workspace_id == workspace.id, GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0).exists()
    contacts = session.scalars(select(GtmContact).where(GtmContact.workspace_id == workspace.id,
        GtmContact.email.is_not(None), or_(GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True), related))
        .order_by(GtmContact.id)).all()
    by_email = {}
    for contact in contacts:
        email = contact.email.strip().lower()
        if email and email != (person.email or "").strip().lower():
            by_email.setdefault(email, contact.id)
    bookings, seen = [], set()
    try:
        for account in accounts:
            _verified_connection(account)
            for event in _events(account, start, end):
                for email in event["attendees"]:
                    contact_id = by_email.get(email)
                    key = (contact_id, event["starts_at"], event["ends_at"])
                    if contact_id and key not in seen:
                        seen.add(key)
                        bookings.append({**event, "attendees": [email], "contact_id": contact_id})
    except Exception as exc:
        log.warning("booking_read_unavailable", extra={"workspace_id": workspace.id, "error_type": type(exc).__name__})
        from app.content.oauth.attempts import OAuthError
        from app.connections.composio_migration import missing_connection
        from app.gtm.supymem.composio_gmail import ReconnectRequired
        signal = {"connected": True, "available": False, "bookings": []}
        if is_local():
            signal["next_step"] = "Open Connections and reconnect your calendar, then check booked calls again. You can also mark your recorded outreach as booked in Find customers."
        if isinstance(exc, (OAuthError, ReconnectRequired)) or missing_connection(exc):
            signal["connection_action"] = {"label": "Connect " + PROVIDERS[account["platform"]],
                "action": "connections.connect", "payload": {"query": "Reconnect " + PROVIDERS[account["platform"]]}}
        return signal
    return {"connected": True, "available": True, "bookings": bookings}
