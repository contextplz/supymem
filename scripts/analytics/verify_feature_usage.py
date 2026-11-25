#!/usr/bin/env python3
"""Per-feature usage and error report. Read-only; aggregate rows only (no person ids, no props blobs).

Two roles in one file so the payload can travel inside one ECS run-task override:

* ``VERIFY_PAYLOAD=1``: runs inside the ``app`` container of the core-api task
  (see ``verify_feature_usage.sh``), queries the database read-only and prints
  ``VERIFY_ROW|{json}`` lines.
* otherwise: ``verify_feature_usage.py --rows rows.txt [--cw cw.json] [--hours N]``
  renders those rows (plus optional CloudWatch Logs Insights results) as tables.

Uses the ``v_feature_usage_events`` / ``v_errors_unified`` views when the
migration is deployed, else classifies ``analytics_events`` directly (coarse
labels), so it also gives a baseline before the views exist.
"""

import json
import os
import sys

PREFIX = "VERIFY_ROW|"
USAGE_EVENTS = ("ui.screen_view", "ui.navigation", "ui.control_activate", "chat.tool_invoked", "feature.used")
ERROR_EVENTS = ("ui.error", "server.error", "server.request_error", "job.failed", "model.output_rejected",
                "chat.reply_error", "build.finished")
_JSON = "CROSS JOIN LATERAL (SELECT CASE WHEN e.props_json LIKE '{%' THEN e.props_json::jsonb ELSE '{}'::jsonb END AS p) j"
_SINCE = "e.occurred_at > (now() AT TIME ZONE 'utc') - make_interval(hours => :h)"


def _rows(conn, sql, **params):
    from sqlalchemy import text

    return [dict(r._mapping) for r in conn.execute(text(sql), params)]


def _has(conn, name):
    return _rows(conn, "SELECT to_regclass(:n) IS NOT NULL AS ok", n=name)[0]["ok"]


def digest_rows(conn, hours):
    """Errors grouped component -> source -> intent -> code, with internal vs external distinct accounts.

    Reads ``analytics_events`` directly (no view dependency). Internal = ``analytics_internal_accounts`` or
    ``SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS``. ``intent`` is what the user/system was doing (client rows:
    ``error_intent`` else the API operation; server rows: the feature the route belongs to).
    """
    internal = [p.strip() for p in os.environ.get("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "").split(",") if p.strip()]
    join = ("LEFT JOIN analytics_internal_accounts ia ON ia.person_id = e.person_id" if _has(conn, "analytics_internal_accounts")
            else "LEFT JOIN (SELECT NULL::text AS person_id) ia ON false")
    sql = f"""SELECT component, source, intent, code, count(*) AS n,
        count(DISTINCT person_id) FILTER (WHERE internal) AS internal_accounts,
        count(DISTINCT person_id) FILTER (WHERE NOT internal) AS external_accounts,
        min(occurred_at) AS first_seen, max(occurred_at) AS last_seen,
        string_agg(DISTINCT version, ',') AS versions
        FROM (SELECT e.person_id, e.occurred_at,
            (ia.person_id IS NOT NULL OR e.person_id = ANY(CAST(:internal AS text[]))) AS internal,
            CASE e.event_name WHEN 'ui.error' THEN CASE WHEN j.p->>'error_context' = 'native' THEN 'desktop' ELSE 'console' END
                 WHEN 'server.error' THEN 'core_api' WHEN 'server.request_error' THEN 'core_api'
                 WHEN 'job.failed' THEN 'worker' WHEN 'build.finished' THEN 'worker'
                 WHEN 'model.output_rejected' THEN 'model' WHEN 'chat.reply_error' THEN 'chat' END AS component,
            CASE e.event_name WHEN 'ui.error' THEN COALESCE(j.p->>'error_source', j.p->>'error_context')
                 WHEN 'job.failed' THEN j.p->>'kind' WHEN 'build.finished' THEN 'build'
                 WHEN 'model.output_rejected' THEN j.p->>'operation_kind' WHEN 'chat.reply_error' THEN COALESCE(j.p->>'tool', 'chat')
                 ELSE j.p->>'route' END AS source,
            COALESCE(CASE e.event_name WHEN 'ui.error' THEN COALESCE(j.p->>'error_intent', j.p->>'error_operation') END,
                     j.p->>'feature', 'unknown') AS intent,
            CASE e.event_name WHEN 'ui.error' THEN COALESCE(j.p->>'error_server_code', j.p->>'error_code')
                 WHEN 'server.error' THEN COALESCE(NULLIF(j.p->>'error_class', 'none'), j.p->>'failure_kind')
                 WHEN 'server.request_error' THEN COALESCE(j.p->>'error_code', 'none')
                 WHEN 'model.output_rejected' THEN j.p->>'rule' WHEN 'chat.reply_error' THEN COALESCE(j.p->>'failure_kind', 'unknown')
                 ELSE COALESCE(j.p->>'error_code', 'none') END AS code,
            j.p->>'app_version' AS version
            FROM analytics_events e {_JSON} {join}
            WHERE e.event_name IN {ERROR_EVENTS!r} AND {_SINCE}
              AND COALESCE(j.p->>'error_severity', 'error') <> 'transient'
              AND (e.event_name <> 'build.finished' OR j.p->>'success' = 'false')) x
        WHERE component IS NOT NULL GROUP BY 1, 2, 3, 4 ORDER BY n DESC LIMIT 300"""
    for row in _rows(conn, sql, h=hours, internal=internal):
        yield {"t": "digest", **row}


def collect(conn, hours, *, catalog=None, force_baseline=False):
    """Yield report rows. ``conn`` must already be read-only; ``catalog`` maps slug -> inventory ids."""
    from sqlalchemy import text

    conn.execute(text("SET TRANSACTION READ ONLY"))
    views = not force_baseline and _has(conn, "v_feature_usage_events") and _has(conn, "v_errors_unified")
    yield {"t": "meta", "hours": hours, "views": views, "catalog": len(catalog or {})}
    if views:
        usage = _rows(conn, """SELECT slug, count(*) FILTER (WHERE use_kind = 'view') AS views,
            count(*) FILTER (WHERE use_kind = 'use') AS uses, count(*) FILTER (WHERE use_kind = 'click') AS clicks,
            count(DISTINCT person_id) AS accounts,
            count(DISTINCT person_id) FILTER (WHERE audience = 'external') AS ext_accounts,
            count(DISTINCT venture_id) AS ventures, max(occurred_at) AS last_seen
            FROM v_feature_usage_events e WHERE use_kind IN ('view', 'use', 'click') AND """ + _SINCE + " GROUP BY slug", h=hours)
        errors = _rows(conn, """SELECT feature, source, error_code, status, count(*) AS n,
            count(DISTINCT person_id) AS accounts, count(*) FILTER (WHERE audience = 'external') AS ext
            FROM v_errors_unified e WHERE severity <> 'transient' AND """ + _SINCE + " GROUP BY 1, 2, 3, 4", h=hours)
    else:
        usage = _rows(conn, f"""SELECT COALESCE(j.p->>'feature', 'unknown') AS slug,
            count(*) FILTER (WHERE e.event_name IN ('ui.screen_view', 'ui.navigation')) AS views,
            count(*) FILTER (WHERE e.event_name IN ('ui.control_activate', 'chat.tool_invoked')) AS uses,
            count(*) FILTER (WHERE e.event_name = 'feature.used' AND e.id LIKE 'usage_%' AND j.p->>'tool' IS NULL) AS clicks,
            count(DISTINCT e.person_id) AS accounts, NULL::int AS ext_accounts,
            count(DISTINCT e.venture_id) AS ventures, max(e.occurred_at) AS last_seen
            FROM analytics_events e {_JSON}
            WHERE e.event_name IN {USAGE_EVENTS!r} AND {_SINCE} GROUP BY 1""", h=hours)
        errors = _rows(conn, f"""SELECT COALESCE(j.p->>'feature', 'unattributed') AS feature,
            CASE e.event_name WHEN 'ui.error' THEN 'client' WHEN 'job.failed' THEN 'worker'
                 WHEN 'model.output_rejected' THEN 'model' WHEN 'chat.reply_error' THEN 'chat' ELSE 'server' END AS source,
            COALESCE(j.p->>'error_code', j.p->>'failure_kind', j.p->>'rule', 'none') AS error_code,
            CASE WHEN (j.p->>'status') ~ '^[0-9]{{1,3}}$' THEN (j.p->>'status')::int END AS status,
            count(*) AS n, count(DISTINCT e.person_id) AS accounts, NULL::int AS ext
            FROM analytics_events e {_JSON}
            WHERE e.event_name IN {ERROR_EVENTS!r} AND COALESCE(j.p->>'error_severity', 'error') <> 'transient'
              AND {_SINCE} GROUP BY 1, 2, 3, 4""", h=hours)
    for row in usage:
        yield {"t": "feature", **row, "inventory": (catalog or {}).get(row["slug"], "")}
    for slug, inventory in sorted((catalog or {}).items()):
        if slug not in {r["slug"] for r in usage}:
            yield {"t": "feature", "slug": slug, "views": 0, "uses": 0, "clicks": 0, "accounts": 0, "ext_accounts": 0,
                   "ventures": 0, "last_seen": None, "inventory": inventory}
    for row in errors:
        yield {"t": "error", **row}
    # What users SAW (exact message, scrubbed): ui.error rows from the UI/voice reporter. The digest above has the
    # cross-component source > intent > code grouping; this adds the message text per source and intent.
    for row in _rows(conn, f"""SELECT j.p->>'error_source' AS error_source, COALESCE(j.p->>'error_intent', '') AS intent,
            COALESCE(j.p->>'feature', '') AS feature, COALESCE(j.p->>'error_code', '') AS error_code,
            min(left(j.p->>'error_text', 70)) AS sample, count(*) AS n, count(DISTINCT e.person_id) AS accounts,
            min(e.occurred_at) AS first_seen, max(e.occurred_at) AS last_seen
            FROM analytics_events e {_JSON}
            WHERE e.event_name = 'ui.error' AND j.p->>'error_context' IN ('ui', 'voice')
              AND COALESCE(j.p->>'error_severity', 'error') <> 'transient' AND {_SINCE}
            GROUP BY 1, 2, 3, 4 ORDER BY n DESC""", h=hours):
        yield {"t": "user_error", **row}
    # Transient states (retried / expected): listed on their own, never part of the error counts above.
    for row in _rows(conn, f"""SELECT COALESCE(j.p->>'feature', '') AS feature, COALESCE(j.p->>'error_context', '') AS context,
            COALESCE(j.p->>'error_source', j.p->>'error_native_command', j.p->>'error_operation', '') AS detail,
            count(*) AS n, COALESCE(sum(CASE WHEN (j.p->>'error_repeat_count') ~ '^[0-9]{{1,6}}$'
                                             THEN (j.p->>'error_repeat_count')::int END), 0) AS repeats,
            count(DISTINCT e.person_id) AS accounts, max(e.occurred_at) AS last_seen
            FROM analytics_events e {_JSON}
            WHERE e.event_name = 'ui.error' AND j.p->>'error_severity' = 'transient' AND {_SINCE}
            GROUP BY 1, 2, 3 ORDER BY n DESC""", h=hours):
        yield {"t": "transient_error", **row}
    for row in _rows(conn, f"SELECT e.event_name, count(*) AS n FROM analytics_events e WHERE {_SINCE} GROUP BY 1 ORDER BY 2 DESC", h=hours):
        yield {"t": "event", **row}
    # Jobs nobody saw: failed in the jobs table vs recorded as job.failed analytics rows.
    for row in _rows(conn, "SELECT kind, status, count(*) AS n FROM jobs WHERE status IN ('failed', 'failed_dead') "
                           "AND finished_at > (now() AT TIME ZONE 'utc') - make_interval(hours => :h) GROUP BY 1, 2", h=hours):
        yield {"t": "job_table", **row}
    for row in _rows(conn, f"SELECT j.p->>'kind' AS kind, count(DISTINCT j.p->>'job_id') AS n FROM analytics_events e {_JSON} "
                           f"WHERE e.event_name = 'job.failed' AND {_SINCE} GROUP BY 1", h=hours):
        yield {"t": "job_event", **row}
    # Same classification as the structured error_event log line, for the CloudWatch cross-check.
    for row in _rows(conn, f"""SELECT CASE WHEN e.event_name = 'ui.error' THEN 'client'
            WHEN e.event_name IN ('server.error', 'server.request_error') THEN 'server'
            WHEN e.event_name = 'job.failed' OR (e.event_name = 'build.finished' AND j.p->>'success' = 'false') THEN 'worker'
            WHEN e.event_name = 'model.output_rejected' THEN 'model'
            WHEN e.event_name = 'chat.reply_error' AND j.p->>'failure_kind' = 'confirm_failed' THEN 'chat' END AS source,
            count(*) AS n FROM analytics_events e {_JSON} WHERE {_SINCE} GROUP BY 1""", h=hours):
        if row["source"]:
            yield {"t": "dbcount", **row}
    yield from digest_rows(conn, hours)
    health = {"last_event": _rows(conn, "SELECT max(occurred_at) AS v FROM analytics_events")[0]["v"]}
    for name, sql in (("catalog_rows", "SELECT count(*) AS v FROM analytics_feature_catalog"),
                      ("internal_accounts", "SELECT count(*) AS v FROM analytics_internal_accounts"),
                      ("last_rollup_day", "SELECT max(day) AS v FROM analytics_feature_daily")):
        table = sql.rsplit(" ", 1)[1]
        health[name] = _rows(conn, sql)[0]["v"] if _has(conn, table) else None
    yield {"t": "health", **health}
    yield {"t": "done"}


def _payload():
    import sqlalchemy as sa

    hours = int(os.environ.get("VERIFY_HOURS", "24"))
    from app.db.session import normalize_db_url, resolve_database_url

    catalog = None
    try:  # an older image may predate the catalog; then only slugs seen in data appear
        from app.analytics.feature_catalog import reference_rows

        catalog = {r["slug"]: r["inventory_ids"] for r in reference_rows()[0]}
    except Exception:  # noqa: BLE001
        pass
    engine = sa.create_engine(normalize_db_url(resolve_database_url()))
    with engine.connect() as conn:
        for row in collect(conn, hours, catalog=catalog):
            print(PREFIX + json.dumps(row, default=str, sort_keys=True), flush=True)
        conn.rollback()


def _table(headers, rows):
    widths = [max(len(str(x)) for x in col) for col in zip(headers, *rows)] if rows else [len(h) for h in headers]
    line = lambda r: "  ".join(str(c).ljust(w) if i == 0 else str(c).rjust(w) for i, (c, w) in enumerate(zip(r, widths)))  # noqa: E731
    return "\n".join([line(headers), line(["-" * w for w in widths]), *[line(r) for r in rows]])


def render(rows, cw=None, hours=24):
    """Text report. ``cw`` is the raw ``aws logs get-query-results`` JSON (or None)."""
    by = lambda t: [r for r in rows if r.get("t") == t]  # noqa: E731
    out, flags = [], []
    meta = (by("meta") or [{}])[0]
    health = (by("health") or [{}])[0]
    out.append(f"Feature usage and errors, last {meta.get('hours', hours)}h  (views: {'yes' if meta.get('views') else 'NO, baseline on raw events'})")
    out.append("pipeline: last event {}, catalog rows {}, internal accounts {}, last rollup day {}".format(
        health.get("last_event"), health.get("catalog_rows"), health.get("internal_accounts"), health.get("last_rollup_day")))
    if not any(r.get("t") == "done" for r in rows):
        flags.append("INCOMPLETE: payload did not finish")
    errs: dict[str, int] = {}
    for r in by("error"):
        errs[r["feature"]] = errs.get(r["feature"], 0) + int(r["n"])
    feats = {}
    for r in by("feature"):
        feats[r["slug"]] = r
    for slug in errs:
        feats.setdefault(slug, {"slug": slug, "views": 0, "uses": 0, "clicks": 0, "accounts": 0, "ext_accounts": 0,
                                "last_seen": None, "inventory": ""})
    table = sorted(feats.values(), key=lambda r: (-(int(r["views"]) + int(r["uses"]) + int(r["clicks"])), -errs.get(r["slug"], 0), r["slug"]))
    out += ["", "Per feature (uses = control + chat tool; clicks = generic client clicks; ext = external accounts)",
            _table(["feature", "views", "uses", "clicks", "errors", "accts", "ext", "last_seen", "note"],
                   [[r["slug"], r["views"], r["uses"], r["clicks"], errs.get(r["slug"], 0), r["accounts"],
                     r["ext_accounts"] if r["ext_accounts"] is not None else "-", str(r["last_seen"] or "")[:19],
                     "ZERO USE" if not (int(r["views"]) + int(r["uses"]) + int(r["clicks"])) else ""] for r in table])]
    zeros = [r["slug"] for r in table if not (int(r["views"]) + int(r["uses"]) + int(r["clicks"]))]
    out.append(f"{len(table) - len(zeros)} features used, {len(zeros)} with zero use")
    d_rows = by("digest")
    out += ["", "Error digest (component > source > intent > code; accounts = distinct people, int = team, ext = customers)",
            _table(["component", "source", "intent", "code", "count", "int", "ext", "first_seen", "last_seen", "versions"],
                   [[r["component"], r["source"] or "", r["intent"], r["code"], r["n"], r["internal_accounts"],
                     r["external_accounts"], str(r["first_seen"])[:19], str(r["last_seen"])[:19], r["versions"] or "-"]
                    for r in d_rows]) if d_rows else "none recorded"]
    e_rows = sorted(by("error"), key=lambda r: -int(r["n"]))
    out += ["", "Errors (client, server, worker, model, chat)",
            _table(["feature", "source", "code", "status", "count", "accts"],
                   [[r["feature"], r["source"], r["error_code"], r["status"] or "", r["n"], r["accounts"]] for r in e_rows])
            if e_rows else "none recorded"]
    u_rows = by("user_error")
    out += ["", "User-visible errors by source and intent (what the founder saw)",
            _table(["source", "intent", "feature", "code", "count", "accts", "first_seen", "last_seen", "message"],
                   [[r["error_source"], r["intent"], r["feature"], r["error_code"], r["n"], r["accounts"],
                     str(r["first_seen"])[:19], str(r["last_seen"])[:19], r["sample"]] for r in u_rows])
            if u_rows else "none recorded (a client older than the reporter, or no user-visible errors)"]
    t_rows = by("transient_error")
    out += ["", "Transient states (retried / expected; not counted as errors)",
            _table(["feature", "context", "detail", "count", "repeats", "accts", "last_seen"],
                   [[r["feature"], r["context"], r["detail"], r["n"], r["repeats"], r["accounts"], str(r["last_seen"])[:19]]
                    for r in t_rows]) if t_rows else "none"]
    out += ["", "Events by name", _table(["event", "count"], [[r["event_name"], r["n"]] for r in by("event")])]
    failed = {r["kind"]: int(r["n"]) for r in by("job_table")}
    seen = {r["kind"]: int(r["n"]) for r in by("job_event")}
    jobs = [[k, failed.get(k, 0), seen.get(k, 0)] for k in sorted(set(failed) | set(seen))]
    out += ["", "Failed jobs (jobs table vs job.failed rows)", _table(["kind", "in jobs table", "in analytics"], jobs) if jobs else "none"]
    flags += [f"UNRECORDED job failures for {k}: jobs table {a}, analytics {b}" for k, a, b in jobs if a > b]
    if cw is not None:
        cw_rows = [{c["field"]: c["value"] for c in r} for r in cw.get("results", [])]
        out += ["", "CloudWatch error_event lines (all prod services)",
                _table(["component", "source", "feature/intent", "code", "count"],
                       [[r.get("component") or "core", r.get("source", ""), r.get("feature") or r.get("intent", ""),
                         r.get("code", ""), r.get("n", 0)] for r in cw_rows])
                if cw_rows else "none"]
        cw_by, db_by = {}, {r["source"]: int(r["n"]) for r in by("dbcount")}
        for r in (r for r in cw_rows if not r.get("component")):  # core/worker lines are the ones mirrored in the database
            cw_by[r.get("source", "")] = cw_by.get(r.get("source", ""), 0) + int(float(r.get("n", 0)))
        for source in sorted(set(cw_by) | set(db_by)):
            if cw_by.get(source, 0) != db_by.get(source, 0):
                flags.append(f"MISMATCH source={source} db={db_by.get(source, 0)} cloudwatch={cw_by.get(source, 0)}")
    out += ["", *(flags or ["OK: no mismatches"])]
    return "\n".join(out)


def parse_rows(text):
    return [json.loads(line.split(PREFIX, 1)[1]) for line in text.splitlines() if PREFIX in line]


def main(argv):
    import argparse

    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rows", required=True, help="file with VERIFY_ROW lines ('-' for stdin)")
    ap.add_argument("--cw", help="get-query-results JSON from CloudWatch Logs Insights")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--json", action="store_true", help="print the parsed rows instead of tables")
    args = ap.parse_args(argv)
    rows = parse_rows(sys.stdin.read() if args.rows == "-" else open(args.rows).read())
    cw = json.load(open(args.cw)) if args.cw else None
    print(json.dumps(rows, indent=1) if args.json else render(rows, cw, args.hours))
    return 0


if os.environ.get("VERIFY_PAYLOAD") == "1":
    _payload()
elif __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
