"""Signed live API scope: filtering must precede both counts and list limits."""
from datetime import timedelta

import pytest

from tests.security import test_core_signed_boundary as boundary

signed_client = boundary.client
headers = boundary.headers
make_workspace = boundary.make_workspace


@pytest.fixture
def client(signed_client):
    return signed_client


def test_scoped_list_counts_before_limit_and_signed_foreign_scope_refusal(client, monkeypatch):
    from app.db.models import Venture
    from app.db.supymem_models import Suggestion
    from app.state import db
    from core.clock import utcnow

    alice = make_workspace(client, "usr_alice")
    bob = make_workspace(client, "usr_bob")
    now = utcnow()
    with db()() as session:
        for vid, wid in (("ven_current", alice), ("ven_other", alice), ("ven_private", bob)):
            session.add(Venture(id=vid, workspace_id=wid, name=vid, raw_idea="Owned fixture",
                                stage="idea", created_at=now, updated_at=now))
        session.flush()
        for index in range(60):
            session.add(Suggestion(id=f"sug_other_{index}", venture_id="ven_other", title="Other proposed work",
                                   action_kind="plan.generate", dedupe_key=f"other_{index}", created_at=now))
        for status in ("new", "saved", "done"):
            session.add(Suggestion(id=f"sug_current_{status}", venture_id="ven_current", title="Current proposed work",
                                   action_kind="plan.generate", status=status, dedupe_key=status,
                                   created_at=now - timedelta(days=1)))
        session.add(Suggestion(id="sug_private", venture_id="ven_private", title="Private proposed work",
                               action_kind="plan.generate", dedupe_key="private", created_at=now))
        session.commit()

    path = "/suggestions"
    from app.suggestions import service
    from tests.unit.suggestions.test_truthful_suggestions import proposal
    monkeypatch.setattr(service.proposals, "propose_next_actions", lambda *_: [proposal()])
    import json
    with db()() as session:
        for row in session.query(Suggestion).filter(Suggestion.venture_id.in_(("ven_current", "ven_other"))):
            row.action_kind = "research.start"
            row.payload_json = json.dumps({"_suggestion_state": service._state_binding(row.venture_id)})
        session.commit()
    current = client.get(path, params={"venture_id": "ven_current"}, headers=headers(path))
    assert current.status_code == 200, current.text
    assert {row["id"] for row in current.json()["items"]} == {"sug_current_new", "sug_current_saved"}
    assert (current.json()["new"], current.json()["saved"]) == (1, 1)
    history = client.get(path, params={"venture_id": "ven_current", "status": "history"}, headers=headers(path))
    assert [row["id"] for row in history.json()["items"]] == ["sug_current_done"]
    assert (history.json()["new"], history.json()["saved"]) == (1, 1)
    all_owned = client.get(path, headers=headers(path)).json()
    assert len(all_owned["items"]) == 50
    assert all_owned["new"] == 61 and all_owned["saved"] == 1
    assert all(row["venture_id"] != "ven_private" for row in all_owned["items"])
    for foreign in ("ven_private", "ven_missing"):
        assert client.get(path, params={"venture_id": foreign}, headers=headers(path)).status_code == 404
    decision = "/suggestions/sug_private/decision"
    assert client.post(decision, json={"decision": "accept"}, headers=headers(decision, "POST")).status_code == 404
    with db()() as session:
        assert session.get(Suggestion, "sug_private").status == "new"
