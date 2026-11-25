from datetime import timedelta

import pytest

from tests.unit.gtm.test_linkedin_people import _app as _app, journey_db as journey_db, linkedin as linkedin


def test_opening_with_audience_flags_once_and_queues_without_provider_calls(linkedin, monkeypatch):
    from app.gtm import exa_people
    data, mod = linkedin, linkedin["mod"]
    monkeypatch.setenv("EXA_API_KEY", "offline-exa-key")
    queued = []

    def queue(vid, actor):
        with data["factory"]() as session:
            _, stored = mod._doc(session, vid)
        queued.append((vid, actor, stored.get("requested_at")))

    monkeypatch.setattr(exa_people, "queue", queue)
    first = mod.view(data["vid"], data["actor"])
    second = mod.view(data["vid"], data["actor"])
    assert first["searching"] and first["has_audience"] and first["exa_configured"]
    assert first["queued_at"] == second["queued_at"] and first["queued_at"]
    assert first["people"] == [] and first["notice"] is None
    assert first["searches_today"] == 0 and first["search_limit"] == 3
    assert queued and all(call == (data["vid"], data["actor"], first["queued_at"]) for call in queued)


@pytest.mark.parametrize("who,key", [("", "offline-exa-key"), ("   ", "offline-exa-key"), ("gym owners", "")])
def test_no_auto_request_without_audience_or_exa(linkedin, monkeypatch, who, key):
    from app.gtm import exa_people
    data, mod = linkedin, linkedin["mod"]
    monkeypatch.setenv("EXA_API_KEY", key)
    monkeypatch.setattr(mod, "_venture_icp", lambda vid: ({"who": who}, "A gym app"))
    monkeypatch.setattr(exa_people, "queue", lambda *args: None)
    result = mod.view(data["vid"], data["actor"])
    assert not result["searching"] and not result["queued_at"]
    assert result["has_audience"] == bool(who.strip())
    assert result["exa_configured"] == bool(key)


@pytest.mark.parametrize("searches", [1, 3])
def test_today_search_is_not_auto_requested_again_and_cap_has_reset_time(linkedin, monkeypatch, searches):
    from app.gtm import exa_people
    data, mod = linkedin, linkedin["mod"]
    monkeypatch.setenv("EXA_API_KEY", "offline-exa-key")
    monkeypatch.setattr(exa_people, "queue", lambda *args: None)
    with data["factory"]() as session:
        row, stored = mod._doc(session, data["vid"], create=True)
        stored["exa_searches"] = {mod._today(): searches}
        stored["order"] = {"source": "exa", "state": "done", "finished_at": mod.utcnow().isoformat()}
        mod._save(row, stored)
        session.commit()
    result = mod.view(data["vid"], data["actor"])
    assert not result["searching"] and result["searches_today"] == searches
    if searches == 3:
        assert result["next_search_at"] == (mod.utcnow() + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        assert not mod.request(data["vid"], data["actor"])["searching"]
        with data["factory"]() as session:
            _, stored = mod._doc(session, data["vid"])
        assert not stored.get("requested_at")
    else:
        assert result["next_search_at"] is None


def test_previous_day_cap_does_not_block_auto_request(linkedin, monkeypatch):
    from app.gtm import exa_people
    data, mod = linkedin, linkedin["mod"]
    monkeypatch.setenv("EXA_API_KEY", "offline-exa-key")
    monkeypatch.setattr(exa_people, "queue", lambda *args: None)
    yesterday = mod.utcnow() - timedelta(days=1)
    with data["factory"]() as session:
        row, stored = mod._doc(session, data["vid"], create=True)
        stored["exa_searches"] = {yesterday.date().isoformat(): 3}
        stored["order"] = {"source": "exa", "state": "done", "finished_at": yesterday.isoformat()}
        mod._save(row, stored)
        session.commit()
    result = mod.view(data["vid"], data["actor"])
    assert result["searching"] and result["searches_today"] == 0
    assert result["next_search_at"] is None
