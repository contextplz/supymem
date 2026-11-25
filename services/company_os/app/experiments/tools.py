"""Opt-in tool-backed tests, using the existing connection and publication boundaries."""
from __future__ import annotations

import json
from datetime import timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.chat.recovery import log_founder_exception
from app.credits.policy import get_policy
from app.connections import crm, surveys
from app.db.models import ContentAccount, ContentOAuthState, Venture, Workspace
from app.db.session import serialize_writes
from app.db.supymem_models import AppEvent, Experiment
from app.experiments import ledger
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import founder_copy


READ_RUNNERS = {
    "hubspot": {"metric": "progress_count", "sample_unit": "contacts", "minimum": 10,
                "signal": "forward deal movements",
                "founder_only": True,
                "reader": crm.snapshot, "scope_key": "crm_contact_ids", "events": "contact_count",
                "method": "Read forward deal movement for only the contacts you choose for this test."},
    "typeform": {"metric": "response_count", "sample_unit": "responses", "minimum": 30,
                 "signal": "completed survey responses",
                 "founder_only": True,
                 "reader": surveys.snapshot, "scope_key": "typeform_form_id", "events": "response_count",
                 "method": "Read completed responses from only the form you link to this test."},
}


def enabled():
    from app.core.local_mode import is_local

    if is_local():
        from app.experiments.autonomous import _local_enabled

        return _local_enabled("run_for_me_experiments") and _local_enabled("connected_apps")
    try:
        flags = get_policy("feature_flags")
    except Exception as exc:  # noqa: BLE001 - an unreadable flag means off; chat replies must not fail on it
        log_founder_exception(exc, None, "experiment.tools.enabled")
        return False
    return isinstance(flags, dict) and flags.get("experiments_tools_v2") is True


def blocked_reply():
    from app.core.local_mode import is_local
    from app.experiments.autonomous import _local_blocked_message, _local_enabled

    name = "connected_apps" if not is_local() or _local_enabled("run_for_me_experiments") else "run_for_me_experiments"
    return {"kind": "experiment_blocked", "body_md": _local_blocked_message(name) if is_local() else
        "Your tool-backed tests are switched off. Nothing was changed.", "actions": [], "citations": []}


def registered_runners():
    from app.content.platforms import PUBLISHERS
    from app.connections.crm import POST_READS

    return {**READ_RUNNERS, **{slug: {
        "metric": "replies", "sample_unit": "replies", "minimum": 10,
        "signal": "post replies",
        "method": "Read replies to your exact approved post and clicks on its tagged landing link. "
                  "You approve every post in Content before anything is published.",
    } for slug in PUBLISHERS if slug in POST_READS}}


def capabilities(venture_id, specs=None):
    """Derive the inventory from catalogs, local account receipts and registered runners."""
    if not enabled():
        return None
    from app.authz.workspace import subject_person_id
    from app.chat.connect import SERVICES
    from app.connections import bookings, toolkits, traffic
    from app.content.platforms import PUBLISHERS
    from app.experiments import autonomous

    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        if not workspace or workspace.archived_at or venture.archived_at or venture.stage in {"purged", "archived", "paused"}:
            return {"tools": [], "native_tests": []}
        workspace_id, owner_id = workspace.id, workspace.owner_id
        social = {row.platform for row in session.scalars(select(ContentAccount).where(
            ContentAccount.workspace_id == workspace_id, ContentAccount.status == "active"))}
        receipts = session.scalars(select(ContentOAuthState).where(ContentOAuthState.workspace_id == workspace_id,
            ContentOAuthState.auth_method == "composio", ContentOAuthState.connected_account_id.is_not(None),
            ContentOAuthState.auth_config_id.is_not(None), ContentOAuthState.consumed_at.is_not(None),
            ContentOAuthState.provider_user_id.in_([owner_id, "content:" + workspace_id]))
            .order_by(ContentOAuthState.created_at.desc(), ContentOAuthState.id.desc())).all()
        accounts = {}
        for receipt in receipts:
            if subject_person_id(receipt.actor_subject) == owner_id:
                accounts.setdefault((receipt.platform, receipt.connected_account_id), (receipt.outcome, receipt.provider_user_id))
    specs = autonomous.select_runnable(autonomous._snapshot(venture_id), autonomous._provider()) if specs is None else specs
    runners = registered_runners()
    extra_runners = {slug: {bookings.read_bookings.__module__ + "." + bookings.read_bookings.__name__} for slug in bookings.READ_TOOLS}
    for slug in traffic.NAMES:
        extra_runners.setdefault(slug, set()).add(traffic.readout.__module__ + "." + traffic.readout.__name__)
    catalog = {**{slug: {"name": SERVICES.get(slug, (slug, ""))[0]} for slug in extra_runners},
        **{slug: {"name": SERVICES.get(slug, (slug, ""))[0]} for spec in specs for slug in spec.get("requires", [])},
        **toolkits.TOOLKITS, **{slug: {"name": SERVICES.get(slug, (slug, ""))[0],
        "goals": [runners[slug]["method"]] if slug in runners else []} for slug in PUBLISHERS}}
    inventory = []
    for slug, entry in catalog.items():
        connected = any(platform == slug and outcome == "connected" and
            (not runners.get(slug, {}).get("founder_only") or provider_user == owner_id)
            for (platform, _account_id), (outcome, provider_user) in accounts.items())
        connected = connected or any(slug in spec.get("requires", []) and spec.get("scan") != "tool_evidence"
            and spec.get("state") != "blocked" for spec in specs)
        matches = sorted({spec.get("runner") or spec.get("scan") for spec in specs
            if slug in spec.get("requires", []) and (spec.get("runner") or spec.get("scan"))})
        runnable = bool(matches) or slug in runners or slug in extra_runners and ledger.autopilot_enabled()
        matches.extend(extra_runners.get(slug, []))
        if slug in runners:
            matches.append("tool_evidence")
        inventory.append({"slug": slug, "name": entry["name"], "goals": entry.get("goals", []),
            "connected": connected, "publishing_connected": slug in social, "runners": sorted(set(matches)),
            "needs_reconnect": not connected and any(platform == slug and outcome != "revoked"
                for (platform, _account_id), (outcome, _provider_user) in accounts.items()),
            "runnable": connected and runnable, "requires_connection": bool(matches) and not connected})
    return {"tools": inventory, "native_tests": [{"id": spec["id"],
        "runner": spec.get("runner") or spec.get("scan"), "requires": spec.get("requires", [])}
        for spec in specs if spec.get("scan") != "tool_evidence"]}


def connection_reply(tool, reason, *, reconnect=False):
    from app.chat.connect import SERVICES
    from app.connections.toolkits import TOOLKITS

    name = (TOOLKITS.get(tool) or {}).get("name") or SERVICES.get(tool, (tool, ""))[0]
    why = founder_copy(reason)
    body = (f"Your best next test uses {name}. {why}\n\n"
        f"Don't have an account? I'll walk you through it.\n"
        f"1. Open the official {name} website yourself and choose Create account or Sign up.\n"
        "2. Enter your own email and choose your own password on that website. Do not send credentials here.\n"
        "3. Complete the website's email verification and any required account setup yourself.\n"
        "4. You can skip optional paid upgrades. Review the terms and any community rules before proceeding.\n"
        f"5. Return here and choose Connect {name}. You review and authorize the connection yourself.\n\n"
        "I will not automate signup, handle credentials, send messages or publish without your approval.")
    return {"kind": "experiment_setup", "body_md": body, "citations": [], "actions": [
        {"label": "Connect " + name, "action": "connections.connect", "payload": {"query": ("reconnect " if reconnect else "connect ") + name}}]}


def specs(snapshot):
    if not enabled() or not snapshot:
        return []
    from app.experiments.autonomous import _fingerprint

    icp = snapshot["journey"].get("icp") or {}
    if not icp.get("who") or not icp.get("problem"):
        return []
    result = []
    for tool, adapter in registered_runners().items():
        fingerprint = _fingerprint([snapshot["venture_id"], snapshot["direction"], tool, icp])
        metric = adapter["metric"]
        result.append({"id": "exp_" + fingerprint[:24], "fingerprint": fingerprint,
            **{key: snapshot[key] for key in ("venture_id", "workspace_id", "direction")},
            "scan": "tool_evidence", "runner": "tool_evidence", "tool": tool, "requires": [tool],
            "icp": icp, "hypothesis": founder_copy(f"You can record {adapter['minimum']} {adapter['signal']} from {icp['who']} about {icp['problem']}.")[:600],
            "title": f"Your {tool} response test", "method": adapter["method"], "metric": metric,
            "min_sample": adapter["minimum"], "sample_unit": adapter["sample_unit"],
            "success_threshold": {"metric": metric, "op": ">=", "value": adapter["minimum"], "min_events": adapter["minimum"]},
            "failure_threshold": {"metric": metric, "op": "<", "value": adapter["minimum"], "min_events": adapter["minimum"]},
            "max_duration_days": 7, "ceiling_micro": 0, "founder_minutes": 5,
            "approval": "once" if tool in READ_RUNNERS else "each_publish", "state": "proposed", "result": None,
            "app_id": snapshot.get("app_id"), "site_url": (snapshot.get("waitlist", {}).get("published_site") or {}).get("url")})
    return result


def prepare(venture_id, spec):
    if not enabled():
        return blocked_reply()
    inventory = capabilities(venture_id, [spec])
    entry = next((item for item in inventory["tools"] if item["slug"] == spec["tool"]), None)
    if not entry or not entry["connected"]:
        return connection_reply(spec["tool"], spec.get("tool_reason") or spec["method"],
            reconnect=bool(entry and (entry["publishing_connected"] or entry["needs_reconnect"])))
    body = spec["method"] + " Nothing is sent or published by linking evidence."
    if spec["tool"] == "typeform":
        body += " Link the existing form ID with experiment.survey.link, then read your results."
        actions = [{"label": "Link your survey", "action": "experiment.survey.link", "payload": {"experiment_id": spec["id"]}}]
    else:
        field = "contact_ids" if spec["tool"] == "hubspot" else "post_id"
        body += f" Choose your exact {field.replace('_', ' ')} for this experiment with experiment.tools.link."
        actions = [{"label": "Link your test evidence", "action": "experiment.tools.link", "payload": {"experiment_id": spec["id"]}}]
        if spec["tool"] != "hubspot":
            from app.experiments.channel_tests import tagged_link
            if spec.get("site_url"):
                body += "\nPut this exact link in your post before you review and approve it: " + tagged_link(
                    spec["site_url"], utm_source=spec["tool"], utm_medium="social", utm_campaign=spec["id"])
            actions.append({"label": "Review your post in Content", "href": f"/content?venture={venture_id}"})
    actions.append({"label": "Read your results", "action": "experiment.tools.readout", "payload": {"experiment_id": spec["id"]}})
    return {"kind": "experiment_setup", "body_md": body, "actions": actions, "citations": []}


def _row(session, venture_id, experiment_id, actor_subject=None):
    from app.authz.workspace import subject_person_id
    if not isinstance(experiment_id, str) or not 1 <= len(experiment_id) <= 160:
        raise ValueError("Choose the experiment you want to measure.")
    venture = session.get(Venture, venture_id)
    workspace = session.get(Workspace, venture.workspace_id) if venture else None
    row = session.get(Experiment, experiment_id)
    context = ledger._ctx(row) if row else {}
    if (not enabled() or not venture or venture.archived_at or venture.stage in {"purged", "archived", "paused"}
            or not workspace or workspace.archived_at or not row or row.venture_id != venture_id
            or row.workspace_id != workspace.id or row.kind != ledger.KIND
            or context.get("spec", {}).get("scan") != "tool_evidence"
            or context["spec"]["direction"] != ledger.current_direction(session, venture_id)
            or actor_subject is not None and subject_person_id(actor_subject) != workspace.owner_id):
        raise ValueError("experiment_tool_scope_invalid")
    return row, context


def link(venture_id, payload, actor_subject):
    from app.core.local_mode import is_local

    if is_local() and not enabled():
        return blocked_reply()
    if actor_subject is None:
        raise ValueError("Only you can link evidence for your test.")
    if not isinstance(payload, dict) or set(payload) - {"experiment_id", "contact_ids", "post_id"}:
        raise ValueError("experiment_tool_link_invalid")
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row, context = _row(session, venture_id, payload.get("experiment_id"), actor_subject)
        if row.state != "proposed":
            raise ValueError("Your running test evidence cannot be changed.")
        spec = context["spec"]
        if spec["tool"] == "hubspot":
            identifiers = payload.get("contact_ids")
            if not isinstance(identifiers, list) or not 1 <= len(identifiers) <= 100 or any(
                    not isinstance(identifier, str) or not identifier for identifier in identifiers) or len(set(identifiers)) != len(identifiers):
                raise ValueError("Choose between 1 and 100 saved contacts for your test.")
            from app.connections.crm import experiment_contacts
            if set(identifiers) - set(experiment_contacts(session, venture_id, row.workspace_id)):
                raise ValueError("experiment_contacts_out_of_scope")
            context["crm_contact_ids"] = identifiers
        else:
            from app.content.publication import _post
            if not isinstance(payload.get("post_id"), str) or not 1 <= len(payload["post_id"]) <= 160:
                raise ValueError("Choose your exact Content post ID for this test.")
            post, _source = _post(session, row.workspace_id, venture_id, payload.get("post_id"))
            if post.platform != spec["tool"] or post.dry_run or post.state not in {"draft", "approved", "scheduled"}:
                raise ValueError("Choose an unpublished, real post for this test.")
            if spec.get("site_url"):
                from app.experiments.channel_tests import tagged_link
                link_url = tagged_link(spec["site_url"], utm_source=spec["tool"], utm_medium="social", utm_campaign=spec["id"])
                if link_url not in post.body:
                    raise ValueError("Your post needs this experiment's tagged link before approval.")
            context["social_post_id"] = post.id
        context["evidence_started_at"] = utcnow().isoformat()
        context.pop("crm_snapshot", None)
        row.context = json.dumps(context)
        session.commit()
    return {"kind": "experiment_setup", "body_md": "Your evidence is linked to this test. You still approve every post before publication.",
        "citations": [], "actions": [{"label": "Read your results", "action": "experiment.tools.readout", "payload": {"experiment_id": spec["id"]}}]}


def observe(spec):
    if not enabled():
        from app.core.local_mode import is_local

        raise ValueError(blocked_reply()["body_md"] if is_local() else "experiments_tools_v2_disabled")
    with db()() as session:
        row, context = _row(session, spec["venture_id"], spec["id"])
        started_at = row.started_at or context.get("evidence_started_at")
        if isinstance(started_at, str):
            from datetime import datetime
            started_at = datetime.fromisoformat(started_at)
    adapter = registered_runners()[spec["tool"]]
    if "reader" in adapter:
        if not context.get(adapter["scope_key"]):
            raise ValueError("Link your exact evidence before reading this test.")
        signal = adapter["reader"](spec["venture_id"], experiment_id=spec["id"])
        if signal and signal.get("observed_from"):
            from datetime import datetime
            started_at = datetime.fromisoformat(signal["observed_from"])
        evidence = next((item for item in (signal or {}).get("experiments", []) if item["experiment_id"] == spec["id"]), signal or {})
        measured = {"events": evidence.get(adapter["events"], 0), spec["metric"]: evidence.get(spec["metric"], 0)}
        sources = [{"url": evidence.get("url") or f"/v/{spec['venture_id']}/market-map", "title": "Your selected test's recorded evidence"}]
    else:
        signal, measured, sources, started_at = _social(spec, context)
    if signal and signal.get("status") == "not_connected":
        return {"error": "You need to reconnect your selected test evidence before reading it.",
            "connection_required": True, "connection_action": signal.get("action"),
            "reconnect": (signal.get("action", {}).get("payload", {}).get("query") or "").lower().startswith("reconnect ")}
    if not signal or signal.get("status") != "connected":
        raise ValueError("Your selected evidence could not be read. This is not counted as a failed test.")
    from app.experiments.autonomous import _outcome_line, outcome
    verdict = outcome(spec, measured)
    if not started_at or utcnow() < as_utc(started_at) + timedelta(days=spec["max_duration_days"]):
        if verdict == "refuted":
            verdict = "inconclusive"
    return {"outcome": verdict, "measured": measured, "sources": sources, "actual_micro": 0,
        "observed_from": as_utc(started_at).isoformat() if started_at else None,
        "evidence_type": "market_response", "attribution": {"experiment_id": spec["id"], "tool": spec["tool"]},
        "insight": founder_copy(f"Your {spec['tool']} test has {measured['events']} recorded observations. "
            "These are observed responses or sales progress, not payments or proof of product-market fit. "
            + _outcome_line(spec, measured, verdict=verdict))}


def _social(spec, context):
    from app.connections import crm
    from app.content.publication import _post

    with db()() as session:
        post, _source = _post(session, spec["workspace_id"], spec["venture_id"], context.get("social_post_id"))
        if post.platform != spec["tool"] or post.dry_run or post.state != "published" or not post.external_id or not post.published_at:
            raise ValueError("Your approved post must be published before it produces evidence.")
        if spec.get("site_url"):
            from app.experiments.channel_tests import tagged_link
            if tagged_link(spec["site_url"], utm_source=spec["tool"], utm_medium="social", utm_campaign=spec["id"]) not in post.body:
                raise ValueError("Your published post no longer contains this test's tagged link.")
        external_id, external_url, published = post.external_id, post.external_url, as_utc(post.published_at)
        events = session.scalars(select(AppEvent).where(AppEvent.app_id == spec.get("app_id"),
            AppEvent.workspace_id == spec["workspace_id"], AppEvent.ts >= published,
            AppEvent.props.contains(spec["id"], autoescape=True),
            AppEvent.ts <= min(utcnow(), published + timedelta(days=spec["max_duration_days"])),
            AppEvent.name.in_(("landing_view", "page_view"))).limit(1001)).all() if spec.get("app_id") else []
    if len(events) > 1000:
        raise ValueError("Your click sample exceeds the bounded read. No partial total is reported.")
    path = urlsplit(spec.get("site_url") or "").path.rstrip("/")
    clicks = sum(props.get("utm_campaign") == spec["id"] and props.get("utm_source") == spec["tool"]
        and props.get("utm_medium") == "social" and isinstance(props.get("path"), str)
        and props["path"].rstrip("/") == path for props in (json.loads(event.props or "{}") for event in events))

    def collect(read, _workspace_id):
        if spec["tool"] == "linkedin":
            identifier = external_id if external_id.startswith("urn:li:") else "urn:li:share:" + external_id
            data = read("post_replies", {"post_id": identifier})
            if data.get("id", identifier) not in {identifier, external_id}:
                raise ValueError("experiment_post_reply_scope_invalid")
            count = data.get("comment_count", (data.get("socialDetail") or {}).get("totalFirstLevelComments"))
            if count is not None and (type(count) is not int or count < 0):
                raise ValueError("experiment_post_reply_invalid")
            # shortcut: post-content totals have no reply dates, add dated reads when the toolkit supplies them.
            return {"replies": count if utcnow() <= published + timedelta(days=spec["max_duration_days"]) else None}
        data = read("post_replies", {"post_id": external_id.removeprefix("t3_"), "limit": 100})
        replies = data.get("comments", data.get("children", data.get("elements", data.get("results", data.get("items")))))
        if not isinstance(replies, list) or len(replies) >= 100 or data.get("paging", {}).get("next"):
            raise ValueError("experiment_post_replies_incomplete")
        identifiers = [reply.get("id") or reply.get("name") for reply in replies if isinstance(reply, dict)]
        if len(identifiers) != len(replies) or any(not isinstance(identifier, str) or not identifier for identifier in identifiers):
            raise ValueError("experiment_post_reply_invalid")
        if len(set(identifiers)) != len(identifiers) or any(reply.get("post_id", reply.get("link_id", external_id)) not in
                {external_id, external_id.removeprefix("t3_")} for reply in replies):
            raise ValueError("experiment_post_reply_scope_invalid")
        return {"replies": len(identifiers) if utcnow() <= published + timedelta(days=spec["max_duration_days"]) else None}

    signal = crm._reader(spec["venture_id"], spec["tool"], collect)
    reply_count = (signal or {}).get("replies")
    measured = {"events": reply_count if reply_count is not None else clicks, "replies": reply_count, "clicks": clicks}
    return signal, measured, [{"url": external_url or "", "title": "Your exact approved post replies"},
        {"url": f"/v/{spec['venture_id']}/analytics", "title": "Your campaign-tagged landing clicks, not unique people"}], published


def readout(venture_id, payload):
    from app.core.local_mode import is_local

    with db()() as session:
        row, context = _row(session, venture_id, payload.get("experiment_id"))
        spec = context["spec"]
        if row.state == "concluded" and context.get("result"):
            result = context["result"]
            return {"kind": "experiment_result", "body_md": result["insight"], "citations": result["sources"], "actions": []}
    if is_local() and not enabled():
        return blocked_reply()
    try:
        result = observe(spec)
    except ValueError as error:
        reply = prepare(venture_id, spec)
        reply["body_md"] = founder_copy(str(error)) + "\n\n" + reply["body_md"]
        return reply
    if result.get("connection_required"):
        return connection_reply(spec["tool"], spec.get("tool_reason") or spec["method"], reconnect=result["reconnect"])
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row, context = _row(session, venture_id, spec["id"])
        context["result"] = result
        if row.state == "proposed":
            from app.core.fsm import advance
            advance(session, row, "running", actor="founder", reason="read_selected_experiment_evidence")
            from datetime import datetime
            row.started_at = datetime.fromisoformat(result["observed_from"]) if result["observed_from"] else utcnow()
        if result["outcome"] != "inconclusive":
            from app.core.fsm import advance
            advance(session, row, "concluded", actor="system", reason="measured_tool_experiment")
            row.ended_at, row.outcome, row.learnings = utcnow(), result["outcome"], result["insight"]
        row.context = json.dumps(context)
        session.commit()
    ledger._invalidate(venture_id)
    return {"kind": "experiment_result", "body_md": result["insight"], "citations": result["sources"], "actions": []}
