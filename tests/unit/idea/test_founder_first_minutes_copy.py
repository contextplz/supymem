"""What a founder reads in the first minutes: no method names, wire markers or their own paste echoed back."""
import ast
import json
import re
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.integration.test_postgres_auth_security import service_modules

ROOT = Path(__file__).resolve().parents[3] / "services/company_os/app"
BRANDING = re.compile(r"GStack|Garry|Y Combinator|\bYC\b|office hours|forcing question|Demand Reality|Status Quo"
                      r"|Desperate Specificity|Narrowest Wedge|Future-Fit", re.I)
# One B2B and one consumer idea: (who, thing, problem, what it does).
DOMAINS = [
    ("independent freight brokers", "a carrier invoice checker", "overcharges go unnoticed", "You can flag an overcharge before payment"),
    ("new parents", "a shared feeding diary", "feeds get forgotten at night", "You can log a bottle and remind your partner"),
]


@pytest.mark.parametrize("who,thing,problem,does", DOMAINS)
def test_clarification_opening_acknowledges_saved_idea_and_explains_the_wait(app_modules, who, thing, problem, does):
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.idea import service
    from core.clock import utcnow

    now = utcnow()
    idea = IdeaSession(id="wait_session", venture_id="wait_venture", workspace_id="wait_workspace",
        state="clarifying", case="know", question_budget=5, questions_asked=0, recon_summary="{}")
    revision = IdeaRevision(id="wait_revision", rev=1, source="founder", summary=f"{thing} for {who}",
        structured=json.dumps({"customer": who, "offer": thing, "problem": problem, "workflow": does}), created_at=now)
    turns = [ClarifierTurn(id="wait_opening", turn=1, role="agent", kind="opening", text=service.OPENING, created_at=now),
             ClarifierTurn(id="wait_answer", turn=2, role="founder", kind="answer", text=f"{thing} for {who}", created_at=now)]
    saved = service.serialize_session(idea, turns, [], revision)
    for body in (saved["opening"], saved["turns"][0]["text"]):
        assert "Your idea is saved" in body
        assert "next question or a summary" in body
        assert "add anything important while you wait" in body
        assert "Tell me what you want to build" not in body
        assert not BRANDING.search(body) and "\u2014" not in body
    empty = service.serialize_session(idea, turns[:1], [], None)
    assert empty["opening"] == service.OPENING


@pytest.mark.parametrize("customer,problem,assumption", [
    ("film location coordinators", "missed permit deadlines", "permit delays cancel paid shoots"),
    ("amateur stargazers", "cloudy viewing nights", "cloud forecasts prevent wasted journeys"),
])
def test_prepared_questions_are_grounded_even_when_the_draft_is_not(app_modules, monkeypatch, customer, problem, assumption):
    from app.chat import experiments
    from app.core.local_mode import is_local
    from app.experiments import coach

    monkeypatch.setattr(experiments, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *_: SimpleNamespace())))
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda *_: {"journey": {"icp": {"who": customer, "problem": problem}}})
    scope = f'For {customer}, about "{problem}", to check "{assumption}"'
    grounded = f"{scope}: What happened during the most recent incident?"
    safety = "Interview four consenting adults on Saturday. Do not contact anyone or collect private records. "
    inline = "Interview four adults who consent, do not contact anyone, then ask "
    for draft in ("", "How did your week go? Which subscriptions do you keep?", grounded,
                  grounded + " How did your week go?", "What happens when a car engine overheats?",
                  "Are you enjoying life?", "Describe your favourite pastime", "Which subscriptions do you keep",
                  inline + "what happened yesterday?"):
        row = {"id": "test", "state": "waiting_data", "hypothesis": assumption,
            "action": safety + draft, "measure": "Record four dated incidents."}
        monkeypatch.setattr(experiments.ledger, "get", lambda *_, row=row: row)
        reply = experiments.review_test_run("test", {"run": True, "experiment_id": "test"})
        body = reply["body_md"].split("Next step:")[0]
        questions = re.findall(r"[^.!?\n]*\?", body)
        assert questions
        if is_local():
            assert any(all(value in line for value in (customer, problem, assumption)) for line in body.splitlines())
        else:
            script = reply["body_md"].split("Interview script: ", 1)[1].split("\n\n", 1)[0]
            assert all(value in script.splitlines()[0] for value in (customer, problem, assumption))
        assert safety.strip() in body
        if inline in draft:
            assert inline in body
        if grounded not in draft:
            assert all(generic not in body for generic in (
                "How did your week go?", "car engine overheats", "enjoying life", "favourite pastime", "subscriptions"))
        assert "Nothing was sent, published or built." in reply["body_md"]
        assert "\u2014" not in reply["body_md"]
    assert coach.interview_script(customer, problem, assumption, grounded).endswith("What happened during the most recent incident?")


@pytest.mark.parametrize("customer,problem", [
    ("film location coordinators", "missed permit deadlines"),
    ("amateur stargazers", "cloudy viewing nights"),
])
@pytest.mark.parametrize("missing", ["customer", "problem"])
def test_prepared_experiment_only_asks_for_the_missing_detail(app_modules, monkeypatch, customer, problem, missing):
    from app.chat import experiments

    audience = {"who": customer if missing == "problem" else "", "problem": problem if missing == "customer" else ""}
    monkeypatch.setattr(experiments, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *_: SimpleNamespace())))
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda *_: {"journey": {"icp": audience}})
    monkeypatch.setattr(experiments.ledger, "get", lambda *_: {"id": "test", "state": "waiting_data",
        "hypothesis": "Your saved assumption", "action": "Use consenting adults and anonymous notes.", "measure": "Record dated incidents."})
    reply = experiments.review_test_run("test", {"run": True, "experiment_id": "test"})
    next_step = reply["body_md"].split("Next step:", 1)[1]
    script = reply["body_md"].split("Interview script: ", 1)[1].split("\n\n", 1)[0]
    known = "customer" if missing == "problem" else "problem"
    assert missing in next_step and known not in next_step
    assert missing in script and known not in script
    assert "Nothing was sent, published or built." in reply["body_md"]


@pytest.mark.parametrize("customer,problem,phrase", [
    ("facilities managers at clinics", "A refrigeration outage spoils stored vaccines", "A refrigeration outage spoils stored vaccines"),
    ("parents with schoolchildren", "A forgotten pickup leaves a child waiting", "A forgotten pickup leaves a child waiting"),
])
def test_interview_script_uses_a_short_problem_phrase(app_modules, customer, problem, phrase):
    from app.chat import experiments
    from app.experiments import coach

    full_answer = problem + ", and today they only discover it after someone calls. The customers would be " + customer + "."
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(experiments, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *_: SimpleNamespace())))
        patch.setattr(experiments.ledger, "get", lambda *_: {"id": "test", "state": "waiting_data", "hypothesis": "Your saved test", "action": "", "measure": ""})
        patch.setattr(experiments, "_experiment_snapshot", lambda *_: {"journey": {"icp": {"who": customer, "problem": full_answer}}})
        reply = experiments.review_test_run("test", {"run": True, "experiment_id": "test"})
    assert f'ask about "{phrase}", to check "Your saved test".\nWhat happened the last time you faced it?' in reply["body_md"]
    assert full_answer not in reply["body_md"] and "The customers would be" not in reply["body_md"]
    assert reply["body_md"].endswith("Next step: who will you interview first?")
    assert coach.interview_script("", phrase) is None


@pytest.mark.parametrize("customer,problem", [
    ("warehouse purchasing teams", "The spare parts in our bins are not counted until a shipment is late"),
    ("people learning pottery at home", "I cannot tell if the clay on my shelf is still safe to use"),
])
def test_prepared_interview_quotes_your_problem_as_written(app_modules, monkeypatch, customer, problem):
    from app.chat import experiments

    monkeypatch.setattr(experiments, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *_: SimpleNamespace())))
    monkeypatch.setattr(experiments.ledger, "get", lambda *_: {
        "id": "test", "state": "waiting_data", "hypothesis": "Your saved test", "action": "", "measure": ""})
    monkeypatch.setattr(experiments, "_experiment_snapshot", lambda *_: {
        "journey": {"icp": {"who": customer, "problem": problem}}})

    reply = experiments.review_test_run("test", {"run": True, "experiment_id": "test"})

    assert f'ask about "{problem}"' in reply["body_md"]
    assert reply["body_md"].endswith("Next step: who will you interview first?")
    assert not any(action.get("confirmation") for action in reply["actions"])


@pytest.fixture(scope="module")
def app_modules(tmp_path_factory):
    with pytest.MonkeyPatch.context() as setup, service_modules("company_os"):
        setup.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path_factory.mktemp('first-minutes')}/copy.db")
        setup.setenv("LLM_OFFLINE", "1")
        from app.entry import chat_start
        from app.gstack import office_hours
        from app.intake import smart
        from app.ventures import current_idea
        yield SimpleNamespace(chat_start=chat_start, office_hours=office_hours, smart=smart, current_idea=current_idea)


def _paste(who, thing, problem, does, lead="I'm making"):
    return f"{lead} {thing} for {who}. They struggle because {problem}. {does}. Please research this idea and suggest the smallest useful test."


@pytest.mark.parametrize("who,thing,problem,does", DOMAINS)
def test_first_question_names_no_method(app_modules, who, thing, problem, does):
    office_hours = app_modules.office_hours
    snapshot = {"idea": f"{thing} for {who}"}
    picks = office_hours.default_picks(snapshot)
    assert picks
    for index, pick in enumerate(picks, 1):
        assert not BRANDING.search(pick["why"])
        options = {**pick, "phase": "gstack", "index": index, "total": len(picks)}
        turn = SimpleNamespace(id="ict_saved", text=pick["text"], options=json.dumps(options))
        body, actions = office_hours._reply(turn, snapshot)
        assert not BRANDING.search(body), body
        assert f"Question {index} of {len(picks)}" in body
        assert not any(BRANDING.search(action["label"]) for action in actions)
    assert not BRANDING.search(office_hours.HEADING + office_hours.ABOUT)


@pytest.mark.parametrize("audience", [
    "museum collections manager", "museum collections managers", "people managing museum collections",
    "parent caring for a newborn", "parents caring for newborns", "families with newborn children",
])
@pytest.mark.parametrize("question_id", ["Q1", "Q2", "Q5"])
def test_pressure_questions_fit_roles_groups_and_audience_phrases(app_modules, audience, question_id):
    from app.clarify.dilemmas import ground_question

    snapshot = {"_answered_fields": {"customer": audience}}
    question = ground_question({"gstack_id": question_id, "text": "Previous question about an unrelated topic."}, snapshot)
    expected = {
        "Q1": f"What evidence do you have that your audience ({audience}) needs help with this?",
        "Q2": f"How does your audience ({audience}) handle this today? Describe the current workaround.",
        "Q5": f"What have you observed about how your audience ({audience}) handles this?",
    }
    assert question["text"] == expected[question_id]


@pytest.mark.parametrize("idea,audience", [
    ("A tool for museum registrars that compares catalog records", "museum registrars"),
    ("An app for hobby gardeners that predicts frost dates", "hobby gardeners"),
])
@pytest.mark.parametrize("clarity", ["vague", "uncertain", "clear"])
def test_intake_asks_for_a_task_only_when_the_idea_needs_one(app_modules, idea, audience, clarity):
    from app.idea.agent import core_followup

    smart = app_modules.smart
    text = f"Something for {audience}" if clarity == "vague" else idea
    if clarity == "uncertain":
        text += ". I'm not sure what it should do yet."
    fallback = smart.classify_fallback(text)
    data = {**fallback.to_dict(), "confidence": .95, "next_step": {
        "situation": "vague_idea", "action": "ask", "field": "offer",
        "question": core_followup("offer", 0), "examples": ["Describe one task", "I'm not sure yet"],
    }}
    intake = smart._validated_result(data, text, smart.PageContext(), fallback)
    progress = app_modules.chat_start._onboarding_progress(intake.to_dict(), text)
    if clarity == "clear":
        assert intake.next_step is None
        assert "offer" in progress["completed"]
        assert progress.get("field") != "offer"
    else:
        assert intake.next_step is not None and intake.next_step.field == "offer"
        assert progress["question"] == core_followup("offer", 0)
        assert "offer" not in progress["completed"]


@pytest.mark.parametrize("name", ["Demand Reality", "Status Quo", "Desperate Specificity", "Narrowest Wedge", "Observation", "Future-Fit"])
def test_saved_question_keeps_its_stored_name_but_shows_a_plain_topic(app_modules, name):
    turn = SimpleNamespace(id="ict_old", text="Which one?", options=json.dumps({
        "phase": "gstack", "gstack_id": "Q1", "gstack_name": name, "index": 2, "total": 3, "options": ["One", "Two"]}))
    body, _ = app_modules.office_hours._reply(turn)
    assert name.casefold() not in body.casefold() or name == "Observation"
    assert not BRANDING.search(body)


@pytest.mark.parametrize("module", [
    "chat/idea_proposal.py", "chat/router.py", "chat/execute.py", "clarify/dilemmas.py",
    "onboarding/steps/understand.py", "onboarding/steps/validate.py",
])
def test_founder_reply_modules_have_no_method_names_in_strings(module):
    tree = ast.parse((ROOT / module).read_text())
    docs = {id(node.body[0].value) for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
            and node.body and isinstance(node.body[0], ast.Expr) and isinstance(node.body[0].value, ast.Constant)}
    leaks = [node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and id(node) not in docs and re.search(r"GStack|Garry|Y Combinator|YC Office", node.value)]
    assert leaks == []


@pytest.mark.parametrize("who,thing,_problem,_does", DOMAINS)
def test_suggested_answers_hide_the_assumption_marker(app_modules, who, thing, _problem, _does):
    text = f"{thing}\nClarification: {thing} that saves time"
    intake = app_modules.smart.classify_fallback(text).to_dict()
    intake["onboarding_options"] = {"customer": [f"Assumed: {who}", f"Assumed: Larger {who}"], "offer": [f"Assumed: {thing}"]}
    intake["extracted"] = {**intake.get("extracted", {}), "audience": ""}
    intake["next_step"] = None
    progress = app_modules.chat_start._onboarding_progress(intake, text)
    assert progress.get("choices"), progress
    assert not any("Assumed:" in choice for choice in progress["choices"])


def _summary(module, monkeypatch, text):
    monkeypatch.setattr(module, "current_idea", lambda *_a, **_k: text)
    return module.idea_summary(SimpleNamespace(), session=object())


@pytest.mark.parametrize("who,thing,problem,does", DOMAINS)
@pytest.mark.parametrize("lead", ["I'm making", "We are building", "I want to build", "I'm makng"])
def test_saved_idea_restates_a_paste_instead_of_echoing_it(app_modules, monkeypatch, who, thing, problem, does, lead):
    paste = _paste(who, thing, problem, does, lead)
    shown = _summary(app_modules.current_idea, monkeypatch, paste)
    assert shown == f"{thing[0].upper()}{thing[1:]} for {who}."
    assert lead.casefold() not in shown.casefold() and "Please" not in shown and problem not in shown


@pytest.mark.parametrize("who,thing,problem,does", DOMAINS)
def test_saved_idea_skips_a_website_aside(app_modules, monkeypatch, who, thing, problem, does):
    paste = "I own https://example.test. " + _paste(who, thing, problem, does)
    assert "example.test" not in _summary(app_modules.current_idea, monkeypatch, paste)


@pytest.mark.parametrize("who,thing,problem,does", DOMAINS)
def test_saved_idea_end_to_end_drops_labels_and_paste(app_modules, monkeypatch, who, thing, problem, does):
    from app.ventures import startup_review
    module = app_modules.current_idea
    joined = f"{_paste(who, thing, problem, does)}\nClarification: {thing} for {who}, because {problem}.\nClarification: {thing}"
    monkeypatch.setattr(module, "current_revision", lambda *_: SimpleNamespace(summary=joined, source="founder"))
    monkeypatch.setattr(module, "revision_fields", lambda _: {})
    monkeypatch.setattr(module, "latest_founder_statement", lambda *_: joined)
    monkeypatch.setattr(startup_review, "effective_fields", lambda _s, _v, fields: fields)
    shown = module.idea_summary(SimpleNamespace(raw_idea=joined), session=object())
    assert "Clarification:" not in shown and "Please" not in shown and "\n" not in shown
    assert shown == f"{thing[0].upper()}{thing[1:]} for {who}."


@pytest.mark.parametrize("background,offer,audience", [
    ("battery manufacturing", "a cell defect prediction platform", "quality leads at large battery factories"),
    ("cardiac rehabilitation", "a wearable recovery coach", "adults recovering after heart surgery"),
])
def test_saved_idea_summarizes_the_product_after_founder_background(app_modules, monkeypatch, background, offer, audience):
    from app.ventures import startup_review

    module = app_modules.current_idea
    opening = (f"My background is {background}. Maybe something around {offer}, for {audience}. "
               "The wedge and first product are still fuzzy. Can you help me work out where to start?")
    joined = opening + f"\nContext: {offer} for {audience}.\nClarification: {offer}\nClarification: {offer}"
    monkeypatch.setattr(module, "revision_fields", lambda _: {})
    monkeypatch.setattr(startup_review, "effective_fields", lambda _session, _venture, fields: fields)
    for revision in (None, SimpleNamespace(summary=joined, source="founder")):
        monkeypatch.setattr(module, "current_revision", lambda *_: revision)
        monkeypatch.setattr(module, "latest_founder_statement", lambda *_: joined if revision else "")
        shown = module.idea_summary(SimpleNamespace(raw_idea=joined), session=object())
        assert shown == f"{offer[0].upper()}{offer[1:]}, for {audience}."
        assert not any(label in shown for label in ("Context:", "Clarification:", "My background", "Can you"))


@pytest.mark.parametrize("text", [
    "Offer: Reads carrier invoices\nCustomer: Freight brokers",
    "A diary for new parents. It reminds both parents when a bottle is due.",
    "It tracks feeding times and reminds both parents when a bottle is due",
])
def test_summaries_and_saved_fields_stay_whole(app_modules, monkeypatch, text):
    assert _summary(app_modules.current_idea, monkeypatch, text) == text
