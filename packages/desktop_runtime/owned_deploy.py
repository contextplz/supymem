"""One reviewed Vercel deployment of immutable local source, never agent argv."""
from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import psutil

from desktop_runtime.owned_coding import OwnedCoding, _SECRET, _trusted_git
from desktop_runtime.owned_terminal import OwnedTerminal

_COMMIT = re.compile(r"[a-f0-9]{40}\Z")
_DIGEST = re.compile(r"[a-f0-9]{64}\Z")
_SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,79}\Z")
CHECKLIST = "Install the Vercel CLI, run vercel login in Terminal, then run vercel deploy --target=preview in your project folder and check the printed link."


def _project_link(path: Path) -> tuple[dict, str]:
    if path.is_symlink() or path.parent.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        raise ValueError("deploy_project_link_invalid")
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or not all(isinstance(value.get(key), str)
            and re.fullmatch(r"[A-Za-z0-9_-]{1,100}", value[key]) for key in ("projectId", "orgId")):
        raise ValueError("deploy_project_link_invalid")
    metadata = {key: value[key] for key in ("projectId", "orgId")}
    digest = hashlib.sha256(json.dumps(metadata, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return metadata, digest


def deployment_url(raw: bytes) -> str:
    """The CLI's stdout is one deployment URL, never an arbitrary redirect."""
    value = raw.decode("utf-8", "strict").strip()
    parsed = urlsplit(value)
    if (len(value) > 2048 or parsed.scheme != "https" or parsed.username or parsed.password
            or parsed.port not in {None, 443} or parsed.path not in {"", "/"}
            or parsed.query or parsed.fragment or not parsed.hostname
            or not re.fullmatch(r"[a-z0-9-]+\.vercel\.app", parsed.hostname)):
        raise ValueError("deploy_result_unconfirmed")
    return value.rstrip("/")


class OwnedDeploy:
    def __init__(self, *, venture_id, verification, tracker, authorize, cancelled):
        self.venture_id, self.verification = venture_id, verification
        self.tracker, self.authorize, self.cancelled = tracker, authorize, cancelled
        self.spawn_attempted = False
        self.deploy_attempted = False

    @staticmethod
    def _env():
        return {**OwnedCoding._env(), "VERCEL_TELEMETRY_DISABLED": "1"}

    def preflight(self, *, check_login=False) -> str:
        binary = shutil.which("vercel", path=os.environ.get("PATH", ""))
        if not binary:
            binary = next((str(path) for path in [Path("/opt/homebrew/bin/vercel"),
                Path("/usr/local/bin/vercel")] if path.is_file() and os.access(path, os.X_OK)), None)
        if not binary:
            raise ValueError("deploy_cli_not_installed")
        if check_login:
            # A status command is detection, not a deployment or account login.
            # Discard the account name; no reviewed order is claimed signed out.
            try:
                probe = subprocess.run([binary, "whoami"], cwd="/", env=self._env(),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=5, check=False)
            except (OSError, subprocess.TimeoutExpired):
                raise ValueError("deploy_sign_in_required") from None
            if probe.returncode != 0:
                raise ValueError("deploy_sign_in_required")
        return binary

    def _run(self, argv, workspace, deadline, *, timeout):
        if self.cancelled() or time.monotonic() >= deadline or not self.authorize():
            raise ValueError("deploy_authority_unconfirmed")
        self.spawn_attempted = True
        process = psutil.Popen(argv, cwd=str(workspace), env=self._env(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True)
        self.tracker.register(process)
        output, error_output = bytearray(), bytearray()
        until = min(deadline, time.monotonic() + timeout)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ, output)
            selector.register(process.stderr, selectors.EVENT_READ, error_output)
            next_authority = time.monotonic() + 5
            try:
                while selector.get_map():
                    now = time.monotonic()
                    if self.cancelled() or now >= until:
                        raise ValueError("deploy_result_unconfirmed")
                    if now >= next_authority:
                        if not self.authorize():
                            raise ValueError("deploy_authority_unconfirmed")
                        next_authority = now + 5
                    for key, _mask in selector.select(.1):
                        chunk = os.read(key.fileobj.fileno(), 8192)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            key.data.extend(chunk)
                            if len(output) + len(error_output) > 65536:
                                raise ValueError("deploy_output_bounds")
                process.wait(timeout=max(.1, min(3, until - time.monotonic())))
            finally:
                self.tracker.capture()
                if process.poll() is None:
                    self.tracker.stop_all()
                process.stdout.close()
                process.stderr.close()
        # Identity/status/CLI diagnostic output never leaves this local call.
        return process.returncode, bytes(output)

    def _snapshot(self, workspace: Path, destination: Path, deadline: float | None = None) -> dict:
        from desktop_runtime.loop_checks import source_manifest
        verification = self.verification
        commit = verification.get("source_commit")
        digest = verification.get("file_manifest_sha256")
        if (not isinstance(commit, str) or not _COMMIT.fullmatch(commit)
                or not isinstance(digest, str) or not _DIGEST.fullmatch(digest)
                or _trusted_git(workspace, "rev-parse", "HEAD").strip() != commit):
            raise ValueError("deploy_source_changed")
        manifest = source_manifest(workspace)
        if manifest["file_manifest_sha256"] != digest:
            raise ValueError("deploy_source_changed")
        next_authority = 0.0
        for row in manifest["files"]:
            now = time.monotonic()
            if self.cancelled() or deadline is not None and now >= deadline:
                raise ValueError("deploy_authority_unconfirmed")
            if now >= next_authority:
                if not self.authorize():
                    raise ValueError("deploy_authority_unconfirmed")
                next_authority = now + 5
            blob = _trusted_git(workspace, "show", f"{commit}:{row['path']}", binary_output=True)
            if len(blob) != row["bytes"] or hashlib.sha256(blob).hexdigest() != row["sha256"]:
                raise ValueError("deploy_source_changed")
            if _SECRET.search(blob):
                raise ValueError("deploy_secret_detected")
            path = destination / row["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(blob)
        # Only the verified upload list is copied, including no CLI configs,
        # auth files, tests, founder specification or uncommitted source.
        (destination / ".vercelignore").write_text(
            "review/\ntests/\ndocs/\n.git/\n.env*\n.supymem-venture\n")
        link = workspace / ".vercel/project.json"
        expected_link = verification.get("project_link_sha256")
        if link.exists() or link.is_symlink():
            value, link_digest = _project_link(link)
            if expected_link != link_digest:
                raise ValueError("deploy_project_link_changed")
            (destination / ".vercel").mkdir()
            (destination / ".vercel/project.json").write_text(json.dumps(value))
        elif expected_link is not None or verification.get("target") == "production":
            raise ValueError("deploy_project_link_changed")
        return manifest

    def run(self, deadline: float) -> dict:
        v = self.verification
        if (v.get("operation") != "workspace_deploy_v1" or v.get("provider") != "vercel"
                or v.get("target") not in {"preview", "production"}
                or not isinstance(v.get("workspace_slug"), str)
                or not _SLUG.fullmatch(v["workspace_slug"])):
            raise ValueError("deploy_scope_unsupported")
        binary = self.preflight()
        workspace = OwnedTerminal(venture_id=self.venture_id,
            workspace_slug=v["workspace_slug"], tracker=self.tracker,
            authorize=self.authorize).workspace()
        self.tracker.watch_workspace(workspace)
        with tempfile.TemporaryDirectory(prefix="supymem-deploy-") as temporary:
            destination = Path(temporary) / v["workspace_slug"]
            destination.mkdir(mode=0o700)
            self._snapshot(workspace, destination, deadline)
            self.tracker.watch_workspace(destination)
            code, _identity = self._run([binary, "whoami"], destination, deadline, timeout=5)
            if code != 0:
                raise ValueError("deploy_sign_in_required")
            if self.cancelled() or not self.authorize():
                raise ValueError("deploy_authority_unconfirmed")
            self.deploy_attempted = True
            # Explicit preview overrides Vercel's implicit first-production rule.
            # Production always belongs to a different reviewed order.
            code, raw = self._run([binary, "deploy", "--yes", f"--target={v['target']}"],
                destination, deadline, timeout=600)
            if code != 0:
                raise ValueError("deploy_result_unconfirmed")
            url = deployment_url(raw)
            linked = destination / ".vercel/project.json"
            link_digest = None
            if linked.is_file() and not linked.is_symlink() and linked.stat().st_size <= 4096:
                metadata, link_digest = _project_link(linked)
                if v.get("project_link_sha256") is not None and link_digest != v["project_link_sha256"]:
                    raise ValueError("deploy_project_link_changed")
                local_link = workspace / ".vercel"
                if local_link.is_symlink() or (local_link / "project.json").is_symlink():
                    raise ValueError("deploy_project_link_invalid")
                local_link.mkdir(mode=0o700, exist_ok=True)
                # Project linkage is local provider metadata, never credentials.
                (local_link / "project.json").write_text(json.dumps(metadata))
            status = health_status = None
            functional = {"functional_status": "not_verified_on_live", "http_journey": []}
            # Approved app code can make even GET journeys consequential; deploy approval is not contact approval.
            try:
                with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                    status = client.get(url).status_code
                    health_status = client.get(url + "/api/health").status_code
            except httpx.HTTPError:
                pass
            return {"provider": "vercel", "target": v["target"], "url": url,
                "exit_code": code, "source_commit": v["source_commit"],
                "file_manifest_sha256": v["file_manifest_sha256"],
                "http_status": status, "health_status": health_status,
                "project_link_sha256": link_digest,
                **functional,
                "availability_status": "available" if status == health_status == 200 else
                    "protected" if status in {401, 403} and health_status in {401, 403} else "unavailable",
                "protected": status in {401, 403} and health_status in {401, 403}}
