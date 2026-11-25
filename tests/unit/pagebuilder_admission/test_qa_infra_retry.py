"""QA that never ran (task stopped before its report) gets a fresh, capped attempt; page verdicts stay final."""
from datetime import UTC, datetime

import pytest

from tests.integration.test_postgres_pagebuilder_admission import complete_no_plan_qa, qa_cloud, worker_step
from tests.integration.test_postgres_fargate_lifecycle import saved
from tests.integration.test_postgres_static_publication import _hosting_environment
from tests.unit.pagebuilder_admission.test_boundary import modules as modules  # noqa: F401
from tests.unit.pagebuilder_admission.test_no_plan_inputs import no_plan_candidate as no_plan_candidate, setup as setup  # noqa: F401


@pytest.fixture(autouse=True)
def hosting(monkeypatch):
    _hosting_environment(monkeypatch)


def publish(d, monkeypatch):
    from app.pagebuilder_admission import publication

    requested = publication.request(d["vid"], d["caller"], d["selection"])
    assert requested["state"] == "checking"
    return qa_cloud(d, monkeypatch, requested["run_id"])


def stop_qa_before_report(d):
    """Production 2026-10-07: the static-qa container exited 1 at startup and never reported."""
    worker_step(d, "build.watch")
    assert d["qa"].tasks, "QA must have launched"
    for task in d["qa"].tasks.values():
        task.update(lastStatus="STOPPED", stoppedAt=datetime.now(UTC), stopCode="EssentialContainerExited")
    for _ in range(6):
        if saved(d)["closed"]:
            break
        worker_step(d, "build.watch")
    closed = saved(d)
    assert closed["state"] == "failed" and closed["closed"] and closed["reserved"] == 0
    assert closed["spec"]["reconciliation_reason"] == "qa_task_stopped_before_report"
    return closed


def test_infra_failed_qa_offers_publish_again_with_a_fresh_run_and_then_goes_through(no_plan_candidate, monkeypatch):
    from app.pagebuilder_admission import publication

    d = publish(no_plan_candidate, monkeypatch)
    first = d["build_run_id"]
    stop_qa_before_report(d)
    view = publication.status(d["vid"], d["caller"], first)
    assert view["state"] == "failed" and view["retryable"] and view["next_action"] == "publish_again"
    assert publication.current(d["vid"], d["caller"], d["selection"])["run_id"] == first

    again = publication.request(d["vid"], d["caller"], d["selection"])
    assert again["run_id"] != first and again["state"] == "checking" and again["next_action"] is None
    # A double click re-reads the fresh attempt; it never opens another one.
    assert publication.request(d["vid"], d["caller"], d["selection"])["run_id"] == again["run_id"]
    assert publication.current(d["vid"], d["caller"], d["selection"])["run_id"] == again["run_id"]

    d = qa_cloud(d, monkeypatch, again["run_id"])
    closed = complete_no_plan_qa(d)
    assert closed["state"] == "succeeded" and closed["spec"]["qa_attempt"] == 1
    assert publication.status(d["vid"], d["caller"], again["run_id"])["next_action"] is None


def test_infra_retries_are_capped_then_the_founder_builds_a_new_version(no_plan_candidate, monkeypatch):
    from app.pagebuilder_admission import inputs, publication

    d = publish(no_plan_candidate, monkeypatch)
    runs = [d["build_run_id"]]
    for _ in range(inputs.QA_INFRA_RETRIES):
        stop_qa_before_report(d)
        nxt = publication.request(d["vid"], d["caller"], d["selection"])
        assert nxt["run_id"] not in runs
        runs.append(nxt["run_id"])
        d = qa_cloud(d, monkeypatch, nxt["run_id"])
    stop_qa_before_report(d)
    final = publication.status(d["vid"], d["caller"], runs[-1])
    assert final["state"] == "failed" and not final["retryable"] and final["next_action"] == "build_new_version"
    assert publication.request(d["vid"], d["caller"], d["selection"])["run_id"] == runs[-1]


def test_failed_page_checks_stay_final(no_plan_candidate, monkeypatch):
    from app.pagebuilder_admission import publication

    d = publish(no_plan_candidate, monkeypatch)
    d["qa_state"]["failed_report"] = True
    closed = complete_no_plan_qa(d)
    assert closed["state"] == "failed" and closed["spec"]["lifecycle"]["completion"]["verified_report"] is True
    view = publication.status(d["vid"], d["caller"], d["build_run_id"])
    assert not view["retryable"] and view["next_action"] == "build_new_version"
    assert publication.request(d["vid"], d["caller"], d["selection"])["run_id"] == d["build_run_id"]
