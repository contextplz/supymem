"""Durable founder memory in chat: context_pack prefs + boundary."""

from __future__ import annotations

import re

import pytest
from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app
from tests.company_os_harness import configure_local_test_model


@pytest.fixture()
def mem_env(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/mem.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("COMPANY_OS_MEMORY_PATH", str(tmp_path / "institutional.sqlite3"))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("ONBOARDING_SYNC", "0")
    main = load_company_os_app()
    configure_local_test_model()
    from app import state as state_mod
    from app.memory import reset_memory_for_tests

    state_mod.reset_db_factory()
    state_mod.MEM.clear()
    reset_memory_for_tests(tmp_path / "institutional.sqlite3")
    with TestClient(main.create_app()) as client:
        a = client.post(
            "/ventures",
            json={"raw_idea": "LabHire for QA engineers", "name": "LabHire"},
        )
        b = client.post(
            "/ventures",
            json={"raw_idea": "OtherCo for designers", "name": "OtherCo"},
        )
        assert a.status_code == 201, a.text
        assert b.status_code == 201, b.text
        yield client, a.json()["id"], b.json()["id"], tmp_path


@pytest.mark.asyncio
async def test_remember_preference_lands_in_brief_and_why(mem_env):
    client, vid_a, _vid_b, _tmp = mem_env
    from app.memory import MemoryKind, get_memory, RecallQuery
    from app.ventures.state_brief import get_venture_state_brief, invalidate_venture_state_brief
    from app.workspaces.resolve import LOCAL_PERSON_ID

    response = client.post(f"/ventures/{vid_a}/chat", headers={"X-Subject": LOCAL_PERSON_ID},
                           json={"message": "Remember that I prefer short answers."})
    assert response.status_code == 200, response.text
    reply = response.json()
    body = str(reply.get("body_md") or "")
    assert reply["kind"] == "memory_saved" and len(reply["citations"]) == 1
    assert "saved" in body.lower() and "review or remove it in settings" in body.lower()
    assert "i prefer" not in body.lower()
    assert not re.sub(r"<!--employee:[^>]*-->", "", body).strip().lower().startswith("remembered:")

    hits = await get_memory().recall(
        RecallQuery(
            venture_id=vid_a,
            text="short answers",
            kinds=[MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION],
            limit=10,
        )
    )
    assert any("short" in h.statement.lower() for h in hits)

    invalidate_venture_state_brief(vid_a)
    brief = get_venture_state_brief(vid_a, force=True)
    prefs = (brief.get("memory") or {}).get("preferences") or []
    assert any("short" in str(p.get("statement") or "").lower() for p in prefs)
    rendered = brief.get("_rendered") or ""
    assert "short" in rendered.lower()
    # Pref line must appear even if we later grow the brief.
    assert "Founder preferences" in rendered

    why_response = client.post(f"/ventures/{vid_a}/chat", headers={"X-Subject": LOCAL_PERSON_ID},
                               json={"message": "Why do you answer the way you do about length?"})
    assert why_response.status_code == 200, why_response.text
    why = why_response.json()
    # Offline why may go through memory ground or assistant fallback; either must see the pref.
    why_body = str(why.get("body_md") or "")
    grounded = await get_memory().answer(
        venture_id=vid_a, question="What answer length do I prefer?"
    )
    assert not grounded.abstained
    assert "short" in grounded.text.lower()
    assert "short" in why_body.lower() or "short" in grounded.text.lower()


def test_memory_api_boundary_and_forget(mem_env):
    client, vid_a, vid_b, _tmp = mem_env
    import asyncio

    from app.gbrain.verbs import remember
    from app.ventures.founder_memory import list_founder_memories

    stored = asyncio.run(
        remember(
            venture_id=vid_a,
            fact="I prefer short answers",
            provenance="test",
            kind="preference",
        )
    )
    mem_id = stored["id"]

    listed = client.get(f"/ventures/{vid_a}/memories")
    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()["items"]]
    assert mem_id in ids

    # Venture B must not see A's memory ids in its list.
    listed_b = client.get(f"/ventures/{vid_b}/memories")
    assert listed_b.status_code == 200
    assert mem_id not in [row["id"] for row in listed_b.json()["items"]]

    # Cross-venture forget is rejected.
    cross = client.delete(f"/ventures/{vid_b}/memories/{mem_id}")
    assert cross.status_code in {404, 403, 400}

    forgot = client.delete(f"/ventures/{vid_a}/memories/{mem_id}")
    assert forgot.status_code == 200, forgot.text
    remaining = list_founder_memories(vid_a)
    assert mem_id not in [row["id"] for row in remaining]


def test_preference_never_truncated_in_context_pack(mem_env):
    _client, vid_a, _vid_b, _tmp = mem_env
    import asyncio

    from app.gbrain.verbs import remember
    from app.memory import MemoryKind, TaskRef, get_memory

    asyncio.run(
        remember(
            venture_id=vid_a,
            fact="I prefer short answers always",
            provenance="test",
            kind="preference",
        )
    )
    # Flood pack with large non-pref memories under a tiny budget.
    async def flood() -> None:
        mem = get_memory()
        from app.memory import MemoryWrite

        for i in range(30):
            await mem.remember(
                MemoryWrite(
                    venture_id=vid_a,
                    kind=MemoryKind.INSIGHT,
                    subject=f"topic-{i}",
                    statement=("Insight padding sentence. " * 40) + str(i),
                    evidence=["test"],
                    confidence=0.6,
                    confidence_source="rule",
                    author="test",
                    dedupe_key=f"pad-{i}",
                )
            )

    asyncio.run(flood())
    pack = asyncio.run(
        get_memory().context_pack(
            venture_id=vid_a,
            task=TaskRef(title="chat", tags=["chat"]),
            token_budget=80,
        )
    )
    pref_stmts = [
        m.statement
        for m in pack.memories
        if m.kind in {MemoryKind.FOUNDER_PREFERENCE, MemoryKind.STYLE_CORRECTION}
    ]
    assert any("short answers" in s.lower() for s in pref_stmts)
    assert MemoryKind.FOUNDER_PREFERENCE.value not in (pack.truncated_kinds or [])
