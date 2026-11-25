"""Real temporary SQLite slot custody and safe output; no model/provider I/O."""
from datetime import timedelta
import json

import pytest

from tests.integration.test_postgres_auth_security import service_modules


@pytest.fixture()
def admitted(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/admission.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    with service_modules("company_os"):
        from app.db.models import Person, Venture, Workspace, WorkspaceMember
        from app.db.supymem_models import EntryChatProposal
        from app.intake import admission
        from app.state import db
        from core.clock import utcnow

        now, factory = utcnow(), db()
        with factory() as session:
            session.add_all([Person(id="actor_a", display_name="A", created_at=now),
                             Person(id="actor_b", display_name="B", created_at=now)])
            session.flush()
            session.add_all([Workspace(id="ws_a", kind="company", name="A", entry_mode="idea", owner_id="actor_a", created_at=now, updated_at=now),
                             Workspace(id="ws_b", kind="company", name="Borrowed", entry_mode="idea", owner_id="actor_a", created_at=now, updated_at=now)])
            session.flush()
            session.add(WorkspaceMember(id="member_a", workspace_id="ws_a", person_id="actor_a", role="viewer", created_at=now))
            session.add(Venture(id="ven_a", workspace_id="ws_a", name="A", raw_idea="A calendar", stage="idea", created_at=now, updated_at=now))
            for ident, target, state, metadata in [
                ("ecp_existing", "ven_a", "classifying", {"created_new": False, "original_source": "Saved idea"}),
                ("ecp_new", None, "classifying", {"created_new": True, "original_source": "Saved idea"}),
                ("ecp_name", None, "pending", {"created_new": True, "chosen_name": "Keep me", "name_requests": {"original:one": {"state": "calling"}}}),
            ]:
                session.add(EntryChatProposal(id=ident, actor_id="actor_a", request_id=ident + ":browser",
                    text="A calendar for tutors", route="startup", target_venture_id=target, skipped_steps_json="[]",
                    message="Review", next_step="idea", state=state, receipt_json=json.dumps(metadata),
                    created_at=now, updated_at=now, expires_at=now + timedelta(hours=24)))
            session.commit()
        yield admission, factory, now


def _args(proposal="ecp_existing", *, name=False, workspace="ws_a", actor="actor_a"):
    return {"actor_id": actor, "workspace_id": workspace,
            "purpose": "intake.suggest_name" if name else "intake.classify",
            "request_id": f"entry:{proposal}:name:original:one" if name else f"entry:{proposal}:classify"}


def _decision():
    from app.intake.smart import classify_fallback
    return classify_fallback("A calendar for tutors").model_dump(mode="json")


@pytest.mark.parametrize("text,question,route,name", [
    ("A tool to organize meal plans.", None, "startup", ""),
    ("Tern Invoice Desk: payment reminders for freelance designers in the UK.", None, "startup", "Tern Invoice Desk"),
    ("BrewBox: coffee subscriptions. We have 140 paying subscribers. Our site is https://brewbox.example.com", None, "gtm", "BrewBox"),
])
def test_entry_onboarding_answers_only_change_the_display_question(admitted, monkeypatch, text, question, route, name):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    monkeypatch.setattr(chat_start, "_assert_can_start", lambda _: None)
    monkeypatch.setattr(chat_start, "_named_existing_target", lambda *_: (None, None))
    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    result = chat_start.preview(text=text, actor_id="actor_a", request_id="onboarding",
        intake=classify_fallback(text).to_dict())
    assert result["status"] == "proposal"
    assert result["proposal"]["route"] == route
    assert result["proposal"]["state"] == "pending"
    assert result["proposal"]["next_step"] == ("idea" if route == "startup" else "gtm")
    assert result["proposal"].get("onboarding_question") == question
    assert chat_start.preview(text=text, actor_id="actor_a", request_id="onboarding",
        intake=classify_fallback(text).to_dict()) == result
    assert chat_start.get(proposal_id=result["proposal"]["id"], actor_id="actor_a") == result
    assert chat_start.history(actor_id="actor_a")["items"][-1]["proposal"] == result["proposal"]
    assert result["proposal"]["venture_name"] == name


def test_smalltalk_stays_with_idea_until_clarified_or_unsure(admitted, monkeypatch):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    from app.intake.smart import DISCOVERY_FOLLOWUP, DISCOVERY_QUESTION
    for text, question in [("hello", "Happy to help, what would you like to work on?"),
                           ("hello\nClarification: Not sure", DISCOVERY_QUESTION)]:
        result = chat_start.preview(text=text, actor_id="actor_a", request_id=text,
            intake=classify_fallback(text).to_dict())
        assert result["status"] == "clarify"
        assert result["onboarding_question"] == question
        saved = chat_start.get(result["id"], "actor_a")["proposal"]
        assert saved["route"] == saved["state"] == "clarify"
        assert saved["message"] == result["question"]
        assert saved["onboarding_question"] == question
        assert chat_start.history("actor_a")["items"][-1]["proposal"] == saved
    for interest in ("childcare", "fleet maintenance"):
        text = f"I'm not sure, but I'm interested in {interest}."
        exploring = classify_fallback(text).to_dict()
        assert exploring["path"] == "clarify" and not exploring["extracted"]["idea"]
        assert chat_start._onboarding_question(exploring, text) == DISCOVERY_FOLLOWUP


@pytest.mark.parametrize("text", [
    "I don't have any idea yet, I just want to make money on the side",
    "we havent got a business idea, we only know we'd like to work for ourselves",
    "No ideas so far. Teaching pays badly and I would like something of my own.",
    "I've got no clue what to do, I'd like a second income",
    "Nothing in mind yet, but I'm ready to leave my job",
    "Looking for business ideas",
])
def test_founder_without_an_idea_is_asked_about_themselves(admitted, monkeypatch, text):
    from app.entry import chat_start
    from app.intake.smart import BACKGROUND_QUESTION, DISCOVERY_QUESTION, classify_fallback

    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    result = chat_start.preview(text=text, actor_id="actor_a", request_id=text, intake=classify_fallback(text).to_dict())
    assert result["status"] == "clarify"
    assert result["onboarding_question"] in {DISCOVERY_QUESTION, BACKGROUND_QUESTION}  # background they gave isn't asked for again


@pytest.mark.parametrize("text,question", [
    ("A tool that reconciles shipment invoices", "Who is it for?"),
    ("An app that plans weekend walking routes", "Who is it for?"),
    ("I have no clue how to price my app that reminds tenants to pay rent", None),
    ("I want to build an app", "What would it actually do? Describe one task it would help with."),
    ("A hiring tool that scores applicants, recruiters have no idea which candidates ghost", "Who is it for?"),
    ("I need an idea validation tool for students", "What would it actually do? Describe one task it would help with."),
])
def test_founder_with_an_idea_keeps_normal_questions(admitted, text, question):
    from app.entry import chat_start
    from app.intake.smart import classify_fallback

    assert chat_start._onboarding_question(classify_fallback(text).to_dict(), text) == question


def _no_idea_step(question):
    return {"path": "clarify", "confidence": 0.9, "extracted": {"idea": "", "language": "en"}, "founder_line": question,
            "button_label": "Continue", "next_step": {"situation": "no_idea", "action": "ask", "question": question,
                "field": "context", "examples": ["I run a small farm", "I fix old radios"]}}


@pytest.mark.parametrize("text", ["I want to start a business", "Saving up to be my own boss by next summer"])
def test_the_model_reads_a_goal_with_no_product_and_keeps_its_question(admitted, monkeypatch, text):
    import asyncio
    from app.entry import chat_start
    from app.intake import smart

    async def model(*_args, **_kwargs):
        return _no_idea_step("What do you know well from your work?")

    monkeypatch.setattr(smart, "_call_model", model)
    for turn in (text, text + "\nClarification: I repair radios"):
        intake = asyncio.run(smart.classify(turn, actor_id="actor_a")).to_dict()
        assert chat_start._onboarding_question(intake, turn) == "What do you know well from your work?"


@pytest.mark.parametrize("text", ["hello", "thanks so much", "asdf qwer"])
def test_small_talk_still_spends_no_model_call(admitted, monkeypatch, text):
    import asyncio
    from app.intake import smart

    async def model(*_args, **_kwargs):
        raise AssertionError("small talk must not reach the model")

    monkeypatch.setattr(smart, "_call_model", model)
    assert asyncio.run(smart.classify(text, actor_id="actor_a")).path == "clarify"


@pytest.mark.parametrize("offer,audience", [
    ("A tool that reconciles shipment invoices", "Freight dispatch teams"),
    ("An app that plans weekend walking routes", "Families exploring local trails"),
])
def test_unsure_smalltalk_discovers_until_a_useful_idea_is_answered(admitted, monkeypatch, offer, audience):
    from app.entry import chat_start
    from app.intake.smart import DISCOVERY_QUESTION, classify_fallback

    monkeypatch.setattr(chat_start, "_assert_can_start", lambda _: None)
    monkeypatch.setattr(chat_start, "_named_existing_target", lambda *_: (None, None))
    monkeypatch.setattr(chat_start, "record_preview", lambda *_: None)
    at = chat_start.utcnow()
    monkeypatch.setattr(chat_start, "utcnow", lambda: at)
    text = "hello"
    for answer, question in [
        (None, "Happy to help, what would you like to work on?"),
        ("\nClarification: Not sure", DISCOVERY_QUESTION),
        ("\nClarification: " + offer, "Who is it for?"),
        ("\nClarification: Not sure", "Who is it for?"),
        ("\nAudience: " + audience, None),
    ]:
        text += answer or ""
        intake = classify_fallback(text).to_dict()
        result = chat_start.preview(text=text, actor_id="actor_a", request_id=text, intake=intake)
        proposal = result["proposal"] if result["status"] == "proposal" else result
        saved = chat_start.get(proposal["id"], "actor_a")["proposal"]
        assert chat_start._onboarding_question(intake, text) == question
        naming = saved["route"] == "startup" and saved["state"] == "pending" and not saved.get("venture_name")
        assert saved["onboarding_question"] == (None if naming else question)
        if naming:
            assert saved["message"] == "What should we call it?"
        assert chat_start.preview(text=text, actor_id="actor_a", request_id=text, intake=intake) == result
        assert chat_start.history("actor_a")["items"][-1]["proposal"] == saved
        assert saved["receipt"] is None and saved["target_venture_id"] is None
        if question == DISCOVERY_QUESTION:
            assert result["status"] == saved["state"] == "clarify"
            assert saved["onboarding_field"] == "context" and len(saved["onboarding_choices"]) == 3
        if answer and offer in text:
            assert intake["extracted"]["idea"] == offer


@pytest.mark.parametrize("recovery", ["absent", "confirmed", "unknown"])
async def test_enabled_dispatch_preserves_original_recovery(admitted, monkeypatch, recovery):
    from app.credits import metered
    from app.intake import model_capture, smart
    from llm_gateway import sole
    from llm_gateway.errors import ProviderCallError

    admission, _, _ = admitted
    args = _args()
    output = _decision()
    if recovery == "confirmed":
        admission.checkpoint(**args, output=output)

    def recover(key):
        if recovery == "unknown":
            raise ValueError("original receipt is unknown")
        return {"outcome": "confirmed", "output_usable": True} if recovery == "confirmed" else None

    # Capture recovery is opt-in; disabled callers retain main's legacy path.
    monkeypatch.setenv("COS_INTAKE_CAPTURE_ENABLED", "1")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("SANDBOX_MODE", "0")
    monkeypatch.setattr(metered, "recover_model_call", recover)
    monkeypatch.setattr(metered, "call", lambda *a, **k: pytest.fail("reserved while fenced"))
    monkeypatch.setattr(model_capture, "capture_request", lambda *a, **k: pytest.fail("quoted while fenced"))
    monkeypatch.setattr(sole, "transmit", lambda *a, **k: pytest.fail("dispatched while fenced"))
    if recovery == "unknown":
        with pytest.raises(ProviderCallError, match="reconciliation"):
            await smart._metered_json([], max_tokens=900, validate_output=lambda data: data, **args)
    elif recovery == "confirmed":
        result = await smart._metered_json([], max_tokens=900, validate_output=lambda data: data, **args)
        assert result == output
    else:
        assert smart.recover_original_json(**args) is None


def test_original_target_dispatch_and_checkpoint_preserve_metadata_and_public_state(admitted):
    from app.db.supymem_models import EntryChatProposal
    admission, factory, _ = admitted
    args, output = _args(), _decision()
    assert admission.identity(**args)["target_venture_id"] == "ven_a"
    assert admission.validate_before_dispatch(**args)["workspace_id"] == "ws_a"
    assert admission.read(**args) == {"available": False, "output": None}
    with factory() as session:
        old = session.get(EntryChatProposal, "ecp_existing").updated_at
    assert admission.checkpoint(**args, output=output) == {"available": True, "output": output}
    assert admission.checkpoint(**args, output=output) == admission.read(**args)
    with factory() as session:
        row = session.get(EntryChatProposal, "ecp_existing")
        assert row.state == "classifying" and row.updated_at == old and row.target_venture_id == "ven_a"
        assert json.loads(row.receipt_json)["original_source"] == "Saved idea"
    with pytest.raises(admission.AdmissionError):
        admission.validate_before_dispatch(**args)
    with pytest.raises(admission.AdmissionError):
        admission.checkpoint(**args, output=None)


def test_null_scope_never_borrows_an_accessible_workspace_or_binds_tenancy(admitted):
    from app.tenancy import bind, current_or_none
    admission, _, _ = admitted
    args = _args("ecp_new", workspace=None)
    with bind(workspace_id="none", venture_id="", actor="actor_a"):
        before = current_or_none()
        assert admission.validate_before_dispatch(**args)["workspace_id"] is None
        assert admission.checkpoint(**args, output=None) == {"available": True, "output": None}
        assert admission.read(**args) == {"available": True, "output": None}
        assert current_or_none() == before
    with pytest.raises(admission.AdmissionError):
        admission.identity(**_args("ecp_new", workspace="ws_b"))


@pytest.mark.parametrize("purpose,nonce", [
    ("intake.classify", "entry:ecp_existing:classify:extra"),
    ("intake.classify", "entry:ecp_existing:name:original:one"),
    ("intake.suggest_name", "entry:ecp_name:classify"),
    ("intake.suggest_name", "entry:ecp_name:name:"),
    ("other", "entry:ecp_existing:classify"),
])
def test_nonce_is_exact_and_purpose_bound(admitted, purpose, nonce):
    admission, _, _ = admitted
    with pytest.raises(admission.AdmissionError):
        admission.parse_request_id(purpose, nonce)


@pytest.mark.parametrize("change", ["actor", "workspace", "unknown_slot"])
def test_wrong_actor_scope_and_unclaimed_name_slot_cannot_checkpoint(admitted, change):
    admission, _, _ = admitted
    args = _args()
    if change == "actor":
        args["actor_id"] = "actor_b"
    elif change == "workspace":
        args["workspace_id"] = "ws_b"
    else:
        args = _args("ecp_name", name=True, workspace=None)
        args["request_id"] = "entry:ecp_name:name:not-claimed"
    with pytest.raises(admission.AdmissionError):
        admission.checkpoint(**args, output=None)


@pytest.mark.parametrize("state", ["pending", "clarify", "committing", "committed", "cancelled"])
def test_saved_classification_output_remains_readable_without_fresh_dispatch(admitted, state):
    from app.db.supymem_models import EntryChatProposal
    admission, factory, _ = admitted
    args, output = _args(), _decision()
    admission.checkpoint(**args, output=output)
    with factory() as session:
        session.get(EntryChatProposal, "ecp_existing").state = state
        session.commit()
    assert admission.read(**args)["output"] == output
    with pytest.raises(admission.AdmissionError):
        admission.validate_before_dispatch(**args)


def test_financial_identity_and_checkpoint_survive_revocation_but_output_read_does_not(admitted):
    from app.db.models import Workspace, WorkspaceMember
    admission, factory, _ = admitted
    args, output = _args(), _decision()
    with factory() as session:
        session.get(Workspace, "ws_a").owner_id = "actor_b"
        session.delete(session.get(WorkspaceMember, "member_a"))
        session.commit()
    assert admission.identity(**args)["actor_id"] == "actor_a"
    assert admission.checkpoint(**args, output=output)["available"] is True
    with pytest.raises(admission.AdmissionError):
        admission.read(**args)
    with pytest.raises(admission.AdmissionError):
        admission.validate_before_dispatch(**args)


@pytest.mark.parametrize("change", ["expired", "complete_slot", "target", "committed"])
def test_name_dispatch_requires_original_calling_slot_on_live_new_proposal(admitted, change):
    from app.db.supymem_models import EntryChatProposal
    admission, factory, now = admitted
    args = _args("ecp_name", name=True, workspace=None)
    assert admission.validate_before_dispatch(**args)["target_venture_id"] is None
    with factory() as session:
        row = session.get(EntryChatProposal, "ecp_name")
        metadata = json.loads(row.receipt_json)
        if change == "expired":
            row.expires_at = now - timedelta(seconds=1)
        elif change == "complete_slot":
            metadata["name_requests"]["original:one"]["state"] = "complete"
        elif change == "target":
            row.target_venture_id = "ven_a"
        else:
            row.state = "committed"
        row.receipt_json = json.dumps(metadata)
        session.commit()
    with pytest.raises(admission.AdmissionError):
        admission.validate_before_dispatch(**args)


def test_original_null_scope_survives_confirmation_link_and_metadata_replacement(admitted):
    from app.db.supymem_models import EntryChatProposal
    admission, factory, _ = admitted
    args = _args("ecp_name", name=True, workspace=None)
    output = {"name": "Orbit Tasks"}
    admission.checkpoint(**args, output=output)
    with factory() as session:
        row = session.get(EntryChatProposal, "ecp_name")
        records = json.loads(row.receipt_json)[admission.OUTPUTS_KEY]
        row.state = "committed"
        row.target_venture_id = "ven_a"
        row.receipt_json = json.dumps({"created_new": True, "status": "committed", admission.OUTPUTS_KEY: records})
        session.commit()
    assert admission.read(**args) == {"available": True, "output": output}
    assert admission.record_read(**args) == admission.read(**args)


@pytest.mark.parametrize("output", [{"name": "Orbit Tasks", "usage": {}}, {"schema": {}}, "raw provider text"])
def test_checkpoint_accepts_only_domain_json_or_known_bad_marker(admitted, output):
    admission, _, _ = admitted
    args = _args("ecp_name", name=True, workspace=None)
    with pytest.raises(admission.AdmissionError):
        admission.checkpoint(**args, output=output)
    assert admission.read(**args) == {"available": False, "output": None}


def test_record_binding_is_checked_and_metadata_cas_never_reports_a_lost_write(admitted, monkeypatch):
    from types import SimpleNamespace
    from sqlalchemy.orm import Session
    from app.db.supymem_models import EntryChatProposal
    admission, factory, _ = admitted
    args = _args("ecp_name", name=True, workspace=None)
    original = Session.execute
    def refuse(session, statement, *a, **kw):
        if statement.is_update:
            return SimpleNamespace(rowcount=0)
        return original(session, statement, *a, **kw)
    with monkeypatch.context() as patch:
        patch.setattr(Session, "execute", refuse)
        with pytest.raises(admission.AdmissionError):
            admission.checkpoint(**args, output={"name": "Orbit Tasks"})
    assert admission.read(**args)["available"] is False
    admission.checkpoint(**args, output={"name": "Orbit Tasks"})
    with factory() as session:
        row = session.get(EntryChatProposal, "ecp_name")
        metadata = json.loads(row.receipt_json)
        metadata[admission.OUTPUTS_KEY][args["request_id"]]["actor_id"] = "actor_b"
        row.receipt_json = json.dumps(metadata)
        session.commit()
    with pytest.raises(admission.AdmissionError):
        admission.read(**args)
