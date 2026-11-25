"""Page experiments stay scoped and thresholded for unrelated B2B and consumer ventures."""
import hashlib
from html import unescape
import json
import re
import subprocess
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from sqlalchemy import select

from tests.unit.test_autonomous_experiments import market, venture
from tests.unit.pagebuilder.test_api import connect_local_tool


@pytest.fixture
def connected_coding_tool(market, monkeypatch):
    from app.build import device_orders
    from app.core import local_owner
    from app.core.local_mode import is_local
    from app.core.local_secrets import update_settings
    from app.pagebuilder import api

    if is_local():
        update_settings("capabilities", {"run_for_me_experiments": True}, {})
        connect_local_tool(monkeypatch)
        monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
        monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", lambda *_args, **_kwargs:
            {"id": "dwo_experiment_fixture", "state": "approved"})
        monkeypatch.setattr(api, "_start", lambda *_args, **_kwargs:
            {"order_id": "dwo_experiment_fixture", "state": "running"})


def _stub_checked_publication(spec, monkeypatch, *, site_url):
    from datetime import datetime
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests

    def checked(session, venture_id, workspace_id, experiment_id):
        if experiment_id != spec["id"]:
            return None
        row = session.get(Experiment, experiment_id)
        assert (row.venture_id, row.workspace_id) == (venture_id, workspace_id)
        context = json.loads(row.context)
        return SimpleNamespace(url=site_url, app_id=context["spec"]["app_id"], target="local_static",
            live_at=datetime.fromisoformat(context["prepared_at"]))

    monkeypatch.setattr(page_tests, "_checked_deployment", checked)


def _stub_checked_publication(spec, monkeypatch, *, site_url):
    from datetime import datetime
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests

    def checked(session, venture_id, workspace_id, experiment_id):
        if experiment_id != spec["id"]:
            return None
        row = session.get(Experiment, experiment_id)
        assert (row.venture_id, row.workspace_id) == (venture_id, workspace_id)
        context = json.loads(row.context)
        return SimpleNamespace(url=site_url, app_id=context["spec"]["app_id"], target="local_static",
            live_at=datetime.fromisoformat(context["prepared_at"]))

    monkeypatch.setattr(page_tests, "_checked_deployment", checked)


def local_measurement_spec(market, monkeypatch, kind, *, host="owned.example", published=True):
    from datetime import timedelta
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, ledger
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(ledger, "quality_enabled", lambda: False)
    variants = [{"key": "alpha", "label": "First", "price": "USD 12", "medium": "other"},
        {"key": "beta", "label": "Second", "price": "USD 24", "medium": "other"}]
    metric = {"headlines": "signup_rate_ratio", "pricing": "cta_click_rate", "channels": "channel_signup_share", "referral": "referrer_share"}[kind]
    spec = {"id": "exp_local_" + kind, "venture_id": market[1], "workspace_id": market[2], "app_id": market[-1],
        "scan": "page_test" if kind in {"headlines", "pricing"} else "channel_test",
        "page_kind": kind, "channel_kind": kind, "title": "Your page comparison", "variants": variants if kind != "referral" else [],
        "site_url": f"https://{host}/", "metric": metric, "max_duration_days": 14,
        "min_sample": 10, "sample_unit": "views", **autonomous._thresholds(1.5 if kind == "headlines" else .1, .1, 10)}
    with db()() as session:
        spec["direction"] = ledger.current_direction(session, market[1])
        session.add(Experiment(id=spec["id"], venture_id=market[1], workspace_id=market[2], app_id=market[-1],
            kind="market", state="running", context=json.dumps({"spec": spec,
                "prepared_at": (utcnow() - timedelta(days=2)).isoformat(), "effort": {}, "cohort": {"one": "alpha", "two": "beta"}})))
        session.commit()
    if published and kind in {"headlines", "pricing"}:
        _stub_checked_publication(spec, monkeypatch, site_url=spec["site_url"])
    return spec


def test_local_public_ingestion_is_private_and_venture_scoped(market, monkeypatch):
    from app.apps.keys import ensure_ingest_key
    from app.db.supymem_models import AppEvent
    from app.product_analytics import service
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    key = ensure_ingest_key(market[-1])["secret"]
    assert service.ingest_public({"key": key, "event": "landing_view", "props": {"path": "/", "venture_id": "foreign"}})
    assert not service.ingest_public({"key": key, "event": "manual_counts", "props": {"views": 999}})
    assert not service.ingest_public({"key": key, "event": "signup", "props": {"email": "private@example.org"}})
    assert not service.ingest_public({"key": key, "event": "landing_view", "props": {"utm_source": "password=not-a-real-credential"}})
    assert not service.ingest_public({"key": "pk_app_unknown", "event": "landing_view"})
    from app.tenancy import bind
    with bind(workspace_id=market[2], venture_id="foreign", actor="owner"):
        assert not service.ingest_public({"key": key, "event": "landing_view"})
    with db()() as session:
        rows = session.scalars(select(AppEvent)).all()
        assert len(rows) == 1
        assert (rows[0].app_id, rows[0].workspace_id) == (market[-1], market[2])
        assert json.loads(rows[0].props) == {"path": "/"}


AUDIT_MARKETS = [("company", "harbor pilots", "scheduling berth arrivals", "Berth Schedule"),
                 ("consumer", "casual bread bakers", "timing overnight dough", "Dough Time")]


@pytest.mark.parametrize("market,host", [(AUDIT_MARKETS[0], "berth-counts.example"),
    (AUDIT_MARKETS[1], "dough-counts.example")], indirect=["market"])
def test_manual_pricing_needs_click_counts_for_every_price(market, host, monkeypatch):
    from app.experiments import page_tests
    from app.product_analytics import service
    from core.clock import utcnow

    spec = local_measurement_spec(market, monkeypatch, "pricing", host=host)
    today = utcnow().date().isoformat()
    payload = {"experiment_id": spec["id"], "path": "/", "start_date": today, "end_date": today,
        "views": 100, "variant": "beta", "clicks": 10, "signups": 0}
    service.record_counts(market[1], payload)
    result = page_tests.observe(spec)
    assert result["outcome"] == "inconclusive" and "each price" in result["insight"]
    assert result["measured"]["higher_price_share"] is None
    assert [item["views"] for item in result["by_variant"]] == [100, 100]
    service.record_counts(market[1], {**payload, "variant": "alpha", "clicks": 0})
    result = page_tests.observe(spec)
    assert result["outcome"] == "supported" and result["measured"]["higher_price_share"] == 1


@pytest.mark.parametrize("market,host,kind", [(AUDIT_MARKETS[0], "berth-launch.example", "pricing"),
    (AUDIT_MARKETS[1], "dough-launch.example", "headlines")], indirect=["market"])
def test_local_page_observes_from_checked_publication(market, host, kind, monkeypatch):
    from datetime import timedelta
    from app.db.supymem_models import AppEvent, BuildRun, Deployment, Experiment
    from app.experiments import ledger, page_tests
    from app.hosting import service as hosting
    from app.pagebuilder import local_publish
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow

    spec = local_measurement_spec(market, monkeypatch, kind, host=host, published=False)
    now = utcnow()
    launch = now - timedelta(days=1)
    url = spec["site_url"]
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        row.kind, row.state = ledger.KIND, "proposed"
        row.context = json.dumps({**json.loads(row.context), "runner": "system", "prepared_at": (now - timedelta(days=30)).isoformat()})
        session.commit()
    readout_request = {"experiment_id": spec["id"]}
    before = page_tests.readout(market[1], readout_request)
    day = launch.date().isoformat()
    payload = {**readout_request, "path": "/", "start_date": day, "end_date": day, "variant": "alpha",
        "views": 100, "clicks": 0, "signups": 0}
    with pytest.raises(ValueError, match="Website.*Publish landing page"):
        service.record_counts(market[1], payload)
    with db()() as session:
        session.add(BuildRun(id="checked-run", venture_id=market[1], workspace_id=market[2], app_id=market[-1],
            state="succeeded", artifact_id="checked-artifact", trigger="manual", agent="pagebuilder",
            spec=json.dumps({"inputs": {"experiment_id": spec["id"]}})))
        session.add(Deployment(id="checked-deployment", venture_id=market[1], workspace_id=market[2], app_id=market[-1],
            build_artifact_id="checked-artifact", target="local_static", state="live", version=1, url=url, live_at=launch))
        session.add(AppEvent(app_id=market[-1], workspace_id=market[2], ts=launch - timedelta(minutes=1), name="landing_view",
            props=json.dumps({**readout_request, "path": "/", "variant": "alpha"})))
        session.commit()
    local_publish._save_publication(market[1], market[2], {"state": "live", "app_id": market[-1],
        "local_deployment_id": "checked-deployment", "url": url, "sha256": "a" * 64})
    monkeypatch.setattr(hosting, "_accepted_source", lambda *_args, **_kwargs: None)
    assert all(item["views"] == 0 for item in page_tests.observe(spec)["by_variant"])
    prior = (launch - timedelta(days=1)).date().isoformat()
    with pytest.raises(ValueError, match=day):
        service.record_counts(market[1], {**payload, "start_date": prior, "end_date": prior})
    reply = service.record_counts(market[1], payload)
    measured = page_tests.observe(spec)
    assert measured["measured"]["events"] == (100 if kind == "pricing" else 0)
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: (market[2], "local"))
    monkeypatch.setattr(local_publish, "_verify_public", lambda *_: None)
    for _ in range(2):
        assert local_publish.verify_current(market[1], "local")["state"] == "live"
    with db()() as session:
        publication = page_tests.measurement_publication(session, spec)
        assert page_tests.observation_deadline(spec, {}, publication=publication) == (launch, launch + timedelta(days=14))
    assert page_tests.observe(spec)["by_variant"] == measured["by_variant"]
    prefill = next(action["payload"]["text"] for action in reply["actions"] if action["action"] == "chat.prefill")
    assert now.date().isoformat() in prefill
    with db()() as session:
        assert session.get(Experiment, spec["id"]).state == "running"
    assert "Website" in before["body_md"] and not any(action["action"] == "chat.prefill" for action in before["actions"])


@pytest.mark.parametrize("market,host,kind", [(("company", "harbor pilots", "scheduling berth arrivals", "Harbor Board"), "berth-build.example", "pricing"),
    (AUDIT_MARKETS[1], "dough-build.test", "headlines")], indirect=["market"])
def test_owned_experiment_build_can_be_published_and_measured(market, monkeypatch, tmp_path, host, kind):
    from app.build import device_orders
    from app.chat import site_actions
    from app.conversion import waitlist
    from app.core import local_owner
    from app.core.local_secrets import update_settings
    from app.db.models import Job, VentureDocument
    from app.experiments import page_tests
    from app.llm.founder_provider import FounderLLMConfigError
    from app.pagebuilder import api, local as page_settings, local_publish, models, pipeline, tool
    from app.product_analytics import service
    from app.state import db
    from core.artifact_snapshot import freeze_source, manifest_digest
    from core.clock import utcnow
    from tests.unit.pagebuilder.test_pipeline import review_payload

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    connect_local_tool(monkeypatch)
    def no_model(_):
        raise FounderLLMConfigError("no separate review model")
    monkeypatch.setattr(page_settings, "route", no_model)
    monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
    monkeypatch.setattr(api, "require_enabled", lambda: None)
    monkeypatch.setattr(local_publish, "enabled", lambda _: True)
    monkeypatch.setattr(service, "enabled", lambda _: True)
    settings = {"provider": "vercel", "project": "test-page", "token": "test-only", "site_url": "https://interest.example"}
    update_settings("publishing", {"site_url": settings["site_url"]}, {})
    monkeypatch.setattr(local_publish, "_settings", lambda: settings)
    monkeypatch.setattr(local_publish, "_events_reachable", lambda *_: False)
    market[4]["enabled"] = True
    venture, workspace, app_id = market[1], market[2], market[-1]
    def check_waitlist(client, address, **options):
        details = waitlist.public_details(venture)
        assert address == details["url"] and client.headers["accept"] == local_publish.WAITLIST_READINESS
        assert options == {"max_redirects": 0, "https_only": True}
        return SimpleNamespace(status_code=204, headers={"X-Waitlist-Consent": hashlib.sha256(details["consent"].encode()).hexdigest()})

    monkeypatch.setattr(local_publish, "get_checked", check_waitlist)
    actor = local_owner.ensure_owner().id
    monkeypatch.setattr(api, "authority", lambda *_args, **_kw: (workspace, actor))
    monkeypatch.setattr(page_tests, "_local_builder", lambda _: (SimpleNamespace(sub=actor), "codex"))
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: (workspace, actor))
    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", lambda *_args, **_kw:
        {"id": "generic", "state": "approved"})
    orders = []

    def prepare(venture_id, caller, job_id, brief, *, provider):
        orders.append((job_id, brief, provider))
        return {"id": "checked-order", "state": "approved"}

    monkeypatch.setattr(device_orders, "prepare_landing_page_from_job", prepare)
    variants = [{"key": "alpha", "label": "First", "price": "$12 per month"},
                {"key": "beta", "label": "Second", "price": "$24 per month"}] if kind == "pricing" else [
        {"key": "alpha", "label": "First", "headline": "Plan your baking time"},
        {"key": "beta", "label": "Second", "headline": "Keep your dough on schedule"}]
    payload = {"kind": kind, "variants": variants}
    spec = page_tests._spec(venture, payload)
    page_tests.configure(venture, {**payload, "fingerprint": spec["fingerprint"]})
    assert len(orders) == 1
    job, brief, provider = orders[0]
    assert spec["id"] in brief and provider == "codex"
    assert local_publish._publication(venture, workspace) is None
    inputs, page = build(spec, market, monkeypatch)
    page_plan = page.plan.model_copy(update={"assets": [asset.model_copy(update={"scene": None,
        "compiler_version": None, "provenance": "original_model_svg"}) for asset in page.assets]})
    destination = {"mode": "endpoint", "url": inputs.waitlist_url, "notice": ""}
    files = {**page.files, "waitlist.html": local_publish.signup_page(venture, inputs.name, destination)}
    for path, body in list(files.items()):
        if path.startswith("price_"):
            files[path] = '<!doctype html><html lang="en"><head><title>Proposed pricing</title></head><body>' + body + '</body></html>'
    files = {path: local_publish._public_html(body, inputs.waitlist_url) if path.endswith(".html") else body
        for path, body in files.items()}
    root = tmp_path / "checked-source"
    for path, body in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body.encode() if isinstance(body, str) else body)
    manifest, archive = freeze_source(root)
    with db()() as session:
        row = session.get(Job, job)
        doc = json.loads(row.payload_json)
        assert doc["_runtime"]["input"]["page_test"]["experiment_id"] == spec["id"]
        doc["tool_build"]["plan"] = page_plan.model_dump(mode="json")
        doc["_runtime"]["input"] = inputs.model_dump(mode="json")
        row.payload_json = api.dump(doc)
        doc = json.loads(row.payload_json)
        inputs = models.PageInput.model_validate(doc["_runtime"]["input"])
        plan = pipeline.read_tool_plan(doc["tool_build"]["plan"])
        session.commit()
    order = {"workspace_id": workspace, "actor_id": actor, "state": "stopped", "execution_id": "checked-execution",
        "task_snapshot": {"pagebuilder_job_id": job, "page_input": inputs.model_dump(mode="json"),
            "page_plan": plan.model_dump(mode="json")}, "iterations": [{"outcome": "pass", "file_manifest_sha256": manifest_digest(manifest)}],
        "native_stop": {"execution_id": "checked-execution", "processes_stopped": True, "task_verified": True, "verification_kind": "workspace_loop_v1"},
        "manifest": {"coding_loop_verification": {"profile": "static_landing_v1", "check_recipe": "static_landing_checks_v1", "phase": "implement"}}}
    with db()() as session:
        session.add(VentureDocument(id="checked-order", venture_id=venture, kind=device_orders.KIND,
            title="Checked page", provenance_json=api.dump(order), created_at=utcnow(), updated_at=utcnow()))
        session.commit()
    monkeypatch.setattr(device_orders, "retrieve", lambda *_: order)
    monkeypatch.setattr(tool.runtime, "validate_inputs", lambda *_: None)
    monkeypatch.setattr(site_actions, "page_built", lambda *_: None)
    tool.review_tool_source(job, inputs, plan, {"reviews": []}, review_source=lambda prompt: json.dumps(review_payload(prompt)))
    assert tool.finish(venture, actor, job, "checked-order", "checked-execution", archive)["state"] == "ready"

    url = f"https://{host}/"
    monkeypatch.setattr(local_publish, "_request", lambda *_args, **_kw: {"id": "host-deployment"})
    monkeypatch.setattr(local_publish, "_poll_deployment", lambda *_: url)
    checked_urls = []
    monkeypatch.setattr(local_publish, "_verify_public", lambda address, _: checked_urls.append(address))
    assert local_publish.publish_site(venture, actor, job_id=job)["url"] == url
    assert local_publish._publication(venture, workspace)["app_id"] == app_id
    with db()() as session:
        assert page_tests.published_url(session, venture, workspace, spec["id"]) == url
        assert page_tests.published_url(session, venture, workspace, "exp_" + "f" * 24) is None
    filename = "price_alpha.html" if kind == "pricing" else "variant_alpha.html"
    assert url + filename in checked_urls and url + "waitlist.html" in checked_urls
    assert page_tests.checked_public_url(venture, spec["id"], filename) == url + filename
    assert page_tests.checked_public_url(venture, spec["id"], "waitlist.html") == url + "waitlist.html"
    today = utcnow().date().isoformat()
    for variant in ("alpha", "beta"):
        service.record_counts(venture, {"experiment_id": spec["id"], "variant": variant, "path": "/",
            "start_date": today, "end_date": today, "views": 100, "clicks": 0, "signups": 0})
    assert page_tests.observe(spec)["measured"]["events"] == 100
    assert page_tests.readout(venture, {"experiment_id": spec["id"]})["body_md"]


@pytest.mark.parametrize("market,domain", list(zip(AUDIT_MARKETS, ["berth-schedules.example", "dough-notes.test"])), indirect=["market"])
def test_independent_live_experiment_links_survive_later_publication(market, domain, monkeypatch):
    from datetime import timedelta
    from app.db.models import Job
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests
    from app.pagebuilder import api, local_publish, native_reference
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow

    venture_id, workspace_id, app_id = market[1], market[2], market[-1]
    unrelated = "exp_" + "f" * 24
    monkeypatch.setattr(native_reference, "_completed_candidate", lambda _session, job: json.loads(job.payload_json))
    monkeypatch.setattr(service, "enabled", lambda _: True)
    settings = {"provider": "vercel", "project": "your-project", "token": "test-only"}
    bindings = []
    for offset, kind, field, filename in [(2, "headlines", "headlines", "variant_alpha.html"), (1, "pricing", "prices", "price_alpha.html")]:
        host = f"{kind}.{domain}"
        spec = local_measurement_spec(market, monkeypatch, kind, host=host, published=False)
        experiment_id, job_id = "exp_" + str(offset) * 24, f"job-{kind}"
        old_id = spec["id"]
        spec["id"] = experiment_id
        launch = utcnow() - timedelta(days=offset)
        variants = [{"key": item["key"], "label": item["label"], **({"headline": "Choose " + item["label"]}
            if field == "headlines" else {"price": item["price"]})} for item in spec["variants"]]
        capture = {"sha256": str(offset) * 64, "manifest": [{"path": file, "sha256": "a" * 64}
            for file in ("index.html", filename)]}
        doc = {"_runtime": {"input": {"page_test": {"experiment_id": experiment_id, field: variants}}, "analytics_app_id": app_id},
            "result": {"bundles": [{"manifest": {"artifact_capture": capture}}]}}
        with db()() as session:
            row = session.get(Experiment, old_id)
            row.id, row.context = experiment_id, json.dumps({**json.loads(row.context), "spec": spec})
            session.add(Job(id=job_id, kind=api.KIND, status="succeeded", venture_id=venture_id, workspace_id=workspace_id,
                created_at=launch, payload_json=json.dumps(doc)))
            session.commit()
        local_publish._save_publication(venture_id, workspace_id, {"state": "live", "job_id": job_id, "variant": 0,
            "experiment_id": experiment_id, "app_id": app_id, "source_sha256": capture["sha256"],
            "files": capture["manifest"], "live_at": launch.isoformat(), "url": spec["site_url"],
            "provider": settings["provider"], "project": settings["project"], "deployment_id": kind})
        assert page_tests.checked_public_url(venture_id, experiment_id, filename) == spec["site_url"] + filename
        bindings.append((spec, launch, filename))

    for spec, launch, filename in bindings:
        assert page_tests.checked_public_url(venture_id, spec["id"], filename) == spec["site_url"] + filename
        with db()() as session:
            assert page_tests.measurement_publication(session, spec).live_at == launch
        day = launch.date().isoformat()
        counts = {"experiment_id": spec["id"], "path": "/", "variant": "alpha", "start_date": day, "end_date": day,
            "views": 100, "clicks": 0, "signups": 0}
        service.record_counts(venture_id, counts)
        assert page_tests.observe(spec)["by_variant"][0]["views"] == 100
        with pytest.raises(ValueError, match="Choose a page or channel test"):
            service.record_counts(venture_id, {**counts, "experiment_id": unrelated})
        with pytest.raises(ValueError, match="Website"):
            page_tests.checked_public_url(venture_id, unrelated, filename)

    removed = []
    monkeypatch.setattr(local_publish, "_site_scope", lambda *_: (workspace_id, "owner"))
    monkeypatch.setattr(local_publish, "_settings", lambda: settings)
    monkeypatch.setattr(local_publish, "_request", lambda method, url, **_: removed.append((method, url.rsplit("/", 1)[-1])))
    monkeypatch.setattr(local_publish, "get_checked", lambda *_args, **_kwargs: SimpleNamespace(status_code=404))
    assert local_publish.unpublish_current(venture_id, "owner")["state"] == "offline"
    assert removed == [("DELETE", "pricing"), ("DELETE", "headlines")]
    for spec, _launch, filename in bindings:
        with db()() as session:
            assert page_tests.measurement_publication(session, spec) is None
        with pytest.raises(ValueError, match="Website"):
            page_tests.checked_public_url(venture_id, spec["id"], filename)


@pytest.mark.parametrize("market", AUDIT_MARKETS, indirect=True)
def test_checked_page_manual_exposure_finishes_its_ledger_result(market, monkeypatch):
    from app.db.supymem_models import Experiment
    from app.experiments import ledger, page_tests
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow

    spec = local_measurement_spec(market, monkeypatch, "headlines")
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        row.kind = ledger.KIND
        row.state = "proposed"
        row.context = json.dumps({**json.loads(row.context), "runner": "system"})
        session.commit()
    page_tests.readout(market[1], {"experiment_id": spec["id"]})
    with db()() as session:
        assert session.get(Experiment, spec["id"]).state == "proposed"
    today = utcnow().date().isoformat()
    payload = {"experiment_id": spec["id"], "path": "/", "start_date": today, "end_date": today,
               "views": 100, "clicks": 40}
    for variant, signups in (("alpha", 35), ("beta", 5)):
        service.record_counts(market[1], {**payload, "variant": variant, "signups": signups})
    with db()() as session:
        assert session.get(Experiment, spec["id"]).state == "concluded"
    assert spec["id"] in {item["id"] for item in ledger.loop_state(market[1])["done"]}


@pytest.mark.parametrize("market", AUDIT_MARKETS, indirect=True)
def test_manual_price_correction_keeps_the_shared_denominator(market, monkeypatch):
    from app.db.supymem_models import AppEvent
    from app.experiments import page_tests
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow

    spec = local_measurement_spec(market, monkeypatch, "pricing")
    now = utcnow()
    props = {"experiment_id": spec["id"], "path": "/"}
    with db()() as session:
        for name, price in [("landing_view", ""), ("cta_click", "alpha"), ("cta_click", "beta")]:
            session.add(AppEvent(app_id=market[-1], workspace_id=market[2], ts=now, name=name,
                props=json.dumps({**props, "price_point": price})))
        session.commit()
    service.record_counts(market[1], {**props, "variant": "beta", "views": 90, "clicks": 3, "signups": 0,
        "start_date": now.date().isoformat(), "end_date": now.date().isoformat()})
    with db()() as session:
        from app.db.supymem_models import Experiment
        context = json.loads(session.get(Experiment, spec["id"]).context)
        window = page_tests.observation_window(spec, context, publication=page_tests.measurement_publication(session, spec))
        counts, _, views = service.measurement_counts(spec, context, session.scalars(select(AppEvent)).all(), [], window=window)
    assert views == 90 and [counts[key]["views"] for key in ("alpha", "beta")] == [90, 90]
    assert [counts[key]["clicks"] for key in ("alpha", "beta")] == [1, 3]
    assert not counts["alpha"]["clicks_complete"] and counts["beta"]["clicks_complete"]
    service.record_counts(market[1], {**props, "variant": "beta", "views": 1, "clicks": 3, "signups": 0,
        "start_date": now.date().isoformat(), "end_date": now.date().isoformat()})
    assert page_tests.observe(spec)["measured"]["higher_price_share"] == .75


@pytest.mark.parametrize("market", AUDIT_MARKETS, indirect=True)
@pytest.mark.parametrize("kind", ["headlines", "channels"])
def test_manual_test_counts_use_only_the_observation_window(market, monkeypatch, kind):
    from datetime import timedelta
    from app.db.supymem_models import AppEvent, Experiment
    from app.experiments import page_tests
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow

    spec = local_measurement_spec(market, monkeypatch, kind)
    now = utcnow()
    first, last = now.date() - timedelta(days=30), now.date() - timedelta(days=16)
    allowed = {"experiment_id": spec["id"], "path": "/", "start_date": first.isoformat(), "end_date": last.isoformat(),
               "channel" if kind == "channels" else "variant": "alpha", "views": 10, "clicks": 2, "signups": 1}
    with db()() as session:
        experiment = session.get(Experiment, spec["id"])
        context = json.loads(experiment.context)
        context["prepared_at"] = (now - timedelta(days=30)).isoformat()
        experiment.context = json.dumps(context)
        session.commit()
    for changes in ({"start_date": (first - timedelta(days=1)).isoformat()},
                    {"end_date": (last + timedelta(days=1)).isoformat()}):
        payload = {**allowed, **changes, "views": 500}
        with pytest.raises(ValueError, match=f"{first}.*{last}"):
            service.record_counts(market[1], payload)
        with db()() as session:
            session.add(AppEvent(app_id=market[-1], workspace_id=market[2], ts=now, name="manual_counts", props=json.dumps(payload)))
            session.commit()
    service.record_counts(market[1], allowed)
    with db()() as session:
        events = session.scalars(select(AppEvent).where(AppEvent.name == "manual_counts")).all()
        publication = page_tests.measurement_publication(session, spec) if kind == "headlines" else None
        window = page_tests.observation_window(spec, context, publication=publication)
        counts, _, views = service.measurement_counts(spec, context, events, [], window=window)
    assert len(events) == 3 and counts["alpha"]["views"] == views == 10


@pytest.mark.usefixtures("measurement_local_owner")
@pytest.mark.parametrize("market", AUDIT_MARKETS, indirect=True)
def test_page_test_guide_connects_then_uses_owned_tool(market, monkeypatch):
    from app.core.local_secrets import update_settings
    from desktop_runtime import agent_status
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests
    from app.pagebuilder import api
    from app.state import db

    update_settings("capabilities", {"landing_pages": True, "run_for_me_experiments": False, "page_measurement": False}, {})
    monkeypatch.setattr(agent_status, "statuses", lambda: [])
    calls = []
    def start_order(vid, _caller, *, provider, experiment_id):
        assert provider == "codex"
        calls.append(page_tests.coding_brief(vid, experiment_id))
        return {"order_id": "dwo_guide", "state": "running"}
    monkeypatch.setattr(api, "_start", start_order)
    client, vid = market[:2]
    market[4]["enabled"] = True
    def choose(action, payload):
        response = client.post(f"/ventures/{vid}/chat/confirm", json={"action": action, "payload": payload})
        assert response.status_code == 200, response.text
        return response.json()

    guide = choose("experiment.page.guide", {})
    assert any(action.get("href") == "/settings/local#coding-tools" for action in guide["actions"])
    assert not calls
    with db()() as session:
        assert not any("page_test" in json.loads(row.context) for row in session.scalars(select(Experiment)))
    connect_local_tool(monkeypatch)
    guide = choose("experiment.page.guide", {})
    action = next(action for action in guide["actions"] if action.get("payload", {}).get("kind") == "headlines")
    reply = choose(action["action"], action["payload"])
    assert any("/device-work-orders/dwo_guide" in action.get("href", "") for action in reply["actions"]), reply
    assert len(calls) == 1 and market[3]["icp"]["problem"] in calls[0]["variants"][0]["headline"]


@pytest.mark.parametrize("kind", ["headlines", "pricing"])
def test_local_manual_counts_feed_page_readouts(market, monkeypatch, kind):
    from core.clock import utcnow
    from app.product_analytics import service
    from app.experiments import page_tests

    spec = local_measurement_spec(market, monkeypatch, kind)
    assert page_tests.observe(spec)["outcome"] == "inconclusive"
    assert "Record" in page_tests.observe(spec)["insight"]
    payload = {"experiment_id": spec["id"], "path": "/", "start_date": utcnow().date().isoformat(),
        "end_date": utcnow().date().isoformat(), "variant": "alpha", "views": 100, "clicks": 20, "signups": 10}
    service.record_counts(market[1], {**payload, "views": 0})
    missing = page_tests.observe(spec)
    assert missing["outcome"] == "inconclusive"
    assert missing["measured"][spec["metric"]] is None
    assert "Record views" in missing["insight"]
    service.record_counts(market[1], payload)
    service.record_counts(market[1], payload)
    if kind == "pricing":
        from datetime import timedelta
        with pytest.raises(ValueError, match="same date range"):
            service.record_counts(market[1], {**payload, "variant": "beta",
                "start_date": (utcnow() - timedelta(days=1)).date().isoformat()})
    service.record_counts(market[1], {**payload, "variant": "beta", "clicks": 10, "signups": 5})
    result = page_tests.observe(spec)
    assert result["measurement_sources"] == ["manual_counts"]
    assert "your manual counts" in result["insight"]
    assert [item["views"] for item in result["by_variant"]] == [100, 100]
    assert result["measured"][spec["metric"]] == (2 if kind == "headlines" else .3)
    from datetime import timedelta
    from app.db.supymem_models import AppEvent
    from app.state import db
    with db()() as session:
        session.add(AppEvent(app_id=spec["app_id"], workspace_id=spec["workspace_id"], ts=utcnow(), name="landing_view",
            props=json.dumps({"path": "/", "experiment_id": spec["id"], "variant": "alpha"})))
        session.add(AppEvent(app_id=spec["app_id"], workspace_id=spec["workspace_id"], ts=utcnow() - timedelta(days=1), name="landing_view",
            props=json.dumps({"path": "/", "experiment_id": spec["id"], "variant": "alpha"})))
        session.commit()
    combined = page_tests.observe(spec)
    assert combined["by_variant"][0]["views"] == 101
    assert combined["measurement_sources"] == ["local_events", "manual_counts"]
    if kind == "pricing":
        with db()() as session:
            for event, props in (("landing_view", {}), ("cta_click", {"variant": "alpha"})):
                session.add(AppEvent(app_id=spec["app_id"], workspace_id=spec["workspace_id"], ts=utcnow() - timedelta(days=1), name=event,
                    props=json.dumps({"path": "/", "experiment_id": spec["id"], **props})))
            session.commit()
        shared = page_tests.observe(spec)
        assert shared["measured"]["clicks"] == 30
        assert shared["measured"]["cta_click_rate"] == 30 / 102
    with pytest.raises(ValueError, match="non-overlapping"):
        service.record_counts(market[1], {**payload, "start_date": (utcnow() - timedelta(days=1)).date().isoformat()})
    with pytest.raises(ValueError, match="views"):
        service.record_counts(market[1], {**payload, "views": -1})
    with pytest.raises(ValueError):
        service.record_counts("foreign", payload)
    monkeypatch.setattr(service, "enabled", lambda name: False)
    assert page_tests.observe(spec)["outcome"] == "inconclusive"
    assert page_tests.readout(market[1], {"experiment_id": spec["id"]})["kind"] == "text"
    with pytest.raises(ValueError):
        service.record_counts(market[1], payload)


def test_local_readouts_do_not_depend_on_hosted_launch_flags(market, monkeypatch):
    from core.clock import utcnow
    from app.product_analytics import service
    from app.experiments import channel_tests, ledger, page_tests

    def hosted_launch(*args):
        pytest.fail("Your local counts must not require hosted launch approval.")

    monkeypatch.setattr(page_tests, "refresh_launch", hosted_launch)
    for module, kind in ((page_tests, "headlines"), (channel_tests, "channels")):
        spec = local_measurement_spec(market, monkeypatch, kind)
        payload = {"experiment_id": spec["id"], "path": "/", "start_date": utcnow().date().isoformat(),
            "end_date": utcnow().date().isoformat(), "channel" if kind == "channels" else "variant": "alpha",
            "views": 100, "clicks": 20, "signups": 10}
        service.record_counts(market[1], payload)
        field = "channel" if kind == "channels" else "variant"
        service.record_counts(market[1], {**payload, field: "beta", "signups": 5})
        monkeypatch.setattr(ledger, "quality_enabled", lambda: True)
        assert module.readout(market[1], {"experiment_id": spec["id"]})["kind"] == "experiment_result"


@pytest.fixture
def measurement_local_owner(monkeypatch):
    from tests.unit.chat import test_round3_matrix
    from tests.unit.test_local_mode import _owner_client

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setenv("CONSOLE_ORIGIN", "http://127.0.0.1")
    monkeypatch.setattr(test_round3_matrix, "TestClient", _owner_client)


def test_local_published_page_retains_only_reachable_owner_measurement(market, monkeypatch, tmp_path):
    import base64
    from core.artifact_snapshot import freeze_source, manifest_digest
    from app.core import local_secrets
    from app.pagebuilder import local_publish
    from app.conversion import waitlist
    from app.product_analytics import beacon

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {
        "values": {"site_url": "https://owned.example"}, "sources": {"site_url": "app"}, "secrets_set": {}})
    monkeypatch.setattr(local_publish, "_settings", lambda: {"site_url": "https://owned.example", "waitlist_email": "owner@owned.example"})
    reachable = [True]
    monkeypatch.setattr(local_publish, "_reachable", lambda *_args: True)
    monkeypatch.setattr(local_publish, "_events_reachable", lambda *_args: reachable[0])
    monkeypatch.setattr(local_publish, "enabled", lambda name: True)
    root = tmp_path / "page"
    root.mkdir()
    monkeypatch.setattr(beacon, "public_events_url", lambda: "https://owned.example/e")
    monkeypatch.setattr(beacon, "_app_for_venture", lambda venture: (market[-1], "pk_app_fixture"))
    script = beacon.script_tag(key="pk_app_fixture", endpoint="https://owned.example/e").strip()
    script_body = script.partition(">")[2].removesuffix("</script>")
    digest = base64.b64encode(hashlib.sha256(script_body.encode()).digest()).decode()
    source = ('<html><head><meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
        f'base-uri \'none\'; script-src \'sha256-{digest}\'; connect-src https://owned.example/e; form-action https://owned.example"></head>'
        f'<body><h1>Your early access page</h1>{script}</body></html>')
    (root / "index.html").write_text(source)
    manifest, archive = freeze_source(root)
    capture = {"manifest": manifest, "sha256": hashlib.sha256(archive).hexdigest(),
        "size_bytes": len(archive), "manifest_digest": manifest_digest(manifest)}
    for deployed, accessible in ((True, True), (False, True), (True, False)):
        reachable[0] = accessible
        if deployed and not accessible:
            with pytest.raises(local_publish.LocalPublishError, match="Settings > Keys and connections > Publishing"):
                local_publish.prepare(archive, capture, venture_id=market[1], name=market[3]["icp"]["who"],
                    waitlist_url=waitlist.public_url(market[1]), deployed=deployed)
            continue
        files, notice = local_publish.prepare(archive, capture, venture_id=market[1], name=market[3]["icp"]["who"],
            waitlist_url=waitlist.public_url(market[1]), deployed=deployed)
        html = next(item["body"].decode() for item in files if item["path"] == "index.html")
        assert "supymem.com" not in html
        assert html == source
        assert f"sha256-{digest}" in unescape(html)
        assert "connect-src https://owned.example/e" in html


@pytest.mark.parametrize("host", ["inventory.example", "gardening.example"])
def test_local_measurement_reachability_probe_uses_only_guarded_head(monkeypatch, host):
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from core.research_web.guard import pin_request
    from app.pagebuilder import local_publish

    state = {"status": 204, "headers": {"X-Page-Measurement": "local"}}
    calls = []
    class Client:
        def __init__(self, **options):
            assert options["trust_env"] is False and options["follow_redirects"] is False
            assert options["event_hooks"] == {"request": [pin_request]}

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def head(self, url):
            calls.append(url)
            return SimpleNamespace(status_code=state["status"], headers=state["headers"])

    monkeypatch.setattr(local_publish.httpx, "Client", Client)
    url = f"https://{host}/e"
    assert local_publish._events_reachable(url)
    state["status"] = 404
    assert not local_publish._events_reachable(url)
    state.update(status=204, headers={})
    assert not local_publish._events_reachable(url)
    assert calls == [url, url, url]


@pytest.mark.usefixtures("measurement_local_owner")
@pytest.mark.parametrize("base_path", ["", "/pages"])
def test_local_measurement_api_and_chat_routes(market, monkeypatch, base_path):
    from fastapi.testclient import TestClient
    from core.clock import utcnow
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.apps.keys import ensure_ingest_key
    from app.api import events_ingest
    from app.core import local_owner
    from app.core import local_secrets
    from app.experiments import page_tests

    assert ACTION_SLUG["experiment.measurement.record"] == "experiments"
    spec = local_measurement_spec(market, monkeypatch, "headlines")
    saved_settings = local_secrets.read_settings
    monkeypatch.setattr(local_secrets, "read_settings", lambda section: {"values": {"site_url": "https://owned.example" + base_path}, "sources": {"site_url": "app"}, "secrets_set": {}}
        if section == "publishing" else saved_settings(section))
    endpoint_path = base_path + "/e"
    key = ensure_ingest_key(spec["app_id"])["secret"]
    remote = TestClient(market[0].app, base_url="https://owned.example", client=("203.0.113.9", 50100))
    headers = {"origin": "https://published.example", "sec-fetch-site": "cross-site", "content-type": "text/plain",
        "x-subject": "system", "x-cos-assertion": "owner-assertion", "authorization": "Bearer owner-credential",
        "x-api-key": "owner-api-key", "x-workspace-id": market[2], "cookie": "supymem_owner=owner-cookie",
        "x-local-proxy-secret": "owner-session"}
    track = events_ingest.track
    async def anonymous_track(request):
        assert request.state.local_owner is None and local_owner._OWNER.get() is None
        assert not any(name in request.headers for name in ("x-subject", "x-cos-assertion", "authorization",
            "x-api-key", "x-workspace-id", "cookie", "x-local-proxy-secret"))
        return await track(request)
    monkeypatch.setattr(events_ingest, "track", anonymous_track)
    probe = remote.head(endpoint_path, headers=headers)
    assert probe.status_code == 204 and probe.headers["X-Page-Measurement"] == "local"
    if base_path:
        assert remote.head("/e", headers=headers).status_code == 403
    assert remote.post(endpoint_path, content=json.dumps({"key": key, "event": "landing_view", "props": {"path": "/", "variant": "alpha", "experiment_id": spec["id"]}}), headers=headers).status_code == 200
    assert remote.post(endpoint_path, content=json.dumps({"key": "pk_app_unknown", "event": "landing_view"}), headers=headers).status_code == 204
    assert remote.post(endpoint_path, content="not-json", headers=headers).status_code == 204
    assert remote.post(endpoint_path, content="{}", headers={**headers, "host": "other.example"}).status_code == 403
    assert remote.post(endpoint_path, content="{}", headers=[("host", "owned.example"), ("host", "owned.example")]).status_code == 403
    assert remote.get(endpoint_path, headers=headers).status_code == 403
    assert remote.get(f"/ventures/{market[1]}", headers=headers).status_code == 403
    path = f"/ventures/{market[1]}/measurement/counts"
    payload = {"experiment_id": spec["id"], "path": "/", "variant": "alpha", "views": 100, "clicks": 20, "signups": 10,
        "start_date": utcnow().date().isoformat(), "end_date": utcnow().date().isoformat()}
    response = market[0].post(path, json=payload)
    assert response.status_code == 200, response.text
    assert market[0].post(path, json={**payload, "views": -1}).status_code == 422
    response = market[0].post(f"/ventures/{market[1]}/chat", json={"message": "Record page counts: " + json.dumps({**payload, "variant": "beta", "signups": 5})})
    assert response.status_code == 200, response.text
    assert "Your page counts are saved" in response.json()["body_md"]
    assert not any(action.get("confirmation") for action in response.json()["actions"])
    assert page_tests.observe(spec)["measured"]["signup_rate_ratio"] == 2
    response = market[0].post(f"/ventures/{market[1]}/chat", json={"message": "Record page counts: []"})
    assert response.status_code == 200 and "Record your page counts" in response.json()["body_md"]
    local_secrets.update_settings("capabilities", {"page_measurement": False}, {})
    response = market[0].post(path, json=payload)
    from app.core.local_capabilities import blocked_message

    assert response.status_code == 422
    assert response.json()["detail"] == blocked_message("page_measurement")
    assert remote.head(endpoint_path, headers=headers).status_code == 403


@pytest.mark.usefixtures("measurement_local_owner")
def test_local_measurement_counts_reject_invalid_bodies_without_echoing_them(market):
    marker = market[3]["icp"]["problem"]
    for body, status in ((json.dumps([marker]), 422), ("[" + json.dumps(marker), 422),
            (json.dumps({"path": marker * 1000}), 413)):
        response = market[0].post(f"/ventures/{market[1]}/measurement/counts", content=body,
            headers={"content-type": "application/json"})
        assert response.status_code == status
        assert isinstance(response.json()["detail"], str)
        assert "Record your page counts" in response.json()["detail"]
        assert marker not in response.text


def consent_payload(venture_id, email, **attribution):
    from app.conversion import waitlist
    details = waitlist.public_details(venture_id)
    return {"email": email, "consent": True, "consent_version": waitlist.VERSION,
        "consent_digest": hashlib.sha256(details["consent"].encode()).hexdigest(), **attribution}


def configure(market, kind, variants=()):
    from app.experiments import page_tests
    client, venture_id, workspace_id, view, landing, *_rest = market
    landing["enabled"] = True
    payload = {"kind": kind, "variants": list(variants)}
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": "experiment.page.prepare", "payload": payload})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert "credit" not in prepared["body_md"].lower()
    if kind == "survey":  # founder decision: a requested test starts without a card
        assert not any((action.get("confirmation") or {}).get("action", "outreach.send") != "outreach.send" for action in prepared["actions"])
    else:
        assert not any((action.get("confirmation") or {}).get("action", "outreach.send") != "outreach.send" for action in prepared["actions"])
    return page_tests.available({"venture_id": venture_id, "workspace_id": workspace_id,
        "direction": page_tests._spec(venture_id, payload)["direction"]})[-1]


@pytest.mark.parametrize("kind", ["headlines", "pricing", "survey"])
@pytest.mark.usefixtures("connected_coding_tool")
def test_experiments_chat_prepares_variants_from_saved_context(market, kind, monkeypatch):
    from app.analytics.feature_catalog import ACTION_SLUG
    from app.chat.actions import get_action
    from app.db.supymem_models import Experiment
    from app.pagebuilder import api
    from app.state import db

    client, venture_id, _workspace_id, view, landing, _provider, calls, *_rest = market
    monkeypatch.setattr(api, "enabled", lambda: True)
    landing["enabled"] = True
    if kind == "pricing":
        from app.experiments import autonomous
        spec = next(item for item in autonomous.available(venture_id) if item["scan"] == "pricing")
        with db()() as session:
            row = session.get(Experiment, spec["id"])
            row.state = "concluded"
            row.context = json.dumps({**json.loads(row.context), "result": {"insight": "Saved public price anchors", "sources": [
                {"url": "https://alternatives.example/pricing", "title": view["icp"]["who"], "price_points": [
                    {"price": "$9", "billing_period": "month"}, {"price": "$24", "billing_period": "month"}]}]}})
            session.commit()
    listed = client.post(f"/ventures/{venture_id}/chat", json={"message": "show my experiments"})
    assert listed.status_code == 200, listed.text
    entry = next(action for action in listed.json()["actions"] if action.get("action") == "experiment.page.guide")
    guide = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": entry["action"], "payload": {}})
    assert guide.status_code == 200, guide.text
    options = guide.json()["actions"]
    assert {action["payload"]["kind"] for action in options} == {"headlines", "pricing", "survey"}
    assert all(action["action"] == "experiment.page.prepare" for action in options)
    for name in (entry["action"], "experiment.page.prepare", "experiment.page.configure", "experiment.page.readout"):
        assert get_action(name) and ACTION_SLUG[name] == "experiments"
    option = next(action for action in options if action["payload"]["kind"] == kind)
    response = client.post(f"/ventures/{venture_id}/chat/confirm", json={"action": option["action"], "payload": option["payload"]})
    assert response.status_code == 200, response.text
    prepared = response.json()
    assert "credit" not in prepared["body_md"].lower()
    with db()() as session:
        saved = [row for row in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id))
                 if json.loads(row.context or "{}").get("page_test")]
        assert len(saved) == 1
        variants = json.loads(saved[0].context)["spec"]["variants"] if saved else []
    if kind != "survey":
        assert len(variants) == 2
        if kind == "headlines":
            assert all(view["icp"]["problem"] in variant["headline"] for variant in variants)
        else:
            assert [variant["price"] for variant in variants] == ["$9 per month", "$24 per month"]
    assert not any((action.get("confirmation") or {}).get("action", "outreach.send") != "outreach.send" for action in prepared["actions"])
    assert not calls


def build(spec, market, monkeypatch):
    from app.conversion import waitlist
    from app.pagebuilder import images, models, pipeline, tool
    from app.pagebuilder.models import BuiltPage, PageInput
    from app.experiments import page_tests
    from app.state import db
    from app.db.models import Venture
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan, fixture_html

    draft_inputs, draft = customer_draft("b2b" if market[3]["icp"]["mode"] == "company" else "consumer")
    venture_id = market[1]
    with db()() as session:
        row = session.get(Venture, venture_id)
        inputs = PageInput(venture_id=venture_id, name=row.name, idea=draft_inputs["idea"],
            waitlist_url=f"https://interest.example/public/waitlists/{venture_id}", waitlist_consent_text=waitlist.consent_text(row.name),
            page_test=spec["runner_params"]["page_test"])
    plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    plan = page_tests.decorate_plan(plan, inputs)
    html = fixture_html(plan, inputs)
    with monkeypatch.context() as source_fixture:
        source_fixture.setenv("SUPYMEM_MODE", "hosted")
        html = page_tests.native_sections(html, inputs, plan)
        files = {"index.html": html, **page_tests.variant_files(html, inputs, plan),
            **{asset.path: asset.svg for asset in plan.assets}}
    from html import escape
    for price in inputs.page_test.prices:
        files[f"price_{price.key}.html"] = (f"<main><h1>{escape(price.label)}: {escape(price.price)}</h1>"
            f"<p>{escape(page_tests.DISCLOSURE)}</p><a href=\"{escape(page_tests.waitlist_target(inputs), quote=True)}\">"
            f"{escape(plan.brief.cta)}</a></main>")
    from tests.unit.pagebuilder.test_tool_build import _with_social_preview
    checked_files = _with_social_preview({name: body.encode() for name, body in files.items()}, inputs)
    checked = tool.check_output(checked_files, inputs, plan)
    assert checked["passed"], checked["failures"]
    files = {name: body.decode() if name in files else body for name, body in checked_files.items()}
    page = BuiltPage(files=files, manifest={"built_with_coding_tool": True, "qa_contract": checked["contract"]},
        brief=plan.brief, direction=plan.direction, sections=plan.sections, assets=plan.assets, qa=checked)
    return inputs, page


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
@pytest.mark.parametrize("kind", ["headlines", "pricing"])
def test_local_experiment_plan_keeps_the_customer_action_last(monkeypatch, domain, kind):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import pipeline
    from tests.unit.pagebuilder.test_pipeline import customer_draft

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    inputs, draft = customer_draft(domain)
    variants = ([{"key": "first", "label": "First perspective", "headline": draft["sections"][0]["headline"]},
        {"key": "second", "label": "Second perspective", "headline": draft["brief"]["promise"]}]
        if kind == "headlines" else [{"key": "first", "label": "First price", "price": "$9 per month"},
            {"key": "second", "label": "Second price", "price": "$24 per month"}])
    inputs["page_test"] = {"experiment_id": "exp_" + "1" * 24, kind if kind == "headlines" else "prices": variants}
    plan = pipeline.compile_draft_plan(inputs, draft)
    assert [section.id for section in plan.sections] == ["hero", "problem", "how-it-works", "audience", "faq",
        "personas" if kind == "headlines" else "pricing", "waitlist"]


@pytest.mark.parametrize("domain", ["b2b", "consumer"])
def test_local_variant_contract_preserves_both_signup_actions(monkeypatch, domain):
    from tests.company_os_harness import load_company_os_app

    load_company_os_app()
    from app.pagebuilder import images, models, pipeline, qa
    from app.experiments import page_tests
    from tests.unit.pagebuilder.test_pipeline import customer_draft, final_plan

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    inputs, draft = customer_draft(domain)
    inputs["page_test"] = {"experiment_id": "exp_" + "1" * 24, "headlines": [
        {"key": "first", "label": "First perspective", "headline": draft["sections"][0]["headline"]},
        {"key": "second", "label": "Second perspective", "headline": draft["brief"]["promise"]}]}
    inputs = models.PageInput.model_validate(inputs)
    plan = final_plan({"models": models, "pipeline": pipeline, "images": images}, inputs, draft)
    contract = qa.qa_contract(inputs, page_tests.decorate_plan(plan, inputs))
    for index, variant in enumerate(inputs.page_test.headlines):
        actions = [action for action in contract.actions if action.id in {f"v{index}_hero_cta", f"v{index}_waitlist_cta"}]
        assert len(actions) == 2
        assert all(action.kind == "external_link" for action in actions)
        assert all(action.target == page_tests.waitlist_target(inputs, variant.key) for action in actions)


def record(spec, name, count, **props):
    from app.db.supymem_models import AppEvent
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        session.add_all(AppEvent(app_id=spec["app_id"], workspace_id=spec["workspace_id"], ts=utcnow(), name=name,
            props=json.dumps({"experiment_id": spec["id"], **props})) for _index in range(count))
        session.commit()


def test_variant_capture_and_beacon_keep_first_touch_without_personal_data(market, monkeypatch):
    from app.conversion import waitlist
    from app.db.supymem_models import AppEvent, VentureWaitlistEntry
    from app.product_analytics.beacon import script_tag
    from app.state import db
    client, venture_id, workspace_id, *_rest = market
    attrs = {"variant": "persona_a", "utm_campaign": "persona_a", "experiment_id": "exp_" + "1" * 24}
    payload = consent_payload(venture_id, "reader@interest-mail.example", **attrs)
    response = client.get(f"/public/waitlists/{venture_id}", params=attrs)
    from app.core.local_mode import is_local
    if is_local():
        assert response.status_code == 404 and "Coding tools" in response.text and "<form" not in response.text
    else:
        assert response.status_code == 200 and 'name="variant" value="persona_a"' in response.text
    assert client.post(f"/public/waitlists/{venture_id}", json=payload).status_code == 201
    assert waitlist.capture(venture_id, {**payload, "variant": "persona_b"})["received"]
    with db()() as session:
        entry = session.scalar(select(VentureWaitlistEntry).where(VentureWaitlistEntry.venture_id == venture_id))
        stored = json.loads(entry.props)
        assert re.fullmatch(r"[A-Za-z0-9_-]{32}", stored.pop("ref_code"))
        assert stored == attrs
        events = session.scalars(select(AppEvent).where(AppEvent.workspace_id == workspace_id, AppEvent.name == "signup")).all()
        assert len(events) == 1 and all(json.loads(events[0].props)[key] == value for key, value in attrs.items())
    with pytest.raises(waitlist.WaitlistError):
        waitlist.capture(venture_id, {**payload, "variant": "private value@example.org"})
    with monkeypatch.context() as hosted:
        hosted.delenv("SUPYMEM_MODE", raising=False)
        script = script_tag(key="pk_app_test", endpoint="https://gateway.example/e").split('data-supymem-analytics="1">', 1)[1].rsplit("</script>", 1)[0]
    target = f"https://gateway.example/public/waitlists/{venture_id}"
    harness = """
const vm = require('node:vm');
const emitted = []; let listener;
const location = new URL('https://site.example/variant_persona_a.html?utm_campaign=shared_a');
const link = {href: TARGET, getAttribute: () => null};
const document = {referrer: '', querySelectorAll: () => [], querySelector: () => ({href: TARGET + '?experiment_id=exp_' + '1'.repeat(24) + '&variant=persona_a'}), addEventListener: (_event, handler) => {listener = handler;}};
const navigator = {sendBeacon: (_url, body) => {emitted.push(body);}};
const context = {location, document, navigator, window: {}, URL, URLSearchParams, Blob};
vm.runInNewContext(SCRIPT, context);
listener({target: {closest: () => link}});
Promise.all(emitted.map(async body => JSON.parse(await body.text()))).then(events => console.log(JSON.stringify({events, href:link.href})));
""".replace("TARGET", json.dumps(target)).replace("SCRIPT", json.dumps(script))
    result = subprocess.run(["node", "-e", harness], check=True, capture_output=True, text=True)
    observed = json.loads(result.stdout)
    assert [event["event"] for event in observed["events"]] == ["landing_view", "cta_click"]
    assert all(event["props"]["variant"] == "persona_a" for event in observed["events"])
    assert "utm_campaign=shared_a" in observed["href"] and "reader@" not in result.stdout


@pytest.mark.usefixtures("connected_coding_tool")
def test_headline_pages_are_identical_except_heading_and_attribution_and_need_each_sample(market, monkeypatch):
    from app.core.local_mode import is_local
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests
    from app.state import db
    from app.conversion import waitlist
    who = market[3]["icp"]["who"]
    variants = [{"key": "persona_a", "label": "First perspective", "headline": f"Plan ahead with {who}"},
        {"key": "persona_b", "label": "Second perspective", "headline": f"Coordinate with {who}"}]
    spec = configure(market, "headlines", variants)
    inputs, page = build(spec, market, monkeypatch)
    for variant in variants:
        html = page.files[f"variant_{variant['key']}.html"]
        assert f"<h1>{variant['headline']}</h1>" in html
        assert f"variant={variant['key']}" in html and f"utm_campaign={variant['key']}" in html
    from app.api import pagebuilder_api
    from app.pagebuilder.jobs import encode_bundle
    encoded = encode_bundle(page, inputs)
    monkeypatch.setattr(pagebuilder_api, "bundle", lambda *_args: encoded)
    preview = pagebuilder_api.preview(market[1], "job", 0, None, "variant_persona_b.html")
    assert f"<h1>{variants[1]['headline']}</h1>" in preview.body.decode()
    assert "page_name=variant_persona_a.html" in preview.body.decode()
    def normalized(html, index):
        html = re.sub(r"<h1[^>]*>.*?</h1>", "<h1>HEADLINE</h1>", html)
        html = re.sub(r"v" + str(index) + "_", "", html)
        return re.sub(r"(variant|utm_campaign)=persona_[ab]", r"\1=VARIANT", html)
    assert normalized(page.files["variant_persona_a.html"], 0) == normalized(page.files["variant_persona_b.html"], 1)
    if is_local():
        _stub_checked_publication(spec, monkeypatch, site_url="https://interest.example/")
    record(spec, "landing_view", 49, variant="persona_a", path="/variant_persona_a.html")
    record(spec, "landing_view", 50, variant="persona_b", path="/variant_persona_b.html")
    for variant, count in (("persona_a", 10), ("persona_b", 4)):
        for index in range(count):
            waitlist.capture(market[1], consent_payload(market[1], f"{variant}{index}@interest-mail.example",
                variant=variant, utm_campaign=variant, experiment_id=spec["id"]))
    before_threshold = page_tests.observe(spec)
    assert before_threshold["outcome"] == "inconclusive" and before_threshold["measured"]["events"] == 49
    record(spec, "landing_view", 1, variant="persona_a", path="/variant_persona_a.html")
    result = page_tests.observe(spec)
    assert result["outcome"] == "supported" and result["measured"]["events"] == 50
    assert result["measured"]["signup_rate_ratio"] == 2.5
    assert "Measured signup rate ratio: 2.5" in result["insight"] and "Next:" in result["insight"]
    assert [(item["views"], item["signups"]) for item in result["by_variant"]] == [(50, 10), (50, 4)]
    assert "50" in page_tests.readout(market[1], {"experiment_id": spec["id"]})["body_md"]
    triple = variants + [{"key": "persona_c", "label": "Third perspective", "headline": f"Arrange the next task with {who}"}]
    with db()() as session:
        session.get(Experiment, spec["id"]).state = "concluded"
        session.commit()
    triple_spec = configure(market, "headlines", triple)
    _inputs, triple_page = build(triple_spec, market, monkeypatch)
    assert len([name for name in triple_page.files if name.startswith("variant_")]) == 3


@pytest.mark.usefixtures("connected_coding_tool")
def test_pricing_discloses_before_data_and_counts_each_price_against_threshold(market, monkeypatch):
    from app.experiments import page_tests
    from app.pagebuilder.qa import honest_copy
    variants = [{"key": "lower", "label": "Lower proposal", "price": "USD 12 per month"},
        {"key": "middle", "label": "Middle proposal", "price": "USD 24 per month"},
        {"key": "higher", "label": "Higher proposal", "price": "USD 48 per month"}]
    with pytest.raises(ValueError):
        page_tests.prepare(market[1], {"kind": "pricing", "variants": [variants[0],
            {**variants[1], "price": "USD 12.00 per month"}]})
    spec = configure(market, "pricing", variants)
    inputs, page = build(spec, market, monkeypatch)
    assert page_tests.DISCLOSURE in page.files["index.html"]
    assert "fake_door_availability_claim" in honest_copy("Buy now and get instant access.", inputs)
    path = f"/public/waitlists/{market[1]}/pricing/{spec['id']}/middle"
    assert market[0].get(path).status_code == 404
    from app.db.supymem_models import BuildRun, Deployment
    from app.pagebuilder_admission import ORIGIN, TRIGGER, publication
    from app.state import db
    from core.clock import utcnow
    with db()() as session:
        run = BuildRun(id="br_pricing", app_id=spec["app_id"], venture_id=market[1], workspace_id=market[2],
            trigger=TRIGGER, agent="pagebuilder", state="succeeded", artifact_id="artifact_pricing", request_digest="source")
        saved = {"source_origin": ORIGIN, "lifecycle_version": 3, "actor": "local",
            "source_import": {"identity": {"scope": {"caller_subject": "local"}, "hostname": "interest.example"}},
            "runtime": {"qa_compute_ceiling_micro": 1}, "inputs": {"pagebuilder_input": inputs.model_dump(mode="json")}}
        saved["publication_request"] = {"schema_version": 1, "binding": publication._binding(run, saved)}
        run.spec = json.dumps(saved)
        session.add(run)
        session.add(Deployment(id="dep_pricing", app_id=spec["app_id"], venture_id=market[1], workspace_id=market[2],
            build_artifact_id=run.artifact_id, target="supymem_static", state="live", version=1,
            url="https://interest.example/", live_at=utcnow()))
        session.commit()
    response = market[0].get(path)
    from app.core.local_mode import is_local
    if is_local():
        assert response.status_code == 404 and "Coding tools" in response.text
        assert "<form" not in page.files["index.html"] and "<input" not in page.files["index.html"]
        assert "payment" in page.files["index.html"]
    else:
        assert response.status_code == 200 and page_tests.DISCLOSURE in response.text
        assert "<form" not in response.text and "<input" not in response.text and "payment" in response.text
    with db()() as session:
        run = session.get(BuildRun, "br_pricing")
        saved["publication_request"]["binding"]["hostname"] = "unapproved.example"
        run.spec = json.dumps(saved)
        session.commit()
    assert market[0].get(path).status_code == 404
    if is_local():
        _stub_checked_publication(spec, monkeypatch, site_url="https://interest.example/")
    record(spec, "landing_view", 99, path="/")
    record(spec, "cta_click", 6, price_point="lower", price=variants[0]["price"])
    record(spec, "cta_click", 2, price_point="middle", price=variants[1]["price"])
    record(spec, "cta_click", 50, price_point="unapproved")
    before_threshold = page_tests.observe(spec)
    assert before_threshold["outcome"] == "inconclusive" and before_threshold["measured"]["events"] == 99
    record(spec, "landing_view", 1, path="/")
    result = page_tests.observe(spec)
    assert result["outcome"] == "supported" and result["measured"]["cta_click_rate"] == .08
    assert "Measured cta click rate: 0.08" in result["insight"] and "Next:" in result["insight"]
    assert [item["clicks"] for item in result["by_variant"]] == [6, 2, 0]


@pytest.mark.usefixtures("connected_coding_tool")
def test_waitlist_survey_is_one_off_consented_scoped_and_counts_only_once(market, monkeypatch):
    from app.autopilot import outreach
    from app.conversion import waitlist
    from app.db.models import GtmContact, GtmSuppression, GtmOutreachSettings
    from app.db.supymem_models import AppEvent, Experiment, VentureWaitlistEntry
    from app.experiments import page_tests
    from app.gtm.relationships.email_compliance import OPT_OUT, validate_send
    from app.product_analytics import service
    from app.state import db
    from core.clock import utcnow
    venture_id, workspace_id = market[1:3]
    for index in range(2):
        waitlist.capture(venture_id, consent_payload(venture_id, f"consented{index}@interest-mail.example"))
    with db()() as session:
        session.add(VentureWaitlistEntry(id="probe", venture_id=venture_id, workspace_id=workspace_id,
            email="probe@qa.invalid", is_test=True, consent_version=waitlist.VERSION,
            consent_text=waitlist.consent_text("Probe"), created_at=utcnow()))
        session.add(GtmSuppression(id="suppressed", workspace_id=workspace_id, email="consented1@interest-mail.example",
            reason="opt_out", source="opt_out", created_at=utcnow()))
        settings = session.get(GtmOutreachSettings, workspace_id)
        if settings is None:
            settings = GtmOutreachSettings(workspace_id=workspace_id, created_at=utcnow(), updated_at=utcnow())
            session.add(settings)
        settings.sender_postal_address = "42 Orchard Street, Springfield, US"
        session.commit()
        from app.core.local_mode import is_local
        if is_local():
            from app.core.local_secrets import update_settings
            from secrets import token_urlsafe
            update_settings("smtp", {"host": "127.0.0.1", "user": "you@owned.example",
                "postal_address": settings.sender_postal_address, "sender_name": outreach.founder_signature(venture_id)},
                {"password": token_urlsafe(24)})
    sent = []
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: sent.append(_args))
    spec = configure(market, "survey")
    if is_local():
        assert not outreach.drafts(venture_id)
        page_tests.draft_mail(venture_id, spec["id"])
    drafts = outreach.drafts(venture_id)
    assert len(drafts) == 1 and not sent and drafts[0]["requires_batch_approval"] and drafts[0]["one_off"]
    assert OPT_OUT in drafts[0]["body"] and "42 Orchard Street" in drafts[0]["body"]
    assert "I have your address from my own contacts." in drafts[0]["body"]
    with db()() as session:
        contact = session.get(GtmContact, drafts[0]["contact_id"])
        with pytest.raises(ValueError, match="unknown_jurisdiction"):
            validate_send(session, workspace_id, contact, drafts[0]["body"], outreach.founder_signature(venture_id))
        contact.recipient_geo = "US"
        session.commit()
        validate_send(session, workspace_id, contact, drafts[0]["body"], outreach.founder_signature(venture_id))
    assert page_tests.draft_mail(venture_id, spec["id"])["count"] == 1 and len(outreach.drafts(venture_id)) == 1
    link = re.search(re.escape(waitlist.public_url(venture_id)) + r"/surveys/\S+", drafts[0]["body"])
    assert link is not None
    path = urlsplit(link.group()).path
    response = market[0].get(path)
    if is_local():
        assert response.status_code == 404 and "Coding tools" in response.text
    else:
        assert response.status_code == 200
    assert market[0].get(path.replace(venture_id, "foreign")).status_code == 404
    if is_local():
        assert market[0].post(path, json={"choice": "very", "benefit": "Planning ahead"}).status_code == 422
        target = "https://owned-site.example/survey.html"
        monkeypatch.setattr(page_tests, "checked_public_url", lambda *_args, **_kwargs: target)
        assert market[0].get(path, follow_redirects=False).headers["location"] == target
    for _index in range(2):
        response = market[0].post(path, json={"choice": "very", "benefit": "Planning ahead"})
        assert response.status_code == 201, response.text
    assert market[0].post(path, json={"choice": "other"}).status_code == 422
    assert market[0].post(path, json={"choice": ["very"]}).status_code == 422
    with db()() as session:
        rows = session.scalars(select(AppEvent).where(AppEvent.name == "survey_response")).all()
        assert len(rows) == 1 and json.loads(rows[0].props)["choice"] == "very"
        assert "consented0" not in rows[0].props and "Planning ahead" not in rows[0].props
    assert page_tests.observe(spec)["outcome"] == "inconclusive"
    record(spec, "survey_response", 11, choice="very", source="consented_waitlist")
    record(spec, "survey_response", 18, choice="somewhat", source="consented_waitlist")
    assert page_tests.observe(spec)["outcome"] == "supported"
    assert page_tests.observe(spec)["measured"]["survey_very_share"] == .4
    assert "directional" in page_tests.readout(venture_id, {"experiment_id": spec["id"]})["body_md"]
    from app.apps.keys import ensure_ingest_key
    assert service.ingest_public({"key": ensure_ingest_key(spec["app_id"])["secret"], "event": "survey_response", "props": {"choice": "very"}}) is False
    metered_calls = []
    def meter(callback, **kwargs):
        metered_calls.append(kwargs)
        receipt = callback()
        kwargs["before_meter_exit"](receipt, {"actual_micro": kwargs["actual_micro"](receipt)})
        return receipt
    monkeypatch.setattr("app.approvals.external.outbound_rate", lambda *_args: SimpleNamespace(amount_micro=7))
    monkeypatch.setattr("app.credits.metered.call_sync", meter)
    monkeypatch.setattr(outreach, "_send_one", lambda *_args: {"provider_message_id": "confirmed_message", "thread_id": "thread"})
    assert page_tests.send_one(venture_id, workspace_id, {**drafts[0], "credit_ceiling_micro": 7}, {})["provider_message_id"] == "confirmed_message"
    assert len(metered_calls) == 1 and metered_calls[0]["estimate_micro"] == 7
    assert metered_calls[0]["purpose"] == "experiment.waitlist_mail"
    with db()() as session:
        contact = session.get(GtmContact, drafts[0]["contact_id"])
        contact.do_not_contact = True
        session.commit()
    assert market[0].get(path).status_code == 404


@pytest.mark.parametrize("entry", ["prepare", "configure"])
@pytest.mark.parametrize("connected", [False, True], ids=["connect-first", "owned-tool"])
@pytest.mark.parametrize("kind", ["headlines", "pricing", "survey", "request"])
def test_local_experiment_build_routes_to_owned_tool(market, monkeypatch, entry, connected, kind):
    from app.build import device_orders
    from app.core import local_owner
    from app.db.supymem_models import Experiment
    from app.experiments import page_tests
    from app.pagebuilder import api
    from app.state import db

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    monkeypatch.setattr(local_owner, "current_owner", local_owner.ensure_owner)
    monkeypatch.setattr(page_tests, "_connected_provider", lambda *_: "codex" if connected else None, raising=False)
    if not connected:
        from app.core import local_capabilities
        capability_calls = []
        def page_capability(name):
            capability_calls.append(name)
            return {"state": "off", "title": "Landing pages"}
        monkeypatch.setattr(local_capabilities, "capability", page_capability)
    market[4]["enabled"] = True
    audience, problem = market[3]["icp"]["who"], market[3]["icp"]["problem"]
    payload = {"kind": kind, "variants": []}
    if kind == "headlines":
        payload["variants"] = [{"key": "focus", "label": audience, "headline": problem},
            {"key": "alternative", "label": "Another perspective", "headline": "A simpler way to " + problem}]
    elif kind == "pricing":
        payload["variants"] = [{"key": "offer", "label": audience, "price": "$18 per month"}]
    elif kind == "request":
        payload = {"kind": kind, "fields": [{"key": "task", "label": problem, "kind": "short_text"},
            {"key": "email", "label": "Your email", "kind": "email"}]}
    spec = page_tests._spec(market[1], payload)
    if entry == "configure":
        payload["fingerprint"] = spec["fingerprint"]
    calls, approvals = [], []

    def prepare_order(venture_id, caller, instruction, **kwargs):
        brief = page_tests.coding_brief(venture_id, kwargs["experiment_id"])
        calls.append((venture_id, caller, instruction, brief))
        return {"id": "dwo_experiment", "state": "pending", "review_digest": "review"}

    def approve(*args):
        approvals.append(args)
        return {"id": "dwo_experiment", "state": "approved"}

    monkeypatch.setattr(device_orders, "prepare_coding_task_from_chat", prepare_order)
    monkeypatch.setattr(device_orders, "transition", approve)
    def prepare_checked_page(venture_id, caller, *, provider, experiment_id):
        order = prepare_order(venture_id, caller, provider, experiment_id=experiment_id)
        approve(venture_id, caller, order["id"], order["review_digest"], "approve")
        return {"order_id": order["id"], "state": "running"}
    monkeypatch.setattr(api, "_start", prepare_checked_page)
    reply = getattr(page_tests, entry)(market[1], payload)
    assert not any(action.get("confirmation") for action in reply["actions"])
    with db()() as session:
        row = session.get(Experiment, spec["id"])
        if not connected:
            assert "Settings > Keys and connections > Coding tools" in reply["body_md"]
            assert "Settings > Capabilities" in reply["body_md"] and reply["body_md"].count(".") == 1
            assert capability_calls[-1] == "landing_pages"
            assert not calls and not approvals and row is None
        else:
            assert len(calls) == len(approvals) == 1
            assert calls[0][0] == market[1] and "codex" in calls[0][2]
            brief = calls[0][3]
            assert audience in json.dumps(brief) and brief["experiment_id"] == spec["id"]
            assert brief["submission"]["consent"] and brief["submission"]["consent_digest"]
            assert brief["measurement"]["metric"] == spec["metric"]
            assert brief["variants"] == spec["variants"]
            from app.db.models import Venture
            from app.ventures.current_idea import current_idea
            assert brief["idea"] == current_idea(session.get(Venture, market[1]), session=session)
            assert row.state == "proposed" and not row.started_at
            context = json.loads(row.context)
            assert context["device_order_id"] == "dwo_experiment" and not context["survey_published"]
            assert any("device-work-orders/dwo_experiment" in action.get("href", "") for action in reply["actions"])


@pytest.mark.parametrize("route", ["waitlist", "request", "pricing", "survey"])
@pytest.mark.parametrize("published", [False, True], ids=["no-tool-page", "checked-tool-page"])
def test_local_public_experiment_pages_use_checked_tool_pages(market, monkeypatch, route, published):
    from app.api import waitlist_api
    from app.conversion import waitlist
    from app.experiments import page_tests, wizard_tests

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    target = "https://owned-site.example/" + route + ".html"
    calls = []

    def checked(*args, **kwargs):
        calls.append((args, kwargs))
        if not published:
            raise ValueError("missing checked tool page")
        return target

    monkeypatch.setattr(page_tests, "checked_public_url", checked, raising=False)
    monkeypatch.setattr(page_tests, "public_price", lambda *_: {"label": market[3]["icp"]["who"], "price": "$18"})
    monkeypatch.setattr(page_tests, "survey", lambda *_: {"name": market[3]["icp"]["who"],
        "question": market[3]["icp"]["problem"], "choices": page_tests.CHOICES})
    monkeypatch.setattr(wizard_tests, "public_form", lambda *_: (market[3]["icp"]["who"], checked()))
    if route == "pricing":
        response = waitlist_api.pricing_disclosure(market[1], "exp_test", "offer")
    elif route == "survey":
        response = waitlist_api.public_survey(market[1], "exp_test", "recipient-token")
    else:
        response = waitlist_api.public_waitlist(market[1], kind="request" if route == "request" else "",
            experiment_id="exp_test", variant="focus")
    assert response.status_code == (303 if published else 404)
    if published:
        assert response.headers["location"] == target
    else:
        assert "Settings > Keys and connections > Coding tools" in unescape(response.body.decode())
    assert len(calls) == 1
    assert b"<form" not in response.body


@pytest.mark.parametrize("helper", ["native_sections", "variant_files"])
@pytest.mark.parametrize("kind", ["headlines", "pricing"])
def test_local_experiment_helpers_check_without_generating(market, monkeypatch, helper, kind):
    from app.experiments import page_tests
    from app.pagebuilder.models import PageInput
    from app.pagebuilder.pipeline import fallback_plan

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    test = {"experiment_id": "exp_" + "a" * 24,
        "headlines": [{"key": "focus", "label": "Perspective", "headline": market[3]["icp"]["problem"]},
            {"key": "alternative", "label": "Another perspective", "headline": "A simpler approach"}]} if kind == "headlines" else {
                "experiment_id": "exp_" + "a" * 24, "prices": [{"key": "offer", "label": market[3]["icp"]["who"], "price": "$18 per month"}]}
    inputs = PageInput(venture_id=market[1], name=market[3]["icp"]["who"], idea=market[3]["icp"]["problem"],
        waitlist_url="https://owned-site.example/public/waitlists/" + market[1], page_test=test)
    monkeypatch.setenv("SUPYMEM_MODE", "hosted")
    plan = fallback_plan(inputs)
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    with pytest.raises(ValueError, match="Coding tools"):
        getattr(page_tests, helper)("<main><h1>Unreviewed</h1></main>", inputs, plan)
    from app.pagebuilder import qa
    monkeypatch.setattr(qa, "check_page", lambda *_: {"passed": True})
    if helper == "native_sections":
        source = "<main><h1>Checked source</h1></main>"
        assert page_tests.native_sections(source, inputs, plan) == source
    elif kind == "pricing":
        from html import escape
        price, = inputs.page_test.prices
        source = f'<main><h1>{escape(price.label)}</h1><p>{escape(price.price)}</p><p>{page_tests.DISCLOSURE}</p><a href="{escape(page_tests.waitlist_target(inputs), quote=True)}">Join the waitlist</a></main>'
        files = {"price_offer.html": source}
        assert page_tests.variant_files("checked index", inputs, plan, tool_files=files) == files
        for unsafe in (source.replace(page_tests.DISCLOSURE, ""), source.replace("</main>", '<form><input name="card"></form></main>')):
            with pytest.raises(ValueError, match="disclosure|collection"):
                page_tests.variant_files("checked index", inputs, plan, tool_files={"price_offer.html": unsafe})
        for navigation in ('<a href="https://payments.example/checkout">Buy now</a>',
                '<meta http-equiv="refresh" content="5;url=https://payments.example/checkout">'):
            with pytest.raises(ValueError, match="pricing pages"):
                page_tests.variant_files("checked index", inputs, plan, tool_files={"price_offer.html": source + navigation})
    else:
        from html import escape
        contract = qa.qa_contract(inputs, plan)
        files = {}
        for variant in inputs.page_test.headlines:
            filename = f"variant_{variant.key}.html"
            links = "".join(f'<a id="{action.id}" href="{escape("#" + action.target if action.kind == "anchor" else action.target, quote=True)}">{escape(action.name)}</a>'
                for action in contract.actions if action.page == filename)
            files[filename] = f'<main><h1>{escape(variant.headline)}</h1>{links}</main>'
        assert page_tests.variant_files("checked index", inputs, plan, tool_files=files) == files
        broken = {**files, "variant_focus.html": files["variant_focus.html"].replace("<h1>", "<h1>Unapproved ")}
        with pytest.raises(ValueError, match="headline"):
            page_tests.variant_files("checked index", inputs, plan, tool_files=broken)
        broken = {**files, "variant_focus.html": files["variant_focus.html"].replace("variant_focus.html", "missing.html")}
        with pytest.raises(ValueError, match="attribution"):
            page_tests.variant_files("checked index", inputs, plan, tool_files=broken)


def test_local_public_page_requires_its_own_checked_contract(market, monkeypatch):
    from app.db.supymem_models import BuildRun, Deployment
    from app.experiments import page_tests
    from app.hosting import service
    from app.pagebuilder import local_publish
    from app.state import db
    from core.clock import utcnow

    monkeypatch.setenv("SUPYMEM_MODE", "local")
    filename = "waitlist.html"
    with db()() as session:
        run = BuildRun(id="br_public_contract", venture_id=market[1], workspace_id=market[2], app_id=market[-1],
            state="succeeded", artifact_id="public_contract_source", trigger="manual", agent="coding_tool",
            spec=json.dumps({"inputs": {"task": {"landing_expectations": {"contract": {"actions": [{"page": "index.html"}]}}}}}))
        session.add(run)
        session.add(Deployment(id="dep_public_contract", venture_id=market[1], workspace_id=market[2], app_id=market[-1],
            build_artifact_id=run.artifact_id, target="local_static", state="live", version=1,
            url="https://owned-site.example/", live_at=utcnow()))
        session.commit()
    local_publish._save_publication(market[1], market[2], {"state": "live", "url": "https://owned-site.example/",
        "app_id": market[-1], "local_deployment_id": "dep_public_contract"})
    monkeypatch.setattr(service, "_accepted_source", lambda *_args, **_kwargs:
        (None, None, None, None, {"files": [{"path": filename}]}))
    with pytest.raises(ValueError, match="independent checks"):
        page_tests.checked_public_url(market[1], "", filename)
    with db()() as session:
        run = session.get(BuildRun, "br_public_contract")
        spec = json.loads(run.spec)
        spec["inputs"]["task"]["landing_expectations"]["contract"]["actions"].append({"page": filename})
        run.spec = json.dumps(spec)
        session.commit()
    assert page_tests.checked_public_url(market[1], "", filename) == "https://owned-site.example/" + filename
