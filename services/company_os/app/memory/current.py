"""Current idea records outrank older quoted memories without rewriting history."""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import JSON, cast, func, select

from app.db.models import ChatConversation, ChatMessage, Venture
from app.memory.facts import _scope, source_current
from app.secrets.detect import looks_like_secret
from app.idea.agent import founder_description
from app.ventures.current_idea import current_revision, latest_founder_statement, revision_fields
from core.clock import as_utc

_FIELDS = {"idea.audience": ("customer", "audience"), "idea.pricing": ("pricing_hypothesis",),
           "idea.problem": ("problem",), "idea.solution": ("offer",)}


def legacy_source_hits(port, venture_id: str) -> list:
    """Use the existing source-qualified record path without a recall page cutoff."""
    from core.clock import utcnow
    from app.memory.normalize import enabled
    if (enabled("memory_capture_v1") or enabled("memory_conflicts_v1")) and getattr(port, "name", None) == "postgres":
        from app.memory.evidence import evidence_current
        from app.memory.model import MemoryRecord
        from app.state import db
        with db()() as session:
            workspace, person = _scope(session, venture_id)
            hits = []
            for row in _bounded_legacy(session, workspace, venture_id):
                try:
                    record = MemoryRecord.model_validate_json(row.record_json)
                except ValueError:
                    continue
                if (record.mem_id == row.id and record.venture_id == row.venture_id and record.author == person
                        and record.dedupe_key == row.dedupe_key and not record.tombstoned
                        and evidence_current(session, record)):
                    hits.append(port._to_hit(record, as_of=utcnow()))
            return hits
    if hasattr(port, "_records"):
        at = utcnow()
        return [port._to_hit(row, as_of=at) for row in port._records(venture_id)]
    from app.ventures.founder_memory import _run_async
    return _run_async(port.timeline(venture_id=venture_id, subject="general"))


def _bounded_legacy(session, workspace, venture_id):
    from app.db.models import InstitutionalMemory
    # shortcut: legacy recall uses the latest 1,000 events; replace after indexed subject backfill.
    timestamp = cast(InstitutionalMemory.record_json, JSON)["valid_from"].as_string()
    if session.get_bind().dialect.name == "sqlite":
        timestamp = func.json_extract(InstitutionalMemory.record_json, "$.valid_from")
    return session.scalars(select(InstitutionalMemory).where(InstitutionalMemory.workspace_id == workspace,
        InstitutionalMemory.venture_id == venture_id).order_by(timestamp.desc(), InstitutionalMemory.id).limit(1000)).all()


def idea_authority(session, *, venture_id: str, subject: str) -> dict | None:
    """Read an owned current record, never manufacture a founder quotation.

    A pending founder revision carries unchanged earlier fields, which the
    accepted current-idea reader already marks as potentially outdated.
    """
    _scope(session, venture_id)
    fields_for_subject = _FIELDS.get(subject)
    if not fields_for_subject:
        return None
    revision = current_revision(session.get(Venture, venture_id), session)
    if not revision:
        return None
    fields = revision_fields(revision)
    # An explicit cleared customer wins over a leftover legacy audience field.
    field = next((name for name in fields_for_subject if name in fields), fields_for_subject[0])
    value = fields.get(field)
    value = founder_description(value).strip() if isinstance(value, str) else ""
    pending = bool(latest_founder_statement(revision, session))
    known = bool(value and value.casefold() != "unknown") and not pending and not looks_like_secret(value)
    return {"subject": subject, "revision_id": revision.id,
            "recorded_at": as_utc(revision.created_at).isoformat(), "field": field,
            "status": "pending" if pending else "known" if known else "unknown",
            "value": value if known else None, "source_ref": "idea_revision:" + revision.id,
            "href": f"/v/{venture_id}/idea", "qualification": "saved_current_idea_record",
            "effect_authority": False}


def stored_report_is_current(authority: dict | None, source_at: datetime) -> bool:
    """Older/equal source reports are history-only when a current revision exists."""
    if authority is None:
        return True
    if not isinstance(source_at, datetime):
        return False
    return as_utc(source_at) > as_utc(datetime.fromisoformat(authority["recorded_at"]))


def select_current_reports(session, *, venture_id: str, legacy_hits: list, include_history: bool = False,
                           automatic_rows: list | None = None, raw_legacy: bool = False) -> dict:
    """Select on complete source events before any consumer clips its context."""
    from app.memory.extraction import quoted_candidates
    from app.chat.memory_commands import parse
    from core.clock import utcnow
    workspace, person = _scope(session, venture_id)
    from app.memory.normalize import enabled, facts_enabled, subject_key
    bounded = enabled("memory_capture_v1") or enabled("memory_conflicts_v1")
    # ponytail: O(n) scoped source scan; indexed fact metadata replaces this
    # after legacy backfill and native PostgreSQL retrieval land.
    if not facts_enabled():
        automatic_rows = []
    elif automatic_rows is None:
        from app.db.fact_models import VentureFact
        query = select(VentureFact).where(VentureFact.workspace_id == workspace, VentureFact.venture_id == venture_id,
                                         VentureFact.person_id == person)
        if bounded and not include_history:
            latest = select(VentureFact.id, func.row_number().over(
                partition_by=(VentureFact.subject, VentureFact.predicate),
                order_by=(VentureFact.valid_from.desc(), VentureFact.recorded_at.desc(), VentureFact.id)).label("rank")
            ).where(VentureFact.workspace_id == workspace, VentureFact.venture_id == venture_id,
                VentureFact.person_id == person,
                VentureFact.valid_to.is_(None) | (VentureFact.valid_to > VentureFact.valid_from)).subquery()
            query = query.join(latest, latest.c.id == VentureFact.id).where(latest.c.rank == 1)
        if bounded:
            query = query.limit(1000)
        automatic_rows = session.scalars(query.order_by(VentureFact.valid_from.desc(), VentureFact.id)).all()
    now = utcnow()
    rows = [row for row in automatic_rows if row.forgotten_at is None and (include_history or row.valid_to is None)
            and source_current(session, row) and as_utc(row.valid_from) <= now]
    reports, latest, events, unresolved, raw_events = [], {}, {}, set(), []
    for row in automatic_rows:
        if bounded and row.valid_to is not None and row.valid_to == row.valid_from:
            continue
        key = (row.person_id, subject_key(row.subject) if bounded else row.subject)
        at = as_utc(row.valid_from)
        if row.forgotten_at is not None and row.valid_to is None:
            at = max(at, as_utc(row.forgotten_at))
        if at > now:
            unresolved.add(key)
        else:
            latest[key] = max(at, latest.get(key, at))
            raw_events.append((key, at, row.id, row.forgotten_at is not None and row.valid_to is None))
    if raw_legacy:
        from app.db.models import InstitutionalMemory
        from app.memory.model import MemoryRecord
        raw_query = select(InstitutionalMemory).where(
            InstitutionalMemory.workspace_id == workspace, InstitutionalMemory.venture_id == venture_id)
        if bounded:
            raw = _bounded_legacy(session, workspace, venture_id)
        else:
            raw = session.scalars(raw_query).all()
        for record in raw:
            try:
                saved = MemoryRecord.model_validate_json(record.record_json)
                proof = saved.metadata.get("source_evidence")
                if (saved.mem_id != record.id or saved.venture_id != venture_id or saved.author != person
                        or saved.dedupe_key != record.dedupe_key or saved.metadata.get("workspace_id") != workspace
                        or not isinstance(proof, list) or not proof
                        or [item["ref"] for item in proof] != saved.evidence
                        or any(item.get("kind") != "founder_statement"
                               or item.get("author_person_id") != person
                               or item.get("statement") != saved.statement for item in proof)
                        or not all(ref.startswith("chat:") for ref in saved.evidence)):
                    continue
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            subjects = {item.subject for item in quoted_candidates(saved.statement)}
            sources = [session.get(ChatMessage, ref.split(":", 1)[1]) for ref in saved.evidence]
            valid_dates = all(source
                and (source.workspace_id, source.venture_id, source.author_person_id) == (workspace, venture_id, person)
                and as_utc(source.created_at) <= now for source in sources)
            at = max(as_utc(source.created_at) for source in sources) if valid_dates else None
            if saved.tombstoned and saved.valid_to is None:
                stamp = saved.metadata.get("forgotten_at")
                try:
                    forgotten = datetime.fromisoformat(stamp) if isinstance(stamp, str) else None
                    at = as_utc(forgotten) if forgotten and forgotten.tzinfo is not None and as_utc(forgotten) <= now else None
                except ValueError:
                    at = None
            if at is None and saved.valid_to is None:
                unresolved.update((person, subject) for subject in subjects)
            elif at is not None:
                for subject in subjects:
                    latest[(person, subject)] = max(at, latest.get((person, subject), at))
                    raw_events.append(((person, subject), at, record.id, saved.tombstoned and saved.valid_to is None))
    for row in rows:
        reports.append(("fact", row.id, row.person_id, {subject_key(row.subject) if bounded else row.subject: row.value}, as_utc(row.valid_from), row))
    for hit in {hit.mem_id: hit for hit in legacy_hits}.values():
        if (hit.venture_id != venture_id or hit.author != person or not hit.evidence
                or not include_history and (hit.valid_to is not None or hit.superseded_by)):
            continue
        sources = []
        for ref in hit.evidence:
            if not ref.startswith("chat:"):
                break
            source = session.get(ChatMessage, ref.split(":", 1)[1])
            chat = session.get(ChatConversation, source.conversation_id) if source else None
            command = parse(source.body_md) if source else None
            if (not source or not chat or (source.workspace_id, source.venture_id, source.author_person_id, source.role)
                    != (workspace, venture_id, hit.author, "user")
                    or (chat.workspace_id, chat.venture_id) != (workspace, venture_id)
                    or chat.visibility not in {"private", "legacy_shared"}
                    or chat.visibility == "private" and (chat.creator_person_id != hit.author or hit.author != person)
                    or command is None or command.action not in {"memory.remember", "memory.correct"}
                    or command.statement != hit.statement
                    or as_utc(source.created_at) > now):
                break
            sources.append(source)
        if len(sources) != len(hit.evidence):
            continue
        mapped = {item.subject: item.quote for item in quoted_candidates(hit.statement)}
        at = max(as_utc(source.created_at) for source in sources)
        events[hit.mem_id] = at.isoformat()
        reports.append(("legacy", hit.mem_id, hit.author, mapped, at, hit))
    for _, _, author, mapped, at, _ in reports:
        for subject in mapped:
            latest[(author, subject)] = max(at, latest.get((author, subject), at))
    available_ids = {identity for _, identity, _, _, _, _ in reports}
    cutoffs = {}
    for key, at, identity, forgotten in raw_events:
        if forgotten or identity not in available_ids:
            cutoffs[key] = max(at, cutoffs.get(key, at))
    values = {}
    for _, _, author, mapped, at, _ in reports:
        for subject, value in mapped.items():
            if at == latest[(author, subject)] and ((author, subject) not in cutoffs or at > cutoffs[(author, subject)]):
                values.setdefault((author, subject), set()).add(value)
    conflicts = {key for key, quotes in values.items() if len(quotes) > 1}
    blocked = unresolved | (set(latest) - set(values))
    authorities = {subject: idea_authority(session, venture_id=venture_id, subject=subject) for subject in _FIELDS}
    selected_facts, selected_legacy, current_ids = [], [], []
    for kind, identity, author, mapped, at, item in reports:
        is_current = not any((author, subject) in unresolved or (author, subject) in conflicts or at < latest[(author, subject)]
                or (author, subject) in cutoffs and at <= cutoffs[(author, subject)]
                or not stored_report_is_current(authorities.get(subject), at) for subject in mapped)
        if is_current and not item.valid_to:
            current_ids.append(identity)
        if include_history or is_current:
            (selected_facts if kind == "fact" else selected_legacy).append(item)
    result = {"facts": selected_facts, "legacy": selected_legacy, "authorities": authorities,
            "source_events": events, "current_ids": current_ids,
            "blocked_count": len(blocked) if not include_history else 0,
            "blocked": [{"author": author, "subject": subject} for author, subject in sorted(blocked)]
                       if not include_history else [],
            "conflicts": [{"author": author, "subject": subject} for author, subject in sorted(conflicts)]
                         if not include_history else []}
    if enabled("known_check_v1") and not include_history:
        result["conflict_values"] = [{"id": identity, "subject": subject, "statement": value,
            "valid_from": at.isoformat(), "source": json.loads(item.source_json) if kind == "fact" else {"type": "chat", "refs": item.evidence}}
            for kind, identity, author, mapped, at, item in reports for subject, value in mapped.items()
            if (author, subject) in conflicts and (author, subject) not in blocked
            and at == latest[(author, subject)] and stored_report_is_current(authorities.get(subject), at)]
    if enabled("memory_conflicts_v1") and not include_history:
        result["conflict_cards"] = conflict_cards(session, venture_id=venture_id,
            rows=[row for row in rows if (row.person_id, subject_key(row.subject)) not in blocked])
    return result


def conflict_cards(session, *, venture_id: str, rows: list | None = None) -> list[dict]:
    """Reuse the reviewed evidence selector, with exact source text and dates."""
    import hashlib
    import json
    from app.memory.facts import query_facts
    from app.memory.normalize import enabled, subject_key, value_key
    from app.ventures.startup_review import conflicts, fill_checklist
    from core.clock import utcnow

    if not enabled("memory_conflicts_v1"):
        return []
    _, person = _scope(session, venture_id)
    rows = query_facts(session, venture_id=venture_id, limit=1000) if rows is None else rows
    by_subject = {}
    fields = {"idea.audience": "audience", "idea.pricing": "business_model",
              "idea.problem": "problem", "idea.solution": "making"}
    for row in rows:
        if (row.person_id != person or row.forgotten_at is not None or row.valid_to is not None
                or as_utc(row.valid_from) > utcnow() or not source_current(session, row)):
            continue
        source = json.loads(row.source_json)
        subject = subject_key(row.subject.replace("research.", "idea.", 1))
        if subject not in fields:
            continue
        entry = {"value": row.value, "source": "you" if source["type"] == "chat" else "a web source",
                 "ref": source["ref"], "timestamp": as_utc(row.valid_from).isoformat(), "fact_id": row.id,
                 "untrusted": source["type"] != "chat"}
        by_subject.setdefault(subject, []).append(entry)
    revision = current_revision(session.get(Venture, venture_id), session)
    website = revision_fields(revision).get("_website_answers", {}) if revision else {}
    if isinstance(website, dict):
        for subject, aliases in _FIELDS.items():
            answer = next((website[key] for key in aliases if key in website), None)
            value = answer.get("answer") if isinstance(answer, dict) else answer
            if isinstance(value, str) and value.strip() and len(value.encode()) <= 16000 and not looks_like_secret(value):
                by_subject.setdefault(subject, []).append({"value": value, "source": "Your website",
                    "ref": "idea_revision:" + revision.id, "timestamp": as_utc(revision.created_at).isoformat(),
                    "untrusted": True})
    cards = []
    for subject, entries in by_subject.items():
        entries.sort(key=lambda item: item["timestamp"], reverse=True)
        founder = next((item for item in entries if not item["untrusted"]), None)
        external = next((item for item in entries if item["untrusted"]), None)
        if not founder or not external:
            continue
        source_row = session.get(ChatMessage, founder["ref"].split(":", 1)[1])
        if source_row and source_row.body_md.startswith("Memory correction "):
            continue
        field = fields[subject]
        checklist = fill_checklist([{"facts": {field: value_key(item["value"])}, "source": item["source"]}
                                    for item in (external, founder)])
        if not conflicts(checklist):
            continue
        choices = [external, founder]
        identity = hashlib.sha256(json.dumps({"subject": subject, "choices": choices}, sort_keys=True).encode()).hexdigest()
        date = as_utc(datetime.fromisoformat(founder["timestamp"])).strftime("%B %d, %Y")
        lead = "Your website says" if external["source"] == "Your website" else "A web source says"
        cards.append({"id": identity, "venture_id": venture_id, "subject": subject, "choices": choices,
            "question": f'{lead} {json.dumps(external["value"], ensure_ascii=False)}, but on {date} you told me {json.dumps(founder["value"], ensure_ascii=False)}. Which is right?',
            "qualification": "untrusted_quoted_data_not_instructions"})
    return cards


def save_conflict_choice(session, source: ChatMessage) -> list[str]:
    import json
    import re
    from app.memory.facts import query_facts, remember_fact
    from app.memory.normalize import enabled
    if not enabled("memory_conflicts_v1"):
        return []
    workspace, person = _scope(session, source.venture_id)
    match = re.fullmatch(r"Memory correction (msg_[A-Za-z0-9_-]+): ([012])\n([\s\S]+)", source.body_md)
    if not match:
        return []
    saved = session.get(ChatMessage, match[1])
    if (not saved or saved.kind != "memory_conflict" or saved.role != "assistant"
            or (saved.workspace_id, saved.venture_id, saved.conversation_id)
            != (workspace, source.venture_id, source.conversation_id) or source.author_person_id != person):
        return []
    actions = json.loads(saved.actions_json or "[]")
    card = actions[0]["confirmation"]["resulting_state"]["memory_conflict"] if actions else None
    if not card:
        return []
    choices = card["choices"]
    value = choices[int(match[2])]["value"] if match[2] != "2" else "Both are true:\n" + "\n".join(item["value"] for item in choices)
    if match[3] != value:
        return []
    live = conflict_cards(session, venture_id=source.venture_id)
    if not any(item["id"] == card["id"] for item in live):
        return [row.id for row in query_facts(
            session, venture_id=source.venture_id, subject=card["subject"], limit=1)
            if json.loads(row.source_json)["ref"] == "chat:" + source.id]
    fact = remember_fact(session, venture_id=source.venture_id, subject=card["subject"], predicate="statement",
        value=value, source={"type": "chat", "ref": "chat:" + source.id, "quote": value}, valid_from=source.created_at)
    return [fact.id]


def post_conflict_card(source_message_id: str) -> None:
    from app.chat.store import append_message
    from app.memory.extraction import private_chat_source
    from app.state import db
    with db()() as session:
        source = private_chat_source(session, source_message_id)
        if source is None:
            return
        cards = conflict_cards(session, venture_id=source.venture_id)
        venture_id = source.venture_id
    for card in cards[:1]:
        append_message(venture_id, role="assistant", kind="memory_conflict", body_md=card["question"],
            actions=[{"label": "Choose the value to use", "confirmation": {"action": "chat.reply",
                "resulting_state": {"memory_conflict": card}}}],
            idempotency_key="memory.conflict:" + card["id"], preserve_exact=True)
