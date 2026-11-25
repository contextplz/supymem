"""No live providers: actual pipeline, parser, HTTP guard and packaged imports."""
from __future__ import annotations

import importlib
import os
import shutil
import socket
import sysconfig
import time
from pathlib import Path

import httpx
import pytest


@pytest.fixture(scope="module")
def modules():
    from tests.company_os_harness import load_company_os_app
    load_company_os_app(reset=False)
    return tuple(importlib.import_module("app.gtm." + name) for name in ("lead_pipeline", "scrapy_leads", "openleads_bridge"))


def test_exact_target_region_count_and_no_unrelated_mix(modules, monkeypatch):
    pipeline, crawler, _ = modules
    queries, crawls = [], []
    monkeypatch.setattr(pipeline, "status", lambda: {"ready": True})
    def search(query, *, limit):
        queries.append((query, limit))
        return [{"url": "https://clinic.example/contact", "title": "Clinic", "snippet": "Dentists in Pune"}]
    def crawl(seeds, **kwargs):
        crawls.append((seeds, kwargs))
        return [{"email": "care@clinic.example", "email_tier": "unverified"}]
    monkeypatch.setattr(pipeline, "_search_web", search)
    monkeypatch.setattr(crawler, "crawl_sites_subprocess", crawl)
    result = pipeline.find_leads_combined(query="dentists", industry="dental", region="Pune", count=1)
    assert result["count"] == 1 and result["safe"] == 0 and result["unverified"] == 1
    assert all("dentists in Pune" in q and "dental" in q and limit <= 2 for q, limit in queries)
    assert len(queries) <= 2 and len(crawls[0][0]) == 1
    seed = crawls[0][0][0]
    assert seed["target_region"] == "Pune" and seed["target_terms"] == ["dentists", "dental"]
    assert "city" not in seed and "country" not in seed
    assert all(term not in " ".join(q for q, _ in queries) for term in ("SaaS", "Austin", "Berlin", "YC", "software"))


def test_case_insensitive_location_and_explicit_region_wins(modules):
    pipeline, _, _ = modules
    assert pipeline._target("dentists IN Pune", "", None)[1:] == ("Pune", ["dentists"])
    assert pipeline._target("dentists in Pune", "", "Delhi") == ("dentists in Delhi", "Delhi", ["dentists"])


def test_default_b2b_does_not_exclude_a_relevant_rehearsal_studio(modules):
    pipeline, crawler, _ = modules
    _, region, terms = pipeline._target("Find 3 rehearsal studio companies in Bangalore", "B2B", "Bangalore")
    assert terms == ["rehearsal", "studio"]
    assert crawler._matches("Music rehearsal studios in Bangalore. Contact our booking team.",
                            {"target_terms": terms, "target_region": region})
    assert not crawler._matches("Accounting studio in Bangalore", {"target_terms": terms, "target_region": region})
    assert pipeline._target("B2B rehearsal studios", "B2B", None)[2] == ["rehearsal", "studios"]


def test_failed_search_is_bounded_honest_and_does_not_fallback(modules, monkeypatch):
    pipeline, crawler, _ = modules
    calls = []
    monkeypatch.setattr(pipeline, "status", lambda: {"ready": True})
    def fail(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("SECRET_TOKEN must never be returned")
    monkeypatch.setattr(pipeline, "_search_web", fail)
    monkeypatch.setattr(crawler, "crawl_sites_subprocess", lambda *a, **k: pytest.fail("no seeds to crawl"))
    result = pipeline.find_leads_combined(query="dentists in Pune", count=3)
    assert len(calls) == 1 and result["state"] == "unavailable"
    assert result["count"] == 0 and result["shortfall"] == 3
    assert result["errors"] == ["search_unavailable"] and "SECRET" not in str(result)
    assert "could not finish" in result["message"]
    assert "no additional supported matches" not in result["message"]


def test_explicit_unavailable_sources_do_not_call_global_providers(modules, monkeypatch):
    pipeline, _, ol = modules
    monkeypatch.setattr(ol.subprocess, "run", lambda *a, **kw: pytest.fail("must not execute CLI"))
    for source in ("yc", "local", "opengtm", "unknown"):
        with pytest.raises(RuntimeError):
            pipeline.find_leads_combined(query="dentists in Pune", source=source)
    assert ol.status()["ready"] is False
    with pytest.raises(RuntimeError):
        ol.find_leads(query="dentists in Pune")
    assert b"care@clinic.example" in ol.leads_to_csv_bytes([{"email": "care@clinic.example"}])


def test_verified_only_does_not_spend_search_work(modules, monkeypatch):
    pipeline, _, _ = modules
    monkeypatch.setattr(pipeline, "status", lambda: {"ready": True})
    monkeypatch.setattr(pipeline, "_search_web", lambda *a, **k: pytest.fail("verification not configured"))
    with pytest.raises(RuntimeError, match="verification"):
        pipeline.find_leads_combined(query="dentists", verified_only=True)


def test_keyless_provider_selected_despite_global_paid_configuration(modules, monkeypatch):
    pipeline, _, _ = modules
    monkeypatch.setenv("OPENWORKER_SEARCH_PROVIDER", "tavily")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "not-used")
    called = []
    class Provider:
        def search(self, query, max_results):
            called.append((query, max_results))
            return []
    def factory(name):
        assert name == "duckduckgo"
        return Provider()
    monkeypatch.setattr(pipeline, "build_provider", factory)
    assert pipeline._search_web("dentists Pune", limit=99) == []
    assert called == [("dentists Pune", 10)]


def test_readiness_does_not_require_vendor_cli_or_network(modules, monkeypatch):
    pipeline, crawler, ol = modules
    monkeypatch.setattr(ol.subprocess, "run", lambda *a, **k: pytest.fail("status must not execute programs"))
    monkeypatch.setattr(crawler, "_fetch", lambda *a, **k: pytest.fail("status must not fetch"))
    result = pipeline.status()
    assert result["ready"] and not result["network_verified"]
    assert result["search"]["paid"] is False and result["openleads"]["ready"] is False


@pytest.mark.parametrize("url", ["http://127.0.0.1", "http://169.254.169.254", "file:///etc/passwd", "https://u:p@public.example", "https://public.example:22", "http://internal", "https://a.local", "https://a\\@public.example", "https://[::1]"])
def test_seed_syntax_rejects_private_and_credential_targets(modules, url):
    assert modules[1]._seed_url(url) == ""


def test_published_contacts_have_no_invented_identity_or_confidence(modules):
    crawler = modules[1]
    html = '''<html><body>Dentists in Pune <a href="mailto:care@clinic.example">Contact</a>
    <p>Jane Doe — Founder</p><a href="https://linkedin.com/in/someone">Profile</a>
    hello@unrelated.example care@evilclinic.example care@branch.clinic.example
    <script>fake@clinic.example</script><!-- hidden@clinic.example --></body></html>'''
    rows = crawler._extract(html, host="clinic.example", org="clinic.example", industry="dental", base="https://clinic.example/contact")
    assert {r["email"] for r in rows} == {"care@clinic.example", "care@branch.clinic.example"}
    assert all(not r["first_name"] and not r["linkedin_url"] and not r["confidence_pct"] and r["email_tier"] == "unverified" for r in rows)
    assert all(r["source_url"].endswith("/contact") and r["evidence"]["kind"] == "published_company_contact" for r in rows)


def test_target_requires_actual_company_page_evidence(modules, monkeypatch):
    crawler = modules[1]
    seed = {"website": "https://clinic.example", "target_terms": ["dentists"], "target_region": "Pune"}
    monkeypatch.setattr(crawler, "_fetch", lambda *a, **k: "<body>Software companies in Berlin care@clinic.example</body>")
    assert crawler._crawl_one(seed, 1, time.monotonic() + 2) == []
    monkeypatch.setattr(crawler, "_fetch", lambda *a, **k: "<body>Dentists in Pune care@clinic.example</body>")
    rows = crawler._crawl_one(seed, 1, time.monotonic() + 2)
    assert len(rows) == 1 and rows[0]["city"] == ""  # requested city is not an address assertion


def test_seed_iteration_and_page_count_are_bounded(modules, monkeypatch):
    crawler = modules[1]
    def seeds():
        for i in range(crawler.MAX_SITES):
            yield {"website": f"https://clinic{i}.example"}
        pytest.fail("unbounded seed consumption")
    counts = []
    monkeypatch.setattr(crawler, "_crawl_one", lambda seed, pages, deadline: counts.append(pages) or [])
    assert crawler.crawl_sites(seeds(), max_per_site=1) == []
    assert counts == [1] * crawler.MAX_SITES


def guarded_http(monkeypatch, handler, addresses):
    from core.research_web import guard
    def resolve(host, *args, **kwargs):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses(host)]
    monkeypatch.setattr(guard.socket, "getaddrinfo", resolve)
    original = httpx.Client
    def client(**kwargs):
        assert kwargs["trust_env"] is False and kwargs["follow_redirects"] is False
        return original(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(httpx, "Client", client)


def test_guard_pins_dns_and_revalidates_redirect_before_any_private_request(modules, monkeypatch):
    crawler = modules[1]
    requests = []
    def handler(request):
        requests.append(request)
        assert request.url.host == "93.184.216.34"
        assert request.headers["host"] == "clinic.example"
        assert request.extensions["sni_hostname"] == "clinic.example"
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})
    guarded_http(monkeypatch, handler, lambda host: ["93.184.216.34"])
    assert crawler._fetch("https://clinic.example") is None
    assert len(requests) == 1


def test_dns_mixed_private_answer_is_rejected_without_request(modules, monkeypatch):
    guarded_http(monkeypatch, lambda r: pytest.fail("private DNS answer"), lambda host: ["93.184.216.34", "10.0.0.1"])
    assert modules[1]._fetch("https://clinic.example") is None


def test_safe_fetch_body_cap_and_relative_redirect(modules, monkeypatch):
    crawler = modules[1]
    paths = []
    def handler(request):
        paths.append(request.url.path)
        if request.url.path == "/":
            return httpx.Response(302, headers={"location": "/contact"})
        return httpx.Response(200, headers={"content-type": "text/html"}, stream=httpx.ByteStream(b"<body>Contact</body>"))
    guarded_http(monkeypatch, handler, lambda host: ["93.184.216.34"])
    assert crawler._fetch("https://clinic.example/") == "<body>Contact</body>"
    assert paths == ["/", "/contact"]
    monkeypatch.setattr(crawler, "MAX_BYTES", 4)
    assert crawler._fetch("https://clinic.example/contact") is None


def test_no_unpinned_empty_dns_fallback(modules, monkeypatch):
    guarded_http(monkeypatch, lambda r: pytest.fail("unvetted resolution"), lambda host: [])
    assert modules[1]._fetch("https://clinic.example") is None


def test_crawler_process_entry_without_network_or_credentials(modules, monkeypatch):
    crawler = modules[1]
    assert crawler.crawl_sites_subprocess([], timeout_sec=10) == []
    original = crawler.subprocess.run
    def inspect_env(*args, **kwargs):
        assert "AWS_SECRET_ACCESS_KEY" not in kwargs["env"]
        assert "LLM_API_KEY" not in kwargs["env"]
        assert kwargs["timeout"] <= crawler.MAX_SECONDS
        return original(*args, **kwargs)
    monkeypatch.setenv("LLM_API_KEY", "must-not-inherit")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-inherit")
    monkeypatch.setattr(crawler.subprocess, "run", inspect_env)
    assert crawler.crawl_sites_subprocess([], timeout_sec=10) == []


def test_crawler_child_imports_guard_from_container_firstparty_path(modules, monkeypatch, tmp_path):
    """Docker installs core outside the venv; editable .pth files must not mask it."""
    crawler = modules[1]
    service = tmp_path / "workspace/services/company_os"
    firstparty = tmp_path / "workspace/firstparty"
    module = service / "app/gtm/scrapy_leads.py"
    module.parent.mkdir(parents=True)
    (service / "app/__init__.py").touch()
    (module.parent / "__init__.py").touch()
    shutil.copyfile(crawler.__file__, module)
    shutil.copytree(Path(__file__).resolve().parents[3] / "packages/core", firstparty / "core",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    monkeypatch.setattr(crawler, "__file__", str(module))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(firstparty), str(service), str(firstparty)]))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "must-not-inherit")
    original = crawler.subprocess.run
    captured = []
    def container_child(command, **kwargs):
        # -S disables local editable installs. Only external dependencies live in
        # the venv, while the real copied core guard lives in firstparty.
        kwargs["env"]["PYTHONPATH"] += os.pathsep + sysconfig.get_path("purelib")
        captured.append(kwargs["env"].copy())
        result = original([command[0], "-S", *command[1:]], **kwargs)
        assert result.returncode == 0, result.stderr
        return result
    monkeypatch.setattr(crawler.subprocess, "run", container_child)
    assert crawler.crawl_sites_subprocess([], timeout_sec=10) == []
    paths = captured[0]["PYTHONPATH"].split(os.pathsep)
    assert paths[0] == str(service) and paths.count(str(firstparty)) == 1
    assert "AWS_SECRET_ACCESS_KEY" not in captured[0]
