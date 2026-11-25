"""Venture experiment ledger: hypothesis, what was tried, result, learning.

Rows live in the existing ``experiments`` table with ``kind="venture_test"`` (the admin A/B
tooling filters that kind out). Free-form parts (source, how-to-run, result text, direction)
sit in ``context`` JSON, so there is no schema change.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime
from typing import Any

from core.clock import as_utc, utcnow
from core.copy import founder_copy, no_dashes
from core.ids import new_id
from sqlalchemy import select

from app.chat.recovery import log_founder_exception
from app.core.fsm import advance
from app.db.models import Venture
from app.db.supymem_models import Experiment, IdeaSession
from app.experiments import normalize_action
from app.state import db

KIND = "venture_test"
OUTCOMES = ("supported", "refuted", "inconclusive")
FAILED = ("refuted",)
FAIL_THRESHOLD = 2  # this many failed tests end a direction even if others are still open
NOT_RUN = re.compile(r"\b(haven['’]?t|have not|not yet|didn['’]?t(?: get to)?|did not|never|yet to)\b"
                     r"[^.!?;\n]{0,40}\b(run|ran|do|done|start|started|perform\w*|conduct\w*|tried|try|tested)\b"
                     r"|\b(?:not|wasn['’]?t|weren['’]?t)\s+(?:yet\s+)?"
                     r"(?:run|done|started|performed|conducted|tried|tested|completed|finished)\b"
                     r"|(?:^|[.!?;:\n]\s*)(?:actually[,\s]+)?(?:(?:i|we)\s+)?"
                     r"(?:haven['’]?t|have not|didn['’]?t|did not|never|not yet|yet to)\s+(?:(?:actually|yet|ever)\s+)?"
                     r"(?:interview(?:ed)?|call(?:ed)?|phon(?:e|ed)|email(?:ed)?|talk(?:ed)?|spoke|spoken|speak|messag(?:e|ed)|survey(?:ed)?|contact(?:ed)?)\b", re.I)
_PERFORMED = re.compile(
    r"(?:^|[.!;:\n]\s*|,\s*)(?:i|we)(?:'ve| have)?\s+(?:just\s+|already\s+)?"
    r"(?:(?:ran|run|finished|completed|did|done|conducted|performed|tried)\s+"
    r"(?:(?:a|an|the|that|this|my|our|all|every|each)\s+){0,2}"
    r"(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?"
    r"(?:(?:planned|scheduled|customer|user)\s+(?:[\w-]+\s+){0,3}?)?(?:test|experiment|pilot|trial|survey|interviews?)\b"
    r"|(?:called|phoned|emailed|interviewed|talked to|spoke (?:to|with)|messaged|surveyed|asked|met with|sent|posted|launched|published|showed|shared|pitched|demoed|sold|charged|contacted|reached out|recruited|got|received)\s+\S)"
    r"|(?:^|[.!;:\n]\s*)(?:got|received|sent|posted|interviewed|called|phoned|emailed|talked\s+to|spoke\s+(?:to|with)|met\s+with|messaged|surveyed|contacted)\s+\S"
    r"|(?:^|[.!;:\n]\s*)(?![^.!?;\n]*\b(?:planned|proposed|scheduled|tomorrow|next\s+\w+)\b)"
    r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+(?:[\w-]+\s+){0,3}?(?:interviews?|chats?|calls?|conversations?)\b"
    r"|(?:^|[.!;:\n]\s*)(?:\d+|two|three|four|five|six|seven|eight|nine|ten|one)\s+(?:(?:out\s+)?of\s+\S+\s+)?(?:[\w-]+\s+){0,3}?(?:signed up|replied|responded|clicked|bought|paid|answered|showed up|booked|said|agreed to|asked for)\b"
    r"|(?:^|[.!;\n]\s*)(?:the|my|our)\s+(?:test|experiment|interviews?|calls?|survey|outreach|pilot)s?\s+"
    r"(?:went|yielded|produced|finished|completed)\b"
    r"|^(?:nobody|no ?one)\b[^.!?;\n]*?\b(?:signed up|replied|responded|clicked|bought|answered|showed up|paid)\b"
    r"|^(?:zero|no)\s+(?:sign ?ups?|replies|responses|reply|takers)\b", re.I)


def reports_performed_test(text: str, *, explicit_only: bool = True) -> bool:
    """Keep offline evidence explicit; allow personal reports through for model interpretation."""
    performed = _PERFORMED.search(text.strip())
    opening = re.split(r"[.!?;\n]", text.strip(), maxsplit=1)[0]
    if not text.strip() or (not performed and (explicit_only or re.search(
            r"\b(?:will|going to|plan to|planning|scheduling|scheduled)\b", opening, re.IGNORECASE))):
        return False
    # shortcut: ambiguous reports need first-person context; use structured results for impersonal reports.
    if not performed and not re.search(r"\b(?:i|we|my|our)\b", text, re.IGNORECASE):
        return False
    interviews = re.search(r"\b(?:interviewed|interviews?|chats?|calls?|called|phoned|talked|spoke|conversations?)\b", performed[0] if performed else text, re.IGNORECASE)
    for denial in NOT_RUN.finditer(text):
        if interviews and re.search(r"\b(?:finished|completed)$", denial[0], re.IGNORECASE) and reports_partial_test(text):
            continue
        if not explicit_only and re.search(r"\b(?:finished|completed)$", denial[0], re.IGNORECASE):
            subject = re.split(r"[.!?;:\n]", text[:denial.start()])[-1]
            if re.search(r"\b(?:was|were)\s*$", subject, re.IGNORECASE) and not re.search(
                    r"\b(?:test|experiment|interviews?|calls?)\s+(?:was|were)\s*$", subject, re.IGNORECASE):
                continue
        target = text[denial.end():].lstrip()
        if ((interviews or not explicit_only) and re.search(r"\b(?:run|ran|do|done|start|started|perform\w*|conduct\w*|tried|try|tested)$", denial[0], re.IGNORECASE)
                and target and not re.match(r"(?:it|this|that|anything|anyone|yet)\b|"
                    r"(?:(?:the|my|our|a|an|any|some|these|those|single|one|of|current|actual|same|selected|saved|\d+)\s+)*"
                    r"(?:tests?|experiments?|interview\w*|calls?)\b|[.!?;,]", target, re.IGNORECASE)):
            continue
        return False
    return True


def reports_partial_test(text: str) -> bool:
    # shortcut: explicit English progress markers only, use structured completion for ambiguous offline reports.
    for ending in re.finditer(r"\b(?:ended|stopped|finished|completed|cancelled|canceled)\s+"
            r"(?:(?:the|this|my|our)\s+)?(?:test|experiment)\b", text, re.IGNORECASE):
        if not re.search(r"(?:\bnot|\bnever|n['’]t)\s+(?:(?:yet|fully|completely|actually|already)\s+)?$",
                text[:ending.start()], re.IGNORECASE):
            return False
    return bool(re.search(r"\b(?:so far|partial results?|still interviewing|"
        r"(?:interviews?|calls?|tests?|visits?|trials?)\s+(?:left|remaining|to go))\b", text, re.IGNORECASE))


def autopilot_enabled() -> bool:
    from app.core.local_mode import is_local

    if is_local():
        from app.experiments.autonomous import autopilot_enabled

        return autopilot_enabled()
    from app.credits.policy import get_policy

    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True


def quality_enabled() -> bool:
    from app.core.local_mode import is_local

    if is_local():
        return True
    from app.credits.policy import get_policy

    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_quality_v1") is True


def _ctx(row: Experiment) -> dict[str, Any]:
    try:
        data = json.loads(row.context or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _how_to_run(action: str, measure: str) -> list[str]:
    steps = [f"Do this: {action}"] if action else []
    if measure:
        steps.append(f"Count this: {measure}")
    return steps + ["Decide how many people or how many days you will give it before you start.",
                    "Write down what actually happened, including silence and no-shows."]


def _hypothesis_key(direction: str, hypothesis: str) -> str:
    hypothesis = founder_copy(" ".join(str(hypothesis or "").split()))
    return hashlib.sha256(f"{direction}|{hypothesis.lower()}".encode()).hexdigest()[:24]


def _needs_evidence(context: dict) -> bool:
    details = context.get("provenance")
    return (context.get("evidence_type") == "founder_reported" and context.get("ran") is True
            and (not context.get("sample_size") or not isinstance(details, dict)
                 or any(not details.get(key) for key in ("source", "method", "observed_at"))))


def _view(row: Experiment) -> dict[str, Any]:
    c = _ctx(row)
    view = {"id": row.id, "hypothesis": founder_copy(row.hypothesis), "action": founder_copy(row.action), "measure": founder_copy(row.metric),
            "state": row.state, "outcome": row.outcome, "result_text": no_dashes(c.get("result_text", "")),
            "learning": no_dashes(row.learnings), "source": c.get("source", ""), "direction": c.get("direction", ""),
            "how_to_run": [founder_copy(step) for step in c.get("how_to_run", [])], "ran": c.get("ran"),
            "runner": c.get("runner"), "needs_evidence": _needs_evidence(c)}
    if c.get("sample"):
        view["source"] = "Sample"
        view["sample"] = True
        if view["result_text"]:
            view["result_text"] = "Sample result, not market evidence: " + view["result_text"]
        if view["learning"]:
            view["learning"] = "Sample learning, not market evidence: " + view["learning"]
    if quality_enabled() or c.get("evidence_type") == "founder_reported":
        if c.get("runner") != "system":
            view["how_to_run"].append("When you report a result, tell me how many people you reached or what you tested, where your evidence came from, how you checked it and when. Your result is labelled reported by you.")
        view.update({key: c[key] for key in ("sample_size", "provenance", "evidence_type", "launch_state", "first_exposure_at") if key in c})
        if c.get("evidence_type") == "founder_reported":
            view["evidence_label"] = "reported by you"
            view["learning"] += " (reported by you)"
    if c.get("sample"):
        view.update(ran=False, evidence_type="sample", evidence_label="Sample, not market evidence")
    return view


def current_direction(session, venture_id: str) -> str:
    """The idea revision the venture is currently pursuing; a pivot starts a new one."""
    idea = session.scalar(select(IdeaSession).where(
        IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned",
    ).order_by(IdeaSession.created_at.desc()).limit(1))
    return (idea.current_revision_id or "") if idea else ""


def create_from_tests(venture_id: str, source: str, tests: list, *, direction: str | None = None,
                      request_id: str | None = None) -> list[str]:
    """Record proposals by hypothesis, or explicitly requested designs by request."""
    made: list[str] = []
    try:
        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture is None:
                return []
            direction = direction if direction is not None else current_direction(session, venture_id)
            have = {(_ctx(r).get("key") if request_id else _hypothesis_key(_ctx(r).get("direction") or "", r.hypothesis)): r.id
                for r in session.scalars(select(Experiment).where(Experiment.venture_id == venture_id, Experiment.kind == KIND))
                if _ctx(r).get("runner") != "system"}
            for test in tests or []:
                t = test if isinstance(test, dict) else {"hypothesis": test}
                hypothesis = founder_copy(" ".join(str(t.get("hypothesis") or "").split())[:1500])
                if not hypothesis:
                    continue
                try:
                    action = normalize_action(t.get("action"))
                except ValueError as error:
                    log_founder_exception(error, venture_id, "experiment.ledger.create")
                    continue
                measure = founder_copy(" ".join(str(t.get("success_measure") or "").split())[:600])
                key = _hypothesis_key(direction, f"design:{request_id}|{hypothesis}" if request_id else hypothesis)
                if key in have:
                    if request_id:
                        made.append(have[key])
                    continue
                row = Experiment(id=new_id("exp"), workspace_id=venture.workspace_id, venture_id=venture_id,
                                 kind=KIND, hypothesis=hypothesis, action=action, metric=measure or None,
                                 state="proposed", context=json.dumps({
                                     "key": key, "seq": time.time_ns(), "source": source, "direction": direction,
                                     "how_to_run": _how_to_run(action, measure)}))
                from app.core.local_mode import is_sample_workspace
                if is_sample_workspace(venture.workspace_id):
                    row.context = json.dumps({**_ctx(row), "sample": True})
                session.add(row)
                have[key] = row.id
                made.append(row.id)
            session.commit()
    except Exception as exc:  # noqa: BLE001 - accepting validation/research must never fail on the ledger
        log_founder_exception(exc, venture_id, "experiment.ledger.create")
        return []
    if made:
        _invalidate(venture_id)
    return made


def on_validation_accepted(run_id: str) -> None:
    """Hook for idea.validation.decide(..., "accept")."""
    from app.db.supymem_models import ValidationRun

    venture_id = None
    try:
        with db()() as session:
            run = session.get(ValidationRun, run_id)
            if run is None:
                return
            venture_id, revision_id, raw = run.venture_id, run.revision_id, run.report
        create_from_tests(venture_id, "validation", (json.loads(raw or "{}") or {}).get("proposed_tests") or [],
                          direction=revision_id)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "experiment.ledger.validation_accepted")


def on_research_accepted(venture_id: str, plan_inputs: dict) -> None:
    """Hook for planning.compiler.start: asking for a plan means the founder accepted the research review."""
    try:
        create_from_tests(venture_id, "research", plan_inputs["context"]["research_review"].get("proposed_tests") or [],
                          direction=plan_inputs.get("revision_id"))
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "experiment.ledger.research_accepted")


def _invalidate(venture_id: str) -> None:
    from app.ventures.state_brief import invalidate_venture_state_brief

    invalidate_venture_state_brief(venture_id)
    from app.ventures import idea_page
    idea_page.request_refresh(venture_id)


def has_venture_access(venture_id: str) -> bool:
    from app.tenancy import current_or_none

    with db()() as session:
        venture = session.get(Venture, venture_id)
        scope = current_or_none()
        return bool(venture and (not scope or scope.workspace_id in {"none", venture.workspace_id}))


def list_for_venture(venture_id: str, *, include_system: bool = False) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(select(Experiment).where(
            Experiment.venture_id == venture_id, Experiment.kind == KIND)).all()
        return [_view(row) for row in sorted(rows, key=lambda row: _ctx(row).get("seq", 0))
                if include_system or _ctx(row).get("runner") != "system"]


def evidence_for_venture(venture_id: str) -> list[dict[str, Any]]:
    """Both founder and system ledgers, including metadata needed to rank safely."""
    with db()() as session:
        rows = session.scalars(select(Experiment).where(
            Experiment.venture_id == venture_id, Experiment.kind == KIND)).all()
        evidence = []
        for row in rows:
            context = _ctx(row)
            spec = context.get("spec") if isinstance(context.get("spec"), dict) else {}
            result = context.get("result") if isinstance(context.get("result"), dict) else {}
            view = _view(row)
            evidence.append({**view, "venture_id": venture_id, "spec": spec,
                "hypothesis": founder_copy(row.hypothesis or spec.get("hypothesis") or spec.get("assumption", "")),
                "state": "concluded" if row.state == "concluded" else context.get("outreach_state") or
                    ("running" if row.state == "proposed" and context.get("claimed") else row.state),
                "direction": spec.get("direction", context.get("direction")) or "",
                "outcome": row.outcome or result.get("outcome"),
                "learning": view["learning"] or no_dashes(result.get("insight", "")),
                "ended_at": row.ended_at, "runner": context.get("runner"),
                "variable": context.get("variable"), "audience": context.get("audience"),
                "input_fingerprint": context.get("input_fingerprint"), "result": result})
            if quality_enabled() and spec:
                from app.experiments.methods import evidence_binding
                evidence[-1]["evidence_binding"] = evidence_binding({**spec, "id": row.id})
        return evidence


def ranking_inputs(venture_id: str) -> dict[str, Any]:
    """Read the existing idea, journey and event sources for the pure engine."""
    from datetime import timedelta
    from urllib.parse import urlsplit

    from sqlalchemy import func

    from app.db.supymem_models import AppEvent
    from app.experiments import autonomous
    from app.ventures.current_idea import current_revision, revision_fields
    from app.ventures.startup_review import effective_fields

    snapshot = autonomous._snapshot(venture_id)
    if not snapshot:
        return {}
    journey = snapshot["journey"]
    volume = {"emails": (journey.get("results") or {}).get("sent", 0),
              "replies": (journey.get("results") or {}).get("replied", 0),
              "signups": snapshot["waitlist"].get("total", 0), "views": 0}
    with db()() as session:
        venture = session.get(Venture, venture_id)
        structured = revision_fields(current_revision(venture, session))
        fields = effective_fields(session, venture, structured)
        if snapshot.get("app_id"):
            now = utcnow()
            events = session.scalars(select(AppEvent).where(
                AppEvent.app_id == snapshot["app_id"], AppEvent.workspace_id == snapshot["workspace_id"],
                AppEvent.ts >= now - timedelta(days=14), AppEvent.ts <= now,
                AppEvent.name.in_(("landing_view", "page_view")))).all()
            site = snapshot["waitlist"].get("published_site") or {}
            path = urlsplit(site.get("url") or "").path.rstrip("/")
            for event in events:
                try:
                    event_path = json.loads(event.props or "{}").get("path")
                except (ValueError, AttributeError):
                    continue
                volume["views"] += isinstance(event_path, str) and event_path.rstrip("/") == path
            volume["activation"] = session.scalar(select(func.count()).select_from(AppEvent).where(
                AppEvent.app_id == snapshot["app_id"], AppEvent.workspace_id == snapshot["workspace_id"],
                AppEvent.name == "first_action")) or 0
    return {**snapshot, "fields": fields, "understanding": structured.get("understanding") or structured,
            "volume": volume, "experiments": evidence_for_venture(venture_id)}


def suggested_tests(venture_id: str) -> list[dict[str, str]]:
    """Saved proposed tests (latest validation review + latest plan's research review) not yet in the ledger."""
    from app.db.supymem_models import ExecutionPlan, ValidationRun

    found: list[tuple[str, dict]] = []
    with db()() as session:
        direction = current_direction(session, venture_id)
        run = session.scalar(select(ValidationRun).where(
            ValidationRun.venture_id == venture_id, ValidationRun.state == "complete",
            ValidationRun.revision_id == direction).order_by(ValidationRun.created_at.desc()).limit(1)) if direction else None
        plan = session.scalar(select(ExecutionPlan).where(ExecutionPlan.venture_id == venture_id,
            ExecutionPlan.revision_id == (direction or None)).order_by(ExecutionPlan.version.desc()).limit(1))
        try:
            found += [("validation", t) for t in (json.loads(run.report or "{}").get("proposed_tests") or [])] if run else []
            doc = json.loads(plan.structured or "{}") if plan else {}
            found += [("research", t) for t in (((doc.get("_runtime") or {}).get("context") or {})
                                                .get("research_review") or {}).get("proposed_tests") or []]
        except (ValueError, AttributeError):
            pass
    have = {r["hypothesis"] for r in list_for_venture(venture_id) if r["direction"] == direction}
    out: list[dict[str, str]] = []
    for source, t in found:
        t = t if isinstance(t, dict) else {"hypothesis": t}
        h = founder_copy(" ".join(str(t.get("hypothesis") or "").split())[:1500])
        if h and h not in have:
            try:
                action = normalize_action(t.get("action"))
            except ValueError as error:
                log_founder_exception(error, venture_id, "experiment.ledger.suggested")
                continue
            have.add(h)
            out.append({"hypothesis": h, "action": action,
                        "success_measure": founder_copy(" ".join(str(t.get("success_measure") or "").split())[:600]),
                        "source": source, "direction": direction})
    return out


def get(experiment_id: str, venture_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        return _view(row) if row and row.venture_id == venture_id and row.kind == KIND and _ctx(row).get("runner") != "system" else None


def start_proposed(venture_id: str, test: dict, source: str = "validation") -> dict[str, Any] | None:
    """Record one proposed test (idempotent) and start it. None when the hypothesis is empty."""
    with db()() as session:
        direction = current_direction(session, venture_id)
    if test.get("direction") is not None and test["direction"] != direction:
        return None
    create_from_tests(venture_id, source, [test], direction=direction)
    want = founder_copy(" ".join(str(test.get("hypothesis") or "").split())[:1500])
    for row in list_for_venture(venture_id):
        if row["hypothesis"] == want and row["direction"] == direction:
            return start(row["id"], venture_id)
    return None


def start(experiment_id: str, venture_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.get(Experiment, experiment_id)
        if row is None or row.venture_id != venture_id or row.kind != KIND or _ctx(row).get("runner") == "system":
            raise KeyError("experiment")
        if row.state == "proposed":
            advance(session, row, "running", actor="founder", reason="chat_start")
            row.started_at = utcnow()
            session.commit()
        out = _view(row)
    _invalidate(venture_id)
    from app.autopilot import receipts
    receipts.persisted_record(Experiment, experiment_id, {"state": out["state"]})
    return out


def record_result(experiment_id: str, venture_id: str, ran: bool, result_text: str, outcome: str,
                  learning: str, *, sample_size=None, provenance=None, complete: bool = True) -> dict[str, Any]:
    """Save performed results, leaving partial reports running. ``ran=False`` notes an unperformed test without changing state."""
    from app.core.local_mode import is_local, is_sample_workspace
    from app.db.session import serialize_writes

    local_report = ran and is_local()
    if ran and outcome not in OUTCOMES:
        raise ValueError("outcome")
    if type(complete) is not bool:
        raise ValueError("complete")
    with db()() as session:
        if local_report:
            serialize_writes(session, key="market-experiments:" + venture_id)
        row = session.get(Experiment, experiment_id)
        if row is None or row.venture_id != venture_id or row.kind != KIND or _ctx(row).get("runner") == "system":
            raise KeyError("experiment")
        c = _ctx(row)
        sample = local_report and bool(c.get("sample"))
        if local_report and (row.state not in {"proposed", "running", "concluded"}
                or sample != is_sample_workspace(row.workspace_id)):
            raise KeyError("experiment")
        if row.state == "concluded" and not (local_report and _needs_evidence(c)):
            raise RuntimeError("experiment_concluded")
        previous = {key: c[key] for key in ("result_text", "sample_size", "provenance", "evidence_type") if key in c}
        previous["learning"] = row.learnings
        quality = ran and not sample and quality_enabled()
        if quality or (local_report and not sample):
            if sample_size is None and provenance is None:
                try:
                    report = json.loads(result_text)
                except (ValueError, TypeError):
                    report = {}
                if isinstance(report, dict) and "result_text" in report:
                    sample_size, provenance = report.get("sample_size"), report.get("provenance")
                    result_text = report.get("result_text", "")
            fields = ("source", "method", "observed_at")
            details = provenance if isinstance(provenance, dict) else {}
            missing = sample_size is None or any(not details.get(key) for key in fields)
            if ((sample_size is not None and (type(sample_size) is not int or sample_size <= 0))
                    or (provenance is not None and not isinstance(provenance, dict))
                    or any(details.get(key) is not None and not isinstance(details[key], str) for key in fields)
                    or (not local_report and (missing or any(not details[key].strip() for key in fields)))):
                raise ValueError("Your reported result needs a positive sample size and source details. Tell me how many people you reached or what you tested, where your evidence came from, how you checked it and when.")
            details = {key: founder_copy(details[key].strip())[:1000] for key in fields if details.get(key)}
            if local_report and _needs_evidence(c):
                prior_details = c.get("provenance")
                details = {**(prior_details if isinstance(prior_details, dict) else {}),
                    **{key: value for key, value in details.items() if value}}
                if sample_size is None:
                    sample_size = c.get("sample_size")
            if details.get("observed_at"):
                try:
                    observed_at = as_utc(datetime.fromisoformat(details["observed_at"]))
                except ValueError as error:
                    raise ValueError("Your reported result needs a valid date. Tell me when it actually happened.") from error
                if observed_at > utcnow():
                    raise ValueError("Your reported date cannot be in the future. Tell me when it actually happened.")
            c.update(provenance=details, evidence_type="founder_reported")
            if sample_size is not None:
                c["sample_size"] = sample_size
            else:
                c.pop("sample_size", None)
            if local_report and (sample_size is None or any(not details.get(key) for key in fields)):
                complete = False
                outcome = "inconclusive"
                learning = str(learning or "") + " Your observations are reported by you, not independently verified."
        if row.state == "concluded" and (not complete or reports_partial_test(str(result_text or ""))):
            raise ValueError("Your reported result still needs details.")
        if ran and not reports_performed_test(str(result_text or ""), explicit_only=False) and (
                local_report or NOT_RUN.search(str(result_text or "")) or not quality):
            raise ValueError("experiment_not_run")
        if c.get("ran") and c.get("result_text") and c["result_text"] != " ".join(str(result_text or "").split())[:2000]:
            c.setdefault("previous_reports", []).append(previous)
        c["ran"] = bool(ran) and not sample
        c["result_text"] = " ".join(str(result_text or "").split())[:2000]
        if ran:
            if row.state == "proposed":
                advance(session, row, "running", actor="founder", reason="chat_result")
                row.started_at = row.started_at or utcnow()
            if complete and not reports_partial_test(str(result_text or "")):
                if row.state != "concluded":
                    advance(session, row, "concluded", actor="founder", reason="chat_result")
                row.ended_at = utcnow()
                row.outcome = None if sample else outcome
            row.learnings = " ".join(str(learning or "").split())[:1500]
        row.context = json.dumps(c)
        session.commit()
        out = _view(row)
    _invalidate(venture_id)
    return out


def loop_state(venture_id: str) -> dict[str, Any]:
    """Counts for the current direction and whether the learning loop should speak."""
    from app.core.local_mode import is_local
    with db()() as session:
        direction = current_direction(session, venture_id)
    enabled = autopilot_enabled()
    local = is_local()
    rows = [r for r in (evidence_for_venture(venture_id) if enabled or local else list_for_venture(venture_id))
            if r["direction"] == direction and (enabled or r["runner"] != "system" or r["state"] == "concluded")]
    done = [r for r in rows if r["state"] == "concluded" and not (local and r["needs_evidence"])]
    failed = [r for r in done if r["outcome"] in (("refuted",) if enabled else FAILED)]
    open_ = [r for r in rows if r["state"] != "concluded" or (local and r["needs_evidence"])]
    supported = [r for r in done if r["outcome"] == "supported"]
    ready = bool(failed) and not supported and (not open_ or len(failed) >= FAIL_THRESHOLD)
    revenue = None
    if enabled:
        from app.connections import crm, local_config, surveys
        from app.connections.revenue import snapshot
        revenue = snapshot(venture_id)
        if revenue and revenue.get("strength") == "cash":
            ready = False
        crm_result = None if is_local() and local_config.missing("hubspot") else crm.snapshot(venture_id)
        survey_result = surveys.snapshot(venture_id)
        connection_evidence = []
        if crm_result and crm_result.get("status") == "connected":
            if crm_result["strength"] == "commitment":
                ready = False
            connection_evidence.append({"strength": crm_result["strength"], "scope": crm_result["scope"],
                "outcome": "inconclusive", "hypothesis": "Your customer deals", "connection": "hubspot",
                "learning": " ".join(crm.summary_lines(crm_result)),
                "counts": {key: crm_result[key] for key in ("contact_count", "deal_count", "stage_counts", "progress_count")},
                "sources": [{"url": "/connections", "title": "Your connected HubSpot account"}]})
        if survey_result and survey_result.get("status") == "connected":
            for experiment, line in zip(survey_result["experiments"], surveys.summary_lines(survey_result)):
                connection_evidence.append({"strength": "facts", "scope": "linked_experiment",
                    "outcome": "inconclusive", "hypothesis": "Your survey responses", "connection": "typeform",
                    "experiment_id": experiment["experiment_id"], "response_count": experiment["response_count"],
                    "responses": experiment["responses"],
                    "learning": line,
                    "sources": [{"url": experiment["url"], "title": "Your linked Typeform form"}]})
    return {"direction": direction, "rows": rows, "done": done, "failed": failed, "open": open_,
            "supported": supported, "ready": ready, **({"revenue": revenue} if revenue is not None else {}),
            **({"crm": crm_result, "surveys": survey_result, "connection_evidence": connection_evidence} if enabled else {})}


def summary_lines(venture_id: str, limit: int = 4) -> list[str]:
    """Compact grounded lines for the state brief."""
    try:
        rows = list_for_venture(venture_id)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "experiment.ledger.summary")
        return []
    if not rows:
        return []
    counts = {s: sum(r["state"] == s for r in rows) for s in ("proposed", "running", "concluded")}
    lines = [f"Experiments: {counts['proposed']} not run, {counts['running']} running, "
             f"{counts['concluded']} finished ("
             + ", ".join(f"{sum(r['outcome'] == o for r in rows)} {o}" for o in OUTCOMES) + ")"]
    for r in [x for x in rows if x["state"] == "concluded"][-limit:]:
        lines.append(f"- {r['outcome']}: {r['hypothesis'][:100]} | learned: {r['learning'][:120]}")
    for r in [x for x in rows if x["state"] != "concluded"][:2]:
        lines.append(f"- {r['state']}: {r['hypothesis'][:100]}")
    return lines

"""Saved experiment data for the flag-gated rounds feed. No planning or execution."""
import math
from datetime import timedelta

from sqlalchemy import and_, or_

from app.authz.workspace import subject_person_id
from app.chat import experiments
from app.db.models import ChatConversation, ChatMessage


SPEC_FIELDS = ("title", "hypothesis", "scan", "catalog_key", "runner", "approval", "metric",
               "min_sample", "max_duration_days", "founder_minutes", "ceiling_micro", "approach",
               "success_threshold", "failure_threshold")
DECISIONS = ("experiment.persevere", "experiment.pivot", "experiment.spinoff", "experiment.kill")


def _number(value, *, integer=False):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0 and (
        not integer or value <= 2**53 - 1 and value % 1 == 0)


def _experiment(row, direction):
    context = _ctx(row)
    saved = context.get("spec")
    if saved is None and context.get("runner") != "system" and row.state != "proposed":
        saved = {"hypothesis": row.hypothesis, "metric": row.metric}
    if not isinstance(saved, dict) or saved.get("venture_id", row.venture_id) != row.venture_id:
        return None
    if saved.get("id", row.id) != row.id:
        return None
    state = "concluded" if row.state == "concluded" else context.get("outreach_state") or (
        "running" if row.state == "proposed" and context.get("claimed") else row.state)
    if state == "proposed" and (saved.get("direction", context.get("direction")) or "") != direction:
        return None
    spec = {key: saved[key] for key in SPEC_FIELDS if key in saved}
    spec.update(id=row.id, venture_id=row.venture_id, state=state)
    for key in ("title", "hypothesis", "metric", "scan", "catalog_key", "runner", "approval"):
        if isinstance(spec.get(key), str):
            if key in {"title", "hypothesis", "metric"}:
                spec[key] = founder_copy(spec[key])
        else:
            spec.pop(key, None)
    for key in ("min_sample", "ceiling_micro", "founder_minutes", "max_duration_days"):
        if key in spec and not _number(spec[key], integer=key in {"min_sample", "ceiling_micro"}):
            del spec[key]
    for key in ("success_threshold", "failure_threshold"):
        threshold = spec.get(key)
        if not isinstance(threshold, dict):
            spec.pop(key, None)
            continue
        if (threshold.get("op") not in (">=", ">", "<=", "<", "==", "!=")
                or type(threshold.get("value")) not in (int, float) or not math.isfinite(threshold["value"])
                or not _number(threshold.get("min_events", spec.get("min_sample")), integer=True)):
            del spec[key]
            continue
        spec[key] = {field: threshold[field] for field in ("op", "value", "min_events") if field in threshold}
        if isinstance(threshold.get("metric"), str):
            spec[key]["metric"] = founder_copy(threshold["metric"])
    spec["approach"] = "red-pill" if saved.get("approach") == "red-pill" or saved.get("scan") == "redteam" or (
        str(saved.get("catalog_key") or "").split("_")[0] in {"E19", "E37", "E39"}) else "sensible"
    spec["contacts_people"] = saved.get("contacts_people") is True or saved.get("runner") == "outreach_split" or saved.get("approval") == "each_send"
    result = context.get("result")
    result = result if isinstance(result, dict) else {}
    measured = result.get("measured")
    collected = measured.get("events") if isinstance(measured, dict) else None
    collected = collected if _number(collected, integer=True) else None
    outcome = row.outcome or result.get("outcome")
    outcome = outcome if outcome in OUTCOMES else None
    started = as_utc(row.started_at)
    review_at = None
    if started and "max_duration_days" in spec:
        try:
            review_at = (started + timedelta(days=spec["max_duration_days"])).isoformat()
        except OverflowError:
            pass
    return {"id": row.id, "venture_id": row.venture_id, "state": state,
            "hypothesis": founder_copy(row.hypothesis or spec.get("hypothesis") or spec.get("title") or ""),
            "spec": spec, "started_at": started.isoformat() if started else None,
            "outcome": outcome, "collected": collected, "n_min": spec.get("min_sample"), "review_at": review_at,
            "result": {"measured": {"events": collected} if collected is not None else {},
                       **({"outcome": outcome} if outcome else {})} if result else None}


def _recommendation(session, venture_id, workspace_id, subject, experiment_ids):
    actor = subject_person_id(subject)
    visible = select(ChatConversation.id).where(
        ChatConversation.venture_id == venture_id, ChatConversation.workspace_id == workspace_id,
        ChatConversation.archived_at.is_(None),
        or_(ChatConversation.visibility == "legacy_shared", and_(ChatConversation.visibility == "private",
            ChatConversation.creator_person_id == actor)))
    messages = session.scalars(select(ChatMessage).where(
        ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant",
        or_(ChatMessage.workspace_id == workspace_id, ChatMessage.workspace_id.is_(None)),
        or_(ChatMessage.conversation_id.is_(None), ChatMessage.conversation_id.in_(visible)),
        ChatMessage.actions_json.is_not(None)).order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()))
    for message in messages:
        try:
            actions = json.loads(message.actions_json)
        except ValueError:
            continue
        if not isinstance(actions, list):
            continue
        actions = [action for action in actions if isinstance(action, dict) and action.get("action") in DECISIONS]
        if not actions:
            continue
        state = loop_state(venture_id)
        if any(item["id"] not in experiment_ids for item in state["done"]):
            return None
        current = []
        for action in actions:
            name = action["action"]
            card = action.get("confirmation")
            payload = card.get("resulting_state") if isinstance(card, dict) else None
            if not isinstance(payload, dict) or card.get("action") != name or not state["done"]:
                continue
            if name in {"experiment.pivot", "experiment.kill"} and not state["ready"]:
                continue
            direction = {key: payload.get(key) for key in ("kind", "text", "idea")} if name in {
                "experiment.pivot", "experiment.spinoff"} else None
            if payload == experiments._decision_payload(venture_id, name, state, direction):
                current.append(action)
        if not current:
            return None
        labels = {"supported": "Worked", "refuted": "Did not work", "inconclusive": "Too early to tell"}
        line = " ".join(f"{labels.get(item['outcome'], 'Too early to tell')}: "
                        f"{founder_copy(item['learning'] or item['hypothesis'])}" for item in current[0]["confirmation"]["resulting_state"]["evidence"])
        return {"evidence_line": line, "actions": current}
    return None


def autopilot_view(venture_id, workspace_id, subject=None):
    with db()() as session:
        direction = current_direction(session, venture_id)
        rows = session.scalars(select(Experiment).where(
            Experiment.venture_id == venture_id, Experiment.workspace_id == workspace_id,
            Experiment.kind == KIND).order_by(Experiment.id))
        saved = [item for row in rows if (item := _experiment(row, direction)) is not None]
        recommendation = _recommendation(session, venture_id, workspace_id, subject, {item["id"] for item in saved})
    from app.credits import spend
    if spend.enabled():
        rows = {row["id"]: row for row in spend.view(workspace_id, venture_id)["experiments"]}
        for item in saved:
            if row := rows.get(item["id"]):
                item["spend"] = {key: row[key] for key in ("spent_micro", "cap_micro", "status")}
    return {"venture_id": venture_id, "experiments": [item for item in saved if item["state"] != "proposed"],
            "proposed_experiments": [item["spec"] for item in saved if item["state"] == "proposed"],
            "decision_recommendation": recommendation}
