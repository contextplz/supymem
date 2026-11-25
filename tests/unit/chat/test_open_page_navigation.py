"""'open content' and other known-page commands navigate instead of reaching the model."""

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.mark.asyncio
@pytest.mark.parametrize(("command", "href"), [
    ("open content", "/content?venture=ven_ours"),
    ("Open the Content page", "/content?venture=ven_ours"),
    ("open go to market", "/gtm?venture=ven_ours"),
    ("Take me to go-to-market", "/gtm?venture=ven_ours"),
    ("open gtm", "/gtm?venture=ven_ours"),
    ("open experiments", "/v/ven_ours/experiments"),
    ("open feedback", "/v/ven_ours/feedback"),
    ("open team", "/v/ven_ours/org"),
    ("open connections", "/v/ven_ours/connections"),
    ("open plan", "/v/ven_ours/charter"),
])
async def test_open_known_page_returns_link_button(command, href):
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.intent import classify_intent
    from app.chat.router import _dispatch_intent

    assert classify_intent(command) == "open_screen"
    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                   "open_screen", command, None)
    assert reply["kind"] == "navigation"
    assert reply["actions"][0]["href"] == href


@pytest.mark.asyncio
async def test_how_do_i_pause_is_answered_deterministically_with_settings_button():
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import _dispatch_intent

    reply = await _dispatch_intent("ven_ours", ChatContext(venture={"id": "ven_ours", "name": "LabHire"}),
                                   "pause_help", "how do I pause?", None)
    assert "pause right here" in reply["body_md"]  # chat-first: the pause runs in chat, no Settings page
    assert "Nothing new starts until you resume" in reply["body_md"]
    assert reply["actions"][0]["label"] == "Pause this venture" and reply["actions"][0]["confirmation"]["action"] == "pause"
    assert "paused=" not in reply["body_md"]


def test_internal_field_assignments_never_reach_the_founder():
    load_company_os_app()
    from app.chat.context import ChatContext
    from app.chat.router import scrub_hallucinations

    out = scrub_hallucinations("Sure. I will set `paused=True` for you; is_paused = False later.", ChatContext(venture={}), "")
    assert "=" not in out
