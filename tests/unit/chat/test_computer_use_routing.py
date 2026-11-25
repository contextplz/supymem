import asyncio

import pytest

from tests.unit.chat.test_assistant_routing import env


def test_browser_routing_table_preserves_device_context_and_native_targets(env, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.build import device_orders
    from app.chat import browser_prepare, llm_router

    client, venture_id = env
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("DEVICE_GENERAL_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "1")
    calls = []

    def prepare(goal, context):
        calls.append(goal)
        context.actions.append({"label": "Review browser task", "href": "/review/pending"})
        return "Your browser task is ready for review. Nothing has started."

    async def forbidden(*args, **kwargs):
        pytest.fail("Browser routing must not dispatch a model or native task")

    monkeypatch.setattr(browser_prepare, "prepare_founder_review", prepare)
    monkeypatch.setattr(llm_router, "route", forbidden)
    tasks = (
        "open https://invoices.example/plans and compare business subscription prices",
        "visit https://hiking.example/routes and read the trail distances",
    )
    goals = [f"On {device}, {task}. Do not change anything."
             for task in tasks for device in ("my Mac", "my computer", "my laptop", "this PC")]
    goals += [f"Please use my Mac to {task}" for task in tasks]
    goals += [
        "On my Mac, open https://en.wikipedia.org/wiki/Houseplant and give me the key points from the whole page, including the sections near the bottom",
        "Could you please use my Mac to read https://www.atlassian.com/software/jira and summarize every section?",
    ]
    goals += ["Using my desktop, browse the website for business plans",
              "On my laptop, open the site for walking routes"]
    for task in tasks:  # no Mac named: the page is read and answered in chat
        for goal in (f"{task.capitalize()}. Do not change anything.", f"Open the website at {task.split()[1]} and read its contents"):
            assert not device_orders.browser_task_request(goal) and not browser_prepare.explicit_browser_request(goal), goal
    for goal in goals:
        assert device_orders.browser_task_request(goal), goal
        assert device_orders.classify_device_chat_intent(goal) == "general_browser", goal
        assert browser_prepare.explicit_browser_request(goal), goal
        reply = client.post(f"/ventures/{venture_id}/chat", json={"message": goal})
        assert reply.status_code == 200, reply.text
        assert reply.json()["actions"][0]["label"] == "Review browser task", reply.json()
        assert calls[-1] == goal
    assert calls == goals
    for task in ("open my website for procurement teams", "read my site for family walks",
                 "open the website preview", "open this site draft",
                 "open my website for procurement teams. Rebuild its pricing page",
                 "read my site for family walks. Update its headline"):
        assert not device_orders.browser_task_request(task), task
        assert not browser_prepare.explicit_browser_request(task), task
        assert device_orders.classify_device_chat_intent(task) is None, task
    for task in ("open TextEdit and read the project checklist",
                 "open Photos and inspect the holiday album",
                 "open TextEdit and type https://invoices.example/plans",
                 "open Photos and search for https://hiking.example/routes"):
        goal = f"Use my Mac to {task}"
        assert not device_orders.browser_task_request(goal), goal
        assert not browser_prepare.explicit_browser_request(goal), goal
        assert device_orders.classify_device_chat_intent(goal) == "desktop", goal
    for task in tasks:
        for goal in (f"Do not {task}", f'Your note says "{task}"'):
            assert not device_orders.browser_task_request(goal), goal
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "0")
    monkeypatch.delenv(browser_prepare.FLAG, raising=False)
    monkeypatch.delenv(browser_prepare.PIN, raising=False)
    assert all(browser_prepare.explicit_browser_request(goal) for goal in goals)
    monkeypatch.setenv(browser_prepare.FLAG, "1")
    monkeypatch.setenv(browser_prepare.PIN, "a" * 64)
    assert all(browser_prepare.explicit_browser_request(goal) for goal in goals)


def test_unavailable_native_reply_offers_working_manual_recovery(env, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.chat import llm_router, recovery
    from app.chat.intent import classify_intent, split_speech_acts

    client, venture_id = env
    monkeypatch.setenv("DEVICE_DESKTOP_EXECUTION_ENABLED", "0")
    monkeypatch.setenv("DEVICE_BROWSER_EXECUTION_ENABLED", "1")
    monkeypatch.setenv("DEVICE_BROWSER_CUAI_EXECUTION_ENABLED", "1")

    async def forbidden(*args, **kwargs):
        pytest.fail("Native unavailability must not dispatch a routing model")

    async def offline(*args, **kwargs):
        return None

    monkeypatch.setattr(llm_router, "route", forbidden)
    monkeypatch.setattr(recovery, "_llm", offline)
    for task in ("open TextEdit and read the project checklist",
                 "open Photos and inspect the holiday album"):
        goal = f"Use my Mac to {task}"
        manual = f"Give me manual steps for this task. Answer only; do not prepare a Mac task: {goal}"
        assert classify_intent(manual) == "assistant"
        assert all(intent == "assistant" for intent, _ in split_speech_acts(manual))
        result = client.post(f"/ventures/{venture_id}/chat", json={"message": goal})
        assert result.status_code == 200, result.text
        reply = result.json()
        action = next((item for item in reply["actions"]
                       if item.get("action") == recovery.ALTERNATIVE), None)
        assert action == recovery.button("mac.task"), reply
        guide = asyncio.run(recovery.alternative(venture_id, action["payload"]))
        assert "Do the task by hand" in guide["body_md"]
        assert any(item.get("action") == recovery.SUBMIT for item in guide["actions"])
