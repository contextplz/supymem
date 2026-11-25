"""Traffic reads stay scoped, read-only and optional for unrelated venture domains."""
import asyncio
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from tests.unit.test_composio_toolkit_catalog import DOMAINS, scopes


@pytest.fixture(params=DOMAINS)
def traffic_account(scopes, request, monkeypatch):
    from app.connections import traffic
    from app.content import composio_social
    from app.credits import metered
    from app.db.models import ContentOAuthState
    from app.gtm.supymem import composio_gmail
    from app.state import db
    from core.clock import utcnow

    scope = next(item for item in scopes if item["domain"] == request.param)
    other = next(item for item in scopes if item is not scope)
    host = scope["workspace"] + ".example.test"
    site = f"https://{host}/start"
    now = utcnow()
    calls, charges = [], []
    flags = {"experiments_autopilot_v1": True}
    monkeypatch.setattr(traffic, "get_policy", lambda key: flags if key == "feature_flags" else {})
    with db()() as session:
        for toolkit in traffic.NAMES:
            session.add(ContentOAuthState(id=toolkit, workspace_id=scope["workspace"], platform=toolkit,
                actor_subject=scope["owner"], provider_user_id=f"content:{scope['workspace']}",
                auth_method="composio", auth_config_id="auth_" + toolkit,
                connected_account_id="account_" + toolkit, outcome="connected", consumed_at=now,
                redirect_uri="https://console.example.test/callback", expires_at=now, created_at=now))
        session.commit()

    @contextmanager
    def charge(**kwargs):
        charges.append(kwargs)
        yield SimpleNamespace(record_micro=lambda amount: charges.append(amount))

    monkeypatch.setattr(metered, "charge_sync", charge)

    def verify(method, path, **kwargs):
        assert method == "GET" and path.startswith("/connected_accounts/account_")
        toolkit = path.removeprefix("/connected_accounts/account_")
        calls.append(("verify", toolkit))
        return {"id": "account_" + toolkit, "user_id": f"content:{scope['workspace']}", "status": "ACTIVE",
                "toolkit": {"slug": toolkit}, "auth_config": {"id": "auth_" + toolkit}}

    monkeypatch.setattr(composio_social, "_request", verify)
    schemas = {
        "GOOGLE_ANALYTICS_GET_ACCOUNT_SUMMARIES": {},
        "GOOGLE_ANALYTICS_LIST_DATA_STREAMS": {"property_id": {}},
        "GOOGLE_ANALYTICS_RUN_REPORT": {"property_id": {}, "date_ranges": {"items": {"properties": {
            "start_date": {}, "end_date": {}}}}, "dimensions": {}, "metrics": {}, "dimension_filter": {}, "limit": {}},
        "GOOGLESEARCHCONSOLE_LIST_SITES": {},
        "GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS": {"site_url": {}, "body": {}},
        "GOOGLE_ANALYTICS_CREATE_PROPERTY": {"display_name": {}},
    }

    def catalog(method, path, **kwargs):
        if method == "POST":
            return execute(path.removeprefix("/tools/execute/"), **kwargs["json"])
        assert method == "GET" and path == "/tools"
        calls.append(("catalog", kwargs["params"]["toolkit_slug"]))
        return {"items": [{"slug": slug, "input_parameters": {"type": "object", "properties": fields}}
                          for slug, fields in schemas.items()]}

    monkeypatch.setattr(composio_gmail, "_request", catalog)
    responses = {}

    def execute(slug, **kwargs):
        assert slug in schemas and "CREATE" not in slug
        assert kwargs["user_id"] == f"content:{scope['workspace']}"
        assert kwargs["connected_account_id"] in {"account_google_analytics", "account_google_search_console"}
        assert kwargs["version"] == "20261001" and "dangerously_skip_version_check" not in kwargs
        arguments = kwargs["arguments"]
        calls.append((slug, arguments))
        if slug in responses:
            return {"successful": True, "data": responses[slug]}
        if slug.endswith("ACCOUNT_SUMMARIES"):
            data = {"accountSummaries": [{"propertySummaries": [{"property": "properties/123"}, {"property": "properties/456"}]}]}
        elif slug.endswith("DATA_STREAMS"):
            uri = site if arguments["property_id"] == "123" else "https://other.example.test/"
            data = {"dataStreams": [{"webStreamData": {"defaultUri": uri}}]}
        elif slug.endswith("RUN_REPORT"):
            assert arguments["property_id"] == "123"
            dimensions = [item["name"] for item in arguments["dimensions"]]
            paths = arguments["dimension_filter"]["andGroup"]["expressions"][1]["filter"]["inListFilter"]["values"]
            data = {"dimensionHeaders": [{"name": name} for name in dimensions],
                "metricHeaders": [{"name": item["name"]} for item in arguments["metrics"]],
                "rows": [{"dimensionValues": [{"value": path}] + ([{"value": "email"}, {"value": "email"}] if len(dimensions) > 1 else []),
                    "metricValues": [{"value": "12"}, {"value": "9"}, {"value": "3"}]} for path in paths], "rowCount": len(paths)}
        elif slug.endswith("LIST_SITES"):
            data = {"siteEntry": [{"siteUrl": "sc-domain:other.example.test", "permissionLevel": "siteOwner"},
                                  {"siteUrl": "sc-domain:" + host, "permissionLevel": "siteOwner"}]}
        else:
            assert arguments["site_url"] == "sc-domain:" + host
            data = {"rows": [{"keys": [scope["domain"], site + "/guide"], "impressions": 71, "clicks": 4}]}
        return {"successful": True, "data": data}

    monkeypatch.setenv("GMAIL_TOOLKIT_VERSION", "gmail-only-pin")
    monkeypatch.setenv("COMPOSIO_GOOGLE_ANALYTICS_TOOLKIT_VERSION", "20261001")
    monkeypatch.setenv("COMPOSIO_GOOGLE_SEARCH_CONSOLE_TOOLKIT_VERSION", "20261001")
    spec = {"venture_id": scope["venture"], "workspace_id": scope["workspace"], "id": "exp_traffic",
            "window_end": now.date().isoformat()}
    return SimpleNamespace(scope=scope, other=other, site=site, host=host, spec=spec, calls=calls,
                           charges=charges, flags=flags, responses=responses, schemas=schemas)


@pytest.mark.parametrize("kind", ["page", "channel"])
def test_readouts_persist_scoped_analytics_without_changing_verdict(traffic_account, monkeypatch, kind):
    from app.db.supymem_models import Experiment
    from app.experiments import autonomous, channel_tests, ledger, page_tests
    from app.state import db
    from core.clock import utcnow

    fake = traffic_account
    module = page_tests if kind == "page" else channel_tests
    spec = {**fake.spec, "scan": "page_test" if kind == "page" else "channel_test", "title": "Your page results",
            "app_id": "app_traffic", "site_url": fake.site, "max_duration_days": 14, "method": "Recorded test events.",
            "metric": "signup_rate_ratio" if kind == "page" else "leading_channel_share", **autonomous._thresholds(2, 0, 50)}
    spec.update({"page_kind": "headlines", "variants": [{"key": "a", "label": "First"}, {"key": "b", "label": "Second"}]}
                if kind == "page" else {"channel_kind": "channels", "variants": [
                    {"key": "email", "label": "Email", "medium": "email"}, {"key": "other", "label": "Other", "medium": "other"}]})
    monkeypatch.setattr(ledger, "current_direction", lambda *args: "direction_traffic")
    monkeypatch.setattr(page_tests, "published_url", lambda *args: fake.site)
    spec["direction"] = "direction_traffic"
    prepared = utcnow() - timedelta(days=20)
    context = {"spec": spec, "prepared_at": prepared.isoformat(), "effort": {}, "cohort": {}}
    with db()() as session:
        session.add(Experiment(id=spec["id"], venture_id=spec["venture_id"], workspace_id=spec["workspace_id"],
            kind="venture_test", hypothesis="Compare your pages", context=json.dumps(context), state="running", started_at=prepared))
        session.commit()
    baseline = module.observe(spec)
    reply = module.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert "From your Google Analytics" in reply["body_md"] and "outside your test hours" in reply["body_md"]
    with db()() as session:
        saved = json.loads(session.get(Experiment, spec["id"]).context)["result"]
    assert saved["outcome"] == baseline["outcome"] == "inconclusive"
    assert saved["measured"] == baseline["measured"]
    assert saved["traffic"]["source"] == "from your Google Analytics"
    assert saved["traffic"]["rows"][0]["users"] == 9 and saved["traffic"]["rows"][0]["conversions"] == 3
    report = next(arguments for slug, arguments in fake.calls if slug == "GOOGLE_ANALYTICS_RUN_REPORT")
    assert report["date_ranges"] == [{"start_date": prepared.date().isoformat(), "end_date": (prepared + timedelta(days=14)).date().isoformat()}]
    filters = report["dimension_filter"]["andGroup"]["expressions"]
    assert filters[0]["filter"]["stringFilter"]["value"] == fake.host
    assert filters[1]["filter"]["inListFilter"]["values"] == (["/start/variant_a.html", "/start/variant_b.html"] if kind == "page" else ["/start", "/start/"])
    if kind == "channel":
        assert filters[2]["filter"]["stringFilter"]["value"] == spec["id"]
    assert fake.charges[0]["workspace_id"] == fake.scope["workspace"] and fake.charges[-1] == 0
    del fake.schemas["GOOGLE_ANALYTICS_RUN_REPORT"]
    failed = module.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert "unavailable" in failed["body_md"]
    assert len([action for action in failed["actions"] if action.get("action") == "connections.connect"]) == 1
    with db()() as session:
        assert json.loads(session.get(Experiment, spec["id"]).context)["result"]["traffic"]["previous"] == saved["traffic"]
    fake.flags.clear()
    fake.calls.clear()
    reply = module.readout(spec["venture_id"], {"experiment_id": spec["id"]})
    assert fake.calls == [] and "Google Analytics" not in reply["body_md"]
    with db()() as session:
        assert json.loads(session.get(Experiment, spec["id"]).context)["result"]["traffic"] == saved["traffic"]


@pytest.mark.parametrize("site_source", ["published", "imported"])
def test_search_demand_adds_own_queries_and_connect_action_without_market_counts(traffic_account, monkeypatch, site_source):
    from app.conversion import waitlist
    from app.experiments import autonomous
    from app.signals.research import SearchResults, ScrapedText

    fake = traffic_account
    monkeypatch.setattr(autonomous, "get_policy", lambda key: fake.flags)
    monkeypatch.setattr(waitlist, "status", lambda vid, wid: {"published_site": {"url": fake.site} if site_source == "published" else None})
    if site_source == "imported":
        from app.db.models import VentureDocument
        from app.state import db
        from core.clock import utcnow
        with db()() as session:
            session.add(VentureDocument(id="external_site:" + fake.spec["venture_id"], venture_id=fake.spec["venture_id"],
                kind="external_site", title="Your existing site", path=fake.site,
                body_md=json.dumps({"url": fake.site, "status": "verified", "audit": {}}), created_at=utcnow(), updated_at=utcnow()))
            session.commit()
    async def search(*args, **kwargs):
        return SearchResults([{"url": "https://research.example.test/question", "title": fake.scope["domain"],
                               "snippet": "How can I get started?"}], provider_rows=1, rejected_shape=0, rejected_missing_url=0,
                             usage={"firecrawl_self_hosted": True})
    async def scrape(*args, **kwargs):
        return ScrapedText("How can I get started?", metadata={}, usage={"firecrawl_self_hosted": True})
    spec = {**fake.spec, "scan": "search_demand", "title": "Your search questions", "icp": {
        "who": fake.scope["domain"], "problem": "getting started", "mode": "company"}, "queries": [fake.scope["domain"]],
        "pages": 1, "search_micro": 0, "scrape_micro": 0}
    result = asyncio.run(autonomous._scan(spec, SimpleNamespace(name="firecrawl", search=search, scrape=scrape)))
    assert "error" not in result
    assert result["traffic"]["rows"] == [{"query": fake.scope["domain"], "page": fake.site + "/guide", "impressions": 71, "clicks": 4}]
    assert result["measured"]["events"] != 71
    query = next(arguments["body"] for slug, arguments in fake.calls if slug == "GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS")
    assert query["endDate"] == fake.spec["window_end"] and query["rowLimit"] == 25
    assert query["dimensions"] == ["query", "page"] and query["dimensionFilterGroups"][0]["filters"][0]["dimension"] == "page"
    from app.db.models import ContentOAuthState
    from app.state import db
    with db()() as session:
        session.get(ContentOAuthState, "google_search_console").outcome = "needs_reconnect"
        session.commit()
    fake.calls.clear()
    result = asyncio.run(autonomous._scan(spec, SimpleNamespace(name="firecrawl", search=search, scrape=scrape)))
    assert fake.calls == [] and "error" not in result
    reply = autonomous.result_reply({**spec, "result": result})
    assert reply["actions"] == [{"label": "Connect Google Search Console", "action": "connections.connect",
                                 "payload": {"query": "connect Google Search Console"}}]


@pytest.mark.parametrize("state", ["off", "missing", "revoked", "foreign_owner", "foreign_workspace"])
def test_traffic_off_and_unapproved_accounts_make_no_provider_calls(traffic_account, state):
    from app.connections import traffic
    from app.db.models import ContentOAuthState
    from app.state import db

    fake = traffic_account
    if state == "off":
        fake.flags.clear()
    else:
        with db()() as session:
            row = session.get(ContentOAuthState, "google_analytics")
            if state == "missing":
                session.delete(row)
            elif state == "revoked":
                row.outcome = "revoked"
            elif state == "foreign_owner":
                row.actor_subject = fake.other["owner"]
            else:
                row.workspace_id = fake.other["workspace"]
            session.commit()
    signal = traffic.analytics(fake.spec, fake.site, ["/start"], "2026-10-01", "2026-10-10")
    assert fake.calls == [] and fake.charges == []
    assert signal is None if state == "off" else signal["actions"][0]["label"] == "Connect Google Analytics"


@pytest.mark.parametrize("failure", ["wrong_site", "invalid_count", "incomplete", "write_tool_only", "provider_failure", "tool_failure"])
def test_unavailable_traffic_never_becomes_zero_or_uses_write_tools(traffic_account, failure, monkeypatch):
    from app.connections import traffic
    from app.gtm.supymem import composio_gmail

    fake = traffic_account
    if failure == "wrong_site":
        fake.responses["GOOGLE_ANALYTICS_LIST_DATA_STREAMS"] = {"dataStreams": [{"webStreamData": {"defaultUri": "https://elsewhere.example.test"}}]}
    elif failure in {"invalid_count", "incomplete"}:
        fake.responses["GOOGLE_ANALYTICS_RUN_REPORT"] = {"dimensionHeaders": [{"name": "pagePath"}],
            "metricHeaders": [{"name": name} for name in ["sessions", "totalUsers", "keyEvents"]],
            "rows": [{"dimensionValues": [{"value": "/start"}], "metricValues": [{"value": value} for value in ["NaN" if failure == "invalid_count" else "1", "1", "1"]]}],
            "rowCount": 1 if failure == "invalid_count" else 2}
    elif failure == "write_tool_only":
        del fake.schemas["GOOGLE_ANALYTICS_RUN_REPORT"]
    elif failure == "tool_failure":
        original = composio_gmail._request
        def refused(method, path, **kwargs):
            if path == "/tools/execute/GOOGLE_ANALYTICS_RUN_REPORT":
                return {"successful": False, "data": {}, "error": "private provider error"}
            return original(method, path, **kwargs)
        monkeypatch.setattr(composio_gmail, "_request", refused)
    else:
        monkeypatch.setattr(composio_gmail, "_request", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("private provider error")))
    signal = traffic.analytics(fake.spec, fake.site, ["/start"], "2026-10-01", "2026-10-10")
    assert signal["status"] == "unavailable" and "rows" not in signal
    assert "private provider error" not in signal["insight"]
    assert len(signal["actions"]) == 1 and all("CREATE" not in slug for slug, _arguments in fake.calls)


@pytest.mark.parametrize("mismatch", ["other_host", "different_path", "similar_path"])
def test_search_console_rejects_results_outside_your_site(traffic_account, mismatch):
    from app.connections import traffic
    fake = traffic_account
    page = "https://other.example.test/start" if mismatch == "other_host" else (
        fake.site.removesuffix("/start") + ("/" if mismatch == "different_path" else "/start-other"))
    fake.responses["GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS"] = {"rows": [
        {"keys": [fake.scope["domain"], page], "impressions": 10, "clicks": 1}]}
    signal = traffic.search_console(fake.spec, fake.site, "2026-10-01", "2026-10-10")
    assert signal["status"] == "unavailable" and "rows" not in signal


def test_pricing_readout_handles_both_page_path_forms(traffic_account):
    from app.connections import traffic
    from core.clock import utcnow
    fake = traffic_account
    signal = traffic.readout({**fake.spec, "page_kind": "pricing", "max_duration_days": 14},
        {"prepared_at": (utcnow() - timedelta(days=1)).isoformat()}, fake.site + "/")
    assert signal["status"] == "connected"
    assert [row["pagePath"] for row in signal["rows"]] == ["/start", "/start/"]


def test_traffic_copy_is_plain_without_changing_your_query(traffic_account):
    from app.connections import traffic
    fake = traffic_account
    query = fake.scope["domain"] + " — getting started"
    fake.responses["GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS"] = {"rows": [
        {"keys": [query, fake.site], "impressions": 10, "clicks": 1}]}
    signal = traffic.search_console(fake.spec, fake.site, "2026-10-01", "2026-10-10")
    assert signal["rows"][0]["query"] == query
    assert "—" not in signal["insight"] and "from your Google Search Console" in signal["source"]


@pytest.mark.parametrize("toolkit", ["google_analytics", "google_search_console"])
def test_invalid_traffic_labels_do_not_become_your_results(traffic_account, toolkit):
    from app.connections import traffic
    fake = traffic_account
    if toolkit == "google_analytics":
        fake.responses["GOOGLE_ANALYTICS_RUN_REPORT"] = {
            "dimensionHeaders": [{"name": name} for name in ["pagePath", "sessionSource", "sessionMedium"]],
            "metricHeaders": [{"name": name} for name in ["sessions", "totalUsers", "keyEvents"]],
            "rows": [{"dimensionValues": [{"value": "/start"}, {"value": {}}, {"value": "email"}],
                      "metricValues": [{"value": "1"}, {"value": "1"}, {"value": "1"}]}], "rowCount": 1}
        signal = traffic.analytics({**fake.spec, "channel_kind": "channels"}, fake.site, ["/start"], "2026-10-01", "2026-10-10")
    else:
        fake.responses["GOOGLESEARCHCONSOLE_QUERY_SEARCH_ANALYTICS"] = {"rows": [
            {"keys": [{}, fake.site], "impressions": 10, "clicks": 1}]}
        signal = traffic.search_console(fake.spec, fake.site, "2026-10-01", "2026-10-10")
    assert signal["status"] == "unavailable" and "rows" not in signal
