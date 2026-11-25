"""Semantic risk gate: task intent vs control role/label. Consent, trial, signup, cancel and fill-only stop with a
founder-facing question; explicit authorized asks run; a block message is never empty."""
import threading
from types import SimpleNamespace

import pytest
from coding_agents.cu_ai_risk import assess_risk, fill_only
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_ai_engine import run_loop


def click(goal, label, **el):
    return assess_risk("click", goal, element={"label": label, **el}, label=label)


@pytest.mark.parametrize("goal,label,el", [
    ("Finish registering with name Jo", "I agree to the Terms", {"type": "checkbox"}),
    ("Just accept the terms so I can continue", "I agree to the Terms", {"type": "checkbox"}),   # legal consent: founder only
    ("Fill the form", "consent_terms", {"type": "checkbox"}),
    ("Find a cheap host", "Start free trial", {"ax_role": "button"}),
    ("Find me a host", "Create account", {"ax_role": "button"}),
    ("Cancel my subscription", "Cancel subscription", {"ax_role": "button"}),   # never by wording alone
    ("Cancel my subscription", "Confirm cancellation", {"ax_role": "button"}),
    ("Read the page", "Accept all", {"ax_role": "button"}),
    ("Sign me up", "Partner offers", {"type": "checkbox"}),
])
def test_blocks_consent_trial_signup_cancel(goal, label, el):
    risk = click(goal, label, **el)
    assert risk is not None and risk.message.strip() and ("?" in risk.message or "Please" in risk.message)


@pytest.mark.parametrize("goal,label,el", [
    ("Remove the draft named Old notes", "Delete", {"ax_role": "button"}),
    ("Get rid of the old draft", "Delete", {"ax_role": "button"}),
    ("Sign me up for the newsletter with jo@example.invalid", "Subscribe", {"ax_role": "button"}),
    ("Email the team that the launch is delayed", "Send", {"ax_role": "button"}),
    ("Buy the cheapest one for me", "Checkout (guest)", {"ax_role": "a"}),
    ("Start a free trial of the Pro plan", "Start free trial", {"ax_role": "button"}),
    ("Read the page", "Pricing", {"ax_role": "a"}),
    ("Find the support email", "Email us", {"ax_role": "a"}),     # a link, not a delivered message
    ("Tick the newsletter box", "Newsletter", {"type": "checkbox"}),
])
def test_authorized_or_harmless_controls_run(goal, label, el):
    assert click(goal, label, **el) is None


def test_delete_all_needs_the_task_to_say_all():
    assert click("Remove the draft", "Delete all", ax_role="button")
    assert click("Delete everything in the folder", "Delete all", ax_role="button") is None


def test_fill_is_not_submit():
    assert fill_only("Fill this form with sensible values.")
    assert not fill_only("Fill this form and submit it once.")
    assert not fill_only("Finish registering at the site with name Jo.")
    risk = click("Fill this form with sensible values.", "Send", submits_form=True, ax_role="button")
    assert risk and risk.kind == "fill_only" and "?" in risk.message
    assert assess_risk("key", "Fill this form.", chord="return")
    assert click("Fill this form and submit it", "Send", submits_form=True) is None


def test_invented_email_is_a_question_but_given_goal_email_is_not():
    assert assess_risk("type", "Sign me up for the newsletter", element={"type": "email"}, text="me@real.com").kind == "provide_value"
    assert assess_risk("type", "Sign me up with jo@example.invalid", element={"type": "email"}, text="jo@example.invalid") is None
    assert assess_risk("type", "x", element={"type": "text"}, text="Atlas") is None


def test_founder_continue_approves_an_approvable_kind_but_never_consent():
    risk = click("Find a host", "Start free trial")
    assert assess_risk("click", "Find a host", label="Start free trial", approved={risk.key}) is None
    consent = click("Register", "I agree to the Terms", type="checkbox")
    assert not consent.approvable
    assert assess_risk("click", "Register", element={"type": "checkbox"}, label="I agree to the Terms", approved={consent.key})


class Backend:
    def __init__(self):
        self.sent = []

    def observe(self):
        return SimpleNamespace(url="http://x/register", title="Register", text="Register", captcha=False,
                               tabs=[{"i": 0, "title": "R", "active": True}],
                               elements=[{"i": 1, "tag": "input", "type": "checkbox", "label": "I agree to the Terms"},
                                         {"i": 2, "tag": "button", "type": "submit", "text": "Register", "submits_form": True}])

    def act(self, payload):
        self.sent.append(payload)
        return {"status": "ok", "action": payload["action"], "seconds": 0.0}


def step(op, **kw):
    item = {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0, "app": "", "ms": 0, "summary": ""}
    item.update(kw)
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": [item]}


def test_engine_stops_at_terms_checkbox_with_a_founder_message_and_never_clicks():
    backend = Backend()
    result = run_loop("Finish registering with name Jo Test", lambda p: (step("click", element_id="b1"), {}), BrowserMac(backend),
                      threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "failed" and result.reason == "needs_founder"
    assert "Terms" in result.summary
    assert backend.sent == []


def test_engine_pauses_with_guardrail_kind_and_resumes_after_continue():
    backend, pauses = Backend(), []
    replies = iter([step("click", element_id="b2"), step("click", element_id="b2"), step("done", summary="ok")])

    def pause(kind, prompt, obs):
        pauses.append((kind, prompt))
        return "continue"
    result = run_loop("Fill this form with sensible values", lambda p: (next(replies), {}), BrowserMac(backend), threading.Event(),
                      pause=pause, verify=lambda *a: (True, ""))
    assert pauses and pauses[0][0] == "guardrail" and "submit" in pauses[0][1].lower()
    assert result.outcome == "done" and [p["action"] for p in backend.sent] == ["click"]   # the second click ran once approved
