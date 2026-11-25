"""Founder-approved channel links and consented waitlist referrals."""
from __future__ import annotations

import json
import hashlib
import math
import re
import secrets
from dataclasses import replace
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import or_, select

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.core.fsm import advance
from app.core.local_mode import is_local
from app.db.models import Approval, GtmContact, Venture, VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import AppEvent, Experiment, VentureWaitlistEntry
from app.experiments import activity, ledger, page_tests
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import founder_copy


def tagged_link(url, **tags):
    parts = urlsplit(url)
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or any(ord(char) < 32 for char in url)):
        raise ValueError("experiment_public_link_invalid")
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in tags]
    return urlunsplit(parts._replace(query=urlencode(query + list(tags.items()))))


def referral_code(entry):
    props = json.loads(entry.props or "{}")
    if not props.get("ref_code"):
        props["ref_code"] = secrets.token_urlsafe(24)
        entry.props = json.dumps(props)
    return props["ref_code"]


def referred_by(session, venture_id, workspace_id, email, code):
    from app.conversion.waitlist import VERSION
    if not code:
        return {}
    # ponytail: scan this venture's JSON-text props, add an indexed code column if waitlist lookup volume warrants a migration.
    entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
        VentureWaitlistEntry.workspace_id == workspace_id, VentureWaitlistEntry.is_test.is_(False),
        VentureWaitlistEntry.consent_version == VERSION))
    for entry in entries:
        if entry.consent_text.strip() and json.loads(entry.props or "{}").get("ref_code") == code:
            return {"referred_by": code} if entry.email.lower() != email else {}
    return {}


def _row(session, venture_id, experiment_id):
    row, context, venture = page_tests._row(session, venture_id, experiment_id, scan="channel_test")
    if context["spec"]["direction"] != ledger.current_direction(session, venture_id):
        raise ValueError("experiment_direction_changed")
    return row, context, venture


def _spec(venture_id, payload):
    from app.approvals.external import outbound_quote
    from app.experiments.autonomous import _fingerprint, _snapshot, _thresholds
    if not isinstance(payload, dict) or set(payload) - {"kind", "communities"}:
        raise ValueError("channel_test_input_invalid")
    kind = payload.get("kind")
    if kind not in {"channels", "referral"}:
        raise ValueError("channel_test_kind_invalid")
    snapshot = _snapshot(venture_id)
    site = (snapshot or {}).get("waitlist", {}).get("published_site") or {}
    if not snapshot or not snapshot.get("app_id") or not snapshot["waitlist"].get("enabled") or not site.get("url"):
        raise ValueError("channel_test_published_waitlist_required")
    tagged_link(site["url"])
    communities = payload.get("communities", snapshot["journey"].get("channels") or [])
    if not isinstance(communities, list) or len(communities) > 8 or (kind == "referral" and payload.get("communities")):
        raise ValueError("channel_test_communities_invalid")
    variants = [{"key": "email", "label": "Email", "medium": "email"}]
    for community in communities if kind == "channels" else []:
        if (not isinstance(community, dict) or not isinstance(community.get("url"), str)
                or not isinstance(community.get("title", community.get("label", "Community")), str)):
            raise ValueError("channel_test_communities_invalid")
        tagged_link(community["url"])
        key = "community_" + _fingerprint(community["url"].rstrip("/"))[:16]
        if any(item["key"] == key for item in variants):
            continue
        variants.append({"key": key, "label": founder_copy(community.get("title") or community.get("label") or "Community")[:120],
            "medium": "community", "url": community["url"], "participation": str(community.get("participation") or "")[:500]})
    variants.extend([{"key": "x", "label": "X", "medium": "social"}, {"key": "other", "label": "Other", "medium": "other"}])
    with db()() as session:
        venture = session.get(Venture, venture_id)
        name = venture.name
    hypothesis = ("One channel brings at least 50% of your first 10 signups, with a unique lead."
        if kind == "channels" else "At least 10% of your consented signups refer another person within 14 days.")
    minimum, threshold = (10, .5) if kind == "channels" else (30, .1)
    spec = {key: snapshot[key] for key in ("venture_id", "workspace_id", "direction", "app_id")}
    spec.update(scan="channel_test", channel_kind=kind, catalog_key="E25_channel_bakeoff" if kind == "channels" else "E27_referral_ask",
        title="Channel bake-off" if kind == "channels" else "Referral ask", source="catalog", question="Q5", product_name=name,
        site_url=site["url"], icp=snapshot["journey"].get("icp") or {}, hypothesis=hypothesis, assumption=hypothesis,
        variable="channel" if kind == "channels" else "referral", variants=variants if kind == "channels" else [],
        runner="observe", runner_params={"mail_quote": outbound_quote("gtm_email_send", "composio_gmail")},
        metric="channel_signup_share" if kind == "channels" else "referrer_share", **_thresholds(threshold, threshold, minimum),
        min_sample=minimum, sample_unit="signups" if kind == "channels" else "consented signups", max_duration_days=14,
        ceiling_micro=0, founder_minutes=15, approval="each_send", state="proposed", result=None,
        compliance=["recorded_consent", "source_disclosure", "opt_out", "sender_address", "suppression", "daily_cap", "founder_posts"],
        requires=["published_site", "waitlist"])
    spec["input_fingerprint"] = _fingerprint({key: spec[key] for key in ("direction", "icp", "site_url", "variants", "runner_params")})
    spec["method"] = (f"Minimum sample: {minimum} {spec['sample_unit']}. Support: {spec['metric']} >= {threshold}; "
        f"refute: < {threshold}, only after 14 days. Small samples are directional and inconclusive. "
        "I will prepare your tagged links and channel drafts, then compare recorded signups. Sending and posting keep their approvals. "
        "Only contact consented entries for referral asks; never resend an uncertain delivery.")
    spec["fingerprint"] = _fingerprint(spec)
    spec["id"] = "exp_" + spec["fingerprint"][:24]
    return spec


def prepare(venture_id, payload):
    try:
        spec = _spec(venture_id, payload)
    except ValueError as error:
        if str(error) != "channel_test_published_waitlist_required":
            raise
        from app.pagebuilder.api import enabled
        if not enabled():
            from app.chat.experiments import page_unavailable_reply
            return page_unavailable_reply()
        reply = page_tests._reply(
            "You need a published landing page with an enabled waitlist before comparing channels. "
            "Next: Prepare your page and waitlist, then review and approve publishing it. Your channel test has not started.",
            [{"label": "Prepare your page and waitlist", "action": "pagebuilder.build",
              "payload": {"prepare": True}}], "needs_you")
        reply["action_started"] = False
        return reply
    rate = spec["runner_params"]["mail_quote"]["credit_estimate_micro"]
    card = ConfirmationCard.for_action(get_action("experiment.channel.configure"),
        interpretation=f"I can run this experiment for you. I will prepare your {spec['title'].lower()}, tagged links, eligible email drafts and signup readout.",
        consequences=[f"Hypothesis: {spec['hypothesis']}", spec["method"],
            "Nothing is sent, published or posted by this step.",
            ("Prepare up to 30 drafts. Review and approve the exact batch; your mailbox cap still applies."
             if type(rate) is int and rate > 0 else "Email sending stays blocked until your configured send price is available."),
            "You post community drafts by hand with moderator permission. X uses your existing, separately approved posting path.",
            "Missing recipient jurisdiction, opt-outs and suppression still block email sending."],
        resulting_state={**payload, "fingerprint": spec["fingerprint"]})
    return page_tests._reply(card.interpretation, [replace(card, confirm_label="Run it for me").as_action()], "confirmation")


def available(snapshot):
    if not snapshot:
        return []
    with db()() as session:
        if ledger.quality_enabled():
            for row in session.scalars(select(Experiment).where(Experiment.venture_id == snapshot["venture_id"],
                    Experiment.workspace_id == snapshot["workspace_id"], Experiment.kind == ledger.KIND)):
                context = json.loads(row.context or "{}")
                if context.get("channel_test") and context["spec"].get("direction") == snapshot["direction"]:
                    page_tests.refresh_launch(session, row, context)
            session.commit()
        return [json.loads(row.context)["spec"] for row in session.scalars(select(Experiment).where(
            Experiment.venture_id == snapshot["venture_id"], Experiment.workspace_id == snapshot["workspace_id"],
            Experiment.kind == ledger.KIND)) if json.loads(row.context or "{}").get("channel_test")
            and json.loads(row.context)["spec"].get("direction") == snapshot["direction"]]


def configure(venture_id, payload):
    spec = _spec(venture_id, {key: value for key, value in payload.items() if key != "fingerprint"})
    if payload.get("fingerprint") != spec["fingerprint"]:
        raise ValueError("channel_test_confirmation_changed")
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        rows = session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
            Experiment.workspace_id == spec["workspace_id"], Experiment.kind == ledger.KIND)).all()
        if any(row.id == spec["id"] for row in rows):
            context = json.loads(next(row.context for row in rows if row.id == spec["id"]))
            session.commit()
            if spec["channel_kind"] == "referral":
                page_tests.draft_mail(venture_id, spec["id"], referral=True)
            elif "mail_batch" not in context:
                _tag_email_drafts(venture_id, spec)
            return _prepared_reply(venture_id, spec["id"])
        from app.experiments.engine import blocked_reason
        reason = blocked_reason(spec, [{"venture_id": row.venture_id,
            "state": "running" if row.state == "proposed" and json.loads(row.context or "{}").get("claimed") else row.state,
            "ended_at": row.ended_at, "spec": json.loads(row.context or "{}").get("spec", {})} for row in rows], utcnow())
        if reason:
            raise ValueError(reason)
        context = {"runner": "system", "spec": spec, "channel_test": True, "prepared_at": utcnow().isoformat(), "effort": {}}
        if spec["channel_kind"] == "channels":
            context["links"] = {item["key"]: tagged_link(spec["site_url"], utm_source=item["key"], utm_medium=item["medium"],
                utm_campaign=spec["id"]) for item in spec["variants"]}
        else:
            serialize_writes(session, key="waitlist:" + venture_id)
            context["cohort"] = {entry.id: referral_code(entry) for entry in session.scalars(select(VentureWaitlistEntry).where(
                VentureWaitlistEntry.venture_id == venture_id, VentureWaitlistEntry.workspace_id == spec["workspace_id"]))
                if page_tests.eligible(session, venture_id, spec["workspace_id"], entry.id, entry.email)}
        row = Experiment(id=spec["id"], venture_id=venture_id, workspace_id=spec["workspace_id"], kind=ledger.KIND,
            hypothesis=spec["hypothesis"], action=spec["method"], state="proposed", context=json.dumps(context))
        session.add(row)
        if ledger.quality_enabled():
            context["launch_state"] = "ready to launch"
            page_tests.schedule_observation(session, row, context, utcnow() + timedelta(days=1))
            row.context = json.dumps(context)
        else:
            advance(session, row, "running", actor="founder", reason="channel_test_confirmed")
            row.started_at = utcnow()
        session.commit()
    if spec["channel_kind"] == "referral":
        page_tests.draft_mail(venture_id, spec["id"], referral=True)
    else:
        _tag_email_drafts(venture_id, spec)
    return _prepared_reply(venture_id, spec["id"])


def _tag_email_drafts(venture_id, spec):
    from app.autopilot import outreach
    from core.ids import new_id
    started_at = utcnow()
    batch, saved = new_id("cbatch"), []
    link = tagged_link(spec["site_url"], utm_source="email", utm_medium="email", utm_campaign=spec["id"])
    for draft in outreach.drafts(venture_id):
        if (len(saved) >= 30 or draft.get("state") != "draft" or draft.get("day", 1) != 1
                or draft.get("channel") != "email" or draft.get("experiment_id") or draft.get("review_state") == "approved"
                or draft.get("waitlist_entry_id") or draft.get("channel_experiment_id")):
            continue
        if spec.get("scan") == "page_test":
            variant = spec["variants"][len(saved) % len(spec["variants"])]
            url = spec["site_url"].rstrip("/") + f"/variant_{variant['key']}.html" if spec["page_kind"] == "headlines" else spec["site_url"]
            link = tagged_link(url, experiment_id=spec["id"], variant=variant["key"], utm_campaign=spec["id"])
        body = draft["body"]
        parts = urlsplit(spec["site_url"])
        body = re.sub(r"https?://[^\s<>]+", lambda match: link if
            (urlsplit(match[0]).hostname, urlsplit(match[0]).path.rstrip("/")) == (parts.hostname, parts.path.rstrip("/")) else match[0], body)
        if link not in body:
            body += f"\n\nYou can see what I'm building here: {link}"
        draft.update(body=body, channel_experiment_id=spec["id"], requires_batch_approval=True,
            requires_journey_review=True, review_state="draft", batch_id=batch)
        outreach._save_draft(venture_id, draft, draft["doc_id"])
        saved.append(draft["doc_id"])
    with db()() as session:
        row, context, _venture = page_tests._row(session, venture_id, spec["id"], scan=spec["scan"])
        context["email_drafts"] = saved
        context["mail_batch"] = {"batch_id": batch, "count": len(saved)}
        row.context = json.dumps(context)
        activity.record_safely(activity.run_context(context, venture_id=venture_id), action="draft", tool="email_drafts", evidence_level="drafted_outreach", started_at=started_at, ended_at=utcnow(), evidence_summary=str(len(saved)), session=session)
        session.commit()


def _prepared_reply(venture_id, experiment_id):
    from app.autopilot import outreach
    from app.gtm.playbooks import community_post
    from app.chat.actions import social_post_enabled
    with db()() as session:
        _experiment, context, _venture = _row(session, venture_id, experiment_id)
    spec = context["spec"]
    document_ids = set(context.get("email_drafts", [])) | {draft["doc_id"] for draft in context.get("mail_drafts", [])}
    waiting = [draft for draft in outreach.drafts(venture_id) if draft["doc_id"] in document_ids
        and draft.get("state") == "draft" and not draft.get("delivery_receipt")]
    batches = {draft["batch_id"] for draft in waiting}
    lines = [f"Your {spec['title'].lower()} is {'ready to launch' if ledger.quality_enabled() and not context.get('first_exposure_at') else 'prepared'}. {len(waiting)} email drafts remain unsent.", spec["method"]]
    actions = [{"label": "Read your results", "action": "experiment.channel.readout", "payload": {"experiment_id": experiment_id}}]
    if waiting:
        actions.append({"label": "Review your exact email batch", **({"href": f"/gtm?venture={venture_id}"}
            if spec["channel_kind"] == "channels" or len(batches) != 1 else {"action": "outreach.send", "payload": {"batch_id": next(iter(batches))}})})
    else:
        actions.append({"label": "Review eligible email drafts", "href": f"/gtm?venture={venture_id}"})
    if spec["channel_kind"] == "channels":
        for item in spec["variants"]:
            link = context["links"][item["key"]]
            lines.append(f"{item['label']} link: {link}")
            if item["medium"] == "community":
                lines.append(f"Draft for {item['url']}:\n{community_post(spec['icp'], participation=item['participation'])}\n"
                    + ("This community prohibits promotion. Do not post your link without explicit moderator permission."
                       if re.search(r"no (?:self[ -]?)?(?:promotion|advertising|pitch)|(?:promotion|advertising).*?(?:not allowed|prohibited)", item["participation"], re.I)
                       else f"Only if the rules and moderator permit a link: {link}"))
            if item["key"] == "x":
                post = founder_copy(f"I'm researching {spec['icp'].get('problem') or 'this problem'}. What have you tried?")
                post = post[:max(0, 279 - len(link))].rstrip() + " " + link
                lines.append(f"Your X draft:\n{post}")
                if social_post_enabled() and len(post) <= 280:
                    card = ConfirmationCard.for_action(get_action("social_post"), interpretation=f"Post this exact X draft for you: {post}",
                        consequences=["This exact text goes public only after you approve it and posting is connected."],
                        resulting_state={"content": post, "platforms": ["twitter"]})
                    actions.append(card.as_action())
                else:
                    lines.append("Manual fallback: copy your X draft and link, then post it yourself.")
    return page_tests._reply("\n\n".join(lines), actions)


def referral_mail(entry, spec):
    link = tagged_link(spec["site_url"], ref=referral_code(entry))
    return founder_copy(f"Your personal invitation for {spec['product_name']}"), founder_copy(
        f"You joined the early-access waitlist for {spec['product_name']}. "
        f"If someone you know has the same problem, you can share your personal invitation link with them:\n\n{link}")


def send_block(venture_id, draft, *, survey=False):
    from app.autopilot import outreach
    try:
        with db()() as session:
            row, context, _venture = (page_tests._row(session, venture_id, draft.get("experiment_id")) if survey else
                _row(session, venture_id, draft.get("experiment_id")))
            ready = ledger.quality_enabled() and row.state == "proposed" and context.get("launch_state") == "ready to launch"
            kind = context["spec"].get("page_kind" if survey else "channel_kind")
            if (kind != ("survey" if survey else "referral") or not (row.state == "running" or ready) or context.get("error")
                    or row.started_at and utcnow() >= as_utc(row.started_at) + timedelta(days=context["spec"]["max_duration_days"])):
                return "experiment_not_running"
            if survey and (not context.get("survey_published") or context["spec"]["direction"] != ledger.current_direction(session, venture_id)):
                return "experiment_not_running"
            saved = next((item for item in context.get("mail_drafts", []) if item["doc_id"] == draft.get("doc_id")), None)
            if not saved or any(saved.get(key) != draft.get(key) for key in (
                    "contact_id", "to", "waitlist_entry_id", "credit_ceiling_micro", "one_off")):
                return "experiment_mail_changed"
            document = session.get(VentureDocument, draft.get("doc_id"))
            stored = json.loads(document.provenance_json or "{}") if document else {}
            if (not document or document.venture_id != venture_id or document.kind != outreach.KIND or stored.get("delivery_receipt")
                    or stored.get("state") in {"sent", "delivery_unconfirmed"}):
                return "experiment_mail_already_attempted"
            if any(stored.get(key) != draft.get(key) for key in ("body", "subject", "batch_id", "experiment_id")):
                return "experiment_mail_changed"
            if survey:
                from app.conversion.waitlist import public_url
                prefix = public_url(venture_id) + f"/surveys/{row.id}/"
                token = re.search(re.escape(prefix) + r"([A-Za-z0-9_-]+)", draft["body"])
                if not token or hashlib.sha256(token[1].encode()).hexdigest() != saved.get("survey_token_hash"):
                    return "experiment_mail_changed"
            elif tagged_link(context["spec"]["site_url"], ref=context["cohort"][draft["waitlist_entry_id"]]) not in draft["body"]:
                return "experiment_mail_changed"
            contact = session.get(GtmContact, draft.get("contact_id"))
            if (not contact or contact.workspace_id != row.workspace_id or (contact.email or "").lower() != draft["to"].lower() or contact.times_emailed
                    or outreach._suppressed(session, row.workspace_id, contact)):
                return "experiment_contact_ineligible"
            if not page_tests.eligible(session, venture_id, row.workspace_id, draft["waitlist_entry_id"], draft["to"]):
                return "experiment_consent_required"
        if outreach.send_slots_used_today(venture_id) - int(stored.get("state") == "delivery_pending") >= 15:
            return "experiment_daily_cap"
    except (ValueError, TypeError):
        return "experiment_not_running"
    return None


def log_effort(venture_id, payload):
    if not isinstance(payload, dict) or set(payload) - {"experiment_id", "channel", "founder_minutes", "credits"}:
        raise ValueError("experiment_effort_invalid")
    values = {key: payload[key] for key in ("founder_minutes", "credits") if key in payload}
    if not values or any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in values.values()):
        raise ValueError("experiment_effort_invalid")
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row, context, _venture = _row(session, venture_id, payload.get("experiment_id"))
        channel = payload.get("channel")
        if channel not in context.get("links", {}):
            raise ValueError("experiment_channel_invalid")
        context["effort"][channel] = {**context["effort"].get(channel, {}), **values}
        row.context = json.dumps(context)
        session.commit()
    return readout(venture_id, {"experiment_id": payload["experiment_id"]})


def observe(spec):
    from app.conversion.waitlist import VERSION
    from app.credits.units import micro_to_credits
    from app.experiments.autonomous import _outcome_line, outcome
    from app.product_analytics import service as measurement
    if is_local() and not measurement.enabled("page_measurement"):
        return {"outcome": "inconclusive", "measured": {"events": 0}, "sources": [], "actual_micro": 0,
            "insight": measurement.blocked_message("page_measurement")}
    with db()() as session:
        _experiment, context, _venture = _row(session, spec["venture_id"], spec["id"])
        if ledger.quality_enabled() and not is_local():
            context = page_tests.refresh_launch(session, _experiment, context)
            session.commit()
            if not context.get("first_exposure_at"):
                return page_tests.ready_result()
        start, end = page_tests.observation_window(spec, context)
        events = session.scalars(select(AppEvent).where(AppEvent.app_id == spec["app_id"],
            AppEvent.workspace_id == spec["workspace_id"], AppEvent.name.in_(("landing_view", "page_view", "cta_click", "signup", "manual_counts")),
            or_(AppEvent.ts >= start, AppEvent.name == "manual_counts"), or_(AppEvent.ts <= end, AppEvent.name == "manual_counts"))).all()
        entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == spec["venture_id"],
            VentureWaitlistEntry.workspace_id == spec["workspace_id"], VentureWaitlistEntry.is_test.is_(False),
            VentureWaitlistEntry.consent_version == VERSION, VentureWaitlistEntry.created_at >= start,
            VentureWaitlistEntry.created_at <= end)).all()
        email_micro = 0
        for document_id in context.get("email_drafts", []):
            document = session.get(VentureDocument, document_id)
            if not document or document.venture_id != spec["venture_id"]:
                email_micro = None
                break
            draft = json.loads(document.provenance_json or "{}")
            approval = session.get(Approval, draft["delivery_approval_id"]) if draft.get("delivery_approval_id") else None
            detail = json.loads(approval.detail_json or "{}") if approval and approval.workspace_id == spec["workspace_id"] else {}
            execution = detail.get("external_execution") or {}
            if draft.get("state") in {"sent", "delivery_pending", "delivery_unconfirmed"}:
                if (detail.get("external_action") != "gtm_email_send" or execution.get("state") != "succeeded"
                        or type(execution.get("settled_micro")) is not int or execution["settled_micro"] < 0):
                    email_micro = None
                    break
                email_micro += execution["settled_micro"]
    by_variant, lines = [], []
    local_counts, measurement_sources, _local_views = measurement.measurement_counts(spec, context, events, entries) if is_local() else ({}, [], 0)
    if spec["channel_kind"] == "channels":
        site_path = urlsplit(spec["site_url"]).path.rstrip("/")
        views = [json.loads(event.props or "{}") for event in events if event.name == "landing_view"]
        signups = [json.loads(entry.props or "{}") for entry in entries if entry.consent_text.strip()]
        for item in spec["variants"]:
            key = item["key"]
            view_count = sum(props.get("utm_campaign") == spec["id"] and props.get("utm_source") == key
                and props.get("utm_medium") == item["medium"] and isinstance(props.get("path"), str)
                and props["path"].rstrip("/") == site_path for props in views)
            count = sum(props.get("utm_campaign") == spec["id"] and props.get("utm_source") == key
                and props.get("utm_medium") == item["medium"] for props in signups)
            if is_local():
                view_count, count = local_counts[key]["views"], local_counts[key]["signups"]
            effort = context["effort"].get(key, {})
            minutes, credits = effort.get("founder_minutes"), effort.get("credits")
            if key == "email" and credits is None and email_micro is not None:
                credits = float(micro_to_credits(email_micro))
            result = {"key": key, "label": item["label"], "views": view_count, "signups": count,
                "signup_rate": count / view_count if view_count else None, "founder_minutes": minutes, "credits": credits,
                "signups_per_founder_hour": count * 60 / minutes if minutes else None,
                "signups_per_credit": count / credits if credits else None}
            if is_local():
                result["clicks"] = local_counts[key]["clicks"]
            by_variant.append(result)
            hourly_rate = result["signups_per_founder_hour"]
            lines.append(f"{item['label']}: {count} signups / {view_count} landing views; " + (
                f"{hourly_rate:g} signups per hour of your time." if hourly_rate is not None else
                "Time efficiency is unknown without recorded effort."))
        total = sum(item["signups"] for item in by_variant)
        counts = sorted((item["signups"] for item in by_variant), reverse=True)
        measured = {"events": total, "views": sum(item["views"] for item in by_variant),
            spec["metric"]: counts[0] / total if total and counts[0] > counts[1] else None}
        if is_local():
            measured["clicks"] = sum(item["clicks"] for item in by_variant)
        lines.append("Channel costs and your effort use your logged totals, not estimates. Unlogged or zero-cost ratios are not measurable. This is observational, not a causal channel winner.")
    else:
        codes = set(context["cohort"].values())
        if ledger.quality_enabled() and not is_local():
            invited = {draft["waitlist_entry_id"] for draft in context.get("mail_drafts", [])
                if draft["contact_id"] in context.get("distribution_cohort", [])}
            codes = {code for entry_id, code in context["cohort"].items() if entry_id in invited}
        referrers = {json.loads(entry.props or "{}").get("referred_by") for entry in entries if entry.consent_text.strip()} & codes
        total = len(codes)
        measured = {"events": total, "referrers": len(referrers), "referred_signups": sum(
            json.loads(entry.props or "{}").get("referred_by") in codes for entry in entries),
            "referrer_share": len(referrers) / total if total else None}
        if is_local():
            active = {code for code in codes if local_counts.get(code, {}).get("signups", 0)}
            measured.update(referrers=len(active), referred_signups=sum(local_counts.get(code, {}).get("signups", 0) for code in codes),
                referrer_share=len(active) / total if total else None,
                views=sum(local_counts.get(code, {}).get("views", 0) for code in codes),
                clicks=sum(local_counts.get(code, {}).get("clicks", 0) for code in codes))
            referrers = active
        lines.append(f"{len(referrers)}/{total} consented referral links recorded at least one signup; {measured['referred_signups']} referred signup events."
            if is_local() else f"{len(referrers)}/{total} consented signups referred at least one person; {measured['referred_signups']} referred signups.")
    verdict = outcome(spec, measured)
    missing_denominator = any(not item["views"] for item in by_variant) if by_variant else not measured.get("events")
    if is_local() and not measurement_sources:
        missing_denominator = True
    if missing_denominator:
        verdict = "inconclusive"
    if verdict == "refuted" and utcnow() < page_tests.observation_deadline(spec, context)[1]:
        verdict = "inconclusive"
    insight = f"Your {spec['title'].lower()}: {verdict}. " + " ".join(lines)
    insight += " " + _outcome_line(spec, measured, verdict=verdict)
    insight += " Anonymous landing events are not unique visitors or verified email ownership."
    if is_local():
        insight += measurement.measurement_source_line(measurement_sources)
    if missing_denominator:
        insight += " Record views, clicks and signups for each channel and date range in chat. For referrals, use each consented person's referral link so you have an invited-person denominator."
    result = {"outcome": verdict, "measured": measured, "by_variant": by_variant, "insight": insight,
        "sources": [{"url": f"/v/{spec['venture_id']}/analytics", "title": "Your recorded landing events and consented waitlist counts"}], "actual_micro": 0}
    if is_local():
        result["measurement_sources"] = measurement_sources
    if ledger.quality_enabled() and not is_local() and spec["channel_kind"] == "referral":
        from app.experiments import engine
        result = engine.mature_result(result, engine.response_maturity(spec,
            [datetime.fromisoformat(sent) for sent in context.get("distribution_sent_at", {}).values()], utcnow()))
    return result


def readout(venture_id, payload):
    from app.product_analytics import service as measurement
    if is_local() and not measurement.enabled("page_measurement"):
        return page_tests._reply(measurement.blocked_message("page_measurement"))
    from app.connections import traffic
    signal = None
    if not is_local() and traffic.enabled():
        with db()() as session:
            _experiment, context, _venture = _row(session, venture_id, payload.get("experiment_id"))
        signal = traffic.readout(context["spec"], context, context["spec"]["site_url"])
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row, context, _venture = _row(session, venture_id, payload.get("experiment_id"))
        if ledger.quality_enabled() and not is_local():
            context = page_tests.refresh_launch(session, row, context)
            session.commit()
        with activity.track(activity.run_context(context, venture_id=venture_id), action="compare", tool="channel_events", evidence_level="conclusion", session=session) as receipt:
            result = observe(context["spec"])
            if not is_local():
                result = traffic.add_result(result, signal, context.get("result"))
            receipt["outcome"] = result["outcome"]
        context["result"] = result
        row.context, row.learnings, row.outcome = json.dumps(context), result["insight"], result["outcome"]
        if row.state == "running" and not (ledger.quality_enabled() and result.get("response_window_open") and
                result["outcome"] == "inconclusive") and utcnow() >= as_utc(datetime.fromisoformat(context["prepared_at"])) + timedelta(days=14):
            advance(session, row, "concluded", actor="system", reason="channel_test_observation")
            row.ended_at = utcnow()
        session.commit()
    actions = page_tests.launch_actions(venture_id, context["spec"], context) if ledger.quality_enabled() and result.get("launch_state") == "ready to launch" else []
    if is_local():
        actions.append(page_tests.manual_counts_action(context["spec"], context))
    return {**page_tests._reply(result["insight"], [*actions, {"label": "Record your manual result", "action": "experiment.record",
        "payload": {"experiment_id": payload["experiment_id"]}}, *(result.get("traffic") or {}).get("actions", [])], "experiment_result"), "citations": result["sources"]}
