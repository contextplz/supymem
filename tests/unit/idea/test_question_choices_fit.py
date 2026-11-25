"""A question and its chips are one unit: chips carried over from another question are never shown."""

import json
import sys
from pathlib import Path
import pytest
from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "services" / "company_os"))
from app.idea.agent import Understanding, fitting_options, is_evidence_question, parse_result
from app.idea.runner import _previous_question_options


@pytest.mark.parametrize("replay", json.loads((Path(__file__).parent / "fixtures" / "first_question_replays.json").read_text()))
def test_first_question_replay_keeps_the_buyer_and_the_biggest_unknown(monkeypatch, replay):
    from app.idea.runner import _core_clarification, _known_fields, _question_reply

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    result = parse_result(json.dumps(replay["response"]), force_propose=False)
    understanding = result.understanding
    snapshot = {"project_intent": "startup", "original_idea": replay["idea"], "founder_idea": replay["idea"],
        "questions_asked": 0, "understanding": understanding.model_dump(),
        "answered_fields": {"offer": replay["idea"], "customer": replay["saved_customer"]}}
    assert _known_fields(snapshot, extracted=understanding.model_dump())["customer"] == replay["customer"]
    result = _core_clarification(snapshot, result)
    assert result.question is not None
    body, _ = _question_reply(result.question.text, result.question.model_dump(), context=snapshot)
    assert body.split("\n\n")[0] == replay["question"]
    assert replay["bad_question"] not in body
    assert result.understanding.customer == replay["customer"]
    assert body.count("?") == 1
    saved_context = {**snapshot, **understanding.model_dump(), "_answered_fields": snapshot["answered_fields"]}
    saved_context.pop("understanding")
    body, _ = _question_reply(result.question.text, result.question.model_dump(), context=saved_context)
    assert body.split("\n\n")[0] == replay["question"]


@pytest.mark.parametrize("audience,question", [
    ("Equipment rental operators", "Who at the equipment rental operators would approve paying for a scanner trial?"),
    ("Home gardeners", "Who would decide whether to pay for the watering device: the home gardener or their family?"),
])
def test_a_buying_decision_is_not_a_repeated_audience_question(monkeypatch, audience, question):
    from app.idea.agent import Clarification, Question, Understanding, answered_field
    from app.idea.runner import _core_clarification

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    assert answered_field(question, "customer") == "first_user_path"
    result = Clarification(case="know", action="question", message="Let's check who makes this decision.",
        understanding=Understanding(customer=audience),
        question=Question(text=question, field="customer", why="Find who decides whether to pay.", options=[]))
    updated = _core_clarification({"project_intent": "startup", "answered_fields": {"customer": audience}}, result)
    assert updated.question is not None and updated.question.text == question
    assert updated.question.field == "first_user_path"


def _question_reply(*args, **kwargs):
    from app.gstack.office_hours import SKIP
    from app.idea.runner import _question_reply as render

    body, actions = render(*args, **kwargs)
    controls = [action for action in actions or [] if action.get("text") == SKIP]
    assert all(action.get("action") == "chat.reply" and action.get("payload", {}).get("text") == SKIP
               and set(action["payload"]) == {"text", "question_id"} and not action.get("confirmation")
               for action in controls)
    return body, [action for action in actions or [] if action not in controls] or None


def test_skip_control_is_bound_to_the_question_and_not_an_answer_choice():
    from app.gstack.office_hours import SKIP
    from app.idea.runner import _question_reply as render

    for audience, options in (("Fleet mechanics", ["Review overdue inspections", "Record maintenance decisions"]),
                              ("Home gardeners", ["Check soil moisture", "Record watering dates"])):
        question = {"options": options.copy(), "field": "workflow"}
        text = f"Which task would you try first for {audience}?"
        body, actions = render(text, question, audience)
        controls = [action for action in actions if action.get("text") == SKIP]
        assert len(controls) == 1 and controls[0]["payload"] == {"text": SKIP, "question_id": audience}
        assert question["options"] == options
        choice_body, choices = _question_reply(text, question, audience)
        assert choice_body == body and [action["text"] for action in choices] == options


VENTURE_CONTEXTS = [
    {"customer": "Local window cleaners in Scotland", "problem": "Local window cleaners in Scotland lose booked appointments when they cannot reply promptly to requests across multiple channels.",
     "idea": "An enquiry board for local window cleaners", "phrase": "lost booked appointments"},
    {"customer": "New cat owners", "problem": "New cat owners face disrupted routines when a new pet wakes the household during the night.",
     "idea": "A household routine planner for new cat owners", "phrase": "disrupted routines"},
]


@pytest.mark.parametrize("audience,requirement", [
    ("laboratory purchasing managers", "Private storage for purchase evidence"),
    ("families planning camping trips", "Private storage for trip evidence"),
], ids=["b2b", "consumer"])
def test_requirements_options_are_checked_after_unsupported_claim_filtering(audience, requirement):
    text = f"What must your product do to help {audience}?"
    question = {"field": "requirements", "options": ["No evidence yet", "Cut costs by 80%"],
                "recommended_option": "No evidence yet", "recommendation_reason": "Start here."}
    body, actions = _question_reply(text, question, "turn_requirements", idea=f"A planning tool for {audience}.")
    assert question["options"] == [] and question["free_text_only"]
    assert question["recommended_option"] == question["recommendation_reason"] == ""
    assert "Type your own answer below." in body and actions is None
    for options in ([requirement], ["No evidence yet", requirement]):
        question = {"field": "requirements", "options": options,
                    "recommended_option": "No evidence yet", "recommendation_reason": "Start here."}
        body, actions = _question_reply(text, question, "turn_requirements", idea=f"A planning tool for {audience}.")
        assert question["options"] == [requirement]
        assert [action["text"] for action in actions] == [requirement]
        assert actions[0]["payload"]["recommended"] and "Select any that fit" in body


@pytest.mark.parametrize("audience,problem", [
    ("Freight dispatchers", "missing delivery receipts before the monthly billing deadline"),
    ("Home bakers", "discarded starter batches after a week of missed feedings"),
])
def test_contextual_choices_are_short_and_preserve_answer_payloads(audience, problem):
    from app.clarify.dilemmas import contextual_choices, detail_choices, ground_question
    from app.gstack.methodology import office_hours_options

    snapshot = {"_answered_fields": {"customer": audience, "problem": problem}}
    expected = {
        "Q1": ["I have no evidence yet.", "I have seen a real example.",
               "I have seen someone commit to addressing it."],
        "Q2": ["I know the tools or habits they use today.",
               "I know an existing product they use for this.",
               "I have not checked how they handle this today."],
        "Q3": ["I know a specific moment when they need help with this.",
               "I expect them to seek help when they need to prevent it.",
               "I expect them to seek help when they need support."],
    }
    for with_facts in (False, True):
        snapshot["founder_facts"] = ({
            "current_workaround": {"value": f"You use notes to handle {problem}."},
            "evidence": {"value": f"You observed {problem}."},
        } if with_facts else {})
        for question_id, slot in (("Q1", "goal_primary"), ("Q2", "market_reach"), ("Q3", "audience")):
            options = contextual_choices(question_id, snapshot)
            labels = expected[question_id].copy()
            if with_facts and question_id == "Q3":
                labels[1:] = ["I expect them to seek help when they use that workaround.",
                              "I expect them to seek help in the situation I described."]
            assert options == labels
            assert all(len(option) <= 70 and audience not in option and problem not in option
                       and "\u2014" not in option for option in options)
            schema = office_hours_options(slot, snapshot)
            assert [option["label"] for option in schema] == labels
            detail = labels[1:] if question_id == "Q1" else labels[:2 if question_id == "Q2" else 1]
            assert detail_choices(labels) == detail
            assert [option["label"] for option in schema if option["requires_detail"]] == detail
            question = ground_question({"gstack_id": question_id, "options": labels,
                                        "text": "What would you like to describe?", "requires_detail": detail,
                                        "contextual_evidence": question_id == "Q1"}, snapshot)
            assert question["text"].count(problem) == 0
            assert question["options"] == labels
            body, actions = _question_reply(question["text"], question, "ict_short", context=snapshot)
            assert body.count(problem) == 0
            assert [action["text"] for action in actions] == labels
            assert [action["payload"]["text"] for action in actions] == labels
            assert all(action["payload"]["question_id"] == "ict_short" for action in actions)
            assert [action["text"] for action in actions if action["payload"]["requires_detail"]] == detail
@pytest.mark.parametrize("audience,jobs", [
    ("Freight dispatchers", ["choose", "reconcile delivery invoices", "lost approval records"]),
    ("Home bakers", ["mix", "prepare weekend loaves", "disrupted baking routines"]),
])
def test_saved_jobs_are_not_inserted_into_question_grammar(audience, jobs):
    from copy import deepcopy
    from app.clarify.dilemmas import ground_question, venture_context

    texts = {
        "customer": "Who needs this first?",
        "offer": f"What should your product do to help {audience}?",
        "desired_outcome": f"What result would you want for {audience}?",
        "first_user_path": f"What would be the first step for {audience} to get help?",
        "pricing_hypothesis": f"How would you charge for helping {audience}?",
        "requirements": f"What must your product do to help {audience}?",
        "constraints": f"What limits must you work within to help {audience}?",
        "proposed_options": f"Which direction would best help {audience}?",
        "Q1": f"What evidence do you have that your audience ({audience}) needs help with this?",
        "Q2": f"How does your audience ({audience}) handle this today? Describe the current workaround.",
        "Q3": f"When is help most urgent for {audience}?",
        "Q4": f"What is the smallest useful version to help {audience}?",
        "Q5": f"What have you observed about how your audience ({audience}) handles this?",
        "Q6": f"What future changes could make help more useful for {audience}?",
    }
    for job in jobs:
        snapshot = {"_answered_fields": {"customer": audience, "workflow": job}}
        original = deepcopy(snapshot)
        assert venture_context(snapshot) == (audience, job)
        for field, text in texts.items():
            for source in ("Old wording?", f"What must your product do to help {audience} with {job}?"):
                question = {"field": field, "text": source, "options": []}
                grounded = ground_question(question, snapshot)
                assert grounded["text"] == text
                assert ground_question(grounded, snapshot) == grounded
                assert question["text"] == source
        assert snapshot == original


@pytest.mark.parametrize("audience,pain,goal", [
    ("Freight dispatchers", "missing delivery receipts", "validate demand before I hire anyone"),
    ("Home bakers", "unreliable dough timing", "explore demand before we invest savings"),
])
def test_founder_goals_do_not_become_customer_problems(audience, pain, goal):
    from app.clarify.dilemmas import contextual_choices, venture_context

    for value in (goal, f"I want to {goal}", f"We hope to {goal}"):
        for field in ("problem", "workflow", "desired_outcome", "priorities"):
            snapshot = {"_answered_fields": {"customer": audience, field: value}}
            assert venture_context(snapshot) == (audience, "")
            assert contextual_choices("Q2", snapshot) == []
        snapshot = {"customer": audience, "workflow": value, "problem": pain}
        assert venture_context(snapshot) == (audience, pain)
        assert all(goal not in option for option in contextual_choices("Q2", snapshot))
        assert venture_context({"customer": audience, "problem": f"{audience} face {pain} before I join them."}) == (audience, f"{pain} before I join them")


@pytest.mark.parametrize("audience,pain", [
    ("Small warehouse teams", "lost dispatch records"),
    ("Shift-working households", "missed dinners"),
])
def test_office_hours_labels_never_append_stale_founder_requests(audience, pain):
    from types import SimpleNamespace
    from app.clarify.dilemmas import _founder_request
    from app.clarify.generator import assert_invariants, generate_questions, refresh_question_copy
    from app.clarify.slots import office_hours_slots
    from app.gstack.methodology import FORCING_QUESTIONS, office_hours_options
    from app.gstack.office_hours import _reply, default_picks, parse_picks, question_turn_options
    from app.idea.service import serialize_turn

    message = "What should I focus on now?"
    snapshot = {"idea": message, "original_idea": f"A planning tool for {audience}. {audience} struggle with {pain}.",
                "customer": audience, "problem": message}
    for slot in FORCING_QUESTIONS:
        assert all(message not in option["label"] for option in office_hours_options(slot, snapshot))
    assert all("{product}" not in pick["text"] and pick["options"] == [] for pick in default_picks({"idea": message}))
    questions = generate_questions(office_hours_slots(), idea=message)
    assert_invariants(questions)
    assert all(message not in option["label"] for question in questions for option in question["options"])
    assert all(question.get("free_text_only") and not question["options"]
               for question in questions if question.get("gstack_id") in {"Q1", "Q2", "Q3"})

    text = "Which workflow should you test first?"
    valid = [f"Record requests from {audience}", f"Review {pain}"]
    for request in ("Can you help me choose one task?", "Which workflow should we test next?"):
        assert _founder_request(request)
        candidate = {"gstack_id": "Q4", "gstack_name": "Scope", "text": text, "why": "Choose a workflow",
                     "options": [f"One workflow for {audience}: {request}", *valid], "contextual_options": True}
        assert question_turn_options(candidate, [], 1, 1, snapshot)["options"] == valid
    assert not _founder_request("When we need to resolve conflicting records.")
    assert not _founder_request("Can reconcile records offline")
    assert not _founder_request("Get 10 paying customers")
    assert not _founder_request("Someone I can contact today")
    malformed = f"{FORCING_QUESTIONS['mvp_scope']['options'][0]['label']} for {audience}: {message}"
    refreshed = refresh_question_copy([{"id": "qst_stale", "slot_key": "mvp_scope", "options": [
        {"id": "opt_a", "label": malformed}]}], idea=message)
    assert refreshed[0]["id"] == "qst_stale" and refreshed[0]["options"] == [] and refreshed[0]["free_text_only"]
    question = {"gstack_id": "Q4", "gstack_name": "Scope", "text": text, "why": "Choose a workflow",
                "options": [malformed, *valid], "contextual_options": True}
    saved = question_turn_options(question, [], 1, 1, snapshot)
    assert saved["options"] == valid
    turn = SimpleNamespace(id="ict_stale", turn=1, role="agent", kind="question", text=text,
                           options=json.dumps(saved), chosen=None, revision_id=None, llm_usage=None)
    assert serialize_turn(turn, snapshot)["options"]["options"] == valid
    assert [action["label"] for action in _reply(turn, snapshot)[1][:-1]] == valid
    assert message in question["options"][0]
    for text, choices in [("Which provider do they hire?", ["A spreadsheet", "A provider"]),
                          ("Which step creates the most friction?", ["Review conflicting entries", "Reconcile incoming records"])]:
        raw = json.dumps({"questions": [
            {"id": "Q2", "question": text, "options": choices},
            {"id": "Q4", "question": "Which workflow should you test?", "options": valid},
        ]})
        pick = parse_picks(raw, snapshot)[0]
        if text.startswith("Which provider"):
            assert pick["text"] != text and all(audience not in option and pain not in option for option in pick["options"])
        else:
            assert pick["text"] == text and pick["options"] == choices
    words = "People use written notes to coordinate requests."
    for source, confidence in (("clarification", .95), ("website", .7)):
        context = {"customer": audience, "founder_facts": {"current_workaround": {
            "value": words, "quote": words, "source": source, "confidence": confidence}}}
        candidate = {"gstack_id": "Q2", "gstack_name": "Workarounds", "text": "What tools do they use today?",
                     "options": [], "why": "Understand the workaround"}
        saved = question_turn_options(candidate, [], 1, 1, context)
        turn = SimpleNamespace(id="ict_workaround", text=candidate["text"], options=json.dumps(saved))
        actions = _reply(turn, context)[1]
        assert saved["options"] == [words]
        assert actions[0]["label"] == (words if source == "clarification" else "Use: " + words)
        assert actions[0]["payload"]["prefill"] == words


@pytest.mark.parametrize("name,domain,audience,choices,statement,phrase", [
    ("LedgerLoom", "ledgerloom.example", "Small warehouse teams", ["Reconcile physical counts", "Update purchase records"],
     "They cannot reconcile deliveries.", "being unable to reconcile deliveries"),
    ("SupperParcel", "supperparcel.example", "Shift-working households", ["Choose portions", "Update dietary preferences"],
     "They spend five hours planning portions.", "spending five hours planning portions"),
])
def test_existing_product_goals_are_not_problems_and_specific_model_choices_survive(name, domain, audience, choices, statement, phrase):
    from app.clarify.dilemmas import contextual_choices, venture_context
    from app.gstack.methodology import office_hours_options
    from app.gstack.office_hours import parse_picks, question_turn_options

    message = f"I run {domain}, an existing product with paid accounts. Help me get more paying customers."
    goal = f"{name} needs more paying customers."
    snapshots = [{"idea": message, "problem": goal},
                 {"idea": message, "founder_facts": {"problem": {"value": goal, "quote": message}}},
                 {"idea": message, "customer": audience, "problem": goal},
                 {"idea": message, "customer": "the people you want to help", "problem": goal}]
    for snapshot in snapshots:
        assert venture_context(snapshot)[1] == ""
        assert contextual_choices("Q1", snapshot) == office_hours_options("goal_primary", snapshot) == []
        raw = json.dumps({"questions": [
            {"id": "Q1", "question": "Which existing behavior should you measure first?", "options": choices},
            {"id": "Q2", "question": "Which existing step creates the most friction?", "options": ["Manual entry", "Repeated checks"]},
        ]})
        for pick, expected in zip(parse_picks(raw, snapshot), (choices, ["Manual entry", "Repeated checks"])):
            saved = question_turn_options(pick, [], 1, 1, snapshot)
            assert saved["options"] == expected
            assert not any(name in option or message in option or goal in option for option in saved["options"])
        text = "What evidence of demand do you have?"
        empty = question_turn_options({"gstack_id": "Q1", "gstack_name": "Demand", "text": text, "why": "Check demand",
                                       "options": [text, text], "contextual_options": True}, [], 1, 1, snapshot)
        assert empty["options"] == [] and empty["free_text_only"] and not empty["recommended_option"]

    context = {"customer": audience, "problem": statement}
    assert venture_context(context) == (audience, phrase)
    assert all(phrase not in option for option in contextual_choices("Q1", context))
    assert not any(f"help with {statement}" in option for option in contextual_choices("Q1", context))


@pytest.mark.parametrize("audience,offer,workaround,consequence,expected", [
    ("Small warehouse teams", "A stock-count tool", "bin cards and emailed spreadsheets",
     "lose about 5 orders a week", "lost about 5 orders a week"),
    ("Shift-working households", "Weekly ingredient boxes", "recipe clippings and grocery notes",
     "miss 2 dinners a week", "missed 2 dinners a week"),
    ("Independent mobile appliance technicians in Canada", "A booking tool", "appointment sheets and social inboxes",
     "lose around 2 reservations per week", "lost around 2 reservations per week"),
])
def test_venture_context_extracts_pronoun_pain_and_mixed_workarounds(audience, offer, workaround, consequence, expected):
    from app.clarify.dilemmas import contextual_choices, venture_context

    stated = f"They juggle {workaround} and {consequence}."
    idea = f"{offer} for {audience}. {stated} $37/month. I talked to 5 people; 4 would try it."
    snapshots = [{"idea": idea}, {"original_idea": idea},
                 {"customer": audience, "problem": stated},
                 {"founder_facts": {"audience": {"value": audience}, "problem": {"value": stated}}},
                 {"customer": audience, "founder_facts": {"problem": {"value": "They use notes.", "quote": stated}}},
                 {"customer": audience, "founder_facts": {"current_workaround": {"value": stated}}},
                 {"customer": audience, "problem": stated, "founder_facts": {
                     "audience": {"value": "What should I focus on now?"}, "problem": {"value": "What should I focus on now?"}}},
                 {"problem": "What should I focus on now?", "original_idea": f"Customer: {audience}\nProblem: {stated}"}]
    for snapshot in snapshots:
        assert venture_context(snapshot) == (audience, expected)
        assert all(expected not in option for option in contextual_choices("Q3", snapshot))


@pytest.mark.parametrize("audience,problem,workaround,evidence", [
    ("Small warehouse teams", "They lose orders when bin counts drift.", "Bin cards and emailed spreadsheets",
     "I talked to 5 supervisors; 4 would try it."),
    ("Shift-working households", "They miss dinners when shopping plans change.", "Recipe clippings and grocery notes",
     "I talked to 6 households; 3 would try it."),
    ("Independent mobile appliance technicians in Canada", "They lose reservations when messages arrive during visits.",
     "Appointment sheets and social inboxes", "I talked to 4 operators; 3 would try it."),
])
def test_option_pipeline_keeps_specific_choices_and_uses_grounded_or_open_fallbacks(audience, problem, workaround, evidence):
    from types import SimpleNamespace
    from app.clarify.dilemmas import contextual_choices
    from app.idea.agent import Clarification, Question, Understanding, parse_result
    from app.idea.runner import _core_clarification
    from app.idea.service import serialize_turn

    saved = {field: {"value": value, "quote": value, "source": "founder message", "confidence": .95}
             for field, value in {"audience": audience, "problem": problem, "current_workaround": workaround,
                                  "evidence": evidence, "progress": "Zero paying customers"}.items()}
    snapshot = {"founder_facts": saved, "active_core_field": "workflow"}
    text = "When does this become urgent?"
    options = [f"When no current tools coordinate {workaround.lower()}.",
               f"When 3 customers ask for help with {problem.lower()}",
               "When we reach 10 paying customers."]
    content = Clarification(case="know", action="question", message="Choose a situation", understanding=Understanding(),
        question=Question(text=text, field="workflow", why="Find the situation", options=options,
                          recommended_option=options[0])).model_dump_json()
    parsed = _core_clarification(snapshot, parse_result(content, force_propose=False))
    assert parsed.question.options == options
    question = parsed.question.model_dump()
    turn = SimpleNamespace(id="ict_pipeline", turn=1, role="agent", kind="question", text=text,
                           options=json.dumps(question), chosen=None, revision_id=None, llm_usage=None)
    assert serialize_turn(turn, snapshot)["options"]["options"] == options
    assert [action["text"] for action in _question_reply(text, question, turn.id, facts=saved)[1]] == options

    question["options"] = []
    body, actions = _question_reply(text, question, turn.id, facts=saved)
    assert question["options"] == contextual_choices("Q3", snapshot)
    assert question["options"][1:] == ["I expect them to seek help when they use that workaround.",
                                      "I expect them to seek help in the situation I described."]
    assert all(workaround not in option and evidence.rstrip(".") not in option for option in question["options"])
    assert not any(fragment in body + " ".join(action["text"] for action in actions)
                   for fragment in ("A deadline creates urgency", "An unexpected difficulty", "usual approach fails"))
    context_question = {"options": []}
    _question_reply(text, context_question, turn.id, context=snapshot)
    assert context_question["options"] == question["options"]
    stated_rate = f"{audience} lose 25% of their weekly capacity."
    rate_question = {"options": [stated_rate, options[0]]}
    _question_reply(text, rate_question, turn.id, context={"idea": "What should I focus on now?", "original_idea": stated_rate})
    assert rate_question["options"] == [stated_rate, options[0]]

    empty_snapshot = {"idea": f"A planning tool for {audience}."}
    for question_id in ("Q1", "Q2", "Q3"):
        assert contextual_choices(question_id, empty_snapshot) == []
    question = {"options": [], "field": "workflow", "recommended_option": "Old", "recommendation_reason": "Old"}
    body, actions = _question_reply(text, question, turn.id, idea=empty_snapshot["idea"])
    assert body == text + "\n\nType your own answer below."
    assert actions is None and question["options"] == [] and question["free_text_only"]
    turn.options = json.dumps(question)
    rendered = serialize_turn(turn, empty_snapshot)["options"]
    assert rendered["options"] == [] and rendered["recommended_option"] == rendered["recommendation_reason"] == ""


@pytest.mark.parametrize("audience,problem,workaround,choice", [
    ("Independent accountants", "They lose receipts during month-end close.", "Paper folders",
     "When a receipt is missing before a filing deadline"),
    ("New parents", "They miss naps when daily routines change.", "Handwritten sleep diaries",
     "When a nap overlaps with an outing"),
])
@pytest.mark.parametrize("source", ["founder message", "website"])
def test_reply_fallback_uses_prefill_facts_and_keeps_remaining_grounded_choice(audience, problem, workaround, choice, source):
    from types import SimpleNamespace
    from app.clarify.dilemmas import contextual_choices
    from app.idea.service import serialize_turn

    facts = {field: {"value": value, "quote": value, "source": source, "confidence": .95}
             for field, value in {"audience": audience, "problem": problem, "current_workaround": workaround}.items()}
    context = ({"prefill_facts": {"customer": facts["audience"], "problem": facts["problem"],
                                  "current_solution": facts["current_workaround"]}}
               if source == "website" else {"founder_facts": facts})
    text = "When does this become urgent?"
    expected = contextual_choices("Q3", {"founder_facts": facts})
    for options, wanted in (([], expected), ([choice, "Help me get more paying customers"], [choice])):
        question = {"field": "workflow", "options": options}
        body, actions = _question_reply(text, question, "ict_grounded", facts={}, context=context)
        assert question["options"] == wanted
        assert [action["text"] for action in actions] == wanted
        assert "Type your own answer below." not in body
        turn = SimpleNamespace(id="ict_grounded", turn=1, role="agent", kind="question", text=text,
                               options=json.dumps(question), chosen=None, revision_id=None, llm_usage=None)
        assert serialize_turn(turn, context)["options"]["options"] == wanted


@pytest.mark.parametrize("audience,pain,alternative", [
    ("Veterinary practices in Arizona", "lost appointment deposits", "a reception ledger and callback lists"),
    ("Cyclists in Denmark", "missed safe routes", "laminated maps and local club messages"),
])
def test_workaround_and_sentence_problems_fall_back_to_idea_or_unmet_need(audience, pain, alternative):
    from app.clarify.dilemmas import contextual_choices, venture_context

    idea = f"A planning app for {audience}. {audience} struggle with {pain}."
    unsafe = [f"Most of the {audience.lower()} rely on {alternative} for updates.",
              f"They use {alternative}.",
              f"We coordinate everything with {alternative}.",
              f"{audience} use {alternative}.",
              f"{audience} rely on {alternative} for every update, and nobody checks the latest changes; after a busy day the whole group compares their records and resolves disagreements before they can get on with anything else."]
    for value in unsafe:
        for source in ("founder_facts", "problem"):
            snapshot = {"customer": audience, source: {"problem": {"value": value}} if source == "founder_facts" else value}
            for description, expected in ((idea, pain), (f"A service for {audience}.", ""), ("", "")):
                context = {**snapshot, "idea": description}
                assert venture_context(context) == (audience, expected)
                for question_id in ("Q1", "Q2", "Q3"):
                    options = contextual_choices(question_id, context)
                    assert options == contextual_choices(question_id, {"customer": audience, "problem": expected})
                    assert len(options) == (3 if expected else 0)
                    assert all(audience not in option and alternative not in option and option.endswith(".") for option in options)
                    assert all("  " not in option and "\u2014" not in option for option in options)


@pytest.mark.parametrize("snapshot", VENTURE_CONTEXTS)
def test_gstack_filters_restore_contextual_choices_and_free_text_copy(snapshot):
    from types import SimpleNamespace
    from app.db.supymem_models import ClarifierTurn
    from app.gstack.office_hours import _reply, question_turn_options
    from app.idea.service import serialize_turn

    text = "Which task should you build first?"
    for options in ([], ["Same", "same"], [text, "Something else"],
                    ["No evidence yet", "Some evidence to describe"], ["Industry average is 75%", "Studies show 90% succeed"]):
        question = {"gstack_id": "Q4", "gstack_name": "Scope", "text": text, "why": "Choose your first task",
                    "options": options, "contextual_options": True}
        saved = question_turn_options(question, [], 1, 1, snapshot)
        turn = ClarifierTurn(id="ict_fallback", role="agent", kind="question", text=text, options=json.dumps(saved))
        rendered = serialize_turn(turn, snapshot)["options"]
        assert saved["options"] == rendered["options"] == []
        assert saved["free_text_only"] and rendered["free_text_only"]
        body, actions = _reply(turn, snapshot)
        assert "Type or speak your description below" in body
        assert len(actions) == 1
        assert not any("%" in action["text"] for action in actions)
    for flag in ("detail_pending", "free_text_only"):
        turn = SimpleNamespace(id="ict_detail", text="Describe your example", options=json.dumps({**saved, "prefill": snapshot["customer"], flag: True}))
        body, actions = _reply(turn, snapshot)
        assert "Type or speak your description below" in body
        assert len(actions) == 1


@pytest.mark.parametrize("snapshot", VENTURE_CONTEXTS)
def test_urgency_choices_are_triggering_moments_not_evidence(snapshot):
    from app.clarify.dilemmas import contextual_choices
    from app.db.supymem_models import ClarifierTurn
    from app.gstack.office_hours import _reply, question_turn_options
    from app.idea.service import serialize_turn

    snapshot = {**snapshot, "founder_facts": {"audience": {"value": snapshot["customer"], "quote": snapshot["customer"],
                                                         "source": "clarification", "confidence": .95}}}
    for text in ("What makes this problem urgent in their day-to-day work?", "When does this become urgent?",
                 "What triggering moment makes this time-sensitive?"):
        for options in (["No evidence yet", "Some evidence to describe"], ["We logged their struggles", "An owner told me about it"]):
            assert not is_evidence_question(text, options)
            assert fitting_options(text, options) == []
            question = {"gstack_id": "Q3", "gstack_name": "Urgency", "text": text, "why": "Find the triggering moment",
                        "options": options, "contextual_options": True}
            saved = question_turn_options(question, [], 1, 1, snapshot)
            turn = ClarifierTurn(id="ict_urgency", role="agent", kind="question", text=text, options=json.dumps(saved))
            expected = contextual_choices("Q3", snapshot)
            assert saved["options"] == serialize_turn(turn, snapshot)["options"]["options"] == expected
            assert all(option.startswith("I ") and " when " in option.casefold()
                       and snapshot["phrase"] not in option for option in expected)
            assert [action["text"] for action in _reply(turn, snapshot)[1][:-1]] == expected


@pytest.mark.parametrize("snapshot", VENTURE_CONTEXTS)
def test_venture_context_is_a_clean_phrase_with_word_boundary_clipping(snapshot):
    from app.clarify.dilemmas import venture_context
    from app.gstack.office_hours import render_question

    audience, problem = venture_context(snapshot)
    assert audience == snapshot["customer"] and problem == snapshot["phrase"]
    question = render_question({"gstack_id": "Q2", "gstack_name": "Workarounds", "text": "What do they use today?",
                               "why": "", "options": []}, snapshot)
    assert question["why"].count(audience) == 1
    assert problem in question["why"] and "when they" not in question["why"]
    long_audience = audience + " who manage several demanding responsibilities every day of the week"
    long_problem = "inconsistent planning across several demanding and unexpected daily responsibilities with other people in the household and"
    clipped_audience, clipped_problem = venture_context({**snapshot, "customer": long_audience, "problem": long_problem}, clip_audience=True)
    assert len(clipped_audience) <= 60 and len(clipped_problem) <= 85
    assert long_audience.startswith(clipped_audience + " ") and long_problem.startswith(clipped_problem + " ")
    assert clipped_problem.split()[-1] not in {"with", "and", "in", "the", "of", "to"}
    scoped = render_question({"gstack_id": "Q4", "gstack_name": "Scope", "text": "What should you build first?",
                              "why": "", "options": []}, {**snapshot, "customer": long_audience, "problem": long_problem})
    assert scoped["options"] == [] and scoped["free_text_only"]
    assert all(venture_context({"customer": "x" * 100, "problem": "y" * 100}))


@pytest.mark.parametrize("audience,problem", [
    ("regional freight dispatchers with 10 to 40 trucks operating across northern Europe", "double-booked loads"),
    ("home sourdough bakers with several starters sharing feeding duties with their families", "forgotten starter feedings"),
])
def test_pressure_test_preserves_the_complete_saved_audience(audience, problem):
    from app.clarify.dilemmas import ground_question, venture_context
    from app.gstack.office_hours import render_question

    snapshot = {"customer": audience, "problem": problem, "_answered_fields": {"customer": audience, "problem": problem}}
    question = {"gstack_id": "Q4", "gstack_name": "Scope", "text": "What should you build first?", "options": [], "why": ""}
    for rendered in (ground_question(question, snapshot), render_question(question, snapshot)):
        assert rendered["text"] == f"What is the smallest useful version to help {audience}?"
        assert venture_context(snapshot) == (audience, problem)
        assert "\u2014" not in rendered["text"]


@pytest.mark.parametrize("idea", ["Invoice reconciliation for finance teams", "A tent rental marketplace for campers"])
def test_saved_office_hours_retry_explains_the_restored_question(idea):
    from app.gstack.office_hours import render_question

    question = {"gstack_id": "Q4", "text": "", "options": [], "retry": 1, "explain": "Old question"}
    rendered = render_question(question, {"idea": idea})
    assert rendered["text"] and rendered["text"] in rendered["explain"]
    assert "Old question" not in rendered["explain"]


@pytest.mark.parametrize("snapshot, expected_problem", [
    ({"name": "CampCircle", "customer": "Weekend campers",
      "idea": "CampCircle: a marketplace where weekend campers rent tents from nearby owners."}, ""),
    ({"name": "AuditBeacon", "customer": "Finance teams",
      "idea": "AuditBeacon: a tool that reconciles vendor invoices for finance teams."}, ""),
    ({"name": "CampCircle", "customer": "a weekend camper",
      "idea": "CampCircle: a marketplace where a weekend camper rents tents from nearby owners."}, ""),
    ({"name": "AuditBeacon", "customer": "a finance manager",
      "idea": "AuditBeacon: a tool that reconciles vendor invoices for a finance manager."}, ""),
    ({"name": "AuditBeacon", "customer": "Finance teams",
      "idea": "AuditBeacon: invoice reconciliation tool for finance teams."}, ""),
    ({"name": "CampCircle", "customer": "Weekend campers",
      "idea": "CampCircle: a tent rental marketplace. Weekend campers struggle with unreliable equipment availability."},
     "unreliable equipment availability"),
    ({"name": "AuditBeacon", "customer": "Finance teams",
      "idea": "AuditBeacon: an invoice reconciliation tool. Finance teams lose approval records when spreadsheets drift."},
     "lost approval records"),
    ({"name": "CampCircle", "customer": "Weekend campers", "idea": "CampCircle: a tent rental marketplace.",
      "founder_facts": {"problem": {"value": "Weekend campers face unreliable equipment availability when preparing for a trip."}}},
     "unreliable equipment availability"),
    ({"name": "AuditBeacon", "customer": "Finance teams", "idea": "AuditBeacon: an invoice reconciliation tool.",
      "problem": "Finance teams lose approval records when spreadsheets drift."}, "lost approval records"),
    ({"name": "CampCircle", "customer": "Weekend campers", "idea": "CampCircle: a tent rental marketplace.",
      "problem": "Unreliable equipment availability for weekend campers"}, "Unreliable equipment availability for weekend campers"),
    ({"name": "AuditBeacon", "customer": "Finance teams", "idea": "AuditBeacon: an invoice reconciliation tool.",
      "problem": "Lost approval records for finance teams"}, "Lost approval records for finance teams"),
])
def test_question_choices_use_problems_not_pitches(snapshot, expected_problem):
    from app.clarify.dilemmas import contextual_choices, venture_context
    from app.gstack.methodology import office_hours_options
    from app.gstack.office_hours import question_turn_options

    audience, problem = venture_context(snapshot)
    assert audience == snapshot["customer"] and problem == expected_problem
    for question_id, slot, text in (("Q1", "goal_primary", "What evidence of demand can you describe?"),
                                    ("Q2", "market_reach", "What tools do they use today?"),
                                    ("Q3", "audience", "When does this become urgent?")):
        options = contextual_choices(question_id, snapshot)
        assert len(options) == (3 if problem else 0)
        if problem:
            assert all(problem not in option for option in options)
        else:
            assert options == []
        for option in options:
            assert audience not in option and option.endswith(".")
            assert not any(fragment in option.casefold() for fragment in (
                snapshot["name"].casefold(), "a marketplace where", "rent tents", "reconciles vendor invoices",
                "your idea", "the problem you described", "  ", "\u2014"))
        question = {"gstack_id": question_id, "gstack_name": "Check", "text": text, "why": "",
                    "options": [], "contextual_options": True}
        saved = question_turn_options(question, [], 1, 1, snapshot)
        assert saved["options"] == options
        assert "  " not in saved["why"]
        schema = office_hours_options(slot, snapshot)
        assert [option["label"] for option in schema] == options
        assert all(" of  for " not in option["implication"] for option in schema)


def _content(options, text="Who pays first?"):
    return json.dumps({"case": "know", "action": "question", "message": "ok", "confidence": 0.3,
                       "understanding": Understanding().model_dump(),
                       "question": {"text": text, "field": "customer", "why": "w", "options": options,
                                    "selection_mode": "single"}})


def test_options_reused_from_previous_question_are_dropped():
    parsed = parse_result(_content(["search ads", "Cold email"]), force_propose=False,
                          previous_options=["Search ads", "Referrals"])
    assert parsed.question.options == []  # mismatched chips: no chips, the founder types
    assert parsed.question.recommended_option == ""


def test_fresh_distinct_options_are_kept_and_dupes_or_echo_dropped():
    assert parse_result(_content(["Dentists", "Gyms"]), force_propose=False,
                        previous_options=["Search ads"]).question.options == ["Dentists", "Gyms"]
    assert fitting_options("Q?", ["A", "a"]) == []
    assert fitting_options("Q?", ["q?", "B"]) == []
    assert fitting_options("Q?", ["only one"]) == []


@pytest.mark.parametrize("options", [
    ["No evidence yet", "Some evidence to describe"],
    ["I heard dog owners struggle", "No evidence yet"],
    ["After work", "During the first walk"],
])
def test_u36_moment_questions_keep_only_situation_options(options):
    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import serialize_turn
    text = "When do first-time dog owners feel most frustrated?"
    expected = options if options[0] == "After work" else []
    assert not is_evidence_question(text, options)
    parsed = parse_result(_content(options, text), force_propose=False)
    assert parsed.question.options == expected
    body, actions = _question_reply(text, {"options": options}, "ict_moment")
    labels = [action["label"] for action in actions or []]
    assert labels == expected
    if not expected:
        assert actions is None and "Type your own answer" in body
    assert not is_evidence_question(text, labels)
    turn = ClarifierTurn(role="agent", kind="question", text=text, options=json.dumps({"options": options}))
    assert serialize_turn(turn)["options"]["options"] == labels


@pytest.mark.parametrize("text,options,evidence", [
    ("Have you talked to structural engineers?", ["I interviewed two engineers", "Not yet"], True),
    ("Has any engineering firm paid for drawing checks?", ["One pays already", "Not yet"], True),
    ("How many active users do you have?", ["Ten engineers", "None"], True),
    ("How many paying customers?", ["Ten customers", "None"], True),
    ("What evidence do you have?", ["Customer receipts", "Not yet"], True),
    ("Which best describes your progress?", ["I've interviewed engineers", "Not yet"], True),
    ("What have you observed during drawing reviews?", ["Missed clashes", "Nothing yet"], True),
    ("Have you actually sat down and watched someone use this?", ["I watched a review", "Not yet"], True),
    ("Which best describes your traction?", ["We have 10 paying customers", "Not yet"], True),
    ("How do engineers review drawings today?", ["Manual checks", "Review software"], False),
    ("What do engineers fear most?", ["Missed errors", "Tender delays"], False),
    ("What would the first version do?", ["Catch drawing errors", "Compare revisions"], False),
    ("Have you chosen a first workflow?", ["Drawing checks", "Tender preparation"], False),
    ("What did you want the first version to do?", ["Drawing checks", "Tender preparation"], False),
    ("Which channel should we test first?", ["Contact engineering firms", "Search ads"], False),
    ("How should customers pay?", ["Subscription", "Per drawing"], False),
    ("How many customers should we target?", ["Ten customers", "One hundred customers"], False),
    ("What evidence should we collect first?", ["Interview notes", "Paid trials"], False),
    ("In three years, what would make this essential?", ["More complex drawings", "Stricter standards"], False),
])
def test_evidence_intent_does_not_overwrite_other_gstack_choices(text, options, evidence):
    from app.gstack.office_hours import parse_picks
    assert is_evidence_question(text, options) is evidence
    fitted = fitting_options(text, options)
    assert (fitted != options) is evidence
    picks = parse_picks(json.dumps({"questions": [
        {"id": "Q1", "question": text, "options": options},
        {"id": "Q4", "question": "Which workflow should we build first?", "options": ["Review drawings", "Compare changes"]},
    ]}))
    assert picks[0]["options"] == fitted
    assert picks[1]["options"] == ["Review drawings", "Compare changes"]


@pytest.mark.parametrize("text,options", [
    ("How do shop owners track orders?", ["Paper notebooks", "I'm not sure", "Spreadsheets"]),
    ("What do owners fear most?", ["Losing repeat customers", "I'm not sure", "Missing orders"]),
])
def test_customer_workflow_hypotheses_keep_generated_choices_and_recommendation(text, options):
    from app.idea.agent import Question
    question = Question(text=text, field="workflow", why="Choose a hypothesis to test", options=options,
        recommended_option=options[0])
    assert fitting_options(text, question.options) == options
    assert question.recommended_option == options[0]
    assert question.recommendation_reason == "Pick this as a starting point, then change it if needed."


def test_chip_carries_its_question_id_and_previous_options_come_from_latest_question():
    _, actions = _question_reply("Which?", {"options": ["A", "B"]}, "ict_1")
    assert all(a["payload"]["question_id"] == "ict_1" for a in actions)

    class T:
        def __init__(self, role, kind, options):
            self.role, self.kind, self.options = role, kind, options

    turns = [T("founder", "answer", None), T("agent", "question", json.dumps({"options": ["X", "Y"]})),
             T("agent", "question", json.dumps({"options": ["old"]}))]
    assert _previous_question_options(turns) == ["X", "Y"]


def test_chat_labels_show_full_option_and_recommendation():
    option = "Start with the independent dental practices already using paper appointment calendars" * 2
    body, actions = _question_reply("Which customer first?", {
        "options": [option, "Large clinics"], "option_labels": ["Dentists", "Clinics"],
        "recommended_option": option, "recommendation_reason": "You can reach these practices this week.",
    }, "ict_1")
    assert actions[0]["label"] == option
    assert actions[0]["payload"]["recommended"] is True
    assert actions[1]["payload"]["recommended"] is False
    assert "You can reach these practices this week." not in body
    assert actions[0]["payload"]["recommendation_reason"] == "You can reach these practices this week."


def test_u50_suggested_option_join_has_one_period_without_rewriting_saved_choices():
    from types import SimpleNamespace
    from app.gstack.office_hours import _reply
    option = "I haven't tried anything else."
    question = {"options": [option, "Other"], "recommended_option": option,
                "recommendation_reason": "First-time founders can start here."}
    body, actions = _question_reply("Which path first?", question, "ict_saved")
    assert "Suggested:" not in body and "First-time" not in body
    assert actions[0]["payload"]["recommendation_reason"] == "First-time founders can start here."
    assert "else.." not in body
    assert actions[0]["payload"]["text"] == option
    turn = SimpleNamespace(id="ict_saved", text="Which path first?", options=json.dumps({**question,
        "phase": "gstack", "gstack_id": "Q1", "gstack_name": "Demand Reality", "index": 2, "total": 3}))
    body, actions = _reply(turn)
    assert "else.." not in body
    assert actions[0]["payload"]["text"] == option


def test_saved_evidence_question_is_safe_in_chat_and_idea_page():
    from types import SimpleNamespace
    from app.idea.service import serialize_turn
    question = {"options": ["I've had a couple of owners promise to pay verbally", "I'm not sure"],
                "recommended_option": "I've had a couple of owners promise to pay verbally",
                "recommendation_reason": "Use this conservative assumption until you have evidence."}
    text = "Have you talked to shop owners / did anyone promise to pay?"
    body, actions = _question_reply(text, question, "ict_saved")
    assert [action["payload"]["recommended"] for action in actions] == [True, False]
    assert [action["label"] for action in actions] == ["No one yet", "A few people"]
    assert "Suggested:" not in body
    assert "conservative assumption" not in body
    turn = SimpleNamespace(id="ict_saved", turn=1, role="agent", kind="question", text=text,
                           options=json.dumps(question), chosen=None, revision_id=None, llm_usage=None)
    serialized = serialize_turn(turn)
    assert serialized["options"]["recommended_option"] == "No one yet"
    assert json.loads(turn.options) == question


def test_overlong_question_is_rejected_for_repair_instead_of_cut_off():
    with pytest.raises(ValidationError):
        parse_result(_content(["Dentists", "Gyms"], text="Which customer? " * 100), force_propose=False)


def test_blank_saved_question_has_display_text_without_rewriting_history():
    from types import SimpleNamespace
    from app.idea.service import serialize_turn
    turn = SimpleNamespace(id="blank", turn=3, role="agent", kind="question", text=" ",
        options=json.dumps({"options": ["Body shops"], "phase": "gstack", "gstack_id": "Q3"}), chosen=None, revision_id=None, llm_usage=None)
    assert serialize_turn(turn)["text"].strip()
    assert turn.text == " "


def test_clarifier_options_drop_statistics_the_founder_never_gave_across_domains():
    for text, invented, stated, kept in (
        ("What pain made the pilots try it?", "They lose 5-15% of project revenue to unclaimed changes.",
         "They lose 12% of revenue to missed claims.", "A dispute where the report was too vague."),
        ("Why do clinics want reminders?", "Studies show half of patients forget appointments.",
         "Half of our patients forget appointments.", "The front desk spends mornings calling patients."),
    ):
        facts = {"problem": {"value": stated, "quote": stated, "source": "founder message"}}
        question = {"options": [invented, stated, kept], "recommended_option": kept,
                    "recommendation_reason": "Let the founder describe what really happened."}
        _, actions = _question_reply(text, question, "turn_x", facts=facts)
        labels = [action["label"] for action in actions]
        assert invented not in labels and stated in labels and kept in labels
        reasons = [action["payload"]["recommendation_reason"] for action in actions if action["payload"]["recommendation_reason"]]
        assert all("the founder" not in reason.lower() for reason in reasons)
@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
@pytest.mark.parametrize("field", ["problem", "workflow", "desired_outcome"])
def test_saved_answer_context_supersedes_raw_and_cached_context(audience, job, field):
    from app.clarify.dilemmas import ground_question, venture_context
    context = {"idea": "A tool for museum curators to catalogue artifacts", "customer": "Museum curators",
        "problem": "catalogue artifacts", "founder_facts": {
            "audience": {"value": "Museum curators"}, "problem": {"value": "catalogue artifacts"}},
        "_answered_fields": {"customer": audience, field: job}}
    assert venture_context(context) == (audience, job)
    paired = {**context, "answered_questions": [{"field": "audience",
        "founder_text": f"Audience: {audience}\nWorkflow: {job}", "phase": "clarification"}]}
    assert venture_context(paired) == (audience, job)
    wrong = f"What would {audience} pay for {job.split(' ', 1)[1]}?"
    grounded = ground_question({"field": "first_user_path", "text": wrong, "options": []}, context)
    assert grounded["text"] != wrong
    assert grounded["text"] == f"What would be the first step for {audience} to get help?"
    parts = audience.split()
    wrong_audience = " ".join([parts[0], "software", *parts[1:]])
    wrong = f"What would be the first step for {wrong_audience} to get help with {job}?"
    grounded = ground_question({"field": "first_user_path", "text": wrong, "options": []}, context)
    assert audience in grounded["text"] and wrong_audience not in grounded["text"]
    assert venture_context({**context, "_answered_fields": {"customer": audience, field: "not sure"}}) == (audience, "")
    assert venture_context({**context, "_answered_fields": {"customer": "not sure", field: job}})[0] == "the people you want to help"
    assert venture_context({**context, "_answered_fields": {"customer": audience, "offer": "not sure"}}) == (audience, "")


@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
def test_sensitive_answers_never_become_question_context_or_choices(audience, job):
    from app.clarify.dilemmas import ground_question, venture_context
    from app.secrets.detect import looks_like_secret
    sensitive = "sk-abcdefghijklmnopqrstuvwx"
    assert looks_like_secret(sensitive)
    context = {"_answered_fields": {"customer": audience, "workflow": job}}
    assert venture_context({**context, "_answered_fields": {"customer": sensitive, "workflow": job}}) == (
        "the people you want to help", job)
    assert venture_context({**context, "_answered_fields": {"customer": audience, "workflow": sensitive}}) == (audience, "")
    question = {"field": "current_workaround", "text": f"What do {audience} use today to {job}?", "options": [sensitive]}
    grounded = ground_question(question, {**context, "founder_facts": {"current_workaround": {"value": sensitive}}})
    assert sensitive not in str(grounded)


@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
def test_saved_founder_facts_ground_questions_without_a_clarifier_answer(audience, job):
    from app.clarify.dilemmas import ground_question, venture_context
    from app.ventures.founder_facts import _fact
    snapshot = {"customer": "Museum curators", "workflow": "catalogue artifacts",
        "founder_facts": {"audience": _fact(audience), "problem": _fact(job)},
        "founder_fact_updated_at": {"audience": "2026-01-01T12:00:00", "problem": "2026-01-01T12:00:00"},
        "revision_created_at": "2026-01-01T13:00:00"}
    assert venture_context(snapshot) == (audience, job)
    question = {"gstack_id": "Q2", "text": "What do museum curators use to catalogue artifacts?",
        "options": ["Museum curators use collection cards"]}
    grounded = ground_question(question, snapshot)
    assert grounded["text"] == f"How does your audience ({audience}) handle this today? Describe the current workaround."
    assert "museum" not in str(grounded).casefold() and "artifacts" not in str(grounded).casefold()


@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
def test_selected_workflow_does_not_invent_an_audience_answer(audience, job):
    from app.clarify.dilemmas import venture_context
    snapshot = {"_answered_fields": {"customer": audience}, "answered_questions": [
        {"field": "workflow", "founder_text": "", "suggested_text": f"Workflow: {job}\nAudience: Museum curators",
         "provenance": "suggested_choice"}]}
    assert venture_context(snapshot) == (audience, job)


@pytest.mark.parametrize("audience,job", [
    ("Freight coordinators", "reconcile shipment charges"),
    ("Parents planning dinners", "plan weekly family menus"),
])
def test_old_workaround_cannot_relabel_latest_question_audience(audience, job):
    from app.clarify.dilemmas import ground_question
    from app.ventures.founder_facts import _fact
    previous = "Museum curators use collection cards"
    snapshot = {"_answered_fields": {"customer": audience, "workflow": job},
        "answered_field_updated_at": {"audience": "2026-01-01T12:00:00", "workflow": "2026-01-01T12:00:00"},
        "founder_facts": {"current_workaround": _fact(previous)},
        "founder_fact_updated_at": {"current_workaround": "2025-01-01T12:00:00"}}
    question = {"gstack_id": "Q2", "text": f"What do {audience} use today to {job}?", "options": [previous], "prefill": previous}
    grounded = ground_question(question, snapshot)
    assert "museum" not in str(grounded).casefold()


@pytest.mark.parametrize("audience,job", [
    ("People using software at work", "process invoices"),
    ("People using software for daily tasks", "plan weekly menus"),
])
def test_generic_words_cannot_change_the_saved_audience(audience, job):
    from app.clarify.dilemmas import ground_question
    different = audience.replace("using", "who build")
    question = {"field": "requirements", "text": f"What must your product do to help {different} with {job}?", "options": []}
    grounded = ground_question(question, {"_answered_fields": {"customer": audience, "workflow": job}})
    assert audience in grounded["text"] and different not in grounded["text"]


@pytest.mark.parametrize("audience,job", [
    ("People using a tool at work", "review supplier records"),
    ("People using an app for fun", "organise weekend outings"),
])
def test_saved_generic_audiences_keep_their_full_question_grammar(audience, job):
    from app.clarify.dilemmas import ground_question, venture_context
    from app.intake.smart import _prefill

    snapshot = {"_answered_fields": {"customer": audience, "workflow": job}}
    assert venture_context(snapshot) == (audience, job)
    grounded = ground_question({"field": "requirements", "text": "What must you build?", "options": []}, snapshot)
    assert grounded["text"] == f"What must your product do to help {audience}?"
    assert _prefill(f"People perform {job}.")["customer"] == f"People who perform {job}"


@pytest.mark.parametrize("audience,idea,object_phrase,task", [
    ("a laboratory curator", "A catalogue where a laboratory curator rents specimen slides.", "a receipt printer", "Deliver preserved specimens"),
    ("a nature photographer", "A directory where a nature photographer rents camera lenses.", "a weather sensor", "Discover secluded trails"),
])
def test_singular_audience_roles_do_not_turn_pitches_into_problems(audience, idea, object_phrase, task):
    from app.clarify.dilemmas import contextual_choices, venture_context
    from app.intake.smart import audience_phrase

    snapshot = {"customer": audience, "idea": idea}
    assert venture_context(snapshot) == (audience, "")
    assert audience_phrase(object_phrase) == ""
    assert venture_context({"original_idea": task, "founder_idea": f"{idea} for {audience}"})[0] == audience
    for question_id in ("Q1", "Q2", "Q3"):
        assert contextual_choices(question_id, snapshot) == []


@pytest.mark.parametrize("audience,smalltalk", [
    ("a freight broker", "Wonderful dinner with old friends tonight"),
    ("a hobby violinist", "Interesting chapter in my book tonight"),
], ids=["b2b", "consumer"])
def test_unknown_singular_roles_need_a_person_cue_not_just_a_suffix(audience, smalltalk):
    from app.idea.agent import answer_prefill
    from app.intake.smart import _prefill, audience_phrase

    assert audience_phrase(audience) == audience
    assert answer_prefill(audience, "customer")["customer"] == audience
    assert audience_phrase(smalltalk) == ""
    assert not _prefill(smalltalk).get("customer")
    assert not answer_prefill(smalltalk, "customer").get("customer")


@pytest.mark.parametrize("audience,job", [
    ("Regional bookkeeping teams", "reconcile weekly invoices"),
    ("Weekend birdwatchers", "plan local outings"),
])
@pytest.mark.parametrize("field,replaced", [("workflow", True), ("Q2", True), ("workflow", False)])
def test_ground_question_replaces_choices_only_when_text_changes(audience, job, field, replaced):
    from copy import deepcopy
    from app.clarify.dilemmas import contextual_choices, ground_question

    snapshot = {"_answered_fields": {"customer": audience, "workflow": job}}
    template = (f"What task should your product help {audience} do first?" if field == "workflow" else
        f"How does your audience ({audience}) handle this today? Describe the current workaround.")
    choices = ["Use existing tools", "Keep work manually"] if replaced else ["Choose one task", "Describe the first step"]
    question = {"field": field, "text": f"What do {audience} use today?" if replaced else template,
        "options": choices, "requires_detail": choices[:1], "detail_prompt": "Describe the tools you use today.",
        "recommended_option": choices[0], "recommendation_reason": "Start with this task.", "why": "Choose one task."}
    original = deepcopy(question)
    saved = deepcopy(snapshot)

    grounded = ground_question(question, snapshot)

    assert grounded["text"] == template
    if replaced:
        assert grounded["text"] != question["text"]
        assert grounded["options"] == contextual_choices(field, snapshot)
        assert not set(grounded["options"]) & set(choices)
        assert grounded["requires_detail"] == []
        assert grounded["recommended_option"] == grounded["recommendation_reason"] == ""
        assert grounded["detail_prompt"] == "Type your own answer."
    else:
        assert grounded == question
    assert question == original and snapshot == saved
    assert ground_question(grounded, snapshot) == grounded
