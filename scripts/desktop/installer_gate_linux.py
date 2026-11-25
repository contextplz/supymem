"""Release smoke inside xvfb and a private D-Bus session.

SUPYMEM_DESKTOP_E2E_MODEL_KEY is an optional repository Actions secret containing
your OpenAI model key. Pass it as E2E_MODEL_KEY only to this process. The check
saves it through setup and compares it inside the installed runtime after restart.
It never calls a paid model. Missing secrets skip only the key check.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import e2e_linux as e2e


MISSING_KEY = ("Your model key persistence check was skipped because SUPYMEM_DESKTOP_E2E_MODEL_KEY is absent. "
               "Add your OpenAI key in GitHub Settings > Secrets and variables > Actions, then rerun this workflow.")


def stop_process(process):
    if process is not None and process.poll() is None:
        os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()


class InstallerJourney(e2e.Journey):
    def __init__(self, output, scenario):
        super().__init__(output, scenario)
        self.fake = Path(os.environ.get("E2E_FAKE_TOOLS", str(output / "tools")))
        self.key = os.environ.get("E2E_MODEL_KEY", "").strip()
        if self.key:
            self.model = {"api_key": self.key}

    def run_api(self):
        super().run_api()
        if self.failed:
            raise RuntimeError("Your sample journey failed. Review results.json and app.log, fix the reported step, then rebuild and rerun.")
        self.connect_coding_tools()
        self.build_landing_page()

    def build_landing_page(self):
        self.step = "landing page"
        (self.fake / "page-fixture.json").write_text(json.dumps({
            "root": str(e2e.ROOT), "template": e2e.page_fixture(self.scenario)}))
        before = len(e2e.cli_runs(self.fake))
        self.get(f"/ventures/{self.venture_id}/chat", payload={
            "message": "Build a private landing page for this venture. Do not publish it."})
        base = f"/ventures/{self.venture_id}/pagebuilder"
        job = {}
        def built():
            current = self.get(base + "/profile").get("current") or {}
            if current.get("state") == "failed":
                reason = current.get("error_reason") or current.get("error") or "Your coding tool page failed its checks."
                runs = e2e.cli_runs(self.fake)[before:]
                (self.output / "landing-page-tool-runs.json").write_text(json.dumps(runs, indent=2) + "\n")
                raise RuntimeError(str(reason) + " Open Build activity, fix the reported issue in your coding tool, then retry on Website.")
            if current.get("state") == "ready" and current.get("job_id"):
                job.update(current)
                return True
            return False
        self.poll(built)
        runs = e2e.cli_runs(self.fake)[before:]
        if not runs or any(run.get("provider") not in {"claude", "codex"} for run in runs):
            raise RuntimeError("Your page did not use your connected coding tool. Open Settings > Keys and connections > Coding tools, connect and allow your tool, then rebuild.")
        preview = self.get(base + "/jobs/" + job["job_id"] + "/preview-document?variant=0")["html"]
        if e2e.MARKER not in preview or "<h1" not in preview:
            raise RuntimeError("Your coding tool preview is incomplete. Open Website, rebuild with your connected tool, then retry this check.")
        (self.output / "landing-page-tool-runs.json").write_text(json.dumps(runs, indent=2) + "\n")
        self.open_screen(f"/v/{self.venture_id}/site")
        self.passed()

    def save_model_key(self):
        self.step = "model key persistence"
        if not self.key:
            e2e.save_result(self.output, self.step, None, MISSING_KEY)
            print("SKIP " + self.step + ": " + MISSING_KEY, flush=True)
            return
        self.open_screen("/setup", "Connect")
        if "Connect your AI service" not in self.read_screen()["text"]:
            if not self.read_screen(click="Connect a model")["clicked"]:
                raise RuntimeError("Your setup connection button is missing. Open setup, choose Connect a model, then rebuild and rerun.")
        self.poll(lambda: self.desktop.evaluate("!!document.querySelector('#provider:not(:disabled)')"))
        self.desktop.fill("#provider", "openai")
        self.poll(lambda: self.desktop.evaluate("!!document.querySelector('#api_key[type=password]:not(:disabled)')"))
        self.desktop.fill("#api_key", self.key)
        if not self.read_screen(click="Save and continue")["clicked"]:
            raise RuntimeError("Your model settings were not saved. Open setup > Connect your AI service, enter your key and choose Save and continue, then retry.")
        self.poll(lambda: self.get("/local/providers").get("sample_selected") is False
                  and self.get("/local/providers").get("secrets_set", {}).get("api_key") is True)
        before = self.get("/local/providers")
        if self.key in json.dumps(before):
            raise RuntimeError("Your settings API exposed your model key. Fix secret redaction in Keys and connections, rotate your key in your provider account, then retry.")
        venture = self.get("/ventures", payload={"raw_idea": self.scenario["idea"], "start": False})
        self.venture_id = venture["id"]
        self.stop()
        self.app = self.launch()
        self.poll(lambda: self.get("/health").get("status") == "ok")
        after = self.get("/local/providers")
        if self.key in json.dumps(after):
            raise RuntimeError("Your settings API exposed your model key after restart. Fix secret redaction in Keys and connections, rotate your key in your provider account, then retry.")
        saved = self.get(f"/ventures/{self.venture_id}")
        if before != after or any(saved.get(field) != venture.get(field) for field in ("id", "name", "raw_idea")):
            raise RuntimeError("Your key or venture did not survive restart. Open Keys and connections and your venture, restore the missing settings, then rebuild and rerun.")
        self.verify_resources()
        resources = Path(json.loads((self.output / "installed_resources.json").read_text())["resources"])
        code = """import sys
from pathlib import Path
root = Path(sys.argv[1]) / 'local'
sys.path[:0] = [str(root / 'packages'), str(root / 'services/company_os')]
from app.core.local_secrets import get_secret
assert get_secret('model', 'api_key') == sys.stdin.read()
"""
        environment = {name: os.environ[name] for name in ("PATH", "DBUS_SESSION_BUS_ADDRESS", "LANG") if name in os.environ}
        environment.update(SUPYMEM_MODE="local", COMPANY_OS_DATA_DIR=str(self.owner_secret_file.parent))
        result = subprocess.run([str(self.runtime_python), "-I", "-B", "-c", code, str(resources)],
                                input=self.key, text=True, env=environment, capture_output=True, timeout=30)
        if result.returncode:
            raise RuntimeError("Your saved model key could not be read after restart. Open Keys and connections > Your AI provider, re-enter your key and unlock your system keychain, then retry.")
        for root in (self.owner_secret_file.parent, self.output):
            if any(self.key.encode() in path.read_bytes() for path in root.rglob("*") if path.is_file() and not path.is_symlink()):
                raise RuntimeError("Your model key appeared in plaintext. Fix your settings encryption or startup log redaction, rotate your key in your provider account, then rerun.")
        self.passed()


class SetupKeyJourney(InstallerJourney):
    def run_api(self):
        self.desktop = e2e.Desktop()
        self.desktop.network = self.network
        for step in ("splash", "setup"):
            self.step = step
            self.wait()
        self.verify_resources()
        self.save_model_key()


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Pass your built AppImage and a new evidence folder to scripts/desktop/installer_gate_linux.py, then retry.")
    os.environ["E2E_MODE"] = "sample"
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="installer-tools-", dir=Path.home()) as temporary:
        os.environ["E2E_FAKE_TOOLS"] = temporary
        os.environ["PATH"] = temporary + os.pathsep + os.environ.get("PATH", os.defpath)
        daemon = shutil.which("gnome-keyring-daemon")
        keyring = None
        try:
            if daemon:
                environment = {name: os.environ[name] for name in ("PATH", "HOME", "DISPLAY", "DBUS_SESSION_BUS_ADDRESS") if name in os.environ}
                environment.update(HOME=temporary, XDG_DATA_HOME=temporary + "/data",
                                   XDG_CONFIG_HOME=temporary + "/config", XDG_CACHE_HOME=temporary + "/cache")
                keyring = subprocess.Popen([daemon, "--foreground", "--components=secrets", "--unlock"],
                    env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, start_new_session=True)
                keyring.stdin.write(os.urandom(32).hex().encode())
                keyring.stdin.close()
            elif os.environ.get("E2E_MODEL_KEY"):
                raise SystemExit("Install gnome-keyring and libsecret-tools on your runner, then rerun your key persistence check.")
            appimage, output = sys.argv[1], Path(sys.argv[2]).resolve()
            if output.exists() and any(output.iterdir()):
                raise SystemExit("Your evidence folder already has a run. Pass a new evidence folder to scripts/desktop/installer_gate_linux.py, then retry.")
            key_output, sample_output = output / "key", output / "sample"
            key_output.mkdir(parents=True)
            sample_output.mkdir()
            if os.environ.get("E2E_MODEL_KEY", "").strip():
                e2e.Journey = SetupKeyJourney
                sys.argv[1:] = [appimage, str(key_output)]
                status = e2e.main()
            else:
                e2e.save_result(key_output, "model key persistence", None, MISSING_KEY)
                print("SKIP model key persistence: " + MISSING_KEY, flush=True)
                status = 0
            e2e.Journey = InstallerJourney
            if status == 0:
                sys.argv[1:] = [appimage, str(sample_output)]
                status = e2e.main()
            results = []
            for profile in (key_output, sample_output):
                path = profile / "results.json"
                if path.exists():
                    results.extend(json.loads(path.read_text()))
            (output / "results.json").write_text(json.dumps(results, indent=2) + "\n")
            key = os.environ.get("E2E_MODEL_KEY", "").strip()
            if key:
                leaked = [path for path in output.rglob("*")
                          if path.is_file() and not path.is_symlink() and key.encode() in path.read_bytes()]
                for path in leaked:
                    path.unlink()
                if leaked:
                    print("FAIL privacy: Your evidence contained a model key and was removed. Rotate your key in your provider account, fix log redaction, then rerun.", flush=True)
                    return 1
            e2e.summary(output)
            required = [item for item in results if not (
                not key and item["step"] == "model key persistence" and item.get("skipped"))]
            return status or int(not required or any(item["passed"] is not True for item in required))
        finally:
            stop_process(keyring)


if __name__ == "__main__":
    raise SystemExit(main())
