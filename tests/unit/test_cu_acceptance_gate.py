"""Negative evidence contracts; no desktop input or paid inference."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
from cu_acceptance import desktop_lease, evaluate, expected_selection, percentile, source_digest
import cu_ai_suite as suite
import cu_tasks


def row(task="a", repetition=1):
    from cu_acceptance import reviewed_browser_profile
    return {"trial": task, "task": task, "repetition": repetition, "run_id": f"run-{repetition}",
            "source_digest": "a" * 64, "model": reviewed_browser_profile()["roles"]["actor"]["model"], "qa_transport": "owned_qa",
            "category": "synthetic", "passed": True, "cleanup_verified": True,
            "focus_mode": "frontmost", "source_unchanged": True,
            "decoy_untouched": True, "background_verified": True, "method_verified": True,
            "foreground_escalations": 0, "verification_status": "passed", "oracle": True,
            "independent_verification": True, "browser_process_gone": True,
            "forbidden_requests": 0, "step_ms": [100, 200], "seconds": 1.0}


@pytest.mark.parametrize("family", ["desktop", "browser"])
def test_full_repeated_qa_evidence_passes_but_is_never_installed_app_or_revenue(family):
    report = evaluate([row(repetition=1), row(repetition=2)], ["a"], suite=family, digest="a" * 64)
    assert report["accepted"] and report["strict_passed"] == 2
    assert not report["installed_app_verified"] and not report["revenue_verified"]


def test_browser_expected_selection_requires_explicit_live_and_covers_combined_catalog():
    from cu_browser_suite import TASKS, LIVE
    assert expected_selection("browser") == [t.id for t in TASKS]
    assert expected_selection("browser", live=True) == [t.id for t in TASKS] + [t.id for t in LIVE]
    live_id = LIVE[0].id
    assert expected_selection("browser", live_id, live=True) == [live_id]
    for suite_name, only, live in [("browser", live_id, False), ("browser", "unknown", True),
                                   ("desktop", "", True)]:
        with pytest.raises(ValueError):
            expected_selection(suite_name, only, live=live)


@pytest.mark.parametrize("rows", [[], [row()], [row(), row()], [row(), row("b", 2)]])
def test_missing_duplicate_or_unselected_trials_do_not_pass(rows):
    assert not evaluate(rows, ["a"], suite="desktop")["accepted"]


@pytest.mark.parametrize("field,value", [("source_digest", None), ("source_digest", "b" * 64),
    ("model", None), ("qa_transport", None), ("passed", None), ("passed", 1),
    ("cleanup_verified", None), ("method_verified", False), ("verification_status", "unknown"),
    ("decoy_untouched", False), ("background_verified", False), ("foreground_escalations", 1),
    ("focus_mode", "input"), ("focus_mode", None), ("source_unchanged", False), ("source_unchanged", None)])
def test_unverified_or_mixed_desktop_evidence_fails(field, value):
    rows = [row(), row(repetition=2)]
    rows[1][field] = value
    assert not evaluate(rows, ["a"], suite="desktop", digest="a" * 64)["accepted"]


@pytest.mark.parametrize("field,value", [("oracle", None), ("forbidden_requests", None),
    ("forbidden_requests", 1), ("cleanup_verified", False), ("passed", False),
    ("independent_verification", False), ("independent_verification", None),
    ("browser_process_gone", False), ("browser_process_gone", None)])
def test_browser_effect_oracle_and_cleanup_are_required(field, value):
    r = row()
    r[field] = value
    assert not evaluate([r], ["a"], suite="browser", repetitions=1)["accepted"]


def test_code_fingerprint_includes_uncommitted_source(tmp_path):
    path = tmp_path / "packages/desktop_runtime/action.py"
    path.parent.mkdir(parents=True)
    path.write_text("first")
    before = source_digest(tmp_path)
    path.write_text("second")
    assert before != source_digest(tmp_path)


@pytest.mark.parametrize("relative", ["packages/core/common.py", "packages/core/pyproject.toml",
    "packages/desktop_runtime/pyproject.toml", "packages/coding_agents/pyproject.toml",
    "services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
    "services/qa_runner/requirements.lock", "scripts/desktop/cu_browser_round1.py",
    "scripts/desktop/cu_tasks/round1_fixture.py"])
@pytest.mark.parametrize("scope", ["all", "browser"])
def test_source_fingerprint_includes_common_runtime_and_dependency_inputs(tmp_path, relative, scope):
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("first")
    before = source_digest(tmp_path, scope=scope)
    path.write_text("second")
    assert before != source_digest(tmp_path, scope=scope)


def test_browser_fingerprint_includes_round1_task_text(tmp_path):
    path = tmp_path / "docs/supymem/beta-2026-10-04/browser-tests/short_tasks.yaml"
    path.parent.mkdir(parents=True)
    path.write_text("tasks: []")
    before = source_digest(tmp_path, scope="browser")
    path.write_text("tasks: [x]")
    assert before != source_digest(tmp_path, scope="browser")


def test_row_fails_when_source_changes_after_run_start(tmp_path, monkeypatch, capsys):
    import cu_acceptance
    monkeypatch.setattr(cu_acceptance, "source_digest", lambda: "b" * 64)
    monkeypatch.setattr(suite, "SOURCE_DIGEST", "a" * 64)
    monkeypatch.setattr(suite, "RESULT_FILE", tmp_path / "results.jsonl")
    result = suite.write_record({"trial": "synthetic", "passed": True})
    assert result["source_digest"] == "a" * 64
    assert not result["passed"] and not result["source_unchanged"] and result["source_changed_during_run"]
    assert len((tmp_path / "results.jsonl").read_text().splitlines()) == 1
    assert "source_changed_during_run" in capsys.readouterr().out


@pytest.mark.parametrize("corruption,expected_exit", [(None, 0), ("source", 1), ("missing", 1), ("model", 1)])
def test_repeated_wrapper_checks_coverage_and_same_source_even_if_child_exits_zero(tmp_path, monkeypatch, corruption, expected_exit, capsys):
    import json
    from types import SimpleNamespace
    import cu_acceptance
    monkeypatch.setattr(cu_acceptance, "source_digest", lambda: "a" * 64)
    monkeypatch.setattr(suite, "TRIALS", {"synthetic": {"category": "synthetic"}})
    output = tmp_path / "combined.jsonl"
    monkeypatch.setattr(sys, "argv", ["cu_ai_suite.py", "--only", "synthetic", "--runs", "2", "--out", str(output)])
    def child(args):
        repetition = int(args[args.index("--repetition") + 1])
        r = row("synthetic", repetition)
        if repetition == 2 and corruption == "source":
            r["source_digest"] = "b" * 64
        if repetition == 2 and corruption == "model":
            r["model"] = "other-model"
        Path(args[args.index("--out") + 1]).write_text("" if repetition == 2 and corruption == "missing" else json.dumps(r) + "\n")
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(suite.subprocess, "run", child)
    assert suite.main() == expected_exit
    assert "ACCEPTANCE" in capsys.readouterr().out


def test_desktop_screen_lease_rejects_overlap_and_releases(tmp_path):
    path = tmp_path / "screen.lock"
    with desktop_lease(path):
        with pytest.raises(ValueError, match="another_desktop_trial_is_running"):
            with desktop_lease(path):
                pytest.fail("overlap")
    with desktop_lease(path):
        pass


def test_desktop_screen_lease_rejects_symlink(tmp_path):
    link = tmp_path / "screen.lock"
    link.symlink_to(tmp_path / "other")
    with pytest.raises(OSError):
        with desktop_lease(link):
            pytest.fail("followed symlink")


def test_percentiles_ignore_invalid_and_do_not_round_down_p95():
    assert percentile([float("nan"), True, -1, *range(1, 21)], .95) == 19
    assert percentile([], .5) is None


def test_menu_and_keyboard_claims_cannot_pass_on_file_only_recipe():
    direct = [{"action": "template textedit_new_save", "ms": {"act": 1}}]
    assert not suite.method_verified("mb_menu_save_as", direct)
    assert not suite.method_verified("kb_shortcuts_only", direct)
    assert not suite.method_verified("di_open_dialog", direct)
    assert suite.method_verified("te_new_save", direct)
    assert not suite.method_verified("mb_menu_save_as", [{"action": "menu [path withheld]", "ms": {"act": 1}}])
    assert not suite.method_verified("kb_shortcuts_only", [{"action": "hotkey Cmd+S", "ms": {"act": 1}}])
    assert not suite.method_verified("mb_menu_save_as", [{"action": "menu [path withheld]; hotkey Cmd+S", "ms": {"act": 1}}])


@pytest.mark.parametrize("rows", [[], [{"action": "fs copy", "result": "copy ok"}],
                                  [{"action": "template", "template_ids": ["finder_copy"]}],
                                  [{"action": "menu [path withheld]", "result": "ok"}]])
def test_finder_duplicate_requires_successful_exact_command_causal_proof(rows):
    assert not suite.method_verified("fi_duplicate", rows)
    assert suite.method_verified("fi_copy", rows)


def test_catalog_cleanup_unknown_is_sticky_and_all_cleanups_attempted(tmp_path, monkeypatch):
    attempts = []
    def setup(ctx):
        ctx.cleanups.extend([lambda: attempts.append("first"), lambda: False,
                             lambda: (_ for _ in ()).throw(RuntimeError("private data"))])
    task = cu_tasks.Task("owned", "test", 1, "task", "oracle", "low", lambda _: (True, "ok"), setup=setup)
    monkeypatch.setattr(cu_tasks, "load", lambda: {task.id: task})
    specs = {}
    cu_tasks.install(specs, lambda: tmp_path)
    specs["owned"]["setup"]()
    assert specs["owned"]["verify"]()[0] is True
    assert specs["owned"]["cleanup"]() is False and attempts == ["first"]
    assert specs["owned"]["cleanup"]() is False


def test_fixture_seed_and_oracle_use_same_known_variables_preserving_literal_braces(tmp_path):
    ctx = cu_tasks.Ctx(tmp_path, uid="abc12345")
    path = ctx.seed("src-{uid}.txt", "Payload {uid}\n{\\rtf1} {unknown}")
    assert path.name == "src-abc12345.txt"
    assert path.read_text() == "Payload abc12345\n{\\rtf1} {unknown}"


def test_cleanup_exception_is_failed_evidence_without_leaking_or_raising():
    def cleanup():
        raise RuntimeError("private content")
    assert suite.cleanup_spec({"cleanup": cleanup}) is False


@pytest.mark.parametrize("field", ["trial", "repetition", "run_id", "model", "source_digest", "qa_transport", "step_ms"])
def test_malformed_metadata_cannot_crash_or_pass_gate(field):
    bad = row()
    bad[field] = {"not": "a scalar"}
    assert not evaluate([bad], ["a"], suite="desktop", repetitions=2)["accepted"]
