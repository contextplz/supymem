from datetime import timedelta

import pytest
from fastapi import HTTPException

from tests.unit.chat.test_chat_request_status import chat_request


def test_renewed_voice_receipts_support_25_turns_while_expired_authority_is_rejected(chat_request, monkeypatch):
    from app.api import voice_api
    from app.db.models import AnalyticsEvent, Venture
    from core.clock import utcnow
    from app.credits import policy
    original = policy.get_policy
    monkeypatch.setattr(policy, "get_policy", lambda key, *args: {"voice_parity_v1": True} if key == "feature_flags" else original(key, *args))

    chat = chat_request
    started = utcnow()
    for index, domain in enumerate(["Warehouse inventory planning", "Family music lessons"]):
        venture_id, receipt_id = f"ven_voice_long_{index}", f"ane_voice_long_{index}"
        original_receipt, issued = receipt_id, started
        with chat["factory"]() as session:
            session.add(Venture(id=venture_id, workspace_id=chat["wid"], name=domain,
                raw_idea=domain, stage="draft", created_at=started, updated_at=started))
            session.add(AnalyticsEvent(id=receipt_id, person_id=chat["actor"], workspace_id=chat["wid"],
                venture_id=venture_id, event_name="voice.session_started", occurred_at=started))
            session.commit()
        for turn in range(1, 26):
            current = started + timedelta(minutes=2 * turn)
            monkeypatch.setattr(voice_api, "utcnow", lambda current=current: current)
            if current - issued >= timedelta(minutes=8):
                receipt_id, issued = f"ane_voice_long_{index}_{turn}", current
                with chat["factory"]() as session:
                    session.add(AnalyticsEvent(id=receipt_id, person_id=chat["actor"], workspace_id=chat["wid"],
                        venture_id=venture_id, event_name="voice.session_started", occurred_at=issued))
                    session.commit()
            args = {"query": f"{domain}: plan step {turn}"}
            for step in range(1, 6):
                body = voice_api.VoiceToolCall(session_event_id=receipt_id, turn_id=f"turn:{turn}",
                    call_id=f"call_{turn}_{step}", arguments=voice_api.json.dumps(args))
                key, remaining, budget, saved = voice_api._reserve_tool(body, "web_search", args,
                    chat["actor"], venture_id, chat["wid"])
                if step <= 4:
                    assert remaining == voice_api.VOICE_TOOL_WALL_S
                    assert saved is None
                    from app.chat.agent import TURN_BUDGET_MICRO

                    assert budget == TURN_BUDGET_MICRO
                    voice_api._save_tool_result(key, {"body_md": "Completed without paid search."}, 0, False)
                else:
                    assert remaining == 0
                    assert saved == {"body_md": voice_api.VOICE_TOOL_PARTIAL, "partial": True}
            if turn == 6:
                with pytest.raises(HTTPException) as stale:
                    voice_api._reserve_tool(body.model_copy(update={"session_event_id": original_receipt}),
                        "web_search", args, chat["actor"], venture_id, chat["wid"])
                assert stale.value.status_code == 404
        for person, venture, workspace in [("another-person", venture_id, chat["wid"]),
                (chat["actor"], "another-venture", chat["wid"]), (chat["actor"], venture_id, "another-workspace")]:
            with pytest.raises(HTTPException) as rejected:
                voice_api._reserve_tool(body, "web_search", args, person, venture, workspace)
            assert rejected.value.status_code == 404
        expired_at = issued + timedelta(minutes=11)
        monkeypatch.setattr(voice_api, "utcnow", lambda expired_at=expired_at: expired_at)
        with pytest.raises(HTTPException) as expired:
            voice_api._reserve_tool(body, "web_search", args, chat["actor"], venture_id, chat["wid"])
        assert expired.value.status_code == 404


def test_voice_parity_off_keeps_baseline_shared_search_budget_and_legacy_rules(chat_request, monkeypatch):
    from app.api import voice_api
    from app.chat.agent import TURN_BUDGET_MICRO
    from app.db.models import AnalyticsEvent, Person, Venture, Workspace
    from app.credits import policy
    from core.clock import utcnow
    chat = chat_request
    now = utcnow()
    original = policy.get_policy
    monkeypatch.setattr("app.tools.browse.enabled", lambda: True)
    for index, domain in enumerate(["Wholesale component sourcing", "Weekend baking lessons"]):
        venture_id, receipt_id = f"ven_flagoff_{index}", f"ane_flagoff_{index}"
        person, workspace = f"per_flagoff_{index}", f"ws_flagoff_{index}"
        with chat["factory"]() as session:
            session.add(Person(id=person, display_name=domain, created_at=now))
            session.flush()
            session.add(Workspace(id=workspace, owner_id=person, kind="venture", name=domain,
                entry_mode="idea", created_at=now, updated_at=now))
            session.flush()
            session.add(Venture(id=venture_id, workspace_id=workspace, name=domain,
                raw_idea=domain, stage="draft", created_at=now, updated_at=now))
            session.add(AnalyticsEvent(id=receipt_id, person_id=person, workspace_id=workspace,
                venture_id=venture_id, event_name="voice.session_started", occurred_at=now))
            session.commit()
        snapshots = []
        for flag in (None, False, "true", 1):
            monkeypatch.setattr(policy, "get_policy", lambda key, *args, flag=flag:
                {"voice_parity_v1": flag} if key == "feature_flags" else original(key, *args))
            body = voice_api.VoiceToolCall(session_event_id=receipt_id, turn_id=str(flag),
                call_id=str(flag), arguments=voice_api.json.dumps({"query": domain}))
            key, _, budget, saved = voice_api._reserve_tool(body, "web_search", {"query": domain},
                person, venture_id, workspace)
            assert budget == TURN_BUDGET_MICRO and saved is None
            voice_api._save_tool_result(key, {"body_md": "No paid search."}, 0, False)
            assert voice_api._reserve_tool(body, "web_search", {"query": domain},
                person, venture_id, workspace)[1:3] == (0, 0)
            snapshots.append(voice_api.build_session(domain, "model", "voice", shared_tools=True))
            entry = voice_api.build_entry_session("model", "voice")["session"]
            assert "never call both chat tools for the same words" in entry["instructions"]
            assert "tap Approve" not in entry["instructions"] and "Keep facts, numbers" not in entry["instructions"]
            assert entry["tools"][0]["description"] == "Send the founder's words to Supymem's start-screen chat and return its reply."
        assert all(snapshot == snapshots[0] for snapshot in snapshots)
        assert "Keep facts, numbers" not in snapshots[0]["session"]["instructions"]
        assert "tap Approve" not in snapshots[0]["session"]["instructions"]
        assert "Every substantive founder turn" not in snapshots[0]["session"]["instructions"]
        assert "browse_web" not in {tool["name"] for tool in snapshots[0]["session"]["tools"]}
