"""Real rendered draft + persisted bytes, with all provider/subprocess seams closed."""
import asyncio
import json
from contextlib import nullcontext

import pytest

from tests.coding_tool_fixtures import installed_test_tool
from tests.unit.autopilot import test_god_mode as harness


@pytest.fixture(name="env")
def site_env(tmp_path, monkeypatch):
    yield from harness.env.__wrapped__(tmp_path, monkeypatch)


def _generate(env, monkeypatch, *, fallback=False, venture=None, workspace=None):
    _, vid = env
    from app.autopilot import service
    from app.autopilot.authority import founder_scope
    from app.autopilot.receipts import collect
    from app.db.models import Venture
    from app.firstlook import service as firstlook, serve
    from app.state import db

    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    from tests.firstlook_legacy import seed_legacy_page

    if workspace in (None, wid) and venture in (None, vid):
        seed_legacy_page(wid, vid)

    async def no_code(*args, **kwargs):
        return None

    monkeypatch.setattr(firstlook, "try_code_html", no_code)
    monkeypatch.setattr(serve, "_commit_site", lambda *args, **kwargs: None)
    copy = {"headline": "Owned draft headline", "subhead": "A draft for synthetic testing.",
        "points": [{"title": "Scope", "body": "Owned test only."}],
        "cta": {"label": "Join the waitlist", "kind": "waitlist"}, "fallback": fallback}
    with founder_scope(vid, service._owner(vid)):
        with collect(vid) as evidence:
            view = asyncio.run(firstlook.generate_first_look(workspace or wid, "Owned synthetic idea",
                venture_id=venture or vid, reuse_copy=copy, allow_model=False))
    return view, evidence


@pytest.mark.hosted_only
def test_real_generated_draft_is_venture_bound_and_independently_proved(env, monkeypatch):
    _, vid = env
    view, evidence = _generate(env, monkeypatch)
    from app.db.models import SiteVersion
    from app.state import db
    with db()() as session:
        row = session.get(SiteVersion, view.id)
        assert row.venture_id == vid
        assert json.loads(row.copy_json)["_render_snapshot"]["html"]
        assert not row.published
    assert evidence.outcome() == "completed"


@pytest.mark.parametrize("wrong", ["venture", "workspace"])
def test_generated_draft_rejects_foreign_scope_before_any_writer(env, monkeypatch, wrong):
    from app.db.models import SiteVersion
    from app.state import db
    with pytest.raises(PermissionError):
        _generate(env, monkeypatch, venture="foreign" if wrong == "venture" else None,
                  workspace="foreign" if wrong == "workspace" else None)
    with db()() as session:
        assert session.query(SiteVersion).filter(SiteVersion.version > 0).count() == 0


@pytest.mark.hosted_only
def test_fallback_draft_never_certifies_requested_rewrite(env, monkeypatch):
    view, evidence = _generate(env, monkeypatch, fallback=True)
    assert view.fallback and evidence.outcome() == "held"


@pytest.mark.hosted_only
def test_actual_rewrite_saves_a_bound_new_draft_and_never_publishes(env, monkeypatch):
    client, vid = env
    first, _ = _generate(env, monkeypatch)
    from app.autopilot import service
    from app.firstlook import service as firstlook, versions
    harness._on(client, vid)

    async def closed_copy(*args, **kwargs):
        assert "Make the headline precise" in kwargs["instruction"]
        return {"headline": "Precisely rewritten owned draft", "subhead": "Synthetic scoped draft.",
            "points": [], "cta": {"label": "Join waitlist", "kind": "waitlist"}}, 0.0, False

    monkeypatch.setattr(firstlook, "generate_copy", closed_copy)
    done = []
    _, stop = asyncio.run(service._execute(vid, "site.rewrite", {"instruction": "Make the headline precise"},
        "Owned synthetic rewrite", service._owner(vid), done))
    latest = versions.get_latest(first.workspace_id, venture_id=vid)
    assert latest.id != first.id and latest.copy["headline"] == "Precisely rewritten owned draft"
    assert not latest.published and not latest.fallback
    assert stop is None and done == ["site.rewrite"]
    assert not service._unresolved_admission(vid, "site.rewrite")


@pytest.mark.hosted_only
def test_historical_unbound_site_requires_fresh_draft_without_permanent_hold(env, monkeypatch):
    client, vid = env
    first, _ = _generate(env, monkeypatch)
    from app.autopilot import service
    from app.db.models import SiteVersion
    from app.firstlook import service as firstlook
    from app.state import db
    with db()() as session:
        for row in session.query(SiteVersion).filter(SiteVersion.workspace_id == first.workspace_id):
            row.venture_id = None
        session.commit()
    async def forbidden(*args, **kwargs):
        pytest.fail("Historical unbound draft must not silently dispatch a model")
    monkeypatch.setattr(firstlook, "generate_copy", forbidden)
    harness._on(client, vid)
    _, stop = asyncio.run(service._execute(vid, "site.rewrite", {"instruction": "Rewrite"},
        "Historical draft", service._owner(vid), []))
    assert stop == "needs_you"
    assert not service._unresolved_admission(vid, "site.rewrite")


@pytest.mark.parametrize("field", ["venture_id", "copy_json", "sha256", "published"])
@pytest.mark.hosted_only
def test_site_receipt_rechecks_scope_and_frozen_bytes_after_writer(env, monkeypatch, field):
    view, evidence = _generate(env, monkeypatch)
    from app.db.models import SiteVersion
    from app.state import db
    with db()() as session:
        row = session.get(SiteVersion, view.id)
        setattr(row, field, 1 if field == "published" else "changed")
        session.commit()
    assert evidence.outcome() == "held"


@pytest.mark.hosted_only
def test_first_look_gets_the_founder_first_name_from_the_desk(env, monkeypatch):
    _, vid = env
    from app.db.models import Venture
    from app.firstlook import service as firstlook, serve
    from app.planning import execute
    from app.state import db

    with db()() as session:
        wid = session.get(Venture, vid).workspace_id
    seen = {}

    async def fake_copy(idea, **kw):
        seen.update(kw)
        from app.firstlook.copy import fallback_copy
        return fallback_copy(idea), 0.0, True

    async def no_code(*a, **k):
        return None

    monkeypatch.setattr(firstlook, "generate_copy", fake_copy)
    monkeypatch.setattr(firstlook, "try_code_html", no_code)
    monkeypatch.setattr(serve, "_commit_site", lambda *a, **k: None)
    monkeypatch.setattr(execute, "_founder_first_name", lambda _v: "Asha")
    from tests.firstlook_legacy import seed_legacy_page

    seed_legacy_page(wid, vid)
    asyncio.run(firstlook.generate_first_look(wid, "Owned synthetic idea", venture_id=vid))
    assert seen["founder_name"] == "Asha"


@pytest.mark.parametrize("idea", ["Stock planning for wholesalers", "Music lessons for families"])
@pytest.mark.parametrize("state", ["unrequested", "not_connected", "connected"])
def test_local_page_work_requires_your_request_and_connected_tool(env, monkeypatch, tmp_path, idea, state):
    from app.autopilot import service
    from app.autopilot.authority import founder_scope
    from app.autopilot.receipts import collect
    from app.build import device_orders
    from app.coding_agents_consent import set_consent
    from app.db.models import SiteVersion, Venture
    from app.firstlook import service as firstlook
    from app.state import db
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    client, _ = env
    created = client.post("/ventures", json={"raw_idea": idea, "start": False})
    assert created.status_code == 201
    venture_id = created.json()["id"]
    actor = service._owner(venture_id)
    with db()() as session:
        workspace_id = session.get(Venture, venture_id).workspace_id
    monkeypatch.setattr(agent_status, "statuses", lambda: [] if state == "not_connected" else [
        {"name": "codex", "installed": True, "signed_in": True, "subscription_login": True, "available": True}])
    binary = tmp_path / "codex"
    binary.touch(mode=0o700)
    with installed_test_tool(monkeypatch, "codex", binary):
        set_consent(actor, "codex", True)
    requests = []

    def prepare(vid, caller, instruction):
        assert vid == venture_id and caller.sub == actor
        requests.append(instruction)
        return {"id": "requested_page", "state": "approved"}

    def forbidden(*args, **kwargs):
        pytest.fail("Your connected coding tool must build the page, not an internal generator")

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(firstlook, "generate_copy", forbidden)
    monkeypatch.setattr(firstlook, "try_code_html", forbidden)
    monkeypatch.setattr(firstlook, "compose_html", forbidden)
    monkeypatch.setattr(firstlook, "write_site", forbidden)
    with founder_scope(venture_id, actor) if state != "unrequested" else nullcontext():
        if state == "connected":
            with collect(venture_id) as evidence:
                result = asyncio.run(firstlook.generate_first_look(workspace_id, idea, venture_id=venture_id))
            assert result["id"] == "requested_page" and result["state"] == "approved"
            assert len(requests) == 1 and idea in requests[0]
            assert "Do not publish" in requests[0]
            assert evidence.outcome() == "held"
        else:
            with pytest.raises(firstlook.FirstLookError) as error:
                asyncio.run(firstlook.generate_first_look(workspace_id, idea, venture_id=venture_id))
            assert error.value.code == ("coding_tool_required" if state == "not_connected" else "page_request_required")
            assert ("Settings > Keys and connections > Coding tools" if state == "not_connected" else "venture chat") in error.value.message
            assert requests == []
    with db()() as session:
        assert session.query(SiteVersion).filter_by(venture_id=venture_id).count() == 0
