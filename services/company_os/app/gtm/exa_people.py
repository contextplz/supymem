"""Public profile search through local search or hosted Exa; never read LinkedIn URLs."""
from __future__ import annotations

import hashlib
import os
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

import httpx

from app.core.jobs import ACTIVE_JOB_STATUSES, FatalError, enqueue
from app.credits import metered
from app.credits.errors import CreditsUnavailable
from app.credits.units import usd_to_micro
from app.db.models import Job, Venture
from app.gtm import linkedin_people as people
from app.gtm.god.generation import authority
from app.state import db
from app.tenancy import current_or_none
from core.clock import utcnow
from core.ids import new_id

KIND = "gtm.linkedin.search"
# ponytail: at most 3 Exa attempts per venture per UTC day; add scheduled/cursor discovery if this misses the target.
MAX_CALLS = 3


def _available() -> bool:
    from app.core.local_mode import is_local
    if is_local():
        from app.gtm.discovery import local_provider
        return local_provider() is not None
    return bool(os.environ.get("EXA_API_KEY"))


def queue(vid: str, actor: str | None = None) -> None:
    from app.core.local_mode import is_local
    if not _available():
        return
    with db()() as session:
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.archived_at:
            return
        row, data = people._doc(session, vid, lock=True)
        if row is None or not (data.get("requested_at") or not is_local() and data.get("exa_active")):
            return
        scope = current_or_none()
        actor = actor or (scope.actor if scope and scope.workspace_id == venture.workspace_id else None)
        if not actor:
            return
        order = data.get("order") or {}
        job = session.get(Job, order.get("job_id")) if order.get("source") == "exa" and order.get("job_id") else None
        if job and job.status in ACTIVE_JOB_STATUSES:
            return
        if job and order.get("state") in {"queued", "running"}:
            order.update(state="failed", finished_at=utcnow().isoformat(), message="Exa search interrupted; try again.")
        if order.get("source") != "exa" and order.get("state") == "running" and not people.wants_search(data):
            return
        data["exa_active"] = True
        if data.get("exa_searches", {}).get(people._today(), 0) >= MAX_CALLS:
            data["requested_at"] = None
        elif data.get("requested_at") or people.new_today(data) < people.DAILY_TARGET:
            run = new_id("lis")
            job_id = enqueue(session, KIND, {"order_id": run, **({"_actor_subject": actor} if actor else {})},
                             workspace_id=venture.workspace_id, venture_id=vid,
                             dedupe_key=f"linkedin-exa:{vid}:{run}", max_attempts=1)
            data["order"] = {"id": run, "job_id": job_id, "source": "exa", "state": "queued", "queued_at": utcnow().isoformat()}
            data["requested_at"] = None
            data["needs_human"] = None
        people._save(row, data)
        session.commit()


def _fence(session, payload):
    venture = session.get(Venture, payload.get("venture_id"), with_for_update=True)
    job = session.get(Job, payload.get("_job_id"))
    if (not venture or venture.workspace_id != payload.get("workspace_id") or venture.archived_at or
            not job or job.kind != KIND or job.venture_id != venture.id or job.workspace_id != venture.workspace_id or
            job.status != "leased" or job.cancel_requested or not job.locked_by or job.locked_by != payload.get("_worker_id") or
            job.attempts != payload.get("_job_attempt")):
        raise FatalError("linkedin_exa_scope_or_lease_invalid")
    authority(venture.workspace_id, venture.id, actor=payload.get("_actor_subject"))
    row, data = people._doc(session, venture.id, lock=True)
    order = data.get("order") or {}
    if row is None or (order.get("id"), order.get("job_id"), order.get("source")) != (payload.get("order_id"), job.id, "exa"):
        raise FatalError("linkedin_exa_superseded")
    return row, data


async def _http(query: str) -> dict:
    from app.core.local_mode import is_local
    if is_local():
        from app.research.local_providers import local_search
        return {"results": [row | {"text": row.get("text") or row.get("content") or row.get("snippet") or ""}
            for row in await local_search("site:linkedin.com/in/ " + query, limit=25)]}
    async with httpx.AsyncClient(timeout=25.0) as client:
        response = await client.post("https://api.exa.ai/search", headers={"x-api-key": os.environ["EXA_API_KEY"]},
                                     json={"query": query, "category": "linkedin profile", "numResults": 25,
                                           "contents": {"text": {"maxCharacters": 600}}})
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            exc.status_code = response.status_code
            raise
        body = response.json()
    if not isinstance(body, dict) or not isinstance(body.get("results"), list):
        raise ValueError("Exa returned invalid results")
    return body


def _cost(body: dict) -> int:
    cost = body.get("costDollars")
    if cost is None:
        return metered.price_micro("exa_search")
    try:
        amount = Decimal(str(cost["total"] if isinstance(cost, dict) else cost))
        if not amount.is_finite() or amount < 0:
            raise ValueError
        return usd_to_micro(amount)
    except (InvalidOperation, TypeError, ValueError, KeyError) as exc:
        raise CreditsUnavailable("Exa cost needs reconciliation") from exc


def _store(payload: dict, body: dict) -> None:
    with db()() as session:
        row, data = _fence(session, payload)
        known = {people.profile_url(person.get("url", "")) for person in data["people"] + (data.get("backlog") or [])}
        known = {url.casefold() for url in known if url}
        now = utcnow()
        added = []
        for result in body["results"][:25]:
            if not isinstance(result, dict) or not isinstance(result.get("url"), str):
                continue
            try:
                parts = urlsplit(result["url"])
                if not re.fullmatch(r"(?:linkedin\.com|(?:www|[a-z]{2})\.linkedin\.com)", parts.hostname or "", re.I):
                    continue
                if parts.scheme not in {"http", "https"} or not parts.path.startswith("/in/"):
                    continue
                path = re.sub(r"/details/[^/]+/?$", "", parts.path)
                url = people.profile_url(parts._replace(path=path).geturl())
            except ValueError:
                continue
            if not url or url.casefold() in known:
                continue
            known.add(url.casefold())
            title = result.get("title") if isinstance(result.get("title"), str) else ""
            text = result.get("text") if isinstance(result.get("text"), str) else ""
            added.append(people.clean_person({"id": "lip_" + hashlib.sha256(url.casefold().encode()).hexdigest()[:12], "url": url,
                          "name": result.get("author"), "company": result.get("company"), "location": result.get("location"),
                          "title": title[:300], "snippet": text[:600], "query": data["order"]["query"],
                          "source": "exa", "found_on": now.date().isoformat(), "found_at": now.isoformat(),
                          "sent_at": None, "replied_at": None}))
        data["people"] += added
        data["order"].update(state="done", finished_at=now.isoformat(), found=len(added), costDollars=body.get("costDollars"), message=None)
        people._save(row, data)
        session.commit()
    people.log.info("linkedin_exa_results", extra={"venture_id": payload["venture_id"], "found": len(added), "costDollars": body.get("costDollars")})


async def run(payload: dict) -> None:
    with db()() as session:
        row, data = _fence(session, payload)
        if data["order"]["state"] != "queued":
            return
        today = people._today()
        count = data.setdefault("exa_searches", {}).get(today, 0)
        if not _available() or count >= MAX_CALLS:
            data["order"].update(state="failed", finished_at=utcnow().isoformat(), message="Open Keys and connections > Search to enable your search, or retry tomorrow if you reached your daily search limit.")
            if not _available():
                data["requested_at"] = utcnow().isoformat()
            people._save(row, data)
            session.commit()
            return
        data["exa_searches"][today] = count + 1
        data["order"].update(state="running", started_at=utcnow().isoformat())
        people._save(row, data)
        session.commit()
    try:
        icp, idea = people._venture_icp(payload["venture_id"])
        queries = [query.replace("site:linkedin.com/in", "").replace('"', "").strip() for query in people.build_queries(icp, idea)]
        if not queries:
            raise ValueError("linkedin_icp_missing")
        queries += [queries[0] + " founder", queries[0] + " manager"]
        query = queries[count % len(queries)]
        with db()() as session:
            row, data = _fence(session, payload)
            data["order"]["query"] = query
            people._save(row, data)
            session.commit()
        from app.core.local_mode import is_local
        if is_local():
            _store(payload, await _http(query))
        else:
            await metered.call(lambda: _http(query), purpose="research.search", provider="exa",
                               workspace_id=payload["workspace_id"], venture_id=payload["venture_id"],
                               request_id=payload["_job_id"], estimate_micro=metered.price_micro("exa_search", 3),
                               actual_micro=_cost, before_meter_exit=lambda body, receipt: _store(payload, body))
    except Exception as exc:
        with db()() as session:
            row, data = _fence(session, payload)
            if data["order"]["state"] != "done":
                data["order"].update(state="failed", finished_at=utcnow().isoformat(), message="Retry your search with a broader customer role or location. If it still fails, check your search provider in Keys and connections > Search.")
                people._save(row, data)
                session.commit()
        people.log.warning("linkedin_exa_failed", extra={"venture_id": payload["venture_id"], "error_class": type(exc).__name__})
        raise
