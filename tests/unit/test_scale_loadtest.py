import importlib.util
import io
import json
from pathlib import Path
import subprocess
import shutil

import pytest
from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/loadtest/k6" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def inspect_driver(fixtures, environment=None, responses=None):
    source = r"""
const fs = require('node:fs');
const vm = require('node:vm');
const crypto = require('node:crypto');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const requests = [];
const counters = {};
class Counter {
  constructor(name) { this.name = name; counters[name] = 0; }
  add(value) { counters[this.name] += value; }
}
class Trend { add() {} }
const context = {
  __ENV: { FIXTURES: 'fixtures', COS_INTERNAL_ASSERTION_SECRET: 'x'.repeat(32), ...input.environment },
  open: path => path === 'fixtures' ? JSON.stringify(input.fixtures) : JSON.stringify(input.approval),
  SharedArray: class { constructor(name, callback) { return callback(); } }, Counter, Trend,
  exec: { vu: { idInTest: 1 }, scenario: { name: 'chat_extraction', iterationInTest: 0 } },
  crypto: {
    sha256: value => crypto.createHash('sha256').update(value).digest('hex'),
    hmac: (algorithm, secret, value) => crypto.createHmac(algorithm, secret).update(value).digest(),
  },
  encoding: { b64encode: value => Buffer.from(value).toString('base64url') },
  check: (response, checks) => Object.values(checks).every(check => check(response)), sleep() {},
  http: { request(method, url, body, options) {
    requests.push({ method, url, body, options });
    const response = input.responses && input.responses[requests.length - 1];
    return { status: response ? response.status : 200, json: () => response ? response.body : input.fixtures.tenants[0] };
  } },
};
vm.createContext(context);
const targets = input.targets.replace(/^export /gm, '');
const driver = input.driver.replace(/^import .*;\n/gm, '').replace(/^export /gm, '');
vm.runInContext(targets + '\n' + driver + '\nworkflow(); globalThis.result = { options, receipt: handleSummary(' +
  JSON.stringify(input.summary) + ') };', context);
process.stdout.write(JSON.stringify({ ...context.result, requests, counters }));
"""
    names = ("http_req_duration", "interactive_queue_wait_ms", "interactive_queue_samples", "tenant_leaks", "workflow_failures", "dropped_iterations")
    summary = {"metrics": {name: {"thresholds": {"test threshold": {"ok": True}}} for name in names},
        "state": {"testRunDurationMs": 21600000}}
    payload = dict(fixtures=fixtures, environment=environment or {}, responses=responses,
        approval=(environment or {}).get("approval"), summary=summary,
        targets=(ROOT / "scripts/loadtest/k6/targets.js").read_text(),
        driver=(ROOT / "scripts/loadtest/k6/scenarios.js").read_text())
    return subprocess.run(["node", "-e", source], input=json.dumps(payload), text=True, capture_output=True)


@pytest.mark.parametrize("domains", [("Commercial refrigeration", "Backyard planting"), ("Factory dispatch", "Family recipes")])
def test_stub_seed_is_scoped_and_unknown_providers_fail_closed(tmp_path, monkeypatch, domains):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.delenv("RDS_PROXY_ENDPOINT", raising=False)
    seed = module("seed")
    url = f"sqlite:///{tmp_path / 'loadtest_fixtures.db'}"
    fixtures = seed.seed(url, 2, domains, 2, {"polling": [{"path": "/ventures/{{venture_id}}/chat"}]})
    assert len(fixtures["tenants"]) == 2
    assert {tenant["domain"] for tenant in fixtures["tenants"]} == set(domains)
    assert [len(tenant["mailbox_ids"]) for tenant in fixtures["tenants"]] == [2, 3]
    engine = create_engine(url)
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM chat_messages")) == 80
        assert connection.scalar(text("SELECT count(*) FROM gtm_email_events")) == 100
    engine.dispose()
    with pytest.raises(ValueError, match="occupied"):
        seed.seed(url, 2, domains, 2, {})
    with pytest.raises(ValueError, match="non-loopback"):
        seed.seed("postgresql+psycopg://user:password@remote.invalid/loadtest_db", 2, domains, 2, {})
    for unsafe_url in ("postgresql://localhost/loadtest_db?host=remote.invalid",
                       "postgresql://localhost/loadtest_db?dbname=live",
                       "mysql://remote.invalid/loadtest_db"):
        with pytest.raises(ValueError, match="without connection overrides"):
            seed.seed(unsafe_url, 2, domains, 2, {})

    stubs = module("provider_stubs")
    handler = stubs.StubHandler.__new__(stubs.StubHandler)
    handler.headers = {"Content-Length": "2"}
    handler.command = "POST"
    handler.path = "/missing"
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    statuses = []
    handler.send_response = statuses.append
    handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    handler.respond()
    assert statuses == [503]
    assert json.loads(handler.wfile.getvalue())["error"] == "unconfigured_stub_route"
    handler.fixtures = {"POST /configured": {"status": 200, "body": {"domain": domains[0]}}}
    handler.path = "/configured"
    handler.rfile, handler.wfile = io.BytesIO(b"{}"), io.BytesIO()
    handler.respond()
    assert statuses[-1] == 200
    assert json.loads(handler.wfile.getvalue())["domain"] == domains[0]
    workflows = {name: [{"path": "/ventures/{{venture_id}}/chat"}] for name in
        ("polling", "chat_extraction", "research", "sends", "reply_reading", "briefings", "autopilot", "nightly_sweeps")}
    fixtures["workflows"] = workflows
    manifest = tmp_path / "fixtures.json"
    manifest.write_text(json.dumps(fixtures))
    import os
    env = dict(PATH=os.environ["PATH"], HOME=str(tmp_path))
    if shutil.which("k6"):
        inspected = subprocess.run(["k6", "inspect", "-e", f"FIXTURES={manifest}", str(ROOT / "scripts/loadtest/k6/scenarios.js")],
                                   env=env, text=True, capture_output=True)
        assert inspected.returncode == 0, inspected.stderr
        assert len(json.loads(inspected.stdout)["scenarios"]) == 8
    inspected = inspect_driver(fixtures)
    assert inspected.returncode == 0, inspected.stderr
    assert len(json.loads(inspected.stdout)["options"]["scenarios"]) == 8


@pytest.mark.parametrize("domains", [("Commercial refrigeration", "Backyard planting"), ("Factory dispatch", "Family recipes")])
def test_staging_requires_exact_unexpired_review_and_blocks_production(domains):
    from copy import deepcopy
    from datetime import datetime, timedelta, timezone
    import hashlib
    tenants = [dict(subject=f"person{index}", email=f"person{index}@example.invalid", workspace_id=f"workspace{index}",
        venture_id=f"venture{index}", mailbox_ids=[f"mailbox{index}"], domain=domain) for index, domain in enumerate(domains)]
    workflows = {name: [{"path": "/ventures/{{venture_id}}/chat", "body": {"domain": "{{domain}}"}}] for name in
        ("polling", "chat_extraction", "research", "sends", "reply_reading", "briefings", "autopilot", "nightly_sweeps")}
    fixtures = dict(stub_only=True, tenants=tenants, workflows=workflows)
    approval = dict(environment="staging", approved=True, approved_by="reviewer", stub_only=True, egress_restricted=True,
        expires_at=(datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        base_url="https://staging.example.invalid", stub_url="https://stubs.staging.example.invalid",
        production_origins=["https://app.example.invalid"],
        fixtures_sha256=hashlib.sha256(json.dumps(fixtures, separators=(",", ":")).encode()).hexdigest())
    environment = dict(BASE_URL=approval["base_url"], STUB_URL=approval["stub_url"], STAGING_APPROVAL="approval", approval=approval)
    for tenant in tenants:
        ordered = {**fixtures, "tenants": [tenant]}
        current = deepcopy(environment)
        current["approval"]["fixtures_sha256"] = hashlib.sha256(json.dumps(ordered, separators=(",", ":")).encode()).hexdigest()
        inspected = inspect_driver(ordered, current)
        assert inspected.returncode == 0, inspected.stderr
        result = json.loads(inspected.stdout)
        assert result["requests"][0]["url"] == approval["base_url"] + "/ventures/" + tenant["venture_id"] + "/chat"
        assert json.loads(result["requests"][0]["body"])["domain"] == tenant["domain"]
        assert result["requests"][0]["options"]["redirects"] == 0
        assert result["counters"]["tenant_leaks"] == 0
        receipt = json.loads(result["receipt"]["stdout"])
        assert receipt["environment"] == "staging" and receipt["capacity_approved"] is False
        assert receipt["application_cost"] == "not measured"
    for key, value in (("environment", "production"), ("approved", False), ("approved_by", ""),
                       ("expires_at", "2000-01-01T00:00:00Z"), ("expires_at", "invalid"),
                       ("stub_only", False), ("egress_restricted", False), ("production_origins", []),
                       ("production_origins", [approval["base_url"]]), ("fixtures_sha256", "unreviewed")):
        invalid = deepcopy(environment)
        invalid["approval"][key] = value
        assert inspect_driver(fixtures, invalid).returncode != 0, (key, value)
    for base in ("https://app.example.invalid", "https://other.staging.example.invalid", "http://staging.example.invalid",
                 "https://staging.example.invalid.attacker.invalid", "https://staging.example.invalid/path",
                 "https://staging.example.invalid@production.example.invalid", "https://staging.example.invalid?redirect=1",
                 "https://staging.example.invalid:99999", "https://production.staging.example.invalid"):
        assert inspect_driver(fixtures, {**environment, "BASE_URL": base}).returncode != 0, base
    assert inspect_driver(fixtures, {**environment, "STUB_URL": "https://app.example.invalid"}).returncode != 0
    assert inspect_driver(fixtures, {"BASE_URL": approval["base_url"]}).returncode != 0
    assert inspect_driver({**fixtures, "stub_only": False}).returncode != 0
    for path in ("https://app.example.invalid/send", "//app.example.invalid/send", "/\\app.example.invalid", "/{{missing}}"):
        invalid = deepcopy(fixtures)
        invalid["workflows"]["chat_extraction"][0]["path"] = path
        assert inspect_driver(invalid).returncode != 0, path
    failed = deepcopy(fixtures)
    failed["workflows"]["chat_extraction"] *= 2
    inspected = inspect_driver(failed, responses=[{"status": 403, "body": {}}])
    assert inspected.returncode == 0, inspected.stderr
    result = json.loads(inspected.stdout)
    assert len(result["requests"]) == 1 and result["counters"]["workflow_failures"] == 1


@pytest.mark.parametrize("founders", [1000, 10000])
def test_soak_receipts_require_matching_measured_cost_and_recovery_evidence(founders):
    from copy import deepcopy
    receipts = module("receipts")
    metrics = {name: {"thresholds": {"required": {"ok": True}}} for name in
        ("http_req_duration", "interactive_queue_wait_ms", "interactive_queue_samples", "tenant_leaks", "workflow_failures", "dropped_iterations")}
    run = dict(founders=founders, environment="staging", metrics=metrics, run_sha256="test-run",
        state={"testRunDurationMs": 21600000}, inject_failures=True,
        started_at="2026-10-30T09:00:00Z", finished_at="2026-10-30T15:00:00Z")
    event = dict(receipt_id="recovery-test", started_at="2026-10-30T10:00:00Z", recovered_at="2026-10-30T10:01:00Z")
    evidence = dict(receipt_ids=["cost-test", "delivery-test"], run_sha256="test-run", provider_cost_usd=0,
        infrastructure_cost_usd=2.5, lost_jobs=0, duplicate_sends=0,
        worker_restart=event, redis_outage=event, rolling_replacement=event)
    result = receipts.combine(run, evidence)
    assert result["soak_checks_passed"] is True and result["capacity_approved"] is False
    assert result["application_cost"] == 2.5
    for key, value in (("run_sha256", "other-run"), ("receipt_ids", []), ("provider_cost_usd", -1),
                       ("infrastructure_cost_usd", float("nan")), ("lost_jobs", "unknown"), ("duplicate_sends", True),
                       ("worker_restart", None), ("redis_outage", {**event, "recovered_at": event["started_at"]}),
                       ("redis_outage", {**event, "recovered_at": "2026-10-30T18:00:00Z"}),
                       ("rolling_replacement", {**event, "started_at": "2026-10-30T10:00:00"})):
        with pytest.raises(ValueError):
            receipts.combine(run, {**evidence, key: value})
    assert receipts.combine(run, {**evidence, "duplicate_sends": 1})["soak_checks_passed"] is False
    assert receipts.combine({**run, "state": {"testRunDurationMs": 1000}}, evidence)["soak_checks_passed"] is False
    assert receipts.combine({**run, "inject_failures": False}, evidence)["soak_checks_passed"] is False
    missing = deepcopy(run)
    del missing["metrics"]["interactive_queue_samples"]
    assert receipts.combine(missing, evidence)["soak_checks_passed"] is False
    failed = deepcopy(run)
    failed["metrics"]["tenant_leaks"]["thresholds"]["required"]["ok"] = False
    assert receipts.combine(failed, evidence)["soak_checks_passed"] is False
