"""Source/build/staging custody, without desktop calls or expensive compilation."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts/desktop" / (name + ".py"))
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


def test_unreviewed_source_candidate_never_stages(tmp_path, monkeypatch):
    mod = module("prepare_cua_driver")
    monkeypatch.setattr(mod, "REVIEWED_SOURCE_PIN", tmp_path / "absent.json")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: pytest.fail("must refuse before signature/side effect"))
    with pytest.raises(ValueError, match="cua_source_build_not_reviewed"):
        mod.stage_reviewed_source_build(tmp_path)


def test_exact_patch_and_license_pins_are_not_placeholders():
    mod = module("build_cua_modal_candidate")
    pin = json.loads((mod.PIN_DIR / "manifest.json").read_text())
    assert pin["source_commit"] == "7a8f66ad04e62fccb18cca9965f2964fcaee124e"
    assert mod.digest((mod.PIN_DIR / "exact-modal.patch").read_bytes()) == pin["patch_sha256"]
    assert mod.digest((mod.PIN_DIR / "LICENSE.upstream.md").read_bytes()) == pin["license_sha256"]
    assert pin["status"] == "candidate_not_native_validated"
    assert not (mod.PIN_DIR / "reviewed-build.json").exists()


def test_source_tree_hash_binds_all_files_and_refuses_symlinks(tmp_path):
    mod = module("build_cua_modal_candidate")
    (tmp_path / "Cargo.lock").write_text("frozen")
    initial = mod.tree_digest(tmp_path)
    (tmp_path / "Cargo.lock").write_text("changed")
    assert mod.tree_digest(tmp_path) != initial
    (tmp_path / "injected").symlink_to(tmp_path / "Cargo.lock")
    with pytest.raises(ValueError, match="cua_source_symlink_refused"):
        mod.tree_digest(tmp_path)


def test_unknown_source_refuses_before_output_or_patch(tmp_path, monkeypatch):
    mod = module("build_cua_modal_candidate")
    source = tmp_path / "source"
    source.mkdir()
    (source / "Cargo.lock").write_text("wrong")
    output = tmp_path / "output"
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: pytest.fail("unverified source patched"))
    with pytest.raises(ValueError, match="cua_source_pin_mismatch"):
        mod.prepare_source(source, output)
    assert not output.exists()


def test_native_build_requires_disk_headroom_before_cargo(tmp_path, monkeypatch):
    mod = module("build_cua_modal_candidate")
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 1024})())
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: pytest.fail("disk cap ignored"))
    with pytest.raises(ValueError, match="cua_native_build_disk_headroom_required"):
        mod.native_build(tmp_path, tmp_path)


def test_reviewed_manifest_cannot_stage_changed_artifact(tmp_path, monkeypatch):
    mod = module("prepare_cua_driver")
    build = module("build_cua_modal_candidate")
    directory = tmp_path / "candidate"
    directory.mkdir()
    source_pin = json.loads((build.PIN_DIR / "manifest.json").read_text())
    ledger_dir = tmp_path / "pin"
    ledger_dir.mkdir()
    (ledger_dir / "manifest.json").write_text(json.dumps(source_pin))
    receipt = b'{"actual_trial":"reviewed externally"}'
    (directory / "native-trial-receipt.json").write_bytes(receipt)
    candidate = {key: source_pin[key] for key in ["source_commit", "source_tree_sha256", "patch_sha256",
        "cargo_lock_sha256", "protocol", "source_version"]}
    candidate.update(native_build=True, binaries_sha256={"cua-driver": build.digest(b"approved"),
                                                        "cua-cursor-theme": build.digest(b"cursor")})
    data = json.dumps(candidate).encode()
    (directory / "candidate.json").write_bytes(data)
    ledger = {"schema": "supymem_reviewed_cua_build_v1", "candidate_manifest_sha256": build.digest(data),
              "reviewed_native_trial_receipt_sha256": build.digest(receipt),
              "binaries_sha256": candidate["binaries_sha256"]}
    path = ledger_dir / "reviewed-build.json"
    path.write_text(json.dumps(ledger))
    monkeypatch.setattr(mod, "REVIEWED_SOURCE_PIN", path)
    artifact = directory / "target/release/cua-driver"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(b"unreviewed replacement")
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw: pytest.fail("changed artifact reached signature/staging"))
    with pytest.raises(ValueError, match="cua_reviewed_source_artifact_mismatch"):
        mod.stage_reviewed_source_build(directory)


def test_source_gate_runs_actual_press_postcondition_module(tmp_path, monkeypatch):
    mod = module("build_cua_modal_candidate")
    commands = []
    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: commands.append(args))
    mod.scope_test(tmp_path / "source", tmp_path)
    readback = str(tmp_path / "source/crates/platform-macos/src/ax/press_readback.rs")
    assert any(cmd[0] == "rustc" and readback in cmd for cmd in commands)
    assert [str(tmp_path / "press-readback-tests")] in commands
    diagnostics = str(tmp_path / "source/crates/platform-macos/src/ax/press_diagnostic.rs")
    assert any(cmd[0] == "rustc" and diagnostics in cmd for cmd in commands)
    assert [str(tmp_path / "press-diagnostic-tests")] in commands
    ancestry = str(tmp_path / "source/crates/platform-macos/src/ax/ancestry_scope.rs")
    assert any(cmd[0] == "rustc" and ancestry in cmd for cmd in commands)
    assert [str(tmp_path / "ancestry-scope-tests")] in commands


def test_native_tests_share_release_profile_and_preserve_all_gates(tmp_path, monkeypatch):
    mod = module("build_cua_modal_candidate")
    monkeypatch.setattr(mod.shutil, "disk_usage", lambda path: type("Disk", (), {"free": 10 * 1024**3})())
    directory = tmp_path / "target/release"
    directory.mkdir(parents=True)
    for name in ["cua-driver", "cua-cursor-theme"]:
        (directory / name).write_bytes(b"test-artifact")
    calls = []
    monkeypatch.setattr(mod, "compiler_identity", lambda _: "compiler")
    monkeypatch.setattr(mod.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    result = mod.native_build(tmp_path / "source", tmp_path)
    tests = [args for args, _ in calls if args[:2] == ["cargo", "test"]]
    assert len(tests) == 18
    assert all("--release" in args and "--locked" in args and "--lib" in args for args in tests)
    assert {args[args.index("--lib") + 1] for args in tests} == {
        "ax::window_scope", "ax::modal_scope", "ax::press_readback", "ax::press_diagnostic", "tools::get_window_state::tests",
        "tools::press_key::tests::modal_transition_evidence_is_truthful",
        "tools::hotkey::tests::scoped_key_receipt_requires_explicit_modal_readback",
        "ax::ancestry_scope", "ax::exact_target::tests", "ax::control_state",
        "ax::bindings::tests::binary_value_", "ax::action_error",
        "tools::click::tests::typed_ax_error_receipt_is_unconfirmed_and_private",
        "ax::action_receipt", "outputs::tests", "runtime_isolation_tests::canonical_", "owned_modal::tests", "session_manifest::tests::reviewed_modal_"}
    assert {(args[args.index("-p") + 1], args[args.index("--lib") + 1]) for args in tests if args[args.index("-p") + 1] != "platform-macos"} == {
        ("cua-driver-contract", "outputs::tests"), ("cua-driver-core", "runtime_isolation_tests::canonical_"),
        ("cua-driver-core", "owned_modal::tests"), ("cua-driver-core", "session_manifest::tests::reviewed_modal_")}
    assert all(args[-2:] == ["--jobs", "2"] for args, _ in calls)
    assert all(kw["env"]["CARGO_TARGET_DIR"] == str(tmp_path / "target") for _, kw in calls)
    assert all(kw["env"]["CARGO_INCREMENTAL"] == "0" for _, kw in calls)
    assert set(result) == {"cua-driver", "cua-cursor-theme"}


def test_cache_private_ownership_and_external_symlink_refuse(tmp_path):
    mod = module('build_cua_modal_candidate')
    tmp_path.chmod(0o700)
    mod.private_owned_tree(tmp_path)
    (tmp_path / 'escape').symlink_to(tmp_path.parent)
    with pytest.raises(ValueError, match='external_symlink'):
        mod.private_owned_tree(tmp_path)
    (tmp_path / 'escape').unlink()
    tmp_path.chmod(0o755)
    with pytest.raises(ValueError, match='not_private'):
        mod.private_owned_tree(tmp_path)


def test_cache_acceptance_and_existing_artifact_cannot_be_inherited(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    (cache / 'target/release').mkdir(parents=True)
    monkeypatch.setattr(mod, 'cache_idle', lambda _: None)
    (cache / 'candidate.json').write_text('{}')
    with pytest.raises(ValueError, match='accepted_receipt_refused'):
        mod.validate_resume_cache(tmp_path, tmp_path, tmp_path, cache)
    (cache / 'candidate.json').unlink()
    (cache / 'target/release/cua-driver').write_bytes(b'previous binary')
    with pytest.raises(ValueError, match='final_artifact_refused'):
        mod.validate_resume_cache(tmp_path, tmp_path, tmp_path, cache)


def test_cache_busy_compiler_denies_without_signalling(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: type('Result', (), {'stdout': 'rustc rustc --crate-name something'})())
    with pytest.raises(ValueError, match='compiler_busy'):
        mod.cache_idle(tmp_path)


def test_cached_build_cleans_only_firstparty_then_runs_all_fresh_gates(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    target = tmp_path / 'cache/target'
    (target / 'release').mkdir(parents=True)
    output = tmp_path / 'new'
    output.mkdir()
    calls = []
    monkeypatch.setattr(mod, 'cache_idle', lambda _: None)
    monkeypatch.setattr(mod, 'compiler_identity', lambda _: 'compiler-proof')
    monkeypatch.setattr(mod.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 2 * 1024**3})())
    def run(args, **kw):
        calls.append((args, kw))
        if args[:2] == ['cargo', 'build']:
            for name in ['cua-driver', 'cua-cursor-theme']:
                (target / 'release' / name).write_bytes(b'fresh current build')
    monkeypatch.setattr(mod.subprocess, 'run', run)
    target.parent.chmod(0o700)
    (output / 'cache-admission.json').write_text(json.dumps({
        'compiler_sha256': mod.digest(b'compiler-proof'),
        'target_path_sha256': mod.digest(str(target.resolve()).encode()),
        'build_profile': mod.BUILD_PROFILE, 'acceptance_inherited': False}))
    result = mod.native_build(tmp_path / 'source', output, cache_target=target)
    assert calls[0][0] == ['cargo', 'clean', '--release', '-p', 'platform-macos', '-p', 'cua-driver', '-p', 'cursor-theme-cli']
    assert len([args for args, _ in calls if args[:2] == ['cargo', 'test']]) == 18
    assert calls[-1][0][:2] == ['cargo', 'build']
    assert all(kw['env']['CARGO_TARGET_DIR'] == str(target) for _, kw in calls)
    assert result['cua-driver'] == mod.digest(b'fresh current build')
    assert not (output / 'candidate.json').exists()
    assert json.loads((output / 'native-prepared.json').read_text())['acceptance'] == 'pending_root_native_trials'


@pytest.mark.parametrize('tamper,code', [
    ('generated', 'prepared_source_mismatch'), ('source', 'prepared_source_mismatch'),
    ('compiler', 'compiler_mismatch')])
def test_legacy_cache_regeneration_rejects_complete_tree_or_compiler_tamper(tmp_path, monkeypatch, tamper, code):
    mod = module('build_cua_modal_candidate')
    pristine = tmp_path / 'pristine'
    pristine.mkdir()
    (pristine / 'Cargo.lock').write_text('same lock')
    (pristine / 'file').write_text('old\n')
    patch = b'diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+patched\n'
    pin = {'patch_sha256': mod.digest(patch), 'source_tree_sha256': mod.tree_digest(pristine),
           'cargo_lock_sha256': mod.digest(b'same lock')}
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    mod.shutil.copytree(pristine, cache / 'source')
    (cache / 'source/file').write_text('patched\n')
    (cache / 'target').mkdir()
    (cache / 'target/.rustc_info.json').write_text(json.dumps({'outputs': {'v': {
        'success': True, 'stdout': 'different' if tamper == 'compiler' else 'compiler'}}}))
    if tamper == 'generated':
        (cache / 'source/cargo-generated').write_text('must not be silently exempt')
    if tamper == 'source':
        (cache / 'source/file').write_text('edited')
    out = tmp_path / 'new'
    out.mkdir(mode=0o700)
    script = b'fixed reviewed script'
    monkeypatch.setattr(mod, 'LEGACY_BUILD_SCRIPT_SHA256', mod.digest(script))
    monkeypatch.setattr(mod, 'cache_idle', lambda _: None)
    monkeypatch.setattr(mod, 'compiler_identity', lambda _: 'compiler')
    original = mod.subprocess.run
    def run(args, **kw):
        if args[:2] == ['git', 'show']:
            payload = json.dumps(pin).encode() if args[-1].endswith('manifest.json') else (
                patch if args[-1].endswith('exact-modal.patch') else script)
            return type('Result', (), {'stdout': payload})()
        return original(args, **kw)
    monkeypatch.setattr(mod.subprocess, 'run', run)
    with pytest.raises(ValueError, match=code):
        mod.validate_resume_cache(pristine, pristine, out, cache)
    assert not (out / 'cache-admission.json').exists()
    assert not (out / 'candidate.json').exists()


def test_cache_lock_does_not_overwrite_or_remove_another_owner_lock(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    cache = tmp_path / 'cache'
    cache.mkdir(mode=0o700)
    target = cache / 'target'
    target.mkdir()
    lock = cache / '.candidate-build.lock'
    lock.write_bytes(b'other owner')
    out = tmp_path / 'new'
    out.mkdir()
    (out / 'cache-admission.json').write_text(json.dumps({
        'compiler_sha256': mod.digest(b'compiler'), 'target_path_sha256': mod.digest(str(target.resolve()).encode()),
        'build_profile': mod.BUILD_PROFILE, 'acceptance_inherited': False}))
    monkeypatch.setattr(mod, 'compiler_identity', lambda _: 'compiler')
    monkeypatch.setattr(mod, 'cache_idle', lambda _: None)
    with pytest.raises(ValueError, match='build_locked'):
        mod.native_build(tmp_path, out, cache_target=target)
    assert lock.read_bytes() == b'other owner'


def test_preparation_receipt_exists_before_any_native_build_failure(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    source = tmp_path / 'pristine'
    source.mkdir()
    (source / 'Cargo.lock').write_text('frozen')
    (source / 'file').write_text('before\n')
    pins = tmp_path / 'pins'
    pins.mkdir()
    patch = b'diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-before\n+after\n'
    (pins / 'exact-modal.patch').write_bytes(patch)
    (pins / 'LICENSE.upstream.md').write_bytes(b'license')
    (pins / 'manifest.json').write_text(json.dumps({'source_tree_sha256': mod.tree_digest(source),
        'patch_sha256': mod.digest(patch), 'license_sha256': mod.digest(b'license'),
        'cargo_lock_sha256': mod.digest(b'frozen')}))
    monkeypatch.setattr(mod, 'PIN_DIR', pins)
    output = tmp_path / 'new'
    workspace, _ = mod.prepare_source(source, output)
    receipt = json.loads((output / 'source-prepared.json').read_text())
    assert receipt['prepared_tree_sha256'] == mod.tree_digest(workspace)
    assert receipt['native_build'] is False
    assert receipt['acceptance'] == 'pending_root_native_trials'
    monkeypatch.setattr(mod.shutil, 'disk_usage', lambda _: type('Disk', (), {'free': 1})())
    with pytest.raises(ValueError, match='disk_headroom'):
        mod.native_build(workspace, output)
    assert (output / 'source-prepared.json').exists()
    assert not (output / 'candidate.json').exists()


def test_control_semantic_gate_runs_actual_closed_policy_module(tmp_path, monkeypatch):
    mod = module('build_cua_modal_candidate')
    commands = []
    monkeypatch.setattr(mod.subprocess, 'run', lambda args, **kw: commands.append(args))
    mod.scope_test(tmp_path / 'source', tmp_path)
    actual = str(tmp_path / 'source/crates/platform-macos/src/ax/control_state.rs')
    assert any(cmd[0] == 'rustc' and actual in cmd for cmd in commands)
    assert [str(tmp_path / 'control-state-tests')] in commands


@pytest.fixture
def built_cache(tmp_path, monkeypatch):
    """Synthetic attested cache, closed compiler/signature seams; never native proof."""
    from types import SimpleNamespace

    mod = module("build_cua_modal_candidate")
    pristine = tmp_path / "pristine"
    pristine.mkdir()
    (pristine / "Cargo.lock").write_text("same frozen lock")
    (pristine / "Cargo.toml").write_text(
        '[workspace]\nmembers=["crates/platform-macos","crates/cua-driver","crates/cua-driver-contract"]\n'
    )
    for name in ["platform-macos", "cua-driver", "cua-driver-contract"]:
        folder = pristine / "crates" / name
        folder.mkdir(parents=True)
        (folder / "Cargo.toml").write_text(
            f'[package]\nname="{name}"\nversion="0.1.0"\n'
        )
    (pristine / "file").write_text("old\n")
    patch = b"diff --git a/file b/file\n--- a/file\n+++ b/file\n@@ -1 +1 @@\n-old\n+patched\n"
    pin = {
        "schema": "supymem_cua_source_patch_v1",
        "source_commit": "closed_fixture",
        "source_version": "0.29.1",
        "protocol": "supymem_exact_modal_v1",
        "source_tree_sha256": mod.tree_digest(pristine),
        "patch_sha256": mod.digest(patch),
        "cargo_lock_sha256": mod.digest((pristine / "Cargo.lock").read_bytes()),
        "license_sha256": mod.digest(b"closed fixture license"),
        "status": "candidate_not_native_validated",
    }
    pin_dir = tmp_path / "pin"
    pin_dir.mkdir()
    (pin_dir / "manifest.json").write_text(json.dumps(pin))
    (pin_dir / "exact-modal.patch").write_bytes(patch)
    (pin_dir / "LICENSE.upstream.md").write_bytes(b"closed fixture license")
    monkeypatch.setattr(mod, "PIN_DIR", pin_dir)
    monkeypatch.setattr(mod, "_built_cache_files", lambda: (pin, patch))
    cache = tmp_path / "cache"
    workspace, _ = mod.prepare_source(pristine, cache)
    prepared_hash = mod.tree_digest(workspace)
    monkeypatch.setattr(mod, "BUILT_CACHE_PREPARED_SHA256", prepared_hash)
    compiler = "closed compiler identity"
    monkeypatch.setattr(mod, "compiler_identity", lambda _: compiler)
    monkeypatch.setattr(
        mod, "BUILT_CACHE_COMPILER_SHA256", mod.digest(compiler.encode())
    )
    target = cache / "target"
    (target / "release").mkdir(parents=True)
    (target / ".rustc_info.json").write_text(
        json.dumps({"outputs": {"v": {"success": True, "stdout": compiler}}})
    )
    unsigned = {
        name: mod.digest(("unsigned_" + name).encode())
        for name in mod.BUILT_CACHE_UNSIGNED
    }
    signed = {name: mod.digest(("signed_" + name).encode()) for name in unsigned}
    monkeypatch.setattr(mod, "BUILT_CACHE_UNSIGNED", unsigned)
    monkeypatch.setattr(mod, "BUILT_CACHE_SIGNED", signed)
    for name in signed:
        (target / "release" / name).write_bytes(("signed_" + name).encode())
    candidate = {
        k: pin[k]
        for k in [
            "source_commit",
            "source_tree_sha256",
            "patch_sha256",
            "cargo_lock_sha256",
            "protocol",
            "source_version",
        ]
    }
    candidate.update(
        schema="supymem_cua_candidate_build_v1",
        native_build=True,
        acceptance="pending_root_native_trials",
        binaries_sha256=unsigned,
    )
    data = json.dumps(candidate).encode()
    (cache / "candidate.json").write_bytes(data)
    monkeypatch.setattr(mod, "BUILT_CACHE_CANDIDATE_SHA256", mod.digest(data))
    (cache / "native-prepared.json").write_text(
        json.dumps(
            {
                "prepared_tree_sha256": prepared_hash,
                "compiler_sha256": mod.BUILT_CACHE_COMPILER_SHA256,
                "build_profile": mod.BUILD_PROFILE,
                "acceptance": "pending_root_native_trials",
                "cache_only": False,
            }
        )
    )
    signed_receipt = {
        "schema": "supymem_signed_qa_candidate_v1",
        "installed_acceptance": False,
        "activation_allowed": False,
        "unsigned_candidate_manifest_sha256": mod.digest(data),
        "artifacts": {
            name: {
                "sha256": signed[name],
                "unsigned_sha256": unsigned[name],
                "strict_verified": True,
                "team_id": mod.BUILT_CACHE_TEAM,
            }
            for name in unsigned
        },
    }
    (cache / "signed-qa-artifacts.json").write_text(json.dumps(signed_receipt))
    output = tmp_path / "new"
    current, _ = mod.prepare_source(pristine, output)
    monkeypatch.setattr(mod, "cache_idle", lambda _: None)
    original = mod.subprocess.run
    calls = []

    def run(args, **kw):
        calls.append(args)
        if args[0] == "/bin/ps":
            return SimpleNamespace(returncode=0, stdout="")
        if args[0] == "/usr/bin/codesign":
            return SimpleNamespace(
                returncode=0, stderr=f"TeamIdentifier={mod.BUILT_CACHE_TEAM}\n"
            )
        if args[0] == "git":
            return original(args, **kw)
        pytest.fail("Unexpected external/compiler dispatch in closed cache fixture")

    monkeypatch.setattr(mod.subprocess, "run", run)
    return mod, pristine, current, output, cache, calls


def test_built_dependency_cache_admits_only_closed_source_and_signature_facts(
    built_cache,
):
    mod, pristine, workspace, output, cache, calls = built_cache
    assert (
        mod.validate_dependency_cache(pristine, workspace, output, cache)
        == cache / "target"
    )
    receipt = json.loads((output / "dependency-cache-admission.json").read_text())
    assert (
        receipt["reuse_only_dependencies"] is True
        and receipt["acceptance_inherited"] is False
    )
    assert (
        receipt["installed_acceptance"] is False
        and receipt["activation_allowed"] is False
    )
    assert receipt["first_party_packages_to_clean"] == [
        "platform-macos",
        "cua-driver",
        "cua-driver-contract",
    ]
    assert len([c for c in calls if c[0] == "/usr/bin/codesign"]) == 4
    assert not (output / "candidate.json").exists()
    assert not (
        output / "cache-admission.json"
    ).exists()  # legacy lane remains separate


@pytest.mark.parametrize(
    "tamper",
    [
        "source",
        "generated",
        "lock",
        "unsigned",
        "signed_binary",
        "signed_unsigned",
        "signature_team",
        "activated",
        "installed",
        "profile",
        "compiler",
        "rustc_info",
        "missing_signed",
        "missing_native",
        "oversized_receipt",
        "reviewed",
        "external_symlink",
        "internal_binary_symlink",
        "public",
    ],
)
def test_built_dependency_cache_rejects_tampering_and_missing_attestations(
    built_cache, tamper, monkeypatch
):
    mod, pristine, workspace, output, cache, _ = built_cache

    def edit(name, key, value):
        p = cache / name
        data = json.loads(p.read_text())
        data[key] = value
        p.write_text(json.dumps(data))

    if tamper == "source":
        (cache / "source/file").write_text("tampered")
    elif tamper == "generated":
        (cache / "source/generated").write_text("new unbound source")
    elif tamper == "lock":
        (cache / "source/Cargo.lock").write_text("different lock")
    elif tamper == "unsigned":
        edit("candidate.json", "native_build", False)
    elif tamper == "signed_binary":
        (cache / "target/release/cua-driver").write_bytes(b"changed signed binary")
    elif tamper in {"signed_unsigned", "signature_team"}:
        p = cache / "signed-qa-artifacts.json"
        d = json.loads(p.read_text())
        d["artifacts"]["cua-driver"][
            "unsigned_sha256" if tamper == "signed_unsigned" else "team_id"
        ] = "foreign"
        p.write_text(json.dumps(d))
    elif tamper == "activated":
        edit("signed-qa-artifacts.json", "activation_allowed", True)
    elif tamper == "installed":
        edit("signed-qa-artifacts.json", "installed_acceptance", True)
    elif tamper == "profile":
        edit("native-prepared.json", "build_profile", {**mod.BUILD_PROFILE, "jobs": 4})
    elif tamper == "compiler":
        monkeypatch.setattr(mod, "compiler_identity", lambda _: "wrong compiler")
    elif tamper == "rustc_info":
        (cache / "target/.rustc_info.json").write_text(json.dumps({"outputs": {}}))
    elif tamper == "missing_signed":
        (cache / "signed-qa-artifacts.json").unlink()
    elif tamper == "missing_native":
        (cache / "native-prepared.json").unlink()
    elif tamper == "oversized_receipt":
        (cache / "signed-qa-artifacts.json").write_bytes(b"x" * 262145)
    elif tamper == "reviewed":
        (cache / "reviewed-build.json").write_text("{}")
    elif tamper == "external_symlink":
        (cache / "external").symlink_to(pristine)
    elif tamper == "internal_binary_symlink":
        p = cache / "target/release/cua-driver"
        p.rename(p.with_name("old"))
        p.symlink_to(p.with_name("old"))
    elif tamper == "public":
        cache.chmod(0o755)
    with pytest.raises(ValueError):
        mod.validate_dependency_cache(pristine, workspace, output, cache)
    assert not (output / "dependency-cache-admission.json").exists()
    assert not (output / "candidate.json").exists()


def test_built_dependency_cache_rejects_busy_compilers_before_proof(
    built_cache, monkeypatch
):
    mod, pristine, workspace, output, cache, _ = built_cache
    monkeypatch.setattr(
        mod,
        "cache_idle",
        lambda _: (_ for _ in ()).throw(ValueError("cua_cache_compiler_busy")),
    )
    with pytest.raises(ValueError, match="compiler_busy"):
        mod.validate_dependency_cache(pristine, workspace, output, cache)
    assert not (output / "dependency-source-proof").exists()


def test_built_cache_rechecks_before_any_clean_and_never_removes_other_lock(
    built_cache,
):
    mod, pristine, workspace, output, cache, calls = built_cache
    target = mod.validate_dependency_cache(pristine, workspace, output, cache)
    (cache / "source/file").write_text("changed after admission")
    with pytest.raises(ValueError, match="source_mismatch"):
        mod.native_build(workspace, output, dependency_cache_target=target)
    assert not any(c[0] == "cargo" for c in calls)
    assert not (cache / ".candidate-build.lock").exists()
    (cache / ".candidate-build.lock").write_bytes(b"other owner")
    with pytest.raises(ValueError, match="build_locked"):
        mod.native_build(workspace, output, dependency_cache_target=target)
    assert (cache / ".candidate-build.lock").read_bytes() == b"other owner"


def test_built_cache_cleans_all_firstparty_and_reruns_every_native_gate(
    built_cache, monkeypatch
):
    mod, pristine, workspace, output, cache, calls = built_cache
    target = mod.validate_dependency_cache(pristine, workspace, output, cache)
    original = mod.subprocess.run
    monkeypatch.setattr(
        mod.shutil, "disk_usage", lambda _: type("Disk", (), {"free": 2 * 1024**3})()
    )

    def run(args, **kw):
        if args[0] != "cargo":
            return original(args, **kw)
        calls.append(args)
        if args[:2] == ["cargo", "clean"]:
            for name in mod.BUILT_CACHE_SIGNED:
                (target / "release" / name).unlink()
        if args[:2] == ["cargo", "build"]:
            for name in mod.BUILT_CACHE_SIGNED:
                (target / "release" / name).write_bytes(b"fresh firstparty")
        return type("Done", (), {"returncode": 0})()

    monkeypatch.setattr(mod.subprocess, "run", run)
    result = mod.native_build(workspace, output, dependency_cache_target=target)
    native = [c for c in calls if c[0] == "cargo"]
    assert native[0] == [
        "cargo",
        "clean",
        "--release",
        "-p",
        "platform-macos",
        "-p",
        "cua-driver",
        "-p",
        "cua-driver-contract",
    ]
    native_tests = [c for c in native if c[:2] == ["cargo", "test"]]
    assert len(native_tests) >= 13
    assert {
        "ax::window_scope", "ax::modal_scope", "ax::press_readback", "ax::press_diagnostic",
        "tools::get_window_state::tests",
        "tools::press_key::tests::modal_transition_evidence_is_truthful",
        "tools::hotkey::tests::scoped_key_receipt_requires_explicit_modal_readback",
        "ax::ancestry_scope", "ax::exact_target::tests", "ax::control_state",
        "ax::bindings::tests::binary_value_", "ax::action_error",
        "tools::click::tests::typed_ax_error_receipt_is_unconfirmed_and_private",
    }.issubset({c[c.index("--lib") + 1] for c in native_tests})
    assert all("--release" in c and "--locked" in c for c in native[1:])
    assert native[-1][:2] == ["cargo", "build"]
    assert result == {
        name: mod.digest(b"fresh firstparty") for name in mod.BUILT_CACHE_SIGNED
    }
    assert all(
        (output / "target/release" / name).read_bytes() == b"fresh firstparty"
        for name in result
    )
    assert not (output / "candidate.json").exists()
    assert not (cache / ".candidate-build.lock").exists()
    # The same 2GiB headroom does not admit the normal fresh build.
    with pytest.raises(ValueError, match="disk_headroom_required"):
        mod.native_build(workspace, output)


@pytest.fixture(params=["inventory.example", "recipes.example"])
def committed_pin(monkeypatch, request):
    # shortcut: historical refs are absent; restore them for real provenance qualification.
    from types import SimpleNamespace

    mod = module("build_cua_modal_candidate")
    chained = module("cua_chained_cache")
    prefix = "scripts/desktop/vendor/cua-modal-0.29.1/"
    patch = request.param.encode()
    license_text = b"closed fixture license"
    builder = b"closed fixture builder"
    pin = {"patch_sha256": mod.digest(patch), "license_sha256": mod.digest(license_text)}
    blobs = {prefix + "manifest.json": json.dumps(pin).encode(), prefix + "exact-modal.patch": patch,
             prefix + "LICENSE.upstream.md": license_text, "scripts/desktop/build_cua_modal_candidate.py": builder}

    def read(args, **options):
        assert args[:2] == ["git", "show"]
        revision, path = args[2].split(":", 1)
        assert revision in {mod.BUILT_CACHE_REF, chained.REF}
        assert options["check"] and options["capture_output"] and options["timeout"] == 30
        return SimpleNamespace(stdout=blobs[path])

    monkeypatch.setattr(mod.subprocess, "run", read)
    monkeypatch.setattr(mod, "BUILT_CACHE_SCRIPT_SHA256", mod.digest(builder))
    monkeypatch.setattr(chained, "BUILDER", mod.digest(builder))
    monkeypatch.setattr(chained, "PATCH", mod.digest(patch))
    return mod, chained, blobs, prefix


@pytest.mark.parametrize("tamper", [None, "manifest.json", "exact-modal.patch", "LICENSE.upstream.md", "builder"])
def test_trusted_successful_baseline_pin_and_builder_are_verified(committed_pin, tamper):
    mod, _chained, blobs, prefix = committed_pin
    if tamper:
        path = "scripts/desktop/build_cua_modal_candidate.py" if tamper == "builder" else prefix + tamper
        blobs[path] = json.dumps({"patch_sha256": "wrong", "license_sha256": "wrong"}).encode() if tamper == "manifest.json" else b"changed"
        with pytest.raises(ValueError, match="trusted_pin_mismatch"):
            mod._built_cache_files()
    else:
        pin, patch = mod._built_cache_files()
        assert mod.digest(patch) == pin["patch_sha256"]
    assert len(mod.BUILT_CACHE_UNSIGNED) == len(mod.BUILT_CACHE_SIGNED) == 2


def test_built_cache_rejects_in_use_native_qa_binary_before_clean(
    built_cache, monkeypatch
):
    mod, pristine, workspace, output, cache, _ = built_cache
    original = mod.subprocess.run

    def run(args, **kw):
        if args[0] == "/bin/ps":
            return type(
                "Result",
                (),
                {
                    "stdout": f"{cache}/target/release/cua-driver"
                },
            )()
        return original(args, **kw)

    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(ValueError, match="native_busy"):
        mod.validate_dependency_cache(pristine, workspace, output, cache)
    assert not (output / "dependency-source-proof").exists()


def test_built_cache_rejects_actual_signature_verifier_failure(
    built_cache, monkeypatch
):
    mod, pristine, workspace, output, cache, _ = built_cache
    original = mod.subprocess.run

    def run(args, **kw):
        if args[:2] == ["/usr/bin/codesign", "--verify"]:
            return type("Result", (), {"returncode": 1})()
        return original(args, **kw)

    monkeypatch.setattr(mod.subprocess, "run", run)
    with pytest.raises(ValueError, match="signature_mismatch"):
        mod.validate_dependency_cache(pristine, workspace, output, cache)
    assert not (output / "dependency-cache-admission.json").exists()


@pytest.mark.parametrize("change", ["admission", "signed_receipt"])
def test_built_cache_rejects_receipt_drift_under_lock_before_clean(built_cache, change):
    mod, pristine, workspace, output, cache, calls = built_cache
    target = mod.validate_dependency_cache(pristine, workspace, output, cache)
    path = (
        output / "dependency-cache-admission.json"
        if change == "admission"
        else cache / "signed-qa-artifacts.json"
    )
    value = json.loads(path.read_text())
    value[
        "reuse_only_dependencies" if change == "admission" else "permission_status"
    ] = "tampered"
    path.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="admission_changed"):
        mod.native_build(workspace, output, dependency_cache_target=target)
    assert not any(c[0] == "cargo" for c in calls)


def test_built_cache_rejects_changed_current_source(built_cache):
    mod, pristine, workspace, output, cache, _ = built_cache
    (workspace / "file").write_text("new source drift")
    with pytest.raises(ValueError, match="lock_or_source_mismatch"):
        mod.validate_dependency_cache(pristine, workspace, output, cache)
    assert not (output / "dependency-cache-admission.json").exists()


def test_built_cache_requires_private_current_user(built_cache, monkeypatch):
    mod, pristine, workspace, output, cache, _ = built_cache
    monkeypatch.setattr(mod.os, "getuid", lambda: cache.stat().st_uid + 1)
    with pytest.raises(ValueError, match="scope_refused"):
        mod.validate_dependency_cache(pristine, workspace, output, cache)


def test_dependency_cache_cli_requires_build_and_refuses_conflicting_lanes(
    tmp_path, monkeypatch
):
    import sys

    mod = module("build_cua_modal_candidate")
    common = [
        "build",
        "--source-tree",
        str(tmp_path),
        "--output",
        str(tmp_path / "new"),
    ]
    monkeypatch.setattr(
        sys, "argv", common + ["--dependency-cache", str(tmp_path / "old")]
    )
    with pytest.raises(ValueError, match="requires_native_build"):
        mod.main()
    assert not (tmp_path / "new").exists()
    monkeypatch.setattr(
        sys,
        "argv",
        common
        + [
            "--native-build",
            "--dependency-cache",
            str(tmp_path / "old"),
            "--resume-cache",
            str(tmp_path / "legacy"),
        ],
    )
    with pytest.raises(SystemExit):
        mod.main()
    assert not (tmp_path / "new").exists()


def test_dependency_package_parser_requires_new_lane_python_only(tmp_path, monkeypatch):
    import sys
    mod = module("build_cua_modal_candidate")
    monkeypatch.setitem(sys.modules, "tomllib", None)
    with pytest.raises(ValueError, match="cua_dependency_cache_python311_required"):
        mod._workspace_packages(tmp_path)


@pytest.mark.parametrize("mode", ["symlink_alias", "unknown_relative", "missing_absolute", "unrelated_absolute", "space_alias"])
def test_built_cache_native_idle_resolves_executable_custody(tmp_path, monkeypatch, mode):
    mod = module("build_cua_modal_candidate")
    cache = tmp_path / "cache"
    cache.mkdir()
    (cache / "signed").mkdir()
    (cache / "signed/cua-driver").write_bytes(b"synthetic")
    outside = tmp_path / "unrelated/cua-driver"
    outside.parent.mkdir()
    outside.write_bytes(b"synthetic")
    alias = tmp_path / "alias with spaces" if mode == "space_alias" else tmp_path / "alias"
    alias.symlink_to(cache, target_is_directory=True)
    command = {"symlink_alias": str(alias / "signed/cua-driver"),
               "unknown_relative": "cua-driver",
               "missing_absolute": str(tmp_path / "missing/cua-driver"),
               "space_alias": str(alias / "signed/cua-driver"),
               "unrelated_absolute": str(tmp_path / "unrelated/cua-driver")}[mode]
    monkeypatch.setattr(mod, "cache_idle", lambda _: None)
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **kw:
                        type("Result", (), {"stdout": command + "\n"})())
    if mode == "unrelated_absolute":
        mod._dependency_cache_idle(cache)
    else:
        with pytest.raises(ValueError, match="cua_dependency_cache_native_busy"):
            mod._dependency_cache_idle(cache)

@pytest.fixture
def chained_cache(built_cache, monkeypatch):
    import shutil
    b, pristine, workspace, output, cache, calls = built_cache
    monkeypatch.syspath_prepend(str(ROOT/'scripts/desktop'))
    import cua_chained_cache as c
    # First attest the old signed cache through its unchanged admission path.
    b.validate_dependency_cache(pristine,workspace,output,cache)
    old = json.loads((output/'dependency-cache-admission.json').read_text())
    preserved = cache/'preserved-signed-root357'
    preserved.mkdir(mode=0o700)
    for name in ('candidate.json','source-prepared.json','native-prepared.json','signed-qa-artifacts.json'):
        shutil.copyfile(cache/name,preserved/name)
    for name in b.BUILT_CACHE_SIGNED:
        shutil.copyfile(cache/'target/release'/name,preserved/name)
    chain = output.parent/'chain700'
    chain.mkdir(mode=0o700)
    shutil.copytree(cache/'source',chain/'source')
    shutil.copytree(cache/'source',output/'chained-source-proof')
    shutil.copyfile(cache/'LICENSE.upstream.md',chain/'LICENSE.upstream.md')
    pin,patch = b._built_cache_files()
    monkeypatch.setattr(c,'files',lambda _: (pin,patch))
    monkeypatch.setattr(c,'PREPARED',b.tree_digest(chain/'source'))
    unsigned = {name:b.digest(('unsigned700_'+name).encode()) for name in b.BUILT_CACHE_UNSIGNED}
    signed = {name:b.digest(('signed700_'+name).encode()) for name in unsigned}
    monkeypatch.setattr(c,'UNSIGNED',unsigned)
    monkeypatch.setattr(c,'SIGNED',signed)
    prepared = json.loads((cache/'source-prepared.json').read_text())
    native = json.loads((cache/'native-prepared.json').read_text())|{'cache_only':True}
    candidate = json.loads((cache/'candidate.json').read_text())|{'binaries_sha256':unsigned}
    (chain/'target/release').mkdir(parents=True)
    for name in unsigned:
        (cache/'target/release'/name).write_bytes(('unsigned700_'+name).encode())
        (chain/'target/release'/name).write_bytes(('signed700_'+name).encode())
    for name,value in [('source-prepared.json',prepared),('native-prepared.json',native),('candidate.json',candidate),
                       ('dependency-cache-admission.json',old)]:
        (chain/name).write_text(json.dumps(value))
    signed_receipt = {'schema':'supymem_signed_qa_candidate_v1','installed_acceptance':False,
        'activation_allowed':False,'unsigned_candidate_manifest_sha256':b.digest((chain/'candidate.json').read_bytes()),
        'artifacts':{name:{'sha256':signed[name],'unsigned_sha256':unsigned[name],
            'strict_verified':True,'team_id':b.BUILT_CACHE_TEAM} for name in unsigned}}
    (chain/'signed-qa-artifacts.json').write_text(json.dumps(signed_receipt))
    monkeypatch.setattr(c,'RECEIPTS',{name:b.digest((chain/name).read_bytes()) for name in c.RECEIPTS})
    return b,c,pristine,workspace,output,cache,chain,calls


def test_chained_cache_admits_actual_preserved_and_unsigned_and_signed_bytes(chained_cache):
    b,c,_,workspace,output,cache,chain,calls = chained_cache
    receipt = c.facts(b,workspace,output,cache,chain)
    assert receipt['schema']=='supymem_cua_chained_dependency_cache_admission_v1'
    assert receipt['reuse_only_dependencies'] is True
    assert receipt['acceptance_inherited'] is False and receipt['activation_allowed'] is False
    assert receipt['chain_unsigned_binaries_sha256']==c.UNSIGNED
    assert all(cmd[0]!='cargo' for cmd in calls)
    # The old lane stays closed: current target no longer has signed357 bytes.
    with pytest.raises(ValueError,match='signature_mismatch'):
        b._dependency_cache_facts(workspace,output,cache)


@pytest.mark.parametrize('change',['unsigned','signed','preserved','receipt','generated','lock','compiler','source',
    'target_alias','output_alias','active','signature','packages','old_source','old_metadata'])
def test_chained_cache_rejects_causal_chain_break_before_any_cargo(chained_cache,monkeypatch,change):
    from types import SimpleNamespace
    b,c,_,workspace,output,cache,chain,calls = chained_cache
    if change=='unsigned':
        (cache/'target/release/cua-driver').write_bytes(b'wrong')
    elif change=='signed':
        (chain/'target/release/cua-driver').write_bytes(b'wrong')
    elif change=='preserved':
        (cache/'preserved-signed-root357/cua-driver').write_bytes(b'wrong')
    elif change=='receipt':
        (chain/'candidate.json').write_text('{}')
    elif change=='generated':
        (chain/'source/generated.rs').write_text('injected')
    elif change=='lock':
        (chain/'source/Cargo.lock').write_text('unlocked')
    elif change=='compiler':
        monkeypatch.setattr(b,'compiler_identity',lambda _: 'different compiler')
    elif change=='source':
        (output/'chained-source-proof/file').write_text('changed')
    elif change=='target_alias':
        chain=cache
    elif change=='output_alias':
        chain=output
    elif change=='active':
        monkeypatch.setattr(b,'cache_idle',lambda _: (_ for _ in ()).throw(ValueError('cua_cache_compiler_busy')))
    elif change=='signature':
        original=b.subprocess.run
        monkeypatch.setattr(b.subprocess,'run',lambda args,**kw: SimpleNamespace(returncode=1,stderr='') if args[0]=='/usr/bin/codesign' else original(args,**kw))
    elif change=='packages':
        (workspace/'Cargo.toml').write_text('[workspace]\nmembers=[]\n')
    elif change=='old_source':
        (cache/'source/file').write_text('changed')
    elif change=='old_metadata':
        (cache/'preserved-signed-root357/native-prepared.json').write_text('{}')
    calls.clear()
    with pytest.raises(ValueError):
        c.facts(b,workspace,output,cache,chain)
    assert all(cmd[0]!='cargo' for cmd in calls)


def test_chained_cache_revalidates_after_lock_before_cleanup(chained_cache):
    b,c,_,workspace,output,cache,chain,calls = chained_cache
    receipt=c.facts(b,workspace,output,cache,chain)
    (output/'dependency-cache-admission.json').write_text(json.dumps(receipt))
    (chain/'target/release/cua-driver').write_bytes(b'drift after admission')
    calls.clear()
    with pytest.raises(ValueError,match='signature_mismatch'):
        b.native_build(workspace,output,dependency_cache_target=cache/'target')
    assert not (cache/'.candidate-build.lock').exists()
    assert all(cmd[0]!='cargo' for cmd in calls)


@pytest.mark.parametrize("tamper", [None, "manifest.json", "exact-modal.patch", "LICENSE.upstream.md", "builder"])
def test_chained_cache_independent_source_pins_are_verified(committed_pin, tamper):
    builder, chained, blobs, prefix = committed_pin
    if tamper:
        path = "scripts/desktop/build_cua_modal_candidate.py" if tamper == "builder" else prefix + tamper
        blobs[path] = json.dumps({"patch_sha256": "wrong", "license_sha256": "wrong"}).encode() if tamper == "manifest.json" else b"changed"
        with pytest.raises(ValueError, match="chained_cache_pin_mismatch"):
            chained.files(builder)
    else:
        pin, patch = chained.files(builder)
        assert builder.digest(patch) == chained.PATCH == pin["patch_sha256"]


def test_chained_cache_clean_low_headroom_never_compiles(chained_cache,monkeypatch):
    from types import SimpleNamespace
    b,c,_,workspace,output,cache,chain,calls = chained_cache
    receipt=c.facts(b,workspace,output,cache,chain)
    (output/'dependency-cache-admission.json').write_text(json.dumps(receipt))
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _: SimpleNamespace(free=800*1024**2))
    run=b.subprocess.run
    def closed(args,**kw):
        if args[0]=='cargo':
            calls.append(args)
            assert args[1]=='clean', 'No test/build below disk floor'
            return SimpleNamespace(returncode=0)
        return run(args,**kw)
    monkeypatch.setattr(b.subprocess,'run',closed)
    calls.clear()
    with pytest.raises(ValueError,match='disk_headroom'):
        b.native_build(workspace,output,dependency_cache_target=cache/'target')
    cargo=[cmd for cmd in calls if cmd[0]=='cargo']
    assert len(cargo)==1 and cargo[0][1]=='clean'
    assert [cargo[0][i+1] for i,item in enumerate(cargo[0]) if item=='-p']==receipt['first_party_packages_to_clean']
    assert not (output/'candidate.json').exists()
    assert not (cache/'.candidate-build.lock').exists()


def test_original357_low_headroom_still_refuses_before_clean(built_cache,monkeypatch):
    from types import SimpleNamespace
    b,pristine,workspace,output,cache,calls=built_cache
    target=b.validate_dependency_cache(pristine,workspace,output,cache)
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _: SimpleNamespace(free=800*1024**2))
    calls.clear()
    with pytest.raises(ValueError,match='disk_headroom'):
        b.native_build(workspace,output,dependency_cache_target=target)
    assert all(cmd[0]!='cargo' for cmd in calls)


def test_chained_cache_cleanup_recovers_headroom_then_all23_gates(chained_cache,monkeypatch):
    from types import SimpleNamespace
    b,c,_,workspace,output,cache,chain,calls=chained_cache
    receipt=c.facts(b,workspace,output,cache,chain)
    (output/'dependency-cache-admission.json').write_text(json.dumps(receipt))
    state={'cleaned':False}
    monkeypatch.setattr(b.shutil,'disk_usage',lambda _: SimpleNamespace(free=(2*1024**3 if state['cleaned'] else 800*1024**2)))
    run=b.subprocess.run
    def closed(args,**kw):
        if args[0]=='cargo' or args[0]=='rustc' or str(args[0]).startswith(str(output)):
            calls.append(args)
            if args[:2]==['cargo','clean']:
                state['cleaned']=True
                for name in c.UNSIGNED:
                    (cache/'target/release'/name).unlink()
            else:
                assert state['cleaned'], 'All compilation must follow admitted cleanup'
            if args[:2]==['cargo','build']:
                for name in c.UNSIGNED:
                    (cache/'target/release'/name).write_bytes(b'fresh graph build')
            return SimpleNamespace(returncode=0)
        return run(args,**kw)
    monkeypatch.setattr(b.subprocess,'run',closed)
    calls.clear()
    result=b.native_build(workspace,output,dependency_cache_target=cache/'target')
    assert len([cmd for cmd in calls if cmd[0]=='rustc'])==7
    native_tests=[cmd for cmd in calls if cmd[:2]==['cargo','test']]
    assert len(native_tests)>=16
    selectors={cmd[cmd.index('--lib')+1] for cmd in native_tests}
    assert {'outputs::tests','runtime_isolation_tests::canonical_','ax::action_receipt'}.issubset(selectors)
    pin=json.loads((b.PIN_DIR/'manifest.json').read_text())
    if 'crates/cua-driver-core/src/owned_modal.rs' in pin.get('changed_files',[]):
        assert {'owned_modal::tests','session_manifest::tests::reviewed_modal_'}.issubset(selectors)
    assert result=={name:b.digest(b'fresh graph build') for name in c.UNSIGNED}
    assert not (output/'candidate.json').exists()
    assert not (cache/'.candidate-build.lock').exists()


def test_chained_cache_full_source_reconstruction_before_admission(chained_cache):
    b,c,pristine,_,output,cache,chain,_=chained_cache
    fresh=output.parent/'fresh_reconstruction'
    workspace,_=b.prepare_source(pristine,fresh)
    assert c.validate(b,pristine,workspace,fresh,cache,chain)==cache/'target'
    assert b.tree_digest(fresh/'dependency-source-proof')==b.BUILT_CACHE_PREPARED_SHA256
    assert b.tree_digest(fresh/'chained-source-proof')==c.PREPARED
    assert json.loads((fresh/'dependency-cache-admission.json').read_text())['acceptance_inherited'] is False


def test_chained_cache_alias_refused_before_proof_mutation(chained_cache):
    b,c,pristine,workspace,output,cache,_,_=chained_cache
    with pytest.raises(ValueError,match='alias_refused'):
        c.validate(b,pristine,workspace,cache/'would_mutate_cache',cache,output)
    assert not (cache/'would_mutate_cache').exists()


@pytest.mark.parametrize('custody',['unknown_native','public_chain','unowned','external_link'])
def test_chained_cache_unknown_custody_refuses_before_cargo(chained_cache,monkeypatch,custody):
    from types import SimpleNamespace
    b,c,_,workspace,output,cache,chain,calls=chained_cache
    if custody=='unknown_native':
        original=b.subprocess.run
        monkeypatch.setattr(b.subprocess,'run',lambda args,**kw: SimpleNamespace(returncode=0,stdout='cua-driver\n') if args==['/bin/ps','-axo','comm='] else original(args,**kw))
    elif custody=='public_chain':
        chain.chmod(0o755)
    elif custody=='unowned':
        current=b.os.getuid()
        monkeypatch.setattr(b.os,'getuid',lambda:current+1)
    else:
        (chain/'escape').symlink_to(output)
    calls.clear()
    with pytest.raises(ValueError):
        c.facts(b,workspace,output,cache,chain)
    assert all(cmd[0]!='cargo' for cmd in calls)


@pytest.mark.parametrize('key',['RUSTC','CARGO_BUILD_RUSTC','CARGO_BUILD_TARGET','CARGO_BUILD_RUSTFLAGS',
    'CARGO_BUILD_RUSTC_WRAPPER','CARGO_BUILD_RUSTC_WORKSPACE_WRAPPER',
    'CARGO_TARGET_AARCH64_APPLE_DARWIN_RUSTFLAGS','CARGO_TARGET_AARCH64_APPLE_DARWIN_RUNNER',
    'CARGO_TARGET_AARCH64_APPLE_DARWIN_LINKER'])
def test_chained_compiler_override_zero_cleanup_and_build(chained_cache,monkeypatch,key):
    b,c,_,workspace,output,cache,chain,calls=chained_cache
    receipt=c.facts(b,workspace,output,cache,chain)
    (output/'dependency-cache-admission.json').write_text(json.dumps(receipt))
    monkeypatch.setenv(key,'owned_test_override_do_not_emit')
    calls.clear()
    with pytest.raises(ValueError,match='^cua_chained_cache_compiler_override_refused$'):
        c.facts(b,workspace,output,cache,chain)
    with pytest.raises(ValueError,match='^cua_chained_cache_compiler_override_refused$'):
        b.native_build(workspace,output,dependency_cache_target=cache/'target')
    assert not (cache/'.candidate-build.lock').exists()
    assert all(cmd[0] not in {'cargo','rustc'} for cmd in calls)


def test_chained_guard_ignores_empty_and_already_sanitized_common_flags(chained_cache,monkeypatch):
    b,c,_,workspace,output,cache,chain,_=chained_cache
    for key in ['RUSTC','CARGO_BUILD_RUSTC','CARGO_BUILD_TARGET','CARGO_TARGET_TEST_RUSTFLAGS']:
        monkeypatch.setenv(key,'')
    # Existing builder strips these before Cargo; this lane need not widen
    # original357 admission or reject inputs which never reach the compiler.
    for key in ['RUSTFLAGS','CARGO_ENCODED_RUSTFLAGS','RUSTC_WRAPPER','RUSTC_WORKSPACE_WRAPPER']:
        monkeypatch.setenv(key,'already_sanitized')
    assert c.facts(b,workspace,output,cache,chain)['acceptance_inherited'] is False


def test_original357_compiler_override_behavior_unchanged(built_cache,monkeypatch):
    b,pristine,workspace,output,cache,_=built_cache
    monkeypatch.setenv('CARGO_BUILD_TARGET','closed_original_lane_fixture')
    assert b.validate_dependency_cache(pristine,workspace,output,cache)==cache/'target'
