"""Read only the Typeform responses you link to an experiment."""
from __future__ import annotations

import json
import math
import re
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select

from app.db.models import Venture, Workspace
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment
from app.state import db
from core.clock import utcnow
from core.copy import no_dashes


def form_id(value):
    if not isinstance(value, str):
        raise ValueError("survey_form_invalid")
    value = value.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", value):
        return value
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username or url.password
            or url.netloc != url.hostname or not (url.hostname == "typeform.com" or url.hostname.endswith(".typeform.com"))):
        raise ValueError("survey_form_invalid")
    match = re.fullmatch(r"/to/([A-Za-z0-9_-]{1,80})/?", url.path)
    if not match:
        raise ValueError("survey_form_invalid")
    return match[1]


def link_form(venture_id, experiment_id, form, actor_subject):
    from app.authz.workspace import subject_person_id
    from app.experiments import ledger

    from app.experiments import tools
    if not ledger.autopilot_enabled() and not tools.enabled():
        return None
    identifier = form_id(form)
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        row = session.scalar(select(Experiment).where(Experiment.id == experiment_id,
            Experiment.venture_id == venture_id, Experiment.kind == ledger.KIND).with_for_update())
        if (not venture or venture.archived_at or venture.stage == "purged" or not workspace or workspace.archived_at
                or subject_person_id(actor_subject) != workspace.owner_id or not row
                or row.venture_id != venture_id or row.workspace_id != workspace.id or row.kind != ledger.KIND):
            raise ValueError("survey_experiment_invalid")
        context = ledger._ctx(row)
        if tools.enabled() and context.get("spec", {}).get("scan") == "tool_evidence":
            tools._row(session, venture_id, experiment_id, actor_subject)
            if row.state != "proposed" or context["spec"]["tool"] != "typeform":
                raise ValueError("survey_experiment_invalid")
            context["evidence_started_at"] = utcnow().isoformat()
        context["typeform_form_id"] = identifier
        row.context = json.dumps(context)
        session.commit()
    return {"experiment_id": experiment_id, "form_id": identifier}


def _answers(answers):
    if not isinstance(answers, list):
        raise ValueError("survey_answers_invalid")
    result = []
    for answer in answers:
        if not isinstance(answer, dict) or not isinstance(answer.get("field"), dict):
            raise ValueError("survey_answer_invalid")
        kind = answer.get("type")
        if kind not in {"text", "boolean", "number", "date", "choice", "choices"}:
            continue
        value = answer.get(kind)
        if kind == "choice":
            value = (value.get("other") or value.get("label")) if isinstance(value, dict) else None
        if kind == "choices":
            selected = value if isinstance(value, dict) else {}
            value = selected.get("labels")
            if isinstance(value, list) and selected.get("other"):
                value = value + [selected["other"]]
        valid = ((kind in {"text", "date", "choice"} and isinstance(value, str))
                 or (kind == "boolean" and type(value) is bool)
                 or (kind == "number" and type(value) in {int, float} and math.isfinite(value))
                 or (kind == "choices" and isinstance(value, list) and all(isinstance(item, str) for item in value)))
        if not valid or not isinstance(answer["field"].get("id"), str) or not answer["field"]["id"]:
            raise ValueError("survey_answer_invalid")
        # shortcut: excerpts keep up to 50 choices and 2,000 characters each; read the linked form for longer answers.
        value = no_dashes(value[:2000]) if isinstance(value, str) else value
        if isinstance(value, list):
            value = [no_dashes(item[:2000]) for item in value[:50]]
        result.append({"field_id": answer["field"]["id"], "value": value})
    return result


def snapshot(venture_id, *, experiment_id=None):
    from app.connections.crm import _reader
    from app.experiments import ledger

    from app.experiments import tools
    if not ledger.autopilot_enabled() and not tools.enabled():
        return None
    with db()() as session:
        direction = ledger.current_direction(session, venture_id)
        linked = []
        windows = {}
        for row in session.scalars(select(Experiment).where(
                Experiment.venture_id == venture_id, Experiment.kind == ledger.KIND).order_by(Experiment.id)):
            context = ledger._ctx(row)
            identifier = context.get("typeform_form_id")
            if identifier and (row.id == experiment_id or (experiment_id is None
                    and (context.get("spec") or {}).get("direction", context.get("direction", "")) == direction)):
                linked.append((row.id, form_id(identifier)))
                if tools.enabled() and context.get("spec", {}).get("scan") == "tool_evidence":
                    windows[row.id] = (context.get("evidence_started_at"), context["spec"]["max_duration_days"])
    if not linked:
        return None

    def collect(read, _workspace_id):
        forms = {}
        for _experiment_id, identifier in linked:
            if identifier in forms:
                continue
            data = read("responses", {"form_id": identifier, "page_size": 1000,
                                     "response_type": "completed", "completed": True})
            rows, total = data.get("items"), data.get("total_items")
            # shortcut: refuse more than 1,000 responses instead of claiming partial totals; add pagination for larger forms.
            if (not isinstance(rows, list) or type(total) is not int or total != len(rows) or total > 1000
                    or type(data.get("page_count")) is not int or not 0 <= data["page_count"] <= 1):
                raise ValueError("survey_responses_incomplete")
            responses, seen = [], set()
            for row in rows:
                if not isinstance(row, dict) or row.get("form_id", identifier) != identifier:
                    raise ValueError("survey_response_scope_invalid")
                token = row.get("response_id") or row.get("token")
                if not isinstance(token, str) or not token or token in seen:
                    raise ValueError("survey_response_invalid")
                seen.add(token)
                if not row.get("submitted_at"):
                    continue
                submitted = datetime.fromisoformat(row["submitted_at"].replace("Z", "+00:00"))
                if submitted.tzinfo is None or submitted > utcnow():
                    raise ValueError("survey_response_time_invalid")
                responses.append({"response_id": token, "answers": _answers(row.get("answers")),
                    **({"submitted_at": submitted.isoformat()} if windows else {})})
            forms[identifier] = sorted(responses, key=lambda response: response["response_id"])
        from datetime import timedelta
        for experiment, identifier in linked:
            responses = forms[identifier]
            if experiment in windows:
                start, days = windows[experiment]
                if not start:
                    raise ValueError("survey_measurement_window_missing")
                start = datetime.fromisoformat(start)
                responses = [response for response in responses if start <= datetime.fromisoformat(response["submitted_at"]) <= start + timedelta(days=days)]
            forms[(experiment, identifier)] = [{key: value for key, value in response.items() if key != "submitted_at"} for response in responses]
        return {"strength": "facts", "scope": "linked_experiments", "experiments": [
            {"experiment_id": experiment_id, "form_id": identifier, "response_count": len(forms[(experiment_id, identifier)]),
             "responses": forms[(experiment_id, identifier)], "url": f"https://form.typeform.com/to/{identifier}"}
            for experiment_id, identifier in linked]}

    result = _reader(venture_id, "typeform", collect)
    if result and result["status"] == "connected":
        result.pop("account_id")
    return result


def summary_lines(result):
    if not result or result.get("status") != "connected":
        return []
    return [f"Your linked Typeform form has {row['response_count']} completed "
            + ("response" if row["response_count"] == 1 else "responses") + " for this test. "
            "Survey answers tell you what people said, not whether they will buy."
            for row in result["experiments"]]
