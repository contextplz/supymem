import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.onboarding.steps.build import landing_builder_prompt


@pytest.fixture(params=["Invoice reconciliation for freight teams", "Recipe sharing for families"])
def builder_scope(tmp_path, monkeypatch, request):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/builder.db")
    load_company_os_app()
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    owner, now = ensure_owner(), utcnow()
    with db()() as session:
        session.add(Workspace(id="wsp_t", owner_id=owner.id, kind="company", name="Your workspace",
                              entry_mode="idea", created_at=now, updated_at=now))
        session.add(Venture(id="ven_t", workspace_id="wsp_t", name="Your page", raw_idea=request.param,
                            stage="idea", created_at=now, updated_at=now))
        session.commit()


def test_default_builder_prompt_is_a_full_first_look(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    prompt = landing_builder_prompt(
        "LedgerLite",
        "Get paid without the spreadsheet",
        "invoicing for freelancers",
        {"understood": "simple invoices", "first_user": "a freelancer", "first_job": "send an invoice"},
        waitlist_action="/w/ws_test/interest",
    )
    assert "Join the waitlist" in prompt
    assert "/w/ws_test/interest" in prompt
    assert "email" in prompt
    assert "site_version" in prompt
    assert "how it works" in prompt.lower()
    assert "FAQ" in prompt
    assert "Do not invent testimonials" in prompt
    assert "index.html" in prompt
    assert "first-look" in prompt
    assert "simple invoices" in prompt
    assert "Keep going" in prompt
    assert "python3 -m http.server" in prompt
    assert "Local preview:" in prompt
    assert "never localhost" in prompt


def test_builder_prompt_without_brief_still_requires_waitlist():
    prompt = landing_builder_prompt("Acme", "", "neighborhood bakers")
    assert "Join the waitlist" in prompt
    assert "neighborhood bakers" in prompt


@pytest.mark.asyncio
async def test_builder_failed_is_returned_on_choice(monkeypatch, builder_scope):
    from app.api import builder as builder_api
    from app.api.builder import BuilderProgressBody
    from app.state import MEM

    monkeypatch.setattr(builder_api._service, "active_run", lambda _id: None)
    monkeypatch.setattr(builder_api._service, "resumable_run", lambda _id: None)
    MEM.pop("builder_failed:ven_t", None)
    out = await builder_api.fail_builder_session(
        "ven_t", BuilderProgressBody(text="Couldn't start Cursor.")
    )
    assert out["status"] == "failed"
    assert out["error"] == "Couldn't start Cursor."
    MEM.pop("builder_failed:ven_t", None)


@pytest.mark.asyncio
async def test_progress_wrote_clears_builder_failed(monkeypatch, builder_scope):
    from app.api import builder as builder_api
    from app.api.builder import BuilderProgressBody
    from app.state import MEM

    monkeypatch.setattr(builder_api._service, "active_run", lambda _id: None)
    monkeypatch.setattr(builder_api._service, "resumable_run", lambda _id: None)

    class FakeNarrator:
        def __init__(self, *a, **k):
            pass

        async def emit(self, **k):
            return None

    monkeypatch.setattr("app.onboarding.narrator.Narrator", FakeNarrator)
    MEM["builder_failed:ven_t"] = "The local builder did not start. Nothing has been written yet."
    out = await builder_api.builder_progress(
        "ven_t", BuilderProgressBody(text="Cursor wrote styles.css")
    )
    assert out["status"] == "ok"
    assert MEM.get("builder_failed:ven_t") is None


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_ingest_builder_page_writes_html_and_css(tmp_path, monkeypatch, builder_scope, mode):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    from types import SimpleNamespace

    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from app.firstlook import service as svc
    from app.state import MEM

    monkeypatch.setattr(svc.versions_mod, "next_version", lambda _w: 3)
    monkeypatch.setattr(svc.versions_mod, "get_latest", lambda _w, **kwargs: None)

    def fake_persist(**kwargs):
        return SimpleNamespace(
            id="site_1",
            workspace_id="wsp_t",
            version=3,
            stage="first_look",
            published=False,
            url=kwargs.get("url"),
            path=kwargs.get("path"),
        )

    monkeypatch.setattr(svc.versions_mod, "persist", fake_persist)
    MEM.pop("website:ven_t", None)
    html = '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body><h1>Studiohour</h1><p>Book a room.</p></body></html>'
    out = svc.ingest_builder_page("ven_t", html, {"styles.css": "body{margin:0}"})
    assert out["ok"] is True
    assert "wsp_t" in (out["url"] or "")
    written = tmp_path / "workspaces" / "wsp_t" / "site" / "v3"
    stored = (written / "index.html").read_text(encoding="utf-8")
    assert stored.startswith("<!doctype html>")
    if mode == "local":
        assert stored == html
    else:
        assert "data-supymem-inline=\"styles.css\"" in stored
        assert "margin:0" in stored
    assert "margin:0" in (written / "styles.css").read_text(encoding="utf-8")
    MEM.pop("website:ven_t", None)


@pytest.mark.parametrize("mode", ["local", "hosted"])
@pytest.mark.parametrize("name", ["Freight inventory", "Family recipes"])
def test_inline_stylesheets_replaces_link_tags(monkeypatch, mode, name):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.firstlook.serve import inline_stylesheets, version_stylesheet_hrefs

    html = '<link rel="stylesheet" href="styles.css"><h1>' + name + '</h1>'
    out = inline_stylesheets(html, {"styles.css": "body{color:red}"})
    if mode == "local":
        assert out == html
    else:
        assert "<style data-supymem-inline=\"styles.css\">" in out
        assert "body{color:red}" in out
        assert "href=" not in out
    versioned = version_stylesheet_hrefs(html, 8)
    if mode == "local":
        assert versioned == html
    else:
        assert 'href="styles.css?v=8"' in versioned


@pytest.mark.parametrize("mode", ["local", "hosted"])
def test_ingest_html_only_keeps_previous_css(tmp_path, monkeypatch, builder_scope, mode):
    monkeypatch.setenv("SUPYMEM_MODE", mode)
    from types import SimpleNamespace

    monkeypatch.setenv("FIRSTLOOK_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    from app.firstlook import service as svc
    from app.state import MEM

    monkeypatch.setattr(svc.versions_mod, "next_version", lambda _w: 4)
    monkeypatch.setattr(
        svc.versions_mod,
        "get_latest",
        lambda _w, **kwargs: SimpleNamespace(
            copy={"page_html": "<html>old</html>", "page_files": {"styles.css": "body{color:red}"}},
            version=3,
        ),
    )

    def fake_persist(**kwargs):
        fake_persist.copy = kwargs.get("copy")
        return SimpleNamespace(
            id="site_2",
            workspace_id="wsp_t",
            version=4,
            stage="first_look",
            published=False,
            url=kwargs.get("url"),
            path=kwargs.get("path"),
        )

    monkeypatch.setattr(svc.versions_mod, "persist", fake_persist)
    MEM.pop("website:ven_t", None)
    html = '<!doctype html><html><head><link rel="stylesheet" href="styles.css"></head><body><h1>New</h1><p>Keep the css.</p></body></html>'
    out = svc.ingest_builder_page("ven_t", html, {})
    assert out["ok"] is True
    stored = (tmp_path / "workspaces" / "wsp_t" / "site" / "v4" / "index.html").read_text(
        encoding="utf-8"
    )
    if mode == "local":
        assert fake_persist.copy["page_files"] == {}
        assert stored == html
        assert not (tmp_path / "workspaces" / "wsp_t" / "site" / "v4" / "styles.css").exists()
    else:
        assert fake_persist.copy["page_files"]["styles.css"] == "body{color:red}"
        assert "body{color:red}" in stored
    MEM.pop("website:ven_t", None)


@pytest.mark.asyncio
async def test_builder_done_is_idempotent(monkeypatch, builder_scope):
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    from types import SimpleNamespace

    from app.api import builder as builder_api
    from app.api.builder import BuilderDoneBody

    run = SimpleNamespace(
        id="run_1",
        status="paused",
        paused_reason="awaiting_builder_session",
        mode="guided",
        entry_mode="idea",
        workspace_id="wsp_t",
    )
    gates: dict[str, str] = {}
    monkeypatch.setattr(builder_api._service, "active_run", lambda _id: run)
    monkeypatch.setattr(builder_api._service, "resumable_run", lambda _id: run)
    monkeypatch.setattr(builder_api._service, "get_run_gate", lambda kind, *_a, **_k: gates.get(kind))

    def _set(kind, *_a, **_k):
        gates[kind] = "done"

    monkeypatch.setattr(builder_api._service, "set_run_gate", _set)
    monkeypatch.setattr(
        builder_api._service,
        "builder_choice_payload",
        lambda _vid: {
            "adapter": "cursor",
            "session": "done",
            "prompt": "p",
            "paused_reason": None,
            "error": None,
            "cloud_available": False,
        },
    )
    monkeypatch.setattr(
        "app.firstlook.service.ingest_builder_page",
        lambda *_a, **_k: {"ok": True, "url": "https://gateway.supymem.com/w/wsp_t/site/"},
    )
    started: list[bool] = []

    async def fake_start(*_a, **kw):
        started.append(bool(kw.get("background")))
        return run

    monkeypatch.setattr("app.onboarding.spawn.start_onboarding", fake_start)
    html = "<!doctype html><html><body><h1>Studiohour booking</h1></body></html>"
    first = await builder_api.finish_builder_session("ven_t", BuilderDoneBody(html=html))
    assert first["status"] == "resumed"
    assert started == [True]
    second = await builder_api.finish_builder_session("ven_t", BuilderDoneBody(ready=True))
    assert second["status"] == "accepted"
    assert started == [True]


@pytest.mark.asyncio
async def test_builder_page_chunks_ingest(monkeypatch, builder_scope):
    from app.api import builder as builder_api
    from app.api.builder import BuilderPageChunk
    from app.state import MEM

    captured: dict = {}

    def fake_ingest(_vid, html, extras):
        captured["html"] = html
        captured["extras"] = extras
        return {"ok": True, "url": "https://gateway.supymem.com/w/wsp_t/site/"}

    monkeypatch.setattr("app.firstlook.service.ingest_builder_page", fake_ingest)
    import base64

    html = "<!doctype html><html><body><h1>Studiohour</h1></body></html>"
    a, b = html[:20], html[20:]
    MEM.pop("page_parts:ven_t:index.html", None)
    MEM.pop("page_files:ven_t", None)
    first = await builder_api.receive_builder_page_chunk(
        "ven_t",
        BuilderPageChunk(name="index.html", index=0, total=2, b64=base64.b64encode(a.encode()).decode()),
    )
    assert first["status"] == "chunk"
    second = await builder_api.receive_builder_page_chunk(
        "ven_t",
        BuilderPageChunk(name="index.html", index=1, total=2, b64=base64.b64encode(b.encode()).decode()),
    )
    assert second["status"] == "ingested"
    assert captured["html"] == html
    MEM.pop("page_files:ven_t", None)


@pytest.fixture
def local_builder_route(builder_scope, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from app.onboarding.state import Ctx
    from app.onboarding.steps import build
    from app.state import MEM
    from app.core.local_owner import ensure_owner

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    owner = ensure_owner()
    monkeypatch.setattr("app.core.local_owner.current_owner", lambda: owner)
    monkeypatch.setattr("app.build.device_orders.retrieve", lambda venture_id, caller, order_id:
                        {"id": order_id, "venture_id": venture_id, "state": "approved"})
    run = SimpleNamespace(id="run_local", status="paused", paused_reason="awaiting_builder_session",
                          mode="guided", workspace_id="wsp_t", entry_mode="idea")
    gates = {}
    monkeypatch.setattr(build, "active_run", lambda _id: run)
    monkeypatch.setattr(build, "resumable_run", lambda _id: run)
    monkeypatch.setattr(build, "get_run_gate", lambda kind, *_args: gates.get(kind))
    monkeypatch.setattr(build, "set_run_gate", lambda kind, _vid, _rid, value: gates.update({kind: value}))
    monkeypatch.setattr("app.builder.ctl.local_tool", lambda _vid, **kwargs: kwargs.get("provider") or "codex")
    monkeypatch.setattr("app.chat.build_path.local_build_reply", lambda *_args: {"body_md": "Connect your tool.", "actions": []})
    for key in list(MEM):
        if key.endswith(":ven_t"):
            MEM.pop(key)
    venture = build.builder_venture("ven_t")
    return build, Ctx(venture=venture, run=run), gates


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["landing_page", "_landing_page_via_byo", "_landing_page_via_qm", "_stub_landing_files"])
async def test_local_onboarding_entrypoints_use_connected_tool(local_builder_route, tmp_path, monkeypatch, entrypoint):
    from app.builder import ctl

    build, ctx, gates = local_builder_route
    repo = tmp_path / "repo"
    repo.mkdir()
    original = "<!doctype html><html><body>Your existing page</body></html>"
    (repo / "index.html").write_text(original)
    requested = []

    def request(venture_id, instruction, **kwargs):
        requested.append((venture_id, instruction, kwargs))
        return {"id": "order_local", "state": "approved", "review_path": "/watch/order_local"}

    monkeypatch.setattr(ctl, "request_local_build", request)
    args = (ctx,) if entrypoint == "landing_page" else (ctx, repo, ctx.company, "Your first page")
    result = await getattr(build, entrypoint)(*args)
    assert len(requested) == 1
    venture_id, instruction, options = requested[0]
    assert venture_id == ctx.venture.id and "landing page" in instruction.lower()
    assert options["repair"] is True
    assert ctx.venture.raw_idea in options["brief"]
    assert "Join the waitlist" in options["brief"] and "Do not invent testimonials" in options["brief"]
    assert result.output["device_work_order_id"] == "order_local"
    assert result.pause.reason == "awaiting_builder_session" and result.partial
    assert gates["builder_device_order"] == "order_local"
    assert gates.get("builder_session") != "done"
    assert (repo / "index.html").read_text() == original
    assert sorted(path.name for path in repo.iterdir()) == ["index.html"]


@pytest.mark.asyncio
@pytest.mark.parametrize("adapter", ["claude", "codex", "cursor", "opencode"])
async def test_local_builder_choice_starts_device_order_not_legacy_executor(local_builder_route, monkeypatch, adapter):
    from app.api import builder as builder_api
    from app.builder import ctl

    requested = []
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs:
                        requested.append(kwargs) or {"id": "order_local", "state": "approved", "review_path": "/watch/order_local"})
    result = await builder_api.choose_builder("ven_t", builder_api.BuilderChoiceBody(adapter=adapter))
    assert result["status"] == "queued"
    assert result["device_work_order_id"] == "order_local"
    assert requested[0]["provider"] == adapter
    assert result["cloud_available"] is False


@pytest.mark.asyncio
async def test_local_builder_cloud_choice_never_uses_hosted_flags(local_builder_route, monkeypatch):
    from fastapi import HTTPException

    from app.api import builder as builder_api
    from app.builder import ctl

    monkeypatch.setenv("BUILDER_HOSTED", "1")
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("Cloud is not your connected tool"))
    with pytest.raises(HTTPException) as failure:
        await builder_api.choose_builder("ven_t", builder_api.BuilderChoiceBody(adapter="cloud"))
    assert failure.value.status_code == 409
    assert "Settings > Keys and connections > Coding tools" in failure.value.detail["message"]
    assert builder_api.get_builder_choice("ven_t")["cloud_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["landing_page", "_landing_page_via_byo", "_landing_page_via_qm", "_stub_landing_files"])
async def test_local_onboarding_no_tool_names_connection_step(local_builder_route, tmp_path, monkeypatch, entrypoint):
    from fastapi import HTTPException

    from app.builder import ctl

    build, ctx, gates = local_builder_route

    def disconnected(*args, **kwargs):
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": "Open Connections > Coding tools."})

    monkeypatch.setattr(ctl, "request_local_build", disconnected)
    args = (ctx,) if entrypoint == "landing_page" else (ctx, tmp_path, ctx.company, "Your first page")
    result = await getattr(build, entrypoint)(*args)
    assert "Settings > Keys and connections > Coding tools" in result.output["message"]
    assert result.pause.founder_action == "/connections"
    assert not result.output.get("device_work_order_id") and "builder_session" not in gates
    assert not (tmp_path / "index.html").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("verified", [False, True])
async def test_local_builder_ready_is_not_source_or_verified_completion(local_builder_route, monkeypatch, verified):
    from fastapi import HTTPException

    from app.api import builder as builder_api
    from app.build import device_orders
    from app.core import local_owner

    build, ctx, gates = local_builder_route
    gates["builder_device_order"] = "order_local"
    monkeypatch.setattr(local_owner, "current_owner", lambda: type("Owner", (), {"id": "owner", "email": ""})())
    order = {"id": "order_local", "venture_id": "ven_t", "state": "stopped", "execution_id": "exec_local", "closed_at": "now",
             "manifest": {"coding_verification": {"provider": "codex", "operation": "workspace_edit_v1"}},
             "native_stop": {"execution_id": "exec_local", "processes_stopped": True,
                             "task_verified": verified, "verification_kind": "workspace_edit_v1"}}
    monkeypatch.setattr(device_orders, "retrieve", lambda *args: order)
    monkeypatch.setattr("app.firstlook.service.ingest_builder_page", lambda *args: pytest.fail("There are no verified bytes"))
    with pytest.raises(HTTPException) as failure:
        await builder_api.finish_builder_session("ven_t", builder_api.BuilderDoneBody(ready=True))
    assert failure.value.status_code == 409
    assert "Builds" in failure.value.detail["message"]
    assert gates.get("builder_session") != "done"


@pytest.mark.asyncio
async def test_local_builder_ingest_failure_does_not_finish(local_builder_route, monkeypatch):
    from fastapi import HTTPException

    from app.api import builder as builder_api

    build, ctx, gates = local_builder_route
    monkeypatch.setattr(build, "_local_builder_order", lambda *args: {"id": "order_local"}, raising=False)
    monkeypatch.setattr(build, "_local_order_verified", lambda *args: True, raising=False)
    monkeypatch.setattr("app.firstlook.service.ingest_builder_page", lambda *args:
                        {"ok": False, "error": "page_ingest_failed"})
    html = "<!doctype html><html><body><h1>" + ctx.venture.raw_idea + "</h1></body></html>"
    with pytest.raises(HTTPException) as failure:
        await builder_api.finish_builder_session("ven_t", builder_api.BuilderDoneBody(html=html))
    assert failure.value.status_code == 409
    assert gates.get("builder_session") != "done"


@pytest.mark.asyncio
async def test_local_builder_verified_source_finishes_once(local_builder_route, monkeypatch):
    from app.api import builder as builder_api

    build, ctx, gates = local_builder_route
    order = {"id": "order_local", "venture_id": "ven_t", "review_path": "/watch/order_local",
             "state": "stopped", "execution_id": "exec_local", "closed_at": "now",
             "manifest": {"coding_verification": {"provider": "codex", "operation": "workspace_edit_v1"}},
             "native_stop": {"execution_id": "exec_local", "processes_stopped": True,
                             "task_verified": True, "verification_kind": "workspace_edit_v1"}}
    monkeypatch.setattr(build, "_local_builder_order", lambda *args: order, raising=False)
    ingested, resumed = [], []
    monkeypatch.setattr("app.firstlook.service.ingest_builder_page", lambda *args:
                        ingested.append(args) or {"ok": True})

    async def resume(*args, **kwargs):
        resumed.append(kwargs)

    monkeypatch.setattr("app.onboarding.spawn.start_onboarding", resume)
    html = "<!doctype html><html><body><h1>" + ctx.venture.raw_idea + "</h1></body></html>"
    first = await builder_api.finish_builder_session("ven_t", builder_api.BuilderDoneBody(html=html))
    second = await builder_api.finish_builder_session("ven_t", builder_api.BuilderDoneBody(ready=True))
    assert first["status"] == "resumed" and second["status"] == "accepted"
    assert gates["builder_choice"] == "codex" and gates["builder_session"] == "done"
    assert len(ingested) == len(resumed) == 1


@pytest.mark.asyncio
async def test_local_builder_reuses_order_without_relaunch(local_builder_route, monkeypatch):
    from app.builder import ctl

    build, ctx, gates = local_builder_route
    order = {"id": "order_local", "state": "approved", "review_path": "/watch/order_local"}
    monkeypatch.setattr(build, "_local_builder_order", lambda *args: order, raising=False)
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("Do not replay requested work"))
    result = await build.landing_page(ctx)
    assert result.output["device_work_order_id"] == "order_local"
    assert result.partial and gates.get("builder_session") != "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["landing_page", "_landing_page_via_byo", "_landing_page_via_qm", "_stub_landing_files"])
async def test_local_onboarding_cancellation_never_starts(local_builder_route, tmp_path, monkeypatch, entrypoint):
    from app.builder import ctl
    from app.onboarding.failures import StepError

    build, ctx, gates = local_builder_route
    ctx.cancel.set()
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs: pytest.fail("You cancelled this build"))
    args = (ctx,) if entrypoint == "landing_page" else (ctx, tmp_path, ctx.company, "Your first page")
    with pytest.raises(StepError):
        await getattr(build, entrypoint)(*args)
    assert not gates and not (tmp_path / "index.html").exists()


def test_local_builder_receipt_rejects_fake_or_uncertain_completion(local_builder_route):
    from copy import deepcopy

    build, ctx, gates = local_builder_route
    order = {"state": "stopped", "execution_id": "exec_local", "closed_at": "now",
             "manifest": {"coding_verification": {"provider": "codex", "operation": "workspace_edit_v1"}},
             "native_stop": {"execution_id": "exec_local", "processes_stopped": True,
                             "task_verified": True, "verification_kind": "workspace_edit_v1"}}
    assert build._local_order_verified(order)
    for field in ("cancelled_at", "stopped_by_user", "stop_reconciliation_required", "inference_reconciliation_required"):
        assert not build._local_order_verified({**order, field: True})
    fake = deepcopy(order)
    fake["manifest"]["coding_verification"]["provider"] = "stub"
    assert not build._local_order_verified(fake)
    fake = deepcopy(order)
    fake["native_stop"]["execution_id"] = "other_execution"
    assert not build._local_order_verified(fake)


@pytest.mark.asyncio
async def test_local_existing_page_cannot_complete_unverified_tool(local_builder_route, monkeypatch):
    build, ctx, gates = local_builder_route
    monkeypatch.setattr("app.firstlook.service.landing_exists", lambda *args: True)
    assert not await build.builder_complete_builder_because_page_exists(ctx.venture.id, ctx.run)
    assert gates.get("builder_session") != "done"


@pytest.mark.parametrize("state", ["approved", "stopped"])
def test_local_builder_cancel_forwards_only_live_order(local_builder_route, monkeypatch, state):
    from app.build import device_orders

    build, ctx, gates = local_builder_route
    order = {"id": "order_local", "venture_id": "ven_t", "state": state, "review_digest": "review_local"}
    monkeypatch.setattr(build, "_local_builder_order", lambda *args: order, raising=False)
    cancelled = []
    monkeypatch.setattr(device_orders, "transition", lambda *args: cancelled.append(args) or {**order, "state": "cancelled"})
    build.builder_cancel_device_order(ctx.venture.id, ctx.run)
    assert len(cancelled) == (1 if state == "approved" else 0)
    if cancelled:
        assert cancelled[0][0] == ctx.venture.id and cancelled[0][-1] == "cancel"
        assert cancelled[0][2:4] == ("order_local", "review_local")
    assert gates.get("builder_session") != "done"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["name", "encoding", "save"])
async def test_local_builder_chunk_failures_name_import_step(local_builder_route, monkeypatch, failure):
    import base64
    from fastapi import HTTPException

    from app.api import builder as builder_api

    build, ctx, gates = local_builder_route
    html = "<!doctype html><html><body><h1>" + ctx.venture.raw_idea + "</h1></body></html>"
    chunk = builder_api.BuilderPageChunk(name="../index.html" if failure == "name" else "index.html",
                                        index=0, total=1, b64="!invalid!" if failure == "encoding" else
                                        base64.b64encode(html.encode()).decode())

    def save_failed(*args):
        raise OSError("Your page could not be written")

    monkeypatch.setattr("app.firstlook.service.ingest_builder_page", save_failed)
    if failure == "save":
        result = await builder_api.receive_builder_page_chunk(ctx.venture.id, chunk)
        assert result["status"] == "failed" and result["site"]["ok"] is False
        message = result["site"]["message"]
    else:
        with pytest.raises(HTTPException) as caught:
            await builder_api.receive_builder_page_chunk(ctx.venture.id, chunk)
        assert caught.value.status_code == 400
        assert isinstance(caught.value.detail, dict)
        message = caught.value.detail["message"]
    assert "Builds" in message and "import" in message.lower()
    assert gates.get("builder_session") != "done"


def test_local_builder_brief_allows_its_saved_loopback_destination(local_builder_route):
    build, ctx, gates = local_builder_route
    prompt = build.builder_prompt_for(ctx.venture)
    assert "http://127.0.0.1:" in prompt
    assert "never localhost" not in prompt
    assert "never invent a replacement" in prompt


@pytest.mark.asyncio
async def test_local_new_order_clears_legacy_done_before_using_old_page(local_builder_route, monkeypatch):
    from app.builder import ctl

    build, ctx, gates = local_builder_route
    gates["builder_session"] = "done"
    requested = []
    closed = {"id": "order_local", "review_path": "/watch/order_local", "state": "stopped",
              "execution_id": "exec_local", "closed_at": "now",
              "manifest": {"coding_verification": {"provider": "codex", "operation": "workspace_edit_v1"}},
              "native_stop": {"execution_id": "exec_local", "processes_stopped": True,
                              "task_verified": True, "verification_kind": "workspace_edit_v1"}}
    monkeypatch.setattr(build, "_local_builder_order", lambda *args: closed if requested else None)
    monkeypatch.setattr(ctl, "request_local_build", lambda *args, **kwargs:
                        requested.append(True) or {**closed, "state": "approved"})
    monkeypatch.setattr("app.firstlook.service.landing_snapshot", lambda *args, **kwargs:
                        pytest.fail("Your old page cannot complete this new build"))
    first = await build.landing_page(ctx)
    assert first.partial and gates["builder_session"] != "done"
    second = await build.landing_page(ctx)
    assert second.partial and second.pause is not None
