"""Static-page check/fix recipe inside the owned device coding loop."""
from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
import tempfile
import time

from desktop_runtime import coding_loop, loop_diagnose
from desktop_runtime.owned_coding import coding_failure_details


def public_files(workspace):
    return {name: body for name, body in coding_loop._scan(workspace).items()
        if name != ".supymem-venture" and not name.startswith(("review/", ".reference/"))}


def source(workspace):
    from core.artifact_snapshot import freeze_source
    from core.artifact_policy import write_file
    from core.logging import _local_secret_values

    secrets = tuple(_local_secret_values() or ())
    with tempfile.TemporaryDirectory(prefix="landing-source-") as raw:
        root = Path(raw)
        for name, body in public_files(workspace).items():
            write_file(root, name, body, exact_secrets=secrets)
        return freeze_source(root)


def checked_archive(venture_id, order_id, execution_id, *, blob=None):
    from dataclasses import replace
    from core.artifact_policy import DEFAULT_LIMITS, read_file, replace_file
    from desktop_runtime.paths import data_root

    if not all(isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{2,79}", value)
               for value in (venture_id, order_id, execution_id)):
        raise ValueError("landing_archive_scope_invalid")
    name = f"ventures/{venture_id}/coding_sources/{order_id}/{execution_id}-landing.zip"
    root = data_root(create=blob is not None)
    limits = replace(DEFAULT_LIMITS, max_file_bytes=DEFAULT_LIMITS.max_archive_bytes)
    if blob is None:
        return read_file(root, name, limits=limits)
    replace_file(root, name, blob, limits=limits)


def _review_source(coding, workspace, prompt, deadline):
    from desktop_runtime.owned_coding import workspace_files

    name = "review/capability-review.json"
    before = workspace_files(workspace, include_git=True)
    if name in before:
        raise ValueError("pagebuilder_grounding_review_invalid")
    try:
        result = coding.run("PAGE_SOURCE_REVIEW\nReview the saved landing page as a separate factual check. "
            f"Write only your JSON verdict to {name}. Do not change the page, plan or any other files.\n\n" + prompt, deadline)
    finally:
        clean = coding_loop._quiescent(workspace, coding.tracker)
        guarded = coding_loop._guard(workspace, before)
        after = workspace_files(workspace, include_git=True)
        raw = after.pop(name, b"")
        if not clean or guarded or after != before:
            coding_loop._restore(workspace, before, git=True)
            raise ValueError("pagebuilder_grounding_review_invalid")
        (workspace / name).unlink(missing_ok=True)
    if result.get("outcome") != "pass" or not 0 < len(raw) <= 180000:
        raise ValueError("pagebuilder_grounding_review_invalid")
    return raw.decode("utf-8")


def run(worker, manifest, gateway, coding, deadline):
    worker.gateway = gateway
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.pipeline import read_tool_plan
    from app.pagebuilder.tool import check_output, failure_explanations, review_requires_reconciliation, reviewed_files, validation_messages
    from app.pagebuilder.local import page_check_message, page_check_names
    snapshot = worker.loop_saved["task_snapshot"]
    inputs = PageInput.model_validate(snapshot["page_input"])
    plan = read_tool_plan(snapshot["page_plan"])
    workspace = coding._workspace()
    descriptor = manifest["coding_loop_verification"]
    if worker.cancelled.is_set() or not coding.authorize():
        raise ValueError("coding_cancelled_or_expired")
    coding_loop._git(workspace, "init", "--quiet", "--initial-branch=main")
    coding_loop.OwnedCoding._env(workspace)
    baseline = coding_loop._checkpoint(workspace, "Supymem page baseline")
    for name, body in reviewed_files(inputs, plan).items():
        from core.artifact_policy import replace_file, write_file
        if (workspace / name).exists():
            replace_file(workspace, name, body)
        else:
            write_file(workspace, name, body)
    coding.loop_mode, coding.max_turns = True, descriptor.get("max_turns")
    coding.line_callback = lambda line: coding_loop._event(worker, value, workspace) if (
        value := loop_diagnose.stream_text(line, coding.provider, workspace)) else None
    started = time.monotonic()
    deadline = min(deadline, started + descriptor["max_wall_seconds"])
    brief = ("Build and save the requested static page in this workspace. Network access is disabled. "
        "Do not install packages, fetch remote assets, access paths outside this workspace, start servers or run "
        "Supymem's checks yourself. Supymem runs its independent checks after you finish and supplies any failures "
        "for up to three fix rounds. You may use installed tools for offline file checks. Keep private check scripts and notes "
        "under review/. Stop as soon as index.html, public assets, required companion pages and page-plan.txt are written.\n\n"
        + snapshot.get("page_brief", manifest["instruction"]))
    prompt, outcome, file_manifest_sha256, commit = brief, "stopped", None, baseline
    result = {}
    soft_stop = getattr(worker, "stop_after_iteration", getattr(worker, "soft_stop_requested", None))
    calls = 0
    iterations = min(4, descriptor["max_iterations"])
    check_seconds, stop_reason, reconcile_review = 0.0, None, False
    for index in range(iterations):
        if (worker.cancelled.is_set() or time.monotonic() >= deadline or not coding.authorize()
                or index and soft_stop and soft_stop.is_set()):
            break
        worker.progress("running_coding", loop_index=index, loop_phase="code")
        coding_loop._event(worker, "Building your page" if index == 0 else "Fixing your page checks", workspace)
        protected = coding_loop.workspace_files(workspace, include_git=True)
        remaining = deadline - time.monotonic() - max(check_seconds, min(30, descriptor["max_wall_seconds"] / 10))
        call_deadline = time.monotonic() + min(descriptor["per_call_seconds"], remaining / (iterations - index))
        if time.monotonic() >= call_deadline:
            outcome, stop_reason = "timeout", "wall_time_limit"
            break
        worker.progress("running_coding", build_deadline_at=time.time() + call_deadline - time.monotonic())
        calls += 1
        try:
            result = coding.run(prompt, call_deadline)
        except Exception as error:
            result = coding_failure_details(error, getattr(coding, "stage", "code"), provider=coding.provider)
            try:
                coding.tracker.stop_all(timeout=.5)
            except Exception:
                pass
            result["outcome"] = {"coding_auth_required": "auth_required", "coding_sign_in_required": "auth_required",
                "coding_authority_unconfirmed": "stopped", "coding_secret_detected": "secret_detected",
                "coding_instruction_private": "secret_detected", "coding_sandbox_denied": "sandbox_denied",
                "coding_output_bounds": "sandbox_denied"}.get(str(error),
                    "stopped" if worker.cancelled.is_set() else "timeout" if time.monotonic() >= call_deadline else
                    "stopped" if str(error) == "coding_cancelled_or_expired" else "cli_error")
            if getattr(coding, "failure_message", None):
                result["failure_message"] = loop_diagnose.scrub(coding.failure_message, workspace, 1000)
        worker.progress("running_coding", loop_index=index, loop_phase="check")
        coding_loop._event(worker, "Checking your saved page files", workspace)
        check_started = time.monotonic()
        try:
            clean = coding_loop._quiescent(workspace, coding.tracker)
        except Exception as error:
            clean = False
            if "failure_code" not in result:
                result.update(coding_failure_details(error, "cleanup"))
        guarded = coding_loop._guard(workspace, protected, preserve_source=True)
        checked = check_output(public_files(workspace), inputs, plan) if clean and not guarded else {
            "passed": False, "failures": ["coding_sandbox_denied" if guarded else "coding_processes_unconfirmed"]}
        check_error = None
        if result.get("outcome") != "stopped" and checked["passed"] and snapshot.get("pagebuilder_job_id"):
            from app.pagebuilder import runtime
            from app.pagebuilder.pipeline import tool_page_plan
            from app.pagebuilder.tool import review_tool_source
            worker.progress("running_coding", loop_index=index, loop_phase="check")
            coding_loop._event(worker, "Checking your page", workspace)
            job_id = snapshot["pagebuilder_job_id"]
            try:
                saved = json.loads(runtime.read(job_id).payload_json)
                from app.pagebuilder.local import CODING_TOOL_REVIEW
                quote = saved.get("_runtime", {}).get("quote", {})
                options = {"review_source": lambda prompt: _review_source(coding, workspace, prompt, call_deadline)} if (
                    quote.get("provider") == CODING_TOOL_REVIEW) else {}
                review = review_tool_source(job_id, inputs, tool_page_plan(inputs, plan, public_files(workspace)),
                    saved["tool_build"]["grounding"], deadline=deadline, **options)
                for suggestion in review["reviews"][-1]["review"].get("suggestions", []):
                    coding_loop._event(worker, "Copy suggestion: " + loop_diagnose.scrub(suggestion["reason"], workspace, 220)
                        + " Open Website if you want to change this copy.", workspace)
            except ValueError as exc:
                checked = {**checked, "passed": False, "failures": validation_messages(exc)}
            except Exception as exc:
                from app.pagebuilder.local import failure_reason
                from app.research.metered import _failure_diagnostics
                code = str(exc).partition(":")[0]
                if code == "pagebuilder_local_spend_limit":
                    code = "pagebuilder_spend_limit"
                if code not in {"pagebuilder_call_budget_exhausted", "pagebuilder_ceiling_exhausted",
                        "pagebuilder_outcome_uncertain", "pagebuilder_settlement_pending", "pagebuilder_operation_in_progress",
                        "pagebuilder_provider_rejected", "pagebuilder_provider_rate_limited", "pagebuilder_spend_limit",
                        "pagebuilder_cancelled", "pagebuilder_scope_mismatch", "pagebuilder_inputs_changed",
                        "pagebuilder_resources_changed", "pagebuilder_stage_invalid", "pagebuilder_invalid_output",
                        "pagebuilder_output_truncated", "pagebuilder_output_incomplete", "pagebuilder_check_timeout",
                        "pagebuilder_byok_unavailable", "pagebuilder_resources_unavailable", "pagebuilder_authority_revoked",
                        "pagebuilder_not_found", "pagebuilder_prompt_too_large", "pagebuilder_provider_offline", "pagebuilder_lease_lost",
                        "pagebuilder_waitlist_disabled", "pagebuilder_waitlist_scope_invalid", "pagebuilder_waitlist_consent_upgrade_required",
                        "pagebuilder_credits_unavailable", "pagebuilder_credit_reservation_unavailable", "pagebuilder_credentials_unavailable"}:
                    code = "pagebuilder_check_failed"
                stop_reason = code
                reconcile_review = review_requires_reconciliation(job_id, code)
                result = {**result, **coding_failure_details(exc, "check"), "failure_code": code,
                    "outcome": "checks_unavailable", "failure_message": failure_reason(code),
                    "check_failure": _failure_diagnostics(exc, time.monotonic() - check_started)}
                coding_loop._event(worker, result["failure_message"], workspace,
                    exception_frames=result.get("exception_frames"))
                checked = {**checked, "passed": False, "failures": [code]}
                check_error = exc
        check_seconds = max(check_seconds, time.monotonic() - check_started)
        marker_verified = result.get("workspace_marker_verified", (workspace / ".supymem-venture").read_text().strip() == inputs.venture_id)
        verified = (clean and not guarded and result.get("outcome") in {"pass", "timeout", "cli_error"} and marker_verified is True
            and checked["passed"] and time.monotonic() <= deadline and not worker.cancelled.is_set() and coding.authorize())
        outcome = ("pass" if verified else "sandbox_denied" if guarded else "stopped_unclean" if not clean
            else "tests_failed" if result.get("outcome") == "cli_error" and result.get("exit_code") == 0 and not checked["passed"]
            else result.get("outcome") if result.get("outcome") in {
                "auth_required", "quota_paused", "cli_error", "timeout", "stopped", "secret_detected", "sandbox_denied", "checks_unavailable"}
            else "tests_failed")
        commit = coding_loop._checkpoint(workspace, "Supymem page checks") if clean and not guarded else baseline
        if verified:
            from core.artifact_snapshot import manifest_digest
            file_manifest_sha256 = manifest_digest(source(workspace)[0])
        evidence = {"failures": [], "next_action": "stop_pass" if verified else "stop" if check_error or outcome == "stopped" else "fix"}
        if not verified:
            names = page_check_names(checked["failures"]) or [result.get("failure_code") or outcome]
            logging.getLogger(__name__).warning("pagebuilder_iteration_failed", extra={
                "job_id": snapshot.get("pagebuilder_job_id"), "iteration": index + 1,
                "outcome": outcome, "failed_checks": names,
                "tool_step": result.get("failure_stage") or ("check" if outcome == "tests_failed" else "code"),
                "tool_failure": result.get("failure_code"), "tool_exit_code": result.get("exit_code")})
            # shortcut: keep the receipt bounded; move large diagnoses to a separate receipt if needed.
            evidence["failed_checks"] = []
            for name in names:
                if len(json.dumps([*evidence["failed_checks"], name]).encode()) > 1000:
                    break
                evidence["failed_checks"].append(name)
        for failure in checked["failures"]:
            if result.get("outcome") != "checks_unavailable":
                coding_loop._event(worker, page_check_message([failure]), workspace)
            candidate = {**evidence, "failures": [*evidence["failures"], failure]}
            if len(json.dumps(candidate).encode()) <= 1500:
                evidence = candidate
        for key in ("failure_stage", "failure_code", "exception_type", "exception_location", "exception_frames", "exception_code", "check_failure"):
            if key in result and len(json.dumps({**evidence, key: result[key]}).encode()) <= 1950:
                evidence[key] = result[key]
        evidence["signature"] = hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest()
        summary = ("Your page passed its checks. Open Website for your private preview and ZIP." if verified
            else "Open Settings > Keys and connections > Coding tools, sign in and check your connection, then retry your page." if outcome == "auth_required"
            else "Your tool reached its usage limit. Check your subscription usage in Settings > Keys and connections > Coding tools, then retry your page after it resets." if outcome == "quota_paused"
            else "Your page needs a fix. Open Build activity to review the checks, then choose Retry on Website if the fix round stops.")
        if not verified and outcome == "tests_failed" and checked["failures"]:
            summary = page_check_message(checked["failures"])
        elif not verified and result.get("failure_message"):
            summary = result["failure_message"]
        elif not verified and result.get("message") and outcome not in {"auth_required", "quota_paused"}:
            summary = result["message"]
        elif not verified and checked["failures"]:
            summary = page_check_message(checked["failures"])
        if not guarded and clean and (remark := result.get("remark")) and remark not in summary:
            if not verified and "Build activity" not in summary:
                summary += " Open Build activity to see what your tool tried."
            summary += " Tool note: " + loop_diagnose.scrub(remark, workspace, 150)
        summary = loop_diagnose.scrub(summary, workspace, 280)
        receipt = {"index": index, "phase": "code", "outcome": outcome, "summary": summary,
            "diagnosis": evidence, "commit_sha": commit,
            "caps_used": {"iterations": index + 1, "wall_seconds": min(descriptor["max_wall_seconds"], int(time.monotonic() - started))},
            "evidence_sha256": hashlib.sha256(json.dumps(evidence, sort_keys=True).encode()).hexdigest(), "posted": False}
        if verified:
            receipt["file_manifest_sha256"] = file_manifest_sha256
        worker.progress("running_coding", iteration_receipts=[*(worker.last.get("iteration_receipts") or []), receipt], message=summary)
        coding_loop._event(worker, summary, workspace)
        coding_loop.post_pending(worker, gateway)
        if check_error is not None:
            worker.progress("running_coding", stop_reason=stop_reason)
            runtime.fail(job_id, None, check_error)
        if verified or not clean or guarded or result.get("outcome") not in {"pass", "timeout", "cli_error"}:
            break
        prompt = brief + f"\nFix round {index + 1} of 3. Preserve supported copy, the confirmed meaning, consent and working actions. Correct ALL remaining checks in your code and page-plan.txt when supplied:\n" + "\n".join(failure_explanations(checked["failures"]))
    verified = outcome == "pass" and not worker.cancelled.is_set() and coding.authorize()
    if not verified:
        file_manifest_sha256 = None
    failure = {key: result[key] for key in ("failure_stage", "failure_code", "exception_type", "exception_location", "exception_frames", "exception_code", "check_failure")
        if key in result} if not verified else {}
    if failure:
        worker.progress("running_coding", **failure)
    if not verified:
        stop_reason = stop_reason or ("cancelled" if worker.cancelled.is_set() else
            "authority_expired" if not coding.authorize() else "wall_time_limit" if time.monotonic() >= deadline else
            "stop_requested" if soft_stop and soft_stop.is_set() else "repair_round_limit" if calls == iterations else outcome)
        worker.progress("running_coding", stop_reason=stop_reason)
    return {"operation": "workspace_loop_v1", "provider": coding.provider, "iterations": calls,
        **failure,
        **({"stop_reason": stop_reason} if stop_reason else {}),
        "outcome": "execution_requires_reconciliation" if outcome == "checks_unavailable" and reconcile_review
            else outcome if verified or outcome != "pass" else "stopped",
        "final_outcome": outcome if verified else "stopped",
        "task_verified": verified, "task_acceptance_verified": verified, "artifact_verified": verified,
        "source_commit": commit if verified else baseline, "file_manifest_sha256": file_manifest_sha256,
        "workspace_marker_verified": (workspace / ".supymem-venture").read_text().strip() == inputs.venture_id}
