"""Your exported tool bytes feed local page results for B2B and consumer ventures."""
import base64
import hashlib
from html import escape
import io
import json
import socket
import threading
import time
import zipfile

import pytest
import uvicorn

from scripts.local_page_signup_journey import run_journey
from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.test_page_experiments import local_measurement_spec


def saved_page_test(market, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger
    from app.state import db

    spec = local_measurement_spec(market, monkeypatch, "headlines")
    with db()() as session:
        session.get(Experiment, spec["id"]).kind = ledger.KIND
        session.commit()
    return spec


@pytest.fixture
def local_page(market, monkeypatch, tmp_path):
    from app.conversion import waitlist
    from app.db.models import Venture
    from app.firstlook import versions
    from app.pagebuilder import local_publish
    from app.product_analytics import beacon
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setenv("CORE_PORT", str(port))
    api_url = f"http://127.0.0.1:{port}"
    spec = saved_page_test(market, monkeypatch)
    destination = local_publish.waitlist_destination(market[1], market[3]["icp"]["who"], deployed=False)
    assert destination["url"] == waitlist.public_url(market[1]) == api_url + f"/public/waitlists/{market[1]}"
    assert beacon.public_events_url() == api_url + "/e"
    with db()() as session:
        name = session.get(Venture, market[1]).name
    key = waitlist.beacon_key(market[1])
    script = beacon.script_tag(key=key)
    content = script.split('<script data-supymem-analytics="1">', 1)[1].split('</script>', 1)[0]
    digest = base64.b64encode(hashlib.sha256(content.encode()).digest()).decode()
    consent = waitlist.consent_text(name)
    source = ('<!doctype html><html><head><meta charset="utf-8">'
        + '<meta http-equiv="Content-Security-Policy" content="' + escape(
            "default-src 'none'; base-uri 'none'; script-src 'sha256-" + digest + "'; connect-src " + api_url + "/e; form-action " + api_url, quote=True) + '"></head><body>'
        + '<h1>' + escape(name) + '</h1><form method="post" action="' + escape(destination["url"], quote=True) + '">'
        + '<label>Email<input name="email" type="email" required></label>'
        + ''.join('<input type="hidden" name="' + field + '" value="' + escape(value, quote=True) + '">' for field, value in {
            "consent_version": waitlist.VERSION, "consent_digest": hashlib.sha256(consent.encode()).hexdigest(),
            "experiment_id": spec["id"], "variant": "alpha"}.items())
        + '<label hidden>Website<input name="website" value="" pattern="^$" tabindex="-1"></label>'
        + '<label><input type="checkbox" name="consent" value="yes" required>' + escape(consent) + '</label>'
        + '<button type="submit">Join the waitlist</button></form>' + script + '</body></html>')
    tool_file = tmp_path / "tool-result.html"
    tool_file.write_text(source)
    versions.persist(workspace_id=market[2], venture_id=market[1], stage="concept", template="local_builder",
        archetype="concept", brandkit_hash="fixture", copy_hash="fixture", copy={}, brandkit={}, variant_index=0,
        path=str(tool_file), url=None, sha256=hashlib.sha256(source.encode()).hexdigest())
    server = uvicorn.Server(uvicorn.Config(market[0].app, host="127.0.0.1", port=port, lifespan="off", log_level="critical", access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.05)
        assert server.started
        yield api_url, spec, source
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        assert not thread.is_alive()


def test_exported_page_signup_and_views_update_your_experiment(local_page, market):
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent, Experiment, VentureWaitlistEntry
    from app.state import db

    api_url, spec, source = local_page
    result = run_journey(api_url, market[1], spec["id"], "local-journey@supymem.com", variant="alpha")
    assert result == {"signups": 1, "views": 2, "experiment_id": spec["id"], "outcome": "inconclusive"}
    exported = market[0].get(f"/ventures/{market[1]}/site/local/export.zip")
    assert exported.status_code == 200
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        assert archive.read("index.html") == source.encode()
    with db()() as session:
        entries = session.scalars(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == market[1])).all()
        events = session.scalars(select(AppEvent).where(AppEvent.app_id == market[-1])).all()
        experiment = session.get(Experiment, spec["id"])
        saved = json.loads(experiment.context)["result"]
    assert len(entries) == 1 and not entries[0].is_test
    assert len([event for event in events if event.name == "landing_view"]) == 2
    assert len([event for event in events if event.name == "signup"]) == 1
    assert saved["by_variant"][0]["views"] == 2 and saved["by_variant"][0]["signups"] == 1
    assert saved["by_variant"][0]["signup_rate"] == .5
    assert saved["measurement_sources"] == ["consented_waitlist", "local_events"]


def test_page_measurement_defaults_to_your_local_app(market):
    from app.conversion import waitlist
    from app.firstlook.waitlist import public_gateway_base
    from app.product_analytics import beacon

    endpoint = public_gateway_base() + "/e"
    assert beacon.public_events_url() == endpoint
    script = beacon.script_tag(key=waitlist.beacon_key(market[1]))
    assert endpoint in script and "https://gateway.supymem.com" not in script


def test_manual_page_counts_update_your_experiment(market, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.state import db
    from core.clock import utcnow

    spec = saved_page_test(market, monkeypatch)
    today = utcnow().date().isoformat()
    response = market[0].post(f"/ventures/{market[1]}/measurement/counts", json={"experiment_id": spec["id"],
        "path": "/", "variant": "alpha", "start_date": today, "end_date": today, "views": 2, "clicks": 0, "signups": 1})
    assert response.status_code == 200, response.text
    with db()() as session:
        saved = json.loads(session.get(Experiment, spec["id"]).context)["result"]
    assert saved["by_variant"][0]["views"] == 2 and saved["by_variant"][0]["signups"] == 1
    assert saved["measurement_sources"] == ["manual_counts"]


def test_manual_counts_refresh_failure_keeps_your_saved_counts(market, monkeypatch):
    from sqlalchemy import select
    from app.db.supymem_models import AppEvent
    from app.experiments import page_tests
    from app.state import db
    from core.clock import utcnow

    spec = saved_page_test(market, monkeypatch)
    def unavailable(*args):
        raise OSError("fixture readout unavailable")
    monkeypatch.setattr(page_tests, "readout", unavailable)
    today = utcnow().date().isoformat()
    response = market[0].post(f"/ventures/{market[1]}/measurement/counts", json={"experiment_id": spec["id"],
        "path": "/", "variant": "alpha", "start_date": today, "end_date": today, "views": 2, "clicks": 0, "signups": 1})
    assert response.status_code == 200, response.text
    assert "saved on your laptop" in response.json()["body_md"]
    assert "Open Experiments, choose your test and refresh your results" in response.json()["body_md"]
    with db()() as session:
        rows = session.scalars(select(AppEvent).where(AppEvent.app_id == market[-1], AppEvent.name == "manual_counts")).all()
    assert len(rows) == 1 and json.loads(rows[0].props)["signups"] == 1
