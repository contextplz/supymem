"""Synthetic localhost web fixture for computer-use tasks (no real sites, accounts or data).

One server per run; every page's facts derive deterministically from `uid` so oracles never
trust the engine. All POSTs are recorded in `posts` and every GET in `hits`.
"""
from __future__ import annotations

import hashlib
import html
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from core.browser_qualification_catalog import (FixtureState, PAGE, post_body_digest)


class FixtureServer(FixtureState):
    def __init__(self, uid: str):
        super().__init__(uid)
        self.lock = threading.Lock()
        self._response_recorded = threading.Condition(self.lock)
        self._pending_response_recording = set()
        self.httpd = self.thread = None

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}" if self.httpd else ""

    def delivered_posts(self, timeout=.5):
        """Bounded observation of existing response recording; no new delivery.

        A client can consume the final response byte just before the handler
        records its post-flush receipt. Wait only for those source-owned active
        records. Missing/failed/unknown or timed-out receipts remain unknown.
        """
        if type(timeout) not in (int, float) or not 0 <= timeout <= 1:
            raise ValueError('fixture_response_observation_bound_invalid')
        deadline = time.monotonic() + timeout
        with self._response_recorded:
            while any(isinstance(r, dict) and type(r.get('request_id')) is int
                      and r['request_id'] in self._pending_response_recording
                      and r.get('response') is None and r.get('handler_error') is None for r in self.post_receipts):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._response_recorded.wait(remaining)
            return list(self.posts), json.loads(json.dumps(self.post_receipts))

    def start(self):
        fx = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def out(self, status, body, ctype="text/html; charset=utf-8", extra=()):
                raw = body if isinstance(body, bytes) else body.encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()
                written = self.wfile.write(raw)
                self.wfile.flush()
                receipt = getattr(self, "_post_receipt", None)
                if receipt is not None:
                    with fx.lock:
                        # Complete server-side response delivery, not actor/model judgment.
                        receipt["response"] = {"status": status, "size_bytes": len(raw),
                                               "sha256": hashlib.sha256(raw).hexdigest(),
                                               "complete": written == len(raw)}
                        fx._response_recorded.notify_all()

            def page(self, title, body, status=200):
                self.out(status, PAGE.format(t=html.escape(title), b=body))

            def do_GET(self):
                u = urlparse(self.path)
                with fx.lock:
                    fx.hits.append(("GET", u.path))
                try:
                    fx.get(self, u.path, parse_qs(u.query))
                except (ValueError, IndexError):
                    self.page("Not found", "Not found", 404)

            def do_POST(self):
                u = urlparse(self.path)
                raw = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 65536))
                if "multipart/form-data" in self.headers.get("Content-Type", ""):
                    # Round-1 addition: record upload metadata (never content) for receipt oracles.
                    from email import message_from_bytes
                    msg = message_from_bytes(b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + raw)
                    body = {}
                    for part in msg.get_payload() if msg.is_multipart() else []:
                        data, name = part.get_payload(decode=True) or b"", part.get_param("name", header="content-disposition")
                        fn = part.get_filename()
                        if fn is not None:
                            body.update(_filename=fn, _sha256=hashlib.sha256(data).hexdigest(), _size=len(data))
                        else:
                            body[name] = data.decode(errors="replace")
                elif "json" in self.headers.get("Content-Type", ""):
                    try:
                        body = json.loads(raw or b"{}")
                    except ValueError:
                        body = {}
                else:
                    body = {k: v[0] for k, v in parse_qs(raw.decode(errors="replace"), keep_blank_values=True).items()}
                with fx.lock:
                    fx.posts.append((u.path, body))
                    self._post_receipt = {"schema_version": 1, "request_id": len(fx.posts),
                                          "method": "POST", "path": u.path,
                                          "body_sha256": post_body_digest(body),
                                          "chain": None, "response": None, "handler_error": None}
                    fx.post_receipts.append(self._post_receipt)
                    fx._pending_response_recording.add(self._post_receipt['request_id'])
                try:
                    fx.post(self, u.path, body)
                except Exception as error:
                    with fx.lock:
                        self._post_receipt["handler_error"] = type(error).__name__
                    raise
                finally:
                    with fx.lock:
                        fx._pending_response_recording.discard(self._post_receipt['request_id'])
                        fx._response_recorded.notify_all()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def close(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
