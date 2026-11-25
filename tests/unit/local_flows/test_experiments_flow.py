"""Owner HTTP journeys; only baseline idea data, replay responses and I/O are fixtures."""

import asyncio
import json
import re
import secrets
import socket
from datetime import datetime, timedelta, timezone
from html import escape
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import or_, select

from tests.unit.test_local_mode import _owner_client, local_owner_app


class OwnerFlow(SimpleNamespace):
    def __repr__(self):
        return "<authenticated local owner flow>"


@pytest.fixture(params=[
    {"audience": "independent vehicle repair shop managers", "problem": "missed spare parts replenishment",
     "offer": "A replenishment checklist for independent vehicle repair shops", "mode": "company"},
    {"audience": "families planning weekend camping trips", "problem": "forgotten camping supplies",
     "offer": "A packing checklist for families planning weekend camping trips", "mode": "consumer"},
], ids=["b2b-repair", "consumer-camping"])
def scenario(request):
    return request.param


@pytest.fixture
def flow_env(monkeypatch, tmp_path, _isolated_local_setup):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("REDIS_URL", raising=False)


@pytest.fixture
def model_configured():
    return True


@pytest.fixture
def owner(flow_env, local_owner_app, monkeypatch, scenario, caplog, model_configured):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from llm_gateway.cassette import DETERMINISTIC_FIXTURES

    traffic, messages, responses = [], [], []
    secret_values = [secrets.token_hex(24) for unused in range(3)]
    now = utcnow()

    def fake_response(request):
        host = request.headers.get("host", request.url.host).split(":")[0]
        traffic.append((request.method, host, request.url.path))
        assert not any(value == "supymem.com" or value.endswith(".supymem.com")
            for value in (host, request.url.host)), "Your local work must not contact Supymem."
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /", request=request)
        if host == "cloudflare-dns.com":
            return httpx.Response(200, json={"Status": 0, "TC": False,
                "Answer": [{"type": 15, "data": "10 mx.evidence.example."}]}, request=request)
        if host == "api.tavily.com":
            saved_key_used = json.loads(request.content).get("api_key") == secret_values[0]
            assert saved_key_used, "Your search did not use the key saved in Keys and connections."
            return httpx.Response(200, json={"results": [
                {"url": f"https://evidence.example/posts/{index}", "title": f"Discussion about {scenario['problem']}",
                 "content": f"I am one of the {scenario['audience']} and I struggle with {scenario['problem']}.",
                 "published_date": now.date().isoformat()} for index in range(6)
            ]}, request=request)
        if host in {"duckduckgo.com", "html.duckduckgo.com", "lite.duckduckgo.com"}:
            links = "".join(f'<div class="body"><h2>Discussion about {escape(scenario["problem"])}</h2>'
                f'<a href="https://evidence.example/posts/{index}">I am one of the {escape(scenario["audience"])} '
                f'and I struggle with {escape(scenario["problem"])}</a></div>' for index in range(6))
            return httpx.Response(200, text=links, request=request)
        if host == "evidence.example":
            page = (f'<html><head><title>Discussion about {escape(scenario["problem"])}</title></head><body><main>'
                    f'<time datetime="{now.date().isoformat()}">Published: {now.date().isoformat()}</time>'
                    f'<p>I am one of the {escape(scenario["audience"])} and I struggle with '
                    f'{escape(scenario["problem"])} every week. I waste hours doing this manually.</p>'
                    f'<p>Contact me at participant{request.url.path.rsplit("/", 1)[-1]}@evidence.example.</p>'
                    '</main></body></html>')
            return httpx.Response(200, text=page, headers={"content-type": "text/html"}, request=request)
        return httpx.Response(503, json={"error": "Your fake service is unavailable."}, request=request)

    original_sync_send = httpx.Client.send

    def sync_send(client, request, **kwargs):
        if request.url.host in {"127.0.0.1", "localhost", "testserver"}:
            return original_sync_send(client, request, **kwargs)
        response = fake_response(request)
        if kwargs.get("stream"):
            return httpx.Response(response.status_code, headers=response.headers,
                stream=httpx.ByteStream(response.content), request=request)
        return response

    async def async_send(client, request, **kwargs):
        return fake_response(request)

    def no_network(*args, **kwargs):
        raise AssertionError("Your local flow attempted un-faked network I/O.")

    def keyless_request(client, method, url, **kwargs):
        return fake_response(httpx.Request(method, url))

    class MailTransport:
        def __init__(self, host, *args, **kwargs):
            traffic.append(("SMTP", host, ""))

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def ehlo(self):
            return 250, b"ok"

        def starttls(self, **kwargs):
            return 220, b"ok"

        def login(self, *args):
            saved_password_used = len(args) == 2 and args[1] == secret_values[0]
            assert saved_password_used, "Your outgoing mail did not use the app password saved in Keys and connections."
            return 235, b"ok"

        def send_message(self, message, **kwargs):
            messages.append(message)
            return {}

        def sendmail(self, sender, recipients, message, **kwargs):
            from email import message_from_string
            messages.append(message_from_string(message))
            return {}

        def quit(self):
            return 221, b"ok"

    class InboxTransport:
        def __init__(self, host, *args, **kwargs):
            traffic.append(("IMAP", host, ""))

        def login(self, *args):
            return "OK", []

        def select(self, *args, **kwargs):
            return "OK", [b"0"]

        def uid(self, *args):
            return "OK", [b""]

        def logout(self):
            return "BYE", []

    import imaplib
    import smtplib
    from ddgs.http_client import HttpClient

    def dns(host, port, *args, **kwargs):
        address = "127.0.0.1" if host in {"localhost", "127.0.0.1"} else "93.184.216.34"
        if host == "::1":
            return [(socket.AF_INET6, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (host, port, 0, 0))]
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, port))]

    monkeypatch.setattr(httpx.Client, "send", sync_send)
    monkeypatch.setattr(httpx.AsyncClient, "send", async_send)
    monkeypatch.setattr(HttpClient, "request", keyless_request)
    monkeypatch.setattr(socket, "getaddrinfo", dns)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(smtplib, "SMTP", MailTransport)
    monkeypatch.setattr(smtplib, "SMTP_SSL", MailTransport)
    monkeypatch.setattr(imaplib, "IMAP4_SSL", InboxTransport)

    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000),
                       raise_server_exceptions=False) as client:
        client.headers["Origin"] = "http://localhost:3100"

        def request(method, path, **kwargs):
            response = client.request(method, path, **kwargs)
            responses.append(response.text)
            assert not any(value in response.text for value in secret_values), "Your API returned a saved secret."
            return response

        created = request("POST", "/ventures", json={"raw_idea": scenario["offer"], "start": False})
        assert created.status_code == 201, created.text
        venture = created.json()
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        path = f"/ventures/{venture['id']}"
        conversation = request("POST", path + "/chat/conversations", json={
            "idempotency_key": secrets.token_hex(12), "title": "Your experiment flow"})
        assert conversation.status_code == 200, conversation.text
        conversation_id = conversation.json()["id"]
        def fixture(purpose, value):
            monkeypatch.setitem(DETERMINISTIC_FIXTURES, purpose, {"content": json.dumps(value), "tokens_in": 80, "tokens_out": 80})

        from llm_gateway import sole
        original_transmit = sole.transmit

        async def replay(messages, **options):
            response = await original_transmit(messages, **options)
            schema = options.get("response_schema") or {}
            candidates = schema.get("properties", {}).get("items", {}).get("allOf")
            if options.get("purpose") == "gtm.discovery.judge" and candidates:
                response.choices[0].message.content = json.dumps({"items": [
                    {"id": candidate["contains"]["properties"]["id"]["const"],
                     "reject_reason": "The supplied page does not establish an eligible interview participant."}
                    for candidate in candidates]})
            elif "role_in_market" in schema.get("properties", {}):
                response.choices[0].message.content = json.dumps({
                    "role_in_market": "consumer/individual" if scenario["mode"] == "consumer" else "buyer",
                    "icp_fit": 0.9, "anti_icp": False,
                    "homepage_quote": f"I am one of the {scenario['audience']} and I struggle with {scenario['problem']} every week."})
            return response

        monkeypatch.setattr(sole, "transmit", replay)

        def action(name, payload=None):
            response = request("POST", path + "/chat/confirm", json={"action": name, "payload": payload or {},
                "conversation_id": conversation_id, "idempotency_key": secrets.token_hex(12)})
            assert response.status_code == 200, response.text
            return response.json()

        def chat(text):
            response = request("POST", path + "/chat", json={"message": text,
                "conversation_id": conversation_id, "idempotency_key": secrets.token_hex(12)})
            assert response.status_code == 200, response.text
            return response.json()

        def drain():
            from app.core.jobs import CLAIMABLE
            from app.worker.main import run_worker
            for unused in range(12):
                with db()() as session:
                    pending = session.scalars(select(Job).where(Job.status.in_(CLAIMABLE),
                        or_(Job.run_after.is_(None), Job.run_after <= utcnow().replace(tzinfo=None)),
                        or_(Job.scheduled_for.is_(None), Job.scheduled_for <= utcnow().replace(tzinfo=None)))).all()
                if not pending:
                    break
                asyncio.run(run_worker(once=True, stop=asyncio.Event()))
            else:
                pytest.fail("Your requested local jobs did not drain in twelve worker batches.")
            with db()() as session:
                failed = [(job.kind, job.status, job.last_error) for job in session.scalars(select(Job))
                          if job.status in {"failed_dead", "running", "leased"}]
            diagnostics = [(record.operation_kind, record.output_diagnostics.get("schema_issues"),
                record.output_diagnostics.get("validation_rule")) for record in caplog.records
                if record.message == "model_output_rejected"]
            assert not failed, (failed, diagnostics)

        from app.db.models import Job
        if model_configured:
            saved = request("PUT", "/local/providers", json={"provider": "openai_compatible",
                "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "offline-flow"}})
            assert saved.status_code == 200, saved.text
        saved = request("PUT", "/local/settings/search", json={"provider": "builtin"})
        assert saved.status_code == 200, saved.text
        from app.core.local_mode import is_offline, is_test_mode
        assert is_offline() and not is_test_mode()
        fixture("venture.chat.assistant", {"hypothesis": f"{scenario['audience']} will commit to trying your offer",
            "action": f"Interview eight consenting {scenario['audience']} about {scenario['problem']}. Record each commitment in your notes. Do not send or publish anything.",
            "success_measure": "Pass: at least four of eight commit to a trial. Fail: five or more decline. Otherwise inconclusive."})
        fixture("task.plan", {"title": "Summarise your saved replies", "steps": [{"title": "Summarise your saved replies",
            "skill": "tasks.waitlist_summary", "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}],
            "done_test": {"type": "deterministic", "spec": {"predicate": "all_steps_verified"}}})
        fixture("gtm.discovery.plan", {"audience": scenario["audience"], "queries": [
            {"query": f"{scenario['audience']} {scenario['problem']}", "intent": "customer_voice"},
            {"query": f"{scenario['audience']} {scenario['problem']} public discussions", "intent": "community"}]})
        fixture("gtm.discovery.judge", {"items": []})
        fixture("gtm.discovery.draft", {"items": []})
        if model_configured:
            drain()
        with db()() as session:
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture["id"])
                .order_by(IdeaSession.created_at.desc()))
            if idea is None:
                idea = IdeaSession(id="idea_" + venture["id"], venture_id=venture["id"], workspace_id=venture["workspace_id"],
                    case="know", state="confirmed", created_at=utcnow(), updated_at=utcnow())
                session.add(idea)
                session.flush()
            previous = session.get(IdeaRevision, idea.current_revision_id) if idea.current_revision_id else None
            revision = IdeaRevision(id="revision_" + venture["id"], session_id=idea.id,
                venture_id=venture["id"], workspace_id=venture["workspace_id"], rev=previous.rev + 1 if previous else 1,
                source="founder", summary=scenario["offer"], structured=json.dumps({"customer": scenario["audience"],
                    "problem": scenario["problem"], "offer": scenario["offer"], "_questions_skipped": True,
                    "_answered_fields": {"customer": scenario["audience"], "problem": scenario["problem"], "offer": scenario["offer"]}}),
                created_at=utcnow())
            session.add(revision)
            session.flush()
            idea.state, idea.updated_at = "confirmed", utcnow()
            idea.current_revision_id = idea.confirmed_revision_id = revision.id
            session.get(Venture, venture["id"]).stage = "idea"
            session.commit()
        yield OwnerFlow(request=request, chat=chat, action=action, drain=drain, fixture=fixture, path=path,
            scenario=scenario, venture=venture, conversation_id=conversation_id, traffic=traffic, messages=messages, responses=responses,
            secrets=secret_values, caplog=caplog)
        assert not any(value in caplog.text for value in secret_values), "Your logs contain a saved secret."


def assert_private(owner):
    assert not any(value in owner.caplog.text for value in owner.secrets), "Your logs contain a saved secret."
    assert not any(host == "supymem.com" or host.endswith(".supymem.com") for method, host, path in owner.traffic)


def assert_requested_reply(reply):
    assert reply.get("kind") not in {"confirmation", "action_failed", "provider_error", "needs_you"}, f"{reply.get('kind')}: {reply.get('body_md')}"
    assert not any(action.get("confirmation") for action in reply.get("actions", [])), reply
    copy = reply.get("body_md", "") + " ".join(action.get("label", "") for action in reply.get("actions", []))
    assert not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d", copy, re.I), copy
    assert "\u2014" not in copy, copy


def experiments(owner):
    response = owner.request("GET", owner.path + "/experiments")
    assert response.status_code == 200, response.text
    return response.json()["items"]


def design(owner):
    before = {item["id"] for item in experiments(owner)}
    reply = owner.chat(f"Design a customer interview experiment to test whether {owner.scenario['audience']} will commit to a trial for {owner.scenario['problem']}. Save the hypothesis, action and success measure. Do not send or publish anything.")
    assert_requested_reply(reply)
    owner.drain()
    saved = [item for item in experiments(owner) if item["id"] not in before and item.get("action") and item.get("measure")]
    assert saved, reply
    item = next(item for item in saved if owner.scenario["audience"].casefold() in item["hypothesis"].casefold())
    assert item["state"] == "proposed", item
    return item


def test_owner_designs_without_starting_unrequested_work(owner):
    """You get a scoped saved plan, not a send, publication, confirmation or charge count."""
    item = design(owner)
    assert owner.scenario["problem"] in item["action"]
    assert not owner.messages
    assert not owner.traffic
    assert_private(owner)


def report_result(owner, outcome="supported", commitments=4):
    item = design(owner)
    started = owner.request("POST", owner.path + "/experiments/start", json={
        "hypothesis": item["hypothesis"], "action": item["action"], "success_measure": item["measure"]})
    assert started.status_code == 200, started.text
    observed = datetime.now(timezone.utc).date().isoformat()
    result_text = (f"I completed eight interviews with {owner.scenario['audience']} on {observed}. "
                   f"{commitments} of eight committed to a trial. I recorded each answer in my interview notes.")
    reply = owner.action("experiment.record", {"experiment_id": item["id"], "ran": True, "complete": True,
        "outcome": outcome, "result_text": result_text, "learning": f"{commitments} people committed to a trial.",
        "sample_size": 8, "provenance": {"source": "Your interview notes", "method": "Eight customer interviews", "observed_at": observed}})
    return item, result_text, reply


@pytest.mark.parametrize("outcome,commitments", [("supported", 4), ("refuted", 0), ("inconclusive", 1)])
def test_owner_records_results_and_reads_the_saved_verdict(owner, outcome, commitments):
    """Your completed reports preserve your verdict, observations and source details without acting."""
    item, result_text, reply = report_result(owner, outcome, commitments)
    assert_requested_reply(reply)
    owner.drain()
    saved = next(row for row in experiments(owner) if row["id"] == item["id"])
    assert saved["state"] == "concluded" and saved["outcome"] == outcome, saved
    assert saved["evidence_label"] == "reported by you" and saved["sample_size"] == 8, saved
    assert saved["result_text"] == result_text
    assert re.search(r"\bnext\b|repeat|collect|test", reply["body_md"], re.I), reply
    assert not owner.messages and not owner.traffic
    assert_private(owner)


@pytest.mark.parametrize("outcome,commitments", [("supported", 4), ("refuted", 0), ("inconclusive", 1)])
@pytest.mark.parametrize("automatic_choices", [True, False])
def test_owner_gets_next_step_advice_without_confirmations_or_unrequested_work(owner, outcome, commitments, automatic_choices):
    """Your requested advice uses your reported result and offers a next step, not a confirmation card."""
    item, result_text, reply = report_result(owner, outcome, commitments)
    assert_requested_reply(reply)
    owner.drain()
    assert next(row for row in experiments(owner) if row["id"] == item["id"])["state"] == "concluded"
    before = {row["id"] for row in experiments(owner)}
    if not automatic_choices:
        response = owner.request("PUT", "/local/capabilities/run_for_me_experiments", json={"on": False})
        assert response.status_code == 200, response.text
    advice = owner.action("experiment.review")
    assert_requested_reply(advice)
    assert advice["body_md"].strip() and advice.get("actions"), {
        "reply": advice, "ledger": owner.request("GET", owner.path + "/experiments").json()}
    assert {row["id"] for row in experiments(owner)} == before
    from app.db.models import VentureDocument
    from app.state import db

    with db()() as session:
        assert not session.scalars(select(VentureDocument).where(VentureDocument.venture_id == owner.venture["id"],
            VentureDocument.kind == "experiment_decision")).all()
    assert not owner.messages and not owner.traffic
    assert_private(owner)


def test_owner_readout_labels_testimony_as_reported_not_verified(owner):
    """Your chat readout must distinguish your interview testimony from independently observed results."""
    item = design(owner)
    owner.action("experiment.record", {"experiment_id": item["id"], "ran": True, "complete": True,
        "outcome": "supported", "result_text": "I completed eight interviews and four people committed to a trial.",
        "learning": "Four people committed to a trial.", "sample_size": 8,
        "provenance": {"source": "Your interview notes", "method": "Customer interviews",
            "observed_at": datetime.now(timezone.utc).date().isoformat()}})
    saved = next(row for row in experiments(owner) if row["id"] == item["id"])
    assert saved["evidence_label"] == "reported by you"
    readout = owner.action("experiment.list", {"experiment_id": item["id"]})
    assert_requested_reply(readout)
    assert "reported by you" in readout["body_md"].lower(), readout
    assert_private(owner)


@pytest.mark.parametrize("legacy_spec", [{}, {"scan": "demand", "metric": "qualifying_complaints"}])
def test_partial_saved_scan_specs_do_not_hide_manual_or_automatic_evidence(owner, legacy_spec):
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow

    manual = design(owner)
    if legacy_spec:
        legacy_spec = {**legacy_spec, "direction": autonomous._snapshot(owner.venture["id"])["direction"]}
    identifier = "exp_" + secrets.token_hex(12)
    with db()() as session:
        session.add(Experiment(id=identifier, venture_id=owner.venture["id"], workspace_id=owner.venture["workspace_id"],
            kind=ledger.KIND, hypothesis=f"Public evidence about {owner.scenario['problem']}", action="Read public pages",
            state="concluded", outcome="inconclusive", learnings="Your public sources are not customer response.",
            ended_at=utcnow(), context=json.dumps({"runner": "system", "spec": legacy_spec,
                "result": {"outcome": "inconclusive", "insight": "Your public sources are not customer response."}})))
        session.commit()
    evidence = ledger.evidence_for_venture(owner.venture["id"])
    assert {manual["id"], identifier} <= {item["id"] for item in evidence}
    binding = next(item for item in evidence if item["id"] == identifier).get("evidence_binding")
    if isinstance(legacy_spec, dict) and legacy_spec:
        assert binding["experiment_id"] == identifier
    autonomous.available(owner.venture["id"])
    assert {manual["id"], identifier} <= {item["id"] for item in experiments(owner)}
    assert identifier not in {item["id"] for item in ledger.list_for_venture(owner.venture["id"])}
    assert not owner.messages and not owner.traffic
    assert_private(owner)


def test_long_hypotheses_keep_the_saved_audience_and_runnable_scan_recipes(owner):
    from app.experiments import autonomous

    snapshot = autonomous._snapshot(owner.venture["id"])
    problem = owner.scenario["problem"] + " during complicated planning cycles with recurring delays, changing requirements and many dependent steps that require repeated checking"
    snapshot["idea_problem"] = problem
    snapshot["journey"]["icp"].update(problem=problem, needs_input=False)
    scans = [spec for spec in autonomous.select_runnable(snapshot, autonomous._provider()) if spec.get("runner") == "scan"]
    demand = next(spec for spec in scans if spec["scan"] == "demand")
    assert owner.scenario["audience"].casefold() in demand["hypothesis"].casefold()
    assert all(spec["queries"] and spec["id"] for spec in scans)
    assert not owner.messages and not owner.traffic
    assert_private(owner)


def test_experiment_api_service_preserves_missing_and_other_workspace_checks(owner):
    from fastapi import HTTPException
    from app.api import experiments_api
    from app.tenancy import bind

    endpoints = [(experiments_api.market_map, ()), (experiments_api.list_venture_experiments, ()),
        (experiments_api.start_proposed_experiment, (experiments_api.ProposedTest(hypothesis=owner.scenario["problem"]),))]
    for workspace, venture_id in [("another_workspace", owner.venture["id"]), (owner.venture["workspace_id"], "missing_venture")]:
        with bind(workspace_id=workspace, venture_id=venture_id):
            for endpoint, arguments in endpoints:
                with pytest.raises(HTTPException) as denied:
                    endpoint(venture_id, *arguments)
                assert denied.value.status_code == 404 and denied.value.detail == "not_found"
    assert not owner.messages and not owner.traffic
    assert_private(owner)


def test_partial_owner_report_keeps_the_test_running_without_a_final_verdict(owner):
    """Your partial interviews are saved as progress, not a passing experiment."""
    item = design(owner)
    reply = owner.action("experiment.record", {"experiment_id": item["id"], "ran": True, "complete": False,
        "outcome": "supported", "result_text": "I interviewed three people so far. One committed. Five interviews remain.",
        "learning": "One of three has committed so far.", "sample_size": 3,
        "provenance": {"source": "Your interview notes", "method": "Customer interviews",
            "observed_at": datetime.now(timezone.utc).date().isoformat()}})
    assert_requested_reply(reply)
    saved = next(row for row in experiments(owner) if row["id"] == item["id"])
    assert saved["state"] == "running" and saved["outcome"] != "supported", saved
    assert saved["sample_size"] == 3 and saved["evidence_label"] == "reported by you"
    assert "no final verdict" in reply["body_md"].lower() and "remaining" in reply["body_md"].lower(), reply
    assert not owner.messages and not owner.traffic
    assert_private(owner)


@pytest.mark.parametrize("invalid", ["not_run", "missing_provenance", "future_date"])
def test_results_need_actual_work_and_nonfuture_evidence(owner, invalid):
    """Incomplete reports remain attributed and inconclusive; unperformed or future results cannot pass."""
    item = design(owner)
    payload = {"experiment_id": item["id"], "ran": invalid != "not_run", "complete": True,
        "outcome": "supported", "result_text": ("I have not run the interviews yet." if invalid == "not_run" else
            "I interviewed eight people and four committed to a trial."),
        "learning": "Four people committed.", "sample_size": 8}
    if invalid != "missing_provenance":
        payload["provenance"] = {"source": "Your interview notes", "method": "Customer interviews",
            "observed_at": (datetime.now(timezone.utc).date() + timedelta(days=1 if invalid == "future_date" else 0)).isoformat()}
    reply = owner.action("experiment.record", payload)
    assert not any(action.get("confirmation") for action in reply["actions"]), reply
    saved = next(row for row in experiments(owner) if row["id"] == item["id"])
    if invalid == "missing_provenance":
        assert saved["state"] == "running" and saved["outcome"] is None and saved["needs_evidence"], saved
        assert saved["evidence_label"] == "reported by you", saved
        assert all(field in reply["body_md"].lower() for field in ("source", "method", "date")), reply
    else:
        assert saved["state"] != "concluded" and not saved.get("ran"), saved
    assert re.search(r"tell me|report|date|count|once|try", reply["body_md"], re.I), reply
    assert not owner.messages and not owner.traffic
    assert_private(owner)


@pytest.mark.parametrize("provider", ["builtin", "tavily"])
@pytest.mark.parametrize("entry", ["chat", "button"])
def test_requested_scan_runs_in_real_local_worker_and_posts_sourced_readout(owner, provider, entry):
    """You can run keyless research or your saved search key without hosted policies or confirmations."""
    settings = {"provider": provider}
    if provider == "tavily":
        settings["api_key"] = owner.secrets[0]
    saved = owner.request("PUT", "/local/settings/search", json=settings)
    assert saved.status_code == 200, saved.text
    if entry == "button":
        from app.db.supymem_models import Experiment
        from app.state import db

        owner.action("experiment.list")
        with db()() as session:
            rows = session.scalars(select(Experiment).where(Experiment.venture_id == owner.venture["id"]))
            scan_id = next((row.id for row in rows if json.loads(row.context or "{}").get("spec", {}).get("scan") == "demand"), None)
        assert scan_id, "Your saved idea has no runnable demand scan."
        reply = owner.action("experiment.prepare", {"experiment_id": scan_id})
    else:
        reply = owner.chat("Run a public demand scan for my saved idea. Do not contact anyone or publish anything.")
    assert_requested_reply(reply)
    owner.drain()
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "experiment.run")))
        assert jobs and all(job.status == "succeeded" for job in jobs), [(job.kind, job.status, job.last_error) for job in jobs]
    scans = [item for item in experiments(owner) if item.get("state") == "concluded"]
    assert scans, {"reply": reply["body_md"], "items": [(item["id"], item["state"]) for item in experiments(owner)]}
    result = owner.action("experiment.prepare", {"experiment_id": scans[0]["id"]})
    assert_requested_reply(result)
    assert "https://evidence.example/" in json.dumps(result), result["body_md"]
    from app.db.supymem_models import Experiment
    with db()() as session:
        sources = json.loads(session.get(Experiment, scans[0]["id"]).context)["result"]["sources"]
    assert sources and all(source.get("published_at") for source in sources), sources
    owner.action("experiment.prepare", {"experiment_id": scans[0]["id"]})
    owner.drain()
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "experiment.run")))) == len(jobs)
    assert any(host == "evidence.example" for method, host, path in owner.traffic), owner.traffic
    assert not owner.messages
    assert not any(host in {"api.vercel.com", "api.netlify.com"} for method, host, path in owner.traffic)
    assert_private(owner)


@pytest.mark.parametrize("capability_name,action,title", [
    ("run_for_me_experiments", "experiment.beliefs.approve", "Run your experiments"),
    ("research", "research.prepare", "Research"),
    ("send_email", "outreach.send", "Send email"),
    ("publish_pages", "site.publish", "Publish pages"),
    ("routines", "routines.create", "Routines"),
    ("page_measurement", "experiment.measurement.record", "Page measurement"),
    ("find_customers", "gtm.find", "Find customers"),
    ("content", "outreach.draft", "Draft content"),
    ("background_work", "autopilot.god_on", "Background work"),
])
def test_switched_off_work_names_the_feature_screen_and_never_executes(owner, capability_name, action, title):
    """Turning an area off blocks its owner entry point and gives the precise app setting to change."""
    switched = owner.request("PUT", f"/local/capabilities/{capability_name}", json={"on": False})
    assert switched.status_code == 200, switched.text
    assert switched.json()["state"] == "off"
    reply = owner.action(action, {"text": "Design a customer interview experiment", "goal": "every Friday summarise replies"})
    assert "Settings > Capabilities" in reply["body_md"] and title in reply["body_md"], reply
    assert switched.json()["next_step"] in reply["body_md"], reply
    assert not any(item.get("confirmation") for item in reply["actions"]), reply
    owner.drain()
    assert not owner.traffic and not owner.messages
    assert_private(owner)


def test_requested_scan_on_the_journey_fast_path_starts_without_another_click(owner):
    from app.db.models import Job
    from app.state import db

    reply = owner.chat("Jump to research and run a demand scan for me. Do not contact anyone or publish anything.")
    assert_requested_reply(reply)
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "experiment.run", Job.venture_id == owner.venture["id"])).all()
        assert len(jobs) == 1, reply["body_md"]
    owner.drain()
    assert not owner.messages
    assert_private(owner)


@pytest.mark.parametrize("entry", [
        "scan",
    "routine",
])
def test_missing_saved_search_key_names_keys_screen_and_exact_key(owner, entry):
    """Selecting a keyed upgrade without its key asks you for that key, never an environment flag."""
    saved = owner.request("PUT", "/local/settings/search", json={"provider": "tavily"})
    assert saved.status_code == 200, saved.text
    if entry == "routine":
        enabled = owner.request("PUT", "/local/capabilities/routines", json={"on": True})
        assert enabled.status_code == 200 and enabled.json()["state"] == "ready", enabled.text
        owner.fixture("task.plan", {"title": "Research your idea", "steps": [{"title": "Research your idea",
            "skill": "research.deep", "done_test": {"type": "deterministic", "spec": {"predicate": "workflow_receipt"}}}],
            "done_test": {"type": "deterministic", "spec": {"predicate": "all_steps_verified"}}})
        response = owner.request("POST", owner.path + "/routines", json={"goal": "every Friday research my idea", "timezone": "UTC"})
        assert response.status_code in {403, 422}, response.text
        copy = response.text
    else:
        reply = owner.chat("Run a public demand scan for my saved idea.")
        copy = reply["body_md"]
        assert not any(action.get("confirmation") for action in reply["actions"]), reply
    assert "Keys and connections" in copy and "API key" in copy and "search" in copy.lower(), copy
    owner.drain()
    assert not owner.traffic and not owner.messages
    assert_private(owner)


@pytest.mark.parametrize("name,section,values,action,field,screen", [
    ("send_email", "smtp", {"host": "smtp.example", "user": "owner@example.test", "sender_name": "Your founder",
        "postal_address": "17 Test Street, Test City, US"}, "outreach.send", "Mail app password", "Your mail account"),
    ("publish_pages", "publishing", {"provider": "vercel", "project": "your-test-site"},
        "site.publish", "Publishing token", "Your page publishing account"),
])
def test_missing_delivery_setting_names_the_exact_key_and_screen(owner, name, section, values, action, field, screen):
    """A switched-on delivery feature still needs your own saved key before it can act."""
    settings = owner.request("PUT", f"/local/settings/{section}", json=values)
    assert settings.status_code == 200, settings.text
    capability = owner.request("PUT", f"/local/capabilities/{name}", json={"on": True})
    assert capability.status_code == 200 and capability.json()["state"] == "needs_setup", capability.text
    reply = owner.action(action)
    assert all(text in reply["body_md"] for text in ("Keys and connections", field, screen)), reply
    assert capability.json()["next_step"] in reply["body_md"], reply
    owner.drain()
    assert not owner.traffic and not owner.messages
    assert_private(owner)


def test_disabling_research_blocks_an_experiment_scan_with_the_research_switch_next_step(owner):
    """Run-for-me does not bypass a Research switch you turned off."""
    capability = owner.request("PUT", "/local/capabilities/research", json={"on": False})
    assert capability.status_code == 200, capability.text
    reply = owner.chat("Run a public demand scan for my saved idea.")
    assert "Settings > Capabilities" in reply["body_md"] and "Research" in reply["body_md"], reply
    assert capability.json()["next_step"] in reply["body_md"], reply
    owner.drain()
    assert not owner.traffic and not owner.messages
    assert_private(owner)


def test_switching_research_off_blocks_queued_scans_with_the_saved_setting_next_step(owner):
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.state import db

    reply = owner.chat("Run a demand scan for my saved idea.")
    assert_requested_reply(reply)
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind == "experiment.run", Job.venture_id == owner.venture["id"])))
        assert len(jobs) == 1
        experiment_id = json.loads(jobs[0].payload_json)["experiment_id"]
    switched = owner.request("PUT", "/local/capabilities/research", json={"on": False})
    assert switched.status_code == 200, switched.text
    owner.drain()
    with db()() as session:
        error = json.loads(session.get(Experiment, experiment_id).context)["error"]
    assert switched.json()["next_step"] in error, error
    readout = owner.action("experiment.prepare", {"experiment_id": experiment_id})
    assert_requested_reply(readout)
    assert switched.json()["next_step"] in readout["body_md"], readout["body_md"]
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "experiment.run", Job.venture_id == owner.venture["id"])))) == 1
    assert not owner.traffic and not owner.messages
    assert_private(owner)


@pytest.mark.parametrize("model_configured", [False])
def test_manual_interview_brief_works_without_a_model_key(owner, model_configured):
    """Your keyless interview brief stays proposed and unsent until you request more work."""
    reply = owner.action("experiment.start", {"text": "Design a customer interview experiment"})
    assert_requested_reply(reply)
    owner.drain()
    saved = experiments(owner)
    assert saved and all(item["state"] == "proposed" for item in saved), reply
    assert any(owner.scenario["problem"] in item["hypothesis"] for item in saved), saved
    assert all(item["action"] and item["measure"] for item in saved), saved
    assert not owner.traffic and not owner.messages
    assert_private(owner)


def test_saved_secrets_and_enabled_delivery_never_authorize_unrequested_work(owner):
    """Saving your keys and enabling delivery does not send or publish your private experiment plan."""
    settings = {
        "smtp": {"host": "smtp.example", "port": 587, "user": "owner@example.test", "password": owner.secrets[0],
            "sender_name": "Your founder", "postal_address": "17 Test Street, Test City, US", "imap_host": "imap.example"},
        "publishing": {"provider": "vercel", "project": "your-test-site", "token": owner.secrets[1]},
        "composio": {"api_key": owner.secrets[2], "auth_configs": {"gmail": "auth_config_fixture"}},
    }
    for section, values in settings.items():
        response = owner.request("PUT", f"/local/settings/{section}", json=values)
        assert response.status_code == 200, response.text
        response = owner.request("GET", f"/local/settings/{section}")
        assert response.status_code == 200, response.text
    for name in ("send_email", "publish_pages", "connected_apps"):
        response = owner.request("PUT", f"/local/capabilities/{name}", json={"on": True})
        assert response.status_code == 200 and response.json()["state"] == "ready", response.text
    design(owner)
    owner.drain()
    assert not owner.traffic and not owner.messages
    assert_private(owner)


def test_run_for_me_starts_only_your_selected_interview_test(owner):
    """Your run request starts the shown test, not an unrelated experiment or any email."""
    item = design(owner)
    reply = owner.chat("Run it for me")
    assert_requested_reply(reply)
    owner.drain()
    saved = next(row for row in experiments(owner) if row["id"] == item["id"])
    assert saved["state"] == "running", reply
    assert not [row for row in experiments(owner) if row["id"] != item["id"] and row["state"] == "running"]
    assert not owner.messages
    assert not any(method in {"SMTP", "IMAP"} or host in {"api.vercel.com", "api.netlify.com"}
                   for method, host, path in owner.traffic)
    contacts = owner.request("GET", owner.path + "/gtm/contacts")
    assert contacts.status_code == 200, contacts.text
    public_addresses = {f"participant{index}@evidence.example" for index in range(6)}
    assert all(not contact.get("email") or contact["email"] in public_addresses for contact in contacts.json()["items"])
    assert_private(owner)


def invitations(owner):
    item = design(owner)
    settings = owner.request("PUT", "/local/settings/smtp", json={"host": "smtp.example", "port": 587,
        "user": "owner@example.test", "password": owner.secrets[0], "sender_name": "Your founder",
        "postal_address": "17 Test Street, Test City, US", "imap_host": "imap.example"})
    assert settings.status_code == 200, settings.text
    addresses = [f"participant{index}@company{index}.example" for index in range(3)]
    preview = owner.request("POST", owner.path + "/gtm/contacts/import/preview", json={
        "text": "email,country\n" + "\n".join(f"{address},US" for address in addresses), "format": "csv"})
    assert preview.status_code == 200, preview.text
    committed = owner.request("POST", owner.path + "/gtm/contacts/import", json={
        "preview_id": preview.json()["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"})
    assert committed.status_code == 200 and len(committed.json()["contact_ids"]) == 3, committed.text
    started = owner.action("experiment.run_review", {"experiment_id": item["id"], "run": True})
    assert_requested_reply(started)
    owner.fixture("outreach.draft_batch", {"drafts": [{"to": address, "subject": "Your interview invitation",
        "body": f"You are invited to discuss {owner.scenario['problem']}. Would you take a 15-minute call?"} for address in addresses]})
    reply = owner.action("outreach.draft", {"batch": True, "run": True, "experiment_id": item["id"]})
    assert_requested_reply(reply)
    owner.drain()
    contacts = owner.request("GET", owner.path + "/gtm/contacts")
    assert contacts.status_code == 200, contacts.text
    drafts = [contact["draft"] for contact in contacts.json()["items"] if contact.get("draft")]
    assert len(drafts) == 3, reply
    for draft in drafts:
        assert "I have your address from my own contacts." in draft["body"], draft
        assert re.search(r"opt.out|unsubscribe|\bSTOP\b|no thanks|Stop these emails", draft["body"], re.I), draft
        assert "17 Test Street, Test City, US" in draft["body"], draft
        assert draft["state"] in {"draft", "manual"} and not draft.get("sent_at"), draft
    assert not owner.messages
    return reply, drafts


def test_experiment_invitation_drafts_keep_disclosure_optout_and_sender_address(owner):
    """Your requested interview drafts disclose your contact source and remain unsent."""
    invitations(owner)
    assert not any(method == "SMTP" for method, host, path in owner.traffic)
    assert_private(owner)


@pytest.mark.parametrize("opt_out", [
    False, True,
], ids=["cap", "opt-out-and-cap"])
def test_requested_invitation_send_respects_suppression_and_owner_send_cap(owner, opt_out):
    """Even when you ask to send, an opted-out contact is excluded and only your daily cap can go out."""
    from app.db.models import GtmContact, GtmTouch
    from app.state import db

    reply, drafts = invitations(owner)
    if opt_out:
        opted_out = owner.request("POST", "/api/v1" + owner.path +
            f"/gtm/journey/prospects/{drafts[0]['contact_id']}/reply", json={"text": "STOP", "outcome": "opted_out"})
        assert opted_out.status_code == 200, opted_out.text
    enabled = owner.request("PUT", "/local/capabilities/send_email", json={"on": True})
    assert enabled.status_code == 200 and enabled.json()["state"] == "ready", enabled.text
    settings = owner.request("PUT", owner.path + "/autopilot", json={
        "allow_send": True, "send_max_per_day": 1, "confirmed_send_max": True})
    assert settings.status_code == 200, settings.text
    campaign = owner.request("PATCH", "/api/v1" + owner.path + "/gtm/journey/settings", json={
        "allow_sending": True, "aup_accepted": True, "dpa_accepted": True})
    assert campaign.status_code == 200, campaign.text
    reply = owner.action("gtm.status", {"show_saved": True})
    assert_requested_reply(reply)
    assert not owner.messages
    sent = owner.chat("Send all three emails")
    assert sent.get("kind") not in {"confirmation", "action_failed", "provider_error"}, sent
    assert not any(action.get("confirmation") for action in sent["actions"]), sent
    assert not re.search(r"\d[\d,.]*\s+credits?", sent["body_md"], re.I), sent
    owner.drain()
    with db()() as session:
        touches = list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))
        assert len(touches) == 1, sent["body_md"]
        contact = session.get(GtmContact, touches[0].contact_id)
        if opt_out:
            assert contact.email != drafts[0]["to"]
        assert len(owner.messages) == 1 and owner.messages[0]["To"] == contact.email
    message = owner.messages[0]
    assert "owner@example.test" in message["From"]
    body = message.get_content()
    assert "I have your address from my own contacts." in body
    assert re.search(r"opt.out|unsubscribe|\bSTOP\b|no thanks|Stop these emails", body, re.I)
    assert "17 Test Street, Test City, US" in body
    shown = owner.action("gtm.status", {"show_saved": True})
    assert not any(action.get("confirmation") for action in shown["actions"]), shown
    retry = owner.chat("Send all the emails shown")
    assert not any(action.get("confirmation") for action in retry["actions"]), retry
    owner.drain()
    with db()() as session:
        assert len(list(session.scalars(select(GtmTouch).where(GtmTouch.direction == "out")))) == 1
    assert re.search(r"limit|cap|tomorrow|next day|wait|review", retry["body_md"], re.I), retry
    assert_private(owner)


@pytest.mark.parametrize("entry", [
    "api",
    "chat",
])
def test_requested_routine_fires_real_jobs_posts_private_report_and_can_be_paused(owner, monkeypatch, entry):
    """Only your saved schedule runs, posts back privately, and respects a subsequent pause."""
    from app.db.models import ChatConversation, ChatMessage, Job, Workspace
    from app.state import db
    from app.worker import main as worker
    from core import clock

    ready = owner.request("PUT", "/local/capabilities/routines", json={"on": True})
    assert ready.status_code == 200 and ready.json()["state"] == "ready", ready.text
    background = owner.request("PUT", "/local/capabilities/background_work", json={"on": False})
    assert background.status_code == 200 and background.json()["state"] == "off", background.text
    if entry == "api":
        created = owner.request("POST", owner.path + "/routines", json={"goal": "every Friday summarise replies", "timezone": "UTC"})
        assert created.status_code == 200, created.text
        routine = created.json()
        assert "credit" not in created.text.lower() and "confirmation" not in created.text.lower()
    else:
        reply = owner.chat("Every Friday summarise replies")
        assert_requested_reply(reply)
        listed = owner.request("GET", owner.path + "/routines")
        assert listed.status_code == 200 and listed.json()["items"], listed.text
        routine = listed.json()["items"][0]
    assert routine["next_fire_at"]
    owner.drain()
    with db()() as session:
        scheduled = list(session.scalars(select(Job).where(Job.kind == "routines.fire")))
        assert scheduled and all(job.status in {"pending", "scheduled"} for job in scheduled)
        assert all((job.run_after or job.scheduled_for) > clock.utcnow().replace(tzinfo=None) for job in scheduled)
    due = datetime.fromisoformat(routine["next_fire_at"])
    monkeypatch.setattr(clock, "_clock", lambda: due)
    worker._run_scheduler()
    owner.drain()
    monkeypatch.setattr(clock, "_clock", lambda: due + timedelta(seconds=60))
    worker._run_scheduler()
    owner.drain()
    with db()() as session:
        jobs = list(session.scalars(select(Job).where(Job.kind.in_(("routines.fire", "tasks.execute", "routines.report")))))
        assert {job.kind for job in jobs if job.status == "succeeded"} == {"routines.fire", "tasks.execute", "routines.report"}, [(job.kind, job.status) for job in jobs]
        reports = list(session.scalars(select(ChatMessage).where(ChatMessage.venture_id == owner.venture["id"],
            ChatMessage.role == "assistant", ChatMessage.body_md.contains("Your routine:"))))
        assert reports and all(report.conversation_id for report in reports)
        assert any("replies" in report.body_md.lower() for report in reports)
        workspace = session.get(Workspace, owner.venture["workspace_id"])
        for report in reports:
            conversation = session.get(ChatConversation, report.conversation_id)
            assert conversation.visibility == "private" and conversation.creator_person_id == workspace.owner_id
            assert not any(action.get("confirmation") for action in json.loads(report.actions_json))
    paused = owner.request("POST", owner.path + f"/routines/{routine['id']}/pause")
    assert paused.status_code == 200 and paused.json()["state"] == "paused", paused.text
    monkeypatch.setattr(clock, "_clock", lambda: due + timedelta(days=8))
    worker._run_scheduler()
    owner.drain()
    with db()() as session:
        assert len(list(session.scalars(select(Job).where(Job.kind == "routines.fire", Job.status == "succeeded")))) == 1
    assert not owner.messages and not owner.traffic
    assert_private(owner)
