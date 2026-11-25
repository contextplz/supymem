"""Social failures are not task completion; simulations need owned draft proof."""
import json

import pytest
from tests.company_os_harness import company_os_client, load_company_os_app


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/social.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    from app import state
    state.reset_db_factory()
    state.MEM.clear()
    with company_os_client(main.create_app()) as c:
        yield c


async def run_social(client, monkeypatch, updates=None, corrupt_note=None):
    from app.actuation.contract import ActuationResult
    from app.planning import execute
    from app.planning.store import enqueue_task
    vid = client.post("/ventures", json={"raw_idea":"synthetic social contract"}).json()["id"]
    task = enqueue_task(vid, {"title":"Post the founder outreach", "detail":"Synthetic exact draft body.", "tags":[], "runnable":True})
    monkeypatch.setattr(execute, "_founder_gate", lambda *args: None)
    async def action(tool, venture, payload, *, allow_live):
        assert tool == "social.post" and venture == vid and allow_live is False
        data = {"mode":"simulated", "tool_id":tool, "venture_id":venture, "ok":True, "dry_run":True, "provider_response":{"payload":payload}}
        data.update(updates or {})
        return ActuationResult(**data)
    monkeypatch.setattr("app.actuation.execute.execute", action)
    if corrupt_note:
        write = execute._write_artifact
        def corrupt(*args, **kwargs):
            receipt = write(*args, **kwargs)
            from app.db.models import VentureDocument
            with execute.db()() as s:
                row = s.get(VentureDocument, "task-note:" + task["id"])
                if corrupt_note == "body":
                    row.body_md = "a different draft"
                elif corrupt_note == "owner":
                    row.venture_id = "ven_other"
                elif corrupt_note == "provenance":
                    row.provenance_json = json.dumps({"task_id":"tsk_other", "source":"task.run"})
                elif corrupt_note == "missing":
                    s.delete(row)
                s.commit()
            return receipt
        monkeypatch.setattr(execute, "_write_artifact", corrupt)
    return vid, task, await execute.execute_planned_task(vid, task["id"])


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [
    {"mode":"failed", "ok":False, "reason":"no_connected_accounts"},
    {"mode":"failed", "ok":False, "reason":"PRIVATE provider failure"},
    {"mode":"refused", "ok":False},
    {"mode":"live", "ok":True, "external_ref":"unrequested-live-effect"},
    {"ok":False}, {"dry_run":False}, {"venture_id":"ven_other"},
    {"tool_id":"other.tool"}, {"provider_response":None},
    {"provider_response":{"payload":{"content":"different body"}}},
])
async def test_failed_uncertain_or_mismatched_social_receipt_never_completes(client, monkeypatch, updates):
    vid, task, result = await run_social(client, monkeypatch, updates)
    from app.planning.store import get_task
    from app.chat.store import list_messages
    assert result["status"] == "blocked"
    saved = get_task(vid, task["id"])
    assert saved["status"] == "open" and saved["runnable"] is False and saved["finished_at"] is None
    messages = list_messages(vid, limit=50)
    assert not any("Finished **Post" in m["body_md"] for m in messages)
    assert not any("PRIVATE provider failure" in m["body_md"] for m in messages)


@pytest.mark.asyncio
async def test_simulated_social_requires_exact_owned_durable_draft(client, monkeypatch):
    vid, task, result = await run_social(client, monkeypatch)
    from app.db.models import VentureDocument
    from app.state import db
    assert result["status"] == "done" and result["completion"]["mode"] == "simulated"
    with db()() as s:
        row = s.get(VentureDocument, "task-note:" + task["id"])
        assert row.venture_id == vid and row.kind == "task_note" and row.title == task["title"]
        assert json.loads(row.provenance_json)["task_id"] == task["id"]
        assert "Synthetic exact draft body." in row.body_md
        assert "not published" in row.body_md


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt_note", ["body", "provenance", "missing"])
async def test_corrupted_durable_social_draft_cannot_complete(client, monkeypatch, corrupt_note):
    vid, task, result = await run_social(client, monkeypatch, corrupt_note=corrupt_note)
    from app.planning.store import get_task
    assert result["status"] == "blocked"
    assert get_task(vid, task["id"])["finished_at"] is None


def test_social_regressions_remain_selected_in_ci():
    from pathlib import Path
    import yaml
    config = yaml.safe_load((Path(__file__).resolve().parents[3] / ".github/workflows/ci.yml").read_text())
    paths = config["env"]["UNIT_PATHS"].split()
    for path in ("tests/unit/test_autopilot_loop.py", "tests/unit/planning/test_offline_dispatch.py",
                 "tests/unit/planning/test_social_result_completion.py"):
        assert path in paths
