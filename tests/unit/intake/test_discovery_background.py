import pytest

from tests.integration.test_chat_first_idea_bridge import client as client  # noqa: PLC0414


@pytest.mark.parametrize("opening,direction", [
    ("I spent eight years running security at a payments company. I don't know what to build yet.", False),
    ("I ran satellite operations for a decade. I want to start something in space but I don't know what yet.", True),
])
def test_stated_background_is_not_asked_for_again(client, opening, direction):
    from app.intake.smart import BACKGROUND_EXAMPLES, BACKGROUND_QUESTION, DISCOVERY_QUESTION, classify_fallback

    first = classify_fallback(opening)
    if direction:
        assert first.path == "startup" and first.extracted.idea == opening
        assert first.founder_line not in {BACKGROUND_QUESTION, DISCOVERY_QUESTION}
        return
    assert first.founder_line == BACKGROUND_QUESTION and first.founder_line != DISCOVERY_QUESTION
    assert first.choices == BACKGROUND_EXAMPLES


def test_no_background_still_asks_for_it(client):
    from app.intake.smart import DISCOVERY_QUESTION, classify_fallback

    assert classify_fallback("I don't know what to build yet.").founder_line == DISCOVERY_QUESTION


@pytest.mark.parametrize("background,problem,offer", [
    ("I ran payments security for eight years.", "Banks miss account takeovers", "Software that flags account takeovers for security teams"),
    ("I ran satellite operations for a decade.", "Operators find methane leaks too late", "Daily methane leak alerts for pipeline operators"),
])
def test_discovery_brief_leads_with_the_offer_not_the_biography(client, background, problem, offer):
    from app.intake.smart import _discovery_brief

    brief = _discovery_brief(f"{background} I don't know what to build yet.\nContext: {problem}\nContext: {offer}")
    assert brief.startswith(offer) and problem in brief and background in brief


@pytest.mark.parametrize("idea", [
    "we build satellite based methane monitoring for pipeline operators in north america",
    "We are building claims automation software for mid size insurers",
])
def test_a_founder_who_says_what_they_build_is_not_asked_what_it_does(client, idea):
    from app.idea.agent import core_answer_status

    assert core_answer_status(idea, 0, field="offer") == (True, False)


@pytest.mark.parametrize("idea,audience", [
    ("we build methane monitoring for pipeline operators in north america they currently find leaks weeks late", "pipeline operators in north america"),
    ("we are building claims automation for mid size insurers we have one pilot so far", "mid size insurers"),
])
def test_unpunctuated_idea_keeps_a_clean_audience(client, idea, audience):
    from app.intake.smart import _prefill

    assert _prefill(idea)["customer"] == audience


@pytest.mark.parametrize("text,expected", [
    ("I led security engineering at a hospital network for twelve years.", None),
    ("Software that inventories devices for hospital CISOs and flags unpatched ones.", "hospital CISOs"),
    ("A planning tool for families of four", "families of four"),
])
def test_durations_are_not_audiences(client, text, expected):
    from app.intake.smart import _prefill

    assert _prefill(text).get("customer") == expected


@pytest.mark.parametrize("opening", [
    "I lead security at a hospital with an idea for a tool that helps small teams chase access review approvals and I do not know what to do first",
    "I built risk models at a lender and have software that flags account takeovers for small banks but I am not sure what to do next",
])
def test_named_idea_with_an_unsure_next_step_is_not_discovery(client, opening):
    from app.intake.smart import no_idea_intent

    assert not no_idea_intent(opening)


@pytest.mark.parametrize("request_text,count", [
    ("Write a short paid pilot proposal for a hospital security lead using synthetic evidence only", False),
    ("Draft a pilot proposal for a lender with synthetic test data and do not send it", False),
    ("Did the synthetic test submission become a real lead?", True),
])
def test_written_documents_are_not_metric_questions(client, request_text, count):
    from app.chat.assistant import _asks_about_venture_metrics

    assert _asks_about_venture_metrics(request_text) is count
