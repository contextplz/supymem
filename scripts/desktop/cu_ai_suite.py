"""Local real-Mac trial suite for the AI loop (dev/QA only; drives the founder's Mac).

Each trial verifies an owned editable decoy focus baseline, samples CG/AppKit PID metadata, and checks recipient input
before and throughout execution, then verifies the real result on disk / in the app.
  python scripts/desktop/cu_ai_suite.py [--only textedit,notes] [--backend cua]
Results append to /tmp/cu_suite.jsonl (one JSON object per trial).
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cu_ai_trial as trial  # noqa: E402
from cu_notes_readback import OwnedNotesReadback  # noqa: E402
from cu_owned_app_readback import OwnedAppReadback  # noqa: E402
from cu_browser_form_fixture import BrowserFormFixture  # noqa: E402
from cu_browser_source_fixture import BrowserSourceFixture  # noqa: E402

from desktop_runtime.cu_ai_engine import frontmost_info  # noqa: E402
from desktop_runtime.cu_cleanup import cleanup_codes, cleanup_trace, public_cleanup_codes, public_cleanup_trace  # noqa: E402
from cu_artifact_checks import exact_text, exact_move, equivalent_png  # noqa: E402
import cu_tasks  # noqa: E402

RUN_ID = uuid.uuid4().hex
DIR = Path.home() / "Documents" / "SupymemCUTest" / ("run-" + RUN_ID)
_OWNED_RUN = False
NOTE = DIR / "supymem-note.txt"
ORGANIZED = DIR / "organized"
HEADLINE = DIR / "headline.txt"
BROWSER_SOURCE = None
MODEL = "gpt-5.6-luna"
FOCUS_MODE = "frontmost"
RESULT_FILE = Path("/tmp/cu_suite.jsonl")
REPETITION = 1
SOURCE_DIGEST = None
SOURCE_TRIALS = frozenset({"browser", "cross_app", "long"})
NOTE_TITLE = "Supymem CU test " + RUN_ID
EDIT_TITLE = NOTE_TITLE + " edited"
NOTE_BODY = "Owned note body " + uuid.uuid4().hex
EDIT_LINE = "Owned edit " + uuid.uuid4().hex


LAST_OSA = {"error": None}
APP_IN_SCRIPT = re.compile(r'tell application "(Calendar|Reminders|Notes|TextEdit|Finder)"')


def osa(script: str, *, preserve_output: bool = False) -> str | None:
    """AppleScript read-back; None when macOS blocks it (Automation permission prompt) so the check is 'unknown'."""
    app = (APP_IN_SCRIPT.search(script) or [None, "an app"])[1]
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=45)
    except subprocess.TimeoutExpired:
        LAST_OSA["error"] = f"timed_out_waiting_for_{app}_automation_prompt"
        return None
    except OSError:
        LAST_OSA["error"] = "osascript_unavailable"
        return None
    if result.returncode == 0:
        LAST_OSA["error"] = None
        return result.stdout.removesuffix("\n") if preserve_output else result.stdout.strip()
    # Name the missing grant so a preflight failure says exactly what to allow.
    LAST_OSA["error"] = (f"automation_not_allowed_for_{app}: allow the host app under System Settings > Privacy & Security > Automation"
                         if "-1743" in (result.stderr or "") else f"{app}_script_error_{result.returncode}")
    return None


def sweep_owned_ui() -> None:
    """Close every Finder window / TextEdit / Preview document whose target is inside the QA root (never the founder's own).

    Exact-prefix match on ~/Documents/SupymemCUTest/; windows without a file target and unsaved documents are left alone.
    """
    base = str(Path.home() / "Documents" / "SupymemCUTest") + "/"
    finder = f'''with timeout of 15 seconds
tell application "Finder"
    repeat with i from (count of Finder windows) to 1 by -1
        try
            set p to POSIX path of (target of Finder window i as alias)
            if p starts with "{base}" then close Finder window i
        end try
    end repeat
end tell
end timeout'''
    def docs(app):
        return f'''tell application "{app}"
    repeat with i from (count of documents) to 1 by -1
        try
            set p to (path of document i) as text
            if p starts with "{base}" then close document i saving no
        end try
    end repeat
end tell'''
    for app, script in (("Finder", finder), ("TextEdit", docs("TextEdit")), ("Preview", docs("Preview"))):
        try:
            if app != "Finder" and subprocess.run(["/usr/bin/pgrep", "-x", app], capture_output=True).returncode != 0:
                continue   # never launch an app just to sweep it
            subprocess.run(["osascript", "-e", script], capture_output=True, timeout=30)
        except (subprocess.TimeoutExpired, OSError):
            pass


def setup_browser_source():
    global BROWSER_SOURCE
    if BROWSER_SOURCE is not None and not BROWSER_SOURCE.closed:
        raise RuntimeError("qa_form_fixture_already_started")
    BROWSER_SOURCE = BrowserSourceFixture().start()


def source_task(name):
    prefix = f"Open {BROWSER_SOURCE.source_url} in a web browser. "
    if name == "browser":
        return prefix + "Read the headline and three listed features; leave this source page visible when done."
    if name == "cross_app":
        return prefix + f"Copy its main headline exactly into a new plain text TextEdit document and save it as {HEADLINE}."
    return (prefix + "Write a plain-text note in TextEdit with the exact headline as its title line, then exactly "
            "three bullet lines ('- ' followed by each listed feature in source order), with no other text. "
            f"Save it as {DIR / 'research.txt'}, then use Finder to create {ORGANIZED} if missing and move research.txt into it.")


def active_fixture(name):
    return BROWSER_FORM if name == "browser_form" else BROWSER_SOURCE if name in SOURCE_TRIALS else None


def kill_textedit():
    # Existing personal documents and saved state belong to the founder.
    # Trials must create their own document; never kill an app or erase its state.
    return None


def rm(*paths):
    for p in paths:
        p = Path(p)
        if not _OWNED_RUN or DIR.resolve() not in p.resolve().parents:
            raise ValueError("qa_cleanup_path_not_owned")
        shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)


def verify_note():
    want = "Supymem test note\nGyms lose members quietly\nCheck-ins keep them"
    return exact_text(NOTE, want), "exact file content check"


OWNED_NOTES = OwnedNotesReadback(lambda script: osa(script, preserve_output=True), NOTE_TITLE, EDIT_TITLE, NOTE_BODY, EDIT_LINE)


def verify_notes_app():
    return OWNED_NOTES.verify_create()


def verify_notes_rename():
    return OWNED_NOTES.verify_edit()


def verify_finder():
    expected = "Supymem test note\nGyms lose members quietly\nCheck-ins keep them\n"
    return exact_move(NOTE, ORGANIZED / "supymem-note-final.txt", expected), "exact moved filename and content"


def verify_headline_file():
    return BROWSER_SOURCE.verify_file(HEADLINE) if BROWSER_SOURCE else (None, "source fixture unavailable")


TRIALS = {
    "textedit": dict(
        task=(f"Open TextEdit, create a new plain text document, type the three lines 'Supymem test note' / "
              f"'Gyms lose members quietly' / 'Check-ins keep them', save it as {NOTE}"),
        setup=lambda: (kill_textedit(), rm(NOTE, ORGANIZED)), verify=verify_note),
    "notes": dict(
        task=f"In the Notes app, create a new note titled '{NOTE_TITLE}' whose body after the title is one line: '{NOTE_BODY}'.",
        setup=OWNED_NOTES.prepare_create, verify=verify_notes_app),
    "notes_edit": dict(
        task=f"In the Notes app, open the note titled '{NOTE_TITLE}', rename its title to '{EDIT_TITLE}' and preserve its body line and add a second body line '{EDIT_LINE}'.",
        setup=OWNED_NOTES.prepare_edit, verify=verify_notes_rename),
    "finder": dict(
        task=(f"In Finder, create a folder named 'organized' inside {DIR}, move supymem-note.txt into it, "
              "then rename the moved file to supymem-note-final.txt"),
        setup=lambda: (DIR.mkdir(exist_ok=True), rm(ORGANIZED), NOTE.write_text("Supymem test note\nGyms lose members quietly\nCheck-ins keep them\n")),
        verify=verify_finder),
    "browser": dict(task=lambda: source_task("browser"), setup=setup_browser_source,
                    verify=lambda: (None, "owned browser readback required")),
    "cross_app": dict(task=lambda: source_task("cross_app"),
                      setup=lambda: (rm(HEADLINE), setup_browser_source()), verify=verify_headline_file),
}

PIC = DIR / "pic.jpg"
EXPORT = DIR / "pic-export.png"
RESEARCH = ORGANIZED / "research.txt"


def make_pic():
    from PIL import Image
    DIR.mkdir(exist_ok=True)
    Image.new("RGB", (240, 160), "orange").save(PIC)
    rm(EXPORT)


OWNED_APPS = OwnedAppReadback(RUN_ID, lambda script: osa(script), proof_path=DIR / "calendar-creation-proof.json")

BROWSER_FORM = None


def setup_browser_form():
    global BROWSER_FORM
    if BROWSER_FORM is not None and not BROWSER_FORM.closed:
        raise RuntimeError("qa_form_fixture_already_started")
    BROWSER_FORM = BrowserFormFixture().start()


TRIALS.update({
    "browser_form": dict(task=lambda: BROWSER_FORM.task(), setup=setup_browser_form,
                         verify=lambda: (None, "owned browser readback required")),
    "calendar": dict(
        setup=lambda: OWNED_APPS.prepare("calendar"), verify=lambda: OWNED_APPS.verify_calendar()),
    "calendar_delete": dict(
        setup=lambda: OWNED_APPS.prepare_delete(), verify=lambda: OWNED_APPS.verify_delete()),
    "reminders": dict(
        setup=lambda: OWNED_APPS.prepare("reminders"), verify=lambda: OWNED_APPS.verify_reminder()),
    "preview": dict(
        task=f"Open {PIC} in Preview and export it as a PNG to {EXPORT}",
        setup=make_pic, verify=lambda: (equivalent_png(PIC, EXPORT), "decoded PNG dimensions and exact source pixels")),
    "long": dict(task=lambda: source_task("long"),
                 setup=lambda: (rm(ORGANIZED, DIR / "research.txt"), setup_browser_source()),
                 verify=lambda: BROWSER_SOURCE.verify_file(RESEARCH, source=DIR / "research.txt")
                 if BROWSER_SOURCE else (None, "source fixture unavailable")),
})


LEGACY_TRIALS = tuple(TRIALS)
cu_tasks.install(TRIALS, lambda: DIR)  # catalog: scripts/desktop/cu_tasks (short + long, --category/--only)


def write_record(rec: dict) -> dict:
    from cu_acceptance import source_digest

    current_digest = source_digest()
    unchanged = SOURCE_DIGEST is not None and current_digest == SOURCE_DIGEST
    rec.update(schema_version=2, run_id=RUN_ID, repetition=REPETITION, model=MODEL,
               category=TRIALS.get(rec["trial"], {}).get("category", rec["trial"]),
               source_digest=SOURCE_DIGEST or current_digest, source_unchanged=unchanged, counts_as_real_user=False,
               cursor_acceptance="not_measured")
    if SOURCE_DIGEST is not None and not unchanged:
        rec["passed"] = False
        rec["source_changed_during_run"] = True
    with open(RESULT_FILE, "a") as f:
        f.write(json.dumps(rec) + "\n")
    print("RESULT", json.dumps(rec), flush=True)
    return rec


def method_verified(name, rows):
    """An artifact cannot prove that an explicitly requested input method was used."""
    constrained = {"mb_menu_save_as": "menu", "mb_make_plain": "menu",
                   "kb_shortcuts_only": "keyboard", "kb_select_copy": "keyboard",
                   "di_open_dialog": "dialog", "di_save_new_folder": "dialog",
                   "te_replace_prompt": "dialog", "te_find_replace": "find_replace",
                   "fi_duplicate": "finder_duplicate"}
    if name not in constrained:
        return True
    # Current rows redact menu paths and carry no successful causal key/menu/
    # dialog receipt. A requested action, including a failed or unrelated menu,
    # cannot prove the required method. Keep these contracts unaccepted until
    # exact requested operations and their successful effects are instrumented.
    return False


def cleanup_spec(spec: dict) -> bool:
    """A teardown exception is failed evidence, never a second trial result."""
    if not callable(spec.get("cleanup")):
        return True
    try:
        return spec["cleanup"]() is True
    except Exception:
        return False


def run(name: str, backend: str, *, qa_direct: bool = False) -> dict:
    global _OWNED_RUN
    if not _OWNED_RUN:
        DIR.mkdir(mode=0o700, parents=True, exist_ok=False)
        _OWNED_RUN = True
    spec = TRIALS[name]
    if name == "calendar_delete" and not OWNED_APPS.history_valid():
        rec = {"qa_transport": "direct_mcp_qa" if qa_direct else ("daemon_mcp_qa" if backend == "cua" else "peekaboo_qa"),
               "trial": name, "run_id": RUN_ID, "passed": False, "task_passed": False, "outcome": "skipped_dependency",
               "reason": "calendar_trial_must_pass_first", "model_calls": 0, "verification_status": "unknown"}
        return write_record(rec)
    decoy = None
    stage = "setup"
    try:
        spec["setup"]()
        stage = "task_resolution"
        task = OWNED_APPS.task(name) if name in {"calendar", "calendar_delete", "reminders"} else (spec["task"]() if callable(spec["task"]) else spec["task"])
        from cu_input_decoy import InputDecoy, evidence, public_snapshot
        stage = "decoy_start"
        decoy = InputDecoy(DIR)
        before = decoy.start()
    except BaseException as error:
        cleaned = cleanup_spec(spec)
        fixture = active_fixture(name)
        if fixture is not None:
            try:
                fixture.close()
            except Exception:
                cleaned = False
        if decoy is not None:
            try:
                decoy.close()
            except Exception:
                cleaned = False
        if not isinstance(error, Exception):
            raise
        allowed = {"qa_owned_readback_unavailable", "qa_owned_scope_not_fresh",
                   "qa_calendar_creation_unproven", "qa_calendar_creation_changed", "qa_form_fixture_start_failed",
                   "qa_form_fixture_already_started", "qa_owned_notes_scope_not_fresh", "qa_owned_note_edit_unproven",
                   "qa_decoy_compile_failed", "qa_decoy_focus_unverified", "qa_decoy_monitor_unavailable"}
        reason = str(error) if str(error) in allowed else "qa_" + stage + "_failed"
        rec = {"qa_transport": "direct_mcp_qa" if qa_direct else ("daemon_mcp_qa" if backend == "cua" else "peekaboo_qa"),
               "cursor_acceptance": "not_measured", "trial": name, "passed": False, "outcome": "preflight_failed", "reason": reason,
               "detail": LAST_OSA["error"],
               "model_calls": 0, "background_verified": False, "verification_status": "unknown", "cleanup_verified": cleaned}
        return write_record(rec)
    spec_cleanup_started = fixture_close_started = mac_close_started = trace_close_started = decoy_close_started = False
    try:
        from cu_focus_trace import FocusTrace, traced_call
        from cu_execution_trace import ExecutionTrace, HarnessBindings
        trace = FocusTrace(decoy, frontmost_info)
        execution = ExecutionTrace()
        trace.execution = execution
        trace.start()
        mac = trial.make_mac(backend, qa_direct=True) if qa_direct else trial.make_mac(backend)
        bind_backend = spec.get("bind_backend")
        if callable(bind_backend):
            bind_backend(mac)  # source-owned QA custody, before any model/browser action
        native = getattr(mac, "native", mac)
        if hasattr(native, "call"):
            native.call = traced_call(native.call, trace)
        # Backend/proxy startup is part of the sampled interval; verify focus
        # again before paying for inference. Never reactivate to hide a steal.
        rows: list[dict] = []
        t0 = time.time()
        try:
            try:
                decoy.wait_for_focus(timeout=3)
                if not trace.samples_verified():
                    raise RuntimeError("qa_decoy_focus_unverified")
            except RuntimeError:
                if FOCUS_MODE != "input":
                    raise
                # input mode: other apps may take focus on a busy Mac; the gate is zero input to the decoy
                # and zero foreground escalations by the engine (recorded as focus_mode).
        except RuntimeError:
            result = {"outcome": "preflight_failed", "reason": "qa_decoy_focus_unverified",
                      "model_calls": 0, "steps": 0, "seconds": round(time.time() - t0, 1)}
        else:
            trace.mark({"phase": "model"})
            try:
                with HarnessBindings(execution, mac=mac, trial=trial) as execution_bindings:
                    result = trial.run_task(task, backend=backend, mac=mac, model=MODEL, log_rows=rows, trace=trace.mark, max_steps=45, max_seconds=420, **({"qa_direct": True} if qa_direct else {}))
            except Exception as error:  # a crashed trial is a result too
                result = {"outcome": "crashed", "reason": type(error).__name__, "steps": 0,
                          "seconds": round(time.time() - t0, 1)}
        trace.mark({"phase": "verification"})
        verification_error = None
        try:
            ok, _detail = (BROWSER_FORM.verify(getattr(mac, "browser", None))
                           if name == "browser_form" else spec["verify"]())
            detail = "independent result check"
            if name == "browser":
                ok, _detail = BROWSER_SOURCE.verify_browser(getattr(mac, "browser", None))
                detail = "independent owned source page check"
        except Exception:
            ok, detail, verification_error = None, "verification unavailable", "qa_verification_failed"
        trace.mark({"phase": "teardown"})
        cleanup_ok = result.get("cleanup_verified") is True
        spec_cleanup_started = True
        if not cleanup_spec(spec):
            cleanup_ok = False
        cleanup_components = public_cleanup_codes(result.get("cleanup_codes"))
        cleanup_stages = public_cleanup_trace(result.get("cleanup_trace"))
        if not cleanup_ok and not cleanup_components:
            cleanup_components.append("qa_trial_cleanup_unconfirmed")
        try:
            if hasattr(mac, "close"):
                mac_close_started = True
                mac.close()
        except Exception as error:
            cleanup_ok = False
            cleanup_components.extend(cleanup_codes(error, "native_backend_close_unconfirmed"))
            cleanup_stages.extend(cleanup_trace(error))
        else:
            cleanup_stages.extend(public_cleanup_trace(getattr(mac, "cleanup_trace", None)))
        if active_fixture(name) is not None:
            try:
                fixture_close_started = True
                active_fixture(name).close()
            except Exception:
                cleanup_ok = False
                cleanup_components.append("qa_form_fixture_cleanup_unconfirmed")
            if ok is True:
                ok = active_fixture(name).final_verdict()
        trace.mark({"phase": "complete"})
        trace_close_started = True
        joined = trace.close()
        if joined is not True:
            cleanup_ok = False
            cleanup_components.append("qa_focus_sampler_cleanup_unconfirmed")
        # Retain the actual engine outcome (including stopped), with independent
        # cleanup/verification gates; never rewrite it as successful teardown.
        # Exact owned recipient event counter plus body digest, never character data.
        after = decoy.read()
        decoy_ok = evidence(before, after, decoy.instance, decoy.process.pid)
        share = round(100 * sum(pid == decoy.process.pid for row in trace.samples if (pid := row["cg_pid"]) is not None) / max(len(trace.samples), 1))
        background_ok = (trace.verified(joined) if FOCUS_MODE != "input"
                         else bool(joined) and decoy_ok is True and getattr(mac, "fronted", 0) == 0)
        method_ok = method_verified(name, rows)
        # Only emit a pass after the owned decoy process has actually stopped.
        try:
            decoy_close_started = True
            decoy.close()
            decoy_closed = True
        except Exception:
            cleanup_ok = False
            cleanup_components.append("qa_decoy_cleanup_unconfirmed")
        trace_file = DIR / ("focus-" + uuid.uuid4().hex + ".json")
        trace_file.write_text(json.dumps({"focus": trace.samples, "driver": trace.actions, "phases": trace.phases,
            "execution": execution.events, "execution_complete": execution.complete()
                and "execution_bindings" in locals() and execution_bindings.installation_verified,
            "execution_gaps": ["backend_startup_not_instrumented", "script_app_effect_boundary_unavailable",
                "inline_native_dispatch_boundary_unavailable", "native_sender_causality_unavailable",
                "oracle_and_teardown_transport_not_instrumented"]}))
        trace_file.chmod(0o600)
        steps_file = DIR / ("steps-" + uuid.uuid4().hex + ".json")  # per-step rows (content already withheld by the engine)
        steps_file.write_text(json.dumps(rows, default=str))
        steps_file.chmod(0o600)
        rec = {"qa_transport": "direct_mcp_qa" if qa_direct else ("daemon_mcp_qa" if backend == "cua" else "peekaboo_qa"),
               "cursor_acceptance": "not_measured", "trial": name, "run_id": RUN_ID, "focus_mode": FOCUS_MODE,
               "task_passed": trial_passed(ok, cu_tasks.normalize(name, catalog_outcome(result)), decoy_ok, background_ok) and verification_error is None,
               "oracle_verified_task": ok is True and result.get("outcome") in {"done", "done_unconfirmed"} and decoy_ok is True and verification_error is None,
               "passed": trial_passed(ok, cu_tasks.normalize(name, catalog_outcome(result)), decoy_ok, background_ok) and cleanup_ok and method_ok and verification_error is None, "verified_on_disk": ok is True, "outcome": result.get("outcome"),
               "method_verified": method_ok,
               "reason": result.get("reason"), "verify": detail, "steps": result.get("steps"), "seconds": result.get("seconds"),
               "model_calls": result.get("model_calls"), "cost_micro_credits": result.get("cost_micro_credits"),
               "avg_ms": result.get("avg_ms"), "step_percentiles": result.get("step_ms"),
               "step_ms": [sum(row["ms"].get(key, 0) for key in ("observe", "model", "act")) for row in rows if row.get("ms")],
               "decoy_input_evidence": public_snapshot(after, decoy.instance, decoy.process.pid), "decoy_untouched": decoy_ok,
               "decoy_frontmost_pct": share, "background_verified": background_ok,
               "other_frontmost_pids": sorted({row["cg_pid"] for row in trace.samples if type(row["cg_pid"]) is int and row["cg_pid"] != decoy.process.pid}),
               "focus_trace_file": str(trace_file), "step_log_file": str(steps_file), "focus_sample_count": len(trace.samples),
               "driver_event_count": len(trace.actions), "sampler_joined": joined,
               "execution_event_count": len(execution.events),
               "execution_trace_complete": execution.complete() and "execution_bindings" in locals()
                   and execution_bindings.installation_verified,
               "cleanup_verified": cleanup_ok, "cleanup_codes": sorted(set(cleanup_components)),
               "cleanup_trace": cleanup_stages[-32:],
               "verification_error": verification_error,
               "verifier_diagnostic_file": result.get("verifier_diagnostic_file"),
               "verifier_diagnostic_count": result.get("verifier_diagnostic_count", 0),
               "verification_status": "unknown" if ok is None else ("passed" if ok is True else "failed"),
               "first_focus_mismatch": next((row for row in trace.samples if row["cg_pid"] != decoy.process.pid or row["active_app_pid"] != decoy.process.pid or row["decoy_is_active"] is not True), None),
               "foreground_escalations": getattr(mac, "fronted", 0)}
        if active_fixture(name) is not None:
            rec["owned_fixture_receipt"] = active_fixture(name).public_receipt()
        teardown_complete = True
        return write_record(rec)
    finally:
        # Preserve an existing task exception while making cleanup uncertainty
        # explicit when it is the only failure. Never close a shared daemon.
        primary_error = sys.exc_info()[1]
        cleanup_error = False
        if not fixture_close_started and active_fixture(name) is not None:
            try:
                fixture_close_started = True
                active_fixture(name).close()
            except Exception:
                cleanup_error = True
        if not spec_cleanup_started:
            spec_cleanup_started = True
            if not cleanup_spec(spec):
                cleanup_error = True
        try:
            if not mac_close_started and "mac" in locals() and hasattr(mac, "close"):
                mac_close_started = True
                if "trace" in locals():
                    trace.mark({"phase": "teardown"})
                mac.close()
        except Exception:
            cleanup_error = True
        try:
            if not trace_close_started and "trace" in locals():
                trace_close_started = True
                trace.mark({"phase": "teardown"})
                if not trace.close():
                    cleanup_error = True
        except Exception:
            cleanup_error = True
        try:
            if not decoy_close_started:
                decoy_close_started = True
                decoy.close()
        except Exception:
            cleanup_error = True
        if cleanup_error and primary_error is None:
            raise RuntimeError("qa_owned_cleanup_unconfirmed") from None



def catalog_outcome(result: dict):
    """Engine outcome in the catalog's vocabulary: an honest stop-and-ask / block / decline is a refusal."""
    if result.get("outcome") == "failed":
        return {"needs_founder": "needs_user", "blocked_security_app": "blocked",
                "model_reported_failure": "declined"}.get(result.get("reason"), "failed")
    return result.get("outcome")


def decoy_unchanged(display: str) -> bool:
    """Missing observation is unknown, and 10/100 are changed values."""
    return display.strip() in {"0", "0 0"}


def trial_passed(verified, outcome: str, decoy_ok: bool, background_ok: bool = False) -> bool:
    return verified is True and outcome == "done" and decoy_ok is True and background_ok is True


def verification_counts(records: list[dict]) -> dict:
    """Unknown/unperformed checks remain failed acceptance, never pending passes."""
    return {"total": len(records), "passed": sum(r.get("passed") is True for r in records),
            "failed": sum(r.get("passed") is not True for r in records),
            "task_passed": sum(r.get("task_passed") is True for r in records),
            "verification_unknown": sum(r.get("verification_status") not in {"passed", "failed"} for r in records)}


def main() -> int:
    global MODEL, FOCUS_MODE, RESULT_FILE, REPETITION, SOURCE_DIGEST

    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="comma-separated trial ids (legacy or catalog); default: legacy trials")
    ap.add_argument("--category", default="", help="catalog categories, e.g. browser,finder,safety")
    ap.add_argument("--long", action="store_true", help="select only long-horizon catalog tasks (with --category/--only: narrows them)")
    ap.add_argument("--list", action="store_true", help="print the selected trial ids and exit")
    ap.add_argument("--backend", default="cua")
    ap.add_argument("--qa-direct", action="store_true", help="QA only: explicit direct MCP runtime; no daemon fallback")
    ap.add_argument("--key-file", default="", help="0600 file holding the OpenAI key; loaded into this process env only, never printed")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", type=Path, default=None, help="Fresh JSONL output file; existing files are refused")
    ap.add_argument("--repetition", type=int, default=1, help=argparse.SUPPRESS)
    ap.add_argument("--focus-mode", choices=("frontmost", "input"), default="frontmost",
                    help="input: do not require the decoy to stay frontmost (busy Mac); require zero decoy input and no foreground escalation")
    args = ap.parse_args()
    MODEL = args.model
    FOCUS_MODE = args.focus_mode
    REPETITION = args.repetition
    if args.runs < 1 or args.repetition < 1:
        ap.error("--runs must be positive")
    if args.key_file:
        keyfile = Path(args.key_file).expanduser()
        if keyfile.exists():
            if keyfile.stat().st_mode & 0o077:
                sys.exit("key file must be 0600")
            import os
            os.environ["OPENAI_DEVICE_VISION_API_KEY"] = keyfile.read_text().strip()
    if args.category or args.long:
        names = [t.id for t in cu_tasks.select(args.category, args.only, long=True if args.long else None)]
    else:
        names = [n for n in args.only.split(",") if n] or [task.id for task in cu_tasks.select(long=False)]
    if not names or any(name not in TRIALS for name in names) or len(set(names)) != len(names):
        ap.error("selection must contain known, unique trial IDs")
    if args.list:
        print("\n".join(names))
        return 0
    from cu_acceptance import source_digest
    SOURCE_DIGEST = source_digest()
    RESULT_FILE = args.out or Path("/tmp") / f"supymem-cu-desktop-{RUN_ID}.jsonl"
    RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
    try:
        RESULT_FILE.touch(mode=0o600, exist_ok=False)
    except FileExistsError:
        ap.error("output already exists; choose a fresh path to avoid mixing evidence")
    if args.runs > 1:
        # Each repetition is a fresh process: owned-object names and run dirs are per-run, never reused.
        failed = False
        for repetition in range(1, args.runs + 1):
            output = RESULT_FILE.with_name(RESULT_FILE.stem + f"-run{repetition}.jsonl")
            result = subprocess.run([sys.executable, __file__, "--only", ",".join(names), "--backend", args.backend, "--model", args.model, "--focus-mode", args.focus_mode,
                                     "--out", str(output), "--repetition", str(repetition)]
                           + (["--qa-direct"] if args.qa_direct else []) + (["--key-file", args.key_file] if args.key_file else []))
            failed = failed or result.returncode != 0
            if output.is_file():
                with RESULT_FILE.open("a") as combined:
                    combined.write(output.read_text())
        print("ARTIFACT", str(RESULT_FILE), flush=True)
        from cu_acceptance import evaluate
        try:
            rows = [json.loads(line) for line in RESULT_FILE.read_text().splitlines() if line.strip()]
            report = evaluate(rows, names, suite="desktop", repetitions=args.runs, digest=source_digest())
        except (OSError, ValueError):
            return 2
        print("ACCEPTANCE", json.dumps(report), flush=True)
        return 1 if failed or not report["accepted"] else 0
    from cu_acceptance import desktop_lease
    try:
        with desktop_lease():
            records = run_selected(names, args)
    except ValueError as error:
        print("SUITE", json.dumps({"accepted": False, "reason": str(error)}), flush=True)
        return 2
    print("SUITE", json.dumps(verification_counts(records)), flush=True)
    print("ARTIFACT", str(RESULT_FILE), flush=True)
    return 0 if records and all(rec.get("passed") is True for rec in records) else 1


def run_selected(names, args):
    sweep_owned_ui()
    records = []
    for name in names:
        try:
            rec = run(name, args.backend, qa_direct=args.qa_direct)
        except Exception as error:
            # Never lose the remaining coverage or log an exception's private text.
            rec = write_record({"trial": name, "passed": False, "outcome": "crashed", "reason": type(error).__name__,
                                "cleanup_verified": False, "verification_status": "unknown"})
        sweep_owned_ui()
        # Never erase failed preflight attempts from a score by silently retrying.
        records.append(rec)
    return records


if __name__ == "__main__":
    raise SystemExit(main())
