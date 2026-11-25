"""Offline validation of independently pinned installed-app acceptance evidence.

Never launches a task or fabricates receipts. Hashes are custody checks, not a
signature on backend responses; the external trusted capture index is required.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import plistlib
import subprocess
from datetime import datetime, timezone
from pathlib import Path

CATEGORIES = frozenset(
    {
        "textedit",
        "notes",
        "finder",
        "calendar",
        "calendar_delete",
        "reminders",
        "preview",
        "browser",
        "browser_form",
        "cross_app",
        "long",
        "interruption_resume",
    }
)
ORACLES = {
    "textedit": "textedit_exact_content_v1",
    "notes": "notes_exact_content_v1",
    "finder": "finder_exact_rename_v1",
    "calendar": "calendar_owned_event_v1",
    "calendar_delete": "calendar_owned_delete_v1",
    "reminders": "reminders_owned_item_v1",
    "preview": "preview_decoded_export_v1",
    "browser": "browser_nonce_read_v1",
    "browser_form": "browser_form_nonce_post_v1",
    "cross_app": "cross_app_exact_transfer_v1",
    "long": "long_artifact_manifest_v1",
    "interruption_resume": "interruption_post_resume_readback_v1",
}
CLOCK_SKEW_NS = 2_000_000_000
HEX = re.compile(r"[a-f0-9]{64}\Z")
UUID = re.compile(r"[a-f0-9]{32}\Z")
IDENTITY = ("order_id", "execution_id", "device_id", "venture_id")
ROLES = (
    "review",
    "claim",
    "terminal",
    "native",
    "runtime_log",
    "process_before",
    "process_after",
    "task_check",
    "focus",
    "input",
)


class InvalidEvidence(ValueError):
    pass


def require(value, code):
    if not value:
        raise InvalidEvidence(code)


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def finite(value):
    return type(value) in {int, float} and math.isfinite(value)


def read_file(path, *, limit=32_000_000):
    require(not path.is_symlink() and path.is_file(), "artifact_unavailable")
    require(path.stat().st_size <= limit, "artifact_too_large")
    return path.read_bytes()


def capture_build(bundle: Path, runtime_relative: str, *, execute=subprocess.run):
    """Read-only collection; no task launch, daemon startup, login or GUI."""
    bundle = bundle.resolve()
    relative = Path(runtime_relative)
    require(
        not relative.is_absolute() and ".." not in relative.parts,
        "runtime_path_invalid",
    )
    runtime = bundle / relative
    require(
        runtime.resolve().is_relative_to(bundle) and not runtime.is_symlink(),
        "runtime_path_invalid",
    )
    info = plistlib.loads(read_file(bundle / "Contents/Info.plist"))
    checked = execute(
        ["codesign", "--verify", "--deep", "--strict", str(bundle)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(checked.returncode == 0, "signed_build_unverified")
    described = execute(
        ["codesign", "-d", "--verbose=4", str(bundle)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    require(described.returncode == 0, "signing_metadata_unavailable")
    metadata = {}
    for line in (described.stdout + "\n" + described.stderr).splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            if key in {"CDHash", "TeamIdentifier", "Identifier"}:
                metadata[key] = value
    sha = hashlib.sha256()
    require(runtime.is_file(), "runtime_unavailable")
    with runtime.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    require(
        metadata.get("Identifier") == info.get("CFBundleIdentifier"),
        "signing_bundle_mismatch",
    )
    require(
        re.fullmatch(r"[A-Z0-9]{10}", metadata.get("TeamIdentifier", "")),
        "signing_team_missing",
    )
    require(
        re.fullmatch(r"[a-fA-F0-9]{40,64}", metadata.get("CDHash", "")),
        "signing_hash_missing",
    )
    return {
        "capture_mode": "installed_app",
        "bundle_id": info["CFBundleIdentifier"],
        "version": info.get("CFBundleShortVersionString"),
        "runtime_sha256": sha.hexdigest(),
        "cdhash": metadata["CDHash"],
        "team_identifier": metadata["TeamIdentifier"],
        "codesign_verify_exit_code": 0,
    }


def capture_process(runtime: Path, pid: int):
    """Read the actual macOS process executable; missing identity stays unknown."""
    import ctypes
    import sys

    require(
        sys.platform == "darwin" and type(pid) is int and pid > 0,
        "process_capture_unavailable",
    )
    library = ctypes.CDLL("/usr/lib/libproc.dylib")
    library.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    library.proc_pidpath.restype = ctypes.c_int
    buffer = ctypes.create_string_buffer(4096)
    require(library.proc_pidpath(pid, buffer, 4096) > 0, "process_capture_unavailable")
    actual = Path(buffer.value.decode()).resolve()
    require(
        actual == runtime.resolve() and not runtime.is_symlink(),
        "process_runtime_mismatch",
    )
    bundles = [parent for parent in actual.parents if parent.suffix == ".app"]
    require(bundles, "process_not_bundled_runtime")
    build = capture_build(bundles[0], str(actual.relative_to(bundles[0])))
    sha = hashlib.sha256()
    with actual.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    require(sha.hexdigest() == build["runtime_sha256"], "process_runtime_changed")
    import time

    return {
        "pid": pid,
        "executable_kind": "signed_bundled_runtime",
        "executable_sha256": sha.hexdigest(),
        "observed_at_ns": time.time_ns(),
    }


class Artifacts:
    def __init__(self, root, pins, tasks=None):
        self.root, self.pins = root.resolve(), pins
        self.tasks = tasks or {}
        self.envelopes = {}
        self.used = set()
        self.capture_clocks = {}

    def load(self, reference):
        require(isinstance(reference, dict), "artifact_reference_missing")
        name, expected = reference.get("path"), reference.get("sha256")
        require(
            isinstance(name, str)
            and isinstance(expected, str)
            and HEX.fullmatch(expected),
            "artifact_reference_invalid",
        )
        relative = Path(name)
        require(
            not relative.is_absolute()
            and ".." not in relative.parts
            and relative.parts,
            "artifact_path_invalid",
        )
        path = self.root / relative
        require(
            all(
                not p.is_symlink()
                for p in [path, *path.parents]
                if p != self.root.parent
            ),
            "artifact_symlink",
        )
        require(path.resolve().is_relative_to(self.root), "artifact_path_invalid")
        require(self.pins.get(name) == expected, "artifact_not_independently_pinned")
        raw = read_file(path)
        require(digest(raw) == expected, "artifact_hash_mismatch")
        self.used.add(name)
        return raw

    def json(self, reference):
        value = json.loads(self.load(reference))
        require(isinstance(value, dict), "artifact_object_required")
        return value

    def owned(self, reference, run, *, role):
        envelope = self.json(reference)
        require(
            envelope.get("capture_mode") == "installed_app"
            and envelope.get("role") == role,
            "artifact_capture_mode_invalid",
        )
        require(envelope.get("run_id") == run["run_id"], "artifact_cross_run")
        require(
            all(envelope.get(k) == run[k] for k in IDENTITY), "artifact_cross_execution"
        )
        require(
            type(envelope.get("captured_at_ns")) is int
            and envelope["captured_at_ns"] > 0,
            "artifact_capture_time_missing",
        )
        require(
            type(envelope.get("captured_monotonic_ns")) is int
            and envelope["captured_monotonic_ns"] > 0,
            "artifact_monotonic_time_missing",
        )
        self.envelopes[reference["path"]] = envelope
        self.capture_clocks[reference["path"]] = envelope["captured_monotonic_ns"] / 1e9
        require(isinstance(envelope.get("data"), dict), "artifact_data_missing")
        return envelope["data"]


def order_matches(value, run):
    require(
        value.get("id") == run["order_id"]
        and all(value.get(k) == run[k] for k in IDENTITY[1:]),
        "order_cross_execution",
    )


def review_digest(review):
    keys = (
        "id",
        "workspace_id",
        "venture_id",
        "actor_id",
        "device_id",
        "manifest",
        "task_snapshot",
        "expires_at",
    )
    require(all(k in review for k in keys), "review_snapshot_missing")
    return digest(canonical({k: review[k] for k in keys}))


def log_rows(raw, run):
    result = []
    for line in raw.decode("utf-8").splitlines():
        if "cu_step " not in line:
            continue
        row = json.loads(line.split("cu_step ", 1)[1])
        require(isinstance(row, dict), "runtime_log_malformed")
        # Other actual workers may share runtime.log. Never borrow their steps.
        if row.get("execution_id") != run["execution_id"]:
            continue
        require(
            all(row.get(k) == run[k] for k in IDENTITY), "runtime_log_cross_execution"
        )
        require(
            type(row.get("observed_at_ns")) is int
            and type(row.get("monotonic_ns")) is int,
            "runtime_log_clock_missing",
        )
        result.append(row)
    require(result, "runtime_log_owned_steps_missing")
    return result


def date_ns(value):
    require(isinstance(value, str), "execution_clock_invalid")
    parsed = datetime.fromisoformat(value)
    require(parsed.tzinfo is not None, "execution_clock_invalid")
    delta = parsed.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (
        delta.days * 86400 + delta.seconds
    ) * 1_000_000_000 + delta.microseconds * 1000


def applied_effect(row):
    # Batched log summaries retain only the first outcome's prefix. They cannot
    # independently prove which later batch operations actually applied.
    action, outcome = row.get("action"), row.get("outcome")
    if not isinstance(action, str) or ";" in action or not isinstance(outcome, str):
        return False
    op = action.split(" ", 1)[0]
    successful = {
        "open_app": {"launched"},
        "open_path": {"opened"},
        "navigate": {"navigated"},
        "click": {"click", "clicked"},
        "double_click": {"double_click"},
        "right_click": {"right_click"},
        "drag": {"dragged"},
        "type": {"typed", "set"},
        "key": {"pressed"},
        "menu": {"menu"},
        "scroll": {"scrolled"},
        "template": {"script ok"},
    }
    if op == "fs":
        parts = action.split(" ")
        if len(parts) < 2 or parts[1] not in {
            "mkdir",
            "move",
            "rename",
            "copy",
            "write_text",
        }:
            return False
        return outcome in {"applied", f"fs {parts[1]} ok"}
    # 'applied' is the explicit successful operation status from a recorder;
    # native prefixes are closed and never accept refusal/error/wait results.
    return op in successful and (
        outcome == "applied"
        or any(
            outcome in {prefix, prefix + " [content withheld]"}
            for prefix in successful[op]
        )
    )


def verified_work_steps(spec, task, rows):
    """Independent task-bound transitions, never planner/model progress counters."""
    units = spec.get("work_units")
    progress = task.get("progress")
    require(
        isinstance(units, list) and 30 <= len(units) <= 512, "long_work_units_missing"
    )
    require(
        isinstance(progress, list) and 30 <= len(progress) <= 512,
        "long_progress_missing",
    )
    expected = {}
    for unit in units:
        require(
            isinstance(unit, dict)
            and set(unit) == {"unit_id", "before_sha256", "after_sha256"},
            "long_work_unit_invalid",
        )
        identifier = unit["unit_id"]
        require(
            isinstance(identifier, str)
            and UUID.fullmatch(identifier)
            and identifier not in expected,
            "long_work_unit_invalid",
        )
        require(
            all(
                isinstance(unit[k], str) and HEX.fullmatch(unit[k])
                for k in ("before_sha256", "after_sha256")
            )
            and unit["before_sha256"] != unit["after_sha256"],
            "long_work_unit_unchanged",
        )
        expected[identifier] = unit
    actions = {row["step"]: row for row in rows}
    require(len(actions) == len(rows), "long_progress_duplicate_runtime_step")
    seen_units, seen_steps = set(), set()
    for proof in progress:
        require(
            isinstance(proof, dict)
            and set(proof)
            == {
                "unit_id",
                "step",
                "invocation_id",
                "before_sha256",
                "after_sha256",
                "observed_at_ns",
            },
            "long_progress_invalid",
        )
        require(isinstance(proof["unit_id"], str), "long_progress_unit_unbound")
        unit = expected.get(proof["unit_id"])
        require(
            unit is not None and proof["unit_id"] not in seen_units,
            "long_progress_unit_unbound",
        )
        step = proof["step"]
        require(
            type(step) is int and step not in seen_steps and step in actions,
            "long_progress_step_unbound",
        )
        row = actions[step]
        require(
            proof["invocation_id"] == row["invocation_id"],
            "long_progress_invocation_unbound",
        )
        require(
            applied_effect(row)
            and row["action"].split(" ", 1)[0]
            not in {"open_app", "open_path", "navigate", "scroll"},
            "long_progress_routing_only",
        )
        require(
            all(proof[k] == unit[k] for k in ("before_sha256", "after_sha256")),
            "long_progress_result_mismatch",
        )
        require(
            type(proof["observed_at_ns"]) is int
            and row["observed_at_ns"]
            <= proof["observed_at_ns"]
            <= task["observed_at_ns"],
            "long_progress_clock_invalid",
        )
        seen_units.add(proof["unit_id"])
        seen_steps.add(step)
    require(len(seen_steps) >= 30, "long_horizon_under_30_meaningful_steps")
    return seen_steps


def verify_run(run, files, build):
    require(
        isinstance(run, dict) and run.get("category") in CATEGORIES,
        "run_category_invalid",
    )
    require(
        isinstance(run.get("run_id"), str) and UUID.fullmatch(run["run_id"]),
        "run_id_invalid",
    )
    require(
        all(
            isinstance(run.get(k), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", run[k])
            for k in IDENTITY
        ),
        "run_identity_missing",
    )
    refs = run.get("artifacts")
    require(
        isinstance(refs, dict) and all(role in refs for role in ROLES),
        "run_artifacts_missing",
    )
    values = {
        role: files.owned(refs[role], run, role=role)
        for role in ROLES
        if role != "runtime_log"
    }
    review, claim, terminal = (values[k] for k in ("review", "claim", "terminal"))
    # Approved review precedes execution and therefore has no execution_id yet.
    require(
        review.get("id") == run["order_id"] and review.get("state") == "approved",
        "review_not_approved",
    )
    require(
        all(review.get(k) == run[k] for k in ("device_id", "venture_id")),
        "review_owner_mismatch",
    )
    rd = review_digest(review)
    require(review.get("review_digest") == rd, "review_digest_invalid")
    for order in (claim, terminal):
        order_matches(order, run)
        require(
            order.get("review_digest") == rd
            and order.get("manifest") == review["manifest"],
            "review_changed",
        )
    require(
        claim.get("state") == "claimed" and claim.get("claimed_at"), "claim_missing"
    )
    require(
        terminal.get("state") == "stopped" and terminal.get("closed_at"),
        "terminal_unsettled",
    )
    claimed_ns, closed_ns = date_ns(claim["claimed_at"]), date_ns(terminal["closed_at"])
    require(
        0 < claimed_ns <= closed_ns and claimed_ns <= date_ns(review["expires_at"]),
        "execution_clock_invalid",
    )
    native = values["native"].get("evidence")
    require(isinstance(native, dict), "native_evidence_missing")
    require(
        native.get("order_id") == run["order_id"]
        and native.get("execution_id") == run["execution_id"],
        "native_cross_execution",
    )
    require(
        native.get("driver") == "ai_loop_cua"
        and native.get("outcome") == "verified"
        and native.get("task_verified") is True
        and native.get("processes_stopped") is True,
        "native_completion_unverified",
    )
    native_hash = digest(canonical(native))
    stop = terminal.get("native_stop") or {}
    require(
        stop.get("execution_id") == run["execution_id"]
        and stop.get("evidence_digest") == native_hash
        and stop.get("processes_stopped") is True
        and stop.get("task_verified") is True,
        "native_stop_mismatch",
    )
    rows = log_rows(files.load(refs["runtime_log"]), run)
    processes = [values[k] for k in ("process_before", "process_after")]
    pid = processes[0].get("pid")
    require(
        type(pid) is int
        and pid > 0
        and all(
            p.get("pid") == pid
            and p.get("executable_kind") == "signed_bundled_runtime"
            and p.get("executable_sha256") == build["runtime_sha256"]
            for p in processes
        ),
        "installed_process_unproven",
    )
    instance = rows[0].get("worker_instance_id")
    require(
        isinstance(instance, str) and UUID.fullmatch(instance),
        "worker_instance_missing",
    )
    require(
        all(
            r.get("worker_pid") == pid and r.get("worker_instance_id") == instance
            for r in rows
        ),
        "worker_identity_changed",
    )
    epochs = [r["observed_at_ns"] for r in rows]
    clocks = [r["monotonic_ns"] / 1e9 for r in rows]
    require(
        all(finite(x) and x > 0 for x in clocks)
        and clocks == sorted(clocks)
        and epochs == sorted(epochs),
        "runtime_clock_invalid",
    )
    require(
        all(
            claimed_ns - CLOCK_SKEW_NS <= epoch <= closed_ns + CLOCK_SKEW_NS
            for epoch in epochs
        ),
        "runtime_epoch_outside_execution",
    )
    offsets = [row["observed_at_ns"] - row["monotonic_ns"] for row in rows]
    for role in ROLES:
        if role == "runtime_log":
            continue
        envelope = files.envelopes[refs[role]["path"]]
        offsets.append(envelope["captured_at_ns"] - envelope["captured_monotonic_ns"])
        observed = values[role].get("observed_at_ns")
        if observed is not None:
            require(
                type(observed) is int
                and observed > 0
                and observed <= envelope["captured_at_ns"] + CLOCK_SKEW_NS,
                "artifact_observation_after_capture",
            )
    require(
        max(offsets) - min(offsets) <= CLOCK_SKEW_NS, "capture_epoch_monotonic_mismatch"
    )
    require(
        files.envelopes[refs["review"]["path"]]["captured_at_ns"]
        <= claimed_ns + CLOCK_SKEW_NS,
        "review_capture_after_claim",
    )
    require(
        files.envelopes[refs["terminal"]["path"]]["captured_at_ns"]
        >= closed_ns - CLOCK_SKEW_NS,
        "terminal_capture_before_close",
    )
    require(
        files.capture_clocks[refs["process_before"]["path"]] <= min(clocks),
        "process_capture_not_before_execution",
    )
    require(
        all(
            files.capture_clocks[refs[role]["path"]] >= max(clocks)
            for role in ("native", "process_after", "task_check")
        ),
        "completion_capture_before_execution_end",
    )
    require(
        processes[0].get("observed_at_ns", 0) <= min(epochs)
        and processes[1].get("observed_at_ns", 0) >= max(epochs),
        "process_capture_incomplete",
    )
    invocations = terminal.get("invocations")
    require(isinstance(invocations, dict) and invocations, "invocations_missing")
    action_rows = [
        r
        for r in rows
        if r.get("action") and isinstance(r.get("ms"), dict) and "act" in r["ms"]
    ]
    require(action_rows, "action_timing_missing")
    steps = set()
    for row in action_rows:
        invocation = row.get("invocation_id")
        require(
            isinstance(invocation, str)
            and invocation in invocations
            and invocations[invocation].get("state") == "completed",
            "invocation_uncorrelated",
        )
        require(type(row.get("step")) is int and row["step"] > 0, "actual_step_missing")
        if applied_effect(row):
            steps.add(row["step"])
        ms = row["ms"]
        require(
            all(
                type(ms.get(k)) is int and ms[k] >= 0
                for k in ("observe", "model", "act")
            ),
            "step_timing_incomplete",
        )
        # Strict aggregate cannot conceal slow routine steps behind averages.
        if run["category"] not in {"long", "interruption_resume"}:
            require(
                sum(ms[k] for k in ("observe", "model", "act")) <= 3000,
                "routine_step_over_3_seconds",
            )
    require(
        type(native.get("steps_taken")) is int and native["steps_taken"] >= len(steps),
        "step_count_inconsistent",
    )
    if run["category"] == "long":
        require(
            len(steps) >= 30 and native["steps_taken"] >= 30,
            "long_horizon_under_30_actual_steps",
        )
    task = values["task_check"]
    instruction = review["manifest"].get("instruction")
    require(
        isinstance(instruction, str)
        and task.get("task_sha256") == digest(instruction.encode()),
        "task_check_unbound",
    )
    spec = files.tasks.get(run["run_id"])
    require(isinstance(spec, dict), "trusted_task_spec_missing")
    keys = ("category", "task_sha256", "oracle", "oracle_source_sha256", "scope_id")
    require(
        spec.get("category") == run["category"]
        and spec.get("task_sha256") == task.get("task_sha256")
        and spec.get("oracle") == ORACLES[run["category"]]
        and isinstance(spec.get("oracle_source_sha256"), str)
        and HEX.fullmatch(spec["oracle_source_sha256"])
        and isinstance(spec.get("scope_id"), str)
        and UUID.fullmatch(spec["scope_id"])
        and all(task.get(key) == spec.get(key) for key in keys),
        "task_category_oracle_unbound",
    )
    require(
        task.get("verification_status") == "passed"
        and task.get("scope") == "uuid_owned"
        and task.get("oracle") == ORACLES[run["category"]],
        "external_task_unverified",
    )
    require(
        type(task.get("observed_at_ns")) is int
        and task["observed_at_ns"] >= max(epochs),
        "task_check_stale",
    )
    if run["category"] == "long":
        steps = verified_work_steps(spec, task, action_rows)
    inp = values["input"]
    before, after = inp.get("before") or {}, inp.get("after") or {}
    decoy_pid = before.get("pid")
    require(
        type(decoy_pid) is int and decoy_pid > 0 and decoy_pid != pid,
        "decoy_identity_missing",
    )
    require(
        isinstance(before.get("instance_id"), str)
        and before["instance_id"] == after.get("instance_id")
        and decoy_pid == after.get("pid"),
        "decoy_instance_changed",
    )
    require(
        type(before.get("samples")) is int
        and type(after.get("samples")) is int
        and 0 < before["samples"] < after["samples"],
        "decoy_counter_unknown",
    )
    require(
        type(before.get("received_input_events")) is int
        and type(after.get("received_input_events")) is int
        and before["received_input_events"] == after["received_input_events"] == 0,
        "decoy_received_input",
    )
    require(
        isinstance(before.get("body_sha256"), str)
        and HEX.fullmatch(before["body_sha256"])
        and before["body_sha256"] == after.get("body_sha256"),
        "decoy_body_changed",
    )
    require(
        finite(before.get("updated_at"))
        and finite(after.get("updated_at"))
        and before["updated_at"] * 1e9 <= min(epochs)
        and after["updated_at"] * 1e9 >= max(epochs),
        "decoy_capture_incomplete",
    )
    focus = values["focus"]
    samples = focus.get("focus")
    require(
        focus.get("overflow") is False
        and focus.get("sampler_joined") is True
        and isinstance(samples, list)
        and len(samples) >= 2,
        "focus_capture_incomplete",
    )
    times = []
    for row in samples:
        require(
            row.get("cg_pid") == decoy_pid
            and row.get("active_app_pid") == decoy_pid
            and row.get("appkit_valid") is True
            and row.get("decoy_is_active") is True,
            "background_focus_unverified",
        )
        require(finite(row.get("at_monotonic")), "focus_clock_unknown")
        times.append(row["at_monotonic"])
    require(
        times[0] <= min(clocks)
        and times[-1]
        >= max(
            [
                max(clocks),
                *(
                    files.capture_clocks[refs[role]["path"]]
                    for role in ("native", "task_check", "process_after")
                ),
            ]
        )
        and all(0 <= b - a <= 2 for a, b in zip(times, times[1:])),
        "focus_interval_incomplete",
    )
    if run["category"] == "interruption_resume":
        pause = terminal.get("human_pause") or {}
        require(
            pause.get("execution_id") == run["execution_id"]
            and pause.get("device_id") == run["device_id"]
            and pause.get("review_digest") == rd
            and pause.get("state") == "pause_loop_exited"
            and all(
                pause.get(k)
                for k in (
                    "pause_token",
                    "request_id",
                    "requested_at",
                    "accepted_at",
                    "cleared_at",
                )
            ),
            "resume_receipt_incomplete",
        )
        dates = [
            datetime.fromisoformat(pause[k])
            for k in ("requested_at", "accepted_at", "cleared_at")
        ]
        require(
            all(d.tzinfo is not None for d in dates)
            and dates == sorted(dates)
            and claimed_ns
            <= date_ns(pause["requested_at"])
            <= date_ns(pause["cleared_at"])
            <= closed_ns,
            "resume_clock_invalid",
        )
        # Capture actual API snapshots, not manually supplied acknowledgement flags.
        resume = files.owned(refs.get("resume"), run, role="resume")
        require(
            resume.get("signal", {}).get("pause_token") == pause["pause_token"]
            and resume.get("signal", {}).get("request_id") == pause["request_id"]
            and resume.get("signal", {}).get("execution_id") == run["execution_id"]
            and resume.get("signal", {}).get("device_id") == run["device_id"]
            and resume.get("signal", {}).get("review_digest") == rd,
            "resume_cross_episode",
        )
        pause_rows = [r for r in rows if r.get("phase") == "needs_you"]
        require(
            pause_rows
            and any(
                r.get("outcome") in {"resumed_continue", "resumed_cleared"}
                for r in pause_rows
            ),
            "owned_worker_resume_missing",
        )
        resumed_rows = [
            r
            for r in pause_rows
            if r.get("outcome") in {"resumed_continue", "resumed_cleared"}
        ]
        exited = max(r["monotonic_ns"] for r in resumed_rows)
        require(
            max(r["observed_at_ns"] for r in resumed_rows)
            >= date_ns(pause["cleared_at"]) - CLOCK_SKEW_NS,
            "resume_epoch_before_clear",
        )
        resume_envelope = files.envelopes[refs["resume"]["path"]]
        require(
            abs(
                (
                    resume_envelope["captured_at_ns"]
                    - resume_envelope["captured_monotonic_ns"]
                )
                - offsets[0]
            )
            <= CLOCK_SKEW_NS
            and resume_envelope["captured_at_ns"]
            >= date_ns(pause["cleared_at"]) - CLOCK_SKEW_NS,
            "resume_capture_clock_invalid",
        )
        require(
            any(r["monotonic_ns"] > exited and applied_effect(r) for r in action_rows),
            "resume_no_subsequent_effect",
        )
    require(steps, "actual_effect_missing")
    return {
        "run_id": run["run_id"],
        "category": run["category"],
        "passed": True,
        "actual_steps": len(steps),
    }


def verify(manifest_path, trusted_index_path):
    """No output contains task text, private paths, raw stderr or model replies."""
    report = {
        "passed": False,
        "runs": [],
        "errors": [],
        "missing_categories": sorted(CATEGORIES),
    }
    try:
        require(
            not trusted_index_path.resolve().is_relative_to(
                manifest_path.parent.resolve()
            ),
            "trusted_index_must_be_external",
        )
        raw = read_file(manifest_path)
        manifest = json.loads(raw)
        trusted = json.loads(read_file(trusted_index_path))
        require(
            manifest.get("schema") == 1 and trusted.get("schema") == 1,
            "manifest_schema_invalid",
        )
        require(
            trusted.get("capture_mode") == "installed_app"
            and trusted.get("manifest_sha256") == digest(raw),
            "manifest_not_trusted",
        )
        require(
            isinstance(trusted.get("artifacts"), dict), "trusted_artifact_index_missing"
        )
        require(isinstance(trusted.get("tasks"), dict), "trusted_task_catalog_missing")
        files = Artifacts(manifest_path.parent, trusted["artifacts"], trusted["tasks"])
        build = files.json(manifest.get("build"))
        required_build = (
            "bundle_id",
            "version",
            "runtime_sha256",
            "cdhash",
            "team_identifier",
        )
        require(
            all(
                build.get(k) == trusted.get("build", {}).get(k) for k in required_build
            ),
            "build_provenance_mismatch",
        )
        require(
            build.get("capture_mode") == "installed_app"
            and type(build.get("codesign_verify_exit_code")) is int
            and build.get("codesign_verify_exit_code") == 0,
            "signed_build_unverified",
        )
        require(
            isinstance(build.get("runtime_sha256"), str)
            and HEX.fullmatch(build["runtime_sha256"])
            and re.fullmatch(r"[a-fA-F0-9]{40,64}", build.get("cdhash", ""))
            and re.fullmatch(r"[A-Z0-9]{10}", build.get("team_identifier", "")),
            "build_provenance_missing",
        )
        require(isinstance(build.get("version"), str), "build_version_missing")
        version = build["version"].split(".")
        require(
            len(version) == 3
            and all(p.isdigit() for p in version)
            and tuple(map(int, version)) >= (0, 1, 78),
            "desktop_version_under_required",
        )
        require(isinstance(manifest.get("runs"), list), "runs_missing")
        seen_runs, seen_orders, seen_execs = set(), set(), set()
        counts = {category: 0 for category in CATEGORIES}
        task_categories = {}
        for run in manifest["runs"]:
            try:
                require(isinstance(run, dict), "run_object_required")
                require(
                    all(
                        run.get(k) not in seen
                        for k, seen in (
                            ("run_id", seen_runs),
                            ("order_id", seen_orders),
                            ("execution_id", seen_execs),
                        )
                    ),
                    "duplicate_execution_or_run",
                )
                seen_runs.add(run.get("run_id"))
                seen_orders.add(run.get("order_id"))
                seen_execs.add(run.get("execution_id"))
                result = verify_run(run, files, build)
                spec = files.tasks[run["run_id"]]
                task_sha = spec["task_sha256"]
                require(
                    task_categories.get(task_sha, result["category"])
                    == result["category"],
                    "same_task_relabelled_across_categories",
                )
                task_categories[task_sha] = result["category"]
                counts[result["category"]] += 1
                report["runs"].append(result)
            except (
                InvalidEvidence,
                KeyError,
                TypeError,
                ValueError,
                OSError,
                AttributeError,
                OverflowError,
            ) as error:
                code = (
                    str(error)
                    if isinstance(error, InvalidEvidence)
                    else "run_evidence_unavailable"
                )
                report["runs"].append({"passed": False, "error": code})
        report["missing_categories"] = sorted(
            category for category, n in counts.items() if n < 2
        )
        report["passed"] = not report["missing_categories"] and all(
            r["passed"] for r in report["runs"]
        )
    except (
        InvalidEvidence,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        AttributeError,
        OverflowError,
    ) as error:
        report["errors"].append(
            str(error) if isinstance(error, InvalidEvidence) else "evidence_unavailable"
        )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path, nargs="?")
    parser.add_argument("--trusted-index", type=Path)
    parser.add_argument("--capture-build", type=Path)
    parser.add_argument("--runtime-relative")
    parser.add_argument("--capture-process", type=int)
    parser.add_argument("--runtime", type=Path)
    args = parser.parse_args()
    if args.capture_build or args.capture_process is not None:
        try:
            if args.capture_build:
                require(args.runtime_relative is not None, "runtime_path_required")
                captured = capture_build(args.capture_build, args.runtime_relative)
            else:
                require(args.runtime is not None, "runtime_path_required")
                captured = capture_process(args.runtime, args.capture_process)
            print(json.dumps(captured))
            return 0
        except (
            InvalidEvidence,
            OSError,
            ValueError,
            subprocess.TimeoutExpired,
        ) as error:
            print(
                json.dumps(
                    {
                        "passed": False,
                        "error": str(error)
                        if isinstance(error, InvalidEvidence)
                        else "capture_unavailable",
                    }
                )
            )
            return 1
    if args.manifest is None or args.trusted_index is None:
        parser.error("manifest and --trusted-index are required for verification")
    result = verify(args.manifest, args.trusted_index)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
