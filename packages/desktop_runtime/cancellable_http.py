"""Native model HTTP on the bridge's existing loop, with bounded admission.

No per-request worker/watchdog thread is created. Native worker threads wait on
one coroutine future; Stop/deadline cancels that future and closes its HTTP I/O.
Disconnecting cannot undo a provider call already admitted by Core: accounting
and reconciliation remain server-owned, and the request is never replayed.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time

import httpx


class ModelRequestStopped(RuntimeError):
    def __init__(self):
        super().__init__("native_model_request_stopped")


class ModelRequestDeadline(RuntimeError):
    def __init__(self):
        super().__init__("native_model_request_deadline")


class CancellableHTTP:
    def __init__(self, *, transport=None, max_pending=4):
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        self.client = httpx.AsyncClient(transport=transport, timeout=httpx.Timeout(40, connect=5, pool=1),
                                       limits=httpx.Limits(max_connections=max_pending,
                                                           max_keepalive_connections=max_pending),
                                       follow_redirects=False, trust_env=False)
        self.max_pending = max_pending
        self.lock = threading.Lock()
        self.pending = 0
        self.tasks = set()
        self.closed = False

    async def _request(self, method, url, *, body, headers, max_bytes=262144):
        timeout = httpx.Timeout(360, connect=5, pool=1) if url.endswith("/browser-ai-call") else self.client.timeout
        async with self.client.stream(method, url, json=body, headers=headers, timeout=timeout) as response:
            content = bytearray()
            async for chunk in response.aiter_bytes():
                if len(content) + len(chunk) > max_bytes:
                    raise ValueError("native_worker_response_bounds")
                content.extend(chunk)
            return httpx.Response(response.status_code, headers=response.headers,
                                  content=bytes(content), request=response.request)

    def _submit(self, method, url, body, headers, max_bytes=262144):
        result = concurrent.futures.Future()
        def start():
            if result.cancelled() or self.closed:
                with self.lock:
                    self.pending -= 1
                if not result.cancelled():
                    try:
                        result.set_exception(RuntimeError("native_model_transport_unavailable"))
                    except concurrent.futures.InvalidStateError:
                        pass
                return
            task = self.loop.create_task(self._request(method, url, body=body, headers=headers, max_bytes=max_bytes))
            self.tasks.add(task)
            def completed(task):
                self.tasks.discard(task)
                with self.lock:
                    self.pending -= 1
                if result.cancelled():
                    # Retrieve exceptions even when the native waiter has left.
                    if not task.cancelled():
                        task.exception()
                    return
                if task.cancelled():
                    result.cancel()
                    return
                error = task.exception()
                try:
                    if error is not None:
                        result.set_exception(error)
                    else:
                        result.set_result(task.result())
                except concurrent.futures.InvalidStateError:
                    pass  # Stop raced a completed response; never deliver it.
            task.add_done_callback(completed)
            def cancel_source(future):
                if future.cancelled() and not task.done():
                    try:
                        self.loop.call_soon_threadsafe(task.cancel)
                    except RuntimeError:
                        pass  # loop shutdown owns and cancels outstanding tasks
            result.add_done_callback(cancel_source)
        try:
            self.loop.call_soon_threadsafe(start)
        except RuntimeError:
            with self.lock:
                self.pending -= 1
            raise RuntimeError("native_model_transport_unavailable") from None
        return result

    def request(self, method, url, *, body, headers, stop, deadline, max_bytes=262144):
        if type(max_bytes) is not int or not 1 <= max_bytes <= 32 * 1024 * 1024:
            raise ValueError("native_worker_response_bounds")
        if threading.get_ident() == self.loop_thread:
            raise RuntimeError("native_model_request_wrong_thread")
        if stop.is_set():
            raise ModelRequestStopped()
        if time.monotonic() >= deadline:
            raise ModelRequestDeadline()
        with self.lock:
            if self.closed or not self.loop.is_running():
                raise RuntimeError("native_model_transport_unavailable")
            if self.pending >= self.max_pending:
                raise RuntimeError("native_model_transport_busy")
            self.pending += 1
        future = self._submit(method, url, body, headers, max_bytes=max_bytes)
        try:
            while True:
                if stop.is_set():
                    raise ModelRequestStopped()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ModelRequestDeadline()
                try:
                    value = future.result(timeout=min(0.05, remaining))
                    # A response racing Stop is never delivered to the model/executor.
                    if stop.is_set():
                        raise ModelRequestStopped()
                    if time.monotonic() >= deadline:
                        raise ModelRequestDeadline()
                    return value
                except concurrent.futures.TimeoutError:
                    continue
                except concurrent.futures.CancelledError:
                    raise ModelRequestStopped() from None
        finally:
            if not future.done():
                future.cancel()

    async def close(self):
        with self.lock:
            self.closed = True
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.aclose()
