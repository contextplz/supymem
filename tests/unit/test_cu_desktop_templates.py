"""Desktop typed operations: scope, independent readback and uncertain-effect holds.

These tests mock app/clipboard transports. They never operate the developer's desktop.
"""
import json
import sys
import threading
from types import SimpleNamespace

import pytest

from coding_agents import cu_ai_templates as catalog
from coding_agents import cu_ai_tools as tools
from coding_agents.cu_ai_loop import Caps, Reply, parse_action
from desktop_runtime.cu_ai_engine import FileCheckpoint, Observation, run_loop


class Mac:
    def observe(self):
        return Observation(b"synthetic", [], "Notes", "", "synthetic", False, 1)


def act(op, **kwargs):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0,
            "app": "", "ms": 0, "summary": "", **kwargs}


def reply(*actions):
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": list(actions)}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents").mkdir()
    return tmp_path


def call(name, goal, **args):
    return tools.run_template(name, json.dumps(args), goal)


class Board:
    def __init__(self):
        self.value, self.version, self.writes, self.unstable = "personal old clipboard", 1, 0, False

    def clearContents(self):
        self.value, self.version = None, self.version + 1

    def setString_forType_(self, text, kind):
        self.value, self.version, self.writes = text, self.version + 1, self.writes + 1
        return True

    def changeCount(self):
        return self.version

    def stringForType_(self, kind):
        if self.unstable:
            self.version += 1
        return self.value


@pytest.fixture
def clipboard(monkeypatch):
    board = Board()
    monkeypatch.setitem(sys.modules, "AppKit", SimpleNamespace(
        NSPasteboard=SimpleNamespace(generalPasteboard=lambda: board), NSPasteboardTypeString="text"))
    return board


def test_clipboard_requires_read_or_write_authority_and_does_not_echo_public_content(clipboard):
    assert call("clipboard_write", "Inspect the clipboard", text="x").startswith("refused")
    assert call("clipboard_read", "Put text on my clipboard").startswith("refused")
    assert clipboard.writes == 0
    result = call("clipboard_write", "Copy the address to my clipboard", text="synthetic@example.invalid")
    assert tools.readback(result) and clipboard.value == "synthetic@example.invalid"
    public, private = call("clipboard_read", "Read my clipboard").split(tools.PRIVATE)
    assert "synthetic@example.invalid" not in public and "synthetic@example.invalid" in private


def test_clipboard_race_and_stop_are_not_successful_effects(clipboard):
    clipboard.unstable = True
    result = call("clipboard_write", "Copy text to my clipboard", text="x")
    assert result.startswith("script cancelled") and tools.readback(result) is None
    stop = threading.Event()
    stop.set()
    assert catalog.run("clipboard_write", {"text": "y"}, "Copy text to clipboard", stop=stop).startswith("refused")
    assert clipboard.writes == 1


@pytest.mark.parametrize("goal", ["TextEdit using only the menu bar", "TextEdit using keyboard shortcuts only", "TextEdit use Format > Make Plain Text", "TextEdit via the Open dialog"])
def test_templates_cannot_substitute_for_requested_input_method(home, goal):
    path = "~/Documents/a.txt"
    assert call("textedit_new_save", goal + " save " + path, path=path, text="x", plain="true").startswith("refused")
    assert not (home / "Documents/a.txt").exists()


def test_textedit_edit_uses_exact_path_and_app_and_file_readbacks(home, monkeypatch):
    path = home / "Documents/a.txt"
    path.write_text("First\n")
    seen = []
    def app(app_name, code, goal, **kw):
        seen.append(code)
        path.write_text("First\nAdded")
        return "script ok (proof)" + tools.PRIVATE + "verified: exact app readback"
    monkeypatch.setattr(tools, "run_script", app)
    touched = set()
    result = tools.run_template("textedit_edit", json.dumps({"path": "~/Documents/a.txt", "mode": "append", "text": "Added"}),
                               "In TextEdit append 'Added' to ~/Documents/a.txt and save it in place", touched=touched)
    assert tools.readback(result) and touched == {str(path)}
    assert "documents whose path" in seen[0] and "document 1" not in seen[0] and "activate" not in seen[0]
    assert "unsaved or concurrent changes" in seen[0] and "save d" in seen[0]
    assert 'if (count of hits) is 0 then' in seen[0] and 'do shell script' not in seen[0]
    assert 'open POSIX file' not in seen[0] and 'already open' in seen[0]


def test_textedit_readback_mismatch_cannot_become_positive_proof(home, monkeypatch):
    path = home / "Documents/a.txt"
    path.write_text("First\n")
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: "script ok (proof)" + tools.PRIVATE + "verified: app readback")
    result = call("textedit_edit", "In TextEdit append 'Added' to ~/Documents/a.txt", path="~/Documents/a.txt", mode="append", text="Added")
    assert result.startswith("script cancelled") and tools.readback(result) is None
    assert call("textedit_edit", "In TextEdit add 'Added' to ~/Documents/a.txt; do not overwrite", path="~/Documents/a.txt", mode="replace", text="Added").startswith("refused")


def test_textedit_creation_refuses_before_file_or_app_effects(home, monkeypatch):
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: pytest.fail("must not dispatch app opening"))
    monkeypatch.setattr(tools, "run_fs", lambda *a, **kw: pytest.fail("must not create file bytes"))
    result = call("text_file_open", "In TextEdit create ~/Documents/new.txt with 'Exact'",
                  app="TextEdit", path="~/Documents/new.txt", text="Exact")
    assert not (home / "Documents/new.txt").exists()
    assert result.startswith("refused:") and tools.readback(result) is None


def test_copy_to_new_textedit_refuses_before_clipboard_file_or_app_effects(home, clipboard, monkeypatch):
    source, dest = home / "Documents/source.txt", home / "Documents/dest.txt"
    source.write_text("Exact\nsource\n")
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: pytest.fail("must not dispatch app opening"))
    monkeypatch.setattr(catalog, "_read_exact_file", lambda *a, **kw: pytest.fail("must not read/copy source"))
    monkeypatch.setattr(tools, "run_fs", lambda *a, **kw: pytest.fail("must not create destination"))
    touched = set()
    result = tools.run_template("textedit_copy_save", json.dumps({"src": "~/Documents/source.txt", "dest": "~/Documents/dest.txt"}),
                               "Copy the text in ~/Documents/source.txt (open it in TextEdit) and paste into a new TextEdit document saved as ~/Documents/dest.txt", touched=touched)
    assert result.startswith("refused:") and tools.readback(result) is None
    assert source.read_text() == "Exact\nsource\n" and not dest.exists()
    assert touched == set() and clipboard.writes == 0


def test_missing_exact_open_document_refuses_before_edit_or_disk_mutation(home, monkeypatch):
    path = home / "Documents/a.txt"
    path.write_text("First\n")
    seen = []
    def unavailable(app, code, goal, **kw):
        seen.append(code)
        assert code.index('if (count of hits) is 0 then return "refused:') < code.index('set text of d to')
        assert 'open POSIX file' not in code and 'activate' not in code
        return "refused: exact reviewed document must be already open"
    monkeypatch.setattr(tools, "run_script", unavailable)
    touched = set()
    result = call("textedit_edit", "In TextEdit append 'Added' to ~/Documents/a.txt", path="~/Documents/a.txt", mode="append", text="Added")
    assert result.startswith("refused:") and len(seen) == 1
    assert path.read_text() == "First\n" and touched == set()


def test_mail_draft_is_named_hidden_persisted_and_never_send(monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "run_script", lambda app, code, goal, **kw: seen.append(code) or "script ok")
    goal = "In Mail make a draft to a@example.invalid and b@example.invalid with subject 'Review' and body 'hello'"
    assert call("mail_draft", goal, to="a@example.invalid,b@example.invalid", subject="Review", body="hello") == "script ok"
    code = seen[0]
    assert "visible:false" in code and "save m" in code and "messages of drafts mailbox" in code
    assert "saved draft recipients mismatched" in code and "\n    send " not in code
    assert code.count("and deleted status is false") == 2
    assert call("mail_draft", goal, to="unapproved@example.invalid", subject="Review", body="hello").startswith("refused")
    assert call("mail_draft", "In Mail send a message", to="a@example.invalid", subject="Review", body="hello").startswith("refused")


MAIL_GOAL = "In Mail save a draft to a@example.invalid and b@example.invalid (both To), subject 'Review', body 'Hi'. Do not send."
MAIL_ARGS = {"to": "a@example.invalid,b@example.invalid", "subject": "Review", "body": "Hi"}


@pytest.mark.parametrize("change,code", [
    ({"cc": "b@example.invalid"}, "mail_draft_keys_invalid"),
    ({"to": ["a@example.invalid", "b@example.invalid"]}, "mail_draft_types_invalid"),
    ({"to": "a@example.invalid,a@example.invalid"}, "mail_draft_duplicate_recipient"),
    ({"to": "a@example.invalid,other@example.invalid"}, "mail_draft_recipients_invalid"),
    ({"to": "a@example.invalid,b@example.invalid (both To)"}, "mail_draft_recipients_invalid"),
    ({"subject": "unreviewed subject"}, "mail_draft_subject_invalid"),
    ({"body": "x" * 16_001}, "mail_draft_body_invalid"),
])
def test_mail_parameter_contract_rejects_before_app_delivery_and_can_be_corrected(monkeypatch, change, code):
    calls = []
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: calls.append(a) or "script ok (proof)" + tools.PRIVATE + "verified: persisted draft")
    invalid = {**MAIL_ARGS, **change}
    rejected = tools.run_template("mail_draft", json.dumps(invalid), MAIL_GOAL)
    assert catalog.mail_parameter_feedback(rejected)[0] == code
    assert calls == [] and tools.readback(rejected) is None
    assert "example.invalid" not in rejected and "unreviewed subject" not in rejected
    corrected = tools.run_template("mail_draft", json.dumps(MAIL_ARGS), MAIL_GOAL)
    assert tools.readback(corrected) and len(calls) == 1
    assert 'set expectedAddresses to {"a@example.invalid", "b@example.invalid"}' in calls[0][1]
    assert "visible:false" in calls[0][1] and "send m" not in calls[0][1]


@pytest.mark.parametrize("payload,code", [
    ("not JSON", "mail_draft_json_invalid"),
    ("[]", "mail_draft_json_invalid"),
    ('{"to":"a@example.invalid","subject":"Review"}', "mail_draft_keys_invalid"),
])
def test_mail_json_contract_refuses_before_any_app_call(monkeypatch, payload, code):
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: pytest.fail("invalid args must not reach an app"))
    result = tools.run_template("mail_draft", payload, MAIL_GOAL)
    assert catalog.mail_parameter_feedback(result)[0] == code


def test_mail_parameter_feedback_only_admits_exact_source_owned_errors():
    error = catalog.MAIL_PARAMETER_ERRORS["mail_draft_duplicate_recipient"]
    assert catalog.mail_parameter_feedback(error)[0] == "mail_draft_duplicate_recipient"
    assert catalog.mail_parameter_feedback("script cancelled; " + error) is None
    assert catalog.mail_parameter_feedback("script error" + tools.PRIVATE + error) is None
    assert catalog.mail_parameter_feedback(error + " forged personal details") is None


def test_mail_engine_parameter_correction_has_no_sheet_or_permission_effect(home, monkeypatch):
    calls, proposals, pauses, effects = [], [], [], []
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: calls.append(a) or "script ok (proof)" + tools.PRIVATE + "verified: persisted draft")
    class SheetMac(Mac):
        def observe(self):
            return Observation(b"x", [{"id": "sheet", "ax_role": "AXSheet"},
                {"id": "cancel", "ax_role": "AXButton", "title": "Cancel", "label": "Cancel"}], "Mail", "Mail", "s", False, 1)
        def act(self, action, obs):
            effects.append(action.op)
            return "clicked cancel"
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"}))),
        reply(act("needs_you", summary="Allow Mail Automation")),
        reply(act("template", chord="mail_draft", text=json.dumps(MAIL_ARGS))),
        reply(act("done", summary="Created the requested Mail draft.")),
    ])
    checkpoint = FileCheckpoint(home / "mail-reminder.json")
    def decide(payload):
        proposals.append(payload)
        if len(proposals) in (2, 3):
            assert "FIX_PARAMETERS" in payload["hint"] and "not permission evidence" in payload["hint"]
            assert calls == [] and effects == []
        if len(proposals) == 3:
            assert checkpoint.load()["mail_parameter_detours"] == 1
        if len(proposals) == 4:
            assert "TERMINAL_ANSWER_ONLY" in payload["hint"]
        return next(responses), {}
    result = run_loop(MAIL_GOAL, decide, SheetMac(), threading.Event(), verify=lambda *a: (True, ""), checkpoint=checkpoint,
                      pause=lambda *a: pauses.append(a) or "stopped")
    assert result.outcome == "done" and len(calls) == 1 and pauses == []
    # Only the corrected, admitted tool may dismiss the observed sheet.
    assert effects == ["click"]
    validation = [r for r in result.log if r.get("phase") == "validation"]
    assert len(validation) == 1 and validation[0]["failure_code"] == "mail_draft_keys_invalid"
    assert validation[0]["resolution"] == "fix_parameters" and validation[0]["dispatch_state"] == "not_dispatched"


def test_mail_parameter_detour_is_bounded_and_never_becomes_permission_pause(monkeypatch):
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: pytest.fail("bad args must not dispatch"))
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "to": "a@example.invalid,a@example.invalid"}))),
        reply(act("open_app", app="Mail")),
        reply(act("needs_you", summary="Grant Mail permission")),
    ])
    mac = Mac()
    mac.act = lambda *a: pytest.fail("parameter repair must not open Mail")
    result = run_loop(MAIL_GOAL, lambda p: (next(responses), {}), mac, threading.Event(),
                      pause=lambda *a: pytest.fail("parameter error is not a permission grant"))
    assert result.reason == "mail_draft_duplicate_recipient" and result.outcome == "failed"


def test_mail_parameter_detour_in_batch_allows_scoped_read_but_never_launches(home, monkeypatch):
    import desktop_runtime.cu_ai_engine as engine
    # The current parser drops later lifecycle ops. Exercise the engine boundary
    # too, with an admitted typed batch, so future batch shaping cannot bypass it.
    monkeypatch.setattr(engine, "parse_reply", lambda value: Reply([parse_action(item) for item in value["actions"]]))
    source = home / "Documents/review.txt"
    source.write_text("Owned reviewed content")
    goal = MAIL_GOAL + " Also read ~/Documents/review.txt."
    scripts, reads = [], []
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: scripts.append(a) or "script ok (proof)" + tools.PRIVATE + "verified: persisted draft")
    original_fs = tools.run_fs
    def scoped_read(*args, **kwargs):
        reads.append(args[0])
        return original_fs(*args, **kwargs)
    monkeypatch.setattr(tools, "run_fs", scoped_read)
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"}))),
        reply(act("wait", ms=1), act("fs", fs_op="read_text", path="~/Documents/review.txt"), act("open_app", app="Mail")),
        reply(act("template", chord="mail_draft", text=json.dumps(MAIL_ARGS))),
        reply(act("done", summary="Created the requested Mail draft and read the reviewed file.")),
    ])
    mac = Mac()
    mac.act = lambda *a: pytest.fail("batched parameter detour must not launch Mail")
    result = run_loop(goal, lambda p: (next(responses), {}), mac, threading.Event(), verify=lambda *a: (True, ""))
    assert result.outcome == "done" and len(scripts) == 1 and reads == ["read_text"]
    assert source.read_text() == "Owned reviewed content"
    assert sum(r.get("resolution") == "fix_parameters" for r in result.log) == 2


def test_mail_batched_needs_you_alias_cannot_become_permission_pause(monkeypatch):
    import desktop_runtime.cu_ai_engine as engine
    # Terminal batching is already refused by shaping; also prove the per-action
    # engine guard if a future contract admits this batch.
    monkeypatch.setattr(engine, "parse_reply", lambda value: Reply([parse_action(item) for item in value["actions"]]))
    scripts = []
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: scripts.append(a) or "script ok (proof)" + tools.PRIVATE + "verified: persisted draft")
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"}))),
        reply(act("wait", ms=1), act("needs_you", summary="Grant Mail Automation")),
        reply(act("template", chord="mail_draft", text=json.dumps(MAIL_ARGS))),
        reply(act("done", summary="Created the requested Mail draft.")),
    ])
    result = run_loop(MAIL_GOAL, lambda p: (next(responses), {}), Mac(), threading.Event(),
                      verify=lambda *a: (True, ""), pause=lambda *a: pytest.fail("batched needs_you must not bypass repair"))
    assert result.outcome == "done" and len(scripts) == 1


def test_mail_parameter_feedback_does_not_suppress_observed_human_gate(monkeypatch):
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: pytest.fail("invalid params must not reach Mail"))
    class GatedMac(Mac):
        observations = 0
        def observe(self):
            self.observations += 1
            obs = super().observe()
            if self.observations > 1:
                obs.human = "captcha"
            return obs
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"}))),
        reply(act("wait", ms=1)),
    ])
    pauses = []
    result = run_loop(MAIL_GOAL, lambda p: (next(responses), {}), GatedMac(), threading.Event(),
                      pause=lambda kind, *a: pauses.append(kind) or "stopped")
    assert result.outcome == "stopped" and pauses == ["captcha"]


def test_mail_parameter_checkpoint_retains_static_hint_and_consumed_detour(home, monkeypatch):
    scripts = []
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: scripts.append(a) or "script ok (proof)" + tools.PRIVATE + "verified: persisted draft")
    checkpoint = FileCheckpoint(home / "mail-resume.json")
    responses = iter([
        reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"}))),
        reply(act("open_app", app="Mail")),
    ])
    result = run_loop(MAIL_GOAL, lambda p: (next(responses), {}), Mac(), threading.Event(),
                      checkpoint=checkpoint, caps=Caps(max_steps=2, max_seconds=60, explicit=True))
    assert result.reason == "step_cap_reached" and scripts == []
    saved = checkpoint.load()
    assert saved["pending_dispatch"] is False and saved["mail_parameter_repair_code"] == "mail_draft_keys_invalid"
    assert saved["mail_parameter_detours"] == 1
    def needs_you(payload):
        assert "FIX_PARAMETERS" in payload["hint"] and "mail_draft_keys_invalid" in payload["hint"]
        return reply(act("needs_you", summary="Grant Mail Automation")), {}
    resumed = run_loop(MAIL_GOAL, needs_you, Mac(), threading.Event(), checkpoint=checkpoint,
                       caps=Caps(max_steps=10, max_seconds=60, explicit=True),
                       pause=lambda *a: pytest.fail("restart must not turn parameter error into permission request"))
    assert resumed.reason == "mail_draft_keys_invalid" and scripts == []
    # No effect was dispatched in either process: an explicit corrected proposal
    # can now proceed under the same exact task and still executes only once.
    corrected = run_loop(MAIL_GOAL, lambda p: (reply(act("done", summary="Created the requested Mail draft."))
                         if "TERMINAL_ANSWER_ONLY" in p["hint"] else reply(act("template", chord="mail_draft", text=json.dumps(MAIL_ARGS))), {}),
                         Mac(), threading.Event(), checkpoint=checkpoint, caps=Caps(max_steps=10, max_seconds=60, explicit=True),
                         verify=lambda *a: (True, ""))
    assert corrected.outcome == "done" and len(scripts) == 1 and not checkpoint.path.exists()


@pytest.mark.parametrize("field,value", [
    ("mail_parameter_repair_code", "not_a_source_owned_code"),
    ("mail_parameter_repair_code", ["mail_draft_keys_invalid"]),
    ("mail_parameter_detours", True),
    ("mail_parameter_detours", 2),
])
def test_mail_parameter_checkpoint_rejects_unadmitted_code_or_counter(home, field, value):
    checkpoint = FileCheckpoint(home / "mail-corrupt.json")
    responses = iter([reply(act("template", chord="mail_draft", text=json.dumps({**MAIL_ARGS, "cc": "b@example.invalid"})))])
    run_loop(MAIL_GOAL, lambda p: (next(responses), {}), Mac(), threading.Event(),
             checkpoint=checkpoint, caps=Caps(max_steps=1, max_seconds=60, explicit=True))
    saved = checkpoint.load()
    saved[field] = value
    checkpoint.save(saved)
    result = run_loop(MAIL_GOAL, lambda p: pytest.fail("invalid persisted metadata must not reach the actor"),
                      Mac(), threading.Event(), checkpoint=checkpoint)
    assert result.reason == "checkpoint_invalid_or_task_changed"


def test_valid_mail_uncertain_effect_never_uses_parameter_correction_or_replays(home, monkeypatch):
    monkeypatch.setattr(tools, "run_script", lambda *a, **kw: "script cancelled; dispatched effect is unconfirmed")
    calls = []
    checkpoint = FileCheckpoint(home / "mail-checkpoint.json")
    def decide(payload):
        calls.append(payload)
        return reply(act("template", chord="mail_draft", text=json.dumps(MAIL_ARGS))), {}
    result = run_loop(MAIL_GOAL, decide, Mac(), threading.Event(), checkpoint=checkpoint)
    assert result.reason == "effect_reconciliation_required" and len(calls) == 1
    assert checkpoint.load()["pending_dispatch"] is True


@pytest.mark.parametrize("csv_text", ["4\n=SUM(A1:A999)\n", "4\n=SUM(A1:A2)\n", "x\n=SUM(A1:A1)\n", "=WEBSERVICE(\"https://x\")\n"])
def test_numbers_refuses_unbounded_recursive_nonnumeric_or_external_formula(home, csv_text):
    path = "~/Documents/result.csv"
    assert call("numbers_csv", f"In Numbers enter data and export {path}", path=path, csv=csv_text).startswith("refused")
    assert not (home / "Documents/result.csv").exists()


def test_numbers_app_formula_and_csv_export_are_independently_checked(home, monkeypatch):
    import re
    seen = []
    def app(app_name, code, goal, **kw):
        seen.append(code)
        temporary = re.search(r'export d to POSIX file "([^"]+)"', code)[1]
        from pathlib import Path
        Path(temporary).write_text("4\n6\n10\n20\n")
        return "script ok (proof)" + tools.PRIVATE + "verified: owned app cells exported"
    monkeypatch.setattr(tools, "run_script", app)
    path = "~/Documents/result.csv"
    result = call("numbers_csv", f"In Numbers enter a SUM and export {path}", path=path, csv="4\n6\n10\n=SUM(A1:A3)\n")
    assert tools.readback(result) and (home / "Documents/result.csv").read_text() == "4\n6\n10\n20\n"
    assert 'formula of cell "A4"' in seen[0] and "close d saving no" in seen[0]
    assert not list((home / "Documents").glob(".supymem-cu-*"))


@pytest.mark.parametrize("status", ["script timed out", "script cancelled; unconfirmed", "script error (exit 1)"])
def test_uncertain_typed_effect_blocks_next_mutation_and_preserves_reservation(home, monkeypatch, status):
    monkeypatch.setattr(tools, "run_template", lambda *a, **kw: status)
    calls = []
    checkpoint = FileCheckpoint(home / "checkpoint.json")
    def decide(payload):
        calls.append(payload)
        return reply(act("template", chord="notes_create", text="{}")), {}
    result = run_loop("In Notes create a note", decide, Mac(), threading.Event(), checkpoint=checkpoint)
    assert result.reason == "effect_reconciliation_required" and len(calls) == 1
    assert checkpoint.load()["pending_dispatch"] is True
    assert result.log[-1]["template_ids"] == ["notes_create"]


def test_zero_exit_template_refusal_and_failed_postcondition_are_not_script_success(monkeypatch):
    for text, prefix in [("refused: duplicate", "refused:"), ("unverified: no saved draft", "script cancelled")]:
        monkeypatch.setattr(tools.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0, stdout=text, stderr=""))
        result = tools.run_script("Mail", "fixed owned template", "In Mail create a draft", trusted=True)
        assert result.startswith(prefix) and tools.readback(result) is None
    assert tools.readback("script error (exit 1)" + tools.PRIVATE + "verified: personal error prose") is None
