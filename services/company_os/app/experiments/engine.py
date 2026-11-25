"""Deterministic experiment priorities from saved inputs, without I/O."""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime, timedelta

from app.credits.units import micro_to_credits
from app.experiments.ledger import _hypothesis_key, quality_enabled
from core.clock import as_utc
from core.copy import founder_copy

QUESTION_ORDER = ("Q1", "Q2", "Q3", "Q5", "Q4", "Q6", "Q7", "Q8", "Q9")
IMPACT = dict(zip(QUESTION_ORDER, (5, 4, 5, 4, 3, 4, 3, 2, 2)))
OPEN_RISKS = {
    "Q1": "the problem may not matter enough", "Q2": "your customer audience may be wrong",
    "Q3": "customers may not pay for your offer", "Q4": "your message may not connect",
    "Q5": "you may not reach customers affordably", "Q6": "customers may not use your offer",
    "Q7": "existing alternatives may already solve the problem", "Q8": "the timing may be wrong",
    "Q9": "customers may not return",
}
SCAN_QUESTIONS = {"demand": "Q1", "competitors": "Q7", "outreach": "Q4", "landing": "Q4"}
QUESTION_WORDS = {
    "Q3": r"\b(pay|paid|price|pricing|deposit|revenue|purchase)\b",
    "Q9": r"\b(retain|retention|return|referral|churn)\b",
    "Q8": r"\b(timing|season|trend|now)\b",
    "Q7": r"\b(competitor|competitors|alternative|alternatives|compete)\b",
    "Q5": r"\b(reach|channel|channels|booking|bookings)\b",
    "Q4": r"\b(message|angle|headline|subject|reply|replies|signup|signups)\b",
    "Q6": r"\b(use|usage|habit|concierge)\b", "Q2": r"\b(segment|audience|persona|who)\b",
}


def spec(experiment: dict) -> dict:
    return {**experiment, **(experiment.get("spec") or {})}


def response_maturity(experiment: dict, sent_at: list[datetime], now: datetime) -> dict:
    now = as_utc(now)
    window = float(experiment.get("response_window_days", experiment["max_duration_days"]))
    if not math.isfinite(window) or window <= 0:
        raise ValueError("Invalid response window")
    deadlines = [as_utc(sent) + timedelta(days=window) for sent in sent_at]
    return {"mature_contacts": sum(deadline <= now for deadline in deadlines),
        "response_window_open": not deadlines or any(deadline > now for deadline in deadlines),
        "readable_at": max(deadlines).isoformat() if deadlines else None}


def mature_result(result: dict, maturity: dict) -> dict:
    if not maturity["response_window_open"] or result["outcome"] == "supported":
        return {**result, **maturity}
    date = maturity["readable_at"]
    return {**result, **maturity, "outcome": "inconclusive",
        "insight": "Your test is too early to tell. " + (f"It will be readable on {date}." if date else
            "Your response clock starts after your first real send.")}


def question(experiment: dict) -> str:
    item = spec(experiment)
    code = item.get("question") or item.get("question_code")
    if code in IMPACT:
        return code
    if item.get("scan") in SCAN_QUESTIONS:
        return SCAN_QUESTIONS[item["scan"]]
    text = str(item.get("assumption") or item.get("hypothesis") or "")
    code = re.search(r"\bQ[1-9]\b", text)
    if code:
        return code.group()
    # ponytail: keyword classification for legacy free text, use explicit question codes as specs migrate.
    return next((code for code, pattern in QUESTION_WORDS.items() if re.search(pattern, text, re.I)), "Q1")


def rank_assumptions(inputs: dict) -> list[dict]:
    understanding = inputs.get("understanding") or {}
    assumptions = [*understanding.get("assumptions", []), *understanding.get("risks", [])]
    journey = inputs.get("journey") or {}
    volume = inputs.get("volume") or {}
    weak = {"Q1": bool(journey.get("need_signals")), "Q5": bool(journey.get("reachable_leads")),
            "Q4": bool(volume.get("replies") or volume.get("signups")),
            "Q6": bool(volume.get("activation")), "Q9": bool(volume.get("retention"))}
    evidence = [row for row in inputs.get("experiments", []) if row.get("state") == "concluded"
                and spec(row).get("direction", "") == inputs.get("direction", "")]
    ranked = []
    quality = quality_enabled()
    for code in QUESTION_ORDER:
        matched = [item if isinstance(item, dict) else {"assumption": item} for item in assumptions
                   if question(item if isinstance(item, dict) else {"assumption": item}) == code]
        rows = [row for row in evidence if question(row) == code]
        supported = [row for row in rows if row.get("outcome") == "supported" and (not quality or
            row.get("evidence_binding", {}).get("evidence_type") != "desk_research")]
        methods = {str(spec(row).get("method") or spec(row).get("scan") or
                       spec(row).get("catalog_key") or "manual") for row in supported}
        uncertainty = .1 if len(methods) >= 2 else .3 if supported else .6 if weak.get(code) else 1.0
        impact = 5 if code == "Q9" and inputs.get("stage") in {"live", "operating", "iterating", "scaling"} else IMPACT[code]
        for assumption in matched or [{"assumption": OPEN_RISKS[code]}]:
            ranked.append({"assumption": founder_copy(str(assumption.get("assumption") or assumption.get("hypothesis") or OPEN_RISKS[code])),
                           "question_code": code, "evidence_state": "supported" if supported else "weak_signal" if weak.get(code) else "belief",
                           "impact": impact, "uncertainty": uncertainty, "risk": impact * uncertainty})
    return sorted(ranked, key=lambda row: (-row["risk"], QUESTION_ORDER.index(row["question_code"])))


def days(experiment: dict) -> float:
    item = spec(experiment)
    scanning = item.get("runner") != "outreach_split" and (item.get("runner") == "scan" or bool(item.get("scan") and item["scan"] not in {"outreach", "landing"}))
    default = 1 if scanning else 14
    duration = float(item.get("days_to_result", item.get("days", item.get("max_duration_days", default))))
    if not math.isfinite(duration) or duration < 0:
        raise ValueError("Invalid experiment duration")
    return duration


def score(experiment: dict, ranking: list[dict], volume: dict) -> float:
    item = spec(experiment)
    risk = next((row["risk"] for row in ranking if row["question_code"] == question(item)), 0)
    scan = item.get("scan")
    scanning = item.get("runner") != "outreach_split" and (item.get("runner") == "scan" or bool(scan and scan not in {"outreach", "landing"}))
    info = float(item.get("info", 1 if item.get("metric") == "deposits" else .5 if scanning else .7))
    unit = item.get("sample_unit") or ("views" if scan == "landing" or item.get("runner") == "page_variant" else "emails" if scan == "outreach" or item.get("runner") == "outreach_split" else None)
    minimum = item.get("min_sample", 50 if unit == "views" else 20)
    conf = .2 if unit and not scanning and volume.get(unit, 0) < minimum else 1.0
    ceiling = int(item.get("ceiling_micro") or 0)
    minutes = float(item.get("founder_minutes") or 0)
    cost = 1 + float(micro_to_credits(ceiling)) / 50 + minutes / 30
    if ceiling < 0 or minutes < 0 or not math.isfinite(info) or not 0 <= info <= 1 or not math.isfinite(cost):
        raise ValueError("Invalid experiment score inputs")
    return risk * info * conf / (cost * (1 + days(item) / 7))


def reason(experiment: dict, ranking: list[dict]) -> str:
    code = question(experiment)
    biggest = bool(ranking and ranking[0]["question_code"] == code)
    duration = days(experiment)
    time = f"about {math.ceil(duration / 7)} weeks" if duration >= 14 else "today" if duration == 0 else f"about {math.ceil(duration)} {'day' if duration <= 1 else 'days'}"
    return f"{'Biggest open risk' if biggest else 'Open risk'}: {OPEN_RISKS[code]}. This test can reduce that uncertainty, {time}."


def input_fingerprint(experiment: dict) -> str:
    item = spec(experiment)
    if item.get("input_fingerprint"):
        return str(item["input_fingerprint"])
    # ponytail: legacy specs expose only saved inputs, use input_fingerprint when the catalog stores a fuller evidence snapshot.
    inputs = {key: item.get(key) for key in ("direction", "icp", "fields", "angles", "app_id", "site_url", "inputs", "volume")}
    if item.get("runner") == "outreach_split" or item.get("scan") == "adjacent_swap":
        inputs.update({key: item.get(key) for key in ("variants", "runner_params")})
    return hashlib.sha256(json.dumps(inputs, sort_keys=True, default=str).encode()).hexdigest()


def blocked_reason(experiment: dict, history: list[dict], now: datetime) -> str:
    item = spec(experiment)
    history = [row for row in history if row.get("venture_id") == item.get("venture_id")]
    running = [row for row in history if row.get("state") in {"running", "waiting_data"}]
    if len(running) >= 3:
        return "You already have 3 experiments running. Finish one before starting another."
    audience = item.get("audience") or (item.get("icp") or {}).get("who")
    variable = item.get("variable")
    if variable and audience and any(spec(row).get("variable") == variable and
            (spec(row).get("audience") or (spec(row).get("icp") or {}).get("who")) == audience for row in running):
        return "You already have an experiment running on this variable and audience."
    key = _hypothesis_key(item.get("direction") or "", item.get("hypothesis") or "")
    for row in history:
        previous = spec(row)
        ended = row.get("ended_at")
        if row.get("state") != "concluded" or not ended:
            continue
        ended = datetime.fromisoformat(ended) if isinstance(ended, str) else ended
        if (_hypothesis_key(previous.get("direction") or "", previous.get("hypothesis") or "") == key
                and as_utc(ended) > as_utc(now) - timedelta(days=14)
                and input_fingerprint(item) == input_fingerprint(previous)):
            return "You tested this hypothesis in the last 14 days. Wait or change the inputs before repeating it."
    return ""
