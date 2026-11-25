"""Hermes adapters — research + browser operator (OpenClaw/Playwright).

``vendor/hermes-agent`` = Nous Hermes web_search/web_extract contract.
``vendor/openclaw`` = PC/browser operator reference; we drive Playwright here
for verification. Publishing LinkedIn images stays on Composio/API.
"""

from __future__ import annotations

from typing import Any

from core.clock import utcnow

from app.marketing.adapters import openclaw_bridge
from app.marketing.schema import proof_shape
from app.marketing.verify import is_block_error


class HermesResearchAdapter:
    """Hermes-shaped research via existing explore_web backends."""

    name = "hermes_research"

    def supports(self, kind: str) -> bool:
        return kind in {"research_topic", "find_threads"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        payload = job.get("payload") or {}
        themes = payload.get("themes") or []
        query = str(themes[0] if themes else payload.get("query") or "AI agent go-to-market")
        notes: list[dict[str, Any]] = []
        try:
            from app.research.explore import explore_web

            result = await explore_web(query=query, max_results=5, extract=3)
            if isinstance(result, dict):
                notes.append({"query": query, "explore": result})
            else:
                notes.append({"query": query, "explore": str(result)[:500]})
        except Exception as exc:
            notes.append(
                {
                    "query": query,
                    "status": "explore_unavailable",
                    "error": str(exc)[:200],
                    "intents": [str(t) for t in themes[:8]],
                }
            )
        proof = proof_shape(
            mode="simulated",
            timestamp=utcnow().isoformat(),
            extra={"kind": job["kind"], "executor": self.name, "note_count": len(notes)},
        )
        return {
            "ok": True,
            "status": "done",
            "result": {"notes": notes, "query": query},
            "proof": proof,
        }


class HermesBrowserAdapter:
    """Browser operator: verify a published URL via OpenClaw/Playwright.

    For ``publish_content`` with ``force_hermes``, we do **not** invent LinkedIn
    clicks — we refuse and point at Composio. For ``check_result``, we open the
    live URL and screenshot it.
    """

    name = "hermes_browser"

    def supports(self, kind: str) -> bool:
        return kind in {"publish_content", "check_result"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        payload = job.get("payload") or {}

        if kind == "publish_content":
            reason = (
                "hermes_browser does not publish LinkedIn (ToS + detection). "
                "Use Composio/API (content_engine). Set force_hermes only for "
                "check_result verification after API publish."
            )
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": reason,
                "error": reason,
                "result": {"needs_you": False, "use": "composio"},
                "proof": {},
            }

        url = str(payload.get("external_url") or payload.get("url") or "")
        post_id = str(payload.get("content_post_id") or "")
        if not url and post_id:
            from app.db.models import ContentPost
            from app.state import db

            factory = db()
            with factory() as session:
                row = session.get(ContentPost, post_id)
                if row is not None:
                    url = row.external_url or ""
                    if not url and row.external_id:
                        urn = row.external_id
                        if not urn.startswith("urn:"):
                            urn = f"urn:li:share:{urn}"
                        url = f"https://www.linkedin.com/feed/update/{urn}/"
                    # Simulated publishes never open a browser.
                    if row.dry_run or (row.external_id or "").startswith("sim_"):
                        proof = proof_shape(
                            external_id=row.external_id,
                            external_url=row.external_url,
                            account_id=row.account_id,
                            mode="simulated",
                            timestamp=utcnow().isoformat(),
                            content_post_id=post_id,
                            visual_path=row.visual_path,
                            extra={
                                "executor": "hermes_browser",
                                "skipped": "dry_run",
                            },
                        )
                        return {
                            "ok": True,
                            "status": "done",
                            "result": {
                                "skipped": "dry_run",
                                "content_post_id": post_id,
                            },
                            "proof": proof,
                        }

        if url.startswith("dryrun://") or (payload.get("external_id") or "").startswith(
            "sim_"
        ):
            return {
                "ok": True,
                "status": "done",
                "result": {"skipped": "dry_run"},
                "proof": proof_shape(
                    external_id=payload.get("external_id"),
                    external_url=url,
                    mode="simulated",
                    timestamp=utcnow().isoformat(),
                    content_post_id=post_id or None,
                    extra={"executor": "hermes_browser", "skipped": "dry_run"},
                ),
            }

        return await openclaw_bridge.verify_public_url_async(
            url,
            workspace_id=job["workspace_id"],
            post_id=post_id or None,
        )


class HermesDesktopAdapter:
    """Desktop app control — NeedsYou until OpenClaw computer-use node is paired."""

    name = "hermes_desktop"

    def supports(self, kind: str) -> bool:
        return False

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        reason = (
            "hermes_desktop_not_enabled: pair OpenClaw computer-use (Peekaboo) "
            "for GUI editors. LinkedIn cards use visual_renderer + Composio."
        )
        _ = is_block_error(reason)
        return {
            "ok": False,
            "status": "blocked",
            "block_reason": reason,
            "error": reason,
            "result": {"needs_you": True, "vendor": "vendor/openclaw"},
            "proof": {},
        }
