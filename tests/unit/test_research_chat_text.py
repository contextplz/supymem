"""Chat text for queued research must not expose the raw job id."""
from tests.company_os_harness import load_company_os_app


def test_research_queued_text_has_no_job_id(monkeypatch, tmp_path):
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.chat import execute
    from app.research import service
    from app.ventures import build_prerequisites
    monkeypatch.setattr(build_prerequisites, "answers_pending", lambda *args: False)
    monkeypatch.setattr(execute, "_confirm_idea_for_research", lambda *a, **k: None)
    monkeypatch.setattr(service, "start", lambda venture_id: {"job_id": "job_5q3df6jzjyzd"})
    out = execute._research_start("ven_x", "user_x")
    assert "job_" not in out["body_md"] and "queued" not in out["body_md"]
    assert out["action_started"] is True


def test_research_offer_omits_usage_costs(monkeypatch):
    load_company_os_app()
    from app.chat import build_path
    monkeypatch.setattr(build_path, "research_prerequisites", lambda *args: None)

    card = build_path.research_card("ven_x")["actions"][0]["confirmation"]
    assert "credit" not in card["interpretation"].lower()
    assert all("credit" not in line.lower() for line in card["consequences"])
    assert "Nothing is built or published. Missing evidence stays unknown." in card["consequences"]
