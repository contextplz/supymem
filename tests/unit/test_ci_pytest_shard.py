"""Adversarial same-run collection evidence and dependency-free aggregate tests."""
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ci_shards", ROOT / "scripts/ci_pytest_shard.py")
shards = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shards)
PATHS = ["tests/example.py"]
EXTRA = ["-m", "not slow"]
IDS = [f"tests/example.py::test_{i}" for i in range(20)]
IDS.sort()


@pytest.fixture(scope="module")
def attested_checkout(tmp_path_factory):
    repo = tmp_path_factory.mktemp("shard-checkout")
    (repo / "scripts").mkdir()
    shutil.copyfile(ROOT / "scripts/ci_pytest_shard.py", repo / "scripts/ci_pytest_shard.py")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid",
                    "commit", "--allow-empty", "-qm", "fixture"], cwd=repo, check=True)
    return repo


@pytest.fixture
def evidence(tmp_path, monkeypatch, attested_checkout):
    monkeypatch.setattr(shards, "ROOT", attested_checkout)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=attested_checkout, text=True).strip()
    for name, value in {"GITHUB_SHA": sha, "GITHUB_RUN_ID": "42", "GITHUB_RUN_ATTEMPT": "2",
                        "GITHUB_REPOSITORY": "owner/repo", "GITHUB_WORKFLOW": "ci"}.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("GITHUB_EVENT_NAME", raising=False)
    identity = shards.attestation_identity(PATHS, EXTRA, 3)
    for group, selected in enumerate(shards.partition(IDS, 3), 1):
        shards.write_nodeids(tmp_path / f"shard-{group}.txt", selected)
        shards.write_nodeids(tmp_path / f"shard-{group}-all.txt", IDS)
        receipt = {**identity, "group": group, "test_exit": 0,
                   "collection_sha256": shards.nodeids_digest(IDS),
                   "selected_sha256": shards.nodeids_digest(selected)}
        (tmp_path / f"shard-{group}.json").write_text(json.dumps(receipt))
    return tmp_path


def verify(directory, **changes):
    return shards.verify_attestations(**{ "paths": PATHS, "extra": EXTRA, "splits": 3,
                                          "nodeids_dir": directory, **changes})


def edit_receipt(directory, update):
    path = directory / "shard-1.json"
    receipt = json.loads(path.read_text())
    update(receipt)
    path.write_text(json.dumps(receipt))


def test_valid_and_no_pytest_dependency(evidence):
    assert verify(evidence) == 0
    # -S disables site packages: aggregate still works without pytest or app deps.
    result = subprocess.run([sys.executable, "-S", str(shards.ROOT / "scripts/ci_pytest_shard.py"),
                             "--verify-attestations", "--splits", "3", "--nodeids-dir",
                             str(evidence), "--", *EXTRA, *PATHS], cwd=shards.ROOT)
    assert result.returncode == 0


@pytest.mark.parametrize("field,value", [("GITHUB_SHA", "stale"), ("GITHUB_RUN_ID", "41"),
                                         ("GITHUB_RUN_ATTEMPT", "1"),
                                         ("GITHUB_REPOSITORY", "wrong/repo"),
                                         ("GITHUB_WORKFLOW", "different")])
def test_stale_or_foreign_identity_rejected(evidence, field, value):
    edit_receipt(evidence, lambda r: r["context"].update({field: value}))
    assert verify(evidence) == 1


@pytest.mark.parametrize("field,value", [("group", 2), ("splits", 4), ("schema", 2),
                                         ("test_exit", 1), ("test_exit", False),
                                         ("paths", ["tests/other.py"]), ("extra", []),
                                         ("assignment", "random"), ("script_sha256", "stale")])
def test_wrong_contract_rejected(evidence, field, value):
    edit_receipt(evidence, lambda r: r.update({field: value}))
    assert verify(evidence) == 1


@pytest.mark.parametrize("suffix", [".txt", "-all.txt", ".json"])
def test_missing_any_shard_file_rejected(evidence, suffix):
    (evidence / f"shard-2{suffix}").unlink()
    assert verify(evidence) == 1


def test_unexpected_artifact_rejected(evidence):
    (evidence / "shard-4.json").write_text("{}")
    assert verify(evidence) == 1


@pytest.mark.parametrize("suffix", [".txt", "-all.txt"])
def test_duplicate_nodeid_rejected_even_with_updated_digest(evidence, suffix):
    path = evidence / f"shard-1{suffix}"
    ids = shards.read_nodeids(path)
    ids.insert(0, ids[0])
    shards.write_nodeids(path, ids)
    field = "selected_sha256" if suffix == ".txt" else "collection_sha256"
    edit_receipt(evidence, lambda r: r.update({field: shards.nodeids_digest(ids)}))
    assert verify(evidence) == 1


def test_different_full_collection_rejected_even_with_valid_hash(evidence):
    ids = IDS[:-1]
    shards.write_nodeids(evidence / "shard-1-all.txt", ids)
    edit_receipt(evidence, lambda r: r.update({"collection_sha256": shards.nodeids_digest(ids)}))
    assert verify(evidence) == 1


@pytest.mark.parametrize("corruption", ["missing", "extra", "overlap", "wrong_assignment"])
def test_partition_corruption_rejected_even_with_valid_hash(evidence, corruption):
    ids = shards.read_nodeids(evidence / "shard-1.txt")
    if corruption == "missing":
        ids.pop()
    else:
        foreign = shards.read_nodeids(evidence / "shard-2.txt")[0]
        ids.append("tests/example.py::invented" if corruption == "extra" else foreign)
        if corruption == "wrong_assignment":
            ids.pop(0)
        ids.sort()
    shards.write_nodeids(evidence / "shard-1.txt", ids)
    edit_receipt(evidence, lambda r: r.update({"selected_sha256": shards.nodeids_digest(ids)}))
    assert verify(evidence) == 1


def test_empty_full_collection_rejected(evidence):
    for group in range(1, 4):
        shards.write_nodeids(evidence / f"shard-{group}-all.txt", [])
        shards.write_nodeids(evidence / f"shard-{group}.txt", [])
        path = evidence / f"shard-{group}.json"
        r = json.loads(path.read_text())
        r.update(collection_sha256=shards.nodeids_digest([]), selected_sha256=shards.nodeids_digest([]))
        path.write_text(json.dumps(r))
    assert verify(evidence) == 1


def test_checkout_context_mismatch_rejected(evidence, monkeypatch):
    monkeypatch.setenv("GITHUB_SHA", "wrong")
    with pytest.raises(SystemExit, match="checkout differs"):
        verify(evidence)


@pytest.mark.parametrize("head_matches", [True, False])
def test_pull_request_attests_the_checked_out_head_not_the_merge_commit(evidence, monkeypatch, tmp_path, head_matches):
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=shards.ROOT, text=True).strip()
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"pull_request": {"head": {"sha": head if head_matches else "other"}}}))
    monkeypatch.setenv("GITHUB_SHA", "synthetic-merge-commit")
    monkeypatch.setenv("GITHUB_EVENT_NAME", "pull_request")
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    if head_matches:
        assert shards.attestation_identity(PATHS, EXTRA, 3)["context"]["GITHUB_SHA"] == head
    else:
        with pytest.raises(SystemExit, match="checkout differs"):
            shards.attestation_identity(PATHS, EXTRA, 3)


def test_missing_run_context_rejected(evidence, monkeypatch):
    monkeypatch.delenv("GITHUB_RUN_ATTEMPT")
    with pytest.raises(SystemExit, match="complete GitHub"):
        verify(evidence)


def test_failed_execution_never_has_success_receipt(evidence, monkeypatch):
    monkeypatch.setattr(shards, "collect_nodeids", lambda *args: IDS)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=shards.ROOT, text=True)
    monkeypatch.setattr(shards.subprocess, "check_output", lambda *a, **k: checkout)
    monkeypatch.setattr(shards.subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess([], 1))
    result = shards.run_shard(pytest_parts=[sys.executable, "-m", "pytest"], paths=PATHS,
                              extra=EXTRA, splits=3, group=1,
                              nodeids_out=evidence / "shard-1.txt", attest=True)
    assert result == 1
    assert not (evidence / "shard-1.json").exists()


def test_collection_error_and_duplicates_fail_closed(monkeypatch):
    for code, text in [(1, "tests/example.py::test_x\n"),
                       (0, "tests/example.py::test_x\ntests/example.py::test_x\n")]:
        monkeypatch.setattr(shards.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], code, text))
        with pytest.raises(SystemExit):
            shards.collect_nodeids([sys.executable, "-m", "pytest"], PATHS, EXTRA)


def test_successful_empty_shard_attests_full_collection(evidence, monkeypatch):
    ids = [next(i for i in IDS if shards.assign_group(i, 3) != 0)]
    monkeypatch.setattr(shards, "collect_nodeids", lambda *args: ids)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=shards.ROOT, text=True)
    monkeypatch.setattr(shards.subprocess, "check_output", lambda *a, **k: checkout)
    monkeypatch.setattr(shards.subprocess, "run", lambda *a, **k: pytest.fail("empty shard executes no tests"))
    assert shards.run_shard(pytest_parts=[], paths=PATHS, extra=EXTRA, splits=3, group=1,
                            nodeids_out=evidence / "shard-1.txt", attest=True) == 0
    assert shards.read_nodeids(evidence / "shard-1-all.txt") == ids
    assert json.loads((evidence / "shard-1.json").read_text())["test_exit"] == 0


@pytest.mark.hosted
def test_ci_all_test_jobs_and_stable_aggregate_names_preserved():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    jobs = workflow["jobs"]
    for aggregate, count, prefix in [("unit", 16, "unit"), ("authenticated-boundary", 3, "auth"),
                                      ("integration-pg", 12, "pg")]:
        shard = jobs[aggregate + "-shard"]
        assert shard["strategy"]["matrix"]["shard"] == list(range(1, count + 1))
        assert shard["strategy"]["fail-fast"] is False
        splits_env = {"unit": "UNIT_SPLITS", "authenticated-boundary": "AUTH_SPLITS",
                      "integration-pg": "PG_SPLITS"}[aggregate]
        assert shard["env"][splits_env] == jobs[aggregate]["env"][splits_env] == str(count)
        assert any("--attest" in step.get("run", "") for step in shard["steps"])
        upload = next(step for step in shard["steps"] if step.get("uses") == "actions/upload-artifact@v4")
        assert len(upload["with"]["path"].splitlines()) == 3
        assert "github.run_attempt" in upload["with"]["name"]
        gate = jobs[aggregate]
        assert gate["name"] == aggregate and set(gate["needs"]) == {"gate", aggregate + "-shard"}
        # Shards may be skipped only by a successful `gate` job, never silently.
        assert 'needs.gate.result }}" = "success"' in gate["steps"][0]["run"]
        assert shard["needs"] == "gate" and "needs.gate.outputs.run" in shard["if"]
        assert "= \"success\"" in gate["steps"][0]["run"]
        assert not any("install" in step.get("run", "") or "setup-python" in step.get("uses", "")
                       for step in gate["steps"])
        assert any("--verify-attestations" in step.get("run", "") for step in gate["steps"])
    assert "tests/unit/test_ci_pytest_shard.py" in workflow["env"]["UNIT_PATHS"]
    for path in ("tests/unit/test_autonomous_experiment_production_fixes.py",
                 "tests/unit/test_experiment_run_for_me.py", "tests/unit/chat/test_onboarding_intents.py",
                 "tests/unit/jobs/test_lease.py"):
        assert path in workflow["env"]["UNIT_PATHS"].split()
    # Every test-running job is behind the gate and every job has a timeout.
    for name, job in jobs.items():
        assert "timeout-minutes" in job, name
        if name not in {"gate", "unit", "authenticated-boundary", "integration-pg", "ci-green"}:
            flag = "light" if name in {"lint", "console", "security", "infra", "dependency-scan"} else "run"
            assert job["needs"] == "gate" and f"needs.gate.outputs.{flag}" in job["if"], name
    assert "pull_request" in workflow[True] and "merge_group" in workflow[True]


@pytest.mark.hosted
def test_ci_registers_every_unit_test_in_its_runner():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    covered = set()
    for name in ("UNIT_PATHS", "AUTH_PATHS", "PG_PATHS"):
        for entry in workflow["env"][name].split():
            path = ROOT / entry
            covered.update(path.rglob("test_*.py") if path.is_dir() else [path])
    expected = set((ROOT / "tests/unit").rglob("test_*.py"))
    assert not expected - covered, sorted(str(path.relative_to(ROOT)) for path in expected - covered)
    assert "tests/unit/db/test_scale_indexes.py" in workflow["env"]["PG_PATHS"].split()
    commands = "\n".join(step.get("run", "") for job in workflow["jobs"].values() for step in job["steps"])
    for path in (ROOT / "tests/unit").rglob("*.test.*"):
        assert path.relative_to(ROOT).as_posix() in commands


@pytest.mark.hosted
def test_ci_firefox_cache_preserves_provisioning_and_verification():
    import tomllib

    package = tomllib.loads((ROOT / "packages/desktop_runtime/pyproject.toml").read_text())
    version = next(dep.removeprefix("playwright==") for dep in package["project"]["dependencies"]
                   if dep.startswith("playwright=="))
    keys = set()
    installers = set()
    for filename in ["ci.yml", "deploy-staging.yml"]:
        workflow = yaml.safe_load((ROOT / ".github/workflows" / filename).read_text())
        for name, job in workflow["jobs"].items():
            for index, step in enumerate(job["steps"]):
                if "scripts/ci_install_playwright.py" not in step.get("run", ""):
                    continue
                installers.add((filename, name))
                cache = job["steps"][index - 1]
                assert cache["uses"] == "actions/cache@v4"
                assert cache["with"]["path"] == "~/.cache/ms-playwright"
                key = cache["with"]["key"]
                assert f"-{version}-" in key
                assert "runner.os" in key and "runner.arch" in key
                assert "hashFiles('packages/desktop_runtime/pyproject.toml', 'scripts/ci_install_playwright.py')" in key
                assert "restore-keys" not in cache["with"]
                assert "if" not in cache and "if" not in step
                assert "cache-hit" not in step["run"]
                if name in {"unit-shard", "integration-pg-shard"}:
                    assert "require_renderer()" in step["run"]
                keys.add(key)
    assert len(keys) == 1
    assert installers == {("ci.yml", name) for name in
                          ["unit-shard", "integration-pg-shard", "browser-waitlist"]} | {
                              ("deploy-staging.yml", "deploy")}


@pytest.mark.hosted
@pytest.mark.parametrize("job,splits", [("unit", 16), ("integration-pg", 12)])
@pytest.mark.parametrize("domain", ["supplier_orders", "meal_planning"], ids=["b2b", "consumer"])
def test_ci_shard_counts_execute_every_test_once_and_require_every_artifact(evidence, monkeypatch, job, splits, domain):
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    assert workflow["jobs"][job + "-shard"]["strategy"]["matrix"]["shard"] == list(range(1, splits + 1))
    nodeids = sorted(f"tests/{domain}.py::test_scenario[case-{index}]" for index in range(160))
    monkeypatch.setattr(shards, "collect_nodeids", lambda *args: nodeids)
    checkout = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=shards.ROOT, text=True)
    monkeypatch.setattr(shards.subprocess, "check_output", lambda *args, **kwargs: checkout)
    pytest_parts = [sys.executable, "-m", "pytest"]
    execution_prefix = [*pytest_parts, "-q", *EXTRA]
    executed = []

    def execute(command, **kwargs):
        assert command[:len(execution_prefix)] == execution_prefix
        executed.extend(command[len(execution_prefix):])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(shards.subprocess, "run", execute)
    directory = evidence / domain
    prefix = f"{domain}-nodeids"
    for group in range(1, splits + 1):
        nodeids_out = directory / f"{prefix}-2-{group}" / f"shard-{group}.txt"
        assert shards.run_shard(pytest_parts=pytest_parts, paths=PATHS, extra=EXTRA,
                                splits=splits, group=group, nodeids_out=nodeids_out, attest=True) == 0
    assert sorted(executed) == nodeids
    assert verify(directory, splits=splits, artifact_prefix=prefix) == 0
    for group in range(1, splits + 1):
        for suffix in (".txt", "-all.txt", ".json"):
            path = directory / f"{prefix}-2-{group}" / f"shard-{group}{suffix}"
            contents = path.read_bytes()
            path.unlink()
            assert verify(directory, splits=splits, artifact_prefix=prefix) == 1
            path.write_bytes(contents)


def test_run_args_reach_execution_only_not_collection(monkeypatch, tmp_path):
    calls = []

    class Done:
        returncode = 0
        stdout = "tests/a.py::t\n"

    monkeypatch.setattr(shards.subprocess, "run", lambda cmd, **k: calls.append(cmd) or Done())
    shards.run_shard(pytest_parts=["py", "-m", "pytest"], paths=["tests/a.py"], extra=["--durations=20"],
                     splits=1, group=1, nodeids_out=tmp_path / "s.txt", run_args=["-n", "2"])
    collect, execute = calls
    assert "--collect-only" in collect and "-n" not in collect
    assert execute[:6] == ["py", "-m", "pytest", "-q", "-n", "2"] and execute[-1] == "tests/a.py::t"


@pytest.mark.hosted
def test_unit_checkout_retains_immutable_native_baseline_ancestors():
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    checkout = next(step for step in workflow["jobs"]["unit-shard"]["steps"]
                    if step.get("uses") == "actions/checkout@v4")
    # Native custody tests git-show immutable ancestor pins; depth1 omits those
    # actual blobs even when the pinned commit is an ancestor of this exact head.
    assert checkout.get("with", {}).get("fetch-depth") == 0


def test_real_fresh_collection_execution_and_aggregate(evidence, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests/tiny.py").write_text("def test_one(): assert 1 + 1 == 2\ndef test_two(): assert True\n")
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "tests/tiny.py"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.name=CI Test", "-c", "user.email=ci@example.invalid",
                    "commit", "-qm", "fixture"], cwd=repo, check=True)
    sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    monkeypatch.setenv("GITHUB_SHA", sha)
    monkeypatch.setattr(shards, "ROOT", repo)
    output = repo / "artifacts"
    for group in range(1, 4):
        assert shards.run_shard(pytest_parts=[sys.executable, "-m", "pytest"],
                                paths=["tests/tiny.py"], extra=[], splits=3, group=group,
                                nodeids_out=output / f"shard-{group}.txt", attest=True) == 0
    assert verify(output, paths=["tests/tiny.py"], extra=[]) == 0


@pytest.mark.parametrize("corruption", [None, "duplicate_directory", "wrong_attempt", "cross_group_file"])
def test_artifacts_kept_separate_cannot_overwrite_duplicate_evidence(evidence, corruption):
    for group in range(1, 4):
        directory = evidence / f"unit-nodeids-2-{group}"
        directory.mkdir()
        for suffix in (".txt", "-all.txt", ".json"):
            (evidence / f"shard-{group}{suffix}").rename(directory / f"shard-{group}{suffix}")
    if corruption == "duplicate_directory":
        (evidence / "unit-nodeids-2-1-duplicate").mkdir()
    elif corruption == "wrong_attempt":
        (evidence / "unit-nodeids-2-1").rename(evidence / "unit-nodeids-1-1")
    elif corruption == "cross_group_file":
        (evidence / "unit-nodeids-2-1/shard-2.txt").write_text("duplicate")
    assert verify(evidence, artifact_prefix="unit-nodeids") == (0 if corruption is None else 1)


@pytest.mark.parametrize("text", ["not json", "[]", "null", '{"group":1,"group":1}'])
def test_malformed_or_duplicate_receipt_fields_rejected(evidence, text):
    (evidence / "shard-1.json").write_text(text)
    assert verify(evidence) == 1


def test_selected_word_inside_parameter_does_not_silently_drop_a_test():
    nodeid = 'tests/unit/product/test_reviewed_document_text.py::test_exact[Type into the selected TextEdit document.]'
    assert shards._parse_collect(f"{nodeid}\n1 test selected\n") == [nodeid]


@pytest.mark.hosted
def test_pg_owned_runtime_tests_install_declared_desktop_dependencies():
    """Bridge acceptance must work on a clean Linux runner, not local imports."""
    import tomllib
    workflow = yaml.safe_load((ROOT / '.github/workflows/ci.yml').read_text())
    pg = workflow['jobs']['integration-pg-shard']
    install = next(step['run'] for step in pg['steps'] if step.get('name') == 'install')
    assert '-e packages/desktop_runtime' in install
    project = tomllib.loads((ROOT / 'packages/desktop_runtime/pyproject.toml').read_text())['project']
    assert any(requirement.startswith('psutil') for requirement in project['dependencies'])
    paths = workflow['env']['PG_PATHS'].split()
    assert 'tests/integration/test_postgres_research.py' in paths
    assert 'tests/integration/test_postgres_device_resume.py' in paths
    assert 'tests/integration/test_postgres_device_resume_transport.py' in paths


@pytest.mark.hosted
def test_merge_queue_requires_real_heavy_runs_and_prs_may_skip_them():
    text = (ROOT / ".github/workflows/ci.yml").read_text()
    jobs = yaml.safe_load(text)["jobs"]
    for aggregate in ("unit", "authenticated-boundary", "integration-pg"):
        step = jobs[aggregate]["steps"][0]["run"]
        # The skip escape hatch is closed for merge_group, so queue runs need real shards.
        assert 'github.event_name }}" != merge_group' in step
    gate = jobs["gate"]["steps"][1]["run"]
    assert "scripts/ci_" in gate and "\\.github/workflows/" in gate and "ci label" in gate
    assert jobs["browser-waitlist"]["if"] == "needs.gate.outputs.run == 'true'"


@pytest.mark.hosted
@pytest.mark.parametrize("event", ["push", "pull_request", "merge_group"])
def test_gitleaks_event_ranges_preserve_exact_released_history(tmp_path, event):
    import os
    import shlex

    repo = tmp_path / "history"
    repo.mkdir()
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()
    git("init", "-q", "-b", "main")
    git("config", "user.name", "CI fixture")
    git("config", "user.email", "fixture@example.invalid")
    def commit(name):
        (repo / name).write_text(name)
        git("add", name)
        git("commit", "-qm", name)
        return git("rev-parse", "HEAD")
    base = commit("released-base")
    pr_parent = commit("released-parent")
    git("switch", "-qc", "merged-side")
    side = commit("released-side")
    git("switch", "-q", "main")
    main = commit("released-main")
    git("merge", "--no-ff", "--no-edit", "merged-side")
    head = git("rev-parse", "HEAD")
    later = commit("later-main")
    git("switch", "-qc", "unrelated", base)
    unrelated = commit("unmerged-ref")
    step = next(item for item in yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())["jobs"]["security"]["steps"]
                if item.get("name") == "gitleaks")
    captured = tmp_path / "gitleaks-args.txt"
    env = dict(os.environ, EVENT=event, GITHUB_SHA=head, GITHUB_WORKSPACE=str(repo),
               PR_RANGE=base + ".." + head, MQ_RANGE=pr_parent + ".." + head,
               GITLEAKS_ARGS_FILE=str(captured))
    script = 'docker() { printf "%s\n" "$@" > "$GITLEAKS_ARGS_FILE"; }\n' + step["run"]
    subprocess.run(["bash", "-c", script], cwd=repo, env=env, check=True)
    args = captured.read_text().splitlines()
    assert "ghcr.io/gitleaks/gitleaks@sha256:691af3c7c5a48b16f187ce3446d5f194838f91238f27270ed36eef6359a574d9" in args
    assert str(repo) + ":/repo:ro" in args and "--redact" in args
    options = [value.split("=", 1)[1] for value in args if value.startswith("--log-opts=")]
    assert len(options) == 1 and "--all" not in shlex.split(options[0])
    if event == "push":
        assert set(shlex.split(options[0])) == {"--full-history", head}
        expected = {base, pr_parent, side, main, head}
    elif event == "pull_request":
        assert options[0] == env["PR_RANGE"]
        expected = {pr_parent, side, main, head}
    else:
        assert options[0] == env["MQ_RANGE"]
        expected = {side, main, head}
    selected = set(git("rev-list", *shlex.split(options[0])).splitlines())
    assert selected == expected and later not in selected and unrelated not in selected


def _mixed_attempts(evidence, monkeypatch, latest):
    """Shards 1-2 passed in attempt 1; shard 3 in attempt 1 (stale) and/or attempt 2 (rerun)."""
    for group in range(1, 4):
        attempts = [1] if group < 3 else sorted(latest)
        for attempt in attempts:
            directory = evidence / f"unit-nodeids-{attempt}-{group}"
            directory.mkdir()
            for suffix in (".txt", "-all.txt", ".json"):
                src = evidence / f"shard-{group}{suffix}"
                (directory / src.name).write_bytes(src.read_bytes())
            receipt = json.loads((directory / f"shard-{group}.json").read_text())
            receipt["context"]["GITHUB_RUN_ATTEMPT"] = str(attempt)
            (directory / f"shard-{group}.json").write_text(json.dumps(receipt))
    for group in range(1, 4):
        for suffix in (".txt", "-all.txt", ".json"):
            (evidence / f"shard-{group}{suffix}").unlink()


def test_rerun_failed_shard_latest_attempt_per_shard_verifies(evidence, monkeypatch):
    _mixed_attempts(evidence, monkeypatch, latest=[1, 2])
    assert verify(evidence, artifact_prefix="unit-nodeids") == 0


def test_older_attempt_ignored_when_newer_exists(evidence, monkeypatch):
    _mixed_attempts(evidence, monkeypatch, latest=[1, 2])
    (evidence / "unit-nodeids-1-3/shard-3.json").write_text("garbage")
    assert verify(evidence, artifact_prefix="unit-nodeids") == 0


def test_missing_shard_still_fails_with_attempts(evidence, monkeypatch):
    _mixed_attempts(evidence, monkeypatch, latest=[2])
    shutil.rmtree(evidence / "unit-nodeids-2-3")
    assert verify(evidence, artifact_prefix="unit-nodeids") == 1


def test_newest_attempt_must_itself_be_valid(evidence, monkeypatch):
    _mixed_attempts(evidence, monkeypatch, latest=[1, 2])
    (evidence / "unit-nodeids-2-3/shard-3.json").write_text("garbage")
    assert verify(evidence, artifact_prefix="unit-nodeids") == 1
