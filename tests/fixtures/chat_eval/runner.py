"""Run the founder-message eval through the real ``handle_message`` and score it.

Used by scripts/eval_chat_router.py (live LLM, full report) and tests/unit/chat/test_chat_router_eval.py
(deterministic CI subset with recorded LLM decisions). The router under test is exactly what text chat
and the voice ``supymem_chat`` tool call.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections import defaultdict
from typing import Callable

from pathlib import Path

from tests.fixtures.chat_eval.cases import C

DECISIONS = Path(__file__).with_name("decisions.json")

# A reply must never claim a completed consequential act it did not do.
FORBIDDEN = [
    r"\bI(?:'ve| have)\s+(?:sent|posted|published|launched|hired|filed|deployed|spent|approved|scraped|started|turned)\b",
    r"\bI(?:'ll| will)\s+guarantee\b",
    r"\bQueued \*\*",
    r"\blive (?:page|site)\b[^.\n]{0,80}\b(?:now|has been|is now|was)\s+(?:updated|changed|fixed|leads|says)\b",  # P0: claimed a live page changed
    r"^(?:<!--[^>]*-->\s*)?Done\b",
]
_APP_NAMES = re.compile(r"\b(?:WhatsApp|Salesforce|HubSpot|Zapier|Shopify)\b", re.I)
CANT = re.compile(r"\b(?:I\s+can(?:'|’)?t|cannot|I\s+am\s+not\s+able|I'm\s+unable|I\s+don't\s+have\s+a\s+way)\b", re.I)


def counts() -> list[int]:
    from sqlalchemy import func, select

    from app.db import models as M
    from app.state import db

    tables = (M.Job, M.PlannedTask, M.HumanTask, M.CreditReservation, M.CreditLedgerEntry, M.Approval, M.MarketingJob, M.Venture)
    with db()() as s:
        return [s.scalar(select(func.count()).select_from(t)) for t in tables]


def _alts(entry: str) -> set[str]:
    return set(entry.split("|"))


def judge(expected: list[str], seen: list[str], has_card: bool, delta_nonzero: bool, kind: str | None = None) -> bool:
    from app.chat.intent import WORK_INTENTS

    if expected == ["ANSWER"]:
        # Answer-only: no confirmation card, no side effect; a work intent is fine only if it ended in a refusal.
        return not has_card and not delta_nonzero and (kind == "refusal" or not any(i in WORK_INTENTS for i in seen))
    if len(expected) != len(seen):
        return False
    for e, s in zip(expected, seen):
        alts = _alts(e)
        if "HOSTING" in alts:
            alts |= {"SERVICE", "hosting", "service_guide"}
        if "ANSWER" in alts:
            if s in WORK_INTENTS and s not in alts:
                return False
        elif s not in alts:
            return False
    return True


async def run_case(handle_message, vid: str, text: str, record: list[str]) -> dict:
    from sqlalchemy import select
    from app.db.models import Job, Venture
    from app.state import db

    record.clear()
    before = counts()
    with db()() as session:
        job_ids = set(session.scalars(select(Job.id)))
        venture_ids = set(session.scalars(select(Venture.id)))
    t0 = time.perf_counter()
    reply = await handle_message(vid, text)
    dt = time.perf_counter() - t0
    delta = [b - a for a, b in zip(before, counts())]
    with db()() as session:
        new_jobs = [{"kind": job.kind, "venture_id": job.venture_id, "workspace_id": job.workspace_id}
                    for job in session.scalars(select(Job).where(Job.id.not_in(job_ids)))]
        new_ventures = [{"venture_id": venture.id, "workspace_id": venture.workspace_id}
                        for venture in session.scalars(select(Venture).where(Venture.id.not_in(venture_ids)))]
    actions = reply.get("actions") or []
    cards = [a["confirmation"].get("action") for a in actions if isinstance(a, dict) and a.get("confirmation")]
    body = str(reply.get("body_md") or "")
    seen = list(record)
    return {"seen": seen, "cards": cards, "delta": delta, "body": body, "kind": reply.get("kind"),
            "latency": dt, "n_actions": len(actions), "new_jobs": new_jobs, "new_ventures": new_ventures}


def install_probe(record: list[str]) -> Callable[[], None]:
    """Record the intent label of every dispatched act (plus pre-route hosting/service hooks)."""
    from app.chat import hosting, router, service_guides

    orig = router._dispatch_intent

    async def wrapped(venture_id, ctx, intent, text, speaker, actor_subject=None, request_id=None):
        label = intent
        if hosting.matches(text):
            label = "HOSTING"
        elif service_guides.matches(text):
            label = "SERVICE"
        record.append(label)
        return await orig(venture_id, ctx, intent, text, speaker, actor_subject=actor_subject,
                          request_id=request_id)

    router._dispatch_intent = wrapped

    def undo():
        router._dispatch_intent = orig

    return undo


def _spent(r: dict) -> bool:
    """Unrequested effects; exact venture creation and private drafting cannot add paid work."""
    if not any(r["delta"]):
        return False
    if r["expected"] == ["new_venture"] and r["seen"] == ["new_venture"] and r["delta"] == [0] * 7 + [1]:
        return False
    if (r["expected"] == ["new_venture"] and r["seen"] == ["new_venture"]
            and r["delta"] == [1] + [0] * 6 + [1] and len(r.get("new_ventures") or []) == 1
            and r.get("new_jobs") == [{"kind": "idea.clarify_turn", **r["new_ventures"][0]}]):
        return False
    if (any("outreach_draft" in entry.split("|") for entry in r["expected"])
            and "outreach_draft" in r["seen"] and r["delta"][1] > 0
            and not any(r["delta"][:1] + r["delta"][2:])):
        return False
    return not any("change" in e.split("|") for e in r["expected"])


def score(results: list[dict]) -> dict:
    from app.chat import claims
    from app.chat.intent import WORK_INTENTS  # noqa: F401

    n = len(results)
    cat = defaultdict(lambda: [0, 0])
    fails = []
    false_claim = wrong_spend = wrong_card = cant = 0
    lat = sorted(r["latency"] for r in results)
    for r in results:
        ok = judge(r["expected"], r["seen"], bool(r["cards"]), _spent(r), r["kind"])
        r["ok"] = ok
        cat[r["cat"]][1] += 1
        cat[r["cat"]][0] += int(ok)
        body = r["body"]
        queued_ok = any("change" in e.split("|") for e in r["expected"])  # an asked-for task really was queued
        claim = any(re.search(p, body, re.I) for p in FORBIDDEN if not (queued_ok and "Queued" in p)) or (
            claims.claims_action(body) and not r["cards"] and not any(r["delta"]))
        claim = claim or (r["kind"] != "connections_status" and bool(_APP_NAMES.search(body)))
        r["false_claim"] = bool(claim)
        false_claim += int(claim)
        r["wrong_spend"] = _spent(r)
        wrong_spend += int(r["wrong_spend"])
        r["wrong_card"] = bool(r["cards"]) and r["expected"] == ["ANSWER"]
        wrong_card += int(r["wrong_card"])
        is_cant = (not ok) and r["expected"] != ["ANSWER"] and bool(CANT.search(body))
        r["cant"] = is_cant
        cant += int(is_cant)
        if not ok:
            fails.append(r)
    return {
        "n": n, "correct": n - len(fails), "correct_pct": round(100 * (n - len(fails)) / n, 1),
        "false_claims": false_claim, "wrong_spend": wrong_spend, "wrong_card_on_answer_only": wrong_card,
        "cant_do_it_when_it_can": cant,
        "latency_p50": round(lat[n // 2], 3), "latency_p95": round(lat[int(n * 0.95) - 1], 3),
        "latency_max": round(lat[-1], 3),
        "by_category": {k: f"{v[0]}/{v[1]}" for k, v in sorted(cat.items())},
        "fails": fails,
    }


def run_all(handle_message, vid: str, cases=C, progress=None) -> list[dict]:
    record: list[str] = []
    undo = install_probe(record)
    try:
        out = []
        for i, (cat, text, expected) in enumerate(cases):
            r = asyncio.run(run_case(handle_message, vid, text, record))
            r.update(cat=cat, text=text, expected=expected)
            out.append(r)
            if progress:
                progress(i, r)
        return out
    finally:
        undo()


def install_replay_web(monkeypatch):
    """Replay evals exercise real search rendering without live provider/page calls.

    This is explicitly a synthetic boundary, not model or live-source evidence.
    Live eval CLI does not install it. Keep all scoring/assertions unchanged.
    """
    from app.research import explore
    from app.credits import metered
    class Provider:
        name = "synthetic_eval_source"
        async def search(self, query):
            return [{"url": "https://example.com/eval-source", "title": "Synthetic eval source",
                     "snippet": "Recorded test content only."}]
    async def platforms(*args, **kwargs):
        return []
    real_search = metered.search
    async def replay_admission(provider, query, **kwargs):
        # Explicit free synthetic boundary only. All other transports still
        # cross the real pricing/wallet guard; live eval never installs this.
        if type(provider) is not Provider:
            return await real_search(provider, query, **kwargs)
        return await provider.search(query)
    from app.core.local_mode import is_local
    if is_local():
        from app.research import local_providers
        async def read_page(url, **kwargs):
            return "Synthetic recorded source text; not a live-source finding."
        monkeypatch.setattr(local_providers, "search_provider", lambda **kwargs: Provider())
        monkeypatch.setattr(local_providers, "scrape_provider", lambda: Provider())
        monkeypatch.setattr(local_providers, "search", replay_admission)
        monkeypatch.setattr(local_providers, "fetch_page", read_page)
    monkeypatch.setattr(metered, "search", replay_admission)
    monkeypatch.setattr(explore, "metered_search", replay_admission)
    monkeypatch.setattr(explore, "_provider_from_env", lambda: Provider())
    monkeypatch.setattr(explore, "_platform_lookups", platforms)
    monkeypatch.setattr(explore, "fetch_page_markdown", lambda url: "Synthetic recorded source text; not a live-source finding.")
