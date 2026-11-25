"""Prepare manual local releases and sign and test installed Mac bundles."""

from __future__ import annotations

import argparse
import base64
import json
import os
import platform
import plistlib
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path, PurePosixPath
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
MAC_TARGETS = {"aarch64-apple-darwin": "arm64", "x86_64-apple-darwin": "x86_64"}
APPLE_VARIABLES = ("APPLE_CERTIFICATE", "APPLE_CERTIFICATE_PASSWORD", "APPLE_SIGNING_IDENTITY", "APPLE_ID",
                   "APPLE_PASSWORD", "APPLE_TEAM_ID", "APPLE_API_KEY", "APPLE_API_ISSUER", "APPLE_API_KEY_PATH")
ENTITLEMENTS = {name: True for name in (
    "com.apple.security.cs.allow-jit", "com.apple.security.cs.allow-unsigned-executable-memory",
    "com.apple.security.cs.disable-library-validation", "com.apple.security.device.audio-input",
    "com.apple.security.automation.apple-events",
)}


def macho(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    with path.open("rb") as source:
        return source.read(4) in {bytes.fromhex(value) for value in (
            "feedface", "cefaedfe", "feedfacf", "cffaedfe", "cafebabe", "bebafeca", "cafebabf", "bfbafeca",
        )}


def assert_payloads(resources: Path, target: str) -> None:
    targets = MAC_TARGETS if target == "universal-apple-darwin" else {target: MAC_TARGETS[target]}
    for triple, architecture in targets.items():
        runtime = resources / "local/runtime" / triple
        for name in ("python3", "node"):
            binary = runtime / "bin" / name
            if not macho(binary):
                raise ValueError(f"Your {triple} {name} payload is missing. Prepare that Python and Node runtime, then rebuild.")
        for binary in runtime.rglob("*"):
            if macho(binary) and architecture not in subprocess.check_output(
                    ["lipo", "-archs", str(binary)], text=True, stderr=subprocess.PIPE).split():
                raise ValueError(f"Your {triple} payload lacks {architecture}. Prepare the matching runtime, then rebuild.")


def signing_plan(app: Path, identity: str, entitlements: Path, keychain: Path | None = None) -> list[list[str]]:
    paths = [path for path in app.rglob("*") if macho(path) or (
        path.is_dir() and not path.is_symlink() and path.suffix in {".app", ".framework", ".xpc", ".bundle"})]
    paths.sort(key=lambda path: (-len(path.parts), str(path)))
    options = ["--options", "runtime", "--timestamp"] if identity != "-" else []
    chain = ["--keychain", str(keychain)] if keychain else []
    return [["codesign", "--force", "--sign", identity, *options, "--entitlements", str(entitlements),
             *chain, str(path)] for path in [*paths, app]]


def _run(command: list[str], *, plan: bool = False) -> str:
    if plan:
        print(" ".join('"' + entry + '"' if entry.startswith("$") else shlex.quote(entry) for entry in command))
        return ""
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def _notarize(path: Path, auth: list[str], *, plan: bool) -> None:
    output = _run(["xcrun", "notarytool", "submit", str(path), *auth, "--wait", "--output-format", "json"], plan=plan)
    if not plan:
        try:
            submission = json.loads(output)
        except ValueError:
            raise ValueError("Your notarization result could not be read. Retry xcrun notarytool submit with --wait --output-format json, then rebuild.") from None
        if submission.get("status") != "Accepted":
            raise ValueError(f"Apple rejected your notarization. Run xcrun notarytool log {submission.get('id', '<submission-id>')} with your saved Apple credentials, fix the reported signing issue, then rebuild and resubmit.")


def _check_mac_version(info: dict, version: str) -> None:
    numeric = version.partition("-")[0]
    if (not isinstance(info, dict) or info.get("CFBundleShortVersionString") != numeric or info.get("CFBundleVersion") != numeric
            or info.get("SupymemReleaseVersion") != version):
        raise ValueError("Your Mac app version does not match the release. Run python3 scripts/desktop_version.py check, then rebuild and use the new installer.")


def sign_mac(app: Path, dmg: Path, *, plan: bool = False) -> None:
    if not plan and platform.system() != "Darwin":
        raise ValueError("Run your signing step on a Mac, or add --plan to inspect its commands on this machine.")
    signed = bool(os.environ.get("APPLE_SIGNING_IDENTITY"))
    identity = "$APPLE_SIGNING_IDENTITY" if plan and signed else (os.environ.get("APPLE_SIGNING_IDENTITY") or "-")
    api_auth = ["--key", os.environ.get("APPLE_API_KEY_PATH", ""), "--key-id", os.environ.get("APPLE_API_KEY", ""),
                "--issuer", os.environ.get("APPLE_API_ISSUER", "")]
    id_auth = ["--apple-id", os.environ.get("APPLE_ID", ""), "--password", os.environ.get("APPLE_PASSWORD", ""),
               "--team-id", os.environ.get("APPLE_TEAM_ID", "")]
    auth = api_auth if os.environ.get("APPLE_API_KEY") else id_auth
    if signed and not plan and (not identity.startswith("Developer ID Application:") or not all(auth[1::2])):
        raise ValueError("Set APPLE_SIGNING_IDENTITY to your Developer ID Application identity and add APPLE_ID, "
                         "APPLE_PASSWORD and APPLE_TEAM_ID or APPLE_API_KEY, APPLE_API_ISSUER and APPLE_API_KEY_PATH "
                         "in your release secrets, then retry.")
    if not signed and any(os.environ.get(name) for name in APPLE_VARIABLES):
        raise ValueError("Add APPLE_SIGNING_IDENTITY to your release secrets, or remove your partial Apple credentials for an ad-hoc build, then retry.")
    if plan and signed:
        variables = ("APPLE_API_KEY_PATH", "APPLE_API_KEY", "APPLE_API_ISSUER") if os.environ.get("APPLE_API_KEY") else (
            "APPLE_ID", "APPLE_PASSWORD", "APPLE_TEAM_ID")
        auth = [entry for flag, name in zip(auth[::2], variables) for entry in (flag, "$" + name)]
    if not signed:
        print("Your build uses ad-hoc signing without notarization. On your Mac, right-click your installed app, choose Open, then confirm Open. For a release, add your Apple signing and notarization secrets.")
    if plan and not app.is_dir():
        print("Your app folder is not built yet. This plan shows bundle commands only. Build your app, then rerun --plan with its path to list every nested binary.")
    if not plan and (app.is_symlink() or not app.is_dir() or app.suffix != ".app"):
        raise ValueError("Choose your built local .app folder, then retry signing.")
    context = tempfile.TemporaryDirectory(prefix="local-mac-sign-") if not plan else None
    temporary = Path(context.name) if context else app.parent / "signing-plan"
    keychain = None
    try:
        entitlements = temporary / "entitlements.plist"
        if not plan:
            entitlements.write_bytes(plistlib.dumps(ENTITLEMENTS))
        if signed and os.environ.get("APPLE_CERTIFICATE"):
            keychain = temporary / "release.keychain-db"
            password = "$KEYCHAIN_PASSWORD" if plan else secrets.token_urlsafe(32)
            certificate = temporary / "certificate.p12"
            if not plan:
                certificate.write_bytes(base64.b64decode(os.environ["APPLE_CERTIFICATE"], validate=True))
                certificate.chmod(0o600)
            certificate_password = "$APPLE_CERTIFICATE_PASSWORD" if plan else os.environ.get("APPLE_CERTIFICATE_PASSWORD", "")
            for command in (["security", "create-keychain", "-p", password, str(keychain)],
                            ["security", "set-keychain-settings", "-lut", "21600", str(keychain)],
                            ["security", "unlock-keychain", "-p", password, str(keychain)],
                            ["security", "import", str(certificate), "-k", str(keychain), "-P", certificate_password,
                             "-T", "/usr/bin/codesign", "-T", "/usr/bin/security"],
                            ["security", "set-key-partition-list", "-S", "apple-tool:,apple:,codesign:", "-s", "-k", password, str(keychain)]):
                _run(command, plan=plan)
        for command in signing_plan(app, identity, entitlements, keychain):
            _run(command, plan=plan)
        _run(["codesign", "--verify", "--deep", "--strict", "--verbose=2", str(app)], plan=plan)
        if not plan:
            config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
            _check_mac_version(plistlib.loads((app / "Contents/Info.plist").read_bytes()), config["version"])
        if signed:
            archive = temporary / "app.zip"
            _run(["ditto", "-c", "-k", "--keepParent", str(app), str(archive)], plan=plan)
            _notarize(archive, auth, plan=plan)
            _run(["xcrun", "stapler", "staple", str(app)], plan=plan)
            _run(["xcrun", "stapler", "validate", str(app)], plan=plan)
            _run(["spctl", "--assess", "--type", "execute", str(app)], plan=plan)
        staging = temporary / "dmg"
        if not plan:
            staging.mkdir()
            (staging / "Applications").symlink_to("/Applications")
        _run(["ditto", str(app), str(staging / app.name)], plan=plan)
        if not plan:
            dmg.unlink(missing_ok=True)
        _run(["hdiutil", "create", "-volname", app.stem, "-srcfolder", str(staging), "-format", "UDZO", str(dmg)], plan=plan)
        if signed:
            _run(["codesign", "--force", "--sign", identity, "--timestamp",
                  *(["--keychain", str(keychain)] if keychain else []), str(dmg)], plan=plan)
            _notarize(dmg, auth, plan=plan)
            _run(["xcrun", "stapler", "staple", str(dmg)], plan=plan)
            _run(["xcrun", "stapler", "validate", str(dmg)], plan=plan)
    finally:
        try:
            if keychain:
                _run(["security", "delete-keychain", str(keychain)], plan=plan)
        finally:
            if context:
                context.cleanup()


def installed_smoke(app: Path) -> None:
    resources = app / "Contents/Resources"
    with (app / "Contents/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    if not isinstance(info, dict) or info.get("CFBundleIdentifier") != config["identifier"]:
        raise ValueError("Install your local DMG, not your hosted app, then retry the installed-app gate.")
    _check_mac_version(info, config["version"])
    executable = app / "Contents/MacOS" / info["CFBundleExecutable"]
    environment = {name: value for name, value in os.environ.items() if name not in APPLE_VARIABLES}
    data = Path.home() / "Library/Application Support" / info["CFBundleName"]
    if data.exists():
        raise ValueError("Use a fresh runner account for your installed-app gate, then retry. Your existing data was not changed.")
    opener = build_opener(ProxyHandler({}))
    endpoint = "http://127.0.0.1:3100/api/local/settings/search"
    key = secrets.token_urlsafe(32)
    process = None
    def request(body=None):
        payload = json.dumps(body).encode() if body is not None else None
        with opener.open(Request(endpoint, data=payload, method="PUT" if payload else "GET",
                                 headers={"Content-Type": "application/json", "Origin": "http://127.0.0.1:3100"}), timeout=5) as response:
            return json.load(response)
    def quit_app():
        _run(["osascript", "-e", f'tell application id "{info["CFBundleIdentifier"]}" to quit'])
        process.wait(timeout=45)
    try:
        for launch in range(2):
            process = subprocess.Popen(["open", "-n", "-W", str(app)], env=environment,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 180
            while True:
                if process.poll() is not None:
                    raise ValueError("Your installed app exited. Open its startup logs, fix the launch error, then rebuild.")
                try:
                    state = request()
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise ValueError("Your installed app did not become ready. Open its startup logs, then rebuild and retry.") from None
                    time.sleep(1)
            if launch == 0:
                if state["secrets_set"].get("api_key"):
                    raise ValueError("Your runner already has a saved search key. Use a fresh runner account, then retry.")
                subprocess.run([sys.executable, str(ROOT / "scripts/desktop/smoke_local_stack.py"), str(executable), str(resources)],
                               env=environment, check=True, timeout=400)
                state = request({"provider": "tavily", "api_key": key})
            if not state["secrets_set"].get("api_key") or key in json.dumps(state):
                raise ValueError("Your key did not persist privately. Check Search on Keys and connections, then rebuild.")
            runtime = resources / "local/runtime" / next(triple for triple, arch in MAC_TARGETS.items() if arch == platform.machine())
            code = """import os, sys
from pathlib import Path
root = Path(sys.argv[1]) / 'local'
sys.path[:0] = [str(root / 'packages'), str(root / 'services/company_os')]
from app.core.local_secrets import get_secret
assert get_secret('search', 'api_key') == sys.stdin.read()
"""
            subprocess.run([str(runtime / "bin/python3"), "-I", "-B", "-c", code, str(resources)], input=key, text=True,
                           env={**environment, "SUPYMEM_MODE": "local", "COMPANY_OS_DATA_DIR": str(data)},
                           check=True, capture_output=True, timeout=30)
            if any(key.encode() in path.read_bytes() for path in data.rglob("*") if path.is_file() and not path.is_symlink()):
                raise ValueError("Your saved key appeared in plaintext. Check your local settings encryption and startup log redaction, then rebuild.")
            quit_app()
            process = None
    finally:
        if process is not None and process.poll() is None:
            quit_app()
    print("Your installed app passed first launch, encrypted key save, quit and relaunch with the same key.")


def prepare(directory: Path, version: str, repository: str) -> dict | None:
    number = r"(?:0|[1-9][0-9]*)"
    prerelease = rf"(?:{number}|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
    if not re.fullmatch(rf"{number}\.{number}\.{number}(?:-{prerelease}(?:\.{prerelease})*)?", version):
        raise ValueError("Use a three-part release version with an optional prerelease suffix, then retry.")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError("Use your GitHub owner/repository, then retry.")
    installers = [path for path in directory.rglob("*") if path.is_file() and not path.is_symlink()
                  and (path.suffix in {".dmg", ".deb", ".AppImage"} or path.name.endswith("-setup.exe"))]
    if installers:
        config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
        if config["version"] != version or any(f"_{version}_" not in path.name for path in installers):
            raise ValueError("Your installer version does not match. Choose your trusted tag build, then retry.")
        (directory / "latest.json").unlink(missing_ok=True)
        return None
    archives = list(directory.glob("*.app.tar.gz"))
    if len(archives) != 1:
        raise ValueError("Choose your built installers or exactly one built Apple Silicon app archive, then retry.")
    archive = archives[0]
    with tarfile.open(archive, "r:gz") as contents:
        candidates = [member for member in contents if len(PurePosixPath(member.name).parts) == 3
                      and PurePosixPath(member.name).parts[0].endswith(".app")
                      and PurePosixPath(member.name).parts[1:] == ("Contents", "Info.plist")]
        if len(candidates) != 1 or not candidates[0].isfile() or candidates[0].size > 65536:
            raise ValueError("Your app archive metadata is invalid. Rebuild your local app, then retry.")
        with contents.extractfile(candidates[0]) as source:
            info = plistlib.load(source)
    config = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())
    if (not isinstance(info, dict) or info.get("CFBundleIdentifier") != config["identifier"]
            or not isinstance(info.get("LSEnvironment"), dict) or info["LSEnvironment"].get("SUPYMEM_MODE") != "local"):
        raise ValueError("Choose your local desktop build, not your hosted Mac app, then retry.")
    _check_mac_version(info, version)
    (directory / "latest.json").unlink(missing_ok=True)
    return None


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] in {"sign-mac", "assert-payloads", "installed-smoke"}:
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument("action", choices=["sign-mac", "assert-payloads", "installed-smoke"])
        parser.add_argument("directory", type=Path)
        parser.add_argument("--dmg", type=Path)
        parser.add_argument("--target", choices=[*MAC_TARGETS, "universal-apple-darwin"], default="universal-apple-darwin")
        parser.add_argument("--plan", action="store_true")
        args = parser.parse_args()
        try:
            if args.action == "sign-mac":
                if args.dmg is None:
                    parser.error("Choose your DMG output with --dmg, then retry.")
                sign_mac(args.directory.resolve(), args.dmg.resolve(), plan=args.plan)
            elif args.action == "assert-payloads":
                assert_payloads(args.directory.resolve(), args.target)
            else:
                installed_smoke(args.directory.resolve())
        except ValueError as error:
            raise SystemExit(str(error)) from None
        except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException):
            if args.action == "sign-mac":
                message = "Your signing step failed. Check APPLE_CERTIFICATE, APPLE_CERTIFICATE_PASSWORD, APPLE_SIGNING_IDENTITY and your notarization credentials in repository Settings > Secrets and variables > Actions, then rerun sign-mac. Credential output was withheld."
            elif args.action == "installed-smoke":
                data_name = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["productName"]
                message = f"Your installed app check failed. Open {Path.home() / 'Library/Application Support' / data_name / 'logs'}, fix the startup error or your Search settings on Keys and connections, then rebuild and rerun installed-smoke."
            else:
                message = "Your payload could not be inspected. Run xcode-select --install on your Mac, prepare both runtime payloads, then rerun assert-payloads."
            raise SystemExit(message) from None
        return
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--repository", required=True)
    args = parser.parse_args()
    try:
        prepare(args.directory, args.version, args.repository)
    except (OSError, ValueError, tarfile.TarError, plistlib.InvalidFileException):
        raise SystemExit("Your release could not be prepared. Check your trusted tag installers or local archive, version and signing output, then retry.") from None


if __name__ == "__main__":
    main()
