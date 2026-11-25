"""Validate internal draft citations against the supplied current source context.

This verifies citation custody. It does not judge every uncited prose claim.
"""
from __future__ import annotations

import re


def validate_fact_citations(context: dict, identifiers: object) -> list[str]:
    if not isinstance(identifiers, list) or len(identifiers) > 24 or any(not isinstance(item, str) for item in identifiers):
        raise ValueError("memory_draft_citations_invalid")
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("memory_draft_citations_invalid")
    allowed = {row["id"] for row in [*context.get("facts", []), *context.get("statements", [])]}
    allowed.update(row["source_ref"] for row in context.get("idea_records", []) if row.get("status") == "known")
    if any(identity not in allowed for identity in identifiers):
        raise ValueError("memory_draft_citations_unproven")
    return list(identifiers)


def outreach_source_clauses(context: dict, *, byte_budget: int = 1400) -> tuple[list[str], list[str]]:
    """Render selected supplied source spans, never license arbitrary model prose."""
    from app.memory.extraction import quoted_candidates
    subjects = {"idea.audience", "idea.problem", "idea.solution", "idea.pricing"}
    selected = {}
    for row in context.get("idea_records", []):
        if row.get("status") == "known" and row.get("subject") in subjects:
            selected[row["subject"]] = (row["source_ref"], row["value"])
    for row in [*context.get("facts", []), *context.get("statements", [])]:
        if row.get("subject") in subjects:
            selected[row["subject"]] = (row["id"], row["statement"])
        else:
            for candidate in quoted_candidates(row["statement"]):
                if candidate.subject in subjects:
                    selected[candidate.subject] = (row["id"], candidate.quote)
    lines, ids, used = [], [], 0
    for identity, quote in selected.values():
        if (not isinstance(quote, str) or not quote.strip()
                or re.search(r"\b(?:fact|mem|wsp|ven|msg|job|idv|pln)_[A-Za-z0-9_-]+\b|\bcredits?\b", quote, re.I)):
            continue
        line = '"' + quote + '"'
        size = len(line.encode())
        if used + size > byte_budget:
            continue  # Whole source clauses only; never truncate a constraint.
        lines.append(line)
        ids.append(identity)
        used += size
    return lines, validate_fact_citations(context, list(dict.fromkeys(ids)))
