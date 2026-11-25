import hashlib
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from coding_agents.cu_ai_loop import Action
from desktop_runtime.agent_desktop import (
    AgentDesktopCli,
    AgentDesktopMac,
    parse_response,
)
from desktop_runtime.cu_ai_cua import CuaError
from desktop_runtime.cu_ai_engine import NeedsHuman

DOMAINS = [
    ("Procurement", "Record the supplier assessment", "Supplier assessment"),
    ("Recipe journal", "Record the dinner recipe", "Dinner recipe"),
]


def envelope(command, data):
    return {"version": "2.4", "ok": True, "command": command, "data": data}


def snapshot(label, generation=1, value="", **node_changes):
    node = {"ref_id": f"@s{generation:02d}:e1", "role": "textfield", "name": label,
            "value": value, "native_id": "entry", "states": [],
            "available_actions": ["click", "type", "clear"], "children": [], **node_changes}
    return envelope("snapshot", {"window": {"id": "w-42", "title": "Document"},
                                 "snapshot_id": f"s{generation:02d}", "ref_count": 1, "complete": True, "tree": node})


def backend(app, goal, responses, *, authorized=lambda: True):
    calls = []
    scope = SimpleNamespace(pid=11, window=42, extra=(), modal_parents=(),
                            live=lambda: SimpleNamespace(pid=11, name=app), admit=lambda *args: None)

    def call(arguments):
        calls.append(arguments)
        if arguments[0] == "list-windows" and (not responses or responses[0].get("command") != "list-windows"):
            contract = json.loads((Path(__file__).parents[1] / "fixtures/agent_desktop_v0_10_1_contract.json").read_text())
            windows = contract["macos_contract_examples"]["windows"]
            for row in windows["data"]:
                row["app_name"] = app
            return windows
        response = responses.pop(0)
        if response.get("command") == "snapshot" and response.get("ok"):
            response["data"].setdefault("app", app)
        return response

    native = AgentDesktopMac(scope, goal, call, authorized=authorized)
    native.set_target(11, app)
    return native, calls


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_snapshot_click_type_verify(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label),
        envelope("click", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 2), envelope("type", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 3, "Recorded"), envelope("get", {"property": "value", "ref": "@s03:e1", "value": "Recorded"})])
    observation = native.observe()
    assert native.act(Action("click", element_id=observation.elements[0]["id"]), observation) == "applied"
    observation = native._observation
    assert native.act(Action("type", element_id=observation.elements[0]["id"], text="Recorded"), observation) == "applied"
    assert native._observation.elements[0]["value"] == "Recorded"
    assert [arguments[0] for arguments in calls] == ["list-windows", "snapshot", "click", "list-windows", "snapshot",
                                                  "type", "list-windows", "snapshot", "get"]
    assert calls[1][calls[1].index("--window-id") + 1] == "w-42"


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_stale_ref_reobserves_and_rechecks_authority(app, goal, label):
    authorizations = []
    stale = {"version": "2.4", "ok": False, "command": "click", "error": {
        "code": "STALE_REF", "message": "private", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
    native, calls = backend(app, goal, [snapshot(label), stale, snapshot(label, 2),
        envelope("click", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}), snapshot(label, 3)],
        authorized=lambda: authorizations.append(True) or True)
    observation = native.observe()
    assert native.act(Action("click", element_id="@s01:e1"), observation) == "applied"
    assert calls[5][1] == "@s02:e1"
    assert len(authorizations) == 9


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_stale_retry_cannot_bypass_changed_safety(app, goal, label):
    stale = {"version": "2.4", "ok": False, "command": "type", "error": {
        "code": "STALE_REF", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
    native, calls = backend(app, goal, [snapshot(label), stale, snapshot(label, 2, states=["secure"])])
    observation = native.observe()
    with pytest.raises(NeedsHuman) as failure:
        native.act(Action("type", element_id="@s01:e1", text="Recorded"), observation)
    assert failure.value.dispatch_state == "not_dispatched"
    assert [row[0] for row in calls] == ["list-windows", "snapshot", "type", "list-windows", "snapshot"]


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_safety_and_revoked_grant_refuse_before_input(app, goal, label):
    for allowed, node in [(False, {}), (True, {"states": ["secure"]})]:
        authority = {"current": True}
        native, calls = backend(app, goal, [snapshot(label, **node)],
                                authorized=lambda authority=authority: authority["current"])
        observation = native.observe()
        authority["current"] = allowed
        with pytest.raises(NeedsHuman) as failure:
            native.act(Action("type", element_id="@s01:e1", text="Recorded"), observation)
        assert failure.value.dispatch_state == "not_dispatched"
        assert len(calls) == 2


@pytest.mark.parametrize("code,delivery,retry", [
    ("PERM_DENIED", "not_delivered", "safe"),
    ("AMBIGUOUS_TARGET", "not_delivered", "safe"),
    ("STALE_REF", "delivery_uncertain", "unsafe"),
    ("STALE_REF", "unknown", "unknown"),
])
@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_error_contract_never_replays_uncertain_input(app, goal, label, code, delivery, retry):
    native, calls = backend(app, goal, [snapshot(label), {"version": "2.4", "ok": False,
        "command": "click", "error": {"code": code, "message": "Private account data",
            "disposition": {"delivery": delivery, "retry": retry}}}])
    observation = native.observe()
    with pytest.raises(NeedsHuman) as failure:
        native.act(Action("click", element_id="@s01:e1"), observation)
    assert failure.value.dispatch_state == ("not_dispatched" if delivery == "not_delivered" else "unconfirmed")
    assert "Private" not in str(failure.value.detail)
    if code == "AMBIGUOUS_TARGET":
        assert "Choose" in failure.value.detail
    assert len(calls) == 3


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_incomplete_or_wrong_window_snapshot_is_unusable(app, goal, label):
    for changes in [{"complete": False}, {"window": {"id": "w-43", "title": "Private"}}, {"app": "Other app"}]:
        response = snapshot(label)
        response["data"].update(changes)
        native, calls = backend(app, goal, [response])
        with pytest.raises(CuaError):
            native.observe()
        assert len(calls) == 2


def test_missing_binary_and_wrong_checksum_fail_before_spawn(tmp_path):
    with pytest.raises(CuaError):
        AgentDesktopCli(tmp_path / "agent-desktop")
    binary = tmp_path / "agent-desktop"
    binary.write_bytes(b"untrusted executable")
    with pytest.raises(CuaError):
        AgentDesktopCli(binary)


def test_stdin_host_keeps_private_text_out_of_process_arguments(tmp_path, monkeypatch):
    from desktop_runtime import agent_desktop
    binary = tmp_path / "agent-desktop"
    binary.write_bytes(b"owned test binary")
    helper = tmp_path / "agent-desktop-macos-helper"
    helper.write_bytes(b"owned test helper")
    monkeypatch.setattr(agent_desktop, "BINARY_HASHES", {
        "arm64": {path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in [binary, helper]}})
    monkeypatch.setattr(agent_desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(agent_desktop.sys, "platform", "darwin")
    requests, spawned = [], []

    class Input:
        def write(self, value):
            requests.append(json.loads(value))

        def flush(self):
            pass

    class Output:
        def readline(self, limit):
            command = requests[-1][0]
            data = {"version": "0.10.1"} if command == "version" else {}
            return (json.dumps(envelope(command, data)) + "\n").encode()

    class Process:
        stdin, stdout = Input(), Output()

        def poll(self):
            return 0

        def wait(self, timeout):
            return 0

    monkeypatch.setattr(agent_desktop.subprocess, "Popen", lambda arguments, **kwargs:
                        spawned.append((arguments, kwargs)) or Process())
    client = AgentDesktopCli(binary)
    client(["type", "@s01:e1", "Private text"])
    before = list(requests)
    with pytest.raises(NeedsHuman) as failure:
        client(["type", "@s01:e1", "sk-" + "a7" * 16])
    assert failure.value.dispatch_state == "not_dispatched"
    assert requests == before
    client.close()
    assert spawned[0][0] == [str(binary)]
    assert spawned[0][1]["stderr"] == agent_desktop.subprocess.DEVNULL
    assert requests[-1] == ["type", "@s01:e1", "Private text"]


@pytest.mark.parametrize("response", [
    {"version": "2.3", "ok": True, "command": "snapshot", "data": {}},
    {"version": "2.4", "ok": "true", "command": "snapshot", "data": {}},
    {"version": "2.4", "ok": True, "command": "type", "data": {}},
])
def test_invalid_envelope_is_rejected(response):
    with pytest.raises(CuaError):
        parse_response(response, "snapshot")


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_real_pinned_ffi_envelope_refuses_without_input(app, goal, label):
    contract = json.loads((Path(__file__).parents[1] / "fixtures/agent_desktop_v0_10_1_contract.json").read_text())
    assert parse_response(contract["version"], "version")["data"]["version"] == "0.10.1"
    assert parse_response(contract["snapshot"], "snapshot")["error"]["code"] == "PLATFORM_NOT_SUPPORTED"
    native, calls = backend(app, goal, [contract["snapshot"]])
    with pytest.raises(CuaError):
        native.observe()
    assert len(calls) == 2


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_worker_flag_off_retains_current_backend_and_flag_on_selects_adapter(app, goal, label, monkeypatch):
    from desktop_runtime import agent_desktop, cua_session
    from desktop_runtime.cu_ai_cua import CuaMac
    from desktop_runtime.device_worker import DeviceWorker

    worker = DeviceWorker.__new__(DeviceWorker)
    worker.cancelled = threading.Event()
    worker._ai_scope = SimpleNamespace(pid=11, window=42, pairs=((11, 42),), extra=(), modal_parents=(),
        live=lambda: SimpleNamespace(pid=11, name=app), caller=lambda call, **kwargs: call)
    worker._ai_goal, worker.cua_socket = goal, "/private/tmp/supymem-cua-test/driver.sock"
    selected = []
    monkeypatch.setattr(cua_session, "CuaSession", lambda *args, **kwargs: SimpleNamespace(close=lambda: None))
    monkeypatch.setattr(agent_desktop, "AgentDesktopCli", lambda path: selected.append(path) or (lambda args: None))
    monkeypatch.delenv("SUPYMEM_AGENT_DESKTOP_ENABLED", raising=False)
    assert type(worker._ai_backend()) is CuaMac
    assert selected == []
    monkeypatch.setenv("SUPYMEM_AGENT_DESKTOP_ENABLED", "1")
    assert isinstance(worker._ai_backend(), AgentDesktopMac)
    assert len(selected) == 1
    source = (Path(__file__).parents[2] / "src-tauri/src/supervisor.rs").read_text()
    environment = source.split("fn apply_bridge_env(", 1)[1].split("\nfn ", 1)[0]
    assert 'if std::env::var("SUPYMEM_AGENT_DESKTOP_ENABLED").as_deref() == Ok("1")' in environment
    assert 'cmd.env("SUPYMEM_AGENT_DESKTOP_ENABLED", "1")' in environment
    assert "cmd.env_clear()" in environment


def test_binary_path_itself_must_be_verified(tmp_path, monkeypatch):
    from desktop_runtime import agent_desktop
    binary, helper = tmp_path / "agent-desktop", tmp_path / "agent-desktop-macos-helper"
    for path in (binary, helper):
        path.write_bytes(b"owned bytes")
    other = tmp_path / "other"
    other.write_bytes(b"untrusted executable")
    monkeypatch.setattr(agent_desktop, "BINARY_HASHES", {"arm64": {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in (binary, helper)}})
    monkeypatch.setattr(agent_desktop.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(agent_desktop.sys, "platform", "darwin")
    spawned = []
    monkeypatch.setattr(agent_desktop.subprocess, "Popen", lambda *args, **kwargs: spawned.append(args))
    with pytest.raises(CuaError):
        AgentDesktopCli(other)
    assert spawned == []


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_field_replacement_keeps_current_executor_semantics(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label, value="Existing content"),
        envelope("clear", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 2), envelope("get", {"property": "value", "ref": "@s02:e1", "value": ""}),
        envelope("type", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 3, "Updated content"), envelope("get", {"property": "value", "ref": "@s03:e1", "value": "Updated content"})])
    observation = native.observe()
    assert native.act(Action("type", element_id="@s01:e1", text="Updated content"), observation) == "applied"
    assert calls[2] == ["clear", "@s01:e1", "--timeout-ms", "0"]
    assert calls[6][:3] == ["type", "@s02:e1", "Updated content"]
    assert native._observation.elements[0]["value"] == "Updated content"


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_unverifiable_text_target_is_refused_before_input(app, goal, label):
    for text, changes in [("First\nSecond", {}), ("Updated", {"native_id": None}),
                          ("Updated", {"states": ["readonly"]}), ("Updated", {"value": None})]:
        native, calls = backend(app, goal, [snapshot(label, **changes)])
        observation = native.observe()
        with pytest.raises(NeedsHuman) as failure:
            native.act(Action("type", element_id="@s01:e1", text=text), observation)
        assert failure.value.dispatch_state == "not_dispatched"
        assert len(calls) == 2


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_revoked_grant_refuses_snapshot_before_reading_private_data(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label)], authorized=lambda: False)
    with pytest.raises(NeedsHuman) as failure:
        native.observe()
    assert failure.value.dispatch_state == "not_dispatched"
    assert calls == []


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_revocation_after_acknowledgment_does_not_make_input_retryable(app, goal, label):
    authority = iter([True, True, True, True, False])
    native, calls = backend(app, goal, [snapshot(label),
        envelope("click", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 2)], authorized=lambda: next(authority))
    observation = native.observe()
    with pytest.raises(NeedsHuman) as failure:
        native.act(Action("click", element_id="@s01:e1"), observation)
    assert failure.value.dispatch_state == "unconfirmed"
    assert len(calls) == 3


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_secure_snapshot_is_redacted_and_not_logged(app, goal, label, caplog):
    native, calls = backend(app, goal, [snapshot(label, value="Private field value", states=["secure", "focused"])])
    observation = native.observe()
    assert observation.secure_focus is True
    assert observation.elements[0]["value"] is None
    assert native.raw[0]["value"] is None
    assert "Private field value" not in caplog.text
    assert label not in caplog.text
    assert len(calls) == 2


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_invalid_snapshot_handle_cannot_be_used(app, goal, label):
    for snapshot_id, ref in [("../private", "@../private:e1"),
                            ("snapshot", "@snapshot:e0"),
                            ("snapshot", "@snapshot:e4294967296")]:
        response = snapshot(label)
        response["data"]["snapshot_id"] = snapshot_id
        response["data"]["tree"]["ref_id"] = ref
        native, calls = backend(app, goal, [response])
        with pytest.raises(CuaError):
            native.observe()
        assert len(calls) == 2


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_secret_text_is_refused_before_transport_or_field_clear(app, goal, label):
    for text in ("PasswordWith7Digits", "Bearer " + "a1" * 16,
                 "sk-" + "a7" * 16, " ".join(["4111"] * 4)):
        native, calls = backend(app, goal, [snapshot(label, value="Existing content")])
        observation = native.observe()
        before = list(calls)
        with pytest.raises(NeedsHuman) as failure:
            native.act(Action("type", element_id="@s01:e1", text=text), observation)
        assert failure.value.dispatch_state == "not_dispatched"
        assert failure.value.detail == "I cannot enter passwords, tokens, keys or card numbers. Enter that information yourself."
        assert calls == before


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_window_selection_uses_reviewed_id_not_ambiguous_app_name(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label)])
    native.observe()
    assert calls[0] == ["list-windows"]
    assert "--app" not in calls[-1]
    assert calls[-1][calls[-1].index("--window-id") + 1] == "w-42"


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_ambiguous_snapshot_asks_without_switching_granted_window(app, goal, label):
    refusal = {"version": "2.4", "ok": False, "command": "snapshot",
               "error": {"code": "AMBIGUOUS_TARGET", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
    native, calls = backend(app, goal, [refusal])
    with pytest.raises(NeedsHuman) as failure:
        native.observe()
    assert failure.value.dispatch_state == "not_dispatched"
    assert "Choose" in failure.value.detail
    assert all(arguments[0] in {"snapshot", "list-windows"} for arguments in calls)


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_pinned_type_uses_positional_text_then_fresh_get_value(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label),
        envelope("type", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 2, "Recorded"),
        envelope("get", {"property": "value", "ref": "@s02:e1", "value": "Recorded"})])
    observation = native.observe()
    assert native.act(Action("type", element_id="@s01:e1", text="Recorded"), observation) == "applied"
    assert next(arguments for arguments in calls if arguments[0] == "type")[:3] == ["type", "@s01:e1", "Recorded"]
    assert calls[-1] == ["get", "@s02:e1", "--property", "value"]
    assert all("--stdin" not in arguments for arguments in calls)


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_headless_key_receipt_cannot_authorize_or_prove_sheet_change(app, goal, label):
    native, calls = backend(app, goal, [snapshot(label)])
    observation = native.observe()
    before = list(calls)
    with pytest.raises(CuaError) as failure:
        native.act(Action("key", chord="escape"), observation)
    assert failure.value.no_effect is True
    assert calls == before


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_clear_then_refusal_remains_partial_and_is_not_replayed(app, goal, label):
    for revoked in (False, True):
        authority = iter([True] * 7 + [False]) if revoked else None
        refusal = {"version": "2.4", "ok": False, "command": "type", "error": {
            "code": "PERM_DENIED", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
        native, calls = backend(app, goal, [snapshot(label, value="Existing content"),
            envelope("clear", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
            snapshot(label, 2), envelope("get", {"property": "value", "ref": "@s02:e1", "value": ""}), refusal],
            authorized=lambda authority=authority: next(authority) if authority is not None else True)
        observation = native.observe()
        with pytest.raises(NeedsHuman) as failure:
            native.act(Action("type", element_id="@s01:e1", text="Updated content"), observation)
        assert failure.value.dispatch_state == "unconfirmed"
        assert native._observation.elements[0]["value"] == ""
        assert [arguments[0] for arguments in calls].count("clear") == 1
        assert [arguments[0] for arguments in calls].count("type") == (0 if revoked else 1)


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_get_readback_must_match_fresh_ref_property_and_exact_text(app, goal, label):
    for changes in ({"value": "Different content"}, {"ref": "@s01:e1"}, {"property": "text"}):
        native, calls = backend(app, goal, [snapshot(label),
            envelope("type", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
            snapshot(label, 2, "Recorded"),
            envelope("get", {"property": "value", "ref": "@s02:e1", "value": "Recorded", **changes})])
        observation = native.observe()
        with pytest.raises(NeedsHuman) as failure:
            native.act(Action("type", element_id="@s01:e1", text="Recorded"), observation)
        assert failure.value.dispatch_state == "unconfirmed"
        assert calls[-1] == ["get", "@s02:e1", "--property", "value"]
        assert [arguments[0] for arguments in calls].count("type") == 1


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_focused_other_instance_cannot_expand_the_window_grant(app, goal, label):
    native, calls = backend(app, goal, [envelope("list-windows", [
        {"id": "w-43", "pid": 12, "app_name": app, "is_focused": True}])])
    with pytest.raises(NeedsHuman) as failure:
        native.observe()
    assert failure.value.dispatch_state == "not_dispatched"
    assert "Choose" in failure.value.detail
    assert calls == [["list-windows"]]


@pytest.mark.parametrize("app,goal,label", DOMAINS)
def test_stale_type_cannot_append_to_concurrently_changed_field(app, goal, label):
    stale = {"version": "2.4", "ok": False, "command": "type", "error": {
        "code": "STALE_REF", "disposition": {"delivery": "not_delivered", "retry": "safe"}}}
    native, calls = backend(app, goal, [snapshot(label), stale, snapshot(label, 2, "Concurrent content"),
        envelope("type", {"disposition": {"delivery": "delivered_unverified", "retry": "unsafe"}}),
        snapshot(label, 3, "Concurrent contentRecorded")])
    observation = native.observe()
    with pytest.raises(NeedsHuman) as failure:
        native.act(Action("type", element_id="@s01:e1", text="Recorded"), observation)
    assert failure.value.dispatch_state == "not_dispatched"
    assert [arguments[0] for arguments in calls].count("type") == 1
    assert [arguments[0] for arguments in calls].count("clear") == 0
