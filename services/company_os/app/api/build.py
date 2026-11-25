"""Build: ADR, work orders, gates."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.api.schemas import AdrBody
from app.build.adr import create_adr, lock_adr, locked_adr_for_venture
from app.build.gates import run_all
from app.build.planner import plan_work_orders
from app.core.local_mode import is_local
from app.build.preview import start_preview
from app.db.models import Venture
from app.state import MEM, db

router = APIRouter(tags=["build"])


def _require_legacy_local_execution() -> None:
    from app.worker.handlers import hosted_mode

    # This route supplies fixture gate inputs and a loopback preview address.
    # Enabling a real build provider elsewhere must not enable this shortcut.
    if hosted_mode():
        raise HTTPException(
            503,
            detail={
                "code": "capability_unavailable",
                "capability": "legacy_build_evidence",
                "message": "Verified build artifacts and executed QA results are required.",
            },
        )


@router.post("/ventures/{venture_id}/build/adr")
def create_and_lock_adr(venture_id: str, payload: AdrBody):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    existing = locked_adr_for_venture(venture_id)
    if existing:
        return {"adr_id": existing.id, "status": existing.status, "checksum": existing.checksum}
    adr = create_adr(
        venture_id,
        title=payload.title,
        stack=payload.stack,
        protected_routes=payload.protected_routes,
        components_estimate=payload.components_estimate,
    )
    locked = lock_adr(adr.id)
    MEM[f"adr:{venture_id}"] = locked.id
    return {"adr_id": locked.id, "status": locked.status, "checksum": locked.checksum}


@router.post("/ventures/{venture_id}/build/plan")
def plan_build(venture_id: str, request: Request):
    try:
        if is_local():
            from app.admin.authn import verify_caller

            orders = plan_work_orders(venture_id, caller=verify_caller(request))
        else:
            orders = plan_work_orders(venture_id)
    except PermissionError as e:
        raise HTTPException(409, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    serialized = [
        {
            "id": wo.id,
            "title": wo.title,
            "components": wo.components,
            "files_expected": wo.files_expected,
            "status": wo.status,
            **({"review_path": f"/v/{venture_id}/device-work-orders/{wo.id}"} if is_local() and wo.id.startswith("dwo_") else {}),
        }
        for wo in orders
    ]
    MEM[f"build:{venture_id}"] = {"work_orders": serialized}
    return {"work_orders": serialized}


@router.get("/ventures/{venture_id}/build")
def get_build(venture_id: str):
    from app.worker.handlers import hosted_mode

    adr = locked_adr_for_venture(venture_id)
    build = MEM.get(f"build:{venture_id}") or {}
    # Old process-local records contain no execution receipt or artifact hash.
    # They cannot establish a hosted QA pass or a reachable product preview.
    gates = None if hosted_mode() or is_local() else MEM.get(f"gates:{venture_id}")
    preview = None if hosted_mode() or is_local() else MEM.get(f"preview:{venture_id}")
    return {
        "adr": (
            {"id": adr.id, "status": adr.status, "checksum": adr.checksum, "title": adr.title}
            if adr
            else None
        ),
        "work_orders": build.get("work_orders") or [],
        "gates": gates,
        "preview": preview,
    }


@router.post("/ventures/{venture_id}/build/gates")
def run_gates(venture_id: str):
    if is_local():
        from app.chat.build_path import local_build_reply
        return local_build_reply(venture_id)
    _require_legacy_local_execution()
    adr = locked_adr_for_venture(venture_id)
    build = MEM.get(f"build:{venture_id}") or {}
    wos = build.get("work_orders") or []
    comps: list[str] = []
    for wo in wos:
        comps.extend(wo.get("components") or [])
    ctx = {
        "schema_ok": True,
        "static_findings": [],
        "unit_tests_ok": True,
        "scaffold_ok": True,
        "secret_leaks": [],
        "auth_live_ok": True,
        "components": comps or ["a", "b"],
        "vulns": [],
        "error_boundary": True,
        "migrations_ok": True,
        "author_agent_id": "builder",
        "reviewer_agent_id": "reviewer",
        "peer_verdict": "approve",
        "readiness_score": 0.95,
        "blocking_issues": [],
        "protected_routes": (adr.protected_routes if adr else ["/api/me"]),
    }
    results = run_all(ctx)
    payload = {
        "results": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in results],
        "all_passed": all(r.passed for r in results),
    }
    MEM[f"gates:{venture_id}"] = payload
    if payload["all_passed"]:
        MEM[f"preview:{venture_id}"] = start_preview(5173).__dict__
    return payload


@router.post("/ventures/{venture_id}/build/preview")
def preview(venture_id: str):
    if is_local():
        from app.chat.build_path import local_build_reply
        return local_build_reply(venture_id)
    _require_legacy_local_execution()
    p = start_preview(5173)
    MEM[f"preview:{venture_id}"] = p.__dict__
    return p.__dict__
