"""Authority checks and HTTP forwarding for private loopback listeners."""
from __future__ import annotations

import re
import threading
import time
from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REOPEN_PREVIEW = "Open your preview again from Build activity."
_HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "proxy-connection", "te", "trailer", "transfer-encoding", "upgrade"}


def valid_loopback_host(hosts: list[str], port: int | None) -> bool:
    return port is not None and hosts in ([f"127.0.0.1:{port}"], [f"localhost:{port}"])


class LoopbackRequest:
    def parse_request(self):
        if not super().parse_request():
            return False
        hosts = self.headers.get_all("Host", [])
        origins = self.headers.get_all("Origin", [])
        if (not valid_loopback_host(hosts, self.server.server_port)
                or self.command not in {"GET", "HEAD", "OPTIONS"} and origins and origins != [f"http://{hosts[0]}"]):
            self.send_error(403, REOPEN_PREVIEW)
            return False
        return True


def _end_to_end(headers):
    excluded = _HOP_HEADERS | {name.strip().lower() for value in headers.get_all("Connection", [])
        for name in value.split(",")} | {"content-length"}
    return [(name, value) for name, value in headers.items()
        if name.lower() not in excluded and "\r" not in value and "\n" not in value]


class _RelayHandler(LoopbackRequest, BaseHTTPRequestHandler):
    timeout = 5

    def log_message(self, *_args):
        pass

    def _forward(self):
        self.close_connection = True
        lengths = self.headers.get_all("Content-Length", [])
        # shortcut: fixed-length HTTP uploads only, upgrade when an approved preview needs streaming or upgrades.
        if (not self.path.startswith("/") or self.headers.get_all("Transfer-Encoding")
                or self.headers.get_all("Upgrade") or self.headers.get_all("Expect")
                or lengths and (len(lengths) != 1 or not re.fullmatch(r"[0-9]{1,8}", lengths[0]))):
            self.send_error(400, REOPEN_PREVIEW)
            return
        length = int(lengths[0]) if lengths else 0
        if length > 16 * 1024 * 1024:
            self.send_error(413, REOPEN_PREVIEW)
            return
        upstream = HTTPConnection("127.0.0.1", timeout=self.timeout)
        responding = False
        try:
            body = self.rfile.read(length)
            if len(body) != length:
                self.send_error(400, REOPEN_PREVIEW)
                return
            if self.server.stopped.is_set():
                return
            upstream.sock = self.server.connection()
            upstream.sock.settimeout(self.timeout)
            upstream.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
            upstream.putheader("Host", self.headers["Host"])
            upstream.putheader("Connection", "close")
            if lengths:
                upstream.putheader("Content-Length", str(length))
            for name, value in _end_to_end(self.headers):
                if name.lower() != "host":
                    upstream.putheader(name, value)
            upstream.endheaders(body)
            with upstream.getresponse() as response:
                if response.status < 200:
                    raise HTTPException()
                responding = True
                self.send_response(response.status)
                lengths = response.headers.get_all("Content-Length", [])
                if not response.chunked and len(lengths) == 1 and re.fullmatch(r"[0-9]+", lengths[0]):
                    self.send_header("Content-Length", lengths[0])
                for name, value in _end_to_end(response.headers):
                    self.send_header(name, value)
                self.end_headers()
                deadline = time.monotonic() + 30
                while not self.server.stopped.is_set() and time.monotonic() < deadline:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        except (OSError, HTTPException, ValueError):
            if not responding:
                self.send_error(502, REOPEN_PREVIEW)
        finally:
            upstream.close()

    do_GET = do_HEAD = do_OPTIONS = do_POST = do_PUT = do_PATCH = do_DELETE = _forward


class LoopbackHTTPRelay(ThreadingHTTPServer):
    def __init__(self, port, connect):
        self.connection = connect
        self.slots = threading.BoundedSemaphore(16)
        self.stopped = threading.Event()
        super().__init__(("127.0.0.1", port), _RelayHandler)
        try:
            threading.Thread(target=self.serve_forever, kwargs={"poll_interval": .1}, daemon=True).start()
        except BaseException:
            self.server_close()
            raise

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def listening(self):
        try:
            self.connection().close()
            return True
        except OSError:
            return False

    def close(self):
        self.stopped.set()
        self.shutdown()
        self.server_close()
