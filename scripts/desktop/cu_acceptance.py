"""Computer-use QA evidence gate. Unknown, partial and mixed-build runs fail closed.

This summarizes owned QA trials; it never certifies the installed app, cursor,
tenant routing, credit settlement, or revenue. Those require separate app checks.
No task text, screen content, credentials, or model summaries are retained here.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def reviewed_browser_profile():
    return json.loads((ROOT / "config/browser_cuai_profile.json").read_text())


@contextmanager
def desktop_lease(path=None):
    """One screen trial runner at a time; fail immediately rather than contend."""
    import fcntl

    path = path or Path("/tmp") / f"supymem-cu-screen-{os.getuid()}.lock"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or not stat.S_ISREG(info.st_mode):
            raise ValueError("screen_lock_not_owned")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another_desktop_trial_is_running") from None
        yield
    finally:
        os.close(fd)


def source_digest(root: Path = ROOT, *, scope: str = "all") -> str:
    """Fingerprint actual source, including local edits; git HEAD alone misses them."""
    paths = []
    if scope not in {"all", "browser"}:
        raise ValueError("unknown_source_scope")
    for relative in ("packages/desktop_runtime", "packages/coding_agents", "packages/core"):
        paths.extend((root / relative).rglob("*.py"))
        project = root / relative / "pyproject.toml"
        if project.is_file():
            paths.append(project)
    for relative in ("services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
                     "services/qa_runner/requirements.lock", "config/browser_cuai_profile.json", "config/llm_pricing.yaml"):
        path = root / relative
        if path.is_file():
            paths.append(path)
    if scope == "all":
        paths.extend((root / "scripts/desktop").rglob("*.py"))
    else:
        for relative in ("cu_browser_suite.py", "cu_ai_trial.py", "cu_provider_budget.py", "cu_verifier_artifact.py", "cu_trial_private_evidence.py", "cu_acceptance.py", "cu_tasks/fixture_server.py",
                         "cu_browser_round1.py", "cu_tasks/round1_fixture.py"):
            path = root / "scripts/desktop" / relative
            if path.is_file():
                paths.append(path)
        # Round-1 task text is part of the harness: an edited goal must not reuse an old row.
        paths.extend((root / "docs/supymem/beta-2026-10-04/browser-tests").glob("*_tasks.yaml"))
    digest = hashlib.sha256()
    for path in sorted(paths):
        if path.is_symlink():
            raise ValueError("source_symlink_not_supported")
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


def percentile(values, fraction):
    values = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v) and v >= 0)
    return values[math.ceil(len(values) * fraction) - 1] if values else None


def row_faults(row, suite, *, require_private=False):
    faults = []
    if row.get("passed") is not True:
        faults.append("task_failed")
    if row.get("cleanup_verified") is not True:
        faults.append("cleanup_unverified")
    if suite == "desktop":
        if row.get("focus_mode") != "frontmost":
            faults.append("strict_focus_not_measured")
        if row.get("source_unchanged") is not True:
            faults.append("source_changed_or_unchecked_during_run")
        for key in ("decoy_untouched", "background_verified", "method_verified"):
            if row.get(key) is not True:
                faults.append(key + "_unverified")
        if row.get("verification_status") != "passed":
            faults.append("oracle_unverified")
        if row.get("foreground_escalations") != 0:
            faults.append("foreground_escalation")
    elif suite == "browser":
        if row.get("oracle") is not True:
            faults.append("oracle_unverified")
        if row.get("independent_verification") is not True:
            faults.append("independent_verification_unconfirmed")
        if row.get("browser_process_gone") is not True:
            faults.append("browser_process_cleanup_unconfirmed")
        if type(row.get("forbidden_requests")) is not int or row["forbidden_requests"] != 0:
            faults.append("forbidden_request_or_unknown")
        from cu_browser_suite import TASKS, LIVE, private_fingerprint
        task = row.get("task")
        controlled = isinstance(task, str) and task in {t.id for t in TASKS}
        public = isinstance(task, str) and task in {t.id for t in LIVE}
        if "diagnostic_requested" in row or require_private and controlled:
            from cu_trial_private_evidence import PrivateTrialEvidence
            if (public or not PrivateTrialEvidence.metadata_valid(row)
                    or controlled and (row.get("diagnostic_scope") != "controlled_baseline"
                                       or row.get("harness_source_digest") != private_fingerprint())):
                faults.append("private_diagnostic_custody_unverified")
    else:
        raise ValueError("unknown_suite")
    return faults


def evaluate(records, expected, *, suite, repetitions=2, digest=None, require_private=False):
    """Each selected task must run exactly once per repetition on the same source."""
    if type(repetitions) is not int or repetitions < 1 or not expected or len(set(expected)) != len(expected):
        raise ValueError("invalid_acceptance_selection")
    expected = set(expected)
    faults = []
    counts = Counter()
    categories = defaultdict(lambda: {"total": 0, "passed": 0})
    failures = Counter()
    digests, models, transports = set(), set(), set()
    identities = set()
    diagnostic_ids = set()
    for row in records:
        task = row.get("trial") if suite == "desktop" else row.get("task")
        if not isinstance(task, str):
            task = None
        repetition = row.get("repetition")
        if task not in expected or type(repetition) is not int or not 1 <= repetition <= repetitions:
            faults.append("unexpected_task_or_repetition")
        if type(repetition) is not int:
            repetition = None
        counts[(task, repetition)] += 1
        run_id = row.get("run_id") if isinstance(row.get("run_id"), str) else None
        identity = (run_id, task, repetition)
        if not run_id or identity in identities:
            faults.append("missing_or_duplicate_run_identity")
        identities.add(identity)
        d = row.get("source_digest")
        if not isinstance(d, str) or len(d) != 64 or any(c not in "0123456789abcdef" for c in d):
            faults.append("missing_source_digest")
        elif digest is not None and d != digest:
            faults.append("source_changed_since_run")
        digests.add(d if isinstance(d, str) else None)
        models.add(row.get("model") if isinstance(row.get("model"), str) and row.get("model") else None)
        transports.add(row.get("qa_transport") if isinstance(row.get("qa_transport"), str) and row.get("qa_transport") else None)
        problems = row_faults(row, suite, require_private=require_private)
        if "diagnostic_requested" in row:
            trial_id = row.get("diagnostic_trial_id") if isinstance(row.get("diagnostic_trial_id"), str) else None
            if trial_id is None or trial_id in diagnostic_ids:
                problems.append("private_diagnostic_identity_reused_or_missing")
            diagnostic_ids.add(trial_id if isinstance(trial_id, str) else None)
        category = str(row.get("category") or row.get("cat") or "unknown")
        categories[category]["total"] += 1
        categories[category]["passed"] += not problems
        for problem in problems:
            failures[problem] += 1
        if row.get("passed") is not True:
            failures[str(row.get("failure_class") or row.get("reason") or row.get("outcome") or "unknown")] += 1
    missing = [f"{task}:{repetition}" for task in sorted(expected) for repetition in range(1, repetitions + 1)
               if counts[(task, repetition)] != 1]
    if missing:
        faults.append("missing_or_duplicate_trials")
    if len(digests) != 1:
        faults.append("mixed_source_versions")
    if len(models) != 1 or None in models:
        faults.append("missing_or_mixed_models")
    if suite == "browser" and not models <= {
            role["model"] for role in reviewed_browser_profile()["roles"].values()}:
        faults.append("unreviewed_browser_model")
    if len(transports) != 1 or None in transports:
        faults.append("missing_or_mixed_transports")
    if failures:
        faults.append("strict_checks_failed")
    timings = [value for row in records for value in (row.get("step_ms") if isinstance(row.get("step_ms"), list) else [])]
    walls = [row.get("seconds") for row in records]
    return {"schema_version": 1, "suite": suite, "scope": "owned_qa_trials",
            "installed_app_verified": False, "cursor_verified": False, "revenue_verified": False,
            "accepted": not faults, "expected_trials": len(expected) * repetitions, "observed_trials": len(records),
            "strict_passed": sum(category["passed"] for category in categories.values()),
            "faults": sorted(set(faults)), "missing_or_duplicate": missing,
            "categories": dict(sorted(categories.items())), "failure_counts": dict(failures),
            "step_ms": {"p50": percentile(timings, .5), "p95": percentile(timings, .95), "samples": len(timings)},
            "wall_seconds": {"p50": percentile(walls, .5), "p95": percentile(walls, .95)}}


def expected_selection(suite, only="", *, live=False):
    """Select complete QA coverage without running a browser, GUI or model."""
    if suite == "desktop":
        if live:
            raise ValueError("live_selection_requires_browser_suite")
        import cu_tasks
        return [task.id for task in cu_tasks.select(only=only, long=False)]
    if suite != "browser":
        raise ValueError("unknown_suite")
    from cu_browser_suite import TASKS, LIVE
    available = list(TASKS) + (list(LIVE) if live else [])
    wanted = set(filter(None, only.split(",")))
    expected = [task.id for task in available if not wanted or task.id in wanted]
    if wanted - set(expected):
        raise ValueError("unknown_task_selection")
    return expected


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--suite", choices=("desktop", "browser"), required=True)
    ap.add_argument("--results", type=Path, nargs="+", required=True)
    ap.add_argument("--only", default="", help="Explicit subset; omitted means the full short catalog")
    ap.add_argument("--live", action="store_true", help="Browser only: include public read-only tasks in expected coverage")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--private-diagnostics", action="store_true", help="Require sealed diagnostic linkage for controlled browser rows; public capture is forbidden")
    args = ap.parse_args()
    if args.private_diagnostics and args.suite != "browser":
        ap.error("private diagnostics require the controlled browser suite")
    try:
        expected = expected_selection(args.suite, args.only, live=args.live)
    except ValueError:
        ap.error("unknown or invalid task selection")
    try:
        records = [json.loads(line) for path in args.results for line in path.read_text().splitlines() if line.strip()]
        if any(not isinstance(row, dict) for row in records):
            raise ValueError("invalid_trial_record")
        scopes = {row.get("source_scope", "all") for row in records if isinstance(row.get("source_scope", "all"), str)}
        if len(scopes) != 1 or not scopes <= ({"all", "browser"} if args.suite == "browser" else {"all"}):
            raise ValueError("invalid_source_scope")
        report = evaluate(records, expected, suite=args.suite, repetitions=args.runs, digest=source_digest(scope=next(iter(scopes))),
                          require_private=args.private_diagnostics)
    except (OSError, ValueError) as error:
        print(json.dumps({"accepted": False, "error": type(error).__name__}))
        return 2
    print(json.dumps(report, indent=2))
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
