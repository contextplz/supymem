"""Adjacent ideas from saved public scans, never automatic venture creation."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
from urllib.parse import urlsplit

from sqlalchemy import select

from app.chat.recovery import log_founder_exception
from app.core.local_mode import is_local
from app.db.models import Venture, VentureDocument
from app.db.supymem_models import Experiment
from app.db.session import serialize_writes
from app.experiments import ledger
from app.research.source_quality import _stems
from app.state import db
from core.clock import utcnow
from core.copy import founder_copy

KIND = "experiment_spinoff_proposals"
_PAIN = r"(?:struggles? with|needs? help (?:with|to)|(?:have|has) to|uses? [^.!?;]{1,80}? (?:to|for))"
_CLAIM = re.compile(r"^(?:As (?:an? )?(?P<self>[\w '\-]{3,80}), (?:I|we)|"
                    r"(?P<who>[\w '\-]{3,80}?))\s+" + _PAIN + r"\s+(?P<problem>[^.!?;]{3,180})[.!?;]?$", re.I)
_PRONOUN = re.compile(r"\b(?:i|we|you|they|it|he|she|someone)\b", re.I)


def _proposals(scans: list[dict], icp: dict) -> list[dict]:
    from app.experiments.autonomous import _uncertain_claim

    audience, problem = _stems(icp.get("who") or ""), _stems(icp.get("problem") or "")
    if not audience or not problem:
        return []
    clusters = {}
    # shortcut: explicit English audience/pain clauses only, upgrade when saved scans carry reviewed semantic clusters.
    for scan in scans:
        for source in scan["sources"]:
            if not isinstance(source, dict):
                continue
            quote, url = source.get("quote"), source.get("url")
            if (not isinstance(quote, str) or not isinstance(url, str)
                    or re.search(r'[\s<>"()\[\]]', url) or _uncertain_claim(quote)):
                continue
            try:
                parsed = urlsplit(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                    continue
            except ValueError:
                continue
            for passage in re.split(r"\n+|(?<=[.!?;])\s+", quote):
                match = _CLAIM.fullmatch(passage.strip())
                if not match:
                    continue
                who = match["self"] or match["who"]
                pain = match["problem"].strip(" .!?;")
                if _PRONOUN.search(who):
                    continue
                who_terms, pain_terms = _stems(who), _stems(pain)
                if not who_terms or not pain_terms or (audience & who_terms and problem & pain_terms):
                    continue
                key = (tuple(sorted(who_terms)), tuple(sorted(pain_terms)))
                cluster = clusters.setdefault(key, {"who": founder_copy(who), "problem": founder_copy(pain), "evidence": {}})
                evidence_key = (url, passage.strip())
                cluster["evidence"].setdefault(evidence_key, {"experiment_id": scan["id"], "url": url,
                    "title": founder_copy(source.get("title") or parsed.hostname), "quote": passage.strip()})
    proposals = []
    for cluster in clusters.values():
        evidence = sorted(cluster["evidence"].values(), key=lambda item: (item["url"], item["quote"]))
        strength = len({item["url"] for item in evidence})
        reach = len({urlsplit(item["url"]).hostname.removeprefix("www.") for item in evidence})
        idea = f"Help {cluster['who']} with {cluster['problem']}."
        proposals.append({"idea": idea, "who": cluster["who"], "problem": cluster["problem"], "evidence": evidence,
            "evidence_strength": strength, "reach": reach, "score": strength * reach,
            "first_experiment": f"You could run a public complaint scan for {cluster['who']} about {cluster['problem']}. "
                                "Check for independent accounts of the same difficulty. Nothing is sent or published."})
    return sorted(proposals, key=lambda item: (-item["score"], item["idea"]))[:5]


def _display(text: str) -> str:
    return re.sub(r"([\\`*_<>\[\]])", r"\\\1", founder_copy(" ".join(text.split())))


async def on_scan_completed(spec: dict) -> list[dict]:
    from app.experiments.autonomous import RECIPES

    if spec.get("scan") not in RECIPES:
        return []
    return await asyncio.to_thread(consume, spec["venture_id"], spec["workspace_id"], spec["id"])


def _enabled() -> bool:
    from app.experiments import autonomous

    return ledger.autopilot_enabled() and (not is_local() or autonomous._local_enabled("run_for_me_experiments"))


def consume(venture_id: str, workspace_id: str, experiment_id: str) -> list[dict]:
    try:
        if not _enabled():
            return []
        from app.chat.experiments import _decision_card, _decision_payload
        from app.chat.milestones import post_milestone
        from app.experiments import autonomous

        snapshot = autonomous._snapshot(venture_id)
        if not snapshot or snapshot["workspace_id"] != workspace_id:
            return []
        with db()() as session:
            venture = session.get(Venture, venture_id)
            if (not venture or venture.workspace_id != workspace_id or venture.archived_at
                    or venture.stage in {"paused", "archived", "purged"}):
                return []
            direction = ledger.current_direction(session, venture_id)
            if snapshot["direction"] != direction:
                return []
            scans, scanned_contexts = [], {}
            for row in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id,
                    Experiment.workspace_id == workspace_id, Experiment.kind == ledger.KIND,
                    Experiment.state == "concluded").order_by(Experiment.id)):
                context = ledger._ctx(row)
                spec, result = context.get("spec") or {}, context.get("result") or {}
                if (context.get("runner") != "system" or spec.get("scan") not in autonomous.RECIPES
                        or spec.get("direction", "") != direction or context.get("error") or result.get("error")
                        or not isinstance(result.get("sources"), list)):
                    continue
                scans.append({"id": row.id, "sources": result["sources"]})
                scanned_contexts[row.id] = row.context
            trigger = next((scan for scan in scans if scan["id"] == experiment_id), None)
            if trigger is None:
                return []
            proposals = _proposals(scans, snapshot["journey"].get("icp") or {})
            if not proposals:
                return []
        state = ledger.loop_state(venture_id)
        with db()() as session:
            serialize_writes(session, key="market-experiments:" + venture_id)
            venture = session.get(Venture, venture_id)
            if (not _enabled() or not venture or venture.workspace_id != workspace_id
                    or venture.archived_at or venture.stage in {"paused", "archived", "purged"}
                    or state["direction"] != direction or ledger.current_direction(session, venture_id) != direction):
                return []
            for identifier, saved_context in scanned_contexts.items():
                row = session.get(Experiment, identifier)
                if (not row or (row.venture_id, row.workspace_id, row.kind, row.state) !=
                        (venture_id, workspace_id, ledger.KIND, "concluded") or row.context != saved_context):
                    return []
            if session.scalar(select(Experiment.id).where(
                    Experiment.id.in_([row["id"] for row in state["done"]]),
                    Experiment.workspace_id != workspace_id).limit(1)):
                return []
            origin = _decision_payload(venture_id, "experiment.spinoff", state)
            saved = {"direction": direction, "proposals": proposals, "origin": origin}
            provenance = json.dumps(saved, sort_keys=True)
            document_id = "esp_" + hashlib.sha256(f"{venture_id}:{direction}".encode()).hexdigest()[:24]
            document = session.get(VentureDocument, document_id)
            if document and document.provenance_json == provenance:
                return proposals
            lines = ["Your saved research suggests these ideas you could try separately. You choose what happens next."]
            actions, citations = [], []
            for proposal in proposals:
                lines.extend([f"\n**{_display(proposal['idea'])}**", f"For: {_display(proposal['who'])}.",
                    f"Priority: {proposal['score']} ({proposal['evidence_strength']} saved pages x {proposal['reach']} sites). "
                    "This compares your sources, not market size.", _display(proposal["first_experiment"])])
                for index, evidence in enumerate(proposal["evidence"], 1):
                    lines.append(f"> {_display(evidence['quote'])}\n[Source {index}]({evidence['url']})")
                    citations.append({"url": evidence["url"], "title": evidence["title"], "quote": evidence["quote"]})
                candidate = {"kind": "different_customer", "text": f"{proposal['who']}: {proposal['problem']}",
                             "idea": proposal["idea"]}
                actions.append(_decision_card(venture_id, "experiment.spinoff", state,
                    f"Try separately: {candidate['text']}", [proposal["idea"], proposal["first_experiment"],
                    "You confirm before a separate venture is created. Your current venture stays unchanged. "
                    "No email, publishing or other outbound approval carries over."], candidate))
            body = "\n".join(lines)
            now = utcnow()
            if document is None:
                document = VentureDocument(id=document_id, venture_id=venture_id, kind=KIND,
                    title="Ideas you could try separately", created_at=now, updated_at=now)
                session.add(document)
            document.body_md, document.provenance_json, document.updated_at = body, provenance, now
            post_milestone(venture_id, key="spinoffs:" + hashlib.sha256(provenance.encode()).hexdigest(),
                kind="confirmation", body_md=body, actions=actions, citations=citations, session=session)
            session.commit()
            return proposals
    except Exception as exc:
        log_founder_exception(exc, venture_id, "experiment.spinoffs.propose")
        return []
