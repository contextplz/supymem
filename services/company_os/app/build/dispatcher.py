"""Dispatch a work order to a coding adapter after the ADR is locked."""

from __future__ import annotations

import asyncio
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.build.adr import locked_adr_for_venture
from app.build.workorder import WorkOrder
from coding_agents.env import child_env
from coding_agents.protocol import CodingRequest, CodingResult


@dataclass
class DispatchResult:
    ok: bool
    reason: str = ""
    changed_files: list[str] | None = None
    device_work_order_id: str | None = None
    review_path: str | None = None


def files_outside(changed: list[str], expected: list[str]) -> list[str]:
    allowed = set(expected)
    return [path for path in changed if path not in allowed]


def request_local_order(venture_id: str, instruction: str, **kwargs) -> dict:
    from fastapi import HTTPException
    from app.builder.ctl import request_local_build

    try:
        return request_local_build(venture_id, instruction, **kwargs)
    except HTTPException as exc:
        if (isinstance(exc.detail, dict) and exc.detail.get("code") == "build_tool_needs_setup"
                and "Coding tools" in exc.detail.get("message", "")):
            raise HTTPException(exc.status_code, detail={**exc.detail, "message":
                "Open Settings > Keys and connections > Coding tools, connect your tool and allow Charlie to use it, then retry your request."}) from exc
        raise


def retrieve_local_order(venture_id: str, order_id: str, *, caller=None) -> dict:
    from app.admin.authn import CallerContext
    from app.api.device_orders_api import invoke
    from app.build import device_orders
    from app.core.local_owner import current_owner

    if caller is None:
        owner = current_owner()
        caller = CallerContext(sub=owner.id, email=owner.email or "", role="user", path="", method="GET", exp=0, nonce="")
    return invoke(device_orders.retrieve, venture_id, caller, order_id)


async def dispatch(
    wo: WorkOrder,
    *,
    workdir: Path | None = None,
    adapter: Any = None,
    fake_result: CodingResult | None = None,
    caller: Any = None,
) -> DispatchResult:
    from app.core.local_mode import is_local

    adr = locked_adr_for_venture(wo.venture_id)
    if adr is None:
        return DispatchResult(ok=False, reason=(
            "Open your venture chat and save your architecture brief, then request your build again."
            if is_local() else "adr_not_locked"))
    if is_local():
        if wo.id.startswith("dwo_"):
            order = retrieve_local_order(wo.venture_id, wo.id, caller=caller)
        else:
            if wo.files_expected:
                return DispatchResult(ok=False, reason="Open your venture chat and request this change from your saved plan. No coding work started because this request restricts the exact files that may change.")
            instruction = wo.title + "\n\nYour locked architecture brief: " + json.dumps({
                "title": adr.title, "stack": adr.stack, "protected_routes": adr.protected_routes,
                "components": wo.components,
            })
            order = request_local_order(wo.venture_id, instruction, caller=caller, repair=True)
            wo.id = order["id"]
        wo.status = order["state"]
        return DispatchResult(ok=False, reason=order["state"], device_work_order_id=order["id"],
            review_path=order.get("review_path") or f"/v/{wo.venture_id}/device-work-orders/{order['id']}")
    dest = Path(workdir) if workdir is not None else Path.cwd()
    dest.mkdir(parents=True, exist_ok=True)

    if fake_result is not None:
        result = fake_result
    elif adapter is not None:
        req = CodingRequest(
            prompt=wo.title,
            workdir=dest,
            allow_write=True,
        )
        result = await adapter.run(req)
    else:
        return DispatchResult(ok=False, reason="no_adapter")

    changed = list(result.changed_files or [])
    if wo.files_expected:
        outside = files_outside(changed, wo.files_expected)
        if outside:
            return DispatchResult(
                ok=False,
                reason="files_outside_expected",
                changed_files=changed,
            )
    if not result.ok:
        return DispatchResult(ok=False, reason=result.error or "adapter_failed", changed_files=changed)
    wo.status = "done"
    return DispatchResult(ok=True, changed_files=changed)


async def dispatch_with_timeout_kill(
    argv: list[str],
    *,
    cwd: Path | str,
    timeout_s: float,
) -> dict[str, Any]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        env=child_env(),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout_s)
        return {"killed": False, "returncode": proc.returncode}
    except TimeoutError:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            return {"killed": True}
        try:
            await asyncio.wait_for(proc.wait(), timeout=0.2)
        except TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        return {"killed": True}
