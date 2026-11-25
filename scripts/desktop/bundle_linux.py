"""Package an offline Cargo build when the Tauri CLI is unavailable."""
from __future__ import annotations

import argparse
import json
import shlex
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import tomllib

ROOT = Path(__file__).resolve().parents[2]
LINUX = Path(__file__).with_name("linux")


def package_permissions(stage: Path) -> None:
    stage.chmod(0o755)
    for entry in stage.rglob("*"):
        if not entry.is_symlink():
            entry.chmod(0o755 if entry.is_dir() or entry.stat().st_mode & 0o111 else 0o644)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("executable", type=Path, nargs="?")
    parser.add_argument("--appimage", type=Path, help="Add a no-FUSE launcher beside your built AppImage.")
    args = parser.parse_args()
    if args.appimage is not None:
        if sys.platform != "linux" or args.executable is not None or not args.appimage.is_file():
            parser.error("Pass --appimage with the path to your built Linux AppImage, then retry.")
        launcher = args.appimage.with_name(args.appimage.name + ".sh")
        shutil.copy2(LINUX / "launch.sh", launcher)
        launcher.chmod(0o755)
        print(f"Launch your app with: sh {shlex.quote(str(launcher))}")
        return
    if sys.platform != "linux" or args.executable is None or not args.executable.is_file():
        parser.error("Compile your Linux executable first, then pass its path and retry.")
    package = tomllib.loads((ROOT / "src-tauri/Cargo.toml").read_text())["package"]
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    version = config["version"]
    dependencies = config["bundle"]["linux"]["deb"]
    output = ROOT / "dist/desktop-local"
    output.mkdir(parents=True, exist_ok=True)
    architecture = subprocess.check_output(["dpkg", "--print-architecture"], text=True).strip()
    artifact = output / f"{package['name']}-local_{version}_{architecture}.deb"
    with tempfile.TemporaryDirectory(prefix="desktop-deb-") as temporary:
        stage = Path(temporary)
        control = stage / "DEBIAN"
        control.mkdir()
        (control / "control").write_text(
            f"Package: {package['name']}-local\nVersion: {version}\nArchitecture: {architecture}\n"
            f"Maintainer: {package['authors'][0]}\n"
            f"Depends: {', '.join(dependencies['depends'])}\n"
            f"Recommends: {', '.join(dependencies['recommends'])}\n"
            "Description: Local desktop application with bundled Python and Node\n")
        binary = stage / "usr/bin" / package["name"]
        binary.parent.mkdir(parents=True)
        shutil.copy2(args.executable, binary)
        binary.chmod(0o755)
        launcher = binary.with_name(package["name"] + "-launch")
        shutil.copy2(LINUX / "launch.sh", launcher)
        launcher.chmod(0o755)
        resources = stage / "usr/lib" / config["productName"]
        for source, destination in config["bundle"]["resources"].items():
            source_path = ROOT / "src-tauri" / source
            target = resources / destination
            if source_path.is_dir():
                shutil.copytree(source_path, target)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, target)
        desktop = stage / "usr/share/applications" / f"{package['name']}-local.desktop"
        desktop.parent.mkdir(parents=True)
        desktop.write_text(f"[Desktop Entry]\nType=Application\nName={config['productName']}\n"
                           f"Exec={package['name']}-launch %U\nIcon={package['name']}\n"
                           f"StartupWMClass={package['name']}\nTerminal=false\nCategories=Office;\n")
        icon = stage / "usr/share/icons/hicolor/128x128/apps" / f"{package['name']}.png"
        icon.parent.mkdir(parents=True)
        shutil.copy2(ROOT / "src-tauri/icons/128x128.png", icon)
        mime = stage / "usr/share/mime/packages/local-desktop-appimage.xml"
        mime.parent.mkdir(parents=True)
        shutil.copy2(LINUX / "appimage.xml", mime)
        mime_icon = stage / "usr/share/icons/hicolor/128x128/mimetypes/application-vnd.appimage.png"
        mime_icon.parent.mkdir(parents=True)
        shutil.copy2(icon, mime_icon)
        package_permissions(stage)
        subprocess.run(["dpkg-deb", "--build", "--root-owner-group", "-Zgzip", "-z1", str(stage), str(artifact)], check=True)
    print(f"Linux bundle: {artifact} ({artifact.stat().st_size} bytes)")


if __name__ == "__main__":
    try:
        main()
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit("Your bundle could not be created. Check your build inputs and Debian tools, then retry.") from None
