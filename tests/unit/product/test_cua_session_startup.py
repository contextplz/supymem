"""A cold embedded MCP proxy may fail before any reviewed action is claimed."""
import asyncio
import threading
from pathlib import Path

import pytest

from desktop_runtime import cua_session


def test_initial_proxy_handshake_retries_without_dispatch(monkeypatch, tmp_path):
    socket = tmp_path / "driver.sock"
    socket.touch()
    monkeypatch.setattr(Path, "is_socket", lambda self: self == socket)
    attempts = []
    session = object.__new__(cua_session.CuaSession)
    session._ready = threading.Event()

    async def initialize(_binary, _socket):
        attempts.append((_binary, _socket))
        if len(attempts) == 1:
            raise ConnectionError("daemon not yet accepting MCP")

    monkeypatch.setattr(session, "_serve_once", initialize)
    asyncio.run(session._serve("cua-driver", str(socket)))
    assert attempts == [("cua-driver", str(socket)), ("cua-driver", str(socket))]


def test_proxy_handshake_stops_after_bounded_failures(monkeypatch, tmp_path):
    socket = tmp_path / "driver.sock"
    socket.touch()
    monkeypatch.setattr(Path, "is_socket", lambda self: self == socket)
    attempts = []
    session = object.__new__(cua_session.CuaSession)
    session._ready = threading.Event()

    async def unavailable(_binary, _socket):
        attempts.append(1)
        raise ConnectionError("MCP unavailable")

    monkeypatch.setattr(session, "_serve_once", unavailable)
    with pytest.raises(ConnectionError, match="MCP unavailable"):
        asyncio.run(session._serve("cua-driver", str(socket)))
    assert len(attempts) == cua_session._PROXY_INIT_ATTEMPTS


def test_proxy_handshake_does_not_retry_after_socket_disappears(monkeypatch, tmp_path):
    socket = tmp_path / "driver.sock"
    attempts = []
    session = object.__new__(cua_session.CuaSession)
    session._ready = threading.Event()

    async def unavailable(_binary, _socket):
        attempts.append(1)
        raise ConnectionError("daemon exited")

    monkeypatch.setattr(session, "_serve_once", unavailable)
    with pytest.raises(ConnectionError, match="daemon exited"):
        asyncio.run(session._serve("cua-driver", str(socket)))
    assert len(attempts) == 1


def test_proxy_never_retries_after_initialization(monkeypatch, tmp_path):
    socket = tmp_path / "driver.sock"
    socket.touch()
    monkeypatch.setattr(Path, "is_socket", lambda self: self == socket)
    attempts = []
    session = object.__new__(cua_session.CuaSession)
    session._ready = threading.Event()

    async def fail_after_ready(_binary, _socket):
        attempts.append(1)
        session._ready.set()
        raise ConnectionError("connection lost after an effect may have run")

    monkeypatch.setattr(session, "_serve_once", fail_after_ready)
    with pytest.raises(ConnectionError, match="connection lost"):
        asyncio.run(session._serve("cua-driver", str(socket)))
    assert len(attempts) == 1


def test_proxy_close_reports_bounded_join_uncertainty_without_daemon_commands():
    from types import SimpleNamespace
    calls = []
    session = object.__new__(cua_session.CuaSession)
    session._closing = SimpleNamespace(set=lambda: None)
    session._loop = SimpleNamespace(call_soon_threadsafe=lambda callback: calls.append("stop_owned_proxy"))
    session._thread = SimpleNamespace(is_alive=lambda: True, join=lambda timeout: calls.append(timeout))
    with pytest.raises(cua_session.CuaSessionError, match="close_unconfirmed"):
        session.close()
    assert calls == ["stop_owned_proxy", 5]
