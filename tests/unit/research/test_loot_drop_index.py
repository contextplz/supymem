"""Offline JSON-LD, sitemap, politeness, conditional fetches and weekly jobs."""
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, inspect

from tests.company_os_harness import load_company_os_app

FIXTURE = Path(__file__).with_name("fixtures") / "graveyard_similar.html"
ROOT = "https://www.loot-drop.io"
URL = ROOT + "/startup/42-crumbqueue"


@pytest.fixture
def index(tmp_path, monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("LLM_OFFLINE", "1")
    load_company_os_app()
    from app.research.prior_art import loot_drop
    return loot_drop


def test_json_ld_extracts_only_internal_short_fields(index):
    result = index.parse_page(URL, FIXTURE.read_text())
    assert result["name"] == "CrumbQueue" and result["category"] == "Bakery software"
    assert (result["founded"], result["ended"], result["cash_burned"]) == ("2018", "2022", "$2 million")
    assert "Too few shops" in result["text"] and "Catalogue navigation" not in result["text"]
    assert index.parse_page(URL, '<script type="application/ld+json">bad JSON</script>') is None
    graph = '<script type="application/ld+json">' + json.dumps({"@graph": [{"headline": "Empty"}, {
        "headline": "Test - Why This Startup Failed", "description": "A short summary."}]}) + '</script>'
    assert index.parse_page(URL, graph)["name"] == "Test"


def test_sitemap_and_rank_ignore_disallowed_urls_and_choose_problem(index):
    xml = f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"><url><loc>{URL}</loc></url>' + \
          f'<url><loc>{ROOT}/startup.html</loc></url><url><loc>{URL}</loc></url></urlset>'
    assert index.sitemap_urls(xml) == [URL]
    rows = [{"url": URL, "name": "CrumbQueue", "description": "bakery orders intake software"},
            {"url": URL.replace("42", "43"), "name": "OvenFleet", "description": "industrial oven maintenance"},
            {"url": ROOT + "/startup.html", "name": "Catalogue", "description": "bakery orders intake software"}]
    ranked = index.rank({"problem": "Lost bakery orders", "customer": "Independent bakery owners"},
                        {"category_terms": ["order intake"]}, rows)
    assert [row["name"] for row in ranked] == ["CrumbQueue"]


def test_reader_user_agent_two_second_slots_conditional_headers_and_robots(index, monkeypatch):
    calls, waits = [], []
    monkeypatch.setattr(index.time, "sleep", waits.append)
    original = httpx.Client
    def respond(request):
        calls.append(request)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /startup.html\nDisallow: /startup/9-hidden")
        return httpx.Response(304)
    monkeypatch.setattr(index.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with index.Reader() as reader:
        assert reader.get(URL, etag='"version-1"', modified="Wed, 01 Oct 2026 00:00:00 GMT").status_code == 304
        for url in (ROOT + "/startup.html", ROOT + "/startup/9-hidden", "https://evil.example/startup/42-name"):
            with pytest.raises(ValueError):
                reader.get(url)
    assert waits == [2, 2] and len(calls) == 2
    assert all(request.headers["User-Agent"] == index.USER_AGENT for request in calls)
    assert calls[1].headers["If-None-Match"] == '"version-1"'
    assert "If-Modified-Since" in calls[1].headers


@pytest.mark.parametrize("status,body", [(302, ""), (200, "x" * 1_000_001)])
def test_reader_does_not_follow_redirects_or_accept_large_pages(index, monkeypatch, status, body):
    calls = []
    monkeypatch.setattr(index.time, "sleep", lambda seconds: None)
    original = httpx.Client
    def respond(request):
        calls.append(request.url.path)
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nAllow: /startup/")
        return httpx.Response(status, text=body, headers={"Location": "https://evil.example/"})
    monkeypatch.setattr(index.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with index.Reader() as reader:
        with pytest.raises((ValueError, httpx.HTTPStatusError)):
            reader.get(URL)
    assert calls == ["/robots.txt", "/startup/42-crumbqueue"]


@pytest.mark.parametrize("content", [
    "You help purchasing teams plan warehouse deliveries.",
    "You help hikers choose safe weekend walking routes.",
])
def test_local_reader_keeps_public_content_and_blocks_private_redirects(index, monkeypatch, content):
    from app.core.local_secrets import update_settings

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    update_settings("search", {"provider": "builtin"}, {})
    monkeypatch.setattr("core.ssrf.socket.getaddrinfo", lambda *args, **kwargs: [
        (0, 0, 0, "", ("93.184.216.34", 0))])
    calls = []
    redirect = False
    original = httpx.Client

    def respond(request):
        calls.append(request)
        assert request.headers["Host"] == httpx.URL(ROOT).host
        assert request.url.host == "93.184.216.34"
        if request.url.path == "/robots.txt":
            body = "User-agent: *\nAllow: /"
        elif redirect:
            return httpx.Response(302, headers={"Location": "http://127.0.0.1/private"},
                stream=httpx.ByteStream(b""))
        else:
            body = f"<html><body><p>{content}</p></body></html>"
        return httpx.Response(200, headers={"content-type": "text/html"},
            stream=httpx.ByteStream(body.encode()))

    monkeypatch.setattr(index.httpx, "Client", lambda **kwargs:
        original(transport=httpx.MockTransport(respond), **kwargs))
    with index.Reader() as reader:
        assert content in reader.get(URL).text
        before = len(calls)
        with pytest.raises(ValueError):
            reader.get("https://unrelated.example/startup/42-example")
        assert len(calls) == before
        redirect = True
        with pytest.raises(ValueError, match="public"):
            reader.get(URL)
    assert all(request.headers["Host"] == httpx.URL(ROOT).host for request in calls)


def test_refresh_is_conditional_and_persists_no_body(index, monkeypatch):
    requests = []
    class Reader:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def get(self, url, **kwargs):
            requests.append((url, kwargs))
            if url.endswith("sitemap.xml"):
                return SimpleNamespace(text=f"<urlset><url><loc>{URL}</loc></url></urlset>")
            return httpx.Response(304 if kwargs.get("etag") else 200, text=FIXTURE.read_text(),
                                  headers={"ETag": '"v1"'})
    monkeypatch.setattr(index, "Reader", Reader)
    assert index.refresh(limit=1)["updated"] == 1
    assert index.refresh(limit=1)["unchanged"] == 1
    assert requests[-1][1]["etag"] == '"v1"'
    rows = index.entries()
    assert len(rows) == 1 and "text" not in rows[0]
    assert "rebuild plans" not in json.dumps(rows) and "Too few shops" not in json.dumps(rows)


def test_weekly_index_job_is_registered_and_deduplicated(index):
    from app.tasks.scheduler import dispatch_tick
    from app.worker.catalog import enabled_kinds
    from app.worker import handlers
    from app.state import db
    from app.db.models import Job
    from sqlalchemy import select
    handlers.install_default_handlers()
    assert "knowledge.loot_drop_refresh" in enabled_kinds() and "knowledge.loot_drop_refresh" in handlers.HANDLERS
    moment = datetime(2026, 10, 11, tzinfo=timezone.utc)
    first = dispatch_tick("weekly", now=moment)
    second = dispatch_tick("weekly", now=moment)
    assert first["ids"] == second["ids"]
    with db()() as session:
        jobs = session.scalars(select(Job).where(Job.kind == "knowledge.loot_drop_refresh")).all()
        assert len(jobs) == 1 and jobs[0].workspace_id is None
    from app.worker.main import _job_scope
    assert _job_scope(SimpleNamespace(kind=jobs[0].kind, workspace_id=None, venture_id=None, payload={})).actor == "system"


def test_migration_creates_idempotently_and_refuses_data_loss(index, monkeypatch):
    path = Path(index.__file__).parents[2] / "db/migrations/versions/s046lootdrop.py"
    spec = importlib.util.spec_from_file_location("loot_drop_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        migration.upgrade()
        assert "loot_drop_entries" in inspect(connection).get_table_names()
        connection.execute(index.LootDropEntry.__table__.insert().values(url=URL, name="CrumbQueue",
            description="Short internal description", checked_at=datetime(2026, 10, 11)))
        with pytest.raises(RuntimeError, match="prevents_downgrade"):
            migration.downgrade()
