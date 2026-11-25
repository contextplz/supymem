"""Check your unsigned .app's supervisor and packaged sample journey without a GUI.

This does not sign or notarize your app. Shipping it needs your Developer ID.
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import e2e_linux as e2e
from installer_gate_linux import stop_process


class HeadlessJourney(e2e.Journey):
    def ports(self):
        return self.selected_ports

    def capture(self):
        return None

    def prepare_experiments(self):
        self.step = "monthly spending limit"
        if self.get("/local/settings/spend")["values"].get("monthly_spend_cap") is None:
            client = e2e.Client(f"http://127.0.0.1:{self.ports()['core']}", 60,
                               owner_secret_file=self.owner_secret_file)
            client.opener = self.opener
            client.request("PUT", "/local/settings/spend", {"monthly_spend_cap": "5.00"})
            if self.get("/local/settings/spend")["values"].get("monthly_spend_cap") is None:
                raise RuntimeError("Your monthly limit was not saved. Open Settings > Keys and connections > Your monthly spend cap, save your limit, then retry.")
        feature = next(item for item in self.get("/local/capabilities")["capabilities"]
                       if item["name"] == "run_for_me_experiments")
        if feature["state"] != "ready":
            raise RuntimeError(feature["next_step"])
        self.passed()

    def run_api(self):
        self.step = "splash"
        self.wait()
        self.get("/local/providers/sample", payload={})
        self.step = "sample"
        self.wait()
        if self.get("/setup/status").get("llm_mode") != "sample":
            raise RuntimeError("Your sample choice was not saved. Open setup and choose sample mode, then rebuild and rerun.")
        self.step = "idea"
        proposal = self.get("/entry/chat/preview", payload={"text": self.scenario["idea"]})["proposal"]
        self.wait()
        self.step = "name"
        self.get("/entry/chat/confirm", payload={"proposal_id": proposal["id"], "name": self.scenario["name"]})
        self.wait()
        session = self.state()["session"]
        base = f"/ventures/{self.venture_id}/idea/sessions/{session['id']}"
        for _ in range(20):
            state = self.state()
            if e2e.step_ready("review", state, self.scenario):
                break
            session = state["session"]
            turn = session["turns"][-1]
            self.previous_turn_id = turn["id"]
            self.step = "answer"
            if turn["kind"] == "proposal":
                self.get(f"/ventures/{self.venture_id}/chat/confirm", payload={"action": "idea.proposal.approve",
                    "payload": {"session_id": session["id"], "revision_id": session["current_revision"]["id"]}})
            else:
                options = turn.get("options") or {}
                self.get(base + "/messages", payload={"text": self.scenario["answers"][options.get("gstack_id") or options.get("field")]})
            self.wait()
        self.step = "review"
        self.wait()
        self.step = "start"
        self.get(base + "/confirm", payload={"revision_id": self.state()["session"]["current_revision"]["id"], "start_research": False})
        self.wait()
        self.prior_experiments = [item["id"] for item in self.get(f"/ventures/{self.venture_id}/experiments")["items"]]
        self.prepare_experiments()
        for step in ("people", "experiment"):
            self.step = step
            self.get(f"/ventures/{self.venture_id}/chat", payload={"message": self.scenario[step]})
            self.wait()
        self.step = "restart"
        state = self.state()
        if not e2e.nothing_sent(state):
            raise RuntimeError("Your sample journey attempted a send. Fix your sample safeguards before rebuilding and rerunning this check.")
        self.before_restart = {"venture": state["venture"], "chat": state["chat"], "prospects": state["journey"]["prospects"]}
        self.stop()
        self.app = self.launch()
        self.wait()


def main():
    if len(sys.argv) != 3:
        raise SystemExit("Pass your built .app and a new evidence folder to scripts/desktop/installer_gate_macos.py, then retry.")
    app, output = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    os.umask(0o077)
    os.environ["E2E_MODE"] = "sample"
    output.mkdir(parents=True, exist_ok=True)
    scenarios = json.loads((e2e.ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())
    with tempfile.TemporaryDirectory(prefix="installer-macos-") as temporary:
        installed = Path(temporary) / app.name
        shutil.copytree(app, installed, symlinks=True)
        resources = installed / "Contents/Resources"
        with (installed / "Contents/Info.plist").open("rb") as stream:
            executable = installed / "Contents/MacOS" / plistlib.load(stream)["CFBundleExecutable"]
        environment = {name: os.environ[name] for name in ("PATH", "LANG", "TMPDIR") if name in os.environ}
        environment.update(HOME=temporary, SUPYMEM_MODE="local", APP_ENV="local", NEXT_TELEMETRY_DISABLED="1")
        subprocess.run([sys.executable, str(e2e.ROOT / "scripts/desktop/smoke_local_stack.py"), str(executable), str(resources)],
                       env=environment, check=True, timeout=900)
        target = "aarch64-apple-darwin" if platform.machine() == "arm64" else "x86_64-apple-darwin"
        python = resources / "local/runtime" / target / "bin/python3"
        code = """import runpy, sys
from pathlib import Path
root = Path(sys.argv.pop(1))
sys.path[:0] = [str(root / 'packages'), str(root / 'services/company_os')]
runpy.run_path(str(root / 'launcher.py'), run_name='__main__')
"""
        for name in ("enterprise", "consumer"):
            evidence = output / name
            evidence.mkdir()
            data = Path(temporary) / name
            data.mkdir(mode=0o700)
            journey = HeadlessJourney(evidence, scenarios[name])
            journey.owner_secret_file = data / "owner-session.secret"
            with socket.socket() as core, socket.socket() as worker:
                core.bind(("127.0.0.1", 0))
                worker.bind(("127.0.0.1", 0))
                journey.selected_ports = {"core": core.getsockname()[1], "worker": worker.getsockname()[1]}
            processes = []
            with (evidence / "app.log").open("w") as log:
                def launch():
                    try:
                        for role, port in journey.selected_ports.items():
                            child_env = {**environment, "COMPANY_OS_DATA_DIR": str(data),
                                "CORE_PORT": str(journey.selected_ports["core"]),
                                "WORKER_HEALTH_PORT": str(journey.selected_ports["worker"]),
                                "CORE_URL": f"http://127.0.0.1:{journey.selected_ports['core']}",
                                "COMPANY_OS_URL": f"http://127.0.0.1:{journey.selected_ports['core']}"}
                            processes.append(subprocess.Popen([str(python), "-I", "-B", "-c", code, str(resources / "local"),
                                "--role", role, "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(data)],
                                env=child_env, cwd=temporary, stdout=log, stderr=subprocess.STDOUT, start_new_session=True))
                        return processes[0]
                    except BaseException:
                        stop()
                        raise
                def stop():
                    for process in reversed(processes):
                        stop_process(process)
                    processes.clear()
                journey.launch = launch
                journey.stop = stop
                try:
                    journey.app = launch()
                    journey.run_api()
                except Exception:
                    e2e.save_result(evidence, journey.step, False,
                        "Your packaged sample journey failed. Review app.log, fix this step in your local app, then rebuild and rerun.")
                    raise
                finally:
                    stop()
            if e2e.summary(evidence):
                return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
