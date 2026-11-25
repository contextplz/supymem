from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.chat.actions import all_actions


def test_every_chat_action_has_a_named_governed_capability():
    for action in all_actions():
        assert action.governed_capability.startswith("cap.")
        assert action.endpoint
