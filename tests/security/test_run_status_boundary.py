"""Persistent bar data uses the dashboard's signed tenant boundary."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def test_dashboard_run_and_attention_are_founder_scoped(client):
    vid, _, _ = venture(client)
    from app.concierge.human import ensure_human_task
    task = ensure_human_task(vid, title="Review the next step", why="A decision is required", provider="decision")
    path = f"/ventures/{vid}/dashboard"
    result = request(client, "GET", path)
    assert result.status_code == 200, result.text
    value = result.json()
    assert value["run_status"]["state"] == "queued"
    assert value["run_status"]["progress"] is None
    assert value["needs_you"][0]["href"] == f"/v/{vid}#attention-{task['id']}"
    assert request(client, "GET", path, subject="usr_bob").status_code in {403, 404}
    alternate = request(client, "POST", f"/ventures/{vid}/human-tasks/{task['id']}/alternative", subject="usr_bob", json={})
    assert alternate.status_code in {403, 404}
