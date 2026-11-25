"""Founder progress from saved evidence, separate from the internal pipeline."""
from __future__ import annotations

import hashlib
import json
import logging
import re
from contextvars import Context
from enum import StrEnum

from sqlalchemy import func, select

from app.db.models import GtmContact, GtmTouch, Venture, VentureDocument, Workspace
from app.db.session import serialize_writes
from app.idea.agent import ASSUMED
from app.state import db
from core.clock import utcnow


class FounderStage(StrEnum):
    EXPLORING = "exploring"
    IDEA = "idea"
    BUILDING = "building"
    PAGE_LIVE = "page_live"
    OUTREACH = "outreach"
    TRACTION = "traction"


LABELS = {"exploring": "Exploring", "idea": "Idea", "building": "Building",
          "page_live": "Landing page live", "outreach": "Outreach", "traction": "Traction"}
OLD_STAGES = {"unknown": "exploring", "idea": "idea", "code": "building",
              "landing": "page_live", "live": "page_live", "customers": "traction"}
log = logging.getLogger(__name__)


def _json(raw):
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def _founder_words(text) -> str:
    """Drop Supymem's assumed picks ("Assumed: ...") so only the founder's own lines count as evidence."""
    return "\n".join(line for line in str(text).splitlines()
                     if not line.lstrip().removeprefix("Clarification:").lstrip().startswith(ASSUMED))


def has_existing_customers(session, venture_id: str) -> bool:
    from app.intake.smart import _has_customers

    intake = session.get(VentureDocument, f"intake_decision:{venture_id}")
    extracted = {}
    if intake and intake.venture_id == venture_id and intake.kind == "intake_decision":
        extracted = _json(intake.body_md).get("extracted") or {}
        extracted = extracted if isinstance(extracted, dict) else {}
    venture = session.get(Venture, venture_id)
    raw = [str(text) for text in (extracted.get("idea"), extracted.get("audience"), extracted.get("stage_evidence"),
                                  getattr(venture, "raw_idea", None)) if text]
    assumed = any(_founder_words(text) != text for text in raw)
    texts = [_founder_words(text) for text in raw]
    # The founder's own words count even when intake extraction missed them ("140 paying subscribers").
    if any(_has_customers(text) for text in texts):
        return True
    if extracted.get("has_customers") is True or extracted.get("stage") == "customers":
        # Before #1092, "designers who invoice 15 clients" was saved as the founder having customers.
        # Re-check English ventures against their own saved words; other languages relied on the model.
        intake_texts = [text for text in (extracted.get("idea"), extracted.get("audience"), extracted.get("stage_evidence")) if text]
        return not (intake_texts and extracted.get("language") in (None, "", "en") or assumed)
    return False


def _present_answer(text: str) -> bool:
    value = str(text or "").strip()
    if not value or value.startswith(ASSUMED) or re.search(r"\b(?:no|not|none|nothing|never|skip|unsure|unknown|haven.t|hasn.t|don.t|doesn.t)\b|\b(?:planning to|plan to|going to|want to|will build|would build)\b", value, re.I):
        return False
    count = re.search(r"\b(\d+)\b", value)
    return int(count[1]) > 0 if count else True


def derive_stage(signals: dict) -> str:
    """Highest observed progress wins; a reply alone does not prove product use."""
    extracted = signals.get("extracted") or {}
    texts = [str(text) for text in (extracted.get("idea"), extracted.get("audience"), extracted.get("stage_evidence"), signals.get("idea")) if text]
    if any(_founder_words(text) != text for text in texts):
        from app.intake.smart import _has_customers
        extracted = {**extracted, "has_customers": any(_has_customers(_founder_words(text)) for text in texts),
                     "stage": "idea" if extracted.get("stage") == "customers" else extracted.get("stage")}
    if signals.get("signups") or extracted.get("has_customers") is True or extracted.get("stage") == "customers":
        return FounderStage.TRACTION.value
    has_site = bool(signals.get("live_url") or extracted.get("existing_site_url"))
    if signals.get("contacts") or signals.get("outbound") or signals.get("replies") or not has_site and (signals.get("entry_goal") == "gtm" or signals.get("path") == "gtm"):
        return FounderStage.OUTREACH.value
    if signals.get("live_url") or extracted.get("existing_site_url") or extracted.get("stage") in {"landing", "live"}:
        return FounderStage.PAGE_LIVE.value
    if signals.get("repo") or signals.get("prototype") or extracted.get("repo_url") or extracted.get("stage") == "code":
        return FounderStage.BUILDING.value
    if extracted.get("idea") or signals.get("idea") or extracted.get("stage") == "idea":
        return FounderStage.IDEA.value
    return FounderStage.EXPLORING.value


def preview_stage(decision: dict) -> dict:
    stage = derive_stage(decision)
    extracted = decision.get("extracted") or {}
    evidence = [{"source": "intake", "timestamp": utcnow().isoformat(),
                 "link": extracted.get("existing_site_url") or extracted.get("repo_url"),
                 "detail": extracted.get("stage_evidence") or "Your supplied assets and answers"}]
    return {"stage": stage, "label": LABELS[stage], "confidence": decision.get("confidence", 0.5),
            "evidence": evidence, "confirmed_by_founder": False, "updated_at": evidence[0]["timestamp"], "history": []}


def _signals(session, venture):
    from app.db.import_models import ImportRun
    from app.db.supymem_models import Deployment, FounderApp, VentureWaitlistEntry

    vid, wid = venture.id, venture.workspace_id
    intake = session.get(VentureDocument, f"intake_decision:{vid}")
    decision = _json(intake.body_md) if intake and intake.venture_id == vid else {}
    extracted = decision.get("extracted") or {}
    if isinstance(extracted, dict) and (extracted.get("has_customers") is True or extracted.get("stage") == "customers") \
            and not has_existing_customers(session, vid):
        extracted = {**extracted, "has_customers": None, "stage": "idea"}
    signals = {"extracted": extracted, "path": decision.get("path"), "idea": extracted.get("idea")}
    evidence = []
    if intake and intake.venture_id == vid:
        evidence.append({"source": "intake", "timestamp": intake.updated_at.isoformat(),
                         "link": extracted.get("existing_site_url") or extracted.get("repo_url"),
                         "detail": extracted.get("stage_evidence") or "Your supplied assets and answers"})
    else:
        # A saved company brief is evidence; a blank venture remains exploration.
        signals["idea"] = (venture.raw_idea or "").strip()
        if signals["idea"]:
            evidence.append({"source": "venture_brief", "timestamp": venture.updated_at.isoformat(),
                             "link": f"/v/{vid}/idea", "detail": "Your saved product brief"})
    workspace = session.get(Workspace, wid) if wid else None
    settings = _json(workspace.settings_json) if workspace else {}
    if settings.get("intake_venture_id") in {None, vid}:
        signals["entry_goal"] = settings.get("entry_goal")
    assets = session.get(VentureDocument, f"founder_assets:{vid}")
    if assets and assets.venture_id == vid and assets.kind == "founder_assets":
        supplied = _json(assets.body_md)
        built = str(supplied.get("built") or "").strip()
        contacts = str(supplied.get("contacts") or "").strip()
        signals["prototype"] = _present_answer(built)
        if _present_answer(contacts):
            signals["entry_goal"] = "gtm"
        evidence.append({"source": "founder_assets", "timestamp": assets.updated_at.isoformat(),
                         "link": None, "detail": "Your supplied asset answers"})
    site = session.get(VentureDocument, f"external_site:{vid}")
    if site and site.venture_id == vid and site.kind == "external_site" and site.path:
        checked = _json(site.body_md)
        verified = checked.get("status") == "verified" or not checked.get("status") and extracted.get("existing_site_url") == site.path
        if checked.get("status") == "unverified":
            extracted = {**extracted, "existing_site_url": None}
            if extracted.get("stage") in {"landing", "live"}:
                extracted["stage"] = "unknown"
            signals["extracted"] = extracted
        signals["live_url" if verified else "pending_site"] = site.path
        evidence.append({"source": "external_site" if verified else "website_unverified", "timestamp": site.updated_at.isoformat(), "link": site.path,
                         "detail": "Readable public website" if verified else "Website supplied by you; public page not checked"})
    publication = session.scalar(select(Deployment).join(FounderApp, Deployment.app_id == FounderApp.id).where(
        Deployment.workspace_id == wid, Deployment.venture_id == vid, Deployment.state == "live",
        Deployment.target == "supymem_static", Deployment.build_artifact_id.is_not(None),
        FounderApp.workspace_id == wid, FounderApp.venture_id == vid, FounderApp.deleted_at.is_(None))
        .order_by(Deployment.version.desc()).limit(1))
    from app.org.summary import published_site_is_public
    if publication and published_site_is_public(True, publication.url):
        signals["live_url"] = publication.url
        evidence.append({"source": "publication", "timestamp": (publication.live_at or publication.started_at or venture.updated_at).isoformat(),
                         "link": publication.url, "detail": "Published landing page"})
    imports = session.scalars(select(ImportRun).join(FounderApp, ImportRun.app_id == FounderApp.id).where(ImportRun.workspace_id == wid, ImportRun.venture_id == vid,
        ImportRun.state.in_(("report_complete", "partial_report")), ImportRun.source_snapshot_id.is_not(None))
        .where(FounderApp.workspace_id == wid, FounderApp.venture_id == vid, FounderApp.deleted_at.is_(None))
        .order_by(ImportRun.updated_at.desc()))
    imported = next((row for row in imports if not _json(row.acquisition_json).get("deleted")
                     and not (_json(row.acquisition_json).get("retired") or {}).get("source")), None)
    if imported:
        signals["repo"] = imported.source_snapshot_id
        evidence.append({"source": "repository_import", "timestamp": imported.updated_at.isoformat(),
                         "link": f"/v/{vid}/import", "detail": "Saved repository or prototype import"})
    counts = (
        ("contacts", "Saved contacts", GtmContact, GtmContact.stage_changed_at,
         (GtmContact.workspace_id == wid, GtmContact.source.startswith(f"venture:{vid}:", autoescape=True))),
        ("outbound", "Sent outreach", GtmTouch, GtmTouch.sent_at,
         (GtmTouch.workspace_id == wid, GtmTouch.venture_id == vid, GtmTouch.simulated == 0,
          GtmTouch.direction == "out", GtmTouch.sent_at.is_not(None))),
        ("replies", "Received replies", GtmTouch, GtmTouch.created_at,
         (GtmTouch.workspace_id == wid, GtmTouch.venture_id == vid, GtmTouch.simulated == 0,
          GtmTouch.direction == "in", GtmTouch.classification != "automated")),
        ("signups", "Real waitlist signups", VentureWaitlistEntry, VentureWaitlistEntry.created_at,
         (VentureWaitlistEntry.workspace_id == wid, VentureWaitlistEntry.venture_id == vid, VentureWaitlistEntry.is_test.is_(False))),
    )
    for key, label, model, timestamp, predicates in counts:
        total, latest = session.execute(select(func.count(), func.max(timestamp)).select_from(model).where(*predicates)).one()
        signals[key] = total
        if total:
            evidence.append({"source": key, "timestamp": (latest or venture.updated_at).isoformat(),
                             "link": f"/gtm?venture={vid}" if key != "signups" else f"/v/{vid}/site",
                             "detail": f"{label}: {total}"})
    return signals, evidence


def _save(session, venture, *, requested: str | None = None, commit: bool = True, persist: bool = True) -> dict:
    signals, evidence = _signals(session, venture)
    signature = hashlib.sha256(json.dumps({"signals": signals, "evidence": evidence}, sort_keys=True).encode()).hexdigest()
    identity, now = f"founder_stage:{venture.id}", utcnow()
    row = session.get(VentureDocument, identity)
    old = _json(row.body_md) if row and row.venture_id == venture.id and row.kind == "founder_stage" else {}
    observed = derive_stage(signals)
    stage = requested or (old.get("stage") if old.get("confirmed_by_founder") and old.get("observed") == signature else observed)
    stage = FounderStage(stage).value
    if old.get("observed") == signature and (not requested or requested == old.get("stage") and old.get("confirmed_by_founder")):
        return {k: v for k, v in old.items() if k != "observed"}
    stamp = now.isoformat() if persist else max([venture.updated_at.isoformat(), old.get("updated_at") or "",
                                               *(item["timestamp"] for item in evidence)])
    for item in evidence:
        if not item["timestamp"]:
            item["timestamp"] = stamp
    history = old.get("history", [])
    changed = old.get("stage") != stage
    if changed or requested:
        history = [*history, {"stage": stage, "previous": old.get("stage"), "timestamp": stamp,
                              "source": "founder_confirmation" if requested else "saved_evidence"}]
    data = {"stage": stage, "label": LABELS[stage], "confidence": 1.0 if requested else 0.9 if evidence else 0.5,
            "evidence": evidence, "confirmed_by_founder": bool(requested or old.get("confirmed_by_founder") and not changed), "updated_at": stamp,
            "history": history, "observed": signature}
    if changed and old:
        data["message"] = f"You're now at {LABELS[stage]}."
    if not persist:
        return {k: v for k, v in data.items() if k != "observed"}
    if row is None:
        row = VentureDocument(id=identity, venture_id=venture.id, kind="founder_stage", title="Founder stage",
                              created_at=now, updated_at=now)
        session.add(row)
    elif row.venture_id != venture.id or row.kind != "founder_stage":
        raise ValueError("founder_stage_scope_mismatch")
    row.body_md, row.updated_at = json.dumps(data), now
    if commit:
        session.commit()
    return {k: v for k, v in data.items() if k != "observed"}


def reassess(venture_id: str, *, session=None) -> dict:
    if session is None:
        with db()() as owned:
            result = reassess(venture_id, session=owned)
            owned.commit()
            return result
    serialize_writes(session, key=f"founder-stage:{venture_id}")
    venture = session.get(Venture, venture_id)
    if not venture or venture.archived_at or venture.stage in {"archived", "purged"}:
        raise KeyError(venture_id)
    return _save(session, venture, commit=False)


def get_stage(venture_id: str, *, session=None) -> dict:
    """Read-only projection; getters never create or flush a stage document."""
    if session is None:
        with db()() as owned:
            return get_stage(venture_id, session=owned)
    venture = session.get(Venture, venture_id)
    if not venture or venture.archived_at or venture.stage in {"archived", "purged"}:
        raise KeyError(venture_id)
    return _save(session, venture, persist=False, commit=False)


def seed_stage(venture_id: str, decision: dict) -> dict:
    """The intake decision is already durable; re-read it rather than trust a caller."""
    return reassess(venture_id)


def confirm_stage(venture_id: str, stage_name: str) -> dict:
    FounderStage(stage_name)
    with db()() as session:
        serialize_writes(session, key=f"founder-stage:{venture_id}")
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage in {"archived", "purged"}:
            raise KeyError(venture_id)
        return _save(session, venture, requested=stage_name)


def evidence_context(venture_id: str, event_key: str, workspace_id: str | None = None, *, session=None, custody=False) -> dict:
    """Re-resolve one committed event and its current owner, never a caller claim."""
    from app.db.supymem_models import Deployment, FounderApp, VentureWaitlistEntry
    if not isinstance(event_key, str) or not 1 <= len(event_key) <= 256 or ":" not in event_key:
        raise ValueError("stage_evidence_invalid")
    kind, identity = event_key.split(":", 1)
    if session is None:
        with db()() as owned:
            return evidence_context(venture_id, event_key, workspace_id, session=owned, custody=custody)
    venture = session.get(Venture, venture_id, with_for_update=custody, populate_existing=custody)
    workspace = session.get(Workspace, venture.workspace_id, with_for_update=custody, populate_existing=custody) if venture else None
    try:
        settings = json.loads(workspace.settings_json or "{}") if workspace else None
    except (ValueError, TypeError):
        settings = None
    if (not venture or venture.archived_at or venture.stage in {"archived", "purged"}
            or not workspace or workspace.archived_at or not workspace.owner_id
            or not isinstance(settings, dict) or settings.get("blocked")
            or workspace_id is not None and workspace.id != workspace_id):
        raise ValueError("stage_evidence_invalid")
    if kind == "waitlist.signup":
        row = session.get(VentureWaitlistEntry, identity)
        valid = bool(row and not row.is_test)
    elif kind == "gtm.reply":
        row = session.get(GtmTouch, identity)
        contact = session.get(GtmContact, row.contact_id) if row else None
        valid = bool(row and row.simulated == 0 and row.direction == "in"
                     and row.classification in {"human_positive", "human_neutral", "human_negative"}
                     and contact and contact.workspace_id == workspace.id)
    elif kind == "publication":
        row = session.get(Deployment, identity)
        app = session.get(FounderApp, row.app_id) if row else None
        valid = bool(row and row.state == "live" and row.live_at and row.url
                     and row.target == "supymem_static" and row.build_artifact_id
                     and app and not app.deleted_at and app.workspace_id == workspace.id and app.venture_id == venture_id)
    else:
        raise ValueError("stage_evidence_invalid")
    if not valid or row.workspace_id != workspace.id or row.venture_id != venture_id:
        raise ValueError("stage_evidence_invalid")
    return {"venture_id": venture_id, "workspace_id": workspace.id, "owner_person_id": workspace.owner_id}


def reassess_evidence(venture_id: str, event_key: str, workspace_id: str) -> dict:
    """Worker ABI: actual event plus current server-owned scope, no paid effects."""
    from app.tenancy import current
    from app.authz.workspace import subject_person_id
    scope = current()
    with db()() as session:
        serialize_writes(session, key=f"founder-stage:{venture_id}")
        context = evidence_context(venture_id, event_key, workspace_id, session=session, custody=True)
        if (scope.workspace_id != workspace_id or scope.venture_id != venture_id
                or subject_person_id(scope.actor) != context["owner_person_id"]):
            raise PermissionError("stage_reassessment_scope_mismatch")
        result = _save(session, session.get(Venture, venture_id), commit=False)
        session.commit()
        return result


def after_evidence(venture_id: str, event_key: str) -> bool:
    """Postcommit convenience cannot undo the original event's durable success."""
    try:
        evidence_context(venture_id, event_key)
        reassess(venture_id)
        return True
    except Exception as exc:
        log.warning("founder_stage_pending", extra={"error_class": type(exc).__name__})
    try:
        def queue_retry():
            from app.core.jobs import enqueue
            from app.tenancy import bind
            context = evidence_context(venture_id, event_key)
            # A background projection inherits no private chat or paid-call context.
            with bind(workspace_id=context["workspace_id"], venture_id=venture_id, actor=context["owner_person_id"]), db()() as session:
                current_owner = session.get(Workspace, context["workspace_id"])
                if not current_owner or current_owner.owner_id != context["owner_person_id"] or current_owner.archived_at:
                    raise ValueError("stage_evidence_invalid")
                enqueue(session, "milestone.reassess", {"event_key": event_key},
                        workspace_id=context["workspace_id"], venture_id=venture_id,
                        dedupe_key=f"stage.evidence:{event_key}", max_attempts=2)
                session.commit()
        Context().run(queue_retry)
    except Exception as exc:
        log.warning("founder_stage_retry_unavailable", extra={"error_class": type(exc).__name__})
    return False
