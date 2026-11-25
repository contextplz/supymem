"""Memory condensation — pinned / condensed / recent. Never lose pinned lines.

Ported from Munder Difflin ``src/main/reflect.ts`` (parseMemory, mergePinned,
rebuild, verify). The LLM summarizer is replaced with an extractive fallback so
we do not spawn their headless Claude session.

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

from dataclasses import dataclass, field

PINNED_HEADING = "## 📌 Durable facts (pinned — never condensed)"
CONDENSED_HEADING = "## 🗜 Condensed history"
RECENT_HEADING = "## Recent"

BUDGET_BYTES = 131_072


@dataclass
class Section:
    heading: str
    body: str


@dataclass
class Parsed:
    header: str
    pinned: str | None
    condensed: str | None
    recent: list[Section] = field(default_factory=list)


def count_sections(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.startswith("## "))


def parse_memory(text: str) -> Parsed:
    lines = text.split("\n")
    first = next((i for i, line in enumerate(lines) if line.startswith("## ")), len(lines))
    header = "\n".join(lines[:first]).rstrip()
    sections: list[Section] = []
    cur: Section | None = None
    for line in lines[first:]:
        if line.startswith("## "):
            if cur:
                sections.append(cur)
            cur = Section(heading=line, body="")
        elif cur:
            cur.body = f"{cur.body}\n{line}" if cur.body else line
    if cur:
        sections.append(cur)

    pinned = None
    condensed = None
    recent: list[Section] = []
    for section in sections:
        heading = section.heading.strip()
        if heading.startswith("## 📌"):
            pinned = section.body.rstrip()
        elif heading.startswith("## 🗜"):
            condensed = section.body.rstrip()
        elif heading == RECENT_HEADING:
            continue
        else:
            recent.append(section)
    return Parsed(header=header, pinned=pinned, condensed=condensed, recent=recent)


def pinned_lines(pinned: str | None) -> list[str]:
    if not pinned:
        return []
    return [line.strip() for line in pinned.split("\n") if line.strip()]


def merge_pinned(old_lines: list[str], hoist: list[str]) -> list[str]:
    have = set(old_lines)
    out = list(old_lines)
    for raw in hoist:
        line = (raw or "").strip()
        if line and line not in have:
            have.add(line)
            out.append(line)
    return out


def rebuild(header: str, pinned: list[str], condensed: str, keep: list[Section]) -> str:
    parts: list[str] = []
    if header.strip():
        parts.append(header.strip())
    parts.append(PINNED_HEADING)
    parts.append("\n".join(pinned) if pinned else "_(none yet)_")
    parts.append(CONDENSED_HEADING)
    parts.append(condensed.strip())
    parts.append(RECENT_HEADING)
    for section in keep:
        parts.append(f"{section.heading}\n{section.body}".rstrip())
    return "\n\n".join(parts) + "\n"


def verify_condense(
    *,
    rebuilt: str,
    new_bytes: int,
    old_bytes: int,
    old_pinned_lines: list[str],
    merged_pinned: list[str],
    condensed: str,
    keep: list[Section],
) -> tuple[bool, str]:
    parsed = parse_memory(rebuilt)
    if parsed.pinned is None or parsed.condensed is None:
        return False, "structure-missing-region"
    if new_bytes <= 200:
        return False, "too-small"
    if not condensed.strip():
        return False, "empty-condensed"
    if not (new_bytes < old_bytes * 0.95):
        return False, "not-smaller"
    new_pinned = set(pinned_lines(parsed.pinned))
    for line in old_pinned_lines:
        if line not in new_pinned:
            return False, "pinned-line-dropped"
    for line in merged_pinned:
        if line not in new_pinned:
            return False, "pinned-merge-mismatch"
    if len(parsed.recent) != len(keep):
        return False, "recent-count-mismatch"
    for i, left in enumerate(keep):
        right = parsed.recent[i]
        a = f"{left.heading}\n{left.body}".rstrip()
        b = f"{right.heading}\n{right.body}".rstrip()
        if a != b:
            return False, "recent-section-altered"
    return True, "ok"


def extractive_summarize(
    condensed: str | None, evict: list[Section], pinned: str | None
) -> dict[str, list[str] | str]:
    """Deterministic stand-in for the headless Haiku pass."""
    parts: list[str] = []
    if condensed and condensed.strip():
        parts.append(condensed.strip()[:1200])
    hoist: list[str] = []
    markers = ("decided", "must ", "never ", "always ", "sha", "path")
    for section in evict:
        heading = section.heading.lstrip("#").strip()
        first = (section.body.strip().split("\n") or [""])[0][:200]
        parts.append(f"{heading}: {first}")
        for line in section.body.split("\n"):
            low = line.lower()
            if line.strip().startswith(("- ", "* ")) and any(m in low for m in markers):
                hoist.append(line.strip().lstrip("-* ").strip())
    text = "\n".join(parts).strip() or "No older history."
    return {"condensed": text[:4000], "hoist": hoist[:20]}


def condense_memory(text: str, *, recent_keep: int = 3) -> dict[str, object]:
    """Backup-free in-memory condense with the verify-don't-trust gate.

    Callers who persist to disk should copy the original first (reflect.ts
    backup-first). We refuse the rewrite unless every check holds.
    """
    old_bytes = len(text.encode("utf-8"))
    parsed = parse_memory(text)
    keep_count = max(1, recent_keep)
    keep = parsed.recent[-keep_count:]
    evict = parsed.recent[: max(0, len(parsed.recent) - keep_count)]
    if not evict:
        return {"condensed": False, "reason": "nothing-to-evict", "text": text, "oldBytes": old_bytes}

    summary = extractive_summarize(parsed.condensed, evict, parsed.pinned)
    old_pinned = pinned_lines(parsed.pinned)
    merged = merge_pinned(old_pinned, list(summary["hoist"]))
    rebuilt = rebuild(parsed.header, merged, str(summary["condensed"]), keep)
    new_bytes = len(rebuilt.encode("utf-8"))
    ok, reason = verify_condense(
        rebuilt=rebuilt,
        new_bytes=new_bytes,
        old_bytes=old_bytes,
        old_pinned_lines=old_pinned,
        merged_pinned=merged,
        condensed=str(summary["condensed"]),
        keep=keep,
    )
    if not ok:
        return {
            "condensed": False,
            "reason": reason,
            "text": text,
            "oldBytes": old_bytes,
            "newBytes": new_bytes,
        }
    return {
        "condensed": True,
        "reason": "condensed",
        "text": rebuilt,
        "oldBytes": old_bytes,
        "newBytes": new_bytes,
        "evicted": len(evict),
        "kept": len(keep),
    }
