"""Owned localhost synthetic form fixture. No real signup, accounts or user data.

Server receipt + fresh existing owned CDP-page readback are independent of model
claims. Nothing in this fixture executes product actions or launches a browser.
"""

from __future__ import annotations

import hashlib
import html
import json
import socket
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs


class BrowserFormFixture:
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.nonce = uuid.uuid4().hex
        self.receipt_id = uuid.uuid4().hex
        self.expected = {
            "project": "Synthetic " + self.run_id,
            "email": "qa-" + self.run_id + "@example.invalid",
            "note": "Synthetic QA only " + self.run_id,
            "confirm": "yes",
            "nonce": self.nonce,
        }
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.server = self.thread = self.active_connection = None
        self.posts = self.valid_posts = self.rejected_posts = self.receipt_reads = 0
        self.rejection_counts = {}
        self.field_mismatch_counts = {}
        self.readback_ok = False
        self.closed = False
        self.cleanup_verified = None

    def start(self):
        if self.server is not None or self.closed:
            raise RuntimeError("qa_form_fixture_already_started")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def setup(self):
                super().setup()
                self.connection.settimeout(0.25)
                with fixture.lock:
                    if fixture.stop.is_set():
                        self.connection.close()
                        raise OSError("fixture stopping")
                    fixture.active_connection = self.connection

            def finish(self):
                try:
                    super().finish()
                except OSError:
                    pass
                finally:
                    with fixture.lock:
                        if fixture.active_connection is self.connection:
                            fixture.active_connection = None

            def log_message(self, *_args):
                pass  # Never log URLs, nonce, form body or errors.

            def send_error(self, code, message=None, explain=None):
                self.send(code, "Request refused")

            def handle(self):
                try:
                    super().handle()
                except (OSError, ValueError):
                    pass

            def send(self, status, body="", location=None):
                raw = body.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'none'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                )
                # Non-CORS form POSTs under no-referrer serialize Origin as null.
                # Same-origin retains exact owned-origin enforcement, with no external referrer.
                self.send_header("Referrer-Policy", "same-origin")
                self.send_header("Connection", "close")
                if location:
                    self.send_header("Location", location)
                self.end_headers()
                self.wfile.write(raw)
                self.close_connection = True

            def owned_request(self):
                return (
                    self.client_address[0] == "127.0.0.1"
                    and self.headers.get("Host") == fixture.host
                )

            def do_GET(self):
                if not self.owned_request():
                    self.send(403, "Refused")
                    return
                if self.path == fixture.form_path:
                    self.send(200, fixture.form_html())
                elif self.path == fixture.receipt_path:
                    with fixture.lock:
                        ready = fixture.valid_posts == 1
                        if ready:
                            fixture.receipt_reads += 1
                    self.send(
                        200 if ready else 404,
                        fixture.receipt_html() if ready else "Not ready",
                    )
                elif self.path == "/favicon.ico":
                    self.send(204)
                else:
                    self.send(404, "Not found")

            def do_POST(self):
                with fixture.lock:
                    fixture.posts += 1
                    duplicate = fixture.posts != 1
                body = None
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if not 0 < length <= 4096 or self.headers.get("Transfer-Encoding"):
                        raise ValueError()
                    raw = self.rfile.read(length)
                    if len(raw) != length:
                        raise ValueError()
                    body = parse_qs(
                        raw.decode("utf-8", errors="strict"),
                        keep_blank_values=True,
                        strict_parsing=True,
                        max_num_fields=8,
                    )
                except (ValueError, OSError):
                    body = None
                reasons = []
                if duplicate:
                    reasons.append("duplicate_submission")
                if not self.owned_request():
                    reasons.append("peer_or_host_mismatch")
                if self.path != fixture.submit_path:
                    reasons.append("submit_path_mismatch")
                origin = self.headers.get("Origin")
                if origin != fixture.origin:
                    reasons.append("origin_missing" if origin is None else "origin_null" if origin == "null" else "origin_mismatch")
                if self.headers.get("Content-Type", "").split(";", 1)[0] != "application/x-www-form-urlencoded":
                    reasons.append("content_type_mismatch")
                mismatched = []
                if body is None:
                    reasons.append("body_unreadable")
                else:
                    if set(fixture.expected) - set(body):
                        reasons.append("field_missing")
                    if set(body) - set(fixture.expected):
                        reasons.append("field_unexpected")
                    if any(len(values) != 1 for values in body.values()):
                        reasons.append("field_duplicate")
                    mismatched = [key for key, value in fixture.expected.items() if key in body and body[key] != [value]]
                    if mismatched:
                        reasons.append("field_value_mismatch")
                valid = not reasons
                with fixture.lock:
                    if valid:
                        fixture.valid_posts += 1
                    else:
                        fixture.rejected_posts += 1
                        for reason in reasons:
                            fixture.rejection_counts[reason] = fixture.rejection_counts.get(reason, 0) + 1
                        for key in mismatched:
                            fixture.field_mismatch_counts[key] = fixture.field_mismatch_counts.get(key, 0) + 1
                if valid:
                    self.send(303, location=fixture.receipt_path)
                else:
                    self.send(400, "Synthetic submission refused")

        class Server(HTTPServer):
            def handle_error(self, *_args):
                pass

        try:
            self.server = Server(("127.0.0.1", 0), Handler)
            self.server.timeout = 0.1
            self.thread = threading.Thread(
                target=self._serve, name="supymem-owned-form-qa", daemon=True
            )
            self.thread.start()
        except Exception:
            self.close()
            raise RuntimeError("qa_form_fixture_start_failed") from None
        return self

    def _serve(self):
        while not self.stop.is_set():
            try:
                self.server.handle_request()
            except OSError:
                if not self.stop.is_set():
                    self.stop.set()

    @property
    def host(self):
        if self.server is None:
            raise RuntimeError("qa_form_fixture_not_started")
        return f"127.0.0.1:{self.server.server_address[1]}"

    @property
    def origin(self):
        return "http://" + self.host

    @property
    def form_path(self):
        return "/form/" + self.nonce

    @property
    def submit_path(self):
        return "/submit/" + self.nonce

    @property
    def receipt_path(self):
        return "/receipt/" + self.receipt_id

    @property
    def marker(self):
        return "Synthetic QA accepted " + self.receipt_id

    def form_html(self):
        esc = html.escape
        return (
            "<!doctype html><html><head><title>Synthetic QA form</title></head><body>"
            "<h1>Owned synthetic QA form</h1><p>This is a local test, never a real signup.</p>"
            f'<form method="post" action="{esc(self.submit_path)}">'
            '<label>Project <input name="project" required maxlength="100"></label>'
            '<label>Synthetic email <input name="email" type="email" required maxlength="100"></label>'
            '<label>QA note <textarea name="note" required maxlength="100"></textarea></label>'
            '<label><input type="checkbox" name="confirm" value="yes" required>I understand this is synthetic QA</label>'
            f'<input type="hidden" name="nonce" value="{esc(self.nonce)}">'
            '<button type="submit">Submit synthetic QA</button></form></body></html>'
        )

    def receipt_html(self):
        return f"<!doctype html><html><head><title>Synthetic QA receipt</title></head><body><h1>{html.escape(self.marker)}</h1><p>No real user signup occurred.</p></body></html>"

    def task(self):
        return (
            f"In the browser open {self.origin}{self.form_path}. Fill Project with '{self.expected['project']}', "
            f"Synthetic email with '{self.expected['email']}', and QA note with '{self.expected['note']}'. "
            "Check 'I understand this is synthetic QA', click 'Submit synthetic QA' exactly once, "
            "and leave the accepted receipt page visible. This is a private localhost test. "
            "Do not use real personal information or submit any external signup form."
        )

    def verify(self, browser):
        with self.lock:
            server_ok = (
                self.posts == self.valid_posts == 1
                and self.rejected_posts == 0
                and self.receipt_reads >= 1
            )
        if not server_ok:
            return False, "qa_form_submission_or_ack_missing"
        # Read the already owned backend. Never create a browser to manufacture a
        # verification observation when the actual runtime did not use one.
        backend = getattr(browser, "b", None)
        if backend is None or not callable(getattr(backend, "observe", None)):
            return None, "qa_form_owned_browser_readback_unavailable"
        try:
            observed = backend.observe()
            ok = (
                observed.url == self.origin + self.receipt_path
                and self.marker in observed.text
            )
        except Exception:
            return None, "qa_form_owned_browser_readback_unavailable"
        with self.lock:
            server_ok = (
                self.posts == self.valid_posts == 1
                and self.rejected_posts == 0
                and self.receipt_reads >= 1
            )
            self.readback_ok = ok is True and server_ok
        return ok is True and server_ok, "qa_form_independent_receipt_and_page"

    def final_verdict(self):
        with self.lock:
            if not self.closed or self.cleanup_verified is not True:
                return None
            return (
                self.readback_ok
                and self.posts == self.valid_posts == 1
                and self.rejected_posts == 0
                and self.receipt_reads >= 1
            )

    def public_receipt(self):
        with self.lock:
            return {
                "fixture_id": self.run_id,
                "synthetic": True,
                "counts_as_real_user": False,
                "form_posts": self.posts,
                "valid_posts": self.valid_posts,
                "rejected_posts": self.rejected_posts,
                "rejection_counts": dict(self.rejection_counts),
                "field_mismatch_counts": dict(self.field_mismatch_counts),
                "ack_page_reads": self.receipt_reads,
                "owned_page_verified": self.readback_ok,
                "payload_sha256": hashlib.sha256(
                    json.dumps(self.expected, sort_keys=True).encode()
                ).hexdigest(),
                "cleanup_verified": self.cleanup_verified,
            }

    def close(self):
        with self.lock:
            self.stop.set()
            connection = self.active_connection
        if connection:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if self.server:
            self.server.server_close()
        if self.thread:
            self.thread.join(timeout=2)
        self.closed = True
        self.cleanup_verified = self.thread is None or not self.thread.is_alive()
        if not self.cleanup_verified:
            raise RuntimeError("qa_form_fixture_cleanup_unconfirmed")
        return True
