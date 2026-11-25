from __future__ import annotations

import pytest
from coding_agents.cursor import CursorAdapter
from coding_agents.claude import ClaudeAdapter
from coding_agents.codex import CodexAdapter
from coding_agents.protocol import CodingRequest, AgentEventKind
from pathlib import Path


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_cursor_stream_keeps_tool_details_and_failed_results(domain):
    adapter = CursorAdapter()
    event = adapter.parse_event({"type": "tool_call", "subtype": "completed", "call_id": domain,
        "tool_call": {"shellToolCall": {"args": {"command": f"node tests/{domain}.js", "path": f"{domain}/index.js"}}}})
    assert event.kind == AgentEventKind.TOOL_RESULT
    assert event.text == f"node tests/{domain}.js"
    assert event.path == f"{domain}/index.js"
    assert event.tool_name == "shellToolCall" and event.tool_call_id == domain
    assert adapter.parse_event({"type": "result", "is_error": True}).kind == AgentEventKind.FAILED


@pytest.mark.parametrize("domain", ["supplier-invoices", "trail-packing"])
def test_cursor_file_edits_and_final_text_reach_build_activity(domain, tmp_path):
    from desktop_runtime.loop_diagnose import stream_text
    import json

    event = {"type": "tool_call", "subtype": "completed", "call_id": domain,
        "tool_call": {"writeToolCall": {"args": {"path": f"{domain}/index.html"}}}}
    assert CursorAdapter().parse_event(event).kind == AgentEventKind.FILE_CHANGED
    assert stream_text(json.dumps(event).encode(), "cursor", tmp_path) == f"Editing {domain}/index.html"
    result = {"type": "result", "subtype": "success", "is_error": False, "result": f"Your {domain} files are ready"}
    assert stream_text(json.dumps(result).encode(), "cursor", tmp_path) == result["result"]


@pytest.mark.asyncio
async def test_parse_recorded_streams(tmp_path):
    recorded = [
        {"type": "started", "session_id": "s"},
        {"type": "file_changed", "path": "a.py"},
        {"type": "message", "text": "hi", "unknown_vendor_field": True},
        {"type": "finished"},
    ]
    for Adapter in (CursorAdapter, ClaudeAdapter, CodexAdapter):
        ad = Adapter(recorded=recorded)
        req = CodingRequest(prompt="x", workdir=tmp_path, allow_write=True)
        res = await ad.run(req)
        assert res.write_flag_used
        assert any(e.kind == AgentEventKind.FILE_CHANGED for e in res.events)
        assert Adapter.write_flag in res.argv
