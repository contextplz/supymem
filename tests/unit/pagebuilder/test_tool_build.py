"""Local pages use the owned coding loop, never the HTML generator."""
import base64
import io
import json
from types import SimpleNamespace
import zipfile

import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html, review_payload
from tests.unit.pagebuilder.test_api import setup as setup


@pytest.mark.parametrize("host,provider", [("warehouse.example", "vercel"), ("balcony.test", "netlify")])
def test_rejected_replacement_preserves_live_page(setup, monkeypatch, host, provider):
    from app.conversion import waitlist
    from app.pagebuilder import local_publish as publish

    venture, workspace, actor = (setup[key] for key in ("vid", "wid", "actor"))
    url = f"https://{host}/"
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(publish, "_site_scope", lambda *_: (workspace, actor))
    monkeypatch.setattr(publish, "enabled", lambda _: True)
    monkeypatch.setattr(waitlist, "configure", lambda *_, **__: None)
    monkeypatch.setattr(publish, "_settings", lambda: {"provider": provider, "project": "project", "token": "test-only"})
    monkeypatch.setattr(publish, "_https_url", lambda value: value)
    monkeypatch.setattr(publish, "_validate_files", lambda _: None)
    monkeypatch.setattr(publish, "_load", lambda *_, **__: (
        [publish._file("index.html", host.encode(), "text/html; charset=utf-8")], ""))
    publish._save_publication(venture, workspace, {"state": "live", "provider": provider,
        "deployment_id": "original", "project": "project", "url": url})
    requests = []

    def request(method, address, **kwargs):
        requests.append((method, address))
        if method == "POST" and not address.endswith("/unpublish"):
            return {"id": "replacement"}
        return {"published_deploy": {"id": "original"}}

    def reject(*_):
        if provider == "netlify" and len(requests) == 1:
            requests.append(("POLL", "replacement"))
            raise publish.LocalPublishError("Choose Check public URL on Website.")
        raise publish.DeploymentRejected("Open your hosting account and check the replacement.")

    monkeypatch.setattr(publish, "_request", request)
    monkeypatch.setattr(publish, "_poll_deployment", reject)
    monkeypatch.setattr(publish, "get_checked", lambda *_args, **_kw: SimpleNamespace(status_code=404))
    with pytest.raises(publish.LocalPublishError):
        publish.publish_site(venture, actor, job_id="replacement")
    if provider == "netlify":
        assert publish._publication(venture, workspace)["deployment_id"] == "original"
        assert publish.verify_current(venture, actor)["state"] == "failed"
    assert publish._publication(venture, workspace)["deployment_id"] == "original"
    assert publish.unpublish_current(venture, actor) == {"state": "offline", "url": url}
    if provider == "vercel":
        assert ("DELETE", "https://api.vercel.com/v13/deployments/original") in requests
    else:
        assert ("POST", "https://api.netlify.com/api/v1/sites/project/unpublish") in requests


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_terminal_page_qa_failure_does_not_block_a_new_attempt(setup, monkeypatch, domain):
    from app.build import device_orders
    from app.core.local_owner import ensure_owner
    from app.db.models import Job, Venture, VentureDocument
    from app.pagebuilder import api, runtime, tool
    from core.clock import utcnow
    from tests.company_os_harness import configure_local_test_model
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_test_model()
    data = seed(setup["factory"], actor=ensure_owner().id)
    raw, _ = customer_draft(domain)
    with data["factory"]() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = raw["name"], raw["idea"]
        session.commit()
    monkeypatch.setattr(tool, "connected_provider", lambda *_args, **_kw: "codex")
    monkeypatch.setattr(api, "require_enabled", lambda: None)
    monkeypatch.setattr(tool, "prepare_job", lambda *_: None)
    job_id = api.build(data["vid"], data["actor"])["job_id"]
    order = {"workspace_id": data["wid"], "actor_id": data["actor"], "state": "stopped", "execution_id": "execution",
        "task_snapshot": {"pagebuilder_job_id": job_id}, "iterations": [{"outcome": "pass"}],
        "native_stop": {"execution_id": "execution", "processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}}}
    with data["factory"]() as session:
        row = session.get(Job, job_id)
        doc = json.loads(row.payload_json)
        doc["tool_build"] = {"order_id": "dwo_checked", "provider": "codex"}
        row.payload_json = api.dump(doc)
        session.add(VentureDocument(id="dwo_checked", venture_id=data["vid"], kind=device_orders.KIND,
            title="Checked page", provenance_json=api.dump(order), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: order)
    runtime.fail(job_id, None, ValueError("pagebuilder_qa_failed:honest_copy"))
    assert api.status(data["vid"], data["actor"], job_id)["retry"]["body"] == {"new_version_of": job_id}
    assert api.build(data["vid"], data["actor"], new_version_of=job_id)["job_id"] != job_id


@pytest.mark.parametrize("domain,provider", [("b2b", "claude"), ("consumer", "cursor")])
def test_chat_retry_preserves_the_failed_page_brief_and_tool(setup, monkeypatch, domain, provider):
    from fastapi import HTTPException

    from app.build import device_orders
    from app.chat import execute
    from app.core.local_owner import ensure_owner
    from app.db.models import Job, VentureDocument
    from app.pagebuilder import api, tool
    from tests.company_os_harness import configure_local_test_model
    from tests.unit.pagebuilder.test_api import seed
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_test_model()
    data = seed(setup["factory"], actor=ensure_owner().id)
    monkeypatch.setattr("app.core.local_capabilities.enabled", lambda name: True)
    monkeypatch.setattr(tool, "connected_provider", lambda actor, *, provider=None: provider or "codex")
    orders = []

    def prepare(venture_id, caller, job_id, brief, *, provider):
        orders.append((job_id, provider, brief))
        return {"id": f"order_{job_id}", "state": "approved"}

    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare)
    inputs, _ = customer_draft(domain)
    original = api._start(data["vid"], data["actor"], provider=provider, instruction=inputs["idea"])["job_id"]
    with data["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="failed", stage="failed")
        row.status, row.payload_json = "failed_dead", api.dump(doc)
        previous = row.payload_json
        order_id = doc["tool_build"]["order_id"]
        order = {"workspace_id": data["wid"], "actor_id": data["actor"], "state": "stopped", "claimed_at": utcnow().isoformat(),
                 "task_snapshot": {"pagebuilder_job_id": original}, "stop_reconciliation_required": True}
        session.add(VentureDocument(id=order_id, kind=device_orders.KIND, venture_id=data["vid"], title="Saved page",
                                    provenance_json=api.dump(order), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    with pytest.raises(HTTPException):
        execute._build_landing_page(data["vid"], {"retry_job_id": "another_venture_attempt"}, data["actor"])
    with pytest.raises(HTTPException) as blocked:
        execute._build_landing_page(data["vid"], {"retry_job_id": original}, data["actor"])
    assert blocked.value.detail["code"] == "pagebuilder_outcome_uncertain"
    assert len(orders) == 1
    with data["factory"]() as session:
        order["stop_reconciliation_required"] = False
        session.get(VentureDocument, order_id).provenance_json = api.dump(order)
        session.commit()
    with pytest.raises(HTTPException) as unreconciled:
        execute._build_landing_page(data["vid"], {"retry_job_id": original}, data["actor"])
    assert unreconciled.value.detail["code"] == "pagebuilder_outcome_uncertain" and len(orders) == 1
    with data["factory"]() as session:
        order["native_stop"] = {"processes_stopped": True}
        session.get(VentureDocument, order_id).provenance_json = api.dump(order)
        session.commit()
    reply = execute._build_landing_page(data["vid"], {"retry_job_id": original}, data["actor"])
    assert reply["run_id"] != original
    assert orders[-1] == (reply["run_id"], provider, orders[0][2]) and len(orders) == 2
    assert execute._build_landing_page(data["vid"], {"retry_job_id": original}, data["actor"])["run_id"] == reply["run_id"]
    assert api.build(data["vid"], data["actor"], new_version_of=original)["job_id"] == reply["run_id"]
    assert len(orders) == 2
    with data["factory"]() as session:
        assert session.get(Job, original).payload_json == previous


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("source_state", ["ready", "renderer_failed", "running"])
@pytest.mark.parametrize("provider,label", [("claude", "Claude Code"), ("codex", "Codex"), ("cursor", "Cursor"), ("opencode", "OpenCode")])
def test_typed_page_rebuild_uses_button_order_path_and_keeps_previous_version(setup, monkeypatch, domain, source_state, provider, label):
    from app import coding_agents_consent
    from app.chat import execute
    from app.build import device_orders
    from app.core.local_owner import ensure_owner
    from app.db.models import Job, Venture
    from app.db.supymem_models import IdeaRevision
    from app.pagebuilder import api, tool
    from tests.company_os_harness import configure_local_test_model
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    configure_local_test_model()
    data = seed(setup["factory"], actor=ensure_owner().id)
    inputs, draft = customer_draft(domain)
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = inputs["idea"]
        session.get(IdeaRevision, data["rid"]).summary = inputs["idea"]
        session.get(IdeaRevision, data["rid"]).structured = api.dump({"audience": draft["brief"]["audience"]})
        session.commit()
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {name: True for name in ("claude", "codex", "cursor", "opencode")})
    monkeypatch.setattr("desktop_runtime.agent_status.statuses", lambda: [{"name": name, "installed": True,
        "signed_in": True, "subscription_login": True, "available": True} for name in ("claude", "codex", "cursor", "opencode")])
    monkeypatch.setattr("app.core.local_capabilities.enabled", lambda name: True)
    monkeypatch.setattr(api, "_candidate_current", lambda doc: True)
    monkeypatch.setattr(tool, "_start_order", lambda *args: None)
    orders = []
    def prepare(venture_id, caller, job_id, instruction, *, provider):
        orders.append((job_id, provider, instruction))
        return {"id": "order_" + job_id, "state": "approved"}
    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare)
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", lambda *args, **kwargs: pytest.fail("A page is not a terminal task"))
    original = api.build(data["vid"], data["actor"])["job_id"]
    with data["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        state = "failed" if source_state == "renderer_failed" else source_state
        doc["_runtime"].update(state=state, stage=state, progress=1)
        if source_state == "renderer_failed":
            doc["_runtime"]["error"] = "pagebuilder_renderer_unavailable"
        doc["result"]["bundles"] = [{"manifest": {}, "direction": "balanced", "plan": {}, "files": {}, "qa": {}}]
        saved_status = "succeeded" if source_state == "ready" else "failed_dead" if source_state == "renderer_failed" else "pending"
        row.status, row.payload_json = saved_status, api.dump(doc)
        previous = row.payload_json
        session.commit()
    text = "Rebuild my landing page with " + label
    if source_state == "running":
        from fastapi import HTTPException
        if provider != "codex":
            with pytest.raises(HTTPException) as busy:
                execute._build_landing_page(data["vid"], {"prepare": True, "query": text}, data["actor"])
            assert busy.value.detail["code"] == "pagebuilder_tool_build_active"
            assert "stop that build" in busy.value.detail["message"]
        else:
            reply = execute._build_landing_page(data["vid"], {"prepare": True, "query": text}, data["actor"])
            assert reply["run_id"] == original
        assert len(orders) == 1
        with data["factory"]() as session:
            assert session.get(Job, original).payload_json == previous
        return
    reply = execute._build_landing_page(data["vid"], {"prepare": True, "query": text}, data["actor"])
    assert reply["run_id"] != original and len(orders) == 2
    assert orders[-1][0:2] == (reply["run_id"], provider) and text in orders[-1][2]
    assert "credit" not in reply["body_md"].lower() and "Open Builds" not in reply["body_md"]
    with data["factory"]() as session:
        assert session.get(Job, original).payload_json == previous
        assert session.get(Job, original).status == saved_status
        doc = json.loads(session.get(Job, reply["run_id"]).payload_json)
        assert doc["coding_provider"] == provider and doc["build_instruction"] == text


def _with_social_preview(files, inputs):
    from html import escape
    from app.pagebuilder.metadata import SOCIAL_PREVIEW_PATH, social_preview_requirements
    from tests.unit.pagebuilder.test_metadata import png

    files = dict(files)
    tags = "".join(f'<meta {"property" if name.startswith("og:") else "name"}="{name}" content="{escape(value, quote=True)}">'
        for name, value in social_preview_requirements(inputs.name).items())
    files["index.html"] = files["index.html"].replace(b"</head>", tags.encode() + b"</head>")
    files[SOCIAL_PREVIEW_PATH] = png()
    return files


@pytest.fixture
def modules(monkeypatch):
    load_company_os_app()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.pagebuilder import models, pipeline, tool
    monkeypatch.setattr(tool, "_start_order", lambda *_: None)
    return models, pipeline, tool


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("instruction", ["", " ", 123, "x" * 16001, "Build with api_key=sk-" + "a" * 40], ids=["empty", "blank", "nontext", "too_long", "credential"])
def test_page_build_instructions_are_checked_before_saving(modules, monkeypatch, domain, instruction):
    from app.pagebuilder import api
    from fastapi import HTTPException

    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: ("workspace", "owner"))
    monkeypatch.setattr(api, "require_enabled", lambda: pytest.fail("Invalid instructions must not reach the builder"))
    with pytest.raises(HTTPException) as refused:
        api._start(domain, "owner", instruction=instruction)
    assert refused.value.status_code in {409, 422}
    assert refused.value.detail["code"] in {"pagebuilder_build_instruction_invalid", "pagebuilder_build_request_contains_secret"}
    assert "then" in refused.value.detail["message"]


@pytest.fixture(params=["b2b", "consumer"])
def page(modules, request):
    from app.pagebuilder import images
    models, pipeline, tool = modules
    inputs, draft = customer_draft(request.param)
    inputs["clarified_answers"] = {"offer": draft["brief"]["promise"],
        "audience": draft["brief"]["audience"], "proof": draft["brief"]["proof_points"]}
    inputs = models.PageInput.model_validate(inputs)
    original = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    files = {asset.path: asset.svg.encode() for asset in original.assets}
    files["index.html"] = fixture_html(original, inputs).encode()
    plan = pipeline.tool_page_plan(inputs, draft, files)
    files["index.html"] = fixture_html(plan, inputs).encode()
    return inputs, plan, _with_social_preview(files, inputs)


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_job_routes_to_tool_without_internal_generation(modules, monkeypatch, connected, domain):
    from app.pagebuilder import jobs
    from fastapi import HTTPException
    tool = modules[2]
    monkeypatch.setattr(tool, "connected_provider", lambda actor: "codex" if connected else None)
    seen = []
    monkeypatch.setattr(tool, "run_job", lambda payload: (tool.require_connected(payload["_actor_subject"]), seen.append(payload)))
    monkeypatch.setattr(modules[1], "generate_page", lambda *args, **kwargs: pytest.fail("internal generator ran"))
    payload = {"_actor_subject": "owner", "venture_id": "ven_" + domain}
    if connected:
        jobs.run(payload)
        assert seen == [payload]
    else:
        with pytest.raises(HTTPException) as blocked:
            jobs.run(payload)
        assert "Settings > Keys and connections > Coding tools" in blocked.value.detail["message"]
        assert not seen


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("entry", ["engine", "job"])
def test_page_engine_delegates_a_complete_brief_to_the_existing_order_path(modules, monkeypatch, connected, domain, entry):
    from app.build import device_orders
    from app.core.jobs import FatalError
    from app.pagebuilder import api, jobs, runtime
    from fastapi import HTTPException
    models, pipeline, tool = modules
    data, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate(data)
    payload = {"_job_id": "page_job", "_actor_subject": "owner", "venture_id": inputs.venture_id}
    doc = {"variant_count": 1, "_actor_subject": "owner", "_runtime": {"state": "queued", "input": inputs.model_dump(mode="json")}}
    def row():
        return SimpleNamespace(id="page_job", venture_id=inputs.venture_id, workspace_id="workspace", payload_json=json.dumps(doc), cancel_requested=False, status="leased")
    monkeypatch.setattr(runtime, "read", lambda *_: row())
    monkeypatch.setattr(runtime, "mutate", lambda job_id, saved_payload, callback: callback(row(), doc, None))
    def validate_inputs(*args):
        if not connected:
            pytest.fail("Connecting your tool must come before model setup or generation")
    monkeypatch.setattr(runtime, "validate_inputs", validate_inputs)
    monkeypatch.setattr(tool, "connected_provider", lambda actor: "codex" if connected else None)
    monkeypatch.setattr(jobs, "ensure_waitlist", lambda *args: None)
    monkeypatch.setattr(jobs, "analytics_binding", lambda *args: None)
    monkeypatch.setattr(api, "status", lambda *args: {"state": doc["_runtime"]["state"], "order_id": doc["tool_build"]["order_id"]})
    stages, orders = [], []
    def model(stage, prompt):
        stages.append(stage)
        raise ValueError("pagebuilder_capability_grounding_rejected")
    monkeypatch.setattr(runtime, "job_model_call", lambda *args: model)
    def prepare(venture_id, caller, job_id, instruction, *, provider):
        assert venture_id == inputs.venture_id and caller.sub == "owner" and provider == "codex"
        assert inputs.idea in instruction and inputs.name in instruction and "assets/" in instruction
        order = {"id": "coding_order", "state": "approved"}
        orders.append(order)
        return order
    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare)
    def forbidden(*args, **kwargs):
        pytest.fail("The page engine generated internal source or artwork")
    monkeypatch.setattr(pipeline, "compile_asset", forbidden)
    def build():
        return pipeline.generate_page(inputs, forbidden, image_call=forbidden, render=forbidden, job_payload=payload) if entry == "engine" else jobs.run(payload)
    if connected:
        result = build()
        if entry == "engine":
            assert result == {"state": "running", "order_id": "coding_order"}
        assert doc["_runtime"]["state"] == "running" and doc["tool_build"]["order_id"] == "coding_order"
        assert len(orders) == 1 and stages == []
        assert doc["tool_build"]["plan"] is None
    else:
        with pytest.raises(FatalError if entry == "job" else HTTPException) as blocked:
            build()
        message = str(blocked.value) if entry == "job" else blocked.value.detail["message"]
        assert "Settings > Keys and connections > Coding tools" in message
        assert orders == [] and stages == []


def test_page_brief_preserves_venture_offer_audience_proof_and_action(page, modules):
    inputs, plan, files = page
    brief = modules[2].build_brief(inputs, plan)
    assert inputs.idea in brief
    for value in (plan.brief.audience, plan.brief.promise, plan.brief.cta, *plan.brief.proof_points):
        assert value in brief
    assert inputs.waitlist_url in brief
    assert "index.html" in brief and "no backend" in brief and "light theme" in brief


def test_landing_brief_is_compact_and_names_offline_commands(page, modules):
    inputs, _, _ = page
    brief = modules[2].build_brief(inputs, None)
    assert len(brief.encode()) < 19000
    assert "Allowed commands:" in brief and "no network" in brief.lower()
    assert "Stop as soon as" in brief and "page-plan.txt" in brief
    assert brief.count(inputs.model_dump_json()) == 1


def test_brief_requires_every_visible_sentence_and_only_declared_controls(page, modules):
    from app.pagebuilder.qa import tool_contract
    inputs, plan, _ = page
    for supplied in (None, plan):
        brief = modules[2].build_brief(inputs, supplied)
        assert "Every visible sentence must come from page-plan.txt" in brief
        assert "Allowed controls: only PAGE_CHECK_CONTRACT.actions" in brief
        contract = json.loads(brief.split("PAGE_CHECK_CONTRACT:\n", 1)[1].split("\n", 1)[0])
        assert contract["actions"] == tool_contract(inputs, supplied)["actions"]


@pytest.mark.parametrize("exit_kind", ["timeout", "cli_error", "zero_cli_error", "exception"])
@pytest.mark.parametrize("complete", [True, False])
def test_stopped_page_checks_and_repairs_keep_tool_files(page, modules, monkeypatch, tmp_path, exit_kind, complete):
    from desktop_runtime import coding_loop, landing_loop

    inputs, plan, files = page
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    prompts = []

    def run(prompt, deadline):
        prompts.append(prompt)
        if len(prompts) == 2:
            assert (workspace / "index.html").read_bytes() == files["index.html"].replace(b"<main", b"<div").replace(b"</main>", b"</div>")
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        if not complete and len(prompts) == 1:
            (workspace / "index.html").write_bytes(files["index.html"].replace(b"<main", b"<div").replace(b"</main>", b"</div>"))
        coding.line_callback(json.dumps({"type": "item.completed", "item": {
            "type": "agent_message", "text": "Your page files are saved. Open Website to check them."}}).encode())
        if exit_kind == "exception":
            raise ValueError("coding_provider_failed")
        if exit_kind == "zero_cli_error":
            return {"outcome": "cli_error", "exit_code": 0, "workspace_marker_verified": True}
        return {"outcome": exit_kind, "workspace_marker_verified": True}

    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    posted = []
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=posted.append)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 1800, "per_call_seconds": 900},
        "instruction": modules[2].build_brief(inputs, plan)}
    result = landing_loop.run(worker, manifest, gateway, coding, float("inf"))
    assert result["task_verified"] is True
    assert result["iterations"] == (1 if complete else 2)
    assert (workspace / "index.html").read_bytes() == files["index.html"]
    assert any("Your page files are saved" in event["text"] for event in worker.last["events"])
    assert posted[-1]["outcome"] == "pass"
    if exit_kind == "zero_cli_error" and not complete:
        assert posted[0]["outcome"] == "tests_failed"
    assert worker.last["message"].startswith("Your page passed its checks")
    assert worker.last["events"][-1]["text"].startswith("Your page passed its checks")


@pytest.mark.parametrize("error", ["coding_secret_detected", "coding_output_bounds"])
def test_landing_does_not_accept_safety_errors_as_tool_exit(page, modules, monkeypatch, tmp_path, error):
    from desktop_runtime import coding_loop, landing_loop

    inputs, plan, files = page
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    calls = []
    def run(*args):
        calls.append(args)
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        raise ValueError(error)
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 1800, "per_call_seconds": 900}, "instruction": "Build your page"}
    result = landing_loop.run(worker, manifest, SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda *_: None), coding, float("inf"))
    assert result["task_verified"] is False and len(calls) == 1
    assert (workspace / "index.html").read_bytes() == files["index.html"]


@pytest.mark.parametrize("plan_kind", ["tool", "reviewed", "draft"])
def test_page_brief_requests_tool_authored_social_preview(page, modules, request, plan_kind):
    from app.pagebuilder.metadata import social_preview_requirements

    inputs, plan, files = page
    if plan_kind == "tool":
        plan = None
    elif plan_kind == "draft":
        plan = modules[0].GenerationDraft.model_validate(customer_draft(request.node.callspec.params["page"])[1])
    brief = modules[2].build_brief(inputs, plan)
    assert "SOCIAL_PREVIEW_CONTRACT" in brief
    contract = json.JSONDecoder().raw_decode(brief.split("\nSOCIAL_PREVIEW_CONTRACT", 1)[1].split("\n", 1)[1])[0]
    assert contract == social_preview_requirements(inputs.name)
    assert "1200 by 630 PNG preview image" in brief and "document-head tags before sealing the source" in brief
    assert "PNG screenshot of this page" not in brief and "No browser or server is required" in brief
    assert inputs.idea in brief
    assert ("page-plan.txt" if plan is None else "REVIEWED_PLAN") in brief


@pytest.mark.parametrize("invalid", [None, "missing_image", "wrong_size", "missing_metadata", "wrong_metadata", "canonical", "outside_head"])
def test_page_output_checks_social_preview_without_rewriting_source(page, modules, invalid):
    from app.pagebuilder.metadata import SOCIAL_PREVIEW_PATH
    from tests.unit.pagebuilder.test_metadata import png

    inputs, plan, files = page
    output = dict(files)
    if invalid == "missing_metadata":
        output["index.html"] = fixture_html(plan, inputs).encode()
    if invalid == "missing_image":
        output.pop(SOCIAL_PREVIEW_PATH)
    elif invalid == "wrong_size":
        output[SOCIAL_PREVIEW_PATH] = png(600, 315)
    elif invalid == "wrong_metadata":
        output["index.html"] = output["index.html"].replace(b'content="assets/social-preview.png"', b'content="assets/missing.png"')
    elif invalid == "canonical":
        output["index.html"] = output["index.html"].replace(b"</head>", b'<meta property="og:url" content="https://example.test"></head>')
    elif invalid == "outside_head":
        output["index.html"] = output["index.html"].replace(b"</head>", b"").replace(b'<meta property="og:image"', b'</head><meta property="og:image"', 1)
    original = dict(output)
    checked = modules[2].check_output(output, inputs, plan)
    assert output == original
    assert checked["passed"] is (invalid is None), checked
    if invalid:
        assert any("Settings > Keys and connections > Coding tools" in failure for failure in checked["failures"]), checked
        with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
            modules[2].bundle(output, inputs, plan, {"passed": True})


def test_tool_authored_page_plan_is_checked_without_generating_artwork(page, modules, monkeypatch, request):
    inputs, plan, files = page
    models, pipeline, tool = modules
    draft = models.GenerationDraft.model_validate(customer_draft(request.node.callspec.params["page"])[1])
    files = {**files, "page-plan.txt": draft.model_dump_json().encode()}
    monkeypatch.setattr(pipeline, "compile_asset", lambda *args: pytest.fail("Internal artwork generator ran"))
    checked = pipeline.tool_page_plan(inputs, None, files)
    assert checked.brief == plan.brief
    assert all(files[asset.path] == asset.svg.encode() for asset in checked.assets)
    assert tool.check_output(files, inputs, checked)["passed"]
    files["page-plan.txt"] = b"{}"
    with pytest.raises(ValueError):
        pipeline.tool_page_plan(inputs, None, files)


def test_tool_source_review_reuses_the_same_metered_stage(page, modules, monkeypatch):
    from app.pagebuilder import api, runtime
    inputs, plan, files = page
    doc = {"_actor_subject": "owner", "tool_build": {}, "_runtime": {"operations": {"variant_0:capability_review_1": {}}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *args: None)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: (row.workspace_id, "owner"))
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(row, doc, None))
    stages = []
    def review(stage, prompt):
        stages.append(stage)
        return json.dumps(review_payload(prompt))
    monkeypatch.setattr(runtime, "job_model_call", lambda *args: review)
    grounding = {"reviews": []}
    first = modules[2].review_tool_source("page_job", inputs, plan, grounding)
    second = modules[2].review_tool_source("page_job", inputs, plan, grounding)
    assert stages == ["capability_review_2", "capability_review_2"]
    assert first == second


def test_source_review_saves_voice_suggestions_without_failing_honesty(page, modules, monkeypatch):
    import hashlib
    from app.pagebuilder import api, runtime
    inputs, plan, _ = page
    doc = {"_actor_subject": "owner", "tool_build": {}, "_runtime": {"operations": {}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: (row.workspace_id, "owner"))
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(row, doc, None))
    suggestion = {"field_id": "brief.promise", "claim": plan.brief.promise,
        "reason": "Address the visitor directly rather than summarizing your offer."}
    def review(stage, prompt):
        assert "Tone, voice, audience framing and customer usefulness are suggestions only" in prompt
        assert "Treat violations as unsupported fields" not in prompt
        return json.dumps({**review_payload(prompt), "suggestions": [suggestion]})
    monkeypatch.setattr(runtime, "job_model_call", lambda *args: review)
    checked = modules[2].review_tool_source("page_job", inputs, plan, {"reviews": []})
    assert checked["passed"] is True
    assert doc["tool_build"]["source_review"]["review"]["suggestions"] == [suggestion]
    assert checked["reviews"][-1]["review"]["suggestions"] == [suggestion]
    legacy = review_payload(modules[1]._grounding_prompt(plan, inputs))
    assert modules[1].review_digest(legacy) == hashlib.sha256(
        json.dumps(legacy, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
    suggestion["field_id"] = "unrecognized.field"
    with pytest.raises(ValueError, match="pagebuilder_grounding_finding_not_in_plan"):
        modules[2].review_tool_source("page_job", inputs, plan, {"reviews": []})


@pytest.mark.parametrize("limit,root_kind", [(None, "plain"), ("budget", "plain"), ("spend", "plain"),
    (None, "temp"), (None, "data"), (None, "home"), (None, "data_space")])
def test_three_tool_rounds_use_real_review_budget_and_local_spend(page, modules, monkeypatch, tmp_path, request, limit, root_kind):
    import sqlite3
    import tempfile
    from app.core.local_secrets import update_settings
    from app.core.providers_local import load_local_config, save_settings
    from app.credits.estimate import estimate_llm_micro
    from app.llm import founder_provider
    from app.pagebuilder import api, runtime
    from desktop_runtime import coding_loop, landing_loop

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = tmp_path
    if root_kind != "plain":
        alias = tmp_path.parent / (tmp_path.name + (" Application Support" if root_kind == "data_space" else " alias"))
        alias.symlink_to(tmp_path, target_is_directory=True)
        if root_kind == "home":
            monkeypatch.setenv("HOME", str(alias))
        elif root_kind == "temp":
            monkeypatch.setenv("TMPDIR", str(alias))
            monkeypatch.setattr(tempfile, "tempdir", None)
        else:
            data = alias
        tmp_path = alias
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(data))
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-review-key", models={"chat": "DeepSeek-V4-Pro"})
    inputs, original, _ = page
    draft = modules[0].GenerationDraft.model_validate(customer_draft(request.node.callspec.params["page"])[1])
    config = load_local_config()
    route = SimpleNamespace(provider=config.provider, model=config.deployment_pro, base_url=config.base_url, source="founder_override")
    monkeypatch.setattr(api, "resolve_route", lambda *_: route)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: ("workspace", "owner"))
    monkeypatch.setattr(founder_provider, "register_workspace_llm_resolver", lambda: None)
    monkeypatch.setattr(founder_provider, "to_provider_config", lambda *args, **kwargs: config)
    monkeypatch.setattr(founder_provider, "resolved_provider_identity", lambda *_: "test-review-provider")
    ceiling = estimate_llm_micro(route.model, prompt_tokens=api.MAX_PROMPT_TOKENS, max_tokens=api.MAX_OUTPUT_TOKENS)
    quote = {"provider": route.provider, "model": route.model, "provider_identity": "test-review-provider", "account_id": None,
        "payer": "founder", "call_ceiling_micro": ceiling, "ceiling_micro": ceiling * api.CALLS_PER_VARIANT,
        "max_output_tokens": api.MAX_OUTPUT_TOKENS, "call_timeout_seconds": api.CALL_TIMEOUT_SECONDS,
        "reasoning_policy": api.reasoning_policy(route.provider, route.model)}
    quote["call_budget"] = 2 if limit == "budget" else api.calls_per_variant(quote)
    doc = {"_actor_subject": "owner", "variant_count": 1, "tool_build": {"grounding": {"reviews": []}},
        "_runtime": {"quote": quote, "operations": {}, "calls_used": 0, "state": "running"}}
    def row():
        return SimpleNamespace(id="page_job", workspace_id="workspace", venture_id=inputs.venture_id,
            attempts=1, locked_by=None, cancel_requested=False, status="succeeded", payload_json=json.dumps(doc))
    monkeypatch.setattr(runtime, "read", lambda *_: row())
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(row(), doc, None))
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    reviews, prompts = [], []
    def reviewer(dial, timeout):
        prompt = dial["messages"][-1]["content"]
        reviews.append(prompt)
        public = json.loads(prompt.split("PUBLIC_COPY_BY_FIELD:\n", 1)[1].split("\nSANITIZED_SVG_BY_ASSET:", 1)[0])
        findings = [] if len(reviews) == 3 else [{"field_id": "brief.promise", "claim": public["brief.promise"],
            "reason": "This promised capability has not been confirmed in the supplied founder facts."}]
        review = review_payload(prompt, findings)
        if not findings:
            review["suggestions"] = [{"field_id": "brief.promise", "claim": public["brief.promise"],
                "reason": "Address the visitor directly rather than summarizing your offer."}]
        return SimpleNamespace(usage=SimpleNamespace(prompt_tokens=100, completion_tokens=100, total_tokens=200),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=json.dumps(review)))])
    monkeypatch.setattr(runtime, "_provider_response", reviewer)
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    def run(prompt, deadline):
        prompts.append(prompt)
        plan = original.model_copy(deep=True)
        plan.sections[0].headline += "?" * len(prompts)
        draft.sections[0].headline = plan.sections[0].headline
        files = {asset.path: asset.svg.encode() for asset in plan.assets}
        files["index.html"] = fixture_html(plan, inputs).encode()
        files["page-plan.txt"] = draft.model_dump_json().encode()
        for name, body in _with_social_preview(files, inputs).items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        if limit == "spend" and len(prompts) == 3:
            update_settings("spend", {"monthly_spend_cap": 0}, {})
        return {"outcome": "pass", "exit_code": 0, "workspace_marker_verified": True}
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *_: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *_: True)
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False), loop_saved={"task_snapshot": {
        "page_input": inputs.model_dump(mode="json"), "page_plan": None, "pagebuilder_job_id": "page_job"}})
    worker.progress = lambda state, **values: worker.last.update(state=state, **values)
    posted = []
    result = landing_loop.run(worker, {"instruction": modules[2].build_brief(inputs, None), "coding_loop_verification": {
        "max_iterations": 3, "max_wall_seconds": 3600, "per_call_seconds": 900}},
        SimpleNamespace(order_id="dwo_page", iteration_receipt=posted.append), coding, float("inf"))
    assert len(prompts) == 3
    assert len(reviews) == (3 if limit is None else 2), [receipt["diagnosis"]["failures"] for receipt in posted]
    assert result["task_verified"] is (limit is None), result
    assert result["outcome"] == ("pass" if limit is None else "checks_unavailable"), result
    if limit:
        assert result["failure_code"] == ("pagebuilder_call_budget_exhausted" if limit == "budget" else "pagebuilder_spend_limit")
        assert doc["_runtime"]["state"] == "failed"
        assert result["exception_type"] == "FatalError"
        assert posted[-1]["diagnosis"]["exception_frames"] == result["exception_frames"]
        assert any(event.get("exception_frames") == result["exception_frames"] for event in worker.last["events"])
        if limit == "budget":
            assert result["exception_location"].startswith("app.pagebuilder.runtime:")
    else:
        assert doc["tool_build"]["source_review"]["review"]["suggestions"]
        assert any(event["text"].startswith("Copy suggestion:") for event in worker.last["events"])
    with sqlite3.connect(tmp_path / "local_spend.sqlite3") as connection:
        calls = connection.execute("SELECT status, cost_usd_micro FROM calls").fetchall()
    assert len(calls) == (3 if limit is None else 2)
    assert all(status == "confirmed" and cost > 0 for status, cost in calls), calls


@pytest.mark.parametrize("cancelled", [False, True])
def test_stopped_tool_waits_for_source_checks_without_claiming_completion(page, modules, monkeypatch, cancelled):
    from contextlib import nullcontext
    from app import state
    from app.build.device_orders import KIND
    inputs, plan, files = page
    row = SimpleNamespace(id="page_job", workspace_id="workspace", venture_id=inputs.venture_id)
    order = {"workspace_id": row.workspace_id, "actor_id": "owner", "state": "stopped", "execution_id": "execution",
        "stopped_by_user": cancelled, "native_stop": {"execution_id": "execution", "processes_stopped": True,
            "task_verified": True, "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}},
        "iterations": [{"outcome": "pass"}], "task_snapshot": {"pagebuilder_job_id": row.id}}
    saved = SimpleNamespace(kind=KIND, venture_id=inputs.venture_id, provenance_json=json.dumps(order))
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *args: saved)))
    doc = {"_actor_subject": "owner", "tool_build": {"order_id": "coding_order"}, "_runtime": {"state": "running"}}
    result = modules[2].progress(row, doc)
    assert result["state"] == ("failed" if cancelled else "critiquing")
    if not cancelled:
        assert result["stage"] == "checking" and "preview_url" not in result


def test_local_page_button_offers_tool_setup_without_legacy_build_fallback(page, modules, monkeypatch):
    from app.chat import site_actions
    from app.pagebuilder import api
    monkeypatch.setattr(api, "enabled", lambda: False)
    action = site_actions.build_landing_button()
    assert action["action"] == "pagebuilder.build"
    assert action["payload"] == {"prepare": True}


def test_tool_receives_signup_instructions_without_staged_html(page, modules):
    from tests.unit.product.test_static_publication import _signup_fixture
    inputs, plan, files = page
    destination = {"mode": "mailto", "url": "mailto:owner@example.test", "notice": "Your own email app opens to send this request."}
    inputs = inputs.model_copy(update={"research": {**inputs.research, "local_signup": destination}})
    output = modules[2].reviewed_files(inputs, plan)
    assert output == {}
    assert destination["url"] in modules[2].build_brief(inputs, plan)
    assert "waitlist.html" in modules[2].build_brief(inputs, plan)
    output = dict(files)
    output["waitlist.html"] = _signup_fixture(inputs.name, destination["url"]).encode()
    output["index.html"] = fixture_html(plan, inputs).encode()
    output = _with_social_preview(output, inputs)
    assert modules[2].check_output(output, inputs, plan)["passed"]
    output["waitlist.html"] = b"<html>Your request was sent.</html>"
    assert "local_signup_changed" in modules[2].check_output(output, inputs, plan)["failures"]


@pytest.mark.parametrize("invalid", [None, "labels", "submit", "prechecked", "email_type"])
def test_tool_signup_accepts_independent_markup_without_losing_consent_or_accessibility(page, modules, invalid):
    from tests.unit.product.test_static_publication import _signup_fixture
    import re
    inputs, plan, files = page
    destination = {"mode": "mailto", "url": "mailto:owner@example.test", "notice": "Your own email app opens to send this request."}
    inputs = inputs.model_copy(update={"research": {**inputs.research, "local_signup": destination}})
    signup = _signup_fixture(inputs.name, destination["url"]).replace("'none'", "&apos;none&apos;").replace('"', "'").replace("><", ">\n<")
    signup = signup.replace("</head>", '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action mailto:"></head>')
    if invalid == "labels":
        signup = re.sub(r"</?label\b[^>]*>", "", signup)
    elif invalid == "submit":
        signup = re.sub(r"<button\b[^>]*>.*?</button>", "", signup)
    elif invalid == "prechecked":
        signup = signup.replace("type='checkbox'", "type='checkbox' checked")
    elif invalid == "email_type":
        signup = signup.replace("type='email'", "type")
    output = {**files, "index.html": fixture_html(plan, inputs).encode(), "waitlist.html": signup.encode()}
    output = _with_social_preview(output, inputs)
    checked = modules[2].check_output(output, inputs, plan)
    assert checked["passed"] is (invalid is None), checked
    if invalid:
        assert "local_signup_changed" in checked["failures"]


@pytest.mark.parametrize("destination_domain", ["quartz-inventory.test", "little-kite-club.test"])
def test_signup_submit_must_belong_to_the_form(modules, destination_domain):
    import re
    from app.pagebuilder.local_publish import signup_page

    signup = {"mode": "mailto", "url": f"mailto:join@{destination_domain}", "notice": "Open your email app to join."}
    inputs = SimpleNamespace(venture_id="venture", name=destination_domain, research={"local_signup": signup})
    source = signup_page(inputs.venture_id, inputs.name, signup)
    assert not modules[2].signup_differences({"waitlist.html": source}, inputs)
    moved = re.sub(r"(<button\b[^>]*>.*?</button>)(\s*</form>)", r"\2\1", source, flags=re.S)
    assert moved != source
    assert any("inside the form" in difference for difference in
               modules[2].signup_differences({"waitlist.html": moved}, inputs))


@pytest.mark.parametrize("target,check", [("h1", "required_text_missing"), ("hero_cta", "approved_action_missing_or_ambiguous:hero_cta")])
@pytest.mark.parametrize("ancestor", [False, True])
def test_page_checks_require_visible_heading_and_signup_action(page, modules, target, check, ancestor):
    import re

    inputs, plan, files = page
    html = files["index.html"].decode()
    pattern = r"<h1\b[^>]*>.*?</h1>" if target == "h1" else r'<a\b[^>]*id="hero_cta"[^>]*>.*?</a>'
    hidden = 'style="display: none"'
    changed, count = re.subn(pattern, lambda match: f'<div {hidden}>{match[0]}</div>' if ancestor else
                             match[0].replace(">", f" {hidden}>", 1), html, count=1, flags=re.S)
    assert count == 1
    assert any(failure.startswith(check) for failure in
               modules[2].check_output({**files, "index.html": changed.encode()}, inputs, plan)["failures"])


def test_repeated_local_asset_must_fit_in_the_finished_preview(page, modules):
    inputs, plan, files = page
    asset = "paper-grain.svg"
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"><!--' + b"z" * 34_000 + b"--></svg>"
    css = "".join(f'.grain-{index}{{background-image:url({asset})}}' for index in range(64))
    html = files["index.html"].replace(b"</head>", f"<style>{css}</style></head>".encode())
    checked = modules[2].check_output({**files, "index.html": html, asset: svg}, inputs, plan)
    assert f"pagebuilder_preview_too_large:{asset}" in checked["failures"]


@pytest.mark.parametrize("domain", ["quartz-inventory.test", "little-kite-club.test"])
def test_too_long_first_build_heading_points_to_chat(modules, domain):
    from app.pagebuilder.local import page_check_message

    message = page_check_message([f"sections.0.headline: String should have at most 140 characters ({domain})"])
    assert "140 characters or fewer" in message
    assert "chat" in message


@pytest.mark.parametrize("mode", ["mailto", "endpoint", "netlify"])
def test_tool_signup_accepts_empty_values_and_explicit_default_types(page, modules, mode):
    from app.pagebuilder.local_publish import signup_page
    inputs, plan, files = page
    destination = {"mode": mode, "url": "mailto:owner@example.test" if mode == "mailto" else
        "https://owner.example.test/join" if mode == "endpoint" else "/", "notice": "Your signup destination."}
    inputs = inputs.model_copy(update={"research": {**inputs.research, "local_signup": destination}})
    signup = signup_page(inputs.venture_id, inputs.name, destination)
    signup = signup.replace('name="website"', 'value="" type="text" name="website"')
    signup = signup.replace('<label>Email address<input', '<label for="signup-email">Email address<input id="signup-email" value=""')
    output = _with_social_preview({**files, "index.html": fixture_html(plan, inputs).encode(),
        "waitlist.html": signup.encode()}, inputs)
    checked = modules[2].check_output(output, inputs, plan)
    assert checked["passed"], checked["failures"]


@pytest.mark.parametrize("change, difference", [
    ("action", 'form.action: expected "mailto:owner@example.test", found "mailto:other@example.test"'),
    ("method", 'form.method: expected "post", found "get"'),
    ("enctype", 'form.enctype: expected "text/plain", found "application/x-www-form-urlencoded"'),
    ("prechecked", 'input[name="consent"].checked: expected absent, found present'),
    ("required", 'input[name="consent"].required: expected true, found false'),
    ("digest", 'input[name="consent_digest"]: expected 1 control, found 0'),
    ("version", 'input[name="consent_version"].value: expected'),
    ("visible_honeypot", 'input[name="website"].hidden: expected hidden from visitors'),
    ("honeypot_pattern", 'input[name="website"].pattern: expected "^$", found ".*"'),
    ("honeypot_value", 'input[name="website"].value: expected absent, found "filled"'),
    ("script", 'script: remove this element'),
    ("handler", 'form.onsubmit: remove this attribute'),
    ("csp", "CSP form-action: expected mailto:, found https://other.example.test"),
])
def test_tool_signup_repairs_name_exact_changed_attributes(page, modules, change, difference):
    import re
    from app.pagebuilder.local_publish import signup_page
    inputs, plan, files = page
    destination = {"mode": "mailto", "url": "mailto:owner@example.test", "notice": "Your signup destination."}
    inputs = inputs.model_copy(update={"research": {**inputs.research, "local_signup": destination}})
    signup = signup_page(inputs.venture_id, inputs.name, destination)
    replacements = {
        "action": ('action="mailto:owner@example.test"', 'action="mailto:other@example.test"'),
        "method": ('method="post"', 'method="get"'),
        "enctype": (' enctype="text/plain"', ''),
        "prechecked": ('type="checkbox"', 'type="checkbox" checked'),
        "required": ('name="consent" value="yes" required', 'name="consent" value="yes"'),
        "version": ('name="consent_version" value="', 'name="consent_version" value="changed-'),
        "visible_honeypot": ('<label hidden aria-hidden="true">Website', '<label>Website'),
        "honeypot_pattern": ('pattern="^$"', 'pattern=".*"'),
        "honeypot_value": ('name="website"', 'name="website" value="filled"'),
        "script": ('</head>', '<script>alert(1)</script></head>'),
        "handler": ('<form ', '<form onsubmit="return false" '),
        "csp": ('</head>', '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; form-action https://other.example.test"></head>'),
    }
    if change == "digest":
        signup = re.sub(r'<input[^>]*name="consent_digest"[^>]*>', '', signup)
    else:
        signup = signup.replace(*replacements[change])
    output = _with_social_preview({**files, "index.html": fixture_html(plan, inputs).encode(),
        "waitlist.html": signup.encode()}, inputs)
    checked = modules[2].check_output(output, inputs, plan)
    assert not checked["passed"]
    assert "local_signup_changed" in checked["failures"]
    message = " ".join(modules[2].failure_explanations(checked["failures"]))
    assert difference in message
    assert "SIGNUP_HTML" in message


@pytest.mark.parametrize("planned", [True, False])
def test_tool_brief_supplies_exact_reference_signup_markup(page, modules, planned):
    from app.pagebuilder.local_publish import signup_page
    inputs, plan, _ = page
    destination = {"mode": "mailto", "url": "mailto:owner@example.test", "notice": "Your signup destination."}
    inputs = inputs.model_copy(update={"research": {**inputs.research, "local_signup": destination}})
    brief = modules[2].build_brief(inputs, plan if planned else None)
    assert "SIGNUP_HTML:\n" + signup_page(inputs.venture_id, inputs.name, destination) in brief


def test_disabled_tool_names_both_switch_and_connection_screens(page, modules, monkeypatch):
    from app.core import local_capabilities
    from fastapi import HTTPException
    monkeypatch.setattr(modules[2], "connected_provider", lambda actor: None)
    monkeypatch.setattr(local_capabilities, "capability", lambda name: {"state": "off", "title": "Build with your coding tool"})
    with pytest.raises(HTTPException) as blocked:
        modules[2].require_connected("owner")
    assert "Settings > Capabilities" in blocked.value.detail["message"]
    assert "Settings > Keys and connections > Coding tools" in blocked.value.detail["message"]


@pytest.mark.parametrize("retryable", [True, False])
def test_tool_job_preserves_retry_and_spend_failure_handling(page, modules, monkeypatch, retryable):
    from app.core.jobs import RetryableError
    from app.credits.local_meter import LocalSpendLimit
    from app.pagebuilder import jobs, runtime
    failure = RetryableError("pagebuilder_artifact_write_failed") if retryable else LocalSpendLimit()
    def fail(payload):
        raise failure
    failures = []
    monkeypatch.setattr(modules[2], "run_job", fail)
    monkeypatch.setattr(runtime, "fail", lambda job, payload, error: failures.append(error))
    with pytest.raises(type(failure)):
        jobs.run({"_job_id": "job_page"})
    assert failures == ([] if retryable else [failure])


@pytest.mark.parametrize("repair_succeeds", [True, False])
def test_page_qa_gets_exactly_one_tool_fix_round(page, modules, monkeypatch, tmp_path, repair_succeeds):
    from desktop_runtime import coding_loop, landing_loop
    inputs, plan, files = page
    workspace = tmp_path / "result"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    for name, body in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    prompts = []

    def run(prompt, deadline):
        prompts.append(prompt)
        html = files["index.html"] if len(prompts) > 1 and repair_succeeds else files["index.html"].replace(b"<main", b"<div").replace(b"</main>", b"</div>")
        (workspace / "index.html").write_bytes(html)
        return {"outcome": "pass", "workspace_marker_verified": True}

    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    from app.build.device_orders import CodingLoopIteration
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda receipt:
        CodingLoopIteration.model_validate({**receipt, "execution_id": "dex_page"}))
    monkeypatch.setattr(coding_loop, "_checkpoint", lambda *args: "a" * 40)
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 120, "per_call_seconds": 60},
        "instruction": modules[2].build_brief(inputs, plan)}
    result = landing_loop.run(worker, manifest, gateway, coding, float("inf"))
    assert len(prompts) == 2
    assert "semantic_landmarks" in prompts[1]
    assert result["task_verified"] is repair_succeeds
    assert len(worker.last["iteration_receipts"]) == 2
    assert all(receipt["posted"] for receipt in worker.last["iteration_receipts"])
    assert (workspace / "index.html").read_bytes() == (files["index.html"] if repair_succeeds else
        files["index.html"].replace(b"<main", b"<div").replace(b"</main>", b"</div>"))


@pytest.mark.parametrize("outcome", ["cli_error", "timeout", "stopped"])
def test_page_tool_failure_keeps_its_sentence_and_setup_step(page, modules, monkeypatch, tmp_path, outcome):
    from desktop_runtime import coding_loop, landing_loop
    from desktop_runtime.owned_coding import _trusted_git
    from app.build.device_orders import CodingLoopIteration

    inputs, plan, files = page
    workspace = tmp_path / "result"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    message = f"I couldn't build {inputs.name}. Open Settings > Keys and connections > Coding tools, check your connection, then retry from Build activity."
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True),
        run=lambda *args: {"outcome": outcome, "failure_message": message})
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    if outcome == "stopped":
        worker.loop_saved["task_snapshot"]["pagebuilder_job_id"] = "job_stopped"
        monkeypatch.setattr(modules[2], "check_output", lambda *args: {"passed": True, "failures": []})
        monkeypatch.setattr("app.pagebuilder.runtime.read", lambda *args: pytest.fail("A stopped tool must not start source review"))
    posted = []
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda receipt:
        posted.append(CodingLoopIteration.model_validate({**receipt, "execution_id": "dex_page"})))
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 120, "per_call_seconds": 60},
        "instruction": modules[2].build_brief(inputs, plan)}
    result = landing_loop.run(worker, manifest, gateway, coding, float("inf"))
    assert result["iterations"] == (1 if outcome == "stopped" else 2) and result["task_verified"] is False
    if outcome == "stopped":
        assert posted[0].diagnosis["next_action"] == "stop"
    assert posted[0].summary == message
    assert worker.last["iteration_receipts"][0]["summary"] == message
    assert worker.last["message"] == message
    _trusted_git(workspace, "status", "--porcelain=v1")


@pytest.mark.usefixtures("private_coding_install")
@pytest.mark.parametrize("writes_files", [True, False])
@pytest.mark.parametrize("exit_code", [0, 2])
def test_final_tool_note_does_not_discard_checked_deliverables(page, modules, monkeypatch, tmp_path, writes_files, exit_code):
    import sys
    import time
    from desktop_runtime import agent_status, landing_loop
    from desktop_runtime.owned_coding import OwnedCoding
    from desktop_runtime.owned_processes import OwnedProcesses
    from app.build.device_orders import CodingLoopIteration

    inputs, plan, files = page
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    note = ("I couldn't run the screenshot check, but index.html and assets are written" if writes_files
        else f"I couldn't build {inputs.name} because the required tool is unavailable.")
    binary = tmp_path / "codex"
    event = {"type": "item.completed", "item": {"type": "agent_message", "text": note}}
    encoded = {name: base64.b64encode(body).decode() for name, body in files.items()} if writes_files else {}
    binary.write_text(f'#!{sys.executable}\nimport base64,json,sys\nfrom pathlib import Path\n'
        'if "--version" in sys.argv:\n print("codex-cli 0.163.0"); sys.exit(0)\n'
        f'for name,body in {encoded!r}.items():\n path=Path(name); path.parent.mkdir(parents=True,exist_ok=True); path.write_bytes(base64.b64decode(body))\n'
        f'print(json.dumps({event!r}))\n'
        'print(json.dumps({"type":"turn.completed"}))\n'
        f'sys.exit({exit_code})\n')
    binary.chmod(0o755)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id=inputs.venture_id, workspace_slug="page", provider="codex",
        tracker=tracker, authorize=lambda: True)
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    tool_results = []
    tool_run = coding.run
    def run(*args):
        result = tool_run(*args)
        tool_results.append(result)
        return result
    monkeypatch.setattr(coding, "run", run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    posted = []
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda receipt:
        posted.append(CodingLoopIteration.model_validate({**receipt, "execution_id": "dex_page"})))
    manifest = {"coding_loop_verification": {"max_iterations": 1, "max_wall_seconds": 120, "per_call_seconds": 60},
        "instruction": modules[2].build_brief(inputs, plan)}
    monkeypatch.setattr(agent_status, "_PROBES_CLOSED", False)
    try:
        result = landing_loop.run(worker, manifest, gateway, coding, time.monotonic() + 30)
        assert result["task_verified"] is writes_files
        if writes_files:
            assert tool_results[-1]["outcome"] == ("pass" if exit_code == 0 else "cli_error")
            assert posted[-1].outcome == "pass"
            assert note in posted[-1].summary
            assert all((workspace / name).read_bytes() == body for name, body in files.items())
            assert modules[2].bundle(landing_loop.public_files(workspace), inputs, plan, {"passed": True})["qa"]["passed"]
        else:
            assert note in posted[-1].summary
            assert "Build activity" in posted[-1].summary
    finally:
        tracker.stop_all()
        agent_status.stop_probes()


@pytest.mark.parametrize("fault", ["marker", "git_config", "cleanup_error", "cleanup_only", "reference", "nested_git"])
def test_page_guard_restores_metadata_without_removing_tool_files(page, modules, monkeypatch, tmp_path, fault):
    from desktop_runtime import coding_loop, landing_loop
    from app.build.device_orders import CodingLoopIteration

    inputs, plan, files = page
    files = {**files, "artifacts/tool-notes.txt": b"Saved tool notes", "review/check.py": b"pass\n"}
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    if fault == "cleanup_only":
        (workspace / "artifacts").mkdir()
        (workspace / "artifacts/tool-notes.txt").write_bytes(files["artifacts/tool-notes.txt"])
    if fault == "reference":
        (workspace / ".reference").mkdir()
        (workspace / ".reference/pinned.txt").write_text(inputs.name)
    def run(*args):
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        if fault in {"marker", "cleanup_error"}:
            if fault == "marker":
                (workspace / ".supymem-venture").unlink()
            raise FileNotFoundError("private workspace path")
        if fault == "reference":
            (workspace / ".reference/pinned.txt").write_text("Changed reference")
        elif fault == "nested_git":
            metadata = workspace / "assets/tool-work/.git"
            metadata.mkdir(parents=True)
            (metadata / "config").write_text('[include]\npath=/outside/private-config\n')
            (metadata.parent / "saved.txt").write_text(inputs.name)
        elif fault != "cleanup_only":
            (workspace / ".git/config").write_text('[include]\npath=/outside/private-config\n')
        return {"outcome": "pass", "workspace_marker_verified": True}
    def stop_all(**kwargs):
        if fault in {"cleanup_error", "cleanup_only"}:
            raise OSError("private cleanup detail")
        return True
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=stop_all), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    posted = []
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=lambda receipt:
        posted.append(CodingLoopIteration.model_validate({**receipt, "execution_id": "dex_page"})))
    monkeypatch.setattr(coding_loop, "_quiescent", lambda workspace, tracker: tracker.stop_all())
    manifest = {"coding_loop_verification": {"max_iterations": 1, "max_wall_seconds": 120, "per_call_seconds": 60},
        "instruction": modules[2].build_brief(inputs, plan)}
    result = landing_loop.run(worker, manifest, gateway, coding, float("inf"))
    assert result["task_verified"] is False
    assert all((workspace / name).read_bytes() == body for name, body in files.items())
    assert (workspace / ".supymem-venture").read_text() == inputs.venture_id
    assert "private-config" not in (workspace / ".git/config").read_text()
    if fault == "reference":
        assert (workspace / ".reference/pinned.txt").read_text() == inputs.name
    elif fault == "nested_git":
        assert not (workspace / "assets/tool-work/.git").exists()
        assert (workspace / "assets/tool-work/saved.txt").read_text() == inputs.name
    if fault in {"marker", "cleanup_error"}:
        assert result["failure_stage"] == "code"
        assert result["exception_type"] == "FileNotFoundError"
        assert result["failure_code"] == "coding_workspace_file_missing"
        assert posted[-1].diagnosis["failure_code"] == result["failure_code"]
        assert posted[-1].diagnosis["exception_type"] == result["exception_type"]
    elif fault == "cleanup_only":
        assert result["outcome"] == "stopped_unclean"
        assert result["failure_stage"] == "cleanup"
        assert result["exception_type"] == "OSError"
        assert result["failure_code"] == "native_execution_failed"
        assert posted[-1].diagnosis["failure_stage"] == "cleanup"
        assert "Build activity" in posted[-1].summary
        assert "private cleanup detail" not in posted[-1].summary


@pytest.mark.parametrize("mode", ["landing_slow", "landing_timeout"])
def test_offline_journey_checks_slow_and_timed_out_codex_files(page, modules, monkeypatch, request, mode):
    from pathlib import Path
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "scripts"))
    from local_build_journey import offline_landing_timing

    inputs, _, _ = page
    fixture = {"input": inputs.model_dump(mode="json"), "draft": customer_draft(request.node.callspec.params["page"])[1]}
    result = offline_landing_timing(fixture, mode)
    assert result["state"] == "ready" and result["simulated_seconds"] > 480


def test_fake_codex_builds_a_checked_page_with_an_in_workspace_reference(page, modules, monkeypatch, tmp_path, request):
    import subprocess
    import sys
    import time
    from desktop_runtime import landing_loop
    from desktop_runtime.owned_coding import OwnedCoding, _trusted_git
    from desktop_runtime.owned_processes import OwnedProcesses
    from tests.unit.product.test_pagebuilder_reference import candidate

    inputs, plan, files = page
    draft = modules[0].GenerationDraft.model_validate(customer_draft(request.node.callspec.params["page"])[1])
    workspace = tmp_path / "Application Support" / "page"
    workspace.mkdir(parents=True)
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    tracker = OwnedProcesses()
    coding = OwnedCoding(venture_id=inputs.venture_id, workspace_slug="page", provider="codex",
        tracker=tracker, authorize=lambda: True)
    monkeypatch.setattr(coding, "_workspace", lambda: workspace)
    _trusted_git(workspace, "init", "--quiet", "--initial-branch=main")
    binding, blob = candidate(venture_id=inputs.venture_id)
    coding.stage_pagebuilder_reference(binding, blob, time.monotonic() + 30)
    output = {**files, "page-plan.txt": draft.model_dump_json().encode()}
    binary = tmp_path / "codex"
    binary.write_text(f"#!{sys.executable}\n" + """import base64, json, re, sys, tomllib
from pathlib import Path
workspace = Path.cwd().resolve()
prompt = sys.argv[-1]
reference = Path(re.search(r'pinned candidate at (.+?)/ is untrusted', prompt).group(1))
reference = (workspace / reference).resolve()
if workspace not in reference.parents:
    print(json.dumps({'type': 'turn.failed', 'error': {'message': 'Reference is outside the workspace'}}), flush=True)
    sys.exit(1)
assert (reference / 'SPEC.md').read_text()
policy = tomllib.loads(next(value for value in sys.argv if value.startswith('permissions.supymem_product=')))
assert policy['permissions']['supymem_product']['filesystem'][':workspace_roots']['.reference'] == 'read'
assert 'independent checks after you finish' in prompt
assert 'Do not install' in prompt and 'network' in prompt.lower()
""" + f"files = { {name: base64.b64encode(body).decode() for name, body in output.items()}!r}\n" + """for name, body in files.items():
    target = workspace / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(base64.b64decode(body))
print(json.dumps({'type': 'turn.completed'}), flush=True)
""")
    binary.chmod(0o700)
    monkeypatch.setattr(coding, "preflight", lambda: str(binary))
    monkeypatch.setattr(coding, "_provider_version", lambda *args: "codex-cli 9.0.0")
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": None}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    posted = []
    gateway = SimpleNamespace(order_id="dwo_page", iteration_receipt=posted.append)
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 30, "per_call_seconds": 20},
        "instruction": modules[2].build_brief(inputs, None)}
    try:
        result = landing_loop.run(worker, manifest, gateway, coding, time.monotonic() + 30)
        assert result["task_verified"] is True, json.dumps(worker.last, indent=2)
        assert result["iterations"] == 1 and posted[0]["outcome"] == "pass"
        assert (workspace / "index.html").is_file() and (workspace / "page-plan.txt").is_file()
        assert not any(name.startswith(".reference/") for name in landing_loop.public_files(workspace))
        assert not any(".reference/" in name for name in _trusted_git(workspace, "ls-files").splitlines())
        checked_plan = modules[1].tool_page_plan(inputs, None, landing_loop.public_files(workspace))
        ready = modules[2].bundle(landing_loop.public_files(workspace), inputs, checked_plan, {"passed": True})
        assert ready["qa"]["passed"] is True
        rejected = subprocess.run([str(binary), f"The pinned candidate at {tmp_path}/outside/ is untrusted"],
            cwd=workspace, env=coding._env(workspace), stdin=subprocess.DEVNULL, capture_output=True,
            start_new_session=True, timeout=3, check=False)
        assert rejected.returncode == 1 and b"Reference is outside the workspace" in rejected.stdout
    finally:
        tracker.stop_all()


def test_checked_tool_page_remains_usable_by_existing_zip_export(page, modules):
    inputs, plan, files = page
    tool = modules[2]
    checked = tool.check_output(files, inputs, plan)
    assert checked["passed"] is True, checked["failures"]
    bundle = tool.bundle(files, inputs, plan, {"passed": True}, repairs=1)
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["source_zip"]))) as archive:
        assert archive.read("index.html") == files["index.html"]
        assert all(archive.read(name) == base64.b64decode(body) for name, body in bundle["files"].items())
    assert bundle["qa"]["repair_iterations"] == 1
    assert bundle["manifest"]["artifact_profile"] == "static_landing_v1"


def test_tool_bundle_passes_existing_saved_page_gate_with_metered_brief_review(page, modules):
    from app.pagebuilder import api, native_reference
    from app.build.device_orders import KIND
    from core.artifact_snapshot import manifest_digest
    inputs, plan, files = page
    reviewed = review_payload(modules[1]._grounding_prompt(plan, inputs))
    plan_sha = modules[1].plan_digest(plan)
    grounding = {"policy": "semantic_capability_v4", "passed": True, "plan_sha256": plan_sha,
        "scope": "generation_only", "independent_qa_attestation": False,
        "reviews": [{"stage": "capability_review_1", "plan_sha256": plan_sha,
            "review": reviewed, "review_sha256": api.digest(reviewed)}]}
    saved = modules[2].bundle(files, inputs, plan, grounding)
    quote = {"request_policy": api.REQUEST_POLICY, "call_budget": api.CALLS_PER_VARIANT,
        "call_timeout_seconds": api.CALL_TIMEOUT_SECONDS, "max_output_tokens": api.MAX_OUTPUT_TOKENS,
        "provider": "azure_foundry", "model": "DeepSeek-V4-Pro", "image": None,
        "reasoning_policy": api.reasoning_policy("azure_foundry", "DeepSeek-V4-Pro")}
    doc = {"variant_count": 1, "result": {"bundles": [saved]}, "_runtime": {"quote": quote,
        "input": inputs.model_dump(mode="json"), "operations": {"variant_0:capability_review_1": {
            "state": "completed", "usage": {"settled": True}, "response": {"content": json.dumps(reviewed)}}}}}
    assert api._candidate_current(doc)
    quote.update(payer="founder", provider_identity="test-identity")
    usage = doc["_runtime"]["operations"]["variant_0:capability_review_1"]["usage"]
    usage.update(provider=quote["provider"], model=quote["model"], provider_identity=quote["provider_identity"], source="byok")
    doc.update(_actor_subject="owner", tool_build={"order_id": "dwo_page"})
    doc["_runtime"]["state"] = "ready"
    row = SimpleNamespace(id="job_page", workspace_id="workspace", venture_id=inputs.venture_id, payload_json=api.dump(doc))
    order = {"workspace_id": row.workspace_id, "actor_id": "owner", "state": "stopped", "execution_id": "dex_page",
        "native_stop": {"execution_id": "dex_page", "processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}},
        "task_snapshot": {"pagebuilder_job_id": row.id, "page_input": doc["_runtime"]["input"], "page_plan": saved["plan"]},
        "iterations": [{"outcome": "pass", "file_manifest_sha256": manifest_digest(saved["manifest"]["manifest"])}]}
    record = SimpleNamespace(kind=KIND, workspace_id=row.workspace_id, venture_id=row.venture_id, provenance_json=api.dump(order))
    session = SimpleNamespace(get=lambda *args: record)
    assert native_reference._completed_candidate(session, row) == doc
    for changed in ({"state": "approved"}, {"cancelled_at": "stopped"}, {"workspace_id": "other"},
            {"iterations": [{"outcome": "pass", "file_manifest_sha256": "a" * 64}]}, {"task_snapshot": {"pagebuilder_job_id": "other"}}):
        record.provenance_json = api.dump({**order, **changed})
        with pytest.raises(ValueError, match="pagebuilder_candidate_invalid"):
            native_reference._completed_candidate(session, row)
    doc["_runtime"]["operations"]["variant_0:capability_review_1"]["usage"]["settled"] = False
    assert not api._candidate_current(doc)


@pytest.mark.parametrize("context_window", [8192, 2048])
def test_loopback_page_check_fits_complete_review(page, modules, monkeypatch, context_window):
    from app.core import providers_local
    from app.core.jobs import FatalError
    from app.pagebuilder import api, local, runtime
    from llm_gateway.local_transport import LoopbackJSONClient
    from llm_gateway.provider_foundry import ProviderConfig

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    settings = {} if context_window == 8192 else {"context_window": context_window}
    config = ProviderConfig(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", **settings)
    assert config.context_window == context_window
    monkeypatch.setattr(providers_local, "load_local_config", lambda: config)
    inputs, plan, _ = page
    inputs = inputs.model_copy(update={"clarified_answers": {**inputs.clarified_answers,
        "details": [section.body for section in plan.sections] * 6}})
    doc = {"_actor_subject": "owner", "tool_build": {},
        "_runtime": {"operations": {}, "quote": {"provider": config.provider}}}
    row = SimpleNamespace(workspace_id="workspace", venture_id=inputs.venture_id)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr(api, "authority", lambda *args, **kwargs: (row.workspace_id, "owner"))
    monkeypatch.setattr(runtime, "mutate", lambda job_id, payload, callback: callback(row, doc, None))
    transport = object.__new__(LoopbackJSONClient)
    transport._context_window = context_window
    calls = []
    responses = {}

    def review(stage, prompt):
        calls.append(stage)
        messages = runtime._model_messages(stage, prompt)
        max_tokens = runtime._loopback_review_tokens(config, messages, api.MAX_OUTPUT_TOKENS)
        assert transport._fit_context({"messages": messages, "max_tokens": max_tokens})["messages"] == messages
        facts = json.loads(prompt.split("CONFIRMED_FACTS:\n", 1)[1].split("\nPUBLIC_COPY_BY_FIELD:", 1)[0])
        assert facts["clarified_answers"] == inputs.clarified_answers and "clarified_answers_json" not in facts
        assert facts["signup_consent"] == inputs.waitlist_consent_text
        responses[stage] = review_payload(prompt)
        return json.dumps(responses[stage])

    monkeypatch.setattr(runtime, "job_model_call", lambda *args, **kwargs: review)
    if context_window == 2048:
        with pytest.raises(FatalError, match="pagebuilder_prompt_too_large"):
            modules[2].review_tool_source("page_job", inputs, plan, {"reviews": []})
        assert not calls
        message = local.failure_reason("pagebuilder_prompt_too_large")
        assert all(step in message for step in ("Your AI provider > Context window", "Increase", "Website", "Retry"))
        assert not doc["tool_build"] and "Nothing was published" in message
        return

    checked = modules[2].review_tool_source("page_job", inputs, plan, {"reviews": []})
    assert len(calls) > 1 and len(set(calls)) == len(calls)
    assert checked["passed"] is True
    review_result = modules[0].CapabilityReview.model_validate(checked["reviews"][-1]["review"])
    assert [item.field_id for item in review_result.field_reviews] == list(modules[1].public_plan_copy(plan))
    modules[1].validate_grounding_review(review_result, plan, inputs)
    batches = doc["tool_build"]["source_review"]["batches"]
    section_sets = [{field.rsplit(".", 1)[0] for field in batch["field_ids"]} for batch in batches.values()]
    assert sum(map(len, section_sets)) == len(set.union(*section_sets))
    last = responses[calls[-1]]["field_reviews"][-1]
    last.update(classification="supported", source_quotes=["This quote is not from the founder or signup consent."])
    with pytest.raises(ValueError, match="pagebuilder_grounding_source_quote_invalid"):
        modules[2].checked_review_batches(plan, inputs, batches, lambda stage, _: json.dumps(responses[stage]))


def test_page_output_rejects_backend_and_secret_files(page, modules):
    inputs, plan, files = page
    for name, body in (("server.py", b"print('backend')"), (".env", b"TOKEN=not-a-real-key")):
        checked = modules[2].check_output({**files, name: body}, inputs, plan)
        assert not checked["passed"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("entry", ["build", "start"])
@pytest.mark.parametrize("status", ["scheduled", "failed_dead"])
def test_retry_stopped_coding_page_starts_a_new_job(setup, monkeypatch, domain, status, entry):
    from app.build.device_orders import KIND
    from app.db.models import Job, VentureDocument
    from app.pagebuilder import api, tool
    from app.core.local_owner import ensure_owner
    from app.core.providers_local import save_settings
    from tests.unit.pagebuilder.test_api import seed
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data = seed(setup["factory"], actor=ensure_owner().id)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": "DeepSeek-V4-Flash", "planning": "DeepSeek-V4-Pro"})
    monkeypatch.setattr(api, "require_enabled", lambda: None)
    monkeypatch.setattr(tool, "connected_provider", lambda actor: "codex")
    monkeypatch.setattr(tool, "prepare_job", lambda *args, **kwargs: None)
    original = api.build(data["vid"], data["actor"])["job_id"]
    raw, draft = customer_draft(domain)
    with data["factory"]() as session:
        row = session.get(Job, original)
        doc = json.loads(row.payload_json)
        doc["_runtime"].update(state="running", stage="building")
        doc["tool_build"] = {"order_id": "dwo_failed"}
        row.status, row.payload_json = status, api.dump(doc)
        session.add(VentureDocument(id="dwo_failed", venture_id=data["vid"], kind=KIND, title=raw["name"],
            created_at=utcnow(), updated_at=utcnow(), provenance_json=api.dump({
                "workspace_id": data["wid"], "actor_id": data["actor"], "state": "stopped",
                "native_stop": {"processes_stopped": True, "task_verified": False}})))
        session.commit()
    retried = getattr(api, entry)(data["vid"], data["actor"])
    assert retried["state"] == "queued" and retried["job_id"] != original
    assert api.build(data["vid"], data["actor"])["job_id"] == retried["job_id"]
    with data["factory"]() as session:
        assert session.get(Job, original).status == status
        assert "tool_build" not in json.loads(session.get(Job, retried["job_id"]).payload_json)


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("state", ["queued", "failed"])
def test_stopped_page_uses_the_shared_watch_screen_and_offers_retry(setup, monkeypatch, domain, state):
    from app.build.device_orders import KIND
    from app.db.models import VentureDocument, Venture
    from app.pagebuilder import api, runtime, tool
    from core.clock import utcnow
    data = setup
    job_id = api.build(data["vid"], data["actor"])["job_id"]
    raw, draft = customer_draft(domain)
    message = f"Your tool stopped while building {raw['name']}. Open Build activity and retry your page."
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = raw["idea"]
        session.add(VentureDocument(id="dwo_page", venture_id=data["vid"], kind=KIND, title=raw["name"],
            created_at=utcnow(), updated_at=utcnow(), provenance_json=api.dump({
                "workspace_id": data["wid"], "actor_id": data["actor"], "state": "cancelled",
                "iterations": [{"outcome": "timeout", "summary": message}]})))
        session.commit()
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    row = runtime.read(job_id)
    doc = json.loads(row.payload_json)
    doc["tool_build"] = {"order_id": "dwo_page"}
    doc["_runtime"]["state"] = state
    row.payload_json = api.dump(doc)
    view = api._view(row)
    assert view["state"] == "failed" and view["watch_url"].endswith("/device-work-orders/dwo_page")
    assert view["retry"]["body"] == {"new_version_of": job_id}
    assert view["error_reason"] == message


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("cancelled", [None, "page", "order", "upload"])
def test_saved_tool_page_uses_existing_authenticated_source_export(setup, monkeypatch, domain, tmp_path, cancelled):
    from app.pagebuilder import api, tool, pipeline, models, native_reference, runtime
    from app.api import pagebuilder_api
    from app.build import device_orders
    from app.db.models import Job, VentureDocument
    from app.chat import site_actions
    from core.artifact_snapshot import freeze_source, manifest_digest
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.core.local_owner import ensure_owner, _ensure_proxy_secret
    from app.core.providers_local import save_settings
    from app import coding_agents_consent
    from desktop_runtime import agent_status
    from tests.unit.pagebuilder.test_api import seed
    _ensure_proxy_secret()
    data = seed(setup["factory"], actor=ensure_owner().id)
    save_settings(provider="azure_openai", base_url="https://provider.example.invalid/v1",
        api_key="synthetic-local-test-key", models={"chat": "DeepSeek-V4-Flash", "planning": "DeepSeek-V4-Pro"})
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])
    monkeypatch.setattr(tool, "prepare_job", lambda *args, **kwargs: None)
    started = api.build(data["vid"], data["actor"])
    job_id = started["job_id"]
    raw_inputs, draft = customer_draft(domain)
    inputs = models.PageInput.model_validate({**raw_inputs, "venture_id": data["vid"],
        "waitlist_url": "https://gateway.supymem.com/public/waitlists/" + data["vid"]})
    from app.pagebuilder import images
    plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    root = tmp_path / "public"
    root.mkdir()
    files = {asset.path: asset.svg.encode() for asset in plan.assets}
    files["index.html"] = fixture_html(plan, inputs).encode()
    files = _with_social_preview(files, inputs)
    for name, body in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
    manifest, archive = freeze_source(root)
    with data["factory"]() as session:
        row = session.get(Job, job_id)
        doc = json.loads(row.payload_json)
        doc["_runtime"]["input"] = inputs.model_dump(mode="json")
        doc["tool_build"] = {"order_id": "dwo_page", "plan": plan.model_dump(mode="json"), "grounding": {"passed": True}}
        row.status, row.payload_json = ("failed_dead" if cancelled == "upload" else "succeeded"), api.dump(doc)
        session.commit()
    monkeypatch.setattr(api, "require_enabled", lambda: None)
    monkeypatch.setattr(runtime, "validate_inputs", lambda *args: None)
    monkeypatch.setattr(api, "_candidate_current", lambda doc: tool.check_output(
        {name: base64.b64decode(body) for name, body in doc["result"]["bundles"][0]["files"].items()}, inputs, plan)["passed"])
    monkeypatch.setattr(native_reference, "persist_job", lambda *args: None)
    def review(job_id, inputs, plan, grounding):
        if cancelled == "page":
            with data["factory"]() as session:
                row = session.get(Job, job_id)
                row.cancel_requested, row.status = True, "cancelled"
                session.commit()
        elif cancelled == "order":
            order["cancelled_at"] = "2026-11-15T15:05:00Z"
        return grounding
    monkeypatch.setattr(tool, "review_tool_source", review)
    monkeypatch.setattr(site_actions, "page_built", lambda *args: None)
    order = {"workspace_id": data["wid"], "actor_id": data["actor"], "state": "stopped", "execution_id": "dex_page",
        "native_stop": {"execution_id": "dex_page", "processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}},
        "task_snapshot": {"pagebuilder_job_id": job_id}, "iterations": [{"outcome": "pass", "file_manifest_sha256": manifest_digest(manifest)}]}
    monkeypatch.setattr(device_orders, "retrieve", lambda *args: order)
    if cancelled == "upload":
        from core.clock import utcnow
        from desktop_runtime.paths import data_root
        path = data_root(create=True) / "ventures" / data["vid"] / "coding_sources" / "dwo_page" / "dex_page-landing.zip"
        path.parent.mkdir(parents=True)
        path.write_bytes(archive)
        with data["factory"]() as session:
            session.add(VentureDocument(id="dwo_page", venture_id=data["vid"], kind=device_orders.KIND,
                title="Checked page", provenance_json=api.dump(order), created_at=utcnow(), updated_at=utcnow()))
            session.commit()
    from fastapi import FastAPI
    from app.core.local_owner import LocalOwnerMiddleware
    from tests.unit.test_local_mode import _owner_client
    application = FastAPI()
    application.add_middleware(LocalOwnerMiddleware)
    application.include_router(pagebuilder_api.router, prefix="/api")
    with _owner_client(application) as client:
        base = f"/api/ventures/{data['vid']}/pagebuilder/jobs/{job_id}"
        if cancelled == "upload":
            with monkeypatch.context() as failure:
                failure.setattr(tool, "finish", lambda *args: (_ for _ in ()).throw(OSError("temporary loss")))
                interrupted = client.post(base + "/tool-result", json={"order_id": "dwo_page", "execution_id": "dex_page",
                    "source_zip": base64.b64encode(archive).decode()})
                assert interrupted.status_code == 503
            assert api.status(data["vid"], data["actor"], job_id)["state"] == "failed"
            response = client.post(base + "/retry-tool-result")
        else:
            response = client.post(base + "/tool-result", headers={"X-Subject": data["actor"]},
                json={"order_id": "dwo_page", "execution_id": "dex_page", "source_zip": base64.b64encode(archive).decode()})
        if cancelled in {"order", "page"}:
            assert response.status_code == 409, response.text
            assert ("Open Website" if cancelled == "page" else "Open Build activity") in response.json()["detail"]["message"]
            saved = runtime.read(job_id)
            assert json.loads(saved.payload_json)["_runtime"]["state"] != "ready"
            if cancelled == "page":
                assert saved.status == "cancelled"
            return
        assert response.status_code == 200, response.text
        assert response.json()["state"] == "ready"
    exported = pagebuilder_api.source(data["vid"], job_id, variant=0, caller=data["actor"])
    with zipfile.ZipFile(io.BytesIO(exported.body)) as saved:
        assert saved.read("index.html") == files["index.html"]
    assert api.status(data["vid"], data["actor"], job_id)["state"] == "ready"
    saved_job = runtime.read(job_id)
    assert tool.progress(saved_job, json.loads(saved_job.payload_json))["watch_url"].endswith("/device-work-orders/dwo_page")
    order["cancelled_at"] = "2026-11-15T15:05:00Z"
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        tool.finish(data["vid"], data["actor"], job_id, "dwo_page", "dex_page", archive)


def test_page_fix_round_keeps_details_for_the_tool_and_a_plain_next_step_for_you(page, modules, monkeypatch, tmp_path):
    from desktop_runtime import coding_loop, landing_loop

    inputs, plan, files = page
    tool = modules[2]
    invalid = plan.direction.model_dump()
    invalid["palette"].pop("primary")
    try:
        modules[0].DesignDirection.model_validate(invalid)
    except ValueError as error:
        failures = tool.validation_messages(error)
    checks = iter([{"passed": False, "failures": failures}, {"passed": True, "failures": []}])
    monkeypatch.setattr(tool, "check_output", lambda *args: next(checks))
    monkeypatch.setattr(coding_loop, "_quiescent", lambda *args: True)
    workspace = tmp_path / "page"
    workspace.mkdir()
    (workspace / ".supymem-venture").write_text(inputs.venture_id)
    prompts = []
    def run(prompt, deadline):
        prompts.append(prompt)
        for name, body in files.items():
            target = workspace / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
        return {"outcome": "pass", "workspace_marker_verified": True}
    coding = SimpleNamespace(provider="codex", _workspace=lambda: workspace, authorize=lambda: True,
        tracker=SimpleNamespace(stop_all=lambda **kwargs: True), run=run)
    worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
        loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": plan.model_dump(mode="json")}},
        progress=lambda state, **values: worker.last.update(state=state, **values))
    posted = []
    manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 1800, "per_call_seconds": 900},
        "instruction": tool.build_brief(inputs, plan)}
    result = landing_loop.run(worker, manifest, SimpleNamespace(order_id="dwo_page", iteration_receipt=posted.append), coding, float("inf"))
    assert result["task_verified"] and len(prompts) == 2
    assert all(failure in prompts[1] for failure in failures)
    assert posted[0]["diagnosis"]["failures"] == failures
    assert any("Your page did not pass its checks" in event["text"] and "Open Build activity" in event["text"]
               for event in worker.last["events"])
    assert all("missing primary" not in event["text"] for event in worker.last["events"])
