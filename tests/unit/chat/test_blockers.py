"""Blocker guides: registry completeness, explain output, with_explain, no consequential action."""
import pytest

from tests.company_os_harness import load_company_os_app
from tests.unit.chat.test_round3_matrix import venture as venture
from tests.unit.pagebuilder.test_api import setup as page_setup

load_company_os_app()

from app.chat.actions import REGISTRY  # noqa: E402
from app.chat.blockers import GUIDES, explain, with_explain  # noqa: E402


@pytest.mark.parametrize("domain", ["freight invoice reconciliation", "family pottery classes"])
def test_page_model_limit_explains_setup_without_sending_you_to_a_missing_page(page_setup, monkeypatch, domain):
    from app.chat.execute import _pagebuilder_quote_card
    from app.credits import estimate
    from app.db.models import Venture
    from app.pagebuilder import api

    data = page_setup
    with data["factory"]() as session:
        session.get(Venture, data["vid"]).raw_idea = domain
        session.commit()
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *args, **kwargs: api.MAX_PAGE_MICRO + 1)
    quote = api.price(data["vid"], data["actor"])
    assert quote["ready"] is False and quote["reason"] == "pagebuilder_model_exceeds_limit"
    reply = _pagebuilder_quote_card(data["vid"], quote)
    assert "model" in reply["body_md"].lower() and "limit" in reply["body_md"].lower()
    detail = explain(data["vid"], {"code": quote["reason"]})
    assert "model" in detail["body_md"].lower() and "Settings" in detail["body_md"]
    assert "saved page" not in detail["body_md"] and "credit" not in detail["body_md"].lower()
    assert not any(action.get("action") == "site.preview" for action in detail["actions"])
    assert detail["actions"][0]["href"] == f"/v/{data['vid']}/settings"


def test_credit_request_guide_distinguishes_saved_request_from_grant_and_delivery():
    guide = explain("ven_1", {"code": "insufficient_credits"})
    body = guide["body_md"]
    assert "usage limit" in body and "credit" not in body.lower()
    assert "Buy more to continue" in body and "\n" not in body
    assert "we will add" not in body and "follow up" not in body


def test_browser_decision_failure_preserves_review_and_unknown_usage_guidance():
    guide = explain("ven_1", {"code": "cu_browser_decision_unavailable"})
    body = guide["body_md"]
    assert "decision was unavailable" in body
    assert "not confirmation" in body and "credit" not in body
    assert "unconfirmed" in body and "reconcile" in body
    assert "refund" not in body.lower() and "automatically" not in body.lower()
    assert guide["actions"] == [{"label": "Review Mac tasks", "href": "/v/ven_1/device-work-orders", "style": "primary"}]


REQUIRED = {
    "insufficient_credits", "computer_use_permissions", "mac_not_connected", "no_hosted_app",
    "build_not_accepted", "waitlist_off", "waitlist_test_failed", "verified_contact_missing",
    "connection_missing", "coding_provider_missing", "validation_incomplete", "research_incomplete",
    "plan_needs_approval", "founder_only_work", "llm_unavailable", "session_expired",
}


def test_registry_complete():
    assert REQUIRED <= set(GUIDES)
    for code, g in GUIDES.items():
        assert g["title"] and g["why"], code
        if code == "job_failed":
            assert len(g["steps"]) == 1
        else:
            assert len(g["steps"]) >= 3, code
        assert g["actions"], code


@pytest.mark.parametrize("code", sorted(set(GUIDES) - {"llm_unavailable"}))
def test_explain_returns_steps_and_buttons(code):
    from app.core.local_mode import is_local

    out = explain("ven_1", {"code": code})
    assert out["kind"] == "text"
    assert ("\n1. " in out["body_md"]) == (code != "insufficient_credits" and not (is_local() and code == "mac_not_connected"))
    assert out["actions"] and all(a["href"] for a in out["actions"])
    assert "{vid}" not in str(out)


def test_permissions_deep_links():
    hrefs = [a["href"] for a in explain("v", {"code": "computer_use_permissions"})["actions"]]
    assert any(h.endswith("Privacy_ScreenCapture") for h in hrefs)
    assert any(h.endswith("Privacy_Accessibility") for h in hrefs)
    assert any(h.endswith("Privacy_Automation") for h in hrefs)
    assert "/v/v/settings#settings-mac" in hrefs
    assert "/setup/permissions" not in hrefs


@pytest.mark.parametrize("venture_id", ["ven_invoicing", "ven_gardening"])
def test_local_copy_desktop_reconnect_never_requires_a_hosted_account(monkeypatch, venture_id):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    for code in ("mac_not_connected", "cu_bridge_not_ready"):
        reply = explain(venture_id, {"code": code})
        assert "restart your app" in reply["body_md"].lower()
        assert "Settings > Keys and connections" in reply["body_md"]
        assert not any(text in reply["body_md"].lower() for text in ("sign in", "approve this mac", "account", "supymem.com"))
        assert reply["actions"][0]["href"] == "/settings/local"


def test_unknown_code_is_harmless():
    assert explain("v", {"code": "nope"})["actions"] == []


def test_waitlist_guides_remain_read_only_and_scoped_across_domains(monkeypatch):
    from app.chat import site_actions

    monkeypatch.setattr(site_actions, "waitlist_card", lambda *_: pytest.fail("A guide must not prepare a change"))
    for venture_id, audience in (("ven_freight", "Freight dispatch teams"), ("ven_garden", "Home gardeners")):
        for code in ("waitlist_off", "waitlist_test_failed"):
            reply = explain(venture_id, {"code": code, "audience": audience})
            assert reply["kind"] == "text" and "\n1. " in reply["body_md"]
            assert reply["actions"] and all(action["href"].startswith(f"/v/{venture_id}/") for action in reply["actions"])
            assert not any(action.get("action") or action.get("confirmation") for action in reply["actions"])


@pytest.mark.parametrize("raw", ["handler_failed:ValueError", "handler_failed_TypeError:job_123abc",
                                 "ValueError in job_123abc", "new_failure:RuntimeError job_private"])
def test_machine_failure_details_never_reach_founder(raw):
    from app.chat.blockers import humanize_error
    copy = humanize_error(raw)
    assert "saved work" in copy and "_" not in copy
    assert all(token not in copy for token in ("ValueError", "TypeError", "RuntimeError", "job_", "handler"))


@pytest.mark.parametrize("raw", ["provider_down", "provider down", "provider_down:ValueError job_private"])
def test_saved_provider_failure_keeps_plain_reason_without_private_diagnostics(raw):
    from app.chat.blockers import humanize_error
    copy = humanize_error(raw)
    assert "Your AI service did not respond" in copy and "saved work" in copy
    assert "provider_down" not in copy and "ValueError" not in copy and "job_private" not in copy


def test_legacy_timeout_and_final_check_failures_use_closed_copy():
    from app.chat.blockers import humanize_error
    timeout = humanize_error("research provider timed out; token=private-secret job_private")
    assert "research service took too long" in timeout
    assert "token=" not in timeout and "private" not in timeout and "[redacted]" not in timeout
    assert "final checks" in humanize_error("qa_failed:ValueError job_private")


def test_failed_job_code_keeps_known_prefix_without_exception_suffix():
    from app.chat.failed_jobs import clean_code, body
    assert clean_code("research_boundary_blocked:BoundaryViolation") == "research_boundary_blocked"
    copy = body("research.deep", "research_boundary_blocked:BoundaryViolation")
    assert "cannot send" in copy and "BoundaryViolation" not in copy


def test_legacy_assistant_failure_is_cleaned_on_read_without_rewriting_founder_text():
    from types import SimpleNamespace
    from app.chat.store import _ser
    raw = "Build stopped: handler failed:ValueError for job_fixture123."
    row = SimpleNamespace(id="message-fixture", workspace_id="workspace", conversation_id=None, role="assistant", kind="text",
                          body_md=raw, citations_json=None, actions_json=None, created_at=None)
    copy = _ser(row)["body_md"]
    assert "saved work is kept" in copy
    assert "ValueError" not in copy and "handler failed" not in copy and "job_fixture123" not in copy
    assert row.body_md == raw
    row.role = "user"
    assert _ser(row)["body_md"] == raw
    row.role, row.kind = "assistant", "memory_list"
    assert _ser(row)["body_md"] == raw


@pytest.mark.parametrize("answer", ["Warehouse teams reconcile delivery receipts", "Families organize weekend hikes"])
@pytest.mark.parametrize("actions_json", [None, "null", "[]"])
def test_free_text_question_history_preserves_text_without_actions(answer, actions_json):
    from types import SimpleNamespace
    from app.chat.store import _ser

    row = SimpleNamespace(id="free-text-question", workspace_id="workspace", conversation_id="saved-chat", role="assistant",
                          kind="idea_clarification_ready", body_md=answer, citations_json=None,
                          actions_json=actions_json, created_at=None)
    reply = _ser(row)
    assert reply["actions"] == []
    assert reply["body_md"] == answer
    assert reply["parts"] == [{"type": "text", "text": answer}]
    assert reply["conversation_id"] == "saved-chat"
    assert row.actions_json == actions_json


def test_with_explain_appended_once():
    r = with_explain({"kind": "needs_you", "actions": [{"label": "x", "href": "/y"}]}, "waitlist_off")
    r = with_explain(r, "waitlist_off")
    ex = [a for a in r["actions"] if a.get("action") == "blocker.explain"]
    assert len(ex) == 1 and ex[0]["label"] == "Explain in detail" and ex[0]["payload"] == {"code": "waitlist_off"}


def test_explain_action_is_not_consequential():
    assert REGISTRY["blocker.explain"].consequential is False


def test_cu_and_mac_offer_codes_alias_into_guides():
    from app.chat.blockers import ALIASES, guide_code

    cu = ["cu_permission_missing", "cu_bridge_not_ready", "cu_session_unavailable", "cu_field_not_cleared",
          "cu_field_value_mismatch", "cu_effect_unverified", "cu_repeated_action", "cu_window_changed",
          "cu_reconcile_unavailable", "cu_stopped"]
    for code in cu + ["mac_screen_recording", "mac_accessibility", "password_required"]:
        assert guide_code(code) in GUIDES, code
    assert all(t in GUIDES for t in ALIASES.values())
    assert "Privacy_ScreenCapture" in str(explain("v", {"code": "cu_permission_missing"})["actions"])
    assert with_explain({}, "cu_repeated_action")["actions"][0]["payload"] == {"code": "mac_repeated_action"}


def test_execute_dispatches_explain(venture):
    import asyncio

    from app.chat.execute import execute_action

    _, vid, _ = venture
    out = asyncio.run(execute_action(vid, "blocker.explain", payload={"code": "insufficient_credits"}))
    assert 'credit' not in out['body_md'].lower()
