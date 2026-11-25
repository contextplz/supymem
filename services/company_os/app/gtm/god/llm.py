"""GTM JSON generation through the existing metered workspace provider adapter."""
from __future__ import annotations

import json
import os
import re
from typing import Any


def llm_enabled() -> bool:
    from app.core.local_mode import is_test_mode, is_local, is_offline
    if is_local():
        return not is_offline()
    return not is_test_mode() and not any(os.getenv(key, "").strip().lower() in {"1", "true", "yes"}
                   for key in ("SANDBOX_MODE", "LLM_OFFLINE"))


def json_chat(*, purpose: str, system: str, user: str,
              workspace_id: str | None = None, response_schema: dict | None = None) -> dict[str, Any] | None:
    if not llm_enabled() or not workspace_id:
        return None
    from app.gtm.supymem.draft import _canonical_complete

    raw, receipt = _canonical_complete(
        user, workspace_id=workspace_id, purpose=purpose, system=system,
        max_tokens=4096, response_schema=response_schema or {"type": "object"},
    )
    if receipt.get("status") != "llm_completed":
        return None
    parsed = _extract_json(raw or "")
    return parsed if isinstance(parsed, dict) else None


def _extract_json(text: str) -> Any:
    body = (text or "").strip()
    if not body:
        return None
    if body.startswith("```"):
        body = body.split("```", 2)[1] if "```" in body[3:] else body.strip("`")
        body = body.removeprefix("json").strip()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", body, flags=re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
