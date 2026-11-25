"""Parse founder chat into work that actually mutates the venture.

Keyword lookups live in intent.py. This module applies the instruction:
rewrite landing HTML, remember a competitor list, draft an email without
sending, or cancel in-flight generic work.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

_HEADLINE_RE = re.compile(
    r"(?:(?:rewrite|update|change|set|make)\s+(?:the\s+)?)?"
    r"(?:landing\s+)?(?:page\s+)?headline\s*(?:to|:|—|-|=|is)\s+(.+)",
    re.I | re.S,
)
_COMPETITORS_RE = re.compile(
    r"(?:(?:our|the)\s+)?competitors?\s+(?:are|is|:|aren'?t|are not)\s+(.+)",
    re.I | re.S,
)
_EMAIL_TO_RE = re.compile(
    r"draft\b.{0,40}?\b(?:email|note|message|outreach)\s+to\s+([A-Za-z][\w'-]*)",
    re.I,
)
# "draft Priya note" / "draft Maya an email", name first, then the artifact.
# Skip articles so "draft an email" is not to="an".
_DRAFT_NAMED_NOTE_RE = re.compile(
    r"\bdraft\s+(?!an?\b|the\b|this\b|that\b)([A-Za-z][\w'-]*)\s+(?:an?\s+)?(?:email|note|message)\b",
    re.I,
)
_DRAFT_BARE_RE = re.compile(r"\bdraft\s+(?:an?\s+)?(?:email|note|message)\b", re.I)
_WAITLIST_ONLY_RE = re.compile(r"\b(waitlist[\s-]*only|only\s+(?:a\s+|the\s+)?waitlist)\b", re.I)
_DO_NOT_SEND_RE = re.compile(
    r"\b(?:do not|don'?t)\b.{0,120}\b(?:contact|send|publish|post|message|email)\b",
    re.I | re.S,
)
_OUTREACH_PREPARATION_RE = re.compile(
    r"\b(?:prepare|plan|research|compare|review|draft|options?|strategy|channels?)\b",
    re.I,
)
_OUTREACH_SCOPE_RE = re.compile(
    r"\b(?:gtm|outreach|email|social|channels?|posts?|publish|contact)\b",
    re.I,
)
_REMOVE_QUIZ_RE = re.compile(r"\bremove (?:the )?(?:quiz(?: line| leak)?)\b", re.I)
_NEXT_CLAUSE = re.compile(
    r",\s*(?:competitors?|draft|rewrite|update the landing|waitlist|do not|don't|and\s+draft)\b|"
    r"[.!?]\s*(?:remove (?:the )?quiz|(?:landing\s+)?headline)\b|"
    r"\s+(?:landing\s+)?headline\s*(?:to|:|—|-|=|is)\b",
    re.I,
)


def _cut_clause(text: str) -> str:
    raw = (text or "").strip()
    hit = _NEXT_CLAUSE.search(raw)
    if hit:
        raw = raw[: hit.start()]
    return raw.strip(" .,")


def _split_names(blob: str) -> list[str]:
    cleaned = re.sub(r"\b(not|the|nyt|nytimes|new york times)\b", " ", blob, flags=re.I)
    parts = re.split(r"\s*(?:,|;| and |\+|\/)\s*", cleaned)
    names: list[str] = []
    for part in parts:
        name = re.sub(r"\s+", " ", part).strip(" .")
        name = re.sub(r"^(and|or)\s+", "", name, flags=re.I).strip()
        if len(name) >= 2 and name.lower() not in {"not", "and", "or"}:
            names.append(name[:80])
    return names[:8]


@dataclass
class SteerOp:
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)


def parse_steer(text: str) -> list[SteerOp]:
    raw = (text or "").strip()
    if not raw:
        return []
    ops: list[SteerOp] = []

    headline_hit = _HEADLINE_RE.search(raw)
    if headline_hit:
        headline = _cut_clause(headline_hit.group(1))
        if headline:
            ops.append(SteerOp("site_copy", {"headline": headline[:90]}))

    if _WAITLIST_ONLY_RE.search(raw):
        existing = next((op for op in ops if op.kind == "site_copy"), None)
        if existing:
            existing.payload["waitlist_only"] = True
        else:
            ops.append(SteerOp("site_copy", {"waitlist_only": True}))

    comp_hit = _COMPETITORS_RE.search(raw)
    if comp_hit:
        names = _split_names(_cut_clause(comp_hit.group(1)))
        if names:
            ops.append(SteerOp("remember_competitors", {"names": names}))

    email_hit = _EMAIL_TO_RE.search(raw)
    named_hit = None if email_hit else _DRAFT_NAMED_NOTE_RE.search(raw)
    if email_hit or named_hit:
        who = (email_hit or named_hit).group(1)
        ops.append(
            SteerOp(
                "draft_email",
                {
                    "to": who,
                    "send": False,
                    "body": raw,
                },
            )
        )
    elif is_non_delivery_outreach(raw):
        # Planning/comparison work belongs in the regular task runner, which
        # saves a sourced brief. It is not an email draft merely because the
        # founder explicitly prohibited sending one.
        pass
    elif _DRAFT_BARE_RE.search(raw) and not re.search(r"\bfounder outreach\b", raw, re.I):
        ops.append(SteerOp("draft_email", {"to": "founder", "send": False, "body": raw}))

    if _REMOVE_QUIZ_RE.search(raw):
        existing = next((op for op in ops if op.kind == "site_copy"), None)
        if existing:
            existing.payload["strip_quiz"] = True
        else:
            ops.append(SteerOp("site_copy", {"strip_quiz": True}))

    from app.chat.intent import looks_like_cancel

    if looks_like_cancel(raw):
        ops.append(SteerOp("cancel", {"query": raw}))

    return ops


def leftover_after_steer(text: str) -> str:
    """Text that parse_steer did not consume. Questions here must not become tasks."""
    if is_non_delivery_outreach(text):
        # Preserve the negative delivery scope with the preparation request.
        # Removing it would turn safe planning into apparent live outreach.
        return (text or "").strip()
    leftover = text or ""
    leftover = _HEADLINE_RE.sub(" ", leftover)
    leftover = _COMPETITORS_RE.sub(" ", leftover)
    leftover = _EMAIL_TO_RE.sub(" ", leftover)
    leftover = _DRAFT_NAMED_NOTE_RE.sub(" ", leftover)
    leftover = _DRAFT_BARE_RE.sub(" ", leftover)
    leftover = _WAITLIST_ONLY_RE.sub(" ", leftover)
    leftover = _DO_NOT_SEND_RE.sub(" ", leftover)
    leftover = _REMOVE_QUIZ_RE.sub(" ", leftover)
    leftover = re.sub(r"\s+", " ", leftover).strip(" .,;:-")
    leftover = re.sub(r"^(and|also|then|please|plus)\s+", "", leftover, flags=re.I)
    return leftover.strip()


def is_site_copy_work(title: str, detail: str = "", tags: list | None = None) -> bool:
    blob = f"{title} {detail} {' '.join(str(t) for t in (tags or []))}".lower()
    needles = (
        "headline",
        "hero",
        "waitlist only",
        "first look",
        "site copy",
        "rewrite the page",
        "rewrite the landing",
        "update the landing",
        "change the landing",
    )
    return any(needle in blob for needle in needles)


def is_private_email_draft(title: str, detail: str = "") -> bool:
    blob = f"{title} {detail}".lower()
    if is_non_delivery_outreach(title, detail):
        return False
    if re.search(r"\b(tweet|twitter|linkedin|instagram|facebook|post to|publish this)\b", blob):
        return False
    if re.search(r"\bfounder outreach\b", blob) and not _DO_NOT_SEND_RE.search(blob):
        return False
    if _EMAIL_TO_RE.search(blob) or _DRAFT_NAMED_NOTE_RE.search(blob) or _DRAFT_BARE_RE.search(blob):
        return True
    if ("email" in blob or "outreach" in blob) and _DO_NOT_SEND_RE.search(blob):
        return True
    return False


_REVIEW_DRAFT_RE = re.compile(r"^\W*(?:(?:go|ok(?:ay)?|yes|please|now|so|then)\b\W*)*(?:draft|write|prepare|compose)\b", re.I)
_DRAFT_SUBJECT_RE = re.compile(
    r"\b(?:e-?mails?|outreach|messages?|dms?|notes?|posts?|tweets?|invitations?|invites?|pitch|sequence)\b", re.I)


_NOT_A_DRAFT_RE = re.compile(r"\b(?:channels?|options?|strategy|compare|research|plan)\b", re.I)


def is_review_draft(title: str, detail: str = "") -> bool:
    """A request to WRITE outreach for the founder to review. Writing needs no posting or sending
    connection; only actually sending or posting does, and that stays a separate, confirmed step."""
    if is_non_delivery_outreach(title) or _NOT_A_DRAFT_RE.search(title or ""):
        return False  # channel/strategy preparation is research, handled by the regular task runner
    return bool(_REVIEW_DRAFT_RE.match(title or "") and _DRAFT_SUBJECT_RE.search(title or ""))


def is_non_delivery_outreach(title: str, detail: str = "") -> bool:
    """True for GTM preparation that explicitly forbids external delivery.

    A founder can ask Supymem to research, compare, plan, or draft channels
    without connecting a sending provider. Concrete named-recipient email
    drafts keep their existing local-draft workflow.
    """
    blob = f"{title} {detail}".strip()
    if not (
        _DO_NOT_SEND_RE.search(blob)
        and _OUTREACH_PREPARATION_RE.search(blob)
        and _OUTREACH_SCOPE_RE.search(blob)
    ):
        return False
    if _EMAIL_TO_RE.search(blob) or _DRAFT_NAMED_NOTE_RE.search(blob):
        return False
    return True


async def apply_steer(venture_id: str, text: str) -> dict[str, Any]:
    """Execute parsed ops. Returns founder-facing lines + whether work landed."""
    ops = parse_steer(text)
    kind_order = {"cancel": 0, "remember_competitors": 1, "site_copy": 2, "draft_email": 3}
    ops = sorted(ops, key=lambda op: kind_order.get(op.kind, 9))
    lines: list[str] = []
    citations: list[dict[str, str]] = []
    applied: list[str] = []
    site_changed = False

    for op in ops:
        if op.kind == "site_copy":
            from app.firstlook.service import FirstLookError, apply_founder_copy

            try:
                view = await apply_founder_copy(
                    venture_id,
                    headline=op.payload.get("headline"),
                    waitlist_only=bool(op.payload.get("waitlist_only")),
                    brief=text,
                    competitors=founder_competitors(venture_id) or None,
                )
            except FirstLookError as exc:
                lines.append(exc.message)
                if exc.code == "coding_tool_required":
                    citations.append({"label": "Connect your coding tool", "href": "/settings/local"})
                continue
            from app.core.local_mode import is_local
            if is_local() and view.get("id") and view.get("state"):
                applied.append("site_copy")
                lines.append("Your coding tool is working on your requested page changes. Open Builds to check its progress.")
                citations.append({"label": "Builds", "href": view["review_path"]})
                continue
            if view and view.get("changed"):
                site_changed = True
                applied.append("site_copy")
                headline = view.get("headline") or op.payload.get("headline") or "the landing"
                url = view.get("url") or ""
                lines.append(f"Updated the landing page draft headline to **{headline}**." if op.payload.get("headline")
                             else "Updated the landing page draft to say it is waitlist only.")
                if url:
                    citations.append({"label": "Site", "href": url})
            elif view and view.get("error"):
                lines.append(str(view["error"]))
            else:
                lines.append(
                    "I did not mark the landing rewrite done, the served HTML did not change."
                )
        elif op.kind == "remember_competitors":
            names = remember_competitors(venture_id, list(op.payload.get("names") or []))
            await _persist_competitor_memory(venture_id, names)
            applied.append("remember_competitors")
            lines.append("Remembered competitors: " + ", ".join(f"**{n}**" for n in names) + ".")
        elif op.kind == "draft_email":
            path = write_email_draft(
                venture_id,
                to=str(op.payload.get("to") or "founder"),
                brief=str(op.payload.get("body") or text),
            )
            applied.append("draft_email")
            who = op.payload.get("to") or "the recipient"
            from app.planning.store import complete_matching_draft_tasks

            closed = complete_matching_draft_tasks(venture_id, str(who))
            lines.append(
                f"Drafted an email to **{who}** at `{path}`. Draft only, nothing went out."
            )
            if closed:
                lines.append(
                    f"Closed {len(closed)} matching draft task(s) that were waiting on this note."
                )
        elif op.kind == "cancel":
            from app.planning.store import cancel_matching_work

            cancelled = cancel_matching_work(venture_id, str(op.payload.get("query") or text))
            applied.append("cancel")
            if cancelled:
                titles = ", ".join(f"**{row['title']}**" for row in cancelled[:4])
                lines.append(f"Parked {len(cancelled)} item(s): {titles}.")
            else:
                lines.append("Nothing matching was open to park.")

    return {
        "ops": ops,
        "applied": applied,
        "lines": lines,
        "citations": citations,
        "site_changed": site_changed,
        "handled": bool(applied or lines),
        "leftover": leftover_after_steer(text),
    }


def remember_competitors(venture_id: str, names: list[str]) -> list[str]:
    from app.state import MEM

    clean = [n.strip() for n in names if str(n).strip()][:8]
    MEM[f"founder_competitors:{venture_id}"] = clean
    return clean


async def _persist_competitor_memory(venture_id: str, names: list[str]) -> None:
    try:
        from app.ventures.founder_memory import remember

        await remember(
            venture_id=venture_id,
            fact="Competitors are: " + ", ".join(names),
            provenance="founder chat",
            kind="fact",
            entity="competitors",
        )
    except Exception as exc:  # noqa: BLE001, memory is best-effort
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "steer.competitor_memory")


def founder_competitors(venture_id: str) -> list[str]:
    from app.state import MEM

    rows = MEM.get(f"founder_competitors:{venture_id}") or []
    return [str(n) for n in rows if str(n).strip()]


def write_email_draft(venture_id: str, *, to: str, brief: str) -> str:
    from app.config import data_root

    slug = re.sub(r"[^a-z0-9]+", "-", (to or "founder").lower()).strip("-") or "founder"
    path = data_root() / "ventures" / venture_id / "repo" / "drafts" / f"email-to-{slug}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    comps = founder_competitors(venture_id)
    market = f"Competitors the founder named: {', '.join(comps)}." if comps else ""
    path.write_text(
        f"# Draft email to {to}\n\n"
        "Status: draft, not sent\n\n"
        f"{brief.strip()}\n\n"
        f"{market}\n".rstrip()
        + "\n",
        encoding="utf-8",
    )
    return str(path.relative_to(data_root()))
