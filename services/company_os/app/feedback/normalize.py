"""Sanitize untrusted feedback text."""

from __future__ import annotations

import re
import unicodedata
from html import escape

ZERO_WIDTH = re.compile(r"[\u200b\u200c\u200d\ufeff\u2060]")
BIDI = re.compile(r"[\u202a-\u202e\u2066-\u2069]")


def sanitize(text: str, *, max_len: int = 4000) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    t = ZERO_WIDTH.sub("", t)
    t = BIDI.sub("", t)
    return t[:max_len]


def delimit_untrusted(text: str, *, max_len: int = 4000) -> str:
    return f"<untrusted>\n{escape(sanitize(text, max_len=max_len), quote=False)}\n</untrusted>"
