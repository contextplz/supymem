import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()


def test_proposal_post_shows_the_understood_fields():
    from app.idea.agent import Understanding
    from app.idea.runner import _understanding_summary

    text = _understanding_summary(Understanding(summary="A forecast tool.", problem="Stock-outs", customer="Roasters"))
    assert "A forecast tool." in text and "**Problem:** Stock-outs" in text and "**Customer:** Roasters" in text
    assert "Pricing" not in text  # empty fields are skipped
    assert _understanding_summary(Understanding()) == ""


@pytest.mark.parametrize("workflow", [
    "Stock records arrive late. You upload manifests. The tool compares deliveries and creates a restocking list.",
    "Accessible trails are hard to find. You enter your mobility needs. The app maps suitable routes and saves sightings.",
])
def test_summary_preserves_complete_workflow_sentences(workflow):
    from app.idea.agent import Understanding
    from app.idea.runner import _summary_text, _understanding_summary

    assert _summary_text("Workflow: " + workflow) == workflow
    assert _summary_text("Could you help me shape this? " + workflow) == workflow
    assert workflow in _understanding_summary(Understanding(workflow=workflow))


@pytest.mark.parametrize("offer,customer,detail", [
    ("You reconcile access permissions across hospital departments", "Hospital IT teams",
     "while keeping an audit trail of every approval and preserving the original permission records so your security lead can investigate an incident without reconstructing changes from emails"),
    ("You track medication schedules and refill dates for your household", "Families managing prescriptions",
     "while keeping each person's instructions separate and showing missed doses without changing the prescribed schedule so you can bring an accurate history to your next appointment"),
])
def test_summary_keeps_long_details_without_repeating_fields(offer, customer, detail):
    from app.idea.agent import Understanding
    from app.idea.runner import _understanding_summary

    description = f"{offer} {detail}."
    text = _understanding_summary(Understanding(summary=description, offer=description,
        workflow="  " + description.upper(), customer=customer,
        requirements=["Keep the history", "Keep the history."]))
    assert description in text
    assert text.casefold().count(description.casefold()) == 1
    assert text.count("Keep the history") == 1
    assert f"**Customer:** {customer}" in text
