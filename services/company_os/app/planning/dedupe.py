"""Dedupe via task fingerprints."""

from __future__ import annotations

import hashlib
import re

from sqlalchemy import select

from app.db.models import TaskFingerprint
from app.state import db


def fingerprint_title(title: str) -> str:
    norm = re.sub(r"\s+", " ", title.strip().lower())
    return hashlib.sha256(norm.encode()).hexdigest()[:24]


def known_fingerprints(venture_id: str) -> set[str]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(TaskFingerprint).where(
                TaskFingerprint.venture_id == venture_id,
                TaskFingerprint.status.in_(("done", "dismissed", "open")),
            )
        ).all()
        return {r.fingerprint for r in rows}


def filter_duplicates(venture_id: str, tasks: list[dict]) -> list[dict]:
    known = known_fingerprints(venture_id)
    out = []
    for t in tasks:
        fp = fingerprint_title(t["title"])
        t = dict(t)
        t["fingerprint"] = fp
        if fp in known and t.get("force") is not True:
            # Skip dismissed/done; allow open duplicates only if not present
            continue
        out.append(t)
    return out or tasks[:3]  # never return empty on first plan
