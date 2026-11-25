from __future__ import annotations

import json
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


SVG = """<svg width="100%" viewBox="0 0 680 280" role="img"><title>Flow</title><desc>Two steps</desc>
<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></marker></defs>
<g class="node c-teal" onclick="sendPrompt('Tell me more about intake')"><rect x="240" y="40" width="200" height="56" rx="8" stroke-width="0.5"/><text class="th" x="340" y="58" text-anchor="middle" dominant-baseline="central">Intake</text><text class="ts" x="340" y="78" text-anchor="middle" dominant-baseline="central">Gather context</text></g>
<g class="node c-teal"><rect x="240" y="160" width="200" height="56"/><text class="t" x="340" y="188" text-anchor="middle">Plan</text></g>
<path class="arr" d="M340 100L340 156" fill="none" marker-end="url(#arrow)"/></svg>"""


@pytest.fixture(scope="module")
def checks():
    load_company_os_app()
    from app.chat import diagram_check
    return diagram_check


def test_good_diagram_passes(checks):
    assert checks.check_diagram(SVG) == []


def test_overlap_fails(checks):
    assert "rect_overlap" in checks.check_diagram(SVG.replace('y="160"', 'y="60"'))


def test_text_overflow_fails(checks):
    assert "text_overflow" in checks.check_diagram(SVG.replace("Intake</text>", "Very long title that cannot fit in this node</text>"))


@pytest.mark.parametrize("source, replacement", [('x="240"', 'x="650"'), ('x="340"', 'x="-1"'), ('y="188"', 'y="281"')])
def test_out_of_canvas_fails(checks, source, replacement):
    assert "out_of_canvas" in checks.check_diagram(SVG.replace(source, replacement))


@pytest.mark.parametrize("connector", ['<path class="arr" d="M340 100L340 156"/>',
                                           '<line class="arr" x1="340" y1="100" x2="340" y2="156"/>'])
def test_arrow_through_unrelated_box_fails(checks, connector):
    svg = SVG.replace('<path class="arr" d="M340 100L340 156" fill="none" marker-end="url(#arrow)"/>',
                      '<rect x="300" y="116" width="80" height="24"/>' + connector)
    assert "arrow_through_box" in checks.check_diagram(svg)


def test_oversize_fails(checks):
    assert checks.check_diagram(SVG.replace("Two steps", "é" * 32768)) == ["oversize"]


def test_bad_xml_fails(checks):
    assert checks.check_diagram(SVG[:-1]) == ["bad_xml"]


@pytest.mark.parametrize("svg, reason", [
    (SVG.replace("<svg", "<html", 1).replace("</svg>", "</html>"), "invalid_root"),
    (SVG.replace("0 0 680 280", "0 0 700 280"), "invalid_viewbox"),
    (SVG.replace("0 0 680 280", "0 0 680 nan"), "invalid_viewbox"),
    ('<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///etc/passwd">]>' + SVG, "entity_declaration"),
    (SVG.replace("</svg>", "<script>alert(1)</script></svg>"), "unsafe_element"),
    (SVG.replace('width="100%"', 'width="100%" onload="alert(1)"'), "unsafe_attribute"),
    (SVG.replace("Tell me more about intake", "');alert(1);//"), "unsafe_attribute"),
    (SVG.replace('class="node c-teal"', 'class="node c-teal" transform="translate(900 0)"'), "unsafe_attribute"),
    (SVG.replace('y="58"', 'y="58" dx="800"'), "unsafe_attribute"),
    (SVG.replace('class="node c-teal"', 'class="node c-teal" font-size="80"'), "unsupported_text_size"),
    (SVG.replace("Intake</text>", "desktop_driver_missing</text>"), "unsafe_label"),
    (SVG.replace('d="M340 100L340 156"', 'd="M340 100Q900 150 340 156"'), "invalid_coordinates"),
])
def test_svg_boundary_rejects_unsafe_or_invalid_input(checks, svg, reason):
    assert reason in checks.check_diagram(svg)


def test_group_container_only_ignores_full_containment(checks):
    group = '<rect class="group" x="200" y="20" width="280" height="220"/>'
    assert checks.check_diagram(SVG.replace("<defs>", group + "<defs>")) == []
    assert "rect_overlap" in checks.check_diagram(SVG.replace("<defs>", group.replace('class="group"', '') + "<defs>"))


def test_routed_arrow_does_not_cross_unrelated_box(checks):
    svg = SVG.replace('d="M340 100L340 156"', 'd="M340 100 L460 100 L460 150 L340 150 L340 156"')
    assert checks.check_diagram(svg.replace("</svg>", '<rect x="300" y="116" width="80" height="24"/></svg>')) == []


def _parts(svg=SVG):
    return [{"type": "text", "text": "Here is the flow."}, {"type": "show_diagram", "title": "intake_flow", "svg": svg},
            {"type": "text", "text": "Context keeps the plan grounded."}]


def test_parts_order_prose_and_failed_diagram_degradation(checks, caplog):
    from app.chat.diagram_reply import parse_reply
    reply = parse_reply(json.dumps({"parts": _parts()}), tokens=1800)
    assert [part["type"] for part in reply.parts] == ["text", "diagram", "text"]
    assert str(reply) == "Here is the flow.\n\nContext keeps the plan grounded."
    assert reply.tokens == 1800 and "<svg" not in reply
    with caplog.at_level("INFO", logger="app.chat.diagram"):
        failed = parse_reply(json.dumps({"parts": _parts("bad XML")}))
    assert str(failed) == str(reply) and all(part["type"] == "text" for part in failed.parts)
    assert "bad_xml" in caplog.text and "bad XML" not in caplog.text
    assert not parse_reply('{"parts": [')
    assert not parse_reply(json.dumps({"parts": [{"type": "text", "text": SVG}]}))
    assert not parse_reply(SVG)
    assert str(parse_reply("A legacy prose reply.")) == "A legacy prose reply."


def test_two_diagram_limit_and_snake_case_title(checks):
    diagram = _parts()[1]
    admitted = checks.checked_parts([diagram, diagram, diagram])
    assert len(admitted) == 2
    assert checks.checked_parts([{**diagram, "title": "Bad title"}])[0]["title"] == "bad_title"


@pytest.mark.parametrize("question", ["Confirm this", "Approve the plan", "How much does it cost?", "Send it now", "Keep it brief", "Why did it fail?"])
def test_restricted_questions_never_include_diagrams(checks, question):
    assert all(part["type"] == "text" for part in checks.checked_parts(_parts(), question=question))


def test_saved_brevity_preferences_and_action_cards_block_diagrams(checks):
    for kwargs in ({"preferences": [{"text": "Keep answers short"}]}, {"kind": "confirmation"},
                   {"actions": [{"confirmation": {"action": "plan.approve"}}]}, {"actions": [{"action": "credits.buy"}]}):
        assert all(part["type"] == "text" for part in checks.checked_parts(_parts(), **kwargs))


def test_reply_only_output_cap(checks):
    from app.chat.assistant import _assistant_output_cap
    config = SimpleNamespace(api_mode="chat_completions")
    assert _assistant_output_cap(config) == 700
    assert _assistant_output_cap(config, diagrams=True) >= 2000
    assert _assistant_output_cap(config, tools=[{}], diagrams=True) == 700


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/diagrams.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "mem.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state
    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as client:
        response = client.post("/ventures", json={"raw_idea": "A planning assistant"})
        assert response.status_code == 201
        yield client, response.json()["id"]


@pytest.mark.parametrize("rephrased", [False, True])
def test_api_orders_and_persists_parts_without_changing_old_fields(env, monkeypatch, rephrased):
    from app.chat import assistant, llm_router, rephrase, store
    from app.chat.diagram_reply import parse_reply
    from app.db.models import ChatMessage
    from app.state import db
    client, venture_id = env
    question = "Draw how that process works — keep it grounded" if rephrased else "Draw how a process works?"
    interpreted = "Explain how the planning process works, keeping it grounded."

    async def rewrite(original, context, **kwargs):
        assert original == question
        return interpreted

    async def complete(messages, **kwargs):
        assert assistant.DIAGRAM_PROMPT in messages[0]["content"]
        if rephrased:
            assert question in messages[1]["content"]
            assert f"Interpreted request:\n{interpreted}" in messages[1]["content"]
        return parse_reply(json.dumps({"parts": _parts()}))

    async def route(*args, **kwargs):
        return None

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    if rephrased:
        monkeypatch.setenv("LLM_OFFLINE", "0")
        monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
        monkeypatch.setenv("CHAT_REPHRASE", "on")
        monkeypatch.setenv("CHAT_AGENT_TOOLS", "0")
        monkeypatch.setattr(rephrase, "_ask_model", rewrite)
    result = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
    assert result.status_code == 200, result.text
    message = result.json()
    assert [part["type"] for part in message["parts"]][:3] == ["text", "diagram", "text"]
    assert message["content"].startswith("Here is the flow.")
    assert "<svg" not in message["content"] and "<svg" not in message["body_md"]
    assert message["body_md"].startswith("<!--employee:Charlie:")
    history = client.get(f"/ventures/{venture_id}/chat").json()["items"]
    saved = next(row for row in history if row["id"] == message["id"])
    assert saved["parts"] == message["parts"] and saved["content"] == message["content"]
    assert not any("_reply_parts" in citation for citation in saved["citations"])
    if rephrased:
        founder = next(row for row in history if row["role"] == "user")
        assert founder["body_md"] == founder["content"] == store._clean(question)
        assert founder["parts"] == [{"type": "text", "text": store._clean(question)}]
        assert interpreted not in json.dumps(history)
        with db()() as session:
            metadata = json.loads(session.get(ChatMessage, founder["id"]).citations_json)
            assert {"interpreted_request": interpreted} in metadata


@pytest.mark.parametrize("audience", ["freight dispatch teams", "families sharing garden plots"])
def test_empty_diagram_reply_retries_plain_and_persists_text_only(env, monkeypatch, audience):
    from app.chat import assistant, llm_router
    client, venture_id = env
    calls = []
    answer = "Choose a specific founder audience. Send a personal message. Learn from the replies."

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return "" if assistant.DIAGRAM_PROMPT in messages[0]["content"] else answer

    async def route(*args, **kwargs):
        return None

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    monkeypatch.setenv("CHAT_REPHRASE", "off")
    result = client.post(f"/ventures/{venture_id}/chat", json={
        "message": f"Draw step by step how outreach to {audience} works?", "idempotency_key": "diagram-plain-retry"})
    assert result.status_code == 200, result.text
    reply = result.json()
    assert answer in reply["body_md"] and reply["parts"] == [{"type": "text", "text": answer}]
    assert len(calls) == 2
    from app.core.local_mode import is_local

    if is_local():
        assert calls[0][1]["request_id"]
        assert calls[1][1]["request_id"] == calls[0][1]["request_id"] + ":plain"
    else:
        assert [call[1]["request_id"] for call in calls] == [None, None]
    assert calls[1][0][0]["content"] == calls[0][0][0]["content"].replace("\n" + assistant.DIAGRAM_PROMPT, "")
    assert "response_schema" not in calls[1][1]


def test_empty_agent_turn_retries_plain_once(checks, monkeypatch):
    from app.chat import assistant
    calls = []

    async def complete(messages, **kwargs):
        calls.append((messages, kwargs))
        return "Use the saved research to explain the process."

    monkeypatch.setattr(assistant, "_complete", complete)
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    final = assistant.diagram_messages(messages, question="Diagram the process")
    result = asyncio.run(assistant._answer_body(messages, final, workspace_id="workspace",
        request_id="turn:tools", turn={"body": "", "partial": False, "output_tokens": 20}))
    assert str(result) == "Use the saved research to explain the process."
    assert result.parts == [{"type": "text", "text": str(result)}]
    assert len(calls) == 1 and calls[0] == (messages, {"workspace_id": "workspace", "request_id": "turn:tools:plain"})


def test_empty_diagram_synthesis_keeps_paid_estimate_and_researched_prose(checks, monkeypatch):
    from app.chat import agent, assistant
    from app.tools import ToolContext
    calls = []

    async def complete(messages, **kwargs):
        calls.append(kwargs)
        return "" if assistant.DIAGRAM_PROMPT in messages[0]["content"] else "Saved research."

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    context = ToolContext(venture_id="venture", workspace_id="workspace", budget_micro=2)
    result = asyncio.run(agent.run_turn(messages, context,
        final_messages=assistant.diagram_messages(messages, question="Diagram the process")))
    assert result["body"] == "Saved research." and context.spent_micro == 2 and len(calls) == 2


@pytest.mark.parametrize("kind, actions", [("confirmation", []), ("money", []), ("text", [{"confirmation": {"action": "plan.approve"}}])])
def test_confirmation_money_approval_storage_never_contains_diagrams(env, kind, actions):
    from app.chat.store import append_message
    _, venture_id = env
    message = append_message(venture_id, role="assistant", kind=kind, body_md="Here is the flow.\n\nContext keeps the plan grounded.",
                             actions=actions, parts=_parts())
    assert all(part["type"] == "text" for part in message["parts"])


def test_merge_keeps_order_and_confirmation_strips_diagrams(env):
    from app.chat.router import _merge_replies
    from app.chat.store import append_message
    _, venture_id = env
    first = {"body_md": "Here is the flow.\n\nContext keeps the plan grounded.", "parts": _parts()}
    merged = _merge_replies([first, {"body_md": "A further explanation."}])
    assert [part["type"] for part in merged["parts"]] == ["text", "show_diagram", "text", "text"]
    merged = _merge_replies([first, {"kind": "confirmation", "body_md": "Review the card."}])
    message = append_message(venture_id, role="assistant", **merged)
    assert all(part["type"] == "text" for part in message["parts"])


@pytest.mark.parametrize("question", ["What is my credit balance?", "Approve the plan", "Confirm", "Send the email now"])
def test_restricted_api_paths_never_include_diagrams(env, monkeypatch, question):
    from app.chat import assistant
    from app.chat.diagram_reply import parse_reply
    client, venture_id = env

    async def complete(*args, **kwargs):
        return parse_reply(json.dumps({"parts": _parts()}))

    monkeypatch.setattr(assistant, "_complete", complete)
    result = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
    assert result.status_code == 200, result.text
    assert all(part["type"] == "text" for part in result.json()["parts"])


def test_agentic_reply_preserves_parts_and_total_output_tokens(checks, monkeypatch):
    from app.chat import agent, assistant
    from app.chat.diagram_reply import parse_reply
    from app.tools import ToolContext
    message = SimpleNamespace(content="Here is the evidence.", tool_calls=[])
    response = SimpleNamespace(choices=[SimpleNamespace(message=message)],
                               usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    calls = []

    async def complete(*args, **kwargs):
        calls.append(kwargs)
        if kwargs.get("tools"):
            assert kwargs["raw"] and assistant.DIAGRAM_PROMPT not in args[0][0]["content"]
            return response
        assert assistant.DIAGRAM_PROMPT in args[0][0]["content"]
        assert kwargs["max_estimate_micro"] == 1
        return parse_reply(json.dumps({"parts": _parts()}), tokens=1800)

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    context = ToolContext(venture_id="venture", workspace_id="workspace", budget_micro=50000)
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}, {"role": "user", "content": "Compare systems"}]
    final = assistant.diagram_messages(messages, question="Diagram the systems")
    result = asyncio.run(agent.run_turn(messages, context, final_messages=final))
    assert result["output_tokens"] == 1820 and len(calls) == 2 and context.spent_micro == 2
    assert [part["type"] for part in result["body"].parts] == ["text", "diagram", "text"]


def test_broken_diagram_logging_still_drops_invalid_svg(checks, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("Telemetry unavailable")

    monkeypatch.setattr(checks.log, "warning", broken)
    assert all(part["type"] == "text" for part in checks.checked_parts(_parts("bad XML")))


@pytest.mark.parametrize("filename", sorted(path for path in (Path(__file__).parents[2] / "fixtures" / "diagrams").glob("*.svg")
                                                         if "messy" not in path.stem), ids=lambda path: path.stem)
def test_realistic_model_diagrams_pass(checks, filename):
    assert checks.check_diagram(filename.read_text()) == []


def test_layout_tolerances_and_group_tokens(checks):
    nearly_touching = SVG.replace('y="160"', 'y="94"').replace('y="188"', 'y="122"')
    assert "rect_overlap" not in checks.check_diagram(nearly_touching)
    assert "rect_overlap" in checks.check_diagram(nearly_touching.replace('y="94"', 'y="90"'))
    narrow = SVG.replace('width="200"', 'width="36"').replace('x="240"', 'x="322"').replace('>Gather context<', '>Flow<')
    assert checks.check_diagram(narrow) == []
    assert "text_overflow" in checks.check_diagram(narrow.replace('>Intake<', '>Intakes<'))
    group = '<g class="group"><rect x="40" y="20" width="600" height="220"/></g>'
    assert checks.check_diagram(SVG.replace('<defs>', group + '<defs>')) == []
    assert "rect_overlap" in checks.check_diagram(SVG.replace('<defs>', group.replace('class="group"', 'class="regrouped"') + '<defs>'))


def test_arrow_endpoint_tolerance_does_not_exempt_whole_box(checks):
    source = 'd="M340 100L340 156"'
    assert checks.check_diagram(SVG.replace(source, 'd="M340 92L340 164"')) == []
    assert "arrow_through_box" in checks.check_diagram(SVG.replace(source, 'd="M340 80L340 176"'))
    assert "arrow_through_box" in checks.check_diagram(SVG.replace(source, 'd="M340 92L340 50L480 50L480 156L340 156"'))
    assert "arrow_through_box" in checks.check_diagram(SVG.replace(source, 'd="M330 60L340 60"'))


def test_drop_warning_has_only_codes_and_counts(checks, caplog):
    with caplog.at_level("WARNING"):
        reasons = []
        checked = checks.checked_parts(_parts("private SVG content"), drop_reasons=reasons)
    record = next(record for record in caplog.records if record.message.startswith("chat_diagram_dropped"))
    assert record.levelname == "WARNING"
    payload = json.loads(record.message.split(" ", 1)[1])
    assert payload == {"reasons": ["bad_xml"], "part_index": 1, "part_count": 3, "diagram_count": 1, "accepted_count": 0}
    assert reasons == [{"part_index": 1, "reasons": ["bad_xml"]}]
    assert "private SVG content" not in caplog.text
    assert all(part["type"] == "text" for part in checked)


@pytest.mark.parametrize("response", [json.dumps({"parts": [{"type": "text", "text": "Private reply"}]}), "Private reply", json.dumps({"parts": []}), ""])
def test_zero_diagrams_warn_without_content(checks, caplog, response):
    from app.chat.diagram_reply import parse_reply
    with caplog.at_level("WARNING"):
        parse_reply(response, tokens=42)
    assert "chat_diagram_none" in caplog.text and "diagram_count=0" in caplog.text
    assert "Private reply" not in caplog.text


def test_dropped_diagram_is_not_reported_as_none(checks, caplog):
    from app.chat.diagram_reply import parse_reply
    with caplog.at_level("WARNING"):
        reply = parse_reply(json.dumps({"parts": _parts("bad XML")}))
    assert reply.raw_parts == _parts("bad XML")
    assert "chat_diagram_dropped" in caplog.text and "chat_diagram_none" not in caplog.text


def test_agent_preserves_rejected_raw_synthesis_for_lab(checks, monkeypatch):
    from app.chat import agent, assistant
    from app.chat.diagram_reply import parse_reply
    from app.tools import ToolContext
    researched = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Grounded evidence.", tool_calls=[]))],
                                 usage={"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30})
    raw_parts = [{"type": "show_diagram", "title": "broken", "svg": "bad XML"}]

    async def complete(messages, **kwargs):
        return researched if kwargs.get("raw") else parse_reply(json.dumps({"parts": raw_parts}), tokens=40)

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}, {"role": "user", "content": "Compare systems"}]
    result = asyncio.run(agent.run_turn(messages, ToolContext(venture_id="venture", workspace_id="workspace", budget_micro=50000),
                                        final_messages=assistant.diagram_messages(messages, question="Diagram the systems")))
    assert result["body"] == "Grounded evidence."
    assert result["diagram_reply"].raw_parts == raw_parts and result["diagram_reply"].parts == []
    assert result["output_tokens"] == 60


@pytest.mark.parametrize("question", ["Hello", "What is my venture called?", "Thanks, that is really helpful for me today", "Explain a process briefly", "Approve this plan", "How does billing work?"])
def test_ordinary_or_restricted_turns_keep_original_prompt_and_caps(checks, question):
    from app.chat import assistant
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    assert assistant.diagram_messages(messages, question=question) is messages
    config = SimpleNamespace(api_mode="chat_completions")
    assert assistant._assistant_output_cap(config) == 700


def test_diagram_synthesis_budget_exhaustion_preserves_researched_prose(checks, monkeypatch):
    from app.chat import agent, assistant
    from app.tools import ToolContext
    calls = []

    async def complete(messages, **kwargs):
        calls.append(kwargs)
        assert assistant.DIAGRAM_PROMPT not in messages[0]["content"]
        return "Evidence about the system."

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(agent, "_estimate", lambda *args, **kwargs: 1)
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}, {"role": "user", "content": "Compare systems"}]
    context = ToolContext(venture_id="venture", workspace_id="workspace", budget_micro=1)
    result = asyncio.run(agent.run_turn(messages, context,
        final_messages=assistant.diagram_messages(messages, question="Diagram the systems")))
    assert result["body"] == "Evidence about the system." and len(calls) == 1 and context.spent_micro == 1


FIXTURES = Path(__file__).parents[2] / "fixtures" / "diagrams"
JOURNEY = {"type": "show_diagram", "title": "Customer journey", "nodes": [
    {"id": "spot", "label": "Founder spots a dental practice with many missed appointments and an empty calendar",
     "detail": "Public reviews mention long waits and no reminders at all", "color": "gray"},
    {"id": "intro", "label": "Send intro", "detail": "", "color": "teal"},
    {"id": "demo", "label": "Demo", "detail": "", "color": "teal"},
    {"id": "pilot", "label": "Pilot", "detail": "Two weeks", "color": "purple"},
    {"id": "paid", "label": "Paid", "detail": "", "color": "gray"}],
    "edges": [{"from": "spot", "to": "intro"}, {"from": "spot", "to": "demo"}, {"from": "intro", "to": "pilot"},
              {"from": "demo", "to": "pilot"}, {"from": "pilot", "to": "paid"}, {"from": "paid", "to": "spot"},
              {"from": "spot", "to": "paid"}]}


def test_messy_model_svg_is_rebuilt_and_renders(checks):
    messy = (FIXTURES / "dentrecall_messy.svg").read_text()
    assert {"out_of_canvas", "text_overflow", "unsafe_element"} <= set(checks.check_diagram(messy))
    reasons = []
    [part] = checks.checked_parts([{"type": "show_diagram", "title": "dentrecall_rebooking", "svg": messy}], drop_reasons=reasons)
    assert reasons == [{"part_index": 0, "reasons": []}] and checks.check_diagram(part["svg"]) == []
    assert "<script" not in part["svg"] and "<style" not in part["svg"] and "alert" not in part["svg"]
    for label in ("Identify patients", "Send a consented rebooking text", "Book a return visit"):
        assert label in part["svg"]
    assert part["svg"].count('class="arr"') == 2


def test_spec_wraps_long_labels_inside_the_canvas(checks):
    from xml.etree.ElementTree import fromstring
    [part] = checks.checked_parts([JOURNEY], question="Draw my customer journey")
    assert part["title"] == "customer_journey" and checks.check_diagram(part["svg"]) == []
    root = fromstring(part["svg"])
    texts = [text.text for text in root.iter("text")]
    assert "Paid" in texts and "empty calendar" in " ".join(texts)
    assert all(len(text.text) * (7 if text.get("class") == "th" else 6.5) <= 280 - 24 for text in root.iter("text"))
    assert "Founder spots a dental practice with many missed appointments and an empty calendar" not in texts
    assert sum(1 for text in root.iter("text") if text.get("class") == "th") > len(JOURNEY["nodes"])  # wrapped
    height = float(root.get("viewBox").split()[3])
    assert all(0 <= float(rect.get("x")) and float(rect.get("x")) + float(rect.get("width")) <= 680
               and float(rect.get("y")) + float(rect.get("height")) <= height for rect in root.iter("rect"))


@pytest.mark.parametrize("part, reason", [
    ({"nodes": []}, "invalid_spec"),
    ({"nodes": [{"id": "a", "label": "desktop_driver_missing"}]}, "unsafe_label"),
    ({"nodes": [{"id": "a", "label": "Root"}] + [{"id": str(i), "label": f"Leaf {i}"} for i in range(5)],
      "edges": [{"from": "a", "to": str(i)} for i in range(5)]}, "too_wide"),
    ({"svg": '<!DOCTYPE svg [<!ENTITY x "y">]><svg><text>&x;</text></svg>'}, "entity_declaration"),
    ({"svg": SVG.replace("Intake</text>", "desktop_driver_missing</text>").replace('x="240"', 'x="650"')}, "unsafe_label"),
    ({"svg": "<svg><script>alert(1)</script></svg>"}, "invalid_viewbox"),
])
def test_unsafe_or_unusable_diagrams_are_still_rejected(checks, part, reason):
    reasons = []
    checked = checks.checked_parts([{"type": "show_diagram", "title": "flow", **part}], drop_reasons=reasons)
    assert checked == [] and reason in reasons[0]["reasons"]


def test_spec_labels_are_escaped_and_stored_diagrams_are_not_rebuilt(checks):
    [part] = checks.checked_parts([{"type": "show_diagram", "title": "x", "nodes": [
        {"id": "a", "label": "<script>alert('x')</script> & go"}, {"id": "b", "label": "Done"}], "edges": [{"from": "a", "to": "b"}]}])
    assert "<script" not in part["svg"] and "&lt;script&gt;" in part["svg"] and "sendPrompt('Tell me more about script" in part["svg"]
    stored = {"type": "diagram", "title": "flow", "svg": SVG.replace("</svg>", "<script>alert(1)</script></svg>")}
    assert checks.checked_parts([stored]) == []


@pytest.mark.parametrize("question", ["Draw my customer journey", "Show the flow", "Map out how we send rebooking texts"])
def test_diagram_requests_admit_the_prompt_and_prose_words_do_not_veto(checks, question):
    from app.chat import assistant
    messages = [{"role": "system", "content": assistant.SYSTEM_PROMPT}]
    assert assistant.DIAGRAM_PROMPT in assistant.diagram_messages(messages, question=question)[0]["content"]
    parts = [{"type": "text", "text": "Send a short text and pay attention to the cost of no-shows."}, JOURNEY]
    assert [part["type"] for part in checks.checked_parts(parts, question=question, body=parts[0]["text"])] == ["text", "diagram"]


def test_chat_diagram_request_returns_an_accepted_rendered_diagram(env, monkeypatch, caplog):
    from app.chat import assistant, llm_router
    from app.chat.diagram_reply import parse_reply
    client, venture_id = env
    seen = []

    async def complete(messages, **kwargs):
        seen.append(kwargs.get("response_schema"))
        assert assistant.DIAGRAM_PROMPT in messages[0]["content"]
        return parse_reply(json.dumps({"parts": [{"type": "text", "text": "Here is the journey. Send the first text today."},
                                                 JOURNEY, {"type": "text", "text": "The pilot is where trust is earned."}]}))

    async def route(*args, **kwargs):
        return None

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    monkeypatch.setenv("CHAT_REPHRASE", "off")
    with caplog.at_level("WARNING"):
        result = client.post(f"/ventures/{venture_id}/chat", json={"message": "Draw my customer journey"})
    assert result.status_code == 200, result.text
    parts = result.json()["parts"]
    assert [part["type"] for part in parts] == ["text", "diagram", "text"]
    assert parts[1]["title"] == "customer_journey" and parts[1]["svg"].startswith('<svg width="100%" viewBox="0 0 680 ')
    from app.chat.diagram_check import check_diagram
    assert check_diagram(parts[1]["svg"]) == [] and "chat_diagram_dropped" not in caplog.text
    history = client.get(f"/ventures/{venture_id}/chat").json()["items"]
    assert next(row for row in history if row["id"] == result.json()["id"])["parts"] == parts


def _ask(env, monkeypatch, question, parts):
    from app.chat import assistant, llm_router
    from app.chat.diagram_reply import parse_reply
    client, venture_id = env
    offered = []

    async def complete(messages, **kwargs):
        offered.append(assistant.DIAGRAM_PROMPT in messages[0]["content"])
        return parse_reply(json.dumps({"parts": parts})) if offered[-1] else parts[0]["text"]

    async def route(*args, **kwargs):
        return None

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    monkeypatch.setenv("CHAT_REPHRASE", "off")
    result = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
    assert result.status_code == 200, result.text
    return offered, result.json()["parts"]


BRAINSTORM = {"draw": True, "title": "first_customers", "nodes": [
    {"id": "goal", "label": "First ten clinics", "detail": "", "color": "gray"},
    {"id": "a", "label": "Local dentist groups", "detail": "", "color": "teal"},
    {"id": "b", "label": "Supplier referrals", "detail": "", "color": "teal"},
    {"id": "c", "label": "Cold calls to front desks", "detail": "", "color": "teal"}],
    "edges": [{"from": "goal", "to": "a"}, {"from": "goal", "to": "b"}, {"from": "goal", "to": "c"}]}
LONG_ANSWER = ("There are three channels worth testing for your first ten dental clinic customers. Start with local "
               "dentist groups, where one talk reaches many owners at once and trust travels fast between peers. Then ask "
               "your suppliers for referrals, because they already visit clinics every week and know who is struggling. "
               "Finally, make short cold calls to front desks before nine in the morning when the phones are quiet.")


def _step(env, monkeypatch, question, answer, model):
    """Post a plain-text answer, then wait for the background diagram step; returns (reply, saved parts, step calls)."""
    import time
    from app.chat import assistant, llm_router
    from app.credits import metered
    client, venture_id = env
    offered, calls = [], []

    async def complete(messages, **kwargs):
        offered.append(assistant.DIAGRAM_PROMPT in messages[0]["content"])
        return answer

    async def route(*args, **kwargs):
        return None

    async def llm(messages, **kwargs):
        calls.append(kwargs)
        return model()

    monkeypatch.setattr(assistant, "_complete", complete)
    monkeypatch.setattr(llm_router, "route", route)
    monkeypatch.setattr(metered, "llm", llm)
    monkeypatch.setenv("CHAT_REPHRASE", "off")
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    monkeypatch.setenv("CHAT_AGENT_TOOLS", "0")
    result = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
    assert result.status_code == 200, result.text
    reply = result.json()
    assert offered == [False]  # the main answer stays plain text, one call
    from app.chat import diagram_reply
    for _ in range(100):  # the step runs on the app's loop after the response
        if not diagram_reply._STEP_TASKS:
            break
        time.sleep(0.05)
    saved = next(row for row in client.get(f"/ventures/{venture_id}/chat").json()["items"] if row["id"] == reply["id"])
    return reply, saved, calls


def _model(payload):
    def model():
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])
    return model


def test_brainstorm_answer_gets_a_diagram_attached_afterwards(env, monkeypatch):
    reply, saved, calls = _step(env, monkeypatch, "What are some ways I could get my first ten dental clinic customers?",
                                LONG_ANSWER, _model(BRAINSTORM))
    assert all(part["type"] == "text" for part in reply["parts"])  # the answer is not held back for the diagram
    assert len(calls) == 1 and calls[0]["purpose"] == "venture.chat.assistant" and calls[0]["tier"] == "flash"
    assert [part["type"] for part in saved["parts"]] == ["text", "diagram"]
    assert "Supplier referrals" in saved["parts"][1]["svg"] and saved["parts"][0]["text"] == saved["content"]


def test_short_factual_answer_makes_no_diagram_call(env, monkeypatch):
    reply, saved, calls = _step(env, monkeypatch, "What is my venture called?", "It is called Planning assistant.",
                                _model(BRAINSTORM))
    assert calls == [] and all(part["type"] == "text" for part in saved["parts"])


@pytest.mark.parametrize("failure", ["raises", "bad_json", "declines"])
def test_diagram_step_failure_leaves_the_answer_intact(env, monkeypatch, failure):
    def model():
        if failure == "raises":
            raise RuntimeError("provider down")
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
            content="not json" if failure == "bad_json" else '{"draw": false}'))])
    reply, saved, calls = _step(env, monkeypatch, "What are some ways I could get my first ten dental clinic customers?",
                                LONG_ANSWER, model)
    assert len(calls) == 1
    assert saved["content"] == reply["content"] and LONG_ANSWER in saved["content"]
    assert all(part["type"] == "text" for part in saved["parts"])


def test_diagram_step_times_out_silently(checks, monkeypatch):
    from app.chat import diagram_reply
    from app.credits import metered

    async def slow(messages, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(metered, "llm", slow)
    monkeypatch.setattr(diagram_reply, "STEP_TIMEOUT_S", 0.05)
    message = {"id": "missing", "content": LONG_ANSWER}
    assert asyncio.run(diagram_reply.diagram_step("venture", message, question="What are some ways?")) is None


def test_short_factual_reply_gets_no_diagram(env, monkeypatch):
    offered, parts = _ask(env, monkeypatch, "What is my venture called?",
                          [{"type": "text", "text": "It is called Planning assistant."}, JOURNEY])
    assert offered and not any(offered)
    assert all(part["type"] == "text" for part in parts)


def test_founder_flow_question_mentioning_confirmed_still_gets_a_diagram(env, monkeypatch, caplog):
    # Production 2026-10-23: "confirmed" in a topic question vetoed the step silently (diagrams_allowed word list).
    question = "explain how an order would flow from a whatsapp message to a confirmed cake with this tool"
    with caplog.at_level("WARNING"):
        reply, saved, calls = _step(env, monkeypatch, question, LONG_ANSWER, _model(BRAINSTORM))
    assert len(calls) == 1 and [part["type"] for part in saved["parts"]] == ["text", "diagram"]
    assert "chat_diagram_step decision=draw" in caplog.text and "chat_diagram_step decision=attached" in caplog.text


@pytest.mark.parametrize("kind", ["text", "idea_clarification_pending"])
def test_every_saved_assistant_reply_goes_through_the_step(env, monkeypatch, caplog, kind):
    """The hook is store.append_message, so idea-session, grounded and router replies all reach it."""
    from app.chat import diagram_reply, store
    _, venture_id = env
    seen = []

    async def step(venture, message, *, question, workspace_id=None):
        seen.append((message["id"], question))

    monkeypatch.setattr(diagram_reply, "diagram_step", step)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")

    async def save():
        store.append_message(venture_id, role="user", body_md="How would a cake order flow end to end?")
        saved = store.append_message(venture_id, role="assistant", kind=kind, body_md=LONG_ANSWER)
        await asyncio.sleep(0)
        return saved

    with caplog.at_level("WARNING"):
        saved = asyncio.run(save())
    assert ("chat_diagram_step decision=draw" in caplog.text) is (kind == "text")  # status cards never reach the step
    assert seen == ([(saved["id"], "How would a cake order flow end to end?")] if kind == "text" else [])


def _save_reply(env, monkeypatch, *, founder_age_s=0, posts_between=0):
    """Founder message (optionally aged), optional assistant posts after it, then a long reply; returns step calls."""
    from datetime import timedelta
    from app.chat import diagram_reply, store
    from app.db.models import ChatMessage
    from app.state import db
    from core.clock import utcnow
    _, venture_id = env
    seen = []

    async def step(venture, message, *, question, workspace_id=None):
        seen.append(question)

    monkeypatch.setattr(diagram_reply, "diagram_step", step)
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")

    async def save():
        founder = store.append_message(venture_id, role="user", body_md="How would a cake order flow end to end?")
        if founder_age_s:
            with db()() as session:
                session.get(ChatMessage, founder["id"]).created_at = utcnow() - timedelta(seconds=founder_age_s)
                session.commit()
        for _ in range(posts_between):
            store.append_message(venture_id, role="assistant", kind="text", body_md="Your plan is ready.")
        store.append_message(venture_id, role="assistant", kind="text", body_md=LONG_ANSWER)
        await asyncio.sleep(0)

    asyncio.run(save())
    return seen


def test_direct_reply_to_a_fresh_founder_message_runs_the_step(env, monkeypatch, caplog):
    with caplog.at_level("WARNING"):
        assert _save_reply(env, monkeypatch) == ["How would a cake order flow end to end?"]
    assert "chat_diagram_step decision=draw" in caplog.text


@pytest.mark.parametrize("founder_age_s, posts_between", [(600, 0), (0, 1)], ids=["stale-founder-message", "milestone-after-reply"])
def test_posts_that_are_not_a_reply_never_pay_for_a_decision(env, monkeypatch, caplog, founder_age_s, posts_between):
    with caplog.at_level("INFO"):
        assert _save_reply(env, monkeypatch, founder_age_s=founder_age_s, posts_between=posts_between) == []
    skips = [r for r in caplog.records if "chat_diagram_step decision=skip reason=not_a_reply" in r.getMessage()]
    assert skips and all(r.levelname == "INFO" for r in skips)  # routine skips stay out of WARNING


def test_sync_save_without_a_loop_skips_with_a_reason(env, monkeypatch, caplog):
    from app.chat import store
    _, venture_id = env
    monkeypatch.setenv("LLM_OFFLINE", "0")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "off")
    store.append_message(venture_id, role="user", body_md="How would a cake order flow end to end?")
    with caplog.at_level("INFO"):
        store.append_message(venture_id, role="assistant", kind="text", body_md=LONG_ANSWER)
    assert "chat_diagram_step decision=skip reason=no_event_loop" in caplog.text
