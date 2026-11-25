"""Upload envelope exception remains bounded and applies only to the intake endpoint."""
import asyncio
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.responses import Response


def test_gateway_upload_envelope_and_normal_limits():
    path = Path(__file__).resolve().parents[3] / "services/gateway/app/body_limit.py"
    spec = importlib.util.spec_from_file_location("intake_gateway_limit", path)
    module = importlib.util.module_from_spec(spec)
    settings = SimpleNamespace(gateway_max_request_body_bytes=25 * 1024 * 1024)
    with patch.dict("sys.modules", {"app.config": SimpleNamespace(settings=settings),
        "app.proxy": SimpleNamespace(_IMPORT_UPLOAD_PATH=__import__("re").compile(r"never"))}):
        spec.loader.exec_module(module)

    async def exercise(path, size):
        sent = []
        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        async def send(message):
            sent.append(message)
        async def upstream(scope, receive, send):
            await Response("ok")(scope, receive, send)
        await module.RequestBodyLimitMiddleware(upstream)({"type": "http", "method": "POST", "path": path,
            "headers": [(b"content-length", str(size).encode())]}, receive, send)
        return sent[0]["status"]

    assert asyncio.run(exercise("/api/entry/evidence/files", 100 * 1024 * 1024 + 4096)) == 200
    assert asyncio.run(exercise("/api/entry/evidence/files", 101 * 1024 * 1024 + 1)) == 413
    assert asyncio.run(exercise("/api/other", 25 * 1024 * 1024 + 1)) == 413
