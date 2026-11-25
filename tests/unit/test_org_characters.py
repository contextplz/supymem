from tests.company_os_harness import load_company_os_app

load_company_os_app()

from app.org.characters import (
    character_for,
    enrich_agent,
    match_mention,
    parse_speaker,
    stamp_speaker,
)


def test_job_title_becomes_a_first_name():
    person = character_for("researcher", "agt_1", "Researcher 1")
    assert person.given_name == "Maya"
    assert person.title == "Researcher"


def test_existing_first_name_is_kept():
    person = character_for("builder", "agt_2", "Cole")
    assert person.given_name == "Cole"


def test_mention_routes_to_that_employee():
    agents = [
        enrich_agent({"id": "a1", "role": "researcher", "name": "Researcher"}),
        enrich_agent({"id": "a2", "role": "builder", "name": "Builder"}),
    ]
    hit = match_mention("@Maya what did you find?", agents)
    assert hit is not None
    assert hit["name"] == "Maya"
    assert hit["id"] == "a1"


def test_mention_by_role_and_prefix():
    agents = [
        enrich_agent({"id": "a1", "role": "researcher", "name": "Maya"}),
        enrich_agent({"id": "a2", "role": "designer", "name": "Lina"}),
    ]
    by_role = match_mention("@Research look at visas", agents)
    assert by_role is not None and by_role["id"] == "a1"
    by_prefix = match_mention("@De this landing page", agents)
    assert by_prefix is not None and by_prefix["id"] == "a2"


def test_stamp_round_trip():
    body = stamp_speaker("Hello from the desk.", {"name": "Ren", "title": "Team lead", "id": "a0"})
    speaker, rest = parse_speaker(body)
    assert speaker is not None
    assert speaker["name"] == "Ren"
    assert rest.strip() == "Hello from the desk."


def test_canonical_role_collapses_pack_and_catalog():
    from app.org.characters import canonical_role

    assert canonical_role("research") == canonical_role("researcher") == "researcher"
    assert canonical_role("concierge") == canonical_role("founder_concierge")


def test_driver_skips_paused_ventures():
    from app.will.driver import drive_open_work
    from unittest.mock import patch

    async def _run():
        with patch("app.will.driver.list_active", return_value=[{"id": "ven_x", "paused": True, "stage": "live"}]):
            out = await drive_open_work()
        assert out["skipped"] == ["ven_x"]
        assert out["started"] == []

    import asyncio

    asyncio.run(_run())


def test_owner_for_work_picks_researcher():
    from app.org.characters import owner_for_work

    agents = [
        enrich_agent({"id": "a1", "role": "researcher", "name": "Maya"}),
        enrich_agent({"id": "a2", "role": "builder", "name": "Cole"}),
    ]
    hit = owner_for_work("Interview five buyers", ["research"], agents)
    assert hit is not None
    assert hit["name"] == "Maya"
