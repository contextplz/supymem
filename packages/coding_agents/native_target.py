"""Resolve a reviewed bundle ID without Hermes' name/title fallbacks.

Inputs must be fresh trusted native inventory, never model-supplied observations.
This is target identity only: it confers no permission, origin authorization,
foreground authority, or execution admission. The caller must own a private
backend and use the exact returned pid/window pair, then verify it again before
an effect. No raw computer-use handler is registered here.
"""
from dataclasses import dataclass
import re


class TargetUnavailable(PermissionError):
    pass


@dataclass(frozen=True)
class NativeTarget:
    bundle_id: str
    pid: int
    window_id: int


def resolve_target(bundle_id: str, applications: list, windows: list, *, previous: NativeTarget | None = None) -> NativeTarget:
    """Exact identity; multiple windows require a previously observed binding.

    No z-order, frontmost-window, display-name or substring heuristic is authority.
    A vanished binding must be captured/reviewed again rather than retargeting the
    next effect. PID reuse cannot be excluded by this inventory shape; the native
    owner still needs a process-instance identity for a durable execution lease.
    """
    if (not isinstance(bundle_id, str) or not re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", bundle_id)
            or not isinstance(applications, list) or not isinstance(windows, list)
            or len(applications) > 2048 or len(windows) > 8192):
        raise TargetUnavailable("native_target_inventory_invalid")
    identities = {}
    for app in applications:
        if not isinstance(app, dict) or app.get("running") is False:
            continue
        pid = app.get("pid")
        identity = app.get("bundle_id", app.get("bundleId"))
        if type(pid) is not int or pid <= 0 or not isinstance(identity, str):
            continue
        if app.get("bundle_id") and app.get("bundleId") and app["bundle_id"] != app["bundleId"]:
            raise TargetUnavailable("native_target_identity_conflict")
        identities.setdefault(pid, set()).add(identity)
    pids = {pid for pid, values in identities.items() if values == {bundle_id}}
    if not pids:
        raise TargetUnavailable("native_target_application_unverified")
    targets = set()
    for window in windows:
        if not isinstance(window, dict) or window.get("off_screen") is True:
            continue
        pid, window_id = window.get("pid"), window.get("window_id")
        if type(pid) is not int or type(window_id) is not int or pid not in pids or window_id <= 0:
            continue
        # A window's reported bundle identity, when present, cannot contradict
        # the independent application inventory. A title is never considered.
        if any(window.get(key) not in (None, bundle_id) for key in ("bundle_id", "bundleId")):
            raise TargetUnavailable("native_target_identity_conflict")
        targets.add(NativeTarget(bundle_id, pid, window_id))
    if previous is not None:
        if not isinstance(previous, NativeTarget) or previous.bundle_id != bundle_id or previous not in targets:
            raise TargetUnavailable("native_target_binding_stale")
        return previous
    if len(targets) != 1:
        raise TargetUnavailable("native_target_window_ambiguous" if targets else "native_target_window_missing")
    return next(iter(targets))
