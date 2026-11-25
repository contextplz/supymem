"""A product founder's GTM choice survives readback without queuing work."""

from sqlalchemy import func, select

from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_core_signed_boundary import headers


def _request(client, method: str, path: str, **kwargs):
    return client.request(method, path, headers=headers(path, method, "usr_alice"), **kwargs)


def test_product_gtm_path_persists_next_action_without_starting_work(client):
    from app.db.models import Job, Venture
    from app.state import db

    description = "It helps rehearsal studios manage bookings"
    created = _request(client, "POST", "/entry", json={
        "text": f"Existing product: {description}. app · unsure · gtm · solo",
        "explicit_mode": "product", "stage": "product", "start": True, "confirm": True,
        "answers": {"what": "app", "where": "unsure", "want_first": "gtm", "team": "solo",
                    "product_summary": description},
    })
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["next_step"] == "gtm"
    assert result["onboarding"] is None
    venture_id = result["venture_id"]
    for _ in range(2):  # A fresh Cockpit load reads the same durable choice.
        saved = _request(client, "GET", f"/ventures/{venture_id}")
        assert saved.status_code == 200, saved.text
        body = saved.json()
        assert body["entry_mode"] == "product"
        assert body["entry_goal"] == "gtm"
        assert body["raw_idea"] == description
        assert body["name"] == "Rehearsal Studios"
    with db()() as session:
        assert session.get(Venture, venture_id) is not None
        assert session.scalar(select(func.count()).select_from(Job).where(Job.venture_id == venture_id)) == 0


def test_product_gtm_without_summary_keeps_a_readable_working_name(client):
    created = _request(client, "POST", "/entry", json={
        "text": "Existing product: app · unsure · gtm · solo", "explicit_mode": "product",
        "confirm": True, "answers": {"what": "app", "want_first": "gtm"},
    })
    assert created.status_code == 200, created.text
    saved = _request(client, "GET", f"/ventures/{created.json()['venture_id']}")
    assert saved.json()["name"] == "Existing app"
    assert saved.json()["raw_idea"] == "Existing app"


def test_chat_first_product_request_uses_the_same_durable_gtm_path(client):
    created = _request(client, "POST", "/entry", json={
        "text": "I already have a product. What should I do next to get users? Do not start paid work or send outreach.",
        "explicit_mode": "idea", "confirm": True,
    })
    assert created.status_code == 200, created.text
    result = created.json()
    assert result["mode"] == "product"
    assert result["next_step"] == "gtm"
    assert result["onboarding"] is None
    saved = _request(client, "GET", f"/ventures/{result['venture_id']}").json()
    assert saved["entry_goal"] == "gtm"
    assert saved["name"] == "Existing product"
    assert "Do not start paid work or send outreach" in saved["raw_idea"]


def test_existing_idea_venture_can_persist_a_gtm_next_step(client):
    from app.workspaces import service as workspace_service

    created = _request(client, "POST", "/ventures", json={
        "name": "StudioLedger", "raw_idea": "Booking software for rehearsal studios", "start": False,
    })
    assert created.status_code == 201, created.text
    venture_id = created.json()["id"]
    workspace_id = created.json()["workspace_id"]
    workspace_service.merge_settings(workspace_id, {"entry_goal": "gtm"})
    saved = _request(client, "GET", f"/ventures/{venture_id}")
    assert saved.status_code == 200, saved.text
    assert saved.json()["entry_mode"] == "idea"
    assert saved.json()["entry_goal"] == "gtm"
