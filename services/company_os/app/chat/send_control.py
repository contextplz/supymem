"""Founder commands that revoke the venture's automatic sending permission."""
from __future__ import annotations

import re
from typing import Any

_OFF = re.compile(
    r"^\s*(?:please\s+)?(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?(?:"
    r"(?:turn|switch|set)\s+(?:off\s+(?:the\s+|my\s+|automatic\s+)?sending|(?:the\s+|my\s+|automatic\s+)?sending\s+off)"
    r"|(?:stop|disable|pause)\s+(?:(?:all|automatic|autonomous)\s+)?(?:sending(?:\s+(?:e-?mails?|messages?|outreach))?|(?:e-?mail\s+)?outreach)"
    r"|(?:do\s+not|don['’]?t|dont|never)\s+send\s+(?:any\s+)?(?:e-?mails?|messages?|outreach)\s+(?:anymore|any\s+more|again|from\s+now\s+on)"
    r")(?:\s+(?:now|please|anymore|any\s+more|from\s+now\s+on))*\s*[.!?]*\s*$",
    re.I,
)


def stop_sending_request(text: str) -> bool:
    """An explicit ongoing stop command, never a question, quotation or draft's no-send constraint."""
    return bool(_OFF.fullmatch(text or ""))


def stop_sending(venture_id: str) -> dict[str, Any]:
    from app.autopilot import service
    from app.needs_you.readiness import allow_send_card

    service.update_settings(venture_id, allow_send=False, send_max_per_day=0)
    return {"kind": "text", "citations": [],
            "body_md": "Automatic sending is off for this venture. The daily send allowance is 0; new outreach stays in drafts. "
                       "A message already submitted to the email provider may still be delivered.",
            "actions": [allow_send_card("this venture")]}
