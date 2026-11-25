"""Registered task workflows and read-only checks of their durable receipts.

Admission and execution stay in the existing workflows. A handler returning is
not a done-test, and an uncertain effect is never dispatched by this module.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
import re
from types import MappingProxyType
from typing import Any, Iterable
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select

from app.db.models import GtmContact, Job, PlannedTask, Venture, VentureDocument


@dataclass(frozen=True)
class Skill:
    name: str
    title: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    cost_class: str
    allowed_tools: frozenset[str]
    runs_on: str = "server"
    consequential: bool = False

    @property
    def needs_mac(self) -> bool:
        return self.runs_on == "device"


SKILLS = MappingProxyType({skill.name: skill for skill in (
    Skill("research.deep", "Research your idea", ("session_id", "revision_id"),
          ("research_dossier",), "metered", frozenset({"explore_web"})),
    Skill("pagebuilder.build", "Prepare a website preview", ("revision_id",),
          ("pagebuilder_native_reference_v1",), "metered", frozenset({"propose_config"})),
    Skill("pagebuilder.publish", "Publish your approved website", ("build_run_id", "approval_digest"),
          ("deployment",), "metered", frozenset({"propose_config", "check_policy"}), consequential=True),
    Skill("gtm.journey.prepare", "Find prospects and prepare drafts", ("journey_id", "prepare_id"),
          ("gtm_journey", "outreach_draft"), "metered", frozenset({"explore_web"})),
    Skill("device.work_order", "Run a reviewed Mac task", ("device_order_id",), ("native_stop_receipt",), "metered",
          frozenset({"device.computer_use", "device.coding"}), runs_on="device", consequential=True),
    Skill("tasks.reply_draft", "Draft a follow-up for review", ("reply_touch_id",), ("task_reply_draft",), "metered", frozenset()),
    Skill("tasks.waitlist_summary", "Summarize signups and replies", (), ("task_report",), "free", frozenset()),
    Skill("task.run", "Prepare the requested work", ("task_id",),
          ("task_note",), "metered", frozenset()),
)})


def get_skill(name: str) -> Skill | None:
    if name in SKILLS:
        return SKILLS[name]
    from app.tasks import goals
    return goals.catalog().get(name) if goals.is_action(name) and goals.enabled() else None


def allowed_tools(role: str) -> frozenset[str]:
    """Unknown roles have no authority; catalog permissions are the ceiling."""
    from app.org.catalog import load_catalog

    entry = next((item for item in load_catalog().get("roles", []) if item.get("role") == role), None)
    return frozenset(entry.get("allowed_tools", [])) if entry else frozenset()


def enforce_allowed_tools(role: str, requested_tools: Iterable[str]) -> tuple[str, ...]:
    if isinstance(requested_tools, (str, bytes)):
        raise ValueError("task_tools_invalid")
    requested = tuple(requested_tools)
    if any(not isinstance(tool, str) or not tool for tool in requested):
        raise ValueError("task_tools_invalid")
    requested = tuple(dict.fromkeys(requested))
    if not set(requested) <= allowed_tools(role):
        raise PermissionError("task_tool_not_allowed")
    return requested


def _json(raw: str | None) -> dict:
    value = json.loads(raw or "{}")
    if not isinstance(value, dict):
        raise ValueError("task_receipt_invalid")
    return value


def _result(*, verified=False, pending=False, evidence=(), blocker=None) -> dict[str, Any]:
    return {"verified": verified, "pending": pending, "evidence": list(evidence), "blocker": blocker}


def source_hash(url: str) -> str:
    """Same observed URL across steps has one private identity, not a UI link."""
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        raise ValueError("task_source_url_invalid")
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("task_source_url_invalid")
    host = parsed.hostname.lower().rstrip(".")
    if ":" in host:
        host = "[" + host + "]"
    port = parsed.port
    if port is not None and port != {"http": 80, "https": 443}[parsed.scheme]:
        host += ":" + str(port)
    canonical = urlunsplit((parsed.scheme.lower(), host, parsed.path or "/", parsed.query, ""))
    return hashlib.sha256(canonical.encode()).hexdigest()


def _blocked(reason: str, *, kind="impossible", action="Review the saved result and try again.", evidence=()) -> dict:
    return _result(evidence=evidence, blocker={"class": kind, "reason": reason, "unblock_action": action})


def _waiting(job, reason: str) -> dict:
    if job.status in {"pending", "scheduled", "retry_wait", "running", "leased"}:
        return _result(pending=True)
    return _blocked(reason)


def _research(session, job, payload):
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    from app.research.service import _public
    from app.ventures.next_step import research_source_hashes

    dossier = session.get(ResearchDossier, identifier(job.id))
    if dossier is None:
        return _waiting(job, "The research result was not saved.")
    doc = _json(dossier.summary)
    runtime = doc.get("_runtime", {})
    if ((dossier.workspace_id, dossier.venture_id, dossier.revision_id, dossier.kind)
            != (job.workspace_id, job.venture_id, payload["revision_id"], "deep")
            or (runtime.get("job_id"), runtime.get("session_id")) != (job.id, payload["session_id"])):
        return _blocked("The research result belongs to a different request.", kind="policy")
    evidence = [{"ref": "research_dossier:" + dossier.id, "state": dossier.state}]
    public = _public(dossier, session)
    if public["summary"]["usage"]["has_unsettled"]:
        if job.status in {"scheduled", "retry_wait", "leased"}:
            return _result(pending=True, evidence=evidence)
        return _blocked("Your research was interrupted.",
                        kind="needs_input", action="Open Research and choose Run research again, or ask to skip for now.", evidence=evidence)
    if public["state"] == "outdated":
        return _blocked("The research used an earlier version of your idea.", kind="needs_input",
                        action="Confirm the current idea and start fresh research.", evidence=evidence)
    if doc.get("error_code") or dossier.state == "failed":
        return _blocked("Your research was interrupted.", kind="needs_input",
                        action="Open Research and choose Run research again, or ask to skip for now.", evidence=evidence)
    if dossier.state not in {"complete", "partial"} or not dossier.completed_at:
        return _waiting(job, "The research did not reach a saved completion checkpoint.")
    hashes = research_source_hashes(session, dossier)
    count = len(hashes)
    if not count:
        return _blocked("Research finished without usable source evidence.", kind="needs_input",
                        action="Refine the research question or provide a source to review.", evidence=evidence)
    evidence[0].update(source_count=count, source_hashes=sorted(hashes), coverage_gap_count=len(doc.get("coverage_gaps", [])))
    return _result(verified=True, evidence=evidence)


def _page(session, job, _payload):
    from app.pagebuilder import native_reference as references

    doc = references._completed_candidate(session, job)
    count = doc.get("variant_count")
    if type(count) is not int or not 1 <= count <= 3:
        raise ValueError("pagebuilder_candidate_invalid")
    evidence = []
    for variant in range(count):
        rid = references._reference_id(job.id, variant)
        saved = session.get(VentureDocument, rid)
        intent = _json(saved.provenance_json) if saved else {}
        binding, _blob = references._encode(job, doc, variant)
        receipt = intent.get("receipt") or {}
        if (not saved or (saved.kind, saved.venture_id) != (references.KIND, job.venture_id)
                or intent.get("state") != "stored"
                or intent.get("binding") != binding
                or (receipt.get("kind"), receipt.get("sha256"), receipt.get("object_digest"), receipt.get("size_bytes"))
                != ("raw", binding["reference_sha256"], binding["reference_sha256"], binding["reference_size_bytes"])
                or not receipt.get("version_id") or receipt["version_id"] == "null"):
            raise ValueError("pagebuilder_candidate_invalid")
        evidence.append({"ref": "venture_document:" + rid, "variant": variant, "state": "stored"})
    # Candidate generation is the done-test here. Publication and independent
    # final-source QA remain separate, founder-approved workflow receipts.
    return _result(verified=True, evidence=evidence)


def _publish(session, job, payload):
    from app.builder.model_proxy import digest
    from app.db.supymem_models import BuildRun, Deployment
    from app.pagebuilder_admission import publication

    run = session.get(BuildRun, payload["build_run_id"])
    if not run or (run.workspace_id, run.venture_id) != (job.workspace_id, job.venture_id):
        return _blocked("The approved website source could not be verified.", kind="policy")
    spec = _json(run.spec)
    approval = publication._approved(run, spec)
    if (approval.get("job_id") != job.id or digest(approval["binding"]) != payload["approval_digest"]
            or approval["binding"].get("caller_subject") != _json(job.payload_json).get("_actor_subject")):
        return _blocked("The publication approval belongs to a different request.", kind="policy")
    view = publication._view(session, run, spec)
    evidence = [{"ref": "build_run:" + run.id, "state": view["state"]}]
    deployment = session.get(Deployment, approval.get("deployment_id")) if approval.get("deployment_id") else None
    if approval.get("deployment_id") and (not deployment or (
            deployment.workspace_id, deployment.venture_id, deployment.app_id,
            deployment.build_artifact_id, deployment.target) != (
            job.workspace_id, job.venture_id, run.app_id, run.artifact_id, "supymem_static")):
        return _blocked("The publication receipt belongs to a different source.", kind="policy", evidence=evidence)
    if view["state"] in {"checking", "publishing"}:
        return _result(pending=True, evidence=evidence)
    if view["state"] != "live":
        from app.tasks.contracts import blocker
        code = view.get("failure_code") or "pagebuilder_publication_refused"
        logging.getLogger(__name__).warning("pagebuilder_publication_verification_failed", extra={
            "job_id": job.id, "build_run_id": run.id, "error_code": code})
        return _result(blocker=blocker(code), evidence=evidence)
    if deployment is None:
        # A crash after queue_accepted_build may precede the approval's final
        # deployment-id checkpoint. Recover the authoritative source receipt.
        deployment = session.scalar(select(Deployment).where(
            Deployment.app_id == run.app_id, Deployment.workspace_id == job.workspace_id,
            Deployment.venture_id == job.venture_id, Deployment.build_artifact_id == run.artifact_id,
            Deployment.target == "supymem_static").order_by(Deployment.version.desc()).limit(1))
    if (not deployment or (deployment.workspace_id, deployment.venture_id, deployment.app_id,
            deployment.build_artifact_id, deployment.target, deployment.state)
            != (job.workspace_id, job.venture_id, run.app_id, run.artifact_id, "supymem_static", "live")
            or not deployment.live_at or deployment.url != view["live_url"]):
        return _blocked("The live publication receipt could not be verified.", evidence=evidence)
    return _result(verified=True, evidence=[*evidence, {"ref": "deployment:" + deployment.id, "state": "live"}])


def _gtm(session, job, payload):
    row = session.get(VentureDocument, payload["journey_id"])
    if not row or (row.venture_id, row.kind) != (job.venture_id, "gtm_journey"):
        return _blocked("The prospect research result could not be verified.", kind="policy")
    data = _json(row.provenance_json)
    if (data.get("job_id"), data.get("run_id")) != (job.id, payload["prepare_id"]):
        successor = session.get(Job, data.get("job_id")) if data.get("job_id") else None
        if successor and successor.created_at > job.created_at:
            saved = _json(successor.payload_json)
            if saved.get("journey_id") == row.id:
                receipt = verify_result(session, job.kind, {**saved, "_job_id": successor.id},
                    workspace_id=job.workspace_id, venture_id=job.venture_id)
                if receipt["verified"]:
                    return receipt
        return _blocked("The prospect research was replaced by a newer request.", kind="needs_input",
                        action="Review the current prospect research result.")
    evidence = [{"ref": "venture_document:" + row.id, "state": data.get("state", "unknown")}]
    if data.get("state") in {"queued", "running"}:
        return _waiting(job, "Prospect research did not reach a saved completion checkpoint.")
    prospects = session.scalars(select(GtmContact).where(GtmContact.workspace_id == job.workspace_id,
        GtmContact.source.startswith(f"venture:{job.venture_id}:", autoescape=True))).all()
    results, sources = {}, set()
    for key in ("reachable_leads", "channels", "need_signals"):
        results[key] = []
        for item in data.get(key) if isinstance(data.get(key), list) else []:
            if not isinstance(item, dict) or not isinstance(item.get("quote"), str) or not item["quote"].strip():
                continue
            try:
                digest = source_hash(item.get("source_url") or item.get("url"))
            except (ValueError, TypeError):
                continue
            sources.add(digest)
            results[key].append(item)
    if data.get("state") != "ready" or not (prospects or any(results.values())):
        if data.get("error_code") == "public_search_unavailable" or data.get("source_access") == "unavailable" or data.get("error_code") == "public_sources_unavailable":
            from app.research.local_providers import READER_UNAVAILABLE, SEARCH_UNAVAILABLE
            return _blocked(SEARCH_UNAVAILABLE if data.get("error_code") == "public_search_unavailable" else READER_UNAVAILABLE,
                kind="needs_input", action="Open Keys and connections > Search, check your connection, then retry Find customers.", evidence=evidence)
        return _blocked("Your customer search found no contacts yet.", kind="needs_input",
                        action="Open Find customers and retry with another public source, or add a contact you know.", evidence=evidence)
    evidence[0]["prospect_count"] = len(prospects)
    evidence[0].update(source_count=len(sources), source_hashes=sorted(sources),
        prospect_refs=["gtm_contact:" + prospect.id for prospect in prospects])
    evidence[0].update({key + "_count": len(value) for key, value in results.items()})
    # Preparing public candidates is not permission to send. Existing send
    # admission still requires exact draft review and founder batch approval.
    return _result(verified=True, evidence=evidence)


def _task(session, job, payload):
    row = session.get(PlannedTask, payload["task_id"])
    note = session.get(VentureDocument, "task-note:" + payload["task_id"])
    if not row or row.venture_id != job.venture_id or row.workspace_id not in {None, job.workspace_id}:
        return _blocked("The saved work belongs to a different request.", kind="policy")
    from app.tasks.contracts import requires_reviewed_action
    if requires_reviewed_action(row.title, row.detail or row.why_now or ""):
        # A local note cannot prove a booked, bought, sent, published or
        # destructive effect. Existing effect/native workflows own those receipts.
        return _blocked("The requested action needs its existing review and verified result.", kind="needs_approval",
                        action="Review the exact action in its approved workflow.")
    provenance = _json(note.provenance_json) if note else {}
    if provenance.get("research_receipt"):
        from app.planning.execute import verified_research_receipt
        receipt = verified_research_receipt(session, row, note, job.id)
        if ((receipt["mode"] == "count_verified" and (row.status != "done" or not row.finished_at))
                or (receipt["mode"] == "rubric_prepared" and row.status not in {"open", "done"})):
            return _blocked("The saved research has not reached its result checkpoint.")
        return _result(verified=True, evidence=[{"ref": "venture_document:" + note.id,
            "state": "prepared" if receipt["mode"] == "rubric_prepared" else "completed",
            "source_count": receipt["source_count"], "source_hashes": sorted({source_hash(item["url"]) for item in receipt["observed_sources"]})}])
    if row.status != "done" or not row.finished_at:
        return _blocked("The requested work did not pass its done-test.", kind="needs_input",
                        action="Review the saved work and its next action.")
    if (not note or (note.venture_id, note.kind, note.title) != (job.venture_id, "task_note", row.title)
            or provenance != {"task_id": row.id, "source": "task.run"}
            or not note.body_md):
        return _blocked("The completed work has no verified saved artifact.")
    body = note.body_md
    simulated = "Status: simulated draft prepared (not published)" in body
    draft_goal = bool(re.match(r"^(draft\b|(?:prepare|write)\b.*\bdrafts?\b)", row.title, re.IGNORECASE))
    if simulated and not draft_goal:
        return _blocked("Only a simulated draft was prepared; the requested action is unfinished.", kind="needs_approval",
                        action="Review the draft and explicitly approve the consequential action.")
    if not simulated and "Status: completed\n" not in body:
        return _blocked("The saved artifact does not confirm completed work.")
    return _result(verified=True, evidence=[{"ref": "venture_document:" + note.id,
        "state": "draft_prepared" if simulated else "completed"}])


_CHECKS = {"research.deep": _research, "pagebuilder.build": _page, "pagebuilder.publish": _publish,
           "gtm.journey.prepare": _gtm, "task.run": _task}


def verify_result(session, kind: str, payload: dict, *, workspace_id: str, venture_id: str) -> dict:
    """Check an owned workflow against DB receipts; never trust its return value."""
    from app.tasks import goals
    if goals.is_action(kind) and goals.enabled():
        return goals.verify_job(session, kind, payload, workspace_id=workspace_id, venture_id=venture_id)
    skill = get_skill(kind)
    job = session.get(Job, payload.get("_job_id"))
    venture = session.get(Venture, venture_id)
    if (not skill or not job or (job.kind, job.workspace_id, job.venture_id) != (kind, workspace_id, venture_id)
            or not venture or venture.workspace_id != workspace_id):
        return _blocked("The task result belongs to a different request.", kind="policy")
    try:
        saved = _json(job.payload_json)
        if any(not payload.get(key) or payload.get(key) != saved.get(key) for key in skill.inputs):
            return _blocked("The task inputs changed before verification.", kind="policy")
        return _CHECKS[kind](session, job, payload)
    except (KeyError, TypeError, ValueError) as exc:
        code = str(exc).partition(":")[0]
        if kind.startswith("pagebuilder."):
            from app.tasks.contracts import blocker
            code = code if re.fullmatch(r"pagebuilder_[a-z_]{1,80}", code) else "pagebuilder_candidate_invalid"
            logging.getLogger(__name__).warning("pagebuilder_task_verification_failed", extra={
                "job_id": job.id, "error_code": code})
            return _result(blocker=blocker(code))
        return _blocked("The saved result receipt is incomplete or invalid.")
