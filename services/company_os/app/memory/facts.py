"""Exact source quotations, with actor isolation and latest-statement history.

This is context, never action permission or independent proof of external effects.
No model output or generated summary is accepted as a fact's source.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from sqlalchemy import select, update

from app.db.models import ChatConversation, ChatMessage, Job, PlannedTask, Venture
from app.db.session import serialize_writes
from app.secrets.detect import looks_like_secret
from app.tenancy import current
from core.clock import as_utc, utcnow
from core.ids import new_id

if TYPE_CHECKING:
    from app.db.fact_models import VentureFact

_KEY = re.compile(r"[a-z][a-z0-9_-]*(?:\.[a-z0-9_-]+)*\Z", re.ASCII)
_REF = re.compile(r"(chat|research|import|task):([A-Za-z0-9_-]{1,128})\Z", re.ASCII)
_SEARCH_PROVIDERS = {"exa", "firecrawl", "tavily", "duckduckgo"}
_RESEARCH_TUPLES = {(provider, "source", kind) for provider in _SEARCH_PROVIDERS
                    for kind in ("search_excerpt", "web")} | {
    ("github", "oss_repo", "repository_metadata"), ("github", "source", "github_issue"),
    ("hn_algolia", "source", "hn_item"), ("hn_firebase", "source", "hn_comment"),
    ("yc_oss", "source", "yc_company"),
}


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def normalize_key(value: str, *, limit: int = 200) -> str:
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not normalized or len(normalized) > limit or not _KEY.fullmatch(normalized):
        raise ValueError("memory_fact_key_invalid")
    return normalized


def _time(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError("memory_fact_time_invalid")
    return as_utc(value).replace(tzinfo=None)


def _scope(session, venture_id: str):
    from app.chat.conversations import actor_id

    scope = current()
    venture = session.get(Venture, venture_id)
    person = actor_id()
    if not venture or not venture.workspace_id or (scope.workspace_id, scope.venture_id) != (venture.workspace_id, venture_id):
        raise PermissionError("memory_fact_scope_mismatch")
    if not person:
        raise PermissionError("memory_fact_actor_required")
    return venture.workspace_id, person


def _research_origin(url: str) -> tuple:
    parsed = urlsplit(url)
    return parsed.scheme, (parsed.hostname or "").lower().removeprefix("www."), parsed.port or (443 if parsed.scheme == "https" else 80)


def _research_source_eligible(row, evidence: dict) -> bool:
    from app.research.deep import safe_source_url
    from app.research.github_search import _REPOSITORY

    kind = evidence.get("evidence_kind")
    if not isinstance(kind, str) or (row.source, row.category, kind) not in _RESEARCH_TUPLES or safe_source_url(row.url) != row.url:
        return False
    try:
        if not isinstance(evidence.get("observed_at"), str) or datetime.fromisoformat(evidence["observed_at"].replace("Z", "+00:00")).tzinfo is None:
            return False
    except ValueError:
        return False
    prior = evidence.get("prior_art")
    if kind == "repository_metadata":
        repo = evidence.get("repository")
        if (not isinstance(repo, dict) or repo.get("private") is not False
                or not isinstance(repo.get("full_name"), str) or not _REPOSITORY.fullmatch(repo["full_name"])
                or row.url != "https://github.com/" + repo["full_name"]):
            return False
        return prior is None or (isinstance(prior, dict) and prior.get("source_kind") == kind
                                  and prior.get("company_url") == row.url)
    if kind == "search_excerpt":
        return prior is None or (isinstance(prior, dict) and prior.get("source_kind") == "web"
                                  and isinstance(prior.get("company_url"), str)
                                  and safe_source_url(prior["company_url"]) == prior["company_url"]
                                  and _research_origin(prior["company_url"]) == _research_origin(row.url))
    if not isinstance(prior, dict) or prior.get("source_kind") != kind:
        return False
    if kind in {"hn_item", "hn_comment"}:
        identity = prior.get("hn_id")
        return (type(identity) is int and 0 < identity < 2**63
                and row.url == f"https://news.ycombinator.com/item?id={identity}"
                and (kind != "hn_comment" or type(prior.get("story_id")) is int and 0 < prior["story_id"] < 2**63))
    if kind == "yc_company":
        return (bool(re.fullmatch(r"https://www\.ycombinator\.com/companies/[A-Za-z0-9_-]+/?", row.url))
                and isinstance(prior.get("company_name"), str) and prior["company_name"] == row.title)
    company = prior.get("company_url")
    if not isinstance(company, str) or safe_source_url(company) != company:
        return False
    if kind == "github_issue":
        repo = company.removeprefix("https://github.com/")
        return (company == "https://github.com/" + repo and bool(_REPOSITORY.fullmatch(repo))
                and bool(re.fullmatch(re.escape(company) + r"/issues/[1-9][0-9]{0,18}", row.url)))
    return kind == "web" and _research_origin(company) == _research_origin(row.url)


def _research_excerpts(row, evidence: dict) -> list[str]:
    excerpts = [row.summary]
    additions = evidence.get("additional_excerpts", [])
    if not isinstance(additions, list):
        return excerpts
    for entry in additions[:5]:
        if (not isinstance(entry, dict) or not isinstance(entry.get("text"), str)
                or not isinstance(entry.get("query"), str) or not entry["query"]
                or not isinstance(entry.get("observed_at"), str)):
            continue
        try:
            if datetime.fromisoformat(entry["observed_at"].replace("Z", "+00:00")).tzinfo is None:
                continue
        except ValueError:
            continue
        ordinary = set(entry) == {"text", "query", "observed_at", "provider"} and entry.get("provider") == row.source
        page = (set(entry) == {"text", "query", "observed_at", "provider", "verified_page"}
                and row.source in _SEARCH_PROVIDERS and evidence.get("evidence_kind") in {"search_excerpt", "web"}
                and entry.get("provider") == "firecrawl" and entry["query"] == "page_scrape"
                and entry["verified_page"] is True and evidence.get("page_verified") is True)
        if ordinary or page:
            excerpts.append(entry["text"])
    return excerpts


def _source(session, *, venture_id: str, source_type: str, reference: str):
    workspace_id, person = _scope(session, venture_id)
    match = _REF.fullmatch(reference) if isinstance(reference, str) else None
    if not match or source_type != match[1]:
        raise ValueError("memory_fact_source_invalid")
    identity = match[2]
    common = {"type": source_type, "ref": reference, "workspace_id": workspace_id,
              "venture_id": venture_id, "person_id": person}

    if source_type == "chat":
        from app.memory.normalize import enabled
        row = session.get(ChatMessage, identity)
        chat = session.get(ChatConversation, row.conversation_id) if row and row.conversation_id else None
        if (not row or (row.workspace_id, row.venture_id, row.author_person_id)
                != (workspace_id, venture_id, person)
                or row.role not in ({"user", "founder"} if enabled("memory_capture_v1") else {"user"}) or not chat
                or (chat.workspace_id, chat.venture_id) != (workspace_id, venture_id)
                or chat.visibility not in {"private", "legacy_shared"}
                or chat.visibility == "private" and chat.creator_person_id != person):
            raise ValueError("memory_fact_founder_source_invalid")
        if row.kind == "voice_summary" or looks_like_secret(row.body_md):
            raise ValueError("memory_fact_founder_source_invalid")
        material = common | {"body_md": row.body_md, "conversation_id": chat.id,
                             "visibility": chat.visibility, "creator_person_id": chat.creator_person_id,
                             "created_at": _time(row.created_at).isoformat(), "kind": row.kind}
        return material, [row.body_md], row.created_at, "founder_statement_context_only"

    if source_type == "research":
        from app.db.supymem_models import ResearchDossier, ResearchFinding
        from app.ventures.current_idea import current_revision

        row = session.get(ResearchFinding, identity)
        dossier = session.get(ResearchDossier, row.dossier_id) if row else None
        if (not row or not dossier or (row.workspace_id, row.venture_id, dossier.workspace_id, dossier.venture_id)
                != (workspace_id, venture_id, workspace_id, venture_id)):
            raise ValueError("memory_fact_research_source_invalid")
        evidence = json.loads(row.evidence)
        if not isinstance(evidence, dict):
            raise ValueError("memory_fact_research_source_invalid")
        latest = current_revision(session.get(Venture, venture_id), session)
        # The deep-research writer stamps provider excerpts separately from model
        # assessments. Its dossier summary and finding model prose are excluded.
        if (dossier.state not in {"complete", "partial"} or evidence.get("founder_flag") == "exclude"
                or latest and dossier.revision_id != latest.id
                or not isinstance(row.url, str) or not row.url or not _research_source_eligible(row, evidence)):
            raise ValueError("memory_fact_research_source_invalid")
        excerpts = _research_excerpts(row, evidence)
        material = common | {"dossier_id": dossier.id, "revision_id": dossier.revision_id,
                             "url": row.url, "provider": row.source,
                             "evidence_kind": evidence["evidence_kind"], "prior_art": evidence.get("prior_art"),
                             "repository": evidence.get("repository"),
                             "created_at": _time(row.created_at).isoformat()}
        return material, excerpts, row.created_at, "external_excerpt_observation_only"

    if source_type == "import":
        from app.db.import_models import ImportRun, SourceSnapshot

        row = session.get(SourceSnapshot, identity)
        run = session.get(ImportRun, row.import_run_id) if row else None
        from app.authz.workspace import subject_person_id

        if (not row or not run or (row.workspace_id, row.venture_id, run.workspace_id, run.venture_id)
                != (workspace_id, venture_id, workspace_id, venture_id) or subject_person_id(run.actor_subject) != person
                or run.source_snapshot_id != row.id or run.state not in {"report_complete", "partial_report"}
                or json.loads(row.source_provenance_json).get("storage_verified") is not True):
            raise ValueError("memory_fact_import_source_invalid")
        receipt = {"source_snapshot_id": row.id, "archive_sha256": row.normalized_archive_sha256,
                   "manifest_digest": row.manifest_digest, "size_bytes": row.size_bytes}
        material = common | receipt | {"created_at": _time(row.created_at).isoformat()}
        return material, [_json(receipt)], row.created_at, "recorded_import_receipt_only"

    row = session.get(Job, identity)
    payload = json.loads(row.payload_json) if row else {}
    task = session.get(PlannedTask, payload.get("task_id")) if payload.get("task_id") else None
    from app.authz.workspace import subject_person_id

    if (not row or not task or (row.workspace_id, row.venture_id, task.workspace_id, task.venture_id)
            != (workspace_id, venture_id, workspace_id, venture_id) or row.kind != "task.run"
            or row.status != "succeeded" or not row.finished_at or task.status != "done" or not task.finished_at
            or not payload.get("_actor_subject") or subject_person_id(payload["_actor_subject"]) != person):
        raise ValueError("memory_fact_task_source_invalid")
    receipt = {"job_id": row.id, "task_id": task.id, "status": task.status,
               "finished_at": _time(task.finished_at).isoformat()}
    return common | receipt, [_json(receipt)], task.finished_at, "recorded_task_status_only"


def source_options(session, *, venture_id: str, source_type: str, reference: str) -> list[str]:
    """Durable founder words/provider excerpts or deterministic receipt JSON."""
    return _source(session, venture_id=venture_id, source_type=source_type, reference=reference)[1]


def _proof(session, *, venture_id: str, source: dict):
    if not isinstance(source, dict) or set(source) != {"type", "ref", "quote"}:
        raise ValueError("memory_fact_source_invalid")
    quote = source["quote"]
    if not isinstance(quote, str) or not quote.strip() or len(quote.encode()) > 16000:
        raise ValueError("memory_fact_quote_invalid")
    material, options, source_at, qualification = _source(session, venture_id=venture_id,
        source_type=source["type"], reference=source["ref"])
    from app.memory.normalize import enabled, founder_text
    if source["type"] == "chat" and enabled("memory_capture_v1"):
        if not any(quote in founder_text(text) for text in options):
            raise ValueError("memory_fact_untrusted_quote")
    matched = next((text for text in options if isinstance(text, str) and quote in text), None)
    if matched is None or source["type"] in {"import", "task"} and quote != matched:
        raise ValueError("memory_fact_quote_unproven")
    if looks_like_secret(quote):
        raise ValueError("memory_credential_withheld")
    if source["type"] == "research":
        from app.boundary import Segment
        from app.boundary.detectors.pii import scan
        if scan(Segment(text=quote, origin="third_party", label="memory research quote")):
            raise ValueError("memory_external_contact_withheld")
    # A source may grow additional research excerpts without changing this quote.
    return _digest(material | {"source_text": matched}), _time(source_at), qualification


def remember_fact(session, *, venture_id: str, subject: str, predicate: str, value: str,
                  source: dict, confidence: float = 1.0, valid_from: datetime | None = None,
                  decision: str | None = None) -> VentureFact:
    """Write in the caller's transaction; retries cannot revive a retired fact."""
    from app.memory.normalize import facts_enabled
    if not facts_enabled():
        raise ValueError("memory_facts_disabled")
    from app.db.fact_models import VentureFact
    subject, predicate = normalize_key(subject), normalize_key(predicate, limit=80)
    from app.memory.normalize import enabled, retirement, subject_aliases, subject_key, value_key
    capture = enabled("memory_capture_v1") and isinstance(source, dict) and source.get("type") == "chat"
    if decision is not None and (not capture or decision not in {"add", "update", "retire", "ignore"}):
        raise ValueError("memory_fact_decision_invalid")
    if capture:
        subject = subject_key(subject)
        if predicate in {"is", "statement"}:
            predicate = "statement"
    if (not isinstance(value, str) or not value.strip() or len(value.encode()) > 16000
            or not isinstance(source, dict) or value != source.get("quote")):
        raise ValueError("memory_fact_value_unproven")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1:
        raise ValueError("memory_fact_confidence_invalid")
    if looks_like_secret(_json({"subject": subject, "predicate": predicate, "value": value, "source": source})):
        raise ValueError("memory_credential_withheld")
    if source.get("type") != "chat" and not subject.startswith(str(source.get("type")) + "."):
        raise ValueError("memory_fact_source_authority_invalid")
    # The caller may already have a source read transaction. PostgreSQL locks
    # are transaction-local; SQLite's shared helper permits a prior read.
    scope = current()
    serialize_writes(session, key="venture-facts:" + venture_id + ":" + scope.actor)
    workspace_id, person = _scope(session, venture_id)
    proof, source_at, _ = _proof(session, venture_id=venture_id, source=source)
    source_row = session.get(ChatMessage, source["ref"].split(":", 1)[1]) if source["type"] == "chat" else None
    correction = bool(source_row and source_row.body_md.startswith("Memory correction "))
    if decision == "retire" and not retirement(value):
        raise ValueError("memory_fact_retirement_unproven")
    if valid_from is not None and _time(valid_from) != source_at:
        raise ValueError("memory_fact_event_time_unproven")
    key = _digest({"subject": subject, "predicate": predicate, "source_type": source["type"], "ref": source["ref"]})
    conditions = [VentureFact.workspace_id == workspace_id, VentureFact.venture_id == venture_id, VentureFact.person_id == person]
    existing = session.scalar(select(VentureFact).where(*conditions, VentureFact.dedupe_key == key))
    if existing:
        if existing.value != value or existing.source_digest != proof or existing.source_json != _json(source):
            raise ValueError("memory_fact_idempotency_conflict")
        return existing
    now = _time(utcnow())
    if source_at > now:
        raise ValueError("memory_fact_future_source_invalid")
    matching = [*conditions,
        VentureFact.subject.in_(subject_aliases(subject)) if capture else VentureFact.subject == subject,
        VentureFact.predicate.in_({"is", "statement"}) if capture and predicate == "statement" else VentureFact.predicate == predicate]
    previous_query = select(VentureFact).where(*matching)
    if capture:
        previous_query = previous_query.where(VentureFact.valid_to.is_(None) | (VentureFact.valid_to > VentureFact.valid_from))
    previous = session.scalar(previous_query.order_by(VentureFact.valid_from.desc(), VentureFact.recorded_at.desc()).limit(1))
    row = VentureFact(id=new_id("fact"), workspace_id=workspace_id, venture_id=venture_id, person_id=person,
        subject=subject, predicate=predicate, value=value, value_json=_json(value), source_json=_json(source),
        source_digest=proof, dedupe_key=key, confidence=confidence, valid_from=source_at, recorded_at=now)
    if decision == "ignore":
        row.valid_to, row.superseded_at = source_at, now
    elif previous:
        if source_at < _time(previous.valid_from):
            # Late delivery retains the older statement without reversing a
            # correction whose source was recorded later.
            row.valid_to, row.superseded_at = previous.valid_from, now
        elif (capture and not correction and previous.valid_to is None and previous.forgotten_at is None
                and value_key(previous.value) == value_key(value) and not retirement(value)):
            row.valid_to, row.superseded_at = source_at, now
        elif capture:
            session.execute(update(VentureFact).where(*matching, VentureFact.valid_to.is_(None),
                VentureFact.valid_from <= source_at).values(valid_to=source_at, superseded_at=now))
            session.flush()
        elif previous.valid_to is None:
            previous.valid_to, previous.superseded_at = source_at, now
            session.flush()  # Retire the prior row before the partial unique insert.
    if capture and retirement(value) and row.valid_to is None:
        row.forgotten_at = source_at
    session.add(row)
    session.flush()
    return row


def source_current(session, row: VentureFact) -> bool:
    try:
        workspace_id, person = _scope(session, row.venture_id)
        if (row.workspace_id, row.person_id) != (workspace_id, person):
            return False
        source = json.loads(row.source_json)
        proof, source_at, _ = _proof(session, venture_id=row.venture_id, source=source)
        return (row.value == source["quote"] and row.value_json == _json(row.value)
                and row.source_digest == proof and _time(row.valid_from) == source_at
                and row.subject == normalize_key(row.subject) and row.predicate == normalize_key(row.predicate, limit=80)
                and (source["type"] == "chat" or row.subject.startswith(source["type"] + "."))
                and row.dedupe_key == _digest({"subject": row.subject, "predicate": row.predicate,
                    "source_type": source["type"], "ref": source["ref"]}))
    except (ValueError, TypeError, KeyError, AttributeError, PermissionError):
        return False


def query_facts(session, *, venture_id: str, subject: str | None = None, predicate: str | None = None,
                include_history: bool = False, as_of: datetime | None = None,
                recorded_as_of: datetime | None = None, limit: int = 100) -> list[VentureFact]:
    workspace_id, person = _scope(session, venture_id)
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("memory_fact_limit_invalid")
    from app.memory.normalize import facts_enabled
    if not facts_enabled():
        return []
    from app.db.fact_models import VentureFact
    query = select(VentureFact).where(VentureFact.workspace_id == workspace_id,
        VentureFact.venture_id == venture_id, VentureFact.person_id == person, VentureFact.forgotten_at.is_(None))
    from app.memory.normalize import enabled, subject_aliases, subject_key
    canonical = enabled("memory_capture_v1") or enabled("memory_conflicts_v1")
    if subject is not None:
        subject = normalize_key(subject)
        query = query.where(VentureFact.subject.in_(subject_aliases(subject)) if canonical else VentureFact.subject == subject)
    if predicate is not None:
        query = query.where(VentureFact.predicate == normalize_key(predicate, limit=80))
    known_at = _time(recorded_as_of) if recorded_as_of is not None else None
    if not include_history:
        if as_of is not None:
            at = _time(as_of)
            valid = VentureFact.valid_to.is_(None) | (VentureFact.valid_to > at)
            if known_at is not None:
                # A later correction had not closed this source at the requested
                # system time, even when its event-time interval was retroactive.
                valid = valid | (VentureFact.superseded_at > known_at)
            query = query.where(VentureFact.valid_from <= at, valid)
        elif recorded_as_of is None:
            query = query.where(VentureFact.valid_to.is_(None))
    if recorded_as_of is not None:
        query = query.where(VentureFact.recorded_at <= known_at)
        if not include_history and as_of is None:
            query = query.where(VentureFact.superseded_at.is_(None) | (VentureFact.superseded_at > known_at))
    rows = session.scalars(query.order_by(VentureFact.valid_from.desc(), VentureFact.recorded_at.desc(), VentureFact.id).limit(limit)).all()
    kept, keys = [], set()
    for row in rows:
        key = (row.subject, row.predicate)
        if canonical:
            key = (subject_key(row.subject), "statement" if row.predicate == "is" else row.predicate)
        if not include_history and key in keys:
            continue
        # A withdrawn newest source must abstain instead of restoring an older
        # value whose historical event interval overlaps a late-delivered fact.
        keys.add(key)
        if source_current(session, row):
            kept.append(row)
    return kept


def forget_fact(session, *, venture_id: str, fact_id: str) -> None:
    """Tombstone only the current actor's fact, retaining the original source."""
    from app.memory.normalize import facts_enabled
    if not facts_enabled():
        raise ValueError("memory_facts_disabled")
    from app.db.fact_models import VentureFact
    serialize_writes(session, key="venture-facts:" + venture_id + ":" + current().actor)
    workspace_id, person = _scope(session, venture_id)
    row = session.scalar(select(VentureFact).where(VentureFact.id == fact_id,
        VentureFact.workspace_id == workspace_id, VentureFact.venture_id == venture_id,
        VentureFact.person_id == person))
    if not row:
        raise KeyError("memory_fact_not_found")
    if row.forgotten_at is None:
        row.forgotten_at = _time(utcnow())
        session.flush()


def project_fact(row: VentureFact) -> dict:
    source = json.loads(row.source_json)
    return {"id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id,
            "person_id": row.person_id, "subject": row.subject, "predicate": row.predicate,
            "value": row.value, "source": source, "confidence": row.confidence,
            "valid_from": as_utc(row.valid_from).isoformat(),
            "valid_to": as_utc(row.valid_to).isoformat() if row.valid_to else None,
            "recorded_at": as_utc(row.recorded_at).isoformat(),
            "superseded_at": as_utc(row.superseded_at).isoformat() if row.superseded_at else None,
            "forgotten_at": as_utc(row.forgotten_at).isoformat() if row.forgotten_at else None,
            "qualification": "founder_statement_context_only" if source["type"] == "chat" else "source_observation_context_only",
            "effect_authority": False}
