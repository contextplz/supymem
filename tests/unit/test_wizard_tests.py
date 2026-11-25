"""Request form regressions for unrelated business and consumer ventures."""
import asyncio
from datetime import timedelta
from html import escape
import json
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from tests.company_os_harness import local_owner_clients
from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_page_experiments import consent_payload, record


@pytest.fixture
def request_builds(monkeypatch):
    from app.build import device_orders
    from app.core import local_owner
    from app.core.local_mode import is_local
    from app.experiments import page_tests

    briefs = []
    if is_local():
        monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
        monkeypatch.setattr(page_tests, "_connected_provider", lambda *_: "codex")

        def prepare_order(venture_id, caller, instruction, *, experiment_id):
            briefs.append(page_tests.coding_brief(venture_id, experiment_id))
            return {"id": "dwo_request", "state": "approved"}

        monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare_order)
    return briefs


def test_local_publication_keeps_accepted_source_and_experiment_binding(market, monkeypatch):
    from app.db.models import VentureDocument
    from app.db.supymem_models import BuildRun, Deployment
    from app.experiments import page_tests, wizard_tests
    from app.hosting import service
    from app.state import db
    from core.clock import utcnow
    from types import SimpleNamespace

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client, venture_id, workspace_id, view, landing, *_rest, app_id = market
    landing["enabled"] = True
    spec = wizard_tests.spec(venture_id, {"kind": "request", "fields": [
        {"key": "task", "label": view["icp"]["problem"], "kind": "short_text"},
        {"key": "email", "label": "Your email", "kind": "email"},
    ]})
    experiment_id, url = spec["id"], "https://your-published-page.example/"
    with db()() as session:
        page_tests._save_page_test(session, spec)
        run = BuildRun(id="br_local_page", venture_id=venture_id, workspace_id=workspace_id, app_id=app_id,
            state="succeeded", artifact_id="artifact_local_page", trigger="manual", agent="pagebuilder",
            spec=json.dumps({"inputs": {"pagebuilder_input": {"page_test": {"experiment_id": experiment_id}}}}))
        session.add(run)
        session.add(Deployment(id="dep_local_page", venture_id=venture_id, workspace_id=workspace_id, app_id=app_id,
            build_artifact_id=run.artifact_id, target="local_static", state="live", version=1, url=url, live_at=utcnow()))
        receipt = {"workspace_id": workspace_id, "app_id": app_id, "local_deployment_id": "dep_local_page", "state": "live", "url": url}
        session.add(VentureDocument(id="local-publication:" + venture_id, venture_id=venture_id, kind="local_publication",
            title="Your published page", provenance_json=json.dumps(receipt), created_at=utcnow(), updated_at=utcnow()))
        session.commit()

        def accepted(source_session, app, build_id, *, historical=False):
            assert (app.id, build_id, historical) == (app_id, run.id, True)
            return run, SimpleNamespace(id=run.artifact_id), None, None, None

        monkeypatch.setattr(service, "_accepted_source", accepted)
        assert page_tests.published_url(session, venture_id, workspace_id, experiment_id) == url
        assert page_tests.published_url(session, venture_id, "another_workspace", experiment_id) is None
        assert page_tests.published_url(session, venture_id, workspace_id, "another_experiment") is None
        path = f"/public/waitlists/{venture_id}"
        submission = consent_payload(venture_id, "reader@request-mail.example", kind="request",
            experiment_id=experiment_id, request_task=view["icp"]["problem"])
        assert client.post(path, json={**submission, "consent": False}).status_code == 422
        assert client.post(path, json=submission).status_code == 201
        valid_spec = run.spec
        run.spec = json.dumps([])
        session.commit()
        assert page_tests.published_url(session, venture_id, workspace_id, experiment_id) is None
        run.spec = valid_spec
        session.commit()
        def unaccepted(*_args, **_kwargs):
            raise service.HostingError("hosting_build_not_accepted")

        monkeypatch.setattr(service, "_accepted_source", unaccepted)
        assert page_tests.published_url(session, venture_id, workspace_id, experiment_id) is None


def test_request_setup_reuses_saved_fields_without_publishing(market, monkeypatch, request_builds):
    from app.core.local_mode import is_local
    from app.credits.policy import set_policy
    from app.experiments import page_tests, wizard_tests
    from app.ventures import known

    _client, venture_id, _workspace_id, view, landing, *_rest = market
    landing["enabled"] = True
    fields = [{"key": "task", "label": view["icp"]["problem"], "kind": "short_text"},
              {"key": "email", "label": "Your email", "kind": "email"}]
    prepared = page_tests.prepare(venture_id, {"kind": "request", "fields": fields})
    if not is_local():
        page_tests.configure(venture_id, prepared["actions"][0]["confirmation"]["resulting_state"])
    set_policy("feature_flags", {"known_check_v1": True}, "test")
    assert known.known(venture_id, "request_fields")["value"] == fields
    reply = wizard_tests.setup(venture_id, {})
    assert "Choose two to four fields" not in reply["body_md"] and "edit this assumption" in reply["body_md"]
    if is_local():
        assert request_builds[-1]["page_test"]["request"]["fields"] == fields
        assert not any(action.get("confirmation") for action in reply["actions"])
    else:
        assert reply["actions"][0]["action"] == "experiment.page.configure"
        assert reply["actions"][0]["confirmation"]["resulting_state"]["fields"] == fields
    original = known.known
    for status in ("confirm", "conflict"):
        monkeypatch.setattr(known, "known", lambda *_args, **_kwargs: {"status": status, "value": fields,
            "source": "Your selected document", **({"values": [{"value": fields, "source": "Your first form"},
                {"value": [{**field, "label": field["label"] + " revised"} for field in fields], "source": "Your second form"}]} if status == "conflict" else {})})
        reply = wizard_tests.setup(venture_id, {})
        assert len(reply["actions"]) == (1 if status == "confirm" else 2)
        assert all(action["action"] == "experiment.request.setup" for action in reply["actions"])
    monkeypatch.setattr(known, "known", original)
    set_policy("feature_flags", {"known_check_v1": False}, "test")
    reply = wizard_tests.setup(venture_id, {})
    if is_local():
        assert request_builds[-1]["page_test"]["request"]["fields"] == fields
        assert not any(action.get("confirmation") for action in reply["actions"])
    else:
        assert "Choose two to four fields" in reply["body_md"]


@pytest.mark.parametrize("views,returns,hours,expected", [
    (50, 5, 25, "supported"), (50, 4, 25, "refuted"), (100, 5, 25, "refuted"),
    (50, 5, 23, "inconclusive"), (49, 5, 25, "inconclusive"),
])
def test_compound_request_results_ingest_the_same_belief_outcome(market, monkeypatch, views, returns, hours, expected):
    from app.conversion import waitlist
    from app.core.local_mode import is_local
    from app.db.supymem_models import Experiment, VentureWaitlistEntry
    from app.experiments import beliefs, page_tests, wizard_tests
    from app.state import db
    from core.clock import as_utc, utcnow

    _client, venture_id, workspace_id, view, landing, *_rest = market
    landing["enabled"] = True
    now = as_utc(utcnow())
    started = now - timedelta(hours=hours)
    fields = [{"key": "task", "label": view["icp"]["problem"], "kind": "short_text"},
              {"key": "address", "label": "Email address", "kind": "email"}]
    spec = wizard_tests.spec(venture_id, {"kind": "request", "fields": fields})
    if is_local():
        with db()() as session:
            page_tests._save_page_test(session, spec)
    else:
        prepared = page_tests.prepare(venture_id, {"kind": "request", "fields": fields})
        page_tests.configure(venture_id, prepared["actions"][0]["confirmation"]["resulting_state"])
    monkeypatch.setattr(wizard_tests, "utcnow", lambda: now)
    monkeypatch.setattr("core.clock.utcnow", lambda: now)
    monkeypatch.setattr(wizard_tests, "published_url", lambda *_args: "https://requests.example/")
    with db()() as session:
        session.get(Experiment, spec["id"]).started_at = started
        for index in range(10):
            requests = [{"experiment_id": spec["id"], "submitted_at": started.isoformat(), "id": f"req_{index}"}]
            if index < returns:
                requests.append({"experiment_id": spec["id"], "submitted_at": (started + timedelta(hours=24)).isoformat(),
                                 "id": f"req_return_{index}"})
            session.add(VentureWaitlistEntry(id=f"entry_{index}", venture_id=venture_id, workspace_id=workspace_id,
                email=f"reader{index}@request-mail.example", consent_version=waitlist.VERSION,
                consent_text="You may reply to my request.", is_test=False,
                props=json.dumps({"requests": requests}), created_at=started))
        session.commit()
    record(spec, "landing_view", views, path="/")
    observed = wizard_tests.observe(spec)
    updated = beliefs.update({"id": "belief_request", "evidence": []}, spec, observed["measured"], verdict=observed["outcome"])
    assert observed["outcome"] == expected
    assert updated["status"] == {"supported": "confirmed", "refuted": "disproven", "inconclusive": "weak"}[expected]
    assert updated["evidence"][0]["outcome"] == expected
    assert updated["evidence"][0]["events"] == views


def test_fulfillment_quotes_the_gmail_send_rate_without_authorizing_email(market, monkeypatch):
    from app.approvals import external
    from app.autopilot import outreach
    from app.conversion import waitlist
    from app.db.models import GtmOutreachSettings
    from app.db.supymem_models import VentureWaitlistEntry
    from app.experiments import wizard_tests
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.state import db
    from core.clock import utcnow

    _client, venture_id, workspace_id, view, *_rest = market
    with db()() as session:
        session.add(VentureWaitlistEntry(id="entry_quote", venture_id=venture_id, workspace_id=workspace_id,
            email="reader@request-mail.example", consent_version=waitlist.VERSION,
            consent_text="You may reply to my request.", is_test=False,
            props=json.dumps({"requests": [{"id": "req_quote", "experiment_id": "exp_quote", "state": "open",
                "submitted_at": utcnow().isoformat(), "values": {"task": view["icp"]["problem"]}}]}), created_at=utcnow()))
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=workspace_id, created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "64 Pine Road, Springfield, US"
        monkeypatch.setenv("SUPYMEM_SENDER_POSTAL_ADDRESS", settings.sender_postal_address)
        session.commit()
    quoted = []

    def quote(action, provider):
        quoted.append((action, provider))
        return {"credit_estimate_micro": 123}

    monkeypatch.setattr(external, "outbound_quote", quote)
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: pytest.fail("fulfillment must not send email"))
    reply = wizard_tests.fulfill(venture_id, {"entry_id": "entry_quote", "request_id": "req_quote",
                                           "reply": "You can now use " + view["icp"]["problem"]})
    assert quoted == [("gtm_email_send", "composio_gmail")]
    draft = outreach.drafts(venture_id)[0]
    assert draft["credit_ceiling_micro"] == 123 and draft["requires_batch_approval"]
    assert draft["state"] == "draft" and reply["actions"][0]["action"] == "outreach.send"
    assert OPT_OUT in draft["body"] and "64 Pine Road" in draft["body"]
    assert "I have your address from my own contacts." in draft["body"]


def test_request_and_market_belief_commands_keep_experiment_routing(market):
    from app.chat.intent import classify_intent, split_speech_acts

    _client, _venture_id, _workspace_id, view, *_rest = market
    for text in (
        "Show my requests",
        f"request form: short text={view['icp']['problem']}; email=Email address",
        f"reply to request req_pending: You can now help {view['icp']['who']}. Do not send anything yet.",
        f"Show my market beliefs for {view['icp']['who']}",
        f"Show my market map for {view['icp']['who']}",
        f"Test my beliefs about {view['icp']['problem']}",
    ):
        assert classify_intent(text) == "experiment_list", text
        assert split_speech_acts(text) == [("experiment_list", text)]


def test_request_form_offer_survives_empty_and_saved_experiment_lists(market, monkeypatch):
    from app.chat import experiments
    from app.experiments import autonomous, ledger

    _client, venture_id, _workspace_id, view, landing, *_rest = market
    landing["enabled"] = True
    monkeypatch.setattr(autonomous, "available", lambda _venture_id: [])
    with monkeypatch.context() as inactive:
        inactive.setattr(autonomous, "_snapshot", lambda _venture_id: None)
        empty = experiments.list_reply(venture_id)
    assert any(action.get("action") == "idea.review" for action in empty["actions"])
    assert empty["actions"] == [{"label": "Review my idea", "action": "idea.review", "style": "primary"}]
    experiment_ids = ledger.create_from_tests(venture_id, "validation", [{
        "hypothesis": f"{view['icp']['who']} need help with {view['icp']['problem']}",
        "action": "Recruit interviews",
    }])
    saved = experiments.list_reply(venture_id)
    assert "Tests I can run or prepare for you:" in saved["body_md"]
    assert any(action.get("action") == "experiment.request.prepare" for action in saved["actions"])
    assert {action["payload"]["experiment_id"] for action in saved["actions"]
            if action.get("action") == "experiment.recommendation.prepare"} == set(experiment_ids)
    assert "run yourself" not in saved["body_md"]
    assert "credits" not in saved["body_md"].lower()


def test_request_recommendation_and_belief_actions_keep_their_handlers(market, monkeypatch):
    from app.chat import experiments, market_beliefs
    from app.core.local_mode import is_local
    from app.experiments import wizard_tests

    _client, venture_id, _workspace_id, view, *_rest = market
    payload = {"audience": view["icp"]["who"], "problem": view["icp"]["problem"]}
    for action, handler in (
        ("experiment.request.prepare", "setup"),
        ("experiment.request.list", "list_reply"),
        ("experiment.request.publish", "publish"),
        ("experiment.request.reply", "prepare_reply"),
        ("experiment.request.fulfill", "fulfill"),
    ):
        monkeypatch.setattr(wizard_tests, handler,
            lambda received_venture, received_payload, action_name=action:
                {"action": action_name, "venture_id": received_venture, "payload": received_payload})
        assert asyncio.run(experiments.execute(venture_id, action, payload, None)) == {
            "action": action, "venture_id": venture_id, "payload": payload}
    monkeypatch.setattr(experiments, "review_test_prepare",
        lambda received_venture, text, received_payload:
            {"venture_id": received_venture, "text": text, "payload": received_payload})
    assert asyncio.run(experiments.execute(venture_id, "experiment.recommendation.prepare", payload, None)) == {
        "venture_id": venture_id, "text": "", "payload": payload}

    async def belief_execute(received_venture, action, received_payload):
        return {"action": action, "venture_id": received_venture, "payload": received_payload}

    monkeypatch.setattr(market_beliefs, "execute", belief_execute)
    if is_local():
        from app.core.local_secrets import update_settings

        update_settings("capabilities", {"run_for_me_experiments": True}, {})
    action = "experiment.beliefs.approve"
    assert asyncio.run(experiments.execute(venture_id, action, payload, None)) == {
        "action": action, "venture_id": venture_id, "payload": payload}


def test_request_content_does_not_trigger_market_belief_dispatch(market, monkeypatch, request_builds):
    from app.chat import experiments, market_beliefs
    from app.core.local_mode import is_local

    _client, venture_id, _workspace_id, view, landing, *_rest = market
    landing["enabled"] = True

    async def unexpected_beliefs(_venture_id):
        pytest.fail("Your request content must not start a market belief plan")

    monkeypatch.setattr(market_beliefs, "prepare", unexpected_beliefs)
    label = f"Market map for {view['icp']['who']}"
    text = f"request form: short text={label}; email=Email address"
    reply = asyncio.run(experiments.dispatch(venture_id, "experiment_list", text))
    if is_local():
        assert request_builds[-1]["page_test"]["request"]["fields"][0]["label"] == label
        assert not any(action.get("confirmation") for action in reply["actions"])
    else:
        card = reply["actions"][0]["confirmation"]
        assert card["action"] == "experiment.page.configure"
        assert card["resulting_state"]["fields"][0]["label"] == label
    with pytest.raises(ValueError, match="request_not_found"):
        asyncio.run(experiments.dispatch(venture_id, "experiment_list",
            f"reply to request req_missing: Your market map for {view['icp']['who']} is ready."))


def test_request_form_contract_preserves_legacy_wire_and_rejects_unsafe_forms(market):
    from app.conversion import waitlist
    from app.experiments import wizard_tests
    from core.static_qa_contracts import Contract, ManagedRequestForm

    _client, venture_id, _workspace_id, view, *_rest = market
    details = waitlist.public_details(venture_id)
    form = wizard_tests.RequestForm(fields=[
        {"key": "task", "label": view["icp"]["problem"], "kind": "short_text"},
        {"key": "address", "label": "Email address", "kind": "email"},
    ])
    html = wizard_tests.form_html(form, details["url"], "exp_contract", details["consent"], waitlist.VERSION)
    managed = ManagedRequestForm(html=html, target=details["url"])
    legacy = Contract(profile="static_landing_v1", product_name=view["icp"]["who"], required_text=[view["icp"]["problem"]])
    assert "managed_request_form" not in legacy.model_dump(mode="json")
    declared = legacy.model_copy(update={"managed_request_form": managed})
    assert Contract.model_validate(declared.model_dump(mode="json")).managed_request_form == managed
    assert declared.digest != legacy.digest
    for target in (details["url"].replace("https:", "http:", 1) if details["url"].startswith("https:") else "http://insecure.example/form",
                   details["url"].replace("://", "://reader@", 1), details["url"] + "?redirect=1"):
        with pytest.raises(ValueError):
            ManagedRequestForm(html=html.replace(details["url"], target), target=target)
    for unsafe in (
        html.replace('type="text"', 'type="file"', 1),
        html.replace(details["url"], "https://exfil.invalid/"),
        html.replace('id="request_submit"', 'id="request_submit" formaction="https://exfil.invalid/"'),
        html.replace('name="consent" value="yes" required', 'name="consent" value="yes"'),
        html.replace("min-width:24px", "background:url(https://exfil.invalid/)"),
        html.replace("</form>", '<script>alert("unsafe")</script></form>'),
    ):
        with pytest.raises(ValueError):
            ManagedRequestForm(html=unsafe, target=details["url"])


@pytest.mark.hosted
def test_hosted_requests_work_with_an_approved_v3_page_without_runtime_deploy(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.builder import qa_transport
    from app.conversion import waitlist
    from app.db.supymem_models import BuildRun, Deployment
    from app.experiments import page_tests, wizard_tests
    from app.pagebuilder import pipeline
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.qa import qa_contract
    from app.pagebuilder_admission import ORIGIN, TRIGGER, publication
    from app.state import db
    from core.clock import utcnow
    from core.static_qa_contracts import Input

    client, venture_id, workspace_id, view, landing, *_rest, app_id = market
    if is_local():
        from app.core.local_secrets import update_settings
        from secrets import token_urlsafe
        update_settings("publishing", {"provider": "vercel", "project": "fixture-request-form"}, {"token": token_urlsafe(24)})
        update_settings("capabilities", {"publish_pages": True}, {})
    landing["enabled"] = True
    details = waitlist.public_details(venture_id)
    inputs = PageInput(venture_id=venture_id, name=details["name"], idea=view["icp"]["problem"],
        waitlist_url=details["url"], waitlist_consent_text=details["consent"], waitlist_consent_version=waitlist.VERSION)
    contract = qa_contract(inputs, pipeline.fallback_plan(inputs))
    assert contract.case_contract_version == 3 and "managed_request_form" not in contract.model_dump(mode="json")
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64,
        size_bytes=100, manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=contract)
    monkeypatch.setattr(qa_transport, "request", lambda _binding, method, path, **_kwargs:
        json.dumps({"status": "ok", "profiles": ["static_landing_v1"], "case_contract_version": 3}).encode())
    monkeypatch.setattr(qa_transport, "state", lambda _binding: {"phase": "running",
        "command_id": request.command_id, "input_digest": request.digest})
    assert qa_transport.progress({}, request.model_dump(mode="json"), lambda: pytest.fail("source upload"),
        permit_execution=lambda: pytest.fail("execution"))["phase"] == "running"
    with db()() as session:
        run = BuildRun(id="br_hosted", app_id=app_id, venture_id=venture_id, workspace_id=workspace_id,
            trigger=TRIGGER, agent="pagebuilder", state="succeeded", artifact_id="artifact_hosted", request_digest="source")
        saved = {"source_origin": ORIGIN, "lifecycle_version": 3, "actor": "local",
            "source_import": {"identity": {"scope": {"caller_subject": "local"}, "hostname": "hosted.cohort.dev"}},
            "runtime": {"qa_compute_ceiling_micro": 1}, "inputs": {"pagebuilder_input": inputs.model_dump(mode="json")}}
        saved["publication_request"] = {"schema_version": 1, "binding": publication._binding(run, saved)}
        run.spec = json.dumps(saved)
        session.add(run)
        session.add(Deployment(id="dep_hosted", app_id=app_id, venture_id=venture_id, workspace_id=workspace_id,
            build_artifact_id=run.artifact_id, target="supymem_static", state="live", version=1,
            url="https://hosted.cohort.dev/", live_at=utcnow()))
        session.commit()
    fields = [{"key": "task", "label": view["icp"]["problem"], "kind": "short_text"},
        {"key": "address", "label": "Email address", "kind": "email"}]
    prepared = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.request.prepare", "payload": {"fields": fields}})
    assert prepared.status_code == 200, prepared.text
    configure = prepared.json()["actions"][0]["confirmation"]
    configured = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.page.configure",
        "payload": configure["resulting_state"], "message_id": prepared.json()["id"]})
    assert configured.status_code == 200, configured.text
    spec = wizard_tests.spec(venture_id, {"kind": "request", "fields": fields})
    path, query = f"/public/waitlists/{venture_id}", {"kind": "request", "experiment_id": spec["id"]}
    assert client.get(path, params=query).status_code == 404
    hosted = next(action["confirmation"] for action in configured.json()["actions"]
        if action.get("confirmation", {}).get("action") == "experiment.request.publish")
    assert ACTION_SLUG[hosted["action"]] == "experiments"
    assert wizard_tests.DISCLOSURE in " ".join(hosted["consequences"])
    with pytest.raises(ValueError, match="request_publish_changed"):
        wizard_tests.publish(venture_id, {**hosted["resulting_state"], "return_url": "https://other.cohort.dev/"})
    approved = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": hosted["action"],
        "payload": hosted["resulting_state"], "message_id": configured.json()["id"]})
    assert approved.status_code == 200, approved.text
    assert wizard_tests.public_form(venture_id, spec["id"])[0] == details["name"]
    assert client.get(path, params=query).status_code == 200
    payload = consent_payload(venture_id, "reader@cohort.dev", **query, request_task=view["icp"]["problem"])
    assert client.post(path, json={**payload, "consent": False}).status_code == 422
    assert client.post(path, json=payload).status_code == 201
    assert any(action.get("href", "").startswith(details["url"]) for action in page_tests.readout(venture_id,
        {"experiment_id": spec["id"]})["actions"])
    with db()() as session:
        assert page_tests.published_url(session, venture_id, workspace_id, spec["id"]) is None
        assert page_tests.published_url(session, venture_id, "other_workspace", None) is None
        run = session.get(BuildRun, "br_hosted")
        assert json.loads(run.spec)["inputs"]["pagebuilder_input"].get("page_test") is None
        session.get(Deployment, "dep_hosted").state = "deleted"
        session.commit()
    assert client.get(path, params=query).status_code == 404
    assert client.post(path, json=payload).status_code == 404


@pytest.mark.hosted
def test_wizard_form_requests_fulfillment_and_readout(market, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.autopilot import outreach
    from app.chat.intent import classify_intent, split_speech_acts
    from app.conversion import waitlist
    from app.db.models import GtmContact, GtmOutreachSettings, GtmSuppression
    from app.db.supymem_models import AppEvent, BuildRun, Deployment, VentureWaitlistEntry
    from app.experiments import page_tests, wizard_tests
    from app.gtm.relationships.email_compliance import OPT_OUT
    from app.pagebuilder.qa import check_page, qa_contract
    from app.pagebuilder.models import PageInput, PageTest
    from app.pagebuilder import pipeline
    from app.pagebuilder.qa import PageParser
    from app.pagebuilder_admission import ORIGIN, TRIGGER, publication
    from app.state import db
    from core.clock import as_utc, utcnow
    from core.static_qa_contracts import CASE_CONTRACT_VERSION, Contract, ManagedRequestForm

    client, venture_id, workspace_id, view, landing, *_rest = market
    landing["enabled"] = True
    now = as_utc(utcnow())
    monkeypatch.setattr(wizard_tests, "utcnow", lambda: now)
    fields = [{"key": "task", "label": view["icp"]["problem"] + ' <img src=x>', "kind": "short_text"},
        {"key": "details", "label": "Details", "kind": "long_text"},
        {"key": "address", "label": "Email address", "kind": "email"},
        {"key": "reference", "label": "Reference", "kind": "file_link"}]
    for invalid in (fields[:2], fields + [fields[0]], [fields[2], fields[3]], [fields[2], fields[2]]):
        with pytest.raises(ValueError):
            wizard_tests.RequestForm.model_validate({"fields": invalid})
    assert "two to four" in wizard_tests.setup(venture_id, {})["body_md"]
    assert classify_intent("Show requests") == "experiment_list"
    typed = "request form: short text=Task; email=Email address"
    assert split_speech_acts(typed) == [("experiment_list", typed)]
    typed_response = client.post(f"/ventures/{venture_id}/chat", json={"message": typed})
    assert typed_response.status_code == 200 and typed_response.json()["kind"] == "confirmation", typed_response.text
    whole_reply = "reply to request req_pending: Your task is done. Update your notes with this result. Do not send anything yet."
    assert split_speech_acts(whole_reply) == [("experiment_list", whole_reply)]
    prepared = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.request.prepare", "payload": {"fields": fields}})
    assert prepared.status_code == 200, prepared.text
    card = prepared.json()["actions"][0]["confirmation"]
    assert wizard_tests.DISCLOSURE in " ".join(card["consequences"])
    assert not any("Hypothesis:" in line or "short_text" in line for line in card["consequences"])
    assert "Minimum sample:" in " ".join(card["consequences"])
    assert "Nothing is published" in " ".join(card["consequences"])
    assert card["resulting_state"]["fields"] == fields
    confirmed = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.page.configure",
        "payload": card["resulting_state"], "message_id": prepared.json()["id"]})
    assert confirmed.status_code == 200, confirmed.text
    now = as_utc(utcnow())
    spec = wizard_tests.spec(venture_id, {"kind": "request", "fields": fields})
    legacy = PageTest(experiment_id=spec["id"], prices=[{"key": "one", "label": view["icp"]["who"], "price": "USD 12 per month"}])
    assert "request" not in legacy.model_dump(mode="json")
    assert spec["catalog_key"] == "E30_wizard_of_oz"
    assert spec["id"] in {item["id"] for item in page_tests.available(spec)}
    assert all(ACTION_SLUG[name] == "experiments" for name in (
        "experiment.request.prepare", "experiment.request.list", "experiment.request.reply", "experiment.request.fulfill"))
    from tests.unit import test_page_experiments as page_checks
    original_render = page_checks.rendered

    def rendered(html, files):
        result = original_render(html, files)
        parser = PageParser()
        parser.feed(html)
        fields = [{"id": attrs.get("id"), "enabled": True} for tag, attrs, _parents in parser.elements
            if tag in {"input", "textarea", "button"} and attrs.get("type") != "hidden"]
        for observation in result["observations"].values():
            observation["visibleControls"] = observation["visibleControls"] + fields
        return result

    monkeypatch.setattr(page_checks, "rendered", rendered)
    details = waitlist.public_details(venture_id)
    inputs = PageInput(venture_id=venture_id, name=details["name"], idea=view["icp"]["problem"],
        waitlist_url=details["url"], waitlist_consent_text=details["consent"], waitlist_consent_version=waitlist.VERSION,
        page_test=spec["runner_params"]["page_test"])
    legacy_plan = pipeline.fallback_plan(inputs.model_copy(update={"page_test": legacy}))
    assert next(section for section in legacy_plan.sections if section.id == "pricing").body == page_tests.DISCLOSURE
    assert all(section.id != "request" for section in legacy_plan.sections)
    plan = pipeline.fallback_plan(inputs)
    monkeypatch.setattr(pipeline, "_prepare_grounded_plan", lambda *_args: (plan, 0, [], {"reviews": []}))
    page = pipeline.generate_page(inputs, lambda *_args: pipeline.fallback_html(plan, inputs), render=rendered)
    assert wizard_tests.rendered_form_failures(inputs, {"visibleControls": []})
    html = page.files["index.html"]
    assert wizard_tests.DISCLOSURE in html and escape(fields[0]["label"]) in html
    assert fields[0]["label"] not in html and 'type="file"' not in html
    assert 'name="consent" value="yes" required' in html and 'maxlength="1000"' in html
    assert html.count('id="request_form"') == 1
    parsed = PageParser()
    parsed.feed(html)
    hidden = {attrs["name"]: attrs["value"] for tag, attrs, _parents in parsed.elements if tag == "input" and attrs.get("type") == "hidden"}
    assert hidden["consent_version"] == waitlist.VERSION
    contract = qa_contract(inputs, page.plan)
    assert CASE_CONTRACT_VERSION == 4
    assert Contract.model_validate(contract.model_dump(mode="json")).managed_request_form.html == wizard_tests.native_form(inputs)
    with pytest.raises(ValueError):
        ManagedRequestForm(html=wizard_tests.native_form(inputs).replace('type="text"', 'type="file"', 1), target=inputs.waitlist_url)
    assert check_page(html.replace('name="consent"', 'name="not_consent"'), page.files, inputs, page.plan)["passed"] is False
    assert check_page(html.replace('</main>', '<form action="https://exfil.invalid/"></form></main>'),
        page.files, inputs, page.plan)["passed"] is False
    path = f"/public/waitlists/{venture_id}"
    query = {"kind": "request", "experiment_id": spec["id"]}
    payload = consent_payload(venture_id, "reader0@cohort.dev", **query,
        request_task=view["icp"]["problem"], request_details='\n  <script>alert("input")</script> ![image](https://exfil.invalid/image.png)\n', request_reference="https://files.cohort.dev/ref")
    assert client.get(path, params=query).status_code == 404
    assert client.post(path, json=payload).status_code == 404
    with db()() as session:
        run = BuildRun(id="br_wizard", app_id=spec["app_id"], venture_id=venture_id, workspace_id=workspace_id,
            trigger=TRIGGER, agent="pagebuilder", state="succeeded", artifact_id="artifact_wizard", request_digest="source")
        saved = {"source_origin": ORIGIN, "lifecycle_version": 3, "actor": "local",
            "source_import": {"identity": {"scope": {"caller_subject": "local"}, "hostname": "request.cohort.dev"}},
            "runtime": {"qa_compute_ceiling_micro": 1}, "inputs": {"pagebuilder_input": inputs.model_dump(mode="json")}}
        saved["publication_request"] = {"schema_version": 1, "binding": publication._binding(run, saved)}
        run.spec = json.dumps(saved)
        session.add(run)
        session.add(Deployment(id="dep_wizard", app_id=spec["app_id"], venture_id=venture_id, workspace_id=workspace_id,
            build_artifact_id=run.artifact_id, target="supymem_static", state="live", version=1,
            url="https://request.cohort.dev/", live_at=utcnow()))
        session.commit()
    assert client.get(path, params=query).status_code == 200
    for invalid in ({**payload, "consent": False}, {key: value for key, value in payload.items() if key != "consent"},
        {**payload, "consent_digest": "changed"}, {**payload, "request_task": "x" * 201},
        {**payload, "request_details": "x" * 1001}, {**payload, "request_reference": "javascript:alert(1)"},
        {**payload, "request_reference": "https://user:secret@files.cohort.dev/ref"}, {**payload, "file": "upload"}):
        response = client.post(path, json=invalid)
        assert response.status_code == 422, response.text
    assert client.post(path, json={**payload, "request_details": "x" * 5000}).status_code == 413
    assert client.post(path, content=json.dumps({**payload, "request_details": "\ud800"}),
        headers={"Content-Type": "application/json"}).status_code == 422
    assert client.post(path, content="consent=yes&consent=yes", headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code == 422
    first = client.post(path, json=payload)
    assert first.status_code == 201, first.text
    assert set(first.json()) == {"received", "email_verified", "return_url"}
    assert client.post(path, json=payload).status_code == 429
    monkeypatch.setattr(wizard_tests, "RATE_PER_MINUTE", 2)
    assert client.post(path, json={**payload, "email": "reader1@cohort.dev"}).status_code == 201
    assert client.post(path, json={**payload, "email": "reader2@cohort.dev"}).status_code == 429
    monkeypatch.setattr(wizard_tests, "RATE_PER_MINUTE", 20)
    now += timedelta(minutes=2)
    for index in range(2, 10):
        wizard_tests.capture(venture_id, {**payload, "email": f"reader{index}@cohort.dev"})
    now += timedelta(minutes=2)
    for index in range(5):
        wizard_tests.capture(venture_id, {**payload, "email": f"reader{index}@cohort.dev"})
    items = wizard_tests.requests(venture_id, workspace_id)
    assert len(items) == 15 and len({item["entry_id"] for item in items}) == 10
    assert items[0]["values"]["details"] == payload["request_details"]
    monkeypatch.setattr(wizard_tests, "MAX_REQUESTS", 2)
    with pytest.raises(waitlist.WaitlistError, match="request_capacity_reached"):
        wizard_tests.capture(venture_id, payload)
    monkeypatch.setattr(wizard_tests, "MAX_REQUESTS", 20)
    monkeypatch.setattr(wizard_tests, "MAX_ENTRIES", 10)
    with pytest.raises(waitlist.WaitlistError, match="request_capacity_reached"):
        wizard_tests.capture(venture_id, {**payload, "email": "reader99@cohort.dev"})
    monkeypatch.setattr(wizard_tests, "MAX_ENTRIES", 1000)
    with pytest.raises(waitlist.WaitlistError):
        wizard_tests.requests(venture_id, "different_workspace")
    listed = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.request.list", "payload": {}})
    assert listed.status_code == 200 and "&lt;script&gt;" in listed.json()["body_md"]
    assert '<script>' not in listed.json()["body_md"]
    assert '![image](' not in listed.json()["body_md"]
    assert wizard_tests.chat(venture_id, "Show requests")["body_md"].startswith("Your requests:")
    with monkeypatch.context() as pagination:
        page_items = [{**items[0], "id": f"req_page_{index:02d}"} for index in range(25)]
        pagination.setattr(wizard_tests, "requests", lambda *_args: page_items)
        first_page = wizard_tests.list_reply(venture_id, {})
        next_page = first_page["actions"][-1]
        assert next_page["label"] == "Show older requests" and len(first_page["actions"]) == 21
        assert len(wizard_tests.list_reply(venture_id, next_page["payload"])["actions"]) == 5
    with db()() as session:
        entry = session.get(VentureWaitlistEntry, items[0]["entry_id"])
        assert json.loads(entry.props)["kind"] == "request"
        assert entry.consent_text and entry.consent_version == waitlist.VERSION
        run = session.get(BuildRun, "br_wizard")
        saved["publication_request"]["binding"]["hostname"] = "not_approved.cohort.dev"
        run.spec = json.dumps(saved)
        session.commit()
    assert client.post(path, json={**payload, "email": "reader11@cohort.dev"}).status_code == 404
    with db()() as session:
        run = session.get(BuildRun, "br_wizard")
        saved["publication_request"]["binding"]["hostname"] = "request.cohort.dev"
        run.spec = json.dumps(saved)
        session.commit()
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=workspace_id, created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "48 Cedar Street, Springfield, US"
        monkeypatch.setenv("SUPYMEM_SENDER_POSTAL_ADDRESS", settings.sender_postal_address)
        session.commit()
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *args: sent.append(args))
    item = next(item for item in items if item["email"] == "reader0@cohort.dev")
    reply_payload = {"entry_id": item["entry_id"], "request_id": item["id"], "reply": "Your request has been handled."}
    with pytest.raises(ValueError):
        wizard_tests.fulfill(venture_id, {**reply_payload, "workspace_id": "different_workspace"})
    reply = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.request.reply", "payload": reply_payload})
    assert reply.status_code == 200, reply.text
    approved = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.request.fulfill",
        "payload": reply.json()["actions"][0]["confirmation"]["resulting_state"], "message_id": reply.json()["id"]})
    assert approved.status_code == 200, approved.text
    assert not sent and approved.json()["actions"][0]["action"] == "outreach.send"
    drafts = outreach.drafts(venture_id)
    assert len(drafts) == 1 and drafts[0]["requires_batch_approval"] and drafts[0]["one_off"]
    assert OPT_OUT in drafts[0]["body"] and "48 Cedar Street" in drafts[0]["body"]
    assert "I have your address from my own contacts." in drafts[0]["body"]
    with db()() as session:
        contact = session.get(GtmContact, drafts[0]["contact_id"])
        assert as_utc(contact.lawful_basis_at).isoformat() == max(request["submitted_at"] for request in items if request["email"] == item["email"])
    assert next(item for item in wizard_tests.requests(venture_id, workspace_id) if item["id"] == reply_payload["request_id"])["state"] == "fulfilled"
    wizard_tests.fulfill(venture_id, reply_payload)
    assert len(outreach.drafts(venture_id)) == 1 and not sent
    suppressed = next(item for item in items if item["email"] == "reader1@cohort.dev")
    with db()() as session:
        session.add(GtmSuppression(id="wizard_suppression", workspace_id=workspace_id, email=suppressed["email"],
            reason="opt_out", source="opt_out", created_at=utcnow()))
        session.commit()
    with pytest.raises(ValueError, match="request_email_ineligible"):
        wizard_tests.fulfill(venture_id, {"entry_id": suppressed["entry_id"], "request_id": suppressed["id"], "reply": "Handled."})
    wizard_tests.fulfill(venture_id, {"entry_id": suppressed["entry_id"], "request_id": suppressed["id"]})
    assert not sent
    monkeypatch.setattr("core.clock.utcnow", lambda: now)
    record(spec, "landing_view", 75, path="/")
    record(spec, "landing_view", 5, path=path)
    with db()() as session:
        session.add(AppEvent(app_id=spec["app_id"], workspace_id="different_workspace", ts=utcnow(), name="landing_view",
            props=json.dumps({"experiment_id": spec["id"], "path": "/"})))
        session.commit()
    result = page_tests.observe(spec)
    assert result["outcome"] == "inconclusive" and result["measured"]["landing_views"] == 75
    now += timedelta(hours=25)
    result = page_tests.observe(spec)
    assert result["outcome"] == "supported" and result["measured"]["submit_rate"] == .2
    assert result["measured"]["return_rate"] == .5 and result["measured"]["submissions"] == 15
    token = parse_qs(urlsplit(first.json()["return_url"]).query)["return_token"][0]
    assert wizard_tests.return_visit(venture_id, spec["id"], token) == "https://request.cohort.dev/"
    with pytest.raises(ValueError):
        wizard_tests.return_visit(venture_id, spec["id"], "invalid")
    record(spec, "landing_view", 1, path="/")
    assert page_tests.observe(spec)["outcome"] == "refuted"
    readout = page_tests.readout(venture_id, {"experiment_id": spec["id"]})
    assert "15/76" in readout["body_md"]
    assert any(action["label"] == "Share your hosted request form" for action in readout["actions"])
    assert client.get(path, params=query).status_code == 200
    assert client.post(path, json={**payload, "email": "reader99@cohort.dev"}).status_code == 201


@pytest.mark.parametrize("entry", ["setup", "publish"])
@pytest.mark.parametrize("connected", [False, True], ids=["connect-first", "owned-tool"])
def test_local_request_entry_uses_owned_tool(market, monkeypatch, entry, connected):
    from app.build import device_orders
    from app.core import local_owner
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests, wizard_tests
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
    monkeypatch.setattr(page_tests, "_connected_provider", lambda *_: "codex" if connected else None, raising=False)
    market[4]["enabled"] = True
    fields = [{"key": "task", "label": market[3]["icp"]["problem"], "kind": "short_text"},
        {"key": "email", "label": "Your email", "kind": "email"}]
    payload = {"fields": fields}
    spec = wizard_tests.spec(market[1], {"kind": "request", **payload})
    if entry == "publish":
        with db()() as session:
            session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2], kind="market_test",
                hypothesis=spec["hypothesis"], state="proposed", context=json.dumps({"spec": spec, "page_test": True,
                    "prepared_at": utcnow().isoformat()})))
            session.commit()
        monkeypatch.setattr(page_tests, "published_url", lambda *_: "https://owned-site.example/")
        payload = {"experiment_id": spec["id"], "return_url": "https://owned-site.example/"}
    calls = []

    def prepare_order(venture_id, caller, instruction, **kwargs):
        calls.append(page_tests.coding_brief(venture_id, kwargs["experiment_id"]))
        return {"id": "dwo_request", "state": "approved"}

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare_order)
    reply = getattr(wizard_tests, entry)(market[1], payload)
    if connected:
        assert len(calls) == 1 and calls[0]["submission"]["request_kind"] == "request"
        assert calls[0]["page_test"]["request"]["fields"] == fields
    else:
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"] and not calls
        if entry == "setup":
            assert "Settings > Keys and connections > Coding tools" in wizard_tests.setup(market[1], {})["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        assert row is None or not json.loads(row.context).get("hosted_request_url")
