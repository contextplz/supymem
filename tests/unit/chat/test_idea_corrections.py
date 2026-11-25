import pytest

from tests.integration.test_chat_first_idea_bridge import (
    _chat_started_venture,
    _ideas,
)
from tests.integration.test_chat_first_idea_bridge import (
    client as client,  # noqa: PLC0414
)

DOMAINS = [
    ("A tool that triages alerts for risk teams", "Heads of fraud operations at mid-size banks"),
    ("Monitoring software for pipeline methane leaks", "Integrity engineers at gas pipeline operators"),
]


def _say(client, venture_id, text):
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": text})
    assert sent.status_code == 200, sent.text
    return sent.json()


@pytest.mark.parametrize("audience,excluded,boundary", [
    ("repair shop managers with multiple locations", "home mechanics", "Keep service records private"),
    ("apartment gardeners with shaded balconies", "commercial growers", "Do not share your home address"),
])
@pytest.mark.parametrize("directive", ["start with", "begin with"])
def test_marked_starting_audience_is_saved_without_starting_work(client, audience, excluded, boundary, directive):
    from app.chat.idea_corrections import parse
    from app.db.models import Job
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.state import db
    from sqlalchemy import select

    venture_id = _chat_started_venture(client, f"A planning tool for {audience}", field="workflow")
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"workflow": "You keep a private daily log", "constraints": [boundary]},
                            venture_id=venture_id)
    text = f"Correction: {directive} {audience}, not {excluded}. {boundary}."
    assert parse(text, before["structured"]) == {"customer": audience}
    assert parse(f"{directive.capitalize()} {audience}.", before["structured"]) is None
    reply = _say(client, venture_id, text)
    current = get_session(saved.id)["current_revision"]
    assert current["structured"]["customer"] == audience
    assert current["structured"]["workflow"] == before["structured"]["workflow"]
    assert boundary in current["structured"]["constraints"]
    assert f"Do not include {excluded}" in current["structured"]["constraints"]
    assert "Updated" in reply["body_md"] and audience in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert not any(turn["role"] == "founder" and turn["text"] == text for turn in get_session(saved.id)["turns"])
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before
    with db()() as session:
        assert not any(job.kind.startswith(("research.", "experiment.", "build.")) for job in
                       session.scalars(select(Job).where(Job.venture_id == venture_id)))


@pytest.mark.parametrize("audience,task", [
    ("repair shop managers", "organize service notes"),
    ("apartment gardeners", "track watering schedules"),
])
def test_use_my_saved_summary_offers_review_without_mutation_or_work(client, monkeypatch, audience, task):
    from app.chat import idea_corrections
    from app.db.models import Job
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.state import db
    from app.workspaces import service as workspaces
    from sqlalchemy import select

    venture_id = _chat_started_venture(client, f"A tool for {audience} to {task}", pending=False)
    saved, _, _ = _ideas(venture_id)
    summary = f"You help {audience} {task}. You have not tested demand or chosen a price."
    _say(client, venture_id, f"My own summary: {summary}")
    before = get_session(saved.id)
    revisions = list_revisions(saved.id)
    other_id = _chat_started_venture(client, "An app for amateur pianists to record practice sessions", pending=False)
    other, _, _ = _ideas(other_id)
    patch_revision(other.id, {"summary": "A different conversation's summary"}, venture_id=other_id)
    with db()() as session:
        jobs = set(session.scalars(select(Job.id)))
    reply = _say(client, venture_id, "Use my summary.")
    assert summary in reply["body_md"] and "saved" in reply["body_md"].casefold()
    assert "A different conversation's summary" not in reply["body_md"]
    assert reply["actions"] == [{"label": "Show my idea review here", "action": "idea.review", "style": "ghost"}]
    assert not any(action.get("confirmation") for action in reply["actions"])
    after = get_session(saved.id)
    assert after["current_revision"] == before["current_revision"] and after["state"] == before["state"]
    assert after["turns"] == before["turns"] and list_revisions(saved.id) == revisions
    with db()() as session:
        assert set(session.scalars(select(Job.id))) == jobs
    monkeypatch.setattr(workspaces, "role_of", lambda *args: "viewer")
    assert idea_corrections.apply(venture_id, "Use my summary.", "usr_alice") is None


@pytest.mark.parametrize("audience,excluded,pain", [
    ("repair shop managers with multiple locations", "home mechanics", "lost service notes delay estimates"),
    ("apartment gardeners with shaded balconies", "commercial growers", "forgotten watering schedules damage seedlings"),
])
@pytest.mark.parametrize("field", ["workflow", "problem"])
def test_marked_scope_does_not_answer_the_pending_question(client, audience, excluded, pain, field):
    import json

    from app.chat.idea_corrections import parse
    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.state import db

    venture_id = _chat_started_venture(client, f"A planning tool for {audience}", field=field)
    saved, turns, _ = _ideas(venture_id)
    with db()() as session:
        question = session.get(ClarifierTurn, turns[-1].id)
        assert question.kind == "question"
        question.options = json.dumps({"field": field})
        question.text = "What problem would you solve?" if field == "problem" else "How would you make it work?"
        session.commit()
    before = patch_revision(saved.id, {"problem": pain, "workflow": "You keep a private daily log",
        "constraints": ["Keep your records private"]}, venture_id=venture_id)
    text = f"Correction: {audience}, not {excluded}. Team leads champion it and the household or business owner approves spending."
    assert parse(text, before["structured"])["customer"] == audience
    reply = _say(client, venture_id, text)
    current = get_session(saved.id)["current_revision"]
    assert current["structured"]["customer"] == audience
    assert current["structured"]["problem"] == pain
    assert current["structured"]["workflow"] == before["structured"]["workflow"]
    assert "Keep your records private" in current["structured"]["constraints"]
    assert any(excluded in limit for limit in current["structured"]["constraints"])
    assert "Updated" in reply["body_md"]
    assert not any(turn["role"] == "founder" and text == turn["text"] for turn in get_session(saved.id)["turns"])
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before
    unresolved = _say(client, venture_id, "Correction: that isn't the direction I meant.")
    assert "which" in unresolved["body_md"].casefold()
    assert get_session(saved.id)["current_revision"]["id"] == current["id"]


@pytest.mark.parametrize("audience,task", [
    ("repair shop managers", "organize service notes"),
    ("apartment gardeners", "track watering schedules"),
])
def test_own_summary_after_founder_answer_is_reviewable(client, audience, task):
    from app.core.fsm import advance
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.idea.service import get_session, list_revisions
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    venture_id = _chat_started_venture(client, f"A tool for {audience} to {task}", pending=False)
    saved, turns, _ = _ideas(venture_id)
    before = get_session(saved.id)["current_revision"]
    with db()() as session:
        row = session.get(IdeaSession, saved.id)
        if row.state == "clarifying":
            advance(session, row, "proposing")
            advance(session, row, "reviewing")
        session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id,
            venture_id=venture_id, turn=turns[-1].turn + 1, role="founder", kind="answer", text="You can keep it private",
            revision_id=before["id"], created_at=utcnow()))
        session.commit()
    summary = f"You would help {audience} {task} with a private weekly checklist."
    reply = _say(client, venture_id, f"My own summary: {summary}")
    current = get_session(saved.id)["current_revision"]
    assert current["structured"]["summary"] == summary
    assert current["id"] != before["id"]
    assert reply["kind"] != "idea_recovery"
    assert "could not prepare" not in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])
    proposal = get_session(saved.id)["turns"][-1]
    assert proposal["role"] == "agent" and proposal["kind"] == "proposal"
    assert proposal["revision_id"] == current["id"]
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("audience,task,pain", [
    ("warehouse managers", "reconcile delivery notes", "incomplete delivery records delay supplier payments by 2.5 days"),
    ("amateur pianists", "track practice sessions", "forgotten practice goals waste 1.5 hours every week"),
])
def test_own_summary_replaces_only_the_explicitly_stated_problem(client, audience, task, pain):
    from app.chat.idea_corrections import parse
    from app.db.models import Venture
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.state import db
    from app.ventures.current_idea import current_revision, revision_fields

    offer = f"A tool that helps {audience} {task}"
    venture_id = _chat_started_venture(client, offer)
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"problem": "An outdated assumption about the problem",
        "offer": offer, "customer": audience}, venture_id=venture_id)
    summary = f"You would build a tool for {audience}. Their problem is that {pain}. You have not tested demand."
    assert parse(f"My own summary: {summary}", before["structured"])["problem"] == pain
    assert "problem" not in parse(f"My own summary: {offer}.", before["structured"])
    reply = _say(client, venture_id, f"My own summary: {summary}")
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    assert current["structured"]["problem"] == pain
    assert current["structured"]["summary"] == summary
    assert current["structured"]["offer"] == offer
    assert current["structured"]["customer"] == audience
    assert pain in reply["body_md"]
    assert before["structured"]["problem"] not in reply["body_md"]
    with db()() as session:
        assert revision_fields(current_revision(session.get(Venture, venture_id), session))["problem"] == pain
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("audience,narrowed", [
    ("costume workshop coordinators", "costume workshop coordinators at small touring companies"),
    ("home aquarium keepers", "home aquarium keepers in rented flats"),
])
@pytest.mark.parametrize("field,directive,answer", [
    ("pricing_hypothesis", "focus on", "I haven't chosen a price yet."),
    ("requirements", "narrow the customer to", "It must keep change notes offline."),
])
def test_audience_narrowing_before_another_answer_saves_separate_fields(client, audience, narrowed, field, directive, answer):
    import json

    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import get_session, list_revisions
    from app.state import db

    from tests.security.test_durable_journey_boundary import run_queued

    venture_id = _chat_started_venture(client, f"A checklist tool for {audience}", field=field)
    saved, turns, _ = _ideas(venture_id)
    before = get_session(saved.id)["current_revision"]
    with db()() as session:
        question = session.get(ClarifierTurn, turns[-1].id)
        question.text = "What price would you try?" if field == "pricing_hypothesis" else "What must your checklist do?"
        question.options = json.dumps({"field": field})
        session.commit()
    text = f"Actually, {directive} {narrowed}, rather than {audience}. {answer}"
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": text,
        "reply_to_question": True, "question_id": turns[-1].id})
    assert sent.status_code == 200, sent.text
    current = get_session(saved.id)["current_revision"]
    assert current["structured"]["customer"] == narrowed
    assert "Updated" in sent.json()["body_md"] and narrowed in sent.json()["body_md"]
    assert any(turn["role"] == "founder" and turn["text"] == answer for turn in get_session(saved.id)["turns"])
    assert current["structured"]["_answered_fields"][field] == answer
    run_queued("idea.clarify_turn")
    current = get_session(saved.id)["current_revision"]
    assert current["structured"]["customer"] == narrowed
    assert narrowed not in str(current["structured"][field])
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("audience,description", [
    ("costume workshop coordinators", "You coordinate garment alterations before each touring performance."),
    ("home aquarium keepers", "You record feeding schedules and water changes for your tanks."),
])
def test_own_summary_replaces_review_heading_without_overwriting_offer(client, audience, description):
    import json

    from app.chat.idea_proposal import EDIT_QUESTION
    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.state import db
    from sqlalchemy import event

    venture_id = _chat_started_venture(client, f"A checklist tool for {audience}")
    saved, turns, _ = _ideas(venture_id)
    with db()() as session:
        question = session.get(ClarifierTurn, turns[-1].id)
        question.text = EDIT_QUESTION
        question.options = json.dumps({"field": "offer"})
        session.commit()
    before = patch_revision(saved.id, {"summary": "An earlier description of your checklist.",
        "offer": "A private checklist with dated entries", "customer": audience}, venture_id=venture_id)
    summary = f"{description} You have not tested demand or set a fee."
    writes = []
    engine = db().kw["bind"]

    def record_write(connection, cursor, statement, parameters, context, executemany):
        if statement.lstrip().upper().startswith("UPDATE IDEA_REVISIONS "):
            writes.append(statement)

    event.listen(engine, "before_cursor_execute", record_write)
    try:
        reply = _say(client, venture_id, f"Please use my summary: {summary} Save this as my summary.")
    finally:
        event.remove(engine, "before_cursor_execute", record_write)
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    assert current["structured"]["summary"] == summary
    assert current["structured"]["offer"] == before["structured"]["offer"]
    assert current["structured"]["customer"] == audience
    assert not any(turn["role"] == "founder" and summary in turn["text"] for turn in get_session(saved.id)["turns"])
    assert f"- **Summary:** {summary}" in reply["body_md"]
    assert before["structured"]["summary"] not in reply["body_md"]
    assert "Save this as my summary" not in reply["body_md"]
    review = _say(client, venture_id, "Skip the questions")
    assert summary in review["body_md"]
    assert before["structured"]["summary"] not in review["body_md"]
    assert get_session(saved.id)["current_revision"]["structured"]["offer"] == before["structured"]["offer"]
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before
    assert not writes


@pytest.mark.parametrize("audience,task,recipient", [
    ("regional museum collection managers", "prevent damage by preparing condition records", "insurance assessors"),
    ("first-time dog owners", "understand health changes by assembling daily observations", "veterinary nurses"),
])
def test_own_summary_keeps_the_beneficiary_not_the_downstream_recipient(client, audience, task, recipient):
    from app.chat.idea_corrections import parse
    from app.idea.service import get_session, list_revisions, patch_revision
    from app.intake.smart import _prefill

    venture_id = _chat_started_venture(client, f"A private journal for {audience}")
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"customer": audience}, venture_id=venture_id)
    summary = f"You help {audience} {task} for {recipient}. You can inspect every record privately."
    assert _prefill(summary, grounded_customer=audience)["customer"] == audience
    reply = _say(client, venture_id, f"My own summary: {summary}")
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    assert current["structured"]["customer"] == audience
    assert current["structured"]["summary"] == summary
    assert current["structured"]["offer"] == before["structured"]["offer"]
    assert f"- **Customer:** {audience}\n" in reply["body_md"]
    assert f"- **Customer:** {recipient}" not in reply["body_md"]
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before
    explicit = parse(f"My summary: My customer is {recipient}. {summary}", {"customer": audience})
    assert explicit["customer"] == recipient


@pytest.mark.parametrize("audience,task", [
    ("regional museum collection managers", "track object condition before arranging exhibition loans"),
    ("first-time dog owners", "record daily routines before choosing training exercises"),
])
@pytest.mark.parametrize("directive", ["Use my own summary", "Please apply my summary", "Replace my summary with"])
def test_summary_replacement_directive_saves_and_receipts_the_replacement(client, audience, task, directive):
    from app.chat.idea_corrections import parse
    from app.idea.service import get_session, list_revisions, patch_revision

    venture_id = _chat_started_venture(client, f"A private journal for {audience}")
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"customer": audience, "offer": "An earlier planning assumption"}, venture_id=venture_id)
    summary = f"You help {audience} {task}. There are no users or validated outcomes yet."
    text = f"{directive}: {summary}"
    assert parse(text, before["structured"])["summary"] == summary
    reply = _say(client, venture_id, text)
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    assert current["structured"]["summary"] == summary
    assert current["structured"]["offer"] == before["structured"]["offer"]
    assert current["structured"]["customer"] == audience
    assert f"- **Summary:** {summary}" in reply["body_md"]
    assert "- **What it does:**" not in reply["body_md"].split("\n\nOpen Idea", 1)[0]
    assert not any(action.get("confirmation") for action in reply["actions"])
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("audience,mechanism,replacement", [
    ("hotel purchasing directors", "that notify reception teams about missing linens", "hotel general managers"),
    ("amateur birdwatchers", "which show families nearby nesting trails", "weekend nature photographers"),
])
def test_own_summary_preserves_customer_when_product_clause_extends_it(audience, mechanism, replacement):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat.idea_corrections import parse

    summary = f"Build a private organizer for {audience} {mechanism}. Keep location records private."
    patch = parse(f"My summary: {summary}", {"customer": audience})
    assert patch["customer"] == audience
    assert patch["summary"] == summary
    assert "offer" not in patch
    explicit = parse(f"My summary: My customer is {replacement}. Build a private organizer.", {"customer": audience})
    assert explicit["customer"] == replacement
    assert parse(f"Actually, my customer is {audience} who work independently.", {"customer": audience})["customer"] == f"{audience} who work independently"


@pytest.mark.parametrize("audience,task", [
    ("inventory managers at independent bookstores", "reconcile supplier invoices to replenish shelves accurately"),
    ("pet owners in shared apartments", "coordinate feeding routines to prevent missed meals"),
])
def test_own_summary_saves_the_grounded_audience_not_its_task(client, audience, task):
    from app.idea.service import get_session, list_revisions, patch_revision

    venture_id = _chat_started_venture(client, f"A planning app for {audience}")
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"customer": audience}, venture_id=venture_id)
    summary = f"We help {audience} {task}. You can review the plan privately."
    reply = _say(client, venture_id, f"My own summary: {summary}")
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    assert current["structured"]["customer"] == audience
    assert current["structured"]["summary"] == summary
    assert current["structured"]["offer"] == before["structured"]["offer"]
    assert f"- **Customer:** {audience}\n" in reply["body_md"]
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("audience,path,pricing", [
    ("inventory managers at independent bookstores", "a walkthrough with Jordan Reed at Paper Lantern Books",
     "a proposed monthly fee per store"),
    ("pet owners in shared apartments", "a usability session with Casey Morgan from my neighborhood",
     "an untested one-time purchase per household"),
])
def test_explicit_correction_saves_all_stated_fields_in_one_revision(client, audience, path, pricing):
    from app.chat.idea_corrections import parse
    from app.idea.service import get_session, list_revisions, patch_revision

    venture_id = _chat_started_venture(client, f"A planning app for {audience}")
    saved, _, _ = _ideas(venture_id)
    before = patch_revision(saved.id, {"first_user_path": "Old distribution assumption",
        "pricing_hypothesis": "Old price assumption"}, venture_id=venture_id)
    patch = {"customer": audience, "first_user_path": path, "pricing_hypothesis": pricing}
    message = f"Correction: our first audience is {audience}. Our first user path is {path}. Pricing is {pricing}."
    assert parse(message) == patch
    assert parse(f"Our first user path is {path}. Pricing is {pricing}.") is None
    assert parse(f"No one has used the app. Pricing is {pricing}.") is None
    reply = _say(client, venture_id, message)
    current = get_session(saved.id)["current_revision"]
    assert current["id"] != before["id"]
    for field, value in patch.items():
        assert current["structured"][field] == value
    for label, value in (("Customer", audience), ("First users", path), ("Pricing", pricing)):
        assert f"- **{label}:** {value}" in reply["body_md"]
    review = _say(client, venture_id, "Skip the questions")
    for label, value in (("First users", path), ("Pricing", pricing)):
        assert f"- **{label}:** {value}" in review["body_md"]
    assert next(revision for revision in list_revisions(saved.id) if revision["id"] == before["id"]) == before


@pytest.mark.parametrize("idea,audience", [
    ("Inspection checklists for theatre technicians", "theatre technicians"),
    ("Trip journals for amateur cyclists", "amateur cyclists"),
])
def test_own_summary_with_nominal_contact_is_saved_without_outreach(client, idea, audience):
    from app.chat.guard import _outreach_requests, guard_reply, outreach_task_reply
    from app.idea.service import get_session

    venture_id = _chat_started_venture(client, idea)
    summary = (f"Build a private planning tool for {audience}, keeping records and reminders together. "
        "We have no prototype or revenue. Avery Lane is only a potential interview contact.")
    text = f"Here is my own summary: {summary}"
    assert not _outreach_requests(text)
    assert guard_reply("", text, read_history=False) is None
    assert outreach_task_reply("", text) is None
    saved, _, _ = _ideas(venture_id)
    previous_offer = get_session(saved.id)["current_revision"]["structured"]["offer"]
    reply = _say(client, venture_id, text)
    fields = get_session(saved.id)["current_revision"]["structured"]
    assert fields["customer"] == audience and fields["summary"] == summary
    assert fields["offer"] == previous_offer
    assert reply["kind"] != "refusal" and not any(action.get("confirmation") for action in reply["actions"])
    for command in ("Contact Avery Lane.", "Send the summary to a customer.", "Email 40 customers."):
        combined = f"{text} {command}"
        refusal = guard_reply("", combined, read_history=False) or outreach_task_reply("", combined)
        assert refusal is not None and refusal["kind"] == "refusal"


@pytest.mark.parametrize("idea,audience,excluded", [
    ("Inspection checklists for theatre technicians", "independent theatre technicians", "cinema operators"),
    ("Trip journals for amateur cyclists", "weekend bicycle commuters", "professional racers"),
])
def test_scope_correction_saves_the_first_audience_without_research(client, monkeypatch, idea, audience, excluded):
    import asyncio

    from app.chat import router
    from app.chat.context import ChatContext
    from app.chat.idea_corrections import parse
    from app.idea.service import get_session

    text = f"Please correct the scope: start with {audience}, not {excluded}. There is no prototype or revenue yet."
    assert parse(text) == {"customer": audience}
    venture_id = _chat_started_venture(client, idea)
    reply = _say(client, venture_id, text)
    saved, _, _ = _ideas(venture_id)
    assert get_session(saved.id)["current_revision"]["structured"]["customer"] == audience
    assert "Updated" in reply["body_md"]
    from app.chat import idea_corrections
    monkeypatch.setattr(idea_corrections, "apply", lambda *_: reply)
    routed = asyncio.run(router._dispatch_intent(venture_id, ChatContext(venture={}), "research.update_and_rerun", text, None, "usr_alice"))
    assert audience in routed["body_md"] and "Nothing changed" not in routed["body_md"]


@pytest.mark.parametrize("idea,audience,problem,customer_statement", [
    ("A tool that tracks deliveries for warehouse managers", "independent warehouse managers",
     "The problem is missing deliveries during busy warehouse shifts.", "One correction: this is for {audience}, not everyone."),
    ("An app that plans dinners for families", "parents managing food allergies",
     "The problem is planning dinners without triggering food allergies.", "One correction: this is for {audience}, not everyone."),
    ("A tool that tracks deliveries for warehouse managers", "independent warehouse managers",
     "Deliveries go missing.", "The customers would be {audience}."),
    ("An app that plans dinners for families", "parents managing food allergies",
     "Meals trigger allergic reactions.", "Our customers are {audience}."),
])
def test_answer_with_later_correction_saves_both(client, idea, audience, problem, customer_statement):
    from app.chat.idea_corrections import parse
    from app.idea.service import get_session

    from tests.security.test_durable_journey_boundary import run_queued

    text = f"{problem} {customer_statement.format(audience=audience)}"
    assert parse(text) == {"customer": audience}
    venture_id = _chat_started_venture(client, idea, field="problem")
    import json

    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    _, turns, _ = _ideas(venture_id)
    with db()() as session:
        question = session.get(ClarifierTurn, turns[-1].id)
        question.text = "What difficulty do you want to solve?"
        question.options = json.dumps({"field": "problem"})
        session.commit()
    reply = _say(client, venture_id, text)
    assert audience in reply["body_md"]
    run_queued("idea.clarify_turn")
    saved, turns, _ = _ideas(venture_id)
    structured = get_session(saved.id)["current_revision"]["structured"]
    assert structured["customer"] == audience
    assert audience not in structured["problem"]
    assert any(turn.role == "founder" and turn.text == problem for turn in turns)


@pytest.mark.parametrize("idea,answer", [
    ("A tool that tracks deliveries for warehouse managers", "It must export delivery receipts for every supplier."),
    ("An app that plans dinners for families", "It must keep allergy information private for each family."),
])
@pytest.mark.parametrize("field", ["requirements", "constraints"])
def test_skip_after_typed_list_answer_shows_valid_saved_review(client, idea, answer, field):
    import json

    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.idea.agent import Understanding
    from app.idea.service import _append_revision, get_session
    from app.state import db

    venture_id = _chat_started_venture(client, idea)
    saved, _, _ = _ideas(venture_id)
    with db()() as session:
        revision = session.get(IdeaRevision, saved.current_revision_id)
        structured = json.loads(revision.structured)
        structured[field] = []
        structured.setdefault("_answered_fields", {})[field] = answer
        _append_revision(session, session.get(IdeaSession, saved.id), structured, source="founder")
        session.commit()
    reply = _say(client, venture_id, "Skip the questions")
    assert answer in reply["body_md"], reply
    structured = get_session(saved.id)["current_revision"]["structured"]
    assert Understanding.model_validate({key: value for key, value in structured.items()
        if key in Understanding.model_fields}).model_dump()[field] == [answer]
    assert structured["_answered_fields"][field] == answer
    assert _say(client, venture_id, "Skip the questions")["body_md"] == reply["body_md"]


@pytest.mark.parametrize("idea,audience", DOMAINS)
@pytest.mark.parametrize("phrase", [
    "No, the customer is {a}", "Replace the customer field with {a}.", "Change the audience to {a}, not the ones you said.",
    "Actually it's for {a}", "Customer: {a}. That is the only field I want to change.", "Second correction: my first audience is now {a}, rather than who I said.",
])
def test_any_phrasing_saves_the_change_and_shows_it_in_the_next_reply(client, idea, audience, phrase):
    from app.db.supymem_models import IdeaRevision
    from app.state import db

    venture_id = _chat_started_venture(client, idea)
    session, _, _ = _ideas(venture_id)
    before = session.current_revision_id
    reply = _say(client, venture_id, phrase.format(a=audience))
    assert audience.casefold() in reply["body_md"].casefold() and "Updated" in reply["body_md"]
    saved, _, _ = _ideas(venture_id)
    assert saved.current_revision_id != before  # a new revision: history stays append-only
    with db()() as db_session:
        import json
        structured = json.loads(db_session.get(IdeaRevision, saved.current_revision_id).structured)
    assert structured["customer"].casefold() == audience.casefold()


@pytest.mark.parametrize("idea,audience", DOMAINS)
def test_replacing_a_saved_phrase_changes_that_field_and_survives_review(client, idea, audience):
    venture_id = _chat_started_venture(client, idea)
    _say(client, venture_id, "The customer is compliance managers at regional clinics")
    reply = _say(client, venture_id, f"That summary is wrong. Please replace compliance managers at regional clinics with {audience}.")
    assert audience in reply["body_md"]
    later = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "idea.review"})
    assert later.status_code == 200 and "regional clinics" not in later.json()["body_md"]


@pytest.mark.parametrize("text", [
    "What should the customer be?", "The customer is always right", "The problem is I have no time today",
    "My favourite dinner is pasta", "As I said, this is expensive", "Change the customer to bank fraud leads and rerun research",
    "Change the customer to bank fraud leads and find me 10 of them",
])
def test_other_messages_are_not_corrections(text):
    from app.chat.idea_corrections import parse

    assert parse(text, {}) is None


@pytest.mark.parametrize("text,field,value", [
    ("Second correction: it should not automate lab instruments. I'm changing the audience to clinical operations managers at small contract research organizations, not preclinical lab managers.",
     "customer", "clinical operations managers at small contract research organizations"),
    ("Customer: compliance and quality directors at independent hospitals, starting with one department. That is the only field I want to change.",
     "customer", "compliance and quality directors at independent hospitals"),
    ("Replace the Customer field with operations leads at regional freight carriers.", "customer", "operations leads at regional freight carriers"),
    ("As I just said, clinical operations managers at small research organizations. I changed away from academic labs.",
     "customer", "clinical operations managers at small research organizations"),
    ("No, the product is a shared evidence workspace for accreditation audits.", "offer", "a shared evidence workspace for accreditation audits"),
])
def test_real_founder_phrasings_parse_cleanly(text, field, value):
    from app.chat.idea_corrections import parse

    assert parse(text, {}) == {field: value}


def test_a_viewer_cannot_correct_the_idea(client, monkeypatch):
    from app.chat import idea_corrections
    from app.workspaces import service

    venture_id = _chat_started_venture(client, DOMAINS[0][0])
    before = _ideas(venture_id)[0].current_revision_id
    monkeypatch.setattr(service, "role_of", lambda *args: "viewer")
    assert idea_corrections.apply(venture_id, f"The customer is {DOMAINS[0][1]}", "usr_alice") is None
    assert _ideas(venture_id)[0].current_revision_id == before


@pytest.mark.parametrize("idea,audience", DOMAINS)
def test_correction_typed_while_a_question_shows_is_still_a_correction(client, idea, audience):
    venture_id = _chat_started_venture(client, idea)
    _, turns, _ = _ideas(venture_id)
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": f"No, the customer is {audience}.",
        "reply_to_question": True, "question_id": turns[-1].id})
    assert sent.status_code == 200, sent.text
    assert "Updated" in sent.json()["body_md"] and audience.casefold() in sent.json()["body_md"].casefold()
    session, _, _ = _ideas(venture_id)
    assert session.current_revision_id


@pytest.mark.parametrize(("idea", "wrong", "audience"), [
    ("We build software that finds unpatched medical devices on hospital networks and tells biomedical engineering directors which ones to fix first", "devices", "biomedical engineering directors"),
    ("A platform that scans satellite telemetry and alerts mission operators before a failure", "telemetry", "mission operators"),
])
def test_the_person_told_is_the_customer_not_the_thing_found(client, idea, wrong, audience):
    from app.intake.smart import _prefill
    customer = _prefill(idea).get("customer", "")
    assert customer.casefold() == audience and wrong not in customer


def test_things_are_not_an_audience(client):
    from app.intake.smart import audience_phrase
    assert audience_phrase("unpatched medical devices") == ""
    assert audience_phrase("satellite operators") == "satellite operators"


def test_correction_rewords_the_question_still_showing(client):
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    venture_id = _chat_started_venture(client, "Software that helps claims teams at regional insurers close disputes faster")
    _, turns, _ = _ideas(venture_id)
    question = turns[-1]
    with db()() as session:
        turn = session.get(ClarifierTurn, question.id)
        turn.text = "What must your product do to help claims teams at regional insurers with this task?"
        turn.options = '{"field": "requirements"}'
        session.commit()
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": "No, the customer is hospital billing managers.",
        "reply_to_question": True, "question_id": question.id})
    assert sent.status_code == 200, sent.text
    _, turns, _ = _ideas(venture_id)
    assert "hospital billing managers" in turns[-1].text and "regional insurers" not in turns[-1].text


@pytest.mark.parametrize(("idea", "answer"), [
    ("We build software that finds unpatched medical devices on hospital networks for hospital CISOs",
     "Three hospital CISOs told me they found exposed infusion pumps only during their last audit"),
    ("A platform that scans satellite telemetry for mission operators at launch companies",
     "Two mission operators told me they lost a satellite because nobody saw the drift in time"),
])
def test_statement_typed_as_the_reply_is_saved_whatever_the_router_guesses(client, monkeypatch, idea, answer):
    from app.chat import llm_router

    async def clarify(*_args, **_kwargs):
        return llm_router.Decision([], "Do you mean research?")

    monkeypatch.setattr(llm_router, "route", clarify)
    from app.db.supymem_models import ClarifierTurn
    from app.state import db
    venture_id = _chat_started_venture(client, idea)
    _, turns, _ = _ideas(venture_id)
    with db()() as session:  # The evidence question, as asked on the live app.
        turn = session.get(ClarifierTurn, turns[-1].id)
        turn.kind, turn.text, turn.options = "question", "What evidence do you have that they need help with this?", '{"field": "Q1"}'
        session.commit()
    sent = client.post(f"/ventures/{venture_id}/chat", json={"message": answer, "reply_to_question": True, "question_id": turns[-1].id})
    assert sent.status_code == 200, sent.text
    _, turns, _ = _ideas(venture_id)
    assert any(turn.role == "founder" and answer in turn.text for turn in turns), [(t.role, t.kind, t.text[:90]) for t in turns]
    assert "leave this point open" not in sent.json()["body_md"], sent.json()["body_md"]


@pytest.mark.parametrize("idea,audience,example", [
    ("A booking tool for recording studios", "Independent recording studio owners",
     "I spoke with Lena Ortiz, the manager at Willow Sound. Her staff double-book rooms and lost a session yesterday."),
    ("A route diary for cycle trips", "Weekend touring cyclists",
     "I spoke with Nolan Reed from Cedar Wheel Club. His group missed a ferry and spent a night waiting."),
])
def test_customer_subject_with_a_named_example_advances_and_keeps_evidence(client, idea, audience, example):
    import json

    from app.chat.idea_bridge import _plausible_answer
    from app.db.supymem_models import ClarifierTurn
    from app.idea.agent import founder_audience
    from app.state import db

    venture_id = _chat_started_venture(client, idea)
    _, turns, _ = _ideas(venture_id)
    with db()() as session:
        question = session.get(ClarifierTurn, turns[-1].id)
        question.text = "Who needs this first?"
        question.options = json.dumps({**json.loads(question.options or "{}"), "field": "customer"})
        session.commit()
    _, turns, _ = _ideas(venture_id)
    answer = f"{audience} need this first. {example}"
    assert _plausible_answer(answer, turns[-1], {"offer": idea})
    assert founder_audience(answer) == audience
    reply = _say(client, venture_id, answer)
    _, saved_turns, _ = _ideas(venture_id)
    assert any(turn.role == "founder" and answer in turn.text for turn in saved_turns)
    assert "Who needs this first?" not in reply["body_md"]
    assert not _plausible_answer("A printer needs more ink. I spoke with Lena about it.", turns[-1], {})
    assert founder_audience(audience + " who need accessible schedules") == audience + " who need accessible schedules"
    assert founder_audience(audience + " whose children need accessible schedules") == audience + " whose children need accessible schedules"


def test_singular_word_is_on_topic_when_the_idea_uses_the_plural(client):
    import json
    from types import SimpleNamespace

    from app.chat.idea_bridge import _plausible_answer
    question = SimpleNamespace(text="What difficulty do you want to solve for hospital CISOs?", options=json.dumps({"field": "problem"}))
    fields = {"offer": "finds unpatched medical devices"}
    assert _plausible_answer("Nobody knows which device could hurt a patient", question, fields)
    assert not _plausible_answer("The weather was lovely on our holiday", question, fields)


@pytest.mark.parametrize("answer", [
    "Pull the payment trail and victim statements into one case file so the manager decides in minutes",
    "Check the telemetry from every satellite and flag drift before an operator has to ask about it today",
])
def test_an_instruction_is_not_pasted_into_the_next_question(client, answer):
    from app.clarify.dilemmas import ground_question
    question = ground_question({"field": "offer", "text": "Old wording?", "options": []},
        {"customer": "bank fraud managers", "answered_fields": {"workflow": answer}, "answered_field_updated_at": {"workflow": "2026-11-02T00:00:00"}})
    assert question["text"] == "What should your product do to help bank fraud managers?"


@pytest.mark.parametrize("answer", [
    "Assemble the evidence pack and recommend reimburse or decline with the reason",
    "Compare each orbit pass against the last known drift and warn the crew early",
])
def test_a_full_sentence_is_never_i_didnt_catch_that(client, answer):
    import json
    from types import SimpleNamespace

    from app.chat.idea_bridge import _plausible_answer
    question = SimpleNamespace(text="What should your product do?", options=json.dumps({"field": "offer"}))
    assert _plausible_answer(answer, question, {"offer": "unrelated words only"})


@pytest.mark.parametrize(("text", "answer", "ask"), [
    ("Assemble a one page shift briefing and recommend who does what before lunch", "Assemble a one page shift briefing and recommend who does what before lunch", ""),
    ("Rank every contract by risk and send the legal lead a morning list", "Rank every contract by risk and send the legal lead a morning list", ""),
    ("We have 3 pilots and find me 10 engineering leaders", "We have 3 pilots", "and find me 10 engineering leaders"),
    ("Yes that is right, and draft the first email for me", "Yes that is right", "and draft the first email for me"),
])
def test_only_a_clause_asking_for_something_splits_an_answer(client, text, answer, ask):
    from app.chat.idea_bridge import split_question_reply
    assert split_question_reply(text) == (answer, ask)


@pytest.mark.parametrize("text,audience", [
    ("A web app for trial site coordinators that spots missing records and shows the study manager where to act", "trial site coordinators"),
    ("A tool for grid planners that reads feeder data and shows the field crew where to dig", "grid planners"),
])
def test_stated_for_audience_beats_the_person_being_shown(text, audience):
    from app.intake.smart import _prefill

    assert _prefill(text)["customer"] == audience


@pytest.mark.parametrize("idea", ["Software that helps claims teams at regional insurers close disputes faster", "An app that helps families plan accessible holidays"])
def test_a_typed_confirmation_is_recognised_but_skip_keeps_its_own_flow(client, idea):
    from app.chat.idea_proposal import confirmation_payload
    from app.core.local_mode import is_local
    venture_id = _chat_started_venture(client, idea)
    for text in ("Yes that looks right, please confirm the idea", "Looks good, confirm my idea", "Please confirm the idea"):
        assert confirmation_payload(venture_id, text), text
    for text in ("Do not confirm the idea yet", "I will confirm the idea after I talk to my team"):
        assert confirmation_payload(venture_id, text) is None, text
    if is_local():
        assert confirmation_payload(venture_id, "Skip the remaining questions and confirm my idea")
    else:
        assert confirmation_payload(venture_id, "Skip the remaining questions and confirm my idea") is None


CONFIRM_DOMAINS = [
    "Software that helps claims teams at regional insurers close disputes faster",
    "Test equipment that helps failure analysts at chip packaging plants find intermittent opens",
]
APPROVALS = ["That summary matches my idea.", "Please confirm my idea.", "This summary looks right to me.", "The summary is accurate. Please confirm this idea.",
             "The summary is right. Please confirm my idea, and do not search for people or send any messages."]


def _saved_idea(venture_id):
    from app.idea.service import get_session

    return get_session(_ideas(venture_id)[0].id)


def _confirmed(venture_id):
    row = _ideas(venture_id)[0]
    return row.state == "confirmed" and row.confirmed_revision_id == row.current_revision_id


def _make_open_proposal(venture_id):
    """The founder has a summary on screen, with no review run and no question open."""
    from app.db.supymem_models import ClarifierTurn, IdeaSession
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    saved = _saved_idea(venture_id)
    with db()() as session:
        row = session.get(IdeaSession, saved["id"])
        row.state = "reviewing"
        session.add(ClarifierTurn(id=new_id("ict"), session_id=row.id, workspace_id=row.workspace_id, venture_id=venture_id,
                                  turn=saved["turns"][-1]["turn"] + 1, role="agent", kind="proposal", text="Review your saved idea.",
                                  revision_id=row.current_revision_id, created_at=utcnow()))
        session.commit()


@pytest.mark.parametrize("idea", CONFIRM_DOMAINS)
@pytest.mark.parametrize("text", APPROVALS)
def test_a_typed_approval_of_the_summary_on_screen_confirms_it(client, idea, text):
    venture_id = _chat_started_venture(client, idea)
    _make_open_proposal(venture_id)
    reply = _say(client, venture_id, text)  # the app sends neither flag here: no question has options
    assert _confirmed(venture_id), reply["body_md"]
    assert "Your idea is saved" in reply["body_md"] and "Nothing was started" not in reply["body_md"]
    assert not any(action.get("confirmation") for action in reply["actions"])


@pytest.mark.parametrize("idea", CONFIRM_DOMAINS)
@pytest.mark.parametrize("flags", [False, True])
def test_a_typed_approval_with_a_question_open_confirms_without_another_card(client, idea, flags):
    venture_id = _chat_started_venture(client, idea)
    question = _saved_idea(venture_id)["turns"][-1]
    assert question["kind"] == "question"
    body = {"message": "Please confirm my idea.", **({"reply_to_question": True, "question_id": question["id"]} if flags else {})}
    sent = client.post(f"/ventures/{venture_id}/chat", json=body)
    assert sent.status_code == 200, sent.text
    reply = sent.json()
    assert "Your idea is saved" in reply["body_md"]
    assert _confirmed(venture_id)
    assert not [turn for turn in _saved_idea(venture_id)["turns"] if turn["role"] == "founder" and "confirm" in turn["text"].casefold()]
    assert not any(item.get("confirmation") or item.get("action") == "idea.confirm" for item in reply["actions"])


@pytest.mark.parametrize("idea", CONFIRM_DOMAINS)
def test_a_spoken_approval_never_confirms_on_its_own(client, idea):
    from app.chat.idea_proposal import typed_confirm
    from app.intake.evidence import INPUT_KIND

    venture_id = _chat_started_venture(client, idea)
    token = INPUT_KIND.set("voice")
    try:
        reply = typed_confirm(venture_id, None)
    finally:
        INPUT_KIND.reset(token)
    assert not _confirmed(venture_id)
    assert any(item["action"] == "idea.confirm" for item in reply["actions"])


@pytest.mark.parametrize("idea", CONFIRM_DOMAINS)
def test_an_old_confirm_button_does_not_confirm_an_idea_that_changed(client, idea):
    venture_id = _chat_started_venture(client, idea)
    saved = _saved_idea(venture_id)
    action = {"action": "idea.confirm", "payload": {"session_id": saved["id"], "revision_id": saved["current_revision"]["id"], "skip_open_questions": True}}
    _say(client, venture_id, "The customer is compliance managers at regional clinics")
    clicked = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": action["action"], "payload": action["payload"]})
    assert clicked.status_code == 200, clicked.text
    assert _saved_idea(venture_id)["state"] != "confirmed"


@pytest.mark.parametrize("text", [
    "Do not confirm my idea yet", "I will confirm the idea after I talk to my team", "Skip the remaining questions and confirm my idea",
    "This summary looks right, but the customer is wrong", "The summary is wrong", "Should I confirm my idea?",
    "If this works, confirm my idea", "The operator must approve it before dispatch and log the reason",
    "That summary matches my idea. Do not confirm it.",
    "That summary matches my idea. Confirm it only if the trial works.",
    "That summary matches my idea. Assuming the trial works, confirm it.",
    "That summary matches my idea. Provided the trial works, confirm it.",
    "That summary matches my idea. As long as the trial works, confirm it.",
    "This is the idea I want to pursue, but do not confirm it.",
    "This is the idea I want to pursue, but never approve it.",
])
def test_other_messages_are_not_a_typed_approval(text):
    from app.chat.idea_proposal import clear_approval
    from app.core.local_mode import is_local

    if is_local() and text == "Skip the remaining questions and confirm my idea":
        assert clear_approval(text) is True, text
    else:
        assert clear_approval(text) is False, text


def test_this_looks_right_needs_no_question_it_could_answer():
    from app.chat.idea_proposal import clear_approval

    assert clear_approval("This looks right to me.") is True
    assert clear_approval("This looks right to me.", question_showing=True) is False
    assert clear_approval("This summary looks right to me.", question_showing=True) is True


@pytest.mark.parametrize("offer,audience", [
    ("I have a live payments app and I want to find customers.", "payments"),
    ("I have a live hospital scheduling app and I want to find customers.", "scheduling"),
])
def test_confirming_with_no_saved_idea_never_claims_it(client, offer, audience):
    preview = client.post("/entry/chat/preview", json={"text": offer})
    assert preview.status_code == 200 and preview.json()["proposal"]["route"] == "gtm", audience
    venture_id = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"]}).json()["venture_id"]
    assert client.get(f"/ventures/{venture_id}/idea/sessions").json()["items"] == []
    reply = _say(client, venture_id, "The summary is accurate. Please confirm this idea.")
    assert reply["body_md"].split("-->")[-1].strip().startswith("There is no saved idea to confirm yet.")


@pytest.mark.parametrize("idea", CONFIRM_DOMAINS)
def test_a_reply_opening_confirmed_is_guarded_when_nothing_is_confirmed(client, idea):
    from app.chat.idea_proposal import guard_confirmation
    from app.chat.rephrase import INPUT

    venture_id = _chat_started_venture(client, idea)
    reply = {"body_md": "<!--employee:Charlie:Manager:manager-->\nConfirmed: this helps your customer.", "actions": []}
    token = INPUT.set({"original": "Please confirm this idea for me"})
    try:
        assert "not confirmed yet" in guard_confirmation(venture_id, reply)["body_md"]
    finally:
        INPUT.reset(token)
    token = INPUT.set({"original": "Did the invoice export run?"})
    try:
        assert guard_confirmation(venture_id, reply) == reply
    finally:
        INPUT.reset(token)


def _as_website_path(venture_id):
    """An unreadable website keeps the intake path "clarify" after the founder's answers route it to a startup."""
    import json

    from app.db.models import VentureDocument
    from app.state import db

    with db()() as session:
        document = session.get(VentureDocument, f"intake_decision:{venture_id}")
        document.body_md = json.dumps({**json.loads(document.body_md), "path": "clarify"})
        session.commit()


@pytest.mark.parametrize("idea,answer", [
    (CONFIRM_DOMAINS[0], "It must import open disputes from the claims system, flag the ones that are missing evidence, and put them into a packet for the claims lead to review."),
    (CONFIRM_DOMAINS[1], "They send failing units to the lab by email and keep the results in spreadsheets, and I watched two analysts lose a full shift chasing one lot."),
])
@pytest.mark.parametrize("flags", [False, True])
def test_a_fresh_answer_on_the_website_path_is_saved(client, idea, answer, flags):
    venture_id = _chat_started_venture(client, idea)
    _as_website_path(venture_id)
    question = _saved_idea(venture_id)["turns"][-1]
    body = {"message": answer, **({"reply_to_question": True, "question_id": question["id"]} if flags else {})}
    sent = client.post(f"/ventures/{venture_id}/chat", json=body)
    assert sent.status_code == 200, sent.text
    assert "already answered" not in sent.json()["body_md"]
    assert any(turn["role"] == "founder" and turn["text"].startswith(answer[:40]) for turn in _saved_idea(venture_id)["turns"])
    from app.chat.store import list_messages
    assert any(message["role"] == "user" and message["body_md"] == answer for message in list_messages(venture_id, limit=20))


@pytest.mark.parametrize("idea,audience,evidence", [
    (CONFIRM_DOMAINS[0], "claims managers at regional insurers", "Two adjusters told me about disputes that reopened after settlement, and they described extra reviews and delayed payouts for weeks."),
    (CONFIRM_DOMAINS[1], "the outsourced assembly and test company", "Two final-test engineers told me about intermittent opens that escaped screening, and they described extra retesting and delayed shipments."),
])
@pytest.mark.parametrize("flags", [False, True])
def test_evidence_and_a_customer_correction_in_one_message_both_land(client, idea, audience, evidence, flags):
    import json

    from app.db.supymem_models import ClarifierTurn
    from app.state import db

    venture_id = _chat_started_venture(client, idea)
    question = _saved_idea(venture_id)["turns"][-1]
    with db()() as session:  # a question the customer correction does not answer
        turn = session.get(ClarifierTurn, question["id"])
        turn.options = json.dumps({**json.loads(turn.options or "{}"), "field": "current_solution"})
        session.commit()
    text = f"{evidence} The customer is still {audience}, not a generic individual engineer."
    body = {"message": text, **({"reply_to_question": True, "question_id": question["id"]} if flags else {})}
    sent = client.post(f"/ventures/{venture_id}/chat", json=body)
    assert sent.status_code == 200, sent.text
    saved = _saved_idea(venture_id)
    assert saved["current_revision"]["structured"]["customer"] == audience
    assert any(turn["role"] == "founder" and turn["text"].startswith(evidence[:40]) for turn in saved["turns"])
    assert not (saved["turns"][-1]["kind"] == "question" and saved["turns"][-1]["id"] == question["id"])


@pytest.mark.parametrize("text", [
    "I have a clickable prototype but no customers yet. The product reconciles intercompany invoices for finance controllers at multinational manufacturers.",
    "We help hospital pharmacists catch dosing errors. There are no customers yet, only a prototype.",
])
def test_a_prototype_with_no_customers_is_an_idea_not_a_customer_search(text):
    from app.entry.chat_start import _classify

    assert _classify(text, None)[0] == "startup"


@pytest.mark.parametrize("text", [
    "I have a live invoice product but no customers yet", "My hospital pharmacy app is live and has no customers",
])
def test_a_live_product_with_no_customers_is_still_a_customer_search(text):
    from app.entry.chat_start import _classify

    assert _classify(text, None)[0] == "gtm"


@pytest.mark.parametrize("typed,name", [
    ("I would like to call it Policy Harbor.", "Policy Harbor"), ("Call it Vendor Trail.", "Vendor Trail"),
    ("I would call it Control Bridge.", "Control Bridge"), ("Let's call it 'Grid Ledger'", "Grid Ledger"),
    ("It's called Probe Ready.", "Probe Ready"), ("The name is Repair Curve", "Repair Curve"),
    ("Policy Harbor", "Policy Harbor"), ("Call It Home", "Call It Home"), ("Acme Inc.", "Acme Inc."),
])
def test_a_full_sentence_name_answer_saves_only_the_name(typed, name):
    from app.entry.chat_start import _typed_name

    assert _typed_name(typed) == name


def test_a_full_sentence_name_answer_reaches_the_venture(client):
    from app.db.models import Venture
    from app.state import db

    preview = client.post("/entry/chat/preview", json={"text": "An order tracking tool for independent bookshops"})
    confirmed = client.post("/entry/chat/confirm", json={"proposal_id": preview.json()["proposal"]["id"], "name": "I would like to call it Policy Harbor."})
    assert confirmed.status_code == 200, confirmed.text
    with db()() as session:
        assert session.get(Venture, confirmed.json()["venture_id"]).name == "Policy Harbor"
