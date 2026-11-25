"""Anonymous hosted-product beacon: skip rules and single-source emission."""

from tests.company_os_harness import load_company_os_app

import pytest

load_company_os_app()


@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
@pytest.mark.parametrize("source", ["checked", "missing", "stale", "duplicate", "commented", "unclosed", "off"])
def test_local_measurement_checks_tool_source_without_rewriting(monkeypatch, host, source):
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(beacon, "public_events_url", lambda: f"https://{host}/e")
    tag = beacon.script_tag(key="pk_app_fixture")
    body = {
        "checked": tag,
        "missing": "",
        "stale": tag.replace("pk_app_fixture", "pk_app_other"),
        "duplicate": tag + tag.replace('data-supymem-analytics="1"', "data-supymem-analytics=1"),
        "commented": "<!--" + tag + "-->",
        "unclosed": tag.replace("</script>", ""),
        "off": tag,
    }[source]
    html = f"<!doctype html><html><body><h1>{host}</h1>{body}</body></html>"
    if source == "checked":
        assert beacon.inject_into_html(html, key="pk_app_fixture") == html
    else:
        key = None if source == "off" else "pk_app_fixture"
        with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
            beacon.inject_into_html(html, key=key)
    plain = f"<html><body><h1>{host}</h1></body></html>"
    assert beacon.inject_into_html(plain, key=None) == plain


@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
def test_local_measurement_reference_rejects_nonpublic_keys(monkeypatch, host):
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(beacon, "public_events_url", lambda: f"https://{host}/e")
    for key in ("sk_app_fixture.private", "pk_app_fixture.private", 'pk_app_fixture</script>'):
        assert beacon.script_tag(key=key) == ""


@pytest.mark.hosted_only
def test_script_tag_skips_webdriver_gpc_dnt_and_supymem_check():
    from app.product_analytics.beacon import script_tag

    html = script_tag(key="pk_app_fixture", endpoint="https://gateway.example/e")
    assert "navigator.webdriver" in html
    assert "globalPrivacyControl" in html
    assert 'doNotTrack === "1"' in html
    assert "supymem_check=1" in html
    assert "shouldSkip" in html
    assert "landing_view" in html
    assert "cta_click" in html
    assert "text/plain;charset=UTF-8" in html
    assert "application/json" not in html
    assert "password" not in html.lower()
    assert "@" not in html


@pytest.mark.hosted_only
def test_script_tag_propagates_supymem_check_across_same_site_hops():
    from app.product_analytics.beacon import script_tag

    html = script_tag(key="pk_app_fixture", endpoint="https://gateway.supymem.com/e")
    assert 'CHECK_FLAG = "supymem_check_tab"' in html
    assert "sessionStorage.setItem(CHECK_FLAG" in html
    assert "checkTabActive()" in html
    assert "rewriteMarkedLinks" in html
    assert "withCheckMarker" in html
    assert 'u.searchParams.set("supymem_check", "1")' in html
    assert ".supymem.com" in html
    assert "gatewayHost()" in html
    assert "hasUrlMarker() || checkTabActive()" in html


@pytest.mark.hosted_only
def test_script_tag_tracks_only_managed_waitlist_links_and_actual_referrer():
    from app.product_analytics.beacon import script_tag

    html = script_tag(key="pk_app_fixture", endpoint="https://gateway.supymem.com/e")
    assert 'if (document.referrer) viewProps.referrer_host = (new URL(document.referrer)).hostname' in html
    assert 'location.hostname || null' not in html
    assert 'node.closest("a[href]")' in html
    assert 'destination.hostname.toLowerCase() === gatewayHost()' in html
    assert '/public\\/waitlists\\/' in html
    assert 'node.closest("a,button' not in html
    assert 'source: "form_submit"' not in html


@pytest.mark.hosted_only
def test_with_check_marker_is_idempotent():
    from app.product_analytics.beacon import with_check_marker

    url = "https://example.test/path"
    once = with_check_marker(url)
    twice = with_check_marker(once)
    assert once == "https://example.test/path?supymem_check=1"
    assert twice == once


@pytest.mark.hosted_only
def test_inject_into_html_is_idempotent():
    from app.product_analytics.beacon import inject_into_html

    base = "<html><body><h1>Hi</h1></body></html>"
    once = inject_into_html(base, key="pk_app_a")
    twice = inject_into_html(once, key="pk_app_a")
    assert twice.count('data-supymem-analytics="1"') == 1


@pytest.mark.hosted_only
def test_emit_browser_event_rejects_unknown_names():
    from app.product_analytics.beacon import emit_browser_event

    assert emit_browser_event(venture_id="ven_x", event="chat.body") is False
    assert emit_browser_event(venture_id="ven_missing_app_zzzz", event="landing_view") is False


def test_local_beacon_uses_only_your_configured_endpoint(monkeypatch):
    import json
    import subprocess
    from app.core import local_env_import, local_secrets
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("SUPYMEM_PUBLIC_URL", "https://retired.example")
    values = {}
    monkeypatch.setattr(local_env_import, "import_local_env", lambda **options: None)
    stored_settings = local_secrets.read_settings
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
        "values": values, "sources": {"site_url": "app"}, "secrets_set": {}
    } if section == "publishing" else stored_settings(section))
    from app.firstlook.waitlist import public_gateway_base
    assert beacon.public_events_url() == public_gateway_base() + "/e"
    assert public_gateway_base() + "/e" in beacon.script_tag(key="pk_app_fixture")
    for host in ("inventory.example", "gardening.example"):
        values["site_url"] = f"https://{host}"
        assert beacon.public_events_url() == f"https://{host}/e"
        script = beacon.script_tag(key="pk_app_fixture")
        assert f"https://{host}/e" in script
        assert "supymem.com" not in script
        assert 'credentials: "omit"' in script
        assert "globalPrivacyControl" in script
        assert "signup" in script
        executable = script.split('data-supymem-analytics="1">', 1)[1].rsplit("</script>", 1)[0]
        harness = r'''
const vm = require('node:vm');
const listeners = {}, bodies = [];
const location = new URL(LOCATION);
const button = {href: '/public/waitlists/venture/pricing/experiment/beta', getAttribute: () => null};
const document = {querySelector: () => null, querySelectorAll: () => [], referrer: '', addEventListener: (event, handler) => {listeners[event] = handler;}};
const navigator = {sendBeacon: (url, body) => {bodies.push(body);}};
vm.runInNewContext(SCRIPT, {location, document, navigator, window: {}, URL, URLSearchParams, Blob});
listeners.click({target: {closest: () => button}});
listeners['supymem:signup']();
Promise.all(bodies.map(async body => JSON.parse(await body.text()))).then(events => console.log(JSON.stringify(events)));
'''.replace("LOCATION", json.dumps(f"https://{host}/?experiment_id=exp_fixture&variant=alpha")).replace("SCRIPT", json.dumps(executable))
        executed = subprocess.run(["node", "-e", harness], capture_output=True, text=True, check=True)
        observed = json.loads(executed.stdout)
        assert [event["event"] for event in observed] == ["landing_view", "cta_click", "signup"]
        assert observed[1]["props"]["price_point"] == "beta"
        assert beacon.script_tag(key="pk_app_fixture", endpoint="https://gateway.supymem.com/e") == ""
        assert beacon.script_tag(key="pk_app_fixture", endpoint="https://unconfigured.example/e") == ""
        assert beacon.script_tag(key="not_a_public_ingest_key") == ""
        old = '<script type="text/javascript" data-supymem-analytics="1">send("https://gateway.supymem.com/e")</script>'
        with pytest.raises(ValueError, match="Settings > Keys and connections > Coding tools"):
            beacon.inject_into_html(old, key="pk_app_fixture")
    for url in ("https://supymem.com", "https://gateway.supymem.com", "https://supymem.com./", "https://%73upymem.com", "https://Ｓｕｐｙｍｅｍ.com", "https://user:pass@owned.example", "javascript:alert(1)"):
        values["site_url"] = url
        assert beacon.public_events_url() == ""
    from app.product_analytics import service
    monkeypatch.setattr(service, "enabled", lambda name: False)
    values["site_url"] = "https://owned.example"
    assert beacon.script_tag(key="pk_app_fixture") == ""
    assert not beacon.emit_browser_event(venture_id="any", event="signup")
