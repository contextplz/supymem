#!/usr/bin/env python3
"""Prove your mail loop against disposable, loopback-only SMTP and IMAP servers.

Run with the project's Python and PYTHONPATH=packages:services/company_os.
OpenSSL creates a temporary test CA; production TLS verification stays enabled.
No real mailbox, model endpoint, browser, or existing data folder is used.
Model generation uses replay; a DNS fixture verifies only the test domain.
The test clock advances one reply-reading interval, not the worker or its gates.
shortcut: the stdlib mail stubs implement only the app's SMTP/IMAP commands,
upgrade to full test servers when the transport adds other protocol commands.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import ExitStack
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parseaddr
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import socketserver
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import threading
import time

from local_acceptance import AcceptanceFailure, Client, require


ROOT = Path(__file__).resolve().parents[1]


class MailServer(socketserver.ThreadingTCPServer):
    daemon_threads = True

    def __init__(self, handler, context, username, password, *, tls=False):
        self.context, self.username, self.password, self.tls = context, username, password, tls
        self.messages = []
        self.lock = threading.Lock()
        self.authenticated = 0
        self.reads = 0
        super().__init__(("127.0.0.1", 0), handler)

    def get_request(self):
        connection, address = super().get_request()
        connection.settimeout(10)
        try:
            return (self.context.wrap_socket(connection, server_side=True) if self.tls else connection), address
        except Exception:
            connection.close()
            raise

    def credentials(self, username, password):
        valid = hmac.compare_digest(username, self.username) and hmac.compare_digest(password, self.password)
        if valid:
            with self.lock:
                self.authenticated += 1
        return valid

    def handle_error(self, request, client_address):
        pass


class SMTP(socketserver.StreamRequestHandler):
    def send(self, text):
        self.wfile.write(text.encode("ascii") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        secure, authenticated, sender, recipient = False, False, "", ""
        self.send("220 loopback test SMTP")
        while raw := self.rfile.readline(8192):
            command, _, argument = raw.decode("ascii").strip().partition(" ")
            command = command.upper()
            if command in {"EHLO", "HELO"}:
                self.send("250-loopback\r\n250-AUTH PLAIN\r\n250 STARTTLS")
            elif command == "STARTTLS" and not secure:
                self.send("220 Start TLS")
                self.connection = self.server.context.wrap_socket(self.connection, server_side=True)
                self.rfile = self.connection.makefile("rb")
                self.wfile = self.connection.makefile("wb")
                secure, authenticated = True, False
            elif command == "AUTH" and secure:
                mechanism, _, encoded = argument.partition(" ")
                try:
                    _, username, password = base64.b64decode(encoded, validate=True).decode("ascii").split("\0")
                    authenticated = mechanism == "PLAIN" and self.server.credentials(username, password)
                except ValueError:
                    authenticated = False
                self.send("235 Authenticated" if authenticated else "535 Authentication refused")
            elif command == "MAIL" and authenticated:
                sender = argument.removeprefix("FROM:").strip().split(" ")[0].strip("<>")
                self.send("250 Sender accepted" if sender == self.server.username else "553 Test sender required")
            elif command == "RCPT" and authenticated and sender == self.server.username:
                recipient = argument.removeprefix("TO:").strip().strip("<>")
                if not re.fullmatch(r"[^\s<>@]+@(?:[a-z0-9-]+\.)+test", recipient):
                    recipient = ""
                self.send("250 Recipient accepted" if recipient else "550 Only reserved test addresses are accepted")
            elif command == "DATA" and authenticated and recipient:
                self.send("354 End with a dot")
                body = bytearray()
                while line := self.rfile.readline(65536):
                    if line == b".\r\n":
                        break
                    body.extend(line[1:] if line.startswith(b"..") else line)
                    if len(body) > 1_048_576:
                        return
                else:
                    return
                with self.server.lock:
                    self.server.messages.append(bytes(body))
                self.send("250 Delivered to local test server")
                sender, recipient = "", ""
            elif command == "RSET":
                sender, recipient = "", ""
                self.send("250 Reset")
            elif command == "QUIT":
                self.send("221 Goodbye")
                return
            else:
                self.send("530 Authenticate with TLS first")


class IMAP(socketserver.StreamRequestHandler):
    def send(self, text):
        self.wfile.write(text.encode("ascii") + b"\r\n")
        self.wfile.flush()

    def handle(self):
        authenticated, selected = False, False
        self.send("* OK loopback test IMAP")
        while raw := self.rfile.readline(8192):
            tokens = shlex.split(raw.decode("ascii"))
            tag, command, *arguments = tokens
            command = command.upper()
            if command == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1")
            elif command == "LOGIN":
                authenticated = len(arguments) == 2 and self.server.credentials(*arguments)
                if not authenticated:
                    self.send(tag + " NO Authentication refused")
                    continue
            elif command == "EXAMINE" and authenticated:
                selected = arguments == ["INBOX"]
                with self.server.lock:
                    count = len(self.server.messages)
                self.send(f"* {count} EXISTS\r\n* FLAGS (\\Seen)\r\n* OK [UIDVALIDITY 1] Validity\r\n* OK [UIDNEXT {count + 1}] Next")
                self.send(tag + " OK [READ-ONLY] EXAMINE completed")
                continue
            elif command == "UID" and selected:
                operation, *criteria = arguments
                with self.server.lock:
                    messages = list(self.server.messages)
                if operation.upper() == "SEARCH":
                    found = []
                    for uid, body in enumerate(messages, 1):
                        message = BytesParser(policy=policy.default).parsebytes(body)
                        if criteria[:1] == ["UID"]:
                            matches = uid >= int(criteria[1].split(":")[0])
                        elif "HEADER" in criteria:
                            positions = [index for index, value in enumerate(criteria) if value == "HEADER"]
                            matches = any(criteria[index + 2] in str(message.get(criteria[index + 1], "")) for index in positions)
                        else:
                            matches = "FROM" not in criteria or criteria[criteria.index("FROM") + 1] == parseaddr(str(message["From"]))[1]
                        if matches:
                            found.append(str(uid))
                    self.send("* SEARCH " + " ".join(found))
                elif operation.upper() == "FETCH":
                    uid = int(criteria[0])
                    if not 1 <= uid <= len(messages) or "BODY.PEEK[]" not in criteria[1]:
                        self.send(tag + " NO Read-only fetch required")
                        continue
                    body = messages[uid - 1]
                    self.send(f"* {uid} FETCH (UID {uid} BODY[] {{{len(body)}}}")
                    self.wfile.write(body + b")\r\n")
                    self.wfile.flush()
                    with self.server.lock:
                        self.server.reads += 1
                else:
                    self.send(tag + " BAD Unsupported UID command")
                    continue
            elif command == "LOGOUT":
                self.send("* BYE Goodbye\r\n" + tag + " OK LOGOUT completed")
                return
            else:
                self.send(tag + " NO Read-only authenticated inbox required")
                continue
            self.send(tag + " OK " + command + " completed")


def wait_for(fetch, timeout, reason):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fetch()
        if result:
            return result
        time.sleep(0.2)
    raise AcceptanceFailure(reason)


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


CHILD = """
import os, socket, sys
from pathlib import Path
from datetime import datetime, timedelta, timezone
from core import clock
clock_file = Path(os.environ['COMPANY_OS_DATA_DIR'], 'clock-offset')
def test_clock():
    offset = int(clock_file.read_text()) if clock_file.exists() else 0
    return datetime.now(timezone.utc) + timedelta(seconds=offset)
clock._clock = test_clock
def loopback_only(event, arguments):
    if event == 'socket.getaddrinfo' and arguments[0] not in {'127.0.0.1', '::1', None}:
        Path(os.environ['COMPANY_OS_DATA_DIR'], 'network-denied').touch()
        raise OSError('Your mail test only permits loopback connections.')
    if event == 'socket.connect' and arguments[0].family in {socket.AF_INET, socket.AF_INET6}:
        if arguments[1][0] not in {'127.0.0.1', '::1'}:
            Path(os.environ['COMPANY_OS_DATA_DIR'], 'network-denied').touch()
            raise OSError('Your mail test only permits loopback connections.')
sys.addaudithook(loopback_only)
import httpx
original_request = httpx.AsyncHTTPTransport.handle_async_request
async def test_domain_dns(transport, request):
    if request.url.host == 'cloudflare-dns.com':
        if request.url.path == '/dns-query' and request.url.params.get('name') == sys.argv[2] and request.url.params.get('type') == 'MX':
            return httpx.Response(200, json={'Status': 0, 'Answer': [{'type': 15, 'data': '10 localhost.'}]})
        raise OSError('Your DNS fixture only verifies your reserved test domain.')
    return await original_request(transport, request)
httpx.AsyncHTTPTransport.handle_async_request = test_domain_dns
import uvicorn
uvicorn.run('app.main:app', host='127.0.0.1', port=int(sys.argv[1]), proxy_headers=False, log_level='warning')
"""


def journey(args):
    require(bool(re.fullmatch(r"(?:[a-z0-9-]+\.)+test", args.domain)),
            "Use a reserved .test domain with --domain, then retry. No real addresses are allowed.")
    with tempfile.TemporaryDirectory(prefix="local-mail-journey-") as temporary, ExitStack() as cleanup:
        data = Path(temporary) / "data"
        data.mkdir(mode=0o700)
        certificate, private_key = data / "ca.pem", data / "ca.key"
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-keyout", str(private_key), "-out", str(certificate), "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
        private_key.chmod(0o600)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certificate, private_key)
        username, password = "owner@sender.test", secrets.token_hex(24)
        smtp = MailServer(SMTP, context, username, password)
        imap = MailServer(IMAP, context, username, password, tls=True)
        for server in (smtp, imap):
            cleanup.callback(server.server_close)
            runner = threading.Thread(target=server.serve_forever, daemon=True)
            runner.start()
            cleanup.callback(runner.join, 5)
            cleanup.callback(server.shutdown)
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        environment = {name: os.environ[name] for name in ("PATH", "LANG", "SYSTEMROOT") if name in os.environ}
        environment.update(SUPYMEM_MODE="local", APP_ENV="local", SUPYMEM_SINGLE_PROCESS="1",
            SUPYMEM_DESKTOP_INSTANCE_ID="local-mail-test", HOME=str(data), COMPANY_OS_DATA_DIR=str(data),
            COMPANY_OS_DB_URL=f"sqlite:///{data}/mail.db", COMPANY_OS_SECRETS_ENV=str(data / ".env"),
            COMPANY_OS_WORKING_MEMORY_PATH=str(data / "memory.db"),
            PYTHONPATH=f"{ROOT / 'packages'}:{ROOT / 'services/company_os'}", SSL_CERT_FILE=str(certificate),
            CONSOLE_ORIGIN=f"http://127.0.0.1:{port}", COMPANY_OS_REQUIRE_SIGNED_CALLER="0", ADMIN_REQUIRE_TOTP="0",
            LLM_OFFLINE="1", LLM_CASSETTE_MODE="replay", GUARDRAILS_MODE="deterministic_only", EMAIL_PROVIDER="smtp",
            WORKER_MAINTENANCE_SECONDS="2")
        log = cleanup.enter_context((data / "api.log").open("wb"))
        (data / "api.log").chmod(0o600)
        process = subprocess.Popen([sys.executable, "-c", CHILD, str(port), args.domain], cwd=data, env=environment,
                                   stdout=log, stderr=log, stdin=subprocess.DEVNULL)
        cleanup.callback(stop, process)
        client = Client(f"http://127.0.0.1:{port}", min(args.timeout, 10), owner_secret_file=data / "owner-session.secret")

        def ready():
            require(process.poll() is None, "Your local API exited. Fix app startup and rerun the mail test.")
            try:
                return client.request("GET", "/setup/status").get("mode") == "local"
            except AcceptanceFailure:
                return False

        wait_for(ready, args.timeout, "Your local API did not start. Check local startup, then rerun this test.")
        venture = client.request("POST", "/ventures", {"raw_idea": args.idea, "start": False})
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        path = f"/ventures/{venture['id']}"
        view_path = "/api/v1" + path + "/gtm/journey"
        client.request("POST", "/local/providers", {"provider": "openai_compatible",
            "base_url": "http://127.0.0.1:11434/v1", "models": {"chat": "offline-fixture"}})
        sender_name, postal_address = "Test Owner", "24 Test Road, Test City, NY 10001"
        saved = client.request("PUT", "/local/settings/smtp", {"host": "127.0.0.1", "port": smtp.server_address[1],
            "user": username, "password": password, "sender_name": sender_name, "postal_address": postal_address,
            "imap_host": "127.0.0.1", "imap_port": imap.server_address[1]})
        require(password not in json.dumps(saved), "Your settings response exposed a password. Fix Keys and connections, then retry.")
        require(password not in json.dumps(client.request("GET", "/local/settings/smtp")),
                "Your saved settings exposed a password. Fix Keys and connections before retrying.")
        for capability in ("send_email", "read_replies"):
            require(client.request("PUT", f"/local/capabilities/{capability}", {"on": True})["state"] == "ready",
                    "Open Keys and connections > Email and enable sending and reply reading, then retry.")
        for section in ("smtp", "imap"):
            tested = client.request("POST", f"/local/settings/{section}/test", {})
            require(tested.get("ok"), "Open Keys and connections > Email and retest your mailbox connection, then retry.")
        print("PASS saved SMTP and IMAP connections", flush=True)
        answers = {"making": args.idea, "audience": args.audience, "problem": args.problem,
                   "progress": "I have a prototype and no users or revenue yet."}
        for iteration in range(8):
            context_view = client.request("GET", path + "/context")
            if not context_view.get("question"):
                break
            field = context_view["question"]["field"]
            client.request("POST", path + "/context/answer", {"revision": context_view["revision"],
                "field": field, "value": answers[field]})
        client.request("PUT", path + "/autopilot", {"allow_send": True, "send_max_per_day": 1})

        def view():
            return client.request("GET", view_path)

        def chat(text):
            reply = client.request("POST", path + "/chat", {"message": text})
            require(reply.get("kind") != "confirmation" and not any(action.get("confirmation") for action in reply.get("actions", [])),
                    "Your requested mail needs another confirmation. Fix the request path and retry.")
            require(not re.search(r"\b\d+(?:\.\d+)?\s+credits?\b", reply.get("body_md", ""), re.I),
                    "Your chat shows usage amounts. Remove that copy and retry.")
            return reply

        def prepare(index):
            address = f"person{index}@{args.domain}"
            preview = client.request("POST", path + "/gtm/contacts/import/preview", {
                "format": "csv", "text": f"name,email,country\nTest Contact,{address},US\n"})
            require(preview.get("valid_rows") == 1,
                    "Your test contact is invalid. Open Find customers > Import and check the preview before retrying.")
            committed = client.request("POST", path + "/gtm/contacts/import", {"preview_id": preview["preview_id"],
                "lawful_basis_attested": True, "lawful_basis": "consent"})
            contact_id = committed["contact_ids"][0]
            prepared = client.request("POST", path + "/chat/confirm", {"action": "outreach.draft", "payload": {
                "run": True, "review_only": True, "contact_id": contact_id,
                "request": "Write a short invitation to discuss the problem. Do not send it."},
                "idempotency_key": secrets.token_hex(16)})
            require(prepared.get("kind") != "needs_you",
                    "Your invitation is blocked. Open Find customers, check the contact's permission and saved email, then retry drafting.")
            wait_for(lambda: next((draft for draft in view()["drafts"] if draft.get("contact_id") == contact_id), None),
                args.timeout, "Your invitation was not saved. Open Find customers and retry drafting your invitation.")
            return contact_id, address

        contact_id, address = prepare(1)
        second_id, second_address = prepare(2)
        chat("Show drafts")
        chat(f"Send it to {address} now.")
        wait_for(lambda: len(smtp.messages) == 1, args.timeout,
                 "Your email was not delivered. Open Keys and connections > Email, check Send email, and retry.")
        message = BytesParser(policy=policy.default).parsebytes(smtp.messages[0])
        body = message.get_body(preferencelist=("plain",)).get_content()
        require(parseaddr(str(message["To"]))[1] == address and sender_name in str(message["From"])
                and postal_address in body and "I have your address from my own contacts." in body
                and 'just reply "no thanks"' in body and smtp.authenticated > 0,
                "Your invitation is missing compliance text. Fix the shared email footer and retry.")
        print("PASS compliant SMTP delivery", flush=True)
        chat(f"Send it to {address} now.")
        require(len(smtp.messages) == 1, "Your invitation was sent twice. Fix send idempotency and retry.")
        cap_reply = chat(f"Send it to {second_address} now.")
        require(len(smtp.messages) == 1 and re.search(r"tomorrow|daily|limit|cap", cap_reply.get("body_md", ""), re.I),
                "Your daily send cap was not enforced. Fix send admission and retry.")
        require(any(item["contact_id"] == second_id and item["state"] == "draft" for item in view()["drafts"]),
                "Your capped invitation was lost. Fix draft preservation and retry.")
        print("PASS daily cap", flush=True)
        reply = EmailMessage()
        reply["From"], reply["To"] = address, username
        reply["Subject"] = "Re: " + str(message["Subject"])
        reply["Message-ID"] = f"<reply@{args.domain}>"
        reply["In-Reply-To"] = reply["References"] = str(message["Message-ID"])
        from datetime import datetime, timezone
        reply["Date"] = datetime.now(timezone.utc)
        reply_text = "Yes, I would like to discuss this. I can talk on Tuesday."
        reply.set_content(reply_text)
        with imap.lock:
            imap.messages.append(reply.as_bytes(policy=policy.SMTP))
        (data / "clock-next").write_text("301")
        (data / "clock-next").replace(data / "clock-offset")
        replies = wait_for(lambda: [item for item in view()["results"]["replies"] if item["contact_id"] == contact_id],
            args.timeout, "Your reply did not appear. Open Keys and connections > Email, enable Read replies and check your IMAP settings, then retry.")
        require(len(replies) == 1 and reply_text in replies[0]["body"] and imap.reads > 0,
                "Your saved reply is incomplete. Fix reply ingestion and retry.")
        print("PASS background reply", flush=True)
        contacts = client.request("GET", path + "/gtm/contacts")
        require(any(item["contact_id"] == contact_id and item["status"] in {"reply received", "positive reply"} for item in contacts["items"]),
                "Your contact was not marked replied. Fix reply attribution and retry.")
        with sqlite3.connect(f"file:{data / 'mail.db'}?mode=ro", uri=True) as database:
            require(database.execute("SELECT replied_at FROM gtm_contacts WHERE id = ?", (contact_id,)).fetchone()[0],
                    "Your reply is visible but your contact is not marked replied. Fix reply persistence and retry.")
            require(database.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'gtm.gmail.read' AND status = 'succeeded'").fetchone()[0],
                    "Your reply was not read by your background worker. Fix local reply scheduling and retry.")
        print("PASS contact marked replied", flush=True)
        chat("Any replies?")
        require(len([item for item in view()["results"]["replies"] if item["contact_id"] == contact_id]) == 1,
                "Your reply was saved twice. Fix reply deduplication and retry.")
        for iteration in range(2):
            client.request("POST", view_path + f"/prospects/{contact_id}/call-booked", {})
        records = [record for record in view()["manual_outreach"] if record["id"] == contact_id]
        require(len(records) == 1 and records[0]["sent_at"] and records[0]["booked_at"],
                "Your booked call was not saved. Open Find customers and retry marking the call booked.")
        print("PASS booked call", flush=True)
        stop(process)
        log.flush()
        require(not any(password.encode() in file.read_bytes() for file in data.rglob("*") if file.is_file()),
                "Your password was stored or logged in plaintext. Fix the encrypted settings boundary before retrying.")
        print("PASS encrypted password", flush=True)
        require(not (data / "network-denied").exists(), "Your mail loop attempted an external connection. Fix its local routing and retry.")
        print("PASS loopback only", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--idea", default="A parts scheduling tool for repair shops")
    parser.add_argument("--audience", default="Repair shop owners")
    parser.add_argument("--problem", default="Parts and technicians arrive at different times")
    parser.add_argument("--domain", default="repairs.test")
    parser.add_argument("--timeout", type=float, default=60)
    args = parser.parse_args(argv)
    def interrupted(signum, frame):
        raise AcceptanceFailure("Your mail test was stopped. Rerun it when you are ready.")

    signal.signal(signal.SIGTERM, interrupted)
    try:
        require(0 < args.timeout <= 120, "Use --timeout between 1 and 120 seconds, then retry.")
        journey(args)
    except (AcceptanceFailure, OSError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, AcceptanceFailure) else "Check your Python environment and OpenSSL installation, then rerun this mail test."
        print("FAIL " + message, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
