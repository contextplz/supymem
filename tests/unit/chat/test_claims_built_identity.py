from tests.company_os_harness import load_company_os_app


def test_passive_built_identity_is_not_an_action_claim():
    load_company_os_app()
    from app.chat.claims import claims_action

    assert not claims_action("I'm Charlie. I'm an AI assistant, not a human. I'm built into Supymem and help you.")
    assert not claims_action("I am built into Supymem.")
    assert claims_action("I built the landing page for you.")
    assert claims_action("I've built it.")
