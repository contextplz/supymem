"""Bounded entry interpretation and durable application of its reviewed decision."""

from __future__ import annotations

import asyncio
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit


def page_read_block(text: str) -> str | None:
    from app.chat.execute import local_capability_reply
    from app.chat.link_context import read_prohibited
    from app.core.local_mode import is_sample_mode

    paste = "paste the relevant page text into Add Venture to continue."
    if read_prohibited(text):
        return "You asked me not to open links, so " + paste
    if is_sample_mode():
        return "Sample mode does not open websites, so " + paste
    if local_capability_reply("research"):
        return "Research is off or needs setup, so " + paste
    return None


async def local_intake_completion(messages: list, *, workspace_id: str, actor_id: str, **kwargs):
    from contextlib import nullcontext

    from app.credits import metered
    from app.tenancy import bind, current_or_none

    scope = current_or_none()
    entry_scope = (bind(workspace_id=workspace_id, venture_id="", actor=actor_id)
                   if scope is None or scope.workspace_id in {"", "none"} else nullcontext())
    with entry_scope:
        return await metered.llm(messages, workspace_id=workspace_id, **kwargs)


def _page(url: str, *, linked_from: str | None = None, deadline: float | None = None) -> dict:
    from app.chat import link_context
    from app.intake.smart import _canonical_url

    canonical = _canonical_url(url)
    result = {"url": url, "status": "failed", "content": "",
              "is_repo": bool(canonical and urlsplit(canonical).hostname == "github.com"), "linked_from": linked_from}
    if not canonical:
        return result
    result["url"] = canonical
    if not link_context._safe_host(urlsplit(canonical).hostname or ""):
        return result
    try:
        if not result["is_repo"]:
            from app.intake.crawl import crawl_site
            pages = crawl_site(canonical, deadline=deadline or time.monotonic() + 3.0)
            if pages:
                answers = {}
                for page in pages:
                    for field, value in page.get("website_answers", {}).items():
                        answers.setdefault(field, value)
                result.update(status="ok", content="\n".join(page["text"][:6000] for page in pages)[:30_000],
                              venture_name=pages[0].get("venture_name", ""), website_answers=answers)
            return result
        content = (link_context._fetch(canonical, 3.0) or "").strip()[:30_000]
    except Exception:
        return result
    if content:
        private = bool(re.search(r"(?:sign in|log in|login)\s+to\s+(?:continue|access|view)", content, re.I))
        result.update(status="private" if private and len(content) < 500 else "ok", content=content)
    return result


async def read_page_context(text: str, *, budget_seconds: float = 4.0) -> dict:
    """DNS and the existing guarded reader run off the event loop under one deadline."""
    from app.intake.smart import _URL, _canonical_url, _owned_urls

    candidates = [m.group().rstrip(".,;:!?") for m in _URL.finditer(text)]
    urls = list(dict.fromkeys(_canonical_url(url) or url for url in _owned_urls(text) + candidates))[:2]
    if not urls:
        return {"pages": []}
    pages = [{"url": url, "status": "failed", "content": ""} for url in urls]
    pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="intake-links")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + budget_seconds
    crawl_deadline = time.monotonic() + budget_seconds
    try:
        pending = [loop.run_in_executor(pool, lambda target=url: _page(target, deadline=crawl_deadline)) for url in urls]
        done, _ = await asyncio.wait(pending, timeout=max(0.0, deadline - loop.time()))
        for index, task in enumerate(pending):
            if task in done:
                try:
                    pages[index] = task.result()
                except Exception:
                    pass
        # Only an explicitly labelled product link from a readable README is
        # considered; badges and documentation never become a founder's site.
        repo = next((p for p in pages if p.get("is_repo") and p["status"] == "ok"), None)
        if repo and len(pages) == 1 and deadline - loop.time() > 0.1:
            match = re.search(r"(?:live\s+(?:product|site|demo)|website|homepage|product|try\s+it)\s*(?:[:\-]\s*|\]\s*\(\s*)(https?://[^\s<>\)\]]+)", repo["content"], re.I)
            if match:
                future = loop.run_in_executor(pool, lambda: _page(match.group(1).rstrip(".,;"), linked_from=repo["url"], deadline=crawl_deadline))
                try:
                    pages.append(await asyncio.wait_for(future, max(0.01, deadline - loop.time())))
                except Exception:
                    pass
    finally:
        # A stalled DNS/reader never extends the HTTP request's deadline.
        pool.shutdown(wait=False, cancel_futures=True)
    return {"pages": pages}


# Leave four seconds for admission, persistence and the HTTP reply after interpretation.
INTAKE_BUDGET_SECONDS = 16.0


async def interpret(text: str, *, actor_id: str, workspace_id: str | None = None,
                    budget_seconds: float = INTAKE_BUDGET_SECONDS, original_input: str | None = None,
                    request_id: str | None = None, previous_intake: dict | None = None) -> dict:
    from app.intake.smart import _URL, _canonical_url, _owned_urls, classify, classify_fallback
    from app.intake.website import changing_idea

    started = time.monotonic()
    supplied = text + ("\n" + original_input if original_input else "")
    blocked = page_read_block(text) if _URL.search(supplied) else None
    context = {"pages": []} if blocked else await read_page_context(supplied, budget_seconds=min(4.0, budget_seconds / 2))
    if previous_intake and not blocked and not changing_idea(text):
        extracted = previous_intake.get("extracted", {})
        site = previous_intake["website_source"]
        answers = previous_intake.get("website_answers", {})
        if site and answers and _owned_urls(text if _URL.search(text) else supplied) == [site]:
            for index, page in enumerate(context["pages"]):
                if _canonical_url(page["url"]) == site and page["status"] == "failed":
                    context["pages"][index] = {"url": site, "status": "ok", "content": "\n".join(answers.values()),
                        "website_answers": answers, "venture_name": extracted.get("venture_name", "")}
    if original_input:
        context["original_input"] = original_input
    remaining = max(0.01, budget_seconds - (time.monotonic() - started))
    try:
        decision = await asyncio.wait_for(classify(text, actor_id=actor_id, workspace_id=workspace_id,
            page_context=context, budget_seconds=remaining, request_id=request_id), remaining)
    except Exception as exc:
        from llm_gateway.errors import ProviderCallError
        from app.intake.smart import recover_original_json
        if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
            raise
        if request_id:
            recovered = recover_original_json(actor_id=actor_id, workspace_id=workspace_id,
                purpose="intake.classify", request_id=request_id)
            if recovered is not None and recovered["output"] is not None:
                return recovered["output"]
        decision = classify_fallback(text, page_context=context)
    result = decision.to_dict()
    if blocked and result["path"] != "refuse":
        result["founder_line"] = blocked
    return result


def apply(venture_id: str, workspace_id: str, *, original: str, decision: dict,
          proposal_id: str, new_venture: bool, assets: dict | None = None) -> None:
    """Save original input and observed assets without asserting publication/QA."""
    from app.chat.recovery import _save_document, clean_url
    from app.db.models import Venture
    from app.intake.smart import IntakeResult
    from app.state import db
    from app.workspaces.service import merge_settings

    validated = IntakeResult.model_validate(decision).to_dict()
    url = clean_url(validated["extracted"].get("existing_site_url"))
    from app.db.models import VentureDocument
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None or venture.workspace_id != workspace_id:
            raise RuntimeError("intake_scope_mismatch")
        old_site = session.get(VentureDocument, f"external_site:{venture_id}")
        preserve_site = bool(old_site and old_site.venture_id == venture_id and old_site.path == url)
        old_assets = session.get(VentureDocument, f"founder_assets:{venture_id}")
        try:
            previous_assets = json.loads(old_assets.body_md) if old_assets and old_assets.venture_id == venture_id else {}
        except ValueError:
            previous_assets = {}
        if assets is not None:
            assets = {**(previous_assets if isinstance(previous_assets, dict) else {}),
                      **{key: value for key, value in assets.items() if isinstance(value, str) and value.strip()}}
    settings = {"intake": validated, "intake_path": validated["path"], "intake_venture_id": venture_id,
                "project_intent": "personal" if validated["path"] == "personal_build" else "startup"}
    merge_settings(workspace_id, settings)
    if new_venture:
        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture is None or venture.workspace_id != workspace_id:
                raise RuntimeError("intake_scope_mismatch")
            venture.raw_idea = original
            session.commit()
    _save_document(venture_id, "intake_source", "Original intake", original, "intake",
                   doc_id=f"intake_source:{proposal_id}")
    _save_document(venture_id, "intake_decision", "Saved intake decision", json.dumps(validated), "intake",
                   unique=False)
    if assets:
        _save_document(venture_id, "founder_assets", "What you already have", json.dumps(assets), "intake", unique=False)
    if url and not preserve_site:
        _save_document(venture_id, "external_site", "Your existing website", "Saved from your intake.",
                       "intake", url=url, unique=False)
    from app.ventures.stage import seed_stage
    seed_stage(venture_id, validated)
