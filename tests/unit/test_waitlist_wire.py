from tests.company_os_harness import load_company_os_app
import pytest

load_company_os_app()

from app.firstlook.waitlist import public_interest_url, rewrite_waitlist_html


@pytest.fixture(autouse=True)
def hosted_waitlist(monkeypatch):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)


def test_local_forms_keep_tool_source_for_unrelated_products(monkeypatch):
    from contextlib import nullcontext
    from types import SimpleNamespace
    from app import state
    from app.core import local_secrets
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {}})
    monkeypatch.setattr(local_secrets, "get_secret", lambda section, field: None)
    monkeypatch.setattr(local_publish, "_reachable", lambda *args: False)
    assert public_interest_url("your-workspace").startswith("http://127.0.0.1:")
    for venture_id, name in (("inventory", "Inventory planning"), ("gardens", "Garden clubs")):
        session = SimpleNamespace(scalars=lambda query: [SimpleNamespace(id=venture_id, name=name)])
        monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(session))
        for provider in ("vercel", "netlify"):
            monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": provider, "waitlist_email": "you@your-site.test"})
            html = '<html><body><form action="https://gateway.supymem.com/w/your-workspace/interest" onsubmit="return false"><input name="email" type="email"><button>Join</button></form></body></html>'
            result = rewrite_waitlist_html(html, "your-workspace", venture_id=venture_id)
            assert result == html
            assert rewrite_waitlist_html(result, "your-workspace", venture_id=venture_id) == html
    def reject_private_source(files):
        raise local_publish.LocalPublishError("Your page contains a private credential. Remove it from your page and export again.")
    monkeypatch.setattr(local_publish, "_validate_files", reject_private_source)
    with pytest.raises(local_publish.LocalPublishError, match="private credential"):
        rewrite_waitlist_html(html, "your-workspace", venture_id=venture_id)


def test_rewrite_waitlist_replaces_localhost_and_relative_actions(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    html = """<!DOCTYPE html><html><body>
<form id="interest-form" action="http://127.0.0.1:3300/w/wsp_x/interest">
<input name="email" type="email"/>
<button>Join the waitlist</button>
</form>
<form action="/w/wsp_x/interest"><input name="email"/></form>
</body></html>"""
    out = rewrite_waitlist_html(html, "wsp_x")
    assert "127.0.0.1:3300" not in out
    assert out.count("https://gateway.supymem.com/w/wsp_x/interest") >= 2
    assert 'data-supymem-waitlist="1"' in out
    assert "form-thanks" in out
    assert "XMLHttpRequest" in out
    assert "application/json" in out
    assert "clone.submit" not in out
    assert "origFetch" not in out
    assert "Join the waitlist" in out


def test_rewrite_waitlist_is_idempotent():
    html = "<!DOCTYPE html><html><body><form id='interest-form' action='/w/wsp_y/interest'></form></body></html>"
    once = rewrite_waitlist_html(html, "wsp_y")
    twice = rewrite_waitlist_html(once, "wsp_y")
    assert twice.count('data-supymem-waitlist="1"') == 1
    assert "origFetch" not in twice


def test_rewrite_replaces_stale_boot_script(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    stale = """<!DOCTYPE html><html><body>
<form action="/w/wsp_y/interest"><input name="email"/></form>
<script data-supymem-waitlist="1">var origFetch = window.fetch; clone.submit();</script>
</body></html>"""
    out = rewrite_waitlist_html(stale, "wsp_y")
    assert out.count('data-supymem-waitlist="1"') == 1
    assert "origFetch" not in out
    assert "clone.submit" not in out


def test_native_submit_rewrites_to_real_form_navigation(monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setenv("PUBLIC_GATEWAY_URL", "https://gateway.supymem.com")
    html = """<!DOCTYPE html><html><body>
<form id="waitlist" action="/w/wsp_x/interest" onsubmit="event.preventDefault()">
<input name="email" type="email"/>
<button>Join</button>
</form>
</body></html>"""
    out = rewrite_waitlist_html(html, "wsp_x", native_submit=True)
    assert "https://gateway.supymem.com/w/wsp_x/interest" in out
    assert "HTMLFormElement.prototype.submit" in out
    assert "XMLHttpRequest" not in out
    assert "Thanks" not in out


def test_public_interest_url_is_loopback_in_pytest():
    url = public_interest_url("wsp_z")
    assert url.endswith("/w/wsp_z/interest")
    assert "127.0.0.1" in url
