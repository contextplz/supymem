"""Exercise the local owner's invitation, self-send and reply journey on SQLite."""

from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formatdate, parseaddr
from pathlib import Path
import secrets
import shlex
import smtplib
import socket
import ssl
import subprocess
from threading import Thread

import httpx
import pytest

from tests.company_os_harness import company_os_test_client, configure_local_test_model
from tests.unit.test_local_mode import local_owner_app as local_owner_app


@pytest.fixture
def mailbox(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts"))
    from local_mail_journey import MailServer, SMTP

    cert, key = tmp_path / "smtp.pem", tmp_path / "smtp.key"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                    "-keyout", str(key), "-out", str(cert), "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)
    owner, password = "owner@sender.test", secrets.token_hex(24)
    recipients, messages, reads = [], [], []
    with MailServer(SMTP, context, owner, password) as server:
        connect = socket.socket.connect
        resolve = socket.getaddrinfo
        rcpt = smtplib.SMTP.rcpt

        def loopback_only(connection, address):
            if connection.family in {socket.AF_INET, socket.AF_INET6} and address != server.server_address:
                pytest.fail("The offline mail journey attempted a non-fixture connection")
            return connect(connection, address)

        def local_resolution(host, *args, **kwargs):
            if host != server.server_address[0]:
                pytest.fail("The offline mail journey attempted an unstubbed DNS lookup")
            return resolve(host, *args, **kwargs)

        def owner_only(client, address, options=()):
            recipients.append(address)
            if address != owner:
                pytest.fail("The mail journey attempted delivery outside the owner's mailbox")
            return rcpt(client, address, options)

        class Inbox:
            """shortcut: all fixture messages are recent, add date filtering when testing older mail."""

            def __init__(self, host, port, **options):
                assert (host, port) == ("inbox.sender.test", 1993)
                assert options["ssl_context"].check_hostname

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def login(self, user, supplied):
                if user != owner or not secrets.compare_digest(supplied, password):
                    pytest.fail("IMAP did not receive the saved mailbox credentials")
                return "OK", []

            def select(self, name, *, readonly):
                assert name == "INBOX" and readonly
                return "OK", [str(len(messages)).encode()]

            def response(self, name):
                return name, [{"UIDVALIDITY": b"1", "UIDNEXT": str(len(messages) + 1).encode()}[name]]

            def uid(self, operation, *args):
                if operation == "fetch":
                    uid, mode = args
                    assert mode == "(BODY.PEEK[])"
                    reads.append(uid)
                    return "OK", [(b"BODY[]", messages[int(uid) - 1].as_bytes(policy=policy.SMTP))]
                assert operation == "search" and args[0] is None
                criteria = [shlex.split(value)[0] for value in args[1:]]
                matches = list(enumerate(messages, 1))
                if criteria[0] == "UID":
                    matches = [(uid, msg) for uid, msg in matches if uid >= int(criteria[1].split(":")[0])]
                elif "HEADER" in criteria:
                    headers = [criteria[index + 1:index + 3] for index, token in enumerate(criteria) if token == "HEADER"]
                    matches = [(uid, msg) for uid, msg in matches
                               if any(value in str(msg.get(header, "")) for header, value in headers)]
                elif "FROM" in criteria:
                    sender = criteria[criteria.index("FROM") + 1]
                    matches = [(uid, msg) for uid, msg in matches if parseaddr(str(msg["From"]))[1] == sender]
                else:
                    assert criteria == ["ALL"] or criteria[0] in {"SINCE", "BEFORE"}
                return "OK", [" ".join(str(uid) for uid, _ in matches).encode()]

        monkeypatch.setattr(socket.socket, "connect", loopback_only)
        monkeypatch.setattr(socket, "getaddrinfo", local_resolution)
        monkeypatch.setattr(smtplib.SMTP, "rcpt", owner_only)
        monkeypatch.setattr("imaplib.IMAP4_SSL", Inbox)
        runner = Thread(target=server.serve_forever, daemon=True)
        runner.start()
        try:
            yield server, recipients, messages, reads
        finally:
            server.shutdown()
            runner.join(timeout=5)


@pytest.mark.parametrize("idea,audience,problem,domain", [
    pytest.param("Track calibration certificates for factories", "Plant maintenance managers",
                 "Expired certificates hold up equipment inspections", "calibration.test", id="b2b"),
    pytest.param("Plan quiet weekend hikes", "Families with young children",
                 "Trail listings rarely explain how crowded routes get", "quiet-hikes.test", id="consumer"),
])
def test_local_mail_self_send(local_owner_app, mailbox, monkeypatch, idea, audience, problem, domain):
    from sqlalchemy import select

    from app.core import local_env_import
    from app.db.models import GtmContact, GtmSuppression, GtmTouch
    from app.state import db
    from core import clock

    server, recipients, inbox, reads = mailbox
    owner, password = server.username, server.password
    prospect = f"reader@{domain}"
    postal = "81 Test Road, Test City, NY 10001"
    today = clock.utcnow()
    monkeypatch.setattr(clock, "_clock", lambda: today)
    monkeypatch.setattr(local_env_import, "_environment", lambda: {})
    monkeypatch.setenv("SUPYMEM_SINGLE_PROCESS", "0")
    monkeypatch.setenv("EMAIL_PROVIDER", "smtp")

    async def dns_fixture(transport, request):
        assert request.url.host == "cloudflare-dns.com" and request.url.path == "/dns-query"
        assert request.url.params.get("name") in {domain, "sender.test"} and request.url.params.get("type") == "MX"
        return httpx.Response(200, json={"Status": 0, "Answer": [{"type": 15, "data": "10 localhost."}]})

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", dns_fixture)
    with company_os_test_client(local_owner_app) as client:
        configure_local_test_model()

        def call(method, path, data=None, *, status=200):
            response = client.request(method, path, json=data)
            if password in response.text:
                pytest.fail("The mailbox password was exposed in an API response")
            assert response.status_code == status, (method, path, response.text)
            return response.json()

        saved = call("PUT", "/local/settings/smtp", {"host": "127.0.0.1", "port": server.server_address[1],
                     "user": owner, "password": password, "sender_name": "Mailbox Owner", "postal_address": postal,
                     "imap_host": "inbox.sender.test", "imap_port": 1993})
        assert saved["secrets_set"]["password"] and saved["values"]["user"] == owner
        for capability in ("send_email", "read_replies"):
            assert call("PUT", f"/local/capabilities/{capability}", {"on": True})["state"] == "ready"
        for section in ("smtp", "imap"):
            assert call("POST", f"/local/settings/{section}/test", {})["ok"]

        venture = call("POST", "/ventures", {"raw_idea": idea, "start": False}, status=201)
        client.headers["X-Workspace-Id"] = venture["workspace_id"]
        venture_path = f"/ventures/{venture['id']}"
        journey_path = f"/api/v1{venture_path}/gtm/journey"

        def chat(text):
            reply = call("POST", venture_path + "/chat", {"message": text})
            assert reply["kind"] != "confirmation"
            return reply["body_md"]

        answers = {"making": idea, "audience": audience, "problem": problem,
                   "progress": "I have a prototype with no users or revenue yet."}
        for _ in answers:
            context = call("GET", venture_path + "/context")
            if not context.get("question"):
                break
            field = context["question"]["field"]
            call("POST", venture_path + "/context/answer", {"revision": context["revision"],
                 "field": field, "value": answers[field]})

        def import_contact(name, address):
            preview = call("POST", venture_path + "/gtm/contacts/import/preview", {
                "format": "csv", "text": f"name,email,country\n{name},{address},US\n"})
            assert preview["valid_rows"] == 1
            result = call("POST", venture_path + "/gtm/contacts/import", {"preview_id": preview["preview_id"],
                          "lawful_basis_attested": True, "lawful_basis": "consent"})
            contact_id, = result["contact_ids"]
            return contact_id

        prospect_id = import_contact("Sample Prospect", prospect)
        owner_id = import_contact("Mailbox Owner", saved["values"]["user"])
        call("PUT", venture_path + "/autopilot", {"allow_send": True, "send_max_per_day": 1})
        for contact_id in (prospect_id, owner_id):
            draft_reply = call("POST", venture_path + "/chat/confirm", {"action": "outreach.draft",
                               "payload": {"run": True, "review_only": True, "contact_id": contact_id,
                                           "request": "Write a short interview invitation. Do not send it."},
                               "idempotency_key": f"draft-{contact_id}"})
            assert draft_reply["kind"] not in {"needs_you", "confirmation"}, draft_reply["body_md"]
        view = call("GET", journey_path)
        drafts = {draft["contact_id"]: draft for draft in view["drafts"] if draft["state"] == "draft"}
        assert set(drafts) == {prospect_id, owner_id}

        def compliant(body):
            assert "I have your address from my own contacts." in body
            assert 'just reply "no thanks"' in body
            assert postal in body

        for draft in drafts.values():
            compliant(draft["body"])
        assert not recipients and not server.messages and view["results"]["sent"] == 0

        chat("Show drafts")
        sent_reply = chat(f"Send it to {owner} now.")
        assert recipients == [owner] and len(server.messages) == 1, sent_reply
        sent = BytesParser(policy=policy.default).parsebytes(server.messages[0])
        assert parseaddr(str(sent["To"]))[1] == owner
        compliant(sent.get_body(preferencelist=("plain",)).get_content())

        capped = chat(f"Send it to {prospect} now.")
        assert "daily" in capped.lower() and "limit" in capped.lower(), capped
        view = call("GET", journey_path)
        assert view["results"]["sent"] == 1 and view["sends_left_today"] == 0
        assert any(draft["contact_id"] == prospect_id and draft["state"] == "draft" for draft in view["drafts"])
        assert recipients == [owner] and len(server.messages) == 1

        call("PUT", venture_path + "/autopilot", {"allow_send": True, "send_max_per_day": 2})
        assert call("GET", journey_path)["sends_left_today"] == 1
        call("POST", f"{journey_path}/prospects/{prospect_id}/reply", {"outcome": "opted_out", "text": "Please unsubscribe me."})
        blocked = chat(f"Send it to {prospect} now.")
        assert "opted out" in blocked.lower() or "suppressed" in blocked.lower(), blocked
        assert recipients == [owner] and len(server.messages) == 1

        reply = EmailMessage()
        reply["From"], reply["To"] = "owner-alias@sender.test", owner
        reply["Subject"] = f"Re: {sent['Subject']}"
        reply["Date"], reply["Message-ID"] = formatdate(usegmt=True), "<self-test-reply@sender.test>"
        reply["In-Reply-To"] = reply["References"] = str(sent["Message-ID"])
        reply_text = "Yes, I would like to discuss this. I can talk on Tuesday."
        reply.set_content(reply_text)
        inbox.extend((sent, reply))

        response = chat("Check replies")
        assert reply_text in response and reads, response
        results = call("GET", journey_path)["results"]
        recorded, = results["replies"]
        assert recorded["contact_id"] == owner_id and reply_text in recorded["body"]
        chat("Any replies?")
        assert recipients == [owner] and len(server.messages) == 1
        with db()() as session:
            assert session.get(GtmContact, owner_id).replied_at is not None
            contact = session.get(GtmContact, prospect_id)
            assert contact.lawful_basis == "consent" and contact.contact_provenance == "founder_import"
            assert contact.times_emailed == 0 and contact.unsubscribed_at is not None
            assert session.scalar(select(GtmSuppression.id).where(GtmSuppression.workspace_id == venture["workspace_id"],
                                  GtmSuppression.email == prospect))
            touch, = session.scalars(select(GtmTouch).where(GtmTouch.venture_id == venture["id"], GtmTouch.direction == "in"))
            assert touch.contact_id == owner_id and touch.in_reply_to == sent["Message-ID"]
