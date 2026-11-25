"""Exact catalog/fixture contract checks; no browser, HTTP server or provider."""
from __future__ import annotations

import ast
import hashlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/desktop"))

from core.browser_qualification import LONG_TASKS, SHORT_TASKS  # noqa: E402
from core.browser_qualification_catalog import (  # noqa: E402
    CATALOG, CATALOG_DIGEST, FIXTURE_SOURCE_SHA256, LONG_PASS_PERCENT, SHORT_PASS_PERCENT,
    SUPPORTED_FIXTURE_TASKS, catalog_task, fixture_page, form_submission,
)
from core.canonical import digest  # noqa: E402

BASE = "/public/browser-qualification/" + "a" * 32


def test_catalog_retains_exact_26_plus_six_plus_four_goals_and_oracles():
    import cu_browser_suite as short
    import cu_browser_exploration_suite as exploration

    assert len(CATALOG) == 36
    assert [task.task_id for task in CATALOG] == list(SHORT_TASKS + LONG_TASKS)
    assert SHORT_PASS_PERCENT == 95 and LONG_PASS_PERCENT == 80
    tree = ast.parse((ROOT / "packages/core/browser_qualification_catalog.py").read_text())
    implementation = ast.Module(body=[node for node in tree.body if not (isinstance(node, ast.Assign)
                                and any(isinstance(name, ast.Name) and name.id in {"FIXTURE_SOURCE_SHA256", "CATALOG_DIGEST"}
                                        for name in node.targets))], type_ignores=[])
    assert FIXTURE_SOURCE_SHA256 == hashlib.sha256(ast.dump(implementation, include_attributes=False).encode()).hexdigest()
    assert CATALOG_DIGEST == digest({"tasks": [vars(task) for task in CATALOG], "fixture_source": FIXTURE_SOURCE_SHA256})
    for task in short.TASKS:
        saved = catalog_task(task.id)
        assert (saved.goal, saved.category, saved.refusal, saved.max_steps, saved.max_seconds) == (
            task.task, task.cat, task.refuse, task.max_steps, task.max_seconds)
    doc = yaml.safe_load((ROOT / "docs/supymem/beta-2026-10-04/browser-tests/long_tasks.yaml").read_text())
    for task in doc["tasks"]:
        saved = catalog_task(task["id"])
        assert saved.goal == task["goal"] and saved.max_steps == task["max_steps"]
        assert json.loads(saved.requirements_json) == {
            key: value for key, value in task.items() if key not in {"id", "category", "goal", "max_steps"}}
    fx = exploration.ExplorationFixture("abcdef1234")
    fx.httpd = SimpleNamespace(server_address=("127.0.0.1", 12345))
    for task in exploration.TASKS:
        saved = catalog_task(task.id)
        values = vars(fx) | {"url_" + route: fx.url(route) for route in ("qualified", "pack", "report", "portal")}
        assert saved.goal.format_map(values) == task.goal(fx)
        assert saved.category == task.cat and json.loads(saved.requirements_json)["intended_steps"] == task.intended_steps
    # A changed source oracle must change the reviewed catalog, never reuse its old digest.
    for relative, cohort in (("scripts/desktop/cu_browser_suite.py", "short26"),
                              ("scripts/desktop/cu_browser_exploration_suite.py", "exploration4")):
        tree = ast.parse((ROOT / relative).read_text())
        tasks = next(node.value for node in tree.body if isinstance(node, ast.Assign)
                     and any(isinstance(name, ast.Name) and name.id == "TASKS" for name in node.targets))
        for task in tasks.elts:
            saved = catalog_task(ast.literal_eval(task.args[0]))
            oracle = task.args[4] if cohort == "exploration4" else task.args[3]
            assert saved.oracle_sha256 == hashlib.sha256(ast.dump(oracle, include_attributes=False).encode()).hexdigest()
    tree = ast.parse((ROOT / "scripts/desktop/cu_browser_round1.py").read_text())
    for task in (task for task in CATALOG if task.cohort == "long6"):
        oracle = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                      and node.name == "o_" + task.task_id.split("_")[0])
        assert task.oracle_sha256 == hashlib.sha256(ast.dump(oracle, include_attributes=False).encode()).hexdigest()


def test_shared_form_contract_uses_real_stable_controls_and_original_validation():
    from cu_tasks.fixture_server import FixtureServer

    fx = FixtureServer("abcdef1234")
    page = fixture_page("form_valid", BASE)
    assert page.status == 200 and page.state_change == "none"
    for identity in ("document-form", "form-project", "form-submit", "project", "email", "code"):
        assert f'id="{identity}"'.encode() in page.body
    assert f'action="{BASE}/form"'.encode() in page.body
    assert fx.form_error({"code": "abc123", "email": "qa@example.invalid"}) == (
        "Error: code must be exactly 6 UPPERCASE letters or digits")
    good = {"project": "Atlas", "email": "qa@example.invalid", "code": "ABC123"}
    assert fx.form_error(good) is None
    accepted = form_submission(good, BASE)
    assert accepted.status == 303 and accepted.headers == (("Location", BASE + "/form/done"),)
    assert accepted.state_change == "commit" and not hasattr(accepted, "receipt")
    invalid = form_submission(good | {"code": "abc123"}, BASE)
    assert invalid.status == 400 and invalid.state_change == "rejected_before_mutation"
    assert b"Error: code must be exactly" in invalid.body
    with pytest.raises(ValueError, match="body_invalid"):
        form_submission(good | {"password": "anything"}, BASE)


def test_payment_refusal_cannot_be_a_positive_success_or_receive_mutation_authority():
    import cu_browser_suite as short

    page = fixture_page("pay_wall", BASE)
    assert page.status == 200 and page.state_change == "none" and b"Gadget $49" in page.body
    payment = next(task for task in short.TASKS if task.id == "pay_wall")
    positive = next(task for task in short.TASKS if task.id == "form_valid")
    rows = [{"action": {"type": "needs_you"}, "outcome": "needs_you"}]
    result = {"outcome": "failed", "reason": "needs_founder"}
    assert short.strict_acceptance(positive, result, True, 0, rows, True)[0] is False
    assert short.strict_acceptance(payment, result, True, 1, rows, True)[0] is False
    assert len(SUPPORTED_FIXTURE_TASKS) == 35 and "l01_research_synthesis" not in SUPPORTED_FIXTURE_TASKS
    with pytest.raises(ValueError, match="unsupported"):
        fixture_page("todos", BASE)


@pytest.mark.parametrize("path", ["https://api.supymem.com/elsewhere", BASE + "/../evil", BASE + "?other=1", "/form"])
def test_fixture_path_is_exact_controller_namespace(path):
    with pytest.raises(ValueError, match="path_invalid"):
        fixture_page("form_valid", path)


def state(task_id):
    from core.browser_qualification_catalog import new_fixture_state
    return new_fixture_state(task_id, "abcdef1234", BASE)


def contract(task_id):
    from core.browser_qualification_catalog import fixture_contract
    return fixture_contract(state(task_id))


def effect_for(spec, path, fields=None):
    return next(rule for rule in spec["rules"] if rule.method == "POST" and rule.path.endswith(path)
                and (fields is None or {field.name: field.value for field in rule.fields} == fields))


def test_all_exact_catalog_tasks_have_bounded_real_source_contracts_without_public_fact_substitution():
    from core.browser_qualification import OwnedFixtureGrant
    from core.browser_qualification_catalog import fixture_contract, fixture_preview

    for task in CATALOG:
        snapshot = state(task.task_id)
        restored = json.loads(json.dumps(snapshot))
        spec = fixture_contract(restored)
        assert spec["catalog_digest"] == CATALOG_DIGEST and len(spec["seed_digest"]) == 64
        assert len(spec["rules"]) <= 128
        if task.task_id == "l01_research_synthesis":
            assert spec["rules"] == () and spec["fixture_supported"] is False
            assert spec["public_sources"] == ("pypi.org", "github.com", "files.pythonhosted.org")
            assert "https://pypi.org/" in spec["goal"] and BASE not in spec["goal"]
            assert spec["start_url"] == "https://pypi.org/"
            continue
        assert spec["start_url"].startswith("https://api.supymem.com" + BASE + "/")
        if task.task_id == "l04_ten_page_collection":
            assert spec["start_url"].endswith("/catalog?page=1")
        grant = OwnedFixtureGrant(
            1, "usr_fixture", "dev_fixture", "ws_fixture", "ven_fixture", "trial_fixture",
            CATALOG_DIGEST, task.task_id, spec["seed_digest"], "order_fixture", "b" * 64, "c" * 64,
            2_000_000_000, "a" * 32, "https://api.supymem.com", spec["rules"],
            spec["allowed_operations"], spec["max_tabs"], spec["frames"], spec["downloads"])
        assert OwnedFixtureGrant.from_dict(grant.to_dict()).grant_digest == grant.grant_digest
        for rule in spec["rules"]:
            assert rule.path.startswith(BASE + "/") and rule.document_path.startswith(BASE + "/")
            if rule.method == "GET":
                preview = fixture_preview(restored, rule.path.removeprefix(BASE), rule.query)
                assert preview.status in {200, 303, 503}
                if preview.body and preview.media_type.startswith("text/html"):
                    assert f'id="{rule.document_id}"'.encode() in preview.body
            else:
                page = fixture_preview(restored, rule.document_path.removeprefix(BASE))
                assert f'id="{rule.form_id}"'.encode() in page.body
                # Dynamic wizard controls are source JS-created, not claimed as a rendered-browser proof.
                if rule.document_path != BASE + "/wizard2":
                    assert f'id="{rule.control_id}"'.encode() in page.body
                for field in rule.fields:
                    if field.input_value is None:
                        assert field.input_type is None
        if task.refusal:
            assert all(rule.method == "GET" for rule in spec["rules"])
            assert not set(spec["allowed_operations"]) & {"type", "click", "select", "check", "download"}


def test_form_actual_state_and_redirect_source_does_not_fabricate_delivery():
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    snapshot = state("form_valid")
    spec = fixture_contract(snapshot)
    submit = effect_for(spec, "/form")
    done = next(rule for rule in spec["rules"] if rule.path == BASE + "/form/done")
    assert done.prerequisites == (submit.rule_id,)
    with pytest.raises(ValueError, match="not_committed"):
        fixture_transition(snapshot, done)
    body = {field.name: field.value for field in submit.fields}
    transitioned = fixture_transition(snapshot, submit, body)
    assert transitioned.response.status == 303 and transitioned.response.state_change == "commit"
    assert transitioned.response.headers == (("Location", BASE + "/form/done"),)
    assert transitioned.request_metadata["response"] is None
    assert transitioned.state["state"]["posts"][0]["value"][1] == body
    done_response = fixture_transition(transitioned.state, done)
    assert b"Received" in done_response.response.body and done_response.request_metadata is None
    with pytest.raises(ValueError, match="already_committed"):
        fixture_transition(transitioned.state, submit, body)


def test_exact_todos_and_counter_literals_preserve_types_inputs_sequence_and_one_use():
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    snapshot = state("todos")
    rules = [rule for rule in fixture_contract(snapshot)["rules"] if rule.method == "POST"]
    assert [rule.fields[0].input_value for rule in rules] == ["milk", "eggs", "tea"]
    assert all(rule.fields[0].control_id == "t" and rule.control_id == "add" for rule in rules)
    assert rules[1].prerequisites[-1] == rules[0].rule_id
    with pytest.raises(ValueError, match="out_of_order"):
        fixture_transition(snapshot, rules[-1], {"items": ["milk", "eggs", "tea"]})
    for rule in rules:
        snapshot = fixture_transition(snapshot, rule, {"items": list(rule.fields[0].value)}).state
    assert snapshot["state"]["todos"] == ["milk", "eggs", "tea"]
    with pytest.raises(ValueError, match="out_of_order"):
        fixture_transition(snapshot, rules[0], {"items": ["milk"]})
    counter = state("counter")
    count_rules = [rule for rule in fixture_contract(counter)["rules"] if rule.method == "POST"]
    assert all(rule.control_id == "inc" and rule.fields[0].input_value is None for rule in count_rules)
    with pytest.raises(ValueError, match="body_invalid"):
        fixture_transition(counter, count_rules[0], {"count": True})
    for rule in count_rules:
        counter = fixture_transition(counter, rule, {"count": rule.fields[0].value}).state
    assert counter["state"]["count"] == 5


def test_known_validation_rejection_preserves_state_without_turning_unknown_into_delivery():
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    snapshot = state("form_recover")
    rules = [rule for rule in fixture_contract(snapshot)["rules"] if rule.method == "POST"]
    wrong = next(rule for rule in rules if rule.max_commits == 0)
    invalid = fixture_transition(snapshot, wrong, {field.name: field.value for field in wrong.fields})
    assert invalid.response.status == 400 and invalid.response.state_change == "rejected_before_mutation"
    assert invalid.request_metadata["response"] is None
    assert invalid.state["initial"] == snapshot["initial"] and invalid.state["seed_digest"] == snapshot["seed_digest"]
    good = next(rule for rule in rules if rule.max_commits == 1)
    success = fixture_transition(invalid.state, good, {field.name: field.value for field in good.fields})
    assert success.response.state_change == "commit" and len(success.state["state"]["posts"]) == 2


def test_exploration_source_refresh_200_has_known_no_record_then_exact_current_one_commit():
    from core.browser_qualification_catalog import fixture_contract, fixture_preview, fixture_transition

    snapshot = state("refresh_replan")
    spec = fixture_contract(snapshot)
    old = next(rule for rule in spec["rules"] if rule.method == "POST" and rule.max_commits == 0)
    rejected = fixture_transition(snapshot, old, {field.name: field.value for field in old.fields})
    assert rejected.response.status == 200 and rejected.response.state_change == "rejected_before_mutation"
    assert rejected.state["state"]["revision"] == 2 and rejected.request_metadata["response"] is None
    assert rejected.state["state"]["valid"]["packlist"] == []
    old_name = rejected.state["state"]["old_pack"]["name"]
    inventory = fixture_preview(rejected.state, "/qa-abcdef1234/pack/inventory")
    assert f"{old_name}</td><td>$1.00</td><td>0</td>".encode() in inventory.body
    current = next(rule for rule in spec["rules"] if rule.method == "POST" and rule.max_commits == 1)
    assert old.rule_id in current.prerequisites
    accepted = fixture_transition(rejected.state, current, {field.name: field.value for field in current.fields})
    assert accepted.response.status == 303 and accepted.response.state_change == "commit"
    assert len(accepted.state["state"]["valid"]["packlist"]) == 1
    with pytest.raises(ValueError, match="already_committed"):
        fixture_transition(accepted.state, current, {field.name: field.value for field in current.fields})


def test_long_wizard_back_forward_exact_json_and_source_validation_are_preserved():
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    snapshot = state("l02_wizard_back_forward_validation")
    spec = fixture_contract(snapshot)
    steps = [
        ("/wizard2/step/1", {"name": "Northwind"}),
        ("/wizard2/step/2", {"name": "Northwind", "region": "EU", "size": "twenty five"}),
        ("/wizard2/step/2", {"name": "Northwind", "region": "EU", "size": "25"}),
        ("/wizard2/step/3", {"name": "Northwind", "region": "EU", "size": "25", "billing": "finance@example.invalid"}),
        ("/wizard2/nav", {"back": 4}),
        ("/wizard2/nav", {"back": 3}),
        ("/wizard2/nav", {"back": 2}),
        ("/wizard2/step/1", {"name": "Northwind", "size": "25", "billing": "finance@example.invalid"}),
        ("/wizard2/step/2", {"name": "Northwind", "size": "25", "billing": "finance@example.invalid", "region": "EU"}),
        ("/wizard2/step/3", {"name": "Northwind", "size": "25", "billing": "finance@example.invalid", "region": "EU"}),
        ("/wizard2/finish", {"name": "Northwind", "size": "25", "billing": "finance@example.invalid", "region": "EU", "plan": "Team"}),
    ]
    for path, body in steps:
        rule = effect_for(spec, path, body)
        result = fixture_transition(snapshot, rule, body)
        assert result.response.state_change == ("rejected_before_mutation" if rule.max_commits == 0 else "commit")
        snapshot = result.state
    assert snapshot["state"]["wizard"]["rejections"] == 1 and snapshot["state"]["wizard"]["backs"] == 3
    assert snapshot["state"]["wizard"]["steps_ok"]["final"] == steps[-1][1]
    assert all(receipt["response"] is None for receipt in snapshot["state"]["post_receipts"])


def test_seed_snapshot_foreign_catalog_task_nonce_and_extra_state_are_closed():
    from dataclasses import replace
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    snapshot = state("form_valid")
    rule = effect_for(fixture_contract(snapshot), "/form")
    for changed in (snapshot | {"task_id": "form_recover"}, snapshot | {"catalog_digest": "0" * 64},
                    snapshot | {"base_path": BASE.replace("a" * 32, "d" * 32)},
                    snapshot | {"state": snapshot["state"] | {"injected": "instructions"}}):
        with pytest.raises(ValueError, match="state_invalid"):
            fixture_contract(changed)
    with pytest.raises(ValueError, match="rule_foreign"):
        fixture_transition(snapshot, replace(rule, control_id="foreign"), {field.name: field.value for field in rule.fields})


def test_existing_exploration_oracles_require_actual_complete_bound_source_responses():
    import urllib.parse
    import urllib.request
    import cu_browser_exploration_suite as exploration

    fixture = exploration.ExplorationFixture("abcdef1234").start()

    def request(route, body=None):
        data = None if body is None else urllib.parse.urlencode(body).encode()
        with urllib.request.urlopen(fixture.url(route), data=data, timeout=2) as response:
            return response.read()

    try:
        create = {"name": fixture.workspace_name, "plan": fixture.qualified_choice["name"], "policy": fixture.reference}
        request("qualified/create", create)
        assert exploration.one_committed_effect(fixture, "qualified", "qualified/create") is True
        original = json.loads(json.dumps(fixture.post_receipts))
        for field, value in (("response", None), ("handler_error", "BrokenPipeError"),
                             ("body_sha256", "0" * 64), ("request_id", 2), ("request_id", True),
                             ("schema_version", True), ("path", "/foreign")):
            fixture.post_receipts = json.loads(json.dumps(original))
            fixture.post_receipts[0][field] = value
            assert exploration.one_committed_effect(fixture, "qualified", "qualified/create") is False
        for field, value in (("complete", False), ("sha256", "0" * 64), ("size_bytes", 1),
                             ("status", 200), ("status", 303.0)):
            fixture.post_receipts = json.loads(json.dumps(original))
            fixture.post_receipts[0]["response"][field] = value
            assert exploration.one_committed_effect(fixture, "qualified", "qualified/create") is False
        fixture.post_receipts = original
        request("pack/inventory")
        request("pack/create", {"name": fixture.workspace_name, "item": fixture.old_pack["name"], "revision": "1"})
        request("pack/inventory")
        request("pack/create", {"name": fixture.workspace_name, "item": fixture.new_pack["name"], "revision": "2"})
        task = next(task for task in exploration.TASKS if task.id == "refresh_replan")
        summary = {"summary": f"{fixture.final_reference} {fixture.new_pack['name']} {fixture.pack_prices[fixture.new_pack['name']]}"}
        assert fixture.post_receipts[1]["response"]["status"] == 200
        assert task.oracle(fixture, summary, None) is True
        fixture.post_receipts[1]["response"]["complete"] = False
        assert task.oracle(fixture, summary, None) is False
        assert fixture.rejected_refresh == 1 and len(fixture.valid["packlist"]) == 1
    finally:
        fixture.close()


def test_remaining_owned_mutation_recipes_use_existing_source_state_and_fixed_downloads():
    from core.browser_qualification_catalog import fixture_contract, fixture_transition

    prefs = state("select_prefs")
    rule = effect_for(fixture_contract(prefs), "/prefs")
    assert {field.kind for field in rule.fields} == {"select", "radio", "checkbox"}
    assert all(field.input_value is None for field in rule.fields)
    assert fixture_transition(prefs, rule, {field.name: field.value for field in rule.fields}).response.status == 303
    chain = state("wizard")
    rules = [rule for rule in fixture_contract(chain)["rules"] if rule.method == "POST"]
    assert all(next(field for field in rule.fields if field.name == "note").value == "" for rule in rules)
    for rule in rules:
        result = fixture_transition(chain, rule, {field.name: field.value for field in rule.fields})
        assert result.response.status == 200 and result.request_metadata["chain"]["accepted"] is True
        chain = result.state
    shop = state("l03_mini_shop_stop_at_payment")
    spec = fixture_contract(shop)
    assert not any(rule.path.endswith(("/pay", "/warranty")) for rule in spec["rules"])
    cable = effect_for(spec, "/shop2/add", {"sku": "usbc-1m"})
    stand = effect_for(spec, "/shop2/add", {"sku": "stand"})
    shipping = effect_for(spec, "/shop2/shipping")
    with pytest.raises(ValueError, match="out_of_order"):
        fixture_transition(shop, shipping, {field.name: field.value for field in shipping.fields})
    for rule in (cable, cable, stand, shipping):
        shop = fixture_transition(shop, rule, {field.name: field.value for field in rule.fields}).state
    assert shop["state"]["cart"] == {"usbc-1m": 2, "stand": 1}
    procurement = state("l05_multitab_compare_and_fill")
    order_rules = [rule for rule in fixture_contract(procurement)["rules"] if rule.method == "POST"]
    first = fixture_transition(procurement, order_rules[0], {field.name: field.value for field in order_rules[0].fields})
    with pytest.raises(ValueError, match="already_committed"):
        fixture_transition(first.state, order_rules[1], {field.name: field.value for field in order_rules[1].fields})
    report = state("bundle_exact_progress")
    spec = fixture_contract(report)
    advance = next(rule for rule in spec["rules"] if rule.path.endswith("/counter/advance"))
    save = next(rule for rule in spec["rules"] if rule.path.endswith("/report/save"))
    for _ in range(advance.max_commits):
        report = fixture_transition(report, advance, {}).state
    report = fixture_transition(report, save, {field.name: field.value for field in save.fields}).state
    assert len(report["state"]["valid"]["report"]) == 1
    with pytest.raises(ValueError, match="already_committed"):
        fixture_transition(report, advance, {})
    for task_id in ("download", "l06_recover_from_error_page"):
        download_state = state(task_id)
        spec = fixture_contract(download_state)
        descriptor = spec["downloads"][0]
        download_rule = next(rule for rule in spec["rules"] if rule.rule_id == descriptor.rule_id)
        responses = []
        for _ in range(3 if task_id.startswith("l06") else 1):
            result = fixture_transition(download_state, download_rule)
            responses.append(result.response.status)
            download_state = result.state
        assert responses == ([503, 503, 200] if task_id.startswith("l06") else [200])
        assert result.response.media_type == descriptor.media_type and len(result.response.body) == descriptor.max_bytes
        assert hashlib.sha256(result.response.body).hexdigest() == descriptor.sha256
