"""New GTM routes inherit the verified gateway workspace and mutation policy."""

import pytest

from tests.security.test_core_signed_boundary import client as client, headers, make_workspace


@pytest.mark.parametrize("path", ["/api/v1/gtm/god", "/api/v1/outreach/evidence-sources"])
def test_new_ops_requires_signed_owned_explicit_workspace(client, monkeypatch, path):
    from app.db.models import Workspace
    from app.state import db

    wid = make_workspace(client, "usr_alice")
    foreign = make_workspace(client, "usr_bob")
    with db()() as session:
        session.get(Workspace, wid).tenancy = "managed"
        session.commit()
    monkeypatch.setenv("OPS_API_KEY", "shared-key-is-not-required-for-signed-users")
    assert client.get(path, params={"workspace_id": wid}).status_code == 401
    assert client.get(path, headers=headers(path)).status_code == 400
    assert client.get(path, params={"workspace_id": foreign}, headers=headers(path)).status_code == 404
    response = client.get(path, params={"workspace_id": wid}, headers=headers(path))
    assert response.status_code == 200, response.text
    response = client.get(path, params={"workspace_id": wid}, headers={**headers(path), "X-Workspace-Id": foreign})
    assert response.status_code == 404


@pytest.mark.parametrize("path,body", [
    ("/api/v1/gtm/god/start", {"idea": "A test idea"}),
    ("/api/v1/outreach/evidence-sources", {"provider": "gmail", "action": "connect"}),
])
def test_viewer_cannot_mutate_new_ops(client, path, body):
    from app.db.models import WorkspaceMember
    from app.state import db
    from core.clock import utcnow

    wid = make_workspace(client, "usr_alice")
    make_workspace(client, "usr_bob")
    with db()() as session:
        session.add(WorkspaceMember(id="wsm_gtm_viewer", workspace_id=wid, person_id="usr_bob", role="viewer", created_at=utcnow()))
        session.commit()
    response = client.post(path, params={"workspace_id": wid}, json=body, headers=headers(path, "POST", "usr_bob"))
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "read_only_workspace"



@pytest.mark.parametrize("path,body", [
    ("/api/v1/gtm/god/start", {"idea": "A test idea"}),
    ("/api/v1/gtm/god/tick", {}),
    ("/api/v1/gtm/god/tasks/legacy-task/complete", {"note": "reviewed"}),
])
def test_legacy_god_mutations_require_quote_without_execution(client, monkeypatch, path, body):
    from app.gtm.god import service, generation
    from llm_gateway import sole
    def no_execution(*args, **kwargs):
        pytest.fail("Legacy route must not execute a service or provider")
    for name in ("start_plan", "tick", "complete_human"):
        monkeypatch.setattr(service, name, no_execution)
    for name in ("quote", "start"):
        monkeypatch.setattr(generation, name, no_execution)
    monkeypatch.setattr(sole, "transmit", no_execution)
    wid = make_workspace(client, "usr_alice")
    response = client.post(path, params={"workspace_id": wid}, json=body, headers=headers(path, "POST"))
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "gtm_quote_required"


@pytest.mark.parametrize("endpoint", ["quotes", "runs"])
def test_diagnosis_checks_signed_venture_authority_before_resources(client, monkeypatch, endpoint):
    from app.gtm.god import generation
    from app.db.models import Venture, GtmGodRun
    from app.state import db
    from core.clock import utcnow
    from llm_gateway import sole
    from sqlalchemy import select

    wid = make_workspace(client, "usr_alice")
    other = make_workspace(client, "usr_alice")
    foreign = make_workspace(client, "usr_bob")
    now = utcnow()
    with db()() as session:
        for vid, workspace in (("ven_own", wid), ("ven_sibling_workspace", other), ("ven_foreign", foreign)):
            session.add(Venture(id=vid, workspace_id=workspace, name=vid, raw_idea="test", stage="idea", created_at=now, updated_at=now))
        session.commit()
    # Replace only pricing/provider configuration, after the real authority check.
    # HTTP auth, generation.authority, snapshot, quote comparison and persistence run unchanged.
    resources = []
    def offline_resources(workspace_id, venture_id):
        resources.append((workspace_id, venture_id))
        return {"resource_digest": "a" * 64, "provider_identity": "offline", "charge_identity": "offline",
                "call_ceiling_micro": 10, "payer": "platform_credits", "account_active": True, "available_micro": 100}
    monkeypatch.setattr(generation, "resources", offline_resources)
    monkeypatch.setattr(sole, "transmit", lambda *a, **kw: pytest.fail("Quote/enqueue must not call a provider"))
    body = {"kind": "diagnose", "idea": "Test concept"}
    if endpoint == "runs":
        body.update(input_digest="b" * 64, resource_digest="a" * 64, ceiling_micro=10)
    def post(vid, workspace, subject="usr_alice", signed=True, payload=None):
        path = f"/api/v1/ventures/{vid}/gtm/god/{endpoint}"
        auth = headers(path, "POST", subject) if signed else {}
        return client.post(path, params={"workspace_id": workspace}, json=payload or body,
                           headers={**auth, "Idempotency-Key": "boundary-intent-1"})
    assert post("ven_own", wid, signed=False).status_code == 401
    for vid in ("ven_sibling_workspace", "ven_foreign", "ven_missing"):
        response = post(vid, wid)
        assert response.status_code == 404, response.text
    assert post("ven_foreign", foreign).status_code == 404  # Alice has no membership.
    assert post("ven_own", wid, subject="usr_bob").status_code == 404
    assert resources == []
    if endpoint == "runs":
        quote_path = "/api/v1/ventures/ven_own/gtm/god/quotes"
        quote = client.post(quote_path, params={"workspace_id": wid}, json={"kind": "diagnose", "idea": "Test concept"},
                            headers=headers(quote_path, "POST"))
        assert quote.status_code == 200, quote.text
        body.update({key: quote.json()[key] for key in ("input_digest", "resource_digest", "ceiling_micro")})
    response = post("ven_own", wid)
    assert response.status_code == (202 if endpoint == "runs" else 200), response.text
    assert resources and all(pair == (wid, "ven_own") for pair in resources)
    if endpoint == "runs":
        with db()() as session:
            runs = session.scalars(select(GtmGodRun)).all()
            assert len(runs) == 1
            assert (runs[0].workspace_id, runs[0].venture_id, runs[0].actor_id, runs[0].state) == (wid, "ven_own", "usr_alice", "queued")
