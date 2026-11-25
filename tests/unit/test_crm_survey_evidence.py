"""Connected evidence stays venture-scoped, read-only, and honest in two markets."""
import asyncio
import json
import re
from contextlib import contextmanager
from copy import deepcopy
from types import SimpleNamespace

import pytest

from tests.unit.test_composio_toolkit_catalog import DOMAINS, scopes


@pytest.fixture(params=DOMAINS)
def evidence_case(scopes, monkeypatch, request):
    from app.connections import crm
    from app.content import composio_social
    from app.credits import metered, policy
    from app.db.models import ContentOAuthState, GtmContact, GtmTouch, Person
    from app.experiments import ledger
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow

    scope = next(scope for scope in scopes if scope["domain"] == request.param)
    other = next(candidate for candidate in scopes if candidate != scope)
    domain = ("warehousegrid.example", "choirhouse.example")[scopes.index(scope)]
    now = utcnow()
    state = {"scope": scope, "other": other, "flag": True, "active": True, "calls": [], "meters": [],
             "stage": "conversation", "fail": False, "catalog": {}, "responses": {}, "contact_pages": {}}
    monkeypatch.setattr(policy, "get_policy", lambda _key: {"experiments_autopilot_v1": state["flag"]})
    monkeypatch.setattr(ledger, "_invalidate", lambda _venture_id: None)

    @contextmanager
    def charge(**kwargs):
        state["meters"].append(kwargs)
        yield SimpleNamespace(record_micro=lambda _amount: None)

    monkeypatch.setattr(metered, "charge_sync", charge)
    for platform in ("hubspot", "typeform"):
        monkeypatch.setenv(f"COMPOSIO_{platform.upper()}_AUTH_CONFIG_ID", "ac_" + platform)
        monkeypatch.setenv(f"COMPOSIO_{platform.upper()}_TOOLKIT_VERSION", "20261026_00")
    with db()() as session:
        session.get(Person, scope["owner"]).email = "founder@" + domain
        for platform in ("hubspot", "typeform"):
            session.add(ContentOAuthState(id="oauth_" + platform, workspace_id=scope["workspace"], platform=platform,
                redirect_uri="https://api.example.test/callback", auth_method="composio", actor_subject=scope["owner"],
                provider_user_id=scope["owner"], auth_config_id="ac_" + platform, connected_account_id="ca_" + platform,
                outcome="connected", created_at=now, expires_at=now, consumed_at=now))
        contacts = [("known", "one@" + domain, f"venture:{scope['venture']}:public", scope["workspace"]),
                    ("yourself", "founder@" + domain, f"venture:{scope['venture']}:public", scope["workspace"]),
                    ("consumer", "your.customer@gmail.com", f"venture:{scope['venture']}:public", scope["workspace"]),
                    ("imported", "imported@othercompany.example", "founder_import", scope["workspace"]),
                    ("simulated", "simulated@simulated.example", "founder_import", scope["workspace"]),
                    ("foreign", "foreign@foreign.example", f"venture:{other['venture']}:public", scope["workspace"]),
                    ("foreign_workspace", "foreign@workspace.example", f"venture:{scope['venture']}:public", other["workspace"])]
        for identifier, email, source, workspace in contacts:
            session.add(GtmContact(id=identifier, workspace_id=workspace, email=email, source=source,
                stage_changed_at=now, created_at=now))
        for identifier, simulated in (("imported", 0), ("simulated", 1)):
            session.add(GtmTouch(id="touch_" + identifier, workspace_id=scope["workspace"], venture_id=scope["venture"],
                contact_id=identifier, direction="out", simulated=simulated, sent_at=now, created_at=now))
        session.commit()
    state["experiment"] = ledger.create_from_tests(scope["venture"], "chat", [{"hypothesis": scope["domain"]}])[0]
    state["contact_pages"] = {
        None: {"results": [{"id": "remote_known", "properties": {"email": "ONE@" + domain}},
                           {"id": "remote_same_domain", "properties": {"email": "two@" + domain}}],
               "paging": {"next": {"after": "next_contacts"}}},
        "next_contacts": {"results": [
            {"id": "remote_yourself", "properties": {"email": "founder@" + domain}},
            {"id": "remote_invalid", "properties": {"email": domain}},
            {"id": "remote_consumer", "properties": {"email": "your.customer@gmail.com"}},
            {"id": "remote_imported", "properties": {"email": "imported@othercompany.example"}},
            {"id": "remote_foreign", "properties": {"email": "private@foreign.example"}},
            {"id": "remote_free_mail", "properties": {"email": "unrelated@gmail.com"}},
            {"id": "remote_simulated", "properties": {"email": "simulated@simulated.example"}},
            {"id": "remote_other_workspace", "properties": {"email": "foreign@workspace.example"}}]},
    }

    def schema(properties, required=()):
        return {"type": "object", "properties": properties, "required": list(required)}

    state["catalog"] = {
        "hubspot": [
            {"slug": "HUBSPOT_RETRIEVE_CONTACTS", "input_parameters": schema({"limit": {"type": "integer"},
                "properties": {"type": "array"}, "after": {"type": "string"}})},
            {"slug": "HUBSPOT_RETRIEVE_PIPELINES", "input_parameters": schema({"object_type": {"type": "string"}})},
            {"slug": "HUBSPOT_RETRIEVE_DEALS", "input_parameters": schema({"limit": {"type": "integer"},
                "properties": {"type": "array"}, "associations": {"type": "array"}, "after": {"type": "string"}})},
            {"slug": "HUBSPOT_UPDATE_DEAL", "input_parameters": schema({})}],
        "typeform": [{"slug": "TYPEFORM_GET_FORM_RESPONSES", "input_parameters": schema({
            "form_id": {"type": "string"}, "page_size": {"type": "integer"},
            "response_type": {"type": "string"}}, ("form_id",))},
            {"slug": "TYPEFORM_CREATE_FORM", "input_parameters": schema({})}],
    }
    state["responses"]["form_one"] = {"total_items": 2, "page_count": 1, "items": [
        {"response_id": "response_one", "submitted_at": now.isoformat(), "hidden": {"email": "private@example.test"},
         "answers": [{"field": {"id": "question"}, "type": "text", "text": scope["domain"]},
                     {"field": {"id": "payment"}, "type": "number", "number": 5},
                     {"field": {"id": "quote"}, "type": "text", "text": "The founder mentioned " + scope["domain"]},
                     {"field": {"id": "other"}, "type": "choice", "choice": {"label": "Other", "other": scope["domain"]}},
                     {"field": {"id": "many"}, "type": "choices", "choices": {"labels": ["First"], "other": scope["domain"]}},
                     {"field": {"id": "contact"}, "type": "email", "email": "private@example.test"}]},
        {"response_id": "response_partial", "submitted_at": None, "answers": []}]}

    def request_provider(method, path, **kwargs):
        state["calls"].append((method, path, kwargs))
        if path == "/tools":
            assert method == "GET"
            return {"items": deepcopy(state["catalog"][kwargs["params"]["toolkit_slug"]])}
        if path.startswith("/connected_accounts/ca_"):
            platform = path.removeprefix("/connected_accounts/ca_")
            assert method == "GET"
            return {"id": "ca_" + platform, "user_id": scope["owner"], "status": "ACTIVE" if state["active"] else "EXPIRED",
                    "toolkit": {"slug": platform}, "auth_config": {"id": "ac_" + platform}}
        if path == "/connected_accounts/link":
            assert method == "POST"
            return {"connected_account_id": "ca_new", "redirect_url": "https://connect.composio.dev/test"}
        raise AssertionError((method, path))

    def execute(slug, **kwargs):
        state["calls"].append(("EXECUTE", slug, deepcopy(kwargs)))
        assert kwargs["user_id"] == scope["owner"] and kwargs["toolkit_version"] == "20261026_00"
        assert any(re.fullmatch(pattern, slug) for pattern in crm.READS.values())
        if state["fail"]:
            raise RuntimeError("private provider failure")
        arguments = kwargs["arguments"]
        if slug == "HUBSPOT_RETRIEVE_CONTACTS":
            assert kwargs["connected_account_id"] == "ca_hubspot"
            data = state["contact_pages"][arguments.get("after")]
        elif slug == "HUBSPOT_RETRIEVE_PIPELINES":
            assert arguments["object_type"] == "deals"
            data = {"results": [{"id": "sales", "stages": [
                {"id": "conversation", "label": "Conversation", "displayOrder": 0},
                {"id": "evaluation", "label": "Evaluation", "displayOrder": 1},
                {"id": "won", "label": "Closed won", "displayOrder": 2,
                 "metadata": {"isClosed": "true", "probability": "1.0"}},
                {"id": "lost", "label": "Closed lost", "displayOrder": 3,
                 "metadata": {"isClosed": "true", "probability": "0.0000"}}]}]}
        elif slug == "HUBSPOT_RETRIEVE_DEALS":
            assert arguments["associations"] == ["contacts"]
            data = {"results": [{"id": "deal_" + identifier,
                "properties": {"pipeline": "sales", "dealstage": state["stage"]},
                "associations": {"contacts": {"results": [{"id": identifier}]}}}
                for identifier in ("remote_known", "remote_consumer", "remote_foreign", "remote_free_mail", "remote_simulated",
                                   "remote_yourself", "remote_invalid")]}
        else:
            assert slug == "TYPEFORM_GET_FORM_RESPONSES" and kwargs["connected_account_id"] == "ca_typeform"
            assert arguments["page_size"] == 1000 and arguments["response_type"] == "completed"
            data = state["responses"][arguments["form_id"]]
        return {"successful": True, "data": deepcopy(data)}

    monkeypatch.setattr(composio_gmail, "_request", request_provider)
    monkeypatch.setattr(composio_social, "_request", request_provider)
    monkeypatch.setattr(composio_gmail, "execute_tool", execute)
    return state


def test_local_crm_reads_use_connected_apps_instead_of_hosted_flags(evidence_case, monkeypatch):
    from app.connections import crm, local_config
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda name: name == "connected_apps", raising=False)
    update_settings("composio", {"auth_configs": {"hubspot": "ac_hubspot"}}, {"api_key": "saved-test-key"})
    evidence_case["flag"] = False
    reply = crm.snapshot(evidence_case["scope"]["venture"])
    assert reply["status"] == "connected" and reply["contact_count"] == 4
    monkeypatch.setattr(local_config, "enabled", lambda name: False)
    blocked = crm.snapshot(evidence_case["scope"]["venture"])
    assert blocked["status"] == "unavailable"
    assert blocked["message"] == local_config.setup_message("hubspot")


def test_crm_scopes_contacts_domains_and_stage_progress_without_claiming_payments(evidence_case):
    from app.chat import experiments
    from app.connections import crm
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    first = crm.snapshot(venture)
    assert (first["contact_count"], first["deal_count"], first["progress_count"], first["strength"]) == (4, 2, 0, "facts")
    assert first["stage_counts"] == {"Conversation": 2}
    ledger.record_result(case["experiment"], venture, True, "I ran the test. No results yet", "refuted", "Try another route")
    case["stage"] = "evaluation"
    state = ledger.loop_state(venture)
    assert state["crm"]["strength"] == "commitment" and state["crm"]["progress_count"] == 2
    assert not state["ready"] and state["failed"]
    chain = experiments._evidence_chain(venture, state)
    assert chain[0]["strength"] == "commitment" and chain[0]["scope"] == "venture_contacts"
    assert "private@" not in json.dumps(chain) and "cash" not in json.dumps(chain)
    reply = asyncio.run(experiments.review_reply(venture, state))
    assert "2 deals moved forward" in reply["body_md"] and "A deal is not a payment" in reply["body_md"]
    assert "commitment" not in reply["body_md"] and "facts" not in reply["body_md"] and "—" not in reply["body_md"]
    payload = experiments._decision_payload(venture, "experiment.persevere", state)
    assert experiments._decision_payload(venture, "experiment.persevere", ledger.loop_state(venture)) == payload
    case["stage"] = "lost"
    assert crm.snapshot(venture)["progress_count"] == 0
    with db()() as session:
        assert session.get(Experiment, case["experiment"]).outcome == "refuted"
    assert all(meter["payer"] == "founder" and meter["provider"] == "composio" for meter in case["meters"])
    assert all(call[0] in {"GET", "EXECUTE"} for call in case["calls"])


@pytest.mark.parametrize("platform", ("hubspot", "typeform"))
def test_readers_require_exact_flag_owner_and_active_connection(evidence_case, platform):
    from app.connections import crm, surveys
    from app.db.models import ContentOAuthState
    from app.experiments import ledger
    from app.state import db

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    surveys.link_form(venture, case["experiment"], "form_one", case["scope"]["owner"])
    reader = crm.snapshot if platform == "hubspot" else surveys.snapshot
    for flag in (False, "true", 1, None):
        case["flag"] = flag
        assert reader(venture) is None and not case["calls"]
    case["flag"] = True
    for attribute, value in (("provider_user_id", case["other"]["owner"]),
                             ("actor_subject", case["other"]["owner"]), ("outcome", "revoked")):
        with db()() as session:
            row = session.get(ContentOAuthState, "oauth_" + platform)
            original = getattr(row, attribute)
            setattr(row, attribute, value)
            session.commit()
        result = reader(venture)
        assert result["status"] == "not_connected" and result["action"]["action"] == "connections.connect"
        assert result["action"]["label"] == "Connect " + crm.NAMES[platform]
        assert not case["calls"]
        with db()() as session:
            setattr(session.get(ContentOAuthState, "oauth_" + platform), attribute, original)
            session.commit()
    case["active"] = False
    assert reader(venture)["status"] == "not_connected"
    assert not any(call[0] == "EXECUTE" for call in case["calls"])
    case["calls"].clear()
    case["flag"] = False
    assert "crm" not in ledger.loop_state(venture) and not case["calls"]


def test_crm_repeated_pagination_does_not_claim_partial_totals(evidence_case):
    from app.connections import crm
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    case = evidence_case
    case["contact_pages"]["next_contacts"] = case["contact_pages"][None]
    assert crm.snapshot(case["scope"]["venture"]) == {"status": "unavailable"}
    with db()() as session:
        assert "crm_snapshot" not in ledger._ctx(session.get(Experiment, case["experiment"]))


def test_survey_answers_are_scoped_facts_not_a_success_verdict(evidence_case):
    from app.chat import experiments
    from app.connections import surveys
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    surveys.link_form(venture, case["experiment"], "https://form.typeform.com/to/form_one", case["scope"]["owner"])
    with db()() as session:
        for identifier, owner, direction in (("exp_foreign", case["other"]["venture"], ""),
                                              ("exp_old", venture, "old_direction")):
            session.add(Experiment(id=identifier, venture_id=owner, workspace_id=case["scope"]["workspace"],
                kind=ledger.KIND, context=json.dumps({"direction": direction, "typeform_form_id": "never_read"})))
        session.commit()
    state = ledger.loop_state(venture)
    survey = state["surveys"]["experiments"][0]
    assert state["surveys"]["strength"] == "facts" and survey["response_count"] == 1
    assert survey["responses"][0]["answers"] == [
        {"field_id": "question", "value": case["scope"]["domain"]}, {"field_id": "payment", "value": 5},
        {"field_id": "quote", "value": "The founder mentioned " + case["scope"]["domain"]},
        {"field_id": "other", "value": case["scope"]["domain"]},
        {"field_id": "many", "value": ["First", case["scope"]["domain"]]}]
    chain = experiments._evidence_chain(venture, state)
    item = next(item for item in chain if item.get("connection") == "typeform")
    assert item["strength"] == "facts" and item["outcome"] == "inconclusive"
    assert item["experiment_id"] == case["experiment"] and not state["done"] and not state["supported"]
    assert "private@example.test" not in json.dumps(chain) and "never_read" not in json.dumps(case["calls"])
    reply = asyncio.run(experiments.review_reply(venture, state))
    assert "1 completed response" in reply["body_md"] and "not whether they will buy" in reply["body_md"]
    assert "facts" not in reply["body_md"] and "—" not in reply["body_md"]


def test_survey_link_action_only_updates_owned_local_experiment(evidence_case):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import actions, experiments
    from app.connections import surveys
    from app.db.models import ContentOAuthState
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    with db()() as session:
        session.get(ContentOAuthState, "oauth_typeform").outcome = "revoked"
        session.commit()
    payload = {"experiment_id": case["experiment"], "form_url": "https://form.typeform.com/to/form_one"}
    case["flag"] = False
    reply = asyncio.run(experiments.execute(venture, "experiment.survey.link", payload, case["scope"]["owner"]))
    assert "Nothing was changed" in reply["body_md"] and not case["calls"]
    case["flag"] = True
    for target, actor in ((case["other"]["venture"], case["scope"]["owner"]), (venture, case["other"]["owner"])):
        with pytest.raises(ValueError, match="survey_experiment_invalid"):
            surveys.link_form(target, case["experiment"], "form_one", actor)
    for invalid in ("https://form.typeform.com.evil.example/to/form_one", "https://you@form.typeform.com/to/form_one",
                    "https://form.typeform.com/to/form_one/extra", "http://form.typeform.com/to/form_one", "../form_one"):
        with pytest.raises(ValueError, match="survey_form_invalid"):
            surveys.link_form(venture, case["experiment"], invalid, case["scope"]["owner"])
    reply = asyncio.run(experiments.execute(venture, "experiment.survey.link", payload, case["scope"]["owner"]))
    assert reply["actions"] == [{"label": "Connect Typeform", "action": "connections.connect", "payload": {"query": "connect Typeform"}}]
    assert "only read your responses" in reply["body_md"] and not case["calls"]
    assert actions.get_action("experiment.survey.link").required_permission == "request_work"
    assert ACTION_SLUG["experiment.survey.link"] == "experiments"
    with db()() as session:
        row = session.get(Experiment, case["experiment"])
        assert ledger._ctx(row)["typeform_form_id"] == "form_one" and row.state == "proposed" and row.outcome is None
    state = ledger.loop_state(venture)
    review = asyncio.run(experiments.review_reply(venture, state))
    assert sum(action.get("label") == "Connect Typeform" for action in review["actions"]) == 1
    with db()() as session:
        row = session.get(Experiment, case["experiment"])
        row.context = json.dumps({**ledger._ctx(row), "direction": "past_direction"})
        session.commit()
    historical = asyncio.run(experiments.execute(venture, "experiment.survey.link", payload, case["scope"]["owner"]))
    assert sum(action.get("label") == "Connect Typeform" for action in historical["actions"]) == 1


@pytest.mark.parametrize("failure", ("writes_only", "scope_missing", "partial", "duplicate", "wrong_form", "bad_answer", "bad_time", "bad_field"))
def test_unsafe_or_incomplete_survey_reads_are_unavailable(evidence_case, failure):
    from app.connections import surveys

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    surveys.link_form(venture, case["experiment"], "form_one", case["scope"]["owner"])
    if failure == "writes_only":
        case["catalog"]["typeform"] = case["catalog"]["typeform"][1:]
    elif failure == "scope_missing":
        case["catalog"]["typeform"][0]["input_parameters"]["properties"].pop("form_id")
    elif failure == "partial":
        case["responses"]["form_one"]["total_items"] = 1001
    elif failure == "duplicate":
        case["responses"]["form_one"]["items"][1]["response_id"] = "response_one"
    elif failure == "wrong_form":
        case["responses"]["form_one"]["items"][0]["form_id"] = "other_form"
    elif failure == "bad_answer":
        case["responses"]["form_one"]["items"][0]["answers"][1]["number"] = float("nan")
    elif failure == "bad_time":
        case["responses"]["form_one"]["items"][0]["submitted_at"] = "not a date"
    else:
        case["responses"]["form_one"]["items"][0]["answers"][0]["field"]["id"] = ""
    assert surveys.snapshot(venture) == {"status": "unavailable"}
    if failure in {"writes_only", "scope_missing"}:
        assert not any(call[0] == "EXECUTE" for call in case["calls"])


def test_provider_failure_is_not_counted_as_a_failed_experiment(evidence_case):
    from app.chat import experiments
    from app.connections import surveys
    from app.experiments import ledger

    case, venture = evidence_case, evidence_case["scope"]["venture"]
    surveys.link_form(venture, case["experiment"], "form_one", case["scope"]["owner"])
    case["fail"] = True
    state = ledger.loop_state(venture)
    assert state["crm"] == state["surveys"] == {"status": "unavailable"}
    assert not state["failed"] and not state["connection_evidence"]
    reply = asyncio.run(experiments.review_reply(venture, state))
    assert "not counted as a failed test" in reply["body_md"] and "private provider failure" not in reply["body_md"]


def test_local_review_does_not_report_a_read_failure_for_unconfigured_apps(evidence_case, monkeypatch):
    from app.chat import experiments
    from app.connections import local_config
    from app.experiments import ledger

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_config, "enabled", lambda _name: False)
    venture = evidence_case["scope"]["venture"]
    state = ledger.loop_state(venture)
    reply = asyncio.run(experiments.review_reply(venture, state))
    assert state["crm"] is None
    assert "No experiment has a result yet." in reply["body_md"]
    assert not evidence_case["calls"]


@pytest.mark.parametrize("platform", ("hubspot", "typeform"))
def test_connect_flow_uses_stable_founder_id_only_when_flag_is_on(evidence_case, monkeypatch, platform):
    from app.connections import toolkits
    from app.content import composio_social
    from app.content.oauth import attempts
    from app.content.oauth.attempts import OAuthError

    case = evidence_case
    monkeypatch.setattr(composio_social, "ensure_auth_config", lambda _platform: "ac_" + platform)
    monkeypatch.setattr(attempts, "initiating_actor", lambda _workspace_id: case["scope"]["owner"])
    result = composio_social.start_connect(workspace_id=case["scope"]["workspace"], platform=platform)
    assert result["composio_user_id"] == case["scope"]["owner"]
    assert toolkits.accounts(case["scope"]["workspace"], platform)[0]["connected_account_id"] == "ca_" + platform
    monkeypatch.setattr(attempts, "initiating_actor", lambda _workspace_id: case["other"]["owner"])
    calls = len(case["calls"])
    with pytest.raises(OAuthError, match="own this workspace"):
        composio_social.start_connect(workspace_id=case["scope"]["workspace"], platform=platform)
    assert len(case["calls"]) == calls
    case["flag"] = False
    result = composio_social.start_connect(workspace_id=case["scope"]["workspace"], platform=platform)
    assert result["composio_user_id"] == "content:" + case["scope"]["workspace"]
