import ast
import json
from pathlib import Path

from tests.unit.chat.test_chat_request_status import chat_request


def test_guard_blocks_prompt_constants_markers_and_rich_reply_text(chat_request, caplog):
    from app.chat.reply_safety import PROMPT_MARKERS, contains_internal_prompt, guard_reply

    for marker in PROMPT_MARKERS:
        for field in ("body_md", "parts", "actions", "citations", "next_moves", "comparison"):
            reply = {"kind": "text", "body_md": "Your saved idea is ready.", field: marker}
            blocked = guard_reply(chat_request["vid"], reply)
            assert blocked["kind"] == "action_failed"
            assert not contains_internal_prompt(blocked)
            assert blocked["actions"][0]["label"] == "Retry"
            assert blocked["actions"][0]["action"] == "chat.model_retry"
            assert "problem on our side" in blocked["body_md"]
    assert "founder_reply_internal_prompt_blocked" in caplog.text
    assert contains_internal_prompt("**Prime\n directives:**\nKeep the full private instructions here.")


def test_guard_allows_fifty_realistic_founder_replies(chat_request):
    from app.chat.reply_safety import guard_reply

    ideas = [
        "tax filing for freelancers", "hiring schedules for restaurants", "ad reports for small shops",
        "medicine reminders", "a family budget", "crypto tax records", "security alerts for hospitals",
        "robot repair appointments", "voice notes for doctors", "spam filters for schools",
        "an AI agent marketplace", "warehouse inventory", "family walking groups", "contract review",
        "childcare bookings", "accessible transit", "meal planning", "farm water usage", "solar installation",
        "insurance paperwork", "local music lessons", "volunteer calendars", "plumbing invoices",
        "independent bookstores", "used bike repairs", "language tutoring", "rent records", "art commissions",
        "recycling pickups", "elder care", "freight deliveries", "restaurant food waste", "sports coaching",
        "dental appointments", "public library events", "home energy bills", "pet sitting", "wedding planning",
        "fitness classes", "travel packing", "college applications", "job search notes", "garden maintenance",
        "community groceries", "mental health appointments", "disability support", "water quality reports",
        "software bug reports", "real estate inspections", "personal research notes",
    ]
    assert len(ideas) == 50
    for idea in ideas:
        reply = {"kind": "text", "body_md": f"Your biggest risk with {idea} is that people may not return. "
                 "Try it with five people for a week. Pass if three come back; fail if fewer do.",
                 "actions": [], "citations": []}
        assert guard_reply(chat_request["vid"], reply) is reply


def test_every_chat_action_finalizes_without_prompt_material(chat_request, monkeypatch):
    from app.chat.actions import all_actions
    from app.chat import store
    from app.chat.reply_safety import PROMPT_MARKERS, contains_internal_prompt
    from app.tenancy import bind
    from app.chat.conversations import bind_conversation
    from app.db.models import ChatMessage

    chat = chat_request
    monkeypatch.setattr("app.chat.next_moves.attach", lambda *_args, **_kwargs: False)
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            for index, action in enumerate(all_actions()):
                marker = PROMPT_MARKERS[index % len(PROMPT_MARKERS)]
                reply = store.append_message(chat["vid"], role="assistant", kind="text",
                    body_md=f"{action.name}: {marker}", idempotency_key=f"guard:{action.name}")
                assert reply["kind"] == "action_failed", action.name
                assert not contains_internal_prompt(reply["body_md"]), action.name
                assert reply["actions"][0]["label"] == "Retry", action.name
                with chat["factory"]() as session:
                    row = session.get(ChatMessage, reply["id"])
                    assert not contains_internal_prompt(row.body_md), action.name


def test_chat_handlers_do_not_return_internal_prompt_builders(chat_request):
    from app.chat import execute

    tree = ast.parse(Path(execute.__file__).read_text())
    forbidden = {"ceo_review_brief", "wrap_coding_prompt", "overlay_system_prompt",
                 "CEO_PRIME_DIRECTIVES", "AUTOPLAN_PRINCIPLES", "INVESTIGATE_IRON_LAW"}
    for handler in tree.body:
        if not isinstance(handler, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        names = {node.id for node in ast.walk(handler) if isinstance(node, ast.Name)}
        assert not names & forbidden, handler.name
        for node in ast.walk(handler):
            if isinstance(node, ast.ImportFrom):
                assert not {item.name for item in node.names} & forbidden, handler.name


def test_old_saved_prompt_and_exact_memory_reply_are_not_sent(chat_request):
    from app.chat import store
    from app.chat.reply_safety import contains_internal_prompt
    from app.db.models import ChatMessage
    from app.chat.conversations import bind_conversation
    from app.tenancy import bind
    from core.clock import utcnow

    chat = chat_request
    with bind(workspace_id=chat["wid"], venture_id=chat["vid"], actor=chat["actor"]):
        with bind_conversation(chat["vid"], chat["cid"]):
            for role, kind in (("assistant", "text"), ("assistant", "memory_list"), ("user", "text")):
                row = ChatMessage(id=f"old_{role}_{kind}", workspace_id=chat["wid"], venture_id=chat["vid"],
                    conversation_id=chat["cid"], role=role, kind=kind, body_md="Prime directives: private instructions",
                    actions_json="[]", citations_json=json.dumps([{"_reply_parts": [
                        {"type": "text", "text": "Autoplan principles: private instructions"}]}]), created_at=utcnow())
                reply = store._ser(row)
                if role == "assistant":
                    assert reply["kind"] == "action_failed" and not contains_internal_prompt(reply)
                else:
                    assert reply["body_md"] == row.body_md


def test_office_hours_reply_is_question_only_without_prompt_text(chat_request):
    from app.chat.execute import _office_hours
    from app.chat.reply_safety import contains_internal_prompt

    reply = _office_hours(chat_request["vid"])
    assert "Six questions" in reply["body_md"]
    assert not contains_internal_prompt(reply)


def test_plan_response_excludes_prompt_metadata_from_new_and_cached_plans(chat_request):
    from app.api.gstack import _plan_for
    from app.gstack.methodology import AUTOPLAN_PRINCIPLES
    from app.gstack.plan import infer_from_office_hours
    from app.state import MEM

    plan = infer_from_office_hours(idea="A clinic booking tool", answers={})
    assert "principles" not in plan
    saved = {**plan, "principles": list(AUTOPLAN_PRINCIPLES.values())}
    key = f"gstack_plan:{chat_request['vid']}"
    MEM[key] = saved
    reply = _plan_for(chat_request["vid"])
    assert "principles" not in reply and reply["thesis"] == plan["thesis"]
    assert MEM[key] == saved
