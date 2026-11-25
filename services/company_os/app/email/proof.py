"""Release-agent proof on the configured app database. Never run against beta users.

queue creates an outbox/job through a real persisted founder milestone trigger;
check reads worker/provider and signed callback evidence; unsubscribe exercises
GET + one-click POST then restores the founder's previous preference.

Run with python -m app.email.proof in the release agent's normal core/worker
environment (with existing
production configuration). This script neither deploys nor retrieves secrets.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from html import escape
from urllib.parse import parse_qs, urlsplit

ALLOWED_RECIPIENT = os.environ.get("SUPYMEM_INTERNAL_EMAIL", "").strip().lower()


def founder(session):
    from sqlalchemy import func, select
    from app.db.models import Person
    if not ALLOWED_RECIPIENT:
        raise ValueError("Set SUPYMEM_INTERNAL_EMAIL to your owned proof mailbox and retry.")
    people = session.scalars(select(Person).where(func.lower(Person.email) == ALLOWED_RECIPIENT).limit(2)).all()
    if len(people) != 1:
        raise ValueError("email_proof_founder_identity_ambiguous")
    return people[0]


def owned_message(session, message_id):
    from app.db.supymem_models import EmailMessage
    person = founder(session)
    message = session.get(EmailMessage, message_id)
    if (message is None or message.person_id != person.id
            or message.to_email.strip().lower() != ALLOWED_RECIPIENT):
        raise ValueError("email_proof_message_not_founder_owned")
    if message.category == "transactional":
        raise ValueError("email_proof_requires_marketing_unsubscribe")
    return message


def queue():
    from sqlalchemy import select
    from app.db.models import Venture, Workspace
    from app.email import service, triggers
    from app.state import db
    if os.environ.get("EMAIL_PROVIDER", "log").strip().lower() != "resend":
        raise ValueError("email_proof_requires_existing_resend_configuration")
    queued = []
    with db()() as session:
        person = founder(session)
        ventures = session.scalars(select(Venture).join(Workspace, Workspace.id == Venture.workspace_id).where(
            Workspace.owner_id == person.id, Workspace.archived_at.is_(None),
            Venture.archived_at.is_(None), Venture.stage != "purged")
            .order_by(Venture.created_at.desc()).limit(100)).all()
        def go(owner, template, context, ref):
            if owner != person.id:
                raise ValueError("email_proof_non_founder_trigger_refused")
            mid = service.send_email(owner, template, context, ref=ref)
            if mid:
                queued.append(mid)
        # Exactly the production scheduler rule, restricted to the founder.
        # It requires real, unemailed progress settled for two hours; no invented proof event.
        for venture in ventures:
            triggers._progress(session, venture, person.id, service.utcnow(), go)
            if queued:
                break
    if not queued:
        raise ValueError("email_proof_requires_eligible_unemailed_progress_and_activity_caps_preferences_rollout")
    with db()() as session:
        message = owned_message(session, queued[0])
        if message.status != "queued":
            raise ValueError("email_proof_progress_not_queued")
        return {"message_id": message.id, "outbox_status": message.status,
                "proof_path": "hourly_progress_rule", "provider_called_by_script": False}


def check(message_id):
    from app.db.models import Job
    from app.email.service import _delivery_receipts, utcnow
    from app.state import db
    from datetime import timedelta
    from core.clock import as_utc
    with db()() as session:
        message = owned_message(session, message_id)
        context = json.loads(message.context_json)
        if not isinstance(context, dict):
            raise ValueError("email_proof_context_invalid")
        job_id, attempt = context.get("_email_sent_job_id"), context.get("_email_sent_job_attempt")
        job = session.get(Job, job_id) if isinstance(job_id, str) else None
        try:
            worker_succeeded = (job is not None and job.kind == "email.send" and job.status == "succeeded"
                                and type(attempt) is int and 0 < attempt <= job.attempts
                                and json.loads(job.payload_json).get("message_id") == message.id
                                and message.sent_at is not None and job.finished_at is not None
                                and as_utc(job.finished_at) >= as_utc(message.sent_at))
        except (TypeError, ValueError, AttributeError):
            worker_succeeded = False
        evidence = {kind for kind, _, mid in _delivery_receipts(session, utcnow() - timedelta(days=7)) if mid == message.id}
        return {"message_id": message.id, "outbox_status": message.status,
                "provider_accepted": bool(message.provider_id and message.sent_at),
                "worker_succeeded": worker_succeeded,
                "signed_delivered_receipt": "delivered" in evidence,
                "signed_bounced_receipt": "bounced" in evidence,
                "signed_complained_receipt": "complained" in evidence}


def _saved_link(url, person_id, category, *, preferences=False):
    from app.email import service
    parsed = urlsplit(url)
    query = parse_qs(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or parsed.netloc != "gateway.supymem.com"
            or parsed.path != "/public/email/unsubscribe" or parsed.fragment
            or set(query) != ({"t", "view"} if preferences else {"t"})
            or any(len(values) != 1 for values in query.values())
            or (preferences and query["view"] != ["preferences"])
            or service.read_token(query.get("t", [""])[0]) != (person_id, category)):
        raise ValueError("email_proof_saved_link_invalid")
    return url


def unsubscribe(message_id):
    import httpx
    from app.email import service
    from app.state import db
    with db()() as session:
        message = owned_message(session, message_id)
        person_id, category = message.person_id, message.category
        payload = json.loads(message.context_json).get("_email_delivery_payload")
        accepted = bool(message.provider_id and message.sent_at)
    if not accepted or not isinstance(payload, dict):
        raise ValueError("email_proof_sent_payload_missing")
    headers, plain, html = payload.get("headers"), payload.get("text"), payload.get("html")
    if not isinstance(headers, dict) or not isinstance(plain, str) or not isinstance(html, str):
        raise ValueError("email_proof_sent_payload_missing")
    header = headers.get("List-Unsubscribe", "")
    saved = re.fullmatch(r"<([^<>\s]+)>", header) if isinstance(header, str) else None
    prefs = [line.removeprefix("Email settings: ") for line in plain.splitlines()
             if line.startswith("Email settings: ")]
    if saved is None or len(prefs) != 1 or headers.get("List-Unsubscribe-Post") != "List-Unsubscribe=One-Click":
        raise ValueError("email_proof_sent_links_ambiguous")
    url = _saved_link(saved[1], person_id, category)
    prefs_url = _saved_link(prefs[0], person_id, "all", preferences=True)
    if (f"Unsubscribe: {url}" not in plain.splitlines()
            or html.count(f'href="{escape(url)}"') != 1
            or html.count(f'href="{escape(prefs_url)}"') != 1):
        raise ValueError("email_proof_sent_footer_mismatch")
    before = service.get_prefs(person_id)[category]
    try:
        with httpx.Client(timeout=20, follow_redirects=False) as client:
            get = client.get(url)
            if get.status_code != 200 or service.get_prefs(person_id)[category] != before:
                raise ValueError("email_proof_get_changed_preference_or_failed")
            post = client.post(url, content="List-Unsubscribe=One-Click", headers={
                "Content-Type": "application/x-www-form-urlencoded"})
            if post.status_code != 200 or service.get_prefs(person_id)[category]:
                raise ValueError("email_proof_one_click_unsubscribe_failed")
            preferences_page = client.get(prefs_url)
            if preferences_page.status_code != 200 or "digests" not in preferences_page.text:
                raise ValueError("email_proof_preferences_page_failed")
    finally:
        service.set_prefs(person_id, {category: before})
    return {"message_id": message_id, "get_did_not_unsubscribe": True,
            "one_click_unsubscribe_verified": True, "preferences_page_verified": True,
            "original_preference_restored": service.get_prefs(person_id)[category] == before}


def _succeeded(result):
    return (result["provider_accepted"] and result["worker_succeeded"] and result["signed_delivered_receipt"]
            and not result["signed_bounced_receipt"] and not result["signed_complained_receipt"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("queue")
    checked = actions.add_parser("check")
    checked.add_argument("--message-id", required=True)
    checked.add_argument("--wait-seconds", type=int, default=0)
    unsub = actions.add_parser("unsubscribe")
    unsub.add_argument("--message-id", required=True)
    args = parser.parse_args()
    try:
        if args.action == "queue":
            result = queue()
        elif args.action == "unsubscribe":
            result = unsubscribe(args.message_id)
        else:
            if not 0 <= args.wait_seconds <= 600:
                raise ValueError("email_proof_wait_invalid")
            deadline = time.monotonic() + args.wait_seconds
            while True:
                result = check(args.message_id)
                if (_succeeded(result) or result["signed_bounced_receipt"]
                        or result["signed_complained_receipt"] or time.monotonic() >= deadline):
                    break
                time.sleep(min(5, max(0, deadline - time.monotonic())))
        print(json.dumps(result, sort_keys=True))
        if args.action == "check" and not _succeeded(result):
            raise SystemExit(1)
    except Exception:
        # Driver/provider/HTTP exceptions may contain identities, tokens or database credentials.
        print(json.dumps({"error": "email_proof_failed", "check": "activity_caps_preferences_rollout_and_configuration"}))
        raise SystemExit(2) from None


if __name__ == "__main__":
    main()
