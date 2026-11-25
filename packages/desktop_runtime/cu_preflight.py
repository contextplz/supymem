"""Permission preflight: find missing macOS grants BEFORE the task starts, not as a mid-task failure.

`check(goal, mac)` returns a list of plain guidance strings (empty = ready). The engine turns a non-empty list into a
needs-you pause with the exact System Settings path. Probes only ask each app a harmless question (its name / how
many calendars it has); they never read titles or content, and nothing is logged.
"""
from __future__ import annotations

import re
import subprocess

from coding_agents.cu_ai_tools import SCRIPTABLE

# app -> (harmless probe expression, extra privacy pane that also gates it)
_PROBES = {
    "textedit": ("name", None), "notes": ("count of folders", None), "finder": ("name", None),
    "calendar": ("count of calendars", "Calendars"), "reminders": ("count of lists", "Reminders"),
    "safari": ("name", None), "mail": ("name", None), "preview": ("name", None), "pages": ("name", None),
    "numbers": ("name", None), "keynote": ("name", None), "music": ("name", None), "photos": ("name", None),
}
_TITLE = {"textedit": "TextEdit", "notes": "Notes", "finder": "Finder", "calendar": "Calendar", "reminders": "Reminders",
          "safari": "Safari", "mail": "Mail", "preview": "Preview", "pages": "Pages", "numbers": "Numbers",
          "keynote": "Keynote", "music": "Music", "photos": "Photos"}
DENIED = re.compile(r"-1743|not authorized|not allowed|-25211|-1744")


def apps_named(goal: str) -> list[str]:
    text = goal.lower()
    return [name for name in SCRIPTABLE if re.search(rf"\b{name}\b", text)]


def probe_automation(app: str, timeout: float = 25.0) -> str:
    """granted | denied | pending (a prompt is waiting for the founder) | unknown."""
    expr = _PROBES[app][0]
    try:
        proc = subprocess.run(["osascript", "-e", f'tell application "{_TITLE[app]}" to return {expr}'],
                              capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return "pending"
    except OSError:
        return "unknown"
    if proc.returncode == 0:
        return "granted"
    return "denied" if DENIED.search(proc.stderr or "") else "unknown"


def native_permissions(mac) -> dict | None:
    call = getattr(getattr(mac, "native", mac), "call", None)
    if not callable(call):
        return None
    try:
        value = call("check_permissions", {})
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def check(goal: str, mac=None, *, probe=probe_automation, permissions=native_permissions,
          surfaces=(), strict=False) -> list[str]:
    problems = []
    perms = permissions(mac) if mac is not None else None
    if perms is not None:
        if perms.get("accessibility") is False or strict and perms.get("accessibility") is not True:
            problems.append("Turn on Supymem in System Settings > Privacy & Security > Accessibility.")
        if perms.get("screen_recording") is False or strict and perms.get("screen_recording") is not True:
            problems.append("Turn on Supymem in System Settings > Privacy & Security > Screen & System Audio Recording.")
        if strict and perms.get("automation") is False:
            problems.append("You need to allow Supymem to control your selected app in System Settings > Privacy & Security > Automation.")
    elif strict:
        problems.append("You need to reopen your Mac app and check Accessibility and Screen Recording in System Settings > Privacy & Security before you retry.")
    for app in apps_named(getattr(mac, "app", "") if strict else goal):
        state = probe(app)
        if state in {"denied", "pending"} or strict and state != "granted":
            pane = _PROBES[app][1]
            extra = f" and System Settings > Privacy & Security > {pane}" if pane else ""
            problems.append(f"Allow Supymem to control {_TITLE[app]}: System Settings > Privacy & Security > "
                            f"Automation > Supymem > {_TITLE[app]}{extra}.")
    if strict and not problems:
        from desktop_runtime.cu_ai_cua import _refusal
        call = getattr(mac, "call", None)
        for pid, window_id in surfaces:
            try:
                rows = call("list_windows", {"pid": pid, "on_screen_only": False}).get("windows")
                matches = [row for row in rows if isinstance(row, dict)
                           and row.get("pid") == pid and row.get("window_id") == window_id]
                if (len(matches) != 1 or matches[0].get("is_on_screen") is not True
                        or matches[0].get("is_minimized") is True):
                    raise ValueError("window_unreachable")
                state = call("get_window_state", {"pid": pid, "window_id": window_id,
                                                  "max_dimension": 960, "max_elements": 400, "max_depth": 20})
                if (type(state.get("pid")) is not int or state["pid"] != pid
                        or type(state.get("window_id")) is not int or state["window_id"] != window_id
                        or not state.get("snapshot_id") or _refusal(state)
                        or state.get("error") or not (state.get("elements") or state.get("screenshot_png_b64"))):
                    raise ValueError("window_unreachable")
            except Exception:
                problems.append("You need to restore this window and retry. If you reopened or moved it, select it again and review the task. I will not bring it to the foreground for you.")
                break
    return problems


def recovery_prompt(code: str) -> str:
    if code == "desktop_cua_host_permissions_missing":
        return "You need to allow Supymem in System Settings > Privacy & Security > Accessibility and Screen & System Audio Recording, then retry. Check Automation for your selected app too."
    if code in {"desktop_window_identity_changed", "desktop_app_identity_changed", "off_space_or_ax_unresolved", "minimized_or_hidden_window"}:
        return "You need to restore this window and retry. If you reopened it, select it again and review the task. I will not bring it to the foreground for you."
    return "You need to reopen your Mac app and review this task again before you retry. I have not started a new input."


def prompt_for(problems: list[str]) -> str:
    return ("Supymem needs a macOS permission before it can start. " + " ".join(problems)
            + " Then click Continue; the task starts by itself once it is granted.")
