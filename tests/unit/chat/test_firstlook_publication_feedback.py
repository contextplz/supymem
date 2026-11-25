"""Publishing feedback preserves build fallback and treats unknown public state honestly."""

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


@pytest.mark.parametrize("code", ["hosting_app_not_found", "hosting_firstlook_draft_required", "hosting_firstlook_draft_unsupported"])
@pytest.mark.parametrize("venture_id", ["ven_warehouse_planning", "ven_family_camping"])
def test_no_firstlook_source_retains_the_existing_build_next_step(monkeypatch, code, venture_id):
    from app.chat import site_actions
    from app.core.local_mode import is_local
    from app.hosting import firstlook_publish as fp

    monkeypatch.setattr(fp, "describe", lambda _vid: {
        "state": "blocked", "draft": None, "blockers": [{"code": code, "message": "No concept landing page exists."}]})
    monkeypatch.setattr(site_actions, "publish_preflight", lambda _vid: (
        None, None, [("There is no hosted app", site_actions.build_landing_button(), "no_hosted_app")]))
    reply = site_actions.route(venture_id, "site.publish", "publish my page")
    assert reply["kind"] == "needs_you"
    assert reply["actions"][0]["action"] == ("pagebuilder.build" if is_local() else "build.prepare")
    assert not any(action.get("confirmation") for action in reply["actions"])


def test_missing_current_draft_does_not_hide_an_existing_uncertain_publication(monkeypatch):
    from app.chat import site_actions
    from app.core.local_mode import is_local
    from app.hosting import firstlook_publish as fp

    monkeypatch.setattr(fp, "describe", lambda _vid: {
        "state": "blocked", "draft": None, "blockers": [
            {"code": "hosting_app_not_found", "message": "Current draft unavailable."},
            {"code": "hosting_public_state_uncertain", "message": fp.plain("hosting_public_state_uncertain")}]})
    reply = site_actions.firstlook_publish_reply("ven_fixture")
    assert reply["kind"] == "needs_you"
    assert [action["action"] for action in reply["actions"]] == (["site.preview"] if is_local() else [])
    assert "could not be confirmed" in reply["body_md"]
    assert "Nothing went public" not in reply["body_md"]


def test_ready_firstlook_confirm_is_bound_to_the_exact_saved_source(monkeypatch):
    from app.chat import site_actions
    from app.hosting import firstlook_publish as fp

    monkeypatch.setattr(fp, "describe", lambda _vid: {
        "state": "publishable", "draft": {"site_version_id": "site_exact", "sha256": "a" * 64}, "blockers": []})
    reply = site_actions.firstlook_publish_reply("ven_fixture")
    assert reply["kind"] == "confirmation"
    assert reply["actions"][0]["confirmation"]["resulting_state"] == {
        "mode": "firstlook", "site_version_id": "site_exact", "public_sha256": "a" * 64}


@pytest.mark.parametrize("venture_id,has_draft", [("ven_warehouse_planning", True), ("ven_family_camping", False)])
def test_an_existing_in_progress_publication_does_not_claim_nothing_went_public(monkeypatch, venture_id, has_draft):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    from app.chat import site_actions
    from app.hosting import firstlook_publish as fp

    draft = {"site_version_id": "site_exact", "sha256": "a" * 64} if has_draft else None
    blockers = [{"code": "hosting_publication_in_progress", "message": fp.plain("hosting_publication_in_progress")}]
    if not has_draft:
        blockers.append({"code": "hosting_app_not_found", "message": "Current draft unavailable."})
    monkeypatch.setattr(fp, "describe", lambda _vid: {
        "state": "blocked", "draft": draft, "blockers": blockers})
    reply = site_actions.firstlook_publish_reply(venture_id)
    assert reply["kind"] == "needs_you"
    assert [action["action"] for action in reply["actions"]] == ["site.preview"]
    assert "Your page is being published" in reply["body_md"]
    assert "Choose Review your saved page below to check its progress before publishing again" in reply["body_md"]
    assert "Nothing went public" not in reply["body_md"]


@pytest.mark.asyncio
@pytest.mark.parametrize("code,uncertain", [("hosting_public_state_uncertain", True), ("hosting_firstlook_source_changed", False)])
async def test_confirmed_firstlook_publish_refusal_does_not_invent_public_outcome(monkeypatch, code, uncertain):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.chat import site_actions
    from app.hosting import firstlook_publish as fp
    from app.hosting.service import HostingError

    calls = []

    def refuse(venture_id, **kwargs):
        calls.append((venture_id, kwargs))
        raise HostingError(code)

    monkeypatch.setattr(fp, "queue_publication", refuse)
    reply = await site_actions.execute("ven_fixture", "site.publish", {
        "mode": "firstlook", "site_version_id": "site_exact", "public_sha256": "a" * 64}, "actor_fixture")
    assert calls == [("ven_fixture", {"actor": "actor_fixture", "site_version_id": "site_exact", "public_sha256": "a" * 64})]
    assert "queued" not in reply["body_md"] and reply["actions"] == []
    assert "Nothing went public" not in reply["body_md"]
    if uncertain:
        assert "could not confirm" in reply["body_md"] and "Open Website" in reply["body_md"]


@pytest.mark.parametrize("venture_id", ["ven_warehouse_planning", "ven_family_camping"])
@pytest.mark.parametrize("code,uncertain", [
    ("hosting_public_state_uncertain", True), ("hosting_public_copy_unreviewed", False),
    ("hosting_public_health_mismatch", False),  # readback can fail after the mapping was briefly switched
])
def test_deployment_failure_notification_preserves_unknown_public_state(monkeypatch, code, uncertain, venture_id):
    from app.chat import milestone_hooks
    from app.core.local_mode import is_local

    messages = []
    monkeypatch.setattr(milestone_hooks, "post_milestone", lambda venture_id, **kwargs: messages.append((venture_id, kwargs)))
    milestone_hooks.deployment_failed(venture_id, "dep_exact", code)
    assert len(messages) == 1
    vid, message = messages[0]
    assert vid == venture_id and message["key"] == "deploy_failed:dep_exact"
    assert message["failure"] == {"step": "publish", "code": code, "job_id": "dep_exact"}
    assert "Nothing new went public" not in message["body_md"]
    assert "nothing new went public" not in message["body_md"]
    if uncertain:
        if is_local():
            assert "couldn't confirm whether your page is public" in message["body_md"]
            assert "Open Website to check before publishing again" in message["body_md"]
        else:
            assert "could not confirm" in message["body_md"]
    else:
        assert "Publishing did not finish" in message["body_md"]
    assert message["actions"][0]["href"] == f"/v/{venture_id}/site"
