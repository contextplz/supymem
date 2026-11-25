"""MCP proxy connection to a Supymem-app-owned, bounded CUA daemon.

This process never starts the daemon. The signed Tauri host must spawn it as a
direct child so macOS attributes TCC grants to Supymem, and must provide its
private socket only for the exact reviewed work order.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import os
import re
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from desktop_runtime.cu_reliability import wait_until

ALLOWED = frozenset({"start_session", "get_session_state", "end_session", "health_report",
                     "check_permissions", "get_window_state", "click", "type_text",
                     "set_value", "press_key", "hotkey", "move_cursor", "scroll"})
# AI-loop engine (SUPYMEM_CU_ENGINE=ai_loop): the model may choose among these per-pid tools. Every call is pinned
# to the engine's target pid by CuaMac; there is no global event-tap tool here.
AI_LOOP_TOOLS = frozenset({"list_windows", "list_apps", "launch_app", "get_window_state", "click", "double_click",
                           "right_click", "drag", "type_text", "set_value", "press_key", "hotkey", "scroll", "invoke_menu",
                           "health_report", "check_permissions", "start_session", "end_session"})
OBSERVATION_PROTOCOL = "exact_window_observation_v1"
READ_ONLY_LIFECYCLE = frozenset({"list_windows"})
_PROXY_INIT_TIMEOUT = 5
_PROXY_INIT_ATTEMPTS = 3
_SOCKET_WAIT_SECONDS = 5


class CuaSessionError(RuntimeError):
    def __init__(self, code: str, detail: str | None = None):
        super().__init__(code)
        self.code = code  # Typed transport code; never infer dispatch phase from prose.
        self.detail = detail


def _reason(error: BaseException) -> str:
    """First line of a transport exception, sanitized and bounded for receipts."""
    first = (str(error).strip().splitlines() or [""])[0]
    return re.sub(r"[^A-Za-z0-9 _.:,;'()/-]", "", f"{type(error).__name__}: {first}")[:160]


class CuaSession:
    def __init__(self, binary: str, socket: str, *, observation_protocol: str | None = None,
                 observation_pid: int | None = None, tools: frozenset | None = None,
                 reviewed_surface: tuple[int, int] | None = None,
                 reviewed_surfaces: tuple[tuple[int, int], ...] | None = None,
                 reviewed_modal_authority=None):
        self.tools = tools
        self.reviewed_modal_authority = reviewed_modal_authority
        if reviewed_surface is not None and (type(reviewed_surface) is not tuple or len(reviewed_surface) != 2
                or any(type(value) is not int or value <= 0 for value in reviewed_surface)):
            raise CuaSessionError("desktop_cua_scope_invalid")
        self.reviewed_surface = reviewed_surface
        if reviewed_surfaces is not None and (reviewed_surface is not None
                or type(reviewed_surfaces) is not tuple or not 1 <= len(reviewed_surfaces) <= 8
                or any(type(row) is not tuple or len(row) != 2
                       or any(type(value) is not int or value <= 0 for value in row) for row in reviewed_surfaces)
                or len(set(reviewed_surfaces)) != len(reviewed_surfaces)):
            raise CuaSessionError("desktop_cua_scope_invalid")
        self.reviewed_surfaces = reviewed_surfaces
        if reviewed_modal_authority is not None:
            from desktop_runtime.cu_modal_authority import ReviewedModalAuthority
            pairs = reviewed_surfaces or ((reviewed_surface,) if reviewed_surface is not None else ())
            if (type(reviewed_modal_authority) is not ReviewedModalAuthority
                    or reviewed_modal_authority.scope.pairs != pairs):
                raise CuaSessionError("desktop_cua_scope_invalid")
        if observation_protocol not in {None, OBSERVATION_PROTOCOL}:
            raise CuaSessionError("desktop_cua_protocol_unsupported")
        if observation_protocol and (type(observation_pid) is not int or observation_pid <= 0):
            raise CuaSessionError("desktop_cua_scope_invalid")
        self.observation_protocol = observation_protocol
        self.observation_pid = observation_pid
        if (not Path(binary).is_file() or not socket.startswith("/private/tmp/supymem-cua-")
                or not socket.endswith("/driver.sock")):
            raise CuaSessionError("desktop_cua_session_unavailable")
        # The Tauri host confirms the socket before starting an order, but a
        # cold daemon can briefly lag; wait a bounded time before failing.
        if not wait_until(Path(socket).is_socket, timeout=_SOCKET_WAIT_SECONDS):
            raise CuaSessionError("desktop_cua_session_unavailable")
        self._loop = None
        self._session = None
        self._closing = None
        self._failure = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(binary, socket), daemon=True,
                                        name="supymem-cua-proxy")
        self._thread.start()
        # The daemon's listening socket can precede its first MCP handshake on
        # a cold macOS launch. Retry only proxy initialization; no tool call or
        # reviewed effect has happened at this point.
        if not self._ready.wait(_PROXY_INIT_TIMEOUT * _PROXY_INIT_ATTEMPTS + 3):
            raise CuaSessionError("desktop_cua_proxy_handshake_unavailable")
        if self._failure or not self._thread.is_alive():
            raise CuaSessionError(self._failure or "desktop_cua_session_unavailable")

    def _thread_main(self, binary: str, socket: str):
        try:
            asyncio.run(self._serve(binary, socket))
        except Exception:
            self._failure = ("desktop_cua_transport_unavailable" if self._ready.is_set()
                             else "desktop_cua_proxy_handshake_unavailable")
            self._ready.set()

    async def _serve(self, binary: str, socket: str):
        for attempt in range(_PROXY_INIT_ATTEMPTS):
            try:
                await self._serve_once(binary, socket)
                return
            except Exception:
                # Once initialized, a tool call may have dispatched an effect.
                # Reconnecting silently here would lose that receipt and risk
                # replaying it on a fresh session.
                if (self._ready.is_set() or attempt + 1 == _PROXY_INIT_ATTEMPTS
                        or not Path(socket).is_socket()):
                    raise
                await asyncio.sleep(0.2)

    async def _serve_once(self, binary: str, socket: str):
        env = dict(os.environ, CUA_DRIVER_EMBEDDED="1", CUA_DRIVER_RS_TELEMETRY_ENABLED="0")
        params = StdioServerParameters(command=binary, args=["mcp", "--embedded", "--socket", socket], env=env)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=_PROXY_INIT_TIMEOUT)
                self._loop = asyncio.get_running_loop()
                self._session = session
                self._closing = asyncio.Event()
                self._ready.set()
                await self._closing.wait()

    def call(self, name: str, arguments: dict, *, timeout: float | None = None):
        authority = getattr(self,"reviewed_modal_authority",None)
        if authority is not None:
            authority.check_transport(name,arguments)
        if authority is None and getattr(self, "reviewed_surfaces", None) is not None:
            from desktop_runtime.cu_reviewed_scope import check_reviewed_call
            check_reviewed_call(name, arguments, self.reviewed_surfaces)
        if authority is None and getattr(self, "reviewed_surface", None) is not None:
            from desktop_runtime.cu_reviewed_scope import check_reviewed_call
            check_reviewed_call(name, arguments, self.reviewed_surface)
        if name in READ_ONLY_LIFECYCLE and not getattr(self, "tools", None) and (
                not isinstance(arguments, dict) or type(arguments.get("pid")) is not int
                or arguments != {"pid": getattr(self, "observation_pid", None), "on_screen_only": False}
                or arguments.get("on_screen_only") is not False):
            raise CuaSessionError("desktop_cua_scope_invalid")
        allowed = ALLOWED | READ_ONLY_LIFECYCLE if getattr(self, "observation_protocol", None) == OBSERVATION_PROTOCOL else ALLOWED
        if getattr(self, "tools", None):
            allowed = self.tools
        if (name not in allowed or self._failure or not self._thread.is_alive()
                or self._loop is None or self._session is None):
            raise CuaSessionError("desktop_cua_session_unavailable")
        if timeout is None:
            # A cold first window walk (fresh install, screen-capture warm-up) can
            # outlast an effect's timeout; give observation its own budget.
            timeout = 60 if name == "get_window_state" else 25
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(name, arguments), self._loop)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise CuaSessionError("desktop_cua_transport_unavailable",
                                  f"{name} timed out after {int(timeout)}s") from error
        except Exception as error:
            raise CuaSessionError("desktop_cua_transport_unavailable", f"{name}: {_reason(error)}") from error
        if not isinstance(result.structuredContent, (dict, type(None))):
            raise CuaSessionError("desktop_cua_response_invalid")
        return result

    def close(self):
        if self._loop is not None and self._closing is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._closing.set)
            self._thread.join(5)
            if self._thread.is_alive():
                raise CuaSessionError("desktop_cua_close_unconfirmed")
