"""A reopened desktop locates active work without a truncated global job list."""
from tests.security.test_connector_oauth_boundary import client as client
from tests.security.test_durable_journey_boundary import request, venture


def test_job_recovery_filters_before_limit_and_preserves_tenant_boundary(client):
    from app.core.jobs import enqueue
    from app.db.models import Job
    from app.state import db

    vid, wid, _ = venture(client)
    other_vid, other_wid, _ = venture(client, "usr_bob")
    with db()() as session:
        active_id = enqueue(session, "plan.compile", {"venture_id": vid}, workspace_id=wid, venture_id=vid)
        for i in range(205):
            job_id = enqueue(session, "plan.compile", {"sequence": i}, workspace_id=wid, venture_id=vid)
            session.flush()
            session.get(Job, job_id).status = "succeeded"
        enqueue(session, "research.deep", {}, workspace_id=wid, venture_id=vid)
        enqueue(session, "plan.compile", {}, workspace_id=other_wid, venture_id=other_vid)
        session.commit()
    path = f"/workspaces/{wid}/jobs"
    params = {"venture_id": vid, "kinds": "plan.compile", "active_only": True, "limit": 1}
    response = request(client, "GET", path, params=params)
    assert response.status_code == 200, response.text
    assert [row["id"] for row in response.json()["items"]] == [active_id]
    assert request(client, "GET", path, params={**params, "venture_id": other_vid}).json()["items"] == []
    assert request(client, "GET", path, "usr_bob", params=params).status_code == 404
