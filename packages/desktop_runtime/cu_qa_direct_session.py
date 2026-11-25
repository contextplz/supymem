"""Explicit QA-only direct MCP runtime; never falls back to a shared daemon.

Existing invoking-host grants determine permission attribution. This module does
not request/grant permissions and is not an installed-app acceptance path.
"""
from __future__ import annotations

import asyncio
import os
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from desktop_runtime.cua_session import (
    AI_LOOP_TOOLS, OBSERVATION_PROTOCOL, CuaSession, CuaSessionError, _PROXY_INIT_TIMEOUT,
)


_SESSION_CODES = frozenset({
    "desktop_cua_protocol_unsupported", "desktop_cua_scope_invalid",
    "desktop_cua_session_unavailable", "desktop_cua_proxy_handshake_unavailable",
    "desktop_cua_transport_unavailable", "desktop_cua_response_invalid", "desktop_cua_close_unconfirmed",
})


def _session_code(error):
    # Never stringify an exception: vendor/transport messages may contain AX data.
    code = getattr(error, "code", None)
    if code is None:
        code = error.args[0] if error.args else None  # legacy CuaSessionError
    return code if type(code) is str and code in _SESSION_CODES else "desktop_cua_transport_unavailable"


class QADirectSession(CuaSession):
    def __init__(self, binary: str, *, tools: frozenset = AI_LOOP_TOOLS,
                 observation_protocol: str | None = None, observation_pid: int | None = None):
        if not Path(binary).is_file():
            raise CuaSessionError("desktop_cua_session_unavailable")
        if (not isinstance(tools, frozenset) or not tools or not tools.issubset(AI_LOOP_TOOLS)
                or observation_protocol not in {None, OBSERVATION_PROTOCOL}
                or (observation_pid is not None and (type(observation_pid) is not int or observation_pid <= 0))
                or (observation_protocol is not None and observation_pid is None)):
            raise CuaSessionError("desktop_cua_scope_invalid")
        self.tools = tools
        self.observation_protocol, self.observation_pid = observation_protocol, observation_pid
        self._loop = self._session = self._closing = self._failure = None
        self._requested_close = threading.Event()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(binary, None), daemon=True,
                                        name="supymem-cua-qa-direct")
        self._thread.start()
        try:
            if not self._ready.wait(_PROXY_INIT_TIMEOUT + 3):
                raise CuaSessionError("desktop_cua_proxy_handshake_unavailable")
            if self._failure or not self._thread.is_alive():
                raise CuaSessionError(self._failure or "desktop_cua_session_unavailable")
        except BaseException as error:
            try:
                self.close()
            except Exception:
                error.cleanup_codes = ["native_backend_close_unconfirmed"]
            raise

    async def _serve(self, binary: str, socket):
        # Exactly one initialization attempt; never reconnect after possible effects.
        self._loop = asyncio.get_running_loop()
        self._closing = asyncio.Event()
        if self._requested_close.is_set():
            return
        # Native observation/input does not need model/cloud credentials. Keep
        # only process/toolchain locale essentials, never inherit provider keys.
        env = {key: value for key, value in os.environ.items()
               if key in {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE"}}
        env.update(CUA_DRIVER_EMBEDDED="1", CUA_DRIVER_RS_TELEMETRY_ENABLED="0")
        params = StdioServerParameters(command=binary, args=["mcp", "--direct"], env=env)
        # Native stderr can quote private AX values. Fixed stage errors suffice;
        # suppress the owned QA child's raw stream rather than printing payloads.
        with open(os.devnull, "w") as errors:
            await self._connected(params, errors)

    async def _connected(self, params, errors):
        async with stdio_client(params, errlog=errors) as (read, write):
            async with ClientSession(read, write) as session:
                self._session = session
                initialize = asyncio.create_task(session.initialize())
                closing = asyncio.create_task(self._closing.wait())
                try:
                    done, _ = await asyncio.wait({initialize, closing}, timeout=_PROXY_INIT_TIMEOUT,
                                                 return_when=asyncio.FIRST_COMPLETED)
                    if initialize not in done or closing in done:
                        raise CuaSessionError("desktop_cua_proxy_handshake_unavailable")
                    await initialize
                    if self._requested_close.is_set():
                        return
                    self._ready.set()
                    await self._closing.wait()
                finally:
                    initialize.cancel()
                    closing.cancel()
                    await asyncio.gather(initialize, closing, return_exceptions=True)
        # MCP context EOF owns only this direct runtime; no socket/daemon commands.

    def call(self, name: str, arguments: dict, *, timeout: float | None = None):
        pid = self.observation_pid
        if pid is not None:
            if not isinstance(arguments, dict):
                raise CuaSessionError("desktop_cua_scope_invalid")
            if name in {"list_windows", "get_window_state", "set_value"}:
                if type(arguments.get("pid")) is not int or arguments["pid"] != pid:
                    raise CuaSessionError("desktop_cua_scope_invalid")
            elif name in {"click", "double_click", "right_click", "drag", "type_text", "hotkey", "press_key", "scroll"}:
                target = arguments.get("target")
                if (not isinstance(target, dict) or target.get("kind") != "window"
                        or type(target.get("pid")) is not int or target["pid"] != pid
                        or type(target.get("window_id")) is not int or target["window_id"] <= 0
                        or arguments.get("delivery_mode") != "background"):
                    raise CuaSessionError("desktop_cua_scope_invalid")
            elif name in {"launch_app", "invoke_menu"}:
                raise CuaSessionError("desktop_cua_scope_invalid")
        try:
            return super().call(name, arguments, timeout=timeout)
        except CuaSessionError as error:
            raise CuaSessionError(_session_code(error)) from None

    def close(self):
        self._requested_close.set()
        if self._loop is not None and self._closing is not None and self._thread.is_alive():
            try:
                self._loop.call_soon_threadsafe(self._closing.set)
            except RuntimeError:
                self._failure = "desktop_cua_close_unconfirmed"
        if self._thread.is_alive():
            self._thread.join(5)
        if self._thread.is_alive() or self._failure:
            raise CuaSessionError("desktop_cua_close_unconfirmed")
