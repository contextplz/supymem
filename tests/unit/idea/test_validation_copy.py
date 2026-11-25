import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("mode", ["hosted", "local"])
@pytest.mark.parametrize("customer,offer", [
    ("Commercial greenhouse managers", "an irrigation checklist"),
    ("Recreational canoeists", "a trip packing guide"),
])
def test_sample_review_deduplicates_displayed_findings_without_changing_saved_evidence(monkeypatch, mode, customer, offer):
    from types import SimpleNamespace

    load_company_os_app()
    from app.idea.service import serialize_validation
    from app.idea.validation_agent import review_markdown, review_presentation
    from app.research.deep import SAMPLE_RESEARCH_NOTE

    monkeypatch.setenv("SUPYMEM_MODE", mode)
    observation = f"You can test {offer} with {customer}."
    report = {"sample": True, "summary": SAMPLE_RESEARCH_NOTE, "findings": [
        {"method": "gstack_office_hours", "assessment": SAMPLE_RESEARCH_NOTE, "source_ids": ["revision"]},
        {"method": "gstack_ceo_review", "assessment": SAMPLE_RESEARCH_NOTE, "source_ids": ["revision"]},
        {"method": "gstack_office_hours", "assessment": observation, "source_ids": ["revision"]},
        {"method": "gstack_ceo_review", "assessment": observation, "source_ids": ["revision"]},
    ], "questions": [], "proposed_tests": []}
    original = json.dumps(report)
    run = SimpleNamespace(id="sample_review", method="gstack_ceo_review", revision_id="revision", state="complete",
        verdict="needs_evidence", report=original, founder_decision=None, founder_note=None)
    context = {"customer": customer, "offer": offer}
    expected = [report["findings"][0], report["findings"][2]] if mode == "local" else report["findings"]
    assert review_presentation(report, context)["findings"] == expected
    served = serialize_validation(run, context)["report"]
    assert served["presentation"]["findings"] == expected
    body = review_markdown(report, context)
    copies = 1 if mode == "local" else 2
    assert body.count("- " + SAMPLE_RESEARCH_NOTE) == copies
    assert body.count("- " + observation) == copies
    assert "What I'd do next" in body and "\u2014" not in body
    assert run.report == original and json.dumps(report) == original


@pytest.mark.parametrize("customer,problem,biography,workflow", [
    ("regional freight dispatchers", "double-booked loads", "I am a dispatcher at a freight depot. This would be my first company.",
     "You can assign a load and see each truck's next stop."),
    ("home sourdough bakers", "forgotten starter feedings", "I am a keen home baker. This would be my first company.",
     "You can log a feeding and see the next reminder."),
])
def test_review_opening_uses_product_workflow_not_your_biography(customer, problem, biography, workflow):
    load_company_os_app()
    from app.idea.validation_agent import review_presentation

    report = {"summary": "Your experience gives you a starting point.", "proposed_tests": [
        {"hypothesis": "People in this group need this result.", "action": "Interview consenting adults about their recent workaround.",
         "success_measure": "Record recent behavior and concrete commitments, not opinions."}]}
    context = {"customer": customer, "problem": problem, "offer": biography, "workflow": workflow}
    for value in (context, {**context, "workflow": ""}):
        display = review_presentation(report, value)
        assert customer in display["opening"] and problem in display["opening"]
        assert "first company" not in display["opening"] and biography not in display["opening"]
        assert "landing page" not in display["opening"] and "\u2014" not in display["opening"]
        assert display["next_steps"] == report["proposed_tests"]
    assert workflow in review_presentation(report, context)["opening"]


@pytest.mark.parametrize("customer,problem,offer", [
    ("commercial electricians", "tracking inspection paperwork", "a job handover checklist"),
    ("home gardeners", "planning seasonal planting", "a weekly planting guide"),
])
def test_review_leads_with_findings_opening_and_runnable_next_step(monkeypatch, customer, problem, offer):
    from types import SimpleNamespace

    load_company_os_app()
    from app.chat import milestones
    from app.idea.service import serialize_validation
    from app.idea.validation import post_outcome_to_chat
    from app.idea.validation_agent import ASSESSMENT_NOTE, SYSTEM, review_markdown
    from app.ventures import startup_review

    report = {"summary": "The idea is an empty container and says nothing about the problem.",
              "findings": [{"method": "gstack_ceo_review", "assessment": f"You described {offer} for {customer}.", "source_ids": ["revision"]},
                           {"method": "gstack_office_hours", "assessment": "No direct conversations were described.", "source_ids": ["revision"]}],
              "biggest_risk": "No identified customer", "gaps": ["No description of demand"],
              "questions": [f"Would {customer} benefit from help with {problem}?", f"Would {customer} prefer a simpler {offer}?"],
              "forcing_questions": [{"question": "Problem: got no clue help me out", "risk": "You haven't spoken to a single customer."}],
              "proposed_tests": [{"hypothesis": f"{customer} would use {offer} for {problem}.",
                                  "action": "Prepare a focused landing page and a consent-safe interview outline for your approval. No description of private messages may be collected.",
                                  "success_measure": "Record an actual commitment or refusal; do not publish or contact anyone without approval."}],
              "assessment_note": ASSESSMENT_NOTE}
    original = json.dumps(report)
    run = SimpleNamespace(id="run", method="gstack_ceo_review", revision_id="revision", state="complete",
                          verdict="needs_evidence", report=original, founder_decision=None, founder_note=None)
    context = {"customer": customer, "problem": problem, "offer": offer}
    served = serialize_validation(run, context)["report"]
    assert served["presentation"]["findings"] == [report["findings"][0]]
    assert served["presentation"]["next_steps"] == report["proposed_tests"]
    assert len(served["presentation"]["options"]) == 2
    assert all(text in served["presentation"]["opening"] for text in (customer, problem, offer))
    personal = serialize_validation(run, {**context, "project_intent": "personal"})["report"]["presentation"]["opening"]
    assert "your own use" in personal and "landing page" not in personal
    captured = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *_args, **kwargs: captured.append(kwargs["body_md"]))
    monkeypatch.setattr(startup_review, "enabled", lambda *_args: True)
    session = SimpleNamespace(get=lambda *_args: SimpleNamespace(venture_id="venture", structured=json.dumps(context)),
                              scalars=lambda *_args: SimpleNamespace(first=lambda: run))
    post_outcome_to_chat(session, "job", "session", "revision", ok=True)
    for body in (review_markdown(report, context), captured[0]):
        assert all(title in body for title in ("What we found", "Your opening", "What I'd do next"))
        assert body.index("What we found") < body.index("Your opening") < body.index("What I'd do next")
        assert customer in body and report["proposed_tests"][0]["action"] in body
        assert not any(text.lower() in body.lower() for text in ("Biggest risk", "Gaps:", "Strengths:", "empty container", "says nothing about", "no clue", "No direct conversation", "No description of demand", "No identified", "haven't spoken to a single", ASSESSMENT_NOTE))
        assert "\u2014" not in body
    assert '2 to 3 candidate problems' in SYSTEM
    assert served["assessment_note"] == ASSESSMENT_NOTE and run.report == original


def test_chat_idea_review_addresses_you_without_rewriting_saved_report(monkeypatch):
    from types import SimpleNamespace

    load_company_os_app()
    from app.chat import milestones
    from app.chat import build_path
    from app.idea.validation import post_outcome_to_chat
    from app.idea.validation_agent import SYSTEM, sanitize_founder_text
    from app.ventures import startup_review

    report = json.dumps({"strengths": ["The founder narrowed the offer around the founder's experience"],
                         "biggest_risk": "The founder hasn't spoken to any gym owner yet",
                         "proposed_tests": [{"hypothesis": "Gym owners will try the founder’s booking tool"}],
                         "gaps": ["The founder's pricing is untested", "Whether the founder is solving a frequent problem"],
                         "forcing_questions": [{"founder_answer": "The founder hasn't spoken to any gym owner yet"}]})
    run = SimpleNamespace(verdict="needs_evidence", report=report)
    session = SimpleNamespace(get=lambda *_args: SimpleNamespace(venture_id="ven_review"),
                              scalars=lambda *_args: SimpleNamespace(first=lambda: run))
    captured = []
    monkeypatch.setattr(milestones, "post_milestone", lambda *_args, **kwargs: captured.append(kwargs["body_md"]))
    monkeypatch.setattr(startup_review, "enabled", lambda *_args: False)
    monkeypatch.setattr(build_path, "research_prerequisites", lambda *_args: None)
    post_outcome_to_chat(session, "job_review", "session_review", "revision_review", ok=True)
    assert len(captured) == 1
    body = captured[0]
    assert "the founder" not in body.lower()
    for text in ("You narrowed the offer around your experience", "Gym owners will try your booking tool"):
        assert text in body
    for label in ("What we found", "Your opening", "What I'd do next"):
        assert f"**{label}**" in body
    assert "Biggest risk" not in body and "Your pricing is untested" not in body
    assert run.report == report and sanitize_founder_text("The founder's saved testimony") == "The founder's saved testimony"
    assert 'Address the founder directly as "you" and "your"' in SYSTEM


def test_d14_sanitizer_keeps_business_gaps_and_saved_testimony(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/founder-copy.db")
    load_company_os_app()
    from app.idea.service import serialize_validation
    from app.idea.validation_agent import NO_MEMORY_GAP, sanitize_founder_text
    internal = "No scoped GBrain memory was provided; no memory-based synthesis was performed."
    for note in (internal, "Scoped memory could not be read.", "The web_fetch tool was unavailable."):
        assert sanitize_founder_text(note) == NO_MEMORY_GAP
    business = "Customers have not confirmed willingness to pay."
    assert sanitize_founder_text(business) == business
    report = {"summary": internal, "gaps": [business, internal], "findings": [
        {"method": "gbrain_think", "assessment": internal, "source_ids": ["gbrain_note"]}],
        "forcing_questions": [{"founder_answer": internal}]}
    result = serialize_validation(SimpleNamespace(id="review", method="gbrain_think", revision_id="revision",
        state="complete", verdict="needs_evidence", report=json.dumps(report), founder_decision=None, founder_note=None))["report"]
    assert result["summary"] == result["findings"][0]["assessment"] == NO_MEMORY_GAP
    assert result["gaps"] == [business, NO_MEMORY_GAP]
    assert result["findings"][0]["method"] == "gbrain_think"
    assert result["findings"][0]["source_ids"] == ["gbrain_note"]
    assert result["forcing_questions"][0]["founder_answer"] == internal


def test_validation_preserves_directive_jargon_until_display(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/validation-copy.db")
    load_company_os_app()
    from app.idea.validation_agent import SYSTEM, parse_result, sanitize_founder_text

    advice = "Write down deferred work so nothing is lost."
    for prefix in ("Prime directive 7 ('Everything deferred must be written down') applies: ",
                   "Prime directive 7: ", "  prime directive 12 (Keep scope small) applies:\n"):
        assert sanitize_founder_text(prefix + advice) == advice
    assert sanitize_founder_text(advice) == advice
    assert sanitize_founder_text("Consider Prime directive 7: as internal context.") == "Consider Prime directive 7: as internal context."
    assert "never cite directive names or numbers" in SYSTEM
    assert '"Prime directive" prefixes, or GStack internals' in SYSTEM

    revision = {"id": "rev_one", "text": json.dumps({"offer": "A clinic booking tool"})}
    prefix = "Prime directive 7 ('Everything deferred must be written down') applies: "
    text = prefix + advice
    report = {
        "verdict": "needs_evidence", "summary": text,
        "findings": [{"method": "gstack_ceo_review", "assessment": text, "source_ids": ["rev_one"]}],
        "gaps": [text], "questions": [text], "strengths": [text], "biggest_risk": text,
        "proposed_tests": [{"hypothesis": text, "action": text, "success_measure": text}],
        "forcing_questions": [{"question_id": "Q1", "question": text, "founder_answer": "",
                               "assessment": text, "risk": text, "suggested_test": text}],
    }
    answers = [{"id": "Q1", "answer": text}]
    saved = parse_result(json.dumps(report), revision, [], ["gstack_ceo_review"], answers).model_dump()
    assert saved["summary"] == saved["biggest_risk"] == saved["findings"][0]["assessment"] == text
    assert saved["gaps"] == saved["questions"] == saved["strengths"] == [text]
    assert set(saved["proposed_tests"][0].values()) == {text}
    question = saved["forcing_questions"][0]
    assert all(question[field] == text for field in ("question", "assessment", "risk", "suggested_test"))
    assert question["founder_answer"] == text, "saved founder testimony must not be rewritten"
    assert saved["findings"][0]["source_ids"] == ["rev_one"]
    from app.idea.validation_agent import _plain_strings
    assert _plain_strings(saved)["summary"] == advice
    assert saved["summary"] == text
    report["summary"] = ""
    with pytest.raises(ValueError):
        parse_result(json.dumps(report), revision, [], ["gstack_ceo_review"], answers)


@pytest.mark.parametrize("domain", ["dental clinics", "public libraries"])
def test_review_copy_is_shared_for_new_and_legacy_reports_without_changing_evidence(domain):
    from types import SimpleNamespace
    load_company_os_app()
    from app.idea.service import serialize_validation
    from app.idea.validation_agent import SYSTEM, parse_result
    from app.gtm.relationships.email_compliance import NO_DASH_RULE
    from core.copy import founder_copy

    text = f"Founder wants to build a tool for {domain}—the founder hasn't clarified the problem."
    expected = f"You want to build a tool for {domain}, you haven't clarified the problem."
    report = {"verdict": "needs_evidence", "summary": text, "strengths": [text], "biggest_risk": text,
              "findings": [{"method": "gstack_ceo_review", "assessment": text, "source_ids": ["revision_one"]}],
              "gaps": [text], "questions": [text],
              "proposed_tests": [{"hypothesis": text, "action": "Ask privately–do not send anything automatically.",
                                  "success_measure": "2–3 reported problems"}],
              "forcing_questions": [{"question_id": "Q1", "question": text, "assessment": text, "risk": text,
                                     "suggested_test": text, "founder_answer": text}]}
    parsed = parse_result(json.dumps(report), {"id": "revision_one", "text": json.dumps({"offer": domain})}, [],
                          ["gstack_ceo_review"], [{"id": "Q1", "answer": text}]).model_dump()
    assert parsed == report
    run = SimpleNamespace(id="run", method="gstack_ceo_review", revision_id="revision_one", state="complete",
                          verdict="needs_evidence", report=json.dumps(report), founder_decision=None, founder_note=None)
    served = serialize_validation(run)["report"]
    assert served["summary"] == served["findings"][0]["assessment"] == expected
    assert served["findings"][0]["source_ids"] == ["revision_one"]
    assert served["forcing_questions"][0]["founder_answer"] == text
    assert json.loads(run.report) == report
    assert NO_DASH_RULE in SYSTEM
    assert founder_copy("A co-founder can help another founder.") == "A co-founder can help another founder."


def test_venture_summary_sanitizes_display_not_saved_input(monkeypatch):
    load_company_os_app()
    from app.ventures import current_idea
    raw = "Founder wants to help public libraries—the founder’s offer is not set."
    calls = []
    def read(venture, *, session, for_display):
        calls.append((venture, session, for_display))
        return raw
    monkeypatch.setattr(current_idea, "current_idea", read)
    assert current_idea.idea_summary("venture", session="reader") == "You want to help public libraries, your offer is not set."
    assert calls == [("venture", "reader", True)]
    assert raw == "Founder wants to help public libraries—the founder’s offer is not set."
    from types import SimpleNamespace
    from app.idea.service import serialize_revision
    revision = SimpleNamespace(id="rev", rev=1, source="agent", summary=raw, structured="{}", diff_from_prev="{}", created_at=None)
    assert serialize_revision(revision)["summary"] == "You want to help public libraries, your offer is not set."
    assert revision.summary == raw
    revision.source = "founder"
    assert serialize_revision(revision)["summary"] == raw
