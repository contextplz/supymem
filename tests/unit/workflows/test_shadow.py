"""Shadow mode never executes connector writes (40 §6)."""

from __future__ import annotations

from tests.company_os_harness import load_company_os_app

load_company_os_app(reset=False)

from app.workflows import shadow


def test_is_shadow_when_workflow_status_shadow() -> None:
    assert shadow.is_shadow_run(workflow_status="shadow", run_shadow=False)
    assert shadow.is_shadow_run(workflow_status="draft", run_shadow=False)
    assert not shadow.is_shadow_run(workflow_status="active", run_shadow=False)
    assert shadow.is_shadow_run(workflow_status="active", run_shadow=True)


def test_record_intended_write_does_not_mark_executed() -> None:
    writes: list[dict] = []
    entry = shadow.record_intended_write(
        writes,
        step_n=4,
        label="Post summary",
        provider="slack",
        op="post_message",
        args={"text": "hello"},
        target="#finance",
    )
    assert entry["executed"] is False
    assert entry["shadow"] is True
    assert len(writes) == 1
    assert "slack.post_message" in shadow.summarize(writes)
