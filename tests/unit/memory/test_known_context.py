"""The preflight can show current conflicting quotes, not unrelated history."""
import pytest

from tests.unit.memory.test_facts import facts


@pytest.mark.parametrize("first,second", [("Factory audit coordinators", "Museum procurement teams"),
                                         ("Urban dog owners", "Teenage chess learners")])
def test_known_context_exposes_both_current_scoped_conflict_sources(facts, monkeypatch, first, second):
    from app.memory import context, normalize
    from app.memory.current import select_current_reports

    data = facts
    monkeypatch.setattr(normalize, "enabled", lambda flag: flag == "known_check_v1")
    with data.bind():
        left = data.write(data.message("message_first", "Our audience is " + first + "."))
        source = data.message("message_second", "Our audience is " + second + ".")
        from app.memory import facts as api
        with data.factory() as session:
            right = api.remember_fact(session, venture_id="ven_owned", subject="idea.audience", predicate="targets",
                value=source["quote"], source=source).id
            session.commit()
        with data.factory() as session:
            reports = select_current_reports(session, venture_id="ven_owned", legacy_hits=[])
            assert {item["id"] for item in reports["conflict_values"]} == {left, right}
            assert {item["source"]["ref"] for item in reports["conflict_values"]} == {"chat:message_first", "chat:message_second"}
            assert reports["facts"] == []
            monkeypatch.setattr("app.memory.get_memory", lambda: object())
            monkeypatch.setattr("app.memory.current.legacy_source_hits", lambda *_args: [])
            projected = context.context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                purpose="known.check", query="audience")
            assert projected["effect_authority"] is False
            assert {item["id"] for item in projected["conflict_values"]} == {left, right}
            monkeypatch.setattr(normalize, "enabled", lambda _flag: False)
            assert "conflict_values" not in context.context_for(session, workspace_id="wsp_owned", venture_id="ven_owned",
                purpose="known.check", query="audience")
