"""Mine a bounded sample of feedback you explicitly selected, without contact."""
from __future__ import annotations

from datetime import datetime, timedelta
import json
import re

from sqlalchemy import select

from app.connections import context_docs
from app.db.models import VentureDocument
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import founder_copy, no_dashes

KIND = "charlie_feedback_v1"
SELECTION_KIND = "charlie_feedback_selection_v1"
MAX_CHANNELS = 5
MAX_ROWS = 100
STOP_WORDS = set("a an and are as at be can for from have i in is it my of on or our please that the this to we with would you your".split())
FEEDBACK = re.compile(r"\b(?:please|wish|need|request|feature|bug|broken|problem|issue|error|fail\w*|struggl\w*|frustrat\w*|missing|conflict\w*|cannot|can't|unable)\b|\?", re.I)


def selection(session, venture_id):
    if not context_docs.enabled():
        return {}
    workspace = context_docs.owner_scope(session, venture_id)
    row = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
        VentureDocument.kind == SELECTION_KIND))
    saved = json.loads(row.provenance_json or "{}") if row else {}
    return saved if saved.get("selected_by") == workspace.owner_id else {}


def select_sources(venture_id, *, person_id, channels=None, repo=None):
    if not context_docs.enabled():
        return {"kind": "experiment_blocked", "body_md": "Your connected feedback reading is switched off.", "actions": [], "citations": []}
    if not isinstance(person_id, str) or not person_id:
        raise PermissionError("context_scope_mismatch")
    if (channels is not None and (not isinstance(channels, list) or len(channels) > MAX_CHANNELS
            or any(not isinstance(channel, str) or not re.fullmatch(r"[CG][A-Z0-9]{1,31}", channel) for channel in channels))
            or repo is not None and (not isinstance(repo, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", repo))):
        raise ValueError("feedback_selection_invalid")
    with db()() as session:
        context_docs.owner_scope(session, venture_id, person_id)
        saved = selection(session, venture_id)
        if channels is not None:
            saved["channels"] = list(dict.fromkeys(channels))
        if repo is not None:
            saved["repo"] = repo
        if not saved.get("channels") and not saved.get("repo"):
            raise ValueError("feedback_selection_required")
        saved["selected_by"] = person_id
        context_docs.save(session, venture_id, SELECTION_KIND, "sources", "Your selected feedback", "", saved)
        session.commit()
    return readiness(venture_id, saved) or {"kind": "context_saved", "body_md": "I saved your selected feedback sources. I will only read these channels and repository issues for you.", "actions": [], "citations": []}


def readiness(venture_id, saved=None):
    if not context_docs.enabled():
        return {"kind": "experiment_blocked", "body_md": "Your connected feedback reading is switched off.", "actions": [], "citations": []}
    with db()() as session:
        current = selection(session, venture_id)
        if not current or saved is not None and current != saved:
            return {"kind": "experiment_blocked", "body_md": "Select the channels or repository you want me to read for you.", "actions": [], "citations": []}
        for platform in platforms(current):
            if not context_docs.connection(session, venture_id, platform):
                return context_docs.connect_reply(platform)
    return None


def platforms(saved):
    return (["slack"] if saved.get("channels") else []) + (["github"] if saved.get("repo") else [])


def specs(snapshot, base):
    from app.experiments.autonomous import RECIPES
    saved = snapshot.get("feedback_selection")
    if not context_docs.enabled() or not saved:
        return []
    recipe = RECIPES["feedback_mining"]
    return [{**base, "scan": "feedback_mining", "title": recipe["title"], "runner": "scan",
        "hypothesis": "Your selected feedback contains recurring problems or requests worth testing.",
        "catalog_key": "feedback_mining", "source": "catalog", "question": "Q1",
        "assumption": "Your selected feedback contains recurring problems or requests worth testing.",
        "variable": "none", "variants": [{"key": "all", "label": "Your selected feedback", "params": {}}],
        "runner_params": {"selection": saved}, "selection": saved, "icp": snapshot["journey"].get("icp") or {},
        **recipe["thresholds"], "metric": "recurring_feedback", "min_sample": 2, "sample_unit": "sources",
        "max_duration_days": 1, "founder_minutes": 1, "approval": "once", "compliance": ["read_only_selected_sources"],
        "requires": platforms(saved), "state": "proposed", "result": None, "queries": [], "pages": 0,
        "provider": "composio", "extractor": "feedback", "recency": "90 days", "ceiling_micro": 0,
        "method": "Read up to 100 recent messages per selected Slack channel and 100 issues in your linked repository. Group repeated problems and requests with source links. No messages are sent and no accounts are changed."}]


def clusters(sources):
    groups = []
    for source in {source["url"]: source for source in sources}.values():
        words = set(re.findall(r"[a-z0-9]+", source["quote"].lower())) - STOP_WORDS
        if len(words) < 2:
            continue
        match = next((group for group in groups if len(words & group["words"]) >= 2
                      and len(words & group["words"]) / len(words | group["words"]) >= .5), None)
        if match is None:
            match = {"words": words, "sources": []}
            groups.append(match)
        match["sources"].append(source)
    return [{"problem": group["sources"][0]["quote"][:160], "sources": group["sources"]}
            for group in groups if len(group["sources"]) >= 2]


def scan(spec):
    from app.experiments import autonomous
    blocked = readiness(spec["venture_id"], spec["selection"])
    if blocked:
        return {"error": blocked["body_md"], "connection_action": next(iter(blocked["actions"]), None), "actual_micro": 0}
    sources, retrieved = [], 0
    since = as_utc(utcnow()) - timedelta(days=90)
    saved = spec["selection"]
    # shortcut: one page per source and English lexical grouping; paginate or add semantic grouping when this sample is insufficient.
    for platform in platforms(saved):
        targets = saved["channels"] if platform == "slack" else [saved["repo"]]
        for target in targets:
            if blocked := readiness(spec["venture_id"], saved):
                return {"error": blocked["body_md"], "connection_action": next(iter(blocked["actions"]), None), "actual_micro": 0}
            if platform == "slack":
                data = context_docs.read(spec["venture_id"], platform, "history",
                    [(("channel", "channelid"), target), (("limit",), MAX_ROWS), (("oldest",), str(since.timestamp()))])
                rows = data.get("messages") if isinstance(data, dict) else None
            else:
                owner, repo = target.split("/")
                data = context_docs.read(spec["venture_id"], platform, "issues",
                    [(("owner",), owner), (("repo", "repository"), repo), (("state",), "all"),
                     (("perpage",), MAX_ROWS), (("since",), since.isoformat())])
                rows = data if isinstance(data, list) else data.get("issues") if isinstance(data, dict) else None
            if isinstance(data, dict) and data.get("kind") == "experiment_blocked":
                return {"error": data["body_md"], "connection_action": data["actions"][0], "actual_micro": 0}
            if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                raise ValueError("feedback_read_invalid")
            retrieved += min(len(rows), MAX_ROWS)
            for row in rows[:MAX_ROWS]:
                if platform == "slack":
                    timestamp = row.get("ts")
                    if (row.get("subtype") or row.get("bot_id") or not isinstance(timestamp, str)
                            or not re.fullmatch(r"[0-9]{1,12}\.[0-9]{1,6}", timestamp)):
                        continue
                    try:
                        published = datetime.fromtimestamp(float(timestamp), tz=since.tzinfo)
                    except (ValueError, OverflowError, OSError):
                        continue
                    seconds, fraction = timestamp.split(".")
                    text, url = row.get("text"), f"https://app.slack.com/archives/{target}/p{seconds}{fraction.ljust(6, '0')}"
                    title = "Your selected Slack feedback"
                else:
                    if row.get("pull_request") is not None or type(row.get("number")) is not int or row["number"] <= 0:
                        continue
                    try:
                        published = datetime.fromisoformat(str(row.get("updated_at") or row.get("created_at") or "").replace("Z", "+00:00"))
                    except ValueError:
                        continue
                    text = "\n".join(value for value in (row.get("title"), row.get("body")) if isinstance(value, str))
                    url, title = f"https://github.com/{target}/issues/{row['number']}", "Your linked repository issue"
                if (published.tzinfo is None or not since <= published <= as_utc(utcnow())
                        or not isinstance(text, str) or not FEEDBACK.search(text)):
                    continue
                sources.append({"url": url, "title": title, "quote": no_dashes(text.strip())[:600], "published_at": published.isoformat()})
    groups = clusters(sources)
    qualifying = [source for group in groups for source in group["sources"]]
    result = autonomous._scan_result(spec, [{"key": "all", "label": "Your selected feedback",
        "sources": qualifying, "retrieved_sources": retrieved}], 0, 0)
    result["details"] = groups
    result["insight"] = founder_copy(f"I found {len(groups)} recurring problems or requests across {len(qualifying)} items in your selected feedback. "
        "This is a bounded sample, not proof that people will pay. I did not send messages or change your accounts.\n" +
        "\n".join(f"- {group['problem']} ({len(group['sources'])} separate items)" for group in groups[:10]) +
        "\n" + ("You have recurring feedback to test next." if result["outcome"] == "supported" else
                  "You need more feedback before choosing your next step. You can select another channel or repository to investigate."))
    with db()() as session:
        context_docs.owner_scope(session, spec["venture_id"])
        if not context_docs.enabled():
            raise ValueError("experiments_autopilot_v1_disabled")
        if selection(session, spec["venture_id"]) != saved:
            raise ValueError("feedback_selection_changed")
        context_docs.save(session, spec["venture_id"], KIND, "feedback", "Your recurring feedback",
            result["insight"], {"selected_by": saved["selected_by"], "selection": saved,
                "sources": qualifying[:20], "source_url": qualifying[0]["url"] if qualifying else None})
        session.commit()
    return result
