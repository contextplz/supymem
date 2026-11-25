import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
LINUX = ROOT / "scripts/desktop/linux"


def builder_module():
    specification = importlib.util.spec_from_file_location("linux_bundle", ROOT / "scripts/desktop/bundle_linux.py")
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_debian_metadata_includes_optional_secret_service_and_menu_assets(tmp_path, monkeypatch, domain):
    builder = builder_module()
    root = tmp_path / domain
    tauri = root / "src-tauri"
    (tauri / "resources/local").mkdir(parents=True)
    (tauri / "resources/local/launcher.py").write_text(domain)
    (tauri / "icons").mkdir()
    (tauri / "icons/128x128.png").write_bytes(b"icon")
    (tauri / "Cargo.toml").write_text('[package]\nname="desktop-test"\nauthors=["Test"]\n')
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    config["productName"] = domain
    config["bundle"]["resources"] = {"resources/local/": "local/"}
    (tauri / "tauri.local.json").write_text(json.dumps(config))
    executable = root / "desktop-test"
    executable.write_bytes(b"executable")
    executable.chmod(0o755)
    monkeypatch.setattr(builder, "ROOT", root)
    monkeypatch.setattr(builder.sys, "argv", ["bundle_linux.py", str(executable)])
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "amd64\n")
    run = subprocess.run

    def bundle(arguments, **kwargs):
        stage = Path(arguments[-2])
        control = (stage / "DEBIAN/control").read_text()
        fields = dict(line.split(": ", 1) for line in control.splitlines())
        dependencies = fields["Depends"].split(", ")
        assert {"libwebkit2gtk-4.1-0", "libayatana-appindicator3-1", "librsvg2-2"} <= set(dependencies)
        assert "libsecret-1-0" not in dependencies and "libdbus-1-3" not in dependencies
        assert {"libsecret-1-0", "libdbus-1-3"} <= set(fields["Recommends"].split(", "))
        desktop = (stage / "usr/share/applications/desktop-test-local.desktop").read_text()
        assert f"Name={domain}\n" in desktop
        assert "Exec=desktop-test-launch %U\n" in desktop
        assert "Icon=desktop-test\n" in desktop
        assert (stage / "usr/share/icons/hicolor/128x128/apps/desktop-test.png").read_bytes() == b"icon"
        assert (stage / "usr/share/mime/packages/local-desktop-appimage.xml").is_file()
        assert (stage / "usr/bin/desktop-test-launch").stat().st_mode & 0o111
        assert (stage / "usr/lib" / domain / "local/launcher.py").read_text() == domain
        assert "--root-owner-group" in arguments
        run(arguments, check=True, capture_output=True, text=True)
        metadata = run(["dpkg-deb", "-I", arguments[-1]], check=True, capture_output=True, text=True).stdout
        assert "Recommends: libsecret-1-0, libdbus-1-3" in metadata
        contents = run(["dpkg-deb", "-c", arguments[-1]], check=True, capture_output=True, text=True).stdout
        assert "root/root" in contents
        assert "./usr/bin/desktop-test-launch" in contents
        assert "./usr/share/applications/desktop-test-local.desktop" in contents

    monkeypatch.setattr(builder.subprocess, "run", bundle)
    builder.main()


def launcher_fixture(tmp_path, domain, *, fuse_device=False, fuse_library=False, fuse_helper=True, gpu=False):
    root = tmp_path / domain
    root.mkdir()
    launcher = root / "Your app.AppImage.sh"
    source = (LINUX / "launch.sh").read_text()
    source = source.replace("/dev/fuse", "/dev/null" if fuse_device else str(root / "no-fuse"))
    source = source.replace("/dev/dri/renderD*", "/dev/null" if gpu else str(root / "no-renderer"))
    launcher.write_text(source)
    launcher.chmod(0o755)
    binary = root / "Your app.AppImage"
    binary.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$CAPTURE"\nprintf "%s" "${WEBKIT_DISABLE_DMABUF_RENDERER-unset}" > "$RENDERER_CAPTURE"\n')
    binary.chmod(0o755)
    tools = root / "tools"
    tools.mkdir()
    (tools / "ldconfig").write_text("#!/bin/sh\nprintf '%s\\n' '" + ("libfuse.so.2" if fuse_library else "libfuse3.so.3") + "'\n")
    (tools / "id").write_text("#!/bin/sh\necho 1000\n")
    (tools / "fusermount").write_text(f"#!/bin/sh\nexit {0 if fuse_helper else 1}\n")
    for tool in tools.iterdir():
        tool.chmod(0o755)
    environment = os.environ.copy()
    for name in ("DISPLAY", "WAYLAND_DISPLAY", "WEBKIT_DISABLE_DMABUF_RENDERER", "APPIMAGE_EXTRACT_AND_RUN"):
        environment.pop(name, None)
    environment.update(PATH=f"{tools}:{os.defpath}", CAPTURE=str(root / "args"), RENDERER_CAPTURE=str(root / "renderer"))
    return launcher, root, environment


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("device,library,helper,extract", [(False, True, True, True), (True, False, True, True),
                                                         (True, True, False, True), (True, True, True, False)])
def test_appimage_launcher_detects_fuse_before_starting(tmp_path, domain, device, library, helper, extract):
    launcher, root, environment = launcher_fixture(tmp_path, domain, fuse_device=device, fuse_library=library, fuse_helper=helper)
    environment["DISPLAY"] = ":1"
    result = subprocess.run([str(launcher), domain], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "args").read_text().splitlines() == (["--appimage-extract-and-run"] if extract else []) + [domain]


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_relative_appimage_launcher_keeps_an_explicit_extract_request(tmp_path, domain):
    launcher, root, environment = launcher_fixture(tmp_path, domain)
    environment["WAYLAND_DISPLAY"] = "wayland-0"
    result = subprocess.run(["sh", launcher.name, "--appimage-extract-and-run", domain], cwd=root,
                            env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "args").read_text().splitlines() == ["--appimage-extract-and-run", domain]


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
@pytest.mark.parametrize("display", ["DISPLAY", "WAYLAND_DISPLAY"])
@pytest.mark.parametrize("gpu,override,expected", [(False, None, "1"), (True, None, "unset"), (False, "0", "0")])
def test_linux_launcher_preserves_display_and_only_disables_dmabuf_without_a_renderer(tmp_path, domain, display, gpu, override, expected):
    launcher, root, environment = launcher_fixture(tmp_path, domain, gpu=gpu)
    native = root / "desktop-test-launch"
    launcher.rename(native)
    (root / "Your app.AppImage").rename(root / "desktop-test")
    environment[display] = ":1" if display == "DISPLAY" else "wayland-0"
    if override is not None:
        environment["WEBKIT_DISABLE_DMABUF_RENDERER"] = override
    result = subprocess.run([str(native), domain], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "args").read_text().splitlines() == [domain]
    assert (root / "renderer").read_text() == expected


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_headless_linux_launcher_explains_web_start_and_keeps_smoke_available(tmp_path, domain):
    launcher, root, environment = launcher_fixture(tmp_path, domain)
    native = root / "desktop-test-launch"
    launcher.rename(native)
    (root / "Your app.AppImage").rename(root / "desktop-test")
    result = subprocess.run([str(native)], env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert "./start --web" in result.stderr
    assert not (root / "args").exists()
    result = subprocess.run([str(native), "--local-stack-smoke", domain], env=environment, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert (root / "args").read_text().splitlines() == ["--local-stack-smoke", domain]


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_linux_launcher_refuses_root_without_starting_the_app(tmp_path, domain):
    launcher, root, environment = launcher_fixture(tmp_path, domain)
    (root / "tools/id").write_text("#!/bin/sh\necho 0\n")
    environment["DISPLAY"] = ":1"
    result = subprocess.run([str(launcher)], env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert "without sudo" in result.stderr
    assert not (root / "args").exists()


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_appimage_companion_launcher_is_executable_and_relocatable(tmp_path, monkeypatch, domain):
    builder = builder_module()
    image = tmp_path / f"{domain} local.AppImage"
    image.write_bytes(b"appimage")
    monkeypatch.setattr(builder.sys, "argv", ["bundle_linux.py", "--appimage", str(image)])
    builder.main()
    launcher = image.with_name(image.name + ".sh")
    assert launcher.read_text() == (LINUX / "launch.sh").read_text()
    assert launcher.stat().st_mode & 0o111


@pytest.mark.parametrize("domain", ["inventory.example", "cycling.example"])
def test_package_permissions_allow_a_normal_user_without_changing_symlink_targets(tmp_path, domain):
    builder = builder_module()
    stage = tmp_path / domain
    directory = stage / "usr/lib/runtime"
    directory.mkdir(parents=True, mode=0o700)
    binary = directory / "python"
    binary.write_bytes(b"runtime")
    binary.chmod(0o700)
    resource = directory / "launcher.py"
    resource.write_text(domain)
    resource.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("private")
    outside.chmod(0o600)
    (directory / "link").symlink_to(outside)
    builder.package_permissions(stage)
    assert stage.stat().st_mode & 0o777 == 0o755
    assert directory.stat().st_mode & 0o777 == 0o755
    assert binary.stat().st_mode & 0o777 == 0o755
    assert resource.stat().st_mode & 0o777 == 0o644
    assert outside.stat().st_mode & 0o777 == 0o600
