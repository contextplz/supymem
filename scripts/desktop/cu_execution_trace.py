"""QA-only execution intervals. Invocation and process creation are different facts.

No arguments, results, error prose, file paths, labels, HTTP data, or screenshots
enter these receipts. These diagnostic intervals never authorize an action or
alter the stationary focus verdict. They do not prove a caller caused focus.
"""
from __future__ import annotations

from contextlib import contextmanager
from functools import wraps
import threading
import time


KINDS = frozenset({"tool_invocation", "observation_invocation", "readback_invocation",
                  "native_transport_invocation", "subprocess_constructor",
                  "inline_native_invocation", "provider_request", "overlay_hint_invocation"})
TOOLS = frozenset({"read_text", "write_text", "mkdir", "list", "stat", "move", "rename", "copy"})
APPS = frozenset({"textedit", "mail", "numbers", "notes", "calendar", "reminders",
                  "preview", "finder"})
NATIVE = frozenset({"launch_app", "list_apps", "list_windows", "get_window_state",
                   "click", "double_click", "right_click", "drag", "type_text",
                   "set_value", "hotkey", "press_key", "scroll", "invoke_menu"})
HINTS = frozenset({"move", "click", "type", "scroll", "read", "begin", "done", "fail", "end"})
TEMPLATES = frozenset({"textedit_new_save", "text_file_open", "notes_create", "notes_rename", "notes_append",
    "calendar_create_event", "calendar_delete_event", "reminders_create_list", "reminders_add",
    "reminders_complete", "preview_export", "finder_mkdir", "finder_move", "finder_rename", "finder_copy",
    "clipboard_write", "clipboard_read", "textedit_edit", "textedit_copy_save", "mail_draft", "numbers_csv"})
OPERATIONS = TOOLS | APPS | NATIVE | HINTS | TEMPLATES | frozenset({
    "typed_readback", "file_readback", "exact_file_readback", "image_export", "observe",
    "script_executor", "image_executor", "responses"})


def admitted(value, choices):
    return value if type(value) is str and value in choices else "other"


def positive_int(value):
    return value if type(value) is int and 0 < value < 2**63 else None


def result_class(value):
    """Static classification only; a success prefix is not an effect receipt."""
    if type(value) is str:
        if value.startswith("refused:"):
            return "refused"
        if value.startswith(("script cancelled", "template cancelled", "fs cancelled")):
            return "uncertain"
        if value.startswith("script timed out"):
            return "uncertain"
    return "returned"


class ExecutionTrace:
    def __init__(self, *, max_events=4000, clock=time.monotonic):
        self.max_events, self.clock = max_events, clock
        self.events = []
        self.lock = threading.RLock()
        self.sequence = 0
        self.owner_thread = threading.get_ident()
        self.stack = []
        self.overflow = False
        self.custody_verified = True

    def _append(self, row):
        if len(self.events) < self.max_events:
            self.events.append(row)
        else:
            self.overflow = True

    def active_ids(self):
        with self.lock:
            return [item["span_id"] for item in self.stack]

    @contextmanager
    def span(self, kind, operation, *, target_pid=None, window_id=None, allow_process=False):
        if threading.get_ident() != self.owner_thread:
            yield None
            return
        with self.lock:
            self.sequence += 1
            item = {"span_id": self.sequence, "parent_span_id": self.stack[-1]["span_id"] if self.stack else None,
                    "kind": admitted(kind, KINDS), "operation": admitted(operation, OPERATIONS),
                    "allow_process": allow_process, "confirmed_processes": 0, "outcome": "returned"}
            metadata = {key: item[key] for key in ("span_id", "parent_span_id", "kind", "operation")}
            for key, value in (("target_pid", target_pid), ("window_id", window_id)):
                if positive_int(value) is not None:
                    metadata[key] = value
            self.stack.append(item)
            self._append({**metadata, "event_stage": "begin", "at_monotonic": self.clock(),
                          "proof": "invocation_only"})
        try:
            yield item
        except BaseException:
            item["outcome"] = "exception"
            raise
        finally:
            with self.lock:
                self._append({**metadata, "event_stage": "end", "at_monotonic": self.clock(),
                              "outcome": item["outcome"],
                              "proof": "process_started" if item["confirmed_processes"] else "invocation_only",
                              "confirmed_subprocess_count": item["confirmed_processes"]})
                if not self.stack or self.stack[-1] is not item:
                    self.custody_verified = False
                else:
                    self.stack.pop()

    def process_allowed(self):
        with self.lock:
            return (threading.get_ident() == self.owner_thread
                    and any(item["allow_process"] is True for item in self.stack))

    def process_started(self, pid):
        pid = positive_int(pid)
        if (pid is None or not self.process_allowed() or not self.stack
                or self.stack[-1]["kind"] != "subprocess_constructor"):
            return
        with self.lock:
            for item in self.stack:
                item["confirmed_processes"] += 1
            leaf = self.stack[-1]
            self._append({"span_id": leaf["span_id"], "parent_span_id": leaf["parent_span_id"],
                          "kind": "subprocess_constructor", "operation": leaf["operation"],
                          "event_stage": "process_started", "at_monotonic": self.clock(),
                          "proof": "process_started", "process_pid": pid})

    def complete(self):
        with self.lock:
            return not self.overflow and self.custody_verified and not self.stack


class HarnessBindings:
    """Bind diagnostics only inside one owned trial/thread; restore exact custody.

    A tool entry is only an invocation. Popen returning an actual PID confirms
    process creation, not execution of an app mutation. Inline clipboard has no
    dispatch hook in frozen runtime and deliberately remains invocation-only.
    """
    def __init__(self, trace, *, mac, trial):
        self.trace, self.mac, self.trial = trace, mac, trial
        self.bindings = []
        self.enabled = False
        self.installation_verified = False

    def _bind(self, owner, name, value):
        original = getattr(owner, name)
        local = getattr(owner, "__dict__", {})
        had_local = name in local
        setattr(owner, name, value)
        self.bindings.append((owner, name, original, value, had_local))

    def _wrap(self, call, kind, operation, *, allow_process=False, target=None):
        @wraps(call)
        def wrapped(*args, **kwargs):
            if not self.enabled or threading.get_ident() != self.trace.owner_thread:
                return call(*args, **kwargs)
            op = operation(args, kwargs) if callable(operation) else operation
            pid, wid = target(args, kwargs) if target else (None, None)
            with self.trace.span(kind, op, target_pid=pid, window_id=wid,
                                 allow_process=allow_process) as span:
                result = call(*args, **kwargs)
                span["outcome"] = result_class(result)
                return result
        return wrapped

    def __enter__(self):
        from coding_agents import cu_ai_tools as tools, cu_ai_templates as catalog
        from desktop_runtime import agent_cursor
        import subprocess
        self.enabled = True
        try:
            self._bind(tools, "run_template", self._wrap(tools.run_template, "tool_invocation",
                lambda a, k: admitted(a[0] if a else k.get("name"), TEMPLATES)))
            self._bind(tools, "run_fs", self._wrap(tools.run_fs, "tool_invocation",
                lambda a, k: admitted(a[0] if a else k.get("verb"), TOOLS)))
            self._bind(tools, "run_script", self._wrap(tools.run_script, "tool_invocation",
                lambda a, k: admitted((a[0] if a else k.get("app", "")).strip().lower()
                                     if type(a[0] if a else k.get("app", "")) is str else None, APPS),
                allow_process=True))
            self._bind(tools, "readback", self._wrap(tools.readback, "readback_invocation", "typed_readback"))
            self._bind(tools, "evidence", self._wrap(tools.evidence, "readback_invocation", "file_readback"))
            self._bind(catalog, "_read_exact_file", self._wrap(catalog._read_exact_file, "readback_invocation", "exact_file_readback"))
            self._bind(catalog, "_clipboard", self._wrap(catalog._clipboard, "inline_native_invocation",
                lambda a, k: admitted(a[0] if a else k.get("name"), {"clipboard_read", "clipboard_write"})))
            self._bind(catalog, "_sips", self._wrap(catalog._sips, "tool_invocation", "image_export", allow_process=True))
            self._bind(agent_cursor, "emit", self._wrap(agent_cursor.emit, "overlay_hint_invocation",
                lambda a, k: admitted(a[0] if a else k.get("kind"), HINTS)))
            self._bind(self.mac, "observe", self._wrap(self.mac.observe, "observation_invocation", "observe"))
            native = getattr(self.mac, "native", self.mac)
            if callable(getattr(native, "call", None)):
                def recipient(a, k):
                    args = a[1] if len(a) > 1 else k.get("args")
                    args = args if type(args) is dict else {}
                    dest = args.get("target")
                    dest = dest if type(dest) is dict else args
                    return dest.get("pid"), dest.get("window_id")
                self._bind(native, "call", self._wrap(native.call, "native_transport_invocation",
                    lambda a, k: admitted(a[0] if a else k.get("tool"), NATIVE), target=recipient))
            real_popen = subprocess.Popen
            @wraps(real_popen)
            def popen(*args, **kwargs):
                if not self.enabled or not self.trace.process_allowed():
                    return real_popen(*args, **kwargs)
                command = args[0] if args else kwargs.get("args")
                executable = command[0] if type(command) in (list, tuple) and command else None
                operation = admitted(executable, {"osascript", "/usr/bin/osascript", "/usr/bin/sips"})
                operation = "script_executor" if operation in {"osascript", "/usr/bin/osascript"} else "image_executor" if operation == "/usr/bin/sips" else "other"
                with self.trace.span("subprocess_constructor", operation):
                    process = real_popen(*args, **kwargs)
                    self.trace.process_started(getattr(process, "pid", None))
                    return process
            self._bind(subprocess, "Popen", popen)
            # This trial module's own client instance is proxied; global httpx
            # classes and other threads' provider clients are not replaced.
            self._bind(self.trial, "httpx", _HttpxBinding(self.trial.httpx, self))
            self.installation_verified = True
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        self.enabled = False
        for owner, name, original, replacement, had_local in reversed(self.bindings):
            if getattr(owner, name) is not replacement:
                self.trace.custody_verified = False
                continue  # Never overwrite another owner's subsequent binding.
            if had_local:
                setattr(owner, name, original)
            else:
                delattr(owner, name)
        self.bindings.clear()


class _HttpxBinding:
    def __init__(self, module, binding):
        self.module, self.binding = module, binding

    def __getattr__(self, name):
        return getattr(self.module, name)

    def Client(self, *args, **kwargs):
        real = self.module.Client(*args, **kwargs)
        if not self.binding.enabled or threading.get_ident() != self.binding.trace.owner_thread:
            return real
        return _ClientBinding(real, self.binding)


class _ClientBinding:
    def __init__(self, client, binding):
        self.client, self.binding = client, binding

    def __getattr__(self, name):
        return getattr(self.client, name)

    def post(self, *args, **kwargs):
        operation = "responses" if args and args[0] == "https://api.openai.com/v1/responses" else "other"
        return self.binding._wrap(self.client.post, "provider_request", operation)(*args, **kwargs)
