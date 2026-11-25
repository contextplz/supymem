"""OpenAI-compatible loopback fixture shared by local provider and journey tests."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace

MODEL = "offline-test-model"


@contextmanager
def serve_model(respond=None):
    state = SimpleNamespace(requests=[], answer="ok", reject_format=False, unavailable=False, model=MODEL)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_body(self, body, status=200, stream=False):
            data = body.encode() if stream else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            if self.path != "/v1/models":
                self.send_error(404)
                return
            state.requests.append((self.path, None))
            self.send_body({"object": "list", "data": [{"id": MODEL, "object": "model", "created": 0, "owned_by": "local"}]})

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions" or request.get("model") != MODEL:
                self.send_error(404)
                return
            state.requests.append((self.path, request))
            if state.unavailable:
                self.send_body({"error": {"message": "Model offline"}}, status=503)
                return
            if state.reject_format and request.get("response_format"):
                self.send_body({"error": {"message": "response_format is not supported"}}, status=400)
                return
            answer = respond(request, state) if respond else state.answer
            content = answer if isinstance(answer, str) else json.dumps(answer)
            usage = {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120}
            common = {"id": "offline-test-reply", "model": MODEL, "created": 0}
            if request.get("stream"):
                chunks = [{"choices": [{"index": 0, "delta": {"role": "assistant", "content": content}, "finish_reason": "stop"}]},
                          {"choices": [], "usage": usage}]
                self.send_body("".join("data: " + json.dumps({**common, "object": "chat.completion.chunk", **chunk}) + "\n\n"
                    for chunk in chunks) + "data: [DONE]\n\n", stream=True)
            else:
                self.send_body({**common, "object": "chat.completion", "usage": usage,
                    "choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": content}}]})

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        state.url = f"http://127.0.0.1:{server.server_port}/v1"
        try:
            yield state
        finally:
            server.shutdown()
            thread.join()
