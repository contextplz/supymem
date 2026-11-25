import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app


@pytest.fixture(scope="module")
def progress():
    load_company_os_app()
    from app.research import progress
    return progress


def job(status="leased"):
    return SimpleNamespace(id="job_progress", kind="research.deep", status=status,
                           workspace_id="workspace", venture_id="venture",
                           started_at=datetime(2026, 10, 13, tzinfo=timezone.utc))


def test_progress_counts_only_completed_receipts_and_never_finishes_early(progress):
    runtime = {"progress_plan": ["prior_category", "prior_complaints", "review"],
               "operations": {"prior_category": {"state": "completed"},
                              "prior_complaints": {"state": "calling"}}}
    result = progress.compute_progress(job(), runtime)
    assert result == {"state": "leased", "percent": 33,
                      "step_label": "Reading customer complaints",
                      "started_at": "2026-10-13T00:00:00+00:00"}
    for state in ("responded", "provider_responded", "retryable", "reserving", "release_pending", "failed"):
        runtime["operations"]["prior_complaints"]["state"] = state
        assert progress.compute_progress(job(), runtime)["percent"] == 33
    runtime["operations"] = {key: {"state": "completed"} for key in runtime["progress_plan"]}
    for status in ("leased", "retry_wait", "failed_dead", "cancelled"):
        assert progress.compute_progress(job(status), runtime)["percent"] == 99
    assert progress.compute_progress(job("succeeded"), runtime)["percent"] == 100


def test_followup_and_repair_receipts_join_the_plan_without_inventing_work(progress):
    runtime = {"progress_plan": ["prior_category", "review"], "operations": {
        "prior_category": {"state": "completed"}, "prior_name_0": {"state": "calling"}}}
    assert progress.compute_progress(job(), runtime)["percent"] == 33
    assert progress.compute_progress(job(), runtime)["step_label"] == "Searching for similar products"
    runtime["operations"]["prior_name_0"]["state"] = "completed"
    runtime["operations"]["review-repair"] = {"state": "calling"}
    result = progress.compute_progress(job(), runtime)
    assert result["percent"] == 50
    assert result["step_label"] == "Writing your report"
    runtime["started_at"] = "2026-10-12T12:00:00+00:00"
    assert progress.compute_progress(job(), runtime)["started_at"] == runtime["started_at"]


def test_queued_unknown_and_validation_progress(progress):
    assert progress.compute_progress(job("pending"), None)["percent"] == 0
    assert progress.compute_progress(job("pending"), None)["state"] == "queued"
    assert progress.compute_progress(job(), None)["percent"] is None
    runtime = {"operations": {"prior_complaints": {"state": "calling"}}}
    assert progress.compute_progress(job(), runtime)["percent"] is None
    assert progress.compute_progress(job(), runtime)["step_label"] == "Reading customer complaints"
    validation = job()
    validation.kind = "idea.validate"
    runtime = {"progress_plan": ["review"], "operations": {"review": {"state": "calling"}}}
    assert progress.compute_progress(validation, runtime)["percent"] == 0
    runtime["operations"]["review"]["state"] = "completed"
    assert progress.compute_progress(validation, runtime)["percent"] == 99
    validation.status = "succeeded"
    assert progress.compute_progress(validation, runtime)["percent"] == 100


def test_job_status_reads_durable_scoped_progress_without_exposing_runtime(progress):
    from app.core.job_status import _view
    current = job()
    current.attempts, current.max_attempts, current.last_error = 1, 3, None
    current.created_at = current.scheduled_for = current.locked_at = current.started_at
    current.finished_at = None
    current.payload_json = '{"revision_id":"revision","secret":"private"}'
    row = SimpleNamespace(workspace_id="workspace", venture_id="venture", summary=json.dumps({"_runtime": {
        "job_id": current.id, "snapshot": {"private": "secret"}, "progress_plan": ["prior_category", "review"],
        "operations": {"prior_category": {"state": "completed", "response": {"private": "secret"}}}}}))
    reads = []
    session = SimpleNamespace(get=lambda model, identifier: reads.append(identifier) or row)
    result = _view(current, session)
    assert result["progress"]["percent"] == 50
    assert reads == [progress.identifier(current.id)]
    assert "secret" not in json.dumps(result)
    row.venture_id = "another_venture"
    assert _view(current, session)["progress"]["percent"] is None
    row.venture_id = "venture"
    row.summary = row.summary.replace(current.id, "old_job")
    assert _view(current, session)["progress"]["percent"] is None
    for summary in ("invalid JSON", "[]", '{"_runtime":null}'):
        row.summary = summary
        assert _view(current, session)["progress"]["percent"] is None


def test_worker_freezes_the_actual_intent_plan_before_searching(progress, monkeypatch):
    from app.research import deep, execution
    from app.research.prior_art import runner
    snapshot = {"summary": "customer workflow software", "customer": "small teams"}
    document = {"_runtime": {"snapshot": snapshot, "operations": {}}}
    row = SimpleNamespace(state="running", summary=json.dumps(document), workspace_id="workspace", completed_at=None)
    monkeypatch.setattr(execution, "claim", lambda *args, **kwargs: "dossier")
    monkeypatch.setattr(execution, "read", lambda *args: row)
    monkeypatch.setattr(execution, "mutate", lambda did, payload, update: update(row, document, None))
    monkeypatch.setattr(runner, "enabled", lambda: False)
    monkeypatch.setattr(deep, "inherit_feedback", lambda *args: None)
    monkeypatch.setattr(deep, "_web_provider", lambda: None)
    monkeypatch.setattr(deep, "sources_for", lambda *args: [])
    deep.run_deep({"phase": "demand"})
    assert document["_runtime"]["progress_plan"] == [key for key, _ in deep.queries_for(snapshot, "demand")] + ["review"]
    assert row.state == "partial"
