"""The UI review bundle has one Mac identity and cannot enter release output.

Native first-launch matrix, pending the coordinator's Windows/macOS runners:
- Windows 11 standard user, NSIS, offline WebView2, no developer tools: install,
  launch and retry startup; bundled API, worker and console must all become ready.
- Windows spaces, Unicode and long install/data paths: launch from another cwd;
  bundled resources and PATH must resolve without using checkout dependencies.
- Windows Credential Manager: save a model key in Setup, close and reopen;
  connection succeeds without re-entry, plaintext files or secret diagnostics.
- Windows permissive ACLs, foreign ownership and reparse paths: settings/key
  writes refuse unsafe destinations and leave existing files unchanged.
- Windows backup/restore: retain the encrypted key reference, use a stable cwd,
  enforce private restored ACLs and roll back a failed restore without data loss.
- Windows cancellation: stop API, worker and coding descendants; unrelated,
  detached unknown and reused PID identities remain untouched, with no false
  stopped receipt or removal of resources still used by a surviving process.
- macOS Intel/Apple Silicon, signed installed app: save/reopen the same key,
  reach the console without repeated keychain prompts and retain useful redacted
  startup diagnostics; repeat with network disabled after initial key setup.
"""
import importlib.util
from contextlib import nullcontext
from io import BytesIO
import json
import plistlib
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_payload_copies_bundled_coaching_packs_and_fingerprints_them(tmp_path, domain):
    builder = load("coaching_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    source = tmp_path / domain / "app"
    knowledge = source / "knowledge"
    knowledge.mkdir(parents=True)
    names = ("startup_pmf.json.gz", "yc_video_advice.json.gz")
    for name in names:
        shutil.copy2(ROOT / "services/company_os/app/knowledge" / name, knowledge / name)
    (knowledge / "corpus.json.gz").write_bytes(b"private data")
    destination = tmp_path / "payload/services/company_os/app"
    builder.copy_source(source, destination)
    for name in names:
        assert (destination / "knowledge" / name).read_bytes() == (knowledge / name).read_bytes()
    assert not (destination / "knowledge/corpus.json.gz").exists()
    digest = builder.source_digest([source])
    (knowledge / names[0]).write_bytes((knowledge / names[0]).read_bytes() + b"damaged")
    assert builder.source_digest([source]) != digest


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_payload_source_archive_copies_current_config_and_excludes_private_data(tmp_path, monkeypatch, domain):
    builder = load("archive_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    source = tmp_path / domain / "config"
    source.mkdir(parents=True)
    monkeypatch.setattr(builder, "ROOT", source.parent)
    (source / "routing.yaml").write_text(f"domain: {domain}\n")
    hosts = {"local": {"app": "http://127.0.0.1:3100"}, "production": {"app": f"https://{domain}"}}
    (source / "hosts.json").write_text(json.dumps(hosts))
    for directory in ("data", "tests", "fixtures", ".git", "node_modules", "scaffold"):
        (source / directory).mkdir()
        (source / directory / "private.yaml").write_text("private")
    (source / ".env").write_text("private")
    (source / "knowledge").mkdir()
    (source / "knowledge/corpus.json.gz").write_bytes(b"private corpus")
    (source / "linked").symlink_to(source.parent, target_is_directory=True)
    destination = tmp_path / "payload/config"
    builder.copy_source(source, destination)
    assert sorted(path.relative_to(destination).as_posix() for path in destination.rglob("*") if path.is_file()) == ["hosts.json", "routing.yaml"]
    assert json.loads((destination / "hosts.json").read_text()) == {"local": hosts["local"]}
    assert json.loads((source / "hosts.json").read_text()) == hosts
    assert (destination / "routing.yaml").read_text() == f"domain: {domain}\n"
    assert '"git", "ls-files"' not in (ROOT / "scripts/desktop/prepare_local_stack.py").read_text()


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_selected_interpreter_is_kept_for_intel_payload(tmp_path, monkeypatch, domain):
    builder = load("intel_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    root = tmp_path / domain
    root.mkdir()
    selected = root / "intel/bin/python3"
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(root / "intel"))
    monkeypatch.setattr(builder.sys, "executable", str(selected))
    monkeypatch.setattr(builder.platform, "machine", lambda: "x86_64")
    monkeypatch.syspath_prepend(str(ROOT / "scripts/desktop"))
    monkeypatch.setattr(builder, "standalone_python", lambda _target: selected)
    monkeypatch.setattr(builder, "runtime_identity", lambda _binary: "pinned identity")
    monkeypatch.setattr(builder.shutil, "which", lambda _tool: "/tools/uv")
    calls = []

    def run(command, **_kwargs):
        calls.append(command)
        if command[1] == "venv":
            (root / ".desktop-python-x86_64").mkdir()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", run)
    with pytest.raises(SystemExit):
        builder.ensure_build_python()
    assert calls[0][-2:] == ["--python", str(selected)]
    assert calls[0][2] == str(root / ".desktop-python-x86_64")


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_payload_digest_changes_for_source_not_saved_work(tmp_path, domain):
    builder = load("payload_digest", ROOT / "scripts/desktop/prepare_local_stack.py")
    source = tmp_path / domain
    source.mkdir()
    code = source / "page.py"
    code.write_text("first")
    digest = builder.source_digest([source])
    (source / ".env").write_text("private")
    (source / "saved.sqlite3").write_text("private")
    assert builder.source_digest([source]) == digest
    code.write_text("second")
    assert builder.source_digest([source]) != digest


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_console_payload_reuses_build_until_code_changes_and_rejects_private_env(tmp_path, monkeypatch, domain):
    builder = load("cached_console", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    console = tmp_path / "apps/console"
    (console / "public").mkdir(parents=True)
    page = console / "page.tsx"
    page.write_text(domain)
    builds = []

    def build(command, **kwargs):
        builds.append(command)
        assert kwargs["env"]["SUPYMEM_MODE"] == "local"
        assert "LLM_API_KEY" not in kwargs["env"]
        (console / ".next/standalone").mkdir(parents=True, exist_ok=True)
        (console / ".next/standalone/server.js").write_text(page.read_text())
        (console / ".next/static").mkdir(exist_ok=True)

    monkeypatch.setattr(builder.subprocess, "run", build)
    node = tmp_path / "standalone/bin/node"
    builder.prepare_console(node)
    builder.prepare_console(node)
    assert len(builds) == 1
    page.write_text(domain + " changed")
    builder.prepare_console(node)
    assert len(builds) == 2
    assert (tmp_path / "src-tauri/resources/console/apps/console/server.js").read_text() == page.read_text()
    (console / ".env").write_text("private")
    with pytest.raises(SystemExit, match="private console"):
        builder.prepare_console(node)


@pytest.mark.parametrize("domain", ["warehouse.example", "fitness.example"])
def test_runtime_payload_cache_refreshes_code_and_selected_runtime(tmp_path, monkeypatch, domain):
    builder = load("cached_runtime", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setitem(builder.sys.modules, "build_runtime", SimpleNamespace(host_triple=lambda: "native-target"))
    monkeypatch.setattr("sys.argv", [domain, "--runtime-only"])
    node = tmp_path / "node"
    node.write_text("runtime")
    monkeypatch.setattr(builder, "standalone_node", lambda _target: node)
    monkeypatch.setattr(builder, "runtime_identity", lambda binary: str(binary.resolve()) + binary.read_bytes().hex())
    verified = []
    monkeypatch.setattr(builder, "verify_payload", verified.append)
    (tmp_path / "packages").mkdir()
    source = tmp_path / "packages/page.py"
    source.write_text(domain)
    builds = []

    def prepare(target, selected_node):
        builds.append((target, selected_node))
        (tmp_path / "src-tauri/resources/local/runtime" / target).mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(builder, "prepare_runtime", prepare)
    builder.main()
    builder.main()
    assert len(builds) == 1
    source.write_text(domain + " changed")
    builder.main()
    assert len(builds) == 2
    node.write_text("changed runtime")
    builder.main()
    assert len(builds) == 3
    assert verified == ["native-target"] * 4


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("runtime", ["python", "node"])
@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_payload_cache_tracks_runtime_checksum_version_and_resolved_path(tmp_path, monkeypatch, domain, runtime, platform):
    import os

    builder = load("runtime_identity_cache", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "sys", SimpleNamespace(**vars(builder.sys)))
    monkeypatch.setattr(builder.sys, "platform", platform)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setitem(builder.sys.modules, "build_runtime", SimpleNamespace(host_triple=lambda: "native-target"))
    monkeypatch.setattr("sys.argv", [domain, "--runtime-only"])
    python = tmp_path / ("python.exe" if platform == "win32" else "python")
    node = tmp_path / "node"
    for binary in (python, node):
        binary.write_bytes(domain.encode())
    if platform == "win32":
        launcher = tmp_path / "Scripts/python.exe"
        launcher.parent.mkdir()
        launcher.write_bytes(b"venv launcher")
        monkeypatch.setattr(builder.sys, "base_prefix", str(tmp_path))
        monkeypatch.setattr(builder.sys, "executable", str(launcher))
    else:
        monkeypatch.setattr(builder.sys, "executable", str(python))
    monkeypatch.setattr(builder, "standalone_node", lambda _target: node)
    version = {"python": "Python 3.12.16", "node": "v22.23.4"}
    monkeypatch.setattr(builder.subprocess, "run", lambda command, **_kwargs:
        SimpleNamespace(returncode=0, stdout=version["node" if Path(command[0]).name == "node" else "python"], stderr=""))
    marker = tmp_path / "src-tauri/resources/local/runtime/native-target/.source-digest"
    builds = []

    def prepare(*_args):
        builds.append(True)
        marker.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(builder, "prepare_runtime", prepare)
    monkeypatch.setattr(builder, "verify_payload", lambda _target: None)
    builder.main()
    builder.main()
    assert len(builds) == 1
    binary = python if runtime == "python" else node
    status = binary.stat()
    binary.write_bytes(b"x" * status.st_size)
    os.utime(binary, ns=(status.st_atime_ns, status.st_mtime_ns))
    builder.main()
    assert len(builds) == 2
    version[runtime] += " changed"
    builder.main()
    assert len(builds) == 3
    moved = tmp_path / "moved" / runtime
    moved.parent.mkdir()
    binary.rename(moved)
    binary.symlink_to(moved)
    builder.main()
    assert len(builds) == 4


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("with_uv", [False, True])
def test_homebrew_payload_python_is_replaced_without_changing_source_venv(tmp_path, monkeypatch, domain, with_uv):
    builder = load("standalone_selection", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "sys", SimpleNamespace(**vars(builder.sys)))
    root = tmp_path / domain
    source = root / ".venv"
    source.mkdir(parents=True)
    (source / "saved-source-venv").write_text(domain)
    homebrew = root / "Cellar/python@3.12/3.12.9/Frameworks/Python.framework/Versions/3.12"
    standalone = root / "cache/python/bin/python3"
    standalone.parent.mkdir(parents=True)
    standalone.write_bytes(b"standalone")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(source))
    monkeypatch.setattr(builder.sys, "base_prefix", str(homebrew))
    monkeypatch.setattr(builder.sys, "executable", str(homebrew / "bin/python3"))
    monkeypatch.setattr(builder.sys, "argv", ["prepare_local_stack.py", "--runtime-only"])
    monkeypatch.setattr(builder, "standalone_python", lambda _target: standalone)
    monkeypatch.syspath_prepend(str(ROOT / "scripts/desktop"))
    monkeypatch.setattr(builder, "runtime_identity", lambda _binary: "standalone identity")
    monkeypatch.setattr(builder.shutil, "which", lambda _tool: "/tools/uv" if with_uv else None)
    commands = []
    environment = root / f".desktop-python-{builder.platform.machine()}"

    def run(command, **_kwargs):
        commands.append(command)
        if "venv" in command:
            (environment / "bin").mkdir(parents=True)
            (environment / "bin/python").touch()
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(builder.subprocess, "run", run)
    with pytest.raises(SystemExit) as stopped:
        builder.ensure_build_python()
    assert stopped.value.code == 0
    assert str(standalone) in commands[0]
    assert not any(str(homebrew) in str(command) for command in commands)
    assert (source / "saved-source-venv").read_text() == domain
    assert len([command for command in commands if "--require-hashes" in command]) == 3


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("target", ["aarch64-apple-darwin", "x86_64-apple-darwin",
    "x86_64-unknown-linux-gnu", "aarch64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
@pytest.mark.parametrize("scenario", ["warm", "cold_offline", "bad_download", "bad_cache", "unsafe_archive", "missing_pin", "relative_symlink"])
def test_standalone_python_download_is_pinned_verified_and_cached(tmp_path, monkeypatch, domain, target, scenario):
    import hashlib
    import io
    import tarfile
    import urllib.error

    builder = load("standalone_python_download", ROOT / "scripts/desktop/prepare_local_stack.py")
    filename = f"cpython-{builder.PYTHON_VERSION}+{builder.PYTHON_BUILD}-{target}-install_only_stripped.tar.gz"
    executable = "python.exe" if "windows" in target else "bin/python3"
    content = domain.encode()
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w:gz") as output:
        members = [executable, "LICENSE.txt", "lib/python3.12/os.py"]
        if scenario == "relative_symlink":
            members.insert(0, executable + ".real")
        for member in members:
            info = tarfile.TarInfo("python/" + member)
            if scenario == "unsafe_archive" and member == executable:
                info.type = tarfile.SYMTYPE
                info.linkname = "/opt/homebrew/bin/python3"
            elif scenario == "relative_symlink" and member == executable:
                info.type = tarfile.SYMTYPE
                info.linkname = Path(executable + ".real").name
            else:
                info.size = len(content)
            output.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive.getvalue()).hexdigest()
    sums = tmp_path / "SHASUMS256.txt"
    sums.write_text("" if scenario == "missing_pin" else f"{checksum}  {filename}\n")
    monkeypatch.setattr(builder, "PYTHON_SHASUMS", sums)
    cache = tmp_path / domain / "cache"
    monkeypatch.setattr(builder, "python_cache_dir", lambda: cache)
    calls = []

    def download(url, **_kwargs):
        calls.append(url)
        if scenario == "cold_offline" or len(calls) > 1:
            raise urllib.error.URLError("offline")
        assert url == f"https://github.com/astral-sh/python-build-standalone/releases/download/{builder.PYTHON_BUILD}/{filename.replace('+', '%2B')}"
        return io.BytesIO(b"bad archive" if scenario == "bad_download" else archive.getvalue())

    monkeypatch.setattr(builder.urllib.request, "urlopen", download)
    if scenario in {"cold_offline", "bad_download", "unsafe_archive", "missing_pin"}:
        with pytest.raises(SystemExit, match="./start"):
            builder.standalone_python(target)
        assert len(calls) == (0 if scenario == "missing_pin" else 1)
        if scenario == "bad_download":
            assert not (cache / filename).exists()
        return
    python = builder.standalone_python(target)
    assert python.read_bytes() == content
    assert python.stat().st_mode & 0o111
    if scenario == "bad_cache":
        (cache / filename).write_bytes(b"tampered archive")
        with pytest.raises(SystemExit, match="SHA-256.*./start"):
            builder.standalone_python(target)
    else:
        base = python.parent if "windows" in target else python.parent.parent
        bytecode = base / "lib/python3.12/__pycache__"
        bytecode.mkdir()
        (bytecode / "os.cpython-312.pyc").write_bytes(b"generated bytecode")
        with monkeypatch.context() as warm:
            warm.setattr(builder.tarfile, "open", lambda *_args, **_kwargs: pytest.fail("Warm verified cache must not unpack again"))
            assert builder.standalone_python(target) == python
        assert not bytecode.exists()
        python.write_bytes(b"tampered executable")
        python = builder.standalone_python(target)
        assert python.read_bytes() == content
        (base / "lib/python3.12/os.py").write_text("tampered stdlib")
        assert builder.standalone_python(target) == python
        assert (base / "lib/python3.12/os.py").read_bytes() == content
    assert len(calls) == 1


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_desktop_identity_is_consistent_and_separate_from_review(domain):
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    info = plistlib.loads((ROOT / "src-tauri/Info.local.plist").read_bytes())
    assert config["identifier"] == "com.supymem.local"
    assert info["CFBundleShortVersionString"] == info["CFBundleVersion"] == config["version"].partition("-")[0]
    assert info["SupymemReleaseVersion"] == config["version"]
    supervisor = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    assert '.env("SUPYMEM_APP_VERSION", env!("SUPYMEM_DESKTOP_VERSION"))' in supervisor
    assert info["CFBundleURLTypes"][0]["CFBundleURLName"] == config["identifier"]
    assert domain not in config["identifier"]
    review = (ROOT / "scripts/desktop/prepare_local_review.py").read_text()
    assert '"identifier": "com.companyos.localreview"' in review


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_launcher_resolves_services_inside_frozen_bundle(tmp_path, monkeypatch, domain):
    import sys

    bundle = tmp_path / domain
    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    runtime = load("local_runtime", ROOT / "packages/desktop_runtime/__main__.py")
    assert runtime.REPO_ROOT == bundle


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("version", ["1.2.3", "1.0.0-rc.1"])
def test_local_release_never_publishes_auto_install_metadata(tmp_path, domain, version):
    import io
    import tarfile

    release = load("prepare_local_release", ROOT / "scripts/desktop/prepare_local_release.py")
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    archive = tmp_path / f"{domain}.app.tar.gz"
    numeric = version.partition("-")[0]
    info = {"CFBundleIdentifier": config["identifier"], "CFBundleShortVersionString": numeric,
            "CFBundleVersion": numeric, "SupymemReleaseVersion": version,
            "LSEnvironment": {"SUPYMEM_MODE": "local"}}
    def write_archive():
        with tarfile.open(archive, "w:gz") as contents:
            body = plistlib.dumps(info)
            member = tarfile.TarInfo(f"{domain}.app/Contents/Info.plist")
            member.size = len(body)
            contents.addfile(member, io.BytesIO(body))

    write_archive()
    assert release.prepare(tmp_path, version, "owner/project") is None
    assert not (tmp_path / "latest.json").exists()
    archive.with_name(archive.name + ".sig").write_text("fixture-public-signature")
    (tmp_path / "latest.json").write_text("stale automatic updater")
    assert release.prepare(tmp_path, version, "owner/project") is None
    assert not (tmp_path / "latest.json").exists()
    with pytest.raises(ValueError, match="version"):
        release.prepare(tmp_path, "1.2.4", "owner/project")
    with pytest.raises(ValueError, match="version"):
        release.prepare(tmp_path, f"{numeric}-rc.2", "owner/project")
    info["CFBundleVersion"] = f"{numeric}-rc.2"
    write_archive()
    with pytest.raises(ValueError, match="version"):
        release.prepare(tmp_path, version, "owner/project")
    info["CFBundleVersion"] = numeric
    info["LSEnvironment"] = {"SUPYMEM_MODE": "hosted"}
    write_archive()
    with pytest.raises(ValueError, match="local"):
        release.prepare(tmp_path, version, "owner/project")


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_publisher_accepts_trusted_tag_installers(tmp_path, monkeypatch, domain):
    release = load("prepare_tag_release", ROOT / "scripts/desktop/prepare_local_release.py")
    root = tmp_path / "source"
    (root / "src-tauri").mkdir(parents=True)
    version = "1.0.0-rc.1"
    (root / "src-tauri/tauri.local.json").write_text(json.dumps({"version": version}))
    monkeypatch.setattr(release, "ROOT", root)
    artifacts = tmp_path / "artifacts"
    for folder, suffix in (("dmg", "aarch64.dmg"), ("deb", "amd64.deb"),
                           ("appimage", "amd64.AppImage"), ("nsis", "x64-setup.exe")):
        path = artifacts / folder / f"{domain}_{version}_{suffix}"
        path.parent.mkdir(parents=True)
        path.write_bytes(b"trusted build artifact")
    (artifacts / "latest.json").write_text("stale updater metadata")
    assert release.prepare(artifacts, version, "owner/project") is None
    assert not (artifacts / "latest.json").exists()
    with pytest.raises(ValueError, match="version"):
        release.prepare(artifacts, "1.0.0-rc.2", "owner/project")
    wrong = artifacts / "deb" / f"{domain}_0.9.0_amd64.deb"
    wrong.write_bytes(b"wrong version")
    with pytest.raises(ValueError, match="version"):
        release.prepare(artifacts, version, "owner/project")
    wrong.unlink()
    for path in artifacts.rglob("*.*"):
        path.unlink()
    outside = tmp_path / f"{domain}_{version}_amd64.deb"
    outside.write_bytes(b"outside your trusted artifacts")
    (artifacts / "deb" / outside.name).symlink_to(outside)
    with pytest.raises(ValueError, match="installer|archive"):
        release.prepare(artifacts, version, "owner/project")
    if (ROOT / ".github/workflows/ci.yml").is_file():
        workflow = (ROOT / ".github/workflows/desktop-local-release.yml").read_text()
        assert 'pattern: desktop-desktop-v${{ env.RELEASE_VERSION }}-*' in workflow
        assert 'run["path"] == ".github/workflows/desktop-release.yml"' in workflow
        assert 'run["head_branch"] == "desktop-v" + version' in workflow
        assert 'gh release create "desktop-v$RELEASE_VERSION"' in workflow
        build = yaml.load((ROOT / ".github/workflows/desktop-release.yml").read_text(), Loader=yaml.BaseLoader)
        assert build["jobs"]["build"]["env"]["SUPYMEM_OSS_REPOSITORY"] == "${{ github.repository }}"


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_desktop_installers_use_hashed_runtime_and_keychain_dependencies(tmp_path, domain):
    import tomllib
    from packaging.requirements import Requirement

    lock = ROOT / "packages/desktop_runtime/requirements.local.lock"
    copied = tmp_path / domain
    copied.mkdir()
    (copied / lock.name).write_text(lock.read_text())
    blocks = (copied / lock.name).read_text().splitlines()
    locked = {}
    for index, line in enumerate(blocks):
        if not line or line.startswith(("#", " ")):
            continue
        requirement = Requirement(line.rstrip(" \\"))
        assert str(requirement.specifier).startswith("==")
        assert blocks[index + 1].strip().startswith("--hash=sha256:")
        locked[requirement.name.lower().replace("_", "-")] = requirement
    project = tomllib.loads((ROOT / "packages/desktop_runtime/pyproject.toml").read_text())["project"]
    for value in project["dependencies"] + project["optional-dependencies"]["local-stack"]:
        requirement = Requirement(value)
        pinned = locked[requirement.name.lower().replace("_", "-")]
        assert next(iter(pinned.specifier)).version in requirement.specifier
    install = "--require-hashes -r packages/desktop_runtime/requirements.local.lock"
    for path in (".github/workflows/desktop-release.yml", "scripts/desktop/build_mac.sh"):
        if path == ".github/workflows/desktop-release.yml" and not (ROOT / ".github/workflows/ci.yml").is_file():
            continue
        installer = (ROOT / path).read_text()
        assert installer.count(install) == 2
        assert "'./packages/desktop_runtime[local-stack]'" not in installer
    public_workflow = (ROOT / ".github/workflows/public-ci.yml").read_text()
    assert "'./packages/desktop_runtime[local-stack]'" not in public_workflow
    jobs = yaml.safe_load(public_workflow)["jobs"]
    for name in ("python", "offline-value-journey"):
        steps = jobs[name]["steps"]
        assert sum(step.get("run", "").count(install) for step in steps) == 1, name


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_splash_marks_need_no_private_artwork(domain):
    screen = ROOT / "src-tauri/splashscreen"
    for name in ("local-starting.html", "local-error.html"):
        html = (screen / name).read_text()
        assert 'role="img" aria-label="Charlie"' in html, name
        assert "url(" not in html and "<img" not in html, name
        assert domain not in html


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_bundled_backend_entrypoint_loads_dependencies_before_every_role(tmp_path, domain):
    import re
    import sys

    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    bootstrap = re.search(r'command.args\(\["-I", "-c", r#"(.*?)"#\]\)', source, re.S).group(1)
    root = tmp_path / domain
    runtime = root / "runtime"
    executable = runtime / ("python.exe" if sys.platform == "win32" else "bin/python3")
    site = runtime / ("Lib/site-packages" if sys.platform == "win32" else
                      f"lib/python{sys.version_info.major}.{sys.version_info.minor}/site-packages")
    site.mkdir(parents=True)
    (site / "bundled_dependency.py").write_text(f"domain = {domain!r}\n")
    settings = root / "services/company_os/app/core"
    settings.mkdir(parents=True)
    (settings / "local_env_import.py").write_text("def import_local_env():\n    pass\n")
    package = root / "packages/desktop_runtime"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "parent_watch.py").write_text("def exit_when_parent_dies():\n    pass\n")
    (package / "crash_report.py").write_text("def install_exception_hooks():\n    pass\n")
    (package / "__main__.py").write_text(
        "import json, sys\nfrom bundled_dependency import domain\n"
        "def main():\n    print(json.dumps([domain, sys.argv[1:]]))\n")
    shutil.copy2(ROOT / "scripts/desktop/local_core.py", root / "launcher.py")
    for role in ("maintenance", "core", "worker", "bridge"):
        arguments = ["--role", "core" if role == "maintenance" else role, "--host", "127.0.0.1", "--port", "3500", "--data-dir", str(root)]
        if role == "maintenance":
            arguments.append("--maintenance")
        result = subprocess.run([sys.executable, "-I", "-c",
                                 f"import sys\nsys.executable = {str(executable)!r}\n" + bootstrap,
                                 str(root), *arguments], env={}, capture_output=True, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == [domain, arguments]


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_sidecars_forward_explicit_sample_settings_without_secrets_or_new_defaults(tmp_path, domain):
    import re

    source = (ROOT / "src-tauri/src/local_stack.rs").read_text().split("fn command(&self", 1)[1]
    assert "command.env_clear();" in source
    names = json.loads(re.search(r"for name in (\[.*?\]) \{", source, re.S).group(1))
    settings = {"LLM_OFFLINE": "1", "LLM_CASSETTE_MODE": "replay", "GUARDRAILS_MODE": "deterministic_only"}
    environment = {"HOME": str(tmp_path / domain), **settings}
    forwarded = {name: environment[name] for name in names if name in environment}
    assert forwarded == environment
    assert not {"OPENAI_API_KEY", "PYTHONPATH", "PYTHONHOME", "NODE_OPTIONS", "NODE_PATH"}.intersection(names)
    environment = {"HOME": str(tmp_path / domain)}
    assert {name: environment[name] for name in names if name in environment} == environment


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_payload_includes_declared_keychain_extra_and_transitive_dependencies(tmp_path, monkeypatch, domain):
    builder = load("prepare_keychain_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    inputs = tmp_path / "services/company_os"
    inputs.mkdir(parents=True)
    (inputs / "requirements.runtime.in").write_text("")
    (inputs / "pyproject.toml").write_text('[project]\ndependencies = []\n')
    project = tmp_path / "packages/desktop_runtime"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text('''[project]
name = "desktop-runtime"
version = "0.1.0"
dependencies = []
[project.optional-dependencies]
local-stack = ["keyring>=25,<26"]
''')
    site = tmp_path / domain
    site.mkdir()
    dependencies = {"keyring": ["SecretStorage", "jaraco.classes"],
                    "SecretStorage": ["jeepney"], "jaraco.classes": ["more-itertools"],
                    "jeepney": [], "more-itertools": []}
    distributions = {}
    for name, requires in dependencies.items():
        entry = Path(name) / "__init__.py"
        (site / entry).parent.mkdir()
        (site / entry).write_text(domain)
        distributions[name] = SimpleNamespace(metadata={"Name": name}, version="25.0.0",
            requires=requires, files=[entry], locate_file=lambda entry: site / entry)
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.sysconfig, "get_path", lambda _name: str(site))
    monkeypatch.setattr(builder.metadata, "distribution", distributions.__getitem__)
    destination = tmp_path / "payload"
    inventory = builder.copy_dependencies(destination)
    assert {row["name"] for row in inventory} == {"desktop-runtime", *dependencies}
    for name in dependencies:
        assert (destination / name / "__init__.py").read_text() == domain
    supervisor = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    for name in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        assert f'"{name}"' in supervisor
        assert f'"{name}"' in (ROOT / "scripts/desktop/smoke_local_stack.py").read_text()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_loading_progress_failure_and_retry_are_visible(tmp_path, domain):
    script = ROOT / "src-tauri/splashscreen/local-error.js"
    result = subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = Object.fromEntries(['status', 'progress', 'retry', 'heading', 'log-path', 'failure-help', 'failure-details'].map(name =>
  [name, { textContent: '', hidden: false, disabled: false, addEventListener(event, handler) { this[event] = handler; } }]));
const calls = [];
let checks = 0;
let scheduled;
const scope = { document: { getElementById: name => elements[name] }, URLSearchParams,
  window: { location: { search: '?starting=1&log=' + encodeURIComponent(process.argv[2]) },
    __TAURI__: { core: { invoke: async command => {
      calls.push(command);
      if (command === 'backend_status') {
        checks++;
        return [{ role: 'core', healthy: true }, { role: 'worker', healthy: checks > 1 }, { role: 'console', healthy: false }];
      }
      throw new Error('private internal failure');
    } } } }, setTimeout: callback => { scheduled = callback; return 1; }, clearTimeout() {} };
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), scope);
(async () => {
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['log-path'].textContent, process.argv[2]);
  assert.equal(elements.progress.value, 1);
  assert.equal(elements.progress.max, 3);
  await scheduled();
  assert.equal(elements.progress.value, 2);
  elements.retry.hidden = false;
  const pending = elements.retry.click();
  elements.retry.click();
  await pending;
  assert.equal(calls.filter(command => command === 'desktop_retry_console').length, 1);
  assert.match(elements.status.textContent, /Retry/);
  assert.equal(elements.status.textContent.includes('private internal'), false);
  assert.equal(elements.retry.disabled, false);
})().catch(error => { console.error(error); process.exitCode = 1; });
""", str(script), str(tmp_path / domain / "desktop.log")], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("manifest", ["missing", "invalid"])
def test_standalone_node_without_a_pinned_checksum_fails_closed(tmp_path, monkeypatch, domain, manifest):
    builder = load("node_pin_missing", ROOT / "scripts/desktop/prepare_local_stack.py")
    sums = tmp_path / domain / "SHASUMS256.txt"
    sums.parent.mkdir()
    if manifest == "invalid":
        sums.write_text(f"not-a-hash  node-v{builder.NODE_VERSION}-darwin-arm64.tar.gz\n")
    monkeypatch.setattr(builder, "NODE_SHASUMS", sums)
    monkeypatch.setattr(builder.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("An unpinned download must not start"))
    with pytest.raises(SystemExit, match="Save the official Node.*./start"):
        builder.standalone_node("aarch64-apple-darwin")


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_failed_verification_invalidates_even_a_warm_payload_marker(tmp_path, monkeypatch, domain):
    builder = load("payload_reverify", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setitem(builder.sys.modules, "build_runtime", SimpleNamespace(host_triple=lambda: "native-target"))
    monkeypatch.setattr(builder.sys, "argv", [domain, "--runtime-only"])
    node = tmp_path / "node"
    node.write_text(domain)
    monkeypatch.setattr(builder, "standalone_node", lambda _target: node)
    monkeypatch.setattr(builder, "runtime_identity", lambda binary: str(binary.resolve()) + binary.read_bytes().hex())
    marker = tmp_path / "src-tauri/resources/local/runtime/native-target/.source-digest"
    builds = []

    def prepare(*_args):
        builds.append(True)
        marker.parent.mkdir(parents=True, exist_ok=True)

    monkeypatch.setattr(builder, "prepare_runtime", prepare)
    monkeypatch.setattr(builder, "verify_payload", lambda _target: None)
    builder.main()
    assert marker.is_file()

    def broken(_target):
        raise SystemExit("Your runtime failed. Rebuild with ./start.")

    monkeypatch.setattr(builder, "verify_payload", broken)
    with pytest.raises(SystemExit, match="./start"):
        builder.main()
    assert len(builds) == 1
    assert not marker.exists()
    monkeypatch.setattr(builder, "verify_payload", lambda _target: None)
    builder.main()
    assert len(builds) == 2


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_payload_verification_resolves_macos_runtime_rpaths(tmp_path, monkeypatch, domain):
    builder = load("macos_payload_rpaths", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.sys, "platform", "darwin")
    monkeypatch.setattr(builder.shutil, "which", lambda _tool: "/usr/bin/otool")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", str(tmp_path / domain / "external"))
    target = "aarch64-apple-darwin"
    runtime = tmp_path / "src-tauri/resources/local/runtime" / target
    for member in ("bin/node", "bin/python3", "lib/libpython3.12.dylib"):
        path = runtime / member
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert "DYLD_LIBRARY_PATH" not in kwargs["env"]
        if Path(command[0]).name == "node":
            output = f"v{builder.NODE_VERSION}"
        elif command[0] != "/usr/bin/otool":
            output = ""
        elif command[1] == "-l":
            output = "Load command 1\n          cmd LC_RPATH\n      cmdsize 48\n         path @loader_path/../lib (offset 12)\n"
        else:
            dependency = "@rpath/libpython3.12.dylib" if command[-1].endswith("python3") else "/usr/lib/libSystem.B.dylib"
            output = f"{command[-1]}:\n\t{dependency} (compatibility version 1.0.0, current version 1.0.0)"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(builder.subprocess, "run", run)
    builder.verify_payload(target)
    assert any(command[1] == "-l" for command in commands)


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_supervisor_diagnostics_are_merged_and_keep_redaction(domain):
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    diagnostics = source.split("fn failed_child_stderr_reaches_the_page_and_log_only_after_redaction()", 1)[1].split("\n    #[test]", 1)[0]
    assert f'"{domain}"' in diagnostics
    assert 'assert!(message.contains("./start"))' in diagnostics
    assert 'assert!(!message.contains("short-private-value"))' in diagnostics
    assert 'assert!(!std::fs::read_to_string(&stack.log_path).unwrap().contains("short-private-value"))' in diagnostics


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_desktop_secures_existing_directories_before_opening_logs(domain):
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    startup = source.split("fn start_inner(&self,", 1)[1].split("    fn ", 1)[0]
    assert startup.index("private_directory(&self.data_dir)") < startup.index("self.log_file()?")
    log_writer = source.split("fn log_file(&self)", 1)[1].split("\n    fn ", 1)[0]
    assert log_writer.index('private_directory(self.log_path.parent().expect("log directory"))') < log_writer.index("options.open(&self.log_path)")
    assert "libc::O_NOFOLLOW | libc::O_DIRECTORY" in source
    assert "metadata.uid() != unsafe { libc::geteuid() }" in source
    directory_test = source.split("fn local_directories_preserve_both_domains_and_reject_symlinks()", 1)[1].split("\n    fn ", 1)[0]
    assert f'"{domain}"' in directory_test


def test_local_supervisor_health_and_recovery_never_use_owner_authentication():
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    for phase, call in (("start_inner", "self.probe_ready(index, &client)"),
                        ("restart_role", "self.wait_for_replacement(index, &mut replacement, &client)"),
                        ("check_health", "self.probe_ready(index, &client)"),
                        ("wait_for_replacement", "self.probe_ready(index, client)")):
        probe = source.split(f"fn {phase}(&self", 1)[1].split("\n    fn ", 1)[0]
        assert call in probe
        assert "owner_secret" not in probe and "X-Local-Proxy-Secret" not in probe
    readiness = source.split("fn probe_ready(&self", 1)[1].split("\n    pub", 1)[0]
    assert '.header("X-Local-Ready-Nonce", &nonce)' in readiness
    assert 'readiness_proven(response.status(), response.headers(), &self.readiness_keys[index], &nonce)' in readiness
    assert "X-Local-Proxy-Secret" not in readiness


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_native_first_run_smoke_uses_the_setup_screen_provider_route(domain):
    import re

    console = (ROOT / "apps/console/src/app/setup/localProviders.ts").read_text()
    route = re.search(r'providerRequest\(path = "([^"]+)"', console).group(1)
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text().split("pub fn smoke(", 1)[1]
    assert f'format!("{{}}{route}", stack.url("console"))' in source
    assert domain not in route


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_fallback_uses_local_version_and_tauri_resource_name(tmp_path, monkeypatch, domain):
    builder = load("desktop_linux_bundle", ROOT / "scripts/desktop/bundle_linux.py")
    root = tmp_path / domain
    tauri = root / "src-tauri"
    (tauri / "resources/local").mkdir(parents=True)
    (tauri / "resources/local/launcher.py").write_text(domain)
    (tauri / "icons").mkdir()
    (tauri / "icons/128x128.png").write_bytes(b"icon")
    version = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["version"]
    (tauri / "Cargo.toml").write_text('[package]\nname = "desktop-test"\nversion = "9.8.7"\nauthors = ["Test"]\n')
    (tauri / "tauri.local.json").write_text(json.dumps({"productName": domain, "version": version,
        "bundle": {"resources": {"resources/local/": "local/"},
                   "linux": json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["bundle"]["linux"]}}))
    executable = root / "desktop-test"
    executable.write_bytes(b"executable")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "platform", "linux")
    monkeypatch.setattr(builder.sys, "argv", ["bundle_linux.py", str(executable)])
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "amd64\n")

    def bundle(arguments, **kwargs):
        stage = Path(arguments[-2])
        assert f"Version: {version}\n" in (stage / "DEBIAN/control").read_text()
        assert (stage / "usr/lib" / domain / "local/launcher.py").read_text() == domain
        Path(arguments[-1]).write_bytes(b"bundle")

    monkeypatch.setattr(builder.subprocess, "run", bundle)
    builder.main()
    assert (root / "dist/desktop-local" / f"desktop-test-local_{version}_amd64.deb").is_file()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_windows_private_writer_never_falls_back_to_plaintext(domain):
    source = (ROOT / "src-tauri/src/native_security.rs").read_text()
    assert '#[cfg(windows)]\npub(crate) fn write_private' in source
    writer = source.split('#[cfg(windows)]\npub(crate) fn write_private', 1)[1].split('#[cfg(test)]', 1)[0]
    assert 'O:{sid}D:P(A;;FA;;;{sid})' in writer
    assert "CreateFileW" in writer and "SecurityAttributes" in writer
    assert writer.index("private_windows(&file)") < writer.index("file.write_all(body)")
    assert "file.sync_all()?" in writer and "MoveFileExW" in writer
    assert "std::fs::write" not in writer
    assert "Keys and connections" in writer and domain not in writer


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_mac_local_build_checks_prerequisites_and_builds_arm64(tmp_path, domain):
    import tomllib

    version = next(package["version"] for package in tomllib.loads((ROOT / "src-tauri/Cargo.lock").read_text())["package"]
                   if package["name"] == "tauri")
    script = ROOT / "scripts/desktop/build_mac.sh"
    assert script.is_file()
    root = tmp_path / (domain + " build")
    tools = root / "tools"
    tools.mkdir(parents=True)
    copied = root / "scripts/desktop/build_mac.sh"
    copied.parent.mkdir(parents=True)
    copied.write_text(script.read_text())
    (root / "src-tauri").mkdir()
    (root / "src-tauri/Cargo.lock").write_text((ROOT / "src-tauri/Cargo.lock").read_text())
    trace = root / "commands"
    stub = '''#!/bin/bash
name=${0##*/}
printf '%s %s\\n' "$name" "$*" >> "$BUILD_TRACE"
case "$name" in
  uname) printf '%s\\n' "${BUILD_OS:-Darwin}" ;;
  node) if [[ "$*" == *arch* ]]; then echo "${BUILD_ARCH:-arm64}"; else echo 22; fi ;;
  cargo) if [[ "$*" == 'tauri --version' ]]; then echo "tauri-cli ${BUILD_CLI_VERSION:-$BUILD_EXPECTED_VERSION}"; fi
         if [[ "$1" == tauri && "$2" == build ]]; then mkdir -p target/aarch64-apple-darwin/release/bundle/macos/Fixture.app target/aarch64-apple-darwin/release/bundle/dmg; touch target/aarch64-apple-darwin/release/bundle/dmg/Fixture.dmg; fi ;;
  rustup) echo aarch64-apple-darwin ;;
  otool) printf 'node:\\n\\t%s (compatibility version 1.0.0)\\n' "${BUILD_NODE_LIB:-/usr/lib/libSystem.B.dylib}" ;;
  uv) if [[ "$*" == *find* ]]; then echo "$BUILD_TOOLS/python3"; fi
      if [[ "$1" == venv ]]; then mkdir -p "$2/bin"; ln -sf "$BUILD_TOOLS/python3" "$2/bin/python"; fi ;;
  python3|python) if [[ "$*" == *tomllib* ]]; then echo "$BUILD_EXPECTED_VERSION"; fi ;;
esac
'''
    for name in ("uname", "node", "cargo", "rustup", "uv", "pnpm", "xcrun", "otool", "codesign", "hdiutil", "python3", "lipo", "ditto"):
        executable = tools / name
        executable.write_text(stub)
        executable.chmod(0o755)
    environment = {"PATH": str(tools) + ":/usr/bin:/bin", "HOME": str(root),
                   "BUILD_TRACE": str(trace), "BUILD_TOOLS": str(tools), "BUILD_EXPECTED_VERSION": version,
                   "SUPYMEM_OSS_REPOSITORY": "owner/project"}
    result = subprocess.run(["bash", str(copied)], cwd=root, env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    commands = trace.read_text()
    assert "--require-hashes -r services/company_os/requirements.runtime.lock" in commands
    assert "--require-hashes -r services/company_os/requirements.build.lock" in commands
    assert "--node-linker=hoisted" in commands
    assert "tauri build --features local-stack --config tauri.local.json --target aarch64-apple-darwin --bundles app,dmg" in commands
    assert "beforeBuildCommand" not in commands
    for variable, value in [("BUILD_OS", "Linux"), ("BUILD_ARCH", "x64"), ("BUILD_CLI_VERSION", "mismatch"),
                            ("BUILD_NODE_LIB", "/opt/local/lib/custom.dylib")]:
        trace.write_text("")
        result = subprocess.run(["bash", str(copied)], env={**environment, variable: value}, capture_output=True, text=True)
        assert result.returncode != 0
        assert "you" in result.stderr.lower()
        assert "tauri build" not in trace.read_text()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_release_uses_app_version_and_separate_startup_splash(domain):
    local = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    hosted = json.loads((ROOT / "src-tauri/tauri.conf.json").read_text())
    assert local["version"] == hosted["version"]
    assert local["bundle"]["targets"] == ["appimage", "app", "dmg", "nsis"]
    assert "local-starting.html" not in (ROOT / "src-tauri/splashscreen/index.html").read_text()
    lifecycle = (ROOT / "src-tauri/src/lib.rs").read_text()
    splash = lifecycle.index("/local-starting.html")
    assert "if local_stack::is_local()" in lifecycle[splash - 350:splash]
    assert splash < lifecycle.index("let started = sup.start_all();")
    html = (ROOT / "src-tauri/splashscreen/local-starting.html").read_text()
    assert 'role="img" aria-label="Charlie"' in html and 'role="status"' in html
    assert "prefers-reduced-motion" in html
    assert domain not in html


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("state", ["python3", "python", "py", "bad_python3", "missing", "child_failed", "child_error", "child_signal"])
def test_local_build_hook_selects_python_without_a_shell(tmp_path, domain, platform, state):
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    assert config["build"]["beforeBuildCommand"] == "node scripts/desktop/prepare_local_stack.cjs"
    launcher = ROOT / "scripts/desktop/prepare_local_stack.cjs"
    directory = tmp_path / (domain + " build")
    result = subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const [launcher, directory, platform, state, domain] = process.argv.slice(1);
const selected = ['python3', 'python', 'py'].includes(state) ? state :
  state === 'bad_python3' ? 'python' : state === 'missing' ? null : 'python3';
const candidates = [['python3'], ['python'], ['py', '-3.12']];
const probes = [];
const builds = [];
const errors = [];
const stopped = {};
let exitCode;
const spawnSync = (command, args, options) => {
  assert.ok(!options.shell);
  if (args.includes('-c')) {
    const candidate = candidates[probes.length];
    assert.equal(command, candidate[0]);
    assert.deepEqual(Array.from(args.slice(0, -2)), candidate.slice(1));
    assert.equal(options.stdio, 'ignore');
    probes.push(command);
    if (command === selected) return {status: 0};
    return state === 'bad_python3' ? {status: 1} : {status: null, error: {code: 'ENOENT'}};
  }
  assert.equal(command, selected);
  assert.deepEqual(Array.from(args), [...(selected === 'py' ? ['-3.12'] : []),
    path.join(directory, 'prepare_local_stack.py'), '--target', domain + ' folder', '--runtime-only']);
  assert.equal(options.stdio, 'inherit');
  builds.push(command);
  if (state === 'child_error') return {status: null, error: {code: 'ENOENT'}};
  if (state === 'child_signal') return {status: null, signal: 'SIGTERM'};
  return {status: state === 'child_failed' ? 23 : 0};
};
try {
  vm.runInNewContext(fs.readFileSync(launcher, 'utf8'), {
    require: name => name === 'node:child_process' ? {spawnSync} : require(name),
    __dirname: directory,
    console: {error: message => errors.push(message)},
    process: {platform, argv: ['node', launcher, '--target', domain + ' folder', '--runtime-only'],
      exit: code => { exitCode = code; throw stopped; }},
  });
} catch (error) { if (error !== stopped) throw error; }
assert.equal(builds.length, selected ? 1 : 0);
assert.equal(probes.length, selected ? candidates.findIndex(candidate => candidate[0] === selected) + 1 : 3);
assert.equal(exitCode, state === 'child_failed' ? 23 :
  ['missing', 'child_error', 'child_signal'].includes(state) ? 1 : 0);
if (['missing', 'child_error', 'child_signal'].includes(state)) {
  assert.match(errors.join(' '), /[Yy]our/);
  assert.match(errors.join(' '), /retry/);
  if (state === 'missing' && platform === 'win32') assert.ok(errors.join(' ').includes('start.ps1 --check'));
} else assert.deepEqual(errors, []);
""", str(launcher), str(directory), platform, state, domain], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
@pytest.mark.parametrize("state", ["new", "existing", "active", "without_uv", "installer_failed", "child_failed", "stale"])
def test_local_stack_bootstraps_its_locked_build_python(tmp_path, monkeypatch, domain, platform, state):
    builder = load("prepare_build_python", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "sys", SimpleNamespace(**vars(builder.sys)))
    monkeypatch.setattr(builder.sys, "platform", platform)
    root = tmp_path / (domain + " build")
    root.mkdir()
    environment = root / f".desktop-python-{builder.platform.machine()}"
    python = environment / ("Scripts/python.exe" if builder.sys.platform == "win32" else "bin/python")
    standalone_base = root / "standalone"
    standalone = standalone_base / ("python.exe" if platform == "win32" else "bin/python3")
    monkeypatch.syspath_prepend(str(ROOT / "scripts/desktop"))
    monkeypatch.setattr(builder, "standalone_python", lambda _target: standalone)
    monkeypatch.setattr(builder, "runtime_identity", lambda _binary: "pinned identity")
    monkeypatch.setattr(builder.sys, "base_prefix", str(standalone_base))
    if state in {"existing", "active", "stale"}:
        python.parent.mkdir(parents=True)
        python.touch()
        (environment / ".runtime-identity").write_text("old identity" if state == "stale" else "pinned identity")
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "prefix", str(environment if state == "active" else root / "ambient"))
    monkeypatch.setattr(builder.sys, "argv", [str(ROOT / "scripts/desktop/prepare_local_stack.py"),
                                              "--target", "x86_64-unknown-linux-gnu", "--runtime-only"])
    monkeypatch.setattr(builder.shutil, "which", lambda tool: None if state == "without_uv" else "/tools/uv")
    commands = []

    def run(command, *, cwd, check):
        commands.append(command)
        assert cwd == root
        if state == "installer_failed" and command[1] == "pip":
            raise subprocess.CalledProcessError(1, command)
        if "venv" in command:
            python.parent.mkdir(parents=True)
            python.touch()
        return SimpleNamespace(returncode=23 if state == "child_failed" and not check else 0)

    monkeypatch.setattr(builder.subprocess, "run", run)
    if state == "active":
        builder.ensure_build_python()
        assert commands == []
        return
    with pytest.raises(SystemExit) as stopped:
        builder.ensure_build_python()
    if state == "installer_failed":
        assert "rerun" in str(stopped.value)
        assert len(commands) == 2
        assert not any(command[0] == str(python) for command in commands)
        return
    expected = [] if state == "existing" else [[str(standalone), "-m", "venv", str(environment)] if state == "without_uv"
        else ["/tools/uv", "venv", str(environment), "--python", str(standalone)]]
    for lock in ("services/company_os/requirements.runtime.lock", "services/company_os/requirements.build.lock",
                 "packages/desktop_runtime/requirements.local.lock"):
        installer = [str(python), "-m", "pip", "install"] if state == "without_uv" else ["/tools/uv", "pip", "install", "--python", str(python)]
        expected.append([*installer, "--require-hashes", "-r", lock])
    expected.append([str(python), str(ROOT / "scripts/desktop/prepare_local_stack.py"),
                     "--target", "x86_64-unknown-linux-gnu", "--runtime-only"])
    assert commands == expected
    assert stopped.value.code == (23 if state == "child_failed" else 0)
    assert (environment / ".runtime-identity").read_text() == "pinned identity"


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("platform", ["linux", "darwin", "win32"])
def test_local_stack_runtime_uses_native_python_and_node_paths(tmp_path, monkeypatch, domain, platform):
    builder = load("prepare_native_runtime", ROOT / "scripts/desktop/prepare_local_stack.py")
    root = tmp_path / (domain + " build")
    base = root / "python"
    windows = platform == "win32"
    python = base / ("python.exe" if windows else "bin/python3")
    stdlib = base / ("Lib" if windows else "lib/python3.12")
    node = root / "node" / ("node.exe" if windows else "bin/node")
    for path in (python, stdlib / "os.py", base / "LICENSE.txt", node, node.parent / "LICENSE",
                 root / "LICENSE", root / "NOTICE", root / "scripts/desktop/local_core.py", root / "scripts/desktop/local_console.cjs"):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(domain)
    openssl_config = (ROOT / "scripts/desktop/openssl.cnf").read_bytes()
    (root / "scripts/desktop/openssl.cnf").write_bytes(openssl_config)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder, "sys", SimpleNamespace(platform=platform, base_prefix=str(base),
        executable=str(python), version_info=SimpleNamespace(major=3, minor=12), version="3.12.0"))
    monkeypatch.setattr(builder.sysconfig, "get_path", lambda _name: str(stdlib))
    sites = []

    def dependencies(destination):
        sites.append(destination)
        destination.mkdir(parents=True, exist_ok=True)
        return []

    monkeypatch.setattr(builder, "copy_dependencies", dependencies)
    copy_source = builder.copy_source
    monkeypatch.setattr(builder, "copy_source", lambda _source, _destination: None)
    libraries = []
    monkeypatch.setattr(builder, "linux_libraries", libraries.append)
    monkeypatch.setattr(builder.subprocess, "check_output", lambda command, **_kwargs:
        "" if command[0] == "git" else "v22.0.0")
    builder.prepare_runtime(platform, node)
    runtime = root / "src-tauri/resources/local/runtime" / platform
    assert (runtime / ("python.exe" if windows else "bin/python3")).read_text() == domain
    assert (runtime / ("node.exe" if windows else "bin/node")).read_text() == domain
    assert (runtime / ("Lib/os.py" if windows else "lib/python3.12/os.py")).read_text() == domain
    assert (root / "src-tauri/resources/local/openssl.cnf").read_bytes() == openssl_config
    assert sites == [runtime / ("Lib/site-packages" if windows else "lib/python3.12/site-packages")]
    assert (root / "src-tauri/resources/local/NOTICE").read_text() == domain
    assert libraries == ([runtime] if platform == "linux" else [])
    configuration = root / "config/current.yaml"
    configuration.parent.mkdir(parents=True)
    configuration.write_text(domain)
    monkeypatch.setattr(builder, "copy_source", copy_source)
    builder.prepare_runtime(platform, node)
    assert (root / "src-tauri/resources/local/config/current.yaml").read_text() == domain
    if windows:
        assert not (runtime / "bin").exists()


def mock_e2e_socket():
    return nullcontext(SimpleNamespace(bind=lambda address: None, listen=lambda: None,
        getsockname=lambda: ("127.0.0.1", 9222)))


def load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("domain", ["warehouse.example", "gardening.example"])
def test_linux_e2e_imports_supplied_keys_without_another_plaintext_file(tmp_path, domain, monkeypatch):
    import os
    import sys

    check = load("linux_encrypted_import", ROOT / "scripts/desktop/e2e_linux.py")
    run = subprocess.run
    def run_import(command, **options):
        assert command[0] == str(ROOT / ".desktop-python-x86_64/bin/python")
        return run([sys.executable, *command[1:]], **options)
    monkeypatch.setattr(check.subprocess, "run", run_import)
    source = tmp_path / "supplied.env"
    secret = "private-test-password-" + domain
    source.write_text("SMTP_PASSWORD=" + secret + "\n")
    data = tmp_path / "owner-data"
    monkeypatch.setenv("HOME", str(tmp_path))
    environment = {**os.environ, "HOME": str(tmp_path)}
    check.prepare_env_file(source, data, "env")
    assert not (data / ".env").exists()
    assert (data / "local-settings.json").is_file()
    assert all(secret.encode() not in path.read_bytes() for path in data.rglob("*") if path.is_file())
    environment.update(SUPYMEM_MODE="local", APP_ENV="local", COMPANY_OS_DATA_DIR=str(data),
        PYTHONPATH=f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}")
    result = run([sys.executable, "-c",
        "from pathlib import Path; import sys; from coding_agents.env import load_dotenv_file; "
        "from app.core.local_secrets import get_secret; "
        "assert get_secret('smtp', 'password') == load_dotenv_file(Path(sys.argv[1]))['SMTP_PASSWORD']", str(source)],
        env=environment, capture_output=True)
    assert result.returncode == 0


@pytest.mark.parametrize("domain", ["warehouse.example", "gardening.example"])
@pytest.mark.parametrize("saved_cap", [None, "0.00"])
def test_linux_e2e_experiments_set_spend_on_owner_screen_and_preserve_existing_cap(tmp_path, domain, saved_cap):
    check = load("linux_experiment_setup", ROOT / "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"idea": domain})
    steps = []
    spend = {"monthly_spend_cap": saved_cap}
    journey.get = lambda _path: {"values": spend}
    journey.open_screen = lambda *args: steps.append(("screen", args))
    journey.desktop = SimpleNamespace(evaluate=lambda _script: True,
        fill=lambda selector, value: (steps.append(("fill", selector, value)), spend.update(monthly_spend_cap=value)))
    journey.poll = lambda predicate: predicate()
    journey.passed = lambda: None
    journey.verify_capabilities = lambda names: steps.append(("switch", names))
    journey.prepare_experiments()
    assert steps[-1] == ("switch", ("run_for_me_experiments",))
    assert bool(any(step[0] == "fill" for step in steps)) is (saved_cap is None)
    assert spend["monthly_spend_cap"] == ("5.00" if saved_cap is None else saved_cap)
    journey.step = "monthly spending limit"
    journey.app = SimpleNamespace(poll=lambda: 0)
    with pytest.raises(RuntimeError, match="Keys and connections > Your monthly spend cap"):
        check.Journey.poll(journey, lambda: False)


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_demo_paces_fixture_typing_without_changing_normal_runs(tmp_path, monkeypatch, domain):
    check = load("linux_demo_pacing", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]["demo"]
    commands, pauses = [], []
    monkeypatch.setattr(check.time, "sleep", pauses.append)
    monkeypatch.setenv("E2E_RECORD", "1")
    journey = check.Journey(tmp_path, scenario)
    journey.input = lambda *args: commands.append(args)
    journey.send(scenario["idea"])
    assert commands == [("key", "ctrl+a"),
                        ("type", "--clearmodifiers", "--delay", 95, "--", scenario["idea"]),
                        ("key", "Return")]
    assert pauses == [2]
    journey.step = "idea"
    journey.state = lambda: {"entries": [{"text": scenario["idea"], "proposal": {"state": "pending"}}]}
    journey.capture = lambda: None
    journey.wait()
    assert pauses == [2, 3]
    commands.clear()
    monkeypatch.delenv("E2E_RECORD")
    normal = check.Journey(tmp_path, scenario)
    normal.input = journey.input
    normal.send(scenario["people"])
    assert commands[1] == ("type", "--clearmodifiers", "--delay", 85, "--", scenario["people"])


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_name_waits_for_saved_question_before_skip(tmp_path, monkeypatch, domain):
    check = load("linux_e2e_name_readiness", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    journey = check.Journey(tmp_path, scenario)
    journey.step = "name"
    base = f"/ventures/{domain}"
    responses = {"/ventures": {"items": [{"id": domain, "name": scenario["name"]}]},
                 base: {"name": scenario["name"], "raw_idea": scenario["idea"]},
                 base + "/idea/sessions": {"items": []}}
    monkeypatch.setattr(journey, "get", lambda path: responses[path])
    assert not check.step_ready("name", journey.state(), scenario)
    responses[base + "/idea/sessions"] = {"items": [{"id": "session-" + domain}]}
    session = {"state": "clarifying", "turns": []}
    responses[base + "/idea/sessions/session-" + domain] = session
    assert not check.step_ready("name", journey.state(), scenario)
    session["turns"] = [{"kind": "question", "text": ""}]
    assert not check.step_ready("name", journey.state(), scenario)
    session["turns"][0]["text"] = "What do you need to learn first?"
    assert check.step_ready("name", journey.state(), scenario)
    session["state"] = "validating"
    assert check.step_ready("name", journey.state(), scenario)


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_people_reads_the_mounted_journey_route(tmp_path, monkeypatch, domain):
    import re
    from urllib.parse import urlencode

    check = load("linux_e2e_people_route", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    prefix = re.search(r'APIRouter\(prefix="([^"]+)"',
                       (ROOT / "services/company_os/app/api/gtm_god_ops.py").read_text()).group(1)
    journey = check.Journey(tmp_path, scenario)
    journey.step, journey.venture_id = "people", domain
    base = f"/ventures/{domain}"
    workspace_id = "workspace " + domain
    responses = {base + "/idea/sessions": {"items": []},
                 base: {"workspace_id": workspace_id},
                 base + "/chat": {"items": [{"role": "user", "body_md": scenario["people"]}]},
                 prefix + base + "/gtm/journey?" + urlencode({"workspace_id": workspace_id}): {"state": "ready", "discovery_started": True,
                                                 "prospects": [{"id": domain}], "results": {"sent": 0}}}
    monkeypatch.setattr(journey, "get", lambda path: responses[path])
    assert check.step_ready("people", journey.state(), scenario)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_capabilities_matches_the_rendered_heading(tmp_path, monkeypatch, domain):
    import re

    check = load("linux_e2e_capabilities", ROOT / "scripts/desktop/e2e_linux.py")
    source = (ROOT / "apps/console/src/app/settings/local/capabilities/CapabilitiesView.tsx").read_text()
    heading = re.search(r"<h1\b[^>]*>([^<]+)</h1>", source).group(1)
    capabilities = [{"name": name, "title": name.replace("_", " ").title(), "state": "ready",
                     "missing": [], "next_step": f"Choose your work for {domain}."}
                    for name in ("research", "find_customers", "build_with_my_tool", "landing_pages")]
    sections = [{"title": item["title"], "text": f'{item["title"]}\nReady\n{item["next_step"]}'}
                for item in capabilities]
    screen = {"documents": 1, "text": heading + "\n" + "\n".join(item["text"] for item in sections),
              "sections": sections}
    assert check.capabilities_ready(screen, capabilities)
    assert not check.capabilities_ready(screen | {"sections": sections[:-1]}, capabilities)
    assert not check.capabilities_ready(screen | {"documents": 0}, capabilities)
    journey = check.Journey(tmp_path, {})
    opened = []
    monkeypatch.setattr(journey, "get", lambda path: {"capabilities": capabilities})
    monkeypatch.setattr(journey, "read_screen", lambda: screen)
    monkeypatch.setattr(journey, "open_screen", lambda path=None, expected=None: opened.append((path, expected)))
    monkeypatch.setattr(journey, "poll", lambda ready: ready() or pytest.fail("Capabilities did not render"))
    monkeypatch.setattr(journey, "passed", lambda: None)
    journey.verify_capabilities()
    assert opened == [("/settings/local/capabilities", heading), (None, heading)]


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_enables_requested_sample_experiments_in_the_app(tmp_path, monkeypatch, domain):
    check = load("linux_e2e_sample_experiments", ROOT / "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"idea": domain})
    item = {"name": "run_for_me_experiments", "title": "Run your experiments", "state": "off", "missing": [],
            "next_step": "In Settings > Capabilities, switch on Run your experiments."}
    clicks = []
    monkeypatch.setattr(journey, "get", lambda path: {"capabilities": [item]})
    monkeypatch.setattr(journey, "open_screen", lambda *args, **kwargs: None)
    monkeypatch.setattr(journey, "read_screen", lambda: {"documents": 1, "text": "Capabilities", "sections": [{
        "title": item["title"], "text": ("Off" if item["state"] == "off" else "Ready") + " " + item["next_step"]}]})
    monkeypatch.setattr(journey, "poll", lambda ready: ready() or pytest.fail("Your requested feature did not become ready"))
    monkeypatch.setattr(journey, "passed", lambda: None)
    def enable(expression):
        assert "Enable Run your experiments" in expression
        clicks.append(expression)
        item["state"] = "ready"
        return True
    journey.desktop = SimpleNamespace(evaluate=enable)
    journey.verify_capabilities(("run_for_me_experiments",))
    assert len(clicks) == 1 and item["state"] == "ready"


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_connects_coding_tools_after_showing_the_setup_step(tmp_path, monkeypatch, domain):
    check = load("linux_e2e_coding_tools", ROOT / "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"idea": domain})
    journey.fake = tmp_path / "bin"
    journey.runtime_python = tmp_path / "runtime/bin/python3"
    capabilities = [{"name": name, "title": name, "state": "needs_setup", "missing": ["Your connected coding tool"],
                     "next_step": "In Settings > Keys and connections > Coding tools, connect your tool and choose Allow Charlie."}
                    for name in ("landing_pages", "build_with_my_tool")]
    screen = {"documents": 1, "text": "Capabilities", "sections": [
        {"title": item["title"], "text": "Needs setup " + " ".join(item["missing"]) + " " + item["next_step"]}
        for item in capabilities]}
    installed, consent, opened, passed = [], {}, [], []
    install = check.install_fake_cli
    def install_fake(directory, provider):
        installed.append(provider)
        return install(directory, provider)
    monkeypatch.setattr(check, "install_fake_cli", install_fake)
    monkeypatch.setattr(journey, "get", lambda path: {"capabilities": capabilities} if path == "/local/capabilities"
                        else {"coding_agents": {"consent": consent}})
    monkeypatch.setattr(journey, "read_screen", lambda click=None: screen)
    monkeypatch.setattr(journey, "open_screen", lambda path=None, expected=None: opened.append((journey.step, path)))
    monkeypatch.setattr(journey, "poll", lambda ready: ready() or pytest.fail("Your coding tool setup did not finish"))
    monkeypatch.setattr(journey, "passed", lambda: passed.append(journey.step))
    def evaluate(expression):
        if "scrollIntoView" in expression:
            assert not installed and "landing_pages-title" in expression
            return True
        assert "coding tool required" in passed
        assert set(installed) == {"claude", "codex"}
        if "node.click()" in expression and "Allow Charlie" in expression:
            consent["claude" if "Claude Code" in expression else "codex"] = True
        return True
    journey.desktop = SimpleNamespace(evaluate=evaluate)
    journey.connect_coding_tools()
    assert set(consent) == {"claude", "codex"}
    assert opened[0] == ("coding tool required", "/settings/local/capabilities")
    assert [(step, path) for step, path in opened if path] == [
        ("coding tool required", "/settings/local/capabilities"), ("connect coding tools", "/settings/local")]
    assert opened[-1] == ("connect coding tools", None)
    assert passed == ["coding tool required", "connect coding tools"]
    capabilities[0]["state"] = "ready"
    with pytest.raises(RuntimeError, match="coding tool"):
        journey.connect_coding_tools()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("step,next_step", [("connect coding tools", "Keys and connections > Coding tools"),
                                          ("landing page", "Retry on Website")])
def test_linux_e2e_tool_timeout_names_the_setup_or_build_next_step(tmp_path, domain, step, next_step):
    check = load("linux_e2e_tool_timeout", ROOT / "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"idea": domain})
    journey.step = step
    journey.app = SimpleNamespace(poll=lambda: 1)
    with pytest.raises(RuntimeError, match=next_step):
        journey.poll(lambda: False)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("state", ["reviewing", "question", "other_question", "pending"])
def test_linux_e2e_answers_a_question_or_confirms_a_complete_brief(tmp_path, monkeypatch, domain, state):
    check = load("linux_e2e_answer_or_review", ROOT / "scripts/desktop/e2e_linux.py")
    journey = check.Journey(tmp_path, {"answers": {"workflow": domain}})
    base = "/ventures/ven_review/idea/sessions/ids_review"
    session = {"state": "reviewing" if state == "reviewing" else "clarifying", "current_revision": {"id": "revision"},
        "turns": [{"role": "agent", "kind": "question", "options": {
            "field": "workflow" if state == "question" else "pricing_hypothesis"}}] if "question" in state else []}
    sent = []
    def get(path, *, payload=None):
        if payload:
            sent.append((path, payload))
            if path.endswith("/messages"):
                session["turns"].append({"role": "founder", "kind": "answer"})
                session["state"] = "reviewing"
        return session
    def poll(ready):
        if not ready():
            assert state == "pending"
            session["state"] = "reviewing"
            assert ready()
    monkeypatch.setattr(journey, "get", get)
    monkeypatch.setattr(journey, "poll", poll)
    journey.answer_live_idea(base)
    expected = [{"text": domain}] if state == "question" else [{"text": "Skip the questions"}] if state == "other_question" else []
    assert [payload for path, payload in sent if path.endswith("/messages")] == expected
    assert sent[-1] == (base + "/confirm", {"revision_id": "revision", "start_research": False})


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_supplies_page_facts_before_confirming_the_idea(domain):
    check = load("linux_e2e_confirmed_facts", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    idea = check.live_idea(scenario)
    assert idea.startswith(scenario["idea"])
    assert all(answer in idea for answer in scenario["answers"].values())


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_preview_predicate_checks_the_open_tool_page(domain):
    check = load("linux_e2e_preview_predicate", ROOT / "scripts/desktop/e2e_linux.py")
    tree = check.ast.parse((ROOT / "scripts/desktop/e2e_linux.py").read_text())
    expression = next(node.left.value for node in check.ast.walk(tree)
        if isinstance(node, check.ast.BinOp) and isinstance(node.op, check.ast.Mod)
        and isinstance(node.left, check.ast.Constant) and isinstance(node.left.value, str)
        and 'iframe[title="Full landing page preview"]' in node.left.value)
    expression %= (json.dumps(check.MARKER), json.dumps(domain))
    script = """
const assert = require('node:assert/strict');
const vm = require('node:vm');
const [expression, name, marker] = process.argv.slice(1);
const ready = {frame: true, height: 600, marker: true, headline: true, name, expected: true};
for (const state of [ready, {...ready, frame: false, expected: false}, {...ready, height: 0, expected: false},
    {...ready, marker: false, expected: false}, {...ready, headline: false, expected: false},
    {...ready, name: '', expected: false}]) {
    const context = {
        document: {querySelector: () => state.frame ? {srcdoc: state.marker ? marker : '',
            getBoundingClientRect: () => ({height: state.height})} : null},
        DOMParser: class {parseFromString() {return {querySelector: () => state.headline ? {} : null,
            body: {textContent: state.name}};}}
    };
    assert.equal(vm.runInNewContext(expression, context), state.expected);
}
"""
    result = subprocess.run(["node", "-e", script, expression, domain, check.MARKER], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_exports_the_same_private_source_zip_as_website(domain):
    check = load("linux_e2e_private_source", ROOT / "scripts/desktop/e2e_linux.py")
    tree = check.ast.parse((ROOT / "scripts/desktop/e2e_linux.py").read_text())
    call = next(node for node in check.ast.walk(tree) if isinstance(node, check.ast.Call)
        and isinstance(node.func, check.ast.Attribute) and node.func.attr == "get"
        and any(keyword.arg == "download" and isinstance(keyword.value, check.ast.Constant)
                and keyword.value.value is True for keyword in node.keywords))
    page_path = f"/ventures/{domain}/pagebuilder"
    path = eval(compile(check.ast.Expression(call.args[0]), "source_download", "eval"),
        {"page_path": page_path, "job": {"job_id": "job_private"}})
    assert path == page_path + "/jobs/job_private/source?variant=0"


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("signup_mode", ["setup", "endpoint"])
@pytest.mark.parametrize("shared_social", [False, True])
@pytest.mark.usefixtures("private_coding_install")
def test_linux_e2e_tool_fixture_builds_a_checked_landing_preview(tmp_path, monkeypatch, domain, provider, signup_mode, shared_social):
    from tests.company_os_harness import load_company_os_app
    from desktop_runtime.owned_coding import provider_outcome

    load_company_os_app()
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.pipeline import tool_page_plan
    from app.pagebuilder.tool import build_brief, check_output

    check = load("linux_e2e_landing_tool", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    if shared_social:
        source = tmp_path / "source"
        for relative in ("scripts/local_build_journey.py", "tests/unit/pagebuilder/test_pipeline.py", "tests/unit/pagebuilder/test_metadata.py",
                         "tests/unit/product/test_static_publication.py"):
            target = source / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((ROOT / relative).read_bytes())
        helpers = check.ast.parse((ROOT / "tests/unit/pagebuilder/test_tool_build.py").read_text())
        helper = next(check.ast.unparse(node) for node in helpers.body
            if isinstance(node, check.ast.FunctionDef) and node.name == "_with_social_preview")
        helper = helper.replace("    return files",
            "    files['social-fixture.png'] = files[SOCIAL_PREVIEW_PATH]\n    return files")
        (source / "tests/unit/pagebuilder/test_tool_build.py").write_text(helper)
        monkeypatch.setattr(check, "ROOT", source)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    inputs = PageInput(venture_id="ven_preview", name=scenario["name"], idea=check.live_idea(scenario),
        waitlist_url="http://127.0.0.1:3300/public/waitlists/ven_preview", clarified_answers=scenario["answers"], research={"local_signup": {
            "mode": signup_mode, "url": "http://127.0.0.1:3300/public/waitlists/ven_preview" if signup_mode == "endpoint" else "#waitlist",
            "notice": "Set up your signup on Keys and connections > Publishing."}})
    binary = check.install_desktop_cli(tmp_path / "bin", provider, check.sys.executable)
    assert "from tests.unit.pagebuilder.test_pipeline" not in binary.read_text()
    assert "from tests.unit.product.test_static_publication" not in binary.read_text()
    (binary.parent / "page-fixture.json").write_text(json.dumps({"root": str(ROOT), "template": check.page_fixture(scenario)}))
    workspace = tmp_path / "result"
    workspace.mkdir()
    brief = build_brief(inputs, None)
    command = [str(binary), "-p", brief] if provider == "claude" else [str(binary), "exec", "--cd", str(workspace), brief]
    result = subprocess.run(command, cwd=workspace, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert provider_outcome(result.stdout.encode(), provider)["outcome"] == "pass", result.stdout
    files = {path.relative_to(workspace).as_posix(): path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
    plan = tool_page_plan(inputs, None, files)
    checked = check_output(files, inputs, plan)
    assert checked["passed"], checked["failures"]
    assert check.MARKER in files["index.html"].decode()
    assert scenario["name"] in files["index.html"].decode()
    assert check.cli_runs(binary.parent)[0]["provider"] == provider
    if shared_social:
        assert files["social-fixture.png"].startswith(b"\x89PNG\r\n\x1a\n")
    elif signup_mode == "endpoint":
        from desktop_runtime.owned_coding import OwnedCoding
        from desktop_runtime.owned_processes import OwnedProcesses
        (workspace / ".supymem-venture").write_text(inputs.venture_id)
        tracker = OwnedProcesses()
        coding = OwnedCoding(venture_id=inputs.venture_id, workspace_slug="preview", provider=provider,
            tracker=tracker, authorize=lambda: True)
        coding.loop_mode = True
        monkeypatch.setattr(coding, "preflight", lambda: str(binary))
        monkeypatch.setattr(coding, "_workspace", lambda: workspace)
        try:
            receipt = coding.run(brief, check.time.monotonic() + 120)
            assert receipt["outcome"] == "pass" and receipt["workspace_marker_verified"], receipt
        finally:
            tracker.stop_all()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_reads_selected_ports(tmp_path, domain):
    import secrets

    check = load("linux_e2e_ports", ROOT / "scripts/desktop/e2e_linux.py")
    ports = (43300, 43301, 43100)
    (tmp_path / "app.log").write_text("desktop_startup phase=local_ports core=%s worker=%s console=%s\n" % ports)
    journey = check.Journey(tmp_path, {})
    journey.owner_secret_file = tmp_path / "owner-session.secret"
    secret = secrets.token_hex(32)
    journey.owner_secret_file.write_text(secret)
    journey.owner_secret_file.chmod(0o600)
    calls = []

    class Opener:
        def open(self, url, timeout):
            from io import BytesIO
            calls.append(url)
            return BytesIO(json.dumps({"domain": domain}).encode())

    journey.opener = Opener()
    for role, port in zip(("core", "worker", "console"), ports):
        assert journey.get("/health", role) == {"domain": domain}
        assert calls[-1].full_url == f"http://127.0.0.1:{port}/health"
        assert secrets.compare_digest(calls[-1].get_header("X-local-proxy-secret"), secret)
    secret = secrets.token_hex(32)
    journey.owner_secret_file.write_text(secret)
    assert journey.get("/probe", payload={"name": domain}) == {"domain": domain}
    assert calls[-1].full_url == f"http://127.0.0.1:{ports[0]}/probe"
    assert calls[-1].get_method() == "POST"
    assert json.loads(calls[-1].data) == {"name": domain}
    assert secrets.compare_digest(calls[-1].get_header("X-local-proxy-secret"), secret)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.parametrize("fault", ["missing", "malformed", "permissions", "symlink", "fifo"])
def test_linux_e2e_rejects_unsafe_owner_credentials(tmp_path, domain, fault):
    import secrets

    check = load("linux_e2e_owner_boundary", ROOT / "scripts/desktop/e2e_linux.py")
    (tmp_path / "app.log").write_text("desktop_startup phase=local_ports core=43300 worker=43301 console=43100\n")
    journey = check.Journey(tmp_path, {"domain": domain})
    journey.owner_secret_file = tmp_path / "owner-session.secret"
    if fault == "fifo":
        import os

        os.mkfifo(journey.owner_secret_file, 0o600)
    elif fault != "missing":
        journey.owner_secret_file.write_text("invalid" if fault == "malformed" else secrets.token_hex(32))
        journey.owner_secret_file.chmod(0o644 if fault == "permissions" else 0o600)
        if fault == "symlink":
            target = tmp_path / "other.secret"
            journey.owner_secret_file.rename(target)
            journey.owner_secret_file.symlink_to(target)
    journey.opener = SimpleNamespace(open=lambda *_args, **_kwargs: pytest.fail("Your credential must be checked before opening a connection"))
    from local_acceptance import AcceptanceFailure

    with pytest.raises(AcceptanceFailure, match="private owner session"):
        journey.get("/local/providers")


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_e2e_sample_discovery_keeps_the_log_delivery_guard(tmp_path, domain):
    import ast
    import re

    tree = ast.parse((ROOT / "scripts/desktop/e2e_linux.py").read_text())
    updates = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
               and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
               and node.func.value.id == "environment" and node.func.attr == "update"]
    settings = {keyword.arg: keyword.value.value for update in updates for keyword in update.keywords
                if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str)}
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text().split("fn command(&self", 1)[1]
    names = json.loads(re.search(r"for name in (\[.*?\]) \{", source, re.S).group(1))
    mode = load("linux_e2e_sample_guard", ROOT / "services/company_os/app/core/local_mode.py")
    mode.is_sample_mode = lambda: False
    mode.is_offline = lambda: (mode.os.environ.get("LLM_OFFLINE", "").strip().lower() in {"1", "true", "yes", "on"}
                              or mode.os.environ.get("LLM_CASSETTE_MODE") == "replay")
    mode.os = SimpleNamespace(environ={"SUPYMEM_MODE": settings["SUPYMEM_MODE"], "HOME": str(tmp_path / domain),
                                      **{name: settings[name] for name in names if name in settings}})
    assert mode.is_test_mode()
    mode.os.environ["EMAIL_PROVIDER"] = "ses"
    assert not mode.is_test_mode()
    mode.os.environ.update(EMAIL_PROVIDER="log", SUPYMEM_MODE="hosted")
    assert not mode.is_test_mode()


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_checks_saved_state_not_recommendations(tmp_path, monkeypatch, domain):
    check = load("linux_e2e", ROOT / "scripts/desktop/e2e_linux.py")
    monkeypatch.delenv("E2E_OCCUPY_PORT", raising=False)
    monkeypatch.setattr(check, "socket", SimpleNamespace(socket=mock_e2e_socket))
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    state = {"provider": {"configured": False, "sample_mode": True},
             "health": {"status": "ok", "desktop_instance": "owned-run"},
             "console_health": {"ok": True},
             "auth": {"authenticated": True, "mode": "local"}, "entries": [], "ventures": []}
    assert check.step_ready("splash", state, scenario)
    assert check.step_ready("setup", state, scenario)
    assert not check.step_ready("sample", state, scenario)
    state["provider"] = {"configured": True, "sample_mode": True, "values": {}, "secrets_set": {"api_key": False}}
    assert check.step_ready("sample", state, scenario)
    assert check.step_ready("charlie", state, scenario)
    state["entries"] = [{"text": scenario["idea"], "proposal": {"state": "pending"}}]
    assert check.step_ready("idea", state, scenario)
    assert not check.step_ready("name", state, scenario)
    state["venture"] = {"id": domain, "name": scenario["name"], "raw_idea": scenario["idea"]}
    state["session"] = {"state": "clarifying", "turns": [{"kind": "question", "text": "What do you need to learn first?"}]}
    assert check.step_ready("name", state, scenario)
    state["session"]["turns"].append({"role": "founder", "kind": "answer", "text": scenario["idea"]})
    assert not check.step_ready("questions", state, scenario)
    state["session"]["turns"].append({"role": "agent", "kind": "question", "text": "What do you do today?"})
    assert check.step_ready("questions", state, scenario)
    state["previous_turn_id"] = "answered"
    state["session"]["turns"][-1]["id"] = "answered"
    assert not check.step_ready("answer", state, scenario)
    state["session"]["turns"][-1]["id"] = "following"
    assert check.step_ready("answer", state, scenario)
    state["session"]["turns"].append({"role": "founder", "kind": "answer", "text": scenario["answers"]["Q1"]})
    state["session"]["current_revision"] = {"id": domain, "structured": {}}
    state["session"]["gstack_answers"] = [{"answer": scenario["answers"]["Q1"], "skipped": False}]
    state["session"]["validation_runs"] = [{"revision_id": domain, "state": "complete",
        "report": {"sample": True, "verified": False}} for _ in range(3)]
    assert check.step_ready("review", state, scenario)
    assert check.step_ready("answer", state, scenario)
    state["session"]["gstack_answers"] = []
    assert check.step_ready("review", state, scenario)
    state["session"]["validation_runs"][0]["state"] = "failed"
    assert not check.step_ready("review", state, scenario)
    state["session"]["validation_runs"][0]["state"] = "complete"
    state["session"]["current_revision"]["structured"]["_questions_skipped"] = True
    assert not check.step_ready("review", state, scenario)
    state["session"] = {"state": "reviewing", "current_revision": {"structured": {"_questions_skipped": True}}}
    assert check.step_ready("skip", state, scenario)
    assert not check.step_ready("start", state, scenario)
    state["session"]["state"] = "confirmed"
    assert check.step_ready("start", state, scenario)
    state["chat"] = [{"role": "user", "body_md": scenario["people"]},
                     {"role": "assistant", "kind": "next_moves", "body_md": "Choose your next step."}]
    state["journey"] = {"state": "ready", "discovery_started": False, "prospects": []}
    assert not check.step_ready("people", state, scenario)
    state["journey"].update(discovery_started=True, prospects=[{"id": domain}])
    assert check.step_ready("people", state, scenario)
    state["chat"].append({"role": "user", "body_md": scenario["experiment"]})
    state["experiments"] = []
    assert not check.step_ready("experiment", state, scenario)
    state["experiments"] = [{"id": domain, "hypothesis": scenario["idea"], "action": "Interview five people", "measure": "Two paid reservations", "state": "proposed"}]
    assert check.step_ready("experiment", state, scenario)
    state["prior_experiments"] = [domain]
    assert not check.step_ready("experiment", state, scenario)
    check.save_result(tmp_path, "people", False, "Check your sample entry, then rerun.")
    assert json.loads((tmp_path / "results.json").read_text()) == [
        {"step": "people", "passed": False, "screenshot": "08_people.png", "message": "Check your sample entry, then rerun."}]
    evidence = tmp_path / domain
    evidence.mkdir()
    stopped = []
    monkeypatch.setattr(check.sys, "argv", ["check", "/test.AppImage", str(evidence)])
    def launch(*args, **kwargs):
        check.os.write(kwargs["pass_fds"][0], b'connect(3, inet_addr("127.0.0.1")) = 0\n')
        return SimpleNamespace(pid=1234, stdout=BytesIO(), poll=lambda: None, wait=lambda **kwargs: 0)

    monkeypatch.setattr(check.subprocess, "Popen", launch)
    monkeypatch.setattr(check.os, "killpg", lambda *args: stopped.append(args))

    def fail_sample(journey):
        journey.step = "sample"
        raise RuntimeError("Update your app, then rerun.")

    monkeypatch.setattr(check.Journey, "run", fail_sample)
    monkeypatch.setattr(check.Journey, "capture", lambda journey: (
        journey.output / check.screenshot_name(journey.step)).write_bytes(b"screenshot"))
    assert check.main() == 1
    results = json.loads((evidence / "results.json").read_text())
    assert len(results) == 2 and results[0]["passed"] is False
    assert results[1]["step"] == "no hosted requests" and results[1]["passed"] is True
    assert (evidence / results[0]["screenshot"]).read_bytes() == b"screenshot"
    assert stopped == [(1234, check.signal.SIGTERM)]


@pytest.mark.parametrize("domain", ["enterprise", "consumer", "health_tech"])
@pytest.mark.parametrize("mode", ["replay", "sample"])
def test_linux_e2e_selects_requested_idea(tmp_path, monkeypatch, domain, mode):
    monkeypatch.delenv("E2E_OCCUPY_PORT", raising=False)
    check = load("linux_e2e_scenario", ROOT / "scripts/desktop/e2e_linux.py")
    monkeypatch.setattr(check, "socket", SimpleNamespace(socket=mock_e2e_socket))
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    selected = []
    monkeypatch.setenv("E2E_SCENARIO", domain)
    monkeypatch.setenv("E2E_MODE", mode)
    monkeypatch.delenv("E2E_RECORD", raising=False)
    monkeypatch.setattr(check.sys, "argv", ["check", "/test.AppImage", str(tmp_path)])
    def launch(*args, **kwargs):
        flags = {"LLM_OFFLINE", "LLM_CASSETTE_MODE", "GUARDRAILS_MODE"}
        assert flags.intersection(kwargs["env"]) == (flags if mode == "replay" else set())
        check.os.write(kwargs["pass_fds"][0], b'connect(3, inet_addr("127.0.0.1")) = 0\n')
        return SimpleNamespace(pid=1234, stdout=BytesIO(), poll=lambda: None, wait=lambda **kwargs: 0)

    monkeypatch.setattr(check.subprocess, "Popen", launch)
    monkeypatch.setattr(check.os, "killpg", lambda *args: None)
    monkeypatch.setattr(check.Journey, "run", lambda journey: selected.append(journey.scenario))
    assert check.main() == 0
    assert selected == [scenario]


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_extended_checks_reject_missing_safety_and_saved_work(domain):
    from copy import deepcopy

    check = load("linux_e2e_extended", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    person = {"id": domain, "email": f"person@{domain}.example.invalid",
              "contact_source_url": f"https://{domain}.example.invalid/contact"}
    draft = {"doc_id": domain, "contact_id": domain, "to": person["email"], "sample": True, "state": "draft",
             "body": f'Sample email, not for sending. From: sender@{domain}.example.invalid.\n'
                     f'Your email address is listed publicly on {domain}.example.invalid.\n'
                     'If this is not relevant, just reply "no thanks" and I will not email you again.\n'
                     'Business outreach · Sample sender address, not for sending'}
    state = {"person": person, "prior_drafts": [], "request": "Draft an interview email.",
             "chat": [{"id": "request", "role": "user", "body_md": "Draft an interview email."},
                      {"id": "reply", "role": "assistant", "body_md": "**SAMPLE draft**\n" + draft["body"]}],
             "journey": {"drafts": [draft], "results": {"sent": 0}, "prospects": [person]}}
    assert check.step_ready("draft", state, scenario)
    for field, value in (("sample", False), ("state", "sent"), ("sent_at", "saved"), ("to", "other@example.invalid")):
        invalid = deepcopy(state)
        invalid["journey"]["drafts"][0][field] = value
        assert not check.step_ready("draft", invalid, scenario)
    for line in draft["body"].splitlines():
        invalid = deepcopy(state)
        invalid["journey"]["drafts"][0]["body"] = draft["body"].replace(line, "")
        assert not check.step_ready("draft", invalid, scenario)
        invalid = deepcopy(state)
        invalid["chat"][-1]["body_md"] = invalid["chat"][-1]["body_md"].replace(line, "")
        assert not check.step_ready("draft", invalid, scenario)
    for field, value in (("body_md", "**Draft**"), ("confirmed_action", {"state": "pending"}), ("actions", [{"confirmation": True}])):
        invalid = deepcopy(state)
        invalid["chat"][-1][field] = value
        assert not check.step_ready("draft", invalid, scenario)
    invalid = deepcopy(state)
    invalid["journey"]["results"]["sent"] = 1
    assert not check.step_ready("draft", invalid, scenario)
    invalid = deepcopy(state)
    invalid["prior_drafts"] = [domain]
    assert not check.step_ready("draft", invalid, scenario)

    question = {"role": "assistant", "kind": "text", "citations": [{"question": True}],
                "body_md": f'{person["email"]} (CA): For a first email, you need their consent. What is your basis?',
                "actions": [{"action": "gtm.eligibility", "payload": {"lawful_basis": "consent"}}]}
    state["chat"][-1] = question
    assert check.step_ready("consent country", state, scenario)
    for body in ("What is your basis? Canada needs consent.", "Canada does not need consent. What is your basis?",
                 "US: You need consent. What is your basis?"):
        invalid = deepcopy(state)
        invalid["chat"][-1]["body_md"] = body
        assert not check.step_ready("consent country", invalid, scenario)
    invalid = deepcopy(state)
    invalid["chat"][-1]["actions"].append({"payload": {"lawful_basis": "legitimate_interest"}})
    assert not check.step_ready("consent country", invalid, scenario)

    saved = {"venture": {"id": domain, "name": scenario["name"], "raw_idea": scenario["idea"]},
             "chat": deepcopy(state["chat"]), "prospects": [person]}
    restarted = {"health": {"status": "ok"}, "before_restart": deepcopy(saved), **deepcopy(saved)}
    assert check.step_ready("restart", restarted, scenario)
    for field in ("venture", "chat", "prospects"):
        invalid = deepcopy(restarted)
        invalid[field] = {} if field == "venture" else []
        assert not check.step_ready("restart", invalid, scenario)

    settings = {"settings_opened": True, "provider": {"sample_mode": True, "secrets_set": {"api_key": False}},
                "screen": {"text": "Your local settings. Your AI provider. Leave your key blank to keep the one you saved. API key",
                           "password_lengths": [0], "links": ["https://supymem.com/docs"]}}
    assert check.step_ready("settings", settings, scenario)
    for field, value in (("settings_opened", False), ("screen", {"text": "Opening your workspace"}),
                         ("provider", {"secrets_set": {"api_key": True}})):
        assert not check.step_ready("settings", settings | {field: value}, scenario)
    assert not check.step_ready("settings", settings | {"screen": settings["screen"] | {"password_lengths": [12]}}, scenario)
    clean = {"pages": [settings["screen"]], "chat": state["chat"]}
    assert check.step_ready("no hosted links", clean, scenario)
    assert not check.step_ready("no hosted links", clean | {"unread_pages": True}, scenario)
    assert not check.step_ready("no hosted links", {"pages": [], "chat": []}, scenario)
    for url in ("https://supymem.com/auth", "//WWW.SUPYMEM.COM/sign-in", "https://supymem.com/account#account-buy", "https://supymem.com/account",
                "https://supymem.com/%62uy", "https://supymem.com/pricing"):
        assert not check.step_ready("no hosted links", {"pages": [{"text": "Settings", "links": [url]}]}, scenario)
        assert not check.step_ready("no hosted links", clean | {"chat": [{"body_md": f"[Continue]({url})"}]}, scenario)
        assert not check.step_ready("no hosted links", clean | {"chat": [{"actions": [{"payload": {"href": url}}]}]}, scenario)


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_draft_request_targets_the_saved_person(monkeypatch, domain):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import customers

    check = load("linux_e2e_draft_request", ROOT / "scripts/desktop/e2e_linux.py")
    names = {"enterprise": "Mira Stone", "consumer": "Theo Brook"}
    person = {"id": domain, "name": names[domain], "email": f"person@{domain}.example.invalid"}
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(customers, "_view", lambda venture_id: {"prospects": [person]})
    monkeypatch.setattr(customers, "_named_people", lambda venture_id, view, text: [
        {"contact_id": person["id"], "name": person["name"]}] if customers._names_person(text, person["name"]) else [])
    for country, expected in (("US", "US"), ("Canada", "CA")):
        request = check.draft_request(person, country)
        selected = customers.command(domain, request)
        assert selected and selected[0] == "draft_named"
        assert selected[1]["contact_id"] == person["id"]
        assert customers._request_country(request) == expected
        assert "Do not send anything" in request and person["email"] not in request
    with pytest.raises(RuntimeError):
        check.draft_request(person | {"name": person["email"]}, "US")


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_consent_accepts_the_product_question_contract(monkeypatch, tmp_path, domain):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.chat import customers
    from app.state import MEM

    check = load("linux_e2e_consent_contract", ROOT / "scripts/desktop/e2e_linux.py")
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    person = {"id": domain, "name": scenario["answers"]["customer"], "email": f"person@{domain}.example.invalid"}
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("E2E_MODE", "sample")
    monkeypatch.setitem(MEM, customers._eligibility_key(domain), None)
    question = customers._eligibility_question(domain, person, {"contact_id": domain, "country": "CA", "draft_request": scenario["people"]})
    assert question["kind"] == "text" and {"question": True} in question["citations"]
    state = {"request": scenario["people"], "person": person, "chat": [{"role": "user", "body_md": scenario["people"]},
             {"role": "assistant", **question}], "journey": {"drafts": [], "results": {"sent": 0}}}
    assert check.step_ready("consent country", state, scenario)
    journey = check.Journey(tmp_path, scenario)
    journey.step = "consent country"
    journey.app = SimpleNamespace(poll=lambda: None)
    journey.state = lambda: state
    monkeypatch.setattr(journey, "capture", lambda: (tmp_path / "consent_country.png").write_bytes(b"screenshot"))
    journey.wait()
    evidence = json.loads((tmp_path / "consent_country_state.json").read_text())
    assert evidence["chat"] == state["chat"] and evidence["person"] == person
    state["chat"][-1] = {"role": "assistant", **question, "citations": []}
    assert not check.step_ready("consent country", state, scenario)


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
@pytest.mark.parametrize("draft_fails", [False, True])
@pytest.mark.parametrize("window", ["available", "missing", "missing_with_hosted_link"])
def test_linux_e2e_extended_restart_reuses_data_and_reports_every_check(tmp_path, monkeypatch, capsys, domain, draft_fails, window):
    check = load("linux_e2e_extended_restart", ROOT / "scripts/desktop/e2e_linux.py")
    monkeypatch.setattr(check, "socket", SimpleNamespace(socket=mock_e2e_socket))
    scenario = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())[domain]
    monkeypatch.setenv("E2E_MODE", "sample")
    monkeypatch.setenv("E2E_SCENARIO", domain)
    for name in ("E2E_RECORD", "E2E_OCCUPY_PORT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(check.sys, "argv", ["check", "/test.AppImage", str(tmp_path)])
    launches, signals, steps, requests = [], [], [], []
    people = [{"id": str(index), "name": f"Person {index} {domain}", "email": f"person{index}@{domain}.example.invalid",
               "source_url": f"https://{domain}.example.invalid/people/{index}"} for index in range(2)]
    saved = {"venture": {"id": domain, "name": scenario["name"], "raw_idea": scenario["idea"]},
             "chat": [{"role": "assistant", "actions": [{"action": "gtm.eligibility", "payload": {"lawful_basis": "consent"}}]}],
             "journey": {"prospects": people, "drafts": []}}
    unsafe = window == "missing_with_hosted_link"
    if unsafe:
        saved["chat"][0]["body_md"] = "[Buy more](https://supymem.com/account#account-buy)"

    def launch(command, **options):
        data = Path(options["env"]["XDG_DATA_HOME"])
        data.mkdir(exist_ok=True)
        record = data / "saved.json"
        if launches:
            assert command == launches[0][0]
            assert options["env"] == launches[0][1]["env"]
            assert options["stdout"] is launches[0][1]["stdout"]
            assert json.loads(record.read_text()) == saved
        else:
            record.write_text(json.dumps(saved))
        launches.append((command, options))
        check.os.write(options["pass_fds"][0], b'connect(3, inet_addr("127.0.0.1")) = 0\n')
        process = SimpleNamespace(pid=1200 + len(launches), running=True, stdout=BytesIO())
        process.poll = lambda: None if process.running else 0
        process.wait = lambda **kwargs: setattr(process, "running", False)
        return process

    screen = {"text": "Your local settings", "links": [], "documents": 1}
    monkeypatch.setattr(check, "Desktop", lambda: SimpleNamespace(
        read=lambda click=None: screen | {"clicked": click == "Settings"}, close=lambda: None)
        if window == "available" else None)
    monkeypatch.setattr(check, "TIMEOUT", 0.01)
    monkeypatch.setattr(check.Journey, "ports", lambda journey: {"console": 3100})
    monkeypatch.setattr(check.Journey, "capture", lambda journey: None)
    monkeypatch.setattr(check.subprocess, "Popen", launch)
    monkeypatch.setattr(check.os, "killpg", lambda *arguments: signals.append(arguments))
    monkeypatch.setattr(check.Journey, "state", lambda journey: saved)

    def request(journey, path, *, payload=None):
        requests.append((path, payload))
        return {"items": saved["chat"]} if payload is None else {}

    def wait(journey):
        steps.append(journey.step)
        if journey.step == "draft" and draft_fails:
            raise RuntimeError("Your draft is missing its source line. Review your sample draft, then retry.")
        check.save_result(journey.output, journey.step, True, "Your check passed.")
        print(f"PASS {journey.step}")

    def run(journey):
        journey.venture_id = domain
        journey.desktop = check.Desktop()
        journey.run_extended()
        assert journey.settings_opened == (window == "available")
        assert journey.before_restart["prospects"] == people

    monkeypatch.setattr(check.Journey, "get", request)
    monkeypatch.setattr(check.Journey, "wait", wait)
    monkeypatch.setattr(check.Journey, "run", run)
    assert check.main() == int(draft_fails or window != "available")
    assert steps == list(check.ADDED_STEPS if window == "available" else check.ADDED_STEPS[:3])
    assert signals == [(1201, check.signal.SIGTERM), (1202, check.signal.SIGTERM)]
    results = json.loads((tmp_path / "results.json").read_text())
    assert len(results) == 6 and sum(result["passed"] is False for result in results) == int(draft_fails) + 2 * int(window != "available")
    assert not any(result.get("skipped") for result in results)
    assert results[-1]["step"] == "no hosted requests" and results[-1]["passed"] is True
    assert all(result["screenshot"] is None for result in results)
    captured = capsys.readouterr()
    for result in results:
        status = "SKIP" if result.get("skipped") else "PASS" if result["passed"] else "FAIL"
        assert f'{status} {result["step"]}' in captured.out + captured.err
        if result.get("skipped"):
            assert result["passed"] is None and "was not verified" in result["message"]
    messages = [payload["message"] for path, payload in requests if payload and "message" in payload]
    assert any(people[0]["name"] in message and "in US" in message and "Do not send anything" in message for message in messages)
    assert any(people[1]["name"] in message and "in Canada" in message and "Do not send anything" in message for message in messages)
    assert not any(payload and payload.get("action") == "gtm.send" for path, payload in requests)


@pytest.mark.parametrize("domain", ["enterprise", "consumer"])
def test_linux_e2e_accessibility_reads_links_but_never_password_values(tmp_path, capsys, domain):
    check = load("linux_e2e_accessibility", ROOT / "scripts/desktop/e2e_linux.py")
    desktop = check.Desktop.__new__(check.Desktop)
    expressions = []

    def evaluate(expression):
        expressions.append(expression)
        assert ".map(node => node.value.length)" in expression
        assert ".map(node => node.value)" not in expression
        return {"text": domain, "documents": 1, "password_lengths": [0],
            "links": [f"https://{domain}.example.invalid/guide"],
            "clicked": 'const label = "Settings"' in expression}

    desktop.evaluate = evaluate
    screen = desktop.read()
    assert screen["documents"] == 1 and screen["password_lengths"] == [0]
    assert screen["links"] == [f"https://{domain}.example.invalid/guide"]
    assert desktop.read(click="Settings")["clicked"] is True
    assert len(expressions) == 2

    def unreadable(click=None):
        raise OSError("Your window is unavailable. Check it manually, then rerun.")

    journey = check.Journey(tmp_path, {})
    journey.step = "settings"
    journey.ports = lambda: {"console": 3100}
    closed = []
    journey.desktop = SimpleNamespace(read=unreadable, close=lambda: closed.append(True))
    with pytest.raises(check.UnreadableWindow):
        journey.read_screen()
    assert closed == [True]
    with pytest.raises(check.UnreadableWindow, match="window evidence is missing"):
        journey.skip()
    assert not (tmp_path / "results.json").exists()
    assert "SKIP settings" not in capsys.readouterr().out


@pytest.mark.parametrize("domain", ["fleet-tools.invalid", "recipe-club.invalid"])
def test_local_payload_copies_code_but_never_credentials_or_saved_work(tmp_path, domain, monkeypatch):
    builder = load("prepare_local_stack", ROOT / "scripts/desktop/prepare_local_stack.py")
    source = tmp_path / domain
    source.mkdir()
    (source / "app.py").write_text("value = 1\n")
    for filename in (".env", ".env.local", "settings.env", "saved.sqlite3", "saved.sqlite3-wal", "private.key", "credentials.json"):
        (source / filename).write_text("private")
    (source / "linked.py").symlink_to(source / ".env")
    destination = tmp_path / "payload"
    builder.copy_source(source, destination)
    assert {path.name for path in destination.iterdir()} == {"app.py"}
    assert (source / "saved.sqlite3").read_text() == "private"
    certificates = source / "certifi"
    certificates.mkdir()
    (certificates / "cacert.pem").write_text(domain)
    inputs = tmp_path / "services/company_os"
    inputs.mkdir(parents=True)
    (inputs / "requirements.runtime.in").write_text("certifi\n")
    (inputs / "pyproject.toml").write_text('[project]\ndependencies = []\n')
    runtime_project = tmp_path / "packages/desktop_runtime"
    runtime_project.mkdir(parents=True)
    (runtime_project / "pyproject.toml").write_text('[project]\nname = "desktop-runtime"\nversion = "0.1.0"\ndependencies = []\n')
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.sysconfig, "get_path", lambda _name: str(source))
    distribution = SimpleNamespace(metadata={"Name": "certifi"}, version="1", requires=[],
        files=[Path("certifi/cacert.pem"), Path("private.key")], locate_file=lambda entry: source / entry)
    monkeypatch.setattr(builder.metadata, "distribution", lambda _name: distribution)
    builder.copy_dependencies(destination)
    assert (destination / "certifi/cacert.pem").read_text() == domain
    assert not (destination / "private.key").exists()
    if (ROOT / ".github/workflows/ci.yml").is_file():
        workflow_text = (ROOT / ".github/workflows/desktop-release.yml").read_text()
        workflow = yaml.load(workflow_text, Loader=yaml.BaseLoader)
        assert workflow["on"] == {"push": {"tags": ["desktop-v*"]}, "workflow_dispatch": ""}
        assert workflow["permissions"] == {"contents": "read"}
        targets = {row["target"] for row in workflow["jobs"]["build"]["strategy"]["matrix"]["include"]}
        assert targets == {"universal-apple-darwin", "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"}
        assert "refs/heads/" not in workflow_text
        assert not workflow["jobs"]["build"]["env"].keys() & {"APPLE_CERTIFICATE", "APPLE_PASSWORD", "APPLE_API_KEY_PATH"}
        assert "--node-linker=hoisted" in workflow_text
    requirements = (ROOT / "services/company_os/requirements.runtime.in").read_text().splitlines()
    assert any("asyncio" in builder.Requirement(value).extras for value in requirements
               if value and not value.startswith("#"))
    greenlet = next(value for value in (ROOT / "services/company_os/requirements.runtime.lock").read_text().splitlines()
                    if value.startswith("greenlet=="))
    requirement = builder.Requirement(greenlet.rstrip(" \\"))
    assert requirement.marker is None or requirement.marker.evaluate({"platform_machine": "arm64"})


def test_local_review_config_uses_bundled_pages_and_no_distribution(tmp_path, monkeypatch):
    script = load("prepare_local_review", ROOT / "scripts/desktop/prepare_local_review.py")
    monkeypatch.setattr(script, "ROOT", tmp_path)
    monkeypatch.setattr(script.platform, "system", lambda: "Darwin")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("APPLE_SIGNING_IDENTITY", raising=False)
    splash = tmp_path / "src-tauri/splashscreen"
    splash.mkdir(parents=True)
    for name in ("index.html", "setup.html", "coach.html", "update.html", "offline.html"):
        (splash / name).write_text("ok")
    (tmp_path / "src-tauri/tauri.conf.json").write_text(json.dumps({
        "productName": "Supymem", "identifier": "com.companyos.app",
        "build": {"frontendDist": "splashscreen"},
        "app": {"windows": [{"label": "main", "title": "Supymem"}, {"label": "setup", "title": "Set up this Mac"}]},
    }))

    script.main()

    config = json.loads((tmp_path / "src-tauri/tauri.local-review.json").read_text())
    assert config["identifier"] == "com.companyos.localreview"
    assert config["productName"] == "Supymem Local Review"
    assert config["app"]["windows"][0]["title"] == "Supymem Local Review"
    assert config["app"]["windows"][1] == {"label": "setup", "title": "Set up this Mac"}
    assert config["bundle"]["createUpdaterArtifacts"] is False
    assert config["bundle"]["macOS"]["infoPlist"] == "Info.local-review.plist"
    assert config["plugins"]["updater"]["endpoints"] == []
    info = plistlib.loads((ROOT / "src-tauri/Info.local-review.plist").read_bytes())
    assert info["SupymemLocalReviewOnly"] is True
    assert info["CFBundleURLTypes"][0]["CFBundleURLSchemes"] == ["supymem"]


def test_local_review_is_refused_by_release_collector_and_publisher(tmp_path, monkeypatch):
    collector = load("collect_macos_review", ROOT / "scripts/desktop/collect_macos.py")
    publisher = load("publish_macos_review", ROOT / "scripts/publish_desktop_downloads.py")
    monkeypatch.setattr(collector, "ROOT", tmp_path)
    monkeypatch.setattr(collector.platform, "system", lambda: "Darwin")
    (tmp_path / "src-tauri").mkdir()
    (tmp_path / "src-tauri/tauri.conf.json").write_text(json.dumps({
        "identifier": "com.companyos.app", "version": "0.1.52",
    }))
    # Even renaming a review bundle to the public filename cannot publish it.
    app = tmp_path / "src-tauri/target/release/bundle/macos/Supymem.app"
    info = app / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "com.companyos.localreview",
                                    "SupymemLocalReviewOnly": True}))

    with pytest.raises(SystemExit, match="cannot enter release artifacts"):
        collector.main()
    with pytest.raises(ValueError, match="cannot be published"):
        publisher._verify_macos_app(app, "darwin-aarch64", "0.1.52")


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_self_hosted_desktop_is_opt_in_and_errors_keep_your_log_path(tmp_path, domain):
    source = (ROOT / "src-tauri/src/local_stack.rs").read_text()
    assert 'cfg!(feature = "local-stack")' in source
    assert 'std::env::var("SUPYMEM_MODE")' in source
    assert 'value.trim().eq_ignore_ascii_case("local")' in source
    assert "local_stack_readiness_failure_and_owned_shutdown" in source
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    assert config["build"]["features"] == ["local-stack", "tauri/custom-protocol"]
    assert config["bundle"]["externalBin"] == []
    assert config["bundle"]["resources"]["resources/local/"] == "local/"
    assert config["plugins"]["updater"]["endpoints"] == []
    assert config["bundle"]["createUpdaterArtifacts"] is False
    assert "local-stack" not in (ROOT / "src-tauri/tauri.conf.json").read_text()
    lifecycle = (ROOT / "src-tauri/src/lib.rs").read_text()
    assert "sup.local_failure()" in lifecycle and "show_local_error" in lifecycle
    failure = lifecycle.split("Err(err) => {", 1)[-1]
    assert "local_stack::is_local()" in failure
    html = (ROOT / "src-tauri/splashscreen/local-error.html").read_text()
    assert 'src="local-error.js"' in html and "Retry" in html
    log_path = str(tmp_path / domain / '<private & saved>/desktop.log')
    script = ROOT / "src-tauri/splashscreen/local-error.js"
    result = subprocess.run(["node", "-e", """
const fs = require('node:fs');
const vm = require('node:vm');
const elements = Object.fromEntries(['status', 'progress', 'retry', 'heading', 'log-path', 'failure-help', 'failure-details'].map(name =>
  [name, { textContent: '', addEventListener() {} }]));
const search = '?' + new URLSearchParams({ log: process.argv[2] });
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: { getElementById: name => elements[name] }, window: { location: { search } }, URLSearchParams,
});
process.stdout.write(elements['log-path'].textContent);
""", str(script), log_path], check=True, capture_output=True, text=True)
    assert result.stdout == log_path
    assert "SUPYMEM_DESKTOP_INSTANCE_ID" in source
    assert "instance-specific API and worker readiness" in (ROOT / "docs/self-hosting/DESKTOP_RELEASE.md").read_text()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_mac_signing_plan_signs_nested_code_inside_out(tmp_path, domain):
    release = load("local_mac_signing", ROOT / "scripts/desktop/prepare_local_release.py")
    app = tmp_path / f"{domain}.app"
    binaries = ["Contents/MacOS/desktop", "Contents/Resources/local/runtime/arm/bin/python3",
                "Contents/Resources/local/runtime/intel/bin/node",
                "Contents/Resources/local/runtime/arm/lib/module.so",
                "Contents/Resources/local/runtime/intel/lib/addon.node",
                "Contents/Frameworks/Engine.framework/Versions/A/Engine",
                "Contents/Resources/local/runtime/arm/lib/libpython.dylib"]
    for name in binaries:
        binary = app / name
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    (app / "Contents/Resources/text.so").write_text("not executable code")
    (app / "Contents/Resources/alias").symlink_to(app / binaries[1])
    commands = release.signing_plan(app, "Developer ID Application: Fixture", tmp_path / "entitlements.plist")
    signed = [Path(command[-1]) for command in commands if command[0] == "codesign" and "--sign" in command]
    assert set(signed) == {app / name for name in binaries} | {app, app / "Contents/Frameworks/Engine.framework"}
    assert signed[-1] == app
    assert signed.index(app / binaries[5]) < signed.index(app / "Contents/Frameworks/Engine.framework")
    assert all("--options" in command and "runtime" in command and "--entitlements" in command
               for command in commands if "--sign" in command)
    assert not any("--deep" in command for command in commands if "--sign" in command)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_universal_payload_requires_both_architectures(tmp_path, monkeypatch, domain):
    release = load("local_mac_payload", ROOT / "scripts/desktop/prepare_local_release.py")
    resources = tmp_path / domain
    targets = {"aarch64-apple-darwin": "arm64", "x86_64-apple-darwin": "x86_64"}
    def architectures(command, **kwargs):
        binary = Path(command[-1])
        return targets[binary.parents[1].name] + "\n"
    monkeypatch.setattr(release.subprocess, "check_output", architectures)
    for target in targets:
        runtime = resources / "local/runtime" / target
        (runtime / "bin").mkdir(parents=True)
        for name in ("python3", "node"):
            (runtime / "bin" / name).write_bytes(b"\xcf\xfa\xed\xfe" + bytes(28))
    release.assert_payloads(resources, "universal-apple-darwin")
    monkeypatch.setattr(release.subprocess, "check_output", lambda *args, **kwargs: "arm64\n")
    with pytest.raises(ValueError, match="x86_64"):
        release.assert_payloads(resources, "universal-apple-darwin")
    monkeypatch.setattr(release.subprocess, "check_output", architectures)
    (resources / "local/runtime/x86_64-apple-darwin/bin/node").unlink()
    with pytest.raises(ValueError, match="Intel|x86_64"):
        release.assert_payloads(resources, "universal-apple-darwin")


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_mac_dry_run_never_prints_signing_secrets(tmp_path, monkeypatch, capsys, domain):
    release = load("local_mac_plan", ROOT / "scripts/desktop/prepare_local_release.py")
    app = tmp_path / f"{domain}.app"
    app.mkdir()
    for name in ("APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_SIGNING_IDENTITY", "APPLE_ID",
                 "APPLE_PASSWORD", "APPLE_TEAM_ID"):
        monkeypatch.setenv(name, "private-fixture-" + name)
    monkeypatch.setattr(release.subprocess, "run", lambda *args, **kwargs: pytest.fail("dry run executed a command"))
    release.sign_mac(app, app.with_suffix(".dmg"), plan=True)
    output = capsys.readouterr().out
    assert "codesign --force" in output and "--options runtime" in output
    assert "xcrun notarytool submit" in output and "--wait" in output
    assert "xcrun stapler staple" in output and "hdiutil create" in output
    assert "private-fixture-" not in output


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_mac_fork_build_with_empty_secrets_uses_ad_hoc(tmp_path, monkeypatch, capsys, domain):
    release = load("local_mac_fork", ROOT / "scripts/desktop/prepare_local_release.py")
    for name in release.APPLE_VARIABLES:
        monkeypatch.setenv(name, "")
    release.sign_mac(tmp_path / f"{domain}.app", tmp_path / f"{domain}.dmg", plan=True)
    output = capsys.readouterr().out
    assert "--sign -" in output and "notarytool" not in output
    assert "right-click" in output and "Open" in output


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
@pytest.mark.skipif(not (ROOT / ".github/workflows/ci.yml").is_file(), reason="The public export does not include private release workflows.")
def test_local_release_gates_artifacts_on_installed_app_and_keeps_drafts(domain):
    build = yaml.load((ROOT / ".github/workflows/desktop-release.yml").read_text(), Loader=yaml.BaseLoader)
    steps = build["jobs"]["build"]["steps"]
    gate = next(index for index, step in enumerate(steps) if "installed-smoke" in step.get("run", ""))
    upload = next(index for index, step in enumerate(steps) if step.get("uses", "").startswith("actions/upload-artifact@"))
    assert gate < upload
    assert not steps[gate].get("continue-on-error")
    assert "hdiutil attach" in steps[gate]["run"] and "ditto" in steps[gate]["run"]
    assert "sign-mac" in "\n".join(step.get("run", "") for step in steps)
    workflow = (ROOT / ".github/workflows/desktop-local-release.yml").read_text()
    assert "--draft" in workflow and "--latest" not in workflow
    assert "workflow_call:" in workflow
    assert domain not in json.dumps(build) and domain not in workflow


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_local_notarization_rejection_blocks_release(tmp_path, monkeypatch, domain):
    release = load("local_notary_rejection", ROOT / "scripts/desktop/prepare_local_release.py")
    monkeypatch.setattr(release, "_run", lambda *args, **kwargs: '{"status": "Invalid"}')
    with pytest.raises(ValueError, match="notarytool log"):
        release._notarize(tmp_path / f"{domain}.dmg", [], plan=False)


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_linux_installer_gate_stages_fake_tools_under_home(tmp_path, monkeypatch, domain):
    monkeypatch.syspath_prepend(str(ROOT / "scripts/desktop"))
    gate = load("linux_installer_tool_root", ROOT / "scripts/desktop/installer_gate_linux.py")
    home = tmp_path / domain
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("E2E_MODE", "sample")
    monkeypatch.setattr(gate.sys, "argv", ["installer_gate_linux.py", "app.AppImage", str(tmp_path / "evidence")])
    monkeypatch.setattr(gate.os, "umask", lambda mode: None)

    def check_directory(*, prefix, dir=None):
        assert dir == home
        raise SystemExit("Found the private tool directory")

    monkeypatch.setattr(gate.tempfile, "TemporaryDirectory", check_directory)
    with pytest.raises(SystemExit, match="Found the private tool directory"):
        gate.main()


@pytest.mark.parametrize("domain", ["procurement.example", "gardening.example"])
def test_installed_mac_gate_saves_then_reopens_the_same_private_key(tmp_path, monkeypatch, domain):
    import io
    from types import SimpleNamespace

    release = load("local_installed_gate", ROOT / "scripts/desktop/prepare_local_release.py")
    app = tmp_path / "installed" / f"{domain}.app"
    info = app / "Contents/Info.plist"
    info.parent.mkdir(parents=True)
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    numeric = config["version"].partition("-")[0]
    metadata = {"CFBundleIdentifier": config["identifier"], "CFBundleName": domain, "CFBundleExecutable": "desktop",
                "CFBundleShortVersionString": numeric, "CFBundleVersion": numeric, "SupymemReleaseVersion": config["version"]}
    info.write_bytes(plistlib.dumps(metadata))
    monkeypatch.setattr(release.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(release.platform, "machine", lambda: "arm64")
    state = {"values": {"provider": "builtin"}, "secrets_set": {"api_key": False}}
    saved = []
    commands = []
    launches = []
    def open_request(request, **kwargs):
        if request.data:
            saved.append(json.loads(request.data)["api_key"])
            state["secrets_set"]["api_key"] = True
        return io.BytesIO(json.dumps(state).encode())
    monkeypatch.setattr(release, "build_opener", lambda *args: SimpleNamespace(open=open_request))
    class Process:
        def __init__(self, command, **kwargs):
            launches.append(command)
        def poll(self):
            return None
        def wait(self, **kwargs):
            return 0
    monkeypatch.setattr(release.subprocess, "Popen", Process)
    def run(command, **kwargs):
        commands.append((command, kwargs))
        return SimpleNamespace(stdout="")
    monkeypatch.setattr(release.subprocess, "run", run)
    release.installed_smoke(app)
    assert len(launches) == 2 and all(command == ["open", "-n", "-W", str(app)] for command in launches)
    assert len(saved) == 1
    assert commands[0][0][-2:] == [str(app / "Contents/MacOS/desktop"), str(app / "Contents/Resources")]
    reads = [kwargs["input"] for command, kwargs in commands if "input" in kwargs]
    assert reads == saved * 2
    assert len([command for command, _ in commands if command[0] == "osascript"]) == 2
    info.write_bytes(plistlib.dumps({**metadata, "CFBundleShortVersionString": f"{numeric}-rc.9"}))
    with pytest.raises(ValueError, match="version"):
        release.installed_smoke(app)
    assert len(launches) == 2


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("target", ["aarch64-apple-darwin", "x86_64-unknown-linux-gnu", "x86_64-pc-windows-msvc"])
@pytest.mark.parametrize("scenario", ["warm", "cold_offline", "bad_download", "bad_cache", "unsafe_archive"])
def test_standalone_node_download_verifies_and_reuses_offline_cache(tmp_path, monkeypatch, domain, target, scenario):
    import hashlib
    import io
    import tarfile
    import urllib.error
    import zipfile

    builder = load("standalone_node", ROOT / "scripts/desktop/prepare_local_stack.py")
    windows = "windows" in target
    system = "win" if windows else "darwin" if "darwin" in target else "linux"
    architecture = "arm64" if target.startswith("aarch64") else "x64"
    name = f"node-v{builder.NODE_VERSION}-{system}-{architecture}"
    filename = name + (".zip" if windows else ".tar.gz")
    executable = "node.exe" if windows else "bin/node"
    content = domain.encode()
    archive = io.BytesIO()
    if windows:
        with zipfile.ZipFile(archive, "w") as output:
            for member in (executable, "LICENSE"):
                info = zipfile.ZipInfo(name + "/" + member)
                if scenario == "unsafe_archive" and member == executable:
                    info.external_attr = 0o120777 << 16
                output.writestr(info, content)
    else:
        with tarfile.open(fileobj=archive, mode="w:gz") as output:
            for member in (executable, "LICENSE"):
                info = tarfile.TarInfo(name + "/" + member)
                if scenario == "unsafe_archive" and member == executable:
                    info.type = tarfile.SYMTYPE
                    info.linkname = "/usr/bin/node"
                else:
                    info.size = len(content)
                output.addfile(info, io.BytesIO(content))
    checksum = hashlib.sha256(archive.getvalue()).hexdigest()
    sums = tmp_path / "SHASUMS256.txt"
    sums.write_text(f"{checksum}  {filename}\n")
    monkeypatch.setattr(builder, "NODE_SHASUMS", sums)
    cache = tmp_path / domain / "cache"
    monkeypatch.setattr(builder, "node_cache_dir", lambda: cache)
    calls = []

    def source(url, **_kwargs):
        calls.append(url)
        if scenario == "cold_offline" or len(calls) > 1:
            raise urllib.error.URLError("offline")
        assert url == f"https://nodejs.org/dist/v{builder.NODE_VERSION}/{filename}"
        return io.BytesIO(b"broken download" if scenario == "bad_download" else archive.getvalue())

    monkeypatch.setattr(builder.urllib.request, "urlopen", source)
    if scenario == "cold_offline":
        with pytest.raises(SystemExit, match="Connect to the internet.*./start"):
            builder.standalone_node(target)
        return
    if scenario == "bad_download":
        with pytest.raises(SystemExit, match="SHA-256.*./start"):
            builder.standalone_node(target)
        assert not (cache / filename).exists()
        return
    if scenario == "unsafe_archive":
        with pytest.raises(SystemExit, match="not a regular file.*./start"):
            builder.standalone_node(target)
        return
    node = builder.standalone_node(target)
    assert node.read_bytes() == content
    assert (node.parent if windows else node.parent.parent).joinpath("LICENSE").read_bytes() == content
    assert node.stat().st_mode & 0o111
    if scenario == "bad_cache":
        (cache / filename).write_bytes(b"tampered archive")
        with pytest.raises(SystemExit, match="SHA-256.*./start"):
            builder.standalone_node(target)
    else:
        node.write_bytes(b"tampered extracted executable")
        assert builder.standalone_node(target).read_bytes() == content
    assert len(calls) == 1


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("system,dependency,allowed", [
    ("Darwin", "/usr/lib/libSystem.B.dylib", True),
    ("Darwin", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation", True),
    ("Darwin", "@loader_path/../lib/libpython3.12.dylib", True),
    ("Darwin", "@rpath/libpython3.12.dylib", True),
    ("Darwin", "@rpath/libnode.141.dylib", False),
    ("Darwin", "/opt/homebrew/opt/node/lib/libnode.141.dylib", False),
    ("Darwin", "/usr/local/opt/python/lib/libpython3.12.dylib", False),
    ("Linux", "libc.so.6 => /lib/x86_64-linux-gnu/libc.so.6 (0x1234)", True),
    ("Linux", "/lib64/ld-linux-x86-64.so.2 (0x1234)", True),
    ("Linux", "libnode.so.141 => /home/linuxbrew/lib/libnode.so.141 (0x1234)", False),
    ("Linux", "libpython3.12.so => not found", False),
])
def test_payload_dependency_parser_rejects_external_or_missing_libraries(tmp_path, domain, system, dependency, allowed):
    builder = load("payload_dependencies", ROOT / "scripts/desktop/prepare_local_stack.py")
    runtime = tmp_path / domain
    library = runtime / "lib/libpython3.12.dylib"
    library.parent.mkdir(parents=True)
    library.touch()
    binary = runtime / "bin/python3"
    binary.parent.mkdir()
    output = f"{binary}:\n\t{dependency} (compatibility version 1.0.0, current version 1.0.0)\n" if system == "Darwin" else f"\t{dependency}\n"
    errors = builder.dependency_errors(output, binary, runtime, system, ["@loader_path/../lib"])
    assert bool(errors) is not allowed
    if errors:
        assert dependency.split(" (", 1)[0] in errors[0]


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("failure", [None, "node", "python", "dependency", "version"])
def test_payload_verification_smokes_isolated_runtime_and_checks_dependencies(tmp_path, monkeypatch, domain, failure):
    builder = load("verify_payload", ROOT / "scripts/desktop/prepare_local_stack.py")
    monkeypatch.setattr(builder, "ROOT", tmp_path)
    monkeypatch.setattr(builder.sys, "platform", "linux")
    monkeypatch.setenv("PAYLOAD_TEST_SECRET", "must-not-be-inherited")
    target = "x86_64-unknown-linux-gnu"
    payload = tmp_path / "src-tauri/resources/local"
    runtime = payload / "runtime" / target
    for name in ("bin/node", "bin/python3", "lib/extension.so"):
        path = runtime / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        assert "PAYLOAD_TEST_SECRET" not in kwargs["env"]
        assert "PYTHONHOME" not in kwargs["env"]
        assert kwargs["env"]["LD_LIBRARY_PATH"] == str(runtime / "lib")
        assert kwargs["cwd"] != ROOT and not Path(kwargs["cwd"]).is_relative_to(payload)
        if str(command[0]).endswith("ldd"):
            return SimpleNamespace(returncode=0, stdout="libcustom.so => not found" if failure == "dependency" else "/lib64/ld-linux-x86-64.so.2 (0x1234)", stderr="")
        node = Path(command[0]).name == "node"
        if not node:
            assert command[1:3] == ["-I", "-c"]
            assert "import app" in command[3]
            assert "services/company_os" in command[3] and "site-packages" in command[3]
            assert "SUPYMEM_MODE" in kwargs["env"]
        broken = failure == ("node" if node else "python")
        return SimpleNamespace(returncode=1 if broken else 0, stdout=f"v{builder.NODE_VERSION}" if node and failure != "version" else "v25.0.0" if node else "", stderr=f"{domain}: runtime cannot load" if broken else "")

    monkeypatch.setattr(builder.subprocess, "run", run)
    monkeypatch.setattr(builder.shutil, "which", lambda name: f"/usr/bin/{name}")
    if failure:
        with pytest.raises(SystemExit, match="./start") as error:
            builder.verify_payload(target)
        if failure in {"node", "python"}:
            assert domain in str(error.value)
        if failure == "dependency":
            assert "libcustom.so => not found" in str(error.value)
    else:
        builder.verify_payload(target)
        assert len(commands) == 5


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_failure_page_displays_child_stderr_and_fix_as_plain_text(tmp_path, domain):
    script = ROOT / "src-tauri/splashscreen/local-error.js"
    reason = f"Rebuild your app with ./start.\n{domain}: <error> failed to load a library"
    result = subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = Object.fromEntries(['status', 'progress', 'retry', 'heading', 'log-path', 'failure-details', 'failure-help'].map(name =>
  [name, { textContent: '', hidden: true, addEventListener() {} }]));
vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), {
  document: { getElementById: name => elements[name] }, URLSearchParams,
  window: { location: { search: '?' + new URLSearchParams({ reason: process.argv[2] }) } },
});
assert.equal(elements.status.textContent, process.argv[2].split('\\n')[0]);
assert.equal(elements['failure-details'].textContent, process.argv[2].split('\\n').slice(1).join('\\n'));
assert.equal(elements['failure-details'].hidden, false);
assert.equal(elements.retry.hidden, false);
""", str(script), reason], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_boot_help_is_hidden_until_a_real_failure(tmp_path, domain):
    screen = ROOT / "src-tauri/splashscreen"
    assert 'id="failure-help" hidden' in (screen / "local-error.html").read_text()
    result = subprocess.run(["node", "-e", """
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const elements = Object.fromEntries(['status', 'progress', 'retry', 'heading', 'log-path', 'failure-details', 'failure-help'].map(name =>
  [name, { textContent: '', hidden: true, addEventListener(type, callback) { this.click = callback; } }]));
let failCheck = false, scheduled;
const context = {document: {getElementById: name => elements[name]}, URLSearchParams,
  setTimeout: callback => {scheduled = callback;}, clearTimeout() {},
  window: {location: {search: '?' + new URLSearchParams({starting: '1', log: process.argv[2]})},
    __TAURI__: {core: {invoke: async command => {
      if (command === 'desktop_retry_console') {failCheck = false; return;}
      if (failCheck) throw new Error('check failed');
      return [{role: 'core', healthy: false}, {role: 'worker', healthy: true}, {role: 'console', healthy: true}];
    }}}}};
(async () => {
  vm.runInNewContext(fs.readFileSync(process.argv[1], 'utf8'), context);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(elements['failure-help'].hidden, true);
  assert.equal(elements.retry.hidden, true);
  assert.equal(elements['failure-details'].hidden, true);
  failCheck = true; await scheduled();
  assert.equal(elements['failure-help'].hidden, false);
  assert.equal(elements.retry.hidden, false);
  assert.equal(elements.progress.hidden, true);
  assert.match(elements.status.textContent, /Select Retry/);
  await elements.retry.click();
  assert.equal(elements['failure-help'].hidden, true);
  assert.equal(elements['failure-details'].hidden, true);
  assert.equal(elements.retry.hidden, true);
  assert.equal(elements.progress.hidden, false);
})().catch(error => {console.error(error); process.exitCode = 1;});
""", str(screen / "local-error.js"), domain], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
