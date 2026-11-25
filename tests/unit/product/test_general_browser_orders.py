"""Unit coverage for general browser prepare, limits, and redaction helpers."""
from __future__ import annotations

import pytest
from desktop_runtime.device_worker import DeviceWorker
from pydantic import ValidationError

from tests.unit.product.test_computer_use_jobs import _client


def test_classify_general_browser_intent(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app.build import device_orders as orders
    assert orders.classify_device_chat_intent(
        "Go to resend.com and sign up for an account using my work email"
    ) == "general_browser"
    assert orders.classify_device_chat_intent(
        "Go to httpbin.org/forms/post and sign up using my work email"
    ) == "general_browser"
    assert orders.classify_device_chat_intent(
        "Go to resend.com and prepare the signup form without submitting or entering credentials"
    ) == "general_browser"
    assert orders.classify_device_chat_intent("Open resend.com") is None
    assert orders.classify_device_chat_intent(
        "Open the browser and check the live site page and waitlist."
    ) == "public_site"


def test_missing_work_email_is_requested_before_review(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app.admin.authn import CallerContext
    from app.build.device_orders import _general_browser_fill_values

    caller = CallerContext(sub="founder", email="", role="founder", path="/", method="POST",
                           exp=9999999999, nonce="test")
    with pytest.raises(ValueError, match="device_chat_email_required"):
        _general_browser_fill_values("Go to example.com and sign up using my work email", caller)
    assert _general_browser_fill_values(
        "Go to resend.com and prepare the signup form without submitting or entering credentials", caller
    ) == {}
    assert _general_browser_fill_values(
        "Go to example.com and sign up using test@example.com", caller
    ) == {"email": "test@example.com"}


def test_general_browser_manifest_rejects_start_outside_origins(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app.build.device_orders import Manifest
    with pytest.raises(ValidationError):
        Manifest(
            task_id="t1", title="Bad", instruction="x" * 8,
            applications=["com.google.Chrome"],
            origins=["https://resend.com"],
            actions=["browser.read", "browser.interact"],
            credit_ceiling_micro=1000, duration_seconds=60,
            general_browser_verification={
                "operation": "general_goal_v1",
                "start_url": "https://evil.example/",
                "allow_type": True,
                "fill_values": {},
            },
        )


def test_core_manifest_rejects_loopback_origin_and_start_url(tmp_path, monkeypatch):
    _client(tmp_path, monkeypatch)
    from app.build.device_orders import Manifest
    with pytest.raises(ValidationError):
        Manifest(
            task_id="t1", title="Loopback", instruction="x" * 8,
            applications=["com.google.Chrome"],
            origins=["http://127.0.0.1:8765"],
            actions=["browser.read", "browser.interact"],
            credit_ceiling_micro=1000, duration_seconds=60,
            general_browser_verification={
                "operation": "general_goal_v1",
                "start_url": "http://127.0.0.1:8765/signup.html",
                "allow_type": True,
                "fill_values": {"email": "a@example.com"},
            },
        )
    with pytest.raises(ValidationError):
        Manifest(
            task_id="t1", title="Loopback start", instruction="x" * 8,
            applications=["com.google.Chrome"],
            origins=["https://httpbin.org"],
            actions=["browser.read", "browser.interact"],
            credit_ceiling_micro=1000, duration_seconds=60,
            general_browser_verification={
                "operation": "general_goal_v1",
                "start_url": "http://127.0.0.1:9/",
                "allow_type": True,
                "fill_values": {},
            },
        )


def test_repeated_action_detection_in_worker_state():
    worker = DeviceWorker(type("G", (), {
        "order_id": "dwo", "venture_id": "v", "execution_id": None, "claim_attempted": False,
    })(), lambda receipt: None)
    worker._record_step(kind="effect", operation="click", target="c1")
    worker._record_step(kind="effect", operation="type", label="Email", typed=True,
                        value="secret-should-not-appear")
    dumped = str(worker.last["steps"])
    assert "secret-should-not-appear" not in dumped
    assert worker.last["steps"][-1]["typed"] is True
    assert "label" in worker.last["steps"][-1]


def test_browser_start_url_for_general_https():
    manifest = {
        "origins": ["https://httpbin.org"],
        "general_browser_verification": {
            "start_url": "https://httpbin.org/forms/post",
        },
    }
    assert DeviceWorker.browser_start_url(manifest) == (
        "https://httpbin.org/forms/post?supymem_check=1"
    )
