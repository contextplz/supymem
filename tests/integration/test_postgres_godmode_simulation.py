"""End-to-end God mode simulation: a brand-new venture, idea to PMF iterations, on real PostgreSQL.

The real scheduler handler ticks every five fake minutes (thirty later, to keep it quick). Providers and the slow
product jobs are fake but realistic (tests/integration/godmode_world.py). Every tick is recorded: what it did,
what it waited on, why. The assertions are the things a capable cofounder must never do: stall, loop, act
twice, spam the chat, ask a human something it can do itself, accept a build the founder has not reviewed,
or cross a floor (credits, consent to send, credentials, deletes).
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from tests.integration.test_postgres_auth_security import pg_database as pg_database, service_modules

UTC = timezone.utc
START = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)


@pytest.fixture()
def sim(pg_database, tmp_path, monkeypatch):
    url, engine = pg_database
    for key, value in {"APP_ENV": "local", "COMPANY_OS_DB_URL": url, "COMPANY_OS_DATA_DIR": str(tmp_path),
                       "COMPANY_OS_WORKING_MEMORY_PATH": str(tmp_path / "memory.db"), "LLM_OFFLINE": "1",
                       "WILL_SCHEDULER": "0", "CONTENT_ENGINE_ENABLED": "0", "CONTENT_AGENT_ENABLED": "0",
                       "MARKETING_EXECUTION_ENABLED": "0", "MARKETING_OUTBOUND_ENABLED": "0", "CHANNELS_DRY_RUN": "true"}.items():
        monkeypatch.setenv(key, value)
    with service_modules("company_os"):
        from app.authz.workspace import ensure_subject_person
        from app.db.models import GtmEmailAccount, Venture
        from app.db.supymem_models import IdeaRevision, IdeaSession
        from app.state import set_factory
        from app.workspaces.service import create_workspace
        from core import clock as core_clock
        from core.ids import new_id
        from tests.integration.godmode_world import FakeClock, World

        factory = sessionmaker(engine, expire_on_commit=False)
        set_factory(factory)
        owner = "usr_sim_" + uuid.uuid4().hex[:12]
        ensure_subject_person(owner)
        clock = FakeClock(START)
        core_clock.set_clock(clock)
        ws = create_workspace(kind="company", name="Club Attendance sim", owner_id=owner)
        vid, sid, rid = new_id("ven"), new_id("ids"), new_id("idv")
        naive = START.replace(tzinfo=None)
        with factory() as session:
            session.add(Venture(id=vid, workspace_id=ws.id, name="Club Attendance", raw_idea="win back gym members who stop coming",
                                stage="draft", autonomy_level="L1", created_at=START, updated_at=START))
            session.flush()
            session.add(IdeaSession(id=sid, workspace_id=ws.id, venture_id=vid, case="know", state="clarifying",
                                    questions_asked=1, current_revision_id=rid, created_at=naive, updated_at=naive))
            session.flush()
            session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=ws.id, venture_id=vid, rev=1, source="founder",
                                     structured=json.dumps({"offer": "win back gym members"}), created_at=naive))
            session.commit()
            world = World(vid=vid, wid=ws.id, owner=owner, clock=clock, mp=monkeypatch)
            world.seed_idea(session, {"sid": sid})
            session.commit()
        world.install()
        from app.autopilot import outreach
        box = outreach._mailbox(ws.id)
        with factory() as session:
            session.add(GtmEmailAccount(id=new_id("mail"), workspace_id=ws.id, app_user_id=owner,
                email=box["email"], composio_user_id=box["user"], connected_account_id=box["account"],
                status="active", created_at=naive, updated_at=naive))
            session.commit()
        yield world
        core_clock.set_clock(None)


class Runner:
    """Ticks the REAL scheduler handler and records each tick."""

    def __init__(self, world):
        from app.worker.handlers import install_default_handlers

        install_default_handlers()
        self.w = world
        self.trace = []
        self.seen_events: set[str] = set()
        self.seen_chat: set[str] = set()
        self.n = 0

    def _new(self):
        from app.db.models import ChatMessage
        from app.db.supymem_models import AutopilotEvent, NeedsYouRequest
        from app.state import db

        with db()() as s:
            ev = [e for e in s.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == self.w.vid)
                                       .order_by(AutopilotEvent.created_at, AutopilotEvent.id)) if e.id not in self.seen_events]
            chat = [m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == self.w.vid)
                                         .order_by(ChatMessage.created_at, ChatMessage.id)) if m.id not in self.seen_chat]
            needs = [r.title for r in s.scalars(select(NeedsYouRequest).where(
                NeedsYouRequest.venture_id == self.w.vid, NeedsYouRequest.status == "open"))]
        self.seen_events |= {e.id for e in ev}
        self.seen_chat |= {m.id for m in chat}
        return ev, chat, needs

    seen_failures: set[str] = set()

    def _failures(self) -> str:
        """Exceptions the tick swallowed into a held outcome (autopilot/diagnostics.py)."""
        import glob

        out = []
        for p in glob.glob(os.environ["COMPANY_OS_DATA_DIR"] + "/autopilot-private/*/*.json"):
            if p not in self.seen_failures:
                self.seen_failures.add(p)
                out.append(open(p).read())
        return " | ".join(out)

    async def tick(self, note: str = ""):
        from app.autopilot import service
        from app.core.jobs import lease
        from app.state import db
        from app.ventures.next_step import next_step
        from app.worker import handlers
        from app.worker.main import _run_one
        from tests.integration.godmode_world import Trace

        self.n += 1
        before = len(self.seen_events)
        await handlers._autopilot_tick({})
        while True:
            with db()() as session:
                jobs = lease(session, "godmode-sim", kinds=["autopilot.tick", "gtm.gmail.read", "gtm.outreach.retry"], n=100)
                session.commit()
            if not jobs:
                break
            for job in jobs:
                await _run_one(db(), job, {(job.id, job.attempts)})
        note = note or self._failures()
        ev, chat, needs = self._new()
        t = Trace(self.n, self.w.clock.now, service.status(self.w.vid)["state"], next_step(self.w.vid)["key"],
                  [(e.action, e.outcome, e.what) for e in ev],
                  [(m.kind, m.body_md.strip().splitlines()[0][:110] if m.body_md.strip() else "") for m in chat], needs, note)
        self.trace.append(t)
        del before
        return t

    async def run(self, until, *, step_minutes: int, founder=None, max_ticks: int = 4000):
        for _ in range(max_ticks):
            if until():
                return True
            self.w.clock.advance(minutes=step_minutes)
            self.w.settle()
            if founder:
                await founder(self)
            await self.tick()
        return False

    def dump(self, path: str) -> None:
        with open(path, "w") as f:
            for t in self.trace:
                if not (t.events or t.chat or t.note) and t.n % 24:
                    continue
                f.write(f"#{t.n} {t.at:%d %H:%M} [{t.status}] next={t.next_key} needs={t.needs} {t.note}\n")
                for e in t.events:
                    f.write(f"    event {e[0]:<22} {e[1]:<9} {e[2][:100]}\n")
                for c in t.chat:
                    f.write(f"    chat  {c[0]:<14} {c[1]}\n")


def _founder(world):
    """The scripted human. Says little, and only the things a person does: answers once, reviews a build, approves a batch."""
    state = {"answered": False, "review_seen": None, "asked_before_review": [], "approved": set(), "replied": set(),
             "allow_send_on": False}

    async def act(r: Runner):
        from app.autopilot import guard, outreach, service
        from app.autopilot.authority import founder_scope
        from app.db.models import ChatMessage, GtmContact
        from app.state import db

        now = world.clock.now
        if not state["answered"] and now >= START + timedelta(minutes=3):  # he answers Q1 himself, before the 5 minute silence
            state["answered"] = True
            world.idea_post_message(world._ids["sid"], "Independent gym owners with 100-400 members.")
        review = [b for b in world.state["builds"] if b["state"] == "review"]
        if review and state["review_seen"] is None:
            state["review_seen"] = now
        if review and state["review_seen"] and now >= state["review_seen"] + timedelta(hours=3):
            state["review_seen"] = None  # one review per build
            from app.db.supymem_models import NeedsYouRequest

            with db()() as s:
                state["asked_before_review"].append(bool(s.scalar(select(NeedsYouRequest.id).where(
                    NeedsYouRequest.venture_id == world.vid, NeedsYouRequest.title.like("Review your%build%"),
                    NeedsYouRequest.status == "open"))))
            world.founder_review("accept")
        # approve each waiting outreach batch card two hours after it was posted (Allow sending stays off for days)
        with db()() as s:
            cards = [m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == world.vid, ChatMessage.kind == "needs_you"))
                     if "outreach.send" in (m.actions_json or "")]
        for m in cards:
            batch = json.loads(m.actions_json)[0]["confirmation"]["resulting_state"]["batch_id"] if "confirmation" in m.actions_json else None
            if batch and batch not in state["approved"] and now >= m.created_at.replace(tzinfo=UTC) + timedelta(hours=2):
                state["approved"].add(batch)
                with founder_scope(world.vid, world.owner):
                    await outreach.send_batch(world.vid, {"batch_id": batch, "approved_by_founder": True}, actor_subject=world.owner)
        # customers answer some of the emails after a day: 1 in 4 positive, 1 in 10 asks to stop
        for sent in list(world.sent):
            to = sent["to"]
            if to in state["replied"] or now < sent["at"] + timedelta(hours=26):
                continue
            state["replied"].add(to)
            k = sum(map(ord, to)) % 20
            if k < 5:
                world.customer_replies(to, "Yes, interested - can we talk Thursday?")
            elif k < 7:
                world.customer_replies(to, "No thanks, please remove me from your list.")
        if now >= START + timedelta(days=4) and not state["allow_send_on"]:
            state["allow_send_on"] = True
            service.update_settings(world.vid, allow_send=True, send_max_per_day=10)
        del guard, GtmContact

    async def owned_act(r: Runner):
        from app.autopilot.authority import founder_scope
        with founder_scope(world.vid, world.owner):
            return await act(r)

    return owned_act, state


def test_new_venture_idea_to_pmf_iterations(sim, tmp_path):
    from app.autopilot import guard, pmf, service
    from app.db.models import ChatMessage, GtmTouch, VentureDocument
    from app.db.supymem_models import AutopilotEvent
    from app.state import db

    w = sim
    service.update_settings(w.vid, god_mode=True, daily_cap_credits=200)
    r = Runner(w)
    founder, fstate = _founder(w)

    async def scenario():
        # phase 1: idea -> published page + waitlist, every five minutes
        ok = await r.run(lambda: w.state["site_live"] and w.state["waitlist"], step_minutes=5, founder=founder, max_ticks=500)
        if not ok:
            r.dump(os.environ.get("GODMODE_SIM_TRACE") or str(tmp_path / "trace.txt"))
        assert ok, f"venture never reached a published page with a waitlist (state {w.state})"
        # phase 2: GTM and PMF for ~10 days: ten-minute ticks for the first day, hourly after that
        start2 = w.clock.now
        await r.run(lambda: w.clock.now >= start2 + timedelta(days=1), step_minutes=10, founder=founder, max_ticks=300)
        await r.run(lambda: w.clock.now >= start2 + timedelta(days=10), step_minutes=60, founder=founder, max_ticks=300)

    asyncio.run(scenario())
    out = os.environ.get("GODMODE_SIM_TRACE") or str(tmp_path / "trace.txt")
    r.dump(out)
    print("JOURNAL", [(i["n"], i["variable"], i["state"], i.get("decision"), i["baseline"], i["measured"], i["target"],
                       i.get("waiting_send"), i.get("waiting_publish")) for i in pmf.journal(w.vid)])
    print("BUILDS", w.state["builds"], "SIGNUPS", w.state["signups"], "PUBLISHED", w.state["published"])

    # ---------------- floors
    assert w.review_called_by_godmode == 0
    assert fstate["asked_before_review"] and all(fstate["asked_before_review"]), "God mode never told the founder a build was waiting for their review"
    with db()() as s:
        events = list(s.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == w.vid)))
        chat = list(s.scalars(select(ChatMessage).where(ChatMessage.venture_id == w.vid, ChatMessage.role == "assistant")))
    assert not [e for e in events if e.action == "build.accept"], "God mode accepted a build"
    days = (w.clock.now - START).days or 1
    per_day_chat = defaultdict(int)
    for m in chat:
        per_day_chat[m.created_at.date()] += 1
    assert max(per_day_chat.values()) <= 14, f"chat spam: {dict(per_day_chat)}"
    # nothing leaves before a person consented: every send happened after a founder-approved batch or Allow sending
    assert all(s["at"] >= START for s in w.sent)
    assert len({s["to"].lower() for s in w.sent if s["thread"] is None}) == len([s for s in w.sent if s["thread"] is None]), "emailed one person twice"
    sends_by_day = Counter(s["at"].date() for s in w.sent)
    assert max(sends_by_day.values(), default=0) <= 20, sends_by_day
    assert all("guarantee" not in s["body"].lower() and "200 gyms" not in s["body"] for s in w.sent), "an unverified claim was sent"
    assert not any("opted" in s["to"] for s in w.sent)
    assert not [e for e in events if e.action == "cap"]
    # ---------------- the cofounder voice: plain words, one thing to do, no bookkeeping
    from app.autopilot import brief as god_brief
    b = god_brief.brief(w.vid, service.timeline(w.vid, 200))
    assert b["now"] and len(b["waiting_on_you"]) <= 1 and b["learned"], b
    banned = ("Attempting", "attempt", "pmf.iterate", "wait:", "Guarded", "admission")
    assert not [r for r in b["recent"] if any(x in r["text"] for x in banned)], b["recent"]
    summaries = [m.body_md for m in chat if m.kind == "autopilot_summary"]
    assert summaries and all("**Did:**" in m and len(m) < 1400 for m in summaries), summaries[:1]
    print("\nTRACE:", out, "ticks:", r.n, "days:", days, "sends:", len(w.sent))
    del guard, pmf, GtmTouch, VentureDocument


def test_suggestion_cards_are_executable_and_tapping_one_really_runs_it(sim):
    """God OFF: a completing research card admits bounded work and reconciles its saved result."""
    from app.autopilot.authority import founder_scope
    from app.credits.units import MICRO_PER_CREDIT
    from app.db.models import Job
    from app.db.supymem_models import AutopilotEvent, Suggestion
    from app.db.supymem_models import ResearchDossier
    from app.research.execution import identifier
    from app.state import db
    from app.suggestions import proposals, service as sug

    w = sim
    async def scenario():
        with founder_scope(w.vid, w.owner):
            w.idea_confirm(w.vid, w.owner)
            await owned_founder()

    async def owned_founder():
        sug.generate(w.vid)
        with db()() as s:
            cards = list(s.scalars(select(Suggestion).where(Suggestion.venture_id == w.vid, Suggestion.status == "new")))
            prior = set(s.scalars(select(AutopilotEvent.id).where(AutopilotEvent.venture_id == w.vid)))
            prior_jobs = set(s.scalars(select(Job.id).where(Job.venture_id == w.vid)))
        assert cards
        assert all(c.action_kind in proposals.EXECUTABLE_KINDS and c.action_kind not in proposals.BLOCKED_KINDS for c in cards)
        card = next(c for c in cards if c.action_kind == "research.start")
        assert card.title == proposals.CAPABILITIES[card.action_kind]["outcome"]
        out = await sug.accept(card.id, w.owner)
        assert out["status"] == "accepted" and out["result_state"] == "running", out
        result = json.loads(out["result_ref"])
        assert result["outcome"] == "running" and result["href"] == f"/v/{w.vid}/research"
        assert len(result["jobs"]) == 1
        job_id = result["jobs"][0]
        with db()() as s:
            events = [event for event in s.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == w.vid)) if event.id not in prior]
            assert sum(event.credits_micro for event in events if event.outcome == "attempt") <= card.est_credits * MICRO_PER_CREDIT
            assert any(event.action == card.action_kind and event.outcome == "queued" for event in events)
            assert not any(event.action in proposals.BLOCKED_KINDS for event in events)
            saved = s.get(Suggestion, card.id)
            assert (saved.status, saved.result_ref) == (out["result_state"], out["result_ref"])
            job = s.get(Job, job_id)
            assert job.id not in prior_jobs and job.kind == "research.deep"
            assert (job.venture_id, job.workspace_id) == (w.vid, w.wid)
            assert job.status in {"pending", "scheduled", "running", "leased"}
        assert [name for _, name in w.calls] == ["research.start"]
        w.clock.advance(minutes=30)
        w.settle()
        history = sug.list_for({w.wid}, statuses=("done",), venture_id=w.vid)["items"]
        completed = next(item for item in history if item["id"] == card.id)
        receipt = json.loads(completed["result_ref"])
        assert receipt["outcome"] == "done" and receipt["jobs"] == [job_id]
        assert receipt["dossier_id"] == identifier(job_id)
        with db()() as s:
            assert s.get(Job, job_id).status == "succeeded"
            dossier = s.get(ResearchDossier, receipt["dossier_id"])
            assert dossier.state == "complete" and dossier.summary
            assert (dossier.venture_id, dossier.workspace_id) == (w.vid, w.wid)
        assert w.state["dossier"] == "complete" and not w.state["site_live"] and not w.sent

    asyncio.run(scenario())


@pytest.mark.parametrize("available_credits", [300, 299])
def test_a_quote_above_the_daily_cap_is_asked_once_and_never_spams_or_loops(sim, tmp_path, available_credits):
    """An above-old-cap build runs once if funded; only insufficient credits prevent dispatch."""
    from app.autopilot import service
    from app.chat import execute
    from app.credits.accounts import ensure_account, get_account_by_id
    from app.credits.errors import InsufficientCredits
    from app.credits.gate import charge_sync
    from app.credits.units import MICRO_PER_CREDIT
    from app.db.models import ChatMessage, CreditAccount
    from app.db.supymem_models import AutopilotEvent, NeedsYouRequest
    from app.state import db

    w = sim
    w.build_quote_credits = 300
    service.update_settings(w.vid, god_mode=True, daily_cap_credits=200)
    account = ensure_account(w.vid)
    with db()() as session:
        session.get(CreditAccount, account.id).granted_micro = available_credits * MICRO_PER_CREDIT
        session.commit()
    denied = []

    async def metered(venture_id, name, *, payload=None, actor_subject=None):
        if name != "build.start":
            return await w.execute(venture_id, name, payload=payload, actor_subject=actor_subject)
        try:
            with charge_sync(account_id=account.id, purpose="build", estimate_micro=payload["ceiling_micro"]) as meter:
                reply = await w.execute(venture_id, name, payload=payload, actor_subject=actor_subject)
                meter.record_micro(payload["ceiling_micro"])
                return reply
        except InsufficientCredits as exc:
            denied.append(exc.code)
            return {"kind": "needs_you", "body_md": "Insufficient credits. Add credits to continue.", "actions": []}

    w.mp.setattr(execute, "execute_action", metered)
    r = Runner(w)
    # docs/company-os/99_CREDITS_AND_ADMIN_CONTROL.md §4.2 admits available >= amount. Leave the first build unreviewed:
    # accepting it starts PMF builds whose separate 300-credit holds correctly fail after spending the balance.

    async def scenario():
        await r.run(lambda: any(t.next_key == "build.prepare" or w.calls and any(c[1] == "build.prepare" for c in w.calls)
                                for t in r.trace[-1:]) and w.clock.now >= START + timedelta(hours=6), step_minutes=5,
                    max_ticks=150)

    asyncio.run(scenario())
    with db()() as s:
        cap_events = [e for e in s.scalars(select(AutopilotEvent).where(AutopilotEvent.venture_id == w.vid, AutopilotEvent.action == "cap"))]
        said = [m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == w.vid)) if "cap" in (m.body_md or "").lower()]
        asks = [a for a in s.scalars(select(NeedsYouRequest).where(NeedsYouRequest.venture_id == w.vid))
                if "daily cap" in (a.what_we_need or "").lower()]
    assert cap_events == [] and said == [] and asks == []
    assert bool(denied) == (available_credits < 300)
    assert all(code == "insufficient_credits" for code in denied)
    assert len(w.state["builds"]) == (1 if available_credits == 300 else 0)
    assert len([call for call in w.calls if call[1] == "build.start"]) == (1 if available_credits == 300 else 0)
    assert w.state["plan"] == "approved", "everything else must carry on"
    after = get_account_by_id(account.id)
    assert after.spent_micro == (300 * MICRO_PER_CREDIT if available_credits == 300 else 0)
    assert after.reserved_micro == 0


def test_god_mode_never_accepts_a_build_and_one_ask_per_review(sim):
    """The overnight race: God mode accepted a build before the founder's feedback saved. Here a build sits finished and
    QA-passed for two simulated days: it stays pending, the founder is asked exactly once, and the venture does not
    start a second build or stall silently."""
    from app.autopilot import service
    from app.db.models import ChatMessage

    w = sim
    service.update_settings(w.vid, god_mode=True, daily_cap_credits=200)
    r = Runner(w)

    async def founder(runner):
        if w.clock.now >= START + timedelta(minutes=3) and not getattr(founder, "answered", False):
            founder.answered = True
            w.idea_post_message(w._ids["sid"], "Independent gym owners.")

    async def scenario():
        await r.run(lambda: any(b["state"] == "review" for b in w.state["builds"]), step_minutes=5, founder=founder, max_ticks=200)
        n_builds = len(w.state["builds"])
        t0 = w.clock.now
        await r.run(lambda: w.clock.now >= t0 + timedelta(days=2), step_minutes=30, founder=founder, max_ticks=200)
        return n_builds

    n_builds = asyncio.run(scenario())
    assert w.review_called_by_godmode == 0 and all(b["state"] == "review" for b in w.state["builds"])
    assert len(w.state["builds"]) == n_builds == 1, "a second build was started while the first awaited review"
    from app.db.supymem_models import NeedsYouRequest
    from app.state import db

    with db()() as s:
        asks = [a for a in s.scalars(select(NeedsYouRequest).where(NeedsYouRequest.venture_id == w.vid)) if "Review your first build" in a.title]
        chat = [m for m in s.scalars(select(ChatMessage).where(ChatMessage.venture_id == w.vid)) if "could not be independently confirmed" in (m.body_md or "")]
    assert len(asks) == 1 and asks[0].status == "open"
    assert not chat, "it nagged about an 'unconfirmed outcome' instead of just waiting for the review"
