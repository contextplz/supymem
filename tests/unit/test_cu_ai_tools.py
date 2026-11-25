"""script (AppleScript allowlist) and fs tools: scope, refusals, private content, verifier evidence."""
import subprocess
import threading
from types import SimpleNamespace

import pytest
from coding_agents import cu_ai_tools as tools
from desktop_runtime.cu_ai_engine import Observation, run_loop

GOAL = "In TextEdit write the note and save it as ~/Documents/SupymemCUTest/supymem-note.txt"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "SupymemCUTest").mkdir(parents=True)
    return tmp_path


def test_script_allowlist_scope_and_forbidden_features():
    ok = 'tell application "TextEdit" to make new document with properties {text:"hi"}'
    assert tools.script_block("TextEdit", ok, GOAL) is None
    assert "not a scriptable" in tools.script_block("Terminal", ok, GOAL)
    assert "does not mention" in tools.script_block("Notes", 'tell application "Notes" to make new note', GOAL)
    for bad in ('do shell script "ls"', 'tell application "System Events" to keystroke "a"', 'open location "http://x"',
                'tell application "TextEdit" to get password'):
        assert "refused" in tools.script_block("TextEdit", bad, GOAL)
    assert "does not ask to delete" in tools.script_block("TextEdit", 'tell application "TextEdit" to delete document 1', GOAL)
    assert tools.script_block("TextEdit", 'tell application "TextEdit" to close document 1', GOAL) is None
    assert tools.script_block("TextEdit", 'tell application "TextEdit" to delete document 1', GOAL + " then delete it") is None
    mail = 'tell application "Mail" to send message 1'
    assert "drafts" in tools.script_block("Mail", mail, "Draft an email in Mail")


def test_run_script_returns_private_result_and_surfaces_errors(monkeypatch):
    calls = []

    def fake(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="document 1\n", stderr="")
    monkeypatch.setattr(subprocess, "run", fake)
    out = tools.run_script("TextEdit", 'tell application "TextEdit" to get name of document 1', GOAL, trusted=True)
    assert out.startswith("script ok (10 chars)") and tools.PRIVATE + "document 1" in out
    assert calls[0][:2] == ["osascript", "-e"]
    tools.run_script("TextEdit", 'Application("TextEdit").name()', GOAL, "jxa", trusted=True)
    assert calls[1][:3] == ["osascript", "-l", "JavaScript"]
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="no such document"))
    assert tools.run_script("TextEdit", 'tell application "TextEdit" to x', GOAL, trusted=True).startswith("script error")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired("osascript", 1)
    monkeypatch.setattr(subprocess, "run", hang)
    assert "Automation permission" in tools.run_script("TextEdit", 'tell application "TextEdit" to x', GOAL, trusted=True)


def test_fs_is_limited_to_task_named_paths_inside_home_and_never_deletes(home):
    note = "~/Documents/SupymemCUTest/supymem-note.txt"
    assert tools.run_fs("write_text", note, "a\nb", GOAL) == "fs write_text ok"
    assert (home / "Documents/SupymemCUTest/supymem-note.txt").read_text() == "a\nb"
    assert "already exists" in tools.run_fs("write_text", note, "x", GOAL)
    assert tools.run_fs("mkdir", "~/Documents/SupymemCUTest/organized", "", GOAL + " organize inside ~/Documents/SupymemCUTest") == "fs mkdir ok"
    assert tools.run_fs("move", note, "~/Documents/SupymemCUTest/organized/final.txt", GOAL + " organize inside ~/Documents/SupymemCUTest") == "fs move ok"
    assert (home / "Documents/SupymemCUTest/organized/final.txt").exists() and not (home / "Documents/SupymemCUTest/supymem-note.txt").exists()
    assert "outside the home" in tools.run_fs("list", "/etc", "", GOAL)
    assert "not a path the task names" in tools.run_fs("read_text", "~/Desktop/secret.txt", "", GOAL)
    assert "no delete" in tools.run_fs("delete", note, "", GOAL)
    assert "not a path the task names" in tools.run_fs("write_text", "~/.ssh/authorized_keys", "x", GOAL)
    read = tools.run_fs("read_text", "~/Documents/SupymemCUTest/organized/final.txt", "", GOAL + " inside ~/Documents/SupymemCUTest")
    assert read.endswith(tools.PRIVATE + "a\nb")


def test_evidence_reads_bytes_back_independently(home):
    f = home / "Documents/SupymemCUTest/x.txt"
    f.write_text("hello")
    text = tools.evidence({str(f), str(home / "nope")})
    assert "5 bytes" in text and "'hello'" in text and "does not exist" in text


def test_no_implicit_trial_folder_or_file_sibling_authority(home):
    assert tools.named_paths("Write a note in TextEdit") == []
    assert "refused" in tools.fs_block("write_text", "~/Documents/SupymemCUTest/unreviewed.txt", "x", GOAL)
    assert "refused" in tools.fs_block("list", "~/Documents", "", "read ~/Documents/one.txt")


def test_literal_backslashes_preserved(home):
    note = "~/Documents/SupymemCUTest/supymem-note.txt"
    assert tools.run_fs("write_text", note, r"literal\nnot a newline", GOAL) == "fs write_text ok"
    assert (home / "Documents/SupymemCUTest/supymem-note.txt").read_text() == r"literal\nnot a newline"


def test_script_cannot_target_another_or_calculated_app():
    assert "different app" in tools.script_block("TextEdit", 'tell application "TextEdit"\ntell application "Notes" to get every note\nend tell', GOAL)
    assert "dynamic" in tools.script_block("TextEdit", 'set a to "TextEdit"\ntell application a to activate', GOAL)


def test_script_error_body_is_private(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="", stderr="PERSONAL NOTE BODY"))
    result = tools.run_script("TextEdit", 'tell application "TextEdit" to x', GOAL, trusted=True)
    public, private = result.split(tools.PRIVATE, 1)
    assert "PERSONAL NOTE" not in public and "PERSONAL NOTE" in private


def test_cancellable_runner_stops_and_reaps_child_without_gui():
    import sys
    import time
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    start = time.monotonic()
    try:
        assert tools._run_cancellable([sys.executable, "-c", "import time; time.sleep(10)"], stop, 12) is None
        assert time.monotonic() - start < 1
    finally:
        timer.join()


def test_background_script_ignores_frontmost_decoy(home, monkeypatch):
    monkeypatch.setattr(tools, "_run_cancellable", lambda *a, **k: SimpleNamespace(returncode=0, stdout="", stderr=""))
    mac = Mac()
    mac.frontmost = lambda: ("Claude", 99)
    mac.acted = []
    replies = iter([reply(act("script", app="TextEdit", text='tell application "TextEdit" to get name')), reply(act("done"))])
    result = run_loop(GOAL, lambda p: (next(replies), {}), mac, threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "failed" and not mac.acted
    assert any(row.get("outcome") == "done_claimed" and row.get("verified") is False for row in result.log)


def test_new_textedit_file_template_refuses_before_bytes_or_opening(home, monkeypatch):
    import json
    captured = []
    def run(cmd, stop, timeout):
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="verified: exact TextEdit document readback", stderr="")
    monkeypatch.setattr(tools, "_run_cancellable", run)
    contents = 'one\ntwo "quoted"\nthree \\ literal'
    payload = json.dumps({"app": "TextEdit", "path": "~/Documents/SupymemCUTest/supymem-note.txt", "text": contents})
    touched = set()
    result = tools.run_template("text_file_open", payload, GOAL, touched=touched, stop=threading.Event())
    assert result.startswith("refused:") and not tools.readback(result)
    assert not (home / "Documents/SupymemCUTest/supymem-note.txt").exists()
    assert captured == [] and touched == set()


def test_template_refuses_extra_keys_unnamed_app_and_outside_path(home):
    import json
    assert "refused" in tools.run_template("notes_create", json.dumps({"title":"hi", "body":"x", "code":"evil"}), "Notes")
    assert "refused" in tools.run_template("text_file_open", json.dumps({"app":"Notes", "path":"~/Documents/SupymemCUTest/supymem-note.txt", "text":"x"}), GOAL)
    assert "refused" in tools.run_template("text_file_open", json.dumps({"app":"TextEdit", "path":"~/Desktop/unreviewed.txt", "text":"x"}), GOAL)


def test_checkpoint_private_mode_and_no_shared_tmp(tmp_path):
    import stat
    from desktop_runtime.cu_ai_engine import FileCheckpoint
    path = tmp_path / "order" / "checkpoint.json"
    checkpoint = FileCheckpoint(path)
    checkpoint.save({"memory": "private screen text"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert checkpoint.load() == {"memory": "private screen text"}
    assert list(path.parent.iterdir()) == [path]


def test_gateway_pause_uses_owned_order_and_closed_kind():
    from desktop_runtime.device_gateway import WorkOrderGateway
    calls = []
    gateway = WorkOrderGateway(venture_id="ven_1", order_id="order_1", device_id="device_1", request=lambda *a: calls.append(a))
    gateway.report_needs_you("captcha", "Complete verification in Chrome, then Continue.")
    assert calls == [("POST", "/api/ventures/ven_1/needs-you/device", {"order_id":"order_1", "pause_kind":"captcha", "prompt":"Complete verification in Chrome, then Continue."})]


def test_corrupt_checkpoint_never_replays_effects(tmp_path):
    from desktop_runtime.cu_ai_engine import FileCheckpoint
    path = tmp_path / "checkpoint.json"
    path.write_text("{truncated")
    def unexpected(*args):
        raise AssertionError("no model call or replay on corrupt checkpoint")
    result = run_loop(GOAL, unexpected, Mac(), threading.Event(), checkpoint=FileCheckpoint(path))
    assert result.outcome == "failed" and result.reason == "checkpoint_unreadable"


def test_wait_interrupts_without_gui_backend():
    import time
    stop = threading.Event()
    timer = threading.Timer(0.1, stop.set)
    timer.start()
    mac = Mac()
    mac.acted = []
    start = time.monotonic()
    try:
        result = run_loop(GOAL, lambda p: (reply(act("wait", ms=10000)), {}), mac, stop)
        assert result.outcome == "stopped" and time.monotonic() - start < 1
        assert not mac.acted
    finally:
        timer.join()


@pytest.mark.parametrize("verb", ["copy", "move", "rename", "write_text"])
def test_destination_created_after_preflight_is_never_overwritten(home, monkeypatch, verb):
    source = home / "Documents/SupymemCUTest/source.txt"
    destination = home / "Documents/SupymemCUTest/target.txt"
    source.write_text("source")
    goal = "Organize ~/Documents/SupymemCUTest"
    original = tools.fs_block
    def race(*args, **kwargs):
        why = original(*args, **kwargs)
        destination.write_text("concurrently created private data")
        return why
    monkeypatch.setattr(tools, "fs_block", race)
    src = str(destination) if verb == "write_text" else str(source)
    dest = "new data" if verb == "write_text" else str(destination)
    result = tools.run_fs(verb, src, dest, goal)
    assert result.startswith("fs error")
    assert destination.read_text() == "concurrently created private data"
    assert source.read_text() == "source"


def test_raw_scripts_never_execute_even_allowed_app(monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not execute")))
    assert "raw model scripts" in tools.run_script("Finder", 'tell application "Finder" to read POSIX file "/tmp/private"', "Use Finder")


def test_cancelled_copy_preserves_existing_destination_and_cleans_only_owned_temp(home, monkeypatch):
    folder = home / "Documents/SupymemCUTest"
    source, target = folder / "source.txt", folder / "target.txt"
    source.write_text("new content" * 10000)
    target.write_text("existing private content")
    stop = threading.Event()
    original = tools.os.read
    def cancel_read(fd, count):
        block = original(fd, count)
        stop.set()
        return block
    monkeypatch.setattr(tools.os, "read", cancel_read)
    result = tools.run_fs("copy", str(source), str(target), "Replace file in ~/Documents/SupymemCUTest", stop=stop, allow_overwrite=True)
    assert "cancelled" in result
    assert target.read_text() == "existing private content"
    assert source.read_text() == "new content" * 10000
    assert sorted(p.name for p in folder.iterdir()) == ["source.txt", "target.txt"]


def test_copy_limit_is_checked_before_destination_creation(home):
    folder = home / "Documents/SupymemCUTest"
    source, target = folder / "large.txt", folder / "copy.txt"
    with source.open("wb") as stream:
        stream.truncate(tools.MAX_COPY + 1)
    assert "16 MiB" in tools.run_fs("copy", str(source), str(target), "Copy inside ~/Documents/SupymemCUTest")
    assert not target.exists()


@pytest.mark.parametrize("verb", ["copy", "move", "rename", "write_text"])
@pytest.mark.parametrize("wording", ["Do not overwrite or replace existing files", "Overwrite existing files"])
def test_goal_words_never_grant_overwrite(home, verb, wording):
    folder = home / "Documents/SupymemCUTest"
    source, target = folder / "source.txt", folder / "target.txt"
    source.write_text("source")
    target.write_text("keep")
    src = str(target) if verb == "write_text" else str(source)
    dest = "changed" if verb == "write_text" else str(target)
    assert tools.run_fs(verb, src, dest, wording + " inside ~/Documents/SupymemCUTest").startswith("refused")
    assert target.read_text() == "keep"
    assert source.read_text() == "source"


@pytest.mark.parametrize("verb", ["copy", "write_text"])
@pytest.mark.parametrize("existing", [False, True])
def test_stop_during_fsync_does_not_publish(home, monkeypatch, verb, existing):
    folder = home / "Documents/SupymemCUTest"
    source, target = folder / "source.txt", folder / "target.txt"
    source.write_text("source")
    if existing:
        target.write_text("original")
    stop = threading.Event()
    monkeypatch.setattr(tools.os, "fsync", lambda fd: stop.set())
    src = str(target) if verb == "write_text" else str(source)
    dest = "changed" if verb == "write_text" else str(target)
    result = tools.run_fs(verb, src, dest, "inside ~/Documents/SupymemCUTest", stop=stop,
                          allow_overwrite=existing)
    assert "cancelled" in result
    assert target.read_text() == "original" if existing else not target.exists()
    assert not list(folder.glob(".supymem-cu-*"))


def test_stop_racing_committed_effect_is_not_reported_as_unchanged(home, monkeypatch):
    folder = home / "Documents/SupymemCUTest"
    source, target = folder / "source.txt", folder / "target.txt"
    source.write_text("source")
    stop = threading.Event()
    original = tools.os.link
    def committed(*args, **kwargs):
        original(*args, **kwargs)
        stop.set()
    monkeypatch.setattr(tools.os, "link", committed)
    result = tools.run_fs("copy", str(source), str(target), "inside ~/Documents/SupymemCUTest", stop=stop)
    assert "committed before Stop" in result
    assert target.read_text() == "source"


def act(op, **kw):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0,
            "app": "", "ms": 0, "summary": "", **kw}


def reply(*a):
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": list(a)}


class Mac:
    acted = []

    def observe(self):
        return Observation(b"x", [], "TextEdit", "Doc", "s", False, 1)

    def frontmost(self):
        return ("TextEdit", 1)

    def focused_role(self):
        return None

    def activate(self, n):
        pass

    def act(self, a, o):
        self.acted.append(a.op)
        return "ok"


def test_missing_file_feedback_is_actionable_without_path_or_exception_body(home):
    result = tools.run_fs("read_text", "~/Documents/SupymemCUTest/supymem-note.txt", "", GOAL)
    assert "source_missing" in result
    assert "create" in result and "task" in result
    assert str(home) not in result and "supymem-note.txt" not in result


def test_failed_effect_cannot_advance_model_claimed_subgoal(home):
    seen = []
    replies = iter([
        {**reply(act("fs", chord="read_text", app="~/Documents/SupymemCUTest/supymem-note.txt")), "subgoal_done": True},
        reply(act("fail", summary="Stop diagnostic")),
    ])
    def decide(payload):
        seen.append(payload["subgoal"])
        return next(replies), {}
    result = run_loop(GOAL, decide, Mac(), threading.Event(),
                      planner=lambda *a: ["Create output", "Read output"])
    assert seen == [0, 0]
    assert not any(row.get("phase") == "subgoal" for row in result.log)


@pytest.mark.parametrize("effect", ["refused: target unavailable", "launch_focus_uncertain", "error: target exited"])
def test_uncertain_native_effect_does_not_advance_subgoal(effect):
    mac = Mac()
    mac.act = lambda *a: effect
    seen = []
    replies = iter([{**reply(act("open_app", app="TextEdit")), "subgoal_done": True}, reply(act("fail"))])
    def decide(payload):
        seen.append(payload["subgoal"])
        return next(replies), {}
    run_loop("Inspect TextEdit", decide, mac, threading.Event(), planner=lambda *a: ["Open", "Inspect"])
    assert seen == [0, 0]


def test_successful_native_menu_label_is_not_a_failure_status():
    mac = Mac()
    mac.act = lambda *a: "menu Help > Report error"
    seen = []
    replies = iter([{**reply(act("open_app", app="TextEdit")), "subgoal_done": True}, reply(act("fail"))])
    def decide(payload):
        seen.append(payload["subgoal"])
        return next(replies), {}
    run_loop("Inspect TextEdit", decide, mac, threading.Event(), planner=lambda *a: ["Open", "Inspect"])
    assert seen == [0, 1]


@pytest.mark.parametrize("result_text", ["ok", "refused: synthetic", "fs error: effect uncertain"])
@pytest.mark.parametrize("tool", ["fs", "template", "script"])
def test_external_tool_does_not_dispatch_queued_gui_action_from_old_observation(home, monkeypatch, result_text, tool):
    mac, invalidated = Mac(), []
    mac.acted = []
    mac.invalidate_observation = lambda: invalidated.append(True)
    monkeypatch.setattr(tools, "run_" + tool, lambda *a, **kw: result_text)
    replies = iter([reply(act(tool, chord="notes_create" if tool == "template" else "write_text",
                              app="~/Documents/SupymemCUTest/supymem-note.txt", text="x"),
                          act("key", chord="cmd+a")), reply(act("done", summary="synthetic"))])
    # Only dispatch custody is under test; the mocked helpers create no output.
    result = run_loop("Inspect the current TextEdit state", lambda _: (next(replies), {}), mac,
                      threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "done" and invalidated == [True]
    assert mac.acted == []  # Reobserving must not blindly execute the old queued coordinates.


def test_external_exception_also_invalidates_observation(home, monkeypatch):
    mac, invalidated = Mac(), []
    mac.invalidate_observation = lambda: invalidated.append(True)
    monkeypatch.setattr(tools, "run_fs", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("synthetic")))
    result = run_loop(GOAL, lambda _: (reply(act("fs", chord="write_text", app="~/Documents/SupymemCUTest/supymem-note.txt")), {}),
                      mac, threading.Event())
    assert (result.outcome, result.reason) == ("failed", "effect_reconciliation_required")
    assert invalidated == [True]


def test_external_tool_then_wait_and_independent_tool_remain_valid(home, monkeypatch):
    mac, invalidated = Mac(), []
    mac.acted = []
    mac.invalidate_observation = lambda: invalidated.append(True)
    monkeypatch.setattr(tools, "run_fs", lambda *a, **kw: "ok")
    path = "~/Documents/SupymemCUTest/supymem-note.txt"
    replies = iter([reply(act("fs", chord="write_text", app=path), act("wait", ms=1)),
                    reply(act("fs", chord="read_text", app=path)),
                    reply(act("done", summary="synthetic"))])
    result = run_loop("Inspect the current TextEdit state", lambda _: (next(replies), {}), mac,
                      threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "done" and invalidated == [True, True] and not mac.acted


def test_loop_runs_tools_without_the_gui_backend_withholds_content_from_logs_and_feeds_verifier_evidence(home, monkeypatch):
    monkeypatch.setattr(tools, "_run_cancellable", lambda *a, **k: SimpleNamespace(returncode=0, stdout="SECRET BODY", stderr=""))
    logs, seen, mac = [], {}, Mac()
    mac.acted = []
    replies = iter([reply(act("template", chord="notes_create", text='{"title":"QA title","body":"body"}')),
                    reply(act("fs", chord="write_text", app="~/Documents/SupymemCUTest/supymem-note.txt", text="L1\nL2")),
                    reply(act("fs", chord="read_text", app="~/Desktop/other.txt")),   # not named: soft refusal
                    reply(act("done", summary="saved"))])
    history = []

    def decide(p):
        history.append(list(p["history"]))
        return next(replies), {}

    def verify(claim, obs, memory):
        seen["memory"] = memory
        return (len(history) >= 4), "other approved steps remain"
    result = run_loop(GOAL + " Notes", decide, mac, threading.Event(), log=logs.append, verify=verify)
    assert result.outcome == "done" and mac.acted == []                      # no GUI action was needed
    assert "SECRET BODY" in " ".join(history[1])                              # the model sees script output
    assert all("SECRET BODY" not in str(row) for row in logs + result.log)    # logs/receipts never do
    assert all("SECRET BODY" not in str(row.get("outcome", "")) for row in logs)
    assert "supymem-note.txt: file, 5 bytes" in seen["memory"] and "'L1\\nL2'" in seen["memory"]
    assert any("refused" in line for line in history[3])                      # soft refusal, task continued
