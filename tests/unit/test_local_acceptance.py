import importlib.util
import json
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.integration.test_local_coding_cli import DOMAINS, local_stack, saved_venture
from tests.unit.test_local_mode import local_owner_app


@pytest.mark.parametrize("scenario,packaged", [("enterprise_health", False), ("consumer_fintech", True)])
def test_offline_value_gate_requires_explicit_source_opt_in(value_journey, monkeypatch, tmp_path, capsys, scenario, packaged):
    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", "1" if packaged else "0")
    monkeypatch.setenv("COMPANY_OS_PACKAGED", "1" if packaged else "0")
    example = Path(value_journey.__file__).parent / "local_journeys" / f"{scenario}.json"
    logs = tmp_path / "gate"
    assert value_journey.main(["--offline", "--log-dir", str(logs), "--arguments-file", str(example)]) == 1
    assert "source checkout" in capsys.readouterr().out
    assert not logs.exists()


@pytest.mark.parametrize("audience", ["freight dispatchers", "home bakers"])
def test_offline_model_serves_provider_probe_before_scenario(local_owner_app, monkeypatch, audience):
    from scripts.desktop.offline_value import respond
    from tests.loopback_model import MODEL, serve_model
    from tests.unit.test_local_mode import _owner_client

    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    with serve_model(respond) as model:
        provider = {"provider": "openai_compatible", "model": MODEL, "base_url": model.url}
        with _owner_client(local_owner_app) as client:
            probe = client.post("/local/providers/test", json=provider)
        assert probe.status_code == 200 and probe.json()["ok"] is True, probe.text
        model.scenario = {"idea": f"A tracker for {audience}", "answer": audience}
        entry = respond({"messages": [{"role": "system", "content": "You are an idea clarifier."},
                                      {"role": "user", "content": model.scenario["idea"]}]}, model)
        assert entry["understanding"]["customer"] == audience


@pytest.mark.asyncio
async def test_offline_search_uses_builtin_transport_with_deadline(local_owner_app, monkeypatch, tmp_path, scenario):
    import socket

    from app.core import local_env_import
    from app.research import local_providers
    from app.signals import local_reader
    from desktop_runtime import agent_status, owned_coding
    from scripts.desktop.offline_value import install_transports

    for module, attribute in [(local_env_import, "repo_root"), (owned_coding, "_safe_directory"),
                              (agent_status, "provider_binary"), (socket, "getaddrinfo"),
                              (local_providers, "_builtin_search_sync"), (local_reader, "_basic_read")]:
        monkeypatch.setattr(module, attribute, getattr(module, attribute))
    idea, audience = scenario
    (tmp_path / "journey-fixture.json").write_text(json.dumps({"idea": idea, "answer": audience}))
    install_transports(tmp_path)

    results = await local_providers._builtin_search(audience, 2)
    assert len(results) == 2
    assert all(audience in item["body"] for item in results)


def test_offline_reviewer_selects_the_verified_offering(scenario):
    from types import SimpleNamespace

    from scripts.desktop.offline_value import OFFERING, respond

    idea, audience = scenario
    sources = [
        {"id": "community", "page_kind": "offering", "excerpts": [f"Our members are {audience}."], "fetched_quote_indexes": [0]},
        {"id": "product", "page_kind": "offering", "excerpts": [OFFERING, f"{OFFERING} For {audience}."], "fetched_quote_indexes": [1]},
    ]
    review = respond({"messages": [{"role": "system", "content": "You review evidence"},
                                   {"role": "user", "content": json.dumps({"sources": sources})}]},
                     SimpleNamespace(scenario={"idea": idea, "answer": audience}))
    assert [(item["source_id"], item["product_quote_index"]) for item in review["competitor_checks"]] == [("product", 1)]


@pytest.mark.parametrize("idea,address", [
    ("An invoice checker for freight brokers", "buyer@freight.example"),
    ("An indoor herb kit for apartment residents", "gardener@herbs.example"),
])
@pytest.mark.parametrize("discovered", [False, True])
def test_local_contact_preview_commit(local_owner_app, monkeypatch, idea, address, discovered):
    from tests.unit.test_local_mode import _owner_client as TestClient
    from sqlalchemy import select
    from app.db.models import GtmContact, GtmTouch
    from app.intake import contacts
    from app.state import db
    from core.clock import utcnow

    async def mail_domains(domains):
        return {domain: "mx" for domain in domains}

    monkeypatch.setattr(contacts, "check_domains", mail_domains)
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        created = client.post("/ventures", json={"raw_idea": idea, "start": False})
        assert created.status_code == 201, created.text
        venture = created.json()
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        path = f"/ventures/{venture['id']}/gtm/contacts/import"
        if discovered:
            with db()() as session:
                session.add(GtmContact(id="public_contact", workspace_id=venture["workspace_id"], email=address,
                    source=f"venture:{venture['id']}:public", enrichment_json=json.dumps({"found_at": f"https://{address.split('@')[1]}/contact"}),
                    times_emailed=2, stage_changed_at=utcnow(), created_at=utcnow()))
                session.commit()
        contact_ids = None
        for attempt in range(2):
            preview = client.post(path + "/preview", json={"text": address, "format": "paste"})
            assert preview.status_code == 200, preview.text
            assert preview.json()["valid_rows"] == 1 - attempt, (attempt, preview.json())
            assert preview.json()["duplicates"] == attempt, (attempt, preview.json())
            denied = client.post(path, json={"preview_id": preview.json()["preview_id"], "lawful_basis_attested": False})
            assert denied.status_code == 422
            committed = client.post(path, json={"preview_id": preview.json()["preview_id"],
                "lawful_basis_attested": True, "lawful_basis": "consent"})
            assert committed.status_code == 200, committed.text
            assert committed.json()["contact_ids"], (attempt, preview.json())
            if contact_ids is not None:
                assert committed.json()["contact_ids"] == contact_ids
            contact_ids = committed.json()["contact_ids"]
        with db()() as session:
            saved = list(session.scalars(select(GtmContact)))
            assert len(saved) == 1 and saved[0].contact_provenance == "founder_import"
            assert saved[0].lawful_basis == "consent"
            assert saved[0].email_confidence == "risky"
            assert json.loads(saved[0].enrichment_json)["founder_import"]["lawful_basis_attested"] is True
            if discovered:
                assert json.loads(saved[0].enrichment_json)["found_at"]
                assert saved[0].times_emailed == 2
            assert not list(session.scalars(select(GtmTouch)))


@pytest.fixture(params=[
    ("A service that helps freight brokers reconcile invoices", "Independent freight brokers with small accounting teams"),
    ("A kit that helps apartment residents grow herbs indoors", "Apartment residents without balconies"),
    ("A log that helps families track follow-up tasks after clinic visits", "Parents managing their children's clinic follow-ups"),
])
def scenario(request):
    return request.param


@pytest.fixture
def acceptance(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("SUPYMEM_LOCAL_PROXY_SECRET_FILE", str(tmp_path / "owner-session.secret"))
    secret_file = tmp_path / "owner-session.secret"
    if not secret_file.exists():
        secret_file.touch(mode=0o600)
        secret_file.write_text(secrets.token_hex(32))
    path = Path(__file__).resolve().parents[2] / "scripts/local_acceptance.py"
    spec = importlib.util.spec_from_file_location("local_acceptance", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def value_journey(acceptance, monkeypatch):
    import sys

    monkeypatch.setitem(sys.modules, "local_acceptance", acceptance)
    path = Path(__file__).resolve().parents[2] / "scripts/local_value_journey.py"
    monkeypatch.syspath_prepend(str(path.parent))
    spec = importlib.util.spec_from_file_location("local_value_journey", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def api(acceptance):
    from core.local_security import _proxy_secret

    expected_secret = _proxy_secret().decode("ascii")
    state = {"answer": "", "results": "", "confirmation": False, "missing": "",
             "requests": [], "sent": 0, "empty": False, "llm_mode": "offline",
             "require_contact": False, "require_page": False}
    state.update(browser_proof=secrets.token_hex(32), browser_cookie=secrets.token_hex(32), navigations=0)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.respond()

        def do_POST(self):
            self.respond()

        def do_PATCH(self):
            self.respond()

        def do_PUT(self):
            self.respond()

        def respond(self):
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            state["requests"].append((self.command, self.path, payload))
            path = self.path.partition("?")[0]
            if state.get("proxy"):
                if path == "/":
                    state["navigations"] += 1
                    assert self.headers.get("X-Local-Proxy-Secret") is None
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html")
                    self.send_header("Set-Cookie", f"supymem_owner={state['browser_cookie']}; Path=/api; HttpOnly; SameSite=Strict")
                    self.end_headers()
                    self.wfile.write(f'<script id="local-owner-session">const proof = "{state["browser_proof"]}";</script>'.encode())
                    return
                assert path.startswith("/api/")
                assert self.headers.get("X-Local-Proxy-Secret") is None
                assert self.headers.get("X-Local-Session-Proof") == state["browser_proof"]
                assert self.headers.get("Cookie") == f"supymem_owner={state['browser_cookie']}"
                path = path[4:]
                if path.startswith("/v1/"):
                    path = "/api" + path
            else:
                assert secrets.compare_digest(self.headers.get("X-Local-Proxy-Secret", ""), expected_secret)
            status, media = 200, "application/json"
            reply = {}
            if state["missing"] and state["missing"] in path:
                status, reply = 404, {"detail": "secret-value-must-not-appear"}
            elif path == "/setup/status":
                reply = {"mode": "local", "llm_mode": state["llm_mode"], "email_mode": state.get("email_mode", "log")}
            elif path == "/local/settings/catalog":
                reply = {"sections": [{"name": "smtp"}, {"name": "search"}]}
            elif path == "/local/settings/spend":
                reply = {"values": {"monthly_spend_cap": payload.get("monthly_spend_cap")}}
            elif path == "/local/capabilities/run_for_me_experiments":
                reply = {"name": "run_for_me_experiments", "state": "ready"}
            elif path == "/local-tools/coding-agents":
                reply = {"tools": [{"name": "codex", "candidate": "reviewed-codex",
                                    "candidate_path": str((Path.home() / "bin/codex").resolve())}]}
            elif path == "/ui-config/coding-agents/consent":
                reply = {"consent": {payload["provider"]: payload["allow"]}}
            elif path == "/local/capabilities":
                reply = {"items": [{"name": name, "title": name, "state": "ready", "missing": [], "next_step": "Continue in your app."}
                                   for name in ("research", "find_customers", "send_email", "read_replies", "landing_pages", "publish_pages", "run_for_me_experiments")]}
                if state.get("blocked_capability"):
                    reply["items"][0].update(state="needs_setup", missing=["model_provider"],
                                             next_step="Open Keys and connections > AI service and save your model provider key.")
            elif path == "/local/settings/smtp":
                reply = {"values": {"host": "smtp.example.invalid", "port": 587, "user": state.get("mailbox_user", "owner@example.invalid"),
                                    "postal_address": "1 Main Street", "imap_host": "imap.example.invalid"}, "secrets_set": {"password": True}}
                if state.get("no_smtp"):
                    reply["secrets_set"]["password"] = False
            elif path in {"/local/settings/smtp/test", "/local/settings/imap/test"}:
                reply = {"ok": not state.get("connection_failed"), "next_step": "Private service detail must not appear"}
            elif path == "/local/health":
                reply = {"database": {"ok": True}, "worker": {"ok": not state.get("worker_unready")}}
            elif path == "/ventures":
                status = 201
                reply = {"id": "ven_test", "workspace_id": "wsp_test", "raw_idea": payload["raw_idea"]}
            elif path.endswith(("/idea/sessions", "/idea/sessions/ise_test")):
                reply = self.session()
            elif path.endswith("/messages"):
                if payload["text"] == "Skip the remaining questions":
                    state["reviewing"] = True
                else:
                    state["answer"] = payload["text"]
                reply = {"session": self.session()}
            elif path.endswith("/confirm"):
                state["confirmed"] = True
                reply = {"session": self.session()}
            elif path.endswith("/chat") and self.command == "GET":
                reply = {"items": [{"id": "scan_reply", "kind": "experiment_result", "body_md": "Your sourced scan is saved.",
                                     "citations": [{"url": "https://source.example.test/scan"}] if not state.get("scan_unsourced") else []}]
                         if state.get("scanned") else []}
            elif path.endswith("/chat"):
                if payload["message"].startswith("Design "):
                    state["designed"] = True
                if payload["message"].startswith("Prepare email"):
                    if state["require_contact"] and not state.get("imported"):
                        status = 422
                    else:
                        state["invitations"] = True
                if payload["message"].startswith("Build "):
                    state["built"] = True
                if payload["message"].startswith("Results:"):
                    state["results"] = payload["message"]
                if payload["message"].startswith("Research "):
                    state["researched"] = True
                if payload["message"].startswith("Find competitors"):
                    state["competitors"] = True
                if payload["message"].startswith("Run for me"):
                    state["scanned"] = True
                if payload["message"].startswith("Publish "):
                    state["published"] = True
                reply = {"kind": "confirmation" if state["confirmation"] else "text",
                         "body_md": state.get("reply", "Your work is saved. Review your next step."), "actions": []}
            elif path.endswith("/research/dossier"):
                sources = [{"url": f"https://source{index}.example.test/article"} for index in range(state.get("source_count", 3))]
                reply = {"available": state.get("researched", False), "sources": sources,
                         "competitors": [{"name": "Alternative", "url": sources[0]["url"]}] if state.get("competitors") and sources else []}
            elif path.endswith("/gtm/contacts/import/preview"):
                reply = {"preview_id": "preview_test"}
            elif path.endswith("/gtm/contacts/import"):
                if payload.get("lawful_basis_attested") is not True:
                    status = 422
                else:
                    state["imported"] = True
                    reply = {"imported": 1, "contact_ids": ["contact_test"]}
            elif path.endswith("/gtm/contacts"):
                draft = {
                    "id": "draft_test", "state": "draft", "sent_at": None,
                    "body": state.get("draft_body", "Your email address is listed publicly on https://example.test/. Reply STOP to opt out. Sender: 1 Main Street.")}
                draft.update(doc_id="draft_test", contact_id="contact_test", to=state.get("draft_to", "owner@example.invalid"),
                             revision="a" * 64, batch_id="batch_test")
                items = [{"id": "contact_test", "draft": draft if state.get("invitations") else None}]
                if state.get("proxy"):
                    items = [{"id": f"contact{index}", "contact_id": None if state.get("unsaved") else f"contact{index}",
                              "source_url": f"https://source{index}.example.test/article"}
                             for index in range(state.get("contact_count", 3))]
                    if state.get("imported"):
                        items.append({"id": "contact_test", "contact_id": "contact_test", "email": "owner@example.invalid",
                                      "draft": draft if state.get("invitations") else None})
                reply = {"items": [] if state["empty"] else items}
            elif path.endswith("/gtm/journey"):
                reply = {"sender_postal_address": "1 Main Street", "results": {"sent": state["sent"], "replies": []},
                         "drafts": [{"doc_id": "draft_test", "contact_id": "contact_test", "to": state.get("draft_to", "owner@example.invalid"),
                                     "revision": "a" * 64, "batch_id": "batch_test"}],
                         "batches": [{"batch_id": "batch_test", "draft_ids": ["draft_test", "someone_else"] if state.get("mixed_batch") else ["draft_test"],
                                      "approval_digest": "b" * 64, "cap": 1}]}
            elif path.endswith("/gtm/journey/drafts/draft_test"):
                state["approved"] = True
                if state.get("mailbox_after_approval"):
                    state["mailbox_user"] = state["mailbox_after_approval"]
            elif path.endswith("/gtm/journey/send"):
                assert payload["draft_ids"] == ["draft_test"] and state.get("approved")
                state["sent"] += 1
            elif path.endswith("/site/local"):
                reply = {"publication": {"url": "https://your-site.example.test/"} if state.get("published") else None}
            elif path.endswith("/experiments"):
                reply = {"items": [{"id": "exp_test", "hypothesis": "People will try your offer",
                                    "action": "Interview your customers", "measure": "Count commitments",
                                    "result_text": state["results"], "state": "concluded" if state["results"] else "running",
                                    "ran": bool(state["results"]), "evidence_type": "founder_reported"}] if state.get("designed") else []}
                if state.get("scanned"):
                    reply["items"].append({"id": "exp_scan", "state": "concluded", "runner": "public_search",
                                           "result": {"insight": "You have sourced findings.", "sources": [{"url": "https://source.example.test/scan"}]}})
            elif path.endswith("/site/export"):
                media = "text/html"
                if state["require_page"] and not state.get("built"):
                    status = 404
                elif state.get("page_waits", 0):
                    state["page_waits"] -= 1
                    status = 404
                else:
                    reply = "<!doctype html><html><body>Your page</body></html>"
            else:
                status = 404
            body = reply.encode() if isinstance(reply, str) else json.dumps(reply).encode()
            self.send_response(status)
            self.send_header("Content-Type", media)
            self.end_headers()
            self.wfile.write(body)

        def session(self):
            return {"id": "ise_test", "state": "confirmed" if state.get("confirmed") else "reviewing" if state.get("reviewing") else "clarifying",
                    "current_revision": {"id": "rev_test", "structured": {}},
                    "turns": [{"role": "agent", "kind": "question", "text": "Who needs your offer?"}] +
                             ([{"role": "founder", "text": state["answer"]}] if state["answer"] else [])}

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def value_arguments(base_url, tmp_path, scenario):
    return [base_url + "/api", "--idea", scenario[0], "--answer", scenario[1],
            "--results", "I interviewed five people; two agreed to try it.",
            "--output", str(tmp_path / "live.html"), "--timeout", "0.05"]


@pytest.mark.parametrize("domain", ["logistics.example", "pottery.example"])
def test_console_http_client_uses_your_document_session_not_the_installation_secret(acceptance, api, tmp_path, domain):
    base_url, state = api
    state["proxy"] = True
    client = acceptance.Client(base_url + "/api", 1, owner_secret_file=tmp_path / domain)
    assert client.request("GET", "/setup/status")["mode"] == "local"
    assert client.request("GET", "/local/settings/catalog")["sections"]
    assert state["navigations"] == 1
    assert not (tmp_path / domain).exists()


def test_live_value_journey_through_console(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", require_page=True)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario)) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 20
    assert sum(line.startswith("PASS ") for line in lines) == 18
    assert sum(line.startswith("SKIP ") for line in lines) == 2
    assert (tmp_path / "live.html").is_file()
    assert not any(path.endswith("/send") or path.endswith("/deploy") for _, path, _ in state["requests"])
    assert all("/api/api/" not in path for _, path, _ in state["requests"])


def test_normal_journey_skips_requested_fake_builds(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", require_page=True)
    arguments_file = tmp_path / "case.json"
    arguments_file.write_text(json.dumps({"idea": scenario[0], "answer": scenario[1],
                                          "results": "Interview notes", "fake_coding_tool": True}))
    output = tmp_path / "page.html"
    output.write_text("Your existing export")
    assert value_journey.main([base_url + "/api", "--arguments-file", str(arguments_file),
                               "--output", str(output)]) == 0
    report = capsys.readouterr().out
    assert "PASS ask what next:" in report
    assert "SKIP export landing page: " + value_journey.FAKE_GATE_STEP in report
    assert output.read_text() == "Your existing export"
    assert not any("coding-agents" in path or "device-work-orders" in path or "/site/" in path
                   or "Build a landing page" in (body or {}).get("message", "")
                   for _, path, body in state["requests"])


@pytest.mark.parametrize("saved_cap", [None, "0.00"])
def test_value_scan_sets_owner_switch_and_preserves_saved_spend_cap(value_journey, scenario, tmp_path, saved_cap):
    requests = []

    class Client:
        def request(self, method, path, payload=None):
            requests.append((method, path, payload))
            if path == "/local/settings/spend":
                return {"values": {"monthly_spend_cap": saved_cap}}
            if path == "/local/capabilities/run_for_me_experiments":
                return {"name": "run_for_me_experiments", "state": "ready"}
            if method == "GET":
                return {"items": [] if len(requests) < 5 else [{"id": "result", "kind": "experiment_result",
                    "body_md": scenario[0], "citations": [{"url": "https://source.example.test/scan"}]}]}
            return {"body_md": scenario[0]}

    check = value_journey.ValueJourney(Client(), value_journey.arguments(value_arguments("http://127.0.0.1:3100", tmp_path, scenario)))
    check.venture = "venture"
    check.scan()
    expected = [("GET", "/local/settings/spend", None)]
    if saved_cap is None:
        expected.append(("PUT", "/local/settings/spend", {"monthly_spend_cap": "5.00"}))
    expected.append(("PUT", "/local/capabilities/run_for_me_experiments", {"on": True}))
    assert requests[:len(expected)] == expected
    assert sum(method == "PUT" and path == "/local/settings/spend" for method, path, _ in requests) == int(saved_cap is None)


def test_value_gate_builds_with_consent_and_checks_both_results(value_journey, scenario, tmp_path):
    requests = []
    name = scenario[0]
    output = tmp_path / "page.html"
    page = "<html><main>Your checked page</main></html>"
    responses = iter([
        ("GET", "/local-tools/coding-agents", {"tools": [{"name": "codex", "candidate": "reviewed-codex",
                                                       "candidate_path": str((Path.home() / "bin/codex").resolve())}]}),
        ("POST", "/ui-config/coding-agents/consent", {"consent": {"codex": True}}),
        ("POST", "/ventures/venture/chat", {"body_md": "Page requested", "actions": [{"payload": {"job_id": "page"}}]}),
        ("GET", "/ventures/venture/pagebuilder/jobs/page", {"state": "ready", "order_id": "page_order"}),
        ("GET", "/ventures/venture/device-work-orders/page_order/iterations", {"iterations": [{"outcome": "pass"}]}),
        ("GET", "/ventures/venture/pagebuilder/jobs/page/preview", page),
        ("GET", "/ventures/venture/site/local/export.html?job_id=page", page),
        ("GET", "/ventures/venture", {"name": name}),
        ("POST", "/ventures/venture/chat", {"kind": "build_tool_started", "body_md": "Product requested",
                                           "actions": [{"payload": {"order_id": "plan"}}]}),
        ("POST", "/native/device-work-orders/venture/plan/start", {"order_id": "plan"}),
        ("GET", "/native/device-work-orders/venture/plan/status", {"next_order_id": "implementation"}),
        ("GET", "/native/device-work-orders/venture/implementation/status", {"state": "stopped", "task_verified": True}),
        ("GET", "/ventures/venture/device-work-orders/implementation/iterations", {"iterations": [{"outcome": "pass"}]}),
    ])

    class Client:
        def request(self, method, path, payload=None, *, html=False):
            expected_method, expected_path, response = next(responses)
            assert (method, path) == (expected_method, expected_path)
            assert html is isinstance(response, str)
            requests.append(payload)
            return response

    args = value_journey.arguments(value_arguments("http://127.0.0.1:3100", tmp_path, scenario) + ["--output", str(output)])
    journey = value_journey.ValueJourney(Client(), args)
    journey.venture = "venture"
    for _label, method in value_journey.journey_steps(True):
        if method in {"connect", "build_page", "preview_export", "start_product", "check_product"}:
            assert getattr(journey, method)()
    assert next(responses, None) is None
    assert requests[1] == {"provider": "codex", "allow": True, "candidate": "reviewed-codex"}
    assert requests[2]["message"] == "Build my landing page with Codex"
    assert requests[8]["message"] == f"Build the first version of {name} with Codex"
    assert output.read_text() == page


@pytest.mark.parametrize("domain", DOMAINS, ids=["b2b", "consumer"])
def test_value_gate_admits_both_builds_with_pinned_codex(local_stack, monkeypatch, domain):
    from types import SimpleNamespace

    core, _, bin_dir = local_stack
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from local_value_journey import AcceptanceFailure, Client, ValueJourney, prepare_fake_coding_tool

    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", "1")
    prepare_fake_coding_tool(bin_dir, [True])
    client = Client(str(core.base_url), 30)
    journey = ValueJourney(client, SimpleNamespace(timeout=30))
    journey.venture = saved_venture(core, domain)
    journey.connect(bin_dir / "codex")
    journey.capability_list()
    journey.build_page()

    page = client.request("GET", journey.path(f"/pagebuilder/jobs/{journey.page_job_id}"))
    order_path = journey.path(f"/device-work-orders/{page['order_id']}")
    order = client.request("GET", order_path)
    assert order["state"] == "approved" and not order.get("claimed_at")
    with pytest.raises(AcceptanceFailure, match="You already have a build on this computer"):
        journey.start_product()

    cancelled = client.request("POST", order_path + "/cancel", {"review_digest": order["review_digest"]})
    assert cancelled["state"] == "cancelled"
    try:
        journey.start_product()
        assert journey.product_order
    finally:
        if journey.product_order:
            client.request("POST", journey.product_order + "/cancel")


@pytest.mark.parametrize("opt_in, switch, packaged, frozen, allowed", [
    (True, "1", "", False, True),
    (False, "1", "", False, False),
    (True, "true", "", False, False),
    (True, "1", "yes", False, False),
    (True, "1", "", True, False),
])
def test_fake_coding_cli_is_only_installed_in_opted_in_source_gate(
    value_journey, scenario, tmp_path, monkeypatch, opt_in, switch, packaged, frozen, allowed,
):
    import subprocess
    import sys

    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", switch)
    monkeypatch.setenv("COMPANY_OS_PACKAGED", packaged)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    arguments_file = tmp_path / "case.json"
    arguments_file.write_text(json.dumps({"idea": scenario[0], "answer": scenario[1], "results": "Interview notes",
                                         "fake_coding_tool": opt_in}))
    args = value_journey.arguments(["--arguments-file", str(arguments_file), "--output", str(tmp_path / "page.html"),
                                    "--gate-child"])
    directory = tmp_path / "bin"
    if opt_in and not allowed:
        with pytest.raises(value_journey.AcceptanceFailure, match="source checkout"):
            value_journey.prepare_fake_coding_tool(directory, [args.fake_coding_tool])
    else:
        value_journey.prepare_fake_coding_tool(directory, [args.fake_coding_tool])
    assert (directory / "codex").is_file() is allowed
    if allowed:
        binary = directory / "codex"
        (directory / "page-fixture.json").write_text(json.dumps({"root": str(Path(__file__).resolve().parents[2])}))
        result = subprocess.run([binary, "exec", "--json", "--cd", str(tmp_path),
            "Original immutable reviewed instruction (verbatim):\nPAGE_SOURCE_REVIEW\nCONFIRMED_FACTS:\n"
            + json.dumps({"idea": scenario[0]}) + "\nPUBLIC_COPY_BY_FIELD:\n{}\nSANITIZED_SVG_BY_ASSET:\n{}"
            "\nVERIFIED_IMAGE_ORIGIN_BY_ALT_FIELD:\n{}"], capture_output=True, cwd=tmp_path)
        assert result.returncode == 0
        assert json.loads((tmp_path / "review/capability-review.json").read_text())["approved"] is True


@pytest.mark.parametrize("reply,expected", [
    ({"service": "worker", "ready": True}, True),
    ({"service": "worker", "ready": False}, False),
    ({"service": "core", "ready": True}, False),
    ({"service": "worker", "ready": True, "padding": "x" * 70000}, False),
    ([], False), (None, False),
])
def test_value_gate_checks_the_local_worker_loop_not_its_hosted_marker(value_journey, scenario, reply, expected):
    import io
    from types import SimpleNamespace
    from urllib.error import URLError

    def open_ready(url, timeout):
        assert url == "http://127.0.0.1:3301/ready" and timeout == 2
        if reply is None:
            raise URLError("Worker unavailable")
        body = {**reply, "worker_id": scenario[0]} if isinstance(reply, dict) else reply
        return io.BytesIO(json.dumps(body).encode())

    client = SimpleNamespace(opener=SimpleNamespace(open=open_ready))
    assert value_journey.worker_ready(client, 3301) is expected


@pytest.mark.parametrize("audience", ["freight dispatchers", "home bakers"])
def test_live_value_journey_accepts_saved_owner_reported_progress(value_journey, audience):
    from types import SimpleNamespace

    report = f"I interviewed two {audience}; one agreed to a follow-up. Three interviews remain."
    journey = value_journey.ValueJourney(None, SimpleNamespace(results=report, timeout=.01))
    journey.experiment_ids = {"exp_requested"}
    messages = []
    journey.chat = messages.append
    journey.experiments = lambda: [{"id": "exp_requested", "result_text": report,
        "state": "running", "ran": True, "evidence_type": "founder_reported"}]
    assert journey.results() == "Your reported experiment results are saved"
    assert journey.results_recorded and report in messages[0]


@pytest.mark.parametrize("broken", [{"source_count": 2}, {"contact_count": 2}, {"unsaved": True},
                                   {"draft_body": "No compliance footer"}, {"llm_mode": "offline"}, {"scan_unsourced": True}])
def test_live_value_journey_rejects_missing_evidence(value_journey, api, tmp_path, scenario, capsys, broken):
    base_url, state = api
    state.update(proxy=True, llm_mode="live")
    state.update(broken)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario)) == 1
    assert "FAIL " in capsys.readouterr().out


def test_live_page_build_is_requested_once_while_waiting(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", page_waits=1)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario)) == 0, capsys.readouterr().out
    builds = [body for method, path, body in state["requests"]
              if method == "POST" and path.endswith("/chat") and body["message"].startswith("Build ")]
    assert len(builds) == 1


@pytest.mark.parametrize("missing", ["/local/settings/catalog", "/local/capabilities"])
def test_new_local_routes_skip_only_when_missing(value_journey, api, tmp_path, scenario, capsys, missing):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", missing=missing)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario)) == int(missing == "/local/capabilities")
    output = capsys.readouterr().out
    assert "SKIP " in output and "404" in output
    if missing == "/local/capabilities":
        assert "FAIL run for me scan:" in output
    assert "secret-value-must-not-appear" not in output


def test_self_send_rejects_a_different_mailbox(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live")
    arguments = value_arguments(base_url, tmp_path, scenario) + ["--send-to-self", "someone@example.invalid"]
    assert value_journey.main(arguments) == 1
    assert not any(path.endswith("/send") for _, path, _ in state["requests"])
    assert "someone@example.invalid" not in capsys.readouterr().out


def test_self_send_refuses_if_mailbox_changes_after_draft_approval(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", mailbox_after_approval="owner@new-mailbox.test")
    arguments = value_arguments(base_url, tmp_path, scenario) + ["--send-to-self", "owner@example.invalid"]
    assert value_journey.main(arguments) == 1
    assert state["approved"] and state["sent"] == 0
    assert not any(path.endswith("/send") for _, path, _ in state["requests"])
    assert "Open Keys and connections > Email and retry with your current mailbox address after --send-to-self." in capsys.readouterr().out


@pytest.mark.parametrize("broken", [{}, {"draft_to": "other@example.invalid"}, {"mixed_batch": True}, {"no_smtp": True}, {"connection_failed": True}])
def test_self_send_is_bounded_to_your_mailbox(value_journey, api, tmp_path, scenario, capsys, broken):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", **broken)
    arguments = value_arguments(base_url, tmp_path, scenario) + ["--send-to-self", "owner@example.invalid"]
    result = value_journey.main(arguments)
    lines = capsys.readouterr().out
    assert state["sent"] == (0 if broken else 1)
    assert result == (1 if broken and not broken.get("no_smtp") else 0), lines
    if not broken:
        [sent] = [body for method, path, body in state["requests"] if method == "POST" and path.endswith("/send")]
        assert sent["self_only"] is True
        assert any(path.endswith("/chat") and body.get("message") == "Check replies"
                   for _, path, body in state["requests"])
        assert any(path.endswith("/smtp/test") for _, path, _ in state["requests"])


@pytest.mark.parametrize("show", [False, True])
def test_failed_charlie_replies_are_opt_in_and_redacted(value_journey, api, tmp_path, scenario, capsys, show):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", confirmation=True,
                 reply='Your password=private-value token=private-value "api_key": "private-value" ' + "Long reply " * 60)
    arguments = value_arguments(base_url, tmp_path, scenario) + (["--show-replies"] if show else [])
    assert value_journey.main(arguments) == 1
    output = capsys.readouterr().out
    replies = [line.partition(": ")[2] for line in output.splitlines() if line.startswith("REPLY ")]
    assert bool(replies) is show
    assert all(len(reply) <= 300 for reply in replies)
    assert "private-value" not in output


def test_malformed_charlie_reply_never_prints_a_body(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", confirmation=True, reply={"token": "private-value"})
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario) + ["--show-replies"]) == 1
    assert "private-value" not in capsys.readouterr().out


def test_blocked_capability_names_the_required_setting(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", blocked_capability=True)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario)) == 1
    assert "Open Keys and connections > AI service and save your model provider key." in capsys.readouterr().out


def test_publication_requires_an_explicit_request(value_journey, api, tmp_path, scenario, capsys):
    base_url, state = api
    state.update(proxy=True, llm_mode="live")
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario) + ["--publish"]) == 0, capsys.readouterr().out
    assert state["published"] and state["sent"] == 0
    assert any(path.endswith("/site/local") for _, path, _ in state["requests"])
    assert not any(path.endswith("/site/publication") for _, path, _ in state["requests"])


@pytest.mark.parametrize("example", ["enterprise_health.json", "consumer_fintech.json"])
def test_supplied_argument_files_cover_distinct_audiences(value_journey, tmp_path, example):
    path = Path(__file__).resolve().parents[2] / "scripts/local_journeys" / example
    args = value_journey.arguments(["--arguments-file", str(path), "--output", str(tmp_path / "new.html")])
    assert args.idea and args.answers and args.results
    assert not args.publish and not args.send_to_self


@pytest.mark.parametrize("missing", ["/local/settings/imap/test", "/local/settings/smtp/test"])
def test_connection_test_routes_skip_when_missing(value_journey, api, tmp_path, scenario, capsys, missing):
    base_url, state = api
    state.update(proxy=True, llm_mode="live", missing=missing)
    assert value_journey.main(value_arguments(base_url, tmp_path, scenario) + ["--send-to-self", "owner@example.invalid"]) == 0
    assert state["sent"] == (0 if "smtp/test" in missing else 1)
    assert "404" in capsys.readouterr().out


@pytest.mark.parametrize("bad", ["http://127.0.0.1:3300", "https://remote.example/api", "http://user:password@127.0.0.1/api"])
def test_value_journey_rejects_nonproxy_urls(value_journey, tmp_path, scenario, capsys, bad):
    assert value_journey.main([bad, *value_arguments("http://127.0.0.1:1", tmp_path, scenario)[1:]]) == 1
    assert "password" not in capsys.readouterr().out


def test_gate_preflight_reaches_stack_start_without_site_packages(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[2]
    env_file = tmp_path / "input.env"
    env_file.write_text("APP_ENV=local\n")
    code = """
import os
from pathlib import Path
import sys

root, scratch = map(Path, sys.argv[1:])
sys.path[:0] = [str(root / 'scripts'), str(root / 'packages')]
import local_value_journey as journey

assert 'site' not in sys.modules
os.environ['HOME'] = os.environ['USERPROFILE'] = str(scratch)
os.environ['SUPYMEM_TEST_FAKE_CODING_TOOL'] = '1'
os.environ.pop('COMPANY_OS_PACKAGED', None)
journey.__file__ = str(scratch / 'scripts' / 'local_value_journey.py')

def stop_at_launch(command, *, cwd, env, **kwargs):
    assert cwd == scratch
    assert command == ['bash', 'scripts/run_local.sh', '--web']
    assert (Path(env['COMPANY_OS_DATA_DIR']) / 'bin' / 'codex').is_file()
    assert 'llm_gateway' not in sys.modules
    raise journey.AcceptanceFailure('Reached isolated stack launch.')

journey.subprocess.Popen = stop_at_launch
journey.free_ports = lambda: (3300, 3100, 3301)
examples = root / 'scripts' / 'local_journeys'
assert journey.gate_main(['--env-file', str(scratch / 'input.env'), '--log-dir', str(scratch / 'logs'),
    '--arguments-file', str(examples / 'consumer_fintech.json'),
    '--arguments-file', str(examples / 'enterprise_health.json')]) == 1
"""
    run = subprocess.run([sys.executable, "-I", "-S", "-c", code, str(root), str(tmp_path)],
                         capture_output=True, text=True, check=True, timeout=15)
    assert "Next step: Reached isolated stack launch." in run.stdout


def test_offline_gate_stages_both_fake_tool_journeys_without_home(value_journey, monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HOME", str(tmp_path / "missing-home"))
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    monkeypatch.setattr(value_journey.tempfile, "tempdir", None)
    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", "1")
    monkeypatch.delenv("COMPANY_OS_PACKAGED", raising=False)
    examples = Path(value_journey.__file__).parent / "local_journeys"

    def stop_before_launch(directory, selections, **options):
        assert selections == [True, True]
        gate_data = directory.parent.parent
        assert gate_data.parent == tmp_path
        assert gate_data.stat().st_mode & 0o777 == 0o700
        raise value_journey.AcceptanceFailure("Reached private tool staging.")

    monkeypatch.setattr(value_journey, "prepare_fake_coding_tool", stop_before_launch)
    assert value_journey.gate_main(["--offline", "--log-dir", str(tmp_path / "logs"),
        "--arguments-file", str(examples / "enterprise_health.json"),
        "--arguments-file", str(examples / "consumer_fintech.json")]) == 1
    assert "Next step: Reached private tool staging." in capsys.readouterr().out


def test_gate_reports_only_redacted_first_line_of_preflight_error(value_journey, tmp_path, monkeypatch, capsys, scenario):
    secret = "private-fixture-value"
    env_file = tmp_path / "input.env"
    env_file.write_text(f"SMTP_PASSWORD={secret}\n")
    cases = []
    for index in range(2):
        path = tmp_path / f"case{index}.json"
        path.write_text(json.dumps({"idea": scenario[0], "answer": scenario[1], "results": "Interview notes"}))
        cases.extend(["--arguments-file", str(path)])

    def unavailable(*args, **kwargs):
        raise ImportError(f"Cannot load {secret}\nDo not show subsequent lines")

    monkeypatch.setattr(value_journey, "prepare_fake_coding_tool", unavailable)
    assert value_journey.gate_main(["--env-file", str(env_file), "--log-dir", str(tmp_path / "logs"), *cases]) == 1
    report = capsys.readouterr().out
    assert "Next step: ImportError: Cannot load [redacted]" in report
    assert secret not in report and "Do not show subsequent lines" not in report


@pytest.mark.parametrize("failure", ["", "traceback", "lock", "check", "empty", "stack_fail"])
def test_gate_isolates_logs_and_stops_started_children(value_journey, api, tmp_path, monkeypatch, capsys, scenario, failure):
    import os
    import sys
    import time
    from core.local_security import _proxy_secret

    base_url, state = api
    state.update(proxy=True, llm_mode="live")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    examples = scripts / "local_journeys"
    examples.mkdir()
    monkeypatch.setenv("SUPYMEM_TEST_FAKE_CODING_TOOL", "1")
    for index in (1, 2):
        (examples / f"case{index}.json").write_text(json.dumps({"idea": scenario[0], "answer": scenario[1], "results": "I interviewed five people.",
                                                                "fake_coding_tool": index == 2}))
    labels = {selected: [label for label, _method in value_journey.journey_steps(selected)] for selected in (False, True)}
    stub = "import os, sys, shutil\nfrom pathlib import Path\nselected = os.environ.get('SUPYMEM_TEST_FAKE_CODING_TOOL') == '1'\n"
    stub += "assert (shutil.which('codex') == str(Path(os.environ['COMPANY_OS_DATA_DIR']) / 'bin' / 'codex')) is selected\nif selected:\n"
    stub += "    Path(sys.argv[sys.argv.index('--output') + 1]).write_text('<html>Your private page</html>')\n"
    stub += "labels = (" + repr(labels) + ")[selected]\n"
    stub += "for label in labels:\n    if os.environ.get('TEST_FAILURE') != 'empty':\n        print(('FAIL' if os.environ.get('TEST_FAILURE') == 'check' else 'PASS') + ' ' + label + ': Continue in your app.')\n"
    (scripts / "local_value_journey.py").write_text(stub)
    launcher = "#!/usr/bin/env bash\nexec \"$SUPYMEM_PYTHON\" - <<'PY'\nimport os, subprocess, sys, time\nfrom pathlib import Path\n"
    launcher += "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True)\n"
    launcher += "Path('child.pid').write_text(str(child.pid))\n"
    launcher += "secret_file = Path(os.environ['SUPYMEM_LOCAL_PROXY_SECRET_FILE'])\n"
    launcher += "secret_file.write_text(" + repr(_proxy_secret().decode("ascii")) + ")\nsecret_file.chmod(0o600)\n"
    launcher += "Path('isolation.json').write_text(__import__('json').dumps({'database': os.environ['COMPANY_OS_DB_URL'], 'data': os.environ['COMPANY_OS_DATA_DIR'], 'ambient': bool(os.environ.get('AMBIENT_PRIVATE_TOKEN')), 'redis': bool(os.environ.get('REDIS_URL')), 'offline': os.environ['LLM_OFFLINE'], 'publishing_seeded': bool(os.environ.get('SUPYMEM_PUBLIC_URL')), 'home_isolated': os.environ.get('HOME') == os.environ['COMPANY_OS_DATA_DIR'], 'worker_health': os.environ.get('COS_WORKER_HEALTH_PATH', '')}))\n"
    launcher += "print(__import__('json').dumps(os.environ['SMTP_PASSWORD']), flush=True)\n"
    launcher += "if os.environ.get('TEST_FAILURE') == 'traceback': print('Traceback (most recent call last):', flush=True)\n"
    launcher += "if os.environ.get('TEST_FAILURE') == 'lock': print('database is locked', flush=True)\n"
    launcher += "if os.environ.get('TEST_FAILURE') == 'stack_fail': print('FAIL startup: Check your setup, then retry.', flush=True)\n"
    launcher += "time.sleep(60)\nPY\n"
    (scripts / "run_local.sh").write_text(launcher)
    env_file = tmp_path / "input.env"
    env_file.write_text(f'SMTP_PASSWORD=gate-private-"value\nTEST_FAILURE={failure}\nDATABASE_URL=postgresql://remote.example/production\nREDIS_URL=redis://remote.example\nSUPYMEM_LOCAL_PROXY_SECRET_FILE={tmp_path / "unrelated.secret"}\n')
    (tmp_path / ".env").write_text("AMBIENT_PRIVATE_TOKEN=never-use-this\n")
    monkeypatch.setenv("AMBIENT_PRIVATE_TOKEN", "never-use-this")
    monkeypatch.setattr(value_journey, "__file__", str(scripts / "local_value_journey.py"))
    monkeypatch.setattr(value_journey, "free_ports", lambda: [34001, int(base_url.rsplit(":", 1)[1]), 34003])
    monkeypatch.setattr(value_journey, "worker_ready", lambda _client, _port: True)
    logs = tmp_path / "logs"
    assert value_journey.gate_main(["--env-file", str(env_file), "--log-dir", str(logs), "--timeout", "2",
        "--arguments-file", str(examples / "case1.json"), "--arguments-file", str(examples / "case2.json"),
        "--page-fixtures", str(Path(__file__).resolve().parents[2] / "tests/unit/pagebuilder/fixtures/local_tool_pages")]) == int(bool(failure))
    output = capsys.readouterr().out
    assert len(output.splitlines()) == 20
    assert 'gate-private-"value' not in output
    assert json.dumps('gate-private-"value')[1:-1] not in (logs / "stack.log").read_text()
    assert "[redacted]" in (logs / "stack.log").read_text()
    assert (logs.stat().st_mode & 0o777) == 0o700
    assert (logs / "stack.log").stat().st_mode & 0o077 == 0
    assert (logs / "journey-1.log").is_file() and (logs / "journey-2.log").is_file()
    assert not (logs / "journey-1.html").exists() and (logs / "journey-2.html").is_file()
    isolation = json.loads((tmp_path / "isolation.json").read_text())
    assert isolation["database"].startswith("sqlite:///")
    assert not isolation["ambient"] and not isolation["redis"] and isolation["offline"] == "0"
    assert not isolation["publishing_seeded"] and isolation["home_isolated"]
    assert Path(isolation["worker_health"]).parent == Path(isolation["data"])
    assert not Path(isolation["data"]).exists()
    child_pid = int((tmp_path / "child.pid").read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline and Path(f"/proc/{child_pid}/stat").exists():
        if Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z":
            break
        time.sleep(0.01)
    if Path(f"/proc/{child_pid}/stat").exists():
        assert Path(f"/proc/{child_pid}/stat").read_text().split()[2] == "Z"
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


def test_proxy_preserves_versioned_routes(acceptance, api):
    base_url, state = api
    state["proxy"] = True
    client = acceptance.Client(base_url + "/api", 1)
    client.request("GET", "/api/v1/ventures/ven_test/gtm/journey")
    assert state["requests"][0][1] == "/"
    assert state["requests"][-1][1] == "/api/v1/ventures/ven_test/gtm/journey"


def test_http_founder_journey(acceptance, api, tmp_path, capsys, scenario):
    idea, answer = scenario
    base_url, state = api
    output = tmp_path / "page.html"
    result = acceptance.main([base_url, "--stub", "--idea", idea, "--answer", answer,
                              "--results", "I interviewed 5 people yesterday; 2 committed to try it.",
                              "--output", str(output), "--timeout", "1"])
    lines = capsys.readouterr().out.splitlines()
    assert result == 0, lines
    assert len(lines) == 11
    assert all(line.startswith("PASS ") for line in lines)
    assert output.read_text().startswith("<!doctype html>")
    assert state["answer"] == answer
    assert state["requests"][1][2]["raw_idea"] == idea
    assert state["requests"][1][2]["start"] is False
    assert not any("send" in path for _, path, _ in state["requests"])
    assert next(body for _, path, body in state["requests"] if path.endswith("/confirm"))["start_research"] is False


def test_stub_invitations_import_a_permitted_contact_first(acceptance, api, tmp_path, capsys, scenario):
    base_url, state = api
    state["require_contact"] = True
    assert acceptance.main([base_url, "--stub", "--idea", scenario[0], "--answer", scenario[1],
        "--results", "I interviewed three people.", "--output", str(tmp_path / "page.html"), "--timeout", "1"]) == 0
    commit = next(body for _, path, body in state["requests"] if path.endswith("/contacts/import"))
    assert commit["lawful_basis_attested"] is True and commit["lawful_basis"] == "consent"
    preview = next(body for _, path, body in state["requests"] if path.endswith("/import/preview"))
    assert ".invalid" in preview["text"]
    assert "FAIL " not in capsys.readouterr().out


@pytest.mark.parametrize("permission", [False, True])
def test_nonstub_contact_requires_your_permission(acceptance, api, tmp_path, capsys, scenario, permission):
    base_url, state = api
    arguments = [base_url, "--idea", scenario[0], "--answer", scenario[1],
        "--results", "I interviewed three people.", "--contact-email", "permitted@example.invalid",
        "--output", str(tmp_path / "page.html"), "--timeout", "1"]
    if permission:
        arguments.append("--contact-permission")
    assert acceptance.main(arguments) == (0 if permission else 1)
    imported = any(path.endswith("/contacts/import") for _, path, _ in state["requests"])
    assert imported is permission
    output = capsys.readouterr().out
    assert ("FAIL prepare invitations" in output) is (not permission)


def test_export_builds_the_requested_page_first(acceptance, api, tmp_path, capsys, scenario):
    base_url, state = api
    state["require_page"] = True
    assert acceptance.main([base_url, "--stub", "--idea", scenario[0], "--answer", scenario[1],
        "--results", "I interviewed three people.", "--output", str(tmp_path / "page.html"), "--timeout", "1"]) == 0
    assert state["built"] is True
    assert "FAIL " not in capsys.readouterr().out


@pytest.mark.parametrize("live", [False, True])
def test_missing_page_export_names_your_coding_tool_setting(acceptance, value_journey, scenario, live):
    from types import SimpleNamespace

    journey_type = value_journey.ValueJourney if live else acceptance.Journey
    journey = journey_type(None, SimpleNamespace(timeout=.01, idea=scenario[0], answer=scenario[1]))
    journey.page = lambda: ""
    journey.chat = lambda _message: None
    if live:
        journey.available = lambda *_args: None
    with pytest.raises(acceptance.AcceptanceFailure, match="Settings > Keys and connections > Coding tools"):
        journey.export()


@pytest.mark.parametrize("missing", ["/setup/status", "/site/export", "/ventures"])
def test_missing_features_fail_without_response_secrets(acceptance, api, tmp_path, capsys, missing, scenario):
    base_url, state = api
    state["missing"] = missing
    result = acceptance.main([base_url, "--stub", "--idea", scenario[0],
                              "--answer", scenario[1], "--results", "I tested it with 3 people.",
                              "--output", str(tmp_path / "page.html"), "--timeout", "1"])
    captured = capsys.readouterr()
    assert result == 1
    assert len(captured.out.splitlines()) == 11
    assert "FAIL " in captured.out
    assert "secret-value-must-not-appear" not in captured.out + captured.err


def test_requested_work_confirmation_is_not_accepted(acceptance, api, tmp_path, capsys, scenario):
    base_url, state = api
    state["confirmation"] = True
    assert acceptance.main([base_url, "--idea", scenario[0],
                            "--answer", scenario[1], "--results", "I tested it with 3 people.",
                            "--output", str(tmp_path / "page.html"), "--timeout", "1"]) == 1
    assert "FAIL ask for customers" in capsys.readouterr().out


def test_empty_customer_list_is_not_a_pass(acceptance, api, tmp_path, capsys, scenario):
    base_url, state = api
    state["empty"] = True
    assert acceptance.main([base_url, "--idea", scenario[0],
                            "--answer", scenario[1], "--results", "I tested it with 3 people.",
                            "--output", str(tmp_path / "page.html"), "--timeout", "0.05"]) == 1
    assert "FAIL ask for customers" in capsys.readouterr().out


def test_export_never_overwrites_your_file(acceptance, api, tmp_path, capsys, scenario):
    output = tmp_path / "page.html"
    output.write_text("keep your work")
    assert acceptance.main([api[0], "--idea", scenario[0],
                            "--answer", scenario[1], "--results", "I tested it with 3 people.",
                            "--output", str(output), "--timeout", "1"]) == 1
    assert output.read_text() == "keep your work"
    assert "FAIL export landing page" in capsys.readouterr().out


@pytest.mark.parametrize("url", ["https://external.test", "http://secret@localhost", "http://localhost?token=secret"])
def test_only_local_urls_without_embedded_secrets(acceptance, url, capsys, scenario):
    assert acceptance.main([url, "--idea", scenario[0],
                            "--answer", scenario[1], "--results", "I tested it with 3 people."]) == 1
    captured = capsys.readouterr()
    assert "secret" not in captured.out + captured.err


def test_stub_never_proceeds_against_reported_live_ai(acceptance, api, capsys, scenario):
    base_url, state = api
    state["llm_mode"] = "live"
    assert acceptance.main([base_url, "--stub", "--idea", scenario[0], "--answer", scenario[1],
                            "--results", "I tested it with 3 people."]) == 1
    assert len(state["requests"]) == 1
    assert len(capsys.readouterr().out.splitlines()) == 11


@pytest.mark.parametrize("failure", ["caller", "server", "caller_email", "server_email", "url"])
def test_setup_failure_explains_the_setting_and_skips_work(acceptance, api, monkeypatch, capsys, scenario, failure):
    base_url, state = api
    if failure == "caller":
        monkeypatch.setenv("LLM_OFFLINE", "0")
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    elif failure == "server":
        state["llm_mode"] = "live"
    elif failure == "caller_email":
        monkeypatch.setenv("EMAIL_PROVIDER", "smtp")
    elif failure == "server_email":
        state["email_mode"] = "replay"
    else:
        base_url = "http://external.invalid"
    assert acceptance.main([base_url, "--stub", "--idea", scenario[0], "--answer", scenario[1],
        "--results", "I interviewed three people."]) == 1
    output = capsys.readouterr().out
    assert output.count("FAIL ") == 1 and output.count("SKIP ") == 10
    assert len(state["requests"]) == (1 if failure in {"server", "server_email"} else 0)
    if failure == "server":
        assert "llm_mode=live" in output and "LLM_OFFLINE=1" in output
    elif failure == "caller":
        assert "LLM_OFFLINE=1" in output and "LLM_CASSETTE_MODE=replay" in output
    elif failure in {"caller_email", "server_email"}:
        assert "EMAIL_PROVIDER=log" in output
        if failure == "server_email":
            assert "email_mode=replay" in output
    else:
        assert "loopback" in output


def test_stub_accepts_saved_sample_mode_without_allowing_live_ai(acceptance, api, scenario):
    from types import SimpleNamespace

    api[1]["llm_mode"] = "sample"
    journey = acceptance.Journey(acceptance.Client(api[0], timeout=1), SimpleNamespace(stub=True, idea=scenario[0]))
    assert journey.setup() == "Your local setup status is available"
    api[1]["llm_mode"] = "live"
    with pytest.raises(acceptance.AcceptanceFailure, match="Your setup reports"):
        journey.setup()


@pytest.mark.parametrize("offline,cassette", [("1", "off"), ("true", "off"), ("0", "replay")])
def test_stub_accepts_either_offline_or_replay(acceptance, api, monkeypatch, tmp_path, scenario, offline, cassette):
    monkeypatch.setenv("LLM_OFFLINE", offline)
    monkeypatch.setenv("LLM_CASSETTE_MODE", cassette)
    assert acceptance.main([api[0], "--stub", "--idea", scenario[0], "--answer", scenario[1],
        "--results", "I interviewed three people.", "--output", str(tmp_path / "page.html")]) == 0


@pytest.mark.parametrize("idea,audience,report", [
    ("A tool that helps freight brokers reconcile invoices", "Independent freight brokers with small accounting teams",
     "Three of five brokers agreed to a call; two asked for pricing."),
    ("A kit for apartment residents without balconies to grow herbs indoors", "I spoke with four apartment residents without balconies who grow plants indoors.",
     "Two of four residents asked for a trial; one said they would decide next month."),
    ("Predictive maintenance software for wind farm operators", "Asset managers at mid-size wind farm operators",
     "Three of five operators agreed to a 20 minute call; two asked for pricing."),
    ("Remote monitoring for patients after knee replacement surgery", "Orthopedic clinics with high surgery volume",
     "Two of four clinics asked for a pilot; one said budget is set in January."),
])
def test_self_hosted_offline_journey_through_http(local_owner_app, acceptance, monkeypatch, tmp_path, idea, audience, report):
    from fastapi.testclient import TestClient
    from core.local_security import _proxy_secret
    import httpx
    from sqlalchemy import select
    from app.core.jobs import lease
    from app.db.models import Job
    from app.state import db
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.config import settings
    import asyncio
    from types import SimpleNamespace
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "true")
    monkeypatch.setenv("GUESSED_EMAILS", "off")
    monkeypatch.setattr(settings, "new_user_flow", "on")
    network_requests = []
    real_client = httpx.AsyncClient
    def offline_dns(request):
        network_requests.append(request.url.host)
        return httpx.Response(200, json={"Status": 3})
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: real_client(**(kwargs | {"transport": httpx.MockTransport(offline_dns)})))
    install_default_handlers()
    kinds = ["idea.clarify_turn", "idea.validate", "gtm.journey.prepare", "pagebuilder.build"]

    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as http:
        http.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        assert http.post("/local/providers/sample", json={}).status_code == 200

        class ProductClient:
            headers = {}

            def request(self, method, path, payload=None, *, html=False):
                for _ in range(30):
                    with db()() as session:
                        jobs = lease(session, "offline-acceptance", kinds=kinds, n=1)
                        session.commit()
                    if not jobs:
                        break
                    asyncio.run(_run_one(db(), jobs[0], {jobs[0].id}))
                response = http.request(method, path, json=payload, headers=self.headers)
                if response.status_code >= 400:
                    raise acceptance.AcceptanceFailure(f"HTTP {response.status_code}: {response.text}", status=response.status_code)
                return response.text if html else response.json()

        journey = acceptance.Journey(ProductClient(), SimpleNamespace(stub=True, idea=idea, answer=audience,
            results=report, output=tmp_path / "page.html", timeout=1))
        failures = []
        for label, method in acceptance.STEPS:
            if method == "export":
                continue
            try:
                getattr(journey, method)()
            except acceptance.AcceptanceFailure as exc:
                failures.append((label, str(exc)))
        with db()() as session:
            errors = [(job.kind, job.status, job.last_error) for job in session.scalars(select(Job).where(Job.last_error.is_not(None)))
                      if job.kind != "idea.clarify_turn" or job.last_error != "clarification_provider_offline"]
        if failures:
            from app.gtm.journey import _icp
            from app.chat.experiments import _experiment_snapshot
            pytest.fail(json.dumps({"failures": failures, "errors": errors, "icp": _icp(journey.venture),
                "scope": (_experiment_snapshot(journey.venture) or {}).get("confirmed_scope")}))
        results = [item for item in journey.experiments() if item["result_text"]]
        assert results and all(item["id"] in journey.experiment_ids and item["evidence_type"] == "sample"
                               and item["ran"] is False and item["outcome"] is None for item in results)
        assert not errors
        assert not network_requests
        response = http.post(f"/ventures/{journey.venture}/chat", json={
            "message": "Build a landing page for my saved idea. Keep it private and do not send anything"},
            headers={"X-Workspace-Id": journey.workspace})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert "coding tool" in reply["body_md"].lower(), reply["body_md"]
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert reply["kind"] != "confirmation"
        assert any(action.get("href") == "/settings/local#coding-tools" for action in reply["actions"])
        with db()() as session:
            assert not session.scalar(select(Job.id).where(Job.venture_id == journey.venture,
                Job.kind == "pagebuilder.build"))
        assert not journey.args.output.exists()


@pytest.mark.parametrize("offline,cassette", [("1", "off"), ("0", "replay")])
@pytest.mark.parametrize("idea,audience", [
    ("A planning tool for enterprise procurement teams", "Procurement managers at large manufacturers"),
    ("A recovery diary for people managing migraines at home", "People managing migraines at home"),
])
def test_zero_key_sample_journey(local_owner_app, acceptance, monkeypatch, tmp_path, idea, audience, offline, cassette):
    import asyncio
    import socket
    import httpx
    from types import SimpleNamespace
    from core.local_security import _proxy_secret

    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.core.jobs import lease
    from app.db.models import GtmTouch, Job
    from app.state import db
    from app.worker.handlers import install_default_handlers
    from app.worker.main import _run_one
    from app.config import settings
    from desktop_runtime import agent_status

    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    monkeypatch.setenv("LLM_OFFLINE", offline)
    monkeypatch.setenv("LLM_CASSETTE_MODE", cassette)
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("CHANNELS_DRY_RUN", "false")
    monkeypatch.setattr(settings, "new_user_flow", "on")
    network_calls = []

    def no_network(*args, **kwargs):
        network_calls.append(True)
        raise AssertionError("Your sample journey must not connect to external services.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", no_network)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", no_network)
    install_default_handlers()
    kinds = ["idea.clarify_turn", "idea.validate", "gtm.journey.prepare", "research.deep"]
    with TestClient(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as http:
        http.headers["X-Local-Proxy-Secret"] = _proxy_secret().decode("ascii")
        assert http.post("/local/providers/sample", json={}).status_code == 200
        from llm_gateway.client import LLMClient
        cached_model = LLMClient()
        messages = [{"role": "user", "content": idea}]
        assert asyncio.run(cached_model.chat(messages, purpose="env_chat_completion")).raw == {"sample": True}

        class ProductClient:
            headers = {}

            def request(self, method, path, payload=None, *, html=False):
                for _ in range(30):
                    with db()() as session:
                        jobs = lease(session, "sample-journey", kinds=kinds, n=1)
                        session.commit()
                    if not jobs:
                        break
                    asyncio.run(_run_one(db(), jobs[0], {jobs[0].id}))
                response = http.request(method, path, json=payload, headers=self.headers)
                if response.status_code >= 400:
                    raise acceptance.AcceptanceFailure(f"HTTP {response.status_code}: {response.text}", status=response.status_code)
                if html:
                    return response.text
                data = response.json()
                if path.endswith("/chat"):
                    assert data["sample"] is True and data["body_md"].startswith("Sample answer")
                    self.last_chat = data
                return data

        client = ProductClient()
        journey = acceptance.Journey(client, SimpleNamespace(stub=True, idea=idea, answer=audience, timeout=1,
            results="I interviewed five people; two committed to try it.", output=tmp_path / "sample.html"))
        journey.create()
        venture = client.request("GET", journey.path(""))
        assert venture["sample"] is True and venture["name"].startswith("Sample: ")
        journey.answer()
        journey.skip()
        journey.confirm()
        research_request = {"request_key": "sample-research-click"}
        started = client.request("POST", journey.path("/research/dossier/regenerate"), research_request)
        repeated = client.request("POST", journey.path("/research/dossier/regenerate"), research_request)
        assert started["job_id"] == repeated["job_id"]
        dossiers = client.request("GET", journey.path("/research/dossiers"))["items"]
        assert dossiers and dossiers[0]["state"] in {"complete", "partial"}
        report = client.request("GET", journey.path("/research/dossiers/" + dossiers[0]["id"]))
        findings = [finding for group in report["findings"].values() for finding in group]
        assert findings and all("Sample" in finding["title"] and "Sample" in finding["source"] for finding in findings)
        assert all(finding["evidence_kind"] == "sample" and not finding["page_verified"] for finding in findings)
        assert all(".example.invalid/" in finding["url"] for finding in findings)
        preview = client.request("GET", journey.path("/research/dossier"))
        assert preview["available"] and preview["mode"] == "sample"
        assert "sample research" in preview["markdown"].lower()
        assert "Settings > Keys and connections > Your AI provider" in preview["markdown"]
        journey.customers()
        status_path = "/api/v1" + journey.path("/gtm/journey")
        status = client.request("GET", status_path)
        assert len(status["prospects"]) == 3
        assert all("Sample" in person["name"] and "Sample" in person["evidence_label"] for person in status["prospects"])
        assert all(not person["eligible"] for person in status["prospects"])
        assert all(person["email"].endswith(".example.invalid") for person in status["prospects"])
        journey.design()
        assert "Sample experiment" in client.last_chat["body_md"]
        assert all(item["source"] == "Sample" for item in journey.experiments())
        assert all(item["hypothesis"].startswith("Sample: ") for item in journey.experiments())
        reply = journey.chat("Prepare email invitations for my saved customer interview experiment. Save drafts only. Do not send anything")
        status = client.request("GET", status_path)
        assert status["drafts"], reply.get("body_md")
        for draft in status["drafts"]:
            assert "Sample" in draft["subject"] and "Sample" in draft["body"]
            assert "Your email address is listed publicly on " in draft["body"]
            assert 'reply "no thanks"' in draft["body"]
            assert status["sender_postal_address"] in draft["body"]
            assert draft["state"] == "draft" and not draft.get("sent_at")
        assert not status["can_send"] and status["results"]["sent"] == 0
        blocked_send = journey.chat("Send it")
        assert "sample email cannot be sent" in blocked_send["body_md"]
        assert "Settings > Keys and connections > Your AI provider" in blocked_send["body_md"]
        person_id = status["prospects"][0]["id"]
        booked = client.request("POST", status_path + f"/prospects/{person_id}/call-booked", {})
        assert booked["manual_outreach"][0]["booked_at"]
        assert "Sample" in booked["manual_outreach"][0]["name"]
        assert not booked["manual_outreach"][0]["sent_at"]
        from app.gtm import local_mail
        from app.gtm.supymem.composio_gmail import DefiniteFailure

        with pytest.raises(DefiniteFailure, match="Sample mode never sends email"):
            with local_mail._smtp(SimpleNamespace()):
                pytest.fail("Your sample must not open an SMTP transport.")
        with pytest.raises(ValueError, match="Sample mode never reads your mailbox"):
            with local_mail._imap():
                pytest.fail("Your sample must not open an IMAP transport.")
        with db()() as session:
            assert not session.scalar(select(GtmTouch.id))
            assert not list(session.scalars(select(Job).where(Job.last_error.is_not(None))))
        result_reply = journey.chat("Results: " + journey.args.results)
        results = [item for item in journey.experiments() if item["result_text"]]
        assert results and all(item["id"] in journey.experiment_ids and item.get("sample") and "Sample" in item["result_text"]
            and item["ran"] is False and item["state"] == "concluded" and item["evidence_type"] == "sample"
            and item["outcome"] is None and not item.get("provenance") for item in results), (result_reply["body_md"], results)
        assert "Your practice result is saved" in result_reply["body_md"]
        page_reply = journey.chat("Build a landing page for my saved idea. Keep it private and do not send anything")
        assert "coding tool" in page_reply["body_md"].lower(), page_reply["body_md"]
        assert "Settings > Keys and connections > Coding tools" in page_reply["body_md"]
        assert page_reply["kind"] != "confirmation"
        assert any(action.get("href") == "/settings/local#coding-tools" for action in page_reply["actions"])
        with db()() as session:
            assert not session.scalar(select(Job.id).where(Job.venture_id == journey.venture,
                Job.kind == "pagebuilder.build"))
        assert not journey.args.output.exists()
        publication = http.post(journey.path("/site/local/publish"), json={})
        assert publication.status_code == 409 and "Your sample page stays private" in publication.text
        catalog = client.request("GET", "/local/settings/catalog")
        assert {section["id"] for section in catalog["sections"]} >= {"providers", "search", "smtp", "publishing"}
        capabilities = client.request("GET", "/local/capabilities")
        assert capabilities["capabilities"]
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
        saved = client.request("POST", "/local/providers", {"provider": "openai", "api_key": "sample-journey-test-key",
            "model": "test-model"})
        assert saved["configured"] and not saved["sample_selected"]
        assert client.request("GET", "/setup/status")["llm_mode"] == "live"
        live = client.request("POST", "/ventures", {"raw_idea": idea, "start": False})
        assert live["sample"] is False
        from llm_gateway import sole
        live_calls = []

        async def live_model(*args, **kwargs):
            live_calls.append(True)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Your own model receives this live request. Tell me what you want to explore next."))],
                usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2), provider_used="openai", model="test-model")

        monkeypatch.setattr(sole, "env_chat_completion", live_model)
        monkeypatch.setattr(sole, "byok_chat_completion", live_model)
        monkeypatch.setattr(sole, "transmit", live_model)
        assert asyncio.run(cached_model.chat(messages, purpose="env_chat_completion")).raw != {"sample": True}
        response = http.post(f"/ventures/{live['id']}/chat", json={"message": "Explain what you understand from my saved idea.", "idempotency_key": "first-live-request"})
        assert response.status_code == 200 and not response.json().get("sample"), response.text
        assert live_calls
        assert client.request("GET", journey.path(""))["sample"] is True
        assert client.request("GET", journey.path("/research/dossier"))["mode"] == "sample"
        journey.customers()
        live_status = http.get(f"/api/v1/ventures/{live['id']}/gtm/journey", headers={"X-Workspace-Id": live["workspace_id"]})
        assert live_status.status_code == 200, live_status.text
        assert not live_status.json()["prospects"]
        live_experiments = http.get(f"/ventures/{live['id']}/experiments", headers={"X-Workspace-Id": live["workspace_id"]})
        assert live_experiments.status_code == 200 and not live_experiments.json()["items"]
        publication = http.post(journey.path("/site/local/publish"), json={})
        assert publication.status_code == 409 and "Your sample page stays private" in publication.text
        client.request("POST", journey.path("/delete"), {"confirmation": "DELETE"})
        active = client.request("GET", "/ventures?summary=true")["items"]
        assert journey.venture not in {item["id"] for item in active}
        assert live["id"] in {item["id"] for item in active}
        assert network_calls == []


@pytest.fixture(params=[
    ("A tool that helps freight brokers reconcile invoices", "Independent freight brokers", "reconciling invoices"),
    ("An indoor herb growing kit for apartment residents", "Apartment residents", "growing herbs indoors"),
], ids=["business", "consumer"])
def requested_work(local_owner_app, acceptance, monkeypatch, tmp_path, request):
    import asyncio
    import socket
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.config import settings
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea.service import append_revision
    from app.state import db
    from app.worker.main import _run_worker
    from tests.unit.test_local_mode import _owner_client

    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.delenv("SUPYMEM_SINGLE_PROCESS", raising=False)
    monkeypatch.delenv("WORKER_JOB_CLASSES", raising=False)
    monkeypatch.setattr(settings, "new_user_flow", "on")
    idea, audience, problem = request.param
    network_requests = []

    def no_network(*args, **kwargs):
        network_requests.append(True)
        raise AssertionError("Your requested-work replay must not connect to external services.")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)

    def drain():
        asyncio.run(_run_worker(once=True, stop=asyncio.Event()))

    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as http:
        assert http.post("/local/providers/sample", json={}).status_code == 200

        class ProductClient:
            headers = {}

            def request(self, method, path, payload=None):
                drain()
                response = http.request(method, path, json=payload, headers=self.headers)
                assert response.status_code < 400, response.text
                return response.json()

        journey = acceptance.Journey(ProductClient(), SimpleNamespace(stub=True, idea=idea,
            answer=audience, output=tmp_path / "requested.html", timeout=1))
        for method in ("create", "answer", "skip", "confirm"):
            getattr(journey, method)()
        http.headers["X-Workspace-Id"] = journey.workspace
        with db()() as session:
            saved = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == journey.venture,
                IdeaSession.state == "confirmed"))
            revision = session.get(IdeaRevision, saved.confirmed_revision_id)
            revision = append_revision(session, saved, {**json.loads(revision.structured), "summary": idea,
                "customer": audience, "problem": problem, "offer": idea}, source="founder", summary=idea)
            saved.confirmed_revision_id = revision.id
            saved.state = "confirmed"
            session.commit()
        yield http, journey.venture, drain, audience, problem
        assert not network_requests


def test_requested_competitors_are_saved_by_local_worker(requested_work, monkeypatch):
    import json
    from sqlalchemy import select
    from app.core import local_mode
    from app.db.models import Job
    from app.db.supymem_models import IdeaRevision
    from app.research import deep, metered
    from app.research.context import current_input
    from app.research.prior_art import runner as prior
    from app.research.source_quality import research_topic
    from app.signals.research import ScrapedText
    from app.state import db

    http, venture_id, drain, audience, problem = requested_work
    with db()() as session:
        _idea, revision_id = current_input(session, venture_id)
        snapshot = json.loads(session.get(IdeaRevision, revision_id).structured)
    title = f"{audience}: {research_topic(snapshot)}"
    excerpt = f"{title}: a product for {audience} who need help {problem}."

    class Provider:
        name = "local"

        async def search(self, query):
            return [{"url": "https://offering.example/product", "title": title, "snippet": excerpt}]

        async def scrape(self, url):
            return ScrapedText(excerpt, metadata={"url": url}, usage={})

    def replay(dossier_id, payload, *, messages, schema, parse, key="review", **kwargs):
        sources = json.loads(messages[1]["content"])["sources"]
        result = parse(json.dumps({"overview": "You have a sourced alternative to compare.",
            "observations": [], "assessments": [], "service_checks": [], "customer_reviews": [],
            "competitor_checks": [{"source_id": sources[0]["id"], "product_quote_index": 0,
                "customer_quote_index": 0, "positioning_quote_index": None, "pricing_quote_index": None,
                "competition_type": "direct"}], "coverage_gaps": [], "proposed_tests": []}))
        return metered.perform(dossier_id, key, payload, purpose="research.deep", provider="local",
            callback=lambda: result, estimate=0)

    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    monkeypatch.setattr(prior, "enabled", lambda: False)
    monkeypatch.setattr(deep, "_web_provider", Provider)
    monkeypatch.setattr(deep, "_scrape_provider", lambda _provider: Provider())
    monkeypatch.setattr(deep, "_github", lambda *args: {"items": [], "incomplete": False})
    monkeypatch.setattr(deep, "synthesize", replay)
    response = http.post(f"/ventures/{venture_id}/chat", json={
        "message": f"List my main competitors for {audience} with links to their products"})
    assert response.status_code == 200, response.text
    with db()() as session:
        queued = session.scalar(select(Job).where(Job.venture_id == venture_id, Job.kind == "research.deep"))
        assert queued is not None, response.json().get("body_md")
        assert queued.status in {"queued", "scheduled"}
        job_id = queued.id
    drain()
    with db()() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "succeeded", saved.last_error
    report = http.get(f"/ventures/{venture_id}/research/dossier").json()
    assert report["available"] and report["competitors"], json.dumps({
        "sources": report.get("sources"), "engine": report.get("engine")})
    assert any(item["url"] == "https://offering.example/product" and item["type"] == "direct"
               for item in report["competitors"])


def test_requested_customers_are_saved_by_local_worker(requested_work, monkeypatch):
    import json
    from sqlalchemy import select
    from app.core import local_mode, providers_local
    from app.db.models import Job
    from app.gtm import journey
    from app.research import local_providers
    from app.state import db
    from app.tenancy import bind
    from tests.unit.gtm.test_leads_without_email import install

    http, venture_id, drain, audience, problem = requested_work
    providers_local.save_settings(provider="openai", api_key="offline-fixture", models={"chat": "fixture-model"})
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: False)
    contacts = [{"url": f"https://contacts.example/person{index}", "title": name,
                 "snippet": f"{audience}: {problem}", "text": f"{name}\n{audience}\n"
                 f"I struggle with {problem}.\nContact: {name.split()[0].lower()}@contacts.example"}
                for index, name in enumerate(("Morgan Hale", "Rowan Gray", "Taylor Reed"))]
    searches = []

    async def search(query, *, limit):
        searches.append(query)
        return [{key: value for key, value in contact.items() if key != "text"} for contact in contacts][:limit]

    async def read(url, *, main_only=True):
        return next(contact["text"] for contact in contacts if contact["url"] == url)

    class Provider:
        name = "duckduckgo"

        def __init__(self, limit):
            self._limit = limit

        async def search(self, query, **options):
            return await search(query, limit=options.get("limit", self._limit))

    monkeypatch.setattr(local_providers, "search_provider", lambda limit=6, **options: Provider(limit))
    monkeypatch.setattr(local_providers, "local_read", read)
    install(monkeypatch)
    judge = journey._discovery_model
    stages = []

    async def replay(payload, stage, context):
        stages.append(stage)
        if stage in {"plan", "reformulate"}:
            return {"target_type": "person", "audience": audience,
                    "queries": [{"query": audience + suffix, "intent": "person"}
                                for suffix in (" public contacts", " published profiles")]}
        if stage == "marketrole":
            return {"role_in_market": "buyer", "icp_fit": 0.9, "anti_icp": False,
                    "homepage_quote": f"I struggle with {problem}."}
        if stage in {"judge", "draft"}:
            return {"items": []}
        result = await judge(payload, stage, context)
        if stage == "reachjudge":
            for item in result["items"]:
                item["role"] = audience
        return result

    monkeypatch.setattr(journey, "_discovery_model", replay)
    workspace_id = http.get(f"/ventures/{venture_id}").json()["workspace_id"]
    actor = http.get("/_local_probe").json()["subject"]
    with bind(workspace_id=workspace_id, venture_id=venture_id, actor=actor):
        journey.update_icp(workspace_id, venture_id, audience, problem=problem, start=False,
            recruitment_opt_in=True, note=f"Find {audience} I can talk to")
    response = http.post(f"/ventures/{venture_id}/chat", json={"message": "Find customers"})
    assert response.status_code == 200, response.text
    with db()() as session:
        queued = session.scalar(select(Job).where(Job.venture_id == venture_id, Job.kind == "gtm.journey.prepare"))
        assert queued is not None, response.json()
        assert queued.status in {"queued", "scheduled"}
        job_id = queued.id
    drain()
    with db()() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "succeeded", saved.last_error
    contacts = http.get(f"/ventures/{venture_id}/gtm/contacts").json()["items"]
    status = http.get(f"/api/v1/ventures/{venture_id}/gtm/journey")
    assert status.status_code == 200, status.text
    view = status.json()
    assert len(contacts) >= 3 and all(contact.get("source_url") for contact in contacts), json.dumps({
        "contacts": contacts, "reachable_leads": view.get("reachable_leads"), "error": view.get("error_code"),
        "searches": searches, "stages": stages, "target": view.get("search_target"),
        "plan": view.get("discovery_plan"), "progress": view.get("progress")})
    assert searches


def test_requested_demand_scan_is_saved_by_local_worker(requested_work, monkeypatch):
    import json
    from sqlalchemy import select
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous
    from app.signals.research import ScrapedText, SearchResults
    from app.state import db
    from core.clock import utcnow

    http, venture_id, drain, audience, problem = requested_work
    published = utcnow().isoformat()
    hits = [{"url": f"https://www.reddit.com/r/discussion/comments/request{index}/problem/",
             "title": f"{audience}: {problem}", "snippet": f"I struggle with {problem} and waste hours every week.",
             "published_at": published, "author": f"reader{index}"} for index in range(3)]

    class Provider:
        name = "local"
        search_micro = scrape_micro = 0

        async def search(self, query, **kwargs):
            return SearchResults(hits, provider_rows=len(hits), rejected_shape=0, rejected_missing_url=0)

        async def scrape(self, url):
            hit = next(item for item in hits if item["url"] == url)
            return ScrapedText(hit["snippet"], metadata={"published_at": published, "author": hit["author"]}, usage={})

    monkeypatch.setattr(autonomous, "_provider", Provider)
    response = http.post(f"/ventures/{venture_id}/chat", json={"message": "Run a demand scan for me"})
    assert response.status_code == 200, response.text
    assert not any(action.get("label") == "Run it for me" or action.get("confirmation")
                   for action in response.json().get("actions", [])), response.json()
    with db()() as session:
        queued = session.scalar(select(Job).where(Job.venture_id == venture_id, Job.kind == "experiment.run"))
        assert queued is not None, response.json()
        assert queued.status in {"queued", "scheduled"}
        job_id = queued.id
        experiment_id = json.loads(queued.payload_json)["experiment_id"]
    drain()
    with db()() as session:
        saved = session.get(Job, job_id)
        assert saved.status == "succeeded", saved.last_error
        context = json.loads(session.get(Experiment, experiment_id).context)
        assert context.get("result"), context.get("error")
        result = context["result"]
        assert result["sources"], result
