"""Choose existing, decisive runners. Missing setup never becomes a new runner."""
from __future__ import annotations

from datetime import timedelta
from copy import deepcopy

from app.credits.policy import get_policy
from app.experiments import autonomous, beliefs, engine
from core.clock import as_utc, utcnow
from core.ids import new_id

QUESTIONS = {"who": "Q2", "pain": "Q1", "price": "Q3", "channel": "Q5",
             "message": "Q4", "competitor_gap": "Q7", "switching": "Q6"}
RUNNERS = {
    "who": ("demand", "community", "postings", "outreach_split"),
    "pain": ("demand", "reviews", "postings", "workarounds", "booking", "wizard_tests"),
    "price": ("pricing", "page_pricing", "price_band"),
    "channel": ("channel_test", "community"),
    "message": ("outreach_split", "page_headlines"),
    "competitor_gap": ("competitors", "reviews", "browser_read_only"),
    "switching": ("workarounds", "reviews", "booking", "wizard_tests"),
}


def enabled():
    from app.core.local_mode import is_local

    if is_local():
        return autonomous.autopilot_enabled()
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True


def runner(spec):
    from app.experiments import tools
    if tools.enabled() and (spec.get("scan") == "page_request" or
            spec.get("scan") == "page_test" and spec.get("page_kind") == "request"):
        return "wizard_tests"
    if spec.get("catalog_key") == "E26":
        return "booking"
    if spec.get("runner") == "outreach_split":
        return "outreach_split"
    if spec.get("scan") == "page_test":
        return "page_" + str(spec.get("page_kind"))
    return spec.get("scan") or spec.get("runner")


def observing(spec):
    return spec.get("state") == "running" and spec.get("scan") in {"page_test", "channel_test"}


def available(venture_id, items):
    snapshot = autonomous._snapshot(venture_id)
    if not snapshot:
        return []
    specs = autonomous.available(venture_id)
    from app.experiments import tools
    if tools.enabled():
        for item in items:
            if item.get("required_tool"):
                for spec in specs:
                    if spec.get("tool") == item["required_tool"]:
                        spec.setdefault("belief_ids", []).append(item["id"])
    provider = autonomous._provider()
    autopilot = enabled()
    for item in items:
        if item["type"] not in {"who", "pain"} or not (item.get("audience") or item.get("problem")):
            continue
        scoped = deepcopy(snapshot)
        icp = dict(scoped["journey"].get("icp") or {})
        icp.update(who=item.get("audience") or icp.get("who", ""), problem=item.get("problem") or icp.get("problem", ""))
        scoped["journey"]["icp"] = icp
        matches = [spec for spec in autonomous.select_runnable(scoped, provider)
                   if spec.get("runner") == "scan" and (runner(spec) in RUNNERS[item["type"]]
                       or autopilot and item["type"] == "pain" and runner(spec) == "redteam")]
        for spec in matches:
            spec["belief_ids"] = [item["id"]]
        for spec in specs:
            if spec.get("belief_ids") is None:
                spec["belief_ids"] = [belief["id"] for belief in items if not
                    (belief["type"] in {"who", "pain"} and (belief.get("audience") or belief.get("problem")))]
        specs.extend(matches)
    merged = {}
    for spec in specs:
        if spec["id"] in merged:
            merged[spec["id"]]["belief_ids"] = list(set(merged[spec["id"]].get("belief_ids", []) + spec.get("belief_ids", [])))
        else:
            merged[spec["id"]] = spec
    return list(merged.values())


def _portfolio_lane(spec):
    if runner(spec) == "redteam" or spec.get("catalog_key") == "E39":
        return "challenge"
    if spec.get("runner") == "outreach_split" or observing(spec) or runner(spec) == "wizard_tests":
        return "response"
    return "research"


def build(items, experiments, inputs, *, ceiling_micro=None, max_emails_per_day=15, now=None):
    if type(max_emails_per_day) is not int or not 0 <= max_emails_per_day <= 15:
        raise ValueError("Your email allowance must be between 0 and 15 a day.")
    if ceiling_micro is not None and (type(ceiling_micro) is not int or ceiling_micro < 0):
        raise ValueError("Your credit ceiling must be a nonnegative integer.")
    now = as_utc(now or utcnow())
    ranking = engine.rank_assumptions(inputs)
    risks = {item["question_code"]: item["risk"] for item in ranking}
    used, tests, waiting = set(), [], []
    autopilot = enabled()
    outreach_catalogs = {"who": {"E08", "E09", "E39"} if autopilot else {"E20"},
                         "message": {"E19"}, "pain": {"E26"}, "switching": {"E26"}}
    from app.autopilot.outreach import FOLLOWUP_DAYS
    cadence = inputs.get("journey", {}).get("followup_days", FOLLOWUP_DAYS)
    emails_left = max_emails_per_day * 7
    pending = sorted((item for item in items if item["status"] == "to_test"),
            key=lambda item: (-risks.get(QUESTIONS[item["type"]], engine.IMPACT[QUESTIONS[item["type"]]]) *
                (1 - item["confidence"]), item["id"]))
    from app.experiments import tools
    if tools.enabled():
        for belief in list(pending):
            if not belief.get("required_tool"):
                continue
            choice = next((spec for spec in experiments if spec.get("tool") == belief["required_tool"]), None)
            if choice:
                waiting.append({"belief_id": belief["id"], "experiment_id": choice["id"],
                    "tool": choice["tool"], "reason": belief["tool_reason"], "runners": ["tool_evidence"]})
                pending.remove(belief)
    while pending:
        options = []
        for belief in pending if autopilot else pending[:1]:
            choices = [spec for spec in experiments if spec["id"] not in used and (spec.get("state") == "proposed" or observing(spec))
                and ("belief_ids" not in spec or belief["id"] in spec["belief_ids"])
                and (runner(spec) in RUNNERS[belief["type"]]
                     or autopilot and belief["type"] == "pain" and runner(spec) == "redteam")
                and spec.get("success_threshold") and spec.get("failure_threshold")
                and (spec.get("approval") not in {"each_send", "each_publish"} or observing(spec)
                     or autopilot and spec.get("approval") == "each_send" and spec.get("runner") == "outreach_split")
                and (max_emails_per_day > 0 or spec.get("runner") != "outreach_split")
                and (spec.get("runner") != "outreach_split" or
                     (inputs.get("journey", {}).get("allow_sending") and inputs.get("journey", {}).get("auto_approve_first_emails")))
                and (spec.get("runner") != "outreach_split" or
                     spec.get("catalog_key") in outreach_catalogs.get(belief["type"], set()))
                and (spec.get("runner") != "outreach_split" or spec.get("min_sample", 0) *
                     (1 + sum(0 < day <= 7 for day in cadence)) <= emails_left)
                and (observing(spec) or not engine.blocked_reason(spec, inputs.get("experiments", []), now))]
            choices.sort(key=lambda spec: (spec["ceiling_micro"], -engine.score(spec, ranking, inputs.get("volume", {}))))
            options.extend((belief, spec) for spec in choices if ceiling_micro is None or
                           sum(test["ceiling_micro"] for test in tests) + spec["ceiling_micro"] <= ceiling_micro)
        selected = next((option for option in options if option[0]["id"] == pending[0]["id"]), (pending[0], None))
        if autopilot:
            lanes = ("research", "response", "challenge")
            position = len(tests) % len(lanes)
            lane = lanes[position]
            later_lanes = set(lanes[position + 1:])
            belief_lanes = {}
            for candidate, spec in options:
                belief_lanes.setdefault(candidate["id"], set()).add(_portfolio_lane(spec))
            sensible = next((option for option in options if _portfolio_lane(option[1]) != "challenge"), selected)
            selected = min((option for option in options if _portfolio_lane(option[1]) == lane),
                key=lambda option: len(belief_lanes[option[0]["id"]] & later_lanes), default=sensible)
        belief, choice = selected
        pending.remove(belief)
        if not choice:
            waiting.append({"belief_id": belief["id"], "runners": list(RUNNERS[belief["type"]]),
                "reason": "Your next test needs setup, a fresh hypothesis, or more of your approved budget."})
            continue
        used.add(choice["id"])
        if choice.get("runner") == "outreach_split":
            emails_left -= choice.get("min_sample", 0) * (1 + sum(0 < day <= 7 for day in cadence))
        tests.append({"belief_id": belief["id"], "experiment_id": choice["id"], "runner": runner(choice),
            "hypothesis": choice["hypothesis"], "method": choice["method"],
            "fingerprint": choice["fingerprint"], "ceiling_micro": choice["ceiling_micro"],
            "metric": choice["metric"], "sample_size": choice.get("min_sample", 1),
            "success_threshold": choice["success_threshold"], "failure_threshold": choice["failure_threshold"],
            "deadline": (now + timedelta(days=7)).isoformat(), "state": "running" if observing(choice) else "queued"})
    total = sum(test["ceiling_micro"] for test in tests)
    plan = {"id": new_id("belplan"), "state": "draft", "days": 7, "tests": tests, "waiting": waiting,
        "ceiling_micro": total if ceiling_micro is None else ceiling_micro, "reserved_micro": 0,
        "max_emails_per_day": max_emails_per_day, "created_at": now.isoformat(), "daily_notes": []}
    plan["fingerprint"] = autonomous._fingerprint(plan)
    return plan
