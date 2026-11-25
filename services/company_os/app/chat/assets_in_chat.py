"""Known-person saves, contact-list imports and existing-website reviews in chat.

Both reuse the exact functions behind the GTM import and Website pages, so the same validations,
permissions and lawful-basis rules apply. Saving a person does not authorize outreach.
"""
from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from fastapi import HTTPException

_EMAIL = re.compile(r"[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
_LINKEDIN = re.compile(r"https://(?:www\.)?linkedin\.com/in/", re.I)
# Lines with an "@" also carry mistyped addresses, so the preview can flag them instead of dropping them.
_CONTACT_LINE = lambda line: bool(re.search(r"\S@", line) or _LINKEDIN.search(line))  # noqa: E731


def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


def pasted_contacts(text: str) -> tuple[str, str] | None:
    """(contacts text, format) when a chat message carries a contact list, else None.

    An explicit one-person add request uses the validated person parser. A CSV needs a heading row
    naming an email or LinkedIn column; a plain paste needs two or more lines with an address.
    A single email in another instruction ("email bob@x.com about...") is never a list.
    """
    from app.intake.contacts import _ADD_PERSON_RE

    if _ADD_PERSON_RE.fullmatch(text):
        return text, "paste"
    lines = [line for line in text.splitlines() if line.strip()]
    for index, line in enumerate(lines):
        cells = [c.strip().lower() for c in re.split(r"[,;\t]", line)]
        if len(cells) > 1 and any(c in {"email", "email address", "work email", "linkedin", "linkedin url"} for c in cells):
            rows = lines[index:]
            return ("\n".join(rows), "csv") if len(rows) > 1 else None
    contact_lines = [line.strip().lstrip("-*• ").strip() for line in lines if _CONTACT_LINE(line)]
    if sum(bool(_EMAIL.search(line) or _LINKEDIN.search(line)) for line in contact_lines) >= 2 \
            and len(contact_lines) * 2 >= len(lines) - 1:
        return "\n".join(contact_lines), "paste"
    return None


def ask_for_contacts(text: str) -> dict[str, Any]:
    """First card: preview right away when the list is in the message, else ask for a paste or CSV."""
    found = pasted_contacts(text)
    action = {"label": "Import contacts", "action": "contacts.preview", "style": "primary"}
    if found:
        return _reply(f"I found {len(found[0].splitlines()) - (found[1] == 'csv')} contact rows in your message. "
                      "Tap Import contacts to preview them and record your outreach permission.",
                      [{**action, "payload": {"text": found[0], "format": found[1]}}])
    return _reply("Paste your contacts here, one per line (like Jane Doe <jane@acme.com>), or a CSV with a heading row. "
                  "You can also attach a CSV with the paperclip and tap Import contacts. "
                  "You get a preview first and supply your outreach permission before import.", [action])


def _latest_csv(venture_id: str) -> str | None:
    """Text of the newest CSV attached in this venture's chat (attachments are saved as founder evidence)."""
    from sqlalchemy import select

    from app.db.models import VentureDocument
    from app.intake.evidence import KIND
    from app.state import db

    with db()() as session:
        rows = session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id, VentureDocument.kind == KIND)).all()
    files: dict[str, list] = {}
    for row in rows:
        meta = json.loads(row.provenance_json or "{}")
        if meta.get("source_kind") == "file" and str(row.title or "").lower().endswith(".csv"):
            files.setdefault(meta["evidence_id"], []).append((meta.get("chunk", 0), row.created_at, row.body_md or ""))
    if not files:
        return None
    newest = max(files.values(), key=lambda chunks: max(c[1] for c in chunks))
    return "".join(body for _, _, body in sorted(newest, key=lambda c: c[0]))


def _workspace(venture_id: str) -> str:
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        venture = session.get(Venture, venture_id)
        return venture.workspace_id if venture else ""


async def preview(venture_id: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    from app.intake import contacts

    text, fmt = str(payload.get("text") or ""), payload.get("format")
    if not text:
        text, fmt = _latest_csv(venture_id) or "", "csv"
    if not text.strip():
        return ask_for_contacts("")
    if fmt not in {"csv", "paste"}:
        fmt = "csv"
    try:
        person = contacts.described_person(text)
        if person is not None:
            result = await contacts.add_person(_workspace(venture_id), venture_id, person, actor=actor)
            body = f"You {'saved' if result['added'] else 'already saved'} {result['email']}. Nothing was sent. You can ask for an email to them."
            return _reply(body, [{"label": "Show your saved customers", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}])
        view = await contacts.preview(_workspace(venture_id), venture_id, text, format=fmt, mapping=None, actor=actor)
    except contacts.ContactImportError as error:
        if re.match(r"\s*add\s+someone\s+i\s+know\s*:", text, re.IGNORECASE):
            return _reply(error.message)
        return _reply(f"I could not read those contacts: {error.message} Nothing was imported.",
                      [{"label": "Import contacts", "action": "contacts.preview", "style": "ghost"}])
    except (ValueError, HTTPException) as error:
        if isinstance(error, HTTPException) and error.status_code not in {403, 404, 423}:
            raise
        return _reply("Your import did not finish. Ask your company owner to check your access, then review your saved contacts before trying again.")
    return preview_card(view)


def preview_card(view: dict[str, Any]) -> dict[str, Any]:
    rows, ready = view["rows"], view["valid_rows"]
    mapped = ", ".join(f"{field.replace('_', ' ')} from \"{column}\"" for field, column in (view.get("mapping") or {}).items() if column)
    lines = [f"**Contact preview**: {view['total_rows']} rows, **{ready} ready to import**"
             + (f", {view['duplicates']} already saved" if view["duplicates"] else "")
             + (f", {view['invalid_rows']} need fixes" if view["invalid_rows"] else "") + "."]
    if mapped:
        lines.append(f"Columns: {mapped}.")
    sample = [r for r in rows if not r["errors"] and not r["duplicate"]][:5]
    if sample:
        lines.append("\n".join(f"{i}. {r['name'] or 'No name'}, {r['email'] or r['linkedin_url']}"
                               + (f", {r['company']}" if r["company"] else "") for i, r in enumerate(sample, 1))
                     + (f"\n...and {ready - len(sample)} more" if ready > len(sample) else ""))
    warnings = [f"Row {r['row_number']}: {r['errors'][0]}" for r in rows if r["errors"]][:3]
    if warnings:
        lines.append("Skipped:\n" + "\n".join(warnings))
    if not ready:
        if view["duplicates"] and not view["invalid_rows"]:
            lines.append("\n".join(f"{row['name'] or 'Your saved contact'}, {row['email'] or row['linkedin_url']}"
                                   for row in rows[:5]))
            return _reply("\n\n".join(lines) + "\n\nYou already saved these contacts. Review your customers in the company where you added them. Nothing was sent.",
                          [{"label": "Show your saved customers", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}])
        return _reply("\n\n".join(lines) + "\n\nNothing can be imported yet. Fix the rows and paste them again.",
                      [{"label": "Import contacts", "action": "contacts.preview", "style": "ghost"}])
    return _reply("\n\n".join(lines) + "\n\nWhat permission do you have to contact these people? "
                  "Choose their consent or your legitimate interest to save the valid contacts. Nothing is sent.",
                  [{"label": label, "action": "contacts.import", "payload": {"preview_id": view["preview_id"],
                    "lawful_basis_attested": True, "lawful_basis": basis}, "style": "primary"}
                   for basis, label in (("consent", "You have their consent"),
                                        ("legitimate_interest", "You have a legitimate interest"))])


def commit(venture_id: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    from app.intake import contacts

    try:
        result = contacts.commit(_workspace(venture_id), venture_id, str(payload.get("preview_id") or ""), actor=actor,
                                 lawful_basis_attested=payload.get("lawful_basis_attested") is True,
                                 lawful_basis=str(payload.get("lawful_basis") or ""))
    except contacts.ContactImportError as error:
        return _reply(f"{error.message} Nothing new was imported.",
                      [{"label": "Import contacts", "action": "contacts.preview", "style": "primary"}])
    except (ValueError, HTTPException) as error:
        if isinstance(error, HTTPException) and error.status_code not in {403, 404, 423}:
            raise
        return _reply("Your import did not finish. Ask your company owner to check your access, then review your saved contacts before trying again.")
    return _reply(f"Imported **{result['imported']}** contact{'s' if result['imported'] != 1 else ''}"
                  + (f", skipped {result['skipped']}" if result.get("skipped") else "") + ". Nothing was sent. You can ask for an email to someone you choose.",
                  [{"label": "Show customers and drafts", "action": "gtm.status", "payload": {"show_saved": True}, "style": "primary"}])


async def review_site(venture_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    from app.intake.assets import import_site

    url = str(payload.get("url") or "")
    try:
        site = (await asyncio.to_thread(import_site, venture_id, url))["site"]
    except ValueError as error:
        return _reply(f"I could not review that website: {error}")
    audit = site.get("audit")
    if site.get("status") != "verified" or not isinstance(audit, dict):
        return _reply(f"I saved {site['url']} but could not read the page. {site.get('note') or ''}".strip(),
                      [{"label": "Try again", "action": "website.review", "payload": {"url": site["url"]}, "style": "ghost"}])
    status = lambda value: "yes" if value == "present" else "not found" if value == "missing" else "not checked"  # noqa: E731
    lines = [f"**Your website**: {site['url']}",
             f"Headline: {audit.get('headline') or 'none found'}\nCall to action: {status(audit.get('cta'))}\n"
             f"Signup or contact form: {status(audit.get('contact'))}\nAnalytics: {status(audit.get('analytics'))}",
             "**What to fix first**\n" + "\n".join(f"{i}. {tip}" for i, tip in enumerate(audit.get("recommendations") or [], 1)),
             "Saved as your existing website, so research and outreach build on it."]
    return _reply("\n\n".join(lines), [{"label": "What should I do next?", "action": "whats_next", "style": "primary"}])
