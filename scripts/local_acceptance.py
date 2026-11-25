#!/usr/bin/env python3
"""Exercise a disposable local founder journey using HTTP, never a browser."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import stat
import time
from pathlib import Path
from http.cookies import CookieError, SimpleCookie
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
from uuid import uuid4


class AcceptanceFailure(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class ArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "FAIL arguments: Check your arguments with --help and retry\n")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Client:
    def __init__(self, base_url: str, timeout: float, *, owner_secret_file: Path | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {"http", "https"}
                or parsed.hostname not in {"localhost", "127.0.0.1", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise AcceptanceFailure("Use a loopback URL without credentials or query parameters")
        if not math.isfinite(timeout) or timeout <= 0:
            raise AcceptanceFailure("Choose a positive finite timeout")
        self.owner_secret_file = owner_secret_file or Path(os.environ.get("SUPYMEM_LOCAL_PROXY_SECRET_FILE") or
            Path(os.environ.get("COMPANY_OS_DATA_DIR") or Path(__file__).resolve().parents[1] / "data") / "owner-session.secret")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._browser_headers = None
        self.headers = {"Accept": "application/json", "Content-Type": "application/json",
                        "X-Subject": os.environ.get("LOCAL_ACCEPTANCE_SUBJECT") or f"acceptance-{uuid4().hex}"}
        token = os.environ.get("LOCAL_ACCEPTANCE_TOKEN")
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.opener = build_opener(ProxyHandler({}), NoRedirect())

    def _console_session(self):
        origin = self.base_url[:-4]
        request = Request(origin + "/", headers={"Accept": "text/html", "Sec-Fetch-Site": "none",
                                                "Sec-Fetch-Dest": "document"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(4 * 1024 * 1024 + 1)
                if len(body) > 4 * 1024 * 1024 or response.headers.get_content_type() != "text/html":
                    raise ValueError
                cookies = SimpleCookie()
                for value in response.headers.get_all("Set-Cookie", []):
                    cookies.load(value)
                cookie = cookies.get("supymem_owner")
                script = re.search(r'<script\b[^>]*\bid="local-owner-session"[^>]*>(.*?)</script>',
                                   body.decode("utf-8"), re.DOTALL)
                proof = re.search(r'\bconst proof = "([0-9a-f]{64})";', script[1]) if script else None
                if (cookie is None or not re.fullmatch(r"[0-9a-f]{64}", cookie.value)
                        or not cookie["httponly"] or cookie["samesite"].lower() != "strict" or not proof):
                    raise ValueError
                return {"Cookie": f"supymem_owner={cookie.value}", "X-Local-Session-Proof": proof[1],
                        "Referer": origin + "/", "Origin": origin, "Sec-Fetch-Site": "same-origin"}
        except (HTTPError, URLError, OSError, ValueError, CookieError):
            raise AcceptanceFailure(f"Open your app at {origin} on this machine, then retry this script.") from None

    def request(self, method: str, path: str, payload=None, *, html: bool = False, download: bool = False):
        self.response_status = None
        data = None if payload is None else json.dumps(payload).encode()
        base_url = self.base_url[:-4] if self.base_url.endswith("/api") and path.startswith("/api/v1/") else self.base_url
        headers = dict(self.headers)
        if urlsplit(self.base_url).path == "/api":
            if self._browser_headers is None:
                self._browser_headers = self._console_session()
            headers.update(self._browser_headers)
        else:
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    descriptor = os.open(self.owner_secret_file, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                    break
                except FileNotFoundError:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise AcceptanceFailure(f"Your private owner session was not created in {self.owner_secret_file.parent}. Check src-tauri/src/local_stack.rs, restart your packaged app, then retry this script.") from None
                    time.sleep(min(0.1, remaining))
                except OSError:
                    raise AcceptanceFailure("Restart your desktop app to create your private owner session, then retry this script.") from None
            try:
                with os.fdopen(descriptor, "rb") as source:
                    metadata = os.fstat(source.fileno())
                    if not stat.S_ISREG(metadata.st_mode) or os.name != "nt" and (metadata.st_mode & 0o077 or metadata.st_uid != os.geteuid()):
                        raise ValueError
                    secret = source.read(129).strip().decode("ascii")
                if not re.fullmatch(r"[0-9a-f]{64}", secret):
                    raise ValueError
                headers["X-Local-Proxy-Secret"] = secret
            except (OSError, ValueError):
                raise AcceptanceFailure("Restart your desktop app to create your private owner session, then retry this script.") from None
        request = Request(base_url + path, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                self.response_status = getattr(response, "status", 200)
                limit = 20_000_000 if download else 4 * 1024 * 1024
                body = response.read(limit + 1)
                if len(body) > limit:
                    raise AcceptanceFailure("Your response is too large; check the local service")
                if download:
                    return body
                if html:
                    if response.headers.get_content_type() != "text/html":
                        raise AcceptanceFailure("Your export is not HTML; check the export endpoint")
                    return body.decode("utf-8")
                return json.loads(body)
        except HTTPError as exc:
            raise AcceptanceFailure(f"HTTP {exc.code}; check this endpoint and your local authentication", status=exc.code) from None
        except (URLError, OSError, ValueError):
            raise AcceptanceFailure("Your local service did not return a valid response; check it and retry") from None


def require(condition, reason: str):
    if not condition:
        raise AcceptanceFailure(reason)


class Journey:
    def __init__(self, client: Client, args):
        self.client = client
        self.args = args
        self.venture = ""
        self.workspace = ""
        self.session = ""
        self.experiment_ids = set()
        self.results_recorded = False
        self.last_reply = None

    def path(self, suffix: str):
        require(self.venture, "Your venture was not created; resolve that step and retry")
        return f"/ventures/{self.venture}{suffix}"

    def session_path(self, suffix: str = ""):
        require(self.session, "Your idea session is unavailable; resolve the question step and retry")
        return self.path(f"/idea/sessions/{self.session}{suffix}")

    def wait(self, fetch, ready, reason: str):
        deadline = time.monotonic() + self.args.timeout
        while True:
            result = fetch()
            if ready(result):
                return result
            if time.monotonic() >= deadline:
                raise AcceptanceFailure(reason)
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))

    def chat(self, text: str):
        reply = self.client.request("POST", self.path("/chat"),
                                    {"message": text, "idempotency_key": uuid4().hex})
        self.last_reply = reply
        require(isinstance(reply, dict), "Your reply is missing; check the saved conversation and retry")
        require(reply.get("kind") not in {"confirmation", "action_failed", "needs_you", "credit_limit", "usage_limit", "spend_limit", "provider_error"}
                and not reply.get("provider_error"),
                "Your requested work is blocked or asks for confirmation; resolve the reply and retry")
        require(not any(action.get("confirmation") for action in reply.get("actions", [])),
                "Your requested work asks for confirmation; check the saved request before retrying")
        copy = reply.get("body_md") or ""
        require(isinstance(copy, str) and copy.strip(), "Your reply is empty; check your saved conversation and retry")
        require(not re.search(r"\b\d+(?:[.,]\d+)?\s*credits?\b|\bcredits?\s*[:=]\s*\d", copy, re.IGNORECASE),
                "Your reply shows usage amounts; remove them from founder copy and retry")
        return reply

    def setup(self):
        status = self.client.request("GET", "/setup/status")
        known = {"local", "hosted", "cloud", "offline", "stub", "replay", "sample", "live", "log", "smtp", "ses", "resend"}
        values = ", ".join(f"{key}={status.get(key) if status.get(key) in known else 'unrecognized'}"
                           for key in ("mode", "llm_mode", "email_mode"))
        require(status.get("mode") == "local", f"Your setup reports {values}; set SUPYMEM_MODE=local on your server and restart, then retry")
        if self.args.stub:
            require(status.get("llm_mode") in {"offline", "stub", "replay", "sample"},
                    f"Your setup reports {values}; set LLM_OFFLINE=1 or LLM_CASSETTE_MODE=replay on your server and restart, then retry")
            require(status.get("email_mode") == "log",
                    f"Your setup reports {values}; set EMAIL_PROVIDER=log on your server and restart, then retry")
        return "Your local setup status is available"

    def create(self):
        venture = self.client.request("POST", "/ventures", {"raw_idea": self.args.idea, "start": False})
        require(venture.get("id") and venture.get("workspace_id") and venture.get("raw_idea") == self.args.idea,
                "Your rough idea was not saved; check venture creation and retry")
        self.venture = quote(venture["id"], safe="")
        self.workspace = quote(venture["workspace_id"], safe="")
        self.client.headers["X-Workspace-Id"] = venture["workspace_id"]
        return "Your rough idea is saved without starting unrequested research"

    def answer(self):
        session = self.client.request("POST", self.path("/idea/sessions"), {})
        require(session.get("id"), "Your idea session is missing; check clarification and retry")
        self.session = quote(session["id"], safe="")
        def pending(data):
            turns = data.get("turns", [])
            last_founder = max((index for index, turn in enumerate(turns) if turn.get("role") == "founder"), default=-1)
            return next((turn for turn in reversed(turns[last_founder + 1:])
                         if turn.get("role") == "agent" and turn.get("kind") == "question"), None)
        self.wait(lambda: self.client.request("GET", self.session_path()), pending,
                  "Your first question is unavailable; check your saved idea and local service, then retry")
        self.client.request("POST", self.session_path("/messages"), {"text": self.args.answer})
        self.wait(lambda: self.client.request("GET", self.session_path()),
                  lambda data: any(turn.get("role") == "founder" and turn.get("text") == self.args.answer
                                   for turn in data.get("turns", [])),
                  "Your answer was not saved; check the idea session and retry")
        return "Your answer to the first question is saved"

    def skip(self):
        self.client.request("POST", self.session_path("/messages"), {"text": "Skip the remaining questions"})
        self.wait(lambda: self.client.request("GET", self.session_path()),
                  lambda data: data.get("state") == "reviewing",
                  "Your remaining questions were not skipped; check your saved idea and retry")
        return "Your remaining questions are skipped"

    def confirm(self):
        session = self.client.request("GET", self.session_path())
        revision = session.get("current_revision") or {}
        require(revision.get("id"), "Your idea revision is missing; review your saved idea and retry")
        self.client.request("POST", self.session_path("/confirm"),
                            {"revision_id": revision["id"], "start_research": False})
        self.wait(lambda: self.client.request("GET", self.session_path()),
                  lambda data: data.get("state") == "confirmed",
                  "Your idea is not confirmed; review its saved status and retry")
        return "Your idea is confirmed without starting unrequested research"

    def contacts(self):
        result = self.client.request("GET", self.path("/gtm/contacts"))
        return result.get("items", []) if isinstance(result, dict) else result

    def customers(self):
        self.chat("Find potential customers for my saved idea")
        self.wait(self.contacts, lambda items: bool(items),
                  "Your customer search saved no contacts; check your local search service and retry")
        return "Your customer search saved contacts"

    def experiments(self):
        return self.client.request("GET", self.path("/experiments"))["items"]

    def design(self):
        before = {item["id"] for item in self.experiments()}
        self.chat("Design a customer interview experiment for my saved idea and save its hypothesis, action and success measure")
        items = self.wait(self.experiments,
                          lambda rows: any(item["id"] not in before and all(item.get(key) for key in ("hypothesis", "action", "measure")) for item in rows),
                          "Your experiment was not saved with a success measure; check your experiment list and retry")
        self.experiment_ids = {item["id"] for item in items if item["id"] not in before}
        return "Your experiment and success measure are saved"

    def invitations(self):
        email = f"acceptance-{uuid4().hex}@example.invalid" if self.args.stub else self.args.contact_email
        require(email and (self.args.stub or self.args.contact_permission),
                "Choose a contact you have permission to email with --contact-email and --contact-permission, then retry")
        status_path = "/api/v1" + self.path("/gtm/journey")
        before = self.client.request("GET", status_path)
        if self.args.stub and not before.get("sender_postal_address"):
            self.client.request("PATCH", status_path + "/settings", {"sender_postal_address": "Synthetic test address, not for sending"})
        preview = self.client.request("POST", self.path("/gtm/contacts/import/preview"),
            {"text": f"email,country\n{email},US" if self.args.stub else email,
             "format": "csv" if self.args.stub else "paste"})
        require(preview.get("preview_id"), "Your contact preview is missing; check your permitted contact and retry")
        imported = self.client.request("POST", self.path("/gtm/contacts/import"),
            {"preview_id": preview["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"})
        require(imported.get("contact_ids"), "Your permitted contact was not imported; check the preview and retry")
        self.chat("Prepare email invitations for my saved customer interview experiment. Save drafts only. Do not send anything")
        contacts = self.wait(self.contacts, lambda items: any(item.get("draft") for item in items),
                             "Your invitations were not saved; check eligible contacts and your sender settings, then retry")
        drafts = [item["draft"] for item in contacts if item.get("draft")]
        after = self.client.request("GET", status_path)
        require(after["results"]["sent"] == before["results"]["sent"],
                "Your send count changed while preparing drafts; review your outreach records before retrying")
        postal = after.get("sender_postal_address")
        require(postal, "Your sender address is missing; save your business mailing address and retry")
        for draft in drafts:
            body = draft.get("body", "")
            require("Your email address is listed publicly on " in body or "I have your address from my own contacts." in body,
                    "Your invitation has no source disclosure; fix its compliance copy and retry")
            require(re.search(r"opt.out|unsubscribe|\bSTOP\b|reply [\"']no thanks[\"']", body, re.IGNORECASE),
                    "Your invitation has no opt-out line; fix its compliance copy and retry")
            require(postal in body, "Your invitation has no sender address; fix its footer and retry")
            require(draft.get("state") in {"draft", "manual"} and not draft.get("sent_at"),
                    "Your invitation is not an unsent draft; review your outreach records before retrying")
        return "Your invitations are saved with disclosure and opt-out; no send was requested"

    def results(self):
        require(self.experiment_ids, "Your experiment was not saved; resolve experiment design and retry")
        self.chat("Results: " + self.args.results)
        self.wait(self.experiments,
                  lambda items: any(item["id"] in self.experiment_ids and item.get("result_text")
                                    and item.get("state") == "concluded" for item in items),
                  "Your experiment results were not recorded; check the saved experiment and retry")
        self.results_recorded = True
        return "Your reported experiment results are saved"

    def next_step(self):
        self.chat("What should I do next based on my recorded experiment results?")
        require(self.results_recorded, "Your experiment results are not saved; resolve that step before asking what next")
        return "Your next-step reply is available"

    def page(self):
        require(self.workspace, "Your venture was not created; resolve venture creation and retry")
        try:
            return self.client.request("GET", f"/workspaces/{self.workspace}/site/export?venture_id={self.venture}", html=True)
        except AcceptanceFailure as exc:
            if exc.status != 404:
                raise
            return ""

    def export(self):
        html = self.page()
        if not html:
            self.chat("Build a landing page for my saved idea. Keep it private and do not send anything")
            html = self.wait(self.page, bool, "Your page is not built yet. Open Settings > Keys and connections > Coding tools to connect your coding tool, then open Website to check the build and retry your export.")
        return self.save_export(html)

    def save_export(self, html):
        require(re.search(r"<html[\s>]", html, re.IGNORECASE), "Your export is not a complete HTML page; check the export endpoint and retry")
        try:
            with self.args.output.open("x", encoding="utf-8") as output:
                output.write(html)
        except OSError:
            raise AcceptanceFailure("Your export could not be saved; choose a new writable output path and retry") from None
        return "Your landing page HTML is exported to your chosen file"


STEPS = (("first-run setup status", "setup"), ("create venture", "create"),
         ("answer a question", "answer"), ("skip remaining questions", "skip"),
         ("confirm idea", "confirm"), ("ask for customers", "customers"),
         ("design experiment", "design"), ("prepare invitations", "invitations"),
         ("record results", "results"), ("ask what next", "next_step"),
         ("export landing page", "export"))


def main(argv=None):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="Your local API URL; for Docker use http://127.0.0.1:3100/api")
    parser.add_argument("--stub", action="store_true", help="Require replay/offline AI and unsent email in your setup status")
    parser.add_argument("--idea", required=True, help="Your rough idea for a disposable venture")
    parser.add_argument("--answer", required=True, help="Your answer to the first question")
    parser.add_argument("--results", required=True, help="Your test result report, never inferred by this script")
    parser.add_argument("--contact-email", help="An address whose owner gave you permission to email them")
    parser.add_argument("--contact-permission", action="store_true", help="Attest that this contact consented to email from you")
    parser.add_argument("--output", type=Path, default=Path("acceptance-landing.html"))
    parser.add_argument("--timeout", type=float, default=30, help="Seconds per HTTP request and evidence wait")
    args = parser.parse_args(argv)
    failures = 0
    try:
        require(os.environ.get("SUPYMEM_MODE") == "local", "Set SUPYMEM_MODE=local in your caller terminal, then retry")
        if args.stub:
            require(os.environ.get("LLM_OFFLINE", "").strip().lower() in {"1", "true", "yes"}
                    or os.environ.get("LLM_CASSETTE_MODE") == "replay",
                    "Set LLM_OFFLINE=1 or LLM_CASSETTE_MODE=replay in your caller terminal, then retry")
            require(os.environ.get("EMAIL_PROVIDER", "log") == "log",
                    "Set EMAIL_PROVIDER=log in your caller terminal, then retry")
        client = Client(args.base_url, args.timeout)
        journey = Journey(client, args)
        reason = journey.setup()
    except (AcceptanceFailure, KeyError, TypeError, AttributeError, ValueError) as exc:
        reason = str(exc) if isinstance(exc, AcceptanceFailure) else "Your setup response has missing or invalid fields; check your local service and retry"
        print(f"FAIL {STEPS[0][0]}: {reason}")
        for label, _ in STEPS[1:]:
            print(f"SKIP {label}: Your setup failed; fix the setting above and retry")
        return 1
    print(f"PASS {STEPS[0][0]}: {reason}; continue to your next step")
    for label, method in STEPS[1:]:
        try:
            reason = getattr(journey, method)()
        except AcceptanceFailure as exc:
            failures += 1
            print(f"FAIL {label}: {exc}")
        except (KeyError, TypeError, AttributeError, ValueError):
            failures += 1
            print(f"FAIL {label}: Your response has missing or invalid fields; check this endpoint and retry")
        else:
            print(f"PASS {label}: {reason}; continue to your next step")
    return int(failures > 0)


if __name__ == "__main__":
    raise SystemExit(main())
