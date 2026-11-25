"""The verification script builds a read-only one-off task, parses its rows and flags gaps."""

import importlib.util
import json
import os
import stat
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "scripts/analytics/verify_feature_usage.sh"


def _module():
    spec = importlib.util.spec_from_file_location("verify_feature_usage", ROOT / "scripts/analytics/verify_feature_usage.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # VERIFY_PAYLOAD is unset, so importing runs nothing
    return module


def _row(t, **kw):
    return "noise VERIFY_ROW|" + json.dumps({"t": t, **kw})


ROWS = "\n".join([
    _row("meta", hours=24, views=True, catalog=3),
    _row("feature", slug="website", views=5, uses=2, clicks=1, accounts=2, ext_accounts=1, ventures=1,
         last_seen="2026-10-01 10:00:00", inventory="D1"),
    _row("feature", slug="waitlist", views=0, uses=0, clicks=0, accounts=0, ext_accounts=0, ventures=0,
         last_seen=None, inventory="D7,D8"),
    _row("error", feature="website", source="server", error_code="http_5xx", status=500, n=3, accounts=1, ext=1),
    _row("error", feature="idea", source="worker", error_code="plan_invalid", status=None, n=2, accounts=1, ext=0),
    _row("user_error", error_source="tasks.run", intent="task_run", feature="execution", error_code="ui_error",
         sample="request outcome unknown", n=4, accounts=2, first_seen="2026-10-01 09:00:00", last_seen="2026-10-01 11:30:00"),
    _row("event", event_name="ui.screen_view", n=5),
    _row("job_table", kind="idea.validate", status="failed_dead", n=3),
    _row("job_event", kind="idea.validate", n=1),
    _row("dbcount", source="server", n=3),
    _row("dbcount", source="worker", n=2),
    _row("health", last_event="2026-10-01 10:00:00", catalog_rows=42, internal_accounts=1, last_rollup_day="2026-10-01"),
    _row("done"),
])
CW = {"status": "Complete", "results": [
    [{"field": "source", "value": "server"}, {"field": "feature", "value": "website"}, {"field": "code", "value": "http_5xx"},
     {"field": "n", "value": "5"}],
    [{"field": "source", "value": "worker"}, {"field": "feature", "value": "idea"}, {"field": "code", "value": "plan_invalid"},
     {"field": "n", "value": "2"}]]}


def test_render_lists_zero_use_features_errors_and_flags_mismatches():
    verify = _module()
    text = verify.render(verify.parse_rows(ROWS), CW)
    assert "waitlist" in text and "ZERO USE" in text and "1 features used, 2 with zero use" in text  # idea has errors but no use
    website = next(line for line in text.splitlines() if line.startswith("website"))
    assert website.split()[:6] == ["website", "5", "2", "1", "3", "2"]  # views uses clicks errors accounts
    assert "plan_invalid" in text and "ui.screen_view" in text
    seen = next(line for line in text.splitlines() if line.startswith("tasks.run"))
    assert seen.split()[:2] == ["tasks.run", "task_run"] and "request outcome unknown" in seen and "2026-10-01 11:30:00" in seen
    assert "UNRECORDED job failures for idea.validate: jobs table 3, analytics 1" in text
    assert "MISMATCH source=server db=3 cloudwatch=5" in text and "MISMATCH source=worker" not in text


def test_render_reports_incomplete_payload_and_clean_run():
    verify = _module()
    partial = verify.render(verify.parse_rows(_row("meta", hours=6, views=False, catalog=0)), None)
    assert "INCOMPLETE" in partial and "baseline on raw events" in partial
    clean = verify.render(verify.parse_rows("\n".join([_row("meta", hours=1, views=True, catalog=0), _row("done")])), None)
    assert "OK: no mismatches" in clean


FAKE_AWS = r'''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
log = open(os.environ["FAKE_AWS_LOG"], "a")
log.write(json.dumps(args) + "\n")
cmd = args[0] + " " + args[1]
if cmd == "ecs describe-task-definition":
    print("arn:aws:ecs:us-east-1:1:task-definition/selfhost-os-prod-core-api:363" if "taskDefinitionArn" in " ".join(args) else "selfhost-os-prod-core-api")
elif cmd == "ecs run-task":
    open(os.environ["FAKE_AWS_OVERRIDES"], "w").write(args[args.index("--overrides") + 1])
    print("arn:aws:ecs:us-east-1:1:task/selfhost-os-prod-core/abc123")
elif cmd == "ecs describe-tasks":
    print("0")
elif cmd == "logs filter-log-events":
    print("\t".join(open(os.environ["FAKE_AWS_ROWS"]).read().splitlines()))
elif cmd == "logs start-query":
    print("qid-1")
elif cmd == "logs get-query-results":
    print(open(os.environ["FAKE_AWS_CW"]).read())
'''


def test_shell_script_starts_a_read_only_task_and_prints_the_report(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "aws"
    fake.write_text(FAKE_AWS)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    (tmp_path / "rows.txt").write_text(ROWS)
    (tmp_path / "cw.json").write_text(json.dumps(CW, indent=4))
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "FAKE_AWS_LOG": str(tmp_path / "calls.log"),
           "FAKE_AWS_OVERRIDES": str(tmp_path / "overrides.json"), "FAKE_AWS_ROWS": str(tmp_path / "rows.txt"),
           "FAKE_AWS_CW": str(tmp_path / "cw.json"),
           "VERIFY_CLUSTER": "selfhost-os-prod-core", "VERIFY_TASK_FAMILY": "selfhost-os-prod-core-api",
           "VERIFY_LOG_GROUP": "/ecs/selfhost-os-prod-core-api",
           "VERIFY_WORKER_LOG_GROUP": "/ecs/selfhost-os-prod-worker-general"}
    done = subprocess.run(["bash", str(SCRIPT), "6"], env=env, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    assert "Per feature" in done.stdout and "MISMATCH source=server db=3 cloudwatch=5" in done.stdout
    calls = [json.loads(line) for line in (tmp_path / "calls.log").read_text().splitlines()]
    run = next(c for c in calls if c[:2] == ["ecs", "run-task"])
    assert run[run.index("--cluster") + 1] == "selfhost-os-prod-core"
    network = run[run.index("--network-configuration") + 1]
    assert "subnet-02f800211cfee1dcc,subnet-03b0398cc0fe76c78" in network and "sg-04ff610fe37431f2f" in network
    assert "assignPublicIp=DISABLED" in network
    override = json.loads((tmp_path / "overrides.json").read_text())["containerOverrides"][0]
    env_set = {e["name"]: e["value"] for e in override["environment"]}
    assert override["name"] == "app" and env_set["PGOPTIONS"] == "-c default_transaction_read_only=on"
    assert env_set["VERIFY_PAYLOAD"] == "1" and env_set["VERIFY_HOURS"] == "6"
    assert override["command"][:2] == ["python", "-c"] and len(json.dumps({"containerOverrides": [override]})) < 8192
    query = next(c for c in calls if c[:2] == ["logs", "start-query"])
    assert "/ecs/selfhost-os-prod-core-api" in query and "/ecs/selfhost-os-prod-worker-general" in query


def test_shell_script_rejects_bad_hours_without_calling_aws(tmp_path):
    for bad in ("0", "abc", "9999", "1;rm"):
        done = subprocess.run(["bash", str(SCRIPT), bad], capture_output=True, text=True, timeout=30,
                              env={**os.environ, "PATH": "/nonexistent:/usr/bin:/bin"})
        assert done.returncode == 2 and "usage" in done.stderr
