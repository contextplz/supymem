"""Controller integration with real durable reservations and a fake provider.

Membership is isolated by the imported fixture; full signed membership coverage
belongs to the mounted PostgreSQL order tests. No test executes native effects.
"""
import pytest

from coding_agents.jev_decision import Observation
from tests.unit.product.test_device_orders import orders  # noqa: F401


@pytest.fixture
def controller(orders, monkeypatch):  # noqa: F811
    svc, caller, manifest = orders
    from app.build import device_jev as jev
    from app.llm import pricing
    monkeypatch.setattr(pricing, "_pricing_cache", None)
    monkeypatch.setenv("JEV_DEVICE_DECISIONS_ENABLED", "1")
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-test-key")
    manifest = manifest.model_copy(update={"credit_ceiling_micro": 8000, "actions": ["browser.interact"]})
    saved = svc.prepare("v_one", caller, manifest)
    for action in ("approve", "claim"):
        svc.transition("v_one", caller, saved["id"], saved["review_digest"], action)
    obs = Observation(revision="one", application="com.apple.Safari", location="https://example.test/page",
        goal="Untrusted caller goal", context="Page loaded", permitted=True, confirmation_pending=False,
        prior_effect_uncertain=False, controls=[{"id": "c1", "label": "Continue", "operations": ["click"],
            "visible": True, "enabled": True}])
    sent = []

    def provider(body):
        sent.append(body)
        assert svc.retrieve("v_one", caller, saved["id"])["invocations"]["call1"]["state"] == "calling"
        return {"model": jev.decision.MODEL, "usage": {"input_tokens": 1000, "output_tokens": 84},
            "answers": {name: {"type": "choice", "choice": "click" if name == "operation" else "c1",
                "probabilities": {key: float(key == ("click" if name == "operation" else "c1"))
                    for key in question["criteria"]}} for name, question in body["questions"].items()}}

    monkeypatch.setattr(jev, "_provider_call", provider)
    return svc, caller, saved, obs, jev, sent, provider


def run(c, obs=None):
    _, caller, saved, observation, jev, *_ = c
    return jev.decide_browser("v_one", caller, saved["id"], invocation_id="call1", observation=obs or observation)


def receipt(c):
    svc, caller, saved, *_ = c
    return svc.retrieve("v_one", caller, saved["id"]).get("invocations", {}).get("call1")


def test_reserves_before_io_uses_approved_goal_records_actual_and_never_replays(controller):
    c = controller
    candidate = run(c)
    assert (candidate.operation, candidate.target) == ("click", "c1")
    assert c[5][0]["state"]["goal"] == c[2]["manifest"]["instruction"]
    assert receipt(c)["quote_micro"] == 2688
    assert receipt(c)["actual_micro"] == 42
    assert receipt(c)["usage"] == {"model": "jev-1.13.0", "input_tokens": 1000, "output_tokens": 84}
    with pytest.raises(ValueError, match="already_attempted"):
        run(c)
    assert len(c[5]) == 1


def test_desktop_decision_uses_reviewed_founder_goal_without_scope_boilerplate():
    from app.build import device_jev as jev, device_orders as order_service

    manifest = order_service.Manifest(task_id="founder_mac_v_one", title="Mac task",
        instruction="Human review: act only in Calculator, stop at passwords and purchases.",
        applications=["com.apple.calculator"], origins=[],
        actions=["desktop.read", "desktop.interact"], credit_ceiling_micro=500000,
        duration_seconds=900,
        desktop_verification={"operation": "desktop_goal_v1",
            "application_bundle": "com.apple.calculator", "process_id": 42,
            "process_start_identity": "123", "window_id": 7,
            "founder_goal": "Clear Calculator, click 4 and 2, then report the display."})
    observation = Observation(revision="ps1_abc", application="com.apple.calculator",
        location="mac-window:7", goal="untrusted", context="display 22789",
        controls=[], permitted=True, confirmation_pending=False,
        prior_effect_uncertain=False)
    scoped = jev._scoped_observation({"manifest": manifest.model_dump(exclude_none=True)}, observation)
    assert scoped.goal == "Clear Calculator, click 4 and 2, then report the display."


def test_review_bound_verifier_accepts_low_probability_done(controller):
    c = controller
    body = c[4].decision.request(c[3])
    response = {"model": c[4].decision.MODEL, "answers": {}}
    for name, question in body["questions"].items():
        choice = "done" if name == "operation" else "none"
        others = max(1, len(question["criteria"]) - 1)
        response["answers"][name] = {"type": "choice", "choice": choice,
            "probabilities": {key: .75 if key == choice else .25 / others
                for key in question["criteria"]}}
    assert c[4]._candidate(response, c[3], has_browser_verifier=True).operation == "done"
    assert c[4]._candidate(response, c[3], has_browser_verifier=False).operation == "blocked"
    assert c[4]._candidate(response, c[3], has_browser_verifier=False,
                           has_desktop_review=True).operation == "done"


def test_low_probability_type_requires_reviewed_field_value(controller):
    c = controller
    obs = type(c[3]).model_validate({**c[3].model_dump(), "controls": [
        {"id": "c1", "label": "Work email", "operations": ["type"],
         "visible": True, "enabled": True},
    ]})
    body = c[4].decision.request(obs)
    response = {"model": c[4].decision.MODEL, "answers": {}}
    for name, question in body["questions"].items():
        choice = "type" if name == "operation" else "c1"
        others = max(1, len(question["criteria"]) - 1)
        response["answers"][name] = {"type": "choice", "choice": choice,
            "probabilities": {key: .75 if key == choice else .25 / others
                for key in question["criteria"]}}
    assert c[4]._candidate(response, obs, has_browser_verifier=False,
                           reviewed_type=True).operation == "type"
    assert c[4]._candidate(response, obs, has_browser_verifier=False,
                           reviewed_type=False).operation == "blocked"


def test_reviewed_desktop_scroll_uses_top_compatible_choice(controller):
    c = controller
    obs = type(c[3]).model_validate({**c[3].model_dump(), "controls": [
        {"id": "c1", "label": "Scroll the selected window down", "operations": ["scroll"],
         "visible": True, "enabled": True},
    ]})
    body = c[4].decision.request(obs)
    response = {"model": c[4].decision.MODEL, "answers": {}}
    for name, question in body["questions"].items():
        choice = "scroll" if name == "operation" else "c1"
        others = max(1, len(question["criteria"]) - 1)
        response["answers"][name] = {"type": "choice", "choice": choice,
            "probabilities": {key: .75 if key == choice else .25 / others
                for key in question["criteria"]}}
    assert c[4]._candidate(response, obs, has_browser_verifier=False,
                           has_desktop_review=True).operation == "scroll"
    assert c[4]._candidate(response, obs, has_browser_verifier=False,
                           has_desktop_review=False).operation == "blocked"


@pytest.mark.parametrize("update", [{"application": "com.apple.Terminal"},
    {"location": "https://example.test.evil/page"}, {"location": "https://example.test@evil.test"},
    {"confirmation_pending": True}, {"prior_effect_uncertain": True}, {"permitted": False}])
def test_scope_and_review_gates_never_spend(controller, update):
    c = controller
    with pytest.raises(ValueError):
        run(c, Observation.model_validate({**c[3].model_dump(), **update}))
    assert not c[5] and receipt(c) is None


@pytest.mark.parametrize("failure", ["timeout", "usage", "answers"])
def test_unknown_usage_stays_held_but_invalid_answer_records_known_usage(controller, monkeypatch, failure):
    c = controller
    def broken(body):
        value = c[6](body)
        if failure == "timeout":
            raise TimeoutError("untrusted provider details")
        if failure == "usage":
            value["usage"]["input_tokens"] = True
        else:
            value["answers"] = {}
        return value
    monkeypatch.setattr(c[4], "_provider_call", broken)
    with pytest.raises(ValueError):
        run(c)
    assert receipt(c)["state"] == ("completed" if failure == "answers" else "uncertain")
    with pytest.raises(ValueError, match="already_attempted"):
        run(c)
    assert len(c[5]) == 1


def test_cancellation_during_request_records_usage_but_returns_no_candidate(controller, monkeypatch):
    c = controller
    def cancel(body):
        value = c[6](body)
        c[0].transition("v_one", c[1], c[2]["id"], c[2]["review_digest"], "cancel")
        return value
    monkeypatch.setattr(c[4], "_provider_call", cancel)
    with pytest.raises(ValueError, match="not_executing"):
        run(c)
    assert receipt(c)["actual_micro"] == 42


def test_budget_exhaustion_refuses_next_provider_request(controller):
    c = controller
    from app.build import device_invocations
    device_invocations.admit("v_one", c[1], c[2]["id"], invocation_id="other",
        request_digest="a" * 64, quote_micro=6000)
    with pytest.raises(ValueError, match="budget_exhausted"):
        run(c)
    assert not c[5] and receipt(c) is None


def test_provider_token_receipts_cannot_be_overwritten(controller):
    c = controller
    run(c)
    from app.build import device_invocations
    args = ("v_one", c[1], c[2]["id"])
    original = receipt(c)["usage"]
    device_invocations.record_result(*args, invocation_id="call1", actual_micro=42, usage=original)
    with pytest.raises(ValueError, match="usage_conflict"):
        device_invocations.record_result(*args, invocation_id="call1", actual_micro=42,
            usage={**original, "input_tokens": 999})
    with pytest.raises(ValueError, match="usage_invalid"):
        device_invocations.record_result(*args, invocation_id="call1", actual_micro=42,
            usage={**original, "output_tokens": True})
    assert receipt(c)["usage"] == original


def test_document_profile_is_restored_only_from_reviewed_manifest(controller):
    svc, _, _, browser, jev, *_ = controller
    exact = "One\nTwo\nThree"
    fields = dict(operation="desktop_goal_v1", application_bundle="com.apple.TextEdit",
        process_id=42, process_start_identity="123", window_id=7,
        reviewed_text=[exact, "/tmp/reviewed.txt"], document_text_index=0)
    manifest = svc.Manifest(task_id="founder_mac_v_one", title="Mac task", instruction="Write reviewed lines",
        applications=["com.apple.TextEdit"], origins=[], actions=["desktop.read", "desktop.interact"],
        credit_ceiling_micro=500000, duration_seconds=900, desktop_verification=fields)
    untrusted = Observation(revision="ps1_review", application="com.apple.TextEdit", location="mac-window:7",
        goal="untrusted", context="", controls=[], reviewed_text=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False)
    saved = {"manifest": manifest.model_dump(exclude_none=True)}
    scoped = jev._scoped_observation(saved, untrusted)
    assert scoped.document_text_index == 0 and scoped.reviewed_text == fields["reviewed_text"]
    injected = Observation(**{**untrusted.model_dump(), "reviewed_text": ["Injected\nUnapproved\nText"],
        "document_text_index": 0})
    assert jev._scoped_observation(saved, injected).reviewed_text == fields["reviewed_text"]
    plain = svc.Manifest.model_validate({**saved["manifest"], "desktop_verification": {
        **fields, "reviewed_text": ["Only plain reviewed text"], "document_text_index": None}})
    restricted = jev._scoped_observation({"manifest": plain.model_dump(exclude_none=True)}, injected)
    assert restricted.document_text_index is None and restricted.reviewed_text == ["Only plain reviewed text"]
    browser_manifest = svc.Manifest(task_id="browser", title="Browser", instruction="Reviewed browser task",
        applications=[browser.application], origins=["https://example.test"], actions=["browser.interact"],
        credit_ceiling_micro=1000, duration_seconds=60)
    browser_injected = browser.model_copy(update={"document_text_index": 0})
    assert jev._scoped_observation({"manifest": browser_manifest.model_dump(exclude_none=True)},
        browser_injected).document_text_index is None
