"""Read-only host/proxy/worker parity; census is never new-window authority."""
from pathlib import Path

import pytest

from desktop_runtime import cua_driver, cua_session
from desktop_runtime.peekaboo_driver import AppChoice
from tests.unit.product.test_cua_driver import FakeSession


class CensusSession(FakeSession):
    def __init__(self):
        super().__init__("", "")
        self.rows = [{"pid": 5678, "window_id": 99, "title": "PRIVATE OTHER WINDOW"}]
        self.complete = False

    def call(self, name, arguments):
        if name == "list_windows":
            self.calls.append((name, arguments))
            from types import SimpleNamespace
            return SimpleNamespace(isError=False, structuredContent={"windows": self.rows}, content=[])
        result = super().call(name, arguments)
        if name == "get_window_state":
            result.structuredContent.update(elements_complete=self.complete,
                window_bounds={"x": 10, "y": 20, "width": 1, "height": 1},
                screenshot_scale=1.0, screenshot_frame_valid=True)
        return result


def make_driver(monkeypatch, bundle="com.example.Editor"):
    session = CensusSession()
    monkeypatch.setattr(cua_driver, "CuaSession", lambda *_, **kwargs: session)
    driver = cua_driver.CuaWindowDriver(app=AppChoice("App", bundle, 5678, "start-id"),
        window_id=99, socket="/private/tmp/supymem-cua-test/driver.sock", host_pid=1234,
        observation_protocol=cua_session.OBSERVATION_PROTOCOL)
    return driver, session


@pytest.mark.parametrize("bundle", ["com.example.Editor", "com.google.Chrome", "com.apple.finder"])
def test_same_generic_observation_contract_never_adopts_new_window(monkeypatch, bundle):
    driver, session = make_driver(monkeypatch, bundle)
    try:
        first = driver.capture()
        assert first["perception"]["elements_complete"] is False
        assert first["perception"]["inventory_changed"] is False
        assert first["perception"]["screenshot_pixels"] == [1, 1]
        assert first["perception"]["window_bounds"]["x"] == 10
        session.rows.append({"pid": 5678, "window_id": 100, "title": "PRIVATE DRAFT"})
        session.complete = True
        second = driver.capture()
        assert second["perception"]["inventory_changed"] is True
        assert second["perception"]["resource_generation"] == 0
        assert second["perception"]["owner_window_count"] == 2
        assert second["perception"]["elements_complete"] is True
        assert driver.window_id == second["window_id"] == 99
        assert "PRIVATE" not in repr(second)
        assert [a for name, a in session.calls if name == "list_windows"] == [
            {"pid": 5678, "on_screen_only": False}] * 2
        assert not any(name in {"invoke_menu", "hotkey", "click"} for name, _ in session.calls)
    finally:
        driver.close()


@pytest.mark.parametrize("rows,code", [
    ([{"pid": 222, "window_id": 100}], "invalid"),
    ([{"pid": 5678, "window_id": True}], "invalid"),
    ([{"pid": 5678, "window_id": 100}], "unpinned"),
    ([{"pid": 5678, "window_id": 99}] * 2, "unpinned"),
    ([{"pid": 5678, "window_id": 99}] * 129, "invalid"),
])
def test_invalid_or_foreign_inventory_revokes_capture(monkeypatch, rows, code):
    driver, session = make_driver(monkeypatch)
    try:
        previous = driver.capture()["snapshot_id"]
        session.rows = rows
        with pytest.raises(cua_driver.CuaDriverUnavailable, match="desktop_cua_inventory_" + code):
            driver.capture()
        assert driver._snapshot_id is None
        assert not driver.snapshot_ready_for_admission(previous)
    finally:
        driver.close()


def test_unknown_protocol_refused_before_socket_or_host_access():
    with pytest.raises(cua_session.CuaSessionError, match="protocol_unsupported"):
        cua_session.CuaSession("not-a-binary", "not-a-socket", observation_protocol="all_windows")
    with pytest.raises(cua_driver.CuaDriverUnavailable, match="protocol_unsupported"):
        cua_driver.CuaWindowDriver(app=AppChoice("App", "com.example.App", 123, "start"),
            window_id=1, socket="no", host_pid=2, observation_protocol="unrestricted")


def test_host_bridge_protocol_parity_and_no_menu_authority():
    root = Path(__file__).resolve().parents[3]
    host = (root / "src-tauri/src/cua_host.rs").read_text().split("#[cfg(test)]")[0]
    transport = (root / "src-tauri/src/lib.rs").read_text()
    assert cua_session.OBSERVATION_PROTOCOL in host
    assert '"x-supymem-cua-protocol", cua_host::OBSERVATION_PROTOCOL' in transport
    assert "    - list_windows\\n" in host
    assert "    - invoke_menu\\n" not in host
    assert "invoke_menu" not in cua_session.ALLOWED | cua_session.READ_ONLY_LIFECYCLE
    assert "all_windows" not in host


def test_worker_consumes_protocol_and_partial_inventory_without_granting_effect(monkeypatch):
    from tests.unit.product.test_device_worker_long_tasks import _setup, _run
    from coding_agents.jev_decision import Candidate
    worker, effects, observations = _run(monkeypatch, _setup(monkeypatch),
        lambda o, _: Candidate("blocked", None, o.digest()), goal="Inspect the selected app",
        protocol=cua_session.OBSERVATION_PROTOCOL,
        perception={"elements_complete": False, "inventory_changed": True})
    assert effects == []
    assert worker.last["task_verified"] is False
    assert "Observation completeness: False" in observations[0].context
    assert "inventory changed: True" in observations[0].context
    assert "grants no new window" in observations[0].context


@pytest.mark.parametrize("arguments", [{}, {"pid": 8}, {"pid": 5678, "on_screen_only": False, "extra": True}])
def test_proxy_refuses_unfiltered_or_foreign_census_before_transport(arguments):
    session = object.__new__(cua_session.CuaSession)
    session.observation_pid = 5678
    with pytest.raises(cua_session.CuaSessionError, match="scope_invalid"):
        session.call("list_windows", arguments)
