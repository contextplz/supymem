"""Validation dossier must survive a missing local sidecar (ECS replicas)."""

from __future__ import annotations

from tests.company_os_harness import company_os_test_client as TestClient

from tests.company_os_harness import load_company_os_app


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/dossier.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("RESEARCH_KEYLESS", "0")
    monkeypatch.setenv("OPENWORKER_SEARCH", "0")
    main = load_company_os_app()
    from app import state

    state.reset_db_factory()
    return TestClient(main.create_app())


def test_persisted_dossier_is_available_without_files(tmp_path, monkeypatch) -> None:
    client = _client(tmp_path, monkeypatch)
    ven = client.post(
        "/ventures",
        json={"raw_idea": "A waitlist for neighbourhood bakers taking custom cake orders", "start": False},
    )
    assert ven.status_code == 201, ven.text
    vid = ven.json()["id"]
    from app.research.dossier import persist_validation_dossier, load_validation_dossier

    persist_validation_dossier(
        vid,
        {
            "engine": "test_engine",
            "mode": "live",
            "fast_scan": {
                "mode": "live",
                "provider": "duckduckgo",
                "elapsed_ms": 2100,
                "depth": "snippet_scan",
                "queries": ["bakers competitors"],
                "sources": [{"url": "https://example.com", "title": "Example"}],
                "summary": "Live web scan for bakers.",
            },
            "markdown": "# Validation dossier: bakers\n\nLive web scan for bakers.\n",
        },
    )
    # Pretend this replica never saw the sidecar files.
    from pathlib import Path

    research = tmp_path / "ventures" / vid / "research"
    if research.is_dir():
        for child in research.iterdir():
            child.unlink()
    md, meta = load_validation_dossier(vid)
    assert md and "bakers" in md
    assert meta.get("engine") == "test_engine"

    got = client.get(f"/ventures/{vid}/research/dossier")
    assert got.status_code == 200, got.text
    body = got.json()
    assert body["available"] is True
    assert body["fast_scan"]["provider"] == "duckduckgo"
    assert "bakers" in (body.get("markdown") or "")


def test_research_api_and_exports_hide_system_kinds_at_every_level(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    vid = client.post("/ventures", json={"raw_idea": "Robotics debugging", "start": False}).json()["id"]
    from app.research.dossier import persist_validation_dossier, load_validation_dossier
    from app.research.presentation import gap

    meta = {"coverage_gaps": [gap("system_verification", "4 of 33 sources were verified against full pages."),
                              gap("pricing", "No evidence establishes pricing.")],
            "limitations": ["YC directory matching is unavailable while data-use rights are reviewed."],
            "fast_scan": {"summary": {"coverage_gaps": [gap("system_request_limit", "Only part of this discussion fit within the request limit.")]},
                          "sources": [{"url": "https://example.com", "title": "Product"}]},
            "markdown": "# Robotics research\n\n## Research gaps\n- 4 of 33 sources were verified against full pages.\n- No evidence establishes pricing.\n\n## Sources\n- [Product](https://example.com)\n"}
    persist_validation_dossier(vid, meta)
    response = client.get(f"/ventures/{vid}/research/dossier")
    assert response.status_code == 200
    body = response.json()
    assert len(body["summary"]["next_questions"]) == 1
    assert body["next_questions"][0]["kind"] == "pricing"
    for suffix in ("", ".md", ".html"):
        rendered = response.text if not suffix else client.get(f"/ventures/{vid}/research/dossier{suffix}").text
        for internal in ("4 of 33", "YC directory", "request limit", "system_verification", "establishes", "Research gaps"):
            assert internal not in rendered
        assert "Your opening" in rendered and "What to find out next" not in rendered
    markdown, saved = load_validation_dossier(vid)
    assert "4 of 33" in markdown
    assert saved["gap_items"][0]["kind"] == "system_verification"
