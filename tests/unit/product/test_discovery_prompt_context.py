"""Both discovery stages keep private third-party evidence out of first tests."""
import json

import pytest
from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("idea", [
    {"customer": "Independent cleaners", "problem": "Client bookings get lost in WhatsApp"},
    {"customer": "Adult childcare providers", "problem": "Private family chats obscure bookings"},
])
def test_questions_and_review_include_discovery_boundary_without_changing_idea(idea):
    load_company_os_app()
    from app.gstack import office_hours
    from app.idea import validation_agent
    from app.idea.discovery_guidance import DISCOVERY_GUIDANCE
    questions = office_hours.messages_for(idea)
    reviews = validation_agent.messages_for({"id": "revision_one", "text": json.dumps(idea)}, [],
                                            ["gstack_office_hours", "gstack_ceo_review"])
    for messages in (questions, reviews):
        assert DISCOVERY_GUIDANCE in messages[0]["content"]
        assert "informed\nconsent" in messages[0]["content"]
        assert "interview adult providers or parents" in messages[0]["content"]
        assert idea["problem"] in messages[1]["content"]
    assert json.loads(questions[1]["content"])["founder_idea"] == idea
    assert json.loads(reviews[1]["content"])["founder_revision_source"]["id"] == "revision_one"
