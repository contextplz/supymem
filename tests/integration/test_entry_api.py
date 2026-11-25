"""Integration tests for POST /entry (35 §9)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/entry_api.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("IDEALAB_DATA_DIR", str(tmp_path / "idealab"))
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as c:
        yield c


def test_list_modes(client: TestClient) -> None:
    body = client.get("/entry/modes").json()
    assert len(body["modes"]) == 5
    assert {m["mode"] for m in body["modes"]} >= {
        "idea",
        "product",
        "existing_company",
    }


def test_list_stages(client: TestClient) -> None:
    body = client.get("/entry/stages").json()
    assert [s["id"] for s in body["stages"]] == [
        "scratch",
        "product",
        "company",
        "other",
    ]


def test_explicit_surprise_commits_slate(client: TestClient, tmp_path, monkeypatch) -> None:
    _seed_corpus(tmp_path)
    r = client.post("/entry", json={"explicit_mode": "surprise"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True
    assert body["mode"] == "surprise"
    assert body["workspace"]["kind"] == "desk"
    assert "slate" in body
    assert body["decision"]["confidence"] == 1.0


def test_low_confidence_creates_nothing(client: TestClient) -> None:
    r = client.post("/entry", json={"text": "please fix this"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is False
    assert body["decision"]["confirm_needed"] is True
    assert "workspace" not in body or body.get("workspace") is None


def test_confirm_then_commits_engagement(client: TestClient) -> None:
    r = client.post(
        "/entry",
        json={"text": "please fix this", "confirm": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True
    assert body["mode"] == "engagement"
    assert body["engagement"]["id"].startswith("eng_")


def test_idea_creates_venture_without_starting(client: TestClient) -> None:
    r = client.post(
        "/entry",
        json={
            "text": "a marketplace for restoring vintage synthesizers",
            "explicit_mode": "idea",
            "start": False,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True
    assert body["venture_id"].startswith("ven_")
    assert body["workspace"]["kind"] == "venture"
    assert body["onboarding"] is None
    assert body["workspace"]["settings"]["project_intent"] == "startup"


def test_own_use_idea_is_inferred_without_a_hobby_selector(client: TestClient) -> None:
    created = client.post("/entry", json={
        "text": "a private Supabase bot just for me to sort photos",
        "explicit_mode": "idea", "start": False,
    })
    assert created.status_code == 200, created.text
    assert created.json()["workspace"]["settings"]["project_intent"] == "personal"
    opened = client.post(f"/ventures/{created.json()['venture_id']}/idea/sessions")
    assert opened.status_code in (200, 201), opened.text
    assert opened.json()["project_intent"] == "personal"


@pytest.mark.parametrize("prompt_text", [
    "I already have a product and only want GTM for its first users",
    "I already have a product and only need users",
])
def test_existing_product_gtm_only_skips_idea_and_onboarding(client: TestClient, prompt_text: str) -> None:
    created = client.post("/entry", json={
        "text": prompt_text,
        "explicit_mode": "idea", "start": True,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["mode"] == "product"
    assert body["next_step"] == "gtm"
    assert body["onboarding"] is None
    assert body["workspace"]["settings"]["answers"]["want_first"] == "gtm"
    assert not client.get(f"/ventures/{body['venture_id']}/idea/sessions").json()["items"]
    from app.credits.accounts import get_account
    from app.db.models import Workspace
    from app.state import db
    wallet = get_account(body["venture_id"])
    assert wallet is not None and wallet.scope == "person"
    assert wallet.workspace_id is None and wallet.venture_id is None
    with db()() as session:
        assert wallet.owner_person_id == session.get(Workspace, body["workspace"]["id"]).owner_id


def test_product_credit_failure_keeps_saved_venture_and_blocks_gtm(client: TestClient, monkeypatch) -> None:
    from app.credits import accounts

    def unavailable(*args, **kwargs):
        raise RuntimeError("temporary credit store failure")

    monkeypatch.setattr(accounts, "ensure_account", unavailable)
    created = client.post("/entry", json={
        "text": "I already have a product and only need users",
        "explicit_mode": "idea", "start": True,
    })
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["committed"] is True
    assert body["venture_id"].startswith("ven_")
    assert body["next_step"] == "credits"
    assert body["setup_issue"] == "credit_account_unavailable"
    assert body["onboarding"] is None
    assert client.get(f"/ventures/{body['venture_id']}").status_code == 200
    assert not client.get(f"/ventures/{body['venture_id']}/idea/sessions").json()["items"]


def test_personal_idea_intent_is_saved_through_clarification(client: TestClient, monkeypatch) -> None:
    r = client.post("/entry", json={
        "text": "a small Supabase bot for my own photo reminders",
        "explicit_mode": "idea", "start": False, "project_intent": "personal",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace"]["settings"]["project_intent"] == "personal"
    session = client.post(f"/ventures/{body['venture_id']}/idea/sessions")
    assert session.status_code in (200, 201), session.text
    saved = session.json()
    assert saved["project_intent"] == "personal"
    assert saved["current_revision"]["structured"]["project_intent"] == "personal"
    assert saved["current_revision"]["structured"].get("pricing_hypothesis") in (None, "")
    from tests.idea_fixtures import install_provider
    from tests.security.test_durable_journey_boundary import run_queued
    calls = install_provider(monkeypatch)
    run_queued("idea.clarify_turn")
    refreshed = client.post(f"/ventures/{body['venture_id']}/idea/sessions").json()
    assert refreshed["project_intent"] == "personal"
    assert refreshed["current_revision"]["structured"]["project_intent"] == "personal"
    assert calls and '"project_intent": "personal"' in calls[0][0][-1]["content"]


@pytest.mark.parametrize("case", ["know", "explore"])
def test_personal_proposal_goes_to_review_and_confirm_without_paid_validation(
    client: TestClient, monkeypatch, case: str,
) -> None:
    from sqlalchemy import select
    from app.db.models import Job
    from app.state import db
    from app.ventures.run_status import snapshot
    from tests.idea_fixtures import install_provider
    from tests.security.test_durable_journey_boundary import run_queued

    created = client.post("/entry", json={
        "text": "a private Supabase bot for sorting my own bookmarks",
        "explicit_mode": "idea", "start": False, "project_intent": "personal",
    })
    assert created.status_code == 200, created.text
    vid = created.json()["venture_id"]
    wid = created.json()["workspace"]["id"]
    opened = client.post(f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201, opened.text
    sid = opened.json()["id"]
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    calls = install_provider(monkeypatch)
    run_queued("idea.clarify_turn")
    if case == "explore":
        selected = client.post(base + "/case", json={"case": "explore"})
        assert selected.status_code == 200, selected.text
    proposed = client.post(base + "/propose")
    assert proposed.status_code == 200, proposed.text
    run_queued("idea.clarify_turn", through_job_id=proposed.json()["job_id"])
    saved = client.get(base).json()
    assert saved["state"] == "reviewing"
    assert saved["project_intent"] == "personal"
    assert saved["current_revision"]["structured"]["project_intent"] == "personal"
    assert client.get(f"/ventures/{vid}/idea/sessions").json()["items"][0]["project_intent"] == "personal"
    assert snapshot(vid, wid)["action"]["label"] == "Review project"
    assert not any(call[1].get("purpose") == "idea.validate" for call in calls)
    legacy_job_id = None
    with db()() as session:
        assert session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind == "idea.validate")) is None
        if case == "explore":
            # A pre-fix personal session can still have a market validation
            # pending. Its receipt remains, but it cannot block the direct
            # personal-project route or trigger a second paid call.
            from app.core.jobs import enqueue
            from app.db.supymem_models import IdeaSession
            session.get(IdeaSession, sid).state = "validating"
            legacy_job_id = enqueue(session, "idea.validate", {
                "session_id": sid, "revision_id": saved["current_revision"]["id"],
                "methods": ["gstack_office_hours"],
            }, workspace_id=wid, venture_id=vid)
            session.commit()
    confirmed = client.post(base + "/confirm", json={"revision_id": saved["current_revision"]["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["dossier_job_id"] is None
    status = snapshot(vid, wid)
    assert status["action"]["label"] == "Review local build"
    assert status["job_id"] is None
    assert status["next_step"] == "Review local build"
    with db()() as session:
        assert session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind == "research.deep")) is None
        if legacy_job_id:
            assert session.get(Job, legacy_job_id).status == "scheduled"


def test_invalid_idea_intent_does_not_create_workspace(client: TestClient) -> None:
    before = len(client.get("/workspaces").json()["grouped"]["venture"])
    response = client.post("/entry", json={
        "text": "a small Supabase bot for my own photo reminders",
        "explicit_mode": "idea", "start": False, "project_intent": "enterprise_only",
    })
    assert response.status_code == 422
    assert len(client.get("/workspaces").json()["grouped"]["venture"]) == before


def test_personal_explore_answer_correction_stays_on_direct_build_route(client: TestClient) -> None:
    """A saved personal answer must not restore paid market validation."""
    import json
    from sqlalchemy import select
    from app.db.models import Job
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow

    created = client.post("/entry", json={
        "text": "a private Supabase bot for my own bookmarks",
        "explicit_mode": "idea", "start": False, "project_intent": "personal",
    })
    assert created.status_code == 200, created.text
    vid = created.json()["venture_id"]
    opened = client.post(f"/ventures/{vid}/idea/sessions")
    assert opened.status_code == 201, opened.text
    sid = opened.json()["id"]
    base = f"/ventures/{vid}/idea/sessions/{sid}"
    revision_id = opened.json()["current_revision"]["id"]
    with db()() as session:
        row = session.get(IdeaSession, sid)
        row.case = "explore"
        row.state = "confirmed"
        row.confirmed_revision_id = revision_id
        row.confirmed_at = utcnow()
        session.add(ClarifierTurn(
            id="ict_personal_question", session_id=sid, workspace_id=row.workspace_id,
            venture_id=vid, turn=20, role="agent", kind="question",
            text="Who will use it?", options=json.dumps({"field": "customer", "options": ["Just me"]}),
            created_at=utcnow(),
        ))
        session.add(ClarifierTurn(
            id="ict_personal_answer", session_id=sid, workspace_id=row.workspace_id,
            venture_id=vid, turn=21, role="founder", kind="answer", text="Just me",
            created_at=utcnow(),
        ))
        session.commit()

    corrected = client.post(base + "/correct-answer", json={
        "revision_id": revision_id, "question_id": "ict_personal_question",
        "text": "Only me", "patch": {"customer": "Only me"},
    })
    assert corrected.status_code == 201, corrected.text
    assert corrected.json()["state"] == "reviewing"
    new_sid = corrected.json()["id"]
    new_base = f"/ventures/{vid}/idea/sessions/{new_sid}"
    edited = client.patch(new_base + "/revision", json={"patch": {"workflow": "Save and sort my own bookmarks"}})
    assert edited.status_code == 200, edited.text
    assert client.get(new_base).json()["state"] == "reviewing"
    confirmed = client.post(new_base + "/confirm", json={"revision_id": edited.json()["id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["session"]["state"] == "confirmed"
    assert confirmed.json()["dossier_job_id"] is None
    changed = client.post(new_base + "/correct-direction", json={
        "case": "explore", "revision_id": edited.json()["id"],
    })
    assert changed.status_code == 201, changed.text
    assert "task, workflow, or smallest useful result" in changed.json()["turns"][-1]["text"]
    assert "customer" not in changed.json()["turns"][-1]["text"]
    with db()() as session:
        assert session.scalar(select(Job.id).where(Job.venture_id == vid, Job.kind.in_(["idea.validate", "research.deep"]))) is None


def test_existing_company_observe_o0(client: TestClient) -> None:
    r = client.post(
        "/entry",
        json={
            "text": "https://github.com/acme/widgets",
            "explicit_mode": "existing_company",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["workspace"]["kind"] == "company"
    assert body["observe_level"] == "O0"
    assert body["workspace"]["observe_level"] == "O0"
    assert body.get("onboarding")
    assert body["onboarding"]["run_id"]
    assert body["venture_id"].startswith("ven_")


def test_bug_report_with_active_venture_is_engagement(client: TestClient) -> None:
    created = client.post(
        "/ventures",
        json={
            "name": "Recolith",
            "raw_idea": "a marketplace for restoring vintage synthesizers",
        },
    )
    assert created.status_code == 201
    ws_id = created.json()["workspace_id"]
    r = client.post(
        "/entry",
        json={
            "text": "checkout is broken on Safari",
            "active_workspace_id": ws_id,
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["committed"] is True
    assert body["mode"] == "engagement"
    assert body["engagement"]["id"].startswith("eng_")
    # No new venture workspace from this ask.
    ventures_before = len(client.get("/workspaces").json()["grouped"]["venture"])
    assert ventures_before >= 1


def _seed_corpus(tmp_path) -> None:
    from datetime import datetime, timezone
    import json

    now = datetime.now(timezone.utc).isoformat()
    items = []
    for i in range(6):
        items.append(
            {
                "claim": f"Clinics still book by phone in region {i}",
                "source": f"https://example.com/signal/{i}",
                "kind": "complaint_thread",
                "category": "local_services",
                "retrieved_at": now,
                "tags": ["local", "booking", f"cluster{i // 2}"],
                "audience": f"small dental practices in city {i}",
                "geography": "Bengaluru",
                "need": f"phone-to-calendar booking for clinics {i}",
            }
        )
        items.append(
            {
                "claim": f"Platform changed booking APIs this year {i}",
                "source": f"https://example.com/change/{i}",
                "kind": "platform_change",
                "category": "local_services",
                "retrieved_at": now,
                "tags": ["local", "booking", f"cluster{i // 2}"],
                "audience": f"small dental practices in city {i}",
                "geography": "Bengaluru",
                "need": f"phone-to-calendar booking for clinics {i}",
            }
        )
    folder = tmp_path / "idealab"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "corpus.json").write_text(
        json.dumps({"refreshed_at": now, "items": items}),
        encoding="utf-8",
    )
