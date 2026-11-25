"""Multiline authority remains one exact bounded reviewed TextEdit literal."""
import pytest
from coding_agents.document_text import textedit_three_lines
from coding_agents.jev_decision import Observation


@pytest.mark.parametrize("value", ["One\nTwo", "One\nTwo\nThree\nFour", "One\n\nThree", "One\t\nTwo\nThree", "One\r\nTwo\nThree", "One\x7f\nTwo\nThree", "x" * 315 + "\nTwo\nThree"])
def test_document_profile_rejects_controls_count_and_total_bounds(value):
    with pytest.raises(ValueError):
        textedit_three_lines(value)


def test_observation_profile_is_exact_document_bound_and_digest_bound():
    base = dict(revision="ps1_review", application="com.apple.TextEdit", location="mac-window:44",
        goal="Write three reviewed lines", context="", controls=[], permitted=True,
        confirmation_pending=False, prior_effect_uncertain=False,
        reviewed_text=["One\nTwo\nThree", "/tmp/reviewed.txt"], document_text_index=0)
    observed = Observation(**base)
    assert observed.reviewed_text[0] == "One\nTwo\nThree"
    changed = Observation(**{**base, "reviewed_text": ["One\nTwo\nChanged", "/tmp/reviewed.txt"]})
    assert observed.digest() != changed.digest()
    for change in [{"application": "com.apple.Safari"}, {"location": "https://example.com"},
                   {"document_text_index": None}, {"document_text_index": 1}, {"document_text_index": False},
                   {"reviewed_text": ["One\nTwo\nThree", "bad\npath"]}]:
        with pytest.raises(ValueError):
            Observation(**{**base, **change})



def test_multiline_order_refuses_generic_driver_before_claim(monkeypatch):
    from desktop_runtime.device_worker import DeviceWorker
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    app = AppChoice("TextEdit", "com.apple.TextEdit", 123, "123456789")
    monkeypatch.setattr(PeekabooDriver, "running_apps", classmethod(lambda cls: [app]))
    class Gateway:
        order_id, venture_id, execution_id = "dwo_review", "ven_review", None
        claim_attempted = False
    manifest = dict(applications=[app.bundle_id], origins=[], actions=["desktop.read", "desktop.interact"],
        duration_seconds=60, instruction="Write the reviewed document", desktop_verification=dict(
            operation="desktop_goal_v1", application_bundle=app.bundle_id, process_id=123,
            process_start_identity="123456789", window_id=44, delivery_mode="background",
            reviewed_text=["One\nTwo\nThree"], document_text_index=0))
    worker = DeviceWorker(Gateway(), lambda _: None)
    worker.run_desktop({"state": "approved"}, manifest)
    assert worker.last["failure_code"] == "desktop_document_profile_unsupported"
    assert worker.gateway.claim_attempted is False


@pytest.mark.parametrize('goal', [
    'Type exactly "Reviewed 0.1.79 text".',
    'Prepare a Mac task: in TextEdit type only "Reviewed 0.1.79 text".',
    'Type exactly "Reviewed 0.1.79 text" into the selected TextEdit document. Do not save.',
])
def test_exact_typing_only_goal_preserves_literal(goal):
    from coding_agents.document_text import textedit_typing_only_goal
    assert textedit_typing_only_goal(goal) == 'Reviewed 0.1.79 text'


@pytest.mark.parametrize('goal', [
    'Create a new document and type only "Reviewed text".',
    'Type exactly "Reviewed text"; save it.',
    'Make a plain text document and type only "Reviewed text".',
    'Type only "Reviewed text". Do not save. Then create a new document.',
    'Type only "Reviewed text" and click Done.',
    'Type only "Reviewed text" and type exactly "Other text".',
    'Type "Reviewed text".',
])
def test_text_equality_cannot_verify_other_document_goals(goal):
    from coding_agents.document_text import textedit_typing_only_goal
    assert textedit_typing_only_goal(goal) is None


def test_quoted_version_text_does_not_create_typing_fragments():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.build.device_orders import _desktop_reviewed_text
    for quote in ['"', "'"]:
        goal = f'Prepare a Mac task: type exactly {quote}Supymem 0.1.79 semantic input test{quote} into the selected TextEdit document. Do not save.'
        assert _desktop_reviewed_text(goal, application_bundle='com.apple.TextEdit') == ['Supymem 0.1.79 semantic input test']
