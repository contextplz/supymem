from tests.company_os_harness import load_company_os_app
import pytest


@pytest.mark.parametrize("notes,clean", [
    ("Audience: i guess mostly home sewists in Uruguay, idk, still messy", "home sewists in Uruguay"),
    ("Customer: maybe wastewater plant coordinators in Denmark, rough notes", "wastewater plant coordinators in Denmark"),
])
def test_idea_sentence_keeps_the_whole_first_sentence(notes, clean):
    load_company_os_app()
    from app.autopilot.outreach import idea_sentence
    idea = ("I want to start a company that helps museum gift shops retain members by offering personal "
            "book recommendations to patrons who are about to cancel. It reads membership renewal data.")
    assert idea_sentence(idea).endswith("who are about to cancel")
    assert len(idea_sentence("word " * 200)) <= 240 and not idea_sentence("word " * 200).endswith(" ")
    assert idea_sentence(notes) == clean
    assert idea_sentence(clean + ", " + "unreviewed qualifier " * 20, len(clean) + 10) == clean
    assert idea_sentence(clean + " unreviewed qualifier " * 20, len(clean) + 10) == ""
    assert idea_sentence(clean + " incomplete-w…") == clean
    assert idea_sentence(clean + ". proposed: At least one person spends time or money") == clean
    assert idea_sentence("[First name] " + notes) == clean
    assert idea_sentence("Product: " + clean + " idk (rough draft). Customer: unreviewed notes") == clean
    assert idea_sentence("Offer: " + clean + ". Problem: unreviewed notes. .") == clean
    assert idea_sentence(clean + " https://foreign.example/ contact@example.com") == clean
