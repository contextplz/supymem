#!/usr/bin/env python3
"""Use your local app in a fresh, headless browser, without supplying credentials.

Run against a running console, or use --gate --env-file to start and stop an
isolated stack. Inputs come from --arguments-file or --idea and --answer.
Nothing is sent or published. Screenshots and redacted failures are saved under
artifacts/browser-journey/<run>/.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
SECRET_PATTERN = re.compile(r'''(?:bearer|basic)\s+\S+|(?:api[_ -]?key|password|token|secret|authorization)["']?\s*[=:]\s*["']?[^\s,;]+|(?:sk-|AIza)[\w-]+''', re.IGNORECASE)


class Skip(Exception):
    pass


def load_environment(path):
    if path is None:
        return {}
    if not path.is_file():
        raise ValueError("Choose your private model .env file with --env-file, then retry.")
    sys.path.insert(0, str(ROOT / "packages"))
    from coding_agents.env import load_dotenv_file

    return load_dotenv_file(path)


def provider_settings(values):
    key_names = {"openai": "OPENAI_API_KEY", "azure_openai": "AZURE_OPENAI_API_KEY", "anthropic": "ANTHROPIC_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
    provider = values.get("SUPYMEM_LLM_PROVIDER") or values.get("LLM_PROVIDER") or ""
    address = values.get("LLM_BASE_URL") or values.get("AZURE_OPENAI_ENDPOINT") or ""
    if not provider and address and (values.get("AZURE_API_KEY") or values.get("AZURE_OPENAI_API_KEY")):
        provider = "azure_openai"
    provider = provider or next((name for name, key in key_names.items() if values.get(key)), "")
    model = values.get("SUPYMEM_MODEL_CHAT") or values.get("LLM_MODEL") or values.get("AZURE_OPENAI_DEPLOYMENT") or ""
    key = values.get("LLM_API_KEY") or values.get(key_names.get(provider, "")) or (values.get("AZURE_API_KEY") if provider == "azure_openai" else "") or ""
    if provider not in {*key_names, "ollama", "lmstudio"} or not model or (provider in key_names and not key) or (provider in {"azure_openai", "ollama", "lmstudio"} and not address):
        raise Skip("Open Keys and connections > Your AI service, choose your provider, model, key and service address, then retry with --env-file.")
    return {"provider": provider, "model": model, "base_url": address, "api_key": key}


def arguments(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:3100")
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--arguments-file", type=Path)
    parser.add_argument("--idea")
    parser.add_argument("--answer")
    parser.add_argument("--name", default=f"Journey {uuid4().hex[:8]}")
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--gate", action="store_true")
    parser.add_argument("--log-dir", type=Path)
    args = parser.parse_args(argv)
    url = urlsplit(args.base_url)
    if url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"} or url.username or url.password or url.path not in {"", "/"} or url.query or url.fragment or not url.port:
        raise ValueError("Use your loopback console URL, for example http://127.0.0.1:3100, without /api or credentials, then retry.")
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("Choose a positive finite --timeout, then retry.")
    args.base_url = args.base_url.rstrip("/")
    fixture = args.arguments_file or next(iter(sorted((ROOT / "scripts/local_journeys").glob("*.json"))), None)
    supplied = json.loads(fixture.read_text()) if fixture else {}
    args.idea = args.idea or supplied.get("idea")
    args.answer = args.answer or next(iter(supplied.get("answers", [])), None)
    if not all(isinstance(value, str) and value.strip() for value in (args.idea, args.answer, args.name)) or len(args.name) > 48:
        raise ValueError("Provide --idea, --answer and a --name of at most 48 characters, or choose --arguments-file, then retry.")
    if args.gate and not args.env_file:
        raise ValueError("Choose your private model .env file with --env-file, then retry your gate.")
    return args


class Failures:
    def __init__(self, secrets=()):
        self.secrets = sorted(set(filter(None, secrets)), key=len, reverse=True)
        self.items = []
        self.cancelled_requests = []

    def safe(self, text):
        for secret in self.secrets:
            text = text.replace(secret, "[redacted]").replace(json.dumps(secret)[1:-1], "[redacted]")
        text = SECRET_PATTERN.sub("[redacted]", text)
        return " ".join(text.split())[:1000]

    def url(self, address):
        try:
            parsed = urlsplit(address)
            host = parsed.hostname or ""
            if ":" in host:
                host = f"[{host}]"
            if parsed.port:
                host += f":{parsed.port}"
            return self.safe(urlunsplit((parsed.scheme, host, parsed.path, "", "")))
        except ValueError:
            return "[invalid URL]"

    def response(self, response):
        if response.status >= 400:
            code = ""
            try:
                payload = response.json()
                error = payload.get("error", payload.get("detail", {})) if isinstance(payload, dict) else {}
                candidate = error.get("code", "") if isinstance(error, dict) else ""
                if isinstance(candidate, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,80}", candidate):
                    code = f" [{self.safe(candidate)}]"
            except Exception:
                pass
            self.items.append(f"network: {response.status} {response.request.method} {self.url(response.url)}{code}")

    def request_failed(self, request):
        record = f"network: {request.method} {self.url(request.url)} {self.safe(request.failure or 'request failed')}"
        if request.failure == "net::ERR_ABORTED":
            self.cancelled_requests.append(record)
        else:
            self.items.append(record)

    def console(self, message):
        if message.type == "error":
            text = re.sub(r"https?://[^\s]+", lambda match: self.url(match.group()), message.text)
            self.items.append("console: " + self.safe(text))

    def page_error(self, error):
        self.items.append("console: " + self.safe(str(error)))


def fresh_page(browser, failures):
    context = browser.new_context(color_scheme="light", viewport={"width": 1440, "height": 1000})
    page = context.new_page()
    page.on("response", failures.response)
    page.on("requestfailed", failures.request_failed)
    page.on("console", failures.console)
    page.on("pageerror", failures.page_error)
    return context, page


class BrowserJourney:
    def __init__(self, page, args, output, failures):
        self.page, self.args, self.output, self.failures = page, args, output, failures
        self.steps = []
        self.venture_id = ""
        self.model_ready = False

    def save_report(self):
        destination = self.output / "report.json"
        destination.write_text(json.dumps({"steps": self.steps, "failures": self.failures.items, "cancelled_requests": self.failures.cancelled_requests}, indent=2) + "\n")
        destination.chmod(0o600)

    def no_errors(self):
        notices = self.page.get_by_role("alert").all_text_contents()
        if any(notice.strip() for notice in notices):
            raise RuntimeError("Your app displays an error notice.")

    def wait(self, target):
        errors = self.page.get_by_role("alert").filter(has_text=re.compile(r"\S"))
        target.or_(errors).first.wait_for(state="visible", timeout=self.args.timeout * 1000)
        self.no_errors()
        if not target.first.is_visible():
            raise RuntimeError("Your requested screen did not render.")

    def refresh_until(self, target, button_name):
        refresh = self.page.get_by_role("button", name=button_name, exact=True)
        self.wait(refresh)
        deadline = time.monotonic() + self.args.timeout
        while not target.first.is_visible():
            self.no_errors()
            if time.monotonic() >= deadline:
                raise RuntimeError("Your requested results did not arrive.")
            if refresh.is_enabled():
                refresh.click()
            self.page.wait_for_timeout(1000)

    def step(self, label, action, next_step):
        state, reason = "PASS", "Your screen and requested work are available."
        try:
            action()
            self.no_errors()
        except Skip as error:
            state, reason = "SKIP", str(error)
        except Exception:
            state, reason = "FAIL", next_step
            notices = [notice for notice in self.page.get_by_role("alert").all_text_contents() if notice.strip()]
            if notices:
                reason = self.failures.safe(" ".join(notices)) + " " + next_step
        try:
            self.page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        screenshot = f"{len(self.steps) + 1:02}-{label.replace(' ', '-')}.png"
        try:
            self.page.screenshot(path=str(self.output / screenshot), full_page=True, timeout=15000)
            (self.output / screenshot).chmod(0o600)
        except Exception:
            state, reason = "FAIL", "Check your artifact folder permissions and Chromium installation, then retry this step."
        self.steps.append({"step": label, "state": state, "message": reason, "screenshot": screenshot})
        self.save_report()
        print(f"{state} {label}: {reason}", flush=True)
        return state == "PASS"

    def open_app(self):
        self.page.goto(self.args.base_url, wait_until="domcontentloaded")
        heading = self.page.get_by_role("heading", name=re.compile(r"^(Connect your AI service|Start with your idea|Which venture\?|What is your idea\?)$"))
        self.wait(heading.or_(self.page.get_by_role("dialog", name="Meet Charlie")))

    def setup(self, values):
        connect = self.page.get_by_role("button", name="Connect a model", exact=True)
        if connect.is_visible():
            connect.click()
        if self.page.get_by_role("heading", name="Connect your AI service", exact=True).is_visible():
            settings = provider_settings(values)
            self.page.get_by_label("AI service", exact=True).select_option(settings["provider"])
            self.page.get_by_label("Your model", exact=True).fill(settings["model"])
            if self.page.get_by_label("Your API key", exact=True).is_visible():
                self.page.get_by_label("Your API key", exact=True).fill(settings["api_key"])
            if self.page.get_by_label("Your service address", exact=True).is_visible():
                self.page.get_by_label("Your service address", exact=True).fill(settings["base_url"])
            self.page.get_by_role("button", name="Save and continue", exact=True).click()
            continuation = self.page.get_by_role("button", name="Continue to your workspace", exact=True)
            self.wait(continuation)
            continuation.click()
            self.wait(self.page.get_by_role("heading", name=re.compile(r"^(Start with your idea|What is your idea\?|Which venture\?)$")))
        if self.page.get_by_role("status").filter(has_text="sample data and answers").count():
            raise Skip("Open Keys and connections > Your AI service, save your live model and turn off sample mode, then retry.")
        self.model_ready = True

    def skip_setup(self):
        raise Skip("Open your local app successfully first, then connect your model in Keys and connections.")

    def venture(self):
        if not self.model_ready:
            raise Skip("Connect your live model in Keys and connections > Your AI service, then retry your idea.")
        self.page.goto(self.args.base_url + "/new?fresh=" + uuid4().hex, wait_until="domcontentloaded")
        idea = self.page.get_by_role("textbox", name="What is your idea?", exact=True)
        self.wait(idea)
        idea.fill(self.args.idea)
        self.page.get_by_role("button", name="Continue", exact=True).click()
        name = self.page.get_by_role("textbox", name="Your venture name", exact=True)
        skip = self.page.get_by_role("button", name="I'm not sure, continue with what I shared", exact=True)
        answered = False
        deadline = time.monotonic() + self.args.timeout
        while True:
            self.wait(name.or_(skip))
            if name.is_visible():
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Your idea questions did not finish.")
            if not answered:
                question = self.page.get_by_role("textbox").first
                question.fill(self.args.answer)
                self.page.get_by_role("button", name="Continue", exact=True).click()
                answered = True
            else:
                skip.click()
        if not answered:
            raise RuntimeError("Your idea did not offer a question to answer.")
        name.fill(self.args.name)
        self.page.get_by_role("button", name=re.compile(r"^Continue(?: to find customers)?$")).click()
        self.page.wait_for_url(re.compile(r"(?:/v/[^/?]+|[?&]venture=[^&]+)"), timeout=self.args.timeout * 1000)
        url = urlsplit(self.page.url)
        self.venture_id = parse_qs(url.query).get("venture", [""])[0] or url.path.split("/v/", 1)[-1].split("/")[0]
        self.wait(self.page.get_by_role("textbox", name=re.compile(r"^Message ")))

    def ask(self, text):
        if not self.venture_id:
            raise Skip("Create and confirm your idea first, then retry this request in your venture chat.")
        message = self.page.get_by_role("textbox", name=re.compile(r"^Message ")).first
        if not message.is_visible():
            opener = self.page.get_by_role("button", name=re.compile(r"^Open chat with ")).first
            if opener.is_visible():
                opener.click()
        self.wait(message)
        message.fill(text + ". Keep this work private. Do not send emails or publish anything.")
        with self.page.expect_response(lambda response: response.request.method == "POST" and f"/ventures/{self.venture_id}/" in urlsplit(response.url).path and "/chat" in urlsplit(response.url).path) as reply:
            message.press("Enter")
        reply.value.finished()
        if reply.value.status >= 400:
            raise RuntimeError("Your chat request failed.")
        self.no_errors()

    def research(self):
        self.ask("Research the market for my saved idea and save a sourced market report")
        self.page.goto(f"{self.args.base_url}/v/{self.venture_id}/research", wait_until="domcontentloaded")
        sources = self.page.get_by_role("complementary", name="Research sources and history", exact=True)
        self.refresh_until(sources, "Refresh research")
        addresses = {self.failures.url(link.get_attribute("href") or "") for link in sources.get_by_role("link").all()}
        if not any(address.startswith(("http://", "https://")) and urlsplit(address).hostname not in {"localhost", "127.0.0.1", "::1"} for address in addresses):
            raise RuntimeError("Your research has no linked sources.")

    def customers(self):
        self.ask("Find potential customers for my saved idea and save sourced leads for review")
        self.leads()
        self.refresh_until(self.page.get_by_role("list", name="Your leads", exact=True).get_by_role("listitem"), "Refresh leads")

    def landing(self):
        self.ask("Build a landing page for my saved idea and show me its private preview")
        self.page.goto(f"{self.args.base_url}/v/{self.venture_id}/site", wait_until="domcontentloaded")
        preview = self.page.get_by_title(re.compile(r"^(Full landing page preview|Landing page preview|Website preview)$")).first
        self.wait(preview)
        frame = preview.content_frame
        frame.get_by_role("heading").first.wait_for(state="visible", timeout=self.args.timeout * 1000)

    def keys(self):
        self.page.goto(self.args.base_url + "/settings/local", wait_until="domcontentloaded")
        self.wait(self.page.get_by_role("heading", name="Keys and connections", exact=True))
        self.wait(self.page.get_by_role("region", name="Your .env file", exact=True))

    def capabilities(self):
        self.page.goto(self.args.base_url + "/settings/local/capabilities", wait_until="domcontentloaded")
        self.wait(self.page.get_by_role("heading", name="Your capabilities", exact=True))
        self.wait(self.page.get_by_role("switch").first)

    def leads(self):
        if not self.venture_id:
            raise Skip("Create and confirm your venture first, then open Leads and retry.")
        self.page.goto(f"{self.args.base_url}/v/{self.venture_id}/leads", wait_until="domcontentloaded")
        self.wait(self.page.get_by_role("status").filter(has_text=re.compile(r"\d+ of \d+ leads")))

    def connections(self):
        self.page.goto(self.args.base_url + "/connections", wait_until="domcontentloaded")
        self.wait(self.page.get_by_role("heading", name="Your keys and connections", exact=True))
        self.wait(self.page.get_by_role("region", name="Your local connections", exact=True))


def artifact_directory():
    output = ROOT / "artifacts/browser-journey" / (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid4().hex[:8])
    output.mkdir(parents=True, mode=0o700)
    return output


def run_browser(args, values, output=None):
    output = output or artifact_directory()
    failures = Failures(value for name, value in values.items() if re.search(r"key|password|secret|token", name, re.IGNORECASE))
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        if not Path(playwright.chromium.executable_path).is_file():
            subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        browser = playwright.chromium.launch(headless=True)
        try:
            context, page = fresh_page(browser, failures)
            page.set_default_timeout(args.timeout * 1000)
            runner = BrowserJourney(page, args, output, failures)
            opened = runner.step("open app", runner.open_app, "Restart ./start --web, open your loopback console URL, then retry. Review the failed requests in report.json.")
            if opened:
                runner.step("connect model", lambda: runner.setup(values), "Open Keys and connections > Your AI service, check your model, key and address, then retry.")
            else:
                runner.step("connect model", runner.skip_setup, "Open your local app, then retry.")
            for label, action, next_step in (
                ("idea questions and confirmation", runner.venture, "Open your idea chat, answer one question, skip the remaining questions, enter your venture name and choose Continue, then retry."),
                ("market research", runner.research, "Open Keys and connections > Search, choose built-in search or save your own search key, then retry in Research."),
                ("find customers", runner.customers, "Open Capabilities, enable Find customers, check Search in Keys and connections, then retry in your chat."),
                ("landing page preview", runner.landing, "Open Capabilities, enable Landing pages, check your model in Keys and connections, then retry in Website."),
                ("Keys and connections", runner.keys, "Restart your local app, then reopen Keys and connections and retry."),
                ("Capabilities", runner.capabilities, "Restart your local app, then reopen Settings > Capabilities and retry."),
                ("Leads", runner.leads, "Open your venture, reopen Leads, then retry."),
                ("Connections", runner.connections, "Open Keys and connections, check your saved connections, then reopen Connections and retry."),
            ):
                runner.step(label, action, next_step)
            page.wait_for_timeout(250)
            for failure in failures.items:
                print(f"FAIL {failure}. Review report.json and the matching screen, then retry.", flush=True)
            runner.save_report()
            failed = bool(failures.items or any(step["state"] == "FAIL" for step in runner.steps))
            state = "FAIL" if failed else "SKIP" if any(step["state"] == "SKIP" for step in runner.steps) else "PASS"
            print(f"{state} browser journey: {sum(step['state'] == 'PASS' for step in runner.steps)} PASS, {sum(step['state'] == 'FAIL' for step in runner.steps) + len(failures.items)} FAIL, {sum(step['state'] == 'SKIP' for step in runner.steps)} SKIP. Screenshots: {output.relative_to(ROOT)}. Review your screenshots, then retry any failed step.", flush=True)
            return int(failed)
        finally:
            browser.close()


def stop_stack(stack):
    groups = {stack.pid}
    try:
        listing = subprocess.run(["ps", "-eo", "pid=,ppid=,pgid="], capture_output=True, text=True, check=True)
        rows = [tuple(map(int, row.split())) for row in listing.stdout.splitlines() if len(row.split()) == 3]
        owned = {stack.pid}
        while True:
            found = {pid for pid, parent, _group in rows if parent in owned} - owned
            if not found:
                break
            owned.update(found)
        groups.update(group for pid, _parent, group in rows if pid in owned and group != os.getpgrp())
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    for group in groups:
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            pass
    try:
        stack.wait(timeout=35)
    except subprocess.TimeoutExpired:
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        stack.wait()


def gate(args, values):
    sys.path.insert(0, str(ROOT / "scripts"))
    from local_value_journey import free_ports

    core, console, worker = free_ports()
    with tempfile.TemporaryDirectory(prefix="browser-journey-") as data:
        from coding_agents.env import CHILD_ENV_VARS

        inherited = load_environment(ROOT / ".env") if (ROOT / ".env").is_file() else {}
        environment = {**{name: value for name, value in os.environ.items() if name in CHILD_ENV_VARS},
                       **{name: "" for name in inherited}, **values}
        environment.update(SUPYMEM_MODE="local", APP_ENV="local", SUPYMEM_PYTHON=sys.executable,
                           PYTHONPATH=f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}",
                           COMPANY_OS_DATA_DIR=data, COMPANY_OS_DB_URL=f"sqlite:///{data}/local.db",
                           DATABASE_URL=f"sqlite:///{data}/local.db", DB_URL=f"sqlite:///{data}/local.db",
                           COMPANY_OS_WORKING_MEMORY_PATH=f"{data}/working_memory.sqlite3",
                           REDIS_URL="", RDS_PROXY_ENDPOINT="", CORE_PORT=str(core), CONSOLE_PORT=str(console),
                           WORKER_HEALTH_PORT=str(worker), CORE_URL=f"http://127.0.0.1:{core}",
                           CONSOLE_ORIGIN=f"http://127.0.0.1:{console}", LLM_OFFLINE="0", LLM_CASSETTE_MODE="off")
        args.base_url = f"http://127.0.0.1:{console}"
        output = artifact_directory()
        redactor = Failures(value for name, value in environment.items() if re.search(r"key|password|secret|token", name, re.IGNORECASE))

        def collect(stream):
            with stream, (output / "stack.log").open("x") as log:
                (output / "stack.log").chmod(0o600)
                for line in stream:
                    log.write(redactor.safe(line) + "\n")

        def interrupted(*_args):
            raise KeyboardInterrupt

        stack = subprocess.Popen(["bash", "scripts/run_local.sh", "--web"], cwd=ROOT, env=environment,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
        reader = threading.Thread(target=collect, args=(stack.stdout,), daemon=True)
        reader.start()
        previous = {signum: signal.signal(signum, interrupted) for signum in (signal.SIGINT, signal.SIGTERM)}
        try:
            deadline = time.monotonic() + args.timeout
            while stack.poll() is None and time.monotonic() < deadline:
                with socket.socket() as probe:
                    if probe.connect_ex(("127.0.0.1", console)) == 0:
                        return run_browser(args, values, output)
                time.sleep(0.25)
            print(f"FAIL local stack: Review {output.relative_to(ROOT)}/stack.log, run ./start --check and fix your prerequisites, then retry.", flush=True)
            return 1
        finally:
            stop_stack(stack)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
            reader.join(timeout=5)


def main(argv=None):
    try:
        args = arguments(argv)
        values = load_environment(args.env_file)
        return gate(args, values) if args.gate else run_browser(args, values)
    except KeyboardInterrupt:
        print("FAIL browser journey: Your run stopped. Restart your local app, then retry.")
        return 130
    except Exception:
        print("FAIL browser journey: Check --help, your private --env-file, loopback URL, Chromium installation and artifact folder permissions, then retry.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
