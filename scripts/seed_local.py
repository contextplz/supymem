#!/usr/bin/env python3
"""Idempotent local demo tenant: one person, one workspace, one venture, credits, two tasks.

Reuse the same service functions the HTTP API uses. Do not raw-INSERT and do not
bypass the credit ledger. Safe to run twice.

Prints the venture id and console URL. Intended to run inside the company_os
container (`make local-seed`) or against a host venv with services/company_os
on PYTHONPATH.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COS_ROOT = ROOT / "services" / "company_os"
if COS_ROOT.is_dir() and str(COS_ROOT) not in sys.path:
    sys.path.insert(0, str(COS_ROOT))

DEMO_VENTURE_ID = "ven_localdemo000"
DEMO_NAME = "Local Demo"
DEMO_IDEA = (
    "A local-compose demo venture so the console has something to open "
    "without spending tokens."
)
DEMO_TASKS = [
    {
        "title": "Sketch the first landing page",
        "why_now": "Founders need a concrete artifact to react to on day one.",
        "detail": "Produce a one-page outline of the demo product's public face.",
        "est_minutes": 25,
        "runnable": True,
        "rank": 0,
    },
    {
        "title": "List the next founder decision",
        "why_now": "The queue should never look empty after seed.",
        "detail": "Write down the single decision that would unblock the next build step.",
        "est_minutes": 15,
        "runnable": True,
        "rank": 1,
    },
]


def _seed() -> str:
    from app.credits.accounts import ensure_account
    from app.db.models import Venture
    from app.planning.store import list_tasks, save_tasks
    from app.state import db
    from app.workspaces import service as workspace_service
    from app.workspaces.resolve import LOCAL_PERSON_ID, local_person
    from core.clock import utcnow
    from core.enums import VentureStage

    local_person()

    factory = db()
    with factory() as session:
        existing = session.get(Venture, DEMO_VENTURE_ID)
        if existing is not None:
            venture_id = existing.id
            workspace_id = existing.workspace_id
            owner_id = LOCAL_PERSON_ID
        else:
            existing = None

    if existing is None:
        workspace = workspace_service.create_workspace(
            kind="venture",
            name=DEMO_NAME,
            entry_mode="idea",
            owner_id=LOCAL_PERSON_ID,
        )
        now = utcnow()
        ven = Venture(
            id=DEMO_VENTURE_ID,
            workspace_id=workspace.id,
            name=DEMO_NAME,
            raw_idea=DEMO_IDEA,
            stage=VentureStage.DRAFT.value,
            autonomy_level="L1",
            created_at=now,
            updated_at=now,
        )
        with factory() as session:
            session.add(ven)
            session.commit()
            venture_id = ven.id
            workspace_id = ven.workspace_id
        owner_id = workspace.owner_id
        created = True
    else:
        created = False

    ensure_account(
        venture_id,
        workspace_id=workspace_id,
        owner_person_id=owner_id,
    )

    already = [
        t
        for t in list_tasks(venture_id, statuses=("open", "running", "done", "dismissed"))
        if t.get("source") == "seed"
    ]
    if not already:
        save_tasks(venture_id, DEMO_TASKS, source="seed")
        tasks_note = "seeded two planned tasks"
    else:
        tasks_note = f"{len(already)} seed task(s) already present"

    action = "created" if created else "already existed"
    return f"{venture_id} ({action}; {tasks_note})"


def main() -> int:
    console_url = os.environ.get("CONSOLE_URL", "http://localhost:3100").rstrip("/")
    summary = _seed()
    venture_id = summary.split(" ", 1)[0]
    print(f"venture_id={venture_id}")
    print(f"console_url={console_url}/v/{venture_id}/now")
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
