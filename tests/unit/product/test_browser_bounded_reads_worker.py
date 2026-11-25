"""Updated-runtime v1 read capability controls; synthetic replies are not model evaluation."""
import threading
import time

import pytest

from desktop_runtime import device_worker
from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
from tests.unit.product.test_cu_owned_browser_adapter import (
    Authority, Tracker, V2Broker, V2Browser, actor, gateway, profile,
)


class BoundedBrowser(V2Browser):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.bounded_reads = kwargs.get("bounded_reads", False)


@pytest.mark.parametrize("action", [actor("navigate", text="https://owned.example/two"),
    actor("scroll", direction="down", amount=1), actor("extract", text="Price")])
@pytest.mark.parametrize("marked", [False, True])
def test_v1_explicit_runtime_marker_enables_only_existing_bounded_reads(action, marked):
    browser = BoundedBrowser(bounded_reads=marked)
    broker = V2Broker([{"subgoals": ["Read"]}, action])
    value = CuOwnedBrowserAdapter(browser=browser, gateway=broker, profile=profile(), goal=browser.goal,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    observation = browser.observe()
    if marked:
        proposal = value.decide(observation)
        value.before_dispatch(proposal, observation)
        assert proposal.candidate.operation == action["actions"][0]["op"]
        assert all(kwargs["context"]["bounded_reads"] is True for _, _, kwargs in broker.calls)
        assert all(kwargs["context"]["links"] for _, _, kwargs in broker.calls)
    else:
        with pytest.raises(PermissionError, match="action_unsupported"):
            value.decide(observation)
        assert all("bounded_reads" not in kwargs["context"] and "links" not in kwargs["context"]
            for _, _, kwargs in broker.calls)
    assert browser.deliveries == []


@pytest.mark.parametrize("action", [actor("hover", element_id="c0"), actor("new_tab", text="https://owned.example/two"),
    actor("switch_tab", element_id="t" + "a" * 32)])
def test_v1_bounded_read_marker_cannot_borrow_fixture_operations(action):
    browser, broker = BoundedBrowser(bounded_reads=True), V2Broker([{"subgoals": ["Read"]}, action])
    value = CuOwnedBrowserAdapter(browser=browser, gateway=broker, profile=profile(), goal=browser.goal,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    with pytest.raises(PermissionError, match="action_unsupported"):
        value.decide(browser.observe())
    assert not browser.deliveries and not value.pending_dispatch


@pytest.mark.parametrize("action", [actor("click", element_id="c0"), actor("type", element_id="c0", text="Ada")])
def test_v1_marked_read_only_runtime_still_refuses_click_and_type(action):
    browser = BoundedBrowser(bounded_reads=True, read_only=True)
    broker = V2Broker([{"subgoals": ["Read"]}, action])
    value = CuOwnedBrowserAdapter(browser=browser, gateway=broker, profile=profile(), goal=browser.goal,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    with pytest.raises(PermissionError, match="action_unsupported"):
        value.decide(browser.observe())
    assert browser.deliveries == []


@pytest.mark.parametrize("action", [actor("navigate", text="https://owned.example/two"),
    actor("scroll", direction="down", amount=1), actor("extract", text="Price")])
def test_worker_marks_updated_v1_runtime_and_dispatches_source_read_once(monkeypatch, action):
    authority = Authority([{"subgoals": ["Read"]}, action, actor("done", summary="Current price $23."),
        {"passed": True, "reason": "Observed source"}])
    authority.broker = V2Broker(authority.broker.replies)
    browsers = []
    def factory(**kwargs):
        browser = BoundedBrowser(**kwargs)
        browsers.append(browser)
        return browser
    monkeypatch.setattr(device_worker, "OwnedBrowser", factory)
    monkeypatch.setattr(device_worker, "OwnedProcesses", Tracker)
    worker = device_worker.DeviceWorker(gateway(authority), lambda _: None)
    worker._notify_terminal = lambda *args, **kwargs: None
    worker.run()
    assert browsers[0].bounded_reads is True and browsers[0].kwargs["bounded_reads"] is True
    assert browsers[0].closed and len(browsers[0].deliveries) == 1
    assert all(kwargs["context"]["bounded_reads"] is True for _, _, kwargs in authority.broker.calls)
    assert worker.last["evidence"]["pending_dispatch"] is False and worker.last["task_verified"] is False
