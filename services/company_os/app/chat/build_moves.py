"""Recommend requested coding work from saved evidence; never start it here."""
import json
import re

from sqlalchemy import select

from app.core.local_mode import is_local
from app.db.models import ChatMessage, GtmTouch, Venture, VentureDocument
from app.db.supymem_models import BuildRun, Experiment
from app.state import db

_BUG = re.compile(r"\b(?:bug|broken|crash(?:es|ed|ing)?|does(?:n['’]t| not) work)\b", re.I)
_RESOLVED = re.compile(r"\b(?:no (?:more )?bugs?|bug.{0,40}(?:fixed|resolved)|fixed.{0,40}bug)\b", re.I)
_DEMO = re.compile(r"\b(?:can|could|want|like|request|show|see|try)\b.{0,80}\bdemo\b", re.I)
_DECLINED = re.compile(r"\b(?:no|not|don['’]t|do not|never)\b.{0,40}\bdemo\b", re.I)
_DEMAND = re.compile(r"\b(?:demand|paid|paying|purchases?|signups?|sign[ -]?ups?|waitlist)\b", re.I)
_TOOLS = {"claude": "Claude Code", "codex": "Codex", "cursor": "Cursor", "opencode": "OpenCode"}


def is_repair_request(text: str) -> bool:
    return bool(isinstance(text, str) and len(text.encode()) <= 10000
                and re.fullmatch(r"Implement this requested fix with (?:claude|codex|cursor|opencode)\.\n\n\S[\s\S]*", text))


def recommendation(venture_id: str, state: dict) -> dict | None:
    from fastapi import HTTPException
    from app.admin.authn import CallerContext
    from app.build.device_orders import KIND, _active_orders
    from app.builder.ctl import local_tool
    from app.core.fsm import BUILD_ACTIVE
    from app.experiments import ledger
    from app.secrets.detect import looks_like_secret
    from app.workspaces.resolve import local_person
    from core.clock import as_utc, utcnow

    if not is_local() or state.get("paused"):
        return None
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage == "purged":
            return None
        scope = (BuildRun.workspace_id == venture.workspace_id, BuildRun.venture_id == venture_id)
        if session.scalar(select(BuildRun.id).where(*scope, BuildRun.state.in_(BUILD_ACTIVE)).limit(1)):
            return None
        tasks = {json.loads(row.provenance_json or "{}").get("manifest", {}).get("task_id")
                 for row in session.scalars(select(VentureDocument).where(
                     VentureDocument.venture_id == venture_id, VentureDocument.kind == KIND))}
        if any(next(_active_orders(session, venture_id, task, utcnow()), None) for task in tasks if task):
            return None
        direction = ledger.current_direction(session, venture_id)
        experiments = set(session.scalars(select(Experiment.id).where(
            Experiment.workspace_id == venture.workspace_id, Experiment.venture_id == venture_id,
            Experiment.kind == ledger.KIND)))
        last_build = session.scalar(select(BuildRun.finished_at).where(*scope, BuildRun.state == "succeeded")
            .order_by(BuildRun.finished_at.desc()).limit(1))
        owner = local_person().id
        # shortcut: scan the latest 20 owner messages, use saved bug records when reports need longer history.
        messages = session.scalars(select(ChatMessage).where(ChatMessage.workspace_id == venture.workspace_id,
            ChatMessage.venture_id == venture_id, ChatMessage.role == "user",
            ChatMessage.author_person_id.is_(None) | (ChatMessage.author_person_id == owner))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(20)).all()
        bug = None
        for message in messages:
            if (last_build and as_utc(message.created_at) <= as_utc(last_build)) or _RESOLVED.search(message.body_md):
                break
            if _BUG.search(message.body_md) and not looks_like_secret(message.body_md):
                bug = {"id": message.id, "text": message.body_md}
                break
        demo = next((touch.id for touch in session.scalars(select(GtmTouch).where(
            GtmTouch.workspace_id == venture.workspace_id, GtmTouch.venture_id == venture_id,
            GtmTouch.direction == "in", GtmTouch.simulated == 0,
            GtmTouch.classification.is_(None) | GtmTouch.classification.not_in(("automated", "opt_out", "not_interested"))))
            if _DEMO.search(touch.body_text or "") and not _DECLINED.search(touch.body_text or "")), None)
    if not bug and not state["idea"].get("confirmed"):
        return None
    rows = [row for row in ledger.evidence_for_venture(venture_id)
            if row["id"] in experiments and row.get("direction") == direction and not row.get("sample")]
    completed = sorted((row for row in rows if row["state"] == "concluded" and row.get("ended_at")
        and as_utc(row["ended_at"]) <= utcnow()), key=lambda row: (as_utc(row["ended_at"]), row["id"]))
    latest = {" ".join(str(row.get("hypothesis") or row["id"]).casefold().split()): row for row in completed}
    demand = next((row for row in reversed(list(latest.values())) if row.get("outcome") == "supported"
        and (row.get("ran") or row.get("result"))
        and (row.get("result_text") or row.get("result"))
        and ((row.get("spec") or {}).get("scan") in {"demand", "search_demand", "outreach", "landing"}
             or _DEMAND.search(" ".join(str(row.get(key) or "") for key in ("hypothesis", "action", "measure"))))), None)
    page_test = next((row for row in rows if row["state"] in {"proposed", "running"}
                     and (row.get("spec") or {}).get("scan") == "landing"), None)
    if bug:
        label, why, signal = "Fix this", "You reported a problem. Your coding tool can fix that exact problem.", bug["id"]
    elif not state.get("product") and (demand or demo):
        label, why, signal = "Build a first version", ("Your saved test supports real demand." if demand else
            "A potential customer asked you for a demo."), demand["id"] if demand else demo
    elif not state.get("page") and state.get("page_build") != "running" and (page_test or state["idea"].get("confirmed")):
        label, why, signal = "Build your landing page", ("Your saved page test needs a page." if page_test else
            "Your idea is confirmed. A page lets you show it and test interest."), page_test["id"] if page_test else direction
    else:
        return None
    try:
        caller = CallerContext(sub=owner, email="", role="user", path="", method="GET", exp=0, nonce="")
        tool = local_tool(venture_id, caller=caller)
    except HTTPException as exc:
        if exc.status_code not in {409, 503}:
            raise
        return {"id": "build:connect:" + signal, "title": "Connect your coding tool",
                "why": exc.detail["message"], "action": None, "payload": {}, "href": "/settings/local#coding-tools"}
    text = (f"Implement this requested fix with {tool}.\n\n{bug['text']}" if bug else
            f"Build my landing page with {tool} from my saved idea." if label == "Build your landing page" else
            f"Build a first working version of my app with {tool} from my saved idea.")
    title = "Retry your landing page" if label == "Build your landing page" and state.get("page_build") == "failed" else label
    return {"id": "build:" + signal, "title": title + " with " + _TOOLS[tool], "why": why,
            "action": "pagebuilder.build" if label == "Build your landing page" else "build.tool.start",
            "payload": {"query": text, "prepare": True} if label == "Build your landing page" else
                {"text": text, **({"repair": True} if bug else {})}}
