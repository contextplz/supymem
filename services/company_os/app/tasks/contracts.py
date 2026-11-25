"""Validated task contracts and code-owned scheduling/stop decisions."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dateutil.rrule import rrulestr
from pydantic import BaseModel, ConfigDict, Field, model_serializer, model_validator


class Budget(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tokens: int = Field(100_000, ge=1, le=2_000_000)
    cost_micro: int = Field(500_000, ge=0, le=50_000_000)
    wall_seconds: int = Field(1800, ge=1, le=86400)
    attempts: int = Field(12, ge=1, le=100)
    tool_calls: int = Field(30, ge=1, le=500)
    max_strategies: int = Field(3, ge=1, le=3)

    @model_validator(mode="after")
    def reasoning_default(self):
        if "cost_micro" not in self.model_fields_set:
            from app.core.local_mode import is_local
            if is_local():
                return self
            from app.tasks.reasoning import enabled, DEFAULT_COST_MICRO
            if enabled():
                self.cost_micro = DEFAULT_COST_MICRO
        return self


class StepContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    skill: str = Field(min_length=1, max_length=80)
    inputs: dict = Field(default_factory=dict)
    done_test: dict = Field(default_factory=dict)
    runs_on: str = Field("server", pattern="^(server|device|either)$")
    subgoal: str | None = Field(None, min_length=1, max_length=8000)
    output_bindings: dict[str, str] = Field(default_factory=dict)

    @model_serializer(mode="wrap")
    def serialize(self, handler):
        value = handler(self)
        if self.subgoal is None:
            value.pop("subgoal", None)
        if not self.output_bindings:
            value.pop("output_bindings", None)
        return value


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    goal: str = Field(min_length=1, max_length=8000)
    kind: str = Field("one_time", pattern="^(one_time|scheduled|event)$")
    trigger: dict = Field(default_factory=dict)
    done_test: dict = Field(default_factory=lambda: {"type": "deterministic", "spec": "All step checks pass"})
    tolerance: float = Field(0.8, ge=0, le=1)
    budget: Budget = Field(default_factory=Budget)
    estimate_seconds: int = Field(900, ge=60, le=86400)
    steps: list[StepContract] = Field(min_length=1, max_length=12)

    @model_validator(mode="after")
    def validate_checks(self):
        if self.done_test.get("type") not in {"deterministic", "rubric"}:
            raise ValueError("A deterministic check or rubric is required")
        if self.done_test["type"] == "deterministic":
            self.tolerance = 1.0
        return self


def timezone_name(value: str | None) -> str:
    if not value:
        return "UTC"
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Choose a valid timezone") from exc
    return value


def next_fire(trigger: dict, after: datetime) -> datetime | None:
    """RRULE in founder local time, UTC storage; skip nonexistent DST times."""
    zone = ZoneInfo(timezone_name(trigger.get("tz")))
    rule = str(trigger.get("rrule", ""))
    if len(rule) > 500 or not rule.startswith("FREQ=") or any(x in rule for x in ("DTSTART", "TZID", "\n", "\r")):
        raise ValueError("Invalid schedule")
    start = datetime.fromisoformat(trigger["start_at"]) if trigger.get("start_at") else after
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    local_after = after.replace(tzinfo=timezone.utc) if after.tzinfo is None else after
    parsed = rrulestr(rule, dtstart=start.astimezone(zone))
    candidate = parsed.after(local_after.astimezone(zone), inc=False)
    # dateutil emits imaginary ZoneInfo wall times. RFC 5545 requires ignoring them.
    for _ in range(370):
        if candidate is None:
            return None
        utc = candidate.astimezone(timezone.utc)
        if utc > local_after.astimezone(timezone.utc) and utc.astimezone(zone).replace(tzinfo=None) == candidate.replace(tzinfo=None):
            return utc.replace(tzinfo=None)
        candidate = parsed.after(candidate, inc=False)
    raise ValueError("Schedule has no valid upcoming time")


def resolve_relative(text: str, tz: str, now: datetime) -> datetime:
    """Bounded founder date grammar; unknown phrases ask for an absolute time."""
    zone = ZoneInfo(timezone_name(tz))
    local = (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).astimezone(zone)
    phrase = text.strip().lower()
    match = re.fullmatch(r"(tomorrow|today|next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))(?: (\d{1,2})(?::(\d{2}))?\s*(am|pm)?)?", phrase)
    if not match:
        raise ValueError("Please choose an absolute date and time")
    day, hour, minute, meridian = match.groups()
    offset = 1 if day == "tomorrow" else 0
    if day.startswith("next "):
        weekday = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday").index(day[5:])
        offset = (weekday - local.weekday()) % 7 or 7
    h, m = int(hour or 9), int(minute or 0)
    if meridian:
        if not 1 <= h <= 12:
            raise ValueError("Choose a valid hour")
        h = h % 12 + (12 if meridian == "pm" else 0)
    if h > 23 or m > 59:
        raise ValueError("Choose a valid time")
    resolved = (local + timedelta(days=offset)).replace(hour=h, minute=m, second=0, microsecond=0)
    utc = resolved.astimezone(timezone.utc)
    if utc.astimezone(zone).replace(tzinfo=None) != resolved.replace(tzinfo=None):
        raise ValueError("That local time does not exist; choose another time")
    return utc.replace(tzinfo=None)


def trigger_from_words(goal: str, tz: str, now: datetime) -> tuple[str, dict]:
    """Frequency/event is founder-owned, never invented by the model."""
    phrase = goal.lower()
    zone = timezone_name(tz)
    weekdays = {day: code for day, code in zip(("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"), ("MO", "TU", "WE", "TH", "FR", "SA", "SU"))}
    frequency = re.search(r"\b(?:every|each)\s+(morning|day|week|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b|\b(daily|weekly|everyday)\b", phrase)
    if frequency:
        period = frequency.group(1) or frequency.group(2)
        at = re.search(r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", phrase)
        hour, minute = (8, 0) if period == "morning" else (9, 0)
        if at:
            hour, minute = int(at[1]), int(at[2] or 0)
            if at[3]:
                if not 1 <= hour <= 12:
                    raise ValueError("Choose a valid hour")
                hour = hour % 12 + (12 if at[3] == "pm" else 0)
            if hour > 23 or minute > 59:
                raise ValueError("Choose a valid time")
        rule = "FREQ=WEEKLY" if period in weekdays or period in {"week", "weekly"} else "FREQ=DAILY"
        if period in weekdays:
            rule += ";BYDAY=" + weekdays[period]
        rule += f";BYHOUR={hour};BYMINUTE={minute};BYSECOND=0"
        trigger = {"rrule": rule, "tz": zone, "start_at": (now.replace(tzinfo=timezone.utc) if now.tzinfo is None else now).isoformat()}
        next_fire(trigger, now)  # reject invalid rules before persisting
        return "scheduled", trigger
    if re.search(r"\bwhen\s+(?:someone|a customer|a person)\s+replies\b", phrase):
        return "event", {"event_type": "outreach.reply", "filter": {}}
    if re.search(r"\bwhen\s+(?:a |someone )?(?:sign[ -]?up|signup)\s+(?:arrives|signs up)\b", phrase):
        return "event", {"event_type": "waitlist.signup", "filter": {}}
    if re.search(r"\b(every|each|when|whenever|monthly|hourly|nightly|quarterly|yearly|weekdays|weekends)\b", phrase):
        raise ValueError("What exact frequency or event should start this task?")
    return "one_time", {}


def consequential_blocker(skill: str | None = None) -> dict:
    from app.core.local_mode import is_local

    if is_local():
        if skill == "pagebuilder.publish":
            from app.core.local_capabilities import blocked_message, enabled

            next_step = (blocked_message("publish_pages") if not enabled("publish_pages") else
                "Open your venture's Website screen and choose Publish for your checked page.")
        elif skill == "device.work_order":
            next_step = "Open Settings > Keys and connections > Coding tools, choose your tool, then ask me to use it for this request."
        else:
            next_step = "Open your venture's private chat and ask me to perform this action directly, without creating a saved task."
        return {"class": "needs_input", "reason": "No work has started. Use the saved action for this request.",
                "unblock_action": next_step}
    return {"class": "needs_approval", "reason": "This step needs an exact action preview and your approval.",
            "unblock_action": "Open the existing review screen to approve the action."}


def blocker(error: str) -> dict:
    """Never put provider bodies, exception names or identifiers into task copy."""
    code = error.lower().partition(":")[0]
    page_code = bool(re.fullmatch(r"(?:pagebuilder|qa|subscription_static)_[a-z_]{1,80}", code))
    if page_code and any(x in code for x in ("scope", "authority", "forbidden", "integrity", "binding", "admission_revoked", "approved_inputs_changed")):
        kind, reason, action = "policy", "The saved page or checks no longer match this approved request.", "Refresh the saved page and review its current approval."
    elif code == "qa_checks_failed":
        kind, reason, action = "needs_input", "The page did not pass its checks and stays private.", "Review the recorded failed checks, then build a new version."
    elif code in {"pagebuilder_qa_failed", "pagebuilder_social_metadata_qa_failed"}:
        from app.pagebuilder.local import page_check_message
        kind, reason, action = "needs_input", page_check_message([error]), "Open Website and choose Retry after requesting the fix."
    elif page_code and any(x in code for x in ("uncertain", "reconciliation", "operation_in_progress")):
        kind, reason, action = "needs_input", "A page-building call stopped without a confirmed result.", "Your saved work is kept. Wait for its result to be reconciled before building again."
    elif code == "pagebuilder_provider_rejected":
        kind, reason, action = "needs_auth", "The page builder refused the request. Your saved work is kept.", "Review the saved page, then retry."
    elif code in {"pagebuilder_provider_offline", "pagebuilder_byok_unavailable",
                  "pagebuilder_provider_or_credits_unavailable", "pagebuilder_pricing_unavailable", "pagebuilder_credit_reservation_unavailable"}:
        kind, reason, action = "needs_auth", "The page build could not start. Your saved work is kept.", "Review the saved page, then retry."
    elif code == "pagebuilder_provider_rate_limited":
        kind, reason, action = "needs_input", "The page builder's model is temporarily busy.", "Your saved work is kept. Try building again later."
    elif code in {"pagebuilder_renderer_unavailable", "pagebuilder_firefox_sandbox_unverified"}:
        kind, reason, action = "needs_input", "The page builder could not start its private browser checks.", "Your saved work is kept. Try building again later."
    elif code.startswith("pagebuilder_waitlist"):
        kind, reason, action = "needs_input", "The page's consent-enabled waitlist is not ready.", "Review your waitlist settings, then build again."
    elif code in {"pagebuilder_output_truncated", "pagebuilder_invalid_output", "pagebuilder_output_invalid",
                  "pagebuilder_plan_json_invalid", "pagebuilder_plan_rejected"}:
        kind, reason, action = "needs_input", "Your coding tool could not produce a complete, usable page plan.", "Open Build activity to review the checks, then request a fix on Website and choose Retry."
    elif code == "pagebuilder_capability_grounding_rejected":
        kind, reason, action = "needs_input", "The draft made claims that could not be supported by your confirmed idea.", "Review your idea's capabilities, then build again."
    elif code in {"pagebuilder_candidate_policy_obsolete", "pagebuilder_candidate_invalid", "pagebuilder_candidate_changed",
                  "pagebuilder_reference_readback_mismatch", "pagebuilder_artifact_invalid"}:
        kind, reason, action = "needs_input", "The saved page source could not be verified against the current build requirements.", "Refresh the saved page; build a new version if it is no longer usable."
    elif code == "pagebuilder_new_version_unavailable":
        kind, reason, action = "needs_input", "A new page build could not start from this saved request.", "Refresh the saved page status and wait for any running request to finish before building again."
    elif code in {"pagebuilder_reference_unavailable", "pagebuilder_admission_storage_failed", "pagebuilder_admission_source_unavailable"}:
        kind, reason, action = "needs_input", "The page source could not be safely saved or read back.", "Retry saving the existing source; no new page generation is needed."
    elif code in {"pagebuilder_resources_changed", "pagebuilder_inputs_changed", "pagebuilder_admission_inputs_changed"}:
        kind, reason, action = "needs_input", "Your idea or the approved build resources changed while the page was being built.", "Review the current idea and Settings, then build again."
    elif code == "pagebuilder_attempt_limit":
        kind, reason, action = "budget_exhausted", "Page building reached the eight-attempt safety limit for this idea version.", "Your idea, research and saved work are kept. Continue to Find customers; no further build is started or charged."
    elif code == "pagebuilder_build_limit":
        kind, reason, action = "budget_exhausted", "Three ready page versions have been delivered for this idea; its version limit is reached.", "Your saved pages are safe. Review them or continue to Find customers."
    elif code == "pagebuilder_provider_usage_above_ceiling":
        kind, reason, action = "budget_exhausted", "The page build reached its approved version or usage limit.", "Your saved page is safe. Review the idea and build allowance before continuing."
    elif code in {"qa_runtime_deadline", "subscription_static_qa_deadline", "subscription_static_import_deadline", "build_runtime_deadline", "build_budget_expired"}:
        kind, reason, action = "needs_input", "Our final checks ran out of time before they completed; no page-content failure was confirmed.", "The page stays private. Try Publish again when the saved attempt has finished cleanup."
    elif code in {"qa_task_stopped_before_report", "qa_command_failed_or_interrupted", "qa_runtime_failed"}:
        kind, reason, action = "needs_input", "Our final-check runner stopped before it finished; no page-content failure was confirmed.", "The page stays private. Try Publish again when the saved attempt has finished cleanup."
    elif code.startswith(("qa_", "subscription_static_")) or code in {
            "build_budget_expired", "build_runtime_deadline", "build_task_control_unavailable",
            "build_compute_usage_unverified", "build_infrastructure_usage_unverified"}:
        kind, reason, action = "needs_input", "Our final checks could not complete; this is not a failed page-content check.", "The page stays private. Try Publish again when the saved attempt has finished cleanup."
    elif code == "pagebuilder_publication_refused":
        kind, reason, action = "needs_input", "Publication could not be confirmed. Your preview stays private.", "Refresh the saved publication status before trying again."
    elif re.fullmatch(r"(?:hosting|waitlist)_[a-z_]{1,80}", code):
        kind, reason, action = "needs_input", "Publishing or the page's public sign-up check could not be confirmed. Your preview stays private.", "Review the saved publication status and waitlist settings before trying again."
    elif code in {"research_outcome_uncertain", "research_interrupted"}:
        kind, reason, action = "needs_input", "Your research was interrupted.", "Open Research and choose Run research again, or ask to skip for now."
    elif code == "discovery_boundary_blocked":
        kind, reason, action = "needs_input", "Your search could not finish because some public pages could not be checked.", "Choose Search again to try a new search. Your saved work is kept."
    elif any(x in code for x in ("uncertain", "reconciliation")):
        kind, reason, action = "needs_input", "An earlier action needs its result checked before we can continue.", "Review the earlier result; it will not be repeated automatically."
    elif any(x in code for x in ("policy", "refused", "refusal", "content_filter", "captcha", "boundary_blocked")):
        kind, reason, action = "policy", "This action is not permitted by the provider's rules.", "Choose a permitted source or action."
    elif any(x in code for x in ("auth", "api_key", "login", "oauth", "account_suspended", "credits_unavailable", "insufficient_credits")):
        kind, reason, action = "needs_auth", "A connection or account needs your attention.", "Review the required account connection."
    elif any(x in code for x in ("budget", "ceiling", "call_budget")):
        kind, reason, action = "budget_exhausted", "The task reached its agreed limit.", "Review the partial result and choose whether to continue."
    elif any(x in code for x in ("approval", "confirmation", "review_required")):
        kind, reason, action = "needs_approval", "This action needs your approval.", "Review the exact action before continuing."
    elif any(x in code for x in ("input", "idea_missing", "revision", "research_required", "not_ready")):
        kind, reason, action = "needs_input", "The task needs one detail before it can continue.", "Review the task and supply the missing detail."
    elif any(x in code for x in ("timeout", "retryable", "network", "connectionerror", "429", "503", "502")):
        kind, reason, action = "needs_input", "A temporary service problem remained after the safe attempts.", "Review the saved progress before trying another approach."
    elif page_code:
        kind, reason, action = "needs_input", "Your page build stopped before finishing.", "Open Build activity to review the checks, then request a fix on Website and choose Retry."
    else:
        kind, reason, action = "impossible", "The task could not verify the requested result.", "Review the saved progress and choose another approach."
    return {"class": kind, "reason": reason, "unblock_action": action}


def stuck(history: list[dict], *, waiting: bool = False) -> bool:
    if waiting:
        return False
    pairs = [(x.get("action"), x.get("result")) for x in history]
    return (len(pairs) >= 4 and len(set(pairs[-4:])) == 1
            or len(history) >= 3 and all(x.get("error") for x in history[-3:]) and len({x.get("error") for x in history[-3:]}) == 1
            or len(pairs) >= 12 and pairs[-12::2] == [pairs[-2]] * 6 and pairs[-11::2] == [pairs[-1]] * 6 and pairs[-2] != pairs[-1]
            or len(history) >= 2 and history[-1].get("metric") is not None and history[-2].get("metric") is not None and history[-1]["metric"] <= history[-2]["metric"])


def requires_reviewed_action(title: str, detail: str = "") -> bool:
    """A draft clause cannot authorize delivery, buying or deletion."""
    from app.chat.search_recovery import without_negated_effect_lists

    values = [re.sub(r"\bI can review and send\b", "I can review", without_negated_effect_lists(value or ""),
                     flags=re.IGNORECASE) for value in (title, detail)]
    verbs = r"(?:send|post|publish|buy|purchase|delete|remove|pay|book|order|subscribe|invite)"
    polite = r"(?:(?:can|could|would|will)\s+you\s+|(?:i|we)\s+(?:want|need)\s+(?:you\s+)?to\s+)?"
    dependency = r"\bafter\s+(?:you\s+)?(?:sending|posting|publishing|buying|purchasing|deleting|removing|paying|booking|ordering|subscribing|inviting|" + verbs + r")\b"
    return any(re.search(r"^(?:please\s+)?" + polite + r"(?:please\s+|automatically\s+)?(?:do\s+|help\s+(?:me|us)\s+(?:to\s+)?)?" + verbs + r"\b|(?:\band\b|\bthen\b|\balso\b|[;&])\s+(?:please\s+|automatically\s+)?" + verbs + r"\b|" + dependency,
                         value.strip(), re.IGNORECASE) for value in values)


def estimated_deadline(goal: str, tz: str, now: datetime, estimate_seconds: int) -> datetime:
    phrase = re.search(r"\b(?:by|before|due(?: at)?)\s+((?:today|tomorrow|next (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday))(?:\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?)\b", goal, re.IGNORECASE)
    if phrase:
        value = resolve_relative(re.sub(r"\s+at\s+", " ", phrase[1], flags=re.IGNORECASE), tz, now)
        if value <= (now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now):
            raise ValueError("That time has passed. What future date and time should we use?")
        return value
    return (now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now) + timedelta(seconds=estimate_seconds)
