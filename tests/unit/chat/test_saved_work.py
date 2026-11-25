from __future__ import annotations

from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def test_saved_task_note_survives_worker_file_loss_and_chat_reads_it(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/saved-work.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    main = load_company_os_app()
    from app import state
    from app.chat.intent import classify_intent
    from app.planning.execute import _write_artifact
    from app.planning.saved_work import task_note

    state.reset_db_factory()
    state.MEM.clear()
    question = ("Show me the exact saved draft from the LabHire 15-minute problem-interview "
                "invitation task completed at 2:24 AM. Where can I review it in Supymem? "
                "Do not send anything.")
    assert classify_intent(question) == "saved_work"
    assert classify_intent("Draft a new interview invitation. Do not send it.") == "change"
    with TestClient(main.create_app()) as client:
        response = client.post("/ventures", json={"raw_idea": "LabHire for engineers"})
        assert response.status_code == 201, response.text
        venture_id = response.json()["id"]
        path = tmp_path / "worker-only" / "ventures" / venture_id / "repo" / "tasks" / "invitation.md"
        _write_artifact(path, "Draft one problem-interview invitation for LabHire",
                        "Status: completed\n\nOptional 15-minute interview invitation. No message sent.",
                        {"name": "Samira"}, venture_id=venture_id, task_id="tsk_interview")
        path.unlink()  # API and worker have separate ephemeral filesystems.
        assert "Optional 15-minute" in task_note(venture_id, "tsk_interview")
        assert task_note("ven_other", "tsk_interview") == ""
        reply = client.post(f"/ventures/{venture_id}/chat", json={"message": question})
        assert reply.status_code == 200, reply.text
        body = reply.json()["body_md"]
        assert "Optional 15-minute interview invitation" in body
        assert "not a sent message" in body
        assert reply.json()["actions"] == []
        by_id = client.post(f"/ventures/{venture_id}/chat", json={"message":
            "Show saved notes for task tsk_interview"})
        assert "Optional 15-minute interview invitation" in by_id.json()["body_md"]
        from app.planning.store import list_tasks

        assert list_tasks(venture_id) == []


def test_missing_legacy_note_is_reported_without_queuing(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/missing-work.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    state.MEM.clear()
    with TestClient(main.create_app()) as client:
        venture_id = client.post("/ventures", json={"raw_idea": "LabHire for engineers"}).json()["id"]
        reply = client.post(f"/ventures/{venture_id}/chat", json={"message":
            "Show me the saved notes from my completed interview task."})
        assert reply.status_code == 200, reply.text
        assert "cannot retrieve a durable saved task note" in reply.json()["body_md"]
        from app.planning.store import list_tasks

        assert list_tasks(venture_id) == []
