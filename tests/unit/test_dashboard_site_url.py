from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.api.dashboard import _prefer_openable_url, _public_site_url


def test_public_site_url():
    assert _public_site_url("wsp_abc", 2) == "http://127.0.0.1:3300/w/wsp_abc/site/?v=2"


def test_local_site_urls_never_default_to_the_hosted_gateway(monkeypatch):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("CORE_PORT", "3300")
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    for workspace_id in ("inventory", "gardens"):
        assert _public_site_url(workspace_id, 2) == f"http://127.0.0.1:3300/w/{workspace_id}/site/?v=2"


def test_rewrite_file_and_dead_ports():
    assert _prefer_openable_url("file:///tmp/x.html", "wsp_1", 1) == (
        "http://127.0.0.1:3300/w/wsp_1/site/?v=1"
    )
    assert _prefer_openable_url("http://127.0.0.1:4173", "wsp_1", None) == (
        "http://127.0.0.1:3300/w/wsp_1/site/"
    )
    assert _prefer_openable_url("http://127.0.0.1:3300/w/wsp_1/site/", "wsp_1", None) == (
        "http://127.0.0.1:3300/w/wsp_1/site/"
    )
