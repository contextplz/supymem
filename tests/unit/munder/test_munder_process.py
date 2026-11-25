"""Munder Difflin process ports: mailbox, breaker, ledger, adjudicate, condense."""

from __future__ import annotations

import asyncio

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app

T0 = 1_000_000_000_000
BEAT = 30_000


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/munder.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    monkeypatch.setenv("CODING_AGENT", "inhouse_stub")
    monkeypatch.delenv("COMPANY_OS_LLM_BUDGET_USD_MONTH", raising=False)
    monkeypatch.delenv("COMPANY_OS_BREAKER_HARD_STOP", raising=False)
    main = load_company_os_app()
    from app import state as state_mod

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    with TestClient(main.create_app()) as client:
        created = client.post(
            "/ventures",
            json={"raw_idea": "invoice chaser for freelancers who hate chasing"},
        )
        assert created.status_code == 201, created.text
        yield client, created.json()["id"], monkeypatch


def _breaker_cfg(**over):
    return {
        "enabled": True,
        "hardStop": False,
        "repeatedToolLimit": 8,
        "errorStormLimit": 5,
        "tokenVelocityPerMin": 60_000,
        **over,
    }


def _sample(ts, output, input_tokens=1000, usd=0):
    return {
        "agentId": "a",
        "sessionId": "s1",
        "ts": ts,
        "input": input_tokens,
        "output": output,
        "cacheRead": 0,
        "cacheCreation": 0,
        "model": "m",
        "usd": usd,
    }


def test_redact_secrets_matches_munder():
    load_company_os_app()
    from app.munder.redact import redact_secrets

    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0In0.abcdeffedcba"
    assert "[redacted]" in redact_secrets(f"token {jwt} ok")
    assert jwt not in redact_secrets(f"token {jwt} ok")
    key = "sk-ant-" + ("a" * 24)
    assert redact_secrets(key) == "[redacted]"
    assert "Bearer supersecrettokenvalue" not in redact_secrets("Bearer supersecrettokenvalue")
    assert "openai_api_key=[redacted]" in redact_secrets("openai_api_key=abcdefghijklmnop")
    assert "hello" == redact_secrets("hello")


def test_hop_cap_drops_and_human_routes_to_god():
    load_company_os_app()
    from app.munder.mailbox import HOP_CAP, drain_for_stop, event_log, inbox, send

    vid = "ven_hive"
    dropped = send(vid, {"to": "coder", "act": "request", "subject": "loop", "hops": HOP_CAP + 1}, "god")
    assert dropped["hops"] > HOP_CAP
    assert inbox(vid, "god") == []
    assert any(ev.get("reason") == "hop-cap" for ev in event_log(vid))

    send(vid, {"to": "human", "act": "request", "subject": "Need a call", "body": "ok?"}, "office")
    god_mail = inbox(vid, "god")
    assert len(god_mail) == 1
    assert god_mail[0]["subject"] == "Need a call"

    send(vid, {"to": "god", "act": "inform", "subject": "noop"}, "god")
    assert len(inbox(vid, "god")) == 1

    first = drain_for_stop(vid, "god")
    assert first["block"] is True
    second = drain_for_stop(vid, "god")
    assert second["block"] is False


def test_requires_reply_and_broadcast_skips_assistant():
    load_company_os_app()
    from app.munder.mailbox import inbox, normalize, register_agent, send

    vid = "ven_cast"
    register_agent(vid, {"id": "pam", "name": "Pam", "can_inbox": True})
    register_agent(vid, {"id": "prep", "name": "Prep", "isAssistant": True, "can_inbox": True})
    req = normalize({"act": "request", "to": "pam", "subject": "q"}, "god")
    info = normalize({"act": "inform", "to": "pam", "subject": "fyi"}, "god")
    assert req["requires_reply"] is True
    assert info["requires_reply"] is False
    send(vid, {"to": "broadcast", "act": "inform", "subject": "standup"}, "god")
    assert inbox(vid, "pam")
    assert inbox(vid, "prep") == []


def test_breaker_loop_error_velocity_and_no_progress_debounce():
    load_company_os_app()
    from app.state import MEM

    MEM.clear()
    from app.munder.breaker import forget, record_compact_start, record_error, record_tool_use, tick

    forget("a")
    cfg = _breaker_cfg()
    for _ in range(8):
        record_tool_use("a", "Bash", {"cmd": "same"}, now=T0)
    d = tick([{"agentId": "a", "sample": None, "progressing": True}], T0, config=cfg)[0]
    assert d["state"]["level"] == "steering"
    assert "looping" in d["state"]["reason"]

    forget("a")
    for _ in range(5):
        record_error("a")
    d = tick([{"agentId": "a", "sample": None, "progressing": True}], T0, config=cfg)[0]
    assert d["state"]["level"] == "steering"
    assert "error storm" in d["state"]["reason"]

    forget("a")
    tick([{"agentId": "a", "sample": _sample(T0, 0), "progressing": True}], T0, config=cfg)
    d = tick(
        [{"agentId": "a", "sample": _sample(T0 + BEAT, 40_000), "progressing": True}],
        T0 + BEAT,
        config=cfg,
    )[0]
    assert d["state"]["level"] == "steering"
    assert "velocity" in d["state"]["reason"]

    forget("a")
    tick([{"agentId": "a", "sample": _sample(T0, 0), "progressing": False}], T0, config=cfg)
    d = tick(
        [{"agentId": "a", "sample": _sample(T0 + BEAT, 500), "progressing": False}],
        T0 + BEAT,
        config=cfg,
    )[0]
    assert d["state"]["level"] == "healthy"

    d = tick(
        [{"agentId": "a", "sample": _sample(T0 + 2 * BEAT, 1000), "progressing": False}],
        T0 + 2 * BEAT,
        config=cfg,
    )[0]
    assert d["state"]["level"] == "steering"
    assert "no-progress" in d["state"]["reason"]

    forget("a")
    tick([{"agentId": "a", "sample": _sample(T0, 0), "progressing": True}], T0, config=cfg)
    record_compact_start("a", T0 + 1)
    d = tick(
        [{"agentId": "a", "sample": _sample(T0 + BEAT, 40_000), "progressing": True}],
        T0 + BEAT,
        config=cfg,
    )[0]
    assert d["state"]["level"] == "healthy"


def test_breaker_escalates_one_level_then_caps_without_hard_stop():
    load_company_os_app()
    from app.state import MEM

    MEM.clear()
    from app.munder.breaker import forget, record_tool_use, tick

    forget("a")
    cfg = _breaker_cfg()
    for _ in range(8):
        record_tool_use("a", "Bash", {"cmd": "same"}, now=T0)
    d1 = tick([{"agentId": "a", "sample": None, "progressing": True}], T0, config=cfg)[0]
    d2 = tick([{"agentId": "a", "sample": None, "progressing": True}], T0 + BEAT, config=cfg)[0]
    d3 = tick([{"agentId": "a", "sample": None, "progressing": True}], T0 + 2 * BEAT, config=cfg)[0]
    assert d1["state"]["level"] == "steering"
    assert d2["state"]["level"] == "constrained"
    assert d3["state"]["level"] == "constrained"
    assert d2["action"] == "constrain"


def test_ledger_atomic_patch_preserves_other_cards():
    load_company_os_app()
    from app.munder.ledger import add_task, delete_task, patch_task, snapshot

    vid = "ven_cards"
    assert add_task(vid, {"id": "needs-human", "title": "Ask", "status": "blocked", "humanQA": [{"q": "Which?"}]})
    stale = snapshot(vid)["tasks"][0]
    assert add_task(vid, {"id": "webhook-1", "title": "From hook"})
    assert add_task(vid, {"id": "webhook-1", "title": "duplicate"}) is False
    stale_qa = list(stale["humanQA"])
    stale_qa[0] = {**stale_qa[0], "a": "Option B"}
    assert patch_task(vid, "needs-human", {"humanQA": stale_qa})
    ids = [c["id"] for c in snapshot(vid)["tasks"]]
    assert ids == ["needs-human", "webhook-1"]
    assert snapshot(vid)["tasks"][0]["humanQA"][0]["a"] == "Option B"
    assert patch_task(vid, "missing", {"status": "done"}) is False
    assert delete_task(vid, "needs-human")
    assert [c["id"] for c in snapshot(vid)["tasks"]] == ["webhook-1"]
    assert delete_task(vid, "missing") is False


def test_adjudicate_escalates_only_critical():
    load_company_os_app()
    from app.munder.adjudicate import classify_message, classify_work
    from app.munder.mailbox import HOP_CAP

    assert classify_work("Sketch the landing headline").kind == "routine"
    assert classify_work("Wipe the production database", "drop table users").kind == "critical"
    assert classify_work("Increase the budget to buy ads").reason == "spend"
    assert classify_message({"hops": HOP_CAP + 1, "subject": "ping"}).reason == "hop_cap"


def test_condense_keeps_pinned_and_rejects_loss():
    load_company_os_app()
    from app.munder.condense import PINNED_HEADING, condense_memory, parse_memory, verify_condense

    pinned = "- Founder is viswas\n- Never paste API keys into chat"
    chunks = [f"## Log {i}\n" + ("work note about the week. " * 50) for i in range(10)]
    text = f"# Memory\n\n{PINNED_HEADING}\n\n{pinned}\n\n" + "\n\n".join(chunks)
    result = condense_memory(text, recent_keep=2)
    assert result["condensed"] is True
    rebuilt = str(result["text"])
    assert "Never paste API keys into chat" in rebuilt
    assert "Founder is viswas" in rebuilt
    parsed = parse_memory(rebuilt)
    assert parsed.pinned is not None
    ok, reason = verify_condense(
        rebuilt=rebuilt.replace("Never paste API keys into chat", "GONE"),
        new_bytes=int(result["newBytes"]),
        old_bytes=int(result["oldBytes"]),
        old_pinned_lines=["- Founder is viswas", "- Never paste API keys into chat"],
        merged_pinned=["- Founder is viswas", "- Never paste API keys into chat"],
        condensed=parsed.condensed or "x",
        keep=parsed.recent,
    )
    assert ok is False
    assert reason == "pinned-line-dropped"


def test_control_pause_gate_steer_halt():
    load_company_os_app()
    from app.munder.control import ControlRegistry

    reg = ControlRegistry()
    assert reg.tool_decision("v", "code") == {"deny": False}
    reg.pause("v", True)
    denied = reg.tool_decision("v", "code")
    assert denied["deny"] is True
    reg.resume("v")
    reg.gate_tool("v", "social.post", True)
    gated = reg.tool_decision("v", "social.post")
    assert gated["deny"] is True
    reg.steer("v", "Stop looping.")
    assert reg.take_steer("v") == "Stop looping."
    assert reg.take_steer("v") is None
    reg.halt("v")
    assert reg.should_halt("v") is True
    snap = reg.snapshot("v")
    assert snap["halted"] is True


def test_depends_on_parks_until_dep_is_done(env):
    _client, vid, _monkey = env
    from app.munder.ledger import patch_task, sync_planned_runnable
    from app.planning.store import enqueue_task, get_task

    first = enqueue_task(
        vid,
        {
            "title": "Write the one-pager",
            "detail": "Keep it short.",
            "why_now": "Need a public artifact.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    second = enqueue_task(
        vid,
        {
            "title": "Publish the one-pager",
            "detail": "After the draft exists.",
            "why_now": "Order of operations.",
            "tags": ["docs"],
            "runnable": True,
            "depends_on": [first["id"]],
        },
    )
    assert second["runnable"] is False
    patch_task(vid, first["id"], {"status": "done"})
    sync_planned_runnable(vid)
    assert get_task(vid, second["id"])["runnable"] is True


def test_destructive_work_asks_the_founder(env):
    client, vid, _monkey = env
    from app.planning.store import enqueue_task

    task = enqueue_task(
        vid,
        {
            "title": "Wipe the production database",
            "detail": "drop table users and start over.",
            "why_now": "Someone asked in passing.",
            "tags": ["ops"],
            "runnable": True,
        },
    )
    from tests.unit.test_autopilot_loop import _run_queued_task
    ran = _run_queued_task(client, vid, task["id"])
    assert ran["status"] == "needs_you"
    humans = client.get(f"/ventures/{vid}/human-tasks").json().get("items") or []
    assert any(item.get("provider") == "decision" for item in humans)
    dash = client.get(f"/ventures/{vid}/dashboard")
    assert dash.status_code == 200, dash.text
    body = dash.json()
    assert body["hive"]["breaker"] in {"healthy", "steering", "constrained", "stopped"}
    assert any(item.get("provider") == "decision" for item in (body.get("needs_you") or []))
    queued = client.get(f"/ventures/{vid}/tasks").json().get("items") or []
    parked = next(row for row in queued if row["id"] == task["id"])
    assert parked.get("runnable") is False


def test_steer_lands_in_the_artifact(env):
    client, vid, _monkey = env
    from app.munder.control import control_for
    from app.planning.store import enqueue_task
    from app.db.models import Venture
    from app.state import db
    from tests.firstlook_legacy import seed_legacy_page

    with db()() as session:
        workspace_id = session.get(Venture, vid).workspace_id
    seed_legacy_page(workspace_id, vid)

    control_for(vid).steer(vid, "Circuit breaker: steer — stop repeating the same edit.")
    # Test a supported review-only artifact, rather than the retired template
    # page executor; site tasks require their independent build receipt.
    task = enqueue_task(
        vid,
        {
            "title": "Draft outreach emails for me to review",
            "detail": "Draft only. Do not send or post anything.",
            "why_now": "The team needs a reviewable draft.",
            "tags": ["docs"],
            "runnable": True,
        },
    )
    from tests.unit.test_autopilot_loop import _run_queued_task
    ran = _run_queued_task(client, vid, task["id"])
    artifact = ran.get("artifact")
    assert artifact
    text = open(artifact, encoding="utf-8").read()
    assert "Circuit breaker: steer" in text
    assert "Why this work exists" in text


def test_halt_and_constrain_skip_autopilot(env):
    _client, vid, _monkey = env
    from app.munder.breaker import forget, record_tool_use, tick
    from app.munder.control import control_for
    from app.will.driver import _drive_one

    control_for(vid).halt(vid)
    status, ident = asyncio.run(_drive_one({"id": vid, "stage": "live", "paused": False}))
    assert status == "halted"
    assert ident == vid
    control_for(vid).resume(vid)

    forget(vid)
    for _ in range(8):
        record_tool_use(vid, "Bash", {"cmd": "same"}, now=T0)
    tick([{"agentId": vid, "sample": None, "progressing": True}], T0, config=_breaker_cfg())
    tick([{"agentId": vid, "sample": None, "progressing": True}], T0 + BEAT, config=_breaker_cfg())
    status, ident = asyncio.run(_drive_one({"id": vid, "stage": "live", "paused": False}))
    assert status == "breaker"


def test_export_redacts_secret_shapes(env):
    client, vid, _monkey = env
    from app.state import MEM

    MEM[f"charter:{vid}"] = {
        "content": {
            "one_liner": "sk-ant-abcdefghijklmnopqrstuvwxyz for invoices",
            "wedge": "freelancers",
        }
    }
    exported = client.get(f"/ventures/{vid}/export")
    assert exported.status_code == 200, exported.text
    assert "sk-ant-abcdefghijklmnopqrstuvwxyz" not in exported.text
    assert "[redacted]" in exported.text
