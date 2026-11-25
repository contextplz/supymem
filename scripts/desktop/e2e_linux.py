"""Gate the packaged desktop using its loopback APIs and its own WebKit DOM.

Sample mode is offline. Live mode imports a private file or fills setup fields.
"""

from __future__ import annotations

import ast
import hashlib
import html
import json
import os
import re
import signal
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import tomllib
import threading
import traceback
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.error import URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from local_acceptance import AcceptanceFailure, Client, NoRedirect
from local_build_journey import MARKER, cli_runs, install_fake_cli

STEPS = ("splash", "setup", "sample", "charlie", "idea", "name", "skip", "start", "people", "experiment")
TIMEOUT = 90
ADDED_STEPS = ("draft", "consent country", "restart", "settings", "no hosted links")
PRODUCT_SOURCES = {
    "name": "services/company_os/app/entry/chat_start.py, services/company_os/app/entry/service.py and services/company_os/app/idea/runner.py",
    "draft": "services/company_os/app/chat/customers.py:2127",
    "consent country": "services/company_os/app/chat/customers.py:768",
    "restart": "src-tauri/src/local_stack.rs:26",
    "settings": "apps/console/src/components/layout/AppShell.tsx:1035",
    "no hosted links": "apps/console/src/components/layout/SideNav.tsx:46",
}


def nothing_sent(state):
    journey = state.get("journey", {})
    return (journey.get("results", {}).get("sent") == 0
            and all(draft.get("state") == "draft" and not draft.get("sent_at")
                    for draft in journey.get("drafts", [])))


def hosted_link(url):
    parsed = urlsplit(url)
    host = unquote(parsed.hostname or "").lower().rstrip(".")
    path = unquote(parsed.path).lower()
    return (hosted_host(host)
            and (re.search(r"/(?:auth|account|login|sign[-_]?in|sign[-_]?up|buy|purchase|checkout|pricing)(?:/|$)", path)
                 or "buy" in unquote(parsed.fragment).lower()))


def hosted_host(host):
    host = host.lower().rstrip(".")
    return host == "supymem.com" or host.endswith(".supymem.com")


def dns_host(packet):
    if len(packet) < 13 or packet[2] & 128 or packet[4:6] != b"\x00\x01":
        return None
    labels, offset = [], 12
    while offset < len(packet):
        length = packet[offset]
        offset += 1
        if not length:
            return ".".join(labels).lower() if labels else None
        if length > 63 or offset + length > len(packet):
            return None
        try:
            label = packet[offset:offset + length].decode("ascii")
        except UnicodeDecodeError:
            return None
        if not re.fullmatch(r"[a-zA-Z0-9_-]+", label):
            return None
        labels.append(label)
        offset += length
    return None


class NetworkLog:
    def __init__(self, output):
        self.output = output
        self.hosts = set()
        self.connections = 0
        self.health_requests = {}
        self.reader, self.writer = os.pipe()
        self.thread = threading.Thread(target=self.collect, daemon=True)
        self.thread.start()

    def collect(self):
        with os.fdopen(self.reader) as stream, (self.output / "network.log").open("a") as log:
            for line in stream:
                if "connect(" in line:
                    self.connections += 1
                    address = re.search(r'inet_(?:addr|pton)\([^)]*\)', line)
                    if address:
                        log.write(address.group() + "\n")
                if ("readv(" in line or "writev(" in line) and not re.search(r'<(?:TCP|UDP):', line):
                    continue
                for quoted in re.findall(r'"(?:\\.|[^"\\])*"', line):
                    try:
                        packet = ast.literal_eval("b" + quoted)
                    except (SyntaxError, ValueError):
                        continue
                    self.record_health(line, packet)
                    host = dns_host(packet)
                    if host:
                        self.hosts.add(host)
                        log.write("DNS " + host + "\n")
                log.flush()

    def record_health(self, line, packet):
        connection = re.search(r'TCP:\[(127\.0\.0\.1:\d+)->(127\.0\.0\.1:\d+)\]', line)
        socket_identity = re.search(r'<(TCP:\[\d+\])>', line)
        if not connection and not socket_identity:
            return
        request = re.match(rb'GET (/health|/ready) HTTP/1\.[01]\r\n', packet)
        response = re.match(rb'HTTP/1\.[01] (\d{3}) ', packet)
        endpoints = connection.groups() if connection else (socket_identity[1],)
        if request:
            host = re.search(rb'(?i)\r\nHost: 127\.0\.0\.1:(\d+)\r\n', packet)
            port = int(host[1]) if host else int(endpoints[1].rsplit(":", 1)[1]) if connection else None
            self.health_requests[endpoints] = {"path": request[1].decode(), "started": time.monotonic(), "port": port}
            record = {"path": request[1].decode(), "status": None, "port": port,
                      "owner_credential_supplied": b"x-local-proxy-secret:" in packet.lower()}
        elif response and (endpoints in self.health_requests or endpoints[::-1] in self.health_requests):
            pending = self.health_requests.pop(endpoints if endpoints in self.health_requests else endpoints[::-1])
            record = {"path": pending["path"], "status": int(response[1]),
                      "seconds": round(time.monotonic() - pending["started"], 3), "port": pending["port"]}
        else:
            return
        with (self.output / "health_probes.jsonl").open("a") as log:
            log.write(json.dumps(record) + "\n")

    def record_url(self, url):
        host = urlsplit(url).hostname
        if host:
            self.hosts.add(host.lower())
            with (self.output / "network.log").open("a") as log:
                log.write("DOM " + host.lower() + "\n")

    def close(self):
        os.close(self.writer)
        self.thread.join(timeout=15)
        if self.thread.is_alive() or not self.connections:
            raise RuntimeError("Your network capture is incomplete. Install strace, then rerun your packaged desktop check.")
        if any(hosted_host(host) for host in self.hosts):
            raise RuntimeError("Your app contacted a supymem.com host. Open network.log, remove the hosted request from your local app, then rebuild and rerun.")


def model_settings(path):
    values = {}
    for line in path.read_text().splitlines():
        name, separator, value = line.strip().removeprefix("export ").partition("=")
        if separator and not name.startswith("#"):
            values[name.strip()] = " ".join(shlex.split(value, comments=True))
    providers = {
        "openai": ("OPENAI_API_KEY", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
        "azure_openai": ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT", ""),
        "anthropic": ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", ""),
        "openai_compatible": ("OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL", ""),
    }
    provider = values.get("SUPYMEM_LLM_PROVIDER") or next((name for name, fields in providers.items() if values.get(fields[0])), None)
    if provider not in providers:
        raise RuntimeError("Your model key is missing. Add your model API key and SUPYMEM_MODEL_CHAT to E2E_ENV_FILE, then rerun live mode.")
    key, address, default = providers[provider]
    settings = {"provider": provider, "api_key": values.get(key), "base_url": values.get(address, default),
                "model": values.get("SUPYMEM_MODEL_CHAT")}
    settings = {name: value.strip() if isinstance(value, str) else value for name, value in settings.items()}
    missing = [name for field, name in (("api_key", key), ("base_url", address), ("model", "SUPYMEM_MODEL_CHAT")) if not settings[field]]
    if missing:
        raise RuntimeError("Add " + ", ".join(missing) + " to E2E_ENV_FILE, or enter these in setup > Connect your AI service, then rerun.")
    endpoint = urlsplit(settings["base_url"])
    if (settings["api_key"] in settings["model"] or settings["api_key"] in unquote(settings["base_url"])
            or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment):
        raise RuntimeError("Keep your key only in the model API key field. Remove credentials from your model name and service address in E2E_ENV_FILE, then rerun setup > Connect your AI service.")
    if provider == "azure_openai":
        # shortcut: this headless runner lacks gateway dependencies, use normalize_base_url if Azure endpoint rules change.
        address = settings["base_url"].rstrip("/")
        settings["base_url"] = (address if address.endswith("/openai/v1") else
                                address + "/v1" if address.endswith("/openai") else address + "/openai/v1")
    return settings


def prepare_env_file(source, data, entry, environment=None):
    data.mkdir(parents=True, exist_ok=True, mode=0o700)
    if entry == "env":
        environment = dict(environment or os.environ)
        environment.update(SUPYMEM_MODE="local", APP_ENV="local", COMPANY_OS_DATA_DIR=str(data),
            SUPYMEM_DESKTOP_INSTANCE_ID="encrypted-import",
            PYTHONPATH=f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}")
        importer = """from pathlib import Path
import sys
from app.core import local_env_import
local_env_import.env_path = lambda: Path(sys.argv[1])
local_env_import.import_local_env(strict=True)
"""
        result = subprocess.run([str(ROOT / ".desktop-python-x86_64/bin/python"), "-c", importer, str(source.resolve())],
            env=environment, capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError("Your settings could not be imported securely. Open Keys and connections, check your model settings and unlock your system keychain, then retry.")


def capabilities_ready(screen, capabilities):
    titles = {"ready": "Ready", "needs_setup": "Needs setup", "off": "Off", "not_in_this_version": "Update your local app"}
    return (screen.get("documents") == 1 and bool(capabilities) and "Capabilities" in screen.get("text", "")
            and all(any(section.get("title") == item.get("title")
                        and titles.get(item.get("state"), "invalid state") in section.get("text", "")
                        and all(field in section["text"] for field in item.get("missing", []))
                        and item.get("next_step", "") in section["text"] for section in screen.get("sections", []))
                    for item in capabilities))


def install_desktop_cli(directory, provider, python):
    binary = install_fake_cli(directory, provider)
    interpreter = directory / "fixture-python"
    if not interpreter.is_symlink():
        interpreter.symlink_to(python)
    sites = [str(path) for path in (Path(python).parent.parent / "lib").glob("python*/site-packages")]
    # shortcut: fixture helpers must stay self-contained; use standalone shared fixtures if their imports grow.
    helpers = ast.parse((ROOT / "scripts/local_build_journey.py").read_text())
    source = "\n\n".join(ast.unparse(node) for node in helpers.body
        if isinstance(node, ast.FunctionDef) and node.name == "fixture_page")
    signup = ast.parse((ROOT / "tests/unit/product/test_static_publication.py").read_text())
    source += "\n\nimport hashlib\nfrom urllib.parse import urlsplit\n" + next(ast.unparse(node) for node in signup.body
        if isinstance(node, ast.FunctionDef) and node.name == "_signup_fixture")
    body = binary.read_text().split("\n", 1)[1]
    original = "    from local_build_journey import fixture_page"
    if original not in body:
        raise RuntimeError("Your shared coding-tool fake changed. Update scripts/desktop/e2e_linux.py to reuse its page fixture routines, then rerun.")
    social_import = "    from tests.unit.pagebuilder.test_tool_build import _with_social_preview"
    if social_import in body:
        metadata = ast.parse((ROOT / "tests/unit/pagebuilder/test_metadata.py").read_text())
        source += "\n\nimport binascii, struct, zlib\n" + next(ast.unparse(node) for node in metadata.body
            if isinstance(node, ast.FunctionDef) and node.name == "png")
        social = ast.parse((ROOT / "tests/unit/pagebuilder/test_tool_build.py").read_text())
        source += "\n\n" + next(ast.unparse(node) for node in social.body
            if isinstance(node, ast.FunctionDef) and node.name == "_with_social_preview").replace("from tests.unit.pagebuilder.test_metadata import png", "")
        body = body.replace(social_import, "")
    bootstrap = "    namespace = {'copy': __import__('copy'), 'html': html, 'json': json, 're': re, 'MARKER': " + repr(MARKER) + "}\n    exec(" + repr(source) + ", namespace)\n    fixture_page = namespace['fixture_page']\n    _with_social_preview = namespace['_with_social_preview']"
    body = body.replace("            from tests.unit.product.test_static_publication import _signup_fixture", "            _signup_fixture = namespace['_signup_fixture']")
    binary.write_text("#!" + str(interpreter) + "\nimport sys\nsys.path[:0] = " + repr(sites) + "\n" + body.replace(original, bootstrap))
    return binary


def live_idea(scenario):
    return "\n".join((scenario["idea"], *scenario["answers"].values()))


def page_fixture(scenario):
    answers, copy = scenario["answers"], scenario["landing_page"]
    sections = [{"id": identity, "purpose": purpose, "headline": headline, "body": body}
        for identity, purpose, headline, body in (
            ("hero", "Introduce the confirmed offer.", copy["headline"], copy["hero"]),
            ("problem", "Explain the confirmed customer problem.", "Your current challenge", answers["problem"]),
            ("how-it-works", "Explain three confirmed workflow steps.", "How it works", " ".join(copy["steps"])),
            ("audience", "Identify the confirmed audience.", "Who this is for", answers["customer"]),
            ("faq", "State what remains unconfirmed.", "When can you try it?", "Availability and pricing are to be confirmed."),
            ("waitlist", "Invite the visitor to early access.", "Follow early access", "Join the early-access list for updates."))]
    sections[0]["image_id"], sections[1]["image_id"] = "hero_art", "problem_art"
    return {"input": {"name": scenario["name"]}, "draft": {
        "brief": {"audience": answers["customer"], "problem": answers["problem"], "promise": copy["hero"],
            "tone": "Clear and practical", "proof_points": copy["steps"], "cta": "Join the early-access list",
            "concept_disclosure": "Join the waitlist for early-access updates."},
        "direction": {"concept": "An open invitation with original abstract artwork",
            "palette": {"background": "#f6f1e8", "text": "#213226", "primary": "#426340", "accent": "#c5954b"},
            "palette_family": "Warm paper and green", "display_font": "Georgia", "body_font": "Arial",
            "layout_archetype": "Left aligned copy above artwork with a visible mobile call to action",
            "density": "balanced", "motion": "none", "illustration_style": "Original abstract shapes without product screenshots",
            "section_rhythm": "Spacious sections with one clear heading and short copy"},
        "sections": sections, "assets": [{"id": identity, "role": role, "section_id": section,
            "subject": "Abstract shapes for an early-access invitation",
            "prompt": "Original abstract illustration without product features or text labels.",
            "scene": {"objects": [{"kind": kind, "x": 600, "y": 450, "scale": .7}]}}
            for identity, role, section, kind in (("hero_art", "hero", "hero", "orbit_rings"),
                ("problem_art", "section", "problem", "wave_bands"))]}}


def text_links(value):
    if isinstance(value, dict):
        return [link for item in value.values() for link in text_links(item)]
    if isinstance(value, list):
        return [link for item in value for link in text_links(item)]
    return re.findall(r"(?:https?:)?//[^\s<>\"')]+", value) if isinstance(value, str) else []


class Desktop:
    """Use the packaged WebKit view's loopback inspector, not another browser."""

    def __init__(self):
        import gi
        gi.require_version("Soup", "3.0")
        from gi.repository import Gio, GLib, Soup
        self.glib, self.soup = GLib, Soup
        self.session = Soup.Session()
        self.session.props.proxy_resolver = Gio.SimpleProxyResolver.new(None, [])
        self.connection = None
        self.target = None
        self.responses = {}
        self.sequence = 0
        self.network = None
        self.console_port = None

    def await_result(self, predicate):
        loop = self.glib.MainLoop()
        def ready():
            if predicate():
                loop.quit()
                return False
            return True
        polling = self.glib.timeout_add(10, ready)
        timeout = self.glib.timeout_add_seconds(10, lambda: loop.quit() or False)
        loop.run()
        if not predicate():
            self.glib.source_remove(polling)
            raise OSError("Your packaged window inspector did not respond. Restart your app and rerun the check.")
        self.glib.source_remove(timeout)

    def connect(self):
        address = "127.0.0.1:" + os.environ["E2E_INSPECTOR_PORT"]
        with build_opener(ProxyHandler({})).open("http://" + address + "/", timeout=3) as response:
            listing = response.read().decode()
        if self.network:
            (self.network.output / "inspector_targets.html").write_text(listing)
        targets = re.findall(r'targeturl">(.*?)</div>.*?(/socket/\d+/\d+/WebPage)', listing)
        target = next((path for url, path in targets if urlsplit(html.unescape(url)).hostname == "127.0.0.1"
                       and urlsplit(html.unescape(url)).port == self.console_port), None)
        if not target:
            raise OSError("Your local console window is not inspectable yet. Wait for startup, then rerun.")
        completed = []
        def connected(session, result, *unused):
            try:
                self.connection = session.websocket_connect_finish(result)
                self.connection.connect("message", self.message)
                completed.append(True)
            except self.glib.Error:
                completed.append(False)
        self.session.websocket_connect_async(self.soup.Message.new("GET", "ws://" + address + target), None, None, 0, None, connected)
        self.await_result(lambda: bool(completed))
        if not completed[0]:
            raise OSError("Your window inspector connection failed. Restart your app, then rerun.")
        self.await_result(lambda: self.target is not None)
        self.command("Network.enable")

    def message(self, connection, kind, data):
        message = json.loads(bytes(data.get_data()))
        if message.get("method") == "Target.targetCreated" and message["params"]["targetInfo"]["type"] == "page":
            self.target = message["params"]["targetInfo"]["targetId"]
        if message.get("method") == "Target.dispatchMessageFromTarget":
            message = json.loads(message["params"]["message"])
        if self.network:
            record = {"method": message.get("method"), "id": message.get("id"), "error": bool(message.get("error"))}
            if message.get("method") == "Network.responseReceived":
                response = message["params"]["response"]
                record.update(path=urlsplit(response.get("url", "")).path, status=response.get("status"),
                              owner_cookie=any(key.lower() == "set-cookie" and "supymem_owner=" in value
                                               for key, value in response.get("headers", {}).items()))
            with (self.network.output / "inspector_protocol.log").open("a") as log:
                log.write(json.dumps(record) + "\n")
        if "id" in message:
            identifier = message["id"]
            if identifier < 1_000_000:
                self.responses[identifier] = message
            elif "error" in message:
                self.responses[identifier - 1_000_000] = message
        if message.get("method") == "Network.requestWillBeSent" and self.network:
            self.network.record_url(message["params"]["request"]["url"])

    def command(self, method, params=None):
        self.sequence += 1
        sequence = self.sequence
        command = json.dumps({"id": sequence, "method": method, "params": params or {}})
        self.connection.send_text(json.dumps({"id": sequence + 1_000_000, "method": "Target.sendMessageToTarget",
            "params": {"targetId": self.target, "message": command}}))
        self.await_result(lambda: sequence in self.responses)
        response = self.responses.pop(sequence)
        if "error" in response:
            if self.network and method == "Network.enable":
                (self.network.output / "inspector_error.json").write_text(json.dumps(response, indent=2))
            raise OSError("Your window inspector command failed. Update WebKitGTK, then rerun.")
        return response["result"]

    def evaluate(self, expression):
        if self.connection is None:
            self.connect()
        result = self.command("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        if result.get("wasThrown"):
            raise OSError("Your window action failed. Open the screenshot, check the screen, then rerun.")
        return result.get("result", {}).get("value")

    def read(self, click=None):
        return self.evaluate("""(() => {
            const label = %s;
            const control = label && [...document.querySelectorAll('button,a,[role=button]')].find(node =>
                (node.getAttribute('aria-label') || node.getAttribute('title') || node.textContent).trim() === label);
            if (control) control.click();
            return {text: document.body.innerText, links: [...document.querySelectorAll('a[href]')].map(node => node.href),
                sections: [...document.querySelectorAll('section')].map(node => ({title: node.querySelector('h2')?.innerText, text: node.innerText})),
                password_lengths: [...document.querySelectorAll('input[type=password]')].map(node => node.value.length),
                owner_proof_ready: !!document.getElementById('local-owner-session')?.textContent.includes('X-Local-Session-Proof'),
                clicked: !!control, documents: 1, url: location.href};
        })()""" % json.dumps(click))

    def fill(self, selector, value):
        changed = self.evaluate("""(() => {
            const node = document.querySelector(%s);
            if (!node) return false;
            const prototype = node.tagName === 'SELECT' ? HTMLSelectElement.prototype :
                node.tagName === 'TEXTAREA' ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
            Object.getOwnPropertyDescriptor(prototype, 'value').set.call(node, %s);
            node.dispatchEvent(new Event('input', {bubbles: true}));
            node.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
        })()""" % (json.dumps(selector), json.dumps(value)))
        if not changed:
            raise RuntimeError("Your setup field is missing. Open the setup screenshot, update your app, then rerun.")

    def navigate(self, path):
        self.evaluate("location.assign(" + json.dumps(path) + "); true")

    def close(self):
        if self.connection:
            self.connection.close(1000, None)
            self.connection = None
            self.target = None


class UnreadableWindow(RuntimeError):
    pass


def screenshot_name(step):
    return f"{STEPS.index(step):02d}_{step}.png"


def save_result(output, step, passed, message):
    path = output / "results.json"
    results = json.loads(path.read_text()) if path.exists() else []
    results.append({"step": step, "passed": passed,
        "screenshot": (step.replace(" ", "_") + ".png") if (output / (step.replace(" ", "_") + ".png")).exists()
            else screenshot_name(step) if step in STEPS and os.environ.get("E2E_MODE") not in {"sample", "live"} else None,
        "message": message})
    if passed is None:
        results[-1]["skipped"] = True
    path.write_text(json.dumps(results, indent=2) + "\n")


def draft_request(person, country):
    name = person.get("name")
    if not isinstance(name, str) or not name.strip() or "@" in name:
        raise RuntimeError("Your sample person has no usable name. Choose a named sample person, then rerun.")
    return f"Draft an interview email to {name} in {country}. Do not send anything."


def venture_name_matches(venture, name):
    expected = "Sample: " + name if venture.get("sample") is True and not name.startswith("Sample: ") else name
    return venture.get("name") == expected


def step_ready(step, state, scenario):
    provider = state.get("provider", {})
    session = state.get("session", {})
    if step == "no hosted links":
        pages = state.get("pages", [])
        links = [link for page in pages for link in page.get("links", [])]
        links.extend(text_links(state.get("chat", [])))
        return (bool(pages) and not state.get("unread_pages") and all(page.get("text") for page in pages)
                and not any(re.search(r"(?im)^\s*(?:sign in|log in|sign up|create an account)\s*$", page["text"]) for page in pages)
                and not any(hosted_link(link) for link in links))
    if step == "settings":
        screen = state.get("screen", {})
        text = screen.get("text", "")
        sample = provider.get("sample_selected", provider.get("sample_mode"))
        connected = (sample is True and provider.get("secrets_set", {}).get("api_key") is False
                     or sample is False and provider.get("configured") is True)
        return (state.get("settings_opened") is True and connected
                and re.search(r"\bprovider\b", text, re.I) and re.search(r"\bAPI key\b", text, re.I)
                and ("Keys and connections" in text or "Your local settings" in text)
                and re.search(r"leave (?:your |a |the )?key blank", text, re.I)
                and bool(screen.get("password_lengths")) and not any(screen["password_lengths"]))
    if step == "restart":
        before = state.get("before_restart", {})
        venture = state.get("venture", {})
        return (state.get("health", {}).get("status") == "ok" and bool(before.get("venture"))
                and all(venture.get(field) == before["venture"].get(field) for field in ("id", "name", "raw_idea"))
                and bool(before.get("chat")) and all(message in state.get("chat", []) for message in before["chat"])
                and bool(before.get("prospects")) and all(person in state.get("prospects", []) for person in before["prospects"]))
    if step in {"draft", "consent country"}:
        chat = state.get("chat", [])
        requests = [index for index, message in enumerate(chat)
                    if message.get("role") == "user" and message.get("body_md") == state.get("request")]
        replies = [message for message in chat[requests[-1] + 1:] if message.get("role") == "assistant"] if requests else []
        if (not replies or not nothing_sent(state)
            or any(message.get("confirmed_action", {}).get("state") in {"pending", "proposed", "awaiting_confirmation"}
                   or any(action.get("confirmation") for action in message.get("actions") or []) for message in replies)):
            return False
        reply = replies[-1]
        if step == "consent country":
            opening = re.split(r"\b(?:which|what)\s+(?:is\s+your\s+)?basis\b", reply.get("body_md", ""), maxsplit=1, flags=re.I)[0]
            bases = [action.get("payload", {}).get("lawful_basis") for action in reply.get("actions", [])
                     if action.get("payload", {}).get("lawful_basis")]
            return (any(citation.get("question") is True for citation in reply.get("citations", []))
                    and re.search(r"\b(?:CA|Canada)\b", opening)
                    and re.search(r"\b(?:you need (?:their )?consent|(?:Canada|CA):? (?:needs?|requires?) consent)\b", opening, re.I)
                    and bases == ["consent"])
        person = state.get("person", {})
        host = (urlsplit(person.get("contact_source_url") or person.get("source_url") or "").hostname or "").removeprefix("www.")
        drafts = [draft for draft in state.get("journey", {}).get("drafts", [])
                  if draft.get("contact_id") == person.get("id") and draft.get("doc_id") not in state.get("prior_drafts", [])]
        if not drafts or not host or not re.search(r"\bSAMPLE draft\b", reply.get("body_md", "")):
            return False
        draft = drafts[-1]
        body = draft.get("body", "")
        lines = body.splitlines()
        displayed = reply.get("body_md", "").splitlines()
        return (any(message.get("role") == "user" and message.get("body_md") == state.get("request") for message in chat)
                and draft.get("sample") is True and draft.get("to") == person.get("email")
                and all(line in lines and line in displayed for line in (
                    f"Your email address is listed publicly on {host}.",
                    'If this is not relevant, just reply "no thanks" and I will not email you again.'))
                and "Sample sender address, not for sending" in body
                and "Sample sender address, not for sending" in reply.get("body_md", "")
                and re.search(r"(?m)^Sample email, not for sending\. From: [^\s@]+@[^\s@]+\.$", body)
                and re.search(r"(?m)^Sample email, not for sending\. From: [^\s@]+@[^\s@]+\.$", reply.get("body_md", "")))
    if step == "splash":
        health = state.get("health", {})
        return health.get("status") == "ok" or health.get("ok") is True
    if step == "setup":
        health = state.get("console_health", {})
        return provider.get("configured") is False and (health.get("ok") is True or health.get("status") == "ok")
    if step == "sample":
        return (provider.get("configured") is True and provider.get("sample_mode") is True
                and provider.get("values") == {} and provider.get("secrets_set", {}).get("api_key") is False)
    if step == "charlie":
        auth = state.get("auth", {})
        return (auth.get("authenticated") is True and auth.get("mode") == "local"
                and state.get("entries") == [] and state.get("ventures") == [])
    if step == "idea":
        return any(entry.get("text") == scenario["idea"] and entry.get("proposal", {}).get("state") == "pending"
                   for entry in state.get("entries", []))
    if step == "name":
        venture = state.get("venture", {})
        return (venture_name_matches(venture, scenario["name"]) and scenario["idea"] in (venture.get("raw_idea") or "")
                and any(turn.get("kind") == "question" and turn.get("text") for turn in session.get("turns", [])))
    if step == "questions":
        turns = session.get("turns", [])
        return bool(turns) and turns[-1].get("role") == "agent" and turns[-1].get("kind") == "question" and bool(turns[-1].get("text"))
    if step in {"answer", "review"}:
        turns = session.get("turns", [])
        if step == "answer" and turns and turns[-1].get("id") != state.get("previous_turn_id"):
            if turns[-1].get("role") == "agent" and turns[-1].get("kind") in {"question", "proposal"}:
                return bool(turns[-1].get("text"))
        revision = session.get("current_revision") or {}
        reviews = [run for run in session.get("validation_runs", []) if run.get("revision_id") == revision.get("id")]
        answers = session.get("gstack_answers", [])
        return (len(reviews) == 3 and all(run.get("state") == "complete" and run.get("report", {}).get("sample") is True
            and run["report"].get("verified") is False for run in reviews)
            and any(turn.get("role") == "founder" and turn.get("kind") in {"answer", "chip"} and turn.get("text") for turn in turns)
            and all(answer.get("answer") and not answer.get("skipped") for answer in answers)
            and not revision.get("structured", {}).get("_questions_skipped"))
    if step == "skip":
        revision = session.get("current_revision") or {}
        return session.get("state") == "reviewing" and revision.get("structured", {}).get("_questions_skipped") is True
    if step == "start":
        return session.get("state") == "confirmed"
    messages = state.get("chat", [])
    if not any(message.get("role") == "user" and message.get("body_md") == scenario[step] for message in messages):
        return False
    if any(message.get("confirmed_action", {}).get("state") in {"pending", "proposed", "awaiting_confirmation"}
           for message in messages if message.get("role") == "assistant"):
        return False
    if step == "people":
        journey = state.get("journey", {})
        return (journey.get("state") == "ready" and journey.get("discovery_started") is True
                and bool(journey.get("prospects")) and not journey.get("error_code")
                and journey.get("results", {}).get("sent", 0) == 0)
    return any(item.get("id") not in state.get("prior_experiments", [])
               and all(item.get(field) for field in ("hypothesis", "action", "measure"))
               and item.get("state") == "proposed" for item in state.get("experiments", []))


class Journey:
    def __init__(self, output, scenario):
        self.output = output
        self.scenario = scenario
        self.window = None
        self.step = "splash"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())
        self.owner_secret_file = None
        self.venture_id = None
        self.prior_experiments = []
        self.previous_turn_id = None
        self.recording = os.environ.get("E2E_RECORD") == "1"
        self.api_mode = os.environ.get("E2E_MODE") in {"sample", "live"}
        self.live = os.environ.get("E2E_MODE") == "live"
        self.entry = os.environ.get("E2E_ENTRY", "env")
        self.model = None
        self.search_provider = None
        self.firecrawl_configured = False
        self.network = None
        self.desktop = None
        self.fake = None
        self.runtime_python = None
        self.pages = []
        self.unread_pages = False
        self.failed = False
        self.settings_opened = False
        self.before_restart = {}
        self.person = {}
        self.request = ""
        self.prior_drafts = []

    def ports(self):
        matches = re.findall(r"desktop_startup phase=local_ports core=(\d+) worker=(\d+) console=(\d+)",
                             (self.output / "app.log").read_text())
        if not matches:
            raise ValueError("Your app has not selected its local ports yet. Wait for startup, then retry.")
        return dict(zip(("core", "worker", "console"), map(int, matches[-1])))

    def get(self, path, role="core", *, payload=None, download=False):
        ports = self.ports()
        port = ports[role]
        client = Client(f"http://127.0.0.1:{port}", 60,
                        owner_secret_file=self.owner_secret_file)
        client.opener = self.opener
        method = "POST" if payload is not None else "GET"
        record = {"method": method, "path": urlsplit(path).path, "role": role, "status": None}
        started = time.monotonic()
        try:
            result = client.request(method, path, payload, **({"download": True} if download else {}))
            record["status"] = client.response_status if isinstance(client.response_status, int) else 200
            if isinstance(result, dict):
                for field in ("status", "state"):
                    if isinstance(result.get(field), str) and result[field] in {"proposal", "reply", "committed", "pending", "clarifying", "reviewing", "confirmed"}:
                        record[field if field == "state" else "response_status"] = result[field]
                if isinstance(result.get("proposal"), dict):
                    proposal = result["proposal"]
                    record["proposal"] = {"needs_answer": bool(proposal.get("onboarding_question")),
                                          "needs_name": not bool(proposal.get("venture_name"))}
                if isinstance(result.get("items"), list):
                    record["items"] = len(result["items"])
                if isinstance(result.get("turns"), list):
                    record["turns"] = [{"question": turn.get("kind") == "question",
                                        "has_field": isinstance(turn.get("options"), dict) and
                                                     bool(turn["options"].get("field") or turn["options"].get("gstack_id"))}
                                       for turn in result["turns"]]
            return result
        except AcceptanceFailure as error:
            status = error.status if error.status is not None else client.response_status
            record["status"] = status if isinstance(status, int) else None
            record["error"] = type(error).__name__
            raise
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            with (self.output / "api_requests.jsonl").open("a") as log:
                log.write(json.dumps(record) + "\n")

    def state(self):
        if self.step == "splash":
            return {"health": self.get("/health")}
        if self.step in {"setup", "sample"}:
            state = {"provider": self.get("/local/providers")}
            if self.step == "setup":
                state["console_health"] = self.get("/api/health", "console")
            return state
        if self.step in {"charlie", "idea"}:
            return {"auth": self.get("/auth/session"), "entries": self.get("/entry/chat/history")["items"],
                    "ventures": self.get("/ventures")["items"]}
        if self.step == "name":
            ventures = self.get("/ventures")["items"]
            if self.api_mode and len(ventures) != 1:
                raise RuntimeError("Your fresh app saved an unexpected venture. Review your ventures before retrying.")
            found = next((item for item in ventures if venture_name_matches(item, self.scenario["name"])), {})
            self.venture_id = found.get("id")
            if not self.venture_id:
                return {"venture": {}}
        base = f"/ventures/{self.venture_id}"
        if self.step in ADDED_STEPS:
            selector = urlencode({"workspace_id": self.get(base)["workspace_id"]})
            state = {"venture": self.get(base), "chat": self.get(base + "/chat")["items"],
                     "journey": self.get("/api/v1" + base + "/gtm/journey?" + selector),
                     "person": self.person, "request": self.request, "prior_drafts": self.prior_drafts}
            if self.step == "restart":
                state.update(health=self.get("/health"), before_restart=self.before_restart,
                             prospects=state["journey"]["prospects"])
            if self.step in {"settings", "no hosted links"}:
                screen = self.read_screen()
                if screen["documents"]:
                    self.pages.append(screen)
                state.update(screen=screen, pages=self.pages, provider=self.get("/local/providers"),
                             settings_opened=self.settings_opened, unread_pages=self.unread_pages)
            return state
        sessions = self.get(base + "/idea/sessions")["items"]
        state = {"session": self.get(base + "/idea/sessions/" + sessions[0]["id"]) if sessions else {}}
        if self.step == "answer":
            state["previous_turn_id"] = self.previous_turn_id
        if self.step == "name":
            state["venture"] = self.get(base)
        if self.step in {"people", "experiment"}:
            state["chat"] = self.get(base + "/chat")["items"]
            if self.step == "people":
                selector = urlencode({"workspace_id": self.get(base)["workspace_id"]})
                state["journey"] = self.get("/api/v1" + base + "/gtm/journey?" + selector)
            else:
                state["experiments"] = self.get(base + "/experiments")["items"]
                state["prior_experiments"] = self.prior_experiments
        return state

    def capture(self):
        if self.api_mode:
            import gi
            gi.require_version("Gdk", "3.0")
            from gi.repository import Gdk
            window = Gdk.get_default_root_window()
            image = Gdk.pixbuf_get_from_window(window, 0, 0, window.get_width(), window.get_height())
            if image is None:
                raise OSError("Your screenshot could not be captured. Check Xvfb, then rerun.")
            image.savev(str(self.output / (self.step.replace(" ", "_") + ".png")), "png", [], [])
        else:
            subprocess.run([os.environ.get("E2E_SCREENSHOT_TOOL", "import"), "-window", "root",
                            str(self.output / screenshot_name(self.step))], check=True, timeout=15)

    def read_screen(self, click=None):
        if self.desktop is None:
            raise UnreadableWindow("Your packaged window could not be read. Enable its loopback WebKit inspector and rerun your desktop check.")
        try:
            self.desktop.console_port = self.ports()["console"]
            return self.desktop.read(click=click)
        except OSError:
            self.desktop.close()
            raise UnreadableWindow("Your packaged window could not be read. Review app.log, restart your app, then rerun your desktop check.") from None

    def open_screen(self, path=None, expected=None):
        if path:
            self.read_screen()
            self.desktop.navigate(path)
        deadline = time.monotonic() + TIMEOUT
        while time.monotonic() < deadline:
            try:
                screen = self.read_screen()
                if (screen.get("documents") and screen.get("text", "").strip()
                        and (not path or urlsplit(screen.get("url", "")).path == urlsplit(path).path)
                        and (not path or urlsplit(screen.get("url", "")).query == urlsplit(path).query)
                        and (not expected or expected in screen["text"])):
                    if (any(hosted_link(link) for link in screen["links"])
                            or re.search(r"(?im)^\s*(?:sign in|log in|sign up|create an account)\s*$", screen["text"])):
                        raise RuntimeError("Your local window shows hosted sign-in. Open the screenshot, remove the hosted screen, then rebuild and rerun.")
                    self.pages.append(screen)
                    self.capture()
                    (self.output / (self.step.replace(" ", "_") + "_dom.json")).write_text(json.dumps(screen, indent=2) + "\n")
                    return screen
            except (OSError, UnreadableWindow):
                pass
            time.sleep(0.2)
        raise RuntimeError(self.runtime_failure() or "Your expected screen did not render. Open the screenshot and app.log, update your local app, then rerun.")

    def runtime_failure(self):
        if self.owner_secret_file is None:
            return None
        for path in (self.owner_secret_file.parent / "logs").glob("desktop-local-*.log"):
            for line in path.read_text(errors="replace").splitlines():
                if re.fullmatch(r"Local (?:core|worker|console) (?:stopped responding|stopped)", line):
                    (self.output / "supervisor_failure.txt").write_text(line + "\n")
                    return f"Your desktop supervisor reported: {line}. Review src-tauri/src/local_stack.rs health checks and api_requests.jsonl, fix the stopped service or its health probe, then rebuild and rerun. Your saved work is retained."
        return None

    def passed(self):
        save_result(self.output, self.step, True, "Your packaged app result was verified.")
        print("PASS " + self.step, flush=True)

    def research_failure(self, job):
        if job.get("status") not in {"failed", "failed_dead", "poison", "canceled"}:
            return None
        code = job.get("error_code")
        if code not in {"research_outcome_uncertain", "research_provider_rejected", "research_invalid_output"}:
            code = "job_failed"
        diagnostic = {"status": job["status"], "error_code": code, "failures": []}
        if self.owner_secret_file is not None:
            database = self.owner_secret_file.parent / "database.sqlite3"
            try:
                with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as connection:
                    rows = connection.execute("SELECT summary FROM research_dossiers WHERE workspace_id=? AND venture_id=?",
                                              (job.get("workspace_id"), job.get("venture_id"))).fetchall()
                for (summary,) in rows:
                    runtime = json.loads(summary).get("_runtime", {})
                    if runtime.get("job_id") != job.get("id"):
                        continue
                    for operation in runtime.get("operations", {}).values():
                        failure = operation.get("failure", {})
                        safe = {}
                        if failure.get("category") in {"internal", "http", "timeout", "connection", "boundary"}:
                            safe["category"] = failure["category"]
                        for field, lower, upper in (("http_status", 100, 599), ("elapsed_ms", 0, 86_400_000)):
                            value = failure.get(field)
                            if type(value) is int and lower <= value <= upper:
                                safe[field] = value
                        if safe:
                            usage = operation.get("usage", {})
                            if usage.get("purpose") in {"research.fetch", "research.search", "research.review"}:
                                safe["purpose"] = usage["purpose"]
                            if usage.get("provider") in {"duckduckgo", "firecrawl", "openai", "azure", "tavily", "exa", "github"}:
                                safe["provider"] = usage["provider"]
                            diagnostic["failures"].append(safe)
            except (OSError, sqlite3.Error, ValueError, AttributeError, TypeError):
                pass
        (self.output / "research_failure.json").write_text(json.dumps(diagnostic, indent=2) + "\n")
        if (code == "research_outcome_uncertain" and self.search_provider == "builtin" and not self.firecrawl_configured
                and any(failure.get("category") == "internal" and failure.get("purpose") == "research.fetch" and failure.get("provider") == "duckduckgo"
                        for failure in diagnostic["failures"])):
            return "Your built-in page reader failed, and research treated that unbilled read as an uncertain paid call. Review services/company_os/app/signals/local_reader.py:300 and services/company_os/app/research/metered.py:189, classify only confirmed unbilled reader failures as failed, keep paid uncertainty protected, then rebuild and rerun."
        detail = ""
        if diagnostic["failures"]:
            failure = diagnostic["failures"][-1]
            detail = " (" + failure.get("category", "provider failure")
            if "http_status" in failure:
                detail += f" HTTP {failure['http_status']}"
            detail += ")"
        return f"Your research worker failed with {code}{detail}. Review services/company_os/app/research/metered.py:264 and research_failure.json, fix the recorded provider failure without retrying uncertain work, then rebuild and rerun."

    def poll(self, predicate):
        deadline = time.monotonic() + (300 if self.live and self.step in {"research", "find customers", "landing page"} else TIMEOUT)
        while time.monotonic() < deadline:
            if predicate():
                return
            if self.app.poll() is not None:
                break
            time.sleep(1)
        if self.step == "connect coding tools":
            raise RuntimeError("Your coding tool connection did not finish. Open Settings > Keys and connections > Coding tools, choose Check connection and Allow Charlie, then retry.")
        if self.step == "landing page":
            raise RuntimeError("Your coding tool page build or preview did not finish. Open Build activity to review its checks, then choose Retry on Website.")
        if self.step == "monthly spending limit":
            raise RuntimeError("Your monthly limit was not saved. Open Settings > Keys and connections > Your monthly spend cap, save your limit, then retry.")
        raise RuntimeError("Your requested result did not finish. Open Keys and connections to check your model and search settings, then retry the request.")

    def verify_resources(self):
        descendants = {self.app.pid}
        for _ in range(12):
            for entry in Path("/proc").iterdir():
                if not entry.name.isdigit():
                    continue
                try:
                    status = (entry / "status").read_text()
                    parent = int(re.search(r"^PPid:\s+(\d+)", status, re.M).group(1))
                    if parent in descendants:
                        descendants.add(int(entry.name))
                except (OSError, AttributeError):
                    continue
        executables, resources = [], None
        for process in descendants:
            try:
                directory = Path("/proc") / str(process)
                arguments = (directory / "cmdline").read_bytes().decode().split("\0")
                for argument in arguments:
                    if argument.endswith("/console/apps/console/server.js"):
                        resources = Path(argument).parents[3]
                executable = (directory / "exe").resolve()
                executables.append(executable)
                if executable.name == "node" and executable.parent.name == "bin" and executable.parents[2].name == "runtime":
                    resources = executable.parents[4]
            except (OSError, UnicodeDecodeError):
                continue
        (self.output / "resource_probe.json").write_text(json.dumps({"descendants": len(descendants), "resources": str(resources),
            "executables": [str(path) for path in executables]}, indent=2) + "\n")
        if resources is None or resources.resolve().is_relative_to(ROOT):
            raise RuntimeError("Your console is not running extracted AppImage resources. Launch your built AppImage without a checkout resource override, then rerun.")
        runtime = resources / "local/runtime"
        if len([path for path in executables if path.is_relative_to(runtime)]) < 3:
            raise RuntimeError("Your API, worker and console are not all packaged processes. Rebuild your AppImage with its local runtime, then rerun.")
        self.runtime_python = next(path for path in executables if path.is_relative_to(runtime) and path.name.startswith("python"))
        files = [resources / "console/apps/console/server.js", resources / "local/services/company_os/app/core/local_mode.py"]
        (self.output / "installed_resources.json").write_text(json.dumps({"resources": str(resources),
            "executables": sorted({str(path) for path in executables}),
            "sha256": {str(path.relative_to(resources)): hashlib.sha256(path.read_bytes()).hexdigest() for path in files}}, indent=2) + "\n")

    def connect_coding_tools(self):
        self.step = "coding tool required"
        capabilities = self.get("/local/capabilities")["capabilities"]
        for name in ("landing_pages", "build_with_my_tool"):
            item = next(item for item in capabilities if item["name"] == name)
            if (item["state"] != "needs_setup" or not any("coding tool" in field.lower() for field in item["missing"])
                    or "Coding tools" not in item["next_step"] or "connect" not in item["next_step"].lower()):
                raise RuntimeError("Your landing page must require your coding tool. Open Settings > Capabilities before connecting a tool, then check its Coding tools setup step.")
        self.open_screen("/settings/local/capabilities", "Capabilities")
        self.poll(lambda: capabilities_ready(self.read_screen(), capabilities))
        self.desktop.evaluate('document.querySelector(\'section[aria-labelledby="landing_pages-title"]\').scrollIntoView({block: "center"})')
        self.open_screen(expected="Capabilities")
        self.passed()
        for provider in ("claude", "codex"):
            install_desktop_cli(self.fake, provider, self.runtime_python)
        self.step = "connect coding tools"
        self.open_screen("/settings/local", "Coding tools")
        for provider, label in (("claude", "Claude Code"), ("codex", "Codex")):
            tile = 'article[aria-label="' + label + '"]'
            self.poll(lambda: self.desktop.evaluate("!!document.querySelector(" + json.dumps(tile + " button:not(:disabled)") + ")"))
            clicked = self.desktop.evaluate("""(() => {
                const node = document.querySelector(%s)?.querySelector('button');
                if (!node || node.disabled) return false;
                node.click(); return true;
            })()""" % json.dumps(tile))
            if not clicked:
                raise RuntimeError("Your tool connection button is missing. Open Settings > Keys and connections > Coding tools, choose Check connection, then rerun.")
            selector = 'input[aria-label="Allow Charlie to use ' + label + '"]'
            self.poll(lambda: self.desktop.evaluate("!!document.querySelector(" + json.dumps(selector + ":not(:disabled)") + ")"))
            self.desktop.evaluate("""(() => {
                const node = document.querySelector(%s);
                if (!node || node.disabled) return false;
                if (!node.checked) node.click(); return true;
            })()""" % json.dumps(selector))
            self.poll(lambda: self.get("/ui-config")["coding_agents"]["consent"].get(provider) is True)
        if cli_runs(self.fake):
            raise RuntimeError("Connecting your coding tool started unrequested work. Rebuild your app so Allow Charlie only saves your permission, then rerun.")
        self.open_screen(expected="Coding tools")
        self.passed()

    def verify_capabilities(self, names=("research", "find_customers", "build_with_my_tool", "landing_pages")):
        self.step = "capabilities"
        capabilities = self.get("/local/capabilities")["capabilities"]
        self.open_screen("/settings/local/capabilities", "Capabilities")
        self.poll(lambda: capabilities_ready(self.read_screen(), capabilities))
        for name in names:
            item = next(item for item in capabilities if item["name"] == name)
            if item["state"] == "off":
                clicked = self.desktop.evaluate("""(() => {
                    const node = [...document.querySelectorAll('input[role=switch]')].find(node => node.getAttribute('aria-label') === %s);
                    if (!node || node.disabled) return false;
                    node.click(); return true;
                })()""" % json.dumps("Enable " + item["title"]))
                if not clicked:
                    raise RuntimeError("Your feature switch is missing. Open Settings > Capabilities, switch on your requested features, then rerun.")
                self.poll(lambda: next(item for item in self.get("/local/capabilities")["capabilities"] if item["name"] == name)["state"] != "off")
            current = next(item for item in self.get("/local/capabilities")["capabilities"] if item["name"] == name)
            if current["state"] != "ready":
                raise RuntimeError(current["next_step"] or "Open Settings > Capabilities, finish setup for your requested feature, then rerun.")
        capabilities = self.get("/local/capabilities")["capabilities"]
        self.poll(lambda: capabilities_ready(self.read_screen(), capabilities))
        self.open_screen(expected="Capabilities")
        self.passed()

    def prepare_experiments(self):
        spend = self.get("/local/settings/spend")["values"]
        if spend.get("monthly_spend_cap") is None:
            self.step = "monthly spending limit"
            self.open_screen("/settings/local", "Your monthly spend cap")
            self.poll(lambda: self.desktop.evaluate("!!document.querySelector('#spend-monthly_spend_cap:not(:disabled)')"))
            self.desktop.fill("#spend-monthly_spend_cap", "5.00")
            if not self.desktop.evaluate("document.querySelector('section[aria-labelledby=spend-title] form')?.requestSubmit(); true"):
                raise RuntimeError("Open Settings > Keys and connections > Your monthly spend cap, save your limit, then retry.")
            self.poll(lambda: self.get("/local/settings/spend")["values"].get("monthly_spend_cap") is not None)
            self.passed()
        self.verify_capabilities(("run_for_me_experiments",))

    def answer_live_idea(self, base):
        self.step = "answer"
        turn = {}
        def question_ready():
            nonlocal turn
            session = self.get(base)
            if session["state"] == "reviewing":
                turn = {}
                return True
            turns = session["turns"]
            last_answer = max((index for index, item in enumerate(turns) if item.get("role") == "founder"), default=-1)
            turn = next((item for item in reversed(turns[last_answer + 1:])
                         if item.get("role") == "agent" and item.get("kind") == "question"), {})
            return bool(turn)
        self.poll(question_ready)
        if turn:
            options = turn.get("options") or {}
            field = options.get("gstack_id") or options.get("field")
            if not field:
                raise RuntimeError("Your saved idea question has no answer field. Review services/company_os/app/idea/service.py and services/company_os/app/idea/runner.py, fix the returned question metadata, then rebuild and rerun.")
            if field in self.scenario["answers"]:
                self.get(base + "/messages", payload={"text": self.scenario["answers"][field]})
                self.poll(lambda: any(turn.get("role") == "founder" and turn.get("kind") in {"answer", "chip"}
                                      for turn in self.get(base)["turns"]))
        if self.get(base)["state"] != "reviewing":
            self.get(base + "/messages", payload={"text": "Skip the questions"})
        self.get(base + "/confirm", payload={"revision_id": self.get(base)["current_revision"]["id"], "start_research": False})

    def run_live(self):
        self.step = "splash"
        self.wait()
        self.step = "installed resources"
        self.verify_resources()
        self.passed()
        self.step = "setup"
        if self.entry == "screens":
            self.poll(lambda: self.get("/local/providers").get("configured") is False)
            self.open_screen(expected="Connect")
            if "Connect your AI service" not in self.read_screen()["text"]:
                if not self.read_screen(click="Connect a model")["clicked"]:
                    raise RuntimeError("Your model connection button is missing. Review apps/console/src/app/setup/LocalSetup.tsx, rebuild your app, then rerun.")
            self.open_screen(expected="Connect your AI service")
            self.poll(lambda: bool(self.desktop.evaluate("!!document.querySelector('#provider:not(:disabled)')")))
            self.desktop.fill("#provider", self.model["provider"])
            required = ("api_key", "model", "base_url") if self.model["provider"] in {"azure_openai", "openai_compatible"} else ("api_key", "model")
            self.poll(lambda: all(self.desktop.evaluate("!!document.querySelector(" + json.dumps("#" + field + ":not(:disabled)") + ")") for field in required))
            for field in ("api_key", "model", "base_url"):
                if self.desktop.evaluate("!!document.querySelector(" + json.dumps("#" + field) + ")"):
                    self.desktop.fill("#" + field, self.model[field])
            if not self.read_screen(click="Save and continue")["clicked"]:
                raise RuntimeError("Your setup save button is missing. Open setup.png, update your app, then rerun.")
        self.poll(lambda: self.get("/local/providers").get("configured") is True
                  and self.get("/local/providers").get("sample_selected") is False
                  and self.get("/setup/status").get("llm_mode") == "live")
        saved = self.get("/local/providers")["values"]
        mismatched = [field for field in ("provider", "model", "base_url")
                      if (saved.get(field) or "").rstrip("/") != self.model[field].rstrip("/")]
        if mismatched:
            (self.output / "model_settings_mismatch.json").write_text(json.dumps({"fields": mismatched}) + "\n")
            raise RuntimeError("Your setup did not save the supplied " + ", ".join(mismatched) + ". Review services/company_os/app/core/providers_local.py and services/company_os/app/core/local_env_import.py. Open Keys and connections > Your AI provider, enter the same service, model and endpoint as E2E_ENV_FILE, then rerun.")
        self.poll(lambda: "Connect your AI service" not in self.read_screen()["text"]
                  and "Checking your AI settings" not in self.read_screen()["text"])
        if self.entry == "screens":
            self.read_screen(click="Continue to your workspace")
        self.open_screen("/")
        self.passed()
        self.connect_coding_tools()
        self.verify_capabilities()
        self.step = "create venture"
        idea = live_idea(self.scenario)
        proposal = self.get("/entry/chat/preview", payload={"text": idea})["proposal"]
        self.get("/entry/chat/confirm", payload={"proposal_id": proposal["id"], "name": self.scenario["name"]})
        ventures = self.get("/ventures")["items"]
        if len(ventures) != 1:
            raise RuntimeError("Your fresh app did not save exactly one venture. Open your workspace, check the result, then rerun.")
        self.venture_id = ventures[0]["id"]
        self.open_screen("/new?" + urlencode({"venture": self.venture_id}))
        self.passed()
        sessions = self.get(f"/ventures/{self.venture_id}/idea/sessions")["items"]
        base = f"/ventures/{self.venture_id}/idea/sessions/{sessions[0]['id']}"
        self.answer_live_idea(base)
        self.open_screen(f"/v/{self.venture_id}")
        self.passed()
        self.step = "research"
        chat_path = f"/ventures/{self.venture_id}/chat"
        before = {item["id"] for item in self.get(chat_path)["items"]}
        search = self.get("/local/settings/search")["values"]
        provider = search.get("provider", "builtin")
        self.search_provider = provider if provider in {"builtin", "tavily", "exa", "openai", "firecrawl"} else None
        self.firecrawl_configured = bool(search.get("firecrawl_url"))
        (self.output / "research_settings.json").write_text(json.dumps({
            "provider": self.search_provider, "firecrawl_configured": self.firecrawl_configured}) + "\n")
        self.get(chat_path, payload={"message": "Research the market and competitors for this venture. Include public sources."})
        jobs_path = f"/workspaces/{ventures[0]['workspace_id']}/jobs?" + urlencode({
            "venture_id": self.venture_id, "kinds": "research.deep"})
        def sourced():
            replies = [item for item in self.get(chat_path)["items"] if item["id"] not in before and item.get("role") == "assistant"]
            for job in self.get(jobs_path)["items"]:
                failure = self.research_failure(job)
                if failure:
                    raise RuntimeError(failure)
            links = text_links([item.get("citations", []) for item in replies])
            return bool(replies) and any(urlsplit(link).scheme in {"http", "https"}
                and not hosted_host(urlsplit(link).hostname or "") and not (urlsplit(link).hostname or "").endswith(".invalid") for link in links)
        self.poll(sourced)
        self.open_screen(f"/v/{self.venture_id}")
        self.passed()
        self.step = "find customers"
        self.get(chat_path, payload={"message": self.scenario["people"]})
        selector = urlencode({"workspace_id": ventures[0]["workspace_id"]})
        def found_people():
            result = self.get(f"/api/v1/ventures/{self.venture_id}/gtm/journey?" + selector)
            return (bool(result.get("prospects")) and not result.get("error_code") and result.get("results", {}).get("sent", 0) == 0
                    and all(person.get("source_url") or person.get("contact_source_url") for person in result["prospects"]))
        self.poll(found_people)
        self.open_screen(f"/v/{self.venture_id}")
        self.passed()
        self.step = "landing page"
        (self.fake / "page-fixture.json").write_text(json.dumps({"root": str(ROOT), "template": page_fixture(self.scenario)}))
        before_runs = len(cli_runs(self.fake))
        self.get(chat_path, payload={"message": "Build a private landing page for this venture. Do not publish it."})
        page_path = f"/ventures/{self.venture_id}/pagebuilder"
        job = {}
        def built():
            profile = self.get(page_path + "/profile")
            candidate = profile.get("current") or {}
            if candidate.get("state") == "failed":
                raise RuntimeError(candidate.get("error_reason") or "Your coding tool page build failed. Open Build activity to review its checks, then choose Retry on Website.")
            if candidate.get("job_id") and candidate.get("state") == "ready":
                job.update(candidate)
                return True
            return False
        self.poll(built)
        runs = cli_runs(self.fake)[before_runs:]
        if not runs or any(run["provider"] not in {"claude", "codex"} for run in runs):
            raise RuntimeError("Your landing page did not use your connected coding tool. Open Settings > Keys and connections > Coding tools, allow your tool, then rebuild the page.")
        (self.output / "landing-page-tool-runs.json").write_text(json.dumps(runs, indent=2) + "\n")
        self.open_screen(f"/v/{self.venture_id}/site")
        self.poll(lambda: self.desktop.evaluate("!!document.querySelector('button[data-usage-control=website_open_full_preview]:not(:disabled)')"))
        self.desktop.evaluate("document.querySelector('button[data-usage-control=website_open_full_preview]:not(:disabled)').click()")
        self.poll(lambda: self.desktop.evaluate("""(() => {
            const frame = document.querySelector('dialog[open] iframe[title="Full landing page preview"]');
            if (!frame || !frame.getBoundingClientRect().height || !frame.srcdoc.includes(%s)) return false;
            const previewDocument = new DOMParser().parseFromString(frame.srcdoc, 'text/html');
            return !!previewDocument.querySelector('h1') && previewDocument.body.textContent.includes(%s);
        })()""" % (json.dumps(MARKER), json.dumps(self.scenario["name"]))))
        self.open_screen()
        self.passed()
        self.step = "export"
        payload = self.get(page_path + "/jobs/" + job["job_id"] + "/source?variant=0", download=True)
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            if (len(payload) > 20_000_000 or sum(item.file_size for item in archive.infolist()) > 20_000_000
                    or not any(name.endswith(".html") for name in archive.namelist()) or archive.testzip()):
                raise RuntimeError("Your landing page export is incomplete. Open your Site screen, rebuild the page, then export again.")
        (self.output / "landing-page.zip").write_bytes(payload)
        self.passed()
        self.step = "settings"
        self.open_screen("/settings/local", "Keys and connections")
        self.settings_opened = True
        self.poll(lambda: step_ready("settings", {"settings_opened": True, "screen": self.read_screen(),
                  "provider": self.get("/local/providers")}, self.scenario))
        self.open_screen(expected="Keys and connections")
        self.passed()
        self.verify_capabilities()
        self.step = "no hosted links"
        if not step_ready(self.step, {"pages": self.pages, "chat": self.get(chat_path)["items"]}, self.scenario):
            raise RuntimeError("Your local app contains hosted account links. Remove them from your local screens, then rebuild and rerun.")
        self.passed()

    def wait(self):
        deadline = time.monotonic() + TIMEOUT
        reason = "Your app did not save the expected result."
        while time.monotonic() < deadline:
            if self.api_mode and self.app.poll() is not None:
                raise RuntimeError(f"Your app exited with status {self.app.poll()} before saving this step. Review app.log, scripts/desktop/linux/launch.sh and src-tauri/src/local_stack.rs, fix the startup error, then rebuild and rerun.")
            try:
                state = self.state()
                if self.step == "name":
                    session = state.get("session", {})
                    (self.output / "name_state.json").write_text(json.dumps({
                        "venture_saved": bool(state.get("venture")), "session_state": session.get("state"),
                        "turns": [{"role": turn.get("role"), "kind": turn.get("kind"), "has_text": bool(turn.get("text"))}
                                  for turn in session.get("turns", [])]}, indent=2) + "\n")
                    if state.get("venture"):
                        reason = ("Your named venture was saved, but its first question never appeared."
                                  if not any(turn.get("kind") == "question" and turn.get("text") for turn in session.get("turns", []))
                                  else "Your named venture did not keep the original idea you entered.")
                    else:
                        reason = "Your venture was created, but it does not keep the name you chose."
                if self.api_mode and self.step in {"draft", "consent country"}:
                    evidence = {key: state[key] for key in ("request", "person", "chat")}
                    evidence["journey"] = {key: state["journey"].get(key) for key in ("drafts", "prospects", "results")}
                    (self.output / (self.step.replace(" ", "_") + "_state.json")).write_text(json.dumps(evidence, indent=2) + "\n")
                if step_ready(self.step, state, self.scenario):
                    time.sleep(0 if self.api_mode else 3 if self.recording else 1)
                    if self.recording and self.step == "people":
                        self.input("mousemove", "--window", self.window, 800, 350,
                                   "click", "--repeat", 3, "--delay", 80, 4)
                        time.sleep(4)
                    if self.recording and self.step == "experiment":
                        time.sleep(6)
                    self.capture()
                    if self.api_mode and self.desktop is not None and self.step != "no hosted links":
                        self.open_screen()
                    save_result(self.output, self.step, True, "Your saved app state matches this step.")
                    print(f"PASS {self.step}", flush=True)
                    return
            except (AcceptanceFailure, URLError, TimeoutError, OSError, ValueError, KeyError) as error:
                status = getattr(error, "status", None) or getattr(error, "code", None)
                reason = str(error) if isinstance(error, AcceptanceFailure) else (
                    f"Your local API returned HTTP {status}." if status else "Your local API is not ready.")
            time.sleep(1)
        source = f" Review {PRODUCT_SOURCES[self.step]} for a product bug, then rebuild and rerun." if self.step in PRODUCT_SOURCES else (
            " Review app.log, update your app, then rerun." if self.api_mode else " Review the screenshot, update your app, then rerun.")
        raise RuntimeError(self.runtime_failure() or reason + source)

    def input(self, *args):
        subprocess.run(["xdotool", *map(str, args)], check=True, timeout=60)

    def click(self, horizontal, vertical):
        self.input("mousemove", "--window", self.window, horizontal, vertical, "click", 1)

    def send(self, text):
        self.input("key", "ctrl+a")
        self.input("type", "--clearmodifiers", "--delay", 95 if self.recording else 85, "--", text)
        time.sleep(2)
        self.input("key", "Return")

    def run(self):
        if self.api_mode:
            self.run_api()
            return
        deadline = time.monotonic() + TIMEOUT
        window_class = tomllib.loads((ROOT / "src-tauri/Cargo.toml").read_text())["package"]["name"]
        while time.monotonic() < deadline:
            windows = subprocess.run(["xdotool", "search", "--onlyvisible", "--class", window_class],
                                     capture_output=True, text=True, timeout=5)
            if windows.returncode == 0:
                self.window = windows.stdout.splitlines()[0]
                break
            if self.app.poll() is not None:
                raise RuntimeError("Your AppImage stopped before opening. Rebuild it, then rerun.")
            time.sleep(0.2)
        if not self.window:
            raise RuntimeError("Your desktop window did not open. Check your AppImage, then rerun.")
        time.sleep(3)
        self.capture()
        self.wait()
        self.input("windowsize", self.window, 1000, 850)
        self.input("windowmove", self.window, 220 if self.recording else 0, 25 if self.recording else 0)
        self.input("windowfocus", self.window)
        self.step = "setup"
        self.wait()
        self.step = "sample"
        if self.get("/local/providers").get("sample_mode") is not True:
            raise RuntimeError("Your packaged app does not offer sample entry. Rebuild with sample support, then rerun.")
        self.click(20, 20)
        self.input("key", "ctrl+End", "shift+Tab", "Return")
        self.wait()
        self.step = "charlie"
        self.wait()
        # shortcut: manual-run 1000x850 control positions, upgrade when a desktop test navigation API exists.
        self.click(500, 488)
        time.sleep(2)
        self.step = "idea"
        self.click(700, 780)
        self.send(self.scenario["idea"])
        self.wait()
        self.step = "name"
        self.send(self.scenario["name"])
        self.wait()
        self.step = "skip"
        self.click(700, 810)
        self.send("Skip the questions")
        self.wait()
        self.step = "start"
        self.click(543, 763)
        self.wait()
        self.prior_experiments = [item["id"] for item in self.get(f"/ventures/{self.venture_id}/experiments")["items"]]
        for step in ("people", "experiment"):
            self.step = step
            if self.recording and step == "experiment":
                self.input("mousemove", "--window", self.window, 800, 350,
                           "click", "--repeat", 30, "--delay", 80, 5)
                time.sleep(1)
            self.click(700, 810)
            self.send(self.scenario[step])
            self.wait()

    def run_api(self):
        self.desktop = Desktop()
        self.desktop.network = self.network
        if self.live:
            self.run_live()
            return
        for step in ("splash", "setup"):
            self.step = step
            self.wait()
            if step == "setup":
                self.open_screen(expected="Try without a key (sample mode)")
        self.step = "installed resources"
        self.verify_resources()
        self.passed()
        self.step = "sample"
        self.open_screen(expected="Try without a key (sample mode)")
        if not self.read_screen(click="Try without a key (sample mode)")["clicked"]:
            raise RuntimeError("Your sample button is missing. Open setup.png, rebuild your local app, then rerun.")
        self.wait()
        if self.get("/setup/status")["llm_mode"] != "sample":
            raise RuntimeError("Your sample choice was not saved. Choose sample mode again, then retry.")
        self.step = "charlie"
        self.wait()
        self.open_screen("/")
        self.step = "idea"
        proposal = self.get("/entry/chat/preview", payload={"text": self.scenario["idea"]})["proposal"]
        self.wait()
        self.step = "name"
        self.get("/entry/chat/confirm", payload={"proposal_id": proposal["id"], "name": self.scenario["name"]})
        self.wait()
        self.open_screen("/new?" + urlencode({"venture": self.venture_id}))
        session = self.state()["session"]
        base = f"/ventures/{self.venture_id}/idea/sessions/{session['id']}"
        self.step = "questions"
        self.wait()
        for _ in range(20):
            state = self.state()
            if step_ready("review", state, self.scenario):
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
                field = options.get("gstack_id") or options.get("field")
                self.get(base + "/messages", payload={"text": self.scenario["answers"][field]})
            self.wait()
        self.step = "review"
        self.wait()
        self.step = "start"
        self.get(base + "/confirm", payload={"revision_id": self.state()["session"]["current_revision"]["id"], "start_research": False})
        self.wait()
        self.prior_experiments = [item["id"] for item in self.get(f"/ventures/{self.venture_id}/experiments")["items"]]
        self.prepare_experiments()
        self.open_screen(f"/v/{self.venture_id}")
        for step in ("people", "experiment"):
            self.step = step
            self.get(f"/ventures/{self.venture_id}/chat", payload={"message": self.scenario[step]})
            self.wait()
        self.run_extended()

    def stop(self):
        if self.desktop:
            self.desktop.close()
        if self.app is not None and self.app.poll() is None:
            os.killpg(self.app.pid, signal.SIGTERM)
            try:
                self.app.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(self.app.pid, signal.SIGKILL)
                self.app.wait()

    def skip(self):
        raise UnreadableWindow("Your window evidence is missing. Enable the loopback WebKit inspector, then rerun your release check.")

    def run_extended(self):
        base = f"/ventures/{self.venture_id}"
        for step in ADDED_STEPS:
            self.step = step
            try:
                state = self.state() if step not in {"settings", "no hosted links"} else None
                if step in {"draft", "consent country"}:
                    people = [person for person in state["journey"]["prospects"]
                              if (urlsplit(person.get("contact_source_url") or person.get("source_url") or "").hostname or "").endswith(".example.invalid")
                              and (person.get("email") or "").split("@")[-1].endswith(".example.invalid")]
                    if len(people) < 2:
                        raise RuntimeError("Your sample people are missing. Rebuild your app with sample support, then rerun.")
                    if step == "draft" and not state["journey"].get("sender_postal_address"):
                        self.get(base + "/chat", payload={"message": "My address is Sample sender address, not for sending"})
                    self.person = people[0 if step == "draft" else 1]
                    self.prior_drafts = [draft["doc_id"] for draft in state["journey"]["drafts"]]
                    country = "US" if step == "draft" else "Canada"
                    self.request = draft_request(self.person, country)
                    self.get(base + "/chat", payload={"message": self.request})
                    if step == "draft":
                        chat = self.get(base + "/chat")["items"]
                        reply = next(message for message in reversed(chat) if message["role"] == "assistant")
                        consent = next((action for action in reply.get("actions", [])
                                        if action.get("payload", {}).get("lawful_basis") == "consent"), None)
                        if consent:
                            self.get(base + "/chat/confirm", payload={"action": consent["action"], "payload": consent["payload"]})
                            self.get(base + "/chat", payload={"message": "Sample-only consent for this fictional person. This is not permission to contact anyone."})
                elif step == "restart":
                    self.before_restart = {"venture": state["venture"], "chat": state["chat"],
                                           "prospects": state["journey"]["prospects"]}
                    self.stop()
                    self.app = self.launch()
                elif step == "settings":
                    self.open_screen()
                    if not self.read_screen(click="Settings")["clicked"]:
                        self.open_screen("/settings/local")
                    self.settings_opened = True
                elif step == "no hosted links":
                    links = text_links(self.get(base + "/chat")["items"]) + [link for page in self.pages for link in page["links"]]
                    if any(hosted_link(link) for link in links):
                        raise RuntimeError("Your saved chat links to a hosted sign-in or buy page. Remove that link in your local app, then rerun.")
                    if self.desktop is None or self.unread_pages:
                        raise UnreadableWindow("Your window evidence is incomplete. Restart your app and rerun the release check.")
                self.wait()
            except (OSError, RuntimeError, ValueError, KeyError, StopIteration, subprocess.SubprocessError) as error:
                self.failed = True
                message = str(error) if isinstance(error, RuntimeError) else "Your saved result could not be checked. Review app.log, then rerun."
                if PRODUCT_SOURCES[step] not in message:
                    message += f" Review {PRODUCT_SOURCES[step]} if your app result is wrong, then rebuild and rerun."
                save_result(self.output, step, False, message)
                print(f"FAIL {step}: {message}", file=sys.stderr, flush=True)


def summary(output):
    path = output / "results.json"
    results = json.loads(path.read_text()) if path.exists() else []
    passed = sum(item["passed"] is True for item in results)
    failed = sum(item["passed"] is False for item in results)
    skipped = sum(item["passed"] is None for item in results)
    print(f"SUMMARY PASS={passed} FAIL={failed} SKIP={skipped} artifacts={output}", flush=True)
    return int(bool(failed or skipped or not results))


def main():
    appimage, output = Path(sys.argv[1]), Path(sys.argv[2])
    os.umask(0o077)
    os.environ["GSETTINGS_BACKEND"] = "memory"
    if any(output.glob("*.png")) or (output / "results.json").exists() or (output / "recording.mkv").exists():
        raise SystemExit("Your evidence folder already has a run. Choose a new folder, then rerun.")
    scenarios = json.loads((ROOT / "tests/fixtures/desktop_e2e_linux.json").read_text())
    scenario = scenarios.get(os.environ.get("E2E_SCENARIO", "enterprise"))
    if not scenario:
        raise SystemExit(f"Choose E2E_SCENARIO from {', '.join(scenarios)}, then rerun.")
    if os.environ.get("E2E_RECORD") == "1":
        scenario = {**scenario, **scenario["demo"]}
    journey = Journey(output, scenario)
    if journey.live:
        try:
            journey.model = model_settings(Path(os.environ["E2E_ENV_FILE"]))
        except (OSError, KeyError, ValueError, RuntimeError) as error:
            message = str(error) if isinstance(error, RuntimeError) else "Your live key file could not be read. Set E2E_ENV_FILE to your private .env file, then rerun."
            save_result(output, "setup", False, message)
            print("FAIL setup: " + message, flush=True)
            return summary(output)
    with tempfile.TemporaryDirectory(prefix="desktop-e2e-", dir=Path.home() if journey.live else None) as temporary, (output / "app.log").open("w") as log, socket.socket() as occupied:
        data_name = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["productName"]
        journey.owner_secret_file = Path(temporary) / "data" / data_name / "owner-session.secret"
        environment = {key: os.environ[key] for key in ("PATH", "DISPLAY", "XAUTHORITY", "LANG", "DBUS_SESSION_BUS_ADDRESS") if key in os.environ}
        environment.update(HOME=temporary, XDG_DATA_HOME=temporary + "/data", XDG_CONFIG_HOME=temporary + "/config",
                           XDG_CACHE_HOME=temporary + "/cache", SUPYMEM_MODE="local",
                           COMPANY_OS_DATA_DIR=str(journey.owner_secret_file.parent),
                           NEXT_TELEMETRY_DISABLED="1",
                           GSETTINGS_BACKEND="memory", WEBKIT_DISABLE_COMPOSITING_MODE="1", LIBGL_ALWAYS_SOFTWARE="1")
        if journey.live:
            journey.fake = Path(temporary) / "bin"
            journey.fake.mkdir(mode=0o700)
            environment["PATH"] = str(journey.fake) + os.pathsep + environment.get("PATH", os.defpath)
        with socket.socket() as inspector:
            inspector.bind(("127.0.0.1", 0))
            inspector_port = inspector.getsockname()[1]
        os.environ["E2E_INSPECTOR_PORT"] = str(inspector_port)
        environment["WEBKIT_INSPECTOR_HTTP_SERVER"] = "127.0.0.1:" + str(inspector_port)
        if journey.live:
            product = json.loads((ROOT / "src-tauri/tauri.local.json").read_text())["productName"]
            prepare_env_file(Path(os.environ["E2E_ENV_FILE"]), Path(environment["XDG_DATA_HOME"]) / product, journey.entry, environment)
        if not journey.api_mode:
            environment.update(LLM_OFFLINE="1", LLM_CASSETTE_MODE="replay", EMAIL_PROVIDER="log", GUARDRAILS_MODE="deterministic_only")
        network = NetworkLog(output)
        journey.network = network
        log_threads = []
        def launch():
            process = subprocess.Popen(["strace", "-f", "-qq", "-yy", "-s", "4096", "-e", "trace=connect,sendto,sendmsg,sendmmsg,recvfrom,recvmsg,writev,readv",
                "-o", f"/proc/self/fd/{network.writer}", str(appimage.resolve()), "--appimage-extract-and-run"],
                cwd=temporary, env=environment, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                pass_fds=(network.writer,), start_new_session=True)
            def collect_log():
                for line in iter(process.stdout.readline, b""):
                    text = line.decode("utf-8", errors="replace")
                    if journey.model:
                        text = text.replace(journey.model["api_key"], "[redacted]")
                    log.write(text)
                    log.flush()
            thread = threading.Thread(target=collect_log, daemon=True)
            thread.start()
            log_threads.append(thread)
            return process
        journey.launch = launch
        journey.app = None
        recorder = None
        keyring = None
        completed = False
        try:
            if journey.live and (daemon := shutil.which("gnome-keyring-daemon")):
                keyring = subprocess.Popen([daemon, "--foreground", "--components=secrets", "--unlock"],
                    env=environment, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
                keyring.stdin.write(os.urandom(32).hex().encode())
                keyring.stdin.close()
            if os.environ.get("E2E_OCCUPY_PORT"):
                occupied.bind(("127.0.0.1", int(os.environ["E2E_OCCUPY_PORT"])))
                occupied.listen()
            if journey.recording:
                recorder = subprocess.Popen(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                                             "-f", "x11grab", "-framerate", "20", "-video_size", "1440x900",
                                             "-i", os.environ["DISPLAY"], "-c:v", "libx264", "-preset", "ultrafast",
                                             "-crf", "18", "-pix_fmt", "yuv420p", str(output / "recording.mkv")],
                                            stdout=log, stderr=log)
            journey.app = journey.launch()
            journey.run()
            completed = True
        except (AcceptanceFailure, OSError, RuntimeError, ValueError, TypeError, KeyError, StopIteration, zipfile.BadZipFile, subprocess.SubprocessError, KeyboardInterrupt) as error:
            trace = type(error).__name__ + "\n" + "".join(traceback.format_tb(error.__traceback__))
            message = str(error) if isinstance(error, (AcceptanceFailure, RuntimeError)) else "Your desktop tools or API failed. Check your prerequisites and AppImage, then rerun."
            if journey.live and not isinstance(error, (AcceptanceFailure, RuntimeError)):
                message = "Your live check failed. Open Keys and connections > Your AI provider and Your search and reader, check your saved key, endpoint and model, then rerun. Review app.log for this step."
                if journey.step == "setup" and not shutil.which("gnome-keyring-daemon"):
                    message = "Your isolated keychain cannot save your model key. Install Ubuntu package gnome-keyring, unlock its secrets service, then open setup > Connect your AI service and rerun."
                if journey.step == "export":
                    message = "Your page export could not be verified. Open your Site screen, rebuild your landing page, then choose Export again."
            if journey.live and journey.step == "setup" and not shutil.which("gnome-keyring-daemon") and "did not finish" in message:
                message = "Your system keychain cannot save your model key. Install Ubuntu package gnome-keyring, then open setup > Connect your AI service and rerun."
            message = journey.runtime_failure() or message
            if journey.model:
                message = message.replace(journey.model["api_key"], "[redacted]")
                trace = trace.replace(journey.model["api_key"], "[redacted]")
            (output / "failure.log").write_text(trace)
            try:
                journey.capture()
            except (OSError, subprocess.SubprocessError):
                message += " Your screenshot could not be saved. Check Xvfb and Ubuntu package gir1.2-gtk-3.0, then rerun."
            save_result(output, journey.step, False, message)
            print(f"FAIL {journey.step}: {message}", file=sys.stderr)
            journey.failed = True
        finally:
            if not completed and not journey.failed:
                message = "Your journey stopped before finishing this step. Review scripts/desktop/e2e_linux.py and app.log, fix the reported error, then rerun."
                save_result(output, journey.step, False, message)
                print(f"FAIL {journey.step}: {message}", file=sys.stderr, flush=True)
            if recorder is not None:
                recorder.send_signal(signal.SIGINT)
                try:
                    recorder.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    recorder.kill()
                    recorder.wait()
            journey.stop()
            if keyring is not None and keyring.poll() is None:
                os.killpg(keyring.pid, signal.SIGTERM)
                try:
                    keyring.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(keyring.pid, signal.SIGKILL)
                    keyring.wait()
            for thread in log_threads:
                thread.join(timeout=10)
            try:
                network.close()
                save_result(output, "no hosted requests", True, "Your whole packaged process tree and window made no hosted requests.")
                print("PASS no hosted requests", flush=True)
            except RuntimeError as error:
                save_result(output, "no hosted requests", False, str(error))
                print("FAIL no hosted requests: " + str(error), flush=True)
    return summary(output)


if __name__ == "__main__":
    sys.exit(main())
