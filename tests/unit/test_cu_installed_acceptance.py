"""Synthetic trust/receipt fixtures validate logic only, never live acceptance."""

import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts/desktop/cu_installed_acceptance.py"
)
SPEC = importlib.util.spec_from_file_location("installed_acceptance", SCRIPT)
qa = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(qa)


class Fixture:
    def __init__(self, tmp):
        self.root = tmp / "untrusted_bundle"
        self.root.mkdir()
        self.manifest = {"schema": 1, "runs": []}
        self.trust = {
            "schema": 1,
            "capture_mode": "installed_app",
            "artifacts": {},
            "tasks": {},
        }
        build = {
            "capture_mode": "installed_app",
            "bundle_id": "com.companyos.app",
            "version": "0.1.90",
            "runtime_sha256": "a" * 64,
            "cdhash": "b" * 40,
            "team_identifier": "123456ABCD",
            "codesign_verify_exit_code": 0,
        }
        self.trust["build"] = build.copy()
        self.manifest["build"] = self.put("build.json", build)
        self.path = self.root / "manifest.json"
        self.anchor = tmp / "independently_reviewed_pins.json"

    def put(self, name, data, raw=False):
        blob = data.encode() if raw else qa.canonical(data)
        (self.root / name).write_bytes(blob)
        sha = qa.digest(blob)
        self.trust["artifacts"][name] = sha
        return {"path": name, "sha256": sha}

    def role(self, run, role, data):
        value = {
            "capture_mode": "installed_app",
            "run_id": run["run_id"],
            "role": role,
            "captured_at_ns": self.epoch
            + {
                "review": -1,
                "claim": 0,
                "process_before": -1,
                "process_after": 3,
                "task_check": 3,
                "native": 3,
                "input": 3,
                "focus": 3,
                "resume": 2,
                "terminal": 60,
            }.get(role, 3)
            * 1_000_000_000,
            "captured_monotonic_ns": 1_000_000_000_000
            + {
                "review": -1,
                "claim": 0,
                "process_before": -1,
                "process_after": 3,
                "task_check": 3,
                "native": 3,
                "input": 3,
                "focus": 3,
                "resume": 2,
                "terminal": 60,
            }.get(role, 3)
            * 1_000_000_000,
            "data": data,
            **{k: run[k] for k in qa.IDENTITY},
        }
        return self.put(f"{run['run_id']}-{role}.json", value)

    def run(self, category, count=1):
        number = len(self.manifest["runs"]) + 1
        run = {
            "run_id": f"{number:032x}",
            "category": category,
            "order_id": f"dwo_{number}",
            "execution_id": f"dex_{number}",
            "device_id": "dev_1",
            "venture_id": "ven_1",
            "artifacts": {},
        }
        from datetime import datetime

        self.epoch = (
            int(datetime.fromisoformat("2026-10-03T11:00:00+00:00").timestamp())
            * 1_000_000_000
        )
        review = {
            "id": run["order_id"],
            "workspace_id": "ws_1",
            "venture_id": "ven_1",
            "actor_id": "person_1",
            "device_id": "dev_1",
            "manifest": {
                "instruction": f"{category} synthetic owned QA task {run['run_id']}"
            },
            "task_snapshot": {},
            "expires_at": "2026-10-03T12:00:00Z",
            "state": "approved",
        }
        self.trust["tasks"][run["run_id"]] = {
            "category": category,
            "scope_id": run["run_id"],
            "oracle": qa.ORACLES[category],
            "oracle_source_sha256": "f" * 64,
            "task_sha256": qa.digest(review["manifest"]["instruction"].encode()),
        }
        review["review_digest"] = qa.review_digest(review)
        claim = {
            **review,
            "state": "claimed",
            "execution_id": run["execution_id"],
            "claimed_at": "2026-10-03T11:00:00Z",
        }
        native = {
            "order_id": run["order_id"],
            "execution_id": run["execution_id"],
            "driver": "ai_loop_cua",
            "outcome": "verified",
            "task_verified": True,
            "processes_stopped": True,
            "steps_taken": count,
        }
        terminal = {
            **claim,
            "state": "stopped",
            "closed_at": "2026-10-03T11:01:00Z",
            "native_stop": {
                "execution_id": run["execution_id"],
                "evidence_digest": qa.digest(qa.canonical(native)),
                "processes_stopped": True,
                "task_verified": True,
            },
            "invocations": {},
        }
        rows = []
        for step in range(1, count + 1):
            inv = f"ai_{step:032x}"
            terminal["invocations"][inv] = {"state": "completed"}
            rows.append(
                {
                    **{k: run[k] for k in qa.IDENTITY},
                    "worker_pid": 42,
                    "worker_instance_id": "c" * 32,
                    "step": step,
                    "invocation_id": inv,
                    "observed_at_ns": self.epoch + 2_000_000_000 + step * 10_000_000,
                    "monotonic_ns": 1_002_000_000_000 + step * 10_000_000,
                    "action": "click",
                    "outcome": "applied",
                    "ms": {"observe": 100, "model": 100, "act": 100},
                }
            )
        if category == "interruption_resume":
            terminal["human_pause"] = {
                "execution_id": run["execution_id"],
                "device_id": "dev_1",
                "review_digest": review["review_digest"],
                "state": "pause_loop_exited",
                "pause_token": "d" * 32,
                "request_id": "nyr_1",
                "requested_at": "2026-10-03T11:00:00+00:00",
                "accepted_at": "2026-10-03T11:00:01+00:00",
                "cleared_at": "2026-10-03T11:00:02+00:00",
            }
            resumed = {
                **rows[0],
                "phase": "needs_you",
                "outcome": "resumed_continue",
                "action": None,
                "monotonic_ns": 1_002_000_000_000,
                "observed_at_ns": self.epoch + 2_000_000_000,
            }
            rows.insert(0, resumed)
            run["artifacts"]["resume"] = self.role(
                run,
                "resume",
                {
                    "signal": {
                        "pause_token": "d" * 32,
                        "request_id": "nyr_1",
                        "execution_id": run["execution_id"],
                        "device_id": "dev_1",
                        "review_digest": review["review_digest"],
                    }
                },
            )
        values = {
            "review": review,
            "claim": claim,
            "terminal": terminal,
            "native": {"evidence": native},
            "process_before": {
                "pid": 42,
                "executable_kind": "signed_bundled_runtime",
                "executable_sha256": "a" * 64,
                "observed_at_ns": self.epoch - 1_000_000_000,
            },
            "process_after": {
                "pid": 42,
                "executable_kind": "signed_bundled_runtime",
                "executable_sha256": "a" * 64,
                "observed_at_ns": self.epoch + 3_000_000_000,
            },
            "task_check": {
                "verification_status": "passed",
                "scope": "uuid_owned",
                **self.trust["tasks"][run["run_id"]],
                "task_sha256": qa.digest(review["manifest"]["instruction"].encode()),
                "observed_at_ns": self.epoch + 3_000_000_000,
            },
            "input": {
                "before": {
                    "instance_id": "decoy1",
                    "pid": 99,
                    "received_input_events": 0,
                    "body_sha256": "e" * 64,
                    "samples": 1,
                    "updated_at": self.epoch / 1e9 - 1,
                },
                "after": {
                    "instance_id": "decoy1",
                    "pid": 99,
                    "received_input_events": 0,
                    "body_sha256": "e" * 64,
                    "samples": 20,
                    "updated_at": self.epoch / 1e9 + 3,
                },
            },
            "focus": {
                "overflow": False,
                "sampler_joined": True,
                "focus": [
                    {
                        "cg_pid": 99,
                        "active_app_pid": 99,
                        "appkit_valid": True,
                        "decoy_is_active": True,
                        "at_monotonic": 1001.9,
                    },
                    {
                        "cg_pid": 99,
                        "active_app_pid": 99,
                        "appkit_valid": True,
                        "decoy_is_active": True,
                        "at_monotonic": 1003,
                    },
                ],
            },
        }
        if category == "long":
            units, progress = [], []
            for row in rows:
                unit = {
                    "unit_id": f"{row['step']:032x}",
                    "before_sha256": qa.digest(
                        f"before:{run['run_id']}:{row['step']}".encode()
                    ),
                    "after_sha256": qa.digest(
                        f"required-result:{run['run_id']}:{row['step']}".encode()
                    ),
                }
                units.append(unit)
                progress.append(
                    {
                        **unit,
                        "step": row["step"],
                        "invocation_id": row["invocation_id"],
                        "observed_at_ns": self.epoch + 3_000_000_000,
                    }
                )
            self.trust["tasks"][run["run_id"]]["work_units"] = units
            values["task_check"]["progress"] = progress
        for role, data in values.items():
            run["artifacts"][role] = self.role(run, role, data)
        run["artifacts"]["runtime_log"] = self.put(
            f"{run['run_id']}-runtime.log",
            "\n".join("INFO cu_step " + json.dumps(r) for r in rows),
            raw=True,
        )
        self.manifest["runs"].append(run)
        return run

    def change(self, run, role, change):
        ref = run["artifacts"][role]
        body = json.loads((self.root / ref["path"]).read_text())
        change(body)
        run["artifacts"][role] = self.put(ref["path"], body)

    def verify(self):
        blob = qa.canonical(self.manifest)
        self.path.write_bytes(blob)
        self.trust["manifest_sha256"] = qa.digest(blob)
        self.anchor.write_bytes(qa.canonical(self.trust))
        return qa.verify(self.path, self.anchor)


def test_full_two_each_matrix_structurally_valid_under_mocked_trust(tmp_path):
    f = Fixture(tmp_path)
    for cat in qa.CATEGORIES:
        f.run(cat, 30 if cat == "long" else 1)
        f.run(cat, 30 if cat == "long" else 1)
    result = f.verify()
    assert result["passed"] and len(result["runs"]) == 24


def test_one_pass_not_release_acceptance(tmp_path):
    f = Fixture(tmp_path)
    f.run("textedit")
    assert not f.verify()["passed"]
    assert "textedit" in f.verify()["missing_categories"]


@pytest.mark.parametrize(
    "role,change,code",
    [
        (
            "claim",
            lambda v: v["data"].update(execution_id="dex_other"),
            "order_cross_execution",
        ),
        ("terminal", lambda v: v["data"].update(state="claimed"), "terminal_unsettled"),
        (
            "native",
            lambda v: v["data"]["evidence"].update(processes_stopped=False),
            "native_completion_unverified",
        ),
        (
            "task_check",
            lambda v: v["data"].update(verification_status="unknown"),
            "external_task_unverified",
        ),
        (
            "task_check",
            lambda v: v["data"].update(observed_at_ns=1),
            "task_check_stale",
        ),
        (
            "input",
            lambda v: v["data"]["after"].update(received_input_events=1),
            "decoy_received_input",
        ),
        (
            "input",
            lambda v: v["data"]["after"].update(samples=1),
            "decoy_counter_unknown",
        ),
        (
            "focus",
            lambda v: v["data"].update(overflow=True),
            "focus_capture_incomplete",
        ),
        (
            "focus",
            lambda v: v["data"]["focus"][1].update(cg_pid=42),
            "background_focus_unverified",
        ),
        (
            "process_after",
            lambda v: v["data"].update(executable_sha256="f" * 64),
            "installed_process_unproven",
        ),
        ("review", lambda v: v.update(run_id="f" * 32), "artifact_cross_run"),
        (
            "review",
            lambda v: v.update(capture_mode="local_source_trial"),
            "artifact_capture_mode_invalid",
        ),
    ],
)
def test_semantic_forgery_even_with_matching_hashes_fails(tmp_path, role, change, code):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(run, role, change)
    assert f.verify()["runs"][0]["error"] == code


def test_file_tamper_fails_independent_hash_pin(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.verify()
    (f.root / run["artifacts"]["review"]["path"]).write_text("forged private body")
    result = qa.verify(f.path, f.anchor)
    assert result["runs"][0]["error"] == "artifact_hash_mismatch"
    assert "private" not in json.dumps(result)


def test_manifest_self_pins_are_not_trust(tmp_path):
    f = Fixture(tmp_path)
    f.run("textedit")
    f.verify()
    inside = f.root / "pins.json"
    inside.write_bytes(f.anchor.read_bytes())
    assert qa.verify(f.path, inside)["errors"] == ["trusted_index_must_be_external"]
    f.manifest["runs"] = []
    f.path.write_bytes(qa.canonical(f.manifest))
    assert qa.verify(f.path, f.anchor)["errors"] == ["manifest_not_trusted"]


def test_missing_and_cross_run_receipts_fail(tmp_path):
    f = Fixture(tmp_path)
    first = f.run("textedit")
    second = f.run("textedit")
    first["artifacts"]["review"] = second["artifacts"]["review"]
    assert f.verify()["runs"][0]["error"] == "artifact_cross_run"
    del second["artifacts"]["focus"]
    assert f.verify()["runs"][1]["error"] == "run_artifacts_missing"


def test_duplicate_execution_cannot_count_twice(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.manifest["runs"].append(run)
    result = f.verify()
    assert (
        not result["passed"]
        and result["runs"][1]["error"] == "duplicate_execution_or_run"
    )


def test_long_must_have_thirty_actual_correlated_steps(tmp_path):
    f = Fixture(tmp_path)
    f.run("long", 29)
    assert f.verify()["runs"][0]["error"] == "long_horizon_under_30_actual_steps"


def test_runtime_steps_cannot_borrow_other_worker_or_invocations(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    ref = run["artifacts"]["runtime_log"]
    raw = (
        (f.root / ref["path"])
        .read_text()
        .replace(
            '"invocation_id": "ai_00000000000000000000000000000001"',
            '"invocation_id": "ai_missing"',
        )
    )
    run["artifacts"]["runtime_log"] = f.put(ref["path"], raw, raw=True)
    assert f.verify()["runs"][0]["error"] == "invocation_uncorrelated"


def test_resumed_ack_requires_same_episode_and_following_real_action(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("interruption_resume")
    assert f.verify()["runs"][0]["passed"]
    f.change(run, "resume", lambda v: v["data"]["signal"].update(pause_token="f" * 32))
    assert f.verify()["runs"][0]["error"] == "resume_cross_episode"


def test_symlink_and_escape_paths_rejected(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    run["artifacts"]["review"]["path"] = "../private.json"
    assert f.verify()["runs"][0]["error"] == "artifact_path_invalid"


def test_capture_build_reads_real_bytes_and_codesign_result_not_flags(tmp_path):
    import plistlib
    from types import SimpleNamespace

    bundle = tmp_path / "app"
    (bundle / "Contents/MacOS").mkdir(parents=True)
    (bundle / "Contents/Info.plist").write_bytes(
        plistlib.dumps(
            {
                "CFBundleIdentifier": "com.companyos.app",
                "CFBundleShortVersionString": "0.1.90",
            }
        )
    )
    runtime = bundle / "Contents/MacOS/runtime"
    runtime.write_bytes(b"actual fixture executable bytes")
    calls = []

    def execute(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(
            returncode=0,
            stdout="",
            stderr="Identifier=com.companyos.app\nCDHash="
            + "b" * 40
            + "\nTeamIdentifier=123456ABCD\n",
        )

    result = qa.capture_build(bundle, "Contents/MacOS/runtime", execute=execute)
    assert result["runtime_sha256"] == qa.digest(runtime.read_bytes())
    assert len(calls) == 2 and all(call[0] == "codesign" for call in calls)

    def failure(*a, **k):
        return SimpleNamespace(returncode=1, stdout="", stderr="private failure")

    with pytest.raises(qa.InvalidEvidence, match="signed_build_unverified"):
        qa.capture_build(bundle, "Contents/MacOS/runtime", execute=failure)


def test_noop_padding_does_not_satisfy_long_actual_steps(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("long", 30)
    ref = run["artifacts"]["runtime_log"]
    raw = (
        (f.root / ref["path"])
        .read_text()
        .replace('"action": "click"', '"action": "wait"')
    )
    run["artifacts"]["runtime_log"] = f.put(ref["path"], raw, raw=True)
    assert f.verify()["runs"][0]["error"] == "long_horizon_under_30_actual_steps"


def test_slow_routine_step_cannot_hide_in_average(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    ref = run["artifacts"]["runtime_log"]
    raw = (f.root / ref["path"]).read_text().replace('"model": 100', '"model": 3100')
    run["artifacts"]["runtime_log"] = f.put(ref["path"], raw, raw=True)
    assert f.verify()["runs"][0]["error"] == "routine_step_over_3_seconds"


def test_focus_must_cover_native_cleanup_and_independent_readback(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(
        run,
        "native",
        lambda v: v.update(
            captured_monotonic_ns=1_004_000_000_000,
            captured_at_ns=f.epoch + 4_000_000_000,
        ),
    )
    assert f.verify()["runs"][0]["error"] == "focus_interval_incomplete"


def test_acknowledgement_without_actual_post_resume_action_cannot_pass(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("interruption_resume")
    ref = run["artifacts"]["runtime_log"]
    raw = (f.root / ref["path"]).read_text()
    raw = raw.replace('"monotonic_ns": 1002000000000', '"monotonic_ns": 1002010000000')
    run["artifacts"]["runtime_log"] = f.put(ref["path"], raw, raw=True)
    assert f.verify()["runs"][0]["error"] == "resume_no_subsequent_effect"


def test_category_relabel_cannot_qualify_a_different_task_family(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    run["category"] = "notes"
    assert not f.verify()["runs"][0]["passed"]


def test_arbitrary_v1_oracle_does_not_prove_category_readback(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(
        run, "task_check", lambda v: v["data"].update(oracle="arbitrary_success_v1")
    )
    assert not f.verify()["runs"][0]["passed"]


@pytest.mark.parametrize(
    "action,outcome",
    [
        ("wait", "waited"),
        ("click", "refused"),
        ("click", "error"),
        ("click", "stopped"),
    ],
)
def test_wait_or_refusal_after_resume_is_not_a_subsequent_effect(
    tmp_path, action, outcome
):
    f = Fixture(tmp_path)
    run = f.run("interruption_resume")
    ref = run["artifacts"]["runtime_log"]
    rows = [
        json.loads(line.split("cu_step ", 1)[1])
        for line in (f.root / ref["path"]).read_text().splitlines()
    ]
    rows[-1].update(action=action, outcome=outcome)
    run["artifacts"]["runtime_log"] = f.put(
        ref["path"],
        "\n".join("INFO cu_step " + json.dumps(row) for row in rows),
        raw=True,
    )
    assert not f.verify()["runs"][0]["passed"]


def test_execution_dates_must_match_real_capture_epoch_not_only_monotonic_order(
    tmp_path,
):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(
        run, "claim", lambda v: v["data"].update(claimed_at="2030-01-01T00:00:00Z")
    )
    f.change(
        run, "terminal", lambda v: v["data"].update(closed_at="2030-01-01T00:01:00Z")
    )
    assert not f.verify()["runs"][0]["passed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("scope_id", "e" * 32),
        ("oracle_source_sha256", "e" * 64),
        ("task_sha256", "e" * 64),
    ],
)
def test_independent_oracle_scope_and_source_and_task_digest_must_match(
    tmp_path, field, value
):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(run, "task_check", lambda v: v["data"].update({field: value}))
    assert not f.verify()["runs"][0]["passed"]


def test_missing_external_task_catalog_is_not_evidence_of_category_coverage(tmp_path):
    f = Fixture(tmp_path)
    f.run("textedit")
    del f.trust["tasks"]
    assert f.verify()["errors"] == ["trusted_task_catalog_missing"]


def test_generic_task_cannot_count_for_multiple_categories_even_if_relabelled_in_trust(
    tmp_path,
):
    f = Fixture(tmp_path)
    for category in ["textedit", "notes"]:
        run = f.run(category)
        f.change(
            run,
            "review",
            lambda v: v["data"]["manifest"].update(instruction="same generic task"),
        )
        reference = run["artifacts"]["review"]
        review = json.loads((f.root / reference["path"]).read_text())["data"]
        rd = qa.review_digest(review)
        f.change(run, "review", lambda v: v["data"].update(review_digest=rd))
        for role in ["claim", "terminal"]:
            f.change(
                run,
                role,
                lambda v: v["data"].update(
                    review_digest=rd, manifest=review["manifest"]
                ),
            )
        sha = qa.digest(b"same generic task")
        f.trust["tasks"][run["run_id"]]["task_sha256"] = sha
        f.change(run, "task_check", lambda v: v["data"].update(task_sha256=sha))
    result = f.verify()
    assert result["runs"][0]["passed"]
    assert result["runs"][1]["error"] == "same_task_relabelled_across_categories"


@pytest.mark.parametrize(
    "action,outcome",
    [
        ("click", "refused"),
        ("click", "click error"),
        ("fs read_text [path withheld]", "applied"),
        ("switch_app", "selected [content withheld]"),
        ("click; type [3 chars]", "click [content withheld]"),
    ],
)
def test_rejected_reads_selections_and_ambiguous_batches_do_not_pad_long_effects(
    tmp_path, action, outcome
):
    f = Fixture(tmp_path)
    run = f.run("long", 30)
    ref = run["artifacts"]["runtime_log"]
    rows = [
        json.loads(line.split("cu_step ", 1)[1])
        for line in (f.root / ref["path"]).read_text().splitlines()
    ]
    for row in rows:
        row.update(action=action, outcome=outcome)
    run["artifacts"]["runtime_log"] = f.put(
        ref["path"],
        "\n".join("INFO cu_step " + json.dumps(row) for row in rows),
        raw=True,
    )
    assert f.verify()["runs"][0]["error"] == "long_horizon_under_30_actual_steps"


def test_capture_epoch_cannot_be_detached_from_matching_monotonic_execution(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(run, "native", lambda v: v.update(captured_at_ns=200_000_000_000))
    assert f.verify()["runs"][0]["error"] == "capture_epoch_monotonic_mismatch"


def test_resume_dates_must_belong_to_the_claimed_execution_interval(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("interruption_resume")

    def change(value):
        value["data"]["human_pause"].update(
            requested_at="1970-01-01T00:00:00Z",
            accepted_at="1970-01-01T00:00:01Z",
            cleared_at="1970-01-01T00:00:02Z",
        )

    f.change(run, "terminal", change)
    assert f.verify()["runs"][0]["error"] == "resume_clock_invalid"


@pytest.mark.parametrize("role", ["native", "process_after", "task_check"])
def test_completion_capture_must_follow_actual_logged_execution(tmp_path, role):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    # Keep wall/monotonic offset consistent; cross-role chronology must still fail.
    f.change(
        run,
        role,
        lambda v: v.update(
            captured_at_ns=f.epoch, captured_monotonic_ns=1_000_000_000_000
        ),
    )
    assert not f.verify()["runs"][0]["passed"]


def test_preexecution_process_capture_must_really_precede_execution(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    f.change(
        run,
        "process_before",
        lambda v: v.update(
            captured_at_ns=f.epoch + 3_000_000_000,
            captured_monotonic_ns=1_003_000_000_000,
        ),
    )
    assert f.verify()["runs"][0]["error"] == "process_capture_not_before_execution"


def test_routine_task_cannot_qualify_with_no_applied_effect(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("textedit")
    ref = run["artifacts"]["runtime_log"]
    raw = (
        (f.root / ref["path"])
        .read_text()
        .replace('"action": "click"', '"action": "wait"')
    )
    run["artifacts"]["runtime_log"] = f.put(ref["path"], raw, raw=True)
    assert f.verify()["runs"][0]["error"] == "actual_effect_missing"


def test_repeated_route_opens_do_not_prove_thirty_meaningful_work_units(tmp_path):
    f = Fixture(tmp_path)
    run = f.run("long", 30)
    ref = run["artifacts"]["runtime_log"]
    rows = [
        json.loads(line.split("cu_step ", 1)[1])
        for line in (f.root / ref["path"]).read_text().splitlines()
    ]
    for row in rows:
        row.update(action="open_app TextEdit", outcome="launched [content withheld]")
    run["artifacts"]["runtime_log"] = f.put(
        ref["path"],
        "\n".join("INFO cu_step " + json.dumps(row) for row in rows),
        raw=True,
    )
    assert f.verify()["runs"][0]["passed"] is False


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda p: p.pop("progress"), "long_progress_missing"),
        (
            lambda p: p["progress"][0].update(unit_id="f" * 32),
            "long_progress_unit_unbound",
        ),
        (
            lambda p: p["progress"][0].update(after_sha256="0" * 64),
            "long_progress_result_mismatch",
        ),
        (
            lambda p: p["progress"][0].update(invocation_id="foreign"),
            "long_progress_invocation_unbound",
        ),
        (lambda p: p["progress"][1].update(step=1), "long_progress_step_unbound"),
        (
            lambda p: p["progress"][0].update(observed_at_ns=0),
            "long_progress_clock_invalid",
        ),
    ],
)
def test_long_requires_independent_task_bound_progress(tmp_path, change, code):
    f = Fixture(tmp_path)
    run = f.run("long", 30)
    f.change(run, "task_check", lambda envelope: change(envelope["data"]))
    assert f.verify()["runs"][0]["error"] == code


def test_distinct_owned_output_transitions_pass_without_model_progress_strings(
    tmp_path,
):
    """Actual local bytes feed independent digests; still no installed execution claim."""
    f = Fixture(tmp_path)
    run = f.run("long", 30)
    task_ref = run["artifacts"]["task_check"]
    task_envelope = json.loads((f.root / task_ref["path"]).read_text())
    proof = task_envelope["data"]["progress"]
    units = f.trust["tasks"][run["run_id"]]["work_units"]
    ref = run["artifacts"]["runtime_log"]
    rows = [
        json.loads(line.split("cu_step ", 1)[1])
        for line in (f.root / ref["path"]).read_text().splitlines()
    ]
    for index, (unit, transition, row) in enumerate(zip(units, proof, rows)):
        output = tmp_path / f"owned-output-{index}.txt"
        wanted = f"{run['run_id']}:{index}:required-value".encode()
        before = qa.digest(b"owned-target-absent")
        assert not output.exists()
        output.write_bytes(wanted)
        after = qa.digest(output.read_bytes())
        assert after == qa.digest(wanted) and before != after
        unit.update(before_sha256=before, after_sha256=qa.digest(wanted))
        transition.update(before_sha256=before, after_sha256=after)
        row.update(action="fs write_text [path withheld]", outcome="fs write_text ok")
    run["artifacts"]["task_check"] = f.put(task_ref["path"], task_envelope)
    run["artifacts"]["runtime_log"] = f.put(
        ref["path"],
        "\n".join("INFO cu_step " + json.dumps(row) for row in rows),
        raw=True,
    )
    result = f.verify()["runs"][0]
    assert result["passed"] is True and result["actual_steps"] == 30


def test_browser_and_cross_app_keep_their_distinct_required_oracles(tmp_path):
    f = Fixture(tmp_path)
    for category in ("browser", "browser_form", "cross_app"):
        f.run(category)
    assert all(result["passed"] for result in f.verify()["runs"])
