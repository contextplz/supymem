"""Kernel filter-count evidence must distinguish Firefox from inherited isolation."""
from types import SimpleNamespace

import pytest

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.pagebuilder import rendering  # noqa: E402


@pytest.mark.parametrize("domain", ["procurement", "gardening"])
def test_local_link_activation_blocks_network_and_nonpublic_routes(monkeypatch, domain):
    monkeypatch.setenv("SUPYMEM_MODE", "local")
    activated = []
    target = f"http://localhost:8000/public/waitlists/{domain}"
    link = SimpleNamespace(get_attribute=lambda name: target if name == "href" else "waitlist_cta",
        is_visible=lambda: True, is_enabled=lambda: True,
        evaluate=lambda *_: activated.append("intercept") or True,
        click=lambda **_: activated.append("click"))
    page = SimpleNamespace(locator=lambda _: SimpleNamespace(count=lambda: 1, nth=lambda _: link),
        goto=lambda *_args, **_kwargs: None, evaluate=lambda *_: True,
        keyboard=SimpleNamespace(press=lambda key: activated.append(key)))
    actions = rendering._link_actions(page, "http://localhost:9000")
    assert len(actions) == 2 and all(action["passed"] for action in actions)
    assert activated.count("intercept") >= 2 and "click" in activated and "Enter" in activated
    for target in ("http://localhost:8000/admin", f"http://outside.example/public/waitlists/{domain}"):
        activated.clear()
        assert all(not action["passed"] for action in rendering._link_actions(page, "http://localhost:9000"))
        assert not activated
    monkeypatch.delenv("SUPYMEM_MODE")
    target = f"http://localhost:8000/public/waitlists/{domain}"
    assert all(not action["passed"] for action in rendering._link_actions(page, "http://localhost:9000"))


@pytest.fixture
def processes(tmp_path, monkeypatch):
    monkeypatch.setattr(rendering.os, "getpid", lambda: 10)

    def process(pid, parent, *, filters=1, seccomp=2, command="python", name="fixture"):
        root = tmp_path / str(pid)
        root.mkdir(exist_ok=True)
        fields = f"Name:\t{name}\nPPid:\t{parent}\nSeccomp:\t{seccomp}\n"
        if filters is not None:
            fields += f"Seccomp_filters:\t{filters}\n"
        (root / "status").write_text(fields)
        (root / "cmdline").write_bytes(command.encode().replace(b" ", b"\0"))
        return root

    process(10, 1)
    process(11, 10, command="node playwright")
    process(12, 11, command="firefox -contentproc forkserver", name="forkserver")
    return tmp_path, process


def test_no_content_or_inherited_container_filter_is_not_sandbox_proof(processes):
    root, process = processes
    assert not rendering.firefox_content_sandbox_verified(root)
    process(13, 12, command="firefox -contentproc -isForBrowser tab")
    assert not rendering.firefox_content_sandbox_verified(root)
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    assert rendering.firefox_content_sandbox_verified(root)


@pytest.mark.parametrize("field,value", [("filters", None), ("filters", 0), ("filters", 1), ("seccomp", 0)])
def test_every_content_descendant_requires_an_additional_filter(processes, field, value):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    process(14, 12, **{field: value}, command="firefox -contentproc rdd")
    assert not rendering.firefox_content_sandbox_verified(root)


@pytest.mark.parametrize("identity,filename", [(10, "status"), (12, "status"), (13, "status"), (13, "cmdline")])
def test_missing_kernel_evidence_rejects_the_browser(processes, identity, filename):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    (root / str(identity) / filename).unlink()
    assert not rendering.firefox_content_sandbox_verified(root)


def test_live_opaque_content_cannot_disappear_from_sandbox_proof(processes):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    opaque = process(14, 12, command="firefox -contentproc rdd")
    assert not rendering.firefox_content_sandbox_verified(root)
    (opaque / "cmdline").unlink()
    (opaque / "status").unlink()
    assert opaque.exists()
    assert not rendering.firefox_content_sandbox_verified(root)


@pytest.mark.parametrize("status", ["PPid:\tunknown\n", "PPid:\t10\nSeccomp:\t2\n",
                                    "PPid:\t10\nSeccomp:\t2\nSeccomp_filters:\t-1\n"])
def test_live_unknown_process_requires_complete_valid_evidence(processes, status):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    opaque = process(14, 10, command="node playwright")
    (opaque / "status").write_text(status)
    assert not rendering.firefox_content_sandbox_verified(root)


def test_process_that_vanishes_during_readback_may_be_skipped(processes, monkeypatch):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    vanished = process(14, 12, command="firefox -contentproc rdd")
    read_bytes = rendering.Path.read_bytes

    def read(path):
        if path == vanished / "cmdline":
            (vanished / "cmdline").unlink()
            (vanished / "status").unlink()
            vanished.rmdir()
        return read_bytes(path)

    monkeypatch.setattr(rendering.Path, "read_bytes", read)
    assert rendering.firefox_content_sandbox_verified(root)
    assert not vanished.exists()


def test_content_with_unproven_parent_cannot_be_assumed_foreign(processes):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    process(14, 99, filters=2, command="firefox -contentproc rdd")
    assert not rendering.firefox_content_sandbox_verified(root)


def test_foreign_browser_and_forkserver_role_cannot_supply_content_proof(processes):
    root, process = processes
    process(13, 1, filters=3, command="firefox -contentproc -isForBrowser tab")
    assert not rendering.firefox_content_sandbox_verified(root)
    process(14, 12, filters=2, command="firefox -contentproc -isForBrowser tab", name="forkserver")
    assert rendering.firefox_content_sandbox_verified(root)
    process(15, 12, filters=1, command="firefox -contentproc rdd", name="forkserver")
    assert not rendering.firefox_content_sandbox_verified(root)


def test_parent_or_baseline_filter_cannot_be_misreported_as_content_filter(processes):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    process(12, 11, filters=2, command="firefox -contentproc forkserver", name="forkserver")
    assert not rendering.firefox_content_sandbox_verified(root)
    process(12, 11, command="firefox -contentproc forkserver", name="forkserver")
    process(10, 1, filters=2)
    assert not rendering.firefox_content_sandbox_verified(root)


def test_filter_failure_diagnostic_has_exact_kernel_numbers_and_no_process_text(processes, caplog):
    root, process = processes
    private_text = "private-process-text-must-not-be-logged"
    process(13, 12, filters=1, command="firefox -contentproc -isForBrowser " + private_text + " tab", name=private_text)
    assert not rendering.firefox_content_sandbox_verified(root)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert message == ("pagebuilder_firefox_sandbox_unverified reason=content_filter_unproven pid=13 parent=12 ancestor=None "
                       "seccomp=2 filters=1 baseline_filters=1 parent_filters=1")
    assert private_text not in message and "firefox -contentproc" not in message and "-contentproc" not in message


@pytest.mark.parametrize("failure,reason", [
    ("enumeration", "proc_enumeration_unavailable"),
    ("opaque", "live_process_evidence_unavailable"),
    ("parent", "content_ancestry_unproven"),
    ("baseline", "baseline_evidence_unavailable"),
    ("missing_content", "browser_content_missing"),
])
def test_unavailable_proof_diagnostic_preserves_negative_decision_without_private_data(processes, caplog, failure, reason):
    root, process = processes
    private_text = "private-status-and-command-text"
    if failure == "enumeration":
        root = root / private_text
    elif failure == "opaque":
        opaque = process(14, 12, command=private_text, name=private_text)
        (opaque / "status").write_text("Name:\t" + private_text + "\nPPid:\t12\n")
    elif failure == "parent":
        process(14, 99, filters=2, command="firefox -contentproc " + private_text + " rdd")
    elif failure == "baseline":
        for path in (root / "10").iterdir():
            path.unlink()
        (root / "10").rmdir()
    assert not rendering.firefox_content_sandbox_verified(root)
    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()
    assert "reason=" + reason + " " in message
    assert private_text not in message and "Traceback" not in message and str(root) not in message


def test_positive_sandbox_proof_does_not_emit_a_failure_diagnostic(processes, caplog):
    root, process = processes
    process(13, 12, filters=2, command="firefox -contentproc -isForBrowser tab")
    assert rendering.firefox_content_sandbox_verified(root)
    assert not caplog.records


def test_linux_launch_requires_live_content_attestation_and_has_no_chromium_fallback(monkeypatch):
    monkeypatch.setattr(rendering.sys, "platform", "linux")
    monkeypatch.setattr(rendering.os, "geteuid", lambda: 10001)
    events = []
    probe = SimpleNamespace(goto=lambda *args, **kwargs: events.append("content_page"),
                            close=lambda: events.append("probe_closed"))
    browser = SimpleNamespace(new_page=lambda: probe, close=lambda: events.append("browser_closed"), on=lambda *_args: None)
    def launch(profile, **kwargs):
        assert profile == ""
        assert kwargs["env"] == rendering.browser_env()
        return SimpleNamespace(browser=browser)
    pw = SimpleNamespace(firefox=SimpleNamespace(launch_persistent_context=launch), chromium=SimpleNamespace(
        launch=lambda **kwargs: pytest.fail("No alternate browser may bypass failed sandbox attestation")))
    monkeypatch.setattr(rendering, "firefox_content_sandbox_verified", lambda: False)
    with pytest.raises(ValueError, match="pagebuilder_renderer_unavailable"):
        rendering._launch_browser(pw)
    assert events == ["content_page", "probe_closed", "browser_closed"]


def test_browser_environment_cannot_disable_sandbox_or_inherit_provider_secrets(monkeypatch):
    monkeypatch.setenv("HOME", "/unwritable-worker-home")
    monkeypatch.setenv("TMPDIR", "/unwritable-worker-home")
    assert rendering.browser_env()["HOME"] == rendering.browser_env()["TMPDIR"] == "/tmp"
    assert rendering.os.environ["HOME"] == "/unwritable-worker-home"
    for key in ("MOZ_DISABLE_CONTENT_SANDBOX", "MOZ_PERMISSIVE_CONTENT_SANDBOX", "MOZ_FAKE_NO_SANDBOX",
                "AZURE_API_KEY", "AWS_SECRET_ACCESS_KEY", "HTTP_PROXY", "HTTPS_PROXY", "NODE_OPTIONS", "PYTHONPATH"):
        monkeypatch.setenv(key, "synthetic-secret")
        assert key not in rendering.browser_env()


def test_linux_root_execution_is_rejected_before_browser_launch(monkeypatch):
    monkeypatch.setattr(rendering.sys, "platform", "linux")
    monkeypatch.setattr(rendering.os, "geteuid", lambda: 0)
    with pytest.raises(ValueError, match="pagebuilder_renderer_unavailable"):
        rendering._launch_browser(SimpleNamespace())


def test_mac_keeps_native_chrome_sandbox_and_filtered_environment(monkeypatch):
    monkeypatch.setattr(rendering.sys, "platform", "darwin")
    calls = []
    browser = object()
    def launch(**kwargs):
        calls.append(kwargs)
        return browser
    pw = SimpleNamespace(chromium=SimpleNamespace(launch=launch))
    assert rendering._launch_browser(pw) is browser
    assert calls == [{"headless": True, "chromium_sandbox": True, "env": rendering.browser_env(), "channel": "chrome"}]


def test_linux_launch_retries_once_with_native_profiles_and_safe_logs(monkeypatch, caplog):
    monkeypatch.setattr(rendering.sys, "platform", "linux")
    monkeypatch.setattr(rendering.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(rendering.time, "sleep", lambda _: None)
    monkeypatch.setattr(rendering, "firefox_content_sandbox_verified", lambda: True)
    profiles = []
    probe = SimpleNamespace(goto=lambda *_args, **_kwargs: None, close=lambda: None)
    browser = SimpleNamespace(new_page=lambda: probe,
                              on=lambda *_args: pytest.fail("Profile cleanup must wait for process exit"))
    def launch(profile, **kwargs):
        profiles.append(profile)
        assert kwargs["timeout"] == 20000
        if len(profiles) == 1:
            raise TimeoutError("synthetic secret and founder text")
        return SimpleNamespace(browser=browser)
    pw = SimpleNamespace(firefox=SimpleNamespace(launch_persistent_context=launch))
    assert rendering._launch_browser(pw) is browser
    assert profiles == ["", ""]
    failure = next(record for record in caplog.records if record.msg == "pagebuilder_renderer_step_failed")
    assert failure.step == "launch" and failure.exception_class == "TimeoutError"
    assert "synthetic secret" not in caplog.text and "founder text" not in caplog.text


def test_linux_launch_stops_after_two_failures_and_preserves_safe_logs(monkeypatch, caplog):
    monkeypatch.setattr(rendering.sys, "platform", "linux")
    monkeypatch.setattr(rendering.os, "geteuid", lambda: 10001)
    monkeypatch.setattr(rendering.time, "sleep", lambda _: None)
    profiles = []
    def launch(profile, **_kwargs):
        profiles.append(profile)
        raise TimeoutError("private exception detail")
    with pytest.raises(TimeoutError):
        rendering._launch_browser(SimpleNamespace(firefox=SimpleNamespace(launch_persistent_context=launch)))
    assert profiles == ["", ""]
    assert all(record.step == "launch" for record in caplog.records)
    assert "private exception detail" not in caplog.text


@pytest.mark.parametrize("failure", ["browser", "driver"])
def test_renderer_job_session_cleans_driver_and_scope_even_when_cleanup_raises(monkeypatch, failure):
    playwright = pytest.importorskip("playwright.sync_api")
    calls = []
    def close():
        calls.append("browser_closed")
        if failure == "browser":
            raise ValueError("closed_failure")
    def stop():
        calls.append("driver_stopped")
        if failure == "driver":
            raise ValueError("closed_failure")
    browser = SimpleNamespace(version="fixture", close=close)
    monkeypatch.setattr(playwright, "sync_playwright", lambda: SimpleNamespace(start=lambda: SimpleNamespace(stop=stop)))
    monkeypatch.setattr(rendering, "_launch_browser", lambda _: browser)
    with pytest.raises(ValueError, match="closed_failure"):
        with rendering.renderer_session():
            rendering.require_renderer()
            with rendering._browser() as same:
                assert same is browser
    assert calls == ["browser_closed", "driver_stopped"]
    assert rendering._SESSION.get() is None
