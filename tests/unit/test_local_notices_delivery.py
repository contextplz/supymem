"""Local Mac-seat notices: needs-you, finish, and channel-separated acks."""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from desktop_runtime import local_notices, notice_delivery


def setup_function():
    local_notices.reset_for_tests()
    notice_delivery.reset_for_tests()


def test_needs_you_and_finish_enqueue_with_dedupe():
    first = local_notices.push_needs_you(
        title="Supymem needs you",
        body="Continue the signup",
        target_path="/v/ven_1/device-work-orders/dwo_1",
        order_id="dwo_1",
    )
    again = local_notices.push_needs_you(
        title="Supymem needs you",
        body="Continue the signup",
        target_path="/v/ven_1/device-work-orders/dwo_1",
        order_id="dwo_1",
    )
    assert first and again is None
    finished = local_notices.push_task_finished(
        outcome="verified",
        task_title="Checked the venture workspace",
        target_path="/v/ven_1/device-work-orders/dwo_1",
        order_id="dwo_1",
    )
    assert finished
    pending = local_notices.pending()
    assert len(pending) == 2
    assert pending[0]["title"] == "Supymem needs you"
    assert pending[1]["title"] == "Mac task finished: Checked the venture workspace"
    dup_finish = local_notices.push_task_finished(
        outcome="verified",
        task_title="Checked the venture workspace",
        target_path="/v/ven_1/device-work-orders/dwo_1",
        order_id="dwo_1",
    )
    assert dup_finish is None


def test_coding_review_notice_does_not_call_unverified_changes_a_failure():
    local_notices.push_task_finished(
        outcome="review_required", task_title="Build the application",
        target_path="/v/ven_1/device-work-orders/dwo_code", order_id="dwo_code",
    )
    notice = local_notices.pending()[0]
    assert notice["title"] == "Review coding changes: Build the application"
    assert notice["kind"] == "device_task_review_required"
    assert "before accepting" in notice["body"]


def test_visual_result_notice_offers_review_without_claiming_success_or_failure():
    local_notices.push_task_finished(
        outcome="visual_review_required", task_title="Read Calculator",
        target_path="/v/ven_1/device-work-orders/dwo_visual", order_id="dwo_visual",
    )
    notice = local_notices.pending()[0]
    assert notice["title"] == "Mac task result ready to review: Read Calculator"
    assert notice["kind"] == "device_task_review_required"
    assert "not independently verified" in notice["body"]


def test_worker_reported_result_queues_review_notice_without_changing_unknown_receipt(monkeypatch):
    from desktop_runtime.device_worker import DeviceWorker

    def forbidden(*_args, **_kwargs):
        raise AssertionError("A local finish notice must not dispatch or save task work")

    gateway = SimpleNamespace(
        claim_attempted=True, order_id="dwo_review", venture_id="ven_review",
        account_id="usr_review", read=forbidden,
    )
    worker = DeviceWorker(gateway, forbidden)
    monkeypatch.setattr("desktop_runtime.device_worker.report_error", forbidden)
    worker.last = {
        "tool": "general_browser", "state": "stopped", "semantic_satisfied": True,
        "result_summary": "The owned page reports three features.", "task_verified": False,
        "credits_reconciled": False, "effect_outcome_unconfirmed": True,
    }
    worker.current_effect = ("inv_review", "click")
    receipt = copy.deepcopy(worker.last)
    effect = worker.current_effect
    def finish():
        worker._notify_terminal("outcome_review_required", manifest={"title": "Read owned page"})
        return "reported_result"

    monkeypatch.setattr(worker, "_run", finish)
    assert worker._run_with_reporting() == "reported_result"
    assert worker._run_with_reporting() == "reported_result"

    notices = local_notices.pending(actor_id="usr_review")
    assert len(notices) == 1
    notice = notices[0]
    assert notice["kind"] == "device_task_review_required"
    assert notice["title"] == "Mac task result ready to review: Read owned page"
    assert notice["body"] == (
        "Supymem reported a result. Review its answer and the source; "
        "the task is not independently verified."
    )
    assert notice["target_path"] == "/v/ven_review/device-work-orders/dwo_review"
    assert local_notices.pending(actor_id="usr_other") == []
    assert worker.last == receipt
    assert worker.current_effect == effect


@pytest.mark.parametrize("outcome, kind, title", [
    ("verified", "device_task_finished", "Mac task finished: Owned task"),
    ("changes_require_acceptance_verification", "device_task_review_required", "Review coding changes: Owned task"),
    ("visual_result_review_required", "device_task_review_required", "Mac task result ready to review: Owned task"),
    ("cancelled", "device_task_cancelled", "Mac task cancelled: Owned task"),
    ("cancelled_or_expired", "device_task_cancelled", "Mac task cancelled: Owned task"),
    ("attention_required", "device_task_failed", "Mac task failed: Owned task"),
])
def test_worker_finish_notice_preserves_existing_outcomes(outcome, kind, title):
    from desktop_runtime.device_worker import DeviceWorker

    gateway = SimpleNamespace(
        claim_attempted=True, order_id="dwo_notice", venture_id="ven_notice", account_id="usr_notice",
    )
    worker = DeviceWorker(gateway, lambda _receipt: None)
    worker._notify_terminal(outcome, manifest={"title": "Owned task"})
    notice = local_notices.pending(actor_id="usr_notice")[0]
    assert (notice["kind"], notice["title"]) == (kind, title)


def test_unclaimed_worker_does_not_queue_a_review_notice():
    from desktop_runtime.device_worker import DeviceWorker

    worker = DeviceWorker(SimpleNamespace(claim_attempted=False, order_id="dwo_unclaimed"), lambda _receipt: None)
    worker._notify_terminal("outcome_review_required", manifest={"title": "No claimed task"})
    assert local_notices.pending() == []


@pytest.mark.parametrize("domain", ["Manufacturing maintenance schedules", "Family reading diary"])
@pytest.mark.parametrize("outcome, cancelled, error_count", [
    ("attention_required", False, 1),
    ("cancelled", True, 0),
])
def test_failed_decision_reports_closed_code_once_but_cancel_does_not(monkeypatch, domain, outcome, cancelled, error_count):
    from desktop_runtime.device_worker import DeviceWorker

    reports = []
    monkeypatch.setattr("desktop_runtime.device_worker.report_error", lambda *args, **kwargs: reports.append((args, kwargs)))
    gateway = SimpleNamespace(
        claim_attempted=True, order_id="dwo_fault", venture_id="ven_fault", account_id="usr_fault",
    )
    worker = DeviceWorker(gateway, lambda _receipt: None)
    worker.last = {"task_verified": False, "credits_reconciled": False,
                   "failure_code": "provider_outcome_unknown", "reason_code": "cu_browser_decision_unavailable",
                   "exception_message": domain + " private data"}
    receipt = copy.deepcopy(worker.last)
    if cancelled:
        worker.cancelled.set()

    def finish():
        worker._notify_terminal(outcome, manifest={"title": "Owned task"})

    monkeypatch.setattr(worker, "_run", finish)
    worker._run_with_reporting()
    assert len(reports) == error_count
    if reports:
        args, details = reports[0]
        assert args == ('device_worker', 'run_mac_task')
        assert set(details) == {"code", "label", "runtime_diagnostic", "exception_details"}
        assert details["exception_details"] == {}
        assert details["code"] == "cu_browser_decision_unavailable"
        assert details["label"] == "Mac native_failure"
        diagnostic = details["runtime_diagnostic"]
        assert set(diagnostic) == {"phase", "reason", "phase_ms", "elapsed_ms"}
        assert diagnostic["phase"] == "starting"
        assert diagnostic["reason"] == details["code"]
        assert all(type(diagnostic[key]) is int and 0 <= diagnostic[key] <= 86_400_000
                   for key in ("phase_ms", "elapsed_ms"))
        assert diagnostic["phase_ms"] <= diagnostic["elapsed_ms"]
    notice = local_notices.pending(actor_id="usr_fault")[0]
    assert notice["kind"] == ("device_task_cancelled" if cancelled else "device_task_failed")
    assert worker.last == receipt


def test_in_app_ack_does_not_clear_native_failure():
    nid = local_notices.push(
        title="Supymem needs you",
        body="Open the order",
        target_path="/v/ven_1/device-work-orders/dwo_9",
        kind="device_needs_you",
    )
    assert nid
    report = local_notices.record_results([{
        "id": nid,
        "in_app": "in_app_shown",
        "native": "native_failed:main_thread",
    }])
    assert report["settled"] == 1
    assert report["statuses"][0]["native_status"] == "native_failed:main_thread"
    assert local_notices.pending() == []


def test_in_app_alone_keeps_notice_pending_for_native():
    nid = local_notices.push(
        title="Mac task finished: Demo",
        body="Done",
        target_path="/v/ven_1/device-work-orders/dwo_2",
        kind="device_task_finished",
    )
    local_notices.record_results([{"id": nid, "in_app": "in_app_shown"}])
    pending = local_notices.pending()
    assert len(pending) == 1
    assert pending[0]["in_app_status"] == "in_app_shown"
    assert pending[0]["native_status"] is None


def test_remote_delivery_settles_only_when_both_channels_done():
    notice = {"id": "not_remote_1", "title": "Needs you", "body": "Approve"}
    annotated = notice_delivery.annotate(notice)
    assert annotated["in_app_status"] is None
    first = notice_delivery.record_results([{
        "id": "not_remote_1", "in_app": "in_app_shown",
    }])
    assert first["settled_ids"] == []
    assert notice_delivery.is_settled("not_remote_1") is False
    second = notice_delivery.record_results([{
        "id": "not_remote_1", "native": "native_shown",
    }])
    assert second["settled_ids"] == ["not_remote_1"]
    assert notice_delivery.is_settled("not_remote_1") is False  # cleared after settle


def test_local_notices_filter_exact_actor_and_do_not_cross_account_dedupe():
    local_notices.reset_for_tests()
    first = local_notices.push_needs_you(title="A private review", body="A owned artifact", order_id="same", actor_id="usr_a")
    second = local_notices.push_needs_you(title="B private review", body="B owned artifact", order_id="same", actor_id="usr_b")
    local_notices.push(title="Unknown owner", body="No recipient")
    assert first and second and first != second
    assert [row["id"] for row in local_notices.pending(actor_id="usr_a")] == [first]
    assert [row["id"] for row in local_notices.pending(actor_id="usr_b")] == [second]
    assert local_notices.pending(actor_id="usr_c") == []


def test_local_notice_ack_and_channel_results_keep_other_actor_receipts():
    a=local_notices.push(title="A",body="A",actor_id="usr_a")
    b=local_notices.push(title="B",body="B",actor_id="usr_b")
    assert local_notices.ack([a,b],actor_id="usr_b") == 1
    report=local_notices.record_results([{"id":a,"in_app":"in_app_shown","native":"native_shown"}],actor_id="usr_b")
    assert report["recorded"] == 0
    assert [row["id"] for row in local_notices.pending(actor_id="usr_a")] == [a]
