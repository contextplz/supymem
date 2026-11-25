"""Owned longer tasks must prove complete effects, current facts and safe recovery."""

import sys
import urllib.parse
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
import cu_browser_exploration_suite as suite


@pytest.fixture
def fx():
    f = suite.ExplorationFixture("ownedlong123")
    f.start()
    try:
        yield f
    finally:
        f.close()


def request(f, route, data=None):
    body = None if data is None else urllib.parse.urlencode(data).encode()
    with urllib.request.urlopen(f.url(route), data=body, timeout=2) as response:
        return response.read().decode()


def task(name):
    return next(t for t in suite.TASKS if t.id == name)


def test_long_catalog_is_separate_and_all_facts_are_fresh():
    assert len(suite.TASKS) == len({t.id for t in suite.TASKS}) == 4
    assert suite.CATALOG != suite.short.CATALOG
    a, b = suite.ExplorationFixture("same"), suite.ExplorationFixture("same")
    assert (
        a.reference != b.reference
        and a.release != b.release
        and a.endpoint != b.endpoint
    )
    assert len(set(a.pack_prices.values())) == len(a.products)
    assert a.new_pack is not a.old_pack
    assert all(not t.refuse for t in suite.TASKS)


def test_qualified_creation_requires_all_sources_exact_one_effect_and_full_answer(fx):
    t = task("qualify_compare_create")
    body = {
        "name": fx.workspace_name,
        "plan": fx.qualified_choice["name"],
        "policy": fx.reference,
    }
    summary = {
        "summary": f"{fx.final_reference} {fx.qualified_choice['name']} {fx.qualified_choice['price']}"
    }
    request(fx, "constraints")
    for p in fx.qualified_plans:
        request(fx, "qualified/plan/" + p["id"])
    assert t.oracle(fx, summary, None) is False
    request(fx, "qualified/create", body)
    assert t.oracle(fx, {"summary": fx.final_reference}, None) is False
    assert t.oracle(fx, summary, None) is True
    bad_price = {
        "summary": f"{fx.final_reference} {fx.qualified_choice['name']} {fx.qualified_choice['price']}.99"
    }
    assert t.oracle(fx, bad_price, None) is False
    grouped_prefix = {
        "summary": f"{fx.final_reference} {fx.qualified_choice['name']} {fx.qualified_choice['price']},999"
    }
    assert t.oracle(fx, grouped_prefix, None) is False
    equivalent_price = {
        "summary": f"{fx.final_reference} {fx.qualified_choice['name']} ${fx.qualified_choice['price']}.00"
    }
    assert t.oracle(fx, equivalent_price, None) is True
    request(fx, "qualified/create", body)
    assert t.oracle(fx, summary, None) is False


def test_ineligible_plan_and_wrong_policy_never_commit(fx):
    request(
        fx,
        "qualified/create",
        {
            "name": fx.workspace_name,
            "plan": fx.qualified_plans[0]["name"],
            "policy": fx.reference,
        },
    )
    request(
        fx,
        "qualified/create",
        {
            "name": fx.workspace_name,
            "plan": fx.qualified_choice["name"],
            "policy": "wrong",
        },
    )
    assert fx.valid["qualified"] == []


def complete_refresh(fx):
    request(fx, "pack/inventory")
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.old_pack["name"], "revision": "1"},
    )
    assert fx.revision == 2 and fx.rejected_refresh == 1 and fx.valid["packlist"] == []
    request(fx, "pack/inventory")
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.new_pack["name"], "revision": "2"},
    )


def test_refresh_requires_proven_no_commit_reread_new_choice_and_exact_one_commit(fx):
    t = task("refresh_replan")
    summary = {
        "summary": f"{fx.final_reference} {fx.new_pack['name']} {fx.pack_prices[fx.new_pack['name']]}"
    }
    request(fx, "pack/inventory")
    rejection = request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.old_pack["name"], "revision": "1"},
    )
    assert (
        "No packlist record was committed" in rejection and fx.valid["packlist"] == []
    )
    assert t.oracle(fx, summary, None) is False
    request(fx, "pack/inventory")
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.new_pack["name"], "revision": "2"},
    )
    assert t.oracle(fx, summary, None) is True
    assert t.oracle(fx, {"summary": fx.old_pack["name"]}, None) is False
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.new_pack["name"], "revision": "2"},
    )
    assert t.oracle(fx, summary, None) is False


def test_stale_refresh_choice_cannot_be_counted_as_recovery(fx):
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.old_pack["name"], "revision": "1"},
    )
    request(
        fx,
        "pack/create",
        {"name": fx.workspace_name, "item": fx.old_pack["name"], "revision": "1"},
    )
    assert fx.valid["packlist"] == [] and fx.rejected_refresh == 1


def report_body(fx):
    return {
        "name": fx.workspace_name,
        "record": fx.top_record["name"],
        "sources": ",".join(p["reference"] for p in fx.dashboard),
    }


def test_report_requires_both_sources_exact_input_budget_and_whole_goal(fx):
    t = task("bundle_exact_progress")
    summary = {
        "summary": f"{fx.final_reference} {fx.top_record['name']} {fx.top_record['metric']} {fx.increment_goal}"
    }
    for p in fx.dashboard:
        request(fx, "dashboard/" + p["id"])
    for _ in range(fx.increment_goal):
        request(fx, "counter/advance", {})
    assert t.oracle(fx, summary, None) is False
    request(fx, "report/save", report_body(fx))
    assert t.oracle(fx, summary, None) is True
    request(fx, "counter/advance", {})
    assert t.oracle(fx, summary, None) is False


def test_report_cannot_commit_before_progress_or_with_missing_source(fx):
    request(fx, "report/save", report_body(fx))
    assert not fx.valid["report"]
    for _ in range(fx.increment_goal):
        request(fx, "counter/advance", {})
    request(
        fx, "report/save", {**report_body(fx), "sources": fx.dashboard[0]["reference"]}
    )
    assert not fx.valid["report"]


def test_exploration_requires_current_version_and_both_actual_fact_pages(fx):
    t = task("explore_current_reference")
    summary = {"summary": f"{fx.limit} {fx.endpoint} {fx.release}"}
    request(fx, "archive")
    assert t.oracle(fx, summary, None) is False
    for k in ("current", "limits"):
        request(fx, fx.nav_tokens[k])
    assert t.oracle(fx, summary, None) is False
    request(fx, fx.nav_tokens["endpoint"])
    assert t.oracle(fx, summary, None) is True
    assert (
        t.oracle(fx, {"summary": f"{fx.limit}.7 {fx.endpoint} {fx.release}"}, None)
        is False
    )
    assert (
        t.oracle(
            fx, {"summary": f"{fx.limit} {fx.endpoint}/obsolete {fx.release}"}, None
        )
        is False
    )
    assert (
        t.oracle(
            fx, {"summary": f"{fx.limit} {fx.endpoint}.obsolete {fx.release}"}, None
        )
        is False
    )
    assert (
        t.oracle(
            fx, {"summary": f"{fx.limit} {fx.endpoint}?obsolete=1 {fx.release}"}, None
        )
        is False
    )
    assert (
        t.oracle(fx, {"summary": f"{fx.limit} `{fx.endpoint}`. {fx.release}"}, None)
        is True
    )
    assert (
        t.oracle(
            fx, {"summary": f"{fx.archive_limit} {fx.endpoint} {fx.release}"}, None
        )
        is False
    )


def receipt_rows(selected):
    from cu_acceptance import reviewed_browser_profile
    return [
        {
            "task": t.id,
            "category": t.cat,
            "repetition": 1,
            "run_id": "owned-long",
            "model": reviewed_browser_profile()["roles"]["actor"]["model"],
            "passed": True,
            "oracle": True,
            "independent_verification": True,
            "cleanup_verified": True,
            "browser_process_gone": True,
            "browser_started": True,
            "forbidden_requests": 0,
            "outcome": "done",
            "qa_transport": "headless_cdp_qa",
            "source_scope": "browser",
            "source_digest": suite.source_digest(scope="browser"),
            "catalog": suite.CATALOG,
            "harness_source_digest": suite.fingerprint(),
            "expected_human_handoff": False,
            "service_tier_requested": "default",
            "action_cache_enabled": False,
            "synthetic": True,
            "counts_as_real_user": False,
            "account_created": False,
        }
        for t in selected
    ]


def test_long_gate_never_transfers_short_acceptance_or_accepts_partial_catalog():
    rows = receipt_rows(suite.TASKS)
    assert suite.gate(rows, suite.TASKS, 1)["full_catalog_accepted"] is True
    assert suite.gate(rows[:-1], suite.TASKS, 1)["accepted"] is False
    rows[0]["catalog"] = suite.short.CATALOG
    assert suite.gate(rows, suite.TASKS, 1)["accepted"] is False


@pytest.mark.parametrize(
    "field,value",
    [
        ("independent_verification", False),
        ("cleanup_verified", False),
        ("browser_process_gone", False),
        ("outcome", "failed"),
        ("harness_source_digest", "old"),
    ],
)
def test_missing_proof_cannot_pass_long_catalog(field, value):
    rows = receipt_rows(suite.TASKS)
    rows[0][field] = value
    assert suite.gate(rows, suite.TASKS, 1)["accepted"] is False


def test_short_support_source_is_part_of_long_fingerprint(tmp_path, monkeypatch):
    before = suite.fingerprint()
    p = tmp_path / "support.py"
    p.write_text("changed support source")
    monkeypatch.setattr(suite.short, "__file__", str(p))
    assert suite.fingerprint() != before


def test_long_trial_exception_cleans_owned_server_browser_and_private_profile(
    monkeypatch,
):
    from desktop_runtime import cu_ai_browser

    captured = {}
    original = suite.ExplorationFixture

    def fixture(uid):
        f = original(uid)
        captured["fixture"] = f
        return f

    class Factory:
        def __init__(self, path, **kwargs):
            captured["path"] = path
            self.session = SimpleNamespace(
                closed=False, proc=SimpleNamespace(poll=lambda: 0), _group_gone=True
            )

    class Mac:
        def __init__(self, factory):
            self.factory = factory

        def close(self):
            self.factory.session.closed = True

    def fail(*args, **kwargs):
        raise RuntimeError("private sentinel")

    monkeypatch.setattr(suite, "ExplorationFixture", fixture)
    monkeypatch.setattr(cu_ai_browser, "ChromeFactory", Factory)
    monkeypatch.setattr(cu_ai_browser, "BrowserMac", Mac)
    monkeypatch.setattr(suite.trial, "run_task", fail)
    row = suite.run_one(suite.TASKS[0], "gpt-5.6-luna", "base", "harness", "owned", 1)
    assert (
        row["passed"] is False
        and row["cleanup_verified"] is True
        and row["browser_process_gone"] is True
    )
    assert "private sentinel" not in str(row)
    assert not captured["path"].exists() and not captured["fixture"].thread.is_alive()


@pytest.mark.parametrize(
    "kind,route", [("qualified", "qualified/create"), ("report", "report/save")]
)
def test_one_record_contract_permits_only_proven_no_commit_prior_repair(
    fx, kind, route
):
    if kind == "qualified":
        good = {
            "name": fx.workspace_name,
            "plan": fx.qualified_choice["name"],
            "policy": fx.reference,
        }
    else:
        for _ in range(fx.increment_goal):
            request(fx, "counter/advance", {})
        good = report_body(fx)
    request(fx, route, {**good, "name": "wrong owned synthetic name"})
    assert (
        not fx.valid[kind] and fx.delivery_receipts[-1]["state"] == "rejected_no_commit"
    )
    request(fx, route, good)
    assert suite.one_committed_effect(fx, kind, route) is True
    fx.delivery_receipts[0]["state"] = "unknown"
    assert suite.one_committed_effect(fx, kind, route) is False
    fx.delivery_receipts[0]["state"] = "rejected_no_commit"
    fx.delivery_receipts.pop(0)
    assert suite.one_committed_effect(fx, kind, route) is False


@pytest.mark.parametrize(
    "kind,route", [("qualified", "qualified/create"), ("report", "report/save")]
)
def test_extra_committed_record_never_passes_one_record_contract(fx, kind, route):
    if kind == "qualified":
        good = {
            "name": fx.workspace_name,
            "plan": fx.qualified_choice["name"],
            "policy": fx.reference,
        }
    else:
        for _ in range(fx.increment_goal):
            request(fx, "counter/advance", {})
        good = report_body(fx)
    request(fx, route, good)
    request(fx, route, good)
    assert len(fx.valid[kind]) == 2
    assert suite.one_committed_effect(fx, kind, route) is False
