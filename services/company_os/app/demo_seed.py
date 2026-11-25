"""A fictional, keyless example saved through the local database only."""

from __future__ import annotations

import json

SAMPLE = {
    "name": "Tool library",
    "idea": "You help neighbours borrow household tools from a small local library.",
    "summary": "You offer a simple booking page so neighbours can borrow a drill or ladder instead of buying one.",
    "hypothesis": "Neighbours will reserve a tool when you show them a simple booking page.",
    "action": "You show a paper booking form to five neighbours and ask which tool they would reserve.",
    "measure": "You count how many of the five people choose a tool and a date to borrow it.",
}


def seed_demo() -> str:
    from app.cli_local import _paths

    _paths()
    from app.db.models import Venture
    from app.db.supymem_models import Experiment, IdeaRevision, IdeaSession
    from app.experiments.ledger import KIND
    from app.state import db
    from app.workspaces.resolve import _adopt_venture
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    venture_id = new_id("ven")
    with db()() as session:
        venture = Venture(
            id=venture_id, name=f"Demo: {SAMPLE['name']}",
            raw_idea=f"Demo example: {SAMPLE['idea']}", stage="draft",
            autonomy_level="L0", created_at=now, updated_at=now,
        )
        session.add(venture)
        workspace = _adopt_venture(session, venture)
        workspace.settings_json = json.dumps({"demo": True})
        idea_id = new_id("ise")
        revision_id = new_id("irv")
        session.add(IdeaSession(
            id=idea_id, workspace_id=workspace.id, venture_id=venture_id,
            case="know", state="confirmed", current_revision_id=revision_id,
            confirmed_revision_id=revision_id, confirmed_at=now,
            confirmed_by_person_id=workspace.owner_id, created_at=now, updated_at=now,
        ))
        session.flush()
        session.add(IdeaRevision(
            id=revision_id, session_id=idea_id, workspace_id=workspace.id,
            venture_id=venture_id, rev=1, source="demo",
            summary=f"Demo example: {SAMPLE['summary']}", created_at=now,
        ))
        session.add(Experiment(
            id=new_id("exp"), workspace_id=workspace.id, venture_id=venture_id,
            kind=KIND, hypothesis=f"Demo example: {SAMPLE['hypothesis']}",
            action=SAMPLE["action"], metric=SAMPLE["measure"], state="proposed",
            context=json.dumps({"source": "demo", "seq": 0, "direction": revision_id,
                                "how_to_run": [
                                    "This is a demo plan. Nothing has started. Create your own venture before running a test.",
                                    SAMPLE["action"], SAMPLE["measure"],
                                ]}),
        ))
        session.commit()
    return venture_id
