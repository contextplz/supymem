"""Private local crash records and bounded, identity-bound crash summaries."""

from __future__ import annotations

import json
import hashlib
import logging
import logging.handlers
import os
import platform
import re
import stat
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from core.runtime_diagnostics import MAX_RUNTIME_MS, RUNTIME_REASONS, compatible_runtime_diagnostic
from desktop_runtime import __version__

QUEUE_DIRNAME = "crash_queue"
OWNER_FILENAME = "crash_owner.json"
PREFS_FILENAME = "crash_report_prefs.json"
INSTALL_ID_FILENAME = "crash_install_id.txt"
MAX_STACK_CHARS = 64_000
MAX_LOGS_CHARS = 32_000
MAX_BODY_CHARS = 8_000
MAX_TITLE_CHARS = 200
MAX_CONTACT_CHARS = 200
SKIP_LOG_NAMES = {
    ".env",
    ".env.local",
    "hosted_account.json",
    "credit_lease.json",
    "open_token.txt",
}

log = logging.getLogger("desktop_runtime.error_event")
_SLUG = re.compile(r"[a-z][a-z0-9_]{0,39}\Z")
_LABELS = {"Mac native_failure", "Mac uncaught_exception", "Mac process_crashed"}
_NATIVE_FAILURE_CODES = {"desktop_cua_observation_unpinned", "desktop_cua_transport_unavailable",
                         "cu_browser_decision_unavailable"}
_log_ready = False

_orig_excepthook = None
_orig_threading_hook = None


def _data_dir() -> Path:
    override = (os.environ.get("COMPANY_OS_DATA_DIR") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    from desktop_runtime.paths import data_root

    return data_root(create=True)


def queue_dir(root: Path | None = None) -> Path:
    dest = (root or _data_dir()) / QUEUE_DIRNAME
    dest.mkdir(parents=True, exist_ok=True, mode=0o700)
    dest.chmod(0o700)
    return dest


def crash_owner(root: Path | None = None) -> dict[str, str] | None:
    """The verified native handoff's local owner marker; never sent to telemetry."""
    try:
        raw = json.loads(((root or _data_dir()) / OWNER_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if (isinstance(raw, dict) and isinstance(raw.get("account_id"), str)
            and raw["account_id"].startswith("usr_") and len(raw["account_id"]) <= 128
            and isinstance(raw.get("device_id"), str)
            and 0 < len(raw["device_id"]) <= 128):
        return {"account_id": raw["account_id"], "device_id": raw["device_id"]}
    return None


def save_crash_owner(account_id: str, device_id: str, root: Path | None = None) -> None:
    base = root or _data_dir()
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / OWNER_FILENAME
    temp = base / f".{OWNER_FILENAME}.{uuid.uuid4().hex}"
    try:
        with open(temp, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
            json.dump({"account_id": account_id, "device_id": device_id}, file)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def clear_crash_owner(root: Path | None = None) -> None:
    ((root or _data_dir()) / OWNER_FILENAME).unlink(missing_ok=True)


def app_version() -> str:
    for name in ("SUPYMEM_APP_VERSION", "COMPANY_OS_APP_VERSION", "SUPYMEM_VERSION"):
        if version := os.environ.get(name, "").strip():
            return version
    return __version__


def os_label() -> str:
    return f"{platform.system()}-{platform.release()}-{platform.machine()}".strip("-")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def install_id(root: Path | None = None) -> str:
    path = (root or _data_dir()) / INSTALL_ID_FILENAME
    if path.is_file():
        existing = path.read_text(encoding="utf-8").strip()
        if existing:
            return existing
    value = str(uuid.uuid4())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value + "\n", encoding="utf-8")
    return value


def load_prefs(root: Path | None = None) -> dict[str, Any]:
    path = (root or _data_dir()) / PREFS_FILENAME
    if not path.is_file():
        return {"include_account_id": False}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"include_account_id": False}
    if not isinstance(raw, dict):
        return {"include_account_id": False}
    return {"include_account_id": bool(raw.get("include_account_id"))}


def save_prefs(*, include_account_id: bool, root: Path | None = None) -> dict[str, Any]:
    prefs = {"include_account_id": bool(include_account_id)}
    path = (root or _data_dir()) / PREFS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2) + "\n", encoding="utf-8")
    return prefs


def _clip(value: str | None, limit: int) -> str:
    text = value or ""
    if len(text) <= limit:
        return text
    return text[: limit - 3] + "..."


def collect_safe_logs(root: Path | None = None, *, limit: int = MAX_LOGS_CHARS) -> str:
    """Attach recent log text. Never reads env, leases, or account session."""
    base = root or _data_dir()
    chunks: list[str] = []
    remaining = limit
    candidates = [
        base / "runtime.log",
        base / "logs" / "runtime.log",
        base / "logs" / "core.log",
    ]
    for path in candidates:
        if remaining <= 0:
            break
        if not path.is_file() or path.name in SKIP_LOG_NAMES:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not text.strip():
            continue
        slice_ = text[-remaining:]
        chunks.append(f"--- {path.name} ---\n{slice_}")
        remaining -= len(slice_)
    return "\n".join(chunks)[:limit]


def build_payload(
    *,
    kind: str,
    stack: str = "",
    title: str | None = None,
    body: str | None = None,
    logs: str | None = None,
    contact: str | None = None,
    include_account_id: bool | None = None,
    occurred_at: str | None = None,
    root: Path | None = None,
) -> dict[str, Any]:
    if kind not in {"crash", "user"}:
        raise ValueError("kind must be crash or user")
    prefs = load_prefs(root)
    opted_in = prefs["include_account_id"] if include_account_id is None else bool(include_account_id)
    payload: dict[str, Any] = {
        "kind": kind,
        "app_version": app_version(),
        "os": os_label(),
        "occurred_at": occurred_at or _now_iso(),
        "stack": _clip(stack, MAX_STACK_CHARS),
        "title": _clip(title, MAX_TITLE_CHARS) or None,
        "body": _clip(body, MAX_BODY_CHARS) or None,
        "logs": _clip(logs, MAX_LOGS_CHARS) or None,
        "contact": _clip(contact, MAX_CONTACT_CHARS) or None if kind == "user" else None,
        "install_id": install_id(root),
        "include_account_id": bool(opted_in),
        "anonymous": not opted_in,
    }
    return payload


def enqueue(payload: Mapping[str, Any], *, root: Path | None = None) -> Path:
    """Persist a report locally. Does not perform HTTP."""
    kind = str(payload.get("kind") or "crash")
    dest = queue_dir(root)
    name = f"{kind}-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid.uuid4().hex[:8]}.json"
    path = dest / name
    # Owner-only records and a bounded queue; the bridge sends fixed summaries only.
    with open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600)) as file:
        file.write(json.dumps(dict(payload), indent=2) + "\n")
    for stale in sorted(dest.glob("*.json"), key=lambda item: item.stat().st_mtime)[:-50]:
        stale.unlink(missing_ok=True)
    return path


def _error_log_directory() -> Path:
    override = (os.environ.get("COMPANY_OS_RUNTIME_LOG_DIR") or "").strip()
    return Path(override).expanduser().resolve() if override else Path.home() / "Library" / "Logs" / "Supymem"


class _PrivateRuntimeLog(logging.handlers.RotatingFileHandler):
    def _open(self):
        stream = super()._open()
        os.chmod(self.baseFilename, 0o600)
        return stream


def _ensure_error_log() -> None:
    """Rotating ~/Library/Logs/Supymem/runtime.log (the sidecar's stdio is discarded by the shell)."""
    global _log_ready
    if _log_ready:
        return
    _log_ready = True
    try:
        directory = _error_log_directory()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        handler = _PrivateRuntimeLog(directory / "runtime.log", maxBytes=1_000_000, backupCount=1)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        log.addHandler(handler)
        log.setLevel(logging.INFO)
    except OSError:
        pass


class RuntimeDiagnostics:
    """One worker's phase clock. The first failure survives cleanup and settlement."""
    _PHASES = {"starting": "starting", "claiming": "claiming", "admitting_action": "claiming",
        "opening_browser": "launching", "observing": "observing", "reviewing_screen": "observing",
        "deciding": "deciding", "acting": "acting", "running_coding": "acting",
        "running_terminal": "acting", "running_inspect": "acting", "awaiting_native_navigation": "acting",
        "needs_you": "waiting", "attention_required": "waiting", "foreground_handoff": "waiting",
        "verifying": "verifying", "closing": "cleanup", "reconciliation_required": "settling"}

    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._started = self._phase_started = clock()
        self._phase = "starting"
        self._failure = None

    def snapshot(self, reason=None) -> dict:
        if self._failure is not None:
            return dict(self._failure)
        return self._current_snapshot(reason)

    def _current_snapshot(self, reason=None) -> dict:
        now = self._clock()
        return {"phase": self._phase, "reason": reason if reason in RUNTIME_REASONS else "runtime_failure",
            "phase_ms": min(MAX_RUNTIME_MS, max(0, int((now - self._phase_started) * 1000))),
            "elapsed_ms": min(MAX_RUNTIME_MS, max(0, int((now - self._started) * 1000)))}

    def phase(self, state, *, reason=None) -> None:
        try:
            if reason is not None and self._failure is None:
                self._failure = self.snapshot(reason)
            phase = self._PHASES.get(state)
            if phase is None or phase == self._phase:
                return
            completed = self._current_snapshot()
            completed.pop("reason")
            _ensure_error_log()
            log.info("runtime_phase %s", json.dumps(completed, sort_keys=True))
            self._phase, self._phase_started = phase, self._clock()
        except Exception:  # Diagnostics never change task authority or cleanup.
            pass


def report_error(source: str, intent: str, *, code: str | None = None, label: str = "Mac native_failure",
                 root: Path | None = None, runtime_diagnostic: dict | None = None, exception_details: dict | None = None) -> None:
    """Standard error event: structured log line plus a closed ui.error summary queued for core.

    ``source`` is the failing part, ``intent`` what it was doing, ``code`` a closed failure slug. The queue
    record is uploaded by the bridge (owner-matched, via /api/telemetry); never raises.
    """
    try:
        source, intent = (v if _SLUG.fullmatch(v) else "other" for v in (source, intent))
        code = code if isinstance(code, str) and re.fullmatch(r"[a-z0-9_]{1,80}", code) else None
        label = label if label in _LABELS else "Mac native_failure"
        diagnostic = compatible_runtime_diagnostic(runtime_diagnostic, source, intent, label)
        from desktop_runtime.cu_public_errors import safe_exception_details
        details = safe_exception_details(exception_details)
        _ensure_error_log()
        log.error("error_event %s", json.dumps({"component": "desktop_runtime", "source": source, "intent": intent,
                                                  "error_code": code, "app_version": app_version(), "t": _now_iso(),
                                                  **({"runtime_diagnostic": diagnostic} if diagnostic else {}),
                                                  **({"exception_details": details} if details else {})}))
        payload = build_payload(kind="crash", stack="", title="Desktop runtime error", root=root)
        payload.update({"crash_owner": crash_owner(root), "crash_process": "python", "error_source": source,
                        "error_intent": intent, "error_label": label, "error_code": code})
        if diagnostic:
            payload["runtime_diagnostic"] = diagnostic
        if details:
            payload["exception_details"] = details
        enqueue(payload, root=root)
    except Exception:  # noqa: BLE001 - reporting never breaks the work being reported
        pass


def enqueue_crash(exc: BaseException | None = None, *, stack: str | None = None, root: Path | None = None) -> Path:
    from desktop_runtime.hosted import _is_local

    text = ""
    if not _is_local():
        text = stack or ("".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                         if exc is not None else "".join(traceback.format_exception(*sys.exc_info())))
    payload = build_payload(kind="crash", stack=text, title="Desktop crash", root=root)
    payload["crash_owner"] = crash_owner(root)
    payload["crash_process"] = "python"
    payload.update({"error_source": "desktop_runtime", "error_intent": "run_runtime", "error_label": "Mac uncaught_exception"})
    return enqueue(payload, root=root)


def local_crash_summaries() -> list[dict[str, str]]:
    directory = _data_dir() / QUEUE_DIRNAME
    try:
        paths = sorted(directory.iterdir())
    except FileNotFoundError:
        return []
    summaries = []
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    for path in [path for path in paths if path.match("crash-*.json")][-50:]:
        if path.is_symlink() or not path.is_file():
            continue
        try:
            with os.fdopen(os.open(path, flags), "rb") as file:
                metadata = os.fstat(file.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 128_000:
                    continue
                contents = file.read(128_001)
            raw = json.loads(contents) if len(contents) <= 128_000 else None
            if isinstance(raw, dict) and raw.get("kind") == "crash" and raw.get("crash_process") in ("python", "rust"):
                summaries.append({"process": raw["crash_process"],
                                  "recorded_at": datetime.fromtimestamp(metadata.st_mtime, timezone.utc).isoformat()})
        except (FileNotFoundError, ValueError, OverflowError):
            continue
    return summaries


def _receipt_uuid(value: str) -> str:
    """Stable v4-shaped retry key, derived only from the private queue filename."""
    digest = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0f) | 0x40
    digest[8] = (digest[8] & 0x3f) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def pending_crash_summaries(account_id: str, device_id: str, root: Path | None = None) -> list[tuple[Path, dict[str, Any]]]:
    """Read only owner-matched records; no stack, logs, title, or contact leaves this function."""
    owner = {"account_id": account_id, "device_id": device_id}
    result: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(queue_dir(root).glob("crash-*.json"))[:50]:
        try:
            if path.stat().st_size > 128_000:
                continue
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or raw.get("kind") != "crash" or raw.get("crash_owner") != owner:
            continue
        version = raw.get("app_version")
        if not isinstance(version, str) or not re.fullmatch(r"\d{1,4}\.\d{1,4}\.\d{1,4}", version):
            version = None
        # Rust's panic hook also runs for caught panics; Python's thread hook
        # can run while the rest of the app stays alive. Neither proves that
        # the Mac process terminated.
        process = raw.get("crash_process")
        label = {"rust": "Mac panic_observed", "python": "Mac uncaught_exception"}.get(
            process if isinstance(process, str) else "", "Mac native_failure")
        if raw.get("error_label") in _LABELS:
            label = raw["error_label"]
        extra = {key: raw[key] for key in ("error_source", "error_intent")
                 if isinstance(raw.get(key), str) and _SLUG.fullmatch(raw[key])}
        feature = "computer_use" if extra.get("error_source") == "device_worker" and extra.get("error_intent") == "run_mac_task" else "settings"
        failure_code = raw.get("error_code")
        failure_code = failure_code if (feature == "computer_use" and label == "Mac native_failure"
            and isinstance(failure_code, str) and failure_code in _NATIVE_FAILURE_CODES) else "native_error"
        diagnostic = compatible_runtime_diagnostic(raw.get("runtime_diagnostic"),
            extra.get("error_source"), extra.get("error_intent"), label)
        result.append((path, {"event": "ui.error", "event_id": _receipt_uuid(device_id + ":" + path.name),
            "context_subject": account_id, "workspace_id": None, "venture_id": None,
            "props": {"feature": feature, "platform": "macos", "app_version": version,
                "session_id": _receipt_uuid("session:" + device_id + ":" + path.name), "error_code": failure_code,
                "error_context": "native", "error_text": label, **extra,
                **({"runtime_diagnostic": diagnostic} if diagnostic else {})}}))
    return result


def enqueue_user_report(
    *,
    title: str,
    body: str,
    contact: str | None = None,
    attach_logs: bool = False,
    include_account_id: bool | None = None,
    root: Path | None = None,
) -> Path:
    logs = collect_safe_logs(root) if attach_logs else None
    payload = build_payload(
        kind="user",
        title=title,
        body=body,
        contact=contact,
        logs=logs,
        include_account_id=include_account_id,
        root=root,
    )
    return enqueue(payload, root=root)


def _excepthook(exc_type: type[BaseException], exc: BaseException, tb: Any) -> None:
    from desktop_runtime.hosted import _is_local

    try:
        enqueue_crash(stack=None if _is_local() else "".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass
    if _is_local():
        return
    hook = _orig_excepthook or sys.__excepthook__
    hook(exc_type, exc, tb)


def _thread_hook(args: threading.ExceptHookArgs) -> None:
    from desktop_runtime.hosted import _is_local

    try:
        stack = None if _is_local() else "".join(
            traceback.format_exception(args.exc_type, args.exc_value, args.exc_traceback)
        )
        enqueue_crash(stack=stack)
    except Exception:
        pass
    if not _is_local() and _orig_threading_hook is not None:
        _orig_threading_hook(args)


def install_exception_hooks() -> None:
    global _orig_excepthook, _orig_threading_hook
    if sys.excepthook is not _excepthook:
        _orig_excepthook = sys.excepthook
        sys.excepthook = _excepthook
    if getattr(threading, "excepthook", None) is not None and threading.excepthook is not _thread_hook:
        _orig_threading_hook = threading.excepthook
        threading.excepthook = _thread_hook
