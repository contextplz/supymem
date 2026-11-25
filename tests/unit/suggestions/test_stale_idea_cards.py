"""A stored idea-stage card is expired at read time once the venture's next step is past the idea stage."""
import pytest

from tests.security import test_core_signed_boundary as boundary

signed_client = boundary.client


@pytest.fixture
def client(signed_client):
    return signed_client


def test_stale_idea_card_expires_current_card_stays(client, monkeypatch):
    from app.db.models import Venture
    from app.db.supymem_models import Suggestion
    from app.state import db
    from app.suggestions import service
    from core.clock import utcnow

    ws = boundary.make_workspace(client, "usr_alice")
    now = utcnow()
    with db()() as s:
        s.add(Venture(id="ven_s", workspace_id=ws, name="s", raw_idea="x", stage="idea", created_at=now, updated_at=now))
        s.flush()
        for sid, kind in (("sug_old", "idea.advance"), ("sug_cur", "plan.generate")):
            s.add(Suggestion(id=sid, venture_id="ven_s", title=kind, action_kind=kind, dedupe_key=sid, created_at=now))
        s.commit()
    import app.ventures.next_step as ns

    monkeypatch.setattr(ns, "next_step", lambda vid: {"key": "idea.clarify"})
    assert service.list_for({ws})["items"] == []
    monkeypatch.setattr(ns, "next_step", lambda vid: {"key": "plan.generate"})
    out = service.list_for({ws})
    assert out["items"] == [] and out["new"] == 0
    with db()() as s:
        assert s.get(Suggestion, "sug_old").status == "expired"
        assert s.get(Suggestion, "sug_cur").status == "expired"
