"""Interim hosted fence for legacy media execution, including queued aliases."""
from app.worker.handlers import hosted_mode

MESSAGE = "Hosted Media execution needs isolated, tenant-approved processing. Uploads and saved assets remain available. Use Ads for reviewed campaign proposals."


def blocked_outcome():
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import capability

        content = capability("content")
        if content["state"] == "off":
            return {"ok": False, "status": "blocked", "error": "content_disabled",
                    "block_reason": content["next_step"]}
        return None
    if hosted_mode():
        return {"ok": False, "status": "blocked", "error": "hosted_media_unavailable",
                "block_reason": MESSAGE}
    return None
