"""Agent-Reach bridge: route URLs, wrap SKILL CLIs, never invent backends."""

from __future__ import annotations

import json

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture()
def reach(tmp_path, monkeypatch):
    monkeypatch.setenv("COMPANY_OS_DB_URL", f"sqlite:///{tmp_path}/reach.db")
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.research import agent_reach_bridge as mod

    if not mod.vendored():
        pytest.skip("vendor/agent-reach not cloned")
    return mod


def test_detects_platforms_and_urls(reach) -> None:
    assert "youtube" in reach.detect_platforms("pull the youtube transcript")
    assert "github" in reach.detect_platforms("search github for waitlist saas")
    urls = reach.urls_in("see https://github.com/acme/app and https://youtu.be/dQw4w9wg")
    assert any("github.com" in u for u in urls)
    assert any("youtu.be" in u for u in urls)


def test_channel_for_url_skips_generic_web(reach) -> None:
    yt = reach.channel_for_url("https://www.youtube.com/watch?v=aaaaaaaaaaa")
    assert yt is not None and yt.name == "youtube"
    gh = reach.channel_for_url("https://github.com/Panniantong/Agent-Reach")
    assert gh is not None and gh.name == "github"
    # Ordinary pages are not specialists — web_fetch keeps OpenWorker → Jina.
    assert reach.channel_for_url("https://example.com/about") is None


def test_read_url_refuses_loopback(reach) -> None:
    with pytest.raises(ValueError):
        reach.read_url("http://127.0.0.1/secret")


def test_github_search_uses_skill_command(reach, monkeypatch) -> None:
    captured: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        captured.append(list(argv))
        return 0, json.dumps(
            [
                {
                    "url": "https://github.com/acme/waitlist",
                    "fullName": "acme/waitlist",
                    "description": "A waitlist kit",
                    "stargazersCount": 12,
                }
            ]
        )

    monkeypatch.setattr(reach, "_run", fake_run)
    monkeypatch.setattr(reach.shutil, "which", lambda name: "/bin/" + name)
    result = reach.search_platform("github", "waitlist saas", limit=3)
    assert result["error"] is None
    assert result["hits"][0]["url"] == "https://github.com/acme/waitlist"
    assert captured[0][:4] == ["gh", "search", "repos", "waitlist saas"]
    assert "--sort" in captured[0] and "stars" in captured[0]


def test_youtube_search_uses_ytsearch(reach, monkeypatch) -> None:
    def fake_run(argv, **_kwargs):
        assert argv[0] == "yt-dlp"
        assert argv[1] == "--dump-json"
        assert argv[2].startswith("ytsearch")
        return 0, json.dumps(
            {
                "title": "Demo",
                "id": "abcdefghijk",
                "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk",
                "description": "A demo video",
            }
        )

    monkeypatch.setattr(reach, "_run", fake_run)
    monkeypatch.setattr(reach.shutil, "which", lambda name: "/bin/" + name)
    result = reach.search_platform("youtube", "form builders")
    assert result["hits"][0]["url"].endswith("abcdefghijk")


def test_meta_lists_channels(reach) -> None:
    info = reach.meta()
    assert info["vendored"] is True
    names = {row["name"] for row in info["channels"]}
    assert {"web", "youtube", "github", "v2ex", "rss", "twitter"} <= names


def test_intent_routes_platform_queries() -> None:
    load_company_os_app()
    from app.chat.intent import classify_intent
    from app.chat.router import _EXECUTABLE

    assert classify_intent("search youtube for landing page teardowns") == "agent_reach"
    assert classify_intent("https://www.youtube.com/watch?v=aaaaaaaaaaa") == "agent_reach"
    assert "agent_reach" in _EXECUTABLE
