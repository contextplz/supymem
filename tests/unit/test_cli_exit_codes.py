"""cos exit codes: 2 blocked, 3 denied (T10.71)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "companyos_cli"))
import cos  # noqa: E402


class _Resp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.content = b"{}"
        self._payload = payload

    def json(self):
        return self._payload


class _Client:
    def __init__(self, resp: _Resp):
        self._resp = resp

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def post(self, *_a, **_k):
        return self._resp

    def get(self, *_a, **_k):
        return self._resp


def test_do_blocked_is_exit_2(monkeypatch) -> None:
    monkeypatch.setenv("COS_WORKSPACE_ID", "wsp_x")
    monkeypatch.setattr(
        cos,
        "_client",
        lambda: _Client(_Resp(200, {"status": "blocked", "message": "needs approval"})),
    )
    assert cos.main(["do", "file an issue"]) == cos.EXIT_BLOCKED


def test_do_denied_is_exit_3(monkeypatch) -> None:
    monkeypatch.setenv("COS_WORKSPACE_ID", "wsp_x")
    monkeypatch.setattr(
        cos,
        "_client",
        lambda: _Client(_Resp(403, {"error": "denied"})),
    )
    assert cos.main(["do", "file an issue"]) == cos.EXIT_DENIED
