from __future__ import annotations

import hashlib
import json
import zipfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from botocore.exceptions import ClientError
from core.artifact_snapshot import freeze_source, manifest_digest
from core.hosting_contracts import host_mapping, public_manifest
from core.public_copy import public_copy_observation, public_html_text

from tests.coding_tool_fixtures import installed_test_tool
from tests.company_os_harness import load_company_os_app
from tests.integration.test_postgres_auth_security import service_modules
from tests.unit.test_local_mode import _owner_client, local_owner_app as local_owner_app


def _signup_fixture(name, endpoint, *, netlify_name=""):
    from html import escape
    from app.conversion.waitlist import VERSION, consent_text

    consent = consent_text(name)
    digest = hashlib.sha256(consent.encode()).hexdigest()
    destination = urlsplit(endpoint)
    origin = "'self'" if netlify_name else "mailto:" if destination.scheme == "mailto" else destination.scheme + "://" + destination.netloc
    attributes = f' data-netlify="true" name="{netlify_name}"' if netlify_name else ''
    fields = f'<input type="hidden" name="form-name" value="{netlify_name}">' if netlify_name else ''
    return ('<!doctype html><html><head><meta http-equiv="Content-Security-Policy" '
        f'content="default-src \'none\'; base-uri \'none\'; script-src \'none\'; form-action {origin}"></head><body>'
        f'<form method="post" action="{escape(endpoint, quote=True)}"{attributes}'
        + (' enctype="text/plain"' if destination.scheme == "mailto" else '') + '>'
        '<label>Email<input name="email" type="email" maxlength="254" required></label>'
        f'<input type="hidden" name="consent_version" value="{VERSION}">'
        f'<input type="hidden" name="consent_digest" value="{digest}">'
        '<input name="website" pattern="^$" hidden>'
        f'<label><input type="checkbox" name="consent" value="yes" required>{escape(consent)}</label>'
        + fields + '<button>Join</button></form></body></html>')


@pytest.mark.parametrize("domain", ["maple-ledger.test", "winter-picnic.test"])
def test_export_get_never_requests_a_coding_order_for_a_new_destination(monkeypatch, domain):
    load_company_os_app()
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: ("workspace", "owner"))
    source = b'<html><body><a href="waitlist.html">Join</a></body></html>'
    original = f"mailto:old@{domain}"
    files = [local_publish._file("index.html", source, "text/html; charset=utf-8"),
             local_publish._file("waitlist.html", _signup_fixture(domain, original).encode(), "text/html; charset=utf-8")]
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: {
        "mode": "mailto", "url": f"mailto:new@{domain}", "notice": "Open Website to read your signups."})
    monkeypatch.setattr(local_publish, "_request_public_bundle", lambda *args, **kwargs: pytest.fail("A download must not start a build"))
    monkeypatch.setattr(local_publish, "_load", lambda venture_id, job_id, variant, caller, **kwargs:
        local_publish.prepare_files(files, venture_id=venture_id, name=domain,
            waitlist_url=f"https://{domain}/waitlist", caller=caller,
            local_signup={"mode": "mailto", "url": original, "notice": "Your saved signup destination."},
            pagebuilder_candidate={"job_id": job_id, "variant": variant}, **kwargs))
    response = local_publish.export_current("venture", "job", 0, "owner")
    with zipfile.ZipFile(BytesIO(response.body)) as archive:
        assert archive.read("waitlist.html") == files[1]["body"]


@pytest.mark.parametrize("domain,provider", [("maple-ledger.test", "vercel"), ("winter-picnic.test", "netlify")])
def test_interrupted_deploy_keeps_the_accepted_provider_id(monkeypatch, domain, provider):
    load_company_os_app()
    from app.conversion import waitlist
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_publish, "is_sample_workspace", lambda *_: False)
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: ("workspace", "owner"))
    monkeypatch.setattr(local_publish, "enabled", lambda *_: True)
    monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": provider, "project": "landing", "token": "test-token"})
    monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: None)
    files = [local_publish._file("index.html", f"<html><h1>{domain}</h1></html>".encode(), "text/html; charset=utf-8")]
    monkeypatch.setattr(local_publish, "_load", lambda *args, **kwargs: (files, "Signup notice"))
    saved, requests = {}, []
    monkeypatch.setattr(local_publish, "_save_publication", lambda venture, workspace, receipt: saved.update(receipt))
    monkeypatch.setattr(local_publish, "_publication", lambda *_: dict(saved))
    monkeypatch.setattr(local_publish.time, "sleep", lambda *_: None)
    def verify(url, file_hash):
        assert (url, file_hash) == (f"https://{domain}/", files[0]["sha256"])

    monkeypatch.setattr(local_publish, "_verify_public", verify)

    def request(method, url, **kwargs):
        requests.append((method, url))
        if method == "POST":
            return {"id": "accepted-deployment"}
        if len(requests) == 2:
            raise local_publish.LocalPublishError("Poll interrupted")
        assert url.endswith("/accepted-deployment")
        return {"id": "accepted-deployment", "readyState": "READY", "url": domain,
                "state": "ready", "ssl_url": f"https://{domain}"}

    monkeypatch.setattr(local_publish, "_request", request)
    with pytest.raises(local_publish.LocalPublishError, match="Check public URL"):
        local_publish.publish_site("venture", "owner", job_id="job")
    assert saved["deployment_id"] == "accepted-deployment" and saved["state"] == "verification_pending"
    with pytest.raises(local_publish.LocalPublishError, match="Check public URL"):
        local_publish.publish_site("venture", "owner", job_id="job")
    assert local_publish.verify_current("venture", "owner")["url"] == f"https://{domain}/"
    assert [method for method, _ in requests] == ["POST", "GET", "GET"]


@pytest.mark.parametrize("domain", ["maple-ledger.test", "winter-picnic.test"])
def test_publish_status_requires_a_saved_signup_destination(monkeypatch, domain):
    from contextlib import nullcontext
    load_company_os_app()
    from app import state
    from app.conversion import waitlist
    from app.core import local_mode
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_mode, "is_sample_mode", lambda: False)
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(None))
    monkeypatch.setattr(waitlist, "_venture", lambda *_: SimpleNamespace(name=domain))
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: ("workspace", "owner"))
    monkeypatch.setattr(local_publish, "_publication", lambda *_: None)
    monkeypatch.setattr(local_publish, "enabled", lambda *_: True)
    settings = {"provider": "vercel", "project": "landing", "token": "test-token"}
    monkeypatch.setattr(local_publish, "_settings", lambda: settings)
    status = local_publish.local_status("venture", "owner")
    assert not status["ready"] and "Waitlist email" in status["next_step"]
    settings["waitlist_email"] = f"join@{domain}"
    assert local_publish.local_status("venture", "owner")["ready"]


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("entry", ["preview", "export"])
def test_local_source_changes_use_your_coding_tool(monkeypatch, name, connected, entry):
    from contextlib import nullcontext
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app import state
    from app.api import firstlook_api
    from app.build import device_orders
    from app.core import local_capabilities, local_mode
    from app import coding_agents_consent
    from app.pagebuilder import api, local_publish, pipeline
    from desktop_runtime import agent_status

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_mode, "is_test_mode", lambda: True)
    monkeypatch.setattr(local_publish, "is_sample_workspace", lambda workspace: False)
    monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
    monkeypatch.setattr(local_capabilities, "enabled", lambda capability: True)
    monkeypatch.setattr(local_capabilities, "capability", lambda capability: {"state": "ready"})
    monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace", "owner"))
    monkeypatch.setattr(firstlook_api, "_assert_site_writer", lambda *args: None)
    venture = SimpleNamespace(id="venture_one", workspace_id="workspace", name=name, archived_at=None, stage="idea")
    monkeypatch.setattr(state, "db", lambda: lambda: nullcontext(SimpleNamespace(get=lambda *args: venture)))
    inputs = {"venture_id": venture.id, "name": name, "idea": name + " for your community",
        "waitlist_url": "https://old.example/public/waitlists/venture_one"}
    monkeypatch.setattr(api, "snapshot", lambda *args: ("revision", inputs))
    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": connected})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": connected,
        "signed_in": connected, "available": connected, "subscription_login": connected}])
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: {
        "mode": "mailto", "url": "mailto:you@example.test", "notice": "Open Website to read your signups."})
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    monkeypatch.setattr(pipeline, "fallback_html", lambda *args: pytest.fail("No internal preview generation"))
    monkeypatch.setattr(local_publish, "signup_page", lambda *args: pytest.fail("No internal signup generation"))
    orders = []
    def prepare(venture_id, caller, instruction, **kwargs):
        orders.append((venture_id, caller.sub, instruction, kwargs))
        return {"id": "order_one", "state": "pending", "review_digest": "review"}
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare)
    monkeypatch.setattr(device_orders, "transition", lambda *args: {"id": "order_one", "state": "approved"})
    if entry == "preview":
        if connected:
            result = local_publish.build_test_preview(venture.id, "owner")
            assert result["kind"] == "device_work_order" and "saved" not in result["body_md"]
        else:
            result = local_publish.build_test_preview(venture.id, "owner")
            assert result["kind"] == "needs_you" and "Settings > Keys and connections > Coding tools" in result["body_md"]
    else:
        source = f'<html><body><h1>{name}</h1><a href="{inputs["waitlist_url"]}">Join</a></body></html>'
        with pytest.raises(local_publish.LocalPublishError,
                match="Open Website" if connected else "Settings > Keys and connections > Coding tools"):
            local_publish.prepare_files([local_publish._file("index.html", source.encode(), "text/html; charset=utf-8")],
                venture_id=venture.id, name=name, waitlist_url=inputs["waitlist_url"], deployed=True, caller="owner",
                pagebuilder_candidate={"job_id": "job_one", "variant": 0})
    assert len(orders) == int(connected)
    if connected:
        assert orders[0][:2] == (venture.id, "owner")
        assert name in orders[0][2] and "consent" in orders[0][2]
        assert "codex" in orders[0][2].lower()
        if entry == "export":
            assert orders[0][3]["pagebuilder_candidate"] == {"job_id": "job_one", "variant": 0}
        def reject_credentials(files):
            raise local_publish.LocalPublishError("Your page contains a private credential. Remove it from your page and export again.")
        monkeypatch.setattr(local_publish, "_validate_files", reject_credentials)
        result = local_publish.build_test_preview(venture.id, "owner")
        assert result["kind"] == "needs_you" and "private credential" in result["body_md"]
        assert len(orders) == 1


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
def test_local_export_keeps_checked_tool_bytes(monkeypatch, name):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.conversion.waitlist import VERSION, consent_text
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    destination = {"mode": "mailto", "url": "mailto:you@example.test", "notice": "Open Website to import consented signups."}
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: destination)
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    monkeypatch.setattr(local_publish, "signup_page", lambda *args: pytest.fail("No generated companion page"))
    consent = consent_text(name)
    digest = hashlib.sha256(consent.encode()).hexdigest()
    policy = '<head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; base-uri \'none\'; script-src \'self\'; form-action mailto:"></head>'
    source = (f'<!doctype html><html>{policy}<body><h1>{name}</h1><a href="waitlist.html">Join</a>'
        '<script src="assets/page.js"></script></body></html>').encode()
    form = (f'<html>{policy}<body><form action="{destination["url"]}" method="post" enctype="text/plain">'
        '<label>Email<input name="email" type="email" required></label>'
        f'<input type="hidden" name="consent_version" value="{VERSION}">'
        f'<input type="hidden" name="consent_digest" value="{digest}">'
        '<input name="website" pattern="^$" hidden>'
        f'<label><input type="checkbox" name="consent" value="yes" required>{consent}</label>'
        '<button>Join</button></form></body></html>').encode()
    files = [local_publish._file("index.html", source, "text/html; charset=utf-8"),
        local_publish._file("waitlist.html", form, "text/html; charset=utf-8"),
        local_publish._file("assets/page.js", b'console.log("ready");', "application/javascript")]
    prepared, notice = local_publish.prepare_files(files, venture_id="venture_one", name=name,
        waitlist_url="https://old.example/public/waitlists/venture_one", deployed=True)
    assert prepared == files and notice == destination["notice"]
    for invalid_form in (form.replace(b'name="consent_digest"', b'name="old_digest"'),
            form.replace(b'pattern="^$"', b'pattern=".*"'), form.replace(b'form-action mailto:', b'form-action *'),
            form.replace(b'<form ', b'<section ').replace(b'</form>', b'</section>'),
            form[:form.index(b'<form')] + b'<form></form></body></html>'):
        changed = [files[0], local_publish._file("waitlist.html", invalid_form, "text/html; charset=utf-8"), files[2]]
        with pytest.raises(local_publish.LocalPublishError, match="Coding tools"):
            local_publish.prepare_files(changed, venture_id="venture_one", name=name,
                waitlist_url="https://old.example/public/waitlists/venture_one", deployed=True)


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
def test_single_html_export_keeps_tool_bytes(monkeypatch, name):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import local_publish

    source = f'<html><head><link rel="stylesheet" href="page.css"></head><body><h1>{name}</h1><a href="waitlist.html">Join</a></body></html>'.encode()
    files = [local_publish._file("index.html", source, "text/html; charset=utf-8"),
        local_publish._file("page.css", b"body { color: black; }", "text/css; charset=utf-8"),
        local_publish._file("waitlist.html", b"<html><body><form></form></body></html>", "text/html; charset=utf-8")]
    monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace", "owner"))
    monkeypatch.setattr(local_publish, "_preview_files", lambda *args, **kwargs: (files, "Your page is private."))
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    response = local_publish.export_current_html("venture_one", variant=0, caller="owner")
    assert response.body == source and "Export ZIP from Website" in response.headers["X-Page-Notice"]


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
def test_unreachable_measurement_names_your_publishing_setting(monkeypatch, name):
    import base64
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import local_publish
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    endpoint = "https://your-site.test/e"
    script = 'navigator.sendBeacon("' + endpoint + '", "pageview");'
    digest = base64.b64encode(hashlib.sha256(script.encode()).digest()).decode()
    source = _signup_fixture(name, "mailto:you@example.test").replace("script-src 'none'",
        "script-src 'sha256-" + digest + "'; connect-src " + endpoint).replace('</body>',
        '<script data-supymem-analytics="1">' + script + '</script></body>')
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: {
        "mode": "mailto", "url": "mailto:you@example.test", "notice": "Open Website."})
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
    monkeypatch.setattr(local_publish, "_events_reachable", lambda url: False)
    monkeypatch.setattr(beacon, "public_events_url", lambda: endpoint)
    with pytest.raises(local_publish.LocalPublishError, match="Settings > Keys and connections > Publishing"):
        local_publish.prepare_files([local_publish._file("index.html", source.encode(), "text/html; charset=utf-8")],
            venture_id="venture_one", name=name, waitlist_url="https://old.test/public/waitlists/venture_one", deployed=True)


@pytest.mark.parametrize("name", ["Fleet inspection scheduling", "Family trail planning"])
def test_local_firstlook_publication_uses_frozen_tool_html(monkeypatch, name):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.firstlook import compose, service, versions
    from app.conversion import waitlist
    from app.hosting import firstlook_publish
    from app.pagebuilder import local_publish

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    body = f'<!doctype html><html><body><h1>{name}</h1><a href="waitlist.html">Join</a></body></html>'.encode()
    saved = SimpleNamespace(id="version_one", venture_id="venture_one", version=1, template="local_builder",
        sha256=hashlib.sha256(body).hexdigest(), copy={"_render_snapshot": {"html": body.decode()}})
    monkeypatch.setattr(service, "_workspace_and_idea", lambda venture: ("workspace", name, name))
    monkeypatch.setattr(versions, "get_latest", lambda *args, **kwargs: saved)
    monkeypatch.setattr(compose, "compose_html", lambda **kwargs: pytest.fail("No internal recomposition"))
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: {
        "mode": "mailto", "url": "mailto:you@example.test", "notice": "Open Website."})
    monkeypatch.setattr(waitlist, "public_url", lambda venture: "http://127.0.0.1:3300/public/waitlists/" + venture)
    page = firstlook_publish.public_page("venture_one", version_id="version_one")
    assert page["body"] == body and page["sha256"] == saved.sha256
    saved.sha256 = "0" * 64
    with pytest.raises(firstlook_publish.HostingError):
        firstlook_publish.public_page("venture_one", version_id="version_one")
    saved.sha256 = hashlib.sha256(body).hexdigest()
    saved.copy["_render_snapshot"]["files"] = []
    with pytest.raises(firstlook_publish.HostingError, match="Your data folder and backup"):
        firstlook_publish.public_page("venture_one", version_id="version_one")


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
@pytest.mark.parametrize("script_state", ["current", "missing", "changed"])
def test_local_publication_checks_exact_measurement_without_changing_bytes(monkeypatch, name, script_state):
    import base64
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import local_publish
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    endpoint = "https://your-site.test/e"
    monkeypatch.setattr(beacon, "public_events_url", lambda: endpoint)
    monkeypatch.setattr(beacon, "_app_for_venture", lambda venture: ("app_owned", "pk_app_fixture"))
    monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
    monkeypatch.setattr(local_publish, "_events_reachable", lambda url: True)
    monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: {
        "mode": "mailto", "url": "mailto:you@example.test", "notice": "Open Website."})
    script = beacon.script_tag(key="pk_app_fixture", endpoint=endpoint).strip()
    if script_state == "missing":
        script = ""
    elif script_state == "changed":
        script = script.replace("pk_app_fixture", "pk_app_another")
    source = _signup_fixture(name, "mailto:you@example.test")
    if script:
        digest = base64.b64encode(hashlib.sha256(script.partition(">")[2].removesuffix("</script>").encode()).digest()).decode()
        source = source.replace("script-src 'none'", f"script-src 'sha256-{digest}'; connect-src {endpoint}")
        source = source.replace("</body>", script + "</body>")
    files = [local_publish._file("index.html", source.encode(), "text/html; charset=utf-8")]
    options = {"venture_id": "venture_owned", "name": name,
        "waitlist_url": "https://old.test/public/waitlists/venture_owned", "deployed": True}
    if script_state == "current":
        assert local_publish.prepare_files(files, **options)[0] == files
    else:
        with pytest.raises(local_publish.LocalPublishError, match="Settings > Keys and connections > Coding tools"):
            local_publish.prepare_files(files, **options)


@pytest.mark.parametrize("domain", ["wholesale", "gardening"])
def test_page_routes_follow_local_mode_when_reusing_app(monkeypatch, tmp_path, domain):
    from tests.company_os_harness import load_company_os_app

    monkeypatch.setenv("COS_TEST_APP_REUSE", "1")
    for index, mode in enumerate(("hosted", "local", "hosted", "local")):
        monkeypatch.setenv("SUPYMEM_MODE", mode)
        monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path / f"{domain}-{index}"))
        main = load_company_os_app(reset=False)
        paths = main.create_app().openapi()["paths"]
        for path in ("/workspaces/{workspace_id}/site/export", "/ventures/{venture_id}/site/local"):
            assert (path in paths) == (mode == "local")


@pytest.mark.parametrize("idea", ["Warehouse maintenance planning", "Family camping reservations"])
@pytest.mark.parametrize("known_model", [False, True])
def test_local_http_page_build_quote_accepts_your_model_without_a_hosted_price_cap(local_owner_app, monkeypatch, idea, known_model):
    from app import coding_agents_consent
    from app.core.local_owner import ensure_owner
    from app.core.local_secrets import update_settings
    from app.core.providers_local import save_settings
    from app.credits import estimate
    from app.db.models import Venture
    from app.llm import founder_provider
    from app.pagebuilder import api
    from app.state import db
    from tests.unit.pagebuilder.test_api import seed
    from desktop_runtime import agent_status

    monkeypatch.setattr(coding_agents_consent, "consents", lambda actor: {"codex": True})
    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "available": True, "subscription_login": True}])
    update_settings("capabilities", {"landing_pages": True}, {})
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:9999/v1",
        api_key="owner-test-key", models={"chat": "owner-deployment", "planning": "owner-deployment"})
    monkeypatch.setattr(founder_provider, "resolved_provider_identity", lambda config: "owner-model")
    monkeypatch.setattr(estimate, "_lookup_model_row", lambda model: {"input": 1, "output": 1} if known_model else None)
    monkeypatch.setattr(estimate, "estimate_llm_micro", lambda *args, **kwargs: api.MAX_PAGE_MICRO + 1)
    with _owner_client(local_owner_app) as client:
        data = seed(db(), actor=ensure_owner().id)
        with db()() as session:
            session.get(Venture, data["vid"]).raw_idea = idea
            session.commit()
        response = client.post(f'/ventures/{data["vid"]}/pagebuilder/estimate', json={"variants": 1})
        assert response.status_code == 200, response.text
        assert response.json()["ready"] and response.json()["reason"] is None


@pytest.mark.parametrize("idea,audience", [("Equipment inspection scheduling", "property managers"),
                                         ("Shared garden planning", "families sharing gardens")])
@pytest.mark.parametrize("connected", [False, True])
def test_local_export_without_a_checked_page_names_the_actual_blocker(local_owner_app, monkeypatch, tmp_path, idea, audience, connected):
    from app import coding_agents_consent
    from app.core import local_capabilities
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture
    from app.state import db
    from desktop_runtime import agent_status
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setattr(agent_status, "statuses", lambda: [{"name": "codex", "installed": True,
        "signed_in": True, "subscription_login": True, "available": True}])
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        actor = ensure_owner().id
        data = seed(db(), actor=actor)
        with db()() as session:
            venture = session.get(Venture, data["vid"])
            venture.name, venture.raw_idea = idea, idea + " for " + audience
            session.commit()
        binary = tmp_path / "codex"
        binary.touch(mode=0o700)
        with installed_test_tool(monkeypatch, "codex", binary):
            coding_agents_consent.set_consent(actor, "codex", connected)
        assert local_capabilities.enabled("landing_pages") is connected, audience
        for path in (f'/ventures/{data["vid"]}/site/local/export.zip',
                     f'/ventures/{data["vid"]}/site/local/export.html',
                     f'/workspaces/{data["wid"]}/site/export?venture_id={data["vid"]}'):
            response = client.get(path)
            assert response.status_code == (404 if "/workspaces/" in path else 409), response.text
            message = response.json()["detail"]
            if connected:
                assert "No page has passed its checks yet" in message
                assert "Website" in message and "Build activity" in message
                assert "build or fix" in message and "checks pass" in message
                assert "connect" not in message.lower()
            else:
                assert message == local_capabilities.blocked_message("landing_pages")
                assert "Settings > Keys and connections > Coding tools" in message


@pytest.mark.parametrize("idea,audience", [("Equipment inspection scheduling", "property managers"),
                                         ("Shared garden planning", "families sharing gardens")])
def test_local_http_export_reads_a_completed_builder_page_without_a_firstlook(local_owner_app, monkeypatch, tmp_path, idea, audience):
    import base64
    from app.core.local_owner import ensure_owner
    from app.db.models import Job, Venture
    from app.pagebuilder import api, local_publish, native_reference
    from app.state import db
    from core.clock import utcnow
    from tests.company_os_harness import append_idea_revision
    from tests.unit.pagebuilder.test_api import seed

    monkeypatch.setattr(local_publish, "_reachable", lambda *args: False)
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        data = seed(db(), actor=ensure_owner().id)
        with db()() as session:
            venture = session.get(Venture, data["vid"])
            venture.name, venture.raw_idea = audience, idea
            append_idea_revision(session, data["rid"], summary=idea, structured=json.dumps({"audience": audience}))
            session.commit()
            revision, inputs = api.snapshot(session, data["vid"], data["wid"])
        root = tmp_path / "page"
        root.mkdir()
        (root / "index.html").write_text(f'<html><head><title>{idea}</title></head><body><h1>{audience}</h1>'
            '<a href="#waitlist">Join</a><section id="waitlist"><p>Preview only. This page does not collect signups.</p></section></body></html>')
        manifest, archive = freeze_source(root)
        capture = {"manifest": manifest, "sha256": hashlib.sha256(archive).hexdigest(),
            "size_bytes": len(archive), "manifest_digest": manifest_digest(manifest)}
        doc = {"revision_id": revision, "_runtime": {"input": inputs}, "result": {"bundles": [
            {"source_zip": base64.b64encode(archive).decode(), "manifest": {"artifact_capture": capture}}]}}
        monkeypatch.setattr(native_reference, "_completed_candidate", lambda session, row: json.loads(row.payload_json))
        with db()() as session:
            session.add(Job(id="job-private-page", workspace_id=data["wid"], venture_id=data["vid"], kind=api.KIND,
                status="succeeded", payload_json=json.dumps(doc), created_at=utcnow()))
            session.commit()
        path = f'/workspaces/{data["wid"]}/site/export?venture_id={data["vid"]}'
        response = client.get(path)
        assert response.status_code == 200, response.text
        assert audience in response.text and "Preview only" in response.text
        assert response.text == (root / "index.html").read_text()
        assert "<form" not in response.text
        assert "local app" in response.headers["X-Page-Notice"] and "Keep your app running" in response.headers["X-Page-Notice"]
        assert "Experiments" in response.headers["X-Page-Notice"] and "Website" in response.headers["X-Page-Notice"]
        assert response.headers["content-disposition"] == 'attachment; filename="landing.html"'
        assert client.get(f'/workspaces/foreign/site/export?venture_id={data["vid"]}').status_code == 404
        with db()() as session:
            row = session.get(Job, "job-private-page")
            row.payload_json = json.dumps({**doc, "revision_id": "old-revision"})
            session.commit()
        assert client.get(path).status_code == 409



@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("code", ["hosting_provider_unavailable", "hosting_provider_configuration_invalid"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_concept_provider_errors_follow_your_capability(monkeypatch, name, code, ready):
    with service_modules("company_os"):
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        next_step = f"Open Settings > Keys and connections > Publishing and connect your account for {name}."
        monkeypatch.setattr(local_publish, "enabled", lambda capability: ready if capability == "publish_pages" else False)
        monkeypatch.setattr(local_publish, "blocked_message", lambda capability: next_step)
        message = firstlook_publish.plain(code)
        if ready:
            assert "Open Website" in message and "publish" in message
        else:
            assert message == next_step
        assert "Supymem hosting" not in message and "\u2014" not in message


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider,email,step", [
    ("vercel", "", "waitlist email"),
    ("s3", "you@your-site.test", "rebuild"),
    ("netlify", "", "rebuild"),
])
def test_local_concept_signup_error_names_your_saved_next_step(monkeypatch, name, provider, email, step):
    with service_modules("company_os"):
        from app.core import local_secrets
        from app.hosting import firstlook_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        local_secrets.update_settings("publishing", {"provider": provider, "waitlist_email": email, "project": name}, {})
        message = firstlook_publish.plain("hosting_conversion_link_required")
        assert step in message and "Open " in message
        assert "Publishing" in message if not email and provider != "netlify" else "Website" in message
        assert "\u2014" not in message


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("mode", ["setup", "mailto", "netlify"])
def test_local_concept_readiness_uses_your_settings_not_managed_hosting(monkeypatch, name, ready, mode):
    with service_modules("company_os"):
        from app.firstlook import service
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        page = {"site_version_id": "site_one", "version": 1, "sha256": "a" * 64,
            "headline": name, "subhead": "Your next step", "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one",
            "workspace_id": "workspace_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", lambda venture_id: page)
        monkeypatch.setattr(service, "_workspace_and_idea", lambda venture_id: ("workspace_one", name, name))
        def forbidden(*args, **kwargs):
            raise AssertionError("Local readiness must not require managed hosting or a hosted submission receipt")
        monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", forbidden)
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        monkeypatch.setattr(firstlook_publish, "db", forbidden)
        monkeypatch.setattr(local_publish, "enabled", lambda capability: ready if capability == "publish_pages" else False)
        next_step = f"Open Settings > Keys and connections > Publishing and connect your account for {name}."
        monkeypatch.setattr(local_publish, "blocked_message", lambda capability: next_step)
        signup_step = "Open Settings > Keys and connections > Publishing and save your waitlist email."
        monkeypatch.setattr(local_publish, "waitlist_destination", lambda venture_id, venture_name: {"mode": mode, "notice": signup_step})
        monkeypatch.setattr(local_publish, "_publication", lambda venture_id, workspace_id: None)
        state = firstlook_publish.describe("venture_one")
        assert state["state"] == ("publishable" if ready and mode != "setup" else "blocked")
        messages = [blocker["message"] for blocker in state["blockers"]]
        assert (signup_step in messages) == (mode == "setup")
        assert (next_step in messages) == (not ready)


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("missing_draft", [False, True])
def test_local_concept_status_keeps_unverified_uploads_blocked(monkeypatch, name, missing_draft):
    with service_modules("company_os"):
        from app.firstlook import service
        from app.hosting import firstlook_publish
        from app.hosting.service import HostingError
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        def page(venture_id):
            if missing_draft:
                raise HostingError("hosting_firstlook_draft_required")
            return {"site_version_id": "site_one", "version": 1, "sha256": "a" * 64,
                "headline": name, "subhead": "Your next step", "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one",
                "workspace_id": "workspace_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", page)
        monkeypatch.setattr(service, "_workspace_and_idea", lambda venture_id: ("workspace_one", name, name))
        def forbidden(*args, **kwargs):
            raise AssertionError("Local status must not read managed hosting")
        monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", forbidden)
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        monkeypatch.setattr(firstlook_publish, "db", forbidden)
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args: {"mode": "netlify", "notice": "Open Netlify > Forms."})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: {
            "state": "verification_pending", "url": "https://your-site.test", "sha256": "a" * 64})
        state = firstlook_publish.describe("venture_one")
        assert state["state"] == "blocked" and state["live_url"] is None
        uncertainty = next(blocker for blocker in state["blockers"] if blocker["code"] == "hosting_public_state_uncertain")
        assert "Website" in uncertainty["message"] and "Check public URL" in uncertainty["message"]


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
def test_local_concept_api_publication_uses_the_saved_publisher(monkeypatch, name):
    with service_modules("company_os"):
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        def forbidden(*args, **kwargs):
            raise AssertionError("Local publication must not configure managed hosting")
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        calls = []
        def publish(venture_id, actor, **kwargs):
            calls.append((venture_id, actor, kwargs))
            return {"state": "live", "url": "https://your-site.test", "notice": name}
        monkeypatch.setattr(local_publish, "publish_site", publish)
        result = firstlook_publish.queue_publication("venture_one", actor="owner_one", site_version_id="site_one", public_sha256="a" * 64)
        assert calls == [("venture_one", "owner_one", {"site_version_id": "site_one", "public_sha256": "a" * 64})]
        assert result["state"] == "live" and result["job_id"] is None


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("changed", [False, True])
def test_local_concept_publication_keeps_the_exact_source_check(monkeypatch, name, changed):
    with service_modules("company_os"):
        from app.conversion import waitlist
        from app.hosting import firstlook_publish
        from app.hosting.service import HostingError
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace_one", "owner_one"))
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: None)
        body = f"<h1>{name}</h1>".encode()
        digest = hashlib.sha256(body).hexdigest()
        def page(venture_id, *, version_id):
            assert (venture_id, version_id) == ("venture_one", "site_one")
            return {"site_version_id": version_id, "workspace_id": "workspace_one", "name": name,
                "body": body, "sha256": digest, "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", page)
        files = [local_publish._file("index.html", body, "text/html; charset=utf-8")]
        def prepare(source, **kwargs):
            assert source == files and kwargs["name"] == name and kwargs["deployed"]
            return files, "Open Website to review your signups."
        monkeypatch.setattr(local_publish, "prepare_files", prepare)
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": "netlify", "project": "your-site"})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: None)
        uploads, saved = [], []
        monkeypatch.setattr(local_publish, "deploy", lambda source, provider, **kwargs:
            uploads.append((source, provider)) or "https://your-site.test")
        monkeypatch.setattr(local_publish, "_save_publication", lambda venture_id, workspace_id, receipt: saved.append(receipt))
        if changed:
            with pytest.raises(HostingError, match="hosting_firstlook_draft_changed"):
                local_publish.publish_site("venture_one", "owner_one", site_version_id="site_one", public_sha256="b" * 64)
            assert not uploads and not saved
        else:
            result = local_publish.publish_site("venture_one", "owner_one", site_version_id="site_one", public_sha256=digest)
            assert result["state"] == "live" and uploads == [(files, "netlify")]
            assert saved[0]["site_version_id"] == "site_one" and saved[0]["public_sha256"] == digest


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("state", ["publishing", "verification_pending"])
def test_local_publication_retry_never_duplicates_an_unconfirmed_upload(monkeypatch, name, state):
    with service_modules("company_os"):
        from app.conversion import waitlist
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace_one", "owner_one"))
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: None)
        body = f"<h1>{name}</h1>".encode()
        files = [local_publish._file("index.html", body, "text/html; charset=utf-8")]
        monkeypatch.setattr(local_publish, "_load", lambda *args, **kwargs: (files, "Your consented signups stay saved."))
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": "netlify", "project": "your-site"})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: {"state": state, "url": "https://your-site.test"})
        def forbidden(*args, **kwargs):
            raise AssertionError("Reconcile the existing upload before starting another")
        monkeypatch.setattr(local_publish, "deploy", forbidden)
        with pytest.raises(local_publish.LocalPublishError, match="Website") as error:
            local_publish.publish_site("venture_one", "owner_one", job_id="job_one")
        assert ("Check public URL" in str(error.value)) == (state == "verification_pending")


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("code", ["hosting_provider_unavailable", "hosting_provider_configuration_invalid"])
@pytest.mark.parametrize("ready", [False, True])
def test_local_concept_provider_errors_follow_your_capability(monkeypatch, name, code, ready):
    with service_modules("company_os"):
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        next_step = f"Open Settings > Keys and connections > Publishing and connect your account for {name}."
        monkeypatch.setattr(local_publish, "enabled", lambda capability: ready if capability == "publish_pages" else False)
        monkeypatch.setattr(local_publish, "blocked_message", lambda capability: next_step)
        message = firstlook_publish.plain(code)
        if ready:
            assert "Open Website" in message and "publish" in message
        else:
            assert message == next_step
        assert "Supymem hosting" not in message and "\u2014" not in message


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider,email,step", [
    ("vercel", "", "waitlist email"),
    ("s3", "you@your-site.test", "rebuild"),
    ("netlify", "", "rebuild"),
])
def test_local_concept_signup_error_names_your_saved_next_step(monkeypatch, name, provider, email, step):
    with service_modules("company_os"):
        from app.core import local_secrets
        from app.hosting import firstlook_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        local_secrets.update_settings("publishing", {"provider": provider, "waitlist_email": email, "project": name}, {})
        message = firstlook_publish.plain("hosting_conversion_link_required")
        assert step in message and "Open " in message
        assert "Publishing" in message if not email and provider != "netlify" else "Website" in message
        assert "\u2014" not in message


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("ready", [False, True])
@pytest.mark.parametrize("mode", ["setup", "mailto", "netlify"])
def test_local_concept_readiness_uses_your_settings_not_managed_hosting(monkeypatch, name, ready, mode):
    with service_modules("company_os"):
        from app.firstlook import service
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        page = {"site_version_id": "site_one", "version": 1, "sha256": "a" * 64,
            "headline": name, "subhead": "Your next step", "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one",
            "workspace_id": "workspace_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", lambda venture_id: page)
        monkeypatch.setattr(service, "_workspace_and_idea", lambda venture_id: ("workspace_one", name, name))
        def forbidden(*args, **kwargs):
            raise AssertionError("Local readiness must not require managed hosting or a hosted submission receipt")
        monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", forbidden)
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        monkeypatch.setattr(firstlook_publish, "db", forbidden)
        monkeypatch.setattr(local_publish, "enabled", lambda capability: ready if capability == "publish_pages" else False)
        next_step = f"Open Settings > Keys and connections > Publishing and connect your account for {name}."
        monkeypatch.setattr(local_publish, "blocked_message", lambda capability: next_step)
        signup_step = "Open Settings > Keys and connections > Publishing and save your waitlist email."
        monkeypatch.setattr(local_publish, "waitlist_destination", lambda venture_id, venture_name: {"mode": mode, "notice": signup_step})
        monkeypatch.setattr(local_publish, "_publication", lambda venture_id, workspace_id: None)
        state = firstlook_publish.describe("venture_one")
        assert state["state"] == ("publishable" if ready and mode != "setup" else "blocked")
        messages = [blocker["message"] for blocker in state["blockers"]]
        assert (signup_step in messages) == (mode == "setup")
        assert (next_step in messages) == (not ready)


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("missing_draft", [False, True])
def test_local_concept_status_keeps_unverified_uploads_blocked(monkeypatch, name, missing_draft):
    with service_modules("company_os"):
        from app.firstlook import service
        from app.hosting import firstlook_publish
        from app.hosting.service import HostingError
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        def page(venture_id):
            if missing_draft:
                raise HostingError("hosting_firstlook_draft_required")
            return {"site_version_id": "site_one", "version": 1, "sha256": "a" * 64,
                "headline": name, "subhead": "Your next step", "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one",
                "workspace_id": "workspace_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", page)
        monkeypatch.setattr(service, "_workspace_and_idea", lambda venture_id: ("workspace_one", name, name))
        def forbidden(*args, **kwargs):
            raise AssertionError("Local status must not read managed hosting")
        monkeypatch.setattr(firstlook_publish, "_waitlist_receipt", forbidden)
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        monkeypatch.setattr(firstlook_publish, "db", forbidden)
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args: {"mode": "netlify", "notice": "Open Netlify > Forms."})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: {
            "state": "verification_pending", "url": "https://your-site.test", "sha256": "a" * 64})
        state = firstlook_publish.describe("venture_one")
        assert state["state"] == "blocked" and state["live_url"] is None
        uncertainty = next(blocker for blocker in state["blockers"] if blocker["code"] == "hosting_public_state_uncertain")
        assert "Website" in uncertainty["message"] and "Check public URL" in uncertainty["message"]


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
def test_local_concept_api_publication_uses_the_saved_publisher(monkeypatch, name):
    with service_modules("company_os"):
        from app.hosting import firstlook_publish
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        def forbidden(*args, **kwargs):
            raise AssertionError("Local publication must not configure managed hosting")
        monkeypatch.setattr(firstlook_publish.HostingConfig, "from_environment", classmethod(forbidden))
        calls = []
        def publish(venture_id, actor, **kwargs):
            calls.append((venture_id, actor, kwargs))
            return {"state": "live", "url": "https://your-site.test", "notice": name}
        monkeypatch.setattr(local_publish, "publish_site", publish)
        result = firstlook_publish.queue_publication("venture_one", actor="owner_one", site_version_id="site_one", public_sha256="a" * 64)
        assert calls == [("venture_one", "owner_one", {"site_version_id": "site_one", "public_sha256": "a" * 64})]
        assert result["state"] == "live" and result["job_id"] is None


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("changed", [False, True])
def test_local_concept_publication_keeps_the_exact_source_check(monkeypatch, name, changed):
    with service_modules("company_os"):
        from app.conversion import waitlist
        from app.hosting import firstlook_publish
        from app.hosting.service import HostingError
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace_one", "owner_one"))
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: None)
        body = f"<h1>{name}</h1>".encode()
        digest = hashlib.sha256(body).hexdigest()
        def page(venture_id, *, version_id):
            assert (venture_id, version_id) == ("venture_one", "site_one")
            return {"site_version_id": version_id, "workspace_id": "workspace_one", "name": name,
                "body": body, "sha256": digest, "waitlist_url": "http://127.0.0.1:8080/public/waitlists/venture_one"}
        monkeypatch.setattr(firstlook_publish, "public_page", page)
        files = [local_publish._file("index.html", body, "text/html; charset=utf-8")]
        def prepare(source, **kwargs):
            assert source == files and kwargs["name"] == name and kwargs["deployed"]
            return files, "Open Website to review your signups."
        monkeypatch.setattr(local_publish, "prepare_files", prepare)
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": "netlify", "project": "your-site"})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: None)
        uploads, saved = [], []
        monkeypatch.setattr(local_publish, "deploy", lambda source, provider, **kwargs:
            uploads.append((source, provider)) or "https://your-site.test")
        monkeypatch.setattr(local_publish, "_save_publication", lambda venture_id, workspace_id, receipt: saved.append(receipt))
        if changed:
            with pytest.raises(HostingError, match="hosting_firstlook_draft_changed"):
                local_publish.publish_site("venture_one", "owner_one", site_version_id="site_one", public_sha256="b" * 64)
            assert not uploads and not saved
        else:
            result = local_publish.publish_site("venture_one", "owner_one", site_version_id="site_one", public_sha256=digest)
            assert result["state"] == "live" and uploads == [(files, "netlify")]
            assert saved[0]["site_version_id"] == "site_one" and saved[0]["public_sha256"] == digest


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("state", ["publishing", "verification_pending"])
def test_local_publication_retry_never_duplicates_an_unconfirmed_upload(monkeypatch, name, state):
    with service_modules("company_os"):
        from app.conversion import waitlist
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("workspace_one", "owner_one"))
        monkeypatch.setattr(local_publish, "enabled", lambda capability: True)
        monkeypatch.setattr(waitlist, "configure", lambda *args, **kwargs: None)
        body = f"<h1>{name}</h1>".encode()
        files = [local_publish._file("index.html", body, "text/html; charset=utf-8")]
        monkeypatch.setattr(local_publish, "_load", lambda *args, **kwargs: (files, "Your consented signups stay saved."))
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": "netlify", "project": "your-site"})
        monkeypatch.setattr(local_publish, "_publication", lambda *args: {"state": state, "url": "https://your-site.test"})
        def forbidden(*args, **kwargs):
            raise AssertionError("Reconcile the existing upload before starting another")
        monkeypatch.setattr(local_publish, "deploy", forbidden)
        with pytest.raises(local_publish.LocalPublishError, match="Website") as error:
            local_publish.publish_site("venture_one", "owner_one", job_id="job_one")
        assert ("Check public URL" in str(error.value)) == (state == "verification_pending")


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_saved_publishing_settings_dispatch_without_environment(monkeypatch, name, provider):
    with service_modules("company_os"):
        from app.core import local_secrets
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        values = {"provider": provider, "project": "your-site", "bucket": "your-bucket",
            "region": "us-east-1", "prefix": "pages/current", "site_url": "https://your-site.test"}
        private = {"token": "saved-token", "access_key_id": "saved-access",
            "secret_access_key": "saved-secret", "session_token": "saved-session"}
        local_secrets.update_settings("publishing", values, private)
        local_secrets.update_settings("capabilities", {"publish_pages": True}, {})
        body = f"<h1>{name}</h1>".encode()
        files = [local_publish._file("index.html", body, "text/html; charset=utf-8")]
        monkeypatch.setattr(local_publish, "_validate_files", lambda files: None)
        calls = []
        def request(method, url, **kwargs):
            assert not (urlsplit(url).hostname or "").endswith("supymem.com")
            calls.append((method, url, kwargs))
            return {"id": "your-deployment", "readyState": "READY", "state": "ready",
                "url": "your-site.test", "ssl_url": "https://your-site.test"}
        monkeypatch.setattr(local_publish, "_request", request)
        class S3:
            def put_object(self, **kwargs):
                calls.append(("put", "s3", kwargs))
            def head_object(self, **kwargs):
                return {"ContentLength": len(body), "Metadata": {"sha256": hashlib.sha256(body).hexdigest()}}
        def s3(service, **kwargs):
            assert kwargs["aws_session_token"] == "saved-session"
            assert kwargs["aws_access_key_id"] == "saved-access"
            return S3()
        monkeypatch.setattr(local_publish, "_s3_client", s3)
        monkeypatch.setattr(local_publish, "_verify_public", lambda url, digest: None)
        assert local_publish.deploy(files, provider) == "https://your-site.test/"
        assert calls
        if provider != "s3":
            assert calls[0][2]["headers"]["Authorization"] == "Bearer saved-token"


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider,reachable,mode", [("vercel", True, "endpoint"), ("netlify", False, "netlify"), ("s3", False, "mailto")])
def test_local_waitlist_destination_modes(monkeypatch, name, provider, reachable, mode):
    with service_modules("company_os"):
        from app.pagebuilder import local_publish
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": provider,
            "site_url": "https://your-site.test", "waitlist_email": "you@your-site.test"})
        monkeypatch.setattr(local_publish, "_reachable", lambda *args: reachable)
        destination = local_publish.waitlist_destination("venture_one", name)
        assert destination["mode"] == mode
        assert "supymem.com" not in destination["url"]


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_inline_signup_forms_submit_with_current_consent(monkeypatch, name, provider):
    with service_modules("company_os"):
        from app.conversion.waitlist import VERSION
        from app.pagebuilder import local_publish
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": provider,
            "site_url": "https://your-site.test", "waitlist_email": "you@your-site.test"})
        monkeypatch.setattr(local_publish, "_reachable", lambda *args: provider == "vercel")
        source = '<form method="get" action="https://gateway.supymem.com/public/waitlists/venture_one" onsubmit="return false"><input name="email" type="email"><button formmethod="get">Join</button></form>'
        with pytest.raises(local_publish.LocalPublishError, match="Coding tools"):
            local_publish.prepare_files([local_publish._file("index.html", source.encode(), "text/html; charset=utf-8")],
                venture_id="venture_one", name=name, waitlist_url="https://your-site.test/public/waitlists/venture_one", deployed=True)
        destination = local_publish.waitlist_destination("venture_one", name)
        source = _signup_fixture(name, destination["url"], netlify_name="waitlist-venture_one" if provider == "netlify" else "")
        files, _ = local_publish.prepare_files([local_publish._file("index.html", source.encode(), "text/html; charset=utf-8")],
            venture_id="venture_one", name=name, waitlist_url="https://your-site.test/public/waitlists/venture_one", deployed=True)
        html = next(item["body"].decode() for item in files if item["path"] == "index.html")
        assert 'method="post"' in html and 'formmethod=' not in html and 'onsubmit=' not in html
        assert 'name="consent" value="yes" required' in html
        assert f'name="consent_version" value="{VERSION}"' in html
        assert 'name="consent_digest"' in html and 'name="website"' in html
        assert "supymem.com" not in html
        assert html == source


@pytest.mark.parametrize("name", ["Inventory planning", "Garden clubs"])
@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_local_unpublish_uses_saved_provider_and_keeps_other_files(monkeypatch, name, provider):
    with service_modules("company_os"):
        from app.pagebuilder import local_publish
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        settings = {"provider": provider, "project": "your-site", "token": "saved-token", "bucket": "your-bucket",
            "region": "us-east-1", "prefix": "pages/current", "access_key_id": "saved-access", "secret_access_key": "saved-secret"}
        monkeypatch.setattr(local_publish, "_settings", lambda: settings)
        digest = hashlib.sha256(name.encode()).hexdigest()
        receipt = {**{key: settings[key] for key in ("provider", "project", "bucket", "region", "prefix")},
            "url": "https://your-site.test/", "deployment_id": "your-deployment", "sha256": digest,
            "files": [{"path": "index.html", "sha256": digest}]}
        calls = []
        def request(method, url, **kwargs):
            assert not (urlsplit(url).hostname or "").endswith("supymem.com")
            calls.append((method, url))
            return {"published_deploy": {"id": "your-deployment"}} if method == "GET" else {}
        monkeypatch.setattr(local_publish, "_request", request)
        class S3:
            def head_object(self, **kwargs):
                return {"Metadata": {"sha256": digest}, "ETag": "your-etag"}
            def delete_object(self, **kwargs):
                assert kwargs == {"Bucket": "your-bucket", "Key": "pages/current/index.html", "IfMatch": "your-etag"}
                calls.append(("DELETE", "s3"))
        monkeypatch.setattr(local_publish, "_s3_client", lambda *args, **kwargs: S3())
        monkeypatch.setattr(local_publish, "get_checked", lambda client, url, **kwargs: SimpleNamespace(status_code=404))
        local_publish.unpublish(receipt)
        assert calls
        assert receipt["files"] == [{"path": "index.html", "sha256": digest}]
        settings["project"] = "another-site"
        settings["prefix"] = "another-folder"
        calls.clear()
        with pytest.raises(local_publish.LocalPublishError, match="restore the account"):
            local_publish.unpublish(receipt)
        assert calls == []


@pytest.mark.parametrize("venture_id", ["inventory", "gardens"])
def test_charlie_publish_and_accepted_dispatch_never_use_managed_hosting(monkeypatch, venture_id):
    import asyncio
    with service_modules("company_os"):
        from app.api import pagebuilder_api
        from app.chat import site_actions
        from app.hosting import service
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "enabled", lambda name: True)
        monkeypatch.setattr(pagebuilder_api, "profile", lambda *args: {"current": {"state": "ready", "job_id": "your-job"}})
        called = []
        monkeypatch.setattr(local_publish, "publish_site", lambda vid, actor, **kwargs: called.append((vid, actor, kwargs)) or {"url": "https://your-site.test/", "notice": "Your signups go to your account."})
        reply = asyncio.run(site_actions.execute(venture_id, "site.publish", {}, "owner"))
        assert "https://your-site.test/" in reply["body_md"]
        assert called == [(venture_id, "owner", {"job_id": "your-job"})]
        assert not any(action.get("confirmation") for action in reply["actions"])
        monkeypatch.setattr(local_publish, "publish_accepted", lambda *args, **kwargs: {"state": "live", "url": "https://your-site.test/"})
        monkeypatch.setattr(service.HostingConfig, "from_environment", lambda: pytest.fail("Your local publish must not use managed hosting"))
        assert service.queue_accepted_build("your-app", "your-build", actor="owner")["state"] == "live"


@pytest.mark.parametrize("venture_id", ["equipment", "pottery"])
def test_local_founder_api_errors_preserve_status_and_name_the_setup_screen(monkeypatch, venture_id):
    from fastapi import HTTPException
    with service_modules("company_os"):
        from app.api import apps_api, waitlist_api
        from app.conversion.waitlist import WaitlistError
        from app.hosting import service

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(apps_api, "_app_or_404", lambda app_id: SimpleNamespace(venture_id=venture_id))
        monkeypatch.setattr(apps_api, "assert_venture_access", lambda vid, actor: None)
        monkeypatch.setattr(apps_api, "subject_person_id", lambda actor: actor)
        def reject(*args, **kwargs):
            raise service.HostingError("hosting_provider_unavailable", status=503)
        monkeypatch.setattr(service, "queue_accepted_build", reject)
        with pytest.raises(HTTPException) as publish_error:
            apps_api.publish_static_build("your-app", apps_api.PublishStaticBody(build_id="your-build"), "owner")
        assert publish_error.value.status_code == 503
        assert "Settings > Keys and connections > Publishing" in publish_error.value.detail
        next_step = "In Settings > Features, switch on Publish pages."
        def needs_capability(*args, **kwargs):
            raise service.HostingError(next_step)
        monkeypatch.setattr(service, "queue_accepted_build", needs_capability)
        with pytest.raises(HTTPException) as capability_error:
            apps_api.publish_static_build("your-app", apps_api.PublishStaticBody(build_id="your-build"), "owner")
        assert capability_error.value.detail == next_step
        with pytest.raises(HTTPException) as waitlist_error:
            waitlist_api.error(WaitlistError("waitlist_local_setup_required", 503))
        assert waitlist_error.value.status_code == 503
        assert "Website > Collect signups" in waitlist_error.value.detail
        assert "Settings > Keys and connections > Publishing" in waitlist_error.value.detail
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        with pytest.raises(HTTPException) as hosted_error:
            waitlist_api.error(WaitlistError("waitlist_not_found", 404))
        assert hosted_error.value.detail == {"code": "waitlist_not_found"}


@pytest.mark.parametrize("idea", ["Equipment inspection scheduling", "Pottery weekends for families"])
def test_local_builder_ingest_and_restore_keep_signup_scope_with_two_ventures(local_site_client, monkeypatch, idea):
    from app.core.local_owner import ensure_owner
    from app.db.models import Venture
    from app.firstlook import service
    from app.pagebuilder import local_publish
    from app.state import db
    from core.clock import utcnow
    from tests.unit.pagebuilder.test_api import seed

    assert local_site_client.post("/local/providers", json={"provider": "openai_compatible",
        "base_url": "http://127.0.0.1:11434/v1",
        "models": {"chat": "offline-test-model", "planning": "offline-test-model"}}).status_code == 200

    data = seed(db(), actor=ensure_owner().id)
    now = utcnow()
    with db()() as session:
        session.add(Venture(id="other-venture", workspace_id=data["wid"], name="Other page", raw_idea="A different saved idea",
            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.get(Venture, data["vid"]).name = idea
        session.commit()
    monkeypatch.setattr(local_publish, "_settings", lambda: {"provider": "netlify"})
    monkeypatch.setattr(local_publish, "_reachable", lambda *args: False)
    html = '<html><body><h1>' + idea + '</h1><form action="/w/' + data["wid"] + '/interest"><input name="email"><button>Join</button></form></body></html>'
    from app.firstlook.waitlist import rewrite_waitlist_html
    html = rewrite_waitlist_html(html, data["wid"], venture_id=data["vid"])
    result = service.ingest_builder_page(data["vid"], html)
    assert result["ok"] is True
    path = Path(result["path"])
    body = path.read_text()
    assert body == html
    path.write_text(html)
    restored = service.materialize_site_files({"workspace_id": data["wid"], "venture_id": data["vid"],
        "version": result["version"], "copy": {"page_html": html}, "path": str(path)})
    assert restored.read_text() == html

@pytest.mark.parametrize("domain,known_url", [("berth-planning.example", False), ("pottery-weekends.test", True)])
def test_take_offline_does_not_claim_success_with_pending_replacement(local_site_client, monkeypatch, domain, known_url):
    from app.core.local_owner import ensure_owner
    from app.pagebuilder import local_publish
    from app.state import db
    from tests.unit.pagebuilder.test_api import seed

    data = seed(db(), actor=ensure_owner().id)
    base = f"/ventures/{data['vid']}/site/local"
    first, replacement = (f"https://{name}.{domain}/" for name in ("first", "replacement"))
    settings = {"provider": "vercel", "project": "your-project", "token": "test-only"}
    monkeypatch.setattr(local_publish, "_settings", lambda: settings)
    for state, identity, url in [("live", "first", first), ("verification_pending", "replacement", replacement if known_url else None)]:
        local_publish._save_publication(data["vid"], data["wid"], {"state": state, "deployment_id": identity,
            "url": url, "provider": "vercel", "project": settings["project"], "sha256": "a" * 64})
    removed = set()
    polls = []

    def delete(method, url, **_kwargs):
        assert method == "DELETE"
        removed.add(url.rsplit("/", 1)[1])
        return {}

    monkeypatch.setattr(local_publish, "_request", delete)
    def poll(_provider, identity, _token):
        polls.append(identity)
        return replacement
    monkeypatch.setattr(local_publish, "_poll_deployment", poll)
    monkeypatch.setattr(local_publish, "get_checked", lambda _client, url, **_kwargs: SimpleNamespace(
        status_code=404 if url == first or url == replacement and allow_replacement else 200))
    allow_replacement = False
    incomplete = local_site_client.post(base + "/unpublish")
    assert incomplete.status_code == 409 and "hosting account" in incomplete.json()["detail"]
    assert removed == {"first", "replacement"}
    status = local_site_client.get(base).json()
    assert status["can_take_offline"] is True and status["publication"]["state"] != "offline"
    allow_replacement = True
    offline = local_site_client.post(base + "/unpublish")
    assert offline.json() == {"state": "offline", "url": first}
    assert polls == ([] if known_url else ["replacement"])
    status = local_site_client.get(base).json()
    assert status["verification_pending"] is False and status["can_take_offline"] is False
    assert status["publication"]["state"] == "offline"
    assert local_site_client.post(base + "/verify").status_code == 409


@pytest.mark.parametrize("idea,audience,domain", [("Equipment inspection scheduling", "property managers", "inspection.example"), ("Garden club membership", "home gardeners", "clubs.example")])
def test_local_publication_export_import_and_offline_over_http(local_site_client, monkeypatch, idea, audience, domain):
    import zipfile
    from app.core import local_secrets
    from app.pagebuilder import local_publish
    from app.conversion import waitlist
    from app.db.supymem_models import VentureWaitlistEntry
    from app.state import db
    from app.core.local_owner import ensure_owner
    from app.core.providers_local import save_settings
    from app.db.models import Venture
    from app.firstlook import service
    from tests.company_os_harness import append_idea_revision
    from tests.unit.pagebuilder.test_api import seed

    client = local_site_client
    save_settings(provider="openai_compatible", base_url="http://127.0.0.1:11434/v1", models={"chat": "publication-fixture"})
    data = seed(db(), actor=ensure_owner().id)
    with db()() as session:
        venture = session.get(Venture, data["vid"])
        venture.name, venture.raw_idea = audience, idea
        append_idea_revision(session, data["rid"], summary=idea, structured=json.dumps({"audience": audience}))
        session.commit()
    base = f"/ventures/{data['vid']}/site/local"
    settings = {"provider": "netlify", "project": "your-site", "site_url": "https://your-site.test"}
    private = {"token": None}
    local_secrets.update_settings("capabilities", {"publish_pages": True}, {})
    local_secrets.update_settings("publishing", settings, {})
    monkeypatch.setattr(local_publish, "_reachable", lambda *args: False)
    monkeypatch.setattr(local_publish, "_events_reachable", lambda *args: False)
    signup = _signup_fixture(audience, "/", netlify_name="waitlist-" + data["vid"])
    html = signup.partition("<body>")[0] + f'<body><h1>{idea}</h1><p>{audience}</p><a href="waitlist.html">Join</a></body></html>'
    from app.firstlook.waitlist import rewrite_waitlist_html
    html = rewrite_waitlist_html(html, data["wid"], venture_id=data["vid"])
    assert service.ingest_builder_page(data["vid"], html, {"waitlist.html": signup})["ok"] is True
    calls = []
    def request(method, url, **kwargs):
        assert not (urlsplit(url).hostname or "").endswith("supymem.com")
        calls.append((method, url))
        return {"id": "your-deployment", "state": "ready", "ssl_url": "https://your-site.test", "published_deploy": {"id": "your-deployment"}}
    monkeypatch.setattr(local_publish, "_request", request)
    monkeypatch.setattr(local_publish, "_verify_public", lambda *args: None)
    exported = client.get(base + "/export.zip")
    assert exported.status_code == 200, exported.text
    assert client.get(base).json()["ready"] is False
    single = client.get(base + "/export.html")
    assert single.status_code == 200, single.text
    assert single.text == html and 'href="waitlist.html"' in single.text
    assert "form-action 'none'" not in single.text
    with zipfile.ZipFile(BytesIO(exported.content)) as archive:
        assert 'data-netlify="true"' in archive.read("waitlist.html").decode()
        assert "supymem.com" not in archive.read("index.html").decode()
    private["token"] = "saved-token"
    local_secrets.update_settings("publishing", settings, {"token": private["token"]})
    response = client.post(base + "/publish", json={})
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "live"
    assert client.post(base + "/publish", json={}).status_code == 200
    assert sum(method == "POST" and url.endswith("/deploys") for method, url in calls) == 1
    status = client.get(base).json()
    assert status["publication"]["url"] == "https://your-site.test/"
    assert "saved-token" not in json.dumps(status)
    assert client.post(base + "/verify").status_code == 200
    from core.research_web import guard
    monkeypatch.setattr(guard, "get_checked", lambda *args, **kwargs: SimpleNamespace(status_code=200,
        text=f'<form data-netlify="true" name="waitlist-{data["vid"]}"></form>'))
    def post_test(client, url, **kwargs):
        assert url == "https://your-site.test/"
        assert kwargs["data"]["email"].endswith("@qa.invalid")
        return SimpleNamespace(status_code=303)
    class ProbeClient:
        def __init__(self, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        post = post_test
    monkeypatch.setattr(waitlist, "httpx", SimpleNamespace(Client=ProbeClient, HTTPError=waitlist.httpx.HTTPError))
    tested = client.post(f'/ventures/{data["vid"]}/waitlist/test')
    assert tested.status_code == 200, tested.text
    assert tested.json()["destination_tested"] is True
    assert tested.json()["submission_verified"] is False
    payload = {"emails": ["member@" + domain, "member@" + domain], "consent": True}
    response = client.post(base + "/signups", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["imported"] == 1
    assert client.post(base + "/signups", json=payload).json()["imported"] == 0
    with db()() as session:
        assert session.query(VentureWaitlistEntry).filter_by(venture_id=data["vid"], is_test=False).count() == 1
        entry = session.query(VentureWaitlistEntry).filter_by(venture_id=data["vid"], is_test=False).one()
        assert json.loads(entry.props)["source"] == "founder_import"
        from app.db.supymem_models import AppEvent
        assert session.query(AppEvent).filter_by(workspace_id=data["wid"], name="signup").count() == 0
    monkeypatch.setattr(local_publish, "get_checked", lambda *args, **kwargs: SimpleNamespace(status_code=404))
    assert client.post(base + "/unpublish").status_code == 200
    assert client.get(base).json()["publication"]["state"] == "offline"
    assert waitlist.status(data["vid"], data["wid"])["total"] == 1
    assert calls


@pytest.fixture
def local_site_client(local_owner_app, monkeypatch):
    from app.core import local_secrets
    from tests.unit.test_local_mode import _owner_client

    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post("/local/providers/sample", json={}).status_code == 200
        local_secrets.update_settings("capabilities", {"publish_pages": True}, {})
        yield client


@pytest.mark.parametrize("domain", ["inspection.example", "clubs.example"])
def test_manual_signups_stay_private_and_require_consent(local_site_client, domain):
    from app.core.local_owner import ensure_owner
    from app.conversion import waitlist
    from app.state import db
    from tests.unit.pagebuilder.test_api import seed

    data = seed(db(), actor=ensure_owner().id)
    path = f'/ventures/{data["vid"]}/site/local/signups'
    payload = {"emails": ["member@" + domain], "consent": True}
    response = local_site_client.post(path, json=payload)
    assert response.status_code == 200, response.text
    state = waitlist.status(data["vid"], data["wid"])
    assert state["total"] == 1 and state["enabled"] is False
    assert local_site_client.get(f'/public/waitlists/{data["vid"]}').status_code == 404
    payload["consent"] = False
    assert local_site_client.post(path, json=payload).status_code == 422
    assert waitlist.status(data["vid"], data["wid"])["total"] == 1


@pytest.fixture(autouse=True)
def _page_publication_test_environment(tmp_path_factory, monkeypatch):
    state = tmp_path_factory.mktemp("page-test-state")
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[3] / "services" / "company_os"))
    monkeypatch.setenv("COMPANY_OS_WORKING_MEMORY_PATH", str(state / "memory.db"))
    monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(state / "data"))


@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
def test_local_export_preserves_the_coding_tool_repair_step(monkeypatch, host):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.pagebuilder import local_publish
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    destination = {"url": f"https://{host}/signup", "notice": "", "mode": "endpoint"}
    monkeypatch.setattr(local_publish, "waitlist_destination", lambda *args, **kwargs: destination)
    monkeypatch.setattr(local_publish, "signup_page", lambda *args, **kwargs: "<html><body>Signup</body></html>")
    monkeypatch.setattr(local_publish, "_events_reachable", lambda endpoint: True)
    monkeypatch.setattr(beacon, "public_events_url", lambda: f"https://{host}/e")
    monkeypatch.setattr(beacon, "_app_for_venture", lambda venture_id: ("app_owned", "pk_app_fixture"))
    source = f"<html><body><h1>{host}</h1></body></html>".encode()
    files = [local_publish._file("index.html", source, "text/html; charset=utf-8")]
    with pytest.raises(local_publish.LocalPublishError, match="Settings > Keys and connections > Coding tools"):
        local_publish.prepare_files(
            files, venture_id="venture_owned", name=host,
            waitlist_url=f"https://{host}/signup", deployed=True)


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
@pytest.mark.parametrize("public", [False, True])
@pytest.mark.parametrize("source_base", ["https://signup.old.test", "http://127.0.0.1:8080"])
def test_local_static_export_preserves_consent_and_never_overwrites(tmp_path, monkeypatch, name, public, source_base):
    import zipfile

    with service_modules("company_os"):
        from app.conversion import waitlist
        from app.pagebuilder import local_publish
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_settings", lambda: {"waitlist_email": "owner@studio.test", "site_url": "https://signup.studio.test"})
        monkeypatch.setattr(local_publish, "_reachable", lambda *_args: public)
        monkeypatch.setattr(local_publish, "_events_reachable", lambda *_args: public)
        root = tmp_path / "source"
        root.mkdir()
        original = source_base + "/public/waitlists/venture_one"
        endpoint = "https://signup.studio.test/public/waitlists/venture_one" if public else "mailto:owner@studio.test"
        form = _signup_fixture(name, endpoint)
        (root / "index.html").write_text(form.partition("<body>")[0] + f'<body><h1>{name}</h1>'
            '<a href="waitlist.html">Join early access</a><a href="waitlist_north.html">Join this version</a></body></html>')
        (root / "waitlist.html").write_text(form)
        (root / "waitlist_north.html").write_text(form.replace('</form>',
            '<input type="hidden" name="variant" value="north"><input type="hidden" name="utm_source" value="shared"></form>'))
        expected = {path.name: path.read_bytes() for path in root.iterdir()}
        manifest, archive = freeze_source(root)
        capture = {"manifest": manifest, "sha256": hashlib.sha256(archive).hexdigest(),
            "size_bytes": len(archive), "manifest_digest": manifest_digest(manifest)}
        files, notice = local_publish.prepare(archive, capture, venture_id="venture_one", name=name,
            waitlist_url=original, deployed=True)
        contents = {item["path"]: item["body"].decode() for item in files}
        assert 'href="waitlist.html"' in contents["index.html"]
        assert {item["path"]: item["body"] for item in files} == expected
        assert "form-action" in contents["waitlist.html"]
        attributed = next(value for path, value in contents.items() if path.startswith("waitlist_") and path.endswith(".html"))
        assert 'name="variant" value="north"' in attributed
        assert 'name="utm_source" value="shared"' in attributed
        form = contents["waitlist.html"]
        assert waitlist.consent_text(name) in form
        assert 'name="consent" value="yes" required' in form
        assert 'name="website"' in form and 'name="consent_digest"' in form
        assert ('action="https://signup.studio.test/public/waitlists/venture_one"' in form) is public
        assert ('action="mailto:owner@studio.test"' in form) is not public
        assert ("email app" in notice) is not public
        folder = tmp_path / "export"
        local_publish.export_folder(files, folder)
        with zipfile.ZipFile(BytesIO(local_publish.export_zip(files))) as zipped:
            assert set(zipped.namelist()) == set(contents)
            assert zipped.read("waitlist.html") == (folder / "waitlist.html").read_bytes()
        with pytest.raises(local_publish.LocalPublishError, match="Choose a new folder"):
            local_publish.export_folder(files, folder)
        assert (folder / "index.html").read_text() == contents["index.html"]


@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
def test_local_deploy_uses_only_your_credentials(monkeypatch, name, provider):
    with service_modules("company_os"):
        from app.core.local_secrets import update_settings
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        settings = {"provider": provider, "token": "local-token", "project": "local-project",
            "access_key_id": "local-access", "secret_access_key": "local-secret",
            "region": "eu-west-1", "bucket": "my-site-bucket",
            "prefix": "pages/release-one", "site_url": "https://pages.studio.test"}
        private = {key: settings[key] for key in ("token", "access_key_id", "secret_access_key")}
        update_settings("publishing", {key: value for key, value in settings.items() if key not in private}, private)
        update_settings("capabilities", {"publish_pages": True}, {})
        calls = []
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return {"id": "deployment-one", "readyState": "READY" if len(calls) > 1 else "QUEUED",
                "state": "ready" if len(calls) > 1 else "processing",
                "url": "pages.studio.test", "ssl_url": "https://pages.studio.test"}
        monkeypatch.setattr(local_publish, "_request", request)
        monkeypatch.setattr(local_publish.time, "sleep", lambda seconds: None)
        class S3:
            def put_object(self, **kwargs):
                calls.append(("put", kwargs))
            def head_object(self, **kwargs):
                return {"ContentLength": len(body), "Metadata": {"sha256": hashlib.sha256(body).hexdigest()}}
        def client(service, **kwargs):
            assert service == "s3"
            assert kwargs["aws_access_key_id"] == settings["access_key_id"]
            assert kwargs["aws_secret_access_key"] == settings["secret_access_key"]
            return S3()
        monkeypatch.setattr(local_publish, "_s3_client", client)
        monkeypatch.setattr(local_publish, "get_checked", lambda client, url, **kwargs: SimpleNamespace(status_code=200, content=body))
        body = f"<h1>{name}</h1>".encode()
        files = [{"path": "index.html", "body": body, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "content_type": "text/html; charset=utf-8"}]
        result = local_publish.deploy(files, provider)
        assert result == "https://pages.studio.test/"
        assert "local-token" not in result and "local-secret" not in result
        assert calls
        if provider == "s3":
            assert calls[0][1]["Bucket"] == settings["bucket"]
            assert calls[0][1]["Key"] == "pages/release-one/index.html"
        else:
            assert calls[0][2]["headers"]["Authorization"] == "Bearer local-token"


def test_local_publish_is_disabled_in_hosted_mode_and_rejects_changed_source(tmp_path, monkeypatch):
    with service_modules("company_os"):
        from app.pagebuilder import local_publish

        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        with pytest.raises(local_publish.LocalPublishError, match="Run locally"):
            local_publish.export_zip([])
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        (tmp_path / "index.html").write_text("<h1>Reviewed page</h1>")
        manifest, archive = freeze_source(tmp_path)
        capture = {"manifest": manifest, "sha256": hashlib.sha256(archive).hexdigest(),
            "size_bytes": len(archive), "manifest_digest": manifest_digest(manifest)}
        with pytest.raises(ValueError, match="artifact_archive_mismatch"):
            local_publish.prepare(archive + b"changed", capture, venture_id="venture_one",
                name="Your project", waitlist_url="https://signup.old.test/public/waitlists/venture_one", deployed=False)


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
def test_local_routes_require_local_mode_and_return_the_signup_notice(monkeypatch, name):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    with service_modules("company_os"):
        from app.api import pagebuilder_api
        from app.pagebuilder import local_publish

        app = FastAPI()
        app.include_router(local_publish.router)
        client = TestClient(app)
        path = "/ventures/venture_one/pagebuilder/jobs/job_one/local"
        monkeypatch.delenv("SUPYMEM_MODE", raising=False)
        assert client.get(path + "/export.zip").status_code == 404
        assert client.post(path + "/deploy", json={"provider": "vercel"}).status_code == 404
        monkeypatch.setenv("SUPYMEM_MODE", "local")
        from tests.company_os_harness import company_os_test_client

        with company_os_test_client(app) as authenticated:
            client.headers.update(authenticated.headers)
        from app.admin import authn
        monkeypatch.setattr(authn, "verify_caller", lambda request: SimpleNamespace(sub="your-account"))
        calls = []
        body = f"<h1>{name}</h1>".encode()
        files = [{"path": "index.html", "body": body, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "content_type": "text/html; charset=utf-8"}]
        notice = "Your signup form opens the visitor's email app. Set your public address to store signups."
        monkeypatch.setattr(local_publish, "_load", lambda *args, **kwargs: (files, notice))
        monkeypatch.setattr(local_publish, "deploy", lambda files, provider: calls.append(provider) or "https://pages.studio.test/")
        monkeypatch.setattr(local_publish, "publish_site", lambda venture_id, caller, **kwargs: {"url": local_publish.deploy(files, kwargs["provider"]), "notice": notice})
        response = client.get(path + "/export.zip")
        assert response.status_code == 200 and response.headers["X-Page-Notice"] == notice
        assert calls == []
        response = client.post(path + "/deploy", json={"provider": "vercel"})
        assert response.status_code == 200 and response.json() == {"url": "https://pages.studio.test/", "notice": notice}
        assert calls == ["vercel"]


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
def test_local_export_keeps_waitlist_scope_and_enablement_checks(monkeypatch, name):
    from contextlib import nullcontext

    with service_modules("company_os"):
        from app import state
        from app.conversion import waitlist
        from app.pagebuilder import api, local_publish, native_reference
        from app.db.models import Job

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_site_scope", lambda *args: ("your-workspace", "your-account"))
        monkeypatch.setattr(native_reference, "_completed_candidate", lambda *args: {})
        monkeypatch.setattr(waitlist, "_venture", lambda *args: SimpleNamespace(name=name))
        for configured in (None, SimpleNamespace(workspace_id="another-workspace", enabled=True, consent_version=waitlist.VERSION),
                SimpleNamespace(workspace_id="your-workspace", enabled=False, consent_version=waitlist.VERSION)):
            job = SimpleNamespace(kind=api.KIND, venture_id="venture_one", workspace_id="your-workspace")
            session = SimpleNamespace(get=lambda model, identity, value=configured: job if model is Job else value)
            monkeypatch.setattr(state, "db", lambda current=session: lambda: nullcontext(current))
            with pytest.raises(local_publish.LocalPublishError, match="Turn on your waitlist"):
                local_publish._load("venture_one", "job_one", 0, SimpleNamespace(sub="your-account"), deployed=True)


@pytest.mark.parametrize("provider", ["vercel", "netlify", "s3"])
def test_local_deployment_refuses_missing_credentials_before_any_upload(monkeypatch, provider):
    with service_modules("company_os"):
        from app.core.local_secrets import update_settings
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        update_settings("capabilities", {"publish_pages": True}, {})
        update_settings("publishing", {"provider": provider}, {})
        monkeypatch.setattr(local_publish, "_settings", dict)
        monkeypatch.setattr(local_publish, "_request", lambda *args, **kwargs: pytest.fail("Missing credentials must not upload"))
        monkeypatch.setattr(local_publish, "_s3_client", lambda *args, **kwargs: pytest.fail("Missing credentials must not upload"))
        body = b"<h1>Your page</h1>"
        files = [{"path": "index.html", "body": body, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "content_type": "text/html; charset=utf-8"}]
        with pytest.raises(local_publish.LocalPublishError, match="Settings > Keys and connections > Your page publishing account"):
            local_publish.deploy(files, provider)


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
@pytest.mark.parametrize("matches", [False, True])
def test_local_public_checks_require_exact_consent_and_page_bytes(monkeypatch, name, matches):
    with service_modules("company_os"):
        from app.conversion.waitlist import consent_text
        from app.pagebuilder import local_publish

        consent = consent_text(name)
        digest = hashlib.sha256(consent.encode()).hexdigest()
        body = f"<h1>{name}</h1>".encode()
        response = SimpleNamespace(status_code=200, text=f'<input name="consent">{digest if matches else "outdated-consent"}',
            content=body if matches else b"previous deployment")
        calls = []
        def get(client, url, **kwargs):
            calls.append(kwargs)
            return response
        monkeypatch.setattr(local_publish, "get_checked", get)
        url = "https://pages.studio.test/"
        assert local_publish._reachable(url, consent) is matches
        if matches:
            local_publish._verify_public(url, hashlib.sha256(body).hexdigest())
        else:
            with pytest.raises(local_publish.LocalPublishError, match="Check public URL in Website"):
                local_publish._verify_public(url, hashlib.sha256(body).hexdigest())
        assert all(call == {"max_redirects": 0, "https_only": True} for call in calls)


@pytest.mark.parametrize("name", ["Warehouse scheduling", "Weekend pottery"])
def test_local_exports_never_include_local_credentials(monkeypatch, name):
    with service_modules("company_os"):
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setattr(local_publish, "_settings", lambda: {"token": "your-private-token"})
        body = f"<h1>{name}</h1><!-- your-private-token -->".encode()
        files = [{"path": "index.html", "body": body, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "content_type": "text/html; charset=utf-8"}]
        with pytest.raises(local_publish.LocalPublishError) as error:
            local_publish.export_zip(files)
        assert str(error.value) == "Your page contains a private credential. Remove it from your page and export again."
        assert "your-private-token" not in str(error.value)


@pytest.mark.parametrize("domain", ["inventory.example", "gardening.example"])
@pytest.mark.parametrize("source", ["process", "dotenv", "vault", "provider"])
@pytest.mark.parametrize("env_key", ["LLM_API_KEY", "FOUNDER_CREDENTIAL", "SESSION_SECRET", "RAZORPAY_KEY_ID"])
def test_all_export_paths_refuse_configured_secrets(tmp_path, monkeypatch, domain, source, env_key):
    with service_modules("company_os"):
        from app.connectors import secrets
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        monkeypatch.setenv("COMPANY_OS_EPHEMERAL_VAULT", "1")
        monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(tmp_path / ".env"))
        value = hashlib.sha256(f"{domain}:{source}:{env_key}".encode()).hexdigest()
        vault = secrets.LocalSecretStore()
        monkeypatch.setattr(secrets, "store", lambda: vault)
        if source == "process":
            monkeypatch.setenv(env_key, f" {value} ")
        elif source == "dotenv":
            monkeypatch.delenv(env_key, raising=False)
            (tmp_path / ".env").write_text(f"{env_key}={value}\n")
        elif source == "vault":
            vault.put(workspace_id=domain, provider="synthetic", key=env_key, value=value)
        else:
            from app.core.providers_local import save_settings

            monkeypatch.setenv("COMPANY_OS_DATA_DIR", str(tmp_path))
            for name in ("SUPYMEM_LLM_PROVIDER", "AZURE_OPENAI_API_KEY", "AZURE_OPENAI_ENDPOINT",
                         "OPENAI_COMPATIBLE_API_KEY", "OPENAI_COMPATIBLE_BASE_URL"):
                monkeypatch.delenv(name, raising=False)
            save_settings(provider="azure_openai", base_url="https://provider.example", api_key=value,
                models={"chat": "synthetic-chat"})
            save_settings(provider="openai_compatible", base_url="http://127.0.0.1:1234",
                api_key="synthetic-selected-provider-key", models={"chat": "synthetic-chat"})
        body = f"<h1>{domain}</h1><!-- {value} -->".encode()
        files = [{"path": "index.html", "body": body, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "content_type": "text/html; charset=utf-8"}]
        monkeypatch.setattr(local_publish, "_request", lambda *args, **kwargs: pytest.fail("Secrets must not upload"))
        monkeypatch.setattr(local_publish, "_s3_client", lambda *args, **kwargs: pytest.fail("Secrets must not upload"))
        destination = tmp_path / "export"
        operations = [lambda: local_publish.export_zip(files), lambda: local_publish.export_folder(files, destination)]
        operations.extend(lambda provider=provider: local_publish.deploy(files, provider) for provider in ("vercel", "netlify", "s3"))
        for operation in operations:
            with pytest.raises(local_publish.LocalPublishError) as rejected:
                operation()
            assert str(rejected.value) == "Your page contains a private credential. Remove it from your page and export again."
            assert value not in str(rejected.value)
            assert not destination.exists()
        if source == "vault":
            def unreadable():
                raise RuntimeError("synthetic unreadable credential storage")

            monkeypatch.setattr(secrets, "store", unreadable)
            with pytest.raises(local_publish.LocalPublishError) as rejected:
                local_publish.export_zip(files)
            assert str(rejected.value) == "Your credentials could not be checked. Restore your local credential storage and export again."


def _config(hosting=None):
    if hosting is None:
        from app.hosting.service import HostingConfig
    else:
        HostingConfig = hosting.HostingConfig

    return HostingConfig(
        bucket="selfhost-prod-sites",
        region="us-east-1",
        expected_owner="123456789012",
        kvs_arn="arn:aws:cloudfront::123456789012:key-value-store/kvs_test",
        public_domain="supymem.com",
    )


def _client_error(code: str):
    return ClientError({"Error": {"Code": code, "Message": code}}, "test")


def test_public_files_are_exactly_the_reviewed_snapshot(tmp_path: Path):
    from app.hosting.service import verified_public_files

    (tmp_path / "assets").mkdir()
    (tmp_path / "index.html").write_text("<h1>Reviewed</h1>")
    (tmp_path / "assets" / "site.css").write_text("h1{color:#53138a}")
    manifest, archive = freeze_source(tmp_path)
    capture = {
        "manifest": manifest,
        "manifest_digest": manifest_digest(manifest),
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size_bytes": len(archive),
    }

    files = verified_public_files(archive, capture, public_manifest(manifest))

    assert [(item["path"], item["body"]) for item in files] == [
        ("assets/site.css", b"h1{color:#53138a}"),
        ("index.html", b"<h1>Reviewed</h1>"),
    ]
    assert files[0]["content_type"] == "text/css; charset=utf-8"
    assert files[1]["content_type"] == "text/html; charset=utf-8"


def test_public_files_reject_a_changed_archive(tmp_path: Path):
    from app.hosting.service import verified_public_files

    (tmp_path / "index.html").write_text("reviewed")
    manifest, archive = freeze_source(tmp_path)
    capture = {
        "manifest": manifest,
        "manifest_digest": manifest_digest(manifest),
        "sha256": hashlib.sha256(archive).hexdigest(),
        "size_bytes": len(archive),
    }
    with pytest.raises(ValueError, match="artifact_archive_mismatch"):
        verified_public_files(archive + b"changed", capture, public_manifest(manifest))


def test_publication_rechecks_visible_copy_even_with_older_accepted_qa(tmp_path: Path):
    (tmp_path / "index.html").write_text(
        "<html><head><title>LabHire</title><script>const note='TODO';</script></head>"
        "<body><h1>LabHire</h1><footer>Verified contact details must be configured before publishing.</footer></body></html>"
    )
    manifest, archive = freeze_source(tmp_path)
    capture = {"manifest": manifest, "manifest_digest": manifest_digest(manifest),
               "sha256": hashlib.sha256(archive).hexdigest(), "size_bytes": len(archive)}
    with service_modules("company_os"):
        from app.hosting.service import HostingError, verified_public_files
        with pytest.raises(HostingError, match="hosting_public_copy_unreviewed"):
            verified_public_files(archive, capture, public_manifest(manifest))


def test_publication_copy_parser_ignores_nonpublic_code_and_hidden_text():
    html = ("<html><head><title>TODO</title><style>/* placeholder text */</style></head>"
            "<body><!-- draft do not publish --><script>const text='TODO'</script>"
            "<section hidden>Replace this placeholder text</section><h1>Publish your reading list</h1></body></html>")
    assert public_copy_observation(public_html_text(html))[0] == []


@pytest.mark.parametrize("html, secondary_html, expected, finding_path", [
    ("<html><body><h1>LabHire</h1><footer>Verified contact details must be configured before publishing.</footer></body></html>",
     None, "needs_review", "index.html"),
    ("<html><body><h1>LabHire</h1><a href='/waitlist'>Join the waitlist</a></body></html>",
     None, "clear", None),
    ("<html><body><h1>LabHire</h1><a href='offer.html'>Read offer</a></body></html>",
     "<html><body><h1>Offer</h1><p>Verified contact details must be configured before publishing.</p></body></html>",
     "needs_review", "offer.html"),
])
def test_live_copy_audit_reads_immutable_html_without_writes(monkeypatch, tmp_path, html,
                                                             secondary_html, expected, finding_path):
    with service_modules("company_os"):
        from app.hosting import service

    deployment_id, app_id, artifact_id = "dep_12345678", "app_12345678", "bar_12345678"
    pages = {"index.html": html}
    if secondary_html:
        pages["offer.html"] = secondary_html
    for path, content in pages.items():
        (tmp_path / path).write_text(content)
    manifest, archive = freeze_source(tmp_path)
    capture = {"artifact_id": artifact_id, "sha256": hashlib.sha256(archive).hexdigest(),
               "size_bytes": len(archive), "manifest": manifest, "manifest_digest": manifest_digest(manifest)}
    row = SimpleNamespace(id=deployment_id, app_id=app_id, build_artifact_id=artifact_id,
                          workspace_id="ws_12345678", venture_id="ven_12345678",
                          state="live", target="supymem_static")
    artifact = SimpleNamespace(id=artifact_id, app_id=app_id, build_run_id="brn_12345678",
                               workspace_id=row.workspace_id, sha256=capture["sha256"], size_bytes=len(archive))
    run = SimpleNamespace(app_id=app_id, workspace_id=row.workspace_id,
                          venture_id=row.venture_id, spec=json.dumps({"artifact_capture": capture}))

    class Session:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, model, _id):
            return {"Deployment": row, "BuildArtifact": artifact, "BuildRun": run}[model.__name__]

    class S3:
        def __init__(self): self.calls = []
        def get_object(self, **kwargs):
            self.calls.append(kwargs)
            body = pages[kwargs["Key"].rsplit("/", 1)[-1]].encode()
            return {"Body": BytesIO(body), "Metadata": {
                "deployment_id": deployment_id, "artifact_id": artifact_id,
                "sha256": hashlib.sha256(body).hexdigest(),
            }}

    s3 = S3()
    monkeypatch.setattr(service, "db", lambda: Session)
    config = _config(service)
    result = service.audit_published_copy(app_id, deployment_id, s3=s3, config=config)
    expected_findings = ([{"path": finding_path, "categories": ["publication_instruction"]}]
                         if finding_path else [])
    assert result == {"deployment_id": deployment_id, "state": expected, "findings": expected_findings}
    assert s3.calls == [{"Bucket": config.bucket,
                         "Key": f"published/{app_id}/{deployment_id}/{path}",
                         "ExpectedBucketOwner": config.expected_owner} for path in sorted(pages)]


def test_live_copy_audit_rejects_other_apps_before_reading_storage(monkeypatch):
    with service_modules("company_os"):
        from app.hosting import service

    class Session:
        def __enter__(self): return self
        def __exit__(self, *_args): pass
        def get(self, _model, _id):
            return SimpleNamespace(app_id="app_another1", state="live", target="supymem_static",
                                   build_artifact_id="bar_12345678")

    monkeypatch.setattr(service, "db", lambda: Session)
    with pytest.raises(service.HostingError, match="hosting_deployment_not_live"):
        service.audit_published_copy("app_12345678", "dep_12345678", s3=object(), config=_config(service))


@pytest.mark.asyncio
async def test_managed_static_deploy_is_a_real_hosted_worker_capability(monkeypatch):
    from app.worker import handlers

    called = []
    monkeypatch.setitem(
        handlers.HANDLERS, "deploy.run", lambda payload: called.append(payload)
    )
    await handlers.dispatch(
        "deploy.run",
        {
            "schema_version": 1,
            "deployment_id": "dep_12345678",
            "app_id": "app_12345678",
        },
    )
    assert called and called[0]["schema_version"] == 1


def test_hosting_config_fails_closed_when_provider_is_missing(monkeypatch):
    from app.hosting.service import HostingConfig, HostingError

    for name in (
        "PUBLIC_SITE_S3_BUCKET",
        "PUBLIC_SITE_AWS_REGION",
        "PUBLIC_SITE_S3_EXPECTED_OWNER",
        "PUBLIC_SITE_KVS_ARN",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(HostingError, match="hosting_provider_unavailable") as observed:
        HostingConfig.from_environment()
    assert observed.value.status == 503


def test_public_objects_are_verified_after_an_idempotent_write():
    from app.hosting.service import AwsStaticPublisher

    class S3:
        def __init__(self):
            self.row = None

        def put_object(self, **kwargs):
            self.row = kwargs

        def head_object(self, **_kwargs):
            return {
                "ContentLength": self.row["ContentLength"],
                "ChecksumSHA256": self.row["ChecksumSHA256"],
                "ContentType": self.row["ContentType"],
                "Metadata": self.row["Metadata"],
                "ServerSideEncryption": self.row["ServerSideEncryption"],
            }

    s3 = S3()
    body = b"<h1>accepted</h1>"
    publisher = AwsStaticPublisher(_config(), s3, object())
    publisher.put_files(
        app_id="app_12345678",
        deployment_id="dep_12345678",
        artifact_id="art_12345678",
        files=[
            {
                "path": "index.html",
                "body": body,
                "sha256": hashlib.sha256(body).hexdigest(),
                "content_type": "text/html; charset=utf-8",
            }
        ],
    )

    assert s3.row["Key"] == "published/app_12345678/dep_12345678/index.html"
    assert s3.row["IfNoneMatch"] == "*"
    assert s3.row["Metadata"]["artifact_id"] == "art_12345678"


def test_public_objects_reject_a_conflicting_existing_object():
    from app.hosting.service import AwsStaticPublisher, HostingError

    class S3:
        def put_object(self, **_kwargs):
            raise _client_error("PreconditionFailed")

        def head_object(self, **_kwargs):
            return {
                "ContentLength": 1,
                "ChecksumSHA256": "wrong",
                "ContentType": "text/html; charset=utf-8",
                "Metadata": {},
                "ServerSideEncryption": "AES256",
            }

    body = b"accepted"
    with pytest.raises(HostingError, match="hosting_public_verification_failed"):
        AwsStaticPublisher(_config(), S3(), object()).put_files(
            app_id="app_12345678",
            deployment_id="dep_12345678",
            artifact_id="art_12345678",
            files=[
                {
                    "path": "index.html",
                    "body": body,
                    "sha256": hashlib.sha256(body).hexdigest(),
                    "content_type": "text/html; charset=utf-8",
                }
            ],
        )


def test_hostname_switch_can_restore_the_previous_mapping():
    from app.hosting.service import AwsStaticPublisher
    from core.artifact_snapshot import canonical

    old = host_mapping(
        app_id="app_12345678",
        deployment_id="dep_aaaaaaaa",
        generation=1,
        enabled=True,
    )
    new = host_mapping(
        app_id="app_12345678",
        deployment_id="dep_bbbbbbbb",
        generation=2,
        enabled=True,
    )

    class Kvs:
        value = canonical(old).decode()
        revision = 1

        def describe_key_value_store(self, **_kwargs):
            return {
                "ETag": str(self.revision),
                "TotalSizeInBytes": len("launch.supymem.com") + len(self.value),
            }

        def get_key(self, **_kwargs):
            return {"Value": self.value}

        def put_key(self, *, Value, IfMatch, **_kwargs):
            assert IfMatch == str(self.revision)
            self.value = Value
            self.revision += 1

    kvs = Kvs()
    publisher = AwsStaticPublisher(_config(), object(), kvs)
    previous = publisher.switch("launch.supymem.com", new)
    assert kvs.value == canonical(new).decode()

    publisher.restore("launch.supymem.com", new, previous)
    assert kvs.value == canonical(old).decode()


def test_hostname_restore_refuses_to_overwrite_a_later_publication():
    from app.hosting.service import AwsStaticPublisher, HostingError
    from core.artifact_snapshot import canonical

    installed = host_mapping(
        app_id="app_12345678",
        deployment_id="dep_aaaaaaaa",
        generation=1,
        enabled=True,
    )
    later = host_mapping(
        app_id="app_12345678",
        deployment_id="dep_bbbbbbbb",
        generation=2,
        enabled=True,
    )

    class Kvs:
        def describe_key_value_store(self, **_kwargs):
            return {"ETag": "2", "TotalSizeInBytes": 100}

        def get_key(self, **_kwargs):
            return {"Value": canonical(later).decode()}

    with pytest.raises(HostingError, match="hosting_restore_conflict"):
        AwsStaticPublisher(_config(), object(), Kvs()).restore(
            "launch.supymem.com", installed, None
        )


def test_new_native_publication_refuses_an_existing_hostname_mapping():
    with service_modules("company_os"):
        from app.hosting.service import AwsStaticPublisher, HostingError
        from core.artifact_snapshot import canonical
        existing = host_mapping(app_id="app_12345678", deployment_id="dep_aaaaaaaa", generation=1, enabled=True)
        incoming = host_mapping(app_id="app_12345678", deployment_id="dep_bbbbbbbb", generation=2, enabled=True)
        class Kvs:
            writes = 0
            def describe_key_value_store(self, **_):
                return {"ETag": "1", "TotalSizeInBytes": 500}
            def get_key(self, **_):
                return {"Value": canonical(existing).decode()}
            def put_key(self, **_):
                self.writes += 1
        kvs = Kvs()
        publisher = AwsStaticPublisher(_config(), object(), kvs)
        with pytest.raises(HostingError, match="^subscription_static_publication_baseline_changed$"):
            publisher.switch("launch.supymem.com", incoming, expected_absent=True)
        assert kvs.writes == 0


@pytest.mark.parametrize("has_predecessor", [False, True])
def test_native_publication_lost_ack_retry_retains_original_rollback(has_predecessor):
    with service_modules("company_os"):
        from app.hosting.service import AwsStaticPublisher, HostingError
        from core.artifact_snapshot import canonical
        prior = host_mapping(app_id="app_12345678", deployment_id="dep_aaaaaaaa", generation=1, enabled=True) if has_predecessor else None
        incoming = host_mapping(app_id="app_12345678", deployment_id="dep_bbbbbbbb", generation=2, enabled=True)
        class Kvs:
            writes = 0
            value = canonical(prior).decode() if prior else None
            def describe_key_value_store(self, **_):
                return {"ETag": "1", "TotalSizeInBytes": 500}
            def get_key(self, **_):
                if self.value is None:
                    raise _client_error("ResourceNotFoundException")
                return {"Value": self.value}
            def put_key(self, **kwargs):
                self.writes += 1
                self.value = kwargs["Value"]
                if self.writes == 1:
                    raise _client_error("InternalServerException")
            def delete_key(self, **_):
                self.value = None
        kvs = Kvs()
        publisher = AwsStaticPublisher(_config(), object(), kvs)
        options = {"expected_absent": prior is None, "expected_previous_deployment_id": prior["deployment"] if prior else None,
                   "expected_previous_mapping": prior}
        with pytest.raises(HostingError, match="hosting_mapping_write_failed"):
            publisher.switch("launch.supymem.com", incoming, **options)
        previous = publisher.switch("launch.supymem.com", incoming, **options)
        assert previous == (canonical(prior).decode() if prior else None)
        assert kvs.writes == 1
        publisher.restore("launch.supymem.com", incoming, previous)
        assert kvs.value == previous


@pytest.mark.parametrize("change", ["deployment", "generation", "enabled"])
def test_native_publication_rejects_mapping_drift(change):
    with service_modules("company_os"):
        from app.hosting.service import AwsStaticPublisher, HostingError
        from core.artifact_snapshot import canonical
        prior = host_mapping(app_id="app_12345678", deployment_id="dep_aaaaaaaa", generation=1, enabled=True)
        changed = dict(prior)
        if change == "deployment":
            changed = host_mapping(app_id="app_12345678", deployment_id="dep_cccccccc", generation=1, enabled=True)
        elif change == "generation":
            changed[change] = 3
        else:
            changed[change] = False
        incoming = host_mapping(app_id="app_12345678", deployment_id="dep_bbbbbbbb", generation=2, enabled=True)
        class Kvs:
            def describe_key_value_store(self, **_):
                return {"ETag": "1", "TotalSizeInBytes": 500}
            def get_key(self, **_):
                return {"Value": canonical(changed).decode()}
            def put_key(self, **_):
                pytest.fail("Drift must not write")
        with pytest.raises(HostingError, match="subscription_static_publication_baseline_changed"):
            AwsStaticPublisher(_config(), object(), Kvs()).switch(
                "launch.supymem.com", incoming, expected_previous_deployment_id=prior["deployment"],
                expected_previous_mapping=prior)


@pytest.mark.parametrize("name", ["Supplier scheduling", "Family recipes"])
def test_local_html_export_reuses_authorized_frozen_source_and_blocks_credentials(monkeypatch, name):
    from secrets import token_urlsafe

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient

    with service_modules("company_os"):
        from app.api import firstlook_api
        from app.firstlook import versions
        from app.pagebuilder import local_publish

        monkeypatch.setenv("SUPYMEM_MODE", "local")
        secret = token_urlsafe(32)
        monkeypatch.setenv("LLM_API_KEY", secret)
        html = f"<!doctype html><html><body><h1>{name}</h1></body></html>"
        previews = []

        def authorize(workspace_id, venture_id, subject):
            previews.append((workspace_id, subject, venture_id))
            if workspace_id != "wsp_export":
                raise HTTPException(404)
        monkeypatch.setattr(firstlook_api, "_assert_site_reader", authorize)
        monkeypatch.setattr(versions, "get_latest", lambda *args, **kwargs: SimpleNamespace(
            sha256=hashlib.sha256(html.encode()).hexdigest(), copy={"_render_snapshot": {"html": html}}))
        application = FastAPI()
        application.include_router(local_publish.router)
        with TestClient(application) as client:
            response = client.get("/workspaces/wsp_export/site/export", headers={"X-Subject": name})
            assert response.status_code == 200 and response.headers["content-type"].startswith("text/html")
            assert response.text == html and response.headers["cache-control"] == "no-store"
            assert previews == [("wsp_export", name, None)]
            assert client.get("/workspaces/wsp_foreign/site/export").status_code == 404
            html = html.replace(name, secret)
            response = client.get("/workspaces/wsp_export/site/export")
            assert response.status_code == 422 and secret not in response.text
            monkeypatch.delenv("SUPYMEM_MODE")
            assert client.get("/workspaces/wsp_export/site/export").status_code == 404


@pytest.mark.parametrize("idea,audience", [
    ("Schedule equipment inspections for property managers", "property managers"),
    ("Plan pottery weekends for families", "families planning pottery weekends"),
])
@pytest.mark.parametrize("source", ["process", "dotenv"])
def test_requested_test_page_needs_your_tool_and_generates_nothing_over_http(local_owner_app, monkeypatch, tmp_path, idea, audience, source):
    from tests.unit.test_local_mode import _owner_client
    from tests.company_os_harness import append_idea_revision
    from tests.unit.pagebuilder.test_api import seed
    from app.core.local_owner import ensure_owner
    from app.db.models import Job, SiteVersion, Venture
    from app.pagebuilder import local_publish
    from app.state import db
    from desktop_runtime import agent_status

    monkeypatch.setenv("LLM_OFFLINE", "1")
    monkeypatch.setenv("LLM_CASSETTE_MODE", "replay")
    monkeypatch.setenv("EMAIL_PROVIDER", "log")
    limits = {"RATE_LIMIT_PASSWORD_900": "5", "LLM_DAILY_TOKEN_BUDGET": "1000"}
    if source == "process":
        for key, value in limits.items():
            monkeypatch.setenv(key, value)
    else:
        config = tmp_path / ".env"
        config.write_text("".join(f"{key}={value}\n" for key, value in limits.items()))
        monkeypatch.setenv("COMPANY_OS_SECRETS_ENV", str(config))
    monkeypatch.setattr(local_publish, "deploy", lambda *_args, **_kwargs: pytest.fail("Your page must stay private"))
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    with _owner_client(local_owner_app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as client:
        assert client.post("/local/providers/sample", json={}).status_code == 200
        data = seed(db(), actor=ensure_owner().id)
        with db()() as session:
            venture = session.get(Venture, data["vid"])
            venture.name, venture.raw_idea = audience, idea
            append_idea_revision(session, data["rid"], summary=idea,
                structured=json.dumps({"audience": audience}))
            session.commit()
        path = f'/workspaces/{data["wid"]}/site/export?venture_id={data["vid"]}'
        assert client.get(path).status_code == 404
        response = client.post(f'/ventures/{data["vid"]}/chat',
            json={"message": "Build a landing page for my saved idea. Keep it private and do not send anything",
                  "idempotency_key": "private-page"})
        assert response.status_code == 200, response.text
        reply = response.json()
        assert reply["kind"] == "text", reply["body_md"]
        assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
        assert not any(action.get("confirmation") for action in reply.get("actions", []))
        assert "you" in reply["body_md"].lower() and "\u2014" not in reply["body_md"]
        response = client.get(path)
        assert response.status_code == 404, response.text
        with db()() as session:
            pages = session.query(SiteVersion).filter_by(venture_id=data["vid"]).all()
            assert pages == []
            assert session.query(Job).filter_by(venture_id=data["vid"]).count() == 0
        monkeypatch.setenv("SUPYMEM_MODE", "hosted")
        assert client.get(path).status_code == 404
