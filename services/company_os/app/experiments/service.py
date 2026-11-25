"""Experiment evaluate job — records observations, never silently ships a winner."""

from __future__ import annotations

import json
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.core.fsm import advance
from app.credits.policy import get_policy
from app.db.models import ActivityLine, Venture
from app.db.session import serialize_writes
from app.db.supymem_models import Experiment, ExperimentObservation
from app.state import db
from app.tenancy import current_or_none
from core.clock import as_utc, utcnow
from core.ids import new_id


def create_experiment(
    *,
    kind: str,
    hypothesis: str,
    workspace_id: str | None = None,
    metric: str = "signup_rate",
    baseline: float | None = None,
) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = Experiment(
            id=new_id("exp"),
            workspace_id=workspace_id,
            kind=kind,
            hypothesis=hypothesis,
            metric=metric,
            baseline=baseline,
            state="proposed",
        )
        session.add(row)
        session.flush()
        advance(session, row, "running", actor="admin", reason="start")
        row.started_at = now
        session.commit()
        return {"id": row.id, "state": row.state}


def list_experiments() -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(Experiment).where(Experiment.kind != "venture_test").order_by(Experiment.id.desc())))
        return [
            {
                "id": r.id,
                "kind": r.kind,
                "hypothesis": r.hypothesis,
                "state": r.state,
                "metric": r.metric,
                "baseline": r.baseline,
                "result": r.result,
                "outcome": r.outcome,
                "learnings": r.learnings,
            }
            for r in rows
        ]


def conclude(experiment_id: str, *, abort: bool = False) -> dict[str, Any]:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(Experiment, experiment_id)
        if row is None:
            raise KeyError("experiment")
        advance(session, row, "aborted" if abort else "concluded", actor="admin", reason="admin")
        row.ended_at = now
        if abort:
            row.outcome = "aborted"
        session.commit()
        return {"id": row.id, "state": row.state}


def run_evaluate(_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.core.local_mode import is_local
    from app.experiments import ledger
    payload = _payload or {}
    factory = db()
    now = utcnow()
    evaluated = 0
    with factory() as session:
        flags = None if is_local() else get_policy("feature_flags")
        enabled = ledger.autopilot_enabled() if is_local() else isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True
        if payload.get("venture_id") and enabled:
            now = as_utc(payload.get("now") or now)
            from app.experiments import activity, autonomous, ledger
            venture_id = payload["venture_id"]
            serialize_writes(session, key="market-experiments:" + venture_id)
            venture = session.get(Venture, venture_id)
            scope = current_or_none()
            if (not venture or venture.workspace_id != payload.get("workspace_id") or
                    scope and (scope.workspace_id not in {"none", venture.workspace_id} or
                               scope.venture_id and scope.venture_id != venture_id)):
                raise PermissionError("experiment_evaluation_scope_invalid")
            query = select(Experiment).where(Experiment.kind == ledger.KIND,
                Experiment.workspace_id == venture.workspace_id, Experiment.venture_id == venture_id,
                Experiment.state.in_(("running", "concluded")))
            if "experiment_ids" in payload:
                query = query.where(Experiment.id.in_(payload["experiment_ids"]))
            for experiment in session.scalars(query):
                context = json.loads(experiment.context or "{}")
                spec = context.get("spec") or {}
                if (context.get("runner") != "system" or context.get("error") or
                        (spec.get("id"), spec.get("venture_id"), spec.get("workspace_id")) !=
                        (experiment.id, venture_id, venture.workspace_id) or
                        spec.get("direction") != ledger.current_direction(session, venture_id)):
                    continue
                result = context.get("result")
                if experiment.state == "running":
                    if spec.get("runner") == "outreach_split":
                        result = autonomous.outreach_tests.observe(spec)
                        if result:
                            result = {key: value for key, value in result.items() if key != "actual_micro"}
                    elif spec.get("scan") in {"page_test", "channel_test"}:
                        from app.experiments import channel_tests, page_tests
                        observer = page_tests if spec["scan"] == "page_test" else channel_tests
                        result = observer.observe(spec)
                    else:
                        continue
                if not result or result.get("error"):
                    continue
                measured = result.get("measured") or {}
                verdict = autonomous.outcome(spec, measured)
                if result.get("outcome") == "inconclusive":
                    verdict = "inconclusive"
                result = {**result, "outcome": verdict}
                start = context.get("prepared_at") or experiment.started_at
                start = as_utc(datetime.fromisoformat(start)) if isinstance(start, str) else as_utc(start) if start else now
                deadline = start + timedelta(days=spec.get("max_duration_days", 7))
                value = measured.get(spec.get("metric"))
                experiment.metric = spec.get("metric") or experiment.metric
                experiment.result = value if type(value) in (int, float) and math.isfinite(value) else None
                experiment.outcome, experiment.learnings = verdict, result.get("insight", experiment.learnings or "")
                if experiment.state == "running" and (verdict != "inconclusive" or now >= deadline):
                    advance(session, experiment, "concluded", actor="system", reason="venture_test_observation")
                    experiment.ended_at = now
                if spec.get("runner") == "outreach_split" and experiment.state == "running":
                    context["observed_result"] = result
                else:
                    context["result"] = result
                    context.pop("observed_result", None)
                    if spec.get("runner") == "outreach_split":
                        context["outreach_state"] = "concluded"
                readout = autonomous._fingerprint({"result": result, "state": experiment.state})
                if context.get("readout") != readout:
                    details = {"experiment_id": experiment.id, "outcome": verdict,
                               "measured": measured, "state": experiment.state}
                    session.add(ActivityLine(venture_id=venture_id, workspace_id=venture.workspace_id,
                        phase="experiments", kind="experiment_readout", status=verdict,
                        text="Your test results were checked against your saved thresholds.",
                        detail_json=json.dumps(details, allow_nan=False), ts=now))
                    activity.record(activity.run_context(context), action="compare", tool="experiment_results",
                        evidence_level="conclusion", started_at=now, ended_at=now, outcome=verdict, session=session)
                    context["readout"] = readout
                experiment.context = json.dumps(context, allow_nan=False)
                evaluated += 1
            session.commit()
            return {"evaluated": evaluated}
        if is_local():
            return {"evaluated": 0}
        running = list(session.scalars(select(Experiment).where(Experiment.state == "running", Experiment.kind != "venture_test")))
        for exp in running:
            value = float(exp.baseline or 0.0) + 0.01
            session.add(
                ExperimentObservation(
                    experiment_id=exp.id,
                    ts=now,
                    metric=exp.metric or "signup_rate",
                    value=value,
                    context=json.dumps({"source": "local_rollup"}),
                )
            )
            obs = list(
                session.scalars(
                    select(ExperimentObservation).where(ExperimentObservation.experiment_id == exp.id)
                )
            )
            if len(obs) >= 8 and exp.baseline is not None:
                mean = sum(o.value for o in obs) / len(obs)
                var = sum((o.value - mean) ** 2 for o in obs) / max(1, len(obs) - 1)
                se = math.sqrt(var / len(obs)) if var > 0 else 0.0
                z = (mean - exp.baseline) / se if se else 0.0
                exp.result = mean
                exp.learnings = json.dumps({"z": round(z, 3), "n": len(obs), "mean": mean})
                if abs(z) >= 1.96:
                    advance(session, exp, "concluded", actor="system", reason="z_test")
                    exp.ended_at = now
                    exp.outcome = "significant"
            evaluated += 1
        session.commit()
    return {"evaluated": evaluated}
