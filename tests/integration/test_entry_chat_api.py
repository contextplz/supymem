"""The first chat message proposes a route before any venture is created."""

from __future__ import annotations

import pytest

from tests.company_os_harness import company_os_test_client, load_company_os_app


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/entry_chat.db")
    monkeypatch.setenv("PROVISIONING_MODE", "local")
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / "data"))
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    from app.core.ratelimit import limiter

    limiter.reset()
    from app.entry import chat_start

    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    monkeypatch.setattr("core.clock.utcnow", lambda: at)
    with company_os_test_client(main.create_app()) as c:
        yield c


def _preview(client, text, **extra):
    response = client.post("/entry/chat/preview", json={"text": text, **extra})
    assert response.status_code == 200, response.text
    return response.json()


def _dashboard_next_step(client, venture_id):
    response = client.get(f"/ventures/{venture_id}/dashboard")
    assert response.status_code == 200, response.text
    return response.json()["next_step"]


def test_gtm_preview_confirm_retry_reload_and_chat_history(client):
    text = "I already have a product. It helps rehearsal studios manage bookings, and I want to get users through GTM"
    first = _preview(client, text, request_id="first")
    assert first["status"] == "proposal"
    proposal = first["proposal"]
    assert proposal["route"] == "gtm"
    assert "build setup" in proposal["skipped_steps"]
    assert _preview(client, text, request_id="first")["proposal"]["id"] == proposal["id"]
    assert client.get("/ventures").json()["items"] == []

    committed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert committed.status_code == 200, committed.text
    result = committed.json()
    assert result["created_new"] is True
    assert result["next_step"] == "gtm"
    assert result["action"]["kind"] == "gtm_review"
    assert result["action"]["href"].endswith(result["venture_id"])
    assert client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).json() == result
    saved = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
    assert saved["receipt"] == result
    assert client.get("/entry/chat/history").json()["items"][0]["text"] == text
    messages = client.get(f"/ventures/{result['venture_id']}/chat").json()["items"]
    assert [m["role"] for m in messages[-2:]] == ["user", "assistant"]
    assert messages[-2]["body_md"] == text


@pytest.mark.parametrize("text,route,next_step,turn_kind", [
    ("I have an app for rehearsal studios to manage bookings and I want to do GTM", "gtm", "gtm", None),
    ("I have an app for families to organize camping trips and I want to do GTM", "gtm", "gtm", None),
    ("I want to build a hobby garden watering tracker for myself", "personal_build", "build", "question"),
    ("I want a personal project for myself to track my garden watering, not a startup.", "personal_build", "build", "proposal"),
    ("I want to build a startup for independent bookshops", "startup", "idea", "question"),
    ("I want to launch an AI startup for schools", "startup", "idea", "question"),
    ("I want to build a startup for sales teams", "startup", "idea", "question"),
])
def test_synthetic_entry_requests_route_to_the_selected_flow(client, text, route, next_step, turn_kind):
    proposal = _preview(client, text)["proposal"]
    assert proposal["route"] == route
    result = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert result.status_code == 200, result.text
    assert result.json()["next_step"] == next_step
    if route != "gtm":
        opened = client.post(f"/ventures/{result.json()['venture_id']}/idea/sessions")
        assert opened.status_code == 201, opened.text
        assert opened.json()["initial_job_id"] is None
        from tests.security.test_durable_journey_boundary import run_queued

        venture_id = result.json()["venture_id"]
        assert _dashboard_next_step(client, venture_id)["title"] == "Preparing your first question"
        run_queued("idea.clarify_turn")
        turns = client.get(f"/ventures/{venture_id}/idea/sessions/{opened.json()['id']}").json()["turns"]
        assert turns[-1]["kind"] == turn_kind
        assert _dashboard_next_step(client, venture_id)["title"] != "Preparing your first question"
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.state import db
    with db()() as session:
        assert session.scalar(select(func.count()).select_from(Job).where(
            Job.venture_id == result.json()["venture_id"])) == (0 if route == "gtm" else 1)
    if route == "gtm":
        messages = client.get(f"/ventures/{result.json()['venture_id']}/chat").json()["items"]
        assert any(action.get("action") == "idea.pressure_test.prepare" for action in messages[-1]["actions"])
    from app.workspaces.service import get_workspace

    settings = get_workspace(result.json()["workspace_id"]).settings
    assert settings["entry_goal"] == route
    if route == "personal_build":
        assert settings["project_intent"] == "personal"


@pytest.mark.parametrize("text", [
    "My app for rehearsal studios manages bookings, but nobody is signing up.",
    "Our live product helps dentists schedule patients, and we need first customers.",
    "I have an app for bookshops to track orders. We have no traction.",
    "Our product helps tutors schedule lessons. How do we attract users?",
    "I have a product for clinics to manage referrals and need help with marketing.",
])
def test_existing_product_with_implied_growth_goal_previews_gtm(client, text):
    proposal = _preview(client, text)["proposal"]
    assert proposal["route"] == "gtm"
    assert proposal["next_step"] == "gtm"


@pytest.mark.parametrize("text", [
    "I want to build a marketing app for small shops",
    "Build an app to help restaurants get customers",
    "Create a marketing tool that tracks campaign performance for agencies",
])
def test_building_a_marketing_product_does_not_preview_gtm(client, text):
    assert _preview(client, text)["proposal"]["route"] == "startup"


def test_ambiguity_correction_and_cancel(client):
    unclear = _preview(client, "I need help")
    assert unclear["status"] == "clarify"
    # Unclear first messages get one-tap answers (founders got stuck typing free text), but the
    # question itself still doesn't steer toward a personal project.
    assert len(unclear["onboarding_choices"]) == 3 and "personal project" not in unclear["question"]
    assert _preview(client, "hobby app")["status"] == "clarify"
    assert _preview(client, "startup")["status"] == "clarify"
    assert _preview(client, "Can you tell me what the weather is today?")["status"] == "clarify"
    bare_gtm = _preview(client, "Hey I want to do GTM")
    assert bare_gtm["status"] == "clarify"
    assert "Which existing product" in bare_gtm["question"]
    assert len(bare_gtm["choices"]) == 3
    qualified = _preview(client, "Hey I want to do GTM\nClarification: It helps rehearsal studios manage bookings")
    assert qualified["proposal"]["route"] == "gtm"
    assert client.get("/entry/chat/history").json()["items"][0]["proposal"]["id"] == unclear["id"]
    proposal = _preview(client, "I need help building a personal watering tracker to use myself")["proposal"]
    assert "skip the build" not in proposal["message"].lower()
    cancelled = client.post(f"/entry/chat/proposals/{proposal['id']}/cancel")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).status_code == 409


@pytest.mark.hosted_only
def test_proposal_actor_scope_and_existing_venture_reuse(client):
    first = _preview(client, "I want to build a startup for bookshops", request_id="same")
    receipt = client.post("/entry/chat/confirm", json={"proposal_id": first["proposal"]["id"]}).json()
    headers = {"X-Subject": "another-founder"}
    assert client.get(f"/entry/chat/proposals/{first['proposal']['id']}", headers=headers).status_code == 404
    assert client.get("/entry/chat/history", headers=headers).json()["items"] == []
    assert client.post("/entry/chat/confirm", json={"proposal_id": first["proposal"]["id"]}, headers=headers).status_code == 404
    assert client.post("/entry/chat/preview", json={"text": "GTM for this", "target_venture_id": receipt["venture_id"]}, headers=headers).status_code == 404
    follow = _preview(client, "I want to get customers for my existing product",
                      target_venture_id=receipt["venture_id"])["proposal"]
    later = client.post("/entry/chat/confirm", json={"proposal_id": follow["id"]}).json()
    assert later["created_new"] is False
    assert later["venture_id"] == receipt["venture_id"]
    assert later["workspace_id"] == receipt["workspace_id"]


def test_named_existing_venture_gtm_reuses_it_without_a_url_target(client):
    created = _preview(client, "I want to build a startup for bookshops")["proposal"]
    receipt = client.post("/entry/chat/confirm", json={"proposal_id": created["id"]}).json()
    venture_id = receipt["venture_id"]
    renamed = client.patch(f"/ventures/{venture_id}", json={"name": "LabHire"})
    assert renamed.status_code == 200, renamed.text

    text = "I already have LabHire. I want to do GTM and find customers for it."
    preview = _preview(client, text, request_id="named-gtm")
    assert preview["proposal"]["target_venture_id"] == venture_id
    assert _preview(client, text, request_id="named-gtm") == preview
    from_clarification = _preview(client, "Hey I want to do GTM\nClarification: LabHire")
    assert from_clarification["proposal"]["target_venture_id"] == venture_id
    native_clarification = _preview(client,
        "I want to do GTM\nClarification: LabHire is the job application assistant already in this account. "
        "I only want to review customer acquisition.")
    assert native_clarification["proposal"]["target_venture_id"] == venture_id
    result = client.post("/entry/chat/confirm", json={"proposal_id": preview["proposal"]["id"]})
    assert result.status_code == 200, result.text
    assert result.json()["venture_id"] == venture_id
    assert len(client.get("/ventures").json()["items"]) == 1

    competitor = _preview(client, "I have an app for shops that competes with LabHire. It manages stock, and I want GTM")
    assert competitor["proposal"]["target_venture_id"] is None
    competitor_id = _preview(client, f"My app competes with {venture_id}; it manages stock for shops and I want GTM")
    assert competitor_id["proposal"]["target_venture_id"] is None

    typo = _preview(client, "I already have LabHires in this account. It helps applicants tailor resumes and I want GTM")
    assert typo["status"] == "clarify"
    assert len(client.get("/ventures").json()["items"]) == 1

    with_request = _preview(client, "GTM for LabHire", request_id="rename-replay")
    assert with_request["proposal"]["target_venture_id"] == venture_id
    assert client.patch(f"/ventures/{venture_id}", json={"name": "CareerKit"}).status_code == 200
    assert _preview(client, "GTM for LabHire", request_id="rename-replay") == with_request

    from app.db.models import Venture
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        session.get(Venture, venture_id).archived_at = utcnow()
        session.commit()
    for name in ("CareerKit", "CareerKits"):
        unresolved = _preview(client, f"GTM for {name}. It helps applicants tailor resumes")
        assert unresolved["status"] == "clarify"
    blocked = client.post("/entry/chat/confirm", json={"proposal_id": with_request["proposal"]["id"]})
    assert blocked.status_code == 404
    assert client.get(f"/entry/chat/proposals/{with_request['proposal']['id']}").json()["proposal"]["state"] == "pending"


def test_named_venture_is_scoped_to_actor_and_ambiguous_names_do_not_create(client):
    from app.db.models import Venture
    from app.state import db

    receipts = []
    for description in ("bookshops", "rehearsal studios"):
        proposal = _preview(client, f"I want to build a startup for {description}")["proposal"]
        receipts.append(client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).json())
    with db()() as session:
        for receipt in receipts:
            session.get(Venture, receipt["venture_id"]).name = "LabHire"
        session.commit()

    ambiguous = _preview(client, "I already have LabHire and want GTM")
    assert ambiguous["status"] == "clarify"
    assert ambiguous["choices"] == []
    assert all(receipt["venture_id"] in ambiguous["question"] for receipt in receipts)
    assert client.get("/ventures").json()["items"] and len(client.get("/ventures").json()["items"]) == 2

    chosen = _preview(client, f"I already have LabHire and want GTM\nClarification: {receipts[1]['venture_id']}")
    assert chosen["proposal"]["target_venture_id"] == receipts[1]["venture_id"]
    assert client.post("/entry/chat/confirm", json={"proposal_id": chosen["proposal"]["id"]}).json()["venture_id"] == receipts[1]["venture_id"]

    other = client.post("/entry/chat/preview", json={
        "text": f"I already have LabHire and want GTM\nClarification: {receipts[1]['venture_id']}",
    }, headers={"X-Subject": "another-founder"})
    assert other.status_code == 200
    assert other.json()["status"] == "clarify"
    inaccessible_named = client.post("/entry/chat/preview", json={
        "text": "GTM for LabHire. It helps applicants tailor resumes",
    }, headers={"X-Subject": "another-founder"})
    assert inaccessible_named.status_code == 200
    assert inaccessible_named.json()["status"] == "clarify"
    inaccessible = client.post("/entry/chat/preview", json={
        "text": "I already have LabHire in this account. It helps job seekers and I want GTM",
    }, headers={"X-Subject": "another-founder"})
    assert inaccessible.status_code == 200
    assert inaccessible.json()["status"] == "clarify"


def test_gtm_for_audience_is_not_treated_as_a_named_venture(client):
    preview = _preview(client, "GTM for engineers. It helps them find better jobs")
    assert preview["status"] == "proposal"
    assert preview["proposal"]["target_venture_id"] is None


def test_request_id_conflict_and_expiry(client):
    first = _preview(client, "I want to build a startup for bookshops", request_id="one")["proposal"]
    conflict = client.post("/entry/chat/preview", json={"text": "I want GTM", "request_id": "one"})
    assert conflict.status_code == 409
    from datetime import timedelta
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow

    with db()() as session:
        row = session.get(EntryChatProposal, first["id"])
        row.expires_at = utcnow() - timedelta(minutes=1)
        row.updated_at = utcnow() - timedelta(days=31)
        session.commit()
    expired = client.post("/entry/chat/confirm", json={"proposal_id": first["id"]})
    assert expired.status_code == 409
    assert expired.json()["detail"]["error"]["code"] == "proposal_expired"
    assert client.get("/ventures").json()["items"] == []


@pytest.mark.parametrize("read", ["history", "get", "answer", "confirm", "name"])
def test_live_pending_proposal_resumes_after_legacy_expiry(client, read):
    from datetime import timedelta
    from app.db.supymem_models import EntryChatProposal
    from app.state import db

    text = "I want to build a startup for bookshops"
    first = _preview(client, text, request_id="resume")["proposal"]
    from app.entry import chat_start

    at = chat_start.utcnow()
    with db()() as session:
        row = session.get(EntryChatProposal, first["id"])
        row.created_at = at - timedelta(days=40)
        row.updated_at = at - timedelta(days=2)
        row.expires_at = at - timedelta(days=1)
        session.commit()
    assert client.get("/entry/chat/history", headers={"X-Subject": "someone_else"}).json()["items"] == []
    with db()() as session:
        assert session.get(EntryChatProposal, first["id"]).expires_at == (at - timedelta(days=1)).replace(tzinfo=None)
    if read == "history":
        assert client.get("/entry/chat/history").status_code == 200
    elif read == "get":
        assert client.get(f"/entry/chat/proposals/{first['id']}").status_code == 200
    elif read == "answer":
        assert _preview(client, text, request_id="resume")["proposal"]["id"] == first["id"]
    elif read == "confirm":
        assert client.post("/entry/chat/confirm", json={"proposal_id": first["id"]}).status_code == 200
    else:
        response = client.post(f"/entry/chat/proposals/{first['id']}/name", json={"request_id": "resume-name"})
        assert response.status_code == 200, response.text
    with db()() as session:
        row = session.get(EntryChatProposal, first["id"])
        assert row.expires_at.replace(tzinfo=at.tzinfo) >= at + timedelta(days=30)
    assert client.post("/entry/chat/confirm", json={"proposal_id": first["id"]}).status_code == 200


def test_history_never_revives_abandoned_or_foreign_proposals(client):
    from datetime import timedelta
    from app.db.supymem_models import EntryChatProposal
    from app.state import db
    from core.clock import utcnow

    first = _preview(client, "I want to build a startup for bookshops")["proposal"]
    expired = utcnow() - timedelta(days=1)
    with db()() as session:
        row = session.get(EntryChatProposal, first["id"])
        row.updated_at = utcnow() - timedelta(days=31)
        row.expires_at = expired
        session.commit()
    assert client.get("/entry/chat/history", headers={"X-Subject": "someone_else"}).json()["items"] == []
    assert client.get("/entry/chat/history").status_code == 200
    with db()() as session:
        assert session.get(EntryChatProposal, first["id"]).expires_at == expired.replace(tzinfo=None)


def test_deferred_question_accepts_only_one_answer(client):
    proposal = _preview(client, "I want to build a startup for bookshops")["proposal"]
    venture_id = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).json()["venture_id"]
    opened = client.post(f"/ventures/{venture_id}/idea/sessions").json()
    from tests.security.test_durable_journey_boundary import run_queued

    run_queued("idea.clarify_turn")
    opened = client.get(f"/ventures/{venture_id}/idea/sessions/{opened['id']}").json()
    question = next(turn for turn in opened["turns"] if turn["kind"] == "question")
    from app.idea.service import post_message

    first = post_message(opened["id"], "Independent bookshops that still manage orders on paper",
                         expected_question_id=question["id"])
    assert first["job_id"]
    with pytest.raises(RuntimeError, match="idea_question_no_longer_pending"):
        post_message(opened["id"], "A second answer", expected_question_id=question["id"])


def test_confirm_recovers_same_venture_after_interrupted_receipt(client, monkeypatch):
    proposal = _preview(client, "I want to build a startup for bookshops")["proposal"]
    from app.credits import accounts

    original = accounts.ensure_account
    calls = 0

    def interrupted_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated interruption after venture commit")
        return original(*args, **kwargs)

    monkeypatch.setattr(accounts, "ensure_account", interrupted_once)
    with pytest.raises(RuntimeError, match="simulated interruption"):
        client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    linked = client.get(f"/entry/chat/proposals/{proposal['id']}").json()["proposal"]
    assert linked["state"] == "committing"
    assert linked["target_venture_id"]
    recovered = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert recovered.status_code == 200, recovered.text
    assert recovered.json()["venture_id"] == linked["target_venture_id"]
    assert len(client.get("/ventures").json()["items"]) == 1


def test_startup_entry_reply_is_pending_and_spoken_by_manager(client):
    proposal = _preview(client, "I want to build a startup for independent bookshops")["proposal"]
    venture_id = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]}).json()["venture_id"]
    reply = client.get(f"/ventures/{venture_id}/chat").json()["items"][-1]
    assert reply["kind"] == "entry_pending"
    assert reply["body_md"].startswith("<!--employee:")
    assert ":Manager:manager-->" in reply["body_md"].split("\n", 1)[0]


def test_personal_as_adjective_is_still_a_startup(client):
    text = ("I want to start a company that helps museum gift shops retain members "
            "by offering personal book recommendations to patrons.")
    assert _preview(client, text)["proposal"]["route"] == "startup"


@pytest.mark.parametrize("idea,answer", [
    ("nearest kiln finder", "i am trying to find the nearest pottery kiln so renters can fire their clay"),
    ("pallet recycler directory", "i am trying to find the closest pallet recycler for warehouse operators"),
])
def test_unclear_idea_offers_choices_and_never_repeats_the_question(client, idea, answer):
    # A short product phrase is enough to start: no clarify question at all.
    first = _preview(client, idea)
    assert first["status"] == "proposal" and first["proposal"]["route"] == "startup"
    vague = _preview(client, "I need help")
    assert vague["status"] == "clarify" and len(vague["onboarding_choices"]) == 3
    # A free-text answer that still doesn't classify must move on, not loop.
    looped = _preview(client, f"I need help\nClarification: {answer}\nClarification: no audience yet")
    assert looped["status"] == "proposal" and looped["proposal"]["route"] == "startup"
    # Category chips are hints, not an invented company idea.
    assert _preview(client, "I need help\nClarification: It's a startup or business idea"
                    )["status"] == "clarify"
    assert _preview(client, "I need help\nClarification: It's a personal project for me"
                    )["status"] == "clarify"


def test_small_talk_rows_can_rewind_without_creating_venture(client):
    for index, text in enumerate(["hello", "hello\nClarification: how are you", "hello\nClarification: how are you\nClarification: ok"]):
        result = _preview(client, text, request_id=f"greeting-{index}")
        assert result["status"] == "clarify"
        cancelled = client.post(f"/entry/chat/proposals/{result['id']}/cancel")
        assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
        assert client.post(f"/entry/chat/proposals/{result['id']}/cancel").json()["status"] == "cancelled"
    assert client.get("/ventures").json()["items"] == []


def test_name_suggestions_vary_and_request_replay_keeps_same_name(client):
    proposal = _preview(client, "A booking calendar for independent tutors")["proposal"]
    names = []
    for index in range(6):
        url = f"/entry/chat/proposals/{proposal['id']}/name"
        response = client.post(url, json={"request_id": f"name-{index}"})
        assert response.status_code == 200, response.text
        name = response.json()["name"]
        assert name not in names and ("calendar" in name.lower() or "booking" in name.lower())
        assert client.post(url, json={"request_id": f"name-{index}"}).json()["name"] == name
        names.append(name)


def _markdown_brief(chars):
    block = ("## Problem\n- Independent bookshops lose customers?\n- We need marketing and to find customers.\n\n"
             "**Solution**: a `shared` loyalty app.\n\n")
    return ("# Shelfmate\n\n" + block * (chars // len(block) + 1))[:chars]


def test_long_markdown_idea_is_understood_and_saved_whole(client):
    brief = _markdown_brief(150000)
    first = _preview(client, brief)
    # Incidental "marketing", "find customers" and "?" in a document are not a GTM/clarify cue.
    assert first["status"] == "proposal" and first["proposal"]["route"] == "startup"
    venture_id = client.post("/entry/chat/confirm", json={"proposal_id": first["proposal"]["id"]}).json()["venture_id"]
    from app import state
    from app.db.models import Venture
    with state.db()() as session:
        assert session.get(Venture, venture_id).raw_idea == brief
    # The first clarification turn accepts the long brief (was capped at 12,000).
    from app.idea import service
    assert service.INITIAL_IDEA_MAX_CHARS >= 200000


def test_idea_over_the_cap_gets_a_friendly_error(client):
    from app.entry.chat_start import IDEA_MAX_CHARS

    assert _preview(client, _markdown_brief(IDEA_MAX_CHARS))["status"] == "proposal"
    response = client.post("/entry/chat/preview", json={"text": _markdown_brief(IDEA_MAX_CHARS + 1)})
    assert response.status_code == 422
    assert "longer than I can read" in response.text


def test_condense_keeps_head_and_tail_with_marker(client):
    from app.intake.smart import condense_input

    text = "A" * 5000 + "M" * 100000 + "Z" * 5000
    out = condense_input(text)
    assert len(out) <= 12000 and out.startswith("AAA") and out.endswith("ZZZ") and "original preserved" in out
    assert condense_input("short") == "short"


@pytest.mark.parametrize("idea,answer", [
    ("I want to build a startup that helps dental clinics fill last-minute appointment cancellations",
     "Clarification: find me dentists in Austin I can email"),
    ("something for yoga teachers idk", "Audience: find me 10 yoga studios in Denver that I can email"),
])
def test_customer_request_during_onboarding_is_a_gtm_skip_offer_not_an_answer(client, idea, answer):
    first = _preview(client, idea)
    pending = first.get("proposal") or {}
    asked = first.get("onboarding_question") or pending.get("onboarding_question")
    assert asked or (pending.get("state") == "pending" and pending.get("route") == "startup"
                     and not pending.get("venture_name") and pending.get("message")), first
    assert client.get("/ventures").json()["items"] == []
    reply = _preview(client, f"{idea}\n{answer}")
    assert reply["status"] == "proposal", reply
    proposal = reply["proposal"]
    assert proposal["route"] == "gtm" and proposal["next_step"] == "gtm"
    assert "finding customers" in proposal["message"] and not proposal.get("onboarding_question")
    assert "You mentioned" not in proposal["message"]
    committed = client.post("/entry/chat/confirm", json={"proposal_id": proposal["id"]})
    assert committed.status_code == 200, committed.text
    assert committed.json()["route"] == "gtm"
