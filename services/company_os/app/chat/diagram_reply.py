"""The additive, ordered diagram reply contract. Old clients still get prose."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time

from app.chat.diagram_check import EXPLICIT, checked_parts, diagrams_allowed

log = logging.getLogger("app.chat.assistant")


DIAGRAM_PROMPT = """When a visual would explain something better than text (a flow, customer journey, process, architecture, plan, or "how does X work"), add a `show_diagram` part. You describe the boxes and arrows; the app draws, sizes, wraps and colours the diagram, so never write SVG.

Diagram spec
- nodes: 2 to 10 steps, each {"id": short unique id, "label": 2 to 6 words, "detail": optional short phrase or "", "color": one of teal, purple, blue, amber, coral, gray}.
- edges: arrows {"from": node id, "to": node id} in the direction work flows. A loop back to an earlier step is fine.
- Order nodes in the order a reader should meet them. At most 4 nodes may sit side by side, so keep branches small.
- Use at most 2 colours and make colour mean something (e.g. purple = founder steps, teal = customer steps, gray = start/end).
- Sentence case, plain words, no emoji, no internal codes.

After the diagram, write a short explanation in plain prose that adds what the boxes can't hold. Don't repeat the diagram box-by-box.

Decision rule (decide on every reply, the founder never has to ask): attach one diagram when a picture makes your answer clearer than prose alone. Good cases: explanations of how something works, brainstorms with several options or ideas, option comparisons, plans and step sequences, customer journeys, funnels, cause and effect, timelines, and systems or architecture. Always draw when the founder asks you to draw, diagram, map out or visualise something. Skip it for short factual answers, greetings, one-line replies, errors, approval/confirmation/money/sending prompts, or when the founder asked for brevity. The diagram must match your prose exactly: the same steps, options and names, in the same order, nothing the text does not say. At most 2 diagrams per reply.
Reply format: Return a JSON object with ordered parts. A text part has type "text" and text containing your normal prose. A show_diagram part has type "show_diagram", a short snake_case title, nodes and edges. Put text before and after each diagram in separate parts. If no diagram helps, return only text parts. Do not put SVG, JSON, internal notes, or error details in prose.
"""

_NODE = {"type": "object", "additionalProperties": False, "properties": {
    "id": {"type": "string"}, "label": {"type": "string"}, "detail": {"type": "string"},
    "color": {"type": "string", "enum": ["teal", "purple", "blue", "amber", "coral", "gray"]}},
    "required": ["id", "label", "detail", "color"]}
_EDGE = {"type": "object", "additionalProperties": False, "properties": {
    "from": {"type": "string"}, "to": {"type": "string"}}, "required": ["from", "to"]}
REPLY_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"parts": {"type": "array", "items": {"anyOf": [
        {"type": "object", "additionalProperties": False, "properties": {
            "type": {"type": "string", "enum": ["text"]}, "text": {"type": "string"}}, "required": ["type", "text"]},
        {"type": "object", "additionalProperties": False, "properties": {
            "type": {"type": "string", "enum": ["show_diagram"]}, "title": {"type": "string"},
            "nodes": {"type": "array", "items": _NODE}, "edges": {"type": "array", "items": _EDGE}},
            "required": ["type", "title", "nodes", "edges"]},
    ]}}}, "required": ["parts"],
}

# The main answer only switches to the JSON parts contract when the founder asks for a picture.
# Everything else stays plain text; diagram_step() decides afterwards, on its own call.
_SHOW_ME = re.compile(r"\bshow\b(?:\s+\w+){0,4}?\s+(?:flow|journey|process|pipeline|architecture|structure)s?\b", re.I)


def diagram_messages(messages: list[dict], *, question: str, preferences=None, actions=None, force: bool = False) -> list[dict]:
    """Offer the diagram contract only on explicit draw/diagram/show-the-flow requests (or force, for the lab)."""
    question = question or ""
    asked = EXPLICIT.search(question) or _SHOW_ME.search(question)
    if not force and not (asked and diagrams_allowed(question, preferences=preferences, actions=actions)):
        return messages
    return [{**messages[0], "content": messages[0]["content"] + "\n" + DIAGRAM_PROMPT}, *messages[1:]]


STEP_PROMPT = """You add an optional diagram to an answer Charlie (a startup co-founder assistant) already gave.
Decide whether one diagram would make the answer clearer than prose alone.
Draw for: processes, plans and step sequences, comparisons, options, brainstorms, funnels, customer journeys, timelines, cause and effect, systems.
Do not draw for: factual or short answers, greetings, money, approvals, sending, errors, or anything a picture adds nothing to.
The diagram must match the answer exactly: the same steps, options and names, in the same order, nothing the answer does not say.
Spec: 2 to 10 nodes {"id": short unique id, "label": 2 to 6 words, "detail": short phrase or "", "color": teal|purple|blue|amber|coral|gray};
edges {"from": id, "to": id} in the direction work flows. At most 4 nodes side by side, at most 2 colours, sentence case, no emoji.
Return JSON only: {"draw": false} or {"draw": true, "title": "short_snake_case", "nodes": [...], "edges": [...]}."""
STEP_TIMEOUT_S, STEP_MIN_WORDS = 12, 60
_STEP_TASKS: set = set()


# Narrower than diagrams_allowed: "a confirmed cake order" or "send a WhatsApp" is a topic, not an approval prompt.
# Approval/money/send cards are already excluded through kind and actions; the model also declines those.
_STEP_SKIP = re.compile(r"^\s*(?:please\s+)?(?:approve|confirm|pay|send|retry|cancel|refund|stop)\b|"
                        r"\b(?:errors?|failed|failing|broken|billing|refunds?|credits?|brief\w*|concise|brevity|in short)\b|[$€£₹]", re.I)


def _log_decision(decision: str, reason: str = "") -> None:
    # draw/attached/failed at WARNING (production drops INFO); routine skips at INFO so CloudWatch is not flooded.
    level = logging.INFO if decision == "skip" else logging.WARNING
    log.log(level, "chat_diagram_step decision=%s%s", decision, f" reason={reason}" if reason else "")


def skip_reason(question: str, body: str, *, kind: str = "text", actions=None, parts=None) -> str:
    """Free rule before the paid decision; returns why not to draw, or "" to ask the model."""
    from app.core.local_mode import is_offline
    if is_offline():
        return "offline"
    if os.environ.get("CHAT_DIAGRAM_STEP") == "off":
        return "disabled"
    from app.chat.idea_bridge import written_request
    if written_request(question) and not (EXPLICIT.search(question) or _SHOW_ME.search(question)):
        return "written_request"
    if kind != "text":
        return "kind"
    if any(isinstance(a, dict) and (a.get("confirmation") or a.get("action") or a.get("quote_id")) for a in actions or []):
        return "action_card"
    if any(isinstance(part, dict) and part.get("type") == "diagram" for part in parts or []):
        return "has_diagram"
    if len((body or "").split()) < STEP_MIN_WORDS:
        return "short"
    if _STEP_SKIP.search(question or ""):
        return "restricted_question"
    return ""


def wants_diagram(question: str, body: str, **kwargs) -> bool:
    return not skip_reason(question, body, **kwargs)


async def diagram_step(venture_id: str, message: dict, *, question: str, workspace_id: str | None = None) -> list[dict] | None:
    """One metered flash call after the answer is saved; attaches a checked diagram or returns None. Never raises."""
    from app.chat.store import attach_diagram
    from app.credits import metered

    body = message.get("content") or ""
    try:
        call = asyncio.ensure_future(metered.llm([
            {"role": "system", "content": STEP_PROMPT},
            {"role": "user", "content": json.dumps({"question": question, "answer": body}, ensure_ascii=False)}],
            purpose="venture.chat.assistant", workspace_id=workspace_id, venture_id=venture_id, tier="flash", max_tokens=2000))
        # shield: a late call still settles its credits normally; we only stop waiting for it.
        response = await asyncio.wait_for(asyncio.shield(call), STEP_TIMEOUT_S)
        text = str(response.choices[0].message.content or "").strip()
        spec = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", text))
        if not isinstance(spec, dict) or not spec.get("draw"):
            _log_decision("skip", "model")
            return None
        part = {"type": "show_diagram", "title": spec.get("title") or "diagram", "nodes": spec.get("nodes"), "edges": spec.get("edges")}
        parts = attach_diagram(message["id"], part, question=question)
        _log_decision("attached" if parts else "failed", "" if parts else "check_failed")
        return parts
    except Exception as exc:  # noqa: BLE001 - the saved answer is already complete
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "diagram.prepare")
        return None


def schedule_diagram_step(venture_id: str, message: dict, *, question: str, workspace_id: str | None = None,
                          actions=None, skip: str = "") -> None:
    """Run diagram_step in the background so it never delays the founder's answer."""
    try:
        reason = skip or skip_reason(question, message.get("content") or "", kind=message.get("kind") or "text",
                                     actions=actions, parts=message.get("parts"))
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            reason = reason or "no_event_loop"  # saved from sync code (e.g. a thread); nothing to run it on
        _log_decision("skip" if reason else "draw", reason)
        if reason:
            return
        task = loop.create_task(
            diagram_step(venture_id, message, question=question, workspace_id=workspace_id))
        _STEP_TASKS.add(task)
        task.add_done_callback(_STEP_TASKS.discard)
    except Exception as exc:  # noqa: BLE001
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "diagram.schedule")


class ReplyText(str):
    def __new__(cls, parts: list[dict], tokens: int = 0):
        body = "\n\n".join(part["text"] for part in parts if part["type"] == "text" and part["text"].strip())
        reply = super().__new__(cls, body)
        reply.parts, reply.tokens = parts, tokens
        return reply


def parse_reply(text: str, *, tokens: int = 0) -> ReplyText:
    def result(parts, raw_parts):
        reply = ReplyText(parts, tokens)
        reply.raw_parts = raw_parts
        return reply

    def log_none(parts):
        if not any(isinstance(part, dict) and part.get("type") in {"diagram", "show_diagram"} for part in parts):
            try:
                log.warning("chat_diagram_none part_count=%s diagram_count=0 output_tokens=%s", len(parts), tokens)
            except Exception as exc:
                from app.chat.recovery import log_founder_exception
                log_founder_exception(exc, None, "diagram.parse_telemetry")

    try:
        payload = json.loads(text)
        parts = payload["parts"]
        if not isinstance(parts, list):
            raise ValueError("invalid_parts")
    except (ValueError, TypeError, KeyError, RecursionError):
        if isinstance(text, str) and text.strip() and not text.lstrip().startswith(("{", "[", "```")) and not re.search(
                r"<\s*(?:svg|script)|<!|\b(?:show_diagram|\"parts\"\s*:)", text, re.I):
            parts = [{"type": "text", "text": text}]
            log_none(parts)
            return result(parts, parts)
        log.warning("chat_diagram_mode_failed reason=%s", "parse_failed" if text and text.strip() else "empty_parts")
        if not text or not text.strip():
            log_none([])
        return result([], [])
    log_none(parts)
    prose = "\n\n".join(part["text"] for part in parts if isinstance(part, dict)
                        and part.get("type") == "text" and isinstance(part.get("text"), str))
    if re.search(r"<\s*(?:svg|script)|<!DOCTYPE", prose, re.I):
        log.warning("chat_diagram_mode_failed reason=%s", "parse_failed")
        return result([], parts)
    reply = result(checked_parts(parts, body=prose), parts)
    if not reply:
        log.warning("chat_diagram_mode_failed reason=%s", "empty_parts")
    return reply


def reply_parts(body: str, *, question: str, preferences=None) -> list[dict]:
    return checked_parts(getattr(body, "parts", []), question=question, body=body, preferences=preferences)


def log_reply(parts: list, *, tokens: int, started: float) -> None:
    try:
        log.info("chat_assistant_reply %s", json.dumps({"diagram_count": sum(part["type"] == "diagram" for part in parts),
            "output_tokens": tokens, "latency_ms": round((time.monotonic() - started) * 1000)}, separators=(",", ":")))
    except Exception as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, None, "diagram.reply_telemetry")
