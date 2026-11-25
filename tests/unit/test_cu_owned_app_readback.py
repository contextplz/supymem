"""No GUI/osascript/model calls: independent owned receipt contract regressions."""
from datetime import datetime, timedelta
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "desktop"))
from cu_owned_app_readback import OwnedAppReadback, numbers

RUN = "a" * 32
NOW = datetime(2026, 10, 3, 23, 59)


def text(values):
    return "\t".join(map(str, values))


def owner(values=None, **kwargs):
    state = {"raw": "0" if values is None else text(values)}
    scripts = []
    def read(script):
        scripts.append(script)
        return state["raw"]
    obj = OwnedAppReadback(RUN, read, now=lambda: NOW, zone=lambda: ("Asia/Kolkata",), **kwargs)
    return obj, state, scripts


def test_calendar_creation_then_scoped_delete_requires_fresh_proof(tmp_path):
    o, state, scripts = owner(proof_path=tmp_path / "history.json")
    o.prepare("calendar")
    state["raw"] = text(o.calendar_expected())
    assert o.verify_calendar()[0] is True
    assert o.proof_path.stat().st_mode & 0o777 == 0o600
    assert json.loads(o.proof_path.read_text())["receipt"] == o.calendar_expected()
    o.prepare_delete()
    assert o.verify_delete()[0] is False  # event still present
    state["raw"] = "1\t0\t0"
    assert o.verify_delete()[0] is True
    assert all(o.calendar in s and o.event in s for s in scripts)
    assert all('tell application "Calendar"' in s for s in scripts)


def test_absence_never_proves_creation_or_delete():
    o, state, _ = owner()
    assert o.verify_calendar()[0] is False
    assert o.verify_delete()[0] is False
    with pytest.raises(RuntimeError, match="creation_unproven"):
        o.prepare_delete()
    o.prepare("calendar")
    state["raw"] = "1\t0\t0"
    assert o.verify_calendar()[0] is False
    with pytest.raises(RuntimeError, match="creation_unproven"):
        o.prepare_delete()


@pytest.mark.parametrize("field,value", [(0,2),(1,2),(2,2),(3,2025),(4,11),(5,5),(6,9),
                                          (9,2025),(12,12),(15,1),(16,1),(17,1800)])
def test_calendar_wrong_scope_date_duration_all_day_recurrence_fail(field, value):
    o, state, _ = owner()
    o.prepare("calendar")
    bad = o.calendar_expected()
    bad[field] = value
    state["raw"] = text(bad)
    assert o.verify_calendar()[0] is False
    assert o.creation_proof is None


def test_created_event_changed_before_delete_is_not_authorized():
    o, state, _ = owner()
    o.prepare("calendar")
    state["raw"] = text(o.calendar_expected())
    assert o.verify_calendar()[0] is True
    state["raw"] = "1\t0\t0"
    with pytest.raises(RuntimeError, match="creation_changed"):
        o.prepare_delete()
    assert o.verify_delete()[0] is False


@pytest.mark.parametrize("raw", [None, "", "private secret text", "1\t1\t1", "-1", "1\n2", "1" * 1000])
def test_unavailable_malformed_private_output_never_counts_as_pass(raw):
    o, state, _ = owner()
    o.prepare("calendar")
    state["raw"] = raw
    verdict, detail = o.verify_calendar()
    assert verdict is None
    assert detail == "qa_owned_readback_unavailable"
    assert "private" not in detail


@pytest.mark.parametrize("raw", [None, "garbage", "1\t1\t1"])
def test_unknown_preflight_never_arms_model_task(raw):
    o, state, _ = owner()
    state["raw"] = raw
    with pytest.raises(RuntimeError, match="readback_unavailable"):
        o.prepare("reminders")
    assert not o.armed


def test_existing_owned_name_collision_refuses_reuse():
    o, state, _ = owner([1,0,0])
    with pytest.raises(RuntimeError, match="scope_not_fresh"):
        o.prepare("calendar")
    assert not o.armed


def test_reminder_exact_due_datetime_not_just_name_or_presence():
    o, state, scripts = owner()
    o.prepare("reminders")
    expected = [1,1,1,0,1] + o.parts(o.start)
    state["raw"] = text(expected)
    assert o.verify_reminder()[0] is True
    for index, value in [(0,2),(1,2),(2,2),(3,1),(5,2027),(7,5),(8,12)]:
        wrong = expected.copy()
        wrong[index] = value
        state["raw"] = text(wrong)
        assert o.verify_reminder()[0] is False
    state["raw"] = "1\t1\t1\t0\t0"
    assert o.verify_reminder()[0] is False
    assert all(o.reminder_list in s and o.reminder in s for s in scripts)


def test_clock_rollover_and_timezone_change_stay_unknown():
    o, state, _ = owner()
    o.prepare("calendar")
    state["raw"] = text(o.calendar_expected())
    o.now = lambda: NOW + timedelta(minutes=2)
    assert o.verify_calendar()[0] is None
    o.now = lambda: NOW
    o.zone = lambda: ("America/New_York",)
    assert o.verify_calendar()[0] is None
    assert "2026-10-04" in o.task("calendar")
    assert "tomorrow" not in o.task("calendar")


def test_clock_change_during_readback_is_unknown():
    o, state, _ = owner()
    o.prepare("calendar")
    def drift(script):
        o.zone = lambda: ("different",)
        return text(o.calendar_expected())
    o.read = drift
    assert o.verify_calendar()[0] is None


def test_private_history_tampering_cannot_authorize_delete(tmp_path):
    o, state, _ = owner(proof_path=tmp_path / "history.json")
    o.prepare("calendar")
    state["raw"] = text(o.calendar_expected())
    assert o.verify_calendar()[0] is True
    o.proof_path.write_text('{"run_id":"other"}')
    with pytest.raises(RuntimeError, match="creation_unproven"):
        o.prepare_delete()


def test_history_write_failure_is_unknown_not_creation_success(tmp_path):
    path = tmp_path / "already-present.json"
    path.write_text("do not overwrite")
    o, state, _ = owner(proof_path=path)
    o.prepare("calendar")
    state["raw"] = text(o.calendar_expected())
    assert o.verify_calendar()[0] is None
    assert o.creation_proof is None
    assert path.read_text() == "do not overwrite"


def test_uuid_scope_injection_rejected():
    with pytest.raises(ValueError, match="invalid_owned_uuid"):
        OwnedAppReadback('" arbitrary user name', lambda s: "0")


def test_generated_scripts_read_only_no_personal_title_enumeration():
    o, _, _ = owner()
    for kind in ("calendar", "reminders"):
        script = o.script(kind)
        for forbidden in ("make new", "delete ", "set name", "name of every", "summary of every", "do shell script"):
            assert forbidden not in script
    assert numbers("123\t0") == [123,0]


def suite_module():
    import importlib.util
    source = Path(__file__).resolve().parents[2] / "scripts" / "desktop" / "cu_ai_suite.py"
    spec = importlib.util.spec_from_file_location("cu_owned_suite_contract", source)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_osascript_nonzero_exit_or_permission_error_never_uses_stdout(monkeypatch):
    from types import SimpleNamespace
    mod = suite_module()
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=1, stdout="1", stderr="private denied text"))
    assert mod.osa("read-only") is None
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(OSError("private permission details")))
    assert mod.osa("read-only") is None


def test_unknown_and_unperformed_checks_count_as_failed_acceptance():
    mod = suite_module()
    result = mod.verification_counts([
        {"passed":True, "verification_status":"passed"},
        {"passed":False, "verification_status":"unknown"},
        {"passed":False, "verification_status":"failed"},
        {"outcome":"preflight_failed"},
    ])
    assert result == {"total":4,"passed":1,"failed":3,"task_passed":0,"verification_unknown":2}
    assert not mod.trial_passed(None, "done", True, True)


def test_owned_preflight_failure_returns_zero_model_calls_without_decoy(tmp_path, monkeypatch, capsys):
    mod = suite_module()
    monkeypatch.setattr(mod, "DIR", tmp_path / "owned-run")
    monkeypatch.setattr(mod.OWNED_APPS, "read", lambda script: None)
    monkeypatch.setattr(mod.trial, "make_mac", lambda *a: (_ for _ in ()).throw(AssertionError("no native startup")))
    got = mod.run("calendar", "cua")
    assert got["passed"] is False and got["model_calls"] == 0
    assert got["verification_status"] == "unknown"
    assert got["reason"] == "qa_owned_readback_unavailable"
    assert "private denied" not in capsys.readouterr().out
