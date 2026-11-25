"""GOD adjudication: routine stays autonomous; critical escalates to the founder.

Ported from Munder Difflin HIVE.md §6 + the god-agent prompt in ``hive.ts``:
destructive ops, real spend, scope changes, and unresolvable conflicts need a
human; clarifications, data asks, and plan tweaks do not.

MIT: Chaitanya Giri. Do not run vendor/munder-difflin in-process.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

_SPEND = (
    "spend real money",
    "charge the card",
    "increase the budget",
    "buy ads",
    "purchase",
    "wire transfer",
    "refund customers",
)
_DESTRUCTIVE = (
    "delete production",
    "wipe the database",
    "drop table",
    "drop the table",
    "rm -rf",
    "destroy the",
    "format the disk",
    "revoke all",
    "wipe production",
)
_SCOPE = (
    "pivot the product",
    "change the product",
    "new market",
    "kill the company",
    "replace our product",
    "abandon the wedge",
    "scope change",
)
_CONFLICT = (
    "unresolvable",
    "agents disagree",
    "deadlock",
    "cannot agree",
)


@dataclass(frozen=True)
class Verdict:
    kind: str  # routine | critical
    reason: str
    provider: str


def _blob(*parts: Iterable[str] | str) -> str:
    chunks: list[str] = []
    for part in parts:
        if isinstance(part, str):
            chunks.append(part)
        else:
            chunks.extend(str(p) for p in part)
    return " ".join(chunks).lower()


def classify_work(title: str, detail: str = "", tags: Iterable[str] | None = None) -> Verdict:
    """What the GOD agent would escalate vs resolve itself."""
    blob = _blob(title, detail, tags or ())
    if any(needle in blob for needle in _DESTRUCTIVE):
        return Verdict("critical", "destructive_ops", "decision")
    if any(needle in blob for needle in _SPEND):
        return Verdict("critical", "spend", "decision")
    if any(needle in blob for needle in _SCOPE):
        return Verdict("critical", "scope_change", "decision")
    if any(needle in blob for needle in _CONFLICT):
        return Verdict("critical", "unresolvable_conflict", "decision")
    return Verdict("routine", "autonomous", "god")


def classify_message(msg: dict) -> Verdict:
    """Router/god may flip ``needs_human``; hop-cap leftovers are conflicts."""
    if msg.get("needs_human"):
        return Verdict("critical", "flagged_needs_human", "decision")
    hops = int(msg.get("hops") or 0)
    from app.munder.mailbox import HOP_CAP

    if hops > HOP_CAP:
        return Verdict("critical", "hop_cap", "decision")
    return classify_work(str(msg.get("subject") or ""), str(msg.get("body") or ""))
