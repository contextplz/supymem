"""Cold-mail adapter — wraps existing gtm.supymem.pipeline (no rebuild)."""

from __future__ import annotations

from typing import Any

from app.approvals.floor import requires_human
from app.marketing.schema import proof_shape
from core.clock import utcnow


class GtmOutreachAdapter:
    name = "gtm_outreach"

    def supports(self, kind: str) -> bool:
        return kind in {"enqueue_outreach", "send_email_touch"}

    async def run(self, job: dict[str, Any]) -> dict[str, Any]:
        kind = job["kind"]
        payload = job.get("payload") or {}
        workspace_id = job["workspace_id"]
        if str(job.get("platform") or payload.get("platform") or "").lower() == "linkedin":
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": "linkedin_send_forbidden",
                "error": "linkedin_send_forbidden",
                "result": {},
                "proof": {},
            }

        # Live sends are external_comms — fail closed unless dry-run or approved.
        from app.gtm.supymem import pipeline

        dry = pipeline._dry_run()  # noqa: SLF001 — shared flag with GTM engine
        if kind == "send_email_touch" and not dry:
            floor = requires_human("external_comms")
            if floor is not None and not payload.get("approval_id"):
                reason = (
                    f"NeedsYou: {floor.key} — cold mail to real people requires "
                    "founder approval (or CHANNELS_DRY_RUN=true)."
                )
                return {
                    "ok": False,
                    "status": "blocked",
                    "block_reason": reason,
                    "error": reason,
                    "result": {"floor": floor.key},
                    "proof": {},
                }

        try:
            from app.connections import mailboxes
            if mailboxes.enabled():
                import json
                from app.autopilot import outreach
                from app.db.models import Approval, GtmEmailSendQueue
                from app.state import db
                mailbox_id = payload.get("mailbox_id")
                with db()() as session:
                    if kind == "send_email_touch" and payload.get("approval_id"):
                        approval = session.get(Approval, payload["approval_id"])
                        if approval is None or approval.workspace_id != workspace_id:
                            raise mailboxes.MailboxUnavailable("mailbox_unavailable")
                        target = json.loads(approval.detail_json or "{}").get("target") or {}
                        queued = session.get(GtmEmailSendQueue, target.get("queue_id")) if target.get("queue_id") else None
                        bound_id = (target.get("sender") or {}).get("mailbox_id") or (queued.email_account_id if queued else None)
                        if bound_id is None or mailbox_id is not None and mailbox_id != bound_id:
                            raise mailboxes.MailboxUnavailable("mailbox_review_required")
                        mailbox_id = bound_id
                    sender = mailboxes.resolve_send_mailbox(session, workspace_id, venture_id=job.get("venture_id"), mailbox_id=mailbox_id)
                    mailbox_id = sender["mailbox_id"]
                    session.commit()
            if kind == "enqueue_outreach":
                contact_ids = payload.get("contact_ids") or []
                auto_send = bool(payload.get("auto_send", False))
                if contact_ids:
                    outcome = pipeline.start_outreach(
                        workspace_id,
                        contact_ids=[str(c) for c in contact_ids],
                        auto_send=auto_send,
                        approval_id=str(payload.get("approval_id") or "") or None,
                        **({"mailbox_id": mailbox_id} if mailboxes.enabled() else {}),
                    )
                else:
                    # No explicit contacts — tick existing queue only.
                    outcome = {
                        "enrolled": [],
                        "queued": 0,
                        "note": "no_contact_ids; use send_email_touch to drain queue",
                        "dry_run": dry,
                    }
            else:
                outcome = pipeline.tick_send_queue(
                    workspace_id,
                    limit=int(payload.get("limit") or 3),
                    respect_stop=True,
                    approval_id=str(payload.get("approval_id") or "") or None,
                    **({"mailbox_id": mailbox_id} if mailboxes.enabled() else {}),
                )
        except mailboxes.MailboxUnavailable:
            failure = outreach.mailbox_failure(str(job.get("venture_id") or ""))
            return {"ok": False, "status": "blocked", "block_reason": failure["body_md"],
                    "error": failure["code"], "actions": failure["actions"], "result": {}, "proof": {}}
        except Exception as exc:
            return {
                "ok": False,
                "status": "failed",
                "error": str(exc)[:300],
                "result": {},
                "proof": {},
            }

        blocker = outcome.get("blocker")
        if blocker:
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": str(blocker)[:400],
                "error": str(blocker)[:400],
                "result": outcome,
                "proof": {},
            }
        approval_errors = [
            item.get("reason")
            for item in outcome.get("processed") or []
            if isinstance(item, dict)
            and str(item.get("reason") or "").startswith("external_approval")
        ]
        if approval_errors:
            reason = str(approval_errors[0])
            return {
                "ok": False,
                "status": "blocked",
                "block_reason": reason,
                "error": reason,
                "result": outcome,
                "proof": {},
            }

        proof = proof_shape(
            mode="simulated" if dry else "live",
            timestamp=utcnow().isoformat(),
            extra={
                "kind": kind,
                "processed": len(outcome.get("processed") or outcome.get("sent") or []),
                "dry_run": dry,
            },
        )
        return {
            "ok": True,
            "status": "done",
            "result": outcome,
            "proof": proof,
        }
