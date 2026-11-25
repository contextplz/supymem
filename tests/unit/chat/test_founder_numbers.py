import json
from datetime import datetime
from types import SimpleNamespace

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


@pytest.mark.parametrize("idea", ["Monthly safety audits for industrial teams", "Family music lessons at home"])
def test_numbers_chat_collects_only_real_inputs_and_survives_restart(tmp_path, monkeypatch, idea):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/numbers.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    main = load_company_os_app()
    from app import state
    from app.analytics import chat_usage
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat import llm_router, numbers, store
    from app.credits import metered
    from app.experiments.price_band import PRICE_FIELDS

    state.reset_db_factory()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Founder calculations must not call any model.")

    monkeypatch.setattr(metered, "llm", forbidden)
    monkeypatch.setattr(llm_router, "route", forbidden)
    with TestClient(main.create_app()) as client:
        venture = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
        other = client.post("/ventures", json={"raw_idea": "A different workspace idea", "start": False}).json()["id"]

        def say(text):
            response = client.post(f"/ventures/{venture}/chat", json={"message": text})
            assert response.status_code == 200, response.text
            reply = response.json()
            assert reply["kind"] == "numbers"
            assert all(item == {"question": True} for item in reply["citations"])
            assert "\u2014" not in reply["body_md"]
            if reply["citations"]:
                assert not reply.get("next_moves")
            return reply

        prompt = say("Work out my price range")
        assert "only real interview or survey answers" in prompt["body_md"]
        assert all(str(index) + "." in prompt["body_md"] for index in range(1, 5))
        assert "unchanged" in say("guess for me")["body_md"]
        assert numbers.command(other, "1,2,3,4") is None
        insufficient = say("10,20,30,40\n20,30,40,50\n30,40,50,60\n40,50,60,70")
        assert "You have 4" in insufficient["body_md"]
        invalid = say("[1, 2]")
        assert "unchanged" in invalid["body_md"]
        state.MEM.clear()
        state.reset_db_factory()
        result = say("50,60,70,80\n40,30,20,10")
        assert "from 5 real answers" in result["body_md"] and "1 dropped (1 out of order" in result["body_md"]
        assert "40.00 to 50.00" in result["body_md"]
        repeated = say(",".join(PRICE_FIELDS) + "\n60,70,80,90")
        assert "from 6 real answers" in repeated["body_md"]
        assert numbers.command(venture, "Find customers for me") is None
        assert "stopped" in say("cancel")["body_md"]
        assert numbers.command(venture, "10,20,30,40") is None

        first = say("Do the numbers for my venture")
        assert "What price" in first["body_md"] and first["body_md"].count("?") == 1
        assert "What price" in say("unknown")["body_md"]
        invalid = say("-1")
        assert "non-negative" in invalid["body_md"] and "What price" in invalid["body_md"]
        assert "What price" in say("100%")["body_md"]
        assert "cost you to serve" in say("100")["body_md"]
        state.MEM.clear()
        state.reset_db_factory()
        store.append_message(venture, role="assistant", body_md="You can return to your calculation when ready.")
        resumed = say("Do the numbers for my venture")
        assert "cost you to serve" in resumed["body_md"] and "What price" not in resumed["body_md"]
        assert "monthly customer churn" in say("20")["body_md"]
        assert "monthly customer churn" in say("I don't know")["body_md"]
        assert "monthly customer churn" in say("5%%")["body_md"]
        assert "acquire" in say("5%")["body_md"]
        assert "fixed monthly costs" in say("160")["body_md"]
        assert "customer ramp" in say("800")["body_md"]
        assert "whole customer counts" in say("[1.5]")["body_md"]
        assert "starting cash" in say("[0, 10, 20, 30, 40]")["body_md"]
        finished = say("skip")
        assert "3,440.00" in finished["body_md"] and "Additional cash needed" not in finished["body_md"]
        assert "Price -10%" in finished["body_md"] and "CAC +25%" in finished["body_md"]
        assert finished["numbers_input"]["complete"] is True
        assert numbers.command(venture, "100") is None
        supplied = {"price_per_month": 100, "cost_to_serve_per_month": 20, "monthly_churn": "5%",
                    "cac": 160, "fixed_monthly_costs": 800, "customer_ramp": [0, 10, 20, 30, 40], "starting_cash": 1000}
        pasted = say("Do the numbers for my venture:\n" + json.dumps(supplied))
        assert "Additional cash needed | 2,440.00" in pasted["body_md"]
        recovered_price = say("Work out my price range")
        assert len(recovered_price["numbers_input"]["answers"]) == 7
        assert "from 6 real answers" in recovered_price["body_md"]
        repeated_economics = say("Do the numbers for my venture")
        assert "Additional cash needed | 2,440.00" in repeated_economics["body_md"]
        saved = store.list_messages(venture)
        assert any(row.get("numbers_input", {}).get("inputs", {}).get("price_per_month") == 100 for row in saved)
        for action, slug in [("price_band.calculate", "price_band"), ("unit_economics.calculate", "unit_economics")]:
            assert ACTION_SLUG[action] == slug and action in chat_usage.READ_INTENT_FEATURE


@pytest.mark.parametrize("idea", ["Equipment maintenance for factories", "Weekend cooking classes for families"])
def test_numbers_resume_after_requested_next_moves(tmp_path, monkeypatch, idea):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/numbers.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "memory.db"))
    main = load_company_os_app()
    from app import state
    from app.chat import next_moves, store
    from app.credits import metered

    state.reset_db_factory()

    async def forbidden(*args, **kwargs):
        raise AssertionError("Numbers and next moves must not call a model.")

    monkeypatch.setattr(metered, "llm", forbidden)
    monkeypatch.setattr(next_moves, "compose", lambda venture_id, **kwargs: {
        "kind": "next_moves", "body_md": f"Your next step for {idea} is ready.", "actions": [],
        "citations": [{"next_moves": {"stage": "Shaping your idea", "audience": idea}}],
    })
    with TestClient(main.create_app()) as client:
        venture = client.post("/ventures", json={"raw_idea": idea, "start": False}).json()["id"]
        first = client.post(f"/ventures/{venture}/chat", json={"message": "Do the numbers for my venture"})
        assert first.status_code == 200 and first.json()["numbers_input"]["awaiting"] == "price_per_month"
        requested = client.post(f"/ventures/{venture}/chat", json={"message": "What should I do next?"})
        assert requested.status_code == 200
        reply = requested.json()
        assert reply["next_moves"]["audience"] == idea and reply["citations"] == []
        assert store.list_messages(venture)[-1] == reply
        resumed = client.post(f"/ventures/{venture}/chat", json={"message": "Do the numbers for my venture"})
        assert resumed.status_code == 200 and resumed.json()["numbers_input"] == first.json()["numbers_input"]
        answered = client.post(f"/ventures/{venture}/chat", json={"message": "80"})
        assert answered.status_code == 200 and answered.json()["numbers_input"]["inputs"] == {"price_per_month": 80}


@pytest.mark.parametrize("audience", ["Equipment maintenance for factories", "Weekend cooking classes for families"])
def test_saved_reply_keeps_numbers_next_moves_and_exploration_separate_from_citations(audience):
    load_company_os_app()
    from app.chat.store import _ser

    numbers_input = {"mode": "economics", "inputs": {"price_per_month": 80}, "audience": audience}
    next_moves = {"stage": "Shaping your idea", "audience": audience}
    exploration = {"round_id": "round_saved", "options": [{"title": audience}], "state": "ready"}
    citation = {"source": "founder_interview", "title": audience}
    metadata = [{"numbers_input": numbers_input}, {"next_moves": next_moves},
                {"exploration_options": exploration}, {"_proactive": {"audience": audience}}, citation]
    row = SimpleNamespace(id="chat_saved", conversation_id="conversation_saved", workspace_id="workspace_saved",
                          venture_id="venture_saved", role="assistant",
                          kind="exploration_options", body_md=f"Your research for {audience} is ready.\n\nThis uses 2 credits.",
                          actions_json="[]", citations_json=json.dumps(metadata), created_at=datetime.now())
    reply = _ser(row)
    assert reply["numbers_input"] == numbers_input and reply["next_moves"] == next_moves
    assert all(reply[key] == value for key, value in exploration.items())
    assert reply["citations"] == [citation]
    assert audience in reply["body_md"] and "credit" not in reply["body_md"].lower()
    assert "credit" not in json.dumps(reply["parts"]).lower()
    row.role = "user"
    user = _ser(row)
    assert "numbers_input" not in user and "next_moves" not in user
