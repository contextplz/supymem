"""The local page worker briefs your tool and waits for its checked files."""
import asyncio
import base64
import hashlib
import json
import io
from pathlib import Path
import subprocess
import zipfile
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.pagebuilder.test_api import setup as setup
from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html


@pytest.fixture
def local(monkeypatch):
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("custom", [False, True])
def test_landing_order_allows_fifteen_minutes_and_configured_limits(local, monkeypatch, domain, custom):
    from contextlib import nullcontext
    from app.build import device_orders

    inputs, _ = customer_draft(domain)
    caller = SimpleNamespace(sub="owner")
    session = SimpleNamespace(get=lambda *_: SimpleNamespace(name=inputs["name"]))
    monkeypatch.setattr(device_orders, "_scope", lambda *_: ("workspace", "owner", "device"))
    monkeypatch.setattr(device_orders, "db", lambda: lambda: nullcontext(session))
    monkeypatch.setattr(device_orders, "authorize", lambda *args, **kwargs: None)
    monkeypatch.setattr(device_orders, "_landing_page_snapshot", lambda *_: {"page_brief": inputs["idea"]})
    monkeypatch.setattr(device_orders, "prepare", lambda venture, caller, manifest, **kwargs: manifest)
    caps = device_orders.CodingLoopCaps(max_iterations=2, max_wall_seconds=1500, per_call_seconds=600) if custom else None
    manifest = device_orders.prepare_landing_page_from_job(inputs["venture_id"], caller, "page_job", inputs["idea"], provider="codex", caps=caps)
    assert manifest.coding_loop_verification.per_call_seconds == (600 if custom else 900)
    assert manifest.duration_seconds == (1500 if custom else 3600)
    assert manifest.coding_loop_verification.max_iterations == (2 if custom else 4)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_job_writes_a_brief_without_internal_planning_repairs(local, monkeypatch, domain):
    from app.build import device_orders
    from app.pagebuilder import jobs, models, runtime, tool

    inputs = models.PageInput.model_validate(customer_draft(domain)[0])
    doc = {"variant_count": 1, "_actor_subject": "owner", "_runtime": {
        "state": "queued", "input": inputs.model_dump(mode="json")}}
    row = SimpleNamespace(id="page_job", venture_id=inputs.venture_id, workspace_id="workspace",
        payload_json=json.dumps(doc), cancel_requested=False, status="leased")
    def mutate(job_id, payload, callback):
        value = callback(row, doc, None)
        row.payload_json = json.dumps(doc)
        return value
    monkeypatch.setattr(runtime, "mutate", mutate)
    monkeypatch.setattr(runtime, "read", lambda *_: row)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr(runtime, "fail", lambda *_: None)
    monkeypatch.setattr(tool, "require_connected", lambda *_: "codex")
    monkeypatch.setattr(jobs, "ensure_waitlist", lambda *_: None)
    monkeypatch.setattr(jobs, "analytics_binding", lambda *_: None)
    def forbidden(*args, **kwargs):
        pytest.fail("The page worker must not generate or repair the tool's plan or code")
    monkeypatch.setattr(runtime, "job_model_call", forbidden)
    orders = []
    starts = []
    monkeypatch.setattr(tool, "_start_order", lambda venture_id, order: starts.append(order["id"]))
    def prepare(venture_id, caller, job_id, instruction, *, provider):
        assert inputs.idea in instruction and inputs.name in instruction
        assert "page-plan.txt" in instruction and "index.html" in instruction
        assert provider == "codex" and caller.sub == "owner"
        orders.append(instruction)
        return {"id": "coding_order", "state": "approved"}
    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare)
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: {"id": "coding_order", "state": "running"})
    payload = {"_job_id": row.id}
    jobs.run(payload)
    jobs.run(payload)
    assert starts == ["coding_order"]
    assert len(orders) == 1
    assert doc["tool_build"]["order_id"] == "coding_order"
    assert doc["_runtime"]["state"] == "running"


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_tool_authored_plan_and_files_use_existing_page_qa_and_export(local, monkeypatch, domain):
    from app.pagebuilder import images, models, pipeline, tool
    data, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate(data)
    plan = final_plan({"images": images, "models": models, "pipeline": pipeline}, inputs, draft)
    files = {asset.path: asset.svg.encode() for asset in plan.assets}
    files.update({"index.html": fixture_html(plan, inputs).encode(),
        "page-plan.txt": json.dumps(draft).encode()})
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    files = _with_social_preview(files, inputs)
    def forbidden(*args, **kwargs):
        pytest.fail("Checking your tool's artwork must not compile internal artwork")
    monkeypatch.setattr(pipeline, "compile_asset", forbidden)
    checked = tool.check_output(files, inputs, None)
    assert checked["passed"], checked["failures"]
    result = tool.bundle(files, inputs, None, {"passed": True, "reviews": []})
    assert result["manifest"]["built_with_coding_tool"]
    assert base64.b64decode(result["files"]["index.html"]) == files["index.html"]
    files["page-plan.txt"] = b"{}"
    assert not tool.check_output(files, inputs, None)["passed"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_general_worker_keeps_page_job_open_while_tool_runs(setup, monkeypatch, domain):
    from app.core import local_worker
    from app.core.jobs import lease
    from app.db.models import Job
    from app.pagebuilder import api, jobs, runtime
    from app.tasks import service
    from app.worker import handlers, main
    from core.clock import utcnow
    from datetime import timedelta
    data = setup
    result = api.build(data["vid"], data["actor"])
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_worker, "blocked_reason", lambda *_: None)
    monkeypatch.setattr(service, "before_job", lambda *_: True)
    monkeypatch.setattr(main, "_job_scope", lambda *_: SimpleNamespace(
        workspace_id=data["wid"], venture_id=data["vid"], actor=data["actor"]))
    monkeypatch.setattr(main, "_funnel_outcome", lambda *_: None)
    monkeypatch.setattr(main, "_stage_funnel", lambda *_: None)
    calls = []
    def handoff(payload):
        calls.append(payload["_job_id"])
        def save(row, doc, session):
            doc["_runtime"].update(state="running", stage="building", progress=.4)
            doc["tool_build"] = {"order_id": "coding_order", "brief": customer_draft(domain)[0]["idea"]}
        runtime.mutate(payload["_job_id"], payload, save)
    monkeypatch.setattr(jobs, "run", handoff)
    async def dispatch(kind, payload):
        assert kind == api.KIND
        handlers._pagebuilder_build(payload)
    monkeypatch.setattr(main, "dispatch", dispatch)
    with data["factory"]() as session:
        claimed = lease(session, "page_worker", kinds=[api.KIND], n=1)
        session.commit()
    job = claimed[0]
    asyncio.run(main._run_one(data["factory"], job, {(job.id, job.attempts)}))
    with data["factory"]() as session:
        saved = session.get(Job, result["job_id"])
        assert calls == [job.id]
        assert saved.status == "retry_wait"
        assert saved.finished_at is None
        assert saved.attempts == 1
        assert json.loads(saved.payload_json)["_runtime"]["state"] == "running"
    with data["factory"]() as session:
        claimed = lease(session, "page_worker", kinds=[api.KIND], n=1, now=utcnow() + timedelta(seconds=16))
        session.commit()
    job = claimed[0]
    asyncio.run(main._run_one(data["factory"], job, {(job.id, job.attempts)}))
    with data["factory"]() as session:
        saved = session.get(Job, result["job_id"])
        assert saved.attempts == 2 and saved.status == "retry_wait"
        assert saved.finished_at is None and calls == [job.id, job.id]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_page_start_does_not_append_a_research_recommendation(local, monkeypatch, domain):
    from app.chat import execute, next_moves
    from app.pagebuilder import api
    from app.db.models import ChatMessage
    monkeypatch.setattr(api, "_start", lambda *args, **kwargs: {"state": "queued", "job_id": "page_job"})
    def forbidden(*args, **kwargs):
        pytest.fail("Starting your page must not add a contradictory next-step recommendation")
    monkeypatch.setattr(next_moves, "compose", forbidden)
    inputs = customer_draft(domain)[0]
    reply = execute._pagebuilder_start(inputs["venture_id"], "owner", "quote")
    assert reply["citations"] == [{"job_started": {"job_id": "page_job"}}]
    row = ChatMessage(kind=reply["kind"], body_md=reply["body_md"],
        actions_json=json.dumps(reply["actions"]), citations_json=json.dumps(reply["citations"]))
    assert not next_moves.attach(inputs["venture_id"], row, request="Build my landing page")
    assert row.body_md == reply["body_md"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("provider", ["codex", "claude"])
def test_journey_fake_cli_builds_the_domain_page_that_the_real_checker_accepts(local, monkeypatch, tmp_path, domain, provider):
    from app.pagebuilder import models, tool
    from desktop_runtime.owned_coding import OwnedCoding
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    import local_build_journey as journey
    index = 1 if domain == "b2b" else 2
    fixture = json.loads((Path(__file__).parent / "fixtures/local_tool_pages" / f"{index}.json").read_text())
    inputs, draft = fixture["input"], fixture["draft"]
    inputs.setdefault("research", {})["local_signup"] = {"mode": "setup", "url": "", "notice": "Connect your signup destination in Settings > Keys and connections."}
    binary = journey.install_fake_cli(tmp_path / "tool", provider)
    (binary.parent / "page-fixture.json").write_text(json.dumps({"root": str(Path(__file__).resolve().parents[3]),
        "input": inputs, "template": {"input": inputs, "draft": draft}}))
    workspace = tmp_path / "page"
    workspace.mkdir()
    prompt = tool.build_brief(models.PageInput.model_validate(inputs), None)
    assert "waitlist.html" in prompt
    command = [str(binary), "-p", prompt] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), prompt]
    environment = OwnedCoding._env()
    result = subprocess.run(command, cwd=workspace, env=environment, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    files = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    tool.tool_page_plan(models.PageInput.model_validate(inputs), None, files)
    checked = tool.check_output(files, models.PageInput.model_validate(inputs), None)
    assert checked["passed"], checked["failures"]
    assert journey.MARKER in files["index.html"].decode()
    assert journey.cli_runs(binary.parent)[0]["provider"] == provider


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("verified", [True, False])
def test_final_check_failure_updates_only_the_verified_page_job(local, monkeypatch, domain, verified):
    from app.build import device_orders
    from app.pagebuilder import api, images, models, pipeline, runtime, tool
    from core.artifact_snapshot import manifest_digest
    from fastapi import HTTPException
    inputs, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate(inputs)
    plan = final_plan({"images": images, "models": models, "pipeline": pipeline}, inputs, draft)
    files = {asset.path: asset.svg.encode() for asset in plan.assets}
    files["index.html"] = fixture_html(plan, inputs).encode()
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    files = _with_social_preview(files, inputs)
    manifest = [{"path": name, "sha256": hashlib.sha256(body).hexdigest(), "size_bytes": len(body)}
        for name, body in sorted(files.items())]
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    doc = {"_actor_subject": "owner", "_runtime": {"state": "running", "input": inputs.model_dump(mode="json")},
        "tool_build": {"order_id": "coding_order", "plan": plan.model_dump(mode="json"), "grounding": {"reviews": []}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id, payload_json=json.dumps(doc))
    order = {"workspace_id": "workspace", "actor_id": "owner", "state": "stopped", "execution_id": "dex_page",
        "native_stop": {"execution_id": "dex_page", "processes_stopped": True, "task_verified": verified,
            "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}},
        "task_snapshot": {"pagebuilder_job_id": "page_job"},
        "iterations": [{"outcome": "pass", "file_manifest_sha256": manifest_digest(manifest)}]}
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: ("workspace", "owner"))
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: order)
    monkeypatch.setattr(runtime, "read", lambda *_: row)
    failures = []
    monkeypatch.setattr(runtime, "fail", lambda *args: failures.append(args))
    def rejected(*args):
        raise ValueError("pagebuilder_qa_failed:honest_copy")
    monkeypatch.setattr(tool, "review_tool_source", rejected)
    with pytest.raises(ValueError if verified else HTTPException):
        tool.finish(inputs.venture_id, "owner", "page_job", "coding_order", "dex_page", output.getvalue())
    assert len(failures) == int(verified)
    if verified:
        assert failures[0][:2] == ("page_job", None)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("state", ["ready", "failed"])
def test_repeated_failure_keeps_your_ready_page_or_specific_next_step(local, monkeypatch, domain, state):
    from app.pagebuilder import runtime
    doc = {"_runtime": {"state": state, "error": "pagebuilder_qa_failed", "error_reason": "Open Website and fix your confirmed copy."}}
    before = json.dumps(doc)
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(None, doc, None))
    runtime.fail(customer_draft(domain)[0]["venture_id"], None, ValueError("pagebuilder_qa_failed"))
    assert json.dumps(doc) == before


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("marker_verified", [True, False])
def test_semantic_page_check_sends_one_fix_back_to_your_tool(local, monkeypatch, tmp_path, domain, marker_verified):
    from app.build.device_orders import CodingLoopIteration
    from app.pagebuilder import images, models, pipeline, runtime, tool
    from desktop_runtime import coding_loop, landing_loop
    inputs, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate(inputs)
    plan = final_plan({"images": images, "models": models, "pipeline": pipeline}, inputs, draft)
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    for asset in plan.assets:
        target = workspace / asset.path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(asset.svg)
    prompts, reviews = [], []
    def run(prompt, deadline):
        prompts.append(prompt)
        from tests.unit.pagebuilder.test_tool_build import _with_social_preview
        files = _with_social_preview({"index.html": fixture_html(plan, inputs).encode()}, inputs)
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return {"outcome": "pass", "workspace_marker_verified": marker_verified}
    def review(job_id, inputs, plan, grounding, *, deadline=None):
        reviews.append(job_id)
        if len(reviews) == 1:
            raise ValueError("pagebuilder_qa_failed:honest_copy")
        return {"passed": True, "reviews": [{"review": {"suggestions": []}}]}
    monkeypatch.setattr(tool, "review_tool_source", review)
    monkeypatch.setattr(runtime, "read", lambda *_: SimpleNamespace(payload_json=json.dumps({"tool_build": {"grounding": {"reviews": []}}})))
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *_: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *_: True)
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False), loop_saved={"task_snapshot": {
        "page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json"), "pagebuilder_job_id": "page_job"}})
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    gateway = SimpleNamespace(order_id="coding_order", iteration_receipt=lambda receipt: CodingLoopIteration.model_validate({**receipt, "execution_id": "dex_page"}))
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 120, "per_call_seconds": 60}, "instruction": "Build your page"}
    result = landing_loop.run(worker, manifest, gateway, coding, float("inf"))
    assert result["task_verified"] is marker_verified
    assert len(prompts) == 2 and reviews == ["page_job", "page_job"]
    assert "pagebuilder_qa_failed" in prompts[1]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("existing", [True, False])
def test_local_tool_workspace_uses_your_data_folder_without_losing_previous_files(local, monkeypatch, tmp_path, domain, existing):
    from desktop_runtime import owned_terminal
    home, data = tmp_path / "home", tmp_path / "data"
    monkeypatch.setattr(Path, "home", lambda: home)
    def data_root(*, create=False):
        if create:
            data.mkdir(exist_ok=True)
        return data
    monkeypatch.setattr(owned_terminal, "data_root", data_root)
    venture_id = customer_draft(domain)[0]["venture_id"]
    slug = "page-" + domain
    previous = home / "Documents" / "Supymem" / "ventures" / slug
    if existing:
        previous.mkdir(parents=True)
        (previous / ".supymem-venture").write_text(venture_id)
        (previous / "index.html").write_text("Your previous checked page")
    terminal = owned_terminal.OwnedTerminal(venture_id=venture_id, workspace_slug=slug, tracker=None, authorize=lambda: True)
    workspace = terminal.workspace()
    assert workspace == (previous if existing else data / "workspaces" / slug)
    if existing:
        assert (workspace / "index.html").read_text() == "Your previous checked page"


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("case", ["started", "lost_reply", "foreign_order", "invalid_reply"])
def test_native_start_uses_your_existing_control_route_and_checks_the_order(local, monkeypatch, domain, case):
    import httpx
    from app.core.jobs import FatalError
    from app.pagebuilder import tool
    from core import local_security
    from desktop_runtime import bridge
    venture_id = customer_draft(domain)[0]["venture_id"]
    monkeypatch.setattr(local_security, "_proxy_secret", lambda: b"only-a-test-secret")
    monkeypatch.setattr(bridge, "_gateway", lambda: "http://127.0.0.1:3300")
    requests = []
    def control(request):
        requests.append((request.method, request.url.path))
        assert request.headers.get("X-Local-Proxy-Secret") is not None
        if case == "lost_reply" and request.method == "POST":
            raise httpx.ReadTimeout("Reply lost", request=request)
        if case == "invalid_reply":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json={"order_id": "foreign_order" if case == "foreign_order" else "coding_order",
            "state": "running_coding"})
    client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client(transport=httpx.MockTransport(control), **kwargs))
    if case in {"foreign_order", "invalid_reply"}:
        with pytest.raises(FatalError, match="Open Build activity.*Retry on Website"):
            tool._start_order(venture_id, {"id": "coding_order", "state": "approved"})
    else:
        tool._start_order(venture_id, {"id": "coding_order", "state": "approved"})
    prefix = f"/native/device-work-orders/{venture_id}/coding_order"
    assert requests == [("POST", prefix + "/start")] + ([] if case == "started" else [("GET", prefix + "/status")])


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("boundary", ["job", "snapshot"])
def test_cancelled_page_work_cannot_start_or_be_claimed(setup, monkeypatch, domain, boundary):
    from app.build import device_orders
    from app.core.jobs import FatalError, lease
    from app.db.models import Job
    from app.pagebuilder import api, jobs, models, runtime, tool
    data = setup
    job_id = api.build(data["vid"], data["actor"])["job_id"]
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr(tool, "require_connected", lambda *_: "codex")
    monkeypatch.setattr(tool, "_start_order", lambda *_: pytest.fail("You stopped this build"))
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: {"id": "coding_order", "state": "approved"})
    inputs = {**customer_draft(domain)[0], "venture_id": data["vid"]}
    inputs["waitlist_url"] = inputs["waitlist_url"].rsplit("/", 1)[0] + "/" + data["vid"]
    with data["factory"]() as session:
        claimed = lease(session, "page_worker", kinds=[api.KIND], n=1)
        row = session.get(Job, job_id)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="running", input=inputs)
        doc["tool_build"] = {"plan": None, "order_id": "coding_order",
            "brief": tool.build_brief(models.PageInput.model_validate(inputs), None)}
        row.payload_json, row.cancel_requested = json.dumps(doc), True
        session.commit()
    if boundary == "snapshot":
        with data["factory"]() as session, pytest.raises(ValueError, match="device_task_not_ready"):
            device_orders._landing_page_snapshot(session, data["wid"], data["vid"], job_id, data["actor"])
    else:
        job = claimed[0]
        with pytest.raises(FatalError, match="pagebuilder_cancelled.*Open Website"):
            jobs.run({**job.payload, "_job_id": job_id, "_worker_id": job.worker_id, "_job_attempt": job.attempts})


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("summarized", [False, True])
def test_local_brief_uses_your_saved_idea_when_the_summary_is_empty(setup, monkeypatch, domain, summarized):
    from app.db.models import Venture
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api
    data = setup
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    raw = customer_draft(domain)[0]["idea"]
    summary = "You want to build " + raw
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = raw
        revision = session.get(IdeaRevision, data["rid"])
        revision.summary = "A saved description"
        revision.structured = json.dumps({"summary": summary if summarized else ""})
        session.commit()
        revision_id, inputs = api.snapshot(session, data["vid"], data["wid"])
    assert revision_id == data["rid"]
    assert inputs["idea"] == (summary if summarized else raw)
