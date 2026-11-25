from types import SimpleNamespace

import pytest

from tests.integration.test_chat_first_idea_bridge import client as client  # noqa: PLC0414


@pytest.mark.parametrize("idea,customer", [
    ("Continuous contract audits for regional hospitals.", "Hospital compliance officers"),
    ("Methane plume alerts from satellite data.", "Pipeline integrity engineers"),
])
def test_summary_does_not_repeat_the_idea_as_a_field(client, idea, customer):
    from app.idea.runner import _understanding_summary

    fields = dict.fromkeys(("problem", "workflow", "first_user_path", "pricing_hypothesis", "desired_outcome"), "")
    text = _understanding_summary(SimpleNamespace(summary=idea, customer=customer, offer=idea, requirements=[], constraints=[], **fields))
    assert text.count(idea) == 1
    assert f"**Customer:** {customer}" in text
