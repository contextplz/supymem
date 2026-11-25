"""Deterministic founder calculations, with inputs saved in the admitted chat."""
from __future__ import annotations

import csv
import io
import json
import re

from app.chat.store import list_messages
from app.estimate.unit_economics import INPUT_QUESTIONS, REQUIRED_INPUTS, calculate_unit_economics, validate_inputs
from app.experiments.price_band import PRICE_FIELDS, PRICE_QUESTIONS, calculate_price_band

_ENTRY = re.compile(r"^(work out my price range|do the numbers for my venture)\s*[.!?]?(?:\s*:\s*|\s*\n|$)", re.I)


def _reply(body: str, state: dict | None = None, *, question: bool = False) -> dict:
    return {"kind": "numbers", "body_md": body, "actions": [],
            "citations": ([{"numbers_input": state}] if state is not None else []) + ([{"question": True}] if question else [])}


def _price_answers(text: str) -> list:
    if text.startswith(("[", "{")):
        value = json.loads(text)
        if isinstance(value, dict):
            return [value]
        if not isinstance(value, list):
            raise ValueError("Paste four prices per real respondent.")
        return [value] if len(value) == 4 and all(not isinstance(item, (list, dict)) for item in value) else value
    lines = text.splitlines()
    if tuple(next(csv.reader([lines[0]]))) == PRICE_FIELDS:
        return list(csv.DictReader(io.StringIO(text)))
    return [[value.strip() for value in re.split(r"[,\t]", line)] for line in lines if line.strip()]


def _price_reply(text: str, state: dict) -> dict:
    if not text and not state["answers"]:
        questions = "\n".join(f"{index}. {question}" for index, question in enumerate(PRICE_QUESTIONS, 1))
        return _reply("Ask these optional questions in your interviews:\n" + questions +
                      "\nPaste or record only real interview or survey answers: four comma-separated prices per respondent, "
                      "one respondent per line, in that order. Use the same currency and billing period, without currency symbols. "
                      "You need at least 5 valid answers. No answers are generated.", state, question=True)
    try:
        answers = _price_answers(text) if text else []
        if any(not isinstance(row, (list, tuple, dict)) or (not isinstance(row, dict) and len(row) != 4) for row in answers):
            raise ValueError("Paste four prices per real respondent, one respondent per line.")
        result = calculate_price_band([*state["answers"], *answers])
    except (ValueError, TypeError, csv.Error) as error:
        return _reply(f"{error} Your previously recorded answers are unchanged.", state)
    state = {**state, "answers": [*state["answers"], *answers]}
    if result["status"] != "ok":
        return _reply(result["message"] + " Paste your next real answers when you have them.", state)
    used, dropped = result["count_used"], result["count_dropped"]
    body = (f"Your price band is from {used} real answers you supplied. {dropped} dropped "
            f"({result['count_out_of_order']} out of order; the rest incomplete or invalid).\n\n"
            "| Point | Price |\n| --- | ---: |\n"
            f"| Acceptable range (PMC to PME) | {result['pmc']:,.2f} to {result['pme']:,.2f} |\n"
            f"| Optimal point | {result['optimal']:,.2f} |\n"
            f"| Indifference point | {result['indifference']:,.2f} |\n\n"
            "Prices use your input currency and billing period. These are price-sensitivity answers, not proof of purchases. "
            "Use this band to choose prices to test. You can record more real answers here.")
    return _reply(body, state)


def _number(value: float | None, unavailable: str = "Not recoverable") -> str:
    return unavailable if value is None else f"{value:,.2f}"


def _economics_table(result: dict) -> str:
    inputs = result["inputs"]
    ltv_note = "Unbounded at zero churn" if inputs["monthly_churn"] == 0 and result["contribution"] > 0 else "Not defined"
    ratio_note = "Not defined at zero CAC or churn"
    rows = [("Contribution per customer-month", _number(result["contribution"])),
            ("CAC payback (months)", _number(result["payback_months"])),
            ("LTV (contribution-based)", _number(result["ltv"], ltv_note)),
            ("LTV/CAC", _number(result["ltv_cac"], ratio_note)),
            ("Break-even active customers, before acquisition", _number(result["break_even_customers"])),
            ("First cash break-even month, including acquisition", str(result["months_to_break_even"] or "Not within your ramp")),
            ("Cash needed for your ramp", _number(result["cash_needed"]))]
    if inputs.get("starting_cash") is not None:
        rows.append(("Additional cash needed", _number(result["additional_cash_needed"])))
    body = "Your numbers, using only the inputs you supplied:\n\n| Metric | Result |\n| --- | ---: |\n"
    body += "\n".join(f"| {label} | {value} |" for label, value in rows)
    body += "\n\n| What-if | Contribution | Payback months | LTV/CAC | Break-even month | Cash needed |\n| --- | ---: | ---: | ---: | ---: | ---: |\n"
    body += "\n".join(f"| {label} | {_number(spec['contribution'])} | {_number(spec['payback_months'])} | "
                      f"{_number(spec['ltv_cac'], ratio_note)} | {spec['months_to_break_even'] or 'Not within your ramp'} | {_number(spec['cash_needed'])} |"
                      for label, spec in result["what_ifs"].items())
    flags = [("Base", result), *result["what_ifs"].items()]
    warnings = [f"{label}: {flag}" for label, spec in flags for flag in spec["red_flags"]]
    if warnings:
        body += "\n\nRed flags:\n" + "\n".join(f"- {flag}" for flag in warnings)
    body += (f"\n\nYour ramp covers {len(inputs['customer_ramp'])} months of active customers, starting from zero. "
             "Acquisition spending includes replacing churned customers. Fractional acquisitions are expected counts. "
             "Cash needed is the deepest cumulative shortfall in that ramp. Break-even month is the first non-negative monthly net cash flow, "
             "not recovery of past losses. LTV uses contribution divided by monthly churn. "
             "The churn what-if is capped at 100%. All money uses your currency, before tax. This is a planning model, not a promise.")
    return body


def _economics_reply(text: str, state: dict) -> dict:
    values = state["inputs"]
    awaiting = state.get("awaiting") or next((field for field in (*REQUIRED_INPUTS, "starting_cash") if field not in values), None)
    if text:
        try:
            if text.startswith("{"):
                update = json.loads(text)
                unknown = set(update) - set(INPUT_QUESTIONS)
                if unknown:
                    raise ValueError("Use these input names: " + ", ".join(INPUT_QUESTIONS) + ".")
            else:
                if awaiting is None:
                    raise ValueError("Paste corrections as named JSON inputs.")
                field = awaiting
                if field == "starting_cash" and text.lower() == "skip":
                    update = {field: None}
                elif field == "customer_ramp":
                    update = {field: json.loads(text) if text.startswith("[") else re.split(r"[,\s]+", text)}
                else:
                    update = {field: text}
            if isinstance(update.get("monthly_churn"), str) and update["monthly_churn"].endswith("%"):
                update["monthly_churn"] = float(update["monthly_churn"][:-1]) / 100
            values = validate_inputs({**values, **update})
        except (ValueError, TypeError, KeyError) as error:
            question = INPUT_QUESTIONS[awaiting] if awaiting else "Supply your named numeric inputs."
            return _reply(f"I could not use that input: {error} Your saved inputs are unchanged.\n{question}", state, question=True)
    missing = next((field for field in (*REQUIRED_INPUTS, "starting_cash") if field not in values), None)
    if missing:
        state = {"mode": "economics", "inputs": values, "awaiting": missing}
        intro = "Use one currency for all money inputs. I will ask only for missing inputs, one at a time.\n" if not values else ""
        return _reply(intro + INPUT_QUESTIONS[missing], state, question=True)
    try:
        return _reply(_economics_table(calculate_unit_economics(values)),
                      {"mode": "economics", "inputs": values, "complete": True})
    except ValueError as error:
        return _reply(f"{error} Your supplied inputs remain recorded. Paste corrections as a named JSON object.",
                      {"mode": "economics", "inputs": values})


def command(venture_id: str, text: str) -> tuple[str, dict] | None:
    """Only an explicit entry or a numeric reply to our latest question is admitted."""
    text = text.strip()
    entry = _ENTRY.match(text)
    numeric = re.match(r"^[\d+\-.\[{]|^(?:skip|nan|inf|true|false|unknown|not sure|i (?:do not|don['’]t) know|guess(?: for me)?)$", text, re.I)
    if not entry and not numeric and text.lower() not in {"cancel", "stop", "never mind"} and not text.startswith(",".join(PRICE_FIELDS)):
        return None
    # ponytail: resume searches the latest 200 chat rows, use an indexed calculation record for longer-lived sessions.
    latest = list_messages(venture_id, limit=200 if entry else 1)
    state = latest[-1].get("numbers_input") if latest and latest[-1]["role"] == "assistant" else None
    if entry:
        mode = "price" if entry[1].lower().startswith("work out") else "economics"
        state = next((row["numbers_input"] for row in reversed(latest) if row["role"] == "assistant"
                      and row.get("numbers_input", {}).get("mode") == mode),
                     {"mode": mode, **({"answers": []} if mode == "price" else {"inputs": {}})})
        state = {**state, "paused": False}
        text = text[entry.end():].strip()
    elif state and (state.get("paused") or state.get("complete")):
        return None
    elif state and text.lower() in {"cancel", "stop", "never mind"}:
        return ("price_band.calculate" if state["mode"] == "price" else "unit_economics.calculate",
                _reply("You stopped this calculation. Your recorded inputs remain in this conversation.", {**state, "paused": True}))
    elif not state:
        return None
    action = "price_band.calculate" if state["mode"] == "price" else "unit_economics.calculate"
    reply = _price_reply(text, state) if state["mode"] == "price" else _economics_reply(text, state)
    return action, reply
