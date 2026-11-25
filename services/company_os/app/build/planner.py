"""Turn a locked ADR into work orders."""

from __future__ import annotations

import json

from app.build.adr import locked_adr_for_venture
from app.build.workorder import WorkOrder, create_workorder, orders_for


def plan_work_orders(venture_id: str, *, caller=None) -> list[WorkOrder]:
    from app.core.local_mode import is_local

    adr = locked_adr_for_venture(venture_id)
    if adr is None:
        raise PermissionError("Open your venture chat and save your architecture brief, then request your build again."
            if is_local() else "adr_not_locked")
    existing = orders_for(venture_id)
    components = list(adr.stack) or ["app"]
    if is_local():
        from app.build.dispatcher import request_local_order, retrieve_local_order

        instruction = "Build from your locked architecture brief: " + json.dumps({
            "title": adr.title, "stack": adr.stack, "protected_routes": adr.protected_routes,
            "components_estimate": adr.components_estimate,
        })
        if existing:
            for work in existing:
                if work.id.startswith("dwo_"):
                    order = retrieve_local_order(venture_id, work.id, caller=caller)
                else:
                    if work.files_expected:
                        raise PermissionError("Open your venture chat and request this change from your saved plan. No coding work started because this request restricts the exact files that may change.")
                    order = request_local_order(venture_id, work.title + "\n\n" + instruction, caller=caller, repair=True)
                    work.id = order["id"]
                work.status = order["state"]
                work.files_expected = sorted({path for task in (order.get("approved_plan") or {}).get("tasks", [])
                    for path in task["files"]})
            return existing
        order = request_local_order(venture_id, instruction, caller=caller)
        work = create_workorder(venture_id, title=f"Build {adr.title}", components=components[:20],
            files_expected=sorted({path for task in (order.get("approved_plan") or {}).get("tasks", [])
                for path in task["files"]}))
        work.id, work.status = order["id"], order["state"]
        return [work]
    if existing:
        return existing
    wo = create_workorder(
        venture_id,
        title=f"Build {adr.title}",
        components=components[:20],
        files_expected=["inhouse_change.txt"],
    )
    return [wo]
