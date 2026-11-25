"""Observed Supymem activity, with product outcomes separated from navigation.

This is intentionally a live 30-day read over first-party receipts. Unknown
measures are null, never estimates from a screen view or an idle agent portrait.
"""
from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from itertools import pairwise
from statistics import median
from zoneinfo import ZoneInfo

from core.clock import utcnow
from sqlalchemy import select

from app.db.models import (
    AnalyticsEvent,
    Charter,
    ChatMessage,
    IdeaBrief,
    Job,
    Venture,
    VentureDocument,
)
from app.db.supymem_models import (
    CodingSourcePreflight,
    CodingSourceReceipt,
    FounderRequest,
)
from app.state import db
from app.analytics.control_catalog import CONTROL_FEATURE
from app.analytics.feature_catalog import JOB_FEATURES
from app.analytics.chat_usage import ACTION_FEATURE, READ_INTENT_FEATURE, REVIEWED_CHAT_FEATURE
from app.analytics.entry_chat_usage import PREVIEW_ROUTES, ROUTE_FEATURE

IST = ZoneInfo("Asia/Kolkata")
INDEPENDENT_DEVICE_VERIFIERS = frozenset({
    "git_status_v1", "open_venture_screen_v1", "workspace_inspect_v1",
    "expected_browser_locations_v1",
})
ACTION_EVENTS = frozenset({"venture.created", "idea.confirmed", "plan.approved", "research.completed",
                           "build.succeeded", "site.published", "invite.accepted"})
JOB_AGENT_TYPES = {
    "research.": "researcher", "build.": "builder", "qa.": "qa", "deploy.": "deployment",
    "content.": "content_writer", "gtm.": "gtm_diagnosis", "plan.": "planner", "idea.": "concierge",
}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _job_type(kind: str) -> str:
    return next((label for prefix, label in JOB_AGENT_TYPES.items() if kind.startswith(prefix)), "other")


def _job_feature(kind: str) -> str:
    return next((label for prefix, label in JOB_FEATURES.items() if kind.startswith(prefix)), "other")


def _device_type(body: dict) -> str:
    manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else {}
    actions = set(manifest.get("actions") or [])
    if "editor.read" in actions:
        return "computer_use_inspect"
    if "editor.edit" in actions:
        coding = manifest.get("coding_verification") if isinstance(manifest.get("coding_verification"), dict) else {}
        provider = coding.get("provider", "unknown")
        return "coding_" + provider if provider in {"cursor", "codex", "claude", "opencode"} else "coding_unknown"
    if "terminal.execute" in actions:
        return "computer_use_terminal"
    if "app.navigate" in actions:
        return "computer_use_navigation"
    if "browser.read" in actions or "browser.interact" in actions:
        return "computer_use_browser"
    return "computer_use_other"


def _device_outcome(body: dict) -> str:
    if body.get("state") == "stopped":
        native_stop = body.get("native_stop") if isinstance(body.get("native_stop"), dict) else {}
        if native_stop.get("processes_stopped") is True and body.get("closed_at") and body.get("claimed_at") and not body.get("navigation_effect_reconciliation") and not body.get("startup_reconciliation"):
            return "verified_stop"
        return "stopped_unverified"
    if body.get("state") == "cancelled":
        return "cancelled"
    return body.get("state") or "unknown"


def _audience(person: str | None, internal_ids: set[str], external_ids: set[str]) -> str:
    if not person:
        return "unknown"
    if person in internal_ids:
        return "internal"
    return "external" if person in external_ids else "unclassified"


def _safe_dimension(value: object) -> str:
    """Never turn founder-entered text into an operator analytics dimension."""
    return value if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value) else "unclassified"


def calculate(events: list[AnalyticsEvent], jobs: list[Job], orders: list[VentureDocument],
              *, now: datetime, internal_ids: set[str], venture_names: dict[str, str],
              external_ids: set[str] | None = None,
              venture_id: str | None = None, venture_portfolio: dict | None = None,
              requests: list | None = None,
              chat_messages: list[tuple] | None = None,
              coding_sources: list[tuple[str, str, datetime]] | None = None,
              source_preflights: list[tuple[str, datetime]] | None = None) -> dict:
    """Pure metric calculation; all rows have already been access-scoped by caller."""
    now = _utc(now)
    assert now is not None
    external_ids = external_ids or set()
    today = now.astimezone(IST).date()
    thirty = now - timedelta(days=30)
    successful_people: dict[str, set[str]] = defaultdict(set)
    view_people: dict[str, set[str]] = defaultdict(set)
    actions_by_venture: Counter[str] = Counter()
    exposures: Counter[str] = Counter()
    interactions: Counter[str] = Counter()
    feature_viewers: dict[str, set[str]] = defaultdict(set)
    feature_interactors: dict[str, set[str]] = defaultdict(set)
    control_activations: Counter[str] = Counter()
    control_actors: dict[str, set[str]] = defaultdict(set)
    control_exposures: Counter[str] = Counter()
    control_viewers: dict[str, set[str]] = defaultdict(set)
    chat_prepared_actions: Counter[str] = Counter()
    chat_preparing_people: dict[str, set[str]] = defaultdict(set)
    chat_tools: Counter[str] = Counter()
    chat_tool_outcomes: dict[str, Counter[str]] = defaultdict(Counter)
    chat_tool_people: dict[str, set[str]] = defaultdict(set)
    chat_error_replies: Counter[str] = Counter()
    chat_feature_uses: Counter[str] = Counter()
    chat_feature_people: dict[str, set[str]] = defaultdict(set)
    entry_previews: Counter[str] = Counter()
    entry_confirmed: Counter[str] = Counter()
    entry_routed_features: Counter[str] = Counter()
    entry_actors: dict[str, set[str]] = defaultdict(set)
    chat_prepared_feature_uses: Counter[str] = Counter()
    mac_start_exposures: set[tuple[str, str, str]] = set()
    mac_start_attempts: set[tuple[str, str, str]] = set()
    chat_review_orders: set[tuple[str, str, str, str]] = set()
    attempt: Counter[str] = Counter()
    success: Counter[str] = Counter()
    failure: Counter[str] = Counter()
    error_codes: Counter[str] = Counter()
    chat_intents: Counter[str] = Counter()
    chat_intent_people: dict[str, set[str]] = defaultdict(set)
    chat_topics: Counter[str] = Counter()
    chat_topic_people: dict[str, set[str]] = defaultdict(set)
    chat_intent_requests = chat_work_requests = 0
    chat_received: dict[str, tuple[str, str | None, str | None, datetime]] = {}
    chat_outcome_candidates: list[tuple[str, str, str | None, str | None, str, int, datetime]] = []
    versions: Counter[str] = Counter()
    agent_started: Counter[str] = Counter()
    agent_states: dict[str, Counter[str]] = defaultdict(Counter)
    agent_in_progress: Counter[str] = Counter()
    starts_by_actor: Counter[str] = Counter()
    latest: datetime | None = None
    latest_ui: datetime | None = None
    latest_native_heartbeat: datetime | None = None
    latest_server: datetime | None = None
    session_ticks: dict[tuple[str, str], list[datetime]] = defaultdict(list)
    process_ticks: dict[tuple[str, str], list[datetime]] = defaultdict(list)

    def active(person: str | None, when: datetime | None, *, viewed: bool = False):
        if not person or not when:
            return
        day = when.astimezone(IST).date().isoformat()
        (view_people if viewed else successful_people)[day].add(person)

    for row in events:
        at = _utc(row.occurred_at)
        if at is None or at < thirty or at > now:
            continue
        latest = max(filter(None, (latest, at))) if latest else at
        if row.id.startswith("usage_"):
            if row.event_name == "desktop.process_heartbeat":
                latest_native_heartbeat = max(latest_native_heartbeat, at) if latest_native_heartbeat else at
            elif row.event_name in {"ui.screen_view", "ui.navigation", "feature.used", "ui.control_activate",
                                    "ui.control_exposure", "ui.error", "session.active"}:
                latest_ui = max(latest_ui, at) if latest_ui else at
        else:
            latest_server = max(latest_server, at) if latest_server else at
        try:
            props = json.loads(row.props_json or "{}")
        except (ValueError, TypeError):
            props = {}
        if not isinstance(props, dict):
            props = {}
        if (row.event_name == "chat.message" and row.id.startswith("ane_")
                and row.person_id and props.get("source") == "entry_chat"
                and props.get("route") in PREVIEW_ROUTES):
            entry_previews[props["route"]] += 1
            entry_actors["preview"].add(row.person_id)
            active(row.person_id, at)  # chatting with Charlie about a new venture is real use
        if (row.event_name == "entry.chat_confirmed" and row.id.startswith("ane_")
                and row.person_id and row.venture_id and props.get("source") == "entry_chat"
                and ROUTE_FEATURE.get(props.get("route")) == props.get("feature")):
            entry_confirmed[props["route"]] += 1
            entry_routed_features[props["feature"]] += 1
            entry_actors["confirmed"].add(row.person_id)
        if row.event_name == "request.received" and props.get("source") == "chat" and row.person_id:
            active(row.person_id, at)  # the app is chat-first: a message to Charlie makes the founder active that day
            categories = props.get("intent_categories")
            if isinstance(categories, list) and 1 <= len(categories) <= 4:
                chat_intent_requests += 1
                chat_received[row.id] = (row.person_id, getattr(row, "workspace_id", None), row.venture_id, at)
                chat_work_requests += int(props.get("work_requested") is True)
                for category in {_safe_dimension(item) for item in categories}:
                    chat_intents[category] += 1
                    chat_intent_people[category].add(row.person_id)
                topics = props.get("topics")
                if isinstance(topics, list) and 1 <= len(topics) <= 4:
                    for topic in {_safe_dimension(item) for item in topics}:
                        chat_topics[topic] += 1
                        chat_topic_people[topic].add(row.person_id)
                # This closed marker is written only after the from-chat Mac
                # endpoint saves a review order. A chat intent or offered card
                # alone is not an invoked feature action.
                if (props.get("invocation") == "mac_review_prepared"
                        and categories == ["computer_use"] and props.get("work_requested") is True):
                    chat_prepared_actions["computer_use"] += 1
                    chat_preparing_people["computer_use"].add(row.person_id)
                    order_id = props.get("order_id")
                    workspace_id = getattr(row, "workspace_id", None)
                    if (isinstance(order_id, str) and re.fullmatch(r"dwo_[A-Za-z0-9]+", order_id)
                            and isinstance(workspace_id, str) and workspace_id and row.venture_id):
                        chat_review_orders.add((row.person_id, workspace_id, row.venture_id, order_id))
        if row.event_name == "request.outcome" and props.get("source") == "chat" and row.person_id:
            ref, kind, count = (props.get("request_event_id"), props.get("response_kind"),
                                props.get("offered_actions"))
            if (isinstance(ref, str) and re.fullmatch(r"ane_[a-z0-9]+", ref)
                    and kind in {"text", "navigation", "confirmation", "needs_you", "next_up", "other"}
                    and type(count) is int and 0 <= count <= 20):
                chat_outcome_candidates.append((ref, row.person_id, getattr(row, "workspace_id", None),
                                                row.venture_id, kind, count, at))
        feature = props.get("feature")
        if row.event_name == "chat.tool_invoked" and row.person_id:
            tool, outcome = props.get("tool"), props.get("outcome")
            if (isinstance(tool, str) and (ACTION_FEATURE.get(tool) or READ_INTENT_FEATURE.get(tool) or REVIEWED_CHAT_FEATURE.get(tool)) == feature
                    and outcome in {"returned", "raised"}):
                chat_tools[tool] += 1
                chat_tool_outcomes[tool][outcome] += 1
                chat_tool_people[tool].add(row.person_id)
        if (row.event_name == "chat.reply_error" and row.id.startswith("ane_")
                and row.person_id and props.get("source") == "chat"
                and props.get("failure_kind") == "provider_call"):
            chat_error_replies["provider_call"] += 1
        if (row.event_name == "feature.used" and props.get("source") == "chat"
                and isinstance(row.person_id, str) and row.person_id.strip()):
            tool = props.get("tool")
            if isinstance(tool, str) and (ACTION_FEATURE.get(tool) or READ_INTENT_FEATURE.get(tool) or REVIEWED_CHAT_FEATURE.get(tool)) == feature:
                chat_feature_uses[feature] += 1
                chat_feature_people[feature].add(row.person_id)
            elif (feature == "computer_use" and props.get("invocation") == "mac_review_prepared"
                  and isinstance(props.get("order_id"), str)
                  and re.fullmatch(r"dwo_[A-Za-z0-9]+", props["order_id"])):
                chat_prepared_feature_uses[feature] += 1
                chat_feature_people[feature].add(row.person_id)
        if row.event_name == "ui.screen_view" and row.id.startswith("usage_") and isinstance(feature, str):
            exposures[feature] += 1
            if row.person_id:
                feature_viewers[feature].add(row.person_id)
            active(row.person_id, at, viewed=True)
        if row.event_name == "feature.used" and row.id.startswith("usage_") and isinstance(feature, str):
            interactions[feature] += 1
            if row.person_id:
                feature_interactors[feature].add(row.person_id)
        if row.event_name == "ui.control_activate" and row.id.startswith("usage_"):
            control_id = props.get("control_id")
            if isinstance(control_id, str) and CONTROL_FEATURE.get(control_id) == feature:
                control_activations[control_id] += 1
                if row.person_id:
                    control_actors[control_id].add(row.person_id)
                if control_id == "mac_start" and row.person_id and row.venture_id and isinstance(props.get("order_id"), str):
                    mac_start_attempts.add((row.person_id, row.venture_id, props["order_id"]))
        if row.event_name == "ui.control_exposure" and row.id.startswith("usage_"):
            control_id = props.get("control_id")
            if isinstance(control_id, str) and CONTROL_FEATURE.get(control_id) == feature:
                control_exposures[control_id] += 1
                if row.person_id:
                    control_viewers[control_id].add(row.person_id)
            if (props.get("control_id") == "mac_start" and feature == "computer_use"
                    and row.person_id and row.venture_id and isinstance(props.get("order_id"), str)):
                mac_start_exposures.add((row.person_id, row.venture_id, props["order_id"]))
        if row.event_name == "ui.error" and row.id.startswith("usage_"):
            code = props.get("error_code")
            if isinstance(code, str):
                error_codes[code] += 1
        if row.event_name == "session.active" and row.id.startswith("usage_"):
            sid = props.get("session_id")
            if row.person_id and isinstance(sid, str):
                session_ticks[(row.person_id, sid)].append(at)
        if row.event_name == "desktop.process_heartbeat" and row.id.startswith("usage_"):
            sid = props.get("session_id")
            if row.person_id and isinstance(sid, str):
                process_ticks[(row.person_id, sid)].append(at)
        version = props.get("app_version")
        if row.id.startswith("usage_") and isinstance(version, str):
            versions[version] += 1
        if row.event_name in ACTION_EVENTS and row.person_id:
            active(row.person_id, at)
            feature_for_event = {"venture.created": "ventures", "idea.confirmed": "idea",
                                 "plan.approved": "planning", "research.completed": "research",
                                 "build.succeeded": "builds", "site.published": "website",
                                 "invite.accepted": "team"}.get(row.event_name, "other")
            success[feature_for_event] += 1
            attempt[feature_for_event] += 1
            if row.venture_id:
                actions_by_venture[row.venture_id] += 1

    for row in jobs:
        at = _utc(row.created_at)
        if at is None or at < thirty or at > now:
            continue
        # A build.watch job polls an existing run. Each poll is an operational
        # receipt, not a new agent or another attempt to build a product.
        if row.kind == "build.watch":
            continue
        feature = _job_feature(row.kind)
        kind = _job_type(row.kind)
        started = row.started_at is not None
        agent_started[kind] += int(started)
        agent_states[kind][row.status] += 1
        agent_in_progress[kind] += int(row.status in {"running", "leased"})
        if started:
            try:
                payload = json.loads(row.payload_json or "{}")
            except (ValueError, TypeError):
                payload = {}
            actor = payload.get("_actor_subject") if isinstance(payload, dict) else None
            if isinstance(actor, str) and actor:
                starts_by_actor[actor] += 1
        attempt[feature] += 1
        if row.status in {"done", "succeeded"}:
            success[feature] += 1
            if row.venture_id:
                actions_by_venture[row.venture_id] += 1
        elif row.status in {"failed", "failed_dead", "poison", "cancelled"}:
            failure[feature] += 1

    order_states: Counter[str] = Counter()
    order_types: Counter[str] = Counter()
    goal_verified_orders = closed_without_goal_verification = 0
    goal_verified_by_type: Counter[str] = Counter()
    closed_without_goal_verification_by_type: Counter[str] = Counter()
    order_durations: list[float] = []
    approved_ceiling_micro = settled_micro = 0
    approval_funnel: Counter[str] = Counter()
    approval_by_type: dict[str, Counter[str]] = defaultdict(Counter)
    approval_seconds: list[float] = []
    mac_start_funnel: Counter[str] = Counter()
    chat_mac_funnel: Counter[str] = Counter()
    chat_mac_people: dict[str, set[str]] = defaultdict(set)
    mac_start_exposed_people: set[str] = set()
    mac_start_attempting_people: set[str] = set()
    for row in orders:
        at = _utc(row.created_at)
        if at is None or at < thirty or at > now:
            continue
        try:
            body = json.loads(row.provenance_json or "{}")
        except (ValueError, TypeError):
            continue
        if not isinstance(body, dict):
            continue
        outcome = _device_outcome(body)
        native_stop = body.get("native_stop") if isinstance(body.get("native_stop"), dict) else {}
        verification_kind = native_stop.get("verification_kind")
        goal_verified = (outcome == "verified_stop" and native_stop.get("task_verified") is True
                         and isinstance(verification_kind, str)
                         and verification_kind in INDEPENDENT_DEVICE_VERIFIERS)
        kind = _device_type(body)
        closed_without_goal = bool(body.get("claimed_at") and body.get("closed_at")) and not goal_verified
        goal_verified_orders += int(goal_verified)
        closed_without_goal_verification += int(closed_without_goal)
        goal_verified_by_type[kind] += int(goal_verified)
        closed_without_goal_verification_by_type[kind] += int(closed_without_goal)
        order_states[outcome] += 1
        order_types[kind] += 1
        approval_funnel["prepared"] += 1
        approval_by_type[kind]["prepared"] += 1
        for key, stage in (("approved_at", "approved"), ("claimed_at", "claimed"),
                           ("closed_at", "closed")):
            if body.get(key):
                approval_funnel[stage] += 1
                approval_by_type[kind][stage] += 1
        if body.get("approved_at"):
            try:
                approved = _utc(datetime.fromisoformat(body["approved_at"]))
                prepared = _utc(row.created_at)
                elapsed = (approved - prepared).total_seconds() if approved and prepared else -1
                if elapsed >= 0:
                    approval_seconds.append(elapsed)
            except (TypeError, ValueError):
                pass
        agent_started[kind] += int(bool(body.get("claimed_at")))
        agent_states[kind][outcome] += 1
        agent_in_progress[kind] += int(bool(body.get("claimed_at")) and not body.get("closed_at") and outcome not in {"cancelled", "verified_stop", "stopped_unverified"})
        actor = body.get("actor_id")
        order_id = getattr(row, "id", None)
        workspace_id = body.get("workspace_id")
        chat_order_key = (actor, workspace_id, row.venture_id, order_id)
        if (isinstance(actor, str) and isinstance(workspace_id, str) and isinstance(order_id, str)
                and chat_order_key in chat_review_orders):
            for stage, reached in (("prepared", True), ("approved", bool(body.get("approved_at"))),
                                   ("claimed", bool(body.get("claimed_at"))), ("verified", goal_verified)):
                if reached:
                    chat_mac_funnel[stage] += 1
                    chat_mac_people[stage].add(actor)
            chat_mac_funnel["closed_unverified"] += int(closed_without_goal)
            chat_mac_funnel["still_open"] += int(bool(body.get("claimed_at")) and not body.get("closed_at"))
        order_key = (actor, row.venture_id, order_id) if isinstance(actor, str) and actor and isinstance(order_id, str) else None
        exposed = bool(order_key and order_key in mac_start_exposures)
        attempted = bool(order_key and order_key in mac_start_attempts)
        claimed = bool(body.get("claimed_at"))
        if exposed:
            mac_start_funnel["eligible_orders"] += 1
            mac_start_exposed_people.add(actor)
        if attempted:
            mac_start_funnel["attempted_orders"] += 1
            mac_start_attempting_people.add(actor)
            if claimed:
                mac_start_funnel["accepted_starts"] += 1
                mac_start_funnel["verified_goals"] += int(goal_verified)
                mac_start_funnel["closed_unverified"] += int(closed_without_goal)
                mac_start_funnel["still_open"] += int(not body.get("closed_at"))
        if claimed and not attempted:
            mac_start_funnel["claims_without_named_attempt"] += 1
        if body.get("claimed_at") and isinstance(actor, str) and actor:
            starts_by_actor[actor] += 1
        attempt["computer_use"] += int(bool(body.get("claimed_at")))
        manifest = body.get("manifest") if isinstance(body.get("manifest"), dict) else {}
        try:
            approved_ceiling_micro += int(manifest.get("credit_ceiling_micro") or 0) if body.get("approved_at") else 0
            settled_micro += int(body.get("settled_micro") or 0)
        except (ValueError, TypeError):
            pass
        if outcome == "verified_stop":
            # A clean executor stop is operational evidence, not goal success.
            if goal_verified and row.venture_id:
                actions_by_venture[row.venture_id] += 1
            try:
                closed_at = _utc(datetime.fromisoformat(body["closed_at"]))
                claimed_at = _utc(datetime.fromisoformat(body["claimed_at"]))
                if goal_verified:
                    active(body.get("actor_id"), closed_at)
                elapsed = (closed_at - claimed_at).total_seconds()
                if elapsed >= 0:
                    order_durations.append(elapsed)
            except (KeyError, ValueError, TypeError):
                pass
        # A clean process stop proves bounded executor closure, not success of
        # the founder's requested goal. The goal receipt is reported separately.

    request_categories: Counter[str] = Counter()
    request_sources: Counter[str] = Counter()
    request_outcomes: Counter[str] = Counter()
    for row in requests or []:
        at = _utc(row.created_at)
        if at is None or not thirty <= at <= now:
            continue
        request_categories[_safe_dimension(row.intent_category)] += 1
        request_sources[_safe_dimension(row.source)] += 1
        request_outcomes[_safe_dimension(row.outcome)] += 1
    chat_roles: Counter[str] = Counter()
    for role, created_at, *author in chat_messages or []:
        at = _utc(created_at)
        if at is not None and thirty <= at <= now and role in {"user", "assistant"}:
            chat_roles[role] += 1
            if role == "user" and author and isinstance(author[0], str) and author[0]:
                active(author[0], at)  # every message a founder sends Charlie counts, whatever path answered it

    # Legacy interaction heartbeats carry no duration. Adjacent foreground
    # ticks bound observed attention; a gap over two minutes is a new session.
    attention_seconds: dict[str, float] = defaultdict(float)
    for (person, _session), ticks in session_ticks.items():
        ordered = sorted(set(ticks))
        for left, right in pairwise(ordered):
            gap = (right - left).total_seconds()
            if 0 < gap <= 120:
                attention_seconds[person] += gap

    # Native process heartbeats are a separate measure from foreground
    # attention. A laptop sleep or network pause over two minutes adds no time.
    process_seconds: dict[str, float] = defaultdict(float)
    for (person, _session), ticks in process_ticks.items():
        ordered = sorted(set(ticks))
        for left, right in pairwise(ordered):
            gap = (right - left).total_seconds()
            if 0 < gap <= 120:
                process_seconds[person] += gap

    # First action day per person from the same action-active definition.
    first_action_day: dict[str, str] = {}
    for day, people_set in successful_people.items():
        for person in people_set:
            if person not in first_action_day or day < first_action_day[person]:
                first_action_day[person] = day

    def _retention_counts(people_days: dict[str, str]) -> tuple[int, int, int, int]:
        eligible1 = retained1 = eligible7 = retained7 = 0
        for person, first in people_days.items():
            try:
                first_date = date.fromisoformat(first)
            except ValueError:
                continue
            day_plus_1 = (first_date + timedelta(days=1)).isoformat()
            day_plus_7 = (first_date + timedelta(days=7)).isoformat()
            if first_date + timedelta(days=1) <= today:
                eligible1 += 1
                if person in successful_people.get(day_plus_1, set()):
                    retained1 += 1
            if first_date + timedelta(days=7) <= today:
                eligible7 += 1
                if person in successful_people.get(day_plus_7, set()):
                    retained7 += 1
        return eligible1, retained1, eligible7, retained7

    external_first = {
        person: first for person, first in first_action_day.items()
        if _audience(person, internal_ids, external_ids) == "external"
    }
    internal_first = {
        person: first for person, first in first_action_day.items()
        if _audience(person, internal_ids, external_ids) == "internal"
    }
    d1_eligible, d1_retained, d7_eligible, d7_retained = _retention_counts(external_first)
    int_d1_eligible, int_d1_retained, int_d7_eligible, int_d7_retained = _retention_counts(internal_first)

    # Activation latency uses identity-linked signup → first venture / first action.
    # Internal operator person ids are reported separately, never as product retention.
    signup_at: dict[str, datetime] = {}
    signup_at_internal: dict[str, datetime] = {}
    first_venture_at: dict[str, datetime] = {}
    first_action_at: dict[str, datetime] = {}
    for row in events:
        at = _utc(row.occurred_at)
        if at is None or at < thirty or at > now or not row.person_id:
            continue
        if row.event_name == "signup":
            audience = _audience(row.person_id, internal_ids, external_ids)
            bucket = signup_at_internal if audience == "internal" else signup_at if audience == "external" else None
            if bucket is not None:
                prior = bucket.get(row.person_id)
                if prior is None or at < prior:
                    bucket[row.person_id] = at
        if row.event_name == "venture.created":
            prior = first_venture_at.get(row.person_id)
            if prior is None or at < prior:
                first_venture_at[row.person_id] = at
        if row.event_name in ACTION_EVENTS:
            prior = first_action_at.get(row.person_id)
            if prior is None or at < prior:
                first_action_at[row.person_id] = at

    def _activation_latencies(signups: dict[str, datetime]) -> tuple[list[float], list[float]]:
        ventures = [
            (first_venture_at[pid] - signed).total_seconds()
            for pid, signed in signups.items()
            if pid in first_venture_at and first_venture_at[pid] >= signed
        ]
        actions = [
            (first_action_at[pid] - signed).total_seconds()
            for pid, signed in signups.items()
            if pid in first_action_at and first_action_at[pid] >= signed
        ]
        return ventures, actions

    venture_latencies, action_latencies = _activation_latencies(signup_at)
    int_venture_latencies, int_action_latencies = _activation_latencies(signup_at_internal)

    def people(days: int, source: dict[str, set[str]]) -> set[str]:
        return set().union(*(source.get((today - timedelta(days=i)).isoformat(), set()) for i in range(days)))

    dau_people = successful_people.get(today.isoformat(), set())
    wau_people = people(7, successful_people)
    mau_people = people(30, successful_people)
    observed_dau_people = dau_people | view_people.get(today.isoformat(), set())
    observed_wau_people = wau_people | people(7, view_people)
    observed_mau_people = mau_people | people(30, view_people)
    viewed_only = view_people.get(today.isoformat(), set()) - dau_people
    chat_response_kinds: Counter[str] = Counter()
    chat_offered_actions = 0
    paired_chat_requests: set[str] = set()
    chat_response_seconds: list[float] = []
    for ref, person, workspace, venture, kind, count, answered_at in sorted(
            chat_outcome_candidates, key=lambda candidate: (candidate[6], candidate[0])):
        received = chat_received.get(ref)
        if (ref in paired_chat_requests or received is None
                or received[:3] != (person, workspace, venture)
                or answered_at < received[3]):
            continue
        paired_chat_requests.add(ref)
        chat_response_kinds[kind] += 1
        chat_offered_actions += count
        chat_response_seconds.append((answered_at - received[3]).total_seconds())
    named_by_feature: Counter[str] = Counter()
    named_people_by_feature: dict[str, set[str]] = defaultdict(set)
    for control_id, feature in CONTROL_FEATURE.items():
        if control_activations[control_id]:
            named_by_feature[feature] += control_activations[control_id]
            named_people_by_feature[feature].update(control_actors[control_id])
    all_features = sorted(set(exposures) | set(interactions) | set(attempt) | set(success) |
                          set(failure) | set(chat_prepared_actions) | set(named_by_feature) |
                          set(chat_feature_uses) | set(chat_prepared_feature_uses))
    features = [{"feature": feature, "views": exposures[feature], "viewing_people": len(feature_viewers[feature]),
                 "interactions": interactions[feature], "interacting_people": len(feature_interactors[feature]),
                 "named_activations": named_by_feature[feature],
                 "named_actors": len(named_people_by_feature[feature]),
                 "chat_prepared_actions": chat_prepared_actions[feature],
                 "chat_preparing_people": len(chat_preparing_people[feature]),
                 "chat_initiated_uses": chat_feature_uses[feature] + chat_prepared_feature_uses[feature],
                 "chat_initiating_people": len(chat_feature_people[feature]),
                 "attempts": attempt[feature],
                 "successes": None if feature == "computer_use" else success[feature],
                 "failures": None if feature == "computer_use" else failure[feature],
                 "success_rate": round(success[feature] / attempt[feature], 3)
                 if feature != "computer_use" and attempt[feature] >= 10 else None}
                for feature in all_features]
    ventures = [{"venture_id": vid, "name": venture_names.get(vid, vid), "successful_actions": count}
                for vid, count in actions_by_venture.most_common(10)]
    top_feature = max(success.items(), key=lambda item: item[1])[0] if success else None
    top_venture = ventures[0]["name"] if ventures else None
    process_total = round(sum(process_seconds.values())) if process_seconds else None
    attention_total = round(sum(attention_seconds.values())) if attention_seconds else None
    process_summary = (f"{process_total} authenticated desktop process-seconds were observed."
                       if process_total is not None else "Authenticated desktop process time is unavailable because no adjacent native ticks were observed.")
    retention_note = (
        f"D1 retention {round(100 * d1_retained / d1_eligible)}% of {d1_eligible} eligible; "
        if d1_eligible else "D1 retention not measured yet; "
    )
    active_people_label = "person" if len(mau_people) == 1 else "people"
    summary = (f"{len(mau_people)} observed action-active {active_people_label} in the last 30 days; "
               f"top venture by completed platform receipts: {top_venture or 'unavailable'}; "
               f"most completed server-receipt category: {top_feature or 'unavailable'}; "
               f"{sum(agent_started.values())} recorded agent/workflow starts, {goal_verified_orders} goal-verified Mac actions, "
               f"and {order_states['verified_stop']} clean executor stops. "
               f"{retention_note}"
               f"Hosted-product users are measured separately; {process_summary}")
    return {
        "window": "trailing_30_days", "timezone": "Asia/Kolkata", "as_of": now.isoformat(),
        "newest_event_at": latest.isoformat() if latest else None,
        "newest_ui_event_at": latest_ui.isoformat() if latest_ui else None,
        "newest_native_heartbeat_at": latest_native_heartbeat.isoformat() if latest_native_heartbeat else None,
        "newest_server_event_at": latest_server.isoformat() if latest_server else None,
        "venture_id": venture_id,
        "definitions": {
            "dau": "Distinct authenticated people who chatted with Charlie, completed a saved server action or an independently goal-verified Mac task on today's Asia/Kolkata calendar day. A clean executor stop alone does not qualify.",
            "wau": "Same people over seven Asia/Kolkata calendar days including today.",
            "mau": "Same people over 30 Asia/Kolkata calendar days including today.",
            "app_reach": "Distinct authenticated people with a validated Supymem screen view or action-active receipt in the period. Internal and unclassified people are included; this is observed reach, not external customer activity or hosted-product traffic.",
            "screen_only": "People with a validated screen view today but no saved action today.",
            "attention": "Observed foreground interaction heartbeat gaps of at most 120 seconds; a lower bound, not full time in app.",
            "process_uptime": "Authenticated native desktop process-heartbeat gaps of at most 120 seconds; sleep, offline gaps and time before sign-in add zero.",
            "d1": "Share of explicitly designated external action-active people whose first action day is at least one Asia/Kolkata day ago and who were also action-active on the next calendar day. Unclassified and internal accounts are excluded.",
            "d7": "Same definition with a seven-day return window for the external cohort. Unavailable until a cohort day has fully elapsed.",
            "activation": "Median seconds from a saved signup event for an explicitly designated external person to their first venture.created or action-active receipt. Unclassified and internal accounts are excluded; internal signups are reported separately.",
        },
        "activity": {"dau": len(dau_people), "wau": len(wau_people), "mau": len(mau_people),
                     "observed_dau": len(observed_dau_people), "observed_wau": len(observed_wau_people),
                     "observed_mau": len(observed_mau_people),
                     "screen_only_today": len(viewed_only),
                     "stickiness": round(len(dau_people) / len(mau_people), 3) if mau_people else None,
                     "internal_mau": len({p for p in mau_people if _audience(p, internal_ids, external_ids) == "internal"}),
                     "unclassified_mau": len({p for p in mau_people if _audience(p, internal_ids, external_ids) == "unclassified"}),
                     "external_customer_mau": len({p for p in mau_people if _audience(p, internal_ids, external_ids) == "external"})
                     if external_ids else None},
        "retention": {
            "d1_rate": round(d1_retained / d1_eligible, 3) if d1_eligible else None,
            "d7_rate": round(d7_retained / d7_eligible, 3) if d7_eligible else None,
            "d1_eligible": d1_eligible if d1_eligible else None,
            "d1_retained": d1_retained if d1_eligible else None,
            "d7_eligible": d7_eligible if d7_eligible else None,
            "d7_retained": d7_retained if d7_eligible else None,
            "internal_d1_rate": round(int_d1_retained / int_d1_eligible, 3) if int_d1_eligible else None,
            "internal_d7_rate": round(int_d7_retained / int_d7_eligible, 3) if int_d7_eligible else None,
            "internal_d1_eligible": int_d1_eligible if int_d1_eligible else None,
            "internal_d1_retained": int_d1_retained if int_d1_eligible else None,
            "internal_d7_eligible": int_d7_eligible if int_d7_eligible else None,
            "internal_d7_retained": int_d7_retained if int_d7_eligible else None,
        },
        "activation": {
            "signups_observed": len(signup_at) if signup_at else None,
            "median_seconds_signup_to_first_venture": round(median(venture_latencies)) if venture_latencies else None,
            "median_seconds_signup_to_first_action": round(median(action_latencies)) if action_latencies else None,
            "signup_to_venture_cohort": len(venture_latencies) if venture_latencies else None,
            "signup_to_action_cohort": len(action_latencies) if action_latencies else None,
            "internal_signups_observed": len(signup_at_internal) if signup_at_internal else None,
            "internal_median_seconds_signup_to_first_venture": (
                round(median(int_venture_latencies)) if int_venture_latencies else None
            ),
            "internal_median_seconds_signup_to_first_action": (
                round(median(int_action_latencies)) if int_action_latencies else None
            ),
            "internal_signup_to_venture_cohort": len(int_venture_latencies) if int_venture_latencies else None,
            "internal_signup_to_action_cohort": len(int_action_latencies) if int_action_latencies else None,
        },
        "attention": {"observed_seconds": attention_total,
                      "mean_observed_seconds_per_person": round(sum(attention_seconds.values()) / len(attention_seconds)) if attention_seconds else None,
                      "process_uptime_seconds": process_total,
                      "mean_process_uptime_seconds_per_person": round(sum(process_seconds.values()) / len(process_seconds)) if process_seconds else None},
        "agents": {"started_by_type": {kind: count for kind, count in agent_started.items() if count},
                   "states_by_type": {kind: dict(states) for kind, states in agent_states.items()},
                   "in_progress_by_type": {kind: count for kind, count in agent_in_progress.items() if count},
                   "attributed_starting_people": len(starts_by_actor),
                   "attributed_starts": sum(starts_by_actor.values()),
                   "mean_per_attributed_person": round(sum(starts_by_actor.values()) / len(starts_by_actor), 2) if starts_by_actor else None,
                   "median_per_attributed_person": median(starts_by_actor.values()) if starts_by_actor else None,
                   "mean_per_active_person": None, "median_per_active_person": None},
        "coding_source_custody": {
            "orders_by_state": dict(Counter(state for state, _venture, _created in (coding_sources or []))),
            "ventures_with_stored_source": len({venture for state, venture, _created in (coding_sources or [])
                                                if state == "stored"}),
            "coverage": "server_confirmed_source_transport_only_no_qa_or_hosting",
        },
        "coding_source_preflight": {
            "reports_by_status": dict(Counter(status for status, _created in (source_preflights or []))),
            "coverage": "read_only_contract_inspection_no_runtime_qa_or_hosting",
        },
        "computer_use": {"orders_by_state": dict(order_states), "orders_by_type": dict(order_types),
                         "goal_verified_orders": goal_verified_orders,
                         "goal_verified_by_type": {kind: count for kind, count in goal_verified_by_type.items() if count},
                         "closed_without_goal_verification": closed_without_goal_verification,
                         "closed_without_goal_verification_by_type": {
                             kind: count for kind, count in closed_without_goal_verification_by_type.items() if count},
                         "approval_funnel": dict(approval_funnel),
                         "approval_funnel_by_type": {kind: dict(counts) for kind, counts in approval_by_type.items()},
                         "median_prepared_to_approval_seconds": round(median(approval_seconds)) if approval_seconds else None,
                         "median_claim_to_verified_stop_seconds": round(median(order_durations)) if order_durations else None,
                         "approved_credit_ceiling_micro_observed": approved_ceiling_micro,
                         "settled_micro_observed": settled_micro},
        "founder_requests": {"saved_by_category": dict(request_categories),
                             "saved_by_source": dict(request_sources),
                             "saved_by_outcome": dict(request_outcomes),
                             "coverage": "saved_founder_requests_only_no_person_attribution"},
        "chat": {"saved_messages_by_role": dict(chat_roles),
                 "entry_previews_by_route": dict(entry_previews),
                 "entry_confirmations_by_route": dict(entry_confirmed),
                 "entry_routed_features": dict(entry_routed_features),
                 "entry_previewing_people": len(entry_actors["preview"]),
                 "entry_confirming_people": len(entry_actors["confirmed"]),
                 "classified_user_requests": chat_intent_requests,
                 "work_requested": chat_work_requests,
                 "paired_saved_responses": len(paired_chat_requests),
                 "without_paired_response_receipt": chat_intent_requests - len(paired_chat_requests),
                 "median_saved_response_seconds": round(median(chat_response_seconds), 2) if chat_response_seconds else None,
                 "saved_responses_over_30_seconds": sum(seconds > 30 for seconds in chat_response_seconds),
                 "response_kinds": dict(chat_response_kinds),
                 "offered_action_cards": chat_offered_actions,
                 "intent_categories": dict(chat_intents),
                 "distinct_requesting_people_by_intent": {kind: len(people) for kind, people in chat_intent_people.items()},
                 "topics": dict(chat_topics),
                 "distinct_requesting_people_by_topic": {kind: len(people) for kind, people in chat_topic_people.items()},
                 "tool_invocations": dict(chat_tools),
                 "tool_outcomes": {tool: dict(outcomes) for tool, outcomes in chat_tool_outcomes.items()},
                 "tool_actors": {tool: len(people) for tool, people in chat_tool_people.items()},
                 "saved_error_replies_by_kind": dict(chat_error_replies),
                 "feature_uses_from_returned_tools": dict(chat_feature_uses),
                 "feature_uses_from_prepared_mac_reviews": dict(chat_prepared_feature_uses),
                 "coverage": "closed_server_chat_action_dispatch_and_return_not_downstream_completion_or_answer_quality_provider_error_replies_separate_historical_requests_unpaired"},
        "features": features,
        "controls": [{"control_id": control_id, "feature": feature,
                      "exposures": control_exposures[control_id], "viewers": len(control_viewers[control_id]),
                      "activations": control_activations[control_id], "actors": len(control_actors[control_id])}
                     for control_id, feature in CONTROL_FEATURE.items()],
        "mac_start_funnel": {**{key: (mac_start_funnel[key] if mac_start_exposures or mac_start_attempts else None) for key in (
            "eligible_orders", "attempted_orders", "accepted_starts", "verified_goals",
            "closed_unverified", "still_open", "claims_without_named_attempt")},
            "eligible_people": len(mac_start_exposed_people) if mac_start_exposures or mac_start_attempts else None,
            "attempting_people": len(mac_start_attempting_people) if mac_start_exposures or mac_start_attempts else None,
            "coverage": "30_day_created_orders_with_client_visible_exposure_and_named_attempt_joined_to_server_claim_and_verified_stop"},
        "chat_mac_funnel": {
            **{stage: chat_mac_funnel[stage] for stage in ("prepared", "approved", "claimed", "verified",
                                                         "closed_unverified", "still_open")},
            "people_by_stage": {stage: len(chat_mac_people[stage]) for stage in ("prepared", "approved", "claimed", "verified")},
            "coverage": "30_day_chat_prepared_orders_joined_by_saved_order_actor_workspace_and_venture;_independent_native_verifiers_only;_no_historical_backfill"},
        "venture_ranking": ventures,
        "venture_portfolio": venture_portfolio if venture_id is None else None,
        "errors_by_code": dict(error_codes),
        "app_versions_by_event": dict(versions), "summary": summary,
        "coverage": {"hosted_product": "separate_app_events", "public_site_visitors": "not_included",
                     "app_reach": "validated_screen_views_or_action_receipts;_lower_bound_if_client_telemetry_missing",
                     "external_customers": "explicit_external_person_ids" if external_ids else "unclassified_until_admin_designation",
                     "agent_per_person": "partial_saved_actor_only",
                     "process_uptime": "authenticated_native_heartbeat_observed" if process_total is not None else "no_measurable_native_intervals",
                     "attention": "interaction_heartbeat_observed" if attention_total is not None else "no_measurable_attention_intervals",
                     "retention": "action_active_d1_d7" if d1_eligible or d7_eligible else "not_measured_yet",
                     "activation": "signup_linked" if signup_at else "not_measured_yet",
                     "feature_success": "server_receipts_only",
                     "named_controls": "selected_closed_ids_visible_enabled_exposure_and_activation_not_outcome",
                     "chat_prepared_actions": "closed_from_chat_mac_review_order_preparation_only_not_execution",
                     "offline_queue": "durable_localstorage"},
    }


def snapshot(*, venture_id: str | None = None, now: datetime | None = None) -> dict:
    now = _utc(now or utcnow())
    assert now is not None
    since = (now - timedelta(days=30)).replace(tzinfo=None)
    with db()() as session:
        events_query = select(AnalyticsEvent).where(AnalyticsEvent.occurred_at >= since)
        jobs_query = select(Job).where(Job.created_at >= since)
        orders_query = select(VentureDocument).where(VentureDocument.created_at >= since,
                                                     VentureDocument.kind == "device_work_order_v1")
        # Project only closed dimensions. Never load FounderRequest.text into
        # operator analytics just to count a request.
        requests_query = select(FounderRequest.created_at, FounderRequest.intent_category,
                                FounderRequest.source, FounderRequest.outcome).where(FounderRequest.created_at >= since)
        chat_query = select(ChatMessage.role, ChatMessage.created_at, ChatMessage.author_person_id).where(
            ChatMessage.created_at >= since, ChatMessage.role.in_(["user", "assistant"]))
        source_query = select(CodingSourceReceipt.state, CodingSourceReceipt.venture_id,
                              CodingSourceReceipt.created_at).where(CodingSourceReceipt.created_at >= since)
        preflight_query = select(CodingSourcePreflight.status, CodingSourcePreflight.created_at).where(
            CodingSourcePreflight.created_at >= since)
        if venture_id:
            events_query = events_query.where(AnalyticsEvent.venture_id == venture_id)
            jobs_query = jobs_query.where(Job.venture_id == venture_id)
            orders_query = orders_query.where(VentureDocument.venture_id == venture_id)
            requests_query = requests_query.where(FounderRequest.venture_id == venture_id)
            chat_query = chat_query.where(ChatMessage.venture_id == venture_id)
            source_query = source_query.where(CodingSourceReceipt.venture_id == venture_id)
            preflight_query = preflight_query.where(CodingSourcePreflight.venture_id == venture_id)
        events = session.scalars(events_query).all()
        jobs = session.scalars(jobs_query).all()
        orders = session.scalars(orders_query).all()
        requests = session.execute(requests_query).all()
        chat_messages = session.execute(chat_query).all()
        coding_sources = session.execute(source_query).all()
        source_preflights = session.execute(preflight_query).all()
        names = {}
        active_charters: dict[str, str] = {}
        stage_counts: Counter[str] = Counter()
        nonpurged_ventures: set[str] = set()
        for vid, name, stage, charter_id in session.execute(
            select(Venture.id, Venture.name, Venture.stage, Venture.active_charter_id)
        ).all():
            names[vid] = name
            if charter_id:
                active_charters[vid] = charter_id
            if venture_id is None:
                stage_counts[stage or "unknown"] += 1
                if stage != "purged":
                    nonpurged_ventures.add(vid)
        archetype_counts: Counter[str] = Counter()
        if venture_id is None:
            # The current idea journey saves its classification in a charter;
            # the older intake path saved an IdeaBrief. Prefer an active
            # charter, then the newest charter, then the newest legacy brief.
            # Never classify private free-text ideas on dashboard reads.
            charter_types: dict[str, str] = {}
            for charter_id, vid, raw in session.execute(
                select(Charter.id, Charter.venture_id, Charter.content_json)
                .order_by(Charter.created_at.desc())
            ).all():
                try:
                    content = json.loads(raw or "{}")
                except (ValueError, TypeError):
                    continue
                kind = content.get("build_archetype") if isinstance(content, dict) else None
                if isinstance(kind, str) and kind in {"crud_web", "integration_bot", "marketplace", "unsupported"} and (
                    vid not in charter_types or active_charters.get(vid) == charter_id
                ):
                    charter_types[vid] = kind
            brief_types: dict[str, str] = {}
            for vid, archetype in session.execute(select(IdeaBrief.venture_id, IdeaBrief.build_archetype)
                                                  .order_by(IdeaBrief.created_at.desc())).all():
                if vid not in brief_types and archetype in {"crud_web", "integration_bot", "marketplace", "unsupported"}:
                    brief_types[vid] = archetype
            for vid in nonpurged_ventures:
                archetype_counts[charter_types.get(vid) or brief_types.get(vid) or "not_classified"] += 1
    internal_ids = {part.strip() for part in os.getenv("SUPYMEM_ANALYTICS_INTERNAL_PERSON_IDS", "").split(",") if part.strip()}
    external_ids = {part.strip() for part in os.getenv("SUPYMEM_ANALYTICS_EXTERNAL_PERSON_IDS", "").split(",") if part.strip()}
    return calculate(events, jobs, orders, now=now, internal_ids=internal_ids, external_ids=external_ids,
                     venture_names=names, venture_id=venture_id,
                     requests=requests, chat_messages=chat_messages, coding_sources=coding_sources,
                     source_preflights=source_preflights,
                     venture_portfolio={"total": len(names), "nonpurged_total": len(nonpurged_ventures),
                                        "by_stage": dict(stage_counts),
                                        "by_build_archetype": dict(archetype_counts)} if venture_id is None else None)
