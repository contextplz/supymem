"""Build admission ignores market context and journey fixtures keep full contracts."""
import base64
import io
import json
import subprocess
import zipfile

import pytest

from tests.unit.test_local_build_journey import journey


@pytest.fixture(scope="module", autouse=True)
def company_app():
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()


@pytest.mark.parametrize("idea", [
    "A health records overview for clinic administrators with payment and database industry trends",
    "A fintech newsletter explaining billing, Stripe and secure accounts to finance teams",
    "A hardware sensor comparison guide for factory purchasing managers",
    "A family hobby guide covering iOS photography, saved memories and Android devices",
])
@pytest.mark.parametrize("instruction", ["Build a landing page", "Build the first version of the product"])
def test_scope_uses_requested_deliverables_not_venture_industry(idea, instruction):
    from app.build.product_scope import assess, assert_supported

    scope = assess({"idea": {"summary": idea}, "request": instruction,
        "plan": {"idea_summary": idea, "tasks": [{"title": "Show the concept",
            "description": "Create a static web page and a small demo API."}]}})
    assert_supported(scope)
    assert len(scope.not_in_this_build) == 1


@pytest.mark.parametrize("requirement", ["a shared database", "secure login", "Stripe checkout",
    "native mobile hardware", "a licensed data API", "persistent storage", "an iOS17 app"])
@pytest.mark.parametrize("source", ["request", "plan"])
def test_scope_still_refuses_unsupported_requested_deliverables(requirement, source):
    from app.build.product_scope import UnsupportedProductScope, assess, assert_supported

    snapshot = {"idea": "A new service for your audience"}
    if source == "request":
        snapshot["request"] = "Build " + requirement
    else:
        snapshot["plan"] = {"tasks": [{"title": "Implement " + requirement}]}
    with pytest.raises(UnsupportedProductScope):
        assert_supported(assess(snapshot))


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
def test_fake_page_keeps_required_plan_for_reviewed_brief(tmp_path, monkeypatch, domain, provider):
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.pagebuilder import images, models, pipeline, tool

    data, draft = customer_draft(domain)
    data.setdefault("clarified_answers", {})["workflow"] = draft["sections"][2]["body"]
    draft["direction"]["palette"].update(background="#F7F4FE", text="#212138", primary="#D8C2FA")
    for section in draft["sections"]:
        section["nav_label"] = None if section["id"] == "waitlist" else section["headline"]
    inputs = models.PageInput.model_validate(data)
    plan = final_plan({"images": images, "models": models, "pipeline": pipeline}, inputs, draft)
    binary = journey.install_fake_cli(tmp_path / "bin", provider)
    other_data, other_draft = customer_draft("consumer" if domain == "b2b" else "b2b")
    (binary.parent / "page-fixture.json").write_text(json.dumps({"root": str(journey.ROOT),
        "template": {"input": other_data, "draft": other_draft}}))
    workspace = tmp_path / "result"
    workspace.mkdir()
    prompt = tool.build_brief(inputs, plan)
    command = [str(binary), "-p", prompt] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), prompt]
    subprocess.run(command, cwd=workspace, capture_output=True, text=True, check=True)
    assert models.GenerationDraft.model_validate(json.loads((workspace / "page-plan.txt").read_text())) == models.GenerationDraft.model_validate(draft)
    files = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    assert pipeline.tool_page_plan(inputs, None, files).brief == plan.brief
    checked = tool.check_output(files, inputs, None)
    assert checked["passed"], checked["failures"]
    assert f'background:{plan.direction.palette["background"]}' in files["index.html"].decode()


@pytest.mark.parametrize("domain,name,idea,audience,workflow", [
    ("b2b", 'Parts & Logs', "A proposed parts inspection notebook for machine shop supervisors. Record inspection notes and review replacement requests.", "Machine shop supervisors",
        "Record the inspected part. Add the inspection notes for that part. Review the notes before deciding on a replacement."),
    ("consumer", 'Clay "Days"', "A proposed pottery practice notebook for hobbyists. Record clay experiments and review glaze notes.", "Pottery hobbyists",
        "Record a clay mixture. Add the glaze notes for that mixture. Review the notes before your next pottery session."),
])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["finish", "landing_bad"])
@pytest.mark.parametrize("confirmed_field", ["offer", "workflow"])
def test_fake_page_follows_actual_brief_through_preview_and_export(tmp_path, monkeypatch, domain, name, idea, audience, workflow, provider, mode, confirmed_field):
    from tests.unit.pagebuilder.test_pipeline import customer_draft
    from app.pagebuilder import models, tool

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    data, draft = customer_draft(domain)
    value = "helps you record notes and review them together before making your next decision"
    summary = idea.split(".", 1)[0] + (" who keep notes in separate files and want to review them together before making their next decision."
        if confirmed_field == "workflow" else " that " + value + ".")
    framed = "You want " if domain == "consumer" else "You plan to build "
    inputs = models.PageInput.model_validate({**data, "name": name, "idea": "You plan to build " + idea + " Private planning note: do not claim unmatched performance.",
        "clarified_answers": {"audience": audience, "summary": framed + summary, confirmed_field: workflow if confirmed_field == "workflow" else idea}, "research": {"local_signup": {
            "mode": "setup", "url": "", "notice": "Connect your signup destination in Settings > Keys and connections."}}})
    binary = journey.install_fake_cli(tmp_path / "bin", provider)
    (binary.parent / "mode").write_text(mode)
    (binary.parent / "page-fixture.json").write_text(json.dumps({"root": str(journey.ROOT),
        "template": {"input": data, "draft": draft}}))
    workspace = tmp_path / "result"
    workspace.mkdir()
    prompt = tool.build_brief(inputs, None)
    command = [str(binary), "-p", prompt] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), prompt]
    result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
    if mode == "landing_bad" and result.returncode:
        assert "pagebuilder_" in result.stderr
        return
    assert result.returncode == 0, result.stderr
    files = {str(path.relative_to(workspace)): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    checked = tool.check_output(files, inputs, None)
    if mode == "landing_bad":
        assert not checked["passed"]
        return
    assert checked["passed"], checked["failures"]
    plan = tool.tool_page_plan(inputs, None, files)
    expected = "How it works" if confirmed_field == "workflow" else "What it does"
    assert plan.sections[2].headline == plan.sections[2].nav_label == expected
    assert plan.brief.promise == "Planned: " + summary
    assert plan.sections[0].headline == (workflow.split(". ")[-1].rstrip(".")
        if confirmed_field == "workflow" else "A concept that " + value)
    assert plan.brief.audience == audience
    assert "Private planning note" not in files["index.html"].decode()
    assert journey.MARKER in files["index.html"].decode()
    bundle = tool.bundle(files, inputs, plan, checked)
    assert base64.b64decode(bundle["files"]["index.html"]) == files["index.html"]
    with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["source_zip"]))) as archive:
        assert all(archive.read(path) == body for path, body in files.items())


def test_journey_uses_distinct_fixture_names_without_changing_either_domain(monkeypatch, tmp_path):
    from types import SimpleNamespace

    identifiers = iter([bytes(range(16)), bytes(range(16, 32))])
    monkeypatch.setattr(journey, "uuid4", lambda: SimpleNamespace(bytes=next(identifiers)))
    ideas = ["A parts inspection notebook for machine shop supervisors", "A pottery practice notebook for hobbyists"]
    created = []
    class Client:
        headers = {}
        state = "reviewing"

        def request(self, method, path, payload=None):
            if path == "/ventures":
                created.append(payload)
                return {"id": "venture", "workspace_id": "workspace"}
            if path.endswith("/idea/sessions"):
                self.state = "reviewing"
                return {"id": "session"}
            if path.endswith("/confirm"):
                self.state = "confirmed"
            return {"state": self.state, "turns": [{"kind": "question"}], "current_revision": {"id": "revision"}}

    build = journey.BuildJourney(Client(), None, tmp_path, tmp_path, 1)
    for idea in ideas:
        assert build.create(idea)
    assert [payload["raw_idea"] for payload in created] == ideas
    assert len({payload["name"] for payload in created}) == len(ideas)
    assert all(payload["name"].isalpha() and not payload["start"] for payload in created)


@pytest.mark.parametrize("idea", ["An inspection board for factory managers", "A weekend family garden planner"])
@pytest.mark.parametrize("changed_field", [None, "actor_id", "device_id", "workspace_id", "requested_build"])
def test_journey_compares_full_parent_and_rejects_changed_custody(tmp_path, monkeypatch, idea, changed_field):
    fake, data = tmp_path / "bin", tmp_path / "data"
    fake.mkdir()
    data.mkdir()
    (data / "index.html").write_text(journey.MARKER)
    (data / "README.md").write_text(idea)
    started = False
    requests = []
    custody = {"actor_id": "owner", "device_id": "device", "workspace_id": "workspace",
        "requested_build": {"request_sha256": "a" * 64, "caps": {"max_iterations": 2}}}
    summary = {"id": "plan", "manifest": {"coding_loop_verification": {
        "phase": "plan", "provider": "claude", "workspace_slug": "project"}}}
    parent = {**summary, **custody}
    child = {**custody, "id": "implementation", "manifest": {"coding_loop_verification": {
        "phase": "implement", "provider": "claude", "workspace_slug": "project", "plan_order_id": "plan"}}}
    if changed_field:
        child[changed_field] = "changed"

    class Client:
        def request(self, method, path, payload=None):
            nonlocal started
            requests.append(path)
            if path.endswith("/chat"):
                started = True
                return {"body_md": "Building " + idea}
            if path.endswith("/device-work-orders"):
                return {"orders": [summary] if started else []}
            if path.endswith("/start"):
                return {"state": "starting"}
            if path.endswith("/status"):
                planning = "/plan/" in path
                return {"state": "stopped", "task_verified": True, "evidence_digest": "digest", "events": [{"text": "Working"}],
                    "next_order_id": "implementation" if planning else None,
                    "evidence": {"processes_stopped": True, "outcome": "plan_ready" if planning else "pass"}}
            order = parent if path.endswith("/plan") else child
            return {**order, "state": "stopped", "closed_at": "2026-11-15T00:00:00Z", "native_stop": {
                "task_verified": True, "processes_stopped": True, "evidence_digest": "digest"}}

    monkeypatch.setattr(journey, "cli_runs", lambda _path: [{"provider": "claude", "workspace": str(data), "pid": 123}] if started else [])
    monkeypatch.setattr(journey, "process_alive", lambda _pid: False)
    report = journey.Report(tmp_path / "findings")
    check = journey.BuildJourney(Client(), report, data, fake, 0.01)
    check.venture, check.provider = "venture", "claude"
    check.build("product", idea)
    assert report.failures == (1 if changed_field else 0)
    assert "/ventures/venture/device-work-orders/plan" in requests


def test_journey_defaults_to_complete_page_fixtures_and_writes_each_result(tmp_path, monkeypatch):
    monkeypatch.setattr(journey, "ROOT", tmp_path)
    monkeypatch.setattr(journey, "load_environment", lambda _path: {})
    monkeypatch.setattr(journey, "provider_settings", lambda _values: {"provider": "azure_openai"})

    def run(args, values, report, output):
        assert args.page_fixtures == tmp_path / "tests/unit/pagebuilder/fixtures/local_tool_pages"
        report.step("factory page", lambda: True, "Retry your page.")
        report.step("family product", lambda: journey.require(False, "Your check failed"), "Retry your product.")

    monkeypatch.setattr(journey, "run", run)
    assert journey.main(["--idea", "A factory inspection board", "--idea", "A family garden planner",
        "--findings", str(tmp_path / "findings")]) == 1
    report = (tmp_path / "artifacts/buildjourney.txt").read_text()
    assert "PASS factory page" in report and "FAIL family product" in report
    assert "1 PASS / 1 FAIL" in report


