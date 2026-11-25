import json
from collections import Counter
from contextlib import contextmanager
from datetime import timedelta
from time import perf_counter

from sqlalchemy import event

from tests.unit.test_local_mode import audited_venture, core_imports, local_owner_app


def test_local_venture_open_query_budget(audited_venture, monkeypatch):
    from app.chat import next_moves
    from app.chat.conversations import resolve
    from app.db.models import ChatMessage, PlannedTask, VentureDocument
    from app.db.supymem_models import ExecutionPlan, PlanMilestone, PlanTask
    from app.state import db
    from core.clock import utcnow

    client, venture = audited_venture
    vid, wid = venture["id"], venture["workspace_id"]
    recommendations = []
    monkeypatch.setattr(next_moves, "post", lambda venture_id, **_: recommendations.append(venture_id))
    now = utcnow()
    conversation = resolve(vid).id
    with db()() as session:
        engine = session.get_bind()
        session.add(ExecutionPlan(id="plan", workspace_id=wid, venture_id=vid, version=1, created_at=now))
        session.flush()
        session.add(PlanMilestone(id="milestone", plan_id="plan", workspace_id=wid, venture_id=vid,
                                  order=0, title="Build", kind="build"))
        session.flush()
        for index in range(45):
            session.add(VentureDocument(id=f"order-{index}", venture_id=vid, kind="device_work_order_v1",
                                        title=venture["raw_idea"], provenance_json=json.dumps({
                                            "state": "cancelled", "workspace_id": wid, "venture_id": vid,
                                        }), created_at=now, updated_at=now))
            session.add(PlanTask(id=f"task-{index}", milestone_id="milestone", workspace_id=wid, venture_id=vid,
                                 order=index, title=f"Task {index}", state="done" if index == 44 else "pending", created_at=now))
            session.add(PlannedTask(id=f"projection-{index}", workspace_id=wid, venture_id=vid,
                                    title=f"Task {index}", why_now="Requested work", source="approved_plan:plan",
                                    rank=index, tags_json=json.dumps([f"plan_task:task-{index}"]),
                                    created_at=now, updated_at=now))
        session.add_all(ChatMessage(id=f"message-{index}", venture_id=vid, workspace_id=wid,
                                    role="assistant" if index % 2 else "user", body_md=venture["raw_idea"],
                                    created_at=now + timedelta(seconds=index)) for index in range(300))
        session.commit()

    statements = []

    def count(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    def read(path):
        started = perf_counter()
        with event_scope():
            result = client.get(path)
        assert result.status_code == 200, result.text
        print(f"{path.split('?')[0].split('/')[-1]}: {len(statements)} statements, {(perf_counter() - started)*1000:.1f} ms",
              Counter(line.splitlines()[0].split()[0] for line in statements))
        return result.json(), statements[:]

    @contextmanager
    def event_scope():
        statements.clear()
        event.listen(engine, "before_cursor_execute", count)
        try:
            yield
        finally:
            event.remove(engine, "before_cursor_execute", count)

    dashboard, dashboard_sql = read(f"/ventures/{vid}/dashboard")
    chat, chat_sql = read(f"/ventures/{vid}/chat?conversation_id={conversation}")
    older, older_sql = read(f"/ventures/{vid}/chat?conversation_id={chat['conversation_id']}&before={chat['next_cursor']}")
    read(f"/api/v1/connections/status?workspace_id={wid}")

    assert dashboard["tiles"]["next_up"]["tasks"][0]["id"] == "projection-0"
    assert dashboard["tiles"]["tasks"]["open"] == 44
    assert len(chat["items"]) == len(older["items"]) == 100
    assert chat["items"][0]["id"] == "message-200" and older["items"][-1]["id"] == "message-199"
    assert sum("FROM plan_tasks" in sql for sql in dashboard_sql) <= 3
    assert len(dashboard_sql) <= 110
    assert len(chat_sql) <= 12 and len(older_sql) <= 13
    assert all(not sql.lstrip().startswith(("BEGIN IMMEDIATE", "INSERT", "UPDATE")) for sql in (*chat_sql, *older_sql))
    assert recommendations == [vid]


def test_local_connection_status_reads_last_saved_provider_result(audited_venture, monkeypatch):
    from app.connections import hub, local_config

    client, venture = audited_venture
    monkeypatch.setattr(local_config, "missing", lambda provider: [] if provider in {"composio", "discord"} else [provider])
    monkeypatch.setattr(hub, "_saved_discord_status", lambda _wid: {
        "connection_id": "receipt", "status": "pending", "last_sync": None,
    })
    polls = []
    monkeypatch.setattr(hub, "refresh_discord_status", polls.append)
    for _ in range(2):
        result = client.get(f"/api/v1/connections/status?workspace_id={venture['workspace_id']}")
        assert result.status_code == 200, result.text
        discord = result.json()["connections"]["discord"]
        assert discord["status"] == "pending" and discord["action"] == "sync"
    assert not polls
