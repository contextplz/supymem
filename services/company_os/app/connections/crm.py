"""Founder-owned, read-only HubSpot evidence scoped to a venture's contacts."""
from __future__ import annotations

import json
import logging
import re

from sqlalchemy import or_, select

from app.db.models import GtmContact, GtmTouch, Person, Venture, Workspace
from app.core.local_mode import is_local
from app.connections import local_config
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.state import db
from core.clock import utcnow
from core.copy import no_dashes

log = logging.getLogger(__name__)
NAMES = {"hubspot": "HubSpot", "typeform": "Typeform"}
READS = {
    "contacts": r"HUBSPOT_(?:GET|LIST|RETRIEVE)_(?:ALL_)?CONTACTS",
    "deals": r"HUBSPOT_(?:GET|LIST|RETRIEVE)_(?:ALL_)?DEALS",
    "pipelines": r"HUBSPOT_(?:GET|LIST|RETRIEVE)_(?:ALL_)?(?:CRM_)?PIPELINES",
    "responses": r"TYPEFORM_(?:GET|LIST|RETRIEVE)_(?:FORM_)?RESPONSES",
}
POST_READS = {"reddit": r"REDDIT_RETRIEVE_POST_COMMENTS", "linkedin": r"LINKEDIN_GET_POST_CONTENT"}


def enabled():
    if is_local():
        return local_config.enabled("connected_apps")
    from app.experiments import tools
    from app.experiments.ledger import autopilot_enabled
    return autopilot_enabled() or tools.enabled()


def _connection(workspace_id, platform):
    from app.authz.workspace import subject_person_id
    from app.connections import toolkits
    from app.connections.revenue import founder_user_id

    owner = founder_user_id(workspace_id)
    from app.experiments import tools
    allowed_users = {owner}
    if (is_local() or tools.enabled()) and platform in tools.registered_runners() and platform not in NAMES:
        allowed_users.add("content:" + workspace_id)
    return next((row for row in toolkits.accounts(workspace_id, platform)
        if row["outcome"] == "connected" and row["provider_user_id"] in allowed_users
        and subject_person_id(row["actor_subject"]) == owner), None)


def _reader(venture_id, platform, collect):
    from app.connections import toolkits
    from app.connections.composio_migration import missing_connection
    from app.content import composio_social
    from app.content.oauth.attempts import OAuthError
    from app.content.oauth.callbacks import _verified_connection
    from app.credits import metered
    from app.gtm.supymem import composio_gmail
    from app.tasks import usage

    from app.experiments import tools as experiment_tools
    if not enabled():
        if is_local():
            return {"status": "unavailable", "message": local_config.setup_message(platform)}
        return None
    from app.content.platforms import PUBLISHERS
    social = (is_local() or experiment_tools.enabled()) and platform in PUBLISHERS
    if platform not in NAMES and not social:
        raise ValueError("experiment_connection_invalid")
    if is_local() and local_config.missing(platform):
        return {"status": "unavailable", "message": local_config.setup_message(platform)}
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage == "purged":
            return None
        workspace_id = venture.workspace_id
    name = NAMES.get(platform, platform.title())
    action = {"label": "Connect " + name, "action": "connections.connect",
              "payload": {"query": "connect " + name}}
    disconnected = {"status": "not_connected", "action": action}
    if is_local():
        disconnected["message"] = f"Reconnect your {name} account on the Connections screen, then try again."
    try:
        account = _connection(workspace_id, platform)
        if any(row["outcome"] != "revoked" for row in toolkits.accounts(workspace_id, platform)):
            action["payload"]["query"] = "reconnect " + name
        if not account:
            return disconnected
        with metered.charge_sync(workspace_id=workspace_id, venture_id=venture_id,
                purpose="experiments." + platform, provider="composio", estimate_micro=0, payer="founder") as meter:
            _verified_connection(account)
            catalog = composio_gmail._request("GET", "/tools",
                params={"toolkit_slug": composio_social.toolkit_for(platform), "limit": 1000})
            meter.record_micro(0)
        tools = catalog.get("items", catalog.get("data"))
        if isinstance(tools, dict):
            tools = tools.get("items")
        if not isinstance(tools, list):
            raise ValueError("experiment_tool_catalog_invalid")

        def read(operation, values):
            if not (enabled() if is_local() or not social else experiment_tools.enabled()) or _connection(workspace_id, platform) != account:
                raise ValueError("experiment_connection_changed")
            if social:
                if operation != "post_replies":
                    raise ValueError("experiment_tool_not_read_only")
                pattern = POST_READS.get(platform, r"(?!)")
            elif operation not in READS or (operation == "responses") != (platform == "typeform"):
                raise ValueError("experiment_tool_not_read_only")
            else:
                pattern = READS[operation]
            matches = [tool for tool in tools if isinstance(tool, dict)
                and re.fullmatch(pattern, str(tool.get("slug") or ""))]
            if len(matches) != 1:
                raise ValueError("experiment_read_tool_unavailable")
            tool = matches[0]
            usage.before_tool()
            with metered.charge_sync(workspace_id=workspace_id, venture_id=venture_id,
                    purpose="experiments." + platform, provider="composio", estimate_micro=0, payer="founder") as meter:
                _verified_connection(account)
                schema = tool.get("input_parameters")
                if not isinstance(schema, dict):
                    schema = composio_gmail._request("GET", "/tools/" + tool["slug"]).get("input_parameters")
                if not isinstance(schema, dict) or not isinstance(schema.get("properties"), dict):
                    raise ValueError("experiment_tool_schema_missing")
                properties = schema["properties"]
                normalized = {name.replace("_", "").lower(): name for name in properties}
                arguments = {}
                for name, value in values.items():
                    target = normalized.get(name.replace("_", "").lower())
                    if target:
                        if isinstance(value, list) and properties[target].get("type") == "string":
                            value = ",".join(value)
                        arguments[target] = value
                    elif name in {"form_id", "post_id", "after", "associations"} or (
                            name in {"limit", "page_size"} and (is_local() or experiment_tools.enabled()) and operation != "pipelines"):
                        raise ValueError("experiment_tool_scope_missing")
                if any(name not in arguments for name in schema.get("required", [])):
                    raise ValueError("experiment_tool_arguments_missing")
                reply = composio_gmail.execute_tool(tool["slug"], user_id=account["provider_user_id"],
                    connected_account_id=account["connected_account_id"], arguments=arguments,
                    toolkit_version=composio_social._toolkit_version(platform))
                meter.record_micro(0)
            if not isinstance(reply, dict) or reply.get("successful") is not True:
                raise ValueError("experiment_connection_read_failed")
            data = reply.get("data")
            if isinstance(data, dict) and isinstance(data.get("data"), dict):
                data = data["data"]
            if not isinstance(data, dict):
                raise ValueError("experiment_connection_data_invalid")
            return data

        result = collect(read, workspace_id)
        if not (enabled() if is_local() or not social else experiment_tools.enabled()) or _connection(workspace_id, platform) != account:
            raise ValueError("experiment_connection_changed")
        return {"status": "connected", "account_id": account["connected_account_id"], **result}
    except (OAuthError, composio_gmail.ReconnectRequired):
        return disconnected
    except Exception as exc:
        if missing_connection(exc):
            return disconnected
        log.warning("experiment_connection_unavailable", extra={"platform": platform, "error_type": type(exc).__name__})
        if is_local():
            return {"status": "unavailable", "message": f"Check your {name} connection on the Connections screen, then try reading it again."}
        return {"status": "unavailable"}


def _pages(read, operation, values):
    after, seen = None, set()
    # shortcut: refuse incomplete lists above 1,000 rows; use scheduled snapshots for larger accounts.
    for _page in range(10):
        data = read(operation, {**values, "limit": 100, **({"after": after} if after else {})})
        rows = data.get("results")
        if not isinstance(rows, list):
            raise ValueError("crm_page_invalid")
        from app.experiments import tools
        if tools.enabled() and len(rows) > 100:
            raise ValueError("crm_page_unbounded")
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"] or row["id"] in seen:
                raise ValueError("crm_row_invalid")
            seen.add(row["id"])
            yield row
        paging = data.get("paging") or {}
        after = (paging.get("next") or {}).get("after")
        if not after:
            return
        if not rows:
            raise ValueError("crm_page_incomplete")
    raise ValueError("crm_totals_incomplete")


def experiment_contacts(session, venture_id, workspace_id):
    related = select(GtmTouch.id).where(GtmTouch.contact_id == GtmContact.id,
        GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0).exists()
    return {row.id: row.email for row in session.scalars(select(GtmContact).where(
        GtmContact.workspace_id == workspace_id, GtmContact.email.is_not(None),
        or_(GtmContact.source.startswith(f"venture:{venture_id}:", autoescape=True), related)))}


def snapshot(venture_id, *, experiment_id=None):
    from app.experiments import ledger
    from app.lead_library.taxonomy import FREE_MAIL
    from app.experiments import tools
    if experiment_id is not None:
        with db()() as session:
            row, context = tools._row(session, venture_id, experiment_id)
            selected = context.get("crm_contact_ids")
            if not selected:
                return None

    def collect(read, workspace_id):
        with db()() as session:
            owner = session.get(Person, session.get(Workspace, workspace_id).owner_id)
            owner_email = (owner.email or "").strip().lower()
            contacts = experiment_contacts(session, venture_id, workspace_id)
            if experiment_id is not None and set(selected) - set(contacts):
                raise ValueError("experiment_contacts_out_of_scope")
            emails = {email.strip().lower() for identifier, email in contacts.items()
                if (experiment_id is None or identifier in selected)
                if email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email.strip())}
        emails.discard(owner_email)
        domains = ({email.rsplit("@", 1)[1].rstrip(".") for email in emails} - FREE_MAIL) if experiment_id is None else set()
        contacts = set()
        if emails:
            for row in _pages(read, "contacts", {"properties": ["email"]}):
                email = (row.get("properties") or {}).get("email")
                if isinstance(email, str):
                    email = email.strip().lower()
                    if (email != owner_email and re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email)
                            and (email in emails or email.rsplit("@", 1)[-1].rstrip(".") in domains)):
                        contacts.add(row["id"])
        stages, counts, deals = {}, {}, {}
        if contacts:
            for pipeline in _pages(read, "pipelines", {"object_type": "deals"}):
                if not isinstance(pipeline.get("stages"), list):
                    raise ValueError("crm_stages_missing")
                for stage in pipeline["stages"]:
                    rank = stage.get("displayOrder")
                    if type(rank) is not int or not isinstance(stage.get("id"), str) or not isinstance(stage.get("label"), str):
                        raise ValueError("crm_stage_invalid")
                    stages[(pipeline["id"], stage["id"])] = {
                        "rank": rank, "label": no_dashes(stage["label"][:120]),
                        "lost": str((stage.get("metadata") or {}).get("isClosed", "")).lower() == "true"
                            and float((stage.get("metadata") or {}).get("probability") or 0) == 0}
            for row in _pages(read, "deals", {"properties": ["pipeline", "dealstage"], "associations": ["contacts"]}):
                associations = row.get("associations") or {}
                linked = (associations.get("contacts") or {}).get("results", [])
                if not isinstance(linked, list):
                    raise ValueError("crm_associations_invalid")
                if not any(str(contact.get("id")) in contacts for contact in linked):
                    continue
                if (associations.get("contacts") or {}).get("paging"):
                    raise ValueError("crm_associations_incomplete")
                properties = row.get("properties") or {}
                pipeline, stage_id = properties.get("pipeline"), properties.get("dealstage")
                stage = stages.get((pipeline, stage_id))
                if not stage:
                    raise ValueError("crm_deal_stage_missing")
                deals[row["id"]] = {"pipeline": pipeline, "stage": stage_id, **stage}
                counts[stage["label"]] = counts.get(stage["label"], 0) + 1
        return {"contact_count": len(contacts), "deal_count": len(deals), "stage_counts": counts, "deals": deals}

    result = _reader(venture_id, "hubspot", collect)
    if not result or result["status"] != "connected":
        return result
    progressed = set()
    try:
        with db()() as session:
            serialize_writes(session, key="market-experiments:" + venture_id)
            direction = ledger.current_direction(session, venture_id)
            rows = session.scalars(select(Experiment).where(Experiment.venture_id == venture_id, Experiment.kind == ledger.KIND)
                .order_by(Experiment.id).with_for_update()).all()
            row = next((row for row in rows if (experiment_id is None or row.id == experiment_id)
                and (experiment_id is not None or not tools.enabled() or ledger._ctx(row).get("spec", {}).get("scan") != "tool_evidence")
                and (ledger._ctx(row).get("spec") or {}).get("direction", ledger._ctx(row).get("direction", "")) == direction), None)
            if row and enabled():
                context = ledger._ctx(row)
                if experiment_id is not None:
                    tools._row(session, venture_id, experiment_id)
                    if context.get("crm_contact_ids") != selected:
                        raise ValueError("experiment_contacts_changed")
                previous = context.get("crm_snapshot") or {}
                if previous.get("account_id") == result["account_id"]:
                    for identifier, stage in result["deals"].items():
                        old = (previous.get("deals") or {}).get(identifier)
                        if old and old["pipeline"] == stage["pipeline"] and not stage["lost"]:
                            if (stage["stage"] != old["stage"] and stage["rank"] > old["rank"]) or (stage["stage"] == old["stage"]
                                    and identifier in previous.get("progressed", [])):
                                progressed.add(identifier)
                context["crm_snapshot"] = {"account_id": result["account_id"], "deals": result["deals"],
                                           "progressed": sorted(progressed),
                    **({"observed_from": previous.get("observed_from", utcnow().isoformat()) if previous.get("account_id") == result["account_id"] else utcnow().isoformat()}
                       if experiment_id is not None else {})}
                row.context = json.dumps(context)
                session.commit()
    except Exception as exc:
        log.warning("crm_progress_unavailable", extra={"error_type": type(exc).__name__})
        return {"status": "unavailable"}
    result.pop("deals")
    result.pop("account_id")
    return {**result, "progress_count": len(progressed), "strength": "commitment" if progressed else "facts",
            "scope": "experiment_contacts" if experiment_id is not None else "venture_contacts",
            **({"experiment_id": experiment_id, "observed_from": context["crm_snapshot"]["observed_from"]} if experiment_id is not None else {})}


def summary_lines(result):
    if not result or result.get("status") != "connected":
        return []
    stages = ", ".join(f"{count} in {stage}" for stage, count in sorted(result["stage_counts"].items()))
    return [f"Your HubSpot contacts for this venture: {result['contact_count']} contacts and {result['deal_count']} deals"
            + (f", {stages}." if stages else "."),
            f"{result['progress_count']} deals moved forward since your first HubSpot check. A deal is not a payment."]
