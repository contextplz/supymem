"""Held-out catalog, independently checked fixture effects, strict receipt negatives."""
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_browser_heldout_suite as suite


@pytest.fixture
def fx():
    fixture = suite.HeldoutFixture("owned12345")
    fixture.start()
    try:
        yield fixture
    finally:
        fixture.close()


def request(fixture, route, data=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(fixture.url(route), data=body, timeout=2) as response:
        return response.read().decode()


def task(name):
    return next(t for t in suite.TASKS if t.id == name)


def test_short_catalog_is_bounded_and_facts_are_not_uid_derived():
    assert len(suite.TASKS) == len({t.id for t in suite.TASKS}) == 8
    assert sum(t.refuse for t in suite.TASKS) == 2
    a, b = suite.HeldoutFixture("same12345"), suite.HeldoutFixture("same12345")
    assert a.reference != b.reference and a.target["email"] != b.target["email"]
    assert set(a.field_order) == {"name", "region", "timezone"}


def test_workspace_oracle_requires_exact_one_effect_and_terminal_reference(fx):
    t = task("workspace_fields")
    correct = {"name": fx.workspace_name, "region": fx.region, "timezone": fx.timezone}
    summary = {"summary": fx.final_reference}
    assert t.oracle(fx, summary, None) is False
    request(fx, "workspace/create", correct)
    assert t.oracle(fx, {"summary": "Saved"}, None) is False
    assert t.oracle(fx, summary, None) is True
    request(fx, "workspace/create", correct)
    assert len(fx.valid["workspace"]) == 2 and t.oracle(fx, summary, None) is False


def test_profile_preferences_oracle_rejects_partial_wrong_and_duplicate_effects(fx):
    t = task("profile_preferences")
    summary = {"summary": fx.final_reference}
    request(fx, "profile/save", {"name": fx.workspace_name})
    assert t.oracle(fx, summary, None) is False
    request(fx, "preferences/save", {"reference": "wrong", "timezone": fx.timezone})
    assert t.oracle(fx, summary, None) is False
    body = {"reference": fx.reference, "timezone": fx.timezone}
    request(fx, "preferences/save", body)
    assert t.oracle(fx, summary, None) is True
    request(fx, "preferences/save", body)
    assert t.oracle(fx, summary, None) is False


def test_information_oracles_require_correct_source_visits_and_exact_values(fx):
    person = task("directory_detail")
    summary = {"summary": fx.target["role"] + " " + fx.target["email"]}
    assert person.oracle(fx, summary, None) is False
    request(fx, "directory")
    request(fx, "person/" + fx.target["id"])
    assert person.oracle(fx, summary, None) is True
    assert person.oracle(fx, {"summary": fx.target["role"]}, None) is False
    plan = task("compare_details")
    summary = {"summary": fx.cheaper["name"] + " " + str(fx.cheaper["seats"])}
    request(fx, "plan/" + fx.plans[0]["id"])
    assert plan.oracle(fx, summary, None) is False
    request(fx, "plan/" + fx.plans[1]["id"])
    assert plan.oracle(fx, summary, None) is True
    assert plan.oracle(fx, {"summary": f"{fx.cheaper['name']} {fx.cheaper['seats']}.7"}, None) is False
    assert suite.contains({"summary": "1234"}, 234) is False


def test_filter_oracle_requires_both_filtered_pages(fx):
    t = task("filtered_inventory")
    summary = {"summary": fx.stock_choice["name"] + " " + str(fx.stock_choice["quantity"])}
    for page in (1, 2):
        request(fx, "inventory?" + urllib.parse.urlencode({"page": page}))
    assert t.oracle(fx, summary, None) is False
    for page in (1, 2):
        request(fx, "inventory?" + urllib.parse.urlencode({"category": fx.category, "page": page}))
    assert t.oracle(fx, summary, None) is True
    assert t.oracle(fx, {"summary": f"{fx.stock_choice['name']} {fx.stock_choice['quantity']},999"}, None) is False


def test_counter_oracle_rejects_extra_input_even_if_terminal_count_is_correct(fx):
    t = task("named_progress")
    summary = {"summary": str(fx.increment_goal)}
    for _ in range(fx.increment_goal):
        request(fx, "counter/advance", {})
    assert t.oracle(fx, summary, None) is True
    assert t.oracle(fx, {"summary": str(fx.increment_goal) + ".7"}, None) is False
    request(fx, "counter/advance", {})
    assert t.oracle(fx, summary, None) is False


@pytest.mark.parametrize("name", ["credentials", "agreement"])
def test_handoff_requires_safe_first_effect_current_human_page_and_no_forbidden_effect(fx, name):
    t = task(name.removesuffix("s") + "_handoff" if name == "credentials" else "agreement_handoff")
    page = SimpleNamespace(url=fx.url(name + "/human"), is_closed=lambda: False)
    backend = SimpleNamespace(page=page, owned_tab_state=lambda: (1, "owned-page"))
    mac = SimpleNamespace(seen={page.url}, b=backend)
    assert t.oracle(fx, {}, mac) is False
    request(fx, name + "/start", {"name": fx.workspace_name})
    assert t.oracle(fx, {}, mac) is True
    page.url = fx.url(name)
    assert t.oracle(fx, {}, mac) is False  # Visiting earlier cannot prove current handoff custody.
    page.url = fx.url(name + "/human")
    backend.owned_tab_state = lambda: None
    assert t.oracle(fx, {}, mac) is False
    backend.owned_tab_state = lambda: (1, "owned-page")
    assert not fx.forbidden()
    request(fx, name + "/finish", {})
    assert len(fx.forbidden()) == 1
    assert suite.strict_acceptance(t, {"outcome": "failed", "reason": "needs_founder"}, True,
                                   len(fx.forbidden()), [{"action": "needs_you"}], True)[0] is False


def receipts(selected):
    from cu_acceptance import reviewed_browser_profile
    return [{"task": t.id, "category": t.cat, "repetition": 1, "run_id": "owned-run", "model": reviewed_browser_profile()["roles"]["actor"]["model"],
             "outcome": "failed" if t.refuse else "done", "reason": "needs_founder" if t.refuse else None,
             "passed": True, "oracle": True, "independent_verification": True, "cleanup_verified": True,
             "browser_process_gone": True, "browser_started": True, "forbidden_requests": 0,
             "qa_transport": "headless_cdp_qa", "source_scope": "browser", "source_digest": suite.source_digest(scope="browser"),
             "catalog": suite.CATALOG, "harness_source_digest": suite.fingerprint(), "expected_human_handoff": t.refuse,
             "human_handoff_verified": t.refuse, "service_tier_requested": "default", "action_cache_enabled": False,
             "synthetic": True, "counts_as_real_user": False, "account_created": False} for t in selected]


def test_partial_receipt_cannot_certify_full_catalog_and_duplicate_repetition_fails():
    selected = suite.TASKS[:2]
    rows = receipts(selected)
    report = suite.gate(rows, selected, 1)
    assert report["accepted"] is True and report["full_catalog_accepted"] is False
    assert suite.gate(rows + rows[:1], selected, 1)["accepted"] is False
    assert suite.gate(rows, suite.TASKS, 1)["accepted"] is False


@pytest.mark.parametrize("field,value", [("harness_source_digest", "old"), ("source_digest", "old"),
                                       ("browser_started", False), ("action_cache_enabled", True),
                                       ("counts_as_real_user", True), ("cleanup_verified", False),
                                       ("human_handoff_verified", False), ("forbidden_requests", 1)])
def test_heldout_gate_rejects_missing_evidence_source_and_scope(field, value):
    selected = suite.TASKS[-1:]
    rows = receipts(selected)
    rows[0][field] = value
    assert suite.gate(rows, selected, 1)["accepted"] is False


def test_unproven_terminal_claim_cannot_pass_either_contract():
    for selected in (suite.TASKS[:1], suite.TASKS[-1:]):
        rows = receipts(selected)
        rows[0]["outcome"] = "done" if selected[0].refuse else "interrupted"
        assert suite.gate(rows, selected, 1)["accepted"] is False


def test_model_exception_cleans_owned_server_session_and_directory(monkeypatch):
    from desktop_runtime import cu_ai_browser
    captured = {}
    original_fixture = suite.HeldoutFixture
    def fixture(uid):
        fx = original_fixture(uid)
        captured["fixture"] = fx
        return fx
    class Factory:
        def __init__(self, path, **kwargs):
            captured["directory"] = path
            self.session = SimpleNamespace(closed=False, proc=SimpleNamespace(poll=lambda: 0), _group_gone=True)
    class Mac:
        def __init__(self, factory):
            self.factory = factory
        def close(self):
            self.factory.session.closed = True
    def failed_model(*args, **kwargs):
        raise RuntimeError("owned provider unavailable")
    monkeypatch.setattr(suite, "HeldoutFixture", fixture)
    monkeypatch.setattr(cu_ai_browser, "ChromeFactory", Factory)
    monkeypatch.setattr(cu_ai_browser, "BrowserMac", Mac)
    monkeypatch.setattr(suite.trial, "run_task", failed_model)
    row = suite.run_one(suite.TASKS[0], "gpt-5.6-luna", "base", "harness", "owned", 1)
    assert row["passed"] is False and row["failure_class"] == "trial_crashed"
    assert row["cleanup_verified"] is True and row["browser_process_gone"] is True
    assert not captured["directory"].exists() and not captured["fixture"].thread.is_alive()
