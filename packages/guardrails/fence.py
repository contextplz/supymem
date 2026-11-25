"""Wrap tool/memory/peer payloads so the model cannot treat them as instructions."""

from __future__ import annotations

import re

_SOURCE_OK = re.compile(r"^[a-z0-9_.:/-]{1,80}$", re.IGNORECASE)
_UNTRUSTED_TAG = re.compile(r"<\s*(/?)\s*(untrusted)(?=[\s/>])", re.IGNORECASE)


def fence_untrusted(text: str, *, source: str) -> str:
    src = source if _SOURCE_OK.match(source or "") else "unknown"
    body = _UNTRUSTED_TAG.sub(r"<\1 \2", text or "")
    return f'<untrusted source="{src}">\n{body}\n</untrusted>'
