"""A deterministic company-in-a-box for God mode simulations.

The REAL scheduler handler, tick, guard, lifecycle, PMF loop, outreach engine, needs-you and chat milestones
run against real PostgreSQL. Only the outside world is faked: the model, web search, Gmail, and the
long-running product jobs (research, plan, build, QA, publish), which finish after a fake-clock delay
with realistic outputs. Time is a fake clock the test advances, so a ten day venture runs in seconds.

Everything the scripted founder does (answer a question, review a build, approve a send batch, reply as a
customer) goes through the same persisted state a real person would change.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select

UTC = timezone.utc


class FakeClock:
    def __init__(self, start: datetime):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class Trace:
    """One row per scheduler tick: what it did, what it waited on, why."""
    n: int
    at: datetime
    status: str
    next_key: str
    events: list[tuple[str, str, str]]  # (action, outcome, what)
    chat: list[tuple[str, str]]  # (kind, first line)
    needs: list[str]
    note: str = ""


@dataclass
class World:
    vid: str
    wid: str
    owner: str
    clock: FakeClock
    mp: Any
    state: dict[str, Any] = field(default_factory=dict)
    pending: list[tuple[datetime, str, Callable[[], None]]] = field(default_factory=list)
    sent: list[dict[str, Any]] = field(default_factory=list)
    inbox: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    founder_accepts: list[str] = field(default_factory=list)
    calls: list[tuple[datetime, str]] = field(default_factory=list)
    review_called_by_godmode: int = 0
    signups_per_day: Callable[["World"], float] = lambda w: 0.9
    _signup_carry: float = 0.0
    _last_advance: datetime | None = None
    model_calls: int = 0
    build_quote_credits: int = 8

    def __post_init__(self) -> None:
        self.state.update(dossier=None, plan=None, plan_proposal=False, builds=[], site_live=False, waitlist=False,
                          signups=0, tasks_open=False, diagnosis=False, rewrites=0, published=0)
        self._last_advance = self.clock.now

    # ------------------------------------------------------------------ install the fakes
    def install(self) -> None:
        mp, w = self.mp, self
        from app.autopilot import lifecycle, llm, outreach, pmf
        from app.chat import execute
        from app.ventures import next_step as ns

        # --- journey facts: idea + jobs + paused stay REAL; the rest comes from the world
        real_facts = ns._facts

        def facts(venture_id: str) -> dict[str, Any]:
            f = real_facts(venture_id)
            s = w.state
            builds = s["builds"]
            accepted = [b for b in builds if b["state"] == "accepted" and b["origin"] == "founder"
                        and b.get("artifact_profile") == "static_landing_v1" and venture_id == w.vid
                        and f.get("workspace_id") == w.wid
                        and (b.get("workspace_id"), b.get("venture_id")) == (w.wid, w.vid)]
            revision = w.current_confirmed_revision()
            f.update(
                plan_state=s["plan"], plan_has_proposal=s["plan_proposal"], dossier_state=s["dossier"],
                products=1 if builds else 0, accepted_build=any(b["state"] == "accepted" for b in builds),
                accepted_landing=bool(accepted),
                accepted_current_landing=bool(venture_id == w.vid and f.get("workspace_id") == w.wid and revision
                    and any(b.get("revision_id") == revision
                    and (b.get("workspace_id"), b.get("venture_id")) == (w.wid, venture_id) for b in accepted)),
                product_attempt=False, accepted_product=False, external_site=False, exp_review=False, exp_run=None,
                build_review_pending=any(b["state"] == "review" for b in builds),
            )
            return f

        mp.setattr(ns, "_facts", facts)
        mp.setattr("app.planning.store.list_current_tasks", lambda _v, **k: w.tasks())
        mp.setattr("app.ventures.state_brief._website_section", lambda _w, _v: w.website())
        mp.setattr("app.conversion.waitlist.status", lambda _v, _w, **k: {
            "enabled": w.state["waitlist"], "url": "https://clubattendance.test/join" if w.state["waitlist"] else None,
            "submission_verified": True, "total": w.state["signups"]})
        mp.setattr(execute, "research_needs_start", lambda _v: w.state["dossier"] is None)
        mp.setattr(execute, "execute_action", w.execute)
        mp.setattr(pmf, "accepted_build", lambda _v: next(((b["id"], True) for b in reversed(w.state["builds"])
                                                          if b["state"] == "accepted"), (None, False)))
        mp.setattr(llm, "ask_json", w.ask_json)
        mp.setattr(llm, "brief_text", lambda _v: "Idea: gym owners cut churn with automatic win-back messages.")
        mp.setattr(outreach, "_brief", lambda _v: {"idea": {"audience": "independent gym owners"}})
        mp.setattr(outreach, "_mailbox", lambda _ws: {"user": "u", "account": f"acct_{_ws}", "email": "founder@clubattendance.test"})
        mp.setattr(outreach, "_send_one", w.send_one)
        mp.setattr("app.approvals.external.execution_enabled", lambda: True)
        mp.setattr("app.gtm.supymem.composio_gmail.fetch_inbound_from", w.fetch_inbound)
        from app.gtm.supymem import composio_gmail

        def inbound():
            return {message["id"]: message for email in w.inbox for message in
                w.fetch_inbound(composio_user_id="", connected_account_id="", from_email=email)["messages"]}

        mp.setattr(composio_gmail, "get_gmail_profile", lambda *args: {"raw": {"historyId": str(int(w.clock.now.timestamp()))}})
        mp.setattr(composio_gmail, "fetch_history", lambda **kwargs: {
            "historyId": str(int(w.clock.now.timestamp())),
            "history": [{"messagesAdded": [{"message": {"id": ident}} for ident in inbound()]}]})
        mp.setattr(composio_gmail, "fetch_message", lambda **kwargs:
            inbound().get(kwargs["message_id"], {"id": kwargs["message_id"]}))
        mp.setattr(composio_gmail, "fetch_thread", lambda **kwargs: {"messages": [message
            for message in inbound().values() if message["threadId"] == kwargs["thread_id"]]})
        mp.setattr(composio_gmail, "fetch_query", lambda **kwargs: {"messages": []})
        mp.setattr("app.research.explore.explore_web", w.explore_web)
        async def pitch(_ws, _v, _idea):  # the outreach drafter's one metered rewrite, simulated like every provider seam
            return "a win-back message tool for independent gym owners"
        mp.setattr("app.gtm.playbooks.pitch", pitch)
        # --- idea service: the clarifier / validator are model-backed; the world plays them
        mp.setattr("app.idea.service.post_message", w.idea_post_message)
        mp.setattr("app.idea.validation.start_validation", w.idea_start_validation)
        mp.setattr(execute, "_confirm_idea_for_research", w.idea_confirm)
        # --- the one real review entry point: God mode must never reach it
        from app.builder import execution as managed_builds

        def review(*a: Any, **k: Any) -> Any:
            w.review_called_by_godmode += 1
            raise AssertionError("God mode called builder.execution.review: it must never accept a build for the founder")

        mp.setattr(managed_builds, "review", review)
        del lifecycle

    def current_confirmed_revision(self) -> str | None:
        """Real current idea custody for synthetic static-build facts; never a provider receipt."""
        from app.db.models import Venture, Workspace
        from app.db.supymem_models import IdeaRevision, IdeaSession
        from app.state import db
        with db()() as session:
            venture, workspace = session.get(Venture, self.vid), session.get(Workspace, self.wid)
            if (not venture or venture.workspace_id != self.wid or venture.archived_at or
                    venture.stage in {"archived", "purged"} or not workspace or workspace.archived_at):
                return None
            idea = session.scalar(select(IdeaSession).where(IdeaSession.workspace_id == self.wid,
                IdeaSession.venture_id == self.vid, IdeaSession.state != "abandoned")
                .order_by(IdeaSession.updated_at.desc()).limit(1))
            revision = session.get(IdeaRevision, idea.current_revision_id) if idea and idea.current_revision_id else None
            if (idea and idea.state == "confirmed" and revision and
                    idea.current_revision_id == idea.confirmed_revision_id and
                    (revision.workspace_id, revision.venture_id, revision.session_id) == (self.wid, self.vid, idea.id)):
                return revision.id
        return None

    # ------------------------------------------------------------------ world facts
    def tasks(self) -> list[dict[str, Any]]:
        if not self.state["tasks_open"]:
            return []
        return [{"id": "pt_build1", "title": "Build the landing page and waitlist", "source": "approved_plan:p1",
                 "runnable": True, "tags": ["capability:build.run", "plan_task:pt_build1"]}]

    def website(self) -> dict[str, Any]:
        s = self.state
        return {"live": s["site_live"], "url": "https://clubattendance.test" if s["site_live"] else None,
                "waitlist_enabled": s["waitlist"], "waitlist_url": "https://clubattendance.test/join" if s["waitlist"] else None}

    # ------------------------------------------------------------------ time
    def after(self, label: str, **delta: float) -> None:
        def noop() -> None:
            return None
        self.pending.append((self.clock.now + timedelta(**delta), label, noop))

    def schedule(self, label: str, fn: Callable[[], None], **delta: float) -> None:
        self.pending.append((self.clock.now + timedelta(**delta), label, fn))

    def settle(self) -> None:
        """Everything whose fake-clock time has come, in order, then waitlist signups for the elapsed time."""
        now = self.clock.now
        due = sorted([p for p in self.pending if p[0] <= now], key=lambda p: p[0])
        self.pending = [p for p in self.pending if p[0] > now]
        for _, _label, fn in due:
            fn()
        from sqlalchemy import update

        from app.db.models import Job
        from app.state import db

        with db()() as s:  # the worker answers the wake-up jobs the platform enqueues (needs-you resolve, ticks)
            s.execute(update(Job).where(Job.venture_id == self.vid, Job.kind == "autopilot.tick",
                                        Job.status.in_(("scheduled", "pending"))).values(status="succeeded"))
            s.commit()
        if self.state["site_live"]:
            hours = (now - self._last_advance).total_seconds() / 3600
            self._signup_carry += self.signups_per_day(self) * hours / 24
            whole = int(self._signup_carry)
            self._signup_carry -= whole
            self.state["signups"] += whole
        self._last_advance = now

    # ------------------------------------------------------------------ jobs
    def enqueue_job(self, kind: str, done: Callable[[], None], **delta: float) -> str:
        from app.core.jobs import enqueue
        from app.state import db

        with db()() as s:
            jid = enqueue(s, kind, {"sim": True}, workspace_id=self.wid, venture_id=self.vid)
            s.commit()

        def finish() -> None:
            from app.db.models import Job

            with db()() as s2:
                row = s2.get(Job, jid)
                row.status, row.finished_at = "succeeded", self.clock.now
                s2.commit()
            done()

        self.schedule(kind, finish, **delta)
        return jid

    def _job(self, kind: str, done: Callable[[], None], **delta: float) -> None:
        from app.core.jobs import enqueue
        from app.state import db

        with db()() as s:
            jid = enqueue(s, kind, {"sim": True}, workspace_id=self.wid, venture_id=self.vid)
            s.commit()

        def finish() -> None:
            from app.db.models import Job

            with db()() as s2:
                row = s2.get(Job, jid)
                row.status, row.finished_at = "succeeded", self.clock.now
                s2.commit()
            done()

        self.schedule(kind, finish, **delta)

    def _doc(self, kind: str, body: str) -> dict[str, Any]:
        from app.autopilot.receipts import saved_document
        from app.db.models import VentureDocument
        from app.state import db
        from core.ids import new_id

        ident = new_id("sim")
        with db()() as s:
            s.add(VentureDocument(id=ident, venture_id=self.vid, kind=kind, title=kind, body_md=body,
                                  created_at=self.clock.now, updated_at=self.clock.now))
            s.commit()
        saved_document(ident, kind, body)
        return {"kind": "text", "body_md": body, "actions": [], "citations": []}

    # ------------------------------------------------------------------ chat actions
    async def execute(self, venture_id: str, name: str, *, payload: dict[str, Any] | None = None,
                      actor_subject: str | None = None) -> dict[str, Any]:
        payload = dict(payload or {})
        s = self.state
        self.calls.append((self.clock.now, name))
        text = lambda b, **k: {"kind": "text", "body_md": b, "actions": [], "citations": [], **k}  # noqa: E731
        card = lambda action, state, body: {"kind": "confirmation", "body_md": body, "citations": [], "actions": [  # noqa: E731
            {"label": "Go ahead", "confirmation": {"action": action, "resulting_state": state}, "style": "primary"}]}
        if name == "research.start":
            def done() -> None:
                from app.db.supymem_models import ResearchDossier
                from app.research.execution import identifier
                from app.state import db

                with db()() as session:
                    session.add(ResearchDossier(id=identifier(job_id), workspace_id=self.wid, venture_id=self.vid,
                        revision_id=self.current_confirmed_revision(), kind="deep", state="complete",
                        summary=json.dumps({"findings": ["Gym owners track churn manually"]}),
                        created_at=self.clock.now, completed_at=self.clock.now))
                    session.commit()
                s["dossier"] = "complete"
            job_id = self.enqueue_job("research.deep", done, minutes=25)
            s["dossier"] = "running"
            return text("Deep research queued.")
        if name == "plan.generate":
            if "ceiling_micro" not in payload:
                return card("plan.generate", {"ceiling_micro": 5 * 10_000}, "A plan costs at most 5 credits.")

            def planned() -> None:
                s["plan"], s["plan_proposal"] = "draft", True
            self.enqueue_job("plan.compile", planned, minutes=10)
            return text("Plan generation queued.")
        if name == "plan.resources_none":
            return text("Noted: no extra resources.")
        if name == "plan.approve":
            s["plan"], s["tasks_open"] = "approved", True
            return self._doc("plan_approval", "Plan v1 approved: landing page + waitlist, then outreach.")
        if name == "build.prepare":
            if any(b["state"] == "review" for b in s["builds"]):  # what the real _product_build_reply says
                return {"kind": "navigation", "body_md": "The last build finished, but founder acceptance is not saved.",
                        "actions": [{"label": "Review build evidence", "href": f"/v/{venture_id}/builds"}], "citations": []}
            n = len(s["builds"]) + 1
            return card("build.start", {"app_id": "app1", "quote_id": f"q{n}", "input_digest": "d", "ceiling_micro": self.build_quote_credits * 10_000},
                        "Build quote: at most 8 credits.")
        if name == "build.start":
            s["tasks_open"] = False
            b = {"id": f"bld_{len(s['builds']) + 1}", "state": "queued", "origin": None,
                 "revision_id": self.current_confirmed_revision() if venture_id == self.vid else None,
                 "artifact_profile": "static_landing_v1",
                 "workspace_id": self.wid, "venture_id": venture_id}
            s["builds"].append(b)

            def built() -> None:
                b["state"] = "review"  # succeeded + static QA passed + review pending: content is NOT certified
            self.enqueue_job("build.run", built, minutes=40)
            return text("The reviewed build was queued.")
        # Like production: these persist their effect but register no receipt, so their outcome is "held" unless a card follows
        if name == "waitlist.enable":
            s["waitlist"] = True
            return card("waitlist.test", {}, "Waitlist is on. Test it before publishing.")
        if name == "waitlist.test":
            return card("site.publish", {}, "The waitlist test passed. Publish when ready.")
        if name == "site.publish":
            def live() -> None:
                s["site_live"] = True
                s["published"] += 1
            self.enqueue_job("deploy.run", live, minutes=15)
            return text("Publishing queued.")
        if name == "site.rewrite":
            s["rewrites"] += 1
            return text("Rewrote the page (draft only, not public).")
        if name == "gtm.diagnosis":
            s["diagnosis"] = True
            return text("Bottleneck: owners do not know who is about to churn.")
        if name.startswith("outreach."):
            from app.autopilot import outreach

            return await outreach.run(venture_id, name, payload, actor_subject=actor_subject)
        if name == "content.drafts":
            return text("Three post drafts saved.")
        return text(f"{name} done")

    # ------------------------------------------------------------------ founder behaviours
    def founder_review(self, decision: str = "accept") -> None:
        """What the Builds screen does when the founder presses Accept: origin is the founder."""
        for b in self.state["builds"]:
            if b["state"] == "review":
                b["state"] = "accepted" if decision == "accept" else "rejected"
                b["origin"] = "founder"
                self.founder_accepts.append(b["id"])

    # ------------------------------------------------------------------ idea session (clarifier + validator)
    QUESTIONS = [
        ("Who is the first customer you can name?", ["Independent gym owners with 100-400 members", "Boutique studio owners"]),
        ("What do they do today about members who stop coming?", ["Nothing until the cancellation email", "A manual spreadsheet"]),
        ("What would make them pay in the first week?", ["A weekly list of members about to churn", "Automatic win-back texts"]),
    ]

    def seed_idea(self, session: Any, ids: dict[str, str]) -> None:
        from app.db.supymem_models import ClarifierTurn

        self._ids = ids
        session.add(ClarifierTurn(id="ct_" + ids["sid"], session_id=ids["sid"], workspace_id=self.wid, venture_id=self.vid, turn=1,
                                  role="agent", kind="question", text=self.QUESTIONS[0][0],
                                  options=json.dumps([{"label": o} for o in self.QUESTIONS[0][1]]),
                                  created_at=self.clock.now.replace(tzinfo=None)))

    def _naive(self) -> datetime:
        return self.clock.now.replace(tzinfo=None)

    def idea_post_message(self, session_id: str, text: str, *, actor_subject: str | None = None,
                          expected_question_id: str | None = None, **_: Any) -> dict[str, Any]:
        from app.db.supymem_models import ClarifierTurn, IdeaSession
        from app.state import db
        from core.ids import new_id

        with db()() as s:
            last = s.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                             .order_by(ClarifierTurn.turn.desc()).limit(1)).first()
            if expected_question_id and last.id != expected_question_id:
                raise RuntimeError("question_changed")
            s.add(ClarifierTurn(id=new_id("ct"), session_id=session_id, workspace_id=self.wid, venture_id=self.vid,
                                turn=last.turn + 1, role="founder", kind="answer", text=text, created_at=self._naive()))
            sess = s.get(IdeaSession, session_id)
            asked = sess.questions_asked
            s.commit()
        self._job("idea.clarify_turn", lambda: self._next_clarifier_turn(session_id, asked), minutes=1)
        return {"ok": True}

    def _next_clarifier_turn(self, session_id: str, asked: int) -> None:
        from app.db.supymem_models import ClarifierTurn, IdeaSession
        from app.state import db
        from core.ids import new_id

        with db()() as s:
            last = s.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                             .order_by(ClarifierTurn.turn.desc()).limit(1)).first()
            sess = s.get(IdeaSession, session_id)
            if asked < len(self.QUESTIONS):
                q, opts = self.QUESTIONS[asked]
                s.add(ClarifierTurn(id=new_id("ct"), session_id=session_id, workspace_id=self.wid, venture_id=self.vid,
                                    turn=last.turn + 1, role="agent", kind="question", text=q,
                                    options=json.dumps([{"label": o} for o in opts]), created_at=self._naive()))
                sess.questions_asked = asked + 1
            else:
                s.add(ClarifierTurn(id=new_id("ct"), session_id=session_id, workspace_id=self.wid, venture_id=self.vid,
                                    turn=last.turn + 1, role="agent", kind="proposal",
                                    text="MVP: a weekly at-risk member list for independent gym owners.", created_at=self._naive()))
                sess.state = "reviewing"
            sess.updated_at = self._naive()
            s.commit()

    def idea_start_validation(self, session_id: str, *, venture_id: str, workspace_id: str, **_: Any) -> dict[str, Any]:
        from app.db.supymem_models import IdeaSession
        from app.state import db

        with db()() as s:
            sess = s.get(IdeaSession, session_id)
            sess.state, sess.updated_at = "validating", self._naive()
            s.commit()
        self._job("idea.validate", lambda: self._validation_done(session_id), minutes=12)
        return {"ok": True}

    def _validation_done(self, session_id: str) -> None:
        from app.db.supymem_models import IdeaSession, ValidationRun
        from app.idea.validation import METHODS
        from app.state import db
        from core.ids import new_id

        with db()() as s:
            sess = s.get(IdeaSession, session_id)
            for m in METHODS:
                s.add(ValidationRun(id=new_id("val"), session_id=session_id, revision_id=sess.current_revision_id,
                                    workspace_id=self.wid, venture_id=self.vid, method=m, state="complete",
                                    verdict="go", report="{}", created_at=self._naive(), completed_at=self._naive()))
            s.commit()

    def idea_confirm(self, venture_id: str, actor_subject: str | None, **_: Any) -> dict[str, Any] | None:
        from app.db.supymem_models import IdeaRevision, IdeaSession, ValidationRun
        from app.state import db

        with db()() as s:
            sess = s.scalar(select(IdeaSession).where(IdeaSession.workspace_id == self.wid,
                IdeaSession.venture_id == venture_id, IdeaSession.state != "abandoned")
                .order_by(IdeaSession.updated_at.desc()).limit(1))
            revision = s.get(IdeaRevision, sess.current_revision_id) if sess and sess.current_revision_id else None
            if (venture_id != self.vid or not sess or not revision or
                    (revision.workspace_id, revision.venture_id, revision.session_id) != (self.wid, self.vid, sess.id)):
                raise RuntimeError("fixture_idea_source_changed")
            for r in s.scalars(select(ValidationRun).where(ValidationRun.session_id == sess.id)):
                r.founder_decision = "accept"
            sess.state, sess.confirmed_at, sess.confirmed_revision_id = "confirmed", self._naive(), revision.id
            s.commit()
        return None

    # ------------------------------------------------------------------ model, web, gmail
    async def ask_json(self, venture_id: str, purpose: str, system: str, prompt: str, schema: dict[str, Any],
                       max_tokens: int = 3000) -> dict[str, Any] | None:
        self.model_calls += 1
        if purpose == "outreach.targets":
            return {"queries": ["independent gym owner contact email", "boutique fitness studio instagram",
                                "crossfit box small business directory email", "yoga studio owner contact"]}
        if purpose == "outreach.draft_batch":
            drafts = []
            for i, to in enumerate(dict.fromkeys(re.findall(r'"to": "([^"]+)"', prompt))):
                body = (f"Hi, I run a small project helping independent gyms spot members who are drifting away before they cancel. "
                        f"I came across your studio and wondered how you keep track of that today. Could I ask you 3 quick questions?")
                if i % 7 == 3:  # the model overreaches now and then: the claims guard must catch it
                    body = "Hi, our platform already sends win-back texts for 200 gyms and guarantees 30% less churn."
                drafts.append({"to": to, "subject": "Quick question about member retention", "body": body})
            return {"drafts": drafts}
        if purpose == "pmf.hypothesis":
            tried = re.findall(r"Already tried, do not repeat: (.*)", prompt)
            n = len(json.loads(tried[0])) if tried else 0
            var = re.search(r"Variable to change: (\w+)", prompt).group(1)
            return {"change": f"Test #{n + 1} on {var}: lead with the Monday morning list of members who missed two weeks in a row."}
        return None

    async def explore_web(self, query: str, max_results: int = 8, extract: int = 6, excerpt_chars: int = 3000) -> dict[str, Any]:
        self.searches = getattr(self, "searches", 0) + 1
        slug = re.sub(r"[^a-z]+", "", query.lower())[:6] + "x" * (self.searches % 7)  # the web keeps turning up new studios
        pages = []
        for i in range(5):
            host = f"{slug}{i}fitness.test"
            pages.append({"url": f"https://{host}/contact", "title": f"{slug.title()}{i} Fitness - Contact",
                          "excerpt": f"Call us or email hello@{host}. Follow instagram.com/{slug}{i}studio. also noreply@{host}"})
        pages.append({"url": "https://www.yelp.com/biz/x", "title": "Yelp", "excerpt": "info@yelp.com"})
        return {"pages": pages, "hits": []}

    def send_one(self, box: dict[str, str], to: str, subject: str, body: str, thread_id: str | None) -> dict[str, Any]:
        self.sent.append({"at": self.clock.now, "to": to, "subject": subject, "body": body, "thread": thread_id})
        tid = thread_id or f"t{len(self.sent)}"
        for x in self.sent[-1:]:
            x["thread"] = tid
        return {"provider_message_id": f"m{len(self.sent)}", "thread_id": tid}

    def fetch_inbound(self, *, composio_user_id: str, connected_account_id: str, from_email: str) -> Any:
        out = []
        for m in self.inbox.get(from_email.lower(), []):
            sent = next((x for x in self.sent if x["to"].lower() == from_email.lower()), None)
            out.append({**m, "threadId": (sent or {}).get("thread") or "t0", "id": "in-" + from_email})
        return {"messages": out}

    def customer_replies(self, email: str, text: str) -> None:
        self.inbox.setdefault(email.lower(), []).append({"snippet": text, "subject": "Re: Quick question"})
