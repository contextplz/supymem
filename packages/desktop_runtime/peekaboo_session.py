"""One local Peekaboo MCP process for a reviewed desktop task.

Peekaboo snapshots are owned by the process that captured them. Keeping one
stdio session alive lets a later action consume the exact snapshot without a
different CLI process guessing at a global or foreground target.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


class PeekabooSessionError(RuntimeError):
    pass


class PeekabooSession:
    def __init__(self, binary: str, *, allow_foreground: bool = False):
        self._loop = None
        self._session = None
        self._closing = None
        self._failure = None
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._thread_main, args=(binary, allow_foreground), daemon=True,
                                        name="supymem-peekaboo")
        self._thread.start()
        if not self._ready.wait(12) or self._failure or not self._thread.is_alive():
            raise PeekabooSessionError("desktop_driver_session_unavailable")

    def _thread_main(self, binary: str, allow_foreground: bool):
        try:
            asyncio.run(self._serve(binary, allow_foreground))
        except Exception:
            self._failure = "desktop_driver_session_unavailable"
            self._ready.set()

    async def _serve(self, binary: str, allow_foreground: bool):
        # Peekaboo defaults to background-only and refuses an AXPress even
        # after our reviewed foreground handoff. This authority is fixed when
        # the local MCP process starts; it cannot be escalated mid-session.
        args = ["mcp", "--no-remote"]
        if allow_foreground:
            args.append("--allow-foreground")
        params = StdioServerParameters(command=binary, args=args)
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                self._loop = asyncio.get_running_loop()
                self._session = session
                self._closing = asyncio.Event()
                self._ready.set()
                await self._closing.wait()

    def call(self, name: str, arguments: dict, *, timeout: float) -> tuple[dict, str, bool]:
        if (name not in {"see", "click", "type", "press", "set_value"} or self._failure
                or not self._thread.is_alive() or self._loop is None or self._session is None):
            raise PeekabooSessionError("desktop_driver_session_unavailable")
        future = asyncio.run_coroutine_threadsafe(
            self._session.call_tool(name, arguments), self._loop)
        try:
            result = future.result(timeout=timeout)
        except concurrent.futures.TimeoutError as error:
            future.cancel()
            raise PeekabooSessionError("desktop_driver_transport_unavailable") from error
        except Exception as error:
            raise PeekabooSessionError("desktop_driver_transport_unavailable") from error
        metadata = result.meta if isinstance(result.meta, dict) else {}
        text = next((part.text for part in result.content if part.type == "text"), "")
        if not isinstance(text, str) or len(text) > 250_000:
            raise PeekabooSessionError("desktop_driver_response_invalid")
        return metadata, text, result.isError is True

    def close(self):
        if self._loop is not None and self._closing is not None and self._thread.is_alive():
            self._loop.call_soon_threadsafe(self._closing.set)
            self._thread.join(5)
