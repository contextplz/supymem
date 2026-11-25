"""Small deterministic grammar for founder Slate overrides (doc 79 §5.1)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.will.model import Slate, SlateItem


class SlateEdit(BaseModel):
    op: Literal["keep", "drop", "replace", "insert", "reorder", "modify", "pin", "defer"]
    target_rank: int | None = None
    target_title: str | None = None
    new_title: str | None = None
    new_position: int | None = None
    note: str | None = None


class OverrideResult(BaseModel):
    edits: list[SlateEdit] = Field(default_factory=list)
    resulting_slate: Slate
    interpretation: str
    unclear: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    autopilot_on: bool = False


def _renumber(items: list[SlateItem]) -> list[SlateItem]:
    return [item.model_copy(update={"rank": index}) for index, item in enumerate(items, 1)]


def _insert_item(slate: Slate, title: str) -> SlateItem:
    base = slate.items[-1]
    return base.model_copy(
        update={
            "title": title[:60],
            "why_now": "Founder requested this next.",
            "evidence": ["founder_override"],
            "origin": "founder",
        }
    )


def parse_override(text: str, slate: Slate) -> OverrideResult:
    """Parse the seven S6 day-one phrasings and apply unambiguous edits to a copy."""
    raw = text.strip()
    lowered = raw.lower()
    items = list(slate.items)
    edits: list[SlateEdit] = []
    unclear: list[str] = []

    if re.search(r"\bwhy\s+is\s+(?:item\s+)?2\b", lowered):
        return OverrideResult(
            resulting_slate=slate,
            interpretation="You are asking why item 2 is on the slate, not changing it.",
            unclear=[],
        )
    if lowered == "just handle it":
        return OverrideResult(
            resulting_slate=slate,
            interpretation="You asked autopilot to handle the current slate.",
            autopilot_on=True,
        )

    first_two = re.search(r"do\s+the\s+first\s+two\s+only", lowered)
    if first_two:
        edits = [SlateEdit(op="keep", target_rank=rank) for rank in (1, 2)]
        edits += [SlateEdit(op="defer", target_rank=rank) for rank in (3, 4, 5)]
        return OverrideResult(
            edits=edits,
            resulting_slate=slate,
            interpretation="Keep items 1 and 2 active; defer items 3 through 5.",
        )

    drop_match = re.search(r"drop\s+(\d)\s+and\s+(\d),?\s+add\s+(.+)", lowered)
    if drop_match:
        ranks = (int(drop_match.group(1)), int(drop_match.group(2)))
        additions = re.split(r"\s+and\s+(?:a\s+)?", drop_match.group(3).strip())
        for rank in ranks:
            edits.append(SlateEdit(op="drop", target_rank=rank))
        for title in additions:
            clean = re.sub(r"^(?:a|an)\s+", "", title).strip(" .")
            if clean:
                edits.append(SlateEdit(op="insert", new_title=clean))
        kept = [item for item in items if item.rank not in ranks]
        for edit in [edit for edit in edits if edit.op == "insert"]:
            kept.append(_insert_item(slate, edit.new_title or "Founder task"))
        return OverrideResult(
            edits=edits,
            resulting_slate=slate.model_copy(update={"items": _renumber(kept[:5])}),
            interpretation="Drop the requested items and add the requested experiments.",
        )

    third = re.search(r"(?:the\s+)?third(?:\s+one)?\s+should\s+be\s+(.+?),\s*not\s+(.+)", lowered)
    if third:
        title = third.group(1).strip()
        edits = [SlateEdit(op="modify", target_rank=3, new_title=title, note=raw)]
        items[2] = items[2].model_copy(update={"title": title[:60], "origin": "founder"})
        return OverrideResult(
            edits=edits,
            resulting_slate=slate.model_copy(update={"items": items}),
            interpretation="Replace the third item's focus with your requested direction.",
        )

    pricing_first = re.search(r"do\s+all\s+of\s+them\s+but\s+(.+?)\s+first", lowered)
    if pricing_first:
        needle = pricing_first.group(1)
        aliases = {"pricing": ("pricing", "paid", "offer")}
        terms = aliases.get(needle, (needle,))
        match = next(
            (item for item in items if any(term in item.title.lower() for term in terms)),
            None,
        )
        if match is None:
            unclear.append(f"I could not find an item matching '{needle}'.")
        else:
            items.remove(match)
            items.insert(0, match)
            edits = [SlateEdit(op="reorder", target_title=match.title, new_position=1)]
        return OverrideResult(
            edits=edits,
            resulting_slate=slate.model_copy(update={"items": _renumber(items)}),
            interpretation="Move the requested item to the top.",
            unclear=unclear,
        )

    cold_email = re.search(r"stop\s+the\s+cold\s+email", lowered)
    if cold_email:
        matching = [item for item in items if "cold email" in item.title.lower()]
        edits = [SlateEdit(op="drop", target_title=item.title, note=raw) for item in matching]
        return OverrideResult(
            edits=edits,
            resulting_slate=slate,
            interpretation="Stop cold email and remember that it feels spammy.",
            unclear=[] if matching else ["No cold-email slate item is present to drop."],
        )

    return OverrideResult(
        resulting_slate=slate,
        interpretation="I could not safely turn that into slate edits.",
        unclear=[raw] if raw else ["Please describe the change you want."],
    )


apply_override = parse_override
