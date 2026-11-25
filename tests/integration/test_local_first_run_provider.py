"""Walk the console's local first-run APIs without a usable AI provider."""

import socket
from time import monotonic, sleep

import httpx
import pytest

from tests.company_os_harness import company_os_test_client
from tests.unit.test_local_mode import local_owner_app as local_owner_app


KEY_SCREEN = "Settings > Keys and connections > Your AI provider"
TOOL_SCREEN = "Settings > Keys and connections > Coding tools"
FAKE_KEY = "synthetic-first-run-provider-key"
ACTIVE = {"scheduled", "retry_wait", "leased", "pending", "running"}
SUCCEEDED = {"succeeded", "partial"}


@pytest.mark.parametrize("idea,audience", [
    pytest.param("I want to build a startup that audits invoices for freight carriers",
                 "Billing managers at independent freight carriers", id="b2b"),
    pytest.param("I want to build a startup that helps renters grow herbs indoors",
                 "Apartment renters with little natural light", id="consumer"),
])
@pytest.mark.parametrize("provider_status", [None, 401, 503], ids=["missing", "rejected", "unavailable"])
def test_first_run_without_a_working_provider(local_owner_app, tmp_path, monkeypatch, idea, audience, provider_status):
    import primp
    from app.core import local_env_import, providers_local
    from app.db.models import Job
    from app.research import local_providers
    from app.state import db
    from app.worker import main as worker
    from desktop_runtime import agent_status
    from sqlalchemy import select

    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "1")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / "missing.env"))
    monkeypatch.setattr(local_env_import, "repo_root", lambda: tmp_path)
    monkeypatch.setattr(worker, "POLL_S", 0.1)
    names = set(local_env_import._FIELDS) | {"SUPYMEM_LLM_PROVIDER"}
    names.update(name for fields in providers_local.PROVIDER_ENV.values() for name in fields)
    names.update(f"SUPYMEM_MODEL_{role.upper()}" for role in providers_local.ROLES)
    for name in names - {"LLM_OFFLINE", "LLM_CASSETTE_MODE"}:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(agent_status, "provider_binary", lambda *args, **kwargs: None)
    requests = []
    unexpected = []

    def deny_network(*args, **kwargs):
        unexpected.append("unmocked network")
        raise AssertionError("The first-run test must not use the network")

    original_connect = socket.socket.connect

    def connect(connection, address):
        if connection.family in {socket.AF_INET, socket.AF_INET6}:
            deny_network()
        return original_connect(connection, address)

    def model_response(request):
        if request.url.host == "hn.algolia.com":
            return httpx.Response(200, json={"hits": []})
        if provider_status is None or request.url.host != "api.anthropic.com":
            deny_network()
        requests.append(request.url.path)
        return httpx.Response(provider_status, json={"error": {"message": f"wire details {FAKE_KEY}"}})

    async_client = httpx.AsyncClient

    def fake_client(**kwargs):
        kwargs["transport"] = httpx.MockTransport(model_response)
        return async_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", deny_network)
    monkeypatch.setattr(primp.Client, "request", deny_network)

    async def empty_public_search(query, limit):
        return []

    monkeypatch.setattr(local_providers, "_builtin_search", empty_public_search)

    problems = []

    def actionable(label, body, screen=None):
        if not body or screen is not None and screen not in body or "—" in body or any(word in body.lower() for word in (
                "app error", "traceback", "provider_outcome_unknown", "wire details", "credits", "reconciliation")):
            problems.append((label, body))

    def founder_text(body):
        detail = body.get("detail", body)
        if isinstance(detail, str):
            return detail
        error = detail.get("error", detail)
        return "\n".join(value for name in ("body_md", "message", "hint") if isinstance(value := error.get(name), str))

    def wait_for_work(venture_id):
        deadline = monotonic() + 12
        while monotonic() < deadline:
            with db()() as session:
                pending = list(session.execute(select(Job.kind, Job.status).where(
                    Job.venture_id == venture_id, Job.status.in_(ACTIVE))))
            if not pending:
                return
            sleep(0.05)
        pytest.fail(f"Your work did not reach a terminal state: {pending}")

    with company_os_test_client(local_owner_app, raise_server_exceptions=False) as client:
        def call(method, path, body=None, *, allow_error=False):
            response = client.request(method, path, json=body)
            if FAKE_KEY in response.text:
                pytest.fail(f"{path} returned the saved API key")
            assert 200 <= response.status_code < 300 or allow_error and 400 <= response.status_code < 500, (
                path, response.status_code, response.text)
            return response.json()

        assert call("GET", "/setup/status")["mode"] == "local"
        assert not call("GET", "/local/providers")["configured"]
        assert call("GET", "/local/settings/catalog")["sections"]
        assert call("GET", "/workspaces/local/llm-catalog")["providers"]
        if provider_status is not None:
            assert call("PUT", "/local/providers", {"provider": "anthropic", "api_key": FAKE_KEY})["configured"]

        preview = call("POST", "/entry/chat/preview", {"text": idea, "request_id": "first-idea"})
        if preview["status"] == "clarify":
            preview = call("POST", "/entry/chat/preview", {"text": audience,
                "previous_proposal_id": preview["id"], "request_id": "idea-details"})
        receipt = call("POST", "/entry/chat/confirm", {"proposal_id": preview["proposal"]["id"]})
        venture_id = receipt["venture_id"]
        client.headers["X-Workspace-Id"] = receipt["workspace_id"]
        venture = f"/ventures/{venture_id}"
        session = call("POST", f"{venture}/idea/sessions")
        session_path = f"{venture}/idea/sessions/{session['id']}"
        wait_for_work(venture_id)
        session = call("GET", session_path)
        if session["turns"] and session["turns"][-1]["kind"] == "question":
            answer = call("POST", f"{session_path}/messages", {"text": audience}, allow_error=True)
            if "session" not in answer:
                actionable("idea clarification", founder_text(answer), KEY_SCREEN)
            wait_for_work(venture_id)
        with db()() as database:
            clarification_jobs = list(database.scalars(select(Job.id).where(Job.venture_id == venture_id,
                Job.kind == "idea.clarify_turn")))
        assert clarification_jobs
        for job_id in clarification_jobs:
            job = call("GET", f"/jobs/{job_id}")
            if job["status"] not in SUCCEEDED:
                actionable("idea clarification job", job.get("failure_reason"), KEY_SCREEN)
        session = call("POST", f"{session_path}/messages", {"text": "Skip the remaining questions"})["session"]
        confirmation = call("POST", f"{session_path}/confirm", {"revision_id": session["current_revision"]["id"],
                            "start_research": False, "accept_validation": True})
        assert confirmation["session"]["state"] == "confirmed"

        for index, (message, screen) in enumerate([
            ("find customers", KEY_SCREEN if provider_status is None else None), ("research the market", None),
            ("build my landing page with Codex", TOOL_SCREEN), ("design an experiment", KEY_SCREEN if provider_status else None),
            ("what should I do next?", TOOL_SCREEN),
        ]):
            reply = call("POST", f"{venture}/chat", {"message": message, "idempotency_key": f"first-run-{index}"},
                         allow_error=True)
            actionable(message, founder_text(reply), screen)
        wait_for_work(venture_id)
        with db()() as database:
            research_jobs = list(database.scalars(select(Job.id).where(Job.venture_id == venture_id,
                Job.kind == "research.deep")))
            assert not database.scalar(select(Job.id).where(Job.venture_id == venture_id,
                Job.kind.in_(["pagebuilder.build", "build.run"])))
        assert research_jobs
        for job_id in research_jobs:
            job = call("GET", f"/jobs/{job_id}")
            if job["status"] not in SUCCEEDED:
                actionable("research job", job.get("failure_reason"), KEY_SCREEN)
                assert job["attempts"] == 1

        assert not any(tool["connected"] for tool in call("GET", "/local-tools/coding-agents")["tools"])
        assert call("GET", "/local/providers")["configured"] is (provider_status is not None)
        for section in ("search", "smtp", "publishing", "composio", "spend", "data"):
            assert "values" in call("GET", f"/local/settings/{section}")
        assert "capabilities" in call("GET", "/local/capabilities")
        call("GET", "/local/settings/backups")

    assert not unexpected
    assert bool(requests) is (provider_status is not None)
    assert not problems, problems
