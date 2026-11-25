"""Retirement holds at real entry paths while saved founder work continues."""
import asyncio
import json
from types import SimpleNamespace

import pytest

from tests.integration.test_firstlook_api import client as base_client


@pytest.fixture
def client(base_client):
    from app.core.local_mode import is_local

    if is_local():
        base_client.headers["Origin"] = str(base_client.base_url).rstrip("/")
    return base_client


def _venture(client):
    response = client.post("/ventures", json={"raw_idea": "A bakery order board"})
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.hosted
@pytest.mark.parametrize("idea", ["Schedule warehouse equipment maintenance", "Plan weekend birdwatching walks"])
def test_headline_rewrite_preserves_the_unrequested_form(client, monkeypatch, idea):
    from pathlib import Path

    from app.firstlook import service
    from app.pagebuilder.qa import forms_preserved

    from tests.firstlook_legacy import seed_legacy_page

    created = client.post("/ventures", json={"raw_idea": idea})
    assert created.status_code == 201, created.text
    venture = created.json()
    seed_legacy_page(venture["workspace_id"], venture["id"])
    copy = {"headline": idea, "subhead": "Keep your next step visible.", "points": [],
            "cta": {"label": "Join the waitlist", "kind": "waitlist"}}
    first = asyncio.run(service.generate_first_look(venture["workspace_id"], idea,
        venture_id=venture["id"], reuse_copy=copy, allow_model=False))
    revised = {**copy, "headline": "Your next step is easy to find.",
               "cta": {"label": "Sign up", "kind": "waitlist"}}

    async def generate(*args, **kwargs):
        return revised, 0.0, False

    monkeypatch.setattr(service, "generate_copy", generate)
    changed = asyncio.run(service.generate_first_look(venture["workspace_id"], idea,
        venture_id=venture["id"], allow_model=False, edit_source=first,
        instruction="Make the headline precise", edit_instruction="Make the headline precise"))
    assert changed.copy["headline"] == revised["headline"]
    assert changed.copy["cta"] == copy["cta"] and not changed.published
    assert forms_preserved(Path(first.path).read_bytes(), Path(changed.path).read_bytes())


@pytest.mark.hosted
@pytest.mark.parametrize("idea", ["Monitor refrigerated freight", "Organize community pottery workshops"])
def test_another_look_changes_brand_without_regenerating_copy(client, monkeypatch, idea):
    from app.firstlook import service

    from tests.firstlook_legacy import seed_legacy_page

    created = client.post("/ventures", json={"raw_idea": idea})
    assert created.status_code == 201, created.text
    venture = created.json()
    seed_legacy_page(venture["workspace_id"], venture["id"])
    first = asyncio.run(service.generate_first_look(venture["workspace_id"], idea,
        venture_id=venture["id"], allow_model=False))

    async def forbidden(*args, **kwargs):
        pytest.fail("A visual variation must not regenerate copy or call a coding agent")

    monkeypatch.setattr(service, "generate_copy", forbidden)
    monkeypatch.setattr(service, "try_code_html", forbidden)
    changed = asyncio.run(service.try_another_look(venture["workspace_id"], idea, venture_id=venture["id"]))
    assert changed.variant_index == first.variant_index + 1
    assert changed.brandkit_hash != first.brandkit_hash and changed.sha256 != first.sha256
    assert changed.copy_hash == first.copy_hash and changed.cost_usd == 0.0
    assert not changed.published


@pytest.mark.hosted
@pytest.mark.parametrize("idea", ["Route planning for delivery dispatchers", "A garden journal for balcony growers"])
def test_first_logo_refresh_never_reports_success_with_the_generated_mark(client, idea):
    import base64
    from pathlib import Path

    from app.firstlook import service, versions

    from tests.firstlook_legacy import seed_legacy_page

    venture = client.post("/ventures", json={"raw_idea": idea}).json()
    wid, vid = venture["workspace_id"], venture["id"]
    seed_legacy_page(wid, vid)
    original = asyncio.run(service.generate_first_look(wid, idea, venture_id=vid, allow_model=False))
    before = Path(original.path).read_bytes()
    logo = b'<svg xmlns="http://www.w3.org/2000/svg"><circle cx="12" cy="12" r="8"/></svg>'
    response = client.post(f"/ventures/{vid}/brand/logo", files={"file": ("new.svg", logo, "image/svg+xml")})
    assert response.status_code == 200, response.text
    saved = versions.get_latest(wid, venture_id=vid)
    preview = client.get(f"/workspaces/{wid}/site/preview", params={"venture_id": vid})
    assert preview.status_code == 200, preview.text
    refreshed = base64.b64encode(logo).decode() in preview.json()["html"]
    assert response.json()["site_refreshed"] is refreshed
    assert Path(original.path).read_bytes() == before
    if not refreshed:
        assert saved.id == original.id


@pytest.mark.hosted
@pytest.mark.parametrize("ideas", [
    ("Track laboratory equipment calibration", "Discover beginner hiking trails"),
    ("Discover beginner hiking trails", "Track laboratory equipment calibration"),
])
def test_logo_upload_keeps_each_ventures_copy_and_artwork(client, monkeypatch, ideas):
    import base64

    from app.db.models import Venture
    from app.firstlook import brand_assets, service
    from app.state import db

    from tests.firstlook_legacy import seed_legacy_page

    ventures = []
    originals = []
    old_logos = [b'<svg xmlns="http://www.w3.org/2000/svg"><ellipse rx="7" ry="3"/></svg>',
                 b'<svg xmlns="http://www.w3.org/2000/svg"><path d="M0 0 L7 9"/></svg>']
    for idea in ideas:
        created = client.post("/ventures", json={"raw_idea": idea})
        assert created.status_code == 201, created.text
        venture = created.json()
        if ventures:
            venture["workspace_id"] = ventures[0]["workspace_id"]
            with db()() as session:
                session.get(Venture, venture["id"]).workspace_id = venture["workspace_id"]
                session.commit()
        ventures.append(venture)
        seed_legacy_page(venture["workspace_id"], venture["id"])
        legacy = brand_assets.save_logo(venture["workspace_id"], old_logos[len(ventures) - 1], "old.svg")
        with monkeypatch.context() as previous_storage:
            previous_storage.setattr(brand_assets, "custom_logo_path", lambda *args, legacy=legacy: legacy)
            original = asyncio.run(service.generate_first_look(venture["workspace_id"], idea,
                venture_id=venture["id"], allow_model=False))
            originals.append(asyncio.run(service.generate_first_look(venture["workspace_id"], idea,
                venture_id=venture["id"], allow_model=False, reuse_copy={**original.copy, "headline": idea})))
    assert ventures[0]["workspace_id"] == ventures[1]["workspace_id"]
    logos = [b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="9"/></svg>',
             b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="18" height="18"/></svg>']
    observed = []
    for venture, logo in zip(ventures, logos):
        upload = client.post(f'/ventures/{venture["id"]}/brand/logo',
            files={"file": ("mark.svg", logo, "image/svg+xml")})
        assert upload.status_code == 200, upload.text
        observed.append(service.current_site(venture["workspace_id"], venture_id=venture["id"])["copy"]["headline"])
    first = ventures[0]
    preview = client.get(f'/workspaces/{first["workspace_id"]}/site/preview', params={"venture_id": first["id"]})
    assert preview.status_code == 200, preview.text
    assert (observed, base64.b64encode(logos[0]).decode() in preview.json()["html"],
            base64.b64encode(logos[1]).decode() in preview.json()["html"]) == (list(ideas), True, False)
    assert legacy.read_bytes() == old_logos[-1]
    for venture, original, old_logo in zip(ventures, originals, old_logos):
        saved = client.get(f'/workspaces/{venture["workspace_id"]}/site/preview',
            params={"venture_id": venture["id"], "v": original.version})
        assert saved.status_code == 200 and base64.b64encode(old_logo).decode() in saved.json()["html"]


def test_charter_lock_and_resume_never_start_new_template_work(client, monkeypatch):
    from app.db.models import Charter, SiteVersion, Venture
    from app.firstlook import service
    from app.onboarding.steps.commit import lock_charter_step
    from app.state import db

    venture = _venture(client)
    calls = []

    async def forbidden_copy(*args, **kwargs):
        calls.append(kwargs)
        raise AssertionError("Retired generation reached the model boundary")

    monkeypatch.setattr(service, "generate_copy", forbidden_copy)
    ctx = SimpleNamespace(venture=SimpleNamespace(id=venture["id"], workspace_id=venture["workspace_id"],
        raw_idea="A bakery order board"), run=SimpleNamespace(workspace_id=venture["workspace_id"]),
        company="Bakery", out={}, narrator=None)
    first = asyncio.run(lock_charter_step(ctx))
    resumed = asyncio.run(lock_charter_step(ctx))
    assert resumed.output["charter_id"] == first.output["charter_id"]
    assert calls == []
    with db()() as session:
        row = session.get(Venture, venture["id"])
        assert row.active_charter_id == first.output["charter_id"]
        assert session.get(Charter, row.active_charter_id).locked == 1
        assert session.query(SiteVersion).count() == 0


def test_logo_upload_does_not_create_a_template(client, monkeypatch):
    from app.db.models import SiteVersion
    from app.firstlook import brand_assets, service
    from app.state import db

    venture = _venture(client)
    saved, calls = [], []
    monkeypatch.setattr(brand_assets, "save_logo", lambda *args: saved.append(args))

    async def forbidden_copy(*args, **kwargs):
        calls.append(kwargs)
        raise service.FirstLookError("test_forbidden", "No new page")

    monkeypatch.setattr(service, "generate_copy", forbidden_copy)
    response = client.post(f'/ventures/{venture["id"]}/brand/logo',
        files={"file": ("logo.png", b"synthetic-logo", "image/png")})
    assert response.status_code == 200, response.text
    assert saved and not response.json()["site_refreshed"]
    assert calls == []
    with db()() as session:
        assert session.query(SiteVersion).count() == 0


@pytest.mark.hosted
def test_existing_published_page_can_be_rewritten_and_refresh_its_logo(client, monkeypatch):
    from app.db.models import SiteVersion
    from app.firstlook import brand_assets, service
    from app.state import db
    from tests.firstlook_legacy import seed_legacy_page

    venture = _venture(client)
    wid, vid = venture["workspace_id"], venture["id"]
    seed_legacy_page(wid, vid)
    original = asyncio.run(service.generate_first_look(wid, "A bakery order board", venture_id=vid, allow_model=False))
    with db()() as session:
        session.get(SiteVersion, original.id).published = 1
        session.commit()
    rewrite = client.post(f"/workspaces/{wid}/firstlook", json={"venture_id": vid, "idea_text": "A bakery preorder board"})
    assert rewrite.status_code == 201, rewrite.text
    monkeypatch.setattr(brand_assets, "save_logo", lambda *args: None)
    logo = client.post(f"/ventures/{vid}/brand/logo", files={"file": ("logo.png", b"synthetic-logo", "image/png")})
    assert logo.status_code == 200 and logo.json()["site_refreshed"], logo.text
    with db()() as session:
        old = session.get(SiteVersion, original.id)
        assert old.published == 1 and old.sha256 == original.sha256
    assert service.current_site(wid, venture_id=vid)["version"] == original.version + 2


def test_product_acceptance_is_not_a_landing_publication_receipt(client, monkeypatch):
    from app.db.supymem_models import BuildRun, FounderApp
    from app.state import db
    from app.ventures import next_step, state_brief
    from core.clock import utcnow

    venture = _venture(client)
    now = utcnow()
    with db()() as session:
        session.add(FounderApp(id="app_profile", workspace_id=venture["workspace_id"], venture_id=venture["id"],
            slug="accepted-product", name="Product", kind="web", created_at=now, updated_at=now))
        session.flush()
        session.add(BuildRun(id="product_run", app_id="app_profile", workspace_id=venture["workspace_id"],
            venture_id=venture["id"], trigger="test", agent="test", runtime_version=1, state="succeeded",
            closed_at=now, spec=json.dumps({"artifact_profile": "founder_product_v1", "qa_state": "passed",
                "lifecycle": {"review": {"status": "accepted"}}})))
        session.commit()
    monkeypatch.setattr(state_brief, "_website_section", lambda *args: {"live": False, "waitlist_enabled": True})
    facts = {**next_step._facts(venture["id"]), "plan_state": "approved", "coding_tool": True}
    assert facts["accepted_product"] and facts["accepted_build"]
    assert next_step._page_step(venture["id"], f'/v/{venture["id"]}', facts)["key"] == "site.build"
    with db()() as session:
        session.add(BuildRun(id="landing_run", app_id="app_profile", workspace_id=venture["workspace_id"],
            venture_id=venture["id"], trigger="test", agent="test", runtime_version=1, state="succeeded",
            closed_at=now, spec=json.dumps({"artifact_profile": "static_landing_v1", "qa_state": "passed",
                "lifecycle": {"review": {"status": "accepted"}}})))
        session.commit()
    facts = next_step._facts(venture["id"])
    assert next_step._page_step(venture["id"], f'/v/{venture["id"]}', facts)["key"] == "site.publish"


@pytest.fixture
def local_entry(monkeypatch):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")
    load_company_os_app()
    from app.api import firstlook_api
    from app.build import device_orders
    from app.firstlook import brand_assets, service, steps
    from app.org import heartbeat

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    calls, writes = [], []
    source = {"id": "saved_page", "version": 2, "stage": "refined", "archetype": "generic",
              "copy": {"headline": "Your saved offer"}, "path": "saved/index.html"}
    order = {"id": "coding_order", "venture_id": "venture", "state": "approved",
             "review_path": "/v/venture/device-work-orders/coding_order"}
    connected = [True]

    def prepare(venture_id, caller, brief, **kwargs):
        calls.append((venture_id, brief))
        return {**order, "manifest": {"instruction": brief}}

    async def admit(workspace_id, idea, **kwargs):
        if not connected[0]:
            raise service.FirstLookError("page_coding_tool_required",
                "Connect your coding tool in Settings > Keys and connections > Coding tools, then retry your page.")
        if kwargs.get("edit_source") is not None:
            assert kwargs["edit_source"].id == source["id"]
            kwargs["edit_source"] = vars(kwargs["edit_source"])
        return device_orders.prepare_coding_task_from_chat(kwargs["venture_id"], None,
            json.dumps({"idea": idea, **kwargs}))

    def forbidden(*args, **kwargs):
        pytest.fail("Local entry reached an internal generator or the retired-template guard")

    monkeypatch.setattr(firstlook_api, "_assert_site_writer", lambda *args: None)
    monkeypatch.setattr(service, "assert_existing_page", forbidden)
    monkeypatch.setattr(service, "current_site", lambda *args, **kwargs: source)
    monkeypatch.setattr(service.versions_mod, "get_latest", lambda *args, **kwargs: SimpleNamespace(**source))
    monkeypatch.setattr(service, "brand_logo_context", lambda *args: ("workspace", "", "Your venture"))
    monkeypatch.setattr(service, "generate_first_look", admit)
    monkeypatch.setattr(service, "try_another_look", admit)
    monkeypatch.setattr(service, "compose_html", forbidden)
    monkeypatch.setattr(service, "try_code_html", forbidden)
    monkeypatch.setattr(brand_assets, "save_logo", lambda *args: writes.append(args))
    monkeypatch.setattr(brand_assets, "custom_logo_url", lambda *args: "/your-logo.svg")
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(steps, "MEM", {})
    monkeypatch.setattr(heartbeat, "mark_working", lambda *args: None)
    monkeypatch.setattr(heartbeat, "mark_idle", lambda *args: None)
    return SimpleNamespace(api=firstlook_api, service=service, steps=steps, connected=connected,
                           calls=calls, writes=writes, source=source, order=order, admit=admit)


_DOMAINS = [("Track fleet maintenance", "Fleet managers"),
            ("Plan family nature walks", "Parents")]


def _entry_result(call, entry, connected):
    from fastapi import HTTPException

    if connected:
        result = asyncio.run(call())
        assert result["id"] == entry.order["id"] and result["state"] == "approved"
        assert len(entry.calls) == 1
        return result
    with pytest.raises(HTTPException) as failure:
        asyncio.run(call())
    assert failure.value.status_code == 409
    assert "Settings > Keys and connections > Coding tools" in str(failure.value.detail)
    assert entry.calls == []


@pytest.mark.parametrize("idea,audience", _DOMAINS)
@pytest.mark.parametrize("connected", [True, False])
def test_local_generate_routes_the_brief_to_your_tool(local_entry, idea, audience, connected):
    entry = local_entry
    entry.connected[0] = connected
    body = entry.api.GenerateBody(idea_text=idea, audience=audience, venture_id="venture")
    _entry_result(lambda: entry.api.generate_site("workspace", body, "owner"), entry, connected)
    if connected:
        assert idea in entry.calls[0][1] and audience in entry.calls[0][1]


@pytest.mark.parametrize("idea,audience", _DOMAINS)
@pytest.mark.parametrize("connected", [True, False])
def test_local_another_look_routes_without_replacing_the_saved_page(local_entry, idea, audience, connected):
    entry = local_entry
    entry.connected[0] = connected
    before = json.dumps(entry.source)
    body = entry.api.TryAnother(idea_text=idea, venture_id="venture")
    _entry_result(lambda: entry.api.try_another("workspace", body, "owner"), entry, connected)
    assert json.dumps(entry.source) == before
    if connected:
        assert idea in entry.calls[0][1]


@pytest.mark.parametrize("idea,audience", _DOMAINS)
@pytest.mark.parametrize("connected", [True, False])
def test_local_logo_upload_requests_only_a_logo_edit(local_entry, idea, audience, connected, monkeypatch):
    entry = local_entry
    entry.connected[0] = connected
    monkeypatch.setattr(entry.service, "brand_logo_context", lambda *args: ("workspace", idea, "Your venture"))

    class Logo:
        filename = "owned.svg"

        async def read(self, limit):
            return b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'

    async def form():
        return {"file": Logo()}

    before = json.dumps(entry.source)
    result = asyncio.run(entry.api.upload_venture_logo("venture", SimpleNamespace(form=form, headers={"X-Subject": "owner"})))
    assert len(entry.writes) == 1 and entry.writes[0][-1] == "venture"
    assert result["ok"] and not result["site_refreshed"]
    assert json.dumps(entry.source) == before
    if connected:
        assert result["device_order"]["id"] == entry.order["id"] and len(entry.calls) == 1
        assert entry.calls[0][0] == "venture"
        brief = json.loads(entry.calls[0][1])
        assert "logo" in brief["edit_instruction"].lower()
        assert brief["edit_source"] == entry.source
        assert brief["reuse_copy"] == entry.source["copy"]
    else:
        assert "Settings > Keys and connections > Coding tools" in result["next_step"]
        assert entry.calls == []


@pytest.mark.parametrize("idea,audience", _DOMAINS)
@pytest.mark.parametrize("connected", [True, False])
def test_local_onboarding_waits_for_the_tool_without_claiming_completion(local_entry, idea, audience, connected):
    entry = local_entry
    entry.connected[0] = connected
    ctx = SimpleNamespace(venture=SimpleNamespace(id="venture", workspace_id="workspace", name="Your venture", raw_idea=idea),
        out={"understand.classify": {"audience": audience}, "orient.read_brief": {"normalized": idea}})
    before = json.dumps(ctx.out)
    result = asyncio.run(entry.steps.generate(ctx))
    assert result.pause and not result.simulated and not result.evidence_url
    assert entry.steps.MEM == {} and json.dumps(ctx.out) == before
    if connected:
        assert result.output["device_order"]["id"] == entry.order["id"]
        assert result.pause.founder_action == entry.order["review_path"]
        assert len(entry.calls) == 1 and idea in entry.calls[0][1] and audience in entry.calls[0][1]
    else:
        assert "Settings > Keys and connections > Coding tools" in result.pause.reason
        assert entry.calls == []


@pytest.mark.parametrize("idea,audience", _DOMAINS)
def test_local_onboarding_tool_failure_never_retries_internal_generation(local_entry, idea, audience, monkeypatch):
    entry = local_entry
    attempts = []

    async def fail(*args, **kwargs):
        attempts.append(kwargs)
        raise RuntimeError("tool failed")

    monkeypatch.setattr(entry.service, "generate_first_look", fail)
    ctx = SimpleNamespace(venture=SimpleNamespace(id="venture", workspace_id="workspace", name="Your venture", raw_idea=idea),
                          out={"understand.classify": {"audience": audience}})
    result = asyncio.run(entry.steps.generate(ctx))
    assert result.pause and "Builds" in result.pause.reason and len(attempts) == 1
    assert entry.steps.MEM == {}


@pytest.mark.parametrize("idea,audience", _DOMAINS)
@pytest.mark.parametrize("state", ["missing", "changed"])
def test_local_logo_upload_with_missing_or_changed_page_only_saves_the_logo(local_entry, idea, audience, state, monkeypatch):
    entry = local_entry
    monkeypatch.setattr(entry.service, "brand_logo_context", lambda *args: ("workspace", idea, "Your venture"))
    if state == "missing":
        monkeypatch.setattr(entry.service, "current_site", lambda *args, **kwargs: None)
    else:
        monkeypatch.setattr(entry.service.versions_mod, "get_latest", lambda *args, **kwargs: SimpleNamespace(id="newer_page"))

    class Logo:
        filename = "owned.svg"

        async def read(self, limit):
            return b"owned-logo"

    async def form():
        return {"file": Logo()}

    result = asyncio.run(entry.api.upload_venture_logo("venture", SimpleNamespace(form=form, headers={"X-Subject": "owner"})))
    assert entry.writes and not result["site_refreshed"] and entry.calls == []


@pytest.mark.parametrize("idea,audience", _DOMAINS)
def test_local_another_look_without_a_page_gives_the_build_step(local_entry, idea, audience, monkeypatch):
    from fastapi import HTTPException

    entry = local_entry
    monkeypatch.setattr(entry.service, "current_site", lambda *args, **kwargs: None)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(entry.api.try_another("workspace", entry.api.TryAnother(idea_text=idea, venture_id="venture"), "owner"))
    assert "Settings > Keys and connections > Coding tools" in str(failure.value.detail)
    assert entry.calls == []


@pytest.mark.parametrize("idea,audience", _DOMAINS)
def test_local_logo_upload_checks_write_access_before_saving(local_entry, idea, audience, monkeypatch):
    from fastapi import HTTPException

    entry = local_entry

    def denied(*args):
        raise HTTPException(403, "Write denied")

    monkeypatch.setattr(entry.api, "_assert_site_writer", denied)
    with pytest.raises(HTTPException) as failure:
        asyncio.run(entry.api.upload_venture_logo("venture", SimpleNamespace(headers={"X-Subject": "viewer"})))
    assert failure.value.status_code == 403 and entry.writes == [] and entry.calls == []
