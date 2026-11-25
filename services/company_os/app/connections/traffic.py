"""Read your connected traffic accounts without replacing recorded experiment events."""
from __future__ import annotations

import math
import re
from datetime import date, datetime, timedelta
from urllib.parse import urlsplit

from sqlalchemy import select

from app.credits import metered
from app.credits.policy import get_policy
from app.db.models import ContentOAuthState, Person, Venture, Workspace
from app.state import db
from core.clock import as_utc, utcnow
from core.copy import founder_copy

NAMES = {"google_analytics": "Google Analytics", "google_search_console": "Google Search Console"}
READS = {
    "summaries": r"GOOGLE_ANALYTICS_(?:LIST|GET)_ACCOUNT_SUMMARIES",
    "streams": r"GOOGLE_ANALYTICS_(?:LIST|GET)_DATA_STREAMS",
    "report": r"GOOGLE_ANALYTICS_RUN_REPORT",
    "sites": r"(?:GOOGLESEARCHCONSOLE|GOOGLE_SEARCH_CONSOLE)_(?:LIST|GET)_SITES",
    "queries": r"(?:GOOGLESEARCHCONSOLE|GOOGLE_SEARCH_CONSOLE)_QUERY_SEARCH_ANALYTICS",
}


def enabled():
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True


def _connection(spec, toolkit):
    from app.authz.workspace import subject_person_id
    with db()() as session:
        venture = session.get(Venture, spec["venture_id"])
        workspace = session.get(Workspace, spec["workspace_id"])
        if (not venture or not workspace or venture.workspace_id != workspace.id
                or venture.archived_at or venture.stage in {"paused", "purged", "archived"}
                or workspace.archived_at or not session.get(Person, workspace.owner_id)):
            raise ValueError("traffic_scope_invalid")
        rows = session.scalars(select(ContentOAuthState).where(
            ContentOAuthState.workspace_id == workspace.id, ContentOAuthState.platform == toolkit,
            ContentOAuthState.auth_method == "composio", ContentOAuthState.outcome == "connected",
            ContentOAuthState.consumed_at.is_not(None), ContentOAuthState.auth_config_id.is_not(None),
            ContentOAuthState.connected_account_id.is_not(None),
        ).order_by(ContentOAuthState.created_at.desc())).all()
        for row in rows:
            if (row.actor_subject and subject_person_id(row.actor_subject) == workspace.owner_id
                    and row.provider_user_id in {workspace.owner_id, f"content:{workspace.id}"}):
                return {column.name: getattr(row, column.name) for column in ContentOAuthState.__table__.columns}
    return None


def _shape(schema, values):
    properties = schema.get("properties") or {}
    if not properties:
        return values
    shaped = {}
    for key, value in values.items():
        normalized = key.replace("_", "").lower()
        matches = [name for name in properties if name.replace("_", "").lower() == normalized]
        if len(matches) != 1:
            raise ValueError("traffic_tool_schema_changed")
        name = matches[0]
        if isinstance(value, dict):
            value = _shape(properties[name], value)
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            value = [_shape(properties[name].get("items", {}), item) for item in value]
        shaped[name] = value
    return shaped


def _reader(spec, toolkit, callback):
    if not enabled():
        return None
    name = NAMES[toolkit]
    connect = {"label": f"Connect {name}", "action": "connections.connect",
               "payload": {"query": f"connect {name}"}}
    try:
        account = _connection(spec, toolkit)
        if not account:
            return {"status": "not_connected", "insight": f"Connect {name} to read your site's results.", "actions": [connect]}
        from app.content.oauth.callbacks import _verified_connection
        from app.content.composio_social import _toolkit_version, toolkit_for
        from app.gtm.supymem import composio_gmail
        with metered.charge_sync(workspace_id=spec["workspace_id"], venture_id=spec["venture_id"],
                                 purpose="experiment.traffic", provider="composio", estimate_micro=0, payer="founder") as meter:
            _verified_connection(account)
            catalog = composio_gmail._request("GET", "/tools", params={"toolkit_slug": toolkit_for(toolkit), "limit": 1000})
            tools = catalog.get("items") or catalog.get("data") or []
            if isinstance(tools, dict):
                tools = tools.get("items") or []

            def read(operation, values, *, property_name=None, site_url=None):
                if not enabled() or _connection(spec, toolkit) != account:
                    raise ValueError("traffic_connection_changed")
                matches = [tool for tool in tools if isinstance(tool, dict)
                           and re.fullmatch(READS[operation], str(tool.get("slug") or ""))]
                if len(matches) != 1:
                    raise ValueError("traffic_read_tool_unavailable")
                tool = matches[0]
                schema = tool.get("input_parameters")
                if not isinstance(schema, dict):
                    detail = composio_gmail._request("GET", "/tools/" + tool["slug"])
                    schema = detail.get("input_parameters")
                if not isinstance(schema, dict):
                    raise ValueError("traffic_tool_schema_missing")
                fields, arguments = schema.get("properties") or {}, {}
                if property_name:
                    field = next((key for key in ("property", "property_id", "parent") if key in fields), None)
                    if not field:
                        raise ValueError("traffic_property_field_missing")
                    arguments[field] = property_name.split("/")[-1] if field == "property_id" else property_name
                if site_url:
                    field = next((key for key in ("site_url", "siteUrl") if key in fields), None)
                    if not field:
                        raise ValueError("traffic_site_field_missing")
                    arguments[field] = site_url
                body = next((key for key in ("body", "request_body") if key in fields), None)
                if body:
                    arguments[body] = _shape(fields[body], values)
                else:
                    arguments.update(_shape(schema, values))
                version = _toolkit_version(toolkit)
                body = {"user_id": account["provider_user_id"], "connected_account_id": account["connected_account_id"],
                        "arguments": arguments, "version": version}
                if version == "latest":
                    body["dangerously_skip_version_check"] = True
                response = composio_gmail._request("POST", "/tools/execute/" + tool["slug"],
                                                  json=body, timeout=composio_gmail.TOOL_TIMEOUT_S)
                if response.get("successful") is False:
                    raise ValueError("traffic_read_failed")
                data = response.get("data")
                if not isinstance(data, dict):
                    raise ValueError("traffic_response_invalid")
                if data.get("nextPageToken"):
                    # shortcut: paginated reads stay unavailable, add bounded pagination when a connected account needs it.
                    raise ValueError("traffic_report_incomplete")
                return data

            result = callback(read)
            meter.record_micro(0)
        return {"status": "connected", "source": f"from your {name}", "actions": [], **result,
                "insight": founder_copy(result["insight"])}
    except Exception:
        return {"status": "unavailable", "insight": f"Your {name} results are unavailable. Check your connection and refresh your results.", "actions": [connect]}


def _site(url):
    parsed = urlsplit(url or "")
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("traffic_site_invalid")
    return parsed


def _number(value):
    if isinstance(value, bool):
        raise ValueError("traffic_count_invalid")
    count = float(value)
    if not math.isfinite(count) or count < 0:
        raise ValueError("traffic_count_invalid")
    return count


def analytics(spec, site_url, paths, start, end):
    def report(read):
        site = _site(site_url)
        summaries = read("summaries", {}).get("accountSummaries")
        if not isinstance(summaries, list):
            raise ValueError("traffic_properties_invalid")
        matched = []
        for summary in summaries:
            for prop in summary.get("propertySummaries", []):
                streams = read("streams", {}, property_name=prop["property"]).get("dataStreams")
                if not isinstance(streams, list):
                    raise ValueError("traffic_streams_invalid")
                if any(_site(stream["webStreamData"]["defaultUri"]).netloc.lower() == site.netloc.lower()
                       for stream in streams if stream.get("webStreamData", {}).get("defaultUri")):
                    matched.append(prop["property"])
        if len(set(matched)) != 1:
            raise ValueError("traffic_site_property_ambiguous")
        filters = [{"filter": {"fieldName": "hostName", "stringFilter": {"matchType": "EXACT", "value": site.hostname}}},
                   {"filter": {"fieldName": "pagePath", "inListFilter": {"values": paths}}}]
        dimensions = ["pagePath"]
        if spec.get("channel_kind") == "channels":
            dimensions += ["sessionSource", "sessionMedium"]
            filters.append({"filter": {"fieldName": "sessionCampaignName", "stringFilter": {"matchType": "EXACT", "value": spec["id"]}}})
        metrics = ["sessions", "totalUsers", "keyEvents"]
        data = read("report", {"dateRanges": [{"startDate": start, "endDate": end}],
            "dimensions": [{"name": dimension} for dimension in dimensions],
            "metrics": [{"name": metric} for metric in metrics],
            "dimensionFilter": {"andGroup": {"expressions": filters}}, "limit": 1000}, property_name=matched[0])
        if ([header["name"] for header in data.get("dimensionHeaders", [])] != dimensions
                or [header["name"] for header in data.get("metricHeaders", [])] != metrics):
            raise ValueError("traffic_report_headers_invalid")
        rows = []
        for row in data.get("rows", []):
            labels = [item["value"] for item in row["dimensionValues"]]
            counts = [_number(item["value"]) for item in row["metricValues"]]
            if (len(labels) != len(dimensions) or len(counts) != 3
                    or not all(isinstance(label, str) for label in labels) or labels[0] not in paths):
                raise ValueError("traffic_report_row_invalid")
            rows.append({**dict(zip(dimensions, labels)), "sessions": counts[0], "users": counts[1], "conversions": counts[2]})
        if int(data.get("rowCount", len(rows))) != len(rows):
            raise ValueError("traffic_report_incomplete")
        lines = [f"{row['pagePath']}: {row['sessions']:g} sessions, {row['users']:g} users, {row['conversions']:g} conversions"
                 + (f" ({row['sessionSource']} / {row['sessionMedium']})" if len(dimensions) > 1 else "") for row in rows]
        return {"start_date": start, "end_date": end, "rows": rows,
            "insight": f"From your Google Analytics ({start} to {end}): " + ("; ".join(lines) if lines else "no matching visits.")
                + " Conversions are your configured key events, not verified signups. Google reports use whole dates, so the first and last dates can include visits outside your test hours. These counts are separate from your recorded test events."}
    return _reader(spec, "google_analytics", report)


def search_console(spec, site_url, start, end):
    def queries(read):
        url = site_url
        if not url:
            from app.conversion.waitlist import status
            url = (status(spec["venture_id"], spec["workspace_id"]).get("published_site") or {}).get("url")
        if not url:
            from app.intake.assets import get_site
            with db()() as session:
                url = (get_site(spec["venture_id"], session=session).get("site") or {}).get("url")
        site = _site(url)
        entries = read("sites", {}).get("siteEntry")
        if not isinstance(entries, list):
            raise ValueError("traffic_sites_invalid")
        matched = []
        for entry in entries:
            if entry.get("permissionLevel") not in {"siteOwner", "siteFullUser", "siteRestrictedUser"}:
                continue
            url = entry.get("siteUrl", "")
            if url.startswith("sc-domain:"):
                domain = url.removeprefix("sc-domain:").lower()
                if site.hostname == domain or site.hostname.endswith("." + domain):
                    matched.append(url)
            else:
                prop = _site(url)
                if (prop.scheme, prop.netloc.lower()) == (site.scheme, site.netloc.lower()) and (
                        site.path.rstrip("/") == prop.path.rstrip("/") or site.path.startswith(prop.path.rstrip("/") + "/")):
                    matched.append(url)
        if not matched:
            raise ValueError("traffic_own_site_missing")
        prefix = f"{site.scheme}://{site.netloc}{site.path.rstrip('/')}"
        data = read("queries", {"startDate": start, "endDate": end, "dimensions": ["query", "page"], "rowLimit": 25,
            "dimensionFilterGroups": [{"filters": [{"dimension": "page", "operator": "includingRegex",
                                                     "expression": "^" + re.escape(prefix) + r"(?:[/?]|$)"}]}]}, site_url=matched[0])
        rows = []
        for row in data.get("rows", []):
            query, page = row["keys"]
            if (not isinstance(query, str) or not isinstance(page, str)
                    or not re.match("^" + re.escape(prefix) + r"(?:[/?]|$)", page)):
                raise ValueError("traffic_query_site_mismatch")
            rows.append({"query": query, "page": page, "impressions": _number(row["impressions"]), "clicks": _number(row["clicks"])})
        lines = [f"{row['query']}: {row['impressions']:g} impressions, {row['clicks']:g} clicks" for row in rows]
        return {"start_date": start, "end_date": end, "rows": rows,
            "insight": f"From your Google Search Console ({start} to {end}): " + ("; ".join(lines) if lines else "no matching queries.")
                + " This is a sample of queries for your own site, not total market search demand. Recent dates may not be complete."}
    return _reader(spec, "google_search_console", queries)


def readout(spec, context, site_url):
    if not enabled() or spec.get("page_kind") in {"survey", "request"} or spec.get("channel_kind") == "referral":
        return None
    start = as_utc(datetime.fromisoformat(context["prepared_at"]))
    end = min(utcnow(), start + timedelta(days=spec["max_duration_days"]))
    path = urlsplit(site_url or "").path.rstrip("/") or "/"
    paths = [path] if path == "/" else [path, path + "/"]
    if spec.get("page_kind") == "headlines":
        paths = [path.rstrip("/") + f"/variant_{variant['key']}.html" for variant in spec["variants"]]
    return analytics(spec, site_url, paths, start.date().isoformat(), end.date().isoformat())


def add_result(result, signal, previous):
    saved = (previous or {}).get("traffic")
    if isinstance(saved, dict) and saved.get("status") != "connected":
        saved = saved.get("previous")
    if signal:
        if signal.get("status") != "connected" and isinstance(saved, dict):
            signal = {**signal, "previous": saved}
        return {**result, "traffic": signal, "insight": result["insight"] + "\n" + signal["insight"]}
    if isinstance(saved, dict) and saved.get("status") == "connected":
        return {**result, "traffic": saved}
    return result


def add_search_demand(spec, result):
    if not enabled():
        return result
    end = min(utcnow().date(), date.fromisoformat(spec["window_end"]))
    traffic = search_console(spec, spec.get("site_url"), (end - timedelta(days=90)).isoformat(), end.isoformat())
    if not traffic:
        return result
    return {**result, "traffic": traffic, "insight": result.get("insight", result.get("error", "")) + "\n" + traffic["insight"]}
