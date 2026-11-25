"""Regenerate the console chat wire fixtures with the company_os PYTHONPATH."""

import ast
import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
APP = ROOT / "services/company_os/app"
FIXTURES = ROOT / "apps/console/src/test-fixtures/chat-contract/messages.json"
MESSAGE_WRITERS = {"append_message", "post_milestone", "ChatMessage"}
REPLY_HELPERS = {"_reply", "reply", "_result", "question_reply"}
ACTION_FIELDS = {"action", "href", "confirmation", "name", "reply_choice"}


def _fields(node):
    if isinstance(node, ast.Dict):
        return {key.value: value for key, value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and isinstance(key.value, str)}
    if isinstance(node, ast.Call):
        return {keyword.arg: keyword.value for keyword in node.keywords}
    return {}


def _callee(node):
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _strings(node, bindings, seen=frozenset()):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.IfExp):
        yield from _strings(node.body, bindings, seen)
        yield from _strings(node.orelse, bindings, seen)
    elif isinstance(node, ast.BoolOp):
        for value in node.values:
            yield from _strings(value, bindings, seen)
    elif isinstance(node, ast.Name) and node.id not in seen:
        for value in bindings.get(node.id, ()):
            yield from _strings(value, bindings, seen | {node.id})


def _bindings(nodes):
    bindings = {}
    for node in nodes:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        for target in node.targets if isinstance(node, ast.Assign) else [node.target]:
            if isinstance(target, ast.Name):
                bindings.setdefault(target.id, []).append(node.value)
            elif isinstance(target, ast.Tuple) and isinstance(node.value, ast.Tuple):
                for name, value in zip(target.elts, node.value.elts):
                    if isinstance(name, ast.Name):
                        bindings.setdefault(name.id, []).append(value)
    return bindings


def source_contract():
    from app.chat.failed_jobs import KINDS as FAILED_JOB_KINDS
    from app.chat.milestone_hooks import OTHER_JOBS

    # shortcut: static wire literals and local assignments, not arbitrary Python data flow;
    # add a builder case for messages or action variants assembled at runtime.
    kinds = {f"{path}_failed" for _, _, path in FAILED_JOB_KINDS.values()} | {job[1] for job in OTHER_JOBS.values()}
    shapes = {("action", "disabled_reason", "label"), ("action", "href", "label", "payload")}
    for path in APP.rglob("*.py"):
        if "migrations" in path.parts:
            continue
        source = path.read_text()
        chat = path.is_relative_to(APP / "chat") or path == APP / "api/chat_api.py"
        if not chat and not re.search(r"\b(?:append_message|post_milestone|ChatMessage)\b|['\"]body_md['\"]\s*:", source):
            continue
        tree = ast.parse(source, filename=str(path))
        scopes = [tree, *(node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)))]
        globals_ = _bindings(tree.body)
        for scope in scopes:
            nodes = list(scope.body)
            for node in nodes:
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    nodes.extend(ast.iter_child_nodes(node))
            bindings = globals_ | _bindings(nodes)
            for node in nodes:
                fields = _fields(node)
                if isinstance(node, ast.Dict):
                    message = "body_md" in fields or ("actions" in fields and {"role", "text"} & fields.keys())
                    command = ((ACTION_FIELDS - {"href", "name"}) & fields.keys()
                               or chat and "name" in fields)
                    link = chat and "href" in fields and not {"source", "id", "title", "path"} & fields.keys()
                    if "label" in fields and (command or link):
                        shapes.add(tuple(sorted(fields)))
                elif isinstance(node, ast.Call):
                    callee = _callee(node.func)
                    if callee == "run_in_threadpool" and node.args:
                        callee = _callee(node.args[0])
                    message = callee in MESSAGE_WRITERS or (chat and callee in REPLY_HELPERS)
                    if message and callee in REPLY_HELPERS and len(node.args) >= 3:
                        fields.setdefault("kind", node.args[2])
                else:
                    continue
                if message and "kind" in fields:
                    kinds.update(_strings(fields["kind"], bindings))
    return kinds, shapes


def _action(shape, label):
    fields = {
        "label": label, "action": "status", "name": "status",
        "href": "https://example.test/charlie-contract", "kind": "recovery_link", "style": "primary",
        "payload": {"task_id": "task_contract"}, "task_id": "task_contract", "slot_id": "slot_contract",
        "state_hash": "saved-contract-state", "text": label, "retry": True, "known_choice": True,
        "reply_choice": True, "gtm_quote": True, "source_ref": "reference_contract",
        "disabled_reason": "Open Settings and check your connection.",
        "confirmation": {"action": "status", "resulting_state": {"task_id": "task_contract"}},
    }
    if missing := set(shape) - fields.keys():
        raise ValueError(f"Add test values for new chat action fields: {sorted(missing)}")
    return {key: fields[key] for key in shape}


def _builder_cases(venture, audience):
    from app.approvals import standing
    from app.autopilot import receipts
    from app.builder import ctl
    from app.chat import conversations, customers, execute, experiments, next_moves, numbers, product_build_request, site_actions, store
    from app.chat.confirm import ConfirmationCard
    from app.chat.milestone_hooks import _local_research_result
    from app.chat.reply_safety import failed_reply
    from app.core import local_mode, local_owner
    from app.gstack import office_hours
    from app.inbox import briefing, weekly
    from app.pagebuilder import api as page_api
    from app.research import service as research
    from app.ventures import stage

    with (patch.object(site_actions, "_venture", return_value=("wsp_contract", None, None)),
          patch.object(site_actions, "_saved_page_builds", return_value=[object()]),
          patch.object(site_actions, "_live_site", return_value=None),
          patch.object(page_api, "_view", return_value={"state": "ready", "job_id": "job_contract"}),
          patch.object(local_mode, "is_local", return_value=True)):
        yield "page-preview", asyncio.run(site_actions._preview(venture, {}, None))

    with (patch.object(execute, "is_local", return_value=True),
          patch.object(local_owner, "current_owner", return_value=SimpleNamespace(id="person_contract")),
          patch.object(product_build_request, "local_product_provider", return_value="codex"),
          patch.object(ctl, "request_local_build", return_value={"id": "order_contract", "state": "running"})):
        yield "coding-tool-receipt", execute._start_tool_build(
            venture, {"text": f"Build my app for {audience}"}, "person_contract")

    with (patch.object(execute, "is_local", return_value=True), patch.object(receipts, "queued_job"),
          patch.object(page_api, "_start", return_value={"state": "queued", "job_id": "job_contract"})):
        yield "page-started", execute._pagebuilder_start(venture, "person_contract", None)

    with (patch.object(next_moves, "_snapshot", return_value={"paused": True, "drafts": []}),
          patch.object(stage, "get_stage", return_value={"stage": "idea", "label": "Shaping your idea"}),
          patch.object(local_mode, "is_local", return_value=False)):
        next_card = next_moves._compose(venture)
    yield "next-moves", next_card
    yield "nested-next-moves", customers._reply(f"Your saved work for {audience} is ready.") | {"next_moves": next_card}
    with patch.object(customers, "is_local", return_value=True):
        view = {"icp": {"who": audience}, "job_id": "job_search_contract"}
        yield "customer-results", customers.render(venture, view | {"state": "ready"})
        yield "customer-search-progress", customers.render(venture, view | {"state": "queued"})
    dossier = {"id": "dossier_contract", "venture_id": venture, "state": "complete", "summary": {"founder_report": {
        "findings": [{"finding": f"Early feedback from {audience}.", "url": "https://example.test/feedback"}],
    }}}
    with patch.object(research, "get_dossier", return_value=dossier):
        yield "research-result", _local_research_result(venture, dossier["id"]) | {"kind": "research_ready"}
    yield "experiment-plan", experiments.question_reply(f"Which test should you run for {audience}?", [
        {"label": "Plan your experiment", "action": "experiment.prepare", "payload": {"audience": audience}},
    ], kind="experiments")
    yield "numbers", numbers._reply(f"What do you charge {audience}?")
    yield "error-retry", failed_reply()
    with patch.object(standing, "enabled", return_value=False):
        consent = ConfirmationCard(action="outreach.allow", capability="cap.autopilot.send",
                                   interpretation="Allow follow-ups?", confirm_label="Allow follow-ups",
                                   resulting_state={"audience": audience}).as_action()
    yield "consent", customers._reply("You decide whether to allow follow-ups.", [consent], kind="confirmation")
    turn = SimpleNamespace(id="office_question", kind="question", text=f"What is hardest for {audience}?",
                           options=json.dumps({"index": 1, "total": 2, "gstack_name": "Problem",
                                               "gstack_id": "Q1", "options": ["Getting started", "Keeping track"]}))
    body, actions = office_hours._reply(turn)
    yield "office-hours", customers._reply(body, actions, kind="idea_clarification_ready")

    mail = briefing.item_card(SimpleNamespace(id="item_contract", mailbox_id="inbox_contract", thread_id="thread_mail",
                              message_id="mail_contract", category="important", sender_name="A reader", sender_email="",
                              subject=f"Feedback from {audience}", reason="You asked for feedback."), "sample@example.test")
    for kind in ("inbox_briefing", "inbox_urgent"):
        yield kind, customers._reply("Your inbox has a reply for you.", mail["actions"][:1], kind=kind) | {
            "citations": [{"inbox_briefing": {"items": [mail], "filtered": {"newsletters": 0, "notifications": 0}}}],
        }
    with (patch.object(conversations, "create", return_value={"id": "thread_contract"}),
          patch.object(store, "idempotent_message", return_value=None),
          patch.object(store, "append_message", side_effect=lambda venture_id, **wire: wire)):
        yield "weekly-brief", weekly._post("person_contract", "wsp_contract", venture, "week_contract", datetime(2024, 1, 1),
                                          [], [{"label": "Check your saved work", "action": "status"}])


def _message(key, venture, reply):
    from app.chat.store import _ser
    from app.db.models import ChatMessage

    citations = list(reply.get("citations", []))
    if "next_moves" in reply:
        citations.append({"next_moves": reply["next_moves"]})
    row = ChatMessage(id=f"msg_{key}", role="assistant", kind=reply["kind"],
                      venture_id=venture, conversation_id="thread_contract", body_md=reply["body_md"],
                      actions_json=json.dumps(reply.get("actions", [])), citations_json=json.dumps(citations))
    return _ser(row, sample=False, flags={"inbox_briefing_v1": True, "cos_brief_v1": True})


def action_shapes(message):
    actions = [*message["actions"], *message.get("next_moves", {}).get("actions", [])]
    for citation in message["citations"]:
        for item in citation.get("inbox_briefing", {}).get("items", []):
            actions.extend(item["actions"])
        brief = citation.get("cos_brief", {})
        if brief.get("next_move"):
            actions.extend([brief["next_move"], *brief["alternatives"]])
    return {tuple(sorted(action)) for action in actions}


def fixtures():
    from app.core import local_mode

    kinds, shapes = source_contract()
    cases = []

    def add(key, venture, reply):
        cases.append({"case": key, "venture": venture, "message": _message(key, venture, reply)})

    with patch.object(local_mode, "is_local", return_value=True):
        for domain, audience in [("factory", "industrial calibration teams"), ("garden", "home vegetable gardeners")]:
            for name, reply in _builder_cases(f"ven_{domain}", audience):
                add(f"{domain}-{name}", f"ven_{domain}", reply)
        for kind in sorted(kinds - {case["message"]["kind"] for case in cases}):
            add(f"kind-{kind}", "ven_factory", {"kind": kind, "body_md": f"Your saved update: {kind.replace('_', ' ')}."})
        built_shapes = set().union(*(action_shapes(case["message"]) for case in cases))
        for shape in sorted(shapes - built_shapes):
            key = "action-" + "-".join(shape)
            add(key, "ven_garden", {"kind": "text", "body_md": "You can review your saved work here.",
                                   "actions": [_action(shape, "Review saved work")]})
    kinds.update(case["message"]["kind"] for case in cases)
    shapes.update(set().union(*(action_shapes(case["message"]) for case in cases)))
    return {"kinds": sorted(kinds), "action_shapes": sorted(shapes), "cases": cases}


if __name__ == "__main__":
    FIXTURES.parent.mkdir(parents=True, exist_ok=True)
    snapshot = fixtures()
    cases = snapshot.pop("cases")
    header = json.dumps(snapshot, ensure_ascii=False)
    FIXTURES.write_text(header[:-1] + ',\n "cases": [\n' + ",\n".join(
        "  " + json.dumps(case, ensure_ascii=False) for case in cases) + "\n ]\n}\n")
