#!/usr/bin/env python3
"""Check real local value through your console proxy, without a browser.

Use --arguments-file with your journey JSON. For a test-only coding tool, run
SUPYMEM_TEST_FAKE_CODING_TOOL=1 python scripts/local_value_journey.py --gate --env-file <private-env>
from a source checkout. Only scenarios with "fake_coding_tool": true check
builds in that isolated gate. Nothing is sent or published by default.
For the source-only CI gate, use the same opt-in with --offline and no .env.
"""

from __future__ import annotations

import ast
import json
import math
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote, urlsplit, urlunsplit
from uuid import uuid4

from local_acceptance import AcceptanceFailure, ArgumentParser, Client, Journey, require

SECRET_PATTERN = re.compile(r'''(?:bearer|basic)\s+\S+|(?:api[_ -]?key|password|token|secret|authorization)["']?\s*[=:]\s*["']?[^\s,;]+|(?:sk-|AIza)[\w-]+''', re.IGNORECASE)
FAKE_GATE_STEP = "Set SUPYMEM_TEST_FAKE_CODING_TOOL=1 and rerun this script with --gate from your source checkout."
OFFLINE_GATE_STEP = "From your source checkout, set SUPYMEM_TEST_FAKE_CODING_TOOL=1 and rerun with --offline and no --env-file."
CODING_TOOLS_STEP = "Open Settings > Keys and connections > Coding tools, connect Codex and choose Allow Charlie, then retry."
PAGE_BUILD_STEP = "Open Website > Build activity, review your page checks, then retry your build."
PRODUCT_BUILD_STEP = "Open Build activity, review your build checks, then retry your build."


class Skip(AcceptanceFailure):
    pass


def source_urls(sources):
    urls = set()
    for source in sources or []:
        value = source.get("url") if isinstance(source, dict) else source
        if not isinstance(value, str):
            continue
        try:
            parsed = urlsplit(value)
            if parsed.scheme in {"http", "https"} and parsed.hostname and not parsed.username and not parsed.password:
                urls.add(urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path.rstrip("/"), parsed.query, "")))
        except ValueError:
            continue
    return urls


def safe_reply(reply):
    text = (reply.get("body_md") or reply.get("message") or "") if isinstance(reply, dict) else ""
    if not isinstance(text, str):
        return ""
    text = SECRET_PATTERN.sub("[redacted]", text)
    text = re.sub(r"https?://\S+", "[link]", text)
    return " ".join(text.split())[:300]


class ValueJourney(Journey):
    def __init__(self, client, args):
        super().__init__(client, args)
        self.capabilities = {}
        self.imported_id = ""
        self.self_draft = None
        self.fake_connected = False
        self.page_job_id = ""
        self.product_order = ""

    def chat(self, text):
        return super().chat(text + ". Keep this work private. Do not send emails or publish anything.")

    def setup(self):
        super().setup()
        status = self.client.request("GET", "/setup/status")
        require(status.get("llm_mode") == "live",
                "Your model is not live. Open Keys and connections, connect your model provider, turn off sample mode, then retry.")
        return "Your local setup uses a live model"

    def catalog(self):
        result = self.client.request("GET", "/local/settings/catalog")
        require(isinstance(result, (dict, list)) and bool(result),
                "Your keys catalog is empty. Open Keys and connections, then restart your app and retry.")
        return "Your Keys and connections catalog is reachable"

    def capability_list(self):
        result = self.client.request("GET", "/local/capabilities")
        items = result.get("items", result.get("capabilities", [])) if isinstance(result, dict) else result
        require(isinstance(items, list) and bool(items) and all(
            isinstance(item, dict) and item.get("name") and item.get("title")
            and item.get("state") in {"ready", "needs_setup", "off", "not_in_this_version"}
            and isinstance(item.get("missing"), list) and isinstance(item.get("next_step"), str) for item in items),
            "Your feature list is invalid. Open Settings, check your feature switches, then restart your app and retry.")
        self.capabilities = {item["name"]: item for item in items}
        return "Your feature readiness and setup steps are available"

    def available(self, name, next_step):
        item = self.capabilities.get(name)
        require(not item or item["state"] == "ready", item.get("next_step") or next_step if item else next_step)

    def research(self):
        self.available("research", "Turn on Research in Settings. In Keys and connections, choose built-in search or save your search key, then retry.")
        self.chat("Research the market for my saved idea and save a sourced market report")
        self.wait(lambda: self.client.request("GET", self.path("/research/dossier")),
                  lambda data: data.get("available") and len(source_urls(data.get("sources"))) >= 3
                  and (not getattr(self.args, "offline_child", False)
                       or (data.get("summary") or {}).get("review_kind") == "model_assessment"),
                  "Your sourced market report did not finish. Open Research, check your search provider in Keys and connections, then retry.")
        return "Your market report is saved with at least three distinct source URLs"

    def competitors(self):
        self.chat("Find competitors for my saved idea with links to their products")
        self.wait(lambda: self.client.request("GET", self.path("/research/dossier")),
                  lambda data: any(isinstance(item, dict) and item.get("name")
                                   and source_urls([item.get("url") or item.get("source_url")])
                                   for item in data.get("competitors", [])),
                  "Your competitor comparison has no linked competitor. Open Research and check your search provider in Keys and connections, then retry.")
        return "Your competitor comparison includes a linked alternative"

    def customers(self):
        self.available("find_customers", "Turn on Find customers in Settings, then check Search in Keys and connections and retry.")
        self.chat("Find potential customers for my saved idea and save their contacts with public source URLs")
        if getattr(self.args, "offline_child", False):
            next_step = "Your customer search did not finish. Open Customers, check Search in Keys and connections, then retry."
            result = self.wait(self.outreach_status, lambda status: status.get("job_status") == "succeeded"
                               or status.get("state") == "failed", next_step)
            require(result.get("job_status") == "succeeded", next_step)
        self.wait(self.contacts, lambda items: len({item.get("contact_id") for item in items
                  if item.get("contact_id") and source_urls([item.get("source_url")])}) >= 3,
                  "Your search has fewer than three saved, sourced contacts. Open Customers, check Search in Keys and connections, then retry.")
        return "Your search saved at least three contacts with source URLs"

    def mailbox(self):
        return self.client.request("GET", "/local/settings/smtp")

    def import_contact(self):
        email = f"acceptance-{uuid4().hex}@example.invalid"
        if self.args.send_to_self:
            saved = self.mailbox()
            values = saved["values"]
            require(self.args.send_to_self == values.get("user"),
                    "Your self-test address must exactly match SMTP mailbox user. Open Keys and connections > Email and use that saved address with --send-to-self.")
            email = self.args.send_to_self
        preview = self.client.request("POST", self.path("/gtm/contacts/import/preview"),
                                      {"text": f"email,country\n{email},US", "format": "csv"})
        require(preview.get("preview_id"), "Your import preview is missing. Open Customers > Import, check your CSV preview, then retry.")
        imported = self.client.request("POST", self.path("/gtm/contacts/import"),
            {"preview_id": preview["preview_id"], "lawful_basis_attested": True, "lawful_basis": "consent"})
        require(len(imported.get("contact_ids", [])) == 1,
                "Your import did not save one contact. Open Customers > Import, review permission and retry.")
        self.imported_id = imported["contact_ids"][0]
        self.wait(self.contacts, lambda items: any(item.get("contact_id") == self.imported_id for item in items),
                  "Your imported contact is not saved. Open Customers > Import and retry the saved preview.")
        return "Your consented self-test or synthetic contact is saved"

    def scan(self):
        self.available("research", "Turn on Research in Settings > Capabilities, check Search in Settings > Keys and connections, then retry.")
        spend = self.client.request("GET", "/local/settings/spend")["values"]
        if spend.get("monthly_spend_cap") is None:
            self.client.request("PUT", "/local/settings/spend", {"monthly_spend_cap": "5.00"})
        self.client.request("PUT", "/local/capabilities/run_for_me_experiments", {"on": True})
        before = {item["id"] for item in self.client.request("GET", self.path("/chat"))["items"]}
        self.chat("Run for me one public-source demand scan for my saved idea and save the findings")
        self.wait(lambda: self.client.request("GET", self.path("/chat"))["items"],
                  lambda items: any(item["id"] not in before and item.get("kind") == "experiment_result"
                  and item.get("body_md") and source_urls(item.get("citations")) for item in items),
                  "Your scan has no completed sourced findings. Open Experiments, check Search in Keys and connections, then retry the scan.")
        return "Your requested scan completed with saved, sourced findings"

    def outreach_status(self):
        return self.client.request("GET", "/api/v1" + self.path("/gtm/journey"))

    def results(self):
        require(self.experiment_ids, "Your experiment was not saved. Open Experiments, design your test, then retry.")
        self.chat("Results: " + self.args.results)
        self.wait(self.experiments,
                  lambda items: any(item["id"] in self.experiment_ids and item.get("result_text")
                                    and item.get("ran") is True and item.get("evidence_type") == "founder_reported"
                                    and item.get("state") in {"running", "concluded"} for item in items),
                  "Your experiment results were not recorded. Open Experiments, check your saved observations, then retry.")
        self.results_recorded = True
        return "Your reported experiment results are saved"

    def invitations(self):
        require(self.imported_id, "Your safe test contact is missing. Finish Customers > Import before preparing invitations.")
        before = self.outreach_status()
        self.chat("Prepare email invitations for my saved customer interview experiment. Include my imported contact. Save drafts only")
        contacts = self.wait(self.contacts, lambda items: any(item.get("contact_id") == self.imported_id
                             and item.get("draft") for item in items),
                             "Your safe contact has no invitation draft. Open Customers, then save your sender postal address in Keys and connections > Email and retry.")
        after = self.outreach_status()
        require(after["results"]["sent"] == before["results"]["sent"],
                "Your send count changed without a send request. Turn off Send email in Settings and inspect Customers > Sent before retrying.")
        postal = after.get("sender_postal_address")
        require(postal, "Your sender postal address is missing. Save it in Keys and connections > Email, then retry.")
        drafts = [item["draft"] for item in contacts if item.get("draft")]
        for draft in drafts:
            body = draft.get("body", "")
            require("Your email address is listed publicly on " in body or "I have your address from my own contacts." in body,
                    "Your invitation lacks source disclosure. Open Customers, restore the public source or founder-import disclosure, then retry.")
            require(re.search(r"opt.out|unsubscribe|\bSTOP\b|reply [\"']no thanks[\"']", body, re.IGNORECASE),
                    "Your invitation lacks opt-out copy. Open Customers, restore the opt-out line, then retry.")
            require(postal in body, "Your invitation lacks your mailing address. Save your postal address in Keys and connections > Email, then rebuild the draft.")
            require(draft.get("state") in {"draft", "manual"} and not draft.get("sent_at"),
                    "Your invitation is not an unsent draft. Turn off Send email in Settings and inspect Customers > Sent before retrying.")
        self.self_draft = next(item["draft"] for item in contacts if item.get("contact_id") == self.imported_id)
        return "Your unsent invitation drafts keep disclosure, opt-out and your sender address"

    def send(self):
        if not self.args.send_to_self:
            raise Skip("You did not request sending. To test it, save SMTP in Keys and connections > Email, then use --send-to-self with your saved mailbox user.")
        saved = self.mailbox()
        values = saved["values"]
        if not all(values.get(field) for field in ("host", "port", "user", "postal_address")) or not saved.get("secrets_set", {}).get("password"):
            raise Skip("Your SMTP settings are incomplete. Save host, port, mailbox user, app password and postal address in Keys and connections > Email, then retry.")
        self.available("send_email", "Turn on Send email in Settings, then save SMTP in Keys and connections > Email and retry.")
        require(self.args.send_to_self == values["user"],
                "Use exactly your saved SMTP mailbox user with --send-to-self. Check Keys and connections > Email, then retry.")
        self.connection_test("smtp", "Your SMTP connection failed. Open Keys and connections > Email, check your host, port, mailbox user and app password, then test again.")
        status = self.outreach_status()
        draft = next((item for item in status.get("drafts", []) if item.get("contact_id") == self.imported_id
                      and item.get("doc_id") == (self.self_draft or {}).get("doc_id")), {})
        require(draft.get("to") == values["user"] and draft.get("contact_id") == self.imported_id,
                "Your draft does not address your own mailbox. Open Customers, prepare your imported self-test invitation, then retry.")
        draft_id = draft.get("doc_id") or draft.get("id")
        require(draft_id and draft.get("revision") and draft.get("batch_id"),
                "Your self-test draft has no saved revision or batch. Open Customers, rebuild that invitation and retry.")
        before = status["results"]["sent"]
        self.client.request("PATCH", "/api/v1" + self.path("/gtm/journey/drafts/" + quote(draft_id, safe="")),
                            {"expected_revision": draft["revision"], "decision": "approved"})
        status = self.outreach_status()
        batch = next((item for item in status.get("batches", []) if item.get("batch_id") == draft["batch_id"]), {})
        require(batch.get("draft_ids") == [draft_id] and batch.get("approval_digest") and 1 <= batch.get("cap", 0) <= 5,
                "Your send batch is not restricted to your self-test draft. Open Customers, leave other invitations unapproved, then retry.")
        require(self.mailbox()["values"].get("user") == draft["to"],
                "Your mailbox changed after you drafted the self-test email. "
                "Open Keys and connections > Email and retry with your current mailbox address after --send-to-self.")
        result = self.client.request("POST", "/api/v1" + self.path("/gtm/journey/send"),
                                     {key: batch[key] for key in ("batch_id", "draft_ids", "approval_digest", "cap")} | {"self_only": True})
        require((result.get("receipt") or {}).get("kind") != "needs_you",
                "Your self-test email was blocked. Open Keys and connections > Email, verify the connection and use your current mailbox address with --send-to-self.")
        self.wait(self.outreach_status, lambda data: data["results"]["sent"] == before + 1,
                  "Your self-test send is not recorded. Open Customers > Sent and check your SMTP app password in Keys and connections > Email before retrying.")
        return "Exactly one invitation to your configured mailbox is recorded as sent"

    def replies(self):
        saved = self.mailbox()
        if not saved["values"].get("imap_host") or not saved.get("secrets_set", {}).get("password"):
            raise Skip("Your reply connection is incomplete. Save IMAP host, port and app password in Keys and connections > Email, then retry.")
        self.available("read_replies", "Turn on Read replies in Settings, then save IMAP in Keys and connections > Email and retry.")
        self.connection_test("imap", "Your IMAP connection failed. Open Keys and connections > Email, check your IMAP host, port and app password, then test again.")
        super().chat("Check replies")
        status = self.outreach_status()
        require(isinstance(status.get("results", {}).get("replies"), list),
                "Your reply check has no saved reply list. Open Customers > Replies, check IMAP in Keys and connections > Email, then retry.")
        return "Your reply check returned a saved reply list; an empty inbox is allowed"

    def connection_test(self, section, next_step):
        try:
            result = self.client.request("POST", f"/local/settings/{section}/test", {})
        except AcceptanceFailure as error:
            if error.status == 404:
                raise Skip("Your connection test route returned 404. Update your app, open Keys and connections > Email and test the connection, then retry.") from None
            raise
        require(result.get("ok") is True, next_step)

    def export(self):
        if getattr(self.args, "gate_child", False) and not self.args.fake_coding_tool:
            raise Skip('This gate scenario did not request a build. Set "fake_coding_tool": true in your journey JSON to check builds.')
        if getattr(self.args, "fake_coding_tool", False) and not self.args.gate_child:
            raise Skip(FAKE_GATE_STEP)
        self.available("landing_pages", "Turn on Landing pages in Settings > Features, then connect your coding tool in Settings > Keys and connections > Coding tools and retry.")
        self.chat("Build a landing page for my saved idea")
        html = self.wait(self.page, bool, "Your page is not built yet. Open Settings > Keys and connections > Coding tools to connect your coding tool, then open Website to check the build and retry your export.")
        return self.save_export(html)

    def connect(self, binary=None):
        approve_fake_coding_tool(self.client, "codex", binary or Path.home() / "bin/codex")
        self.fake_connected = True
        return "You allowed the isolated Codex test tool in Coding tools"

    def build_page(self):
        require(self.fake_connected, CODING_TOOLS_STEP)
        self.available("landing_pages", "Turn on Landing pages in Settings > Features, then retry your build.")
        reply = super().chat("Build my landing page with Codex")
        self.page_job_id = action_id(reply, "job_id", PAGE_BUILD_STEP)
        return "Your private landing page request is saved"

    def preview_export(self):
        require(self.page_job_id, PAGE_BUILD_STEP)
        job = self.path(f"/pagebuilder/jobs/{self.page_job_id}")
        status = self.wait(lambda: self.client.request("GET", job),
                           lambda data: data.get("state") in {"ready", "failed"}, PAGE_BUILD_STEP)
        require(status["state"] == "ready", status.get("error_reason") or PAGE_BUILD_STEP)
        self.checks_passed(status.get("order_id"), PAGE_BUILD_STEP)
        preview = self.client.request("GET", job + "/preview", html=True)
        require(re.search(r"<html[\s>]", preview, re.IGNORECASE), PAGE_BUILD_STEP)
        html = self.client.request("GET", self.path(f"/site/local/export.html?job_id={self.page_job_id}"), html=True)
        return self.save_export(html)

    def start_product(self):
        require(self.fake_connected, CODING_TOOLS_STEP)
        venture = self.client.request("GET", self.path(""))
        name = venture.get("name")
        require(isinstance(name, str) and name.strip(), "Open your venture, check its saved name, then retry your build.")
        reply = super().chat(f"Build the first version of {name} with Codex")
        if reply.get("kind") != "build_tool_started":
            active = reply.get("kind") == "navigation" and any(
                re.fullmatch(r"/v/[^/?#]+/device-work-orders/[^/?#]+", action.get("href") or "")
                for action in reply.get("actions", []))
            raise AcceptanceFailure(
                "You already have a build on this computer. Open Build activity, finish or stop it, then retry."
                if active else PRODUCT_BUILD_STEP)
        order = action_id(reply, "order_id", PRODUCT_BUILD_STEP)
        native = f"/native/device-work-orders/{self.venture}/{order}"
        started = self.client.request("POST", native + "/start")
        require(started.get("order_id") == order, PRODUCT_BUILD_STEP)
        self.product_order = native
        return "Codex started your product build"

    def check_product(self):
        require(self.product_order, PRODUCT_BUILD_STEP)
        terminal = {"stopped", "reconciliation_required"}
        plan = self.wait(lambda: self.client.request("GET", self.product_order + "/status"),
                         lambda data: data.get("next_order_id") or data.get("state") == "reconciliation_required"
                         or (data.get("state") == "stopped" and data.get("task_verified") is False), PRODUCT_BUILD_STEP)
        order_id = plan.get("next_order_id")
        require(isinstance(order_id, str) and order_id, PRODUCT_BUILD_STEP)
        order = quote(order_id, safe="")
        native = f"/native/device-work-orders/{self.venture}/{order}"
        result = self.wait(lambda: self.client.request("GET", native + "/status"),
                           lambda data: data.get("state") in terminal, PRODUCT_BUILD_STEP)
        require(result.get("state") == "stopped" and result.get("task_verified") is True, PRODUCT_BUILD_STEP)
        self.checks_passed(order_id, PRODUCT_BUILD_STEP)
        return "Your product build passed its checks"

    def checks_passed(self, order_id, next_step):
        require(isinstance(order_id, str) and order_id, next_step)
        checks = self.client.request("GET", self.path(f"/device-work-orders/{quote(order_id, safe='')}/iterations"))
        require(checks.get("iterations") and checks["iterations"][-1].get("outcome") == "pass", next_step)

    def publish(self):
        if not self.args.publish:
            raise Skip("You did not request publication. To publish, save hosting in Keys and connections > Publishing, then retry with --publish.")
        self.available("publish_pages", "Turn on Publish pages in Settings, then save your hosting provider and credentials in Keys and connections > Publishing and retry.")
        super().chat("Publish my saved landing page using my configured local hosting provider. Do not send emails.")
        self.wait(lambda: self.client.request("GET", self.path("/site/local")),
                  lambda data: bool(source_urls([(data.get("publication") or {}).get("url")])),
                  "Your page has no saved live URL. Open Website, check your provider, project and credentials in Keys and connections > Publishing, then retry.")
        return "Your requested publication has a saved live URL"


STEPS = (("live setup status", "setup"), ("keys catalog", "catalog"), ("capabilities", "capability_list"),
         ("create venture", "create"), ("answer idea question", "answer"), ("finish questions", "skip"),
         ("confirm idea", "confirm"), ("market research", "research"), ("competitors", "competitors"),
         ("find customers", "customers"), ("import contact", "import_contact"), ("design experiment", "design"),
         ("run for me scan", "scan"), ("prepare invitations", "invitations"), ("send to self", "send"),
         ("check replies", "replies"), ("record results", "results"), ("ask what next", "next_step"),
         ("export landing page", "export"), ("publish landing page", "publish"))

NEXT_STEPS = {"create": "Open your venture list and retry creating your idea.",
              "answer": "Open your idea questions and answer the pending question, then retry.",
              "skip": "Open your idea questions and skip the remaining questions, then retry.",
              "confirm": "Open your idea review and confirm the saved revision, then retry.",
              "design": "Open Experiments and check your saved test and success measure, then retry.",
              "results": "Open Experiments and record your actual results for the designed test, then retry.",
              "scan": "Open Settings > Capabilities and turn on Run your experiments, then save your monthly limit in Settings > Keys and connections > Your monthly spend cap and retry.",
              "next_step": "Open Experiments, check your recorded results, then ask what to do next.",
              "export": "Open Settings > Keys and connections > Coding tools, connect your tool and choose Allow Charlie, then open Website and retry with a new writable export path.",
              "connect": CODING_TOOLS_STEP, "build_page": PAGE_BUILD_STEP, "preview_export": PAGE_BUILD_STEP,
              "start_product": PRODUCT_BUILD_STEP, "check_product": PRODUCT_BUILD_STEP}


def journey_steps(fake_coding_tool):
    if not fake_coding_tool:
        return STEPS
    return (*STEPS[:2], ("allow Codex", "connect"), *STEPS[2:-2],
            ("build landing page with Codex", "build_page"), ("export landing page", "preview_export"),
            ("start product build with Codex", "start_product"), ("check product build", "check_product"), STEPS[-1])


def action_id(reply, key, next_step):
    for action in reply.get("actions", []):
        value = (action.get("payload") or {}).get(key)
        if isinstance(value, str) and value:
            return quote(value, safe="")
    raise AcceptanceFailure(next_step)


def read_arguments_file(path):
    try:
        supplied = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AcceptanceFailure("Your argument file is unreadable. Choose a JSON file with idea, answers and results, then retry.") from None
    require(isinstance(supplied, dict) and set(supplied) <= {"idea", "answer", "answers", "results", "fake_coding_tool"}
            and isinstance(supplied.get("fake_coding_tool", False), bool),
            'Use an argument JSON object with idea, answer or answers, and results. Set "fake_coding_tool" to true or false, then retry.')
    return supplied


def arguments(argv):
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("base_url", nargs="?", default="http://127.0.0.1:3100/api")
    parser.add_argument("--arguments-file", type=Path, help="JSON with your idea, answer and results")
    parser.add_argument("--idea")
    parser.add_argument("--answer", action="append", help="Your answer to an idea question; repeat for more answers")
    parser.add_argument("--results")
    parser.add_argument("--send-to-self", help="Send exactly one draft to your configured SMTP mailbox user")
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--show-replies", action="store_true", help="Show up to 300 characters of Charlie's reply on a failed step")
    parser.add_argument("--output", type=Path, default=Path("value-landing.html"))
    parser.add_argument("--timeout", type=float, default=180)
    parser.add_argument("--gate-child", action="store_true", help="Run an isolated scenario started by --gate")
    args = parser.parse_args(argv)
    supplied = read_arguments_file(args.arguments_file) if args.arguments_file else {}
    args.offline_child = args.gate_child and os.environ.get("SUPYMEM_TEST_OFFLINE_JOURNEY") == "1"
    args.fake_coding_tool = args.offline_child or supplied.get("fake_coding_tool", False)
    args.idea = args.idea or supplied.get("idea")
    args.results = args.results or supplied.get("results")
    args.answers = args.answer or supplied.get("answers") or [supplied.get("answer")]
    require(isinstance(args.answers, list) and bool(args.answers)
            and all(isinstance(answer, str) and answer.strip() for answer in args.answers)
            and all(isinstance(value, str) and value.strip() for value in (args.idea, args.results)),
            "Provide your idea, at least one answer and your actual results with arguments or --arguments-file, then retry.")
    args.answer = args.answers[0]
    args.stub = False
    require(math.isfinite(args.timeout) and args.timeout > 0, "Choose a positive finite --timeout and retry.")
    require(urlsplit(args.base_url).path.rstrip("/") == ("" if args.offline_child else "/api"),
            "Use your console proxy URL, such as http://127.0.0.1:3100/api, then retry.")
    require((args.fake_coding_tool and not args.gate_child) or not args.output.exists(),
            "Your export path already exists. Choose a new --output path to keep your saved page, then retry.")
    return args


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "--gate":
        return gate_main(argv[1:])
    if argv and argv[0] == "--offline":
        return gate_main(argv)
    try:
        args = arguments(argv)
        if args.gate_child and args.fake_coding_tool:
            data = os.environ.get("COMPANY_OS_DATA_DIR")
            require(fake_coding_tool_allowed() and data and
                    shutil.which("codex") == str(Path(data) / "bin" / "codex"), FAKE_GATE_STEP)
        if args.offline_child:
            require(not args.send_to_self and not args.publish, OFFLINE_GATE_STEP)
        journey = ValueJourney(Client(args.base_url, args.timeout), args)
    except AcceptanceFailure as error:
        print(f"FAIL arguments: {error}")
        return 1
    except ValueError:
        print("FAIL arguments: Check --help, use a loopback console /api URL and a new writable export path, then retry.")
        return 1
    failures = 0
    setup_failed = False
    for label, method in journey_steps(args.gate_child and args.fake_coding_tool):
        if setup_failed:
            print(f"SKIP {label}: Your live setup failed. Fix your model in Keys and connections, then retry.")
            continue
        journey.last_reply = None
        next_step = NEXT_STEPS.get(method, "Open Keys and connections, check this feature's settings, then retry.")
        try:
            reason = getattr(journey, method)()
            if method == "answer":
                for answer in args.answers[1:]:
                    journey.client.request("POST", journey.session_path("/messages"), {"text": answer})
                    journey.wait(lambda: journey.client.request("GET", journey.session_path()),
                                 lambda data: any(turn.get("role") == "founder" and turn.get("text") == answer for turn in data.get("turns", [])),
                                 "Your answer was not saved. Open your idea questions, answer the pending question and retry.")
        except AcceptanceFailure as error:
            optional_missing = error.status == 404 and method in {"catalog", "capability_list"}
            if isinstance(error, Skip) or optional_missing:
                reason = str(error) if isinstance(error, Skip) else "This new local route returned 404. Update your app to include this route, then retry."
                print(f"SKIP {label}: {reason}")
            else:
                failures += 1
                print(f"FAIL {label}: {error}; {next_step}")
                if args.show_replies and journey.last_reply:
                    print(f"REPLY {label}: {safe_reply(journey.last_reply)}")
                setup_failed = method == "setup"
        except (KeyError, TypeError, AttributeError, ValueError):
            failures += 1
            print(f"FAIL {label}: Your response has missing or invalid fields. {next_step}")
            setup_failed = method == "setup"
        else:
            print(f"PASS {label}: {reason}; continue in your app.")
    return int(failures > 0)


def free_ports():
    with socket.socket() as core, socket.socket() as console, socket.socket() as worker:
        sockets = (core, console, worker)
        for listener in sockets:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in sockets]


def fake_coding_tool_allowed():
    from core.desktop_env import is_packaged_desktop

    return os.environ.get("SUPYMEM_TEST_FAKE_CODING_TOOL") == "1" and not is_packaged_desktop()


def prepare_fake_coding_tool(directory, selections, *, packaged=False, python=None):
    if not any(selections):
        return
    require(not packaged and fake_coding_tool_allowed(), FAKE_GATE_STEP)
    from desktop.e2e_linux import ROOT, install_desktop_cli

    python = Path(python or sys.executable)
    binary = install_desktop_cli(directory, "codex", python)
    # shortcut: inline this dependency-free fixture, move it to shared test support if it gains imports.
    fixture = ast.parse((ROOT / "tests/unit/pagebuilder/test_pipeline.py").read_text(encoding="utf-8"))
    review_source = next((ast.unparse(node) for node in fixture.body
        if isinstance(node, ast.FunctionDef) and node.name == "review_payload"), None)
    review = '''if "PAGE_SOURCE_REVIEW" in arguments[-1]:
    target = Path("review/capability-review.json")
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps(review_payload(arguments[-1])))
    emit({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 0}})
    raise SystemExit(0)
'''
    script = binary.read_text(encoding="utf-8")
    require(review_source and "\nfixture_path =" in script, "Update the coding tool fixture in your source checkout, then rerun your gate.")
    # The stack may create its venv after we install this fake.
    sites = f"sys.path[:0] = [str(path) for path in Path({str(python.parent.parent)!r}).glob('lib/python*/site-packages')]\n"
    binary.write_text(script.replace("\nfixture_path =", "\n" + sites + review_source + "\n" + review + "fixture_path =", 1), encoding="utf-8")
    (directory / "mode").write_text("build", encoding="utf-8")
    return review_source


def approve_fake_coding_tool(client, provider, binary):
    tools = client.request("GET", "/local-tools/coding-agents")["tools"]
    tool = next((item for item in tools if item["name"] == provider), {})
    if tool.get("connected"):
        return
    require(tool.get("candidate") and tool.get("candidate_path") == str(binary.resolve()),
            "Your fake coding tool is not ready. Restart your isolated gate with its fake CLI on PATH, then retry.")
    result = client.request("POST", "/ui-config/coding-agents/consent", {"provider": provider, "allow": True, "candidate": tool["candidate"]})
    require(result["consent"].get(provider) is True,
            "Open Settings > Keys and connections > Coding tools, choose Allow Charlie for Codex, then retry.")


def worker_ready(client, port):
    try:
        with client.opener.open(f"http://127.0.0.1:{port}/ready", timeout=2) as response:
            document = response.read(65537)
        if len(document) > 65536:
            return False
        status = json.loads(document)
        return status.get("service") == "worker" and status.get("ready") is True
    except (URLError, OSError, ValueError, AttributeError):
        return False


def gate_main(argv):
    parser = ArgumentParser(description="Start your isolated local stack and run both HTTP value journeys. Logs stay private; no emails or pages are sent or published.")
    parser.add_argument("--env-file", type=Path, help="Your private .env file with your own live model settings")
    parser.add_argument("--offline", action="store_true", help="Use isolated source-only test fixtures instead of your live model and web settings")
    parser.add_argument("--log-dir", type=Path, help="A new directory for private redacted logs and exported HTML")
    parser.add_argument("--timeout", type=float, default=180, help="Seconds per readiness wait, journey request and evidence wait")
    parser.add_argument("--arguments-file", type=Path, action="append", help="Repeat with two unrelated value journey argument files")
    parser.add_argument("--page-fixtures", type=Path, default=Path(__file__).resolve().parents[1] / "tests/unit/pagebuilder/fixtures/local_tool_pages")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0, "tracebacks": 0, "locks": 0}
    children = []
    groups = set()
    threads = []
    completed = 0
    ready = False
    interrupted = False
    failure_reason = "Review the private logs and Keys and connections, then rerun scripts/local_gate.sh."
    logs = None
    data = None
    model = None
    previous_handlers = {}
    reports = {}
    selections = []
    secrets = []
    counts_lock = threading.Lock()

    def redact(text):
        for secret in secrets:
            for value in (secret, json.dumps(secret)[1:-1], repr(secret)[1:-1]):
                text = text.replace(value, "[redacted]")
        text = SECRET_PATTERN.sub("[redacted]", text)
        return re.sub(r"(://)[^\s/@]+:[^\s/@]+@", r"\1[redacted]@", text)

    def descendants():
        if not children:
            return
        listing = subprocess.run(["ps", "-axo", "pid=,ppid=,pgid="], capture_output=True, text=True, check=True)
        rows = [tuple(map(int, row.split())) for row in listing.stdout.splitlines() if len(row.split()) == 3]
        owned = {child.pid for child in children}
        while True:
            found = {pid for pid, parent, _group in rows if parent in owned} - owned
            if not found:
                break
            owned.update(found)
        groups.update(group for pid, _parent, group in rows if pid in owned and group != os.getpgrp())

    def stop(signum, frame):
        raise KeyboardInterrupt

    try:
        require(math.isfinite(args.timeout) and args.timeout > 0, "Choose a positive finite --timeout and retry.")
        sys.path[:0] = [str(root), str(root / "packages")]
        if args.offline:
            require(args.env_file is None and fake_coding_tool_allowed(), OFFLINE_GATE_STEP)
        else:
            require(args.env_file and args.env_file.is_file(), "Choose your private .env with --env-file, then retry.")
        examples = args.arguments_file or [root / "scripts/local_journeys" / name
            for name in ("enterprise_health.json", "consumer_fintech.json")]
        require(len(examples) == 2, "Pass --arguments-file twice for two unrelated value journeys, then retry your gate.")
        scenarios = [read_arguments_file(example) for example in examples]
        selections = [args.offline or scenario.get("fake_coding_tool", False) for scenario in scenarios]
        from coding_agents.env import CHILD_ENV_VARS, load_dotenv_file
        from core.desktop_env import is_packaged_desktop

        provided = {} if args.offline else load_dotenv_file(args.env_file)
        require(args.offline or bool(provided), "Your .env is empty. Add your model settings to that private file, then retry.")
        secrets = sorted({value for name, value in provided.items() if value
                          and re.search(r"(?i)key|password|secret|token", name)}, key=len, reverse=True)
        environment = {name: value for name, value in os.environ.items() if name in CHILD_ENV_VARS}
        if not args.offline:
            environment.update({name: "" for name in load_dotenv_file(root / ".env")})
        environment.update(provided)
        logs = args.log_dir.resolve() if args.log_dir else Path(tempfile.mkdtemp(prefix="supymem-local-gate-"))
        if args.log_dir:
            logs.mkdir(mode=0o700)
        os.chmod(logs, 0o700)
        data = tempfile.TemporaryDirectory(prefix="supymem-local-data-",
                                           dir=Path.home() if not args.offline and any(selections) else None)
        state = Path(data.name)
        if args.offline:
            state = state / "state"
            state.mkdir(mode=0o700)
        fake = state / "bin"
        review_source = prepare_fake_coding_tool(fake, selections, packaged=is_packaged_desktop(environment),
                                                python=sys.executable if args.offline else root / ".venv/bin/python")
        core, console, worker = free_ports()
        environment.update(SUPYMEM_MODE="local", APP_ENV="local", COMPANY_OS_ENV="local",
            SUPYMEM_PYTHON=sys.executable, PYTHONPATH=f"{root / 'packages'}:{root / 'services/company_os'}",
            HOME=str(state), COMPANY_OS_DATA_DIR=str(state), COMPANY_OS_DB_URL=f"sqlite:///{state}/local.db",
            SUPYMEM_LOCAL_PROXY_SECRET_FILE=str(state / "owner-session.secret"),
            DATABASE_URL=f"sqlite:///{state}/local.db", DB_URL=f"sqlite:///{state}/local.db",
            COMPANY_OS_WORKING_MEMORY_PATH=f"{state}/working_memory.sqlite3", REDIS_URL="", RDS_PROXY_ENDPOINT="",
            COS_WORKER_HEALTH_PATH=str(state / "worker-health.json"),
            CORE_PORT=str(core), CONSOLE_PORT=str(console), WORKER_HEALTH_PORT=str(worker),
            CORE_URL=f"http://127.0.0.1:{core}", CONSOLE_ORIGIN=f"http://127.0.0.1:{console}",
            LLM_OFFLINE="0", LLM_CASSETTE_MODE="off",
            COMPANY_OS_REQUIRE_SIGNED_CALLER="false", PYTHONUNBUFFERED="1")
        source_path = os.environ.get("PATH", "")
        fake_path = f"{fake}{os.pathsep}{source_path}"
        environment["PATH"] = fake_path if any(selections) else source_path
        environment["SUPYMEM_TEST_FAKE_CODING_TOOL"] = "1" if args.offline else ""
        if any(selections) and not args.offline:
            shadow = root / ".venv/bin/codex"
            require(not (shadow.is_file() and os.access(shadow, os.X_OK)),
                    "Move your checkout's .venv/bin/codex aside so your gate runs only the fake tool, then retry.")

        def collect(stream, filename, journey=False):
            seen = []
            with stream, filename.open("x", encoding="utf-8") as output:
                os.chmod(filename, 0o600)
                for line in stream:
                    with counts_lock:
                        counts["tracebacks"] += line.count("Traceback (most recent call last)")
                        counts["locks"] += line.lower().count("database is locked")
                        counts["FAIL"] += int(line.startswith("FAIL "))
                        if journey:
                            for state in ("PASS", "FAIL", "SKIP"):
                                if line.startswith(state + " "):
                                    if state != "FAIL":
                                        counts[state] += 1
                                    seen.append((state, line.partition(" ")[2].partition(":")[0]))
                    clean = redact(line)
                    output.write(clean)
                    if args.offline and (line.startswith("FAIL ") or journey and line.startswith(("PASS ", "SKIP "))):
                        print(f"{filename.stem}: {clean}", end="", flush=True)
            if journey:
                reports[filename.name] = seen

        def start(command, filename, *, journey=False, cwd=root):
            child = subprocess.Popen(command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, errors="replace", start_new_session=True)
            children.append(child)
            groups.add(child.pid)
            reader = threading.Thread(target=collect, args=(child.stdout, logs / filename, journey))
            reader.start()
            threads.append(reader)
            return child

        for signum in (signal.SIGINT, signal.SIGTERM):
            previous_handlers[signum] = signal.signal(signum, stop)
        stack_processes = []
        if args.offline:
            from desktop.offline_value import respond
            from tests.loopback_model import MODEL, serve_model

            model = serve_model(respond)
            model_state = model.__enter__()
            review_fixture = {"json": json, "re": re}
            exec(review_source, review_fixture)
            model_state.page_review = review_fixture["review_payload"]
            environment.update(SUPYMEM_TEST_OFFLINE_JOURNEY="1", GUARDRAILS_MODE="deterministic_only",
                OPENAI_COMPATIBLE_BASE_URL=model_state.url, SUPYMEM_LLM_PROVIDER="openai_compatible",
                SUPYMEM_MODEL_CHAT=MODEL, SUPYMEM_MODEL_PLANNING=MODEL, SUPYMEM_MODEL_EXTRACTION=MODEL,
                COMPANY_OS_URL=environment["CORE_URL"])
            environment["PYTHONPATH"] += f":{root}"
            command = [sys.executable, "-m", "scripts.desktop.offline_value"]
            stack_processes.append(start(command + ["api"], "stack.log", cwd=state))
            stack_processes.append(start(command + ["worker"], "worker.log", cwd=state))
        else:
            environment.pop("SUPYMEM_TEST_OFFLINE_JOURNEY", None)
            stack_processes.append(start(["bash", "scripts/run_local.sh", "--web"], "stack.log"))
        client = Client(f"http://127.0.0.1:{core}" if args.offline else f"http://127.0.0.1:{console}/api",
                        min(args.timeout, 2), owner_secret_file=state / "owner-session.secret")
        deadline = time.monotonic() + args.timeout
        while all(stack.poll() is None for stack in stack_processes) and time.monotonic() < deadline:
            descendants()
            try:
                status = client.request("GET", "/setup/status")
                health = client.request("GET", "/local/health")
                ready = (status.get("mode") == "local" and status.get("llm_mode") == "live"
                         and health.get("database", {}).get("ok") is True and worker_ready(client, worker))
            except (AcceptanceFailure, AttributeError):
                ready = False
            if ready:
                break
            time.sleep(0.25)
        require(ready, "Your local console did not become ready. Review stack.log, fix your model or startup settings, then retry.")
        if args.offline:
            client.timeout = args.timeout
            provider = {"provider": "openai_compatible", "model": MODEL, "base_url": model_state.url,
                        "context_window": 32_768}
            client.request("PUT", "/local/providers", provider)
            require(client.request("POST", "/local/providers/test", provider).get("ok"),
                    "Your fixture model is not ready. Check the isolated model settings, then retry.")
            client.request("PUT", "/local/settings/smtp", {"postal_address": "1 Fixture Street, Test City, US"})
        for index, (example, selected) in enumerate(zip(examples, selections), 1):
            if args.offline:
                model_state.scenario = scenarios[index - 1]
                (state / "journey-fixture.json").write_text(json.dumps(model_state.scenario), encoding="utf-8")
            if selected:
                try:
                    fixture = json.loads((args.page_fixtures / f"{index}.json").read_text())
                except (OSError, ValueError):
                    raise AcceptanceFailure(f"Choose a --page-fixtures folder containing a valid {index}.json, then retry your gate.") from None
                (fake / "page-fixture.json").write_text(json.dumps({"root": str(root), "template": fixture}))
            environment["SUPYMEM_TEST_FAKE_CODING_TOOL"] = "1" if selected else ""
            environment["PATH"] = fake_path if selected else source_path
            run = start([sys.executable, "scripts/local_value_journey.py", client.base_url,
                "--arguments-file", str(example.resolve()), "--output", str(logs / f"journey-{index}.html"),
                "--timeout", str(args.timeout), "--gate-child", *(["--show-replies"] if args.offline else [])],
                f"journey-{index}.log", journey=True)
            deadline = time.monotonic() + args.timeout * (len(journey_steps(selected)) * 2 + 1)
            while run.poll() is None and all(stack.poll() is None for stack in stack_processes) and time.monotonic() < deadline:
                descendants()
                time.sleep(0.25)
            require(run.poll() is not None, "Your journey did not finish or your stack stopped. Review the private logs, then restart and retry.")
            counts["FAIL"] += int(run.returncode != 0)
            completed += 1
        require(all(stack.poll() is None for stack in stack_processes),
                "Your local stack stopped during the journey. Review stack.log, then restart and retry.")
    except KeyboardInterrupt:
        counts["FAIL"] += 1
        interrupted = True
    except AcceptanceFailure as error:
        counts["FAIL"] += 1
        failure_reason = str(error)
    except (ImportError, OSError, ValueError, subprocess.SubprocessError) as error:
        counts["FAIL"] += 1
        first_line = next(iter(redact(str(error)).splitlines()), "")
        failure_reason = f"{type(error).__name__}: {first_line}. Fix the reported issue, then retry your gate."
    finally:
        for signum in previous_handlers:
            signal.signal(signum, signal.SIG_IGN)
        try:
            descendants()
        except (OSError, ValueError, subprocess.SubprocessError):
            counts["FAIL"] += 1
        for group in groups:
            try:
                os.killpg(group, signal.SIGTERM)
            except ProcessLookupError:
                pass
        deadline = time.monotonic() + 35
        while any(child.poll() is None for child in children) and time.monotonic() < deadline:
            time.sleep(0.1)
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for child in children:
            child.wait()
        for reader in threads:
            reader.join(timeout=5)
            if reader.is_alive():
                counts["FAIL"] += 1
        for index, selected in enumerate(selections[:completed], 1):
            report = reports.get(f"journey-{index}.log", [])
            expected = [("SKIP" if method in {"send", "replies", "publish"} else "PASS", label)
                        for label, method in journey_steps(selected)]
            matches = report == expected if args.offline else [label for _, label in report] == [label for _, label in expected]
            if not matches or (logs / f"journey-{index}.html").is_file() != selected:
                counts["FAIL"] += 1
        if model:
            model.__exit__(None, None, None)
        if data:
            data.cleanup()
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
    failed = bool(counts["FAIL"] or counts["tracebacks"] or counts["locks"] or completed != 2)
    summary = [f"{'FAIL' if failed else 'PASS'} local release gate: {'Fix the failures below, then retry.' if failed else 'Review your saved reports before releasing.'}",
        f"{'Offline API and worker' if args.offline else 'Live console'} ready: {'yes' if ready else 'no'}", f"Journeys completed: {completed}/2",
        f"PASS checks: {counts['PASS']}", f"FAIL checks or processes: {counts['FAIL']}", f"SKIP checks: {counts['SKIP']}",
        f"Python tracebacks: {counts['tracebacks']}", f"Database lock errors: {counts['locks']}",
        "API and worker logs: stack.log, worker.log" if args.offline else "API, worker and console logs: stack.log",
        "First journey log: journey-1.log", "Second journey log: journey-2.log",
        *(f"Journey {index} HTML export: {f'journey-{index}.html' if selected else 'not requested'}"
          for index, selected in enumerate((selections or [False, False]), 1)),
        f"Private output directory: {logs or 'not created'}", "Temporary database: isolated from your saved work",
        "Temporary data: removed after stopping your processes", "Started process groups: stopped", "Sending: not requested",
        "Publishing: not requested", f"Next step: {failure_reason if failed else 'Review the two journey logs and HTML exports, then test your packaged app.'}"]
    print("\n".join(summary))
    return 130 if interrupted else int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
