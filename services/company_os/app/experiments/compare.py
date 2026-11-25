"""Compare recorded option receipts without generating findings or contacts."""
from __future__ import annotations

import math

from app.experiments.activity import EvidenceLevel
from app.experiments import methods


COUNT_ACTIONS = {"search": "searches", "fetch": "sources", "discover": "prospects",
    "draft": "drafted", "drafted": "drafted", "approved": "approved", "queued": "queued",
    "email": "sent", "sent": "sent", "reply": "replies", "reply_received": "replies",
    "positive_reply": "positive_replies", "meeting_booked": "meetings"}


def _number(value):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def _interval(positive, sample):
    rate = positive / sample
    denominator = 1 + 1.96 ** 2 / sample
    center = (rate + 1.96 ** 2 / (2 * sample)) / denominator
    margin = 1.96 * math.sqrt(rate * (1 - rate) / sample + 1.96 ** 2 / (4 * sample ** 2)) / denominator
    return center - margin, center + margin


def option_result(option, items):
    from app.experiments.autonomous import outcome as classify_outcome

    counts = dict.fromkeys(COUNT_ACTIONS.values(), 0)
    contributions = {key: {} for key in counts}
    observed_methods, metrics, citations = set(), {}, []
    level = EvidenceLevel.HYPOTHESIS.value
    sample = 0
    positive_known = False
    for item in items:
        outcome = item["outcome"]
        values = outcome if isinstance(outcome, dict) else {}
        status = values.get("status", "succeeded") if values else outcome
        if isinstance(outcome, dict) and not outcome:
            status = "succeeded"
        if status not in {"succeeded", "supported", "refuted", "inconclusive"}:
            continue
        action = item["action"]
        count_key = COUNT_ACTIONS.get(action)
        count = values.get("count", values.get("events", 1)) if isinstance(outcome, dict) else (
            int(item["evidence_summary"]) if action in {"draft", "email", "reply", "discover"} else 1)
        if count_key in {"searches", "sources"}:
            count = 1
        identity = values.get("message_id") or values.get("event_id") or item["id"]
        if count_key:
            contributions[count_key][identity] = int(_number(count) or 0)
            if action == "reply_received" and "positive_replies" in values:
                contributions["positive_replies"][identity] = int(values["positive_replies"])
                positive_known = True
            if action == "positive_reply":
                contributions["replies"].setdefault(identity, int(_number(count) or 0))
                positive_known = True
        for key in ("pain", "need", "buying_intent"):
            if _number(values.get(key)) is not None:
                metrics[key] = values[key]
        if item["evidence_level"] in {EvidenceLevel.MARKET_RESPONSE, EvidenceLevel.CONCLUSION} and _number(values.get("sample_size")) is not None:
            sample = max(sample, int(values["sample_size"]))
        recorded_level = item["evidence_level"]
        if list(EvidenceLevel).index(EvidenceLevel(recorded_level)) >= list(EvidenceLevel).index(EvidenceLevel(level)):
            level = recorded_level
        if action in {"search", "fetch"}:
            observed_methods.add("Web research")
        elif count_key in {"prospects", "drafted", "approved", "queued", "sent", "replies", "positive_replies", "meetings"}:
            observed_methods.add("Email")
        elif action != "compare":
            observed_methods.add(item["tool"].replace("_", " "))
        citations.append({"id": item["id"], "option_id": option["id"], "text": item["text"]})
    counts = {key: sum(values.values()) for key, values in contributions.items()}
    arms = {item["outcome"]["experiment_id"]: item["outcome"]["arm_result"] for item in items
        if isinstance(item["outcome"], dict) and item["outcome"].get("arm_result") is not None
        and item["outcome"].get("status", "succeeded") == "succeeded"}
    for key, field in (("sent", "sent"), ("replies", "replies"), ("positive_replies", "positive_replies"), ("meetings", "booked")):
        if arms:
            counts[key] = max(counts[key], sum(int(_number(arm.get(field)) or 0) for arm in arms.values()))
    positive_known = positive_known or any("positive_replies" in arm for arm in arms.values())
    booking_known = bool(contributions["meetings"]) or any("booked" in arm for arm in arms.values())
    spec = option.get("spec") or {}
    minimum = max(2, math.ceil(_number(spec.get("min_sample")) or 20))
    sample = max(sample, counts["sent"])
    positive = counts["positive_replies"]
    rate = positive / sample if positive_known and sample and positive <= sample else None
    booking_rate = counts["meetings"] / sample if booking_known and sample and counts["meetings"] <= sample else None
    rates = {"positive_reply_rate": rate, "booking_rate": booking_rate}
    metric = spec.get("metric", "positive_reply_rate")
    response_rate = rates.get(metric)
    verdict = classify_outcome(spec, {**rates, "events": sample})
    if not spec.get("success_threshold") and response_rate is not None and response_rate > 0:
        verdict = "supported"
    buying_intent = metrics.get("buying_intent")
    if buying_intent is None and (positive_known or booking_known):
        buying_intent = "; ".join([f"{positive} positive replies"] if positive_known else [])
        if booking_known:
            buying_intent = "; ".join(filter(None, (buying_intent, f"{counts['meetings']} meetings booked")))
    last_outcome = items[-1]["outcome"] if items else {}
    last_status = last_outcome.get("status") if isinstance(last_outcome, dict) else last_outcome
    blocker = last_status if last_status in {"failed", "blocked_by_permission"} else None
    details = methods.comparison_details(option, items)
    details["missing_data"] = [key for key in details["missing_data"] if key not in metrics]
    if buying_intent is not None:
        details["missing_data"] = [key for key in details["missing_data"] if key != "buying_intent"]
    return {"option_id": option["id"], "pain": metrics.get("pain") if "pain" in metrics else (
            f"{len(details['research']['pain'])} matching public problem reports" if details["research"]["pain"] else None),
        "need": metrics.get("need") if "need" in metrics else (
            "Recorded public accounts of the problem, not proof of demand" if details["research"]["need"] else None),
        "buying_intent": buying_intent, "method": ", ".join(sorted(observed_methods)) or methods.METHOD_LABELS.get(option.get("method"), "Not tested"),
        "evidence_level": level, "sample_size": sample, "min_sample": minimum,
        "counts": counts, "positive_reply_rate": rate, "metric": metric, "response_rate": response_rate,
        "outcome": verdict, "blocker": blocker, "citations": citations, "too_small": sample < minimum,
        "interval": _interval(response_rate * sample, sample) if response_rate is not None else None,
        **details}


def compose(round_, activity_items):
    rows = [option_result(option, [item for item in activity_items if item["option_id"] == option["id"]])
            for option in round_["options"]]
    compared_rows = [row for row in rows if not round_.get("ab_option_ids") or row["option_id"] in round_["ab_option_ids"]]
    comparable = len({(row["method"], row["metric"]) for row in compared_rows}) == 1 and all(not row["too_small"] for row in compared_rows)
    selected = None
    if comparable and all(row["interval"] is not None for row in compared_rows):
        best = max(compared_rows, key=lambda row: row["response_rate"])
        if best["outcome"] == "supported" and all(
                best["interval"][0] > other["interval"][1] for other in compared_rows if other is not best):
            selected = best["option_id"]
    too_small = any(row["too_small"] for row in compared_rows)
    uncertainty = ("Too small to conclude. You need more people before choosing a winner." if too_small else
        "You measured different things or used different methods, so these results are not directly comparable." if not comparable else
        "You have not recorded enough response details. You need those results before choosing." if any(row["interval"] is None for row in compared_rows) else
        "The results are too close to choose a winner yet." if selected is None else
        "This is your best-supported next test, not a guarantee that more people will buy.")
    explanations, why_not, citations = [], [], []
    for index, row in enumerate(rows, 1):
        references = row["citations"]
        citations.extend(references)
        if references:
            marker = " ".join(f"[activity:{item['id']}]" for item in references[-3:])
            explanations.append(f"Option {index}: you recorded {row['counts']['searches']} searches, "
                f"{row['counts']['sources']} reads of sources, {row['counts']['sent']} sends, "
                f"{row['counts']['replies']} replies and {row['counts']['meetings']} meetings. {marker}")
        why_not.append({"option_id": row["option_id"], "reason": (
            "You have not included this option in your two-message comparison." if round_.get("ab_option_ids") and row["option_id"] not in round_["ab_option_ids"] else
            "You have too few people in this test to rule this option out." if row["too_small"] else
            "You need more results from the other options before ruling this one out." if too_small else
            "You measured different things or used different methods, so you cannot rule this option out from these counts." if not comparable else
            "You recorded fewer responses to your test per person, with a clear gap." if selected and row["option_id"] != selected else
            "Your recorded results do not clearly separate this option from the others.")})
    next_step = {"label": "Run another round", "action": "experiment.exploration.next_round",
                 "payload": {"round_id": round_["id"]}, "style": "primary"}
    if round_.get("ab_option_ids") and too_small and not any(row["blocker"] for row in compared_rows):
        next_step = {"label": "Extend your email test", "action": "experiment.exploration.next",
            "payload": {"round_id": round_["id"], "extend": True}, "style": "primary"}
    explanation = " ".join(explanations) or "You have no recorded results to compare yet."
    if selected:
        number = next(index for index, row in enumerate(rows, 1) if row["option_id"] == selected)
        explanation = f"You should continue with option {number}: it has the clearest recorded response to your test. " + explanation
        next_step = {"label": "Plan your next test", "action": "experiment.beliefs.prepare", "style": "primary"}
    return {"options": [{key: value for key, value in row.items() if key != "interval"} for row in rows],
        "selected_option_id": selected, "too_small": too_small,
        "explanation": explanation,
        "why_not_the_others": [item for item in why_not if item["option_id"] != selected],
        "uncertainty": uncertainty, "citations": citations, "next_step": next_step}


def execution_mode(items):
    if not items:
        return "pending"
    return "parallel" if any(first["option_id"] != second["option_id"] and
        first["started_at"] < second["ended_at"] and second["started_at"] < first["ended_at"]
        for index, first in enumerate(items) for second in items[index + 1:]) else "sequential"
