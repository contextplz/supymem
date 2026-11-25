"""Confirmed exports to a new spreadsheet on your connected account."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from sqlalchemy import select

from app.authz.workspace import assert_venture_access, subject_person_id
from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard
from app.chat.conversations import resolve
from app.chat.store import _partition, _ser
from app.connections import toolkits
from app.credits import metered
from app.db.models import ChatMessage, GtmCompany, GtmContact, Venture, VentureDocument, Workspace
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.experiments.ledger import _ctx, autopilot_enabled
from app.core.local_mode import is_local
from app.connections import local_config
from app.gtm.supymem import composio_gmail
from app.state import db
from core.clock import utcnow
from core.ids import new_id

ACTION = "experiment.export_sheet"
KIND = "experiment_sheet_export"
CREATE = "GOOGLESHEETS_CREATE_GOOGLE_SHEET"
UPDATE = "GOOGLESHEETS_BATCH_UPDATE_SPREADSHEET"


def _enabled():
    return local_config.enabled("connected_apps") if is_local() else autopilot_enabled()


def _reply(body, *, kind="text", actions=None, started=False):
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": [], "action_started": started}


def _connect(*, reconnect=False):
    if is_local() and local_config.missing("googlesheets"):
        return _reply(local_config.setup_message("googlesheets"), kind="needs_you")
    return _reply("Connect your Google Sheets account, then ask me to export your results. Nothing has been exported.",
        kind="needs_you", actions=[{"label": "Connect Google Sheets", "action": "connections.connect",
            "payload": {"query": "reconnect Google Sheets" if reconnect else "connect Google Sheets"}, "style": "primary"}])


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value).replace("\u2014", ", ").replace("\u2013", "-")


def _lead_source(contact):
    try:
        data = json.loads(contact.enrichment_json or "{}")
    except (TypeError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if data.get("founder_import"):
        return "Imported by you"
    return data.get("found_at") or ""


def _snapshot(session, venture, include_leads):
    rows = [["Name", "What you tested", "What you measured", "Collected", "Target", "Outcome", "Started", "Ended"]]
    experiments = session.scalars(select(Experiment).where(Experiment.venture_id == venture.id,
        (Experiment.workspace_id == venture.workspace_id) | Experiment.workspace_id.is_(None)).order_by(Experiment.id)).all()
    for experiment in experiments:
        context = _ctx(experiment)
        spec, result = context.get("spec") or {}, context.get("result") or {}
        metric = experiment.metric or spec.get("metric") or ""
        thresholds = []
        for key, label in (("success_threshold", "Works"), ("failure_threshold", "Does not work")):
            threshold = spec.get(key) or {}
            if threshold:
                thresholds.append(f"{label}: {threshold.get('op', '')} {threshold.get('value', '')}"
                    + (f" with at least {threshold['min_events']} events" if "min_events" in threshold else ""))
        values = [spec.get("name") or spec.get("title") or experiment.kind.replace("_", " "),
            experiment.hypothesis, metric.replace("_", " "), (result.get("measured") or {}).get(metric, experiment.result),
            "; ".join(thresholds), {"supported": "Worked", "refuted": "Did not work",
                "inconclusive": "Too early to tell"}.get(experiment.outcome or result.get("outcome"), ""),
            experiment.started_at.isoformat() if experiment.started_at else "",
            experiment.ended_at.isoformat() if experiment.ended_at else ""]
        rows.append([_cell(value) for value in values])
    if include_leads:
        rows.extend([[""], ["Your leads"], ["Name", "Role", "Company", "Source", "Status"]])
        contacts = session.execute(select(GtmContact, GtmCompany.canonical_name).outerjoin(GtmCompany,
            (GtmCompany.id == GtmContact.company_id) & (GtmCompany.workspace_id == venture.workspace_id)).where(
                GtmContact.workspace_id == venture.workspace_id,
                GtmContact.source.startswith(f"venture:{venture.id}:", autoescape=True)).order_by(GtmContact.id)).all()
        for contact, company in contacts:
            rows.append([_cell(value) for value in (" ".join(part for part in (contact.first_name, contact.last_name) if part),
                contact.role, company, _lead_source(contact), contact.relationship_stage.replace("_", " "))])
    return rows


def _call(saved, callback):
    workspace_id = saved["workspace_id"]
    if not _enabled():
        raise ValueError(local_config.setup_message("googlesheets") if is_local()
                         else "Your export permission changed. Nothing more will be written.")
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        venture = session.get(Venture, saved["venture_id"])
        if workspace is None or workspace.owner_id != saved["actor_id"] or venture is None \
                or venture.workspace_id != workspace_id or venture.archived_at is not None:
            raise PermissionError("Your export permission changed. Nothing more will be written.")
    if not any(account["outcome"] == "connected" and account["connected_account_id"] == saved["connection_id"]
            and account["provider_user_id"] == saved["user_id"]
            and subject_person_id(account["actor_subject"]) == saved["actor_id"]
            for account in toolkits.accounts(workspace_id, "googlesheets")):
        raise composio_gmail.ReconnectRequired()
    with metered.charge_sync(workspace_id=workspace_id, venture_id=saved["venture_id"], purpose=ACTION, provider="composio",
            estimate_micro=0, payer="founder") as meter:
        result = callback()
        meter.record_micro(0)
        return result


def _tools(saved):
    # shortcut: only the first 100 Sheets tools are checked; paginate if the required tools move beyond that page.
    discovered = _call(saved, lambda: composio_gmail._request("GET", "/tools",
        params={"toolkit_slug": "googlesheets", "limit": 100}))
    items = discovered.get("items") or discovered.get("data") or []
    if isinstance(items, dict):
        items = items.get("items") or []
    slugs = {item.get("slug") or item.get("name") or item.get("tool_slug") for item in items if isinstance(item, dict)}
    if not {CREATE, UPDATE} <= slugs:
        raise ValueError("Your Google Sheets connection does not support this export yet.")
    schemas = {}
    for slug in (CREATE, UPDATE):
        tool = _call(saved, lambda: composio_gmail._request("GET", f"/tools/{slug}"))
        schemas[slug] = tool.get("input_parameters") or (tool.get("data") or {}).get("input_parameters") or {}
    return schemas


def _arguments(schema, values):
    properties = schema.get("properties") or {}
    if "body" in properties and not set(values) <= properties.keys():
        values = {key: value for key, value in values.items() if key in properties} | {
            "body": {key: value for key, value in values.items() if key not in properties}}
    if not set(values) <= properties.keys() or not set(schema.get("required") or []) <= values.keys():
        raise ValueError("Your Google Sheets connection does not support this export yet.")
    return values


def _update_arguments(schema, spreadsheet_id, rows, sheet_id, sheet_name):
    properties = schema.get("properties") or {}
    if {"values", "value_input_option"} <= properties.keys():
        values = {"spreadsheet_id": spreadsheet_id, "values": rows, "value_input_option": "RAW"}
        if "sheet_name" in properties and sheet_name:
            values["sheet_name"] = sheet_name
        if "first_cell_location" in properties:
            values["first_cell_location"] = "A1"
        if "range" in properties:
            values["range"] = "'" + sheet_name.replace("'", "''") + "'!A1" if sheet_name else "A1"
        return _arguments(schema, values)
    if type(sheet_id) is not int or sheet_id < 0:
        raise ValueError("Your new spreadsheet's tab could not be verified.")
    requests = [{"updateSheetProperties": {"properties": {"sheetId": sheet_id, "gridProperties": {
        "rowCount": len(rows), "columnCount": max(len(row) for row in rows)}},
        "fields": "gridProperties.rowCount,gridProperties.columnCount"}},
        {"updateCells": {"start": {"sheetId": sheet_id, "rowIndex": 0, "columnIndex": 0},
        "rows": [{"values": [{"userEnteredValue": {"stringValue": value}} for value in row]} for row in rows],
        "fields": "userEnteredValue"}}]
    return _arguments(schema, {"spreadsheet_id": spreadsheet_id, "requests": requests})


def _approved(venture_id, payload):
    scope = resolve(venture_id)
    with db()() as session:
        messages = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
            ChatMessage.role == "assistant", ChatMessage.actions_json.contains(payload["export_id"], autoescape=True),
            _partition(scope))).all()
        for row in messages:
            if any(item.get("confirmation", {}).get("action") == ACTION
                and item["confirmation"].get("resulting_state") == payload
                and item["confirmation"].get("state") == "starting"
                for item in _ser(row, scope.id)["actions"] if isinstance(item, dict) and isinstance(item.get("confirmation"), dict)):
                return True
    return False


def _saved_reply(saved):
    link = saved.get("link")
    if saved["state"] == "complete":
        return _reply(f"You can open your exported results here: {link}",
            actions=[{"label": "Open your spreadsheet", "href": link, "style": "primary"}], started=True)
    if saved.get("needs_connection") and not link:
        return _connect(reconnect=True)
    body = "Your export was interrupted. Check your Drive before requesting a new export. I will not repeat this write."
    if link:
        body = f"Your new spreadsheet was created, but its results were not confirmed. Check it here: {link}. I will not repeat this write."
    actions = [{"label": "Open your spreadsheet", "href": link, "style": "primary"}] if link else []
    if saved.get("needs_connection"):
        body += " Reconnect Google Sheets, then ask me for a new export."
        actions.extend(_connect(reconnect=True)["actions"])
    return _reply(body, kind="needs_you", actions=actions)


def export(venture_id: str, payload: dict[str, Any], actor_subject: str | None) -> dict[str, Any]:
    if is_local() and local_config.missing("googlesheets"):
        return _connect()
    if not _enabled():
        return _reply("You cannot export results to Google Sheets yet. Your saved results are unchanged.", kind="needs_you")
    _, workspace_id = assert_venture_access(venture_id, actor_subject)
    actor_id = subject_person_id(actor_subject)
    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        venture = session.get(Venture, venture_id)
        if workspace is None or workspace.owner_id != actor_id or venture.archived_at is not None:
            raise PermissionError("Only you can export results to your connected account.")
    accounts = [account for account in toolkits.accounts(workspace_id, "googlesheets")
        if account["outcome"] == "connected" and subject_person_id(account["actor_subject"]) == actor_id]
    if not accounts:
        return _connect()
    if "export_id" not in payload:
        if set(payload) - {"include_leads"} or type(payload.get("include_leads", False)) is not bool:
            raise ValueError("Choose whether to include your leads. Existing spreadsheets cannot be used.")
        now, export_id = utcnow(), new_id("sheet_export")
        with db()() as session:
            saved = {"state": "prepared", "workspace_id": workspace_id, "venture_id": venture_id, "actor_id": actor_id,
                "connection_id": accounts[0]["connected_account_id"], "user_id": accounts[0]["provider_user_id"],
                "title": f"{_cell(venture.name)} results {now.date().isoformat()}",
                "rows": _snapshot(session, venture, payload.get("include_leads", False))}
            session.add(VentureDocument(id=export_id, venture_id=venture_id, kind=KIND, title=saved["title"],
                provenance_json=json.dumps(saved), created_at=now, updated_at=now))
            session.commit()
        if not is_local():
            card = ConfirmationCard.for_action(get_action(ACTION), interpretation="Export your saved results to a new Google spreadsheet?",
                consequences=["You will create a new spreadsheet in your connected Google account. Existing spreadsheets will not change.",
                    "Your results are copied as saved when you requested this export. Private notes and email addresses are not included."
                    + (" Your leads' names, roles, companies, sources and statuses are included." if payload.get("include_leads") else "")],
                resulting_state={"export_id": export_id})
            return _reply("Review this export, then confirm before I write to your account.", kind="confirmation",
                actions=[{**card.as_action(), "label": "Create your spreadsheet"}])
        payload = {"export_id": export_id}
    if set(payload) != {"export_id"} or not isinstance(payload["export_id"], str) or (not is_local() and not _approved(venture_id, payload)):
        raise ValueError("Accept the exact confirmation in your private chat before exporting.")
    with db()() as session:
        serialize_writes(session, key=f"sheet_export:{payload['export_id']}")
        document = session.scalar(select(VentureDocument).where(VentureDocument.id == payload["export_id"],
            VentureDocument.venture_id == venture_id, VentureDocument.kind == KIND))
        if document is None:
            raise ValueError("Your export review is no longer available. Ask for a new export.")
        saved = json.loads(document.provenance_json)
        if saved["workspace_id"] != workspace_id or saved["actor_id"] != actor_id:
            raise PermissionError("Your export belongs to another account.")
        account = next((account for account in accounts if account["connected_account_id"] == saved["connection_id"]
            and account["provider_user_id"] == saved["user_id"]), None)
        if account is None:
            return _connect()
        if saved["state"] != "prepared":
            return _saved_reply(saved)
        saved["state"] = "writing"
        document.provenance_json = json.dumps(saved)
        document.updated_at = utcnow()
        session.commit()
    try:
        schemas = _tools(saved)
        create_values = {"title": saved["title"]}
        if "sheet_name" in (schemas[CREATE].get("properties") or {}):
            create_values["sheet_name"] = "Results"
        create_arguments = _arguments(schemas[CREATE], create_values)
        _update_arguments(schemas[UPDATE], "new", saved["rows"], 0, "Results")
        def execute(slug, arguments):
            return _call(saved, lambda: composio_gmail.execute_tool(slug, user_id=saved["user_id"],
                connected_account_id=saved["connection_id"], arguments=arguments))
        created = execute(CREATE, create_arguments)
        if created.get("successful") is not True:
            raise ValueError("Your new spreadsheet could not be verified.")
        data = created.get("data") or {}
        if isinstance(data.get("data"), dict):
            data = data["data"]
        spreadsheet_id = data.get("spreadsheetId") or data.get("spreadsheet_id")
        sheets = data.get("sheets") or []
        sheet_id = (sheets[0].get("properties") or {}).get("sheetId") if sheets else None
        sheet_name = (sheets[0].get("properties") or {}).get("title") if sheets else None
        sheet_name = sheet_name or data.get("sheet_name") or create_arguments.get("sheet_name")
        if not isinstance(spreadsheet_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", spreadsheet_id):
            raise ValueError("Your new spreadsheet could not be verified.")
        saved["link"] = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        with db()() as session:
            document = session.get(VentureDocument, payload["export_id"])
            document.provenance_json = json.dumps(saved)
            session.commit()
        updated = execute(UPDATE, _update_arguments(schemas[UPDATE], spreadsheet_id, saved["rows"], sheet_id, sheet_name))
        if updated.get("successful") is not True:
            raise ValueError("Your exported results could not be verified.")
        saved["state"] = "complete"
    except Exception as exc:
        saved["state"] = "interrupted"
        saved["error_type"] = type(exc).__name__
        saved["needs_connection"] = isinstance(exc, composio_gmail.ReconnectRequired) or (
            isinstance(exc, composio_gmail.DefiniteFailure) and exc.status_code in {401, 403})
        logging.getLogger(__name__).warning("sheet_export_interrupted", extra={"venture_id": venture_id,
            "export_id": payload["export_id"], "error_type": type(exc).__name__})
    with db()() as session:
        document = session.get(VentureDocument, payload["export_id"])
        document.provenance_json = json.dumps(saved)
        document.updated_at = utcnow()
        session.commit()
    return _saved_reply(saved)
