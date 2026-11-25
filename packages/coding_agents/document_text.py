"""Exact, bounded literal for the reviewed TextEdit three-line profile."""
from __future__ import annotations


def textedit_three_lines(value: str) -> str:
    if (not isinstance(value, str) or not 0 < len(value) <= 320
            or any((ord(char) < 32 and char != "\n") or ord(char) == 127
                   or char in "\u2028\u2029" for char in value)):
        raise ValueError("invalid_reviewed_document_text")
    lines = value.split("\n")
    if len(lines) != 3 or any(not line.strip() or line != line.strip() for line in lines):
        raise ValueError("invalid_reviewed_document_text")
    return value  # Validation never rewrites the reviewed literal.


def textedit_typing_only_goal(instruction: str) -> str | None:
    """A narrow quoted typing-only goal; never infer creation, formatting or saving."""
    import re
    if not isinstance(instruction, str) or len(instruction) > 3000:
        return None
    match = re.search(r"\b(?:type|write|enter)\s+(?:exactly|only)\s+([\"'“])(.{1,320}?)([\"'”])", instruction, re.I)
    if match is None or {"\"": "\"", "'": "'", "“": "”"}.get(match[1]) != match[3]:
        return None
    text = match[2]
    if any(ord(char) < 32 or ord(char) == 127 for char in text):
        return None
    # Known request/location words carry no other document-state requirement.
    prefix_words = set(re.findall(r"[a-z0-9]+", instruction[:match.start()].casefold()))
    if not prefix_words <= {"prepare", "a", "an", "mac", "task", "for", "in", "on", "the", "this",
            "selected", "reviewed", "existing", "current", "textedit", "window", "document", "app",
            "application", "please", "and", "then", "open", "installed", "to", "using"}:
        return None
    suffix = instruction[match.end():].strip()
    location = r"(?:\s+(?:in|into|to)\s+(?:(?:the|this|selected|reviewed|existing|current)\s+)*(?:TextEdit\s+)?(?:window|document|text\s+area))?"
    negative = r"(?:\s*[.;,]\s*(?:do\s+not|don't|never)\s+(?:save(?:\s+(?:it|anything|the\s+document))?|press\s+return))?"
    if not re.fullmatch(location + r"\s*[.!;]?" + negative + r"\s*[.!;]?", " " + suffix, re.I):
        return None
    return text
