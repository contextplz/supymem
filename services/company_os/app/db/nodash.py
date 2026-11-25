"""Assistant-authored rows never reach storage with an em or en dash.

Mapper events cover every writer (chat store, idea runner, GStack office hours) at once.
User-authored rows (role=user, founder revisions) are never touched.
"""
from __future__ import annotations

import json

from sqlalchemy import event

from app.db.models import ChatMessage
from app.db.supymem_models import ClarifierTurn, IdeaRevision
from core.copy import no_dashes

_SHOW = {"label", "text", "title", "body", "description", "detail", "hint", "reason", "why", "options", "option_labels", "question", "summary", "message", "recommendation_reason"}


def _walk(value, key="", everything=False):
    if isinstance(value, str):
        return no_dashes(value) if everything or key in _SHOW else value
    if isinstance(value, list):
        return [_walk(v, key, everything) for v in value]
    if isinstance(value, dict):
        return {k: _walk(v, k, everything) for k, v in value.items()}
    return value


def _json(raw: str | None, everything: bool = False) -> str | None:
    if not raw or "—" not in raw and "–" not in raw and "\\u2014" not in raw and "\\u2013" not in raw:
        return raw
    try:
        return json.dumps(_walk(json.loads(raw), everything=everything), ensure_ascii=False)
    except ValueError:
        return no_dashes(raw)


def _chat(_mapper, _conn, row: ChatMessage) -> None:
    if row.role == "assistant":
        row.body_md = no_dashes(row.body_md)
        row.actions_json = _json(row.actions_json)


def _turn(_mapper, _conn, row: ClarifierTurn) -> None:
    if row.role != "user" and row.role != "founder":
        row.text = no_dashes(row.text)
        row.options = _json(row.options, True)


def _revision(_mapper, _conn, row: IdeaRevision) -> None:
    if row.source == "agent":
        row.summary = no_dashes(row.summary)
        row.structured = _json(row.structured, True) or row.structured


for _model, _fn in ((ChatMessage, _chat), (ClarifierTurn, _turn), (IdeaRevision, _revision)):
    event.listen(_model, "before_insert", _fn)
    event.listen(_model, "before_update", _fn)
