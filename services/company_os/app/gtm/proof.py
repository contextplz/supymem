"""Release-agent GTM proof, restricted to the founder's own reviewed smoke batch.

Default is a content-free read. --send-reviewed sends only an already reviewed
batch from a dedicated venture owned by the configured operator. It never adds contacts,
accepts policies, changes settings, approves drafts, or exposes message content.
Packaged with Core; run python -m app.gtm.proof in its normal installed runtime.
Explicit sending creates the ordinary metered action-approval records.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
ALLOWED_RECIPIENT = os.environ.get("SUPYMEM_INTERNAL_EMAIL", "").strip().lower()


def reviewed_request(view: dict, batch_id: str) -> dict:
    if not ALLOWED_RECIPIENT:
        raise ValueError("Set SUPYMEM_INTERNAL_EMAIL to your owned proof mailbox and retry.")
    batch = next((b for b in view["batches"] if b["batch_id"] == batch_id), None)
    if not batch or not batch["draft_ids"] or len(batch["draft_ids"]) > batch["cap"]:
        raise ValueError("smoke_batch_missing_or_over_cap")
    drafts = {d["doc_id"]: d for d in view["drafts"]}
    for draft_id in batch["draft_ids"]:
        draft = drafts.get(draft_id)
        if not draft or draft.get("review_state") != "approved" or draft.get("state") != "draft":
            raise ValueError("smoke_exact_review_required")
        if draft.get("to", "").strip().lower() != ALLOWED_RECIPIENT:
            raise ValueError("smoke_recipient_refused")
    if not view.get("can_send"):
        raise ValueError("smoke_send_not_ready")
    return {k: batch[k] for k in ("batch_id", "draft_ids", "approval_digest", "cap")}


def inspect_and_send(venture_id: str, batch_id: str | None, send: bool) -> dict:
    from app.db.models import Person, Venture, Workspace
    from app.gtm import journey
    from app.state import db
    from app.tenancy import bind

    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        owner = session.get(Person, workspace.owner_id) if workspace else None
        if not ALLOWED_RECIPIENT or not owner or (owner.email or "").strip().lower() != ALLOWED_RECIPIENT or venture.archived_at or workspace.archived_at:
            raise ValueError("smoke_owner_refused")
        wid, actor = workspace.id, owner.id
    with bind(workspace_id=wid, venture_id=venture_id, actor=actor):
        view = journey.status(wid, venture_id)
        if send:
            if not batch_id:
                raise ValueError("smoke_batch_id_required")
            request = reviewed_request(view, batch_id)
            view = asyncio.run(journey.send(wid, venture_id, request))
        return {"venture_id": venture_id, "state": view["state"], "phase": view["phase"],
                "can_send": view.get("can_send"), "send_blocked_reason": view.get("send_blocked_reason"),
                "prospect_count": len(view["prospects"]), "draft_count": len(view["drafts"]),
                "results": {k: view["results"][k] for k in ("sent", "replied", "interested")},
                "send_requested": send}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venture-id", required=True)
    parser.add_argument("--batch-id")
    parser.add_argument("--send-reviewed", action="store_true")
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_and_send(args.venture_id, args.batch_id, args.send_reviewed), sort_keys=True))
    except ValueError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("smoke_runtime_failed_review_server_status") from None


if __name__ == "__main__":
    main()
