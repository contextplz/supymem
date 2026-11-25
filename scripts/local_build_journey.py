#!/usr/bin/env python3
"""Check your real local build journey over HTTP with subscription-shaped fake CLIs.

Run through ~/skills/tq with --idea twice. Fake tools build from the supplied brief.
Only your configured model is live. Builds remain private and disposable.
Use --offline-landing-timing for keyless slow and timeout checks with a patched clock.
"""
from __future__ import annotations

import argparse
import base64
import copy
import html
import io
import json
import math
import os
import re
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import HTTPDefaultErrorHandler, ProxyHandler, build_opener
from uuid import uuid4

from local_acceptance import AcceptanceFailure, Client, NoRedirect, require
from local_browser_journey import Failures, load_environment, provider_settings, stop_stack
from local_value_journey import approve_fake_coding_tool, free_ports


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV = Path.home() / "lanes/e2e_live.env"
DEFAULT_FINDINGS = Path.home() / "lanes/LIVE_FINDINGS.md"
MARKER = 'data-build-journey="own-tool"'
CONNECT = "Open Settings > Keys and connections > Coding tools, connect your tool and allow Charlie to use it, then retry."
BUILD_SOURCE = "services/company_os/app/builder/ctl.py; packages/desktop_runtime/coding_loop.py"

FAKE_CLI = r'''
import hashlib
import html
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

binary = Path(sys.argv[0]).resolve()
provider = binary.name
arguments = sys.argv[1:]
if arguments == ["--version"]:
    print({"claude": "2.1.284 (Claude Code)", "codex": "codex-cli 0.163.0", "opencode": "2.2.5"}[provider])
    raise SystemExit(0)
if provider == "claude" and arguments == ["auth", "status"]:
    print(json.dumps({"loggedIn": True, "authMethod": "claude.ai"}))
    raise SystemExit(0)
if provider == "codex" and arguments == ["login", "status"]:
    print("Logged in using ChatGPT", file=sys.stderr)
    raise SystemExit(0)
if provider == "opencode":
    if arguments == ["auth", "list"]:
        print("● OpenAI oauth")
        raise SystemExit(0)
    if arguments == ["debug", "config"]:
        print(Path(os.environ["OPENCODE_CONFIG"]).read_text())
        raise SystemExit(0)
    if arguments[:3] != ["run", "--format", "json"]:
        raise SystemExit(2)
if (provider == "claude" and "-p" not in arguments) or (provider == "codex" and (not arguments or arguments[0] != "exec")):
    raise SystemExit(2)
workspace = Path(arguments[arguments.index("--cd") + 1]).resolve() if "--cd" in arguments else Path.cwd()
workspace.mkdir(parents=True, exist_ok=True)
os.chdir(workspace)
mode = (binary.parent / "mode").read_text().strip()
record = {"pid": os.getpid(), "provider": provider, "workspace": str(workspace), "mode": mode}
with (binary.parent / "runs.jsonl").open("a") as stream:
    stream.write(json.dumps(record) + "\n")
def emit(value):
    print(json.dumps(value), flush=True)
emit({"type": "system", "subtype": "init", "session_id": "journey"} if provider == "claude" else
     {"type": "step_start", "sessionID": "journey", "part": {"type": "step-start"}} if provider == "opencode" else
     {"type": "thread.started", "thread_id": "journey"})
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Preparing your requested files"}]}} if provider == "claude" else
     {"type": "text", "part": {"text": "Preparing your requested files"}} if provider == "opencode" else
     {"type": "item.completed", "item": {"id": "message", "type": "agent_message", "text": "Preparing your requested files"}})
if mode == "hang":
    signal.signal(signal.SIGTERM, lambda *_arguments: sys.exit(0))
    while True:
        time.sleep(0.1)
fixture_path = binary.parent / "page-fixture.json"
if fixture_path.exists() and "Confirmed venture inputs:\n" in arguments[-1]:
    os.environ.update(SUPYMEM_MODE="local", APP_ENV="local", LLM_OFFLINE="1", LLM_CASSETTE_MODE="replay",
        COMPANY_OS_DATA_DIR=str(binary.parent / "fixture-data"))
    fixture = json.loads(fixture_path.read_text())
    sys.path.insert(0, fixture["root"])
    sys.path.insert(0, str(Path(fixture["root"]) / "packages"))
    sys.path.insert(0, str(Path(fixture["root"]) / "services/company_os"))
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    sys.path.insert(0, str(Path(fixture["root"]) / "scripts"))
    from local_build_journey import fixture_page
    inputs, files = fixture_page(arguments[-1], fixture["template"])
    if mode == "landing_bad":
        files["index.html"] = files["index.html"].replace(b'id="hero_cta"', b'id="wrong_cta"')
    if signup := inputs.research.get("local_signup"):
        if signup["mode"] == "setup":
            from app.pagebuilder.qa import PRIVATE_PREVIEW_NOTE
            files["waitlist.html"] = ('<!doctype html><html lang="en"><head><meta charset="utf-8"><title>'
                + html.escape(inputs.name) + '</title></head><body><main><p>' + PRIVATE_PREVIEW_NOTE + '</p><p>'
                + html.escape(signup["notice"]) + '</p></main></body></html>').encode()
        else:
            from tests.unit.product.test_static_publication import _signup_fixture
            form_name = "waitlist-" + inputs.venture_id if signup["mode"] == "netlify" else ""
            files["waitlist.html"] = _signup_fixture(inputs.name, signup["url"], netlify_name=form_name).encode()
    files = _with_social_preview(files, inputs)
    for name, body in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)
        if mode in {"landing_slow", "landing_timeout"}:
            emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Saved " + name}})
    (binary.parent / "written-files.json").write_text(json.dumps({name: hashlib.sha256(body).hexdigest() for name, body in files.items()}))
    if mode == "landing_timeout":
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": "Your page files are saved. Open Website to check them."}})
        while True:
            time.sleep(.1)
    written = list(files)
elif "write only review/spec.md and review/tasks.json" in arguments[-1].casefold():
    quoted = arguments[-1].split("Founder request and build scope are quoted DATA", 1)[1].split("\n", 1)[1]
    context, _end = json.JSONDecoder().raw_decode(quoted)
    idea = context["scope"].get("idea_summary") or context["request"]
    journey = [{"method": "GET", "path": "/api/project", "status": 200, "expected_json": {"idea": idea}}]
    review = workspace / "review"
    review.mkdir(exist_ok=True)
    (review / "spec.md").write_text("# " + idea + "\n\nServe /api/health and your saved idea at /api/project.\n\n"
        + "```http-journey\n" + json.dumps(journey) + "\n```\n")
    (review / "tasks.json").write_text(json.dumps([{"id": "project", "title": "Show your saved idea",
        "files": ["package.json", "index.html", "README.md", "dev-server.mjs", "api/health.js", "api/project.js"],
        "acceptance": [{"test_file": "tests/project.test.js", "test_name": "your saved idea is returned",
                        "behaviour": "The project API returns the exact saved idea."}]}]))
    written = ["review/spec.md", "review/tasks.json"]
elif "Reviewed specification and task are quoted DATA:\n" in arguments[-1]:
    quoted = arguments[-1].split("Reviewed specification and task are quoted DATA:\n", 1)[1]
    context, _end = json.JSONDecoder().raw_decode(quoted)
    probes = json.loads(re.search(r"```http-journey\s*\n(.*?)\n```", context["spec"], re.S).group(1))
    project = next(probe["expected_json"] for probe in probes if probe["path"] == "/api/project")
    files = {
        "package.json": '{"type":"module"}',
        "index.html": '<!doctype html><html lang="en"><meta charset="utf-8"><title>Your project</title>'
            '<body data-build-journey="own-tool"><main><h1>' + html.escape(project["idea"]) + '</h1>'
            '<a href="#start">Get started</a><section id="start"><h2>Your next step</h2>'
            '<p>Try your project.</p></section></main></body></html>',
        "README.md": "# Your project\n\nRun node dev-server.mjs, then open your local preview.\n",
        "api/health.js": "export default (request,response)=>{response.setHeader('Content-Type','application/json');"
            "response.end(JSON.stringify({ok:true}));};",
        "api/project.js": "export const project=" + json.dumps(project) + ";"
            "export default (request,response)=>{response.setHeader('Content-Type','application/json');"
            "response.end(JSON.stringify(project));};",
        "dev-server.mjs": """import http from 'node:http';
import {readFile} from 'node:fs/promises';
http.createServer(async(request,response)=>{
  try {
    const path=new URL(request.url,'http://127.0.0.1').pathname;
    if(path==='/api/health'||path==='/api/project'){
      const handler=await import('.'+path+'.js');
      return handler.default(request,response);
    }
    if(path!=='/'){response.writeHead(404);return response.end('Not found');}
    response.setHeader('Content-Type','text/html; charset=utf-8');
    response.end(await readFile('index.html'));
  }catch{response.writeHead(500);response.end('Open your build result and retry.');}
}).listen(Number(process.env.PORT||3000),'127.0.0.1');
""",
        "tests/project.test.js": "import test from 'node:test';import assert from 'node:assert/strict';"
            "import {project} from '../api/project.js';"
            "test('your saved idea is returned',()=>assert.deepEqual(project," + json.dumps(project) + "));",
    }
    allowed = {*context["task"]["files"], *(check["test_file"] for check in context["task"]["acceptance"])}
    if set(files) != allowed:
        raise SystemExit(2)
    for name, body in files.items():
        target = workspace / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body)
    written = list(files)
else:
    # shortcut: unreviewed prompts produce smoke files only, extend the page fixture before certifying landing completion.
    (workspace / "index.html").write_text('<!doctype html><html lang="en"><meta charset="utf-8"><title>Your page</title>'
        '<body data-build-journey="own-tool"><main><h1>Your project</h1><a href="#start">Get started</a>'
        '<section id="start"><h2>Your next step</h2><p>Try your project.</p></section></main></body></html>')
    (workspace / "README.md").write_text("# Your project\n\nOpen index.html to preview your page.\n")
    written = ["index.html", "README.md"]
emit({"type": "assistant", "message": {"content": [{"type": "text", "text": "Your files are ready"}]}} if provider == "claude" else
     {"type": "text", "part": {"text": "Your files are ready"}} if provider == "opencode" else
     {"type": "item.completed", "item": {"id": "files", "type": "file_change", "changes": [
         {"path": name, "kind": "add"} for name in written]}})
emit({"type": "result", "subtype": "success", "is_error": False, "result": "Your files are ready", "usage": {"input_tokens": 1, "output_tokens": 1}} if provider == "claude" else
     {"type": "step_finish", "part": {"reason": "stop", "tokens": {"input": 1, "output": 1}}} if provider == "opencode" else
     {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}})
'''


def fixture_page(brief, template):
    from textwrap import shorten

    from app.pagebuilder.models import PageInput, Section
    from app.pagebuilder.qa import contrast

    def quoted(marker):
        return json.JSONDecoder().raw_decode(brief.split(marker + "\n", 1)[1])[0]

    inputs = PageInput.model_validate(quoted("Confirmed venture inputs:"))
    contract = quoted("PAGE_CHECK_CONTRACT:")
    if "\nREVIEWED_PLAN:\n" in brief:
        draft = quoted("REVIEWED_PLAN:")
        draft["assets"] = [{key: value for key, value in asset.items()
            if key in {"id", "role", "section_id", "subject", "prompt", "scene"}} for asset in draft["assets"]]
    else:
        draft = copy.deepcopy(template["draft"])
        confirmed_offer = inputs.clarified_answers.get("summary") or inputs.clarified_answers.get("offer")
        source = confirmed_offer if isinstance(confirmed_offer, str) and confirmed_offer.strip() else inputs.idea
        source = re.sub(r"^(?:you|i|we)\s+(?:(?:want|plan|hope|intend|aim)\s+(?:to\s+(?:offer|sell|build|launch|create)\s+)?|"
            r"(?:are|am)\s+trying\s+to\s+(?:offer|sell|build|launch|create)\s+)", "Planned: ", source.strip(), flags=re.I)
        # shortcut: unplanned fakes repeat a short confirmed offer; use a real tool for varied or nuanced copy.
        offer = re.split(r"(?<=[.!?])\s+", source)[0][:500]
        workflow = inputs.clarified_answers.get("workflow")
        steps = re.split(r"(?<=[.!?])\s+", workflow.strip()) if isinstance(workflow, str) else []
        workflow_confirmed = len(steps) == 3
        headline = offer.rstrip(".")
        headline_schema = Section.model_json_schema()["properties"]["headline"]
        headline_limit = headline_schema["maxLength"]
        clauses = re.split(r"\s+(?:that|which)\s+", headline, maxsplit=1)
        if workflow_confirmed and len(clauses) == 1:
            headline = steps[-1].rstrip(".")
        elif len(headline) > headline_limit and len(clauses) == 2:
            headline = "A concept that " + clauses[1]
        if len(headline) > headline_limit:
            shortened = shorten(headline, width=headline_limit, placeholder="")
            headline = shortened if len(shortened) >= headline_schema["minLength"] else headline[:headline_limit]
        audience = inputs.clarified_answers.get("audience") or inputs.clarified_answers.get("customer") or "You"
        if not isinstance(audience, str):
            audience = "You"
        problem = inputs.clarified_answers.get("problem") or "Which part of your current process would you like to change?"
        disclosure = "Join the waitlist for early-access updates."
        draft["brief"] = {"audience": audience, "problem": problem, "promise": offer,
            "tone": "Clear and practical", "proof_points": [offer] * 3,
            "cta": "Join the waitlist", "forbidden_claims": [], "concept_disclosure": disclosure}
        labels = ["Overview", "The problem", "How it works" if workflow_confirmed else "What it does", "What it is", "Questions", "Early access"]
        bodies = [offer, problem, workflow if workflow_confirmed else offer,
            audience + "." if len(audience) >= 9 else offer,
            "Early-access timing and details are to be confirmed.", disclosure]
        draft["sections"] = [{"id": section["id"], "purpose": "Present the confirmed offer without extra claims.",
            "headline": headline if index == 0 else labels[index], "body": bodies[index],
            "nav_label": labels[index] if index < len(labels) - 1 else None}
            for index, section in enumerate(contract["sections"])]
        for index, asset in enumerate(draft["assets"]):
            section = draft["sections"][0 if index == 0 else 1]
            asset.update(section_id=section["id"], subject="Original abstract geometric illustration",
                prompt="Draw an original abstract geometric illustration using the page palette.",
                scene={"objects": [{"kind": "stacked_blocks", "x": 200, "y": 200}]})
            section["image_id"] = asset["id"]

    def resolve(value):
        if match := re.fullmatch(r"(brief|sections|assets)(?:\[(\d+)\])?\.(\w+)", value):
            item = draft[match[1]]
            if match[2] is not None:
                item = item[int(match[2])]
            return item.get(match[3]) or item.get("headline")
        return re.sub(r"<([^<>]+)>", lambda match: resolve(match[1]), value)

    actions = [{**action, "name": resolve(action["name"])} for action in contract["actions"]]
    artwork = [{**asset, **{key: resolve(asset[key]) for key in ("id", "path", "section_id")}}
        for asset in contract["artwork"]]
    palette = draft["direction"]["palette"]
    on_primary = palette.get("on_primary") or max(("#000000", "#FFFFFF"), key=lambda color: contrast(color, palette["primary"]))
    files = {"page-plan.txt": json.dumps(draft).encode()}
    for asset in artwork:
        width, height = asset["width"], asset["height"]
        files[asset["path"]] = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
            f'<rect width="{width}" height="{height}" fill="{palette["background"]}"/>'
            f'<rect x="120" y="120" width="960" height="660" rx="80" fill="{palette["primary"]}"/>'
            f'<circle cx="600" cy="450" r="180" fill="{palette["accent"]}"/>'
            f'<circle cx="600" cy="450" r="80" fill="{palette["background"]}"/></svg>').encode()

    def link(action):
        external = ' target="_blank" rel="noopener noreferrer"' if action["kind"] == "external_link" else ""
        return f'<a id="{html.escape(action["id"], quote=True)}" href="{html.escape(action["href"], quote=True)}"{external}>{html.escape(action["name"])}</a>'

    sections = []
    for index, declared in enumerate(contract["sections"]):
        section = {**draft["sections"][index], **declared}
        heading = "h1" if index == 0 else "h2"
        steps = section.get("steps")
        if steps is None and section["id"] == "how-it-works" and section["headline"] != "What it does":
            steps = re.split(r"(?<=[.!?])\s+", section["body"].strip())
        body = ('<ol>' + ''.join(f'<li>{html.escape(step)}</li>' for step in steps) + '</ol>'
            if steps else f'<p>{html.escape(section["body"])}</p>')
        images = ''.join(f'<img id="{asset["id"]}" src="{asset["path"]}" width="{asset["width"]}" height="{asset["height"]}" loading="eager" alt="Original geometric illustration.">'
            for asset in artwork if asset["section_id"] == section["id"])
        ctas = ''.join(link(action) for action in actions if action["id"] in declared["cta_ids"])
        sections.append(f'<section id="{section["id"]}"><{heading}>{html.escape(section["headline"])}</{heading}>{body}{images}{ctas}</section>')
    required = list(contract["required_text"])
    if "headline" not in contract["sections"][0]:
        required.extend([draft["brief"]["concept_disclosure"], *draft["brief"]["proof_points"]])
    copy_text = ''.join(f'<p>{html.escape(text)}</p>' for text in required)
    nav = ''.join(link(action) for action in actions if action["id"] not in {
        identity for section in contract["sections"] for identity in section["cta_ids"]})
    files["index.html"] = (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
        f'<title>{html.escape(inputs.name)}</title><meta http-equiv="Content-Security-Policy" '
        'content="default-src \'none\'; base-uri \'none\'; img-src \'self\'; style-src \'unsafe-inline\'; form-action \'none\'">'
        f'<style>body{{background:{palette["background"]};color:{palette["text"]};font:18px Arial;margin:0}}section{{padding:32px}}img{{max-width:100%;height:auto}}'
        f'a{{display:inline-flex;min-height:44px;padding:12px;color:{on_primary};background:{palette["primary"]}}}'
        f'a:focus-visible{{outline:3px solid {palette["text"]};outline-offset:3px}}</style></head><body {MARKER}><header>{html.escape(inputs.name)}'
        f'<nav aria-label="Primary navigation">{nav}</nav></header><main>{"".join(sections)}{copy_text}</main></body></html>').encode()
    return inputs, files


def install_fake_cli(directory, provider):
    require(provider in {"claude", "codex", "opencode"}, CONNECT)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    binary = directory / provider
    binary.write_text(f"#!{sys.executable}\n" + FAKE_CLI)
    binary.chmod(0o700)
    (directory / "mode").write_text("finish")
    return binary


def cli_runs(directory):
    path = directory / "runs.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def offline_landing_timing(fixture, mode):
    from types import SimpleNamespace
    from unittest.mock import patch
    from app.pagebuilder import models, pipeline, tool
    from desktop_runtime import coding_loop, landing_loop, owned_coding
    from desktop_runtime.owned_processes import OwnedProcesses

    inputs = models.PageInput.model_validate(fixture["input"])
    with tempfile.TemporaryDirectory(prefix="landing-timing-") as temporary:
        root = Path(temporary)
        workspace = root / "page"
        workspace.mkdir()
        (workspace / ".supymem-venture").write_text(inputs.venture_id)
        binary = install_fake_cli(root / "bin", "codex")
        (binary.parent / "mode").write_text(mode)
        (binary.parent / "page-fixture.json").write_text(json.dumps({"root": str(ROOT), "template": fixture, "input": fixture["input"]}))
        elapsed = 0
        per_call_seconds = 600 if mode == "landing_timeout" else 900
        clock = SimpleNamespace(monotonic=lambda: time.monotonic() + elapsed,
            time=lambda: time.time() + elapsed, sleep=time.sleep)
        tracker = OwnedProcesses()
        coding = owned_coding.OwnedCoding(venture_id=inputs.venture_id, workspace_slug="page", provider="codex",
            tracker=tracker, authorize=lambda: True)
        worker = SimpleNamespace(last={}, cancelled=SimpleNamespace(is_set=lambda: False),
            loop_saved={"task_snapshot": {"page_input": inputs.model_dump(mode="json"), "page_plan": None}})
        updates, posted = [], []
        def progress(state, **values):
            nonlocal elapsed
            worker.last.update(state=state, **values)
            updates.append(dict(worker.last))
            # Simulated time must not outrun the tool output observed by the worker.
            if "activity_seq" in values and worker.last.get("loop_phase") == "code":
                text = values["events"][-1]["text"]
                if mode == "landing_timeout" and text.startswith("Your page files are saved."):
                    elapsed = per_call_seconds + 20
                elif text.startswith("Saved "):
                    elapsed = min(elapsed + 100, per_call_seconds - 100)
        worker.progress = progress
        gateway = SimpleNamespace(order_id="dwo_timing", iteration_receipt=posted.append)
        manifest = {"coding_loop_verification": {"max_iterations": 2, "max_wall_seconds": 1800, "per_call_seconds": per_call_seconds},
            "instruction": tool.build_brief(inputs, None)}
        try:
            with patch.object(coding, "_workspace", return_value=workspace), patch.object(coding, "preflight", return_value=str(binary)), \
                    patch.object(coding, "_provider_version", return_value="codex-cli 9.0.0"), \
                    patch.object(owned_coding, "time", clock), patch.object(landing_loop, "time", clock), patch.object(coding_loop, "time", clock):
                result = landing_loop.run(worker, manifest, gateway, coding, clock.monotonic() + 1800)
            require(result["task_verified"] is True, "Your saved page did not pass. Open Build activity, review the checks and retry.")
            require(elapsed > 480, "Your fake build did not exceed eight simulated minutes. Check its clock and rerun.")
            files = landing_loop.public_files(workspace)
            written = json.loads((binary.parent / "written-files.json").read_text())
            import hashlib
            require((workspace / "page-plan.txt").is_file() and set(files) == set(written)
                and all(hashlib.sha256(files[name]).hexdigest() == digest for name, digest in written.items()),
                "Your tool's files were removed. Review the landing loop and rerun.")
            require(any(update.get("events") and any(event["text"].startswith("Saved ") for event in update["events"])
                and not update.get("iteration_receipts") for update in updates),
                "Your tool's stream was not saved during the build. Open Build activity and retry.")
            plan = pipeline.tool_page_plan(inputs, None, files)
            bundle = tool.bundle(files, inputs, plan, {"passed": True})
            require(base64.b64decode(bundle["files"]["index.html"]) == files["index.html"],
                "Your preview changed the saved page. Open Website and retry.")
            with zipfile.ZipFile(io.BytesIO(base64.b64decode(bundle["source_zip"]))) as archive:
                require(all(archive.read(name) == body for name, body in files.items()),
                    "Your export lost saved files. Open Website > Export and retry.")
            return {"state": "ready", "mode": mode, "simulated_seconds": elapsed, "files": len(files), "events": len(worker.last["events"])}
        finally:
            tracker.stop_all()


def process_alive(pid):
    try:
        os.kill(pid, 0)
        status = Path(f"/proc/{pid}/stat")
        return not status.exists() or status.read_text().split(")", 1)[1].split()[0] != "Z"
    except ProcessLookupError:
        return False


def wait_for(fetch, timeout, reason):
    deadline = time.monotonic() + timeout
    while True:
        value = fetch()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise AcceptanceFailure(reason)
        time.sleep(0.2)


def completed_receipt(receipt):
    return (receipt.get("state") == "stopped" and receipt.get("task_verified") is True
            and (receipt.get("evidence") or {}).get("processes_stopped") is True
            and (receipt.get("evidence") or {}).get("outcome") == "pass")


class Report:
    def __init__(self, findings, secrets=()):
        self.findings = findings
        self.redactor = Failures(secrets)
        self.results = []
        self.failures = 0
        self.append_error = False

    def step(self, label, action, next_step, source=BUILD_SOURCE):
        try:
            result = action()
        except (AcceptanceFailure, OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile, subprocess.SubprocessError) as error:
            self.failures += 1
            reason = self.redactor.safe(str(error))
            record = {"step": label, "state": "FAIL", "root_cause": reason, "source": source, "next_step": next_step}
            stamp = datetime.now(timezone.utc).isoformat()
            entry = f"\n## LIVE buildgate {stamp}\n- FAIL {label}: {reason}\n- Trace: `{source}`\n- Next step: {next_step}\n"
            try:
                with self.findings.open("a") as stream:
                    stream.write(entry)
            except OSError:
                self.append_error = True
                with (ROOT / "buildgate__LIVE_FINDINGS.md").open("a") as stream:
                    stream.write(entry)
            print(f"FAIL {label}: {reason}. {next_step}", flush=True)
            result = None
        else:
            record = {"step": label, "state": "PASS"}
            print(f"PASS {label}", flush=True)
        self.results.append(record)
        return result


class SafeErrors(HTTPDefaultErrorHandler):
    def __init__(self, redactor):
        self.redactor = redactor

    def http_error_default(self, request, response, code, message, headers):
        try:
            detail = json.loads(response.read(65536)).get("detail", {})
            text = detail if isinstance(detail, str) else " ".join(str(detail.get(name, "")) for name in ("code", "message"))
        except (ValueError, AttributeError):
            text = "Your endpoint did not return a structured error"
        finally:
            response.close()
        raise AcceptanceFailure(f"{request.method} {request.selector}: HTTP {code}: {self.redactor.safe(text)}", status=code)


class OwnerClient(Client):
    def __init__(self, *arguments, redactor, **options):
        super().__init__(*arguments, **options)
        self.opener = build_opener(ProxyHandler({}), NoRedirect(), SafeErrors(redactor))


class BuildJourney:
    def __init__(self, client, report, data, fake, timeout):
        self.client, self.report, self.data, self.fake, self.timeout = client, report, data, fake, timeout
        self.venture = None
        self.provider = None
        self.page_fixture = None

    def path(self, suffix):
        require(self.venture, "Your venture could not be created, so this check cannot run")
        return f"/ventures/{self.venture}{suffix}"

    def allow(self, provider, allow):
        for name in ("claude", "codex", "cursor", "opencode"):
            if allow and name == provider:
                approve_fake_coding_tool(self.client, provider, self.fake / provider)
            else:
                result = self.client.request("POST", "/ui-config/coding-agents/consent", {"provider": name, "allow": False})
                require(result["consent"].get(name) is False, "Your coding tool permission was not saved")
        return True

    def create(self, idea):
        # shortcut: fixture names use letters; extend this check when numeric-name copy checks are fixed.
        name = "".join(chr(97 + value % 26) for value in uuid4().bytes[:8]).capitalize()
        result = self.client.request("POST", "/ventures", {"name": name, "raw_idea": idea, "start": False})
        self.venture = result["id"]
        self.client.headers["X-Workspace-Id"] = result["workspace_id"]
        session = self.client.request("POST", self.path("/idea/sessions"), {})
        session_path = self.path(f"/idea/sessions/{session['id']}")
        wait_for(lambda: any(turn.get("kind") == "question" for turn in self.client.request("GET", session_path).get("turns", [])),
                 self.timeout, "Your idea questions did not become ready")
        self.client.request("POST", session_path + "/messages", {"text": "Skip the remaining questions"})
        review = wait_for(lambda: self._session_state(session_path, "reviewing"), self.timeout,
                          "Your saved idea did not become ready to review")
        self.client.request("POST", session_path + "/confirm", {"revision_id": review["current_revision"]["id"], "start_research": False})
        wait_for(lambda: self._session_state(session_path, "confirmed"), self.timeout, "Your idea confirmation was not saved")
        return True

    def _session_state(self, path, state):
        session = self.client.request("GET", path)
        return session if session.get("state") == state else None

    def orders(self):
        return self.client.request("GET", self.path("/device-work-orders"))["orders"]

    def chat(self, message):
        return self.client.request("POST", self.path("/chat"), {"message": message, "idempotency_key": uuid4().hex})

    def snapshot(self):
        with sqlite3.connect(f"file:{self.data / 'local.db'}?mode=ro", uri=True) as database:
            jobs = database.execute("SELECT id FROM jobs WHERE venture_id = ? AND (kind LIKE '%build%' OR kind LIKE '%coding%') ORDER BY id", (self.venture,)).fetchall()
        files = sorted((str(path.relative_to(self.data)), path.read_bytes()) for path in self.data.rglob("index.html"))
        return {"orders": self.orders(), "jobs": jobs, "files": files, "runs": cli_runs(self.fake)}

    def build(self, label, message, *, hanging=False, product=False):
        before = self.orders()
        before_ids = {order["id"] for order in before}
        previous_runs = len(cli_runs(self.fake))
        (self.fake / "mode").write_text("hang" if hanging else "finish")
        if "landing" in label and self.page_fixture:
            (self.fake / "page-fixture.json").write_text(json.dumps({"root": str(ROOT),
                "template": json.loads(self.page_fixture.read_text())}))
        reply = self.report.step(label + " chat", lambda: self.chat(message), CONNECT, "services/company_os/app/chat/execute.py")

        def started():
            require(reply is not None, "Your chat request failed before a coding order could start")
            if product:
                require(reply.get("kind") == "build_tool_started"
                        and not any(action.get("confirmation") for action in reply.get("actions") or []),
                        "Your explicit product request did not start immediately without confirmation")
            text = reply.get("body_md") or reply.get("message") or reply.get("kind") or "empty reply"
            def admitted():
                order = next((order for order in self.orders() if order["id"] not in before_ids), None)
                if order is None and "landing" in label:
                    job = self.client.request("GET", self.path("/pagebuilder/profile")).get("current") or {}
                    if job.get("state") == "failed":
                        raise AcceptanceFailure("Your page brief stopped before invoking your tool: " + str(job.get("error") or "pagebuilder_job_failed"))
                return order
            order = wait_for(admitted,
                             self.timeout, "Your chat created no coding order: " + self.report.redactor.safe(str(text)))
            native = f"/native/device-work-orders/{self.venture}/{order['id']}"
            if "landing" not in label:
                self.client.request("POST", native + "/start", {})

            def invoked():
                if len(cli_runs(self.fake)) > previous_runs:
                    return True
                receipt = self.client.request("GET", native + "/status")
                if receipt.get("failure_code"):
                    raise AcceptanceFailure("Your coding order stopped before invoking your tool: " + str(receipt["failure_code"]))
                return False

            wait_for(invoked, self.timeout,
                     "Your build controller never invoked the connected coding tool")
            run = cli_runs(self.fake)[previous_runs]
            require(run["provider"] == self.provider, "Your order invoked a tool you did not allow")
            return {"order": order, "native": native, "run": run}

        work = self.report.step(label + " order starts", started, CONNECT)

        def events():
            require(work is not None, "Your coding order did not start, so there is no watch stream")
            def fetch():
                receipt = self.client.request("GET", work["native"] + "/status")
                return receipt if receipt.get("events") else None
            return wait_for(fetch, self.timeout, "Your tool streamed output but Build activity received no events")

        self.report.step(label + " watch events", events, "Open your venture > Build activity, refresh it and retry.", "packages/desktop_runtime/bridge.py; packages/desktop_runtime/device_worker.py")

        if hanging:
            def stop():
                require(work is not None, "Your hanging coding order did not start, so Stop cannot be verified")
                require(process_alive(work["run"]["pid"]), "Your hanging tool exited before Stop was requested")
                self.client.request("POST", work["native"] + "/cancel", {})
                wait_for(lambda: not process_alive(work["run"]["pid"]), self.timeout,
                         "Your Stop request returned but the coding process is still alive")
                def closed():
                    receipt = self.client.request("GET", work["native"] + "/status")
                    return receipt if receipt.get("state") == "stopped" and (
                        receipt.get("evidence") or {}).get("processes_stopped") is True else None
                receipt = wait_for(closed, self.timeout, "Your Stop request did not save a stopped result")
                wait_for(lambda: self.client.request("GET", self.path(f"/device-work-orders/{work['order']['id']}"))
                         .get("state") == "stopped", self.timeout,
                         "Your Stop request has not reached your saved order. Open Build activity, press Stop and retry.")
                require(receipt.get("task_verified") is False and not receipt.get("next_order_id"),
                        "Your stopped build reported completion or started more work")
                return True
            self.report.step(label + " Stop ends process", stop, "Open Build activity, press Stop and check your local helper logs before retrying.", "packages/desktop_runtime/bridge.py; services/company_os/app/build/device_stop.py")
            return

        def finished():
            require(work is not None, "Your coding order did not start, so no completed result can be checked")
            def fetch(follow_plan=True):
                receipt = self.client.request("GET", work["native"] + "/status")
                if follow_plan and (receipt.get("evidence") or {}).get("outcome") == "plan_ready":
                    parent = self.client.request("GET", self.path(f"/device-work-orders/{work['order']['id']}"))
                    if receipt.get("plan_posted") is False:
                        self.client.request("GET", self.path(f"/device-work-orders/{parent['id']}/plan"))
                    child_id = receipt.get("next_order_id") or next((order["id"] for order in self.orders()
                        if (order.get("manifest", {}).get("coding_loop_verification") or {}).get("plan_order_id") == parent["id"]), None)
                    if child_id:
                        child = self.client.request("GET", self.path(f"/device-work-orders/{child_id}"))
                        descriptor = child.get("manifest", {}).get("coding_loop_verification") or {}
                        parent_descriptor = parent.get("manifest", {}).get("coding_loop_verification") or {}
                        require(descriptor.get("plan_order_id") == parent["id"] and descriptor.get("phase") == "implement"
                            and descriptor.get("provider") == self.provider
                            and descriptor.get("workspace_slug") == parent_descriptor.get("workspace_slug")
                            and all(child.get(field) == parent.get(field) for field in ("actor_id", "device_id", "workspace_id", "requested_build")),
                            "Your implementation order does not match the plan, tool and scope you requested")
                        work["order"], work["native"] = child, f"/native/device-work-orders/{self.venture}/{child_id}"
                        return receipt
                    return None
                if completed_receipt(receipt):
                    return receipt
                if receipt.get("state") == "stopped" and receipt.get("task_verified") is False:
                    raise AcceptanceFailure(receipt.get("message") or "Your page checks failed. Open Build activity to review them, then choose Retry on Website.")
                if receipt.get("state") in {"failed", "stopped", "cancelled", "needs_you", "reconciliation_required", "not_started"} and receipt.get("failure_code"):
                    raise AcceptanceFailure("Your coding order stopped: " + str(receipt["failure_code"]))
            receipt = wait_for(fetch, self.timeout, "Your tool exited successfully but the order has no finished result")
            if (receipt.get("evidence") or {}).get("outcome") == "plan_ready":
                receipt = wait_for(lambda: fetch(follow_plan=False), self.timeout,
                    "Your implementation order did not finish")
            order = self.client.request("GET", self.path(f"/device-work-orders/{work['order']['id']}"))
            stop = order.get("native_stop") or {}
            require(order.get("state") == "stopped" and order.get("closed_at")
                    and stop.get("task_verified") is True and stop.get("processes_stopped") is True
                    and stop.get("evidence_digest") == receipt.get("evidence_digest"),
                    "Your local result is not bound to a finished, verified server order")
            folder = Path(work["run"]["workspace"]).resolve()
            require(folder.is_relative_to(self.data.resolve()), "Your build result escaped the disposable data folder")
            companion = "page-plan.txt" if "landing" in label and self.page_fixture else "README.md"
            require((folder / companion).is_file() and MARKER in (folder / "index.html").read_text(), "Your finished order did not preserve the coding tool's result files")
            require(not process_alive(work["run"]["pid"]), "Your finished order still has a running coding process")
            return receipt

        result = self.report.step(label + " finished result folder", finished, "Open Build activity, review the failed check and ask Charlie to fix it.")
        if product:
            def product_preview():
                require(result is not None, "Your product order did not finish, so its preview cannot start")
                build = (result.get("evidence") or {}).get("product_build") or {}
                require(build.get("task_verified") is True and build.get("functional_status") == "verified"
                        and build.get("source_commit") and build.get("file_manifest_sha256"),
                        "Your product files and app behaviour have not passed their checks")
                runs = cli_runs(self.fake)[previous_runs:]
                require(len(runs) >= 2 and all(run["provider"] == self.provider
                        and run["workspace"] == work["run"]["workspace"] and not process_alive(run["pid"]) for run in runs),
                        "Your plan and implementation did not finish in the same result folder with your selected tool")
                try:
                    self.client.request("POST", work["native"] + "/start", {})

                    def ready():
                        receipt = self.client.request("GET", work["native"] + "/status")
                        preview = receipt.get("product_preview") or {}
                        if preview.get("status") in {"failed", "expired"}:
                            raise AcceptanceFailure("Your checked product preview could not start")
                        return preview if preview.get("ready") is True else None

                    preview = wait_for(ready, self.timeout, "Your checked product preview never became ready")
                    url = urlsplit(preview.get("url") or "")
                    require(url.scheme == "http" and url.hostname == "127.0.0.1" and url.port
                            and not (url.username or url.password or url.query or url.fragment)
                            and url.path in {"", "/"}
                            and preview.get("source_commit") == build["source_commit"]
                            and preview.get("file_manifest_sha256") == build["file_manifest_sha256"],
                            "Your preview is not bound to your checked local product")
                    require(cli_runs(self.fake)[previous_runs:] == runs, "Your preview unexpectedly started another coding run")
                    opener = build_opener(ProxyHandler({}), NoRedirect())
                    with opener.open(preview["url"], timeout=self.timeout) as response:
                        document = response.read(1024 * 1024 + 1)
                        require(response.status == 200 and len(document) <= 1024 * 1024
                                and MARKER.encode() in document, "Your loopback preview does not serve your tool's product")
                    from desktop_runtime.loop_checks import _http_request, http_journey, verify_http_journey

                    folder = Path(work["run"]["workspace"])
                    steps = http_journey({"spec": (folder / "review/spec.md").read_text()})
                    checked = verify_http_journey(lambda step: _http_request(opener, preview["url"].rstrip("/"), step), steps)
                    require(checked["functional_status"] == "verified", "Your preview does not perform your saved product journey")
                    return True
                finally:
                    self.client.request("POST", work["native"] + "/cancel", {})
                    stopped = (self.client.request("GET", work["native"] + "/status").get("product_preview") or {})
                    require(stopped.get("processes_stopped") is True and not stopped.get("url"),
                            "Your product preview process did not stop")

            self.report.step(label + " usable preview", product_preview,
                "Open your venture > Build activity, review the failed check and ask Charlie to fix it.",
                "packages/desktop_runtime/loop_checks.py; packages/desktop_runtime/bridge.py")
        if "landing" in label:
            def preview():
                require(result is not None, "Your page order did not finish, so a usable preview cannot be verified")
                profile = self.client.request("GET", self.path("/pagebuilder/profile"))
                job = profile.get("current") or {}
                job_id = job.get("job_id") or job.get("id")
                require(job_id, "Your completed page order is not connected to a page preview")
                document = self.client.request("GET", self.path(f"/pagebuilder/jobs/{job_id}/preview-document"))["html"]
                require(MARKER in document and "<main" in document and "<h1" in document and 'href="' in document,
                        "Your page preview does not serve the connected tool's usable HTML")
                return True
            self.report.step(label + " usable preview", preview, "Open your venture > Website, choose your finished page and preview it.", "services/company_os/app/api/pagebuilder_api.py")

            def export():
                require(result is not None, "Your page order did not finish, so its export cannot be verified")
                archive = self.client.request("GET", self.path("/site/local/export.zip"), download=True)
                with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
                    require("index.html" in bundle.namelist() and MARKER in bundle.read("index.html").decode(),
                            "Your export does not contain the coding tool's page")
                return True
            self.report.step(label + " usable export", export, "Open your venture > Website > Export and retry.", "services/company_os/app/pagebuilder/local_publish.py")

    def denied(self, message):
        before = self.snapshot()
        reply = self.chat(message)
        text = str(reply.get("body_md") or reply.get("message") or "").lower()
        require("connect" in text and ("coding tool" in text or "codex" in text or "claude" in text)
                and ("settings" in text or "connections" in text or any("connect" in str(action).lower() for action in reply.get("actions", []))),
                "Your unallowed-tool reply did not offer the exact connection step")
        time.sleep(1)
        require(self.snapshot() == before, "Your unallowed-tool chat started an order, internal generator or coding process")
        return True


def run(args, values, report, output):
    core, console, worker = free_ports()
    from coding_agents.env import CHILD_ENV_VARS

    inherited = load_environment(ROOT / ".env") if (ROOT / ".env").is_file() else {}
    with tempfile.TemporaryDirectory(prefix="local-build-journey-", dir=Path.home()) as temporary:
        data = Path(temporary) / "data"
        data.mkdir(mode=0o700)
        fake = Path(temporary) / "bin"
        for provider in ("claude", "codex", "opencode"):
            install_fake_cli(fake, provider)
        environment = {**{name: value for name, value in os.environ.items() if name in CHILD_ENV_VARS},
                       **{name: "" for name in inherited}, **values}
        environment.update(SUPYMEM_MODE="local", APP_ENV="local", SUPYMEM_PYTHON=sys.executable,
            PATH=f"{fake}:{os.environ.get('PATH', '')}", PYTHONPATH=f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}",
            HOME=str(data), COMPANY_OS_DATA_DIR=str(data), COMPANY_OS_DB_URL=f"sqlite:///{data}/local.db", DATABASE_URL=f"sqlite:///{data}/local.db",
            DB_URL=f"sqlite:///{data}/local.db", COMPANY_OS_WORKING_MEMORY_PATH=str(data / "memory.db"),
            REDIS_URL="", RDS_PROXY_ENDPOINT="", CORE_PORT=str(core), CONSOLE_PORT=str(console), WORKER_HEALTH_PORT=str(worker),
            CORE_URL=f"http://127.0.0.1:{core}", COMPANY_OS_URL=f"http://127.0.0.1:{core}",
            CONSOLE_ORIGIN=f"http://127.0.0.1:{console}", LLM_OFFLINE="0", LLM_CASSETTE_MODE="off", EMAIL_PROVIDER="log",
            PIP_NO_INDEX="1", COREPACK_ENABLE_NETWORK="0", PNPM_CONFIG_OFFLINE="true")
        client = OwnerClient(f"http://127.0.0.1:{core}", args.timeout, owner_secret_file=data / "owner-session.secret", redactor=report.redactor)
        log_lines = []
        stack = subprocess.Popen([str(ROOT / "start"), "--web"], cwd=ROOT, env=environment,
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 text=True, errors="replace", start_new_session=True)

        def collect():
            with stack.stdout, (output / "stack.log").open("w") as log:
                (output / "stack.log").chmod(0o600)
                for line in stack.stdout:
                    safe = report.redactor.safe(line).rstrip()
                    log_lines.append(safe)
                    del log_lines[:-40]
                    log.write(safe + "\n")

        reader = threading.Thread(target=collect, daemon=True)
        reader.start()
        try:
            def ready():
                deadline = time.monotonic() + args.startup_timeout
                while time.monotonic() < deadline:
                    if stack.poll() is not None:
                        raise AcceptanceFailure("Your ./start --web exited: " + " ".join(log_lines[-5:]))
                    try:
                        status = client.request("GET", "/setup/status")
                        with socket.socket() as probe:
                            console_up = probe.connect_ex(("127.0.0.1", console)) == 0
                        with socket.socket() as probe:
                            worker_up = probe.connect_ex(("127.0.0.1", worker)) == 0
                        if console_up and worker_up and status.get("mode") == "local":
                            require(status.get("llm_mode") == "live", "Your app did not import the live model into your saved settings")
                            return True
                    except AcceptanceFailure:
                        pass
                    time.sleep(0.25)
                raise AcceptanceFailure("Your real API, worker or console did not become ready. " + " ".join(log_lines[-5:]))

            if not report.step("real local stack", ready, "Run ./start --web, fix its reported prerequisite, then rerun this journey.", "start; scripts/run_local.sh"):
                return
            journey = BuildJourney(client, report, data, fake, args.timeout)
            for provider in ("claude", "codex", "opencode"):
                journey.provider = provider
                def status_ready():
                    tools = client.request("GET", "/local-tools/coding-agents")["tools"]
                    status = next((tool for tool in tools if tool["name"] == provider), {})
                    require(status.get("connected") or status.get("candidate_path") == str((fake / provider).resolve()),
                            "Your test coding tool was not found in its private installation")
                    return True
                report.step(provider + " detected", status_ready, CONNECT, "packages/desktop_runtime/agent_status.py")
                for index, idea in enumerate(args.idea, 1):
                    fixtures = getattr(args, "page_fixtures", None)
                    journey.page_fixture = fixtures / f"{index}.json" if fixtures else None
                    label = f"{provider} domain {index}"
                    if not report.step(label + " venture", lambda: journey.create(idea), "Open your app, create your venture and retry.", "services/company_os/app/api/ventures.py"):
                        continue
                    report.step(label + " allow tool", lambda: journey.allow(provider, True), CONNECT, "services/company_os/app/coding_agents_consent.py")
                    builds = (("landing", "build a landing page"),) if getattr(args, "pages_only", False) else (
                        ("landing", "build a landing page"), ("product", "build the first version of the product"))
                    for kind, message in builds:
                        journey.build(label + " " + kind, message, product=kind == "product")
                    if not getattr(args, "pages_only", False):
                        journey.build(label + " hanging", "build the first version of the product", hanging=True, product=True)
                    report.step(label + " disallow tool", lambda: journey.allow(provider, False), CONNECT)
                    for kind, message in builds:
                        report.step(label + " unallowed " + kind + " connects without generation", lambda message=message: journey.denied(message), CONNECT, "services/company_os/app/chat/execute.py; services/company_os/app/chat/product_build_request.py")
        finally:
            stop_stack(stack)
            reader.join(timeout=5)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    parser.add_argument("--findings", type=Path, default=DEFAULT_FINDINGS)
    parser.add_argument("--idea", action="append", required=True)
    parser.add_argument("--timeout", type=float, default=25)
    parser.add_argument("--startup-timeout", type=float, default=120)
    parser.add_argument("--pages-only", action="store_true")
    parser.add_argument("--offline-landing-timing", action="store_true")
    parser.add_argument("--page-fixtures", type=Path, default=ROOT / "tests/unit/pagebuilder/fixtures/local_tool_pages")
    args = parser.parse_args(argv)
    require(len(set(args.idea)) >= 2, "Pass --idea twice for two unrelated venture domains, then retry.")
    require(all(math.isfinite(value) and value > 0 for value in (args.timeout, args.startup_timeout)), "Choose positive finite timeouts and retry.")
    if args.offline_landing_timing:
        os.environ.update(SUPYMEM_MODE="local", APP_ENV="local")
        sys.path.insert(0, str(ROOT))
        from tests.company_os_harness import load_company_os_app
        load_company_os_app()
        passed = 0
        for index, _idea in enumerate(args.idea, 1):
            fixture = json.loads((args.page_fixtures / f"{index}.json").read_text())
            for mode in ("landing_slow", "landing_timeout"):
                result = offline_landing_timing(fixture, mode)
                print("PASS " + json.dumps({"domain": index, **result}), flush=True)
                passed += 1
        print(f"RESULT {passed} PASS / 0 FAIL; offline landing timing", flush=True)
        return 0
    output = ROOT / "artifacts/local-build-journey" / uuid4().hex
    output.mkdir(parents=True, mode=0o700)
    values = load_environment(args.env_file)
    settings = provider_settings(values)
    require(settings["provider"] == "azure_openai", "Choose your configured Azure model in --env-file, then retry. This live gate does not contact other services.")
    report = Report(args.findings, [value for name, value in values.items() if any(word in name.lower() for word in ("key", "password", "secret", "token"))])
    try:
        run(args, values, report, output)
    except KeyboardInterrupt:
        report.step("interrupted", lambda: require(False, "Your journey was interrupted"), "Restart your app and rerun your journey.")
    finally:
        (output / "report.json").write_text(json.dumps(report.results, indent=2))
        (ROOT / "artifacts/buildjourney.txt").write_text("\n".join(
            f"{record['state']} {record['step']}" + (f": {record['root_cause']}. {record['next_step']}" if record['state'] == "FAIL" else "")
            for record in report.results) + f"\n{len(report.results) - report.failures} PASS / {report.failures} FAIL\n")
    print(f"RESULT {len(report.results) - report.failures} PASS / {report.failures} FAIL; {output.relative_to(ROOT)}/report.json", flush=True)
    if report.append_error:
        print("Your shared findings file is not writable. Append buildgate__LIVE_FINDINGS.md to ~/lanes/LIVE_FINDINGS.md.", flush=True)
    return int(report.failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
