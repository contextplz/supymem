"""The small priorities view reuses saved next moves without starting work."""
from sqlalchemy import func, select

from tests.unit.test_exploration import exploration_market, market, venture


def test_dashboard_priorities_are_scoped_flagged_read_only_and_fail_independently(exploration_market, monkeypatch, caplog):
    monkeypatch.delenv("SUPYMEM_MODE", raising=False)
    from app.chat import next_moves
    from app.db.models import ChatMessage, Job
    from app.db.supymem_models import Experiment
    from app.experiments import exploration
    from app.state import db

    client, venture_id, _workspace_id, view, *_rest = exploration_market
    options = [{"statement": f"You could help {view['icp']['who']} with {view['icp']['problem']} in group {index}.",
                "audience": f"Group {index} of {view['icp']['who']}", "evidence_level": "hypothesis"}
               for index in range(3)]
    exploration.create_round(venture_id, kind="segment", options=options)
    original_rounds = exploration.list_rounds(venture_id)
    expected_moves = next_moves.compose(venture_id)
    compose = next_moves.compose
    calls = []

    def counted(vid):
        calls.append(vid)
        return compose(vid)

    monkeypatch.setattr(next_moves, "compose", counted)
    with db()() as session:
        before = [session.scalar(select(func.count()).select_from(model)) for model in (Job, ChatMessage, Experiment)]
    response = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data.pop("next_moves") == expected_moves
    assert data == original_rounds
    assert calls == [venture_id]
    assert expected_moves["actions"] and all(action.get("action") for action in expected_moves["actions"])
    with db()() as session:
        after = [session.scalar(select(func.count()).select_from(model)) for model in (Job, ChatMessage, Experiment)]
    assert after == before
    assert exploration.list_rounds(venture_id) == original_rounds

    calls.clear()
    denied = client.get(f"/ventures/{venture_id}/experiments/rounds", headers={"X-Subject": "another-account"})
    assert denied.status_code in {401, 404}, denied.text
    assert not calls and "next_moves" not in denied.json()
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {})
    disabled = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert disabled.status_code == 404 and not calls
    monkeypatch.setattr(exploration, "get_policy", lambda _key: {"exploration_v1": True})

    def unavailable(_vid):
        raise RuntimeError("Saved priorities are unavailable")

    monkeypatch.setattr(next_moves, "compose", unavailable)
    failed = client.get(f"/ventures/{venture_id}/experiments/rounds")
    assert failed.status_code == 200, failed.text
    data = failed.json()
    assert data.pop("next_moves") is None
    assert data == original_rounds
    assert "exploration_priorities_unavailable" in caplog.text
