import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[2] / "scripts/local_browser_journey.py"
spec = importlib.util.spec_from_file_location("local_browser_journey", SCRIPT)
journey = importlib.util.module_from_spec(spec)
spec.loader.exec_module(journey)


@pytest.mark.parametrize("idea,answer", [
    ("Scheduling maintenance for warehouse teams", "Warehouse supervisors using shared calendars"),
    ("Helping families exchange used toys", "Parents arranging neighborhood swaps"),
])
def test_arguments_preserve_unrelated_founder_inputs(tmp_path, idea, answer):
    fixture = tmp_path / "idea.json"
    fixture.write_text(json.dumps({"idea": idea, "answers": [answer]}))
    args = journey.arguments(["--arguments-file", str(fixture), "--name", "Trial workspace"])
    assert (args.idea, args.answer, args.name) == (idea, answer, "Trial workspace")


@pytest.mark.parametrize("url", ["https://example.org", "http://127.0.0.1:3100/api", "http://user:pass@localhost:3100", "http://localhost:3100/?token=private"])
def test_arguments_reject_non_console_or_credential_urls(url):
    with pytest.raises(ValueError):
        journey.arguments([url])


@pytest.mark.parametrize("status", [200, 399, 400, 403, 500])
def test_network_failures_are_not_ignored(status):
    recorder = journey.Failures(("private-value",))
    recorder.response(SimpleNamespace(status=status, url="http://127.0.0.1:3100/api/local/providers?key=private-value", request=SimpleNamespace(method="GET")))
    assert len(recorder.items) == int(status >= 400)
    if recorder.items:
        assert "private-value" not in recorder.items[0]
        assert f"{status} GET http://127.0.0.1:3100/api/local/providers" in recorder.items[0]


def test_console_and_transport_errors_are_redacted():
    recorder = journey.Failures(("private-value",))
    recorder.console(SimpleNamespace(type="warning", text="ignore this warning"))
    recorder.console(SimpleNamespace(type="error", text="api_key=private-value at https://example.org/?token=private-value"))
    recorder.page_error(Exception("private-value"))
    recorder.request_failed(SimpleNamespace(method="GET", url="http://localhost/private-value", failure="private-value"))
    assert len(recorder.items) == 3
    assert "private-value" not in json.dumps(recorder.items)


def test_navigation_cancellation_is_recorded_without_an_http_failure():
    recorder = journey.Failures()
    recorder.request_failed(SimpleNamespace(method="GET", url="http://localhost/_next/hot-update.js", failure="net::ERR_ABORTED"))
    assert not recorder.items
    assert len(recorder.cancelled_requests) == 1


def test_setup_reads_private_file_without_printing(tmp_path, capsys):
    env = tmp_path / "live.env"
    env.write_text('LLM_PROVIDER=azure_openai\nLLM_MODEL="deployment-name"\nLLM_BASE_URL=https://azure.example.org\nLLM_API_KEY=private-value\n')
    settings = journey.provider_settings(journey.load_environment(env))
    assert settings == {"provider": "azure_openai", "model": "deployment-name", "base_url": "https://azure.example.org", "api_key": "private-value"}
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("values", [{}, {"LLM_PROVIDER": "unknown"}, {"LLM_PROVIDER": "azure_openai", "LLM_MODEL": "model"}])
def test_incomplete_setup_has_an_exact_next_step(values):
    with pytest.raises(journey.Skip, match="Keys and connections"):
        journey.provider_settings(values)


def test_step_failure_saves_screenshot_and_does_not_print_fill_values(tmp_path, capsys):
    class Page:
        def screenshot(self, **options):
            Path(options["path"]).write_bytes(b"screenshot")

        def get_by_role(self, role):
            return SimpleNamespace(all_text_contents=lambda: [])

    runner = journey.BrowserJourney(Page(), SimpleNamespace(timeout=1), tmp_path, journey.Failures(("private-value",)))
    def fail():
        raise RuntimeError("fill(private-value) failed")
    assert runner.step("open app", fail, "Restart your local app, then retry.") is False
    assert (tmp_path / "01-open-app.png").is_file()
    output = capsys.readouterr().out
    assert "FAIL open app:" in output
    assert "private-value" not in output
    assert "private-value" not in (tmp_path / "report.json").read_text()


def test_browser_context_is_fresh_and_has_no_manual_credentials(tmp_path):
    calls = []
    page = SimpleNamespace(on=lambda *args: calls.append(args))
    context = SimpleNamespace(new_page=lambda: page)
    browser = SimpleNamespace(new_context=lambda **options: (calls.append(options), context)[1])
    assert journey.fresh_page(browser, journey.Failures()) == (context, page)
    options = calls[0]
    assert options == {"color_scheme": "light", "viewport": {"width": 1440, "height": 1000}}
    assert {event for event, _callback in calls[1:]} == {"response", "requestfailed", "console", "pageerror"}


def test_gate_runs_browser_even_when_http_gate_fails():
    script = SCRIPT.with_name("local_gate.sh").read_text()
    assert "local_value_journey.py --gate" in script
    assert "local_browser_journey.py --gate" in script
    assert "exec " not in script


def test_gate_preserves_runtime_isolates_data_and_stops_stack(tmp_path, monkeypatch):
    import sys

    output = tmp_path / "artifacts"
    output.mkdir()
    calls = {}
    monkeypatch.setattr(journey, "ROOT", tmp_path)
    monkeypatch.setattr(journey, "artifact_directory", lambda: output)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("UNRELATED_API_KEY", "do-not-inherit")
    monkeypatch.setitem(sys.modules, "local_value_journey", SimpleNamespace(free_ports=lambda: (42001, 42002, 42003)))
    stack = SimpleNamespace(pid=123, stdout=None, poll=lambda: None, wait=lambda **options: calls.update(waited=True))
    def start(command, **options):
        calls.update(command=command, **options)
        return stack
    monkeypatch.setattr(journey.subprocess, "Popen", start)
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **options: SimpleNamespace(stdout="123 1 123\n"))
    monkeypatch.setattr(journey.os, "killpg", lambda *args: calls.update(stopped=args))
    monkeypatch.setattr(journey.threading, "Thread", lambda **options: SimpleNamespace(start=lambda: None, join=lambda **options: None))
    class Probe:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def connect_ex(self, address):
            return 0
    monkeypatch.setattr(journey.socket, "socket", Probe)
    monkeypatch.setattr(journey, "run_browser", lambda *args: 1)
    args = SimpleNamespace(timeout=1)
    assert journey.gate(args, {"OPENAI_API_KEY": "owner-key"}) == 1
    assert calls["env"]["PATH"] == "/usr/bin"
    assert calls["env"]["OPENAI_API_KEY"] == "owner-key"
    assert "UNRELATED_API_KEY" not in calls["env"]
    assert calls["env"]["COMPANY_OS_DB_URL"].startswith("sqlite:///")
    assert not Path(calls["env"]["COMPANY_OS_DATA_DIR"]).exists()
    assert calls["stopped"] == (123, journey.signal.SIGTERM)
    assert calls["waited"] is True


@pytest.mark.parametrize("http_status,browser_status,expected", [(1, 0, 1), (0, 1, 1), (0, 0, 0), (130, 0, 130)])
def test_shell_gate_preserves_failure_and_interrupt_status(tmp_path, http_status, browser_status, expected):
    python = tmp_path / "python"
    python.write_text('#!/usr/bin/env python3\nimport os, sys\nraise SystemExit(int(os.environ["HTTP_STATUS" if "local_value_journey" in sys.argv[1] else "BROWSER_STATUS"]))\n')
    python.chmod(0o700)
    result = subprocess.run(["bash", str(SCRIPT.with_name("local_gate.sh"))], env={**os.environ, "SUPYMEM_PYTHON": str(python), "HTTP_STATUS": str(http_status), "BROWSER_STATUS": str(browser_status)}, capture_output=True)
    assert result.returncode == expected


def test_preview_waits_inside_its_own_frame(tmp_path, monkeypatch):
    calls = []
    heading = SimpleNamespace(first=SimpleNamespace(wait_for=lambda **options: calls.append(options)))
    frame = SimpleNamespace(get_by_role=lambda role: heading)
    preview = SimpleNamespace(first=SimpleNamespace(content_frame=frame))
    page = SimpleNamespace(goto=lambda *args, **options: None, get_by_title=lambda name: preview)
    runner = journey.BrowserJourney(page, SimpleNamespace(base_url="http://localhost:3100", timeout=2), tmp_path, journey.Failures())
    runner.venture_id = "saved-venture"
    monkeypatch.setattr(runner, "ask", lambda text: None)
    monkeypatch.setattr(runner, "wait", lambda target: None)
    runner.landing()
    assert calls == [{"state": "visible", "timeout": 2000}]


def test_pending_results_refresh_the_ui_without_manual_api_calls(tmp_path, monkeypatch):
    states = iter([False, False, True])
    clicks = []
    target = SimpleNamespace(first=SimpleNamespace(is_visible=lambda: next(states)))
    button = SimpleNamespace(is_enabled=lambda: True, click=lambda: clicks.append("refresh"))
    page = SimpleNamespace(get_by_role=lambda *args, **options: button, wait_for_timeout=lambda timeout: None)
    runner = journey.BrowserJourney(page, SimpleNamespace(timeout=1), tmp_path, journey.Failures())
    monkeypatch.setattr(runner, "wait", lambda target: None)
    monkeypatch.setattr(runner, "no_errors", lambda: None)
    runner.refresh_until(target, "Refresh research")
    assert clicks == ["refresh", "refresh"]


def test_cleanup_stops_descendant_groups_without_touching_other_stacks(monkeypatch):
    signals = []
    waits = []
    def wait(**options):
        waits.append(options)
        if options:
            raise subprocess.TimeoutExpired("stack", 35)
    monkeypatch.setattr(journey.subprocess, "run", lambda *args, **options: SimpleNamespace(stdout="123 1 123\n240 123 240\n241 240 240\n999 1 999\n"))
    monkeypatch.setattr(journey.os, "killpg", lambda *args: signals.append(args))
    journey.stop_stack(SimpleNamespace(pid=123, wait=wait))
    assert set(signals) == {(group, signum) for group in (123, 240) for signum in (journey.signal.SIGTERM, journey.signal.SIGKILL)}
    assert waits == [{"timeout": 35}, {}]


@pytest.mark.parametrize("idea,answer", [
    ("Planning repairs for commercial kitchens", "Restaurant maintenance managers"),
    ("Sharing garden tools between neighbors", "Households with small gardens"),
])
def test_venture_uses_founder_words_answers_once_and_skips_the_rest(tmp_path, monkeypatch, idea, answer):
    inputs = []
    state = {"phase": -1}
    class Field:
        first = property(lambda self: self)
        def __init__(self, name):
            self.name = name
        def fill(self, value):
            inputs.append((self.name, value))
        def is_visible(self):
            return self.name == "name" and state["phase"] == 2
        def or_(self, other):
            return self
    def skip():
        inputs.append(("skip", None))
        state["phase"] = 2
    def advance():
        state["phase"] += 1
        if state["phase"] == 3:
            page.url = "http://localhost:3100/new?venture=created-venture"
    def control(role, name=None, **options):
        if role == "textbox":
            return Field("name" if name == "Your venture name" else "idea" if name == "What is your idea?" else "answer")
        return SimpleNamespace(click=skip if name == "I'm not sure, continue with what I shared" else advance)
    page = SimpleNamespace(get_by_role=control, goto=lambda *args, **options: None, wait_for_url=lambda pattern, **options: None)
    args = SimpleNamespace(base_url="http://localhost:3100", timeout=1, idea=idea, answer=answer, name="Trial workspace")
    runner = journey.BrowserJourney(page, args, tmp_path, journey.Failures())
    runner.model_ready = True
    monkeypatch.setattr(runner, "wait", lambda target: None)
    runner.venture()
    assert inputs == [("idea", idea), ("answer", answer), ("skip", None), ("name", "Trial workspace")]
    assert runner.venture_id == "created-venture"
