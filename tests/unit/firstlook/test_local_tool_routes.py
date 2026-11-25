import hashlib
import json
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.fixture
def route(monkeypatch, tmp_path):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/route.db")
    load_company_os_app()
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture, Workspace
    from app.state import db
    from core.clock import utcnow

    owner, now = ensure_owner(), utcnow()
    with db()() as session:
        session.add(Workspace(id="workspace", owner_id=owner.id, kind="company", name="Saved workspace",
            entry_mode="idea", created_at=now, updated_at=now))
        session.add(Venture(id="venture", workspace_id="workspace", name="Saved project", raw_idea="Saved idea",
            stage="idea", created_at=now, updated_at=now))
        session.commit()
    from app.build import device_orders
    from app.firstlook import service
    from app import coding_agents_consent
    from app.core import local_capabilities
    from app.tenancy import bind
    from desktop_runtime import agent_status

    source = '<!doctype html><html><body><h1>Saved offer</h1><form action="/join"><input name="email"><button>Join</button></form></body></html>'
    path = tmp_path / "index.html"
    path.write_text(source)
    latest = SimpleNamespace(id="saved-page", workspace_id="workspace", venture_id="venture",
        path=str(path), version=2, sha256=hashlib.sha256(source.encode()).hexdigest(),
        copy={"headline": "Saved offer", "cta": {"label": "Join"}, "page_html": source},
        brandkit={"wordmark": "Saved project"}, archetype="landing", variant_index=1,
        stage="first_look", url="http://127.0.0.1/saved", published=False)
    orders = []

    def prepare(venture_id, caller, instruction):
        orders.append((venture_id, caller, instruction))
        return {"id": "order", "state": "pending", "review_digest": "digest", "manifest": {"instruction": instruction}}

    def approve(venture_id, caller, order_id, digest, action):
        assert (venture_id, order_id, digest, action) == ("venture", "order", "digest", "approve")
        return {"id": "order", "state": "approved", "review_digest": digest}

    def forbidden(*args, **kwargs):
        pytest.fail("Local building reached an internal generator or wrote a page")

    monkeypatch.setattr(service.versions_mod, "validate_binding", lambda *args: None)
    monkeypatch.setattr(service.versions_mod, "get_latest", lambda *args, **kwargs: latest)
    monkeypatch.setattr(service.versions_mod, "persist", forbidden)
    monkeypatch.setattr(service, "generate_copy", forbidden)
    monkeypatch.setattr(service, "compose_html", forbidden)
    monkeypatch.setattr(service, "write_site", forbidden)
    monkeypatch.setattr(service, "plan_brief", lambda *args: {"name": "", "text": "", "customer": ""})
    monkeypatch.setattr(service, "_workspace_and_idea", lambda *args: ("workspace", "Saved idea", "Saved project"))
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(device_orders, "transition", approve)
    monkeypatch.setattr(local_capabilities, "enabled", lambda *args: True)
    monkeypatch.setattr(local_capabilities, "capability", lambda name: {
        "name": name, "title": "Landing pages", "state": "ready", "missing": [], "next_step": ""})
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])
    with bind(workspace_id="workspace", venture_id="venture", actor="owner"):
        yield SimpleNamespace(service=service, latest=latest, orders=orders, path=path,
            source=source, monkeypatch=monkeypatch, agent_status=agent_status)


@pytest.mark.parametrize("idea,audience", [
    ("Track industrial refrigeration maintenance", "warehouse operators"),
    ("Plan pottery workshops near home", "weekend hobbyists"),
])
@pytest.mark.parametrize("entry", ["generate", "literal_edit", "founder_copy", "charter", "another_look", "direct_code"])
@pytest.mark.parametrize("connected", [True, False])
async def test_local_entry_uses_connected_order_or_connect_step(route, idea, audience, entry, connected):
    service = route.service
    if not connected:
        route.monkeypatch.setattr(route.agent_status, "statuses", lambda: [])
    if entry == "generate":
        route.monkeypatch.setattr(service.versions_mod, "get_latest", lambda *args, **kwargs: None)
        call = service.generate_first_look("workspace", idea, venture_id="venture", audience=audience)
    elif entry == "literal_edit":
        call = service.generate_first_look("workspace", idea, venture_id="venture", audience=audience,
            edit_source=route.latest, edit_instruction='Change the headline to "Your next step"')
    elif entry == "founder_copy":
        route.monkeypatch.setattr(service, "_workspace_and_idea", lambda *args: ("workspace", idea, "Saved project"))
        call = service.apply_founder_copy("venture", headline="Your next step", brief=audience,
            waitlist_only=True, competitors=["Alternative offer"])
    elif entry == "charter":
        call = service.refine_from_charter("workspace", idea,
            {"content": {"one_liner": idea, "target_customer": audience}}, venture_id="venture")
    elif entry == "another_look":
        call = service.try_another_look("workspace", idea, venture_id="venture")
    else:
        from app.firstlook.compose import try_code_html
        call = try_code_html(brand={}, copy={"audience": audience}, idea_text=idea,
            version=3, stage="first_look", interest_action="/join", variant_index=0,
            archetype="landing", logo_url=None)
    if connected:
        result = await call
        assert result["state"] == "approved" and result["id"] == "order"
        assert len(route.orders) == 1
        venture_id, caller, instruction = route.orders[0]
        assert venture_id == "venture" and caller.sub == "owner"
        assert "codex" in instruction.splitlines()[0]
        assert idea in instruction
        if entry not in {"generate", "direct_code"}:
            assert route.latest.sha256 in instruction and route.source in json.loads(instruction.split("\n", 1)[1])["source"]["files"]["index.html"]
    else:
        with pytest.raises(service.FirstLookError) as raised:
            await call
        assert "Settings > Keys and connections > Coding tools" in raised.value.message
        assert route.orders == []
    assert route.path.read_text() == route.source


@pytest.mark.parametrize("failure", ["scope", "changed", "missing", "digest"])
@pytest.mark.parametrize("idea", ["Track industrial refrigeration maintenance", "Plan pottery workshops near home"])
async def test_local_edit_rejects_unverified_previous_page(route, failure, idea):
    if failure == "scope":
        route.latest.venture_id = "another-venture"
    elif failure == "changed":
        route.monkeypatch.setattr(route.service.versions_mod, "get_latest",
            lambda *args, **kwargs: SimpleNamespace(**{**vars(route.latest), "id": "newer-page"}))
    elif failure == "missing":
        route.path.unlink()
    else:
        route.path.write_text("Changed outside the saved version")
    with pytest.raises(route.service.FirstLookError):
        await route.service.generate_first_look("workspace", idea,
            venture_id="venture", edit_source=route.latest, edit_instruction="Edit the headline")
    assert route.orders == []


@pytest.mark.parametrize("idea", ["Track industrial refrigeration maintenance", "Plan pottery workshops near home"])
def test_local_ingestion_and_restore_keep_tool_source_bytes(route, idea):
    from app.firstlook import serve
    from app.product_analytics.beacon import _app_for_venture
    body = '\r\n<!doctype html><html><head><link rel="stylesheet" href="style.css"></head><body><h1>' + idea + '</h1><form action="mailto:owner@example.test"><input name="email"><button>Join</button></form></body></html>\r\n'
    route.monkeypatch.setattr(route.service.versions_mod, "next_version", lambda *args: 3)
    saved = []
    route.monkeypatch.setattr(route.service, "write_site", lambda html, **kwargs: (saved.append(html) or route.path, "http://127.0.0.1/site"))
    route.monkeypatch.setattr(route.service.versions_mod, "persist", lambda **kwargs:
        SimpleNamespace(**{**vars(route.latest), **{key: value for key, value in kwargs.items() if key in vars(route.latest)}, "version": 3}))
    assert route.service.ingest_builder_page("venture", body, {"style.css": "body{color:black}"})["ok"]
    assert _app_for_venture("venture") is not None
    assert saved == [body]
    assert serve.inline_stylesheets(body, {"style.css": "body{color:black}"}) == body
    assert serve.version_stylesheet_hrefs(body, 3) == body
    route.monkeypatch.setattr(route.service, "site_dir", lambda **kwargs: route.path.parent)
    route.path.write_text(body)
    site = {"workspace_id": "workspace", "venture_id": "venture", "version": 3,
        "path": str(route.path), "sha256": hashlib.sha256(body.encode()).hexdigest(),
        "copy": {"page_html": body, "page_files": {"style.css": "body{color:black}"}}}
    assert route.service.materialize_site_files(site).read_bytes() == body.encode()
    route.monkeypatch.setattr(serve, "_commit_site", lambda *args, **kwargs: None)
    written, _ = serve.write_site(body, workspace_id="workspace", version=4,
        extras={"style.css": "body {\r\n color: black;\r\n}"})
    assert written.read_bytes() == body.encode()
    assert (written.parent / "style.css").read_bytes() == b"body {\r\n color: black;\r\n}"


@pytest.mark.parametrize("idea", ["Track industrial refrigeration maintenance", "Plan pottery workshops near home"])
async def test_local_logo_edit_accepts_the_bound_api_snapshot(route, idea):
    await route.service.generate_first_look("workspace", idea,
        venture_id="venture", edit_source=vars(route.latest), reuse_copy=route.latest.copy,
        edit_instruction="Replace only the logo with the uploaded logo.")
    assert len(route.orders) == 1 and "Replace only the logo" in route.orders[0][2]
    assert route.path.read_text() == route.source


@pytest.mark.parametrize("idea", ["Track industrial refrigeration maintenance", "Plan pottery workshops near home"])
@pytest.mark.parametrize("outcome", ["form_changed", "unchanged", "applied"])
def test_local_tool_output_keeps_edit_checks(route, idea, outcome):
    candidate = route.source.replace("Saved offer", idea)
    if outcome == "form_changed":
        candidate = candidate.replace('action="/join"', 'action="/other"')
    elif outcome == "unchanged":
        candidate = route.source
    saved = []
    route.monkeypatch.setattr(route.service.versions_mod, "next_version", lambda *args: 3)
    route.monkeypatch.setattr(route.service, "write_site", lambda html, **kwargs:
        (saved.append(html) or route.path, "http://127.0.0.1/site"))
    route.monkeypatch.setattr(route.service.versions_mod, "persist", lambda **kwargs:
        SimpleNamespace(**{**vars(route.latest), **{key: value for key, value in kwargs.items() if key in vars(route.latest)}, "version": 3}))
    result = route.service.ingest_builder_page("venture", candidate,
        edit_source_id=route.latest.id, edit_instruction='Change the headline to "' + idea + '"')
    assert result["ok"] is (outcome == "applied")
    if not result["ok"]:
        assert "previous page stays saved" in result["error"] and saved == []
    else:
        assert saved == [candidate]
    assert route.path.read_text() == route.source


@pytest.mark.parametrize("idea", ["Track industrial refrigeration maintenance", "Plan pottery workshops near home"])
async def test_local_page_switch_off_never_starts_a_tool(route, idea):
    from app.core import local_capabilities
    route.monkeypatch.setattr(local_capabilities, "capability", lambda name: {
        "name": name, "title": "Landing pages", "state": "off", "missing": [],
        "next_step": "Turn on Landing pages in Settings > Capabilities."})
    with pytest.raises(route.service.FirstLookError, match="Settings > Capabilities"):
        await route.service.generate_first_look("workspace", idea, venture_id="venture")
    assert route.orders == []


