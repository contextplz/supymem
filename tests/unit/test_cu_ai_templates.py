"""Verified script templates, sheet dismissal before scripting, and the permission preflight."""
import json
import threading
from types import SimpleNamespace

import pytest
from coding_agents import cu_ai_loop as loop
from coding_agents import cu_ai_templates as catalog
from coding_agents import cu_ai_tools as tools
from desktop_runtime import cu_preflight
from desktop_runtime.cu_ai_engine import Observation, run_loop


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / "Documents" / "SupymemCUTest").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def scripts(monkeypatch):
    seen = []
    monkeypatch.setattr(tools, "run_script", lambda app, code, goal, **k: seen.append((app, code)) or "script ok (2 chars)")
    return seen


def call(name, goal, **params):
    return tools.run_template(name, json.dumps(params), goal)


def test_catalog_and_parser_agree():
    assert catalog.NAMES == loop.TEMPLATE_NAMES
    for name in loop.TEMPLATE_NAMES:
        assert loop.parse_action({"op": "template", "chord": name, "text": "{}"}).chord == name
    with pytest.raises(loop.ActionError):
        loop.parse_action({"op": "template", "chord": "run_anything", "text": "{}"})


def test_notes_rename_and_append_touch_only_named_exactly_one_note(scripts):
    goal = "In Notes rename 'Alpha' to 'Beta' and add a line"
    assert call("notes_rename", goal, title="Alpha", new_title="Beta") == "script ok (2 chars)"
    code = scripts[-1][1]
    assert 'notes whose name is "Alpha"' in code and 'set name of n to "Beta"' in code and "count of hits) is not 1" in code
    assert call("notes_append", goal, title="Alpha", text="x <b>\ny") == "script ok (2 chars)"
    assert "x &lt;b&gt;<br>y" in scripts[-1][1]
    assert "does not name" in call("notes_rename", goal, title="Personal diary", new_title="Beta")


def test_param_quotes_cannot_break_out_of_the_literal(scripts):
    goal = 'In Notes append to \'a" & (do shell script "x") & "\''
    call("notes_append", goal, title='a" & (do shell script "x") & "', text="t")
    assert 'a\\" & (do shell script \\"x\\") & \\"' in scripts[-1][1]
    assert '"a" & (do shell script' not in scripts[-1][1]


def test_calendar_create_is_locale_free_and_delete_needs_the_task_to_ask(scripts):
    goal = "In Calendar create calendar 'QA cal' with event 'QA ev' on 2026-10-04 10:00 to 11:00"
    call("calendar_create_event", goal, calendar="QA cal", title="QA ev", start="2026-10-04 10:00", end="2026-10-04 11:00:00")
    code = scripts[-1][1]
    assert "my mkdate(2026, 10, 4, 10, 0, 0)" in code and "my mkdate(2026, 10, 4, 11, 0, 0)" in code
    assert 'make new calendar with properties {name:"QA cal"}' in code
    assert "YYYY-MM-DD" in call("calendar_create_event", goal, calendar="QA cal", title="QA ev", start="tomorrow", end="2026-10-04 11:00")
    assert "does not ask to delete" in call("calendar_delete_event", goal, calendar="QA cal", title="QA ev")
    assert call("calendar_delete_event", goal + ", then delete 'QA ev'", calendar="QA cal", title="QA ev") == "script ok (2 chars)"
    assert "delete (item 1 of hits)" in scripts[-1][1] and "count of hits) is not 1" in scripts[-1][1]


def test_reminders_add_and_complete(scripts):
    goal = "In Reminders list 'QA list' add 'QA rem' due 2026-10-04 10:00 and complete it"
    call("reminders_create_list", goal, list="QA list")
    call("reminders_add", goal, list="QA list", title="QA rem", due="2026-10-04 10:00")
    assert "due date:my mkdate(2026, 10, 4, 10, 0, 0)" in scripts[-1][1]
    call("reminders_complete", goal, list="QA list", title="QA rem")
    assert "set completed of (item 1 of hits) to true" in scripts[-1][1]
    assert "refused" in call("reminders_add", goal, list="Other", title="QA rem")


def test_templates_refuse_unknown_keys_and_unnamed_apps(scripts):
    assert "refused" in call("notes_create", "In Notes", title="t", body="b", code="x")
    assert "refused" in call("calendar_create_event", "In Notes", calendar="c", title="t", start="2026-10-04 10:00", end="2026-10-04 11:00")
    assert scripts == []


def test_textedit_new_save_refuses_before_creation_or_opening(home, scripts, monkeypatch):
    monkeypatch.setattr(tools, "run_script", lambda *a, **k: pytest.fail("must not dispatch app opening"))
    goal = "In TextEdit save ~/Documents/SupymemCUTest/a.txt"
    out = call("textedit_new_save", goal, path="~/Documents/SupymemCUTest/a.txt", text="L1\nL2", plain="true")
    assert out.startswith("refused:") and not tools.readback(out)
    assert not (home / "Documents/SupymemCUTest/a.txt").exists() and scripts == []
    assert "refused" in call("textedit_new_save", goal, path="~/Documents/SupymemCUTest/b.txt", text="x", plain="false")


def test_finder_templates_use_scoped_fs(home):
    base = "~/Documents/SupymemCUTest"
    goal = f"In Finder create {base}/organized, move {base}/n.txt into it and rename it final.txt"
    (home / "Documents/SupymemCUTest/n.txt").write_text("hi")
    assert call("finder_mkdir", goal, path=f"{base}/organized") == "fs mkdir ok"
    assert call("finder_move", goal, src=f"{base}/n.txt", dest=f"{base}/organized/n.txt") == "fs move ok"
    assert call("finder_rename", goal, path=f"{base}/organized/n.txt", new_name="final.txt") == "fs rename ok"
    assert (home / "Documents/SupymemCUTest/organized/final.txt").read_text() == "hi"
    assert "refused" in call("finder_rename", goal, path=f"{base}/organized/final.txt", new_name="../x.txt")
    assert "refused" in call("finder_mkdir", goal, path="~/Desktop/elsewhere")


def test_preview_export_converts_without_overwriting(home):
    PIL = pytest.importorskip("PIL.Image")
    base = home / "Documents/SupymemCUTest"
    PIL.new("RGB", (8, 6), "orange").save(base / "pic.jpg")
    goal = f"Open ~/Documents/SupymemCUTest/pic.jpg in Preview and export it as png to ~/Documents/SupymemCUTest/out.png"
    out = call("preview_export", goal, src="~/Documents/SupymemCUTest/pic.jpg", dest="~/Documents/SupymemCUTest/out.png", format="png")
    if not (base / "out.png").exists():   # sips exists only on macOS
        pytest.skip("sips unavailable: " + out)
    assert out.startswith("preview_export ok") and (base / "out.png").read_bytes()[:4] == b"\x89PNG"
    assert "already exists" in call("preview_export", goal, src="~/Documents/SupymemCUTest/pic.jpg", dest="~/Documents/SupymemCUTest/out.png", format="png")
    assert not [p for p in base.iterdir() if p.name.startswith(".supymem-cu-")]


def test_sheet_cancel_id_only_with_a_sheet():
    cancel = {"id": "e7", "ax_role": "AXButton", "label": "Cancel"}
    assert loop.sheet_cancel_id([{"id": "e1", "ax_role": "AXSheet"}, cancel]) == "e7"
    assert loop.sheet_cancel_id([cancel]) is None   # a plain Cancel button is not a blocking sheet
    assert loop.sheet_cancel_id([{"id": "e1", "ax_role": "AXSheet"}]) is None


class SheetMac:
    def __init__(self):
        self.acted = []

    def observe(self):
        els = [{"id": "e1", "ax_role": "AXSheet"}, {"id": "e2", "ax_role": "AXButton", "label": "Cancel"}]
        return Observation(png=b"x", elements=els, frontmost="TextEdit", window_title="Untitled", snapshot_id="s", pid=5)

    def frontmost(self):
        return "TextEdit", 5

    def focused_role(self):
        return None

    def activate(self, name):
        pass

    def act(self, action, obs):
        self.acted.append((action.op, action.element_id))
        return "clicked"


def _act(**kw):
    base = dict(op="done", element_id="", x=0, y=0, text="", chord="", direction="", amount=0, app="", ms=0, summary="", path="", fs_op="")
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": [{**base, **kw}]}


def test_open_sheet_is_dismissed_before_a_template_runs(home, scripts):
    mac, replies = SheetMac(), iter([_act(op="template", chord="textedit_new_save",
                                         text=json.dumps({"path": "~/Documents/SupymemCUTest/a.txt", "text": "x", "plain": "true"})),
                                     _act(op="fail", summary="stop here")])
    run_loop("In TextEdit save ~/Documents/SupymemCUTest/a.txt", lambda p: (next(replies), {}), mac, threading.Event())
    assert mac.acted == [("click", "e2")]


def test_preflight_pauses_once_with_exact_guidance_then_runs(home):
    asked, answers = [], iter([["Allow Supymem to control Calendar: System Settings > Privacy & Security > Automation > Supymem > Calendar."], []])
    paused = []
    run_loop("In Calendar add an event", lambda p: (_act(op="fail", summary="end"), {}), SheetMac(), threading.Event(),
             preflight=lambda goal: next(answers),
             pause=lambda kind, prompt, obs: paused.append((kind, prompt, obs)) or "continue")
    assert len(paused) == 1 and paused[0][2] is None and "Automation > Supymem > Calendar" in paused[0][1]


def test_preflight_never_granted_fails_with_a_clear_code(home):
    result = run_loop("In Calendar add an event", lambda p: (_act(op="fail"), {}), SheetMac(), threading.Event(),
                      preflight=lambda goal: ["x"], pause=lambda *a: "resolved")
    assert (result.outcome, result.reason) == ("failed", "permission_not_granted")


def test_preflight_check_maps_denials_to_settings_paths():
    states = {"calendar": "denied", "notes": "granted"}
    out = cu_preflight.check("In Calendar and Notes do things", object(), probe=lambda app: states[app],
                             permissions=lambda mac: {"accessibility": False, "screen_recording": True})
    text = " ".join(out)
    assert "Accessibility" in text and "Automation > Supymem > Calendar and System Settings > Privacy & Security > Calendars" in text
    assert "Notes" not in text and "Screen" not in text
    assert cu_preflight.check("hello", probe=lambda a: "denied", permissions=lambda m: None) == []
    assert cu_preflight.DENIED.search("execution error: Not authorized to send Apple events to Calendar. (-1743)")
