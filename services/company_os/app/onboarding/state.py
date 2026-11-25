"""Onboarding run/step accessors and Ctx (26 §3)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select

from app.db.models import OnboardingRun, OnboardingStep, Venture
from app.state import db
from core.clock import utcnow
from core.ids import new_id

AUTONOMY_WORDS = {
    "L1": "Suggest",
    "L2": "Assist",
    "L3": "Act",
    "L4": "Autopilot",
}


@dataclass
class Budget:
    cost_usd: float = 0.0
    tokens: int = 0
    cap_usd: float = 0.30

    def exhausted(self) -> bool:
        return self.cost_usd >= self.cap_usd

    def charge(self, usd: float, tokens: int = 0) -> None:
        self.cost_usd += usd
        self.tokens += tokens


@dataclass
class Ctx:
    venture: Venture
    run: OnboardingRun
    out: dict[str, Any] = field(default_factory=dict)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    budget: Budget = field(default_factory=Budget)
    mode: str = "guided"
    narrator: Any = None
    warden: Any = None
    downgraded: bool = False
    session_factory: Any = None

    @property
    def company(self) -> str:
        return self.venture.name

    @property
    def autonomy_word(self) -> str:
        return AUTONOMY_WORDS.get(self.venture.autonomy_level or "L1", "Suggest")

    def flags(self) -> dict[str, Any]:
        try:
            return json.loads(self.run.flags_json or "{}")
        except json.JSONDecodeError:
            return {}

    def set_flag(self, key: str, value: Any) -> None:
        flags = self.flags()
        flags[key] = value
        self.run.flags_json = json.dumps(flags)
        persist_run(self.run)

    def input_digest(self, step_id: str) -> str:
        """Stable digest of inputs available before a step (for idempotency)."""
        from app.onboarding.registry import all_steps

        order = [s.id for s in all_steps()]
        try:
            idx = order.index(step_id)
        except ValueError:
            idx = len(order)
        prior_ids = set(order[:idx])
        prior = {k: self.out[k] for k in sorted(self.out) if k in prior_ids}
        blob = json.dumps(
            {"step": step_id, "mode": self.mode, "prior": prior},
            sort_keys=True,
            default=str,
        )
        import hashlib

        return hashlib.sha256(blob.encode()).hexdigest()[:32]

    @classmethod
    def build(cls, run: OnboardingRun, *, narrator: Any = None) -> Ctx:
        factory = db()
        with factory() as session:
            ven = session.get(Venture, run.venture_id)
            if not ven:
                raise ValueError(f"venture missing: {run.venture_id}")
            # Detach venture for use outside session (run is already detached)
            session.expunge(ven)
        outs = load_outputs(run.id)
        ctx = cls(
            venture=ven,
            run=run,
            out=outs,
            mode=run.mode,
            budget=Budget(cost_usd=run.cost_usd or 0.0, tokens=run.tokens_used or 0),
            narrator=narrator,
            session_factory=factory,
            downgraded=(ven.autonomy_level or "L1") in {"L1", "L2"},
        )
        return ctx


def load_run(run_id: str) -> OnboardingRun | None:
    factory = db()
    with factory() as session:
        row = session.get(OnboardingRun, run_id)
        if row:
            session.expunge(row)
        return row


def active_run(venture_id: str) -> OnboardingRun | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OnboardingRun)
            .where(
                OnboardingRun.venture_id == venture_id,
                OnboardingRun.status.in_(("running", "paused")),
            )
            .order_by(OnboardingRun.started_at.desc())
        ).first()
        if row:
            session.expunge(row)
        return row


def latest_run(venture_id: str) -> OnboardingRun | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(OnboardingRun)
            .where(OnboardingRun.venture_id == venture_id)
            .order_by(OnboardingRun.started_at.desc())
        ).first()
        if row:
            session.expunge(row)
        return row


def resumable_run(venture_id: str) -> OnboardingRun | None:
    """Active (running/paused) run, or the latest failed run Resume should continue."""
    current = active_run(venture_id)
    if current:
        return current
    latest = latest_run(venture_id)
    if latest and latest.status == "failed":
        return latest
    return None


def create_run(
    venture_id: str,
    *,
    mode: str = "guided",
    research_founder: bool = False,
    skip_research: bool = False,
    entry_mode: str = "idea",
    workspace_id: str | None = None,
) -> OnboardingRun:
    from app.onboarding.registry import phases_for_mode

    now = utcnow()
    phases = phases_for_mode(entry_mode)
    run = OnboardingRun(
        id=new_id("obr"),
        venture_id=venture_id,
        workspace_id=workspace_id,
        entry_mode=entry_mode,
        mode=mode if mode in {"guided", "auto"} else "guided",
        status="running",
        phase=phases[0],
        phases_done=0,
        phases_total=len(phases),
        flags_json=json.dumps({"skip_research": True} if skip_research else {}),
        research_founder=1 if research_founder else 0,
        started_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(run)
        session.commit()
        session.refresh(run)
        session.expunge(run)
    return run


def persist_run(run: OnboardingRun) -> None:
    factory = db()
    with factory() as session:
        row = session.get(OnboardingRun, run.id)
        if not row:
            return
        for col in (
            "status",
            "phase",
            "phases_done",
            "paused_reason",
            "resume_token",
            "tokens_used",
            "cost_usd",
            "error",
            "flags_json",
            "next_up_headline",
            "finished_at",
        ):
            setattr(row, col, getattr(run, col))
        row.updated_at = utcnow()
        session.commit()
        run.updated_at = row.updated_at


def load_outputs(run_id: str) -> dict[str, Any]:
    factory = db()
    out: dict[str, Any] = {}
    with factory() as session:
        rows = session.scalars(
            select(OnboardingStep).where(
                OnboardingStep.run_id == run_id,
                OnboardingStep.status == "done",
            )
        ).all()
        for r in rows:
            if r.output_json:
                try:
                    out[r.step_id] = json.loads(r.output_json)
                except json.JSONDecodeError:
                    out[r.step_id] = {}
    return out


def find_completed_step(
    run_id: str, step_id: str, idempotency_key: str
) -> OnboardingStep | None:
    factory = db()
    with factory() as session:
        # Prefer exact key match; fall back to any prior successful completion
        # so resume after pause does not re-run (digest may include new later outs).
        row = session.scalars(
            select(OnboardingStep).where(
                OnboardingStep.run_id == run_id,
                OnboardingStep.step_id == step_id,
                OnboardingStep.idempotency_key == idempotency_key,
                OnboardingStep.status == "done",
            )
        ).first()
        if not row:
            row = session.scalars(
                select(OnboardingStep)
                .where(
                    OnboardingStep.run_id == run_id,
                    OnboardingStep.step_id == step_id,
                    OnboardingStep.status.in_(("done", "skipped")),
                )
                .order_by(OnboardingStep.attempt.desc())
            ).first()
        if row:
            session.expunge(row)
        return row


def record_running(run_id: str, step_id: str, phase: str, key: str) -> OnboardingStep:
    factory = db()
    now = utcnow()
    # next attempt
    with factory() as session:
        prior = session.scalars(
            select(OnboardingStep).where(
                OnboardingStep.run_id == run_id,
                OnboardingStep.step_id == step_id,
            )
        ).all()
        attempt = max((p.attempt for p in prior), default=0) + 1
        rec = OnboardingStep(
            id=new_id("ost"),
            run_id=run_id,
            step_id=step_id,
            phase=phase,
            attempt=attempt,
            status="running",
            idempotency_key=key,
            started_at=now,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)
        session.expunge(rec)
    return rec


def record_done(rec: OnboardingStep, output: dict[str, Any], *, cost_usd: float = 0.0) -> None:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(OnboardingStep, rec.id)
        if not row:
            return
        row.status = "done"
        row.output_json = json.dumps(output, default=str)
        row.cost_usd = cost_usd
        row.finished_at = now
        if row.started_at:
            start = row.started_at
            if getattr(start, "tzinfo", None) is not None and now.tzinfo is None:
                start = start.replace(tzinfo=None)
            elif getattr(start, "tzinfo", None) is None and now.tzinfo is not None:
                now_cmp = now.replace(tzinfo=None)
                row.duration_ms = int((now_cmp - start).total_seconds() * 1000)
                session.commit()
                return
            row.duration_ms = int((now - start).total_seconds() * 1000)
        session.commit()


def record_failed(rec: OnboardingStep, error_class: str, detail: str = "") -> None:
    factory = db()
    now = utcnow()
    with factory() as session:
        row = session.get(OnboardingStep, rec.id)
        if not row:
            return
        row.status = "failed"
        row.error_class = error_class
        row.error_detail = detail[:2000]
        row.finished_at = now
        session.commit()


def record_skipped(run_id: str, step_id: str, phase: str, key: str) -> OnboardingStep:
    factory = db()
    now = utcnow()
    with factory() as session:
        rec = OnboardingStep(
            id=new_id("ost"),
            run_id=run_id,
            step_id=step_id,
            phase=phase,
            attempt=1,
            status="skipped",
            idempotency_key=key,
            output_json="{}",
            started_at=now,
            finished_at=now,
            duration_ms=0,
        )
        session.add(rec)
        session.commit()
        session.refresh(rec)
        session.expunge(rec)
    return rec


def list_steps(run_id: str) -> list[OnboardingStep]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(OnboardingStep)
            .where(OnboardingStep.run_id == run_id)
            .order_by(OnboardingStep.started_at.asc())
        ).all()
        for r in rows:
            session.expunge(r)
        return list(rows)
