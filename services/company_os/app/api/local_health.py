"""Local setup status from the stores used by your local services."""

import os

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from cryptography.exceptions import InvalidTag

from app.core.local_mode import is_local

router = APIRouter(tags=["local"])


@router.get("/local/diagnostics/search", include_in_schema=False)
def local_search_diagnostics(request: Request) -> JSONResponse:
    if not is_local() or os.environ.get("SUPYMEM_DEV_DIAGNOSTICS") != "1":
        raise HTTPException(404, "not_found")
    from app.core.local_owner import request_owner
    from app.health import search_diagnostics

    return JSONResponse(search_diagnostics(request_owner(request).id), headers={"Cache-Control": "no-store"})


@router.get("/local/health")
def local_health() -> JSONResponse:
    if not is_local():
        raise HTTPException(404, detail={"code": "not_found",
            "message": "You can check service status at /health."})
    from app.api.local_settings import get_settings
    from app.core.providers_local import configured_providers
    from app.gtm.local_mail import configured
    from app.health import _probe_database
    from app.worker.health import healthy

    database, _ = _probe_database()
    worker_ok = healthy()
    try:
        providers = configured_providers()
        model_ok = bool(providers["providers"].get(providers["provider"], {}).get("configured"))
        search = get_settings("search")
        mail_ok = configured() and get_settings("smtp")["state"] == "ready"
        search_ok = search["state"] == "ready"
    except (OSError, ValueError, InvalidTag, HTTPException):
        model_ok = search_ok = mail_ok = False
        search = {"next_step": "Open Keys and connections, check your data folder permissions and restore your saved settings and encryption key from your backup, then restart your app."}
    try:
        from app.core.local_capabilities import capability
    except ImportError:
        research = {"state": "ready" if search_ok else "needs_setup", "next_step": search["next_step"]}
        mail = {"state": "ready" if mail_ok else "needs_setup", "next_step": "Open Keys and connections and add your Outgoing mail host, Mailbox address and Mail app password under Your mail account."}
    else:
        research = capability("research")
        mail = capability("send_email")
    return JSONResponse({
        "database": {"ok": database.ok, "next_step": "You can continue." if database.ok else "Restart your local app. If your data folder cannot be opened, restore it from your backup on Keys and connections."},
        "worker": {"ok": worker_ok, "next_step": "You can continue." if worker_ok else "Restart your local app to start your background worker, then check this screen again."},
        "provider_keys": {"model": model_ok, "search": search_ok, "mail": mail_ok},
        "reader": {"status": "configured" if research["state"] == "ready" else "disabled" if research["state"] == "off" else "not_configured",
                   "next_step": "Your built-in reader is ready. You can ask for research." if research["state"] == "ready" else research["next_step"]},
        "mail": {"status": "configured" if mail["state"] == "ready" else "disabled" if mail["state"] == "off" else "not_configured",
                 "next_step": "Your mail account is ready. You can ask to send mail." if mail["state"] == "ready" else mail["next_step"]},
    }, headers={"Cache-Control": "no-store"})
