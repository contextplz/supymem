"""Founder-approved, human-handled early-access requests using waitlist storage."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
from html import escape
import json
import re
import secrets
from typing import Literal
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from app.conversion import waitlist
from app.core.local_mode import is_local
from app.db.models import GtmContact, VentureDocument
from app.db.session import serialize_writes
from app.db.supymem_models import AppEvent, VentureWaitlist, VentureWaitlistEntry
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

DISCLOSURE = "A person reviews every request during early access."
MAX_REQUESTS = 20
MAX_ENTRIES = 1000
RATE_PER_MINUTE = 20


def _text(value):
    return re.sub(r"([\\`*_{}\[\]()#+.!|>\-])", r"\\\1", escape(value))


class RequestField(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,19}$")
    label: str = Field(min_length=1, max_length=80)
    kind: Literal["short_text", "long_text", "email", "file_link"]


class RequestForm(BaseModel):
    model_config = ConfigDict(extra="forbid")
    fields: list[RequestField] = Field(min_length=2, max_length=4)

    @model_validator(mode="after")
    def task_and_email(self):
        if (sum(field.kind == "email" for field in self.fields) != 1
                or not any(field.kind in {"short_text", "long_text"} for field in self.fields)
                or len({field.key for field in self.fields}) != len(self.fields)
                or len({field.label.strip().casefold() for field in self.fields}) != len(self.fields)
                or any(not field.label.strip() or re.search(r"[\x00-\x1f\x7f\ud800-\udfff\u2013\u2014]", field.label) for field in self.fields)):
            raise ValueError("request_fields_invalid")
        return self


def spec(venture_id, payload):
    from app.experiments import autonomous
    if not isinstance(payload, dict) or set(payload) - {"kind", "fields"}:
        raise ValueError("request_input_invalid")
    form = RequestForm.model_validate({"fields": payload.get("fields")})
    snapshot = autonomous._snapshot(venture_id)
    if not snapshot or not snapshot.get("app_id") or not snapshot["waitlist"].get("enabled"):
        raise ValueError("page_test_waitlist_required")
    hypothesis = "At least 20% of your landing views lead to a request and at least 50% of your submitters return."
    result = {key: snapshot[key] for key in ("venture_id", "workspace_id", "direction", "app_id")}
    result.update(scan="page_test", page_kind="request", catalog_key="E30_wizard_of_oz", source="founder",
        title="Human-handled request test", question="Q6", assumption=hypothesis, hypothesis=hypothesis,
        variable="core_task", variants=[{"key": "request", "label": "Human-handled request"}], runner="observe",
        runner_params={"page_test": {"experiment_id": "exp_" + "0" * 24, "request": form.model_dump(mode="json")}},
        metric="submit_rate_and_return_rate", **autonomous._thresholds(.2, .2, 50),
        return_threshold={"op": ">=", "value": .5, "min_events": 10}, min_sample=50, sample_unit="landing views",
        max_duration_days=14, ceiling_micro=0, founder_minutes=30, approval="each_publish",
        compliance=["human_handling_disclosure", "recorded_consent", "founder_publish", "opt_out", "sender_identity"],
        requires=["waitlist", "published_site"], state="proposed", result=None,
        window_end=utcnow().date().isoformat(), icp=snapshot["journey"].get("icp") or {},
        method="Minimum sample: 50 landing views and 10 distinct submitters. Support: submit rate >=20% and return rate >=50%. "
            "Return means another accepted request or your saved return link used after 24 hours. "
            "Small samples are directional and inconclusive. You handle requests yourself. "
            "Manual fallback: share the approved hosted form and record the same counts in your experiment ledger.")
    result["input_fingerprint"] = autonomous._fingerprint({key: result[key] for key in ("direction", "icp", "runner_params")})
    result["fingerprint"] = autonomous._fingerprint(result)
    result["id"] = "exp_" + result["fingerprint"][:24]
    result["runner_params"]["page_test"]["experiment_id"] = result["id"]
    return result


def target(inputs):
    return inputs.waitlist_url + "?" + urlencode({"kind": "request", "experiment_id": inputs.page_test.experiment_id})


def form_html(form, url, experiment_id, consent, version):
    """Canonical checker reference; only hosted routes use it as page source."""
    hidden = {"kind": "request", "experiment_id": experiment_id, "consent_version": version,
        "consent_digest": hashlib.sha256(consent.encode()).hexdigest()}
    parts = [f'<form id="request_form" method="post" action="{escape(url, quote=True)}">']
    for key, value in hidden.items():
        parts.append(f'<input type="hidden" name="{key}" value="{escape(value, quote=True)}">')
    for field in form.fields:
        name = "email" if field.kind == "email" else "request_" + field.key
        identity = "request_input_" + field.key
        label = escape(field.label)
        if field.kind == "long_text":
            control = f'<textarea id="{identity}" name="{name}" maxlength="1000" required></textarea>'
        else:
            maximum = 254 if field.kind == "email" else 500 if field.kind == "file_link" else 200
            control = f'<input id="{identity}" name="{name}" type="{"email" if field.kind == "email" else "text"}" maxlength="{maximum}"' + (
                ' autocomplete="email" required>' if field.kind == "email" else '>' if field.kind == "file_link" else ' required>')
        control = control.replace(' id="', ' style="min-width:24px;min-height:24px;max-width:100%;box-sizing:border-box" id="', 1)
        parts.append(f'<label for="{identity}">{label}</label>{control}')
    parts.extend([f'<label for="request_consent"><input style="min-width:24px;min-height:24px;max-width:100%;box-sizing:border-box" id="request_consent" type="checkbox" name="consent" value="yes" required> {escape(consent)}</label>',
        '<button style="min-width:24px;min-height:24px;max-width:100%;box-sizing:border-box" id="request_submit" type="submit">Send request</button></form>'])
    return "".join(parts)


def native_form(inputs):
    """Return the canonical form contract for independent source checking."""
    return form_html(inputs.page_test.request, inputs.waitlist_url, inputs.page_test.experiment_id,
        inputs.waitlist_consent_text, inputs.waitlist_consent_version)


def rendered_form_failures(inputs, observation):
    expected = ["request_input_" + field.key for field in inputs.page_test.request.fields] + ["request_consent", "request_submit"]
    controls = observation.get("visibleControls", [])
    return ["request_form_control_missing:" + identity for identity in expected
        if sum(control.get("id") == identity and bool(control.get("enabled")) for control in controls) != 1]


def setup(venture_id, payload):
    from app.experiments import page_tests
    if payload.get("fields"):
        return page_tests.prepare(venture_id, {"kind": "request", "fields": payload["fields"]})
    if is_local() and page_tests._local_builder(venture_id) is None:
        return page_tests._connect_reply()
    from app.ventures import known
    if known.enabled():
        result = known.known(venture_id, "request_fields")
        if result["status"] != "missing":
            values = result.get("values") or [{"value": result["value"], "source": result["source"]}]
            try:
                for value in values:
                    RequestForm.model_validate({"fields": value["value"]})
            except (ValueError, TypeError):
                result = {"status": "missing"}
            else:
                note = known.question(result, "request fields")["text"]
                if result["status"] == "known":
                    reply = page_tests.prepare(venture_id, {"kind": "request", "fields": result["value"]})
                    reply["body_md"] = note + "\n\n" + reply["body_md"]
                    return reply
                return page_tests._reply(note + " Use request form: to edit the fields. " + DISCLOSURE,
                    [{"label": "Yes, use these fields" if result["status"] == "confirm" else "Use fields from " + value["source"],
                      "action": "experiment.request.setup", "payload": {"fields": value["value"]}} for value in values])
    return page_tests._reply("Choose two to four fields, including one email and your core-task input. "
        "Use short text, long text, email, or an optional file link. File links are text only; there are no uploads. "
        "Say: request form: short text=<your task label>; email=<your email label>. "
        "You can add long text=<your details label> and file link=<your reference label>. " + DISCLOSURE)


def chat(venture_id, text):
    if re.fullmatch(r"\s*(?:show|list)(?: my| your)? requests[.!]?\s*", text, re.I):
        return list_reply(venture_id, {})
    if match := re.fullmatch(r"\s*request form:\s*(.+)", text, re.I | re.S):
        fields = []
        for index, part in enumerate(match[1].split(";")):
            kind, separator, label = part.strip().partition("=")
            if not separator:
                raise ValueError("request_fields_invalid")
            fields.append({"key": "field_" + str(index + 1), "label": label.strip(), "kind": kind.strip().lower().replace(" ", "_")})
        return setup(venture_id, {"fields": fields})
    if match := re.fullmatch(r"\s*reply to request (req_[A-Za-z0-9_-]+):\s*(.+)", text, re.I | re.S):
        with db()() as session:
            workspace_id = waitlist._venture(session, venture_id).workspace_id
        item = next((item for item in requests(venture_id, workspace_id) if item["id"] == match[1]), None)
        if not item:
            raise ValueError("request_not_found")
        return prepare_reply(venture_id, {"entry_id": item["entry_id"], "request_id": item["id"], "reply": match[2]})
    return None


def _published(session, venture_id, experiment_id, workspace_id=None):
    from app.experiments import page_tests
    row, context, venture = page_tests._row(session, venture_id, experiment_id)
    if workspace_id and venture.workspace_id != workspace_id:
        raise waitlist.WaitlistError("request_not_found", 404)
    if context["spec"].get("page_kind") != "request":
        raise waitlist.WaitlistError("request_not_found", 404)
    url = published_url(session, context["spec"], context)
    if not url:
        raise waitlist.WaitlistError("request_publish_required", 404)
    return row, context, venture, url


def published_url(session, spec, context):
    from app.experiments import page_tests
    url = page_tests.published_url(session, spec["venture_id"], spec["workspace_id"], spec["id"])
    if not url and context.get("hosted_request_url"):
        current = page_tests.published_url(session, spec["venture_id"], spec["workspace_id"], None)
        if current == context["hosted_request_url"]:
            url = current
    return url


def publish_card(session, spec):
    if is_local():
        return None
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.experiments import page_tests
    url = page_tests.published_url(session, spec["venture_id"], spec["workspace_id"], None)
    if not url:
        return None
    return ConfirmationCard.for_action(get_action("experiment.request.publish"),
        interpretation="Publish your hosted request form without rebuilding your landing page.",
        consequences=["Your hosted form becomes public. " + DISCLOSURE,
            "Your saved fields and consent are required. Your landing page is unchanged. Nothing is emailed.",
            "Your saved return links go back to " + url],
        resulting_state={"experiment_id": spec["id"], "return_url": url}).as_action()


def publish(venture_id, payload):
    from app.experiments import page_tests
    if is_local():
        builder = page_tests._local_builder(venture_id)
        if builder is None:
            return page_tests._connect_reply()
        with db()() as session:
            _row, context, venture = page_tests._row(session, venture_id, payload.get("experiment_id"))
            if context["spec"].get("page_kind") != "request":
                raise ValueError("request_not_found")
            url = page_tests.published_url(session, venture_id, venture.workspace_id, None)
            if not url or url != payload.get("return_url"):
                raise ValueError("request_publish_changed")
            spec = context["spec"]
        return page_tests._start_local_build(venture_id, spec, builder)
    with db()() as session:
        serialize_writes(session, key="market-experiments:" + venture_id)
        row, context, venture = page_tests._row(session, venture_id, payload.get("experiment_id"))
        if context["spec"].get("page_kind") != "request":
            raise ValueError("request_not_found")
        url = page_tests.published_url(session, venture_id, venture.workspace_id, None)
        if not url or url != payload.get("return_url"):
            raise ValueError("request_publish_changed")
        context["hosted_request_url"] = url
        row.context = json.dumps(context)
        session.commit()
    return page_tests._reply("Your hosted request form is public. Your landing page is unchanged. Nothing was emailed.",
        [{"label": "Share your hosted request form", "href": waitlist.public_url(venture_id) + "?" +
            urlencode({"kind": "request", "experiment_id": payload["experiment_id"]})}])


def _entries(session, venture_id, workspace_id):
    # ponytail: bounded 1000-person cohort stored in JSON, use a request table and indexed queries above this ceiling.
    return list(session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
        VentureWaitlistEntry.workspace_id == workspace_id, VentureWaitlistEntry.is_test.is_(False))
        .order_by(VentureWaitlistEntry.created_at, VentureWaitlistEntry.id).limit(MAX_ENTRIES + 1)))


def _requests(entry, experiment_id):
    return [request for request in json.loads(entry.props or "{}").get("requests", [])
        if request.get("experiment_id") == experiment_id]


def public_form(venture_id, experiment_id):
    details = waitlist.public_details(venture_id)
    with db()() as session:
        _row, context, _venture, _url = _published(session, venture_id, experiment_id)
        form = RequestForm.model_validate(context["spec"]["runner_params"]["page_test"]["request"])
    if is_local():
        from app.experiments.page_tests import checked_public_url
        return details["name"], checked_public_url(venture_id, experiment_id, "request.html")
    return details["name"], f'<p role="status">{DISCLOSURE}</p>' + form_html(form, details["url"], experiment_id,
        details["consent"], details["consent_version"])


def capture(venture_id, payload):
    if not isinstance(payload, dict):
        raise waitlist.WaitlistError("request_input_invalid", 422)
    try:
        size = len(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    except (UnicodeError, TypeError, ValueError):
        raise waitlist.WaitlistError("request_input_invalid", 422) from None
    if size > 4096:
        raise waitlist.WaitlistError("request_input_too_large", 413)
    if payload.get("kind") != "request" or not re.fullmatch(r"exp_[a-f0-9]{24}", str(payload.get("experiment_id", ""))):
        raise waitlist.WaitlistError("request_input_invalid", 422)
    if (payload.get("consent") is not True and payload.get("consent") != "yes") or payload.get("consent_version") != waitlist.VERSION:
        raise waitlist.WaitlistError("waitlist_consent_required", 422)
    email = waitlist.clean_email(payload.get("email"))
    receiver_url = waitlist.public_url(venture_id)
    now = as_utc(utcnow())
    token = secrets.token_urlsafe(32)
    with db()() as session:
        serialize_writes(session, key="waitlist:" + venture_id)
        row, context, venture, _url = _published(session, venture_id, payload["experiment_id"])
        settings = session.get(VentureWaitlist, venture_id)
        consent = waitlist.consent_text(venture.name)
        if not settings or settings.workspace_id != venture.workspace_id or not settings.enabled or settings.consent_version != waitlist.VERSION:
            raise waitlist.WaitlistError("waitlist_not_found", 404)
        if payload.get("consent_digest") != hashlib.sha256(consent.encode()).hexdigest():
            raise waitlist.WaitlistError("waitlist_consent_changed", 422)
        form = RequestForm.model_validate(context["spec"]["runner_params"]["page_test"]["request"])
        allowed = {"kind", "experiment_id", "email", "consent", "consent_version", "consent_digest"}
        values = {}
        for field in form.fields:
            if field.kind == "email":
                continue
            key = "request_" + field.key
            allowed.add(key)
            value = payload.get(key, "")
            maximum = {"short_text": 200, "long_text": 1000, "file_link": 500}[field.kind]
            if not isinstance(value, str) or len(value) > maximum or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value):
                raise waitlist.WaitlistError("request_field_invalid", 422)
            if field.kind == "file_link":
                value = value.strip()
            if field.kind != "file_link" and not value.strip():
                raise waitlist.WaitlistError("request_field_required", 422)
            if field.kind == "file_link" and value:
                parsed = urlsplit(value)
                if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or re.search(r"\s", value):
                    raise waitlist.WaitlistError("request_file_link_invalid", 422)
            values[field.key] = value
        if set(payload) - allowed:
            raise waitlist.WaitlistError("request_input_invalid", 422)
        entries = _entries(session, venture_id, venture.workspace_id)
        if len(entries) > MAX_ENTRIES:
            raise waitlist.WaitlistError("request_capacity_reached", 429)
        submissions = [request for entry in entries for request in _requests(entry, row.id)]
        if sum(as_utc(datetime.fromisoformat(request["submitted_at"])) > now - timedelta(minutes=1) for request in submissions) >= RATE_PER_MINUTE:
            raise waitlist.WaitlistError("request_rate_limited", 429)
        entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id,
            VentureWaitlistEntry.workspace_id == venture.workspace_id, VentureWaitlistEntry.email == email))
        if entry and entry.is_test:
            raise waitlist.WaitlistError("request_input_invalid", 422)
        if not entry and len(entries) >= MAX_ENTRIES:
            raise waitlist.WaitlistError("request_capacity_reached", 429)
        if not entry:
            entry = VentureWaitlistEntry(id=new_id("wle"), venture_id=venture_id, workspace_id=venture.workspace_id,
                email=email, is_test=False, created_at=now)
            session.add(entry)
        props = json.loads(entry.props or "{}")
        requests = props.setdefault("requests", [])
        if len(requests) >= MAX_REQUESTS:
            raise waitlist.WaitlistError("request_capacity_reached", 429)
        if requests and as_utc(datetime.fromisoformat(requests[-1]["submitted_at"])) > now - timedelta(minutes=1):
            raise waitlist.WaitlistError("request_rate_limited", 429)
        requests.append({"id": new_id("req"), "experiment_id": row.id, "values": values,
            "submitted_at": now.isoformat(), "state": "open", "return_token_hash": hashlib.sha256(token.encode()).hexdigest()})
        props.update(kind="request")
        entry.props, entry.consent_version, entry.consent_text = json.dumps(props), waitlist.VERSION, consent
        session.add(AppEvent(app_id=context["spec"]["app_id"], workspace_id=venture.workspace_id, ts=now,
            name="request_submission", props=json.dumps({"experiment_id": row.id, "source": "managed_request", "consented": True})))
        session.commit()
    return {"received": True, "email_verified": False, "return_url": receiver_url + "?" +
        urlencode({"kind": "request", "experiment_id": payload["experiment_id"], "return_token": token})}


def return_visit(venture_id, experiment_id, token):
    if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43}", token):
        raise waitlist.WaitlistError("request_return_invalid", 404)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with db()() as session:
        serialize_writes(session, key="waitlist:" + venture_id)
        _row, _context, venture, url = _published(session, venture_id, experiment_id)
        for entry in _entries(session, venture_id, venture.workspace_id):
            props = json.loads(entry.props or "{}")
            requests = _requests(entry, experiment_id)
            if not any(secrets.compare_digest(request.get("return_token_hash", ""), digest) for request in requests):
                continue
            if requests and as_utc(utcnow()) >= as_utc(datetime.fromisoformat(requests[0]["submitted_at"])) + timedelta(hours=24):
                props.setdefault("returned", {})[experiment_id] = utcnow().isoformat()
                entry.props = json.dumps(props)
                session.commit()
            return url
    raise waitlist.WaitlistError("request_return_invalid", 404)


def requests(venture_id, workspace_id):
    with db()() as session:
        waitlist._venture(session, venture_id, workspace_id)
        return [{"entry_id": entry.id, "email": entry.email, **request} for entry in _entries(session, venture_id, workspace_id)
            for request in json.loads(entry.props or "{}").get("requests", [])]


def list_reply(venture_id, payload):
    from app.experiments import page_tests
    with db()() as session:
        venture = waitlist._venture(session, venture_id, payload.get("workspace_id"))
        workspace_id = venture.workspace_id
    items = sorted(requests(venture_id, workspace_id), key=lambda item: (item["submitted_at"], item["id"]), reverse=True)
    before = payload.get("before", "")
    if before:
        position = next((index for index, item in enumerate(items) if item["id"] == before), None)
        if position is None:
            raise ValueError("request_not_found")
        items = items[position + 1:]
    lines, actions = [], []
    for item in items[:20]:
        values = "; ".join(f"{_text(key)}: {_text(value)}" for key, value in item["values"].items())
        lines.append(f"- {escape(item['id'])} ({item['state']}), {_text(item['email'])}: {values}")
        if item["state"] == "open":
            actions.append({"label": "Mark fulfilled", "action": "experiment.request.reply",
                "payload": {"entry_id": item["entry_id"], "request_id": item["id"]}})
    if len(items) > 20:
        actions.append({"label": "Show older requests", "action": "experiment.request.list", "payload": {"before": items[19]["id"]}})
    return page_tests._reply("Your requests:\n" + ("\n".join(lines) or "No requests yet.") +
        "\nTo write a reply, say: reply to request <request id>: <your reply>. You approve the exact email before sending.", actions)


def _request(session, venture_id, payload):
    venture = waitlist._venture(session, venture_id, payload.get("workspace_id"))
    entry = session.get(VentureWaitlistEntry, payload.get("entry_id"))
    if not entry or entry.is_test or (entry.venture_id, entry.workspace_id) != (venture_id, venture.workspace_id):
        raise ValueError("request_not_found")
    props = json.loads(entry.props or "{}")
    request = next((item for item in props.get("requests", []) if item["id"] == payload.get("request_id")), None)
    if not request:
        raise ValueError("request_not_found")
    return venture, entry, props, request


def prepare_reply(venture_id, payload):
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from app.experiments import page_tests
    body = payload.get("reply", "")
    if not isinstance(body, str) or len(body) > 4000 or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff\u2013\u2014]", body):
        raise ValueError("request_reply_invalid")
    with db()() as session:
        _venture, entry, _props, request = _request(session, venture_id, payload)
        if request["state"] != "open":
            return page_tests._reply("Your request is already fulfilled. Review your saved email draft before sending.")
        card = ConfirmationCard.for_action(get_action("experiment.request.fulfill"),
            interpretation="Mark your request fulfilled" + (" and save your reply draft." if body.strip() else "."),
            consequences=[f"Request: {escape(request['id'])}. Recipient: {_text(entry.email)}.",
                f"Your reply: {_text(body)}" if body.strip() else "You handle this request yourself. No email draft is created.",
                "Nothing is sent. Sending requires your separate approval, consent, opt-out checks and sender address."],
            resulting_state={"entry_id": entry.id, "request_id": request["id"], "reply": body.strip()})
    return page_tests._reply(card.interpretation, [card.as_action()], "confirmation")


def fulfill(venture_id, payload):
    from app.approvals.external import outbound_quote
    from app.autopilot import outreach
    from app.experiments import page_tests
    from app.gtm.relationships.email_compliance import complete_body, sender_address
    prepare_reply(venture_id, payload)
    body = payload.get("reply", "").strip()
    with db()() as session:
        serialize_writes(session, key="waitlist:" + venture_id)
        venture, entry, props, request = _request(session, venture_id, payload)
        if request["state"] != "open":
            return page_tests._reply("Your request is already fulfilled. Nothing new was saved or sent.")
        actions = []
        if body:
            if not sender_address(session, venture.workspace_id):
                raise ValueError("gtm_sender_address_required")
            if not page_tests.eligible(session, venture_id, venture.workspace_id, entry.id, entry.email):
                raise ValueError("request_email_ineligible")
            contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == venture.workspace_id,
                GtmContact.email == entry.email).order_by(GtmContact.id).limit(1))
            # shortcut: first consented reply only, use threaded replies for previously emailed contacts.
            if contact and (outreach._suppressed(session, venture.workspace_id, contact) or contact.times_emailed
                    or (contact.source or "").split(":")[1:2] != [venture_id]):
                raise ValueError("request_email_ineligible")
            if not contact:
                contact = GtmContact(id=new_id("gtmc"), workspace_id=venture.workspace_id, email=entry.email,
                    email_confidence="risky", source=f"venture:{venture_id}:waitlist", lawful_basis="consent",
                    lawful_basis_at=as_utc(datetime.fromisoformat(props["requests"][-1]["submitted_at"])), lawful_basis_note=entry.consent_text,
                    enrichment_json=json.dumps({"founder_import": True, "waitlist_entry_id": entry.id}),
                    stage_changed_at=utcnow(), created_at=utcnow())
                session.add(contact)
                session.flush()
            if outreach._suppressed(session, venture.workspace_id, contact):
                raise ValueError("request_email_ineligible")
            batch_id = new_id("wmail")
            draft = {"contact_id": contact.id, "to": entry.email, "channel": "email", "day": 1,
                "subject": "Your early-access request", "body": complete_body(session, venture.workspace_id, contact, body,
                    outreach.founder_signature(venture_id)), "state": "draft", "batch_id": batch_id,
                "source": "waitlist", "requires_batch_approval": True, "one_off": True,
                "waitlist_entry_id": entry.id, "request_id": request["id"],
                "credit_ceiling_micro": outbound_quote("gtm_email_send", "composio_gmail")["credit_estimate_micro"]}
            if outreach.mailboxes.enabled():
                sender = outreach.draft_mailbox(venture.workspace_id, venture_id, draft, session=session)
                draft.update(sender=sender, mailbox_id=sender["mailbox_id"])
                draft["body"] = outreach.complete_sender_body(session, venture_id, draft, sender)
            document = VentureDocument(id=new_id("odr"), venture_id=venture_id, kind=outreach.KIND,
                title="Your request reply", body_md=draft["body"], provenance_json=json.dumps(draft),
                created_at=utcnow(), updated_at=utcnow())
            session.add(document)
            request["reply_document_id"] = document.id
            actions.append({"batch_id": batch_id})
        request.update(state="fulfilled", fulfilled_at=utcnow().isoformat())
        entry.props = json.dumps(props)
        session.commit()
    actions = [outreach.send_card(1, action["batch_id"], venture_id=venture_id) for action in actions]
    return page_tests._reply("Your request is marked fulfilled. " +
        ("Your reply is saved, not sent. Review and approve the email batch. If sending is unavailable, use your saved compliant draft only while every recipient eligibility check still passes." if body else "No email was sent."), actions)


def observe(spec):
    from app.experiments import page_tests
    from app.experiments.exploration import founder_status
    now = as_utc(utcnow())
    with db()() as session:
        row, context, venture = page_tests._row(session, spec["venture_id"], spec["id"])
        url = published_url(session, spec, context)
        start = as_utc(row.started_at) if row.started_at else as_utc(datetime.fromisoformat(context["prepared_at"]))
        end = min(now, start + timedelta(days=spec["max_duration_days"]))
        entries = _entries(session, venture.id, venture.workspace_id)
        cohorts = [(entry, [request for request in _requests(entry, row.id)
            if start <= as_utc(datetime.fromisoformat(request["submitted_at"])) <= end]) for entry in entries]
        cohorts = [(entry, requests) for entry, requests in cohorts if requests]
        records = session.scalars(select(AppEvent).where(AppEvent.app_id == spec["app_id"],
            AppEvent.workspace_id == venture.workspace_id, AppEvent.name == "landing_view", AppEvent.ts >= start, AppEvent.ts <= end))
        views = sum(json.loads(event.props or "{}").get("experiment_id") == row.id and
            not str(json.loads(event.props or "{}").get("path", "")).startswith("/public/waitlists/") for event in records)
    submissions = sum(len(requests) for _entry, requests in cohorts)
    submitters = len(cohorts)
    # ponytail: repeats and saved links identify returns, add explicit opt-in visit tracking to measure anonymous revisits.
    returned = sum(len(_requests(entry, row.id)) > 1 or row.id in json.loads(entry.props or "{}").get("returned", {}) for entry, requests in cohorts)
    submit_rate = submissions / views if views else None
    return_rate = returned / submitters if submitters else None
    mature = submitters >= 10 and all(now >= as_utc(datetime.fromisoformat(requests[0]["submitted_at"])) + timedelta(hours=24)
        for _entry, requests in cohorts)
    verdict = "inconclusive"
    if url and views >= spec["min_sample"] and mature:
        verdict = "supported" if submit_rate >= .2 and return_rate >= .5 else "refuted"
    measured = {"landing_views": views, "submissions": submissions, "submitters": submitters,
        "returning_submitters": returned, "submit_rate": submit_rate, "return_rate": return_rate,
        "events": views,
        "submit_rate_and_return_rate": min(submit_rate, return_rate * .2 / .5)
            if url and mature and submit_rate is not None and return_rate is not None else None}
    status = founder_status(state="concluded", outcome=verdict, evidence_level="conclusion")
    return {"outcome": verdict, "measured": measured, "by_variant": {}, "actual_micro": 0,
        "insight": f"Your request test: {submissions}/{views} submissions per landing view; {returned}/{submitters} submitters returned. "
            f"Submit rate: {submit_rate}; return rate: {return_rate}. Minimum sample: 50 views and 10 submitters observed for 24 hours. "
            f"Result: {status}. Small samples are directional. Landing views are anonymous events, not unique people. "
            "Submitters are distinct consented email addresses, not verified people. "
            "Submission counts cover the first 14 days; returns can be observed in later follow-up. "
            "Returns count repeat requests or saved return links used after 24 hours, not every anonymous revisit." +
            (" Approve Publish before collecting requests." if not url else ""),
        "sources": [{"url": f"/v/{spec['venture_id']}/analytics", "title": "Your recorded landing views and stored request counts"}]}
