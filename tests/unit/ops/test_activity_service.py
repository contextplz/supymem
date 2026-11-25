"""Ops activity service — DB write + pubsub."""

from __future__ import annotations

def test_log_activity_creates_record(ops_env):
    from app.ops.services import activity as activity_svc

    ws = ops_env["workspace_id"]
    entry = activity_svc.log_activity(
        ws,
        agent_type="social_media",
        action="post_tweet",
        summary="Posted a tweet about our product",
        level="success",
    )
    assert entry["id"].startswith("oay_")
    assert entry["agent_type"] == "social_media"
    assert entry["level"] == "success"


def test_log_activity_publishes_to_subscribers(ops_env):
    from app.ops.services import activity as activity_svc

    ws = ops_env["workspace_id"]
    seen: list[dict] = []
    unsub = activity_svc.subscribe(seen.append, workspace_id=ws)
    try:
        activity_svc.log_activity(
            ws,
            agent_type="finance",
            action="revenue_snapshot",
            summary="Saved daily revenue snapshot",
        )
        assert len(seen) == 1
        assert seen[0]["summary"] == "Saved daily revenue snapshot"
    finally:
        unsub()


def test_get_recent_activity_returns_ordered(ops_env):
    from app.ops.services import activity as activity_svc

    ws = ops_env["workspace_id"]
    activity_svc.log_activity(ws, agent_type="a", action="x", summary="First-unique-aaa")
    activity_svc.log_activity(ws, agent_type="b", action="y", summary="Second-unique-bbb")
    results = activity_svc.get_recent_activity(ws, limit=10)
    summaries = [r["summary"] for r in results]
    assert "Second-unique-bbb" in summaries
    assert "First-unique-aaa" in summaries
    # Newest first.
    assert summaries.index("Second-unique-bbb") < summaries.index("First-unique-aaa")
