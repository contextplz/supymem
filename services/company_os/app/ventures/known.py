"""Source-backed question preflight. Recalled context never authorizes an action."""
from __future__ import annotations

import json
import logging
import re
from contextlib import nullcontext

from sqlalchemy import select

from app.credits.policy import get_policy
from app.db.models import Venture, VentureDocument
from app.state import db
from app.ventures import founder_facts

log = logging.getLogger(__name__)
ALIASES = {**founder_facts.ALIASES, "who": "audience", "icp": "audience", "name": "product_name",
           "target_users": "audience", "making": "solution", "product": "solution", "idea": "solution",
           "sender_country": "sender_geo", "sender_address": "sender_postal_address",
           "sending_mode": "outbound_mode", "excluded_domains": "excludes",
           "product_buyer": "audience", "product_job": "problem", "product_today": "current_workaround",
           "product_object": "solution", "offer": "solution", "write_like": "voice", "tone": "voice"}


def enabled():
    from app.core.local_mode import is_local
    if is_local():
        return True
    try:
        flags = get_policy("feature_flags")
    except Exception:  # noqa: BLE001 - an unreadable flag means off
        return False
    return isinstance(flags, dict) and flags.get("known_check_v1") is True


def known(venture, field, *, session=None, saved=None):
    if not enabled():
        return {"status": "missing", "value": None, "source": None}
    if session is None:
        with db()() as owned:
            row = owned.get(Venture, venture if isinstance(venture, str) else venture.id)
            return known(row, field, session=owned, saved=saved)
    from app.tenancy import current_or_none
    scope = current_or_none()
    if not venture or not venture.workspace_id:
        return {"status": "missing", "value": None, "source": None}
    if scope and (scope.workspace_id != venture.workspace_id or scope.venture_id not in {"", venture.id}):
        raise PermissionError("known_context_scope_mismatch")
    field = ALIASES.get(field, field)
    from app.ventures.startup_review import INTERNAL_KIND, _json, _known, evidence_for
    record = session.get(VentureDocument, f"{INTERNAL_KIND}:{venture.id}")
    stored = _json(record.body_md) if record and record.venture_id == venture.id else {}
    if field in stored.get("cleared_fields", []):
        return {"status": "missing", "value": None, "source": None}
    candidates = []

    def add(value, source, trusted=False, **metadata):
        if value is None or not _known(value) and not (field == "excludes" and value == []):
            return
        from app.secrets.detect import looks_like_secret
        if looks_like_secret(json.dumps(value, ensure_ascii=False)):
            return
        if isinstance(value, str):
            if field == "problem" and not founder_facts.usable_problem(value):
                return
            value = value.strip()
            if field == "audience":
                from app.idea.agent import broad_audience
                if broad_audience(value):
                    return
        candidates.append({"value": value, "source": source, "trusted": trusted, **metadata})

    if saved is not None:
        add(saved, "Your saved setup", True, resolved=True)
    fact = founder_facts.read(session, venture).get(field, {})
    add(fact.get("value"), "Your " + str(fact.get("source") or "saved answer"),
        founder_facts.policy({field: fact}, field) == "skip", resolved=fact.get("source") == "review",
        founder_fact=fact.get("source") in founder_facts.FOUNDER_SOURCES)
    if field in {"audience", "problem"}:
        from app.gtm.journey import _row
        journey = _row(session, venture.id)
        setup = _json(journey.provenance_json).get("icp", {}) if journey and journey.venture_id == venture.id else {}
        if setup.get("chat_confirmed"):
            add(setup.get("who" if field == "audience" else field), "Your saved outreach answer", True)
    descriptions = [venture.raw_idea]
    for evidence in evidence_for(session, venture):
        for key, value in (evidence.get("facts") or {}).items():
            if ALIASES.get(key, key) == "solution" and evidence.get("origin") == "founder":
                descriptions.append(value)
            if ALIASES.get(key, key) != field:
                continue
            website = evidence.get("origin") == "website" or "website" in evidence.get("source", "").lower()
            if field not in founder_facts.FIELDS or website:
                add(value, evidence["source"], not website and evidence.get("confidence", 0) >= .95)
    from app.state import MEM
    answers = MEM.get(f"product_answers:{venture.id}") or {}
    questions = MEM.get(f"product_questions:{venture.id}") or []
    for question in questions:
        if ALIASES.get(question.get("slot_key"), question.get("slot_key")) != field:
            continue
        value = answers.get(question.get("slot_key")) or answers.get(question["id"])
        option = next((item for item in question.get("options", []) if item["id"] == value), None)
        add(option["label"] if option else value, "Your product answer",
            MEM.get(f"product_answers_source:{venture.id}") != "auto")
    if field == "request_fields":
        from app.db.supymem_models import Experiment
        rows = session.scalars(select(Experiment).where(Experiment.venture_id == venture.id,
            Experiment.workspace_id == venture.workspace_id))
        rows = sorted(rows, key=lambda row: (_json(row.context).get("prepared_at", ""), row.id), reverse=True)
        for row in rows:
            context = _json(row.context)
            spec = context.get("spec") or {}
            if spec.get("page_kind") == "request" and context.get("page_test"):
                add(spec.get("runner_params", {}).get("page_test", {}).get("request", {}).get("fields"),
                    "Your saved request form", True)
                break
    from app.memory.normalize import facts_enabled, subject_key
    if facts_enabled():
        from app.memory.context import context_for
        from app.tenancy import bind
        try:
            binding = bind(workspace_id=scope.workspace_id, venture_id=venture.id, actor=scope.actor) if scope and not scope.venture_id else nullcontext()
            with binding:
                memory = context_for(session, workspace_id=venture.workspace_id, venture_id=venture.id,
                    purpose="known.check", query=field.replace("_", " "))
        except (PermissionError, RuntimeError):
            log.warning("known_memory_scope_unavailable")
            memory = {}
        if any(ALIASES.get(subject_key(subject).split(".", 1)[-1], subject_key(subject).split(".", 1)[-1]) == field
               for subject in memory.get("blocked_subjects", [])):
            return {"status": "missing", "value": None, "source": None}
        from app.memory.extraction import quoted_candidates
        for item in memory.get("facts", []) + memory.get("statements", []) + memory.get("conflict_values", []):
            subject = subject_key(item.get("subject", "")).split(".", 1)[-1]
            if ALIASES.get(subject, subject) == field:
                quotes = [quote.quote for quote in quoted_candidates(item["statement"])
                          if ALIASES.get(quote.subject.removeprefix("idea."), quote.subject.removeprefix("idea.")) == field]
                value = founder_facts.literal_facts(item["statement"]).get(field, {}).get("value")
                if field in {"request_fields", "excludes"}:
                    try:
                        value = json.loads(item["statement"])
                    except ValueError:
                        continue
                add(value if value is not None else (quotes[0] if quotes else item["statement"]), "Your saved memory: " + item["id"],
                    item.get("source", {}).get("type") == "chat" or "kind" in item,
                    **{key: item[key] for key in ("valid_from",) if key in item})
    from app.connections import context_docs
    if context_docs.enabled():
        context = context_docs.research_context(session, venture.id, venture.workspace_id)
        for document in context.get("connected_context", {}).get("documents", []):
            excerpt = document.get("excerpt") or ""
            value = founder_facts.literal_facts(excerpt).get(field, {}).get("value")
            if not value:
                # shortcut: only labelled document slots, use typed extraction for unlabelled prose.
                labels = [field, *(alias for alias, target in ALIASES.items() if target == field)]
                match = re.search(r"(?im)^\s*(?:" + "|".join(re.escape(label).replace("_", "[ _]") for label in labels)
                                  + r")\s*:\s*(.+)$", excerpt)
                value = match[1].strip() if match else None
                if field in {"request_fields", "excludes"} and value:
                    try:
                        value = json.loads(value)
                    except ValueError:
                        value = None
            add(value, document.get("title") or "Your selected document", source_url=document.get("source_url"))
    if field == "problem" and not candidates:
        from app.ventures.startup_review import problem_prefill
        add(problem_prefill(*(founder_facts.plain_source(text) for text in descriptions)),
            "Your idea description")
    return _result(candidates)


def _result(candidates):
    from app.memory.normalize import value_key
    resolved = next((item for item in candidates if item.get("resolved")), None)
    if resolved:
        candidates = [item for item in candidates if item.get("resolved") or item.get("founder_fact")
                      or item["source"].startswith("Your saved memory:")]
    unique = {}
    for item in candidates:
        key = value_key(item["value"]) if isinstance(item["value"], str) else json.dumps(item["value"], sort_keys=True, ensure_ascii=False).casefold()
        if key not in unique or item["trusted"] and not unique[key]["trusted"]:
            unique[key] = item
    values = list(unique.values())
    if not values:
        return {"status": "missing", "value": None, "source": None}
    if len(values) > 1:
        return {"status": "conflict", "value": [item["value"] for item in values],
                "source": [item["source"] for item in values], "values": values}
    item = values[0]
    return {"status": "known" if item["trusted"] else "confirm", **{key: value for key, value in item.items() if key not in {"trusted", "resolved", "founder_fact"}}}


def question(result, field):
    from core.copy import no_dashes
    values = result.get("values") or [{"value": result["value"], "source": result["source"]}]
    def text(value):
        return no_dashes(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False))
    summary = "; ".join(text(item["value"]) + " (" + text(item["source"]) + ")" for item in values)
    label = "audience" if enabled() and field == "who" else field.replace("_", " ")
    prompt = (f"For your {label}, I used {summary}. You can edit this assumption." if result["status"] == "known"
              else f"For your {label}, I found {summary}. Is this correct? Say yes or edit it." if result["status"] == "confirm"
              else f"Your {label} has conflicting values: {summary}. Choose the value to use or edit it.")
    return {"field": field, "text": prompt, "values": values, "options": [text(item["value"]) for item in values]}
