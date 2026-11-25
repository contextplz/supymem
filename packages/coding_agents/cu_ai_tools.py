"""Background-safe tools for the AI loop: `script` (AppleScript/JXA against an allowlist of scriptable apps) and
`fs` (a few file operations on paths the reviewed task names). Both work off-Space and without focus.

Validators return a refusal reason (str) or None. Refusals go back to the model as an outcome so it can pick another
route; they never abort the task. Content read back (script results, file text) is marked private with PRIVATE so
logs/receipts withhold it while the model still sees it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
import uuid
import subprocess
import time
from pathlib import Path

PRIVATE = "§"   # outcome text after this marker goes to the model only, never to logs/receipts
SCRIPTABLE = ("textedit", "notes", "finder", "calendar", "reminders", "safari", "mail", "preview", "pages", "numbers",
              "keynote", "music", "photos")
FS_VERBS = ("mkdir", "move", "copy", "rename", "write_text", "read_text", "list")
TRIAL_ROOT = "~/Documents/SupymemCUTest"
MAX_SCRIPT = 4000
MAX_RESULT = 1500
MAX_FILE = 200_000
MAX_COPY = 16 * 1024 * 1024

_ALWAYS_FORBIDDEN = re.compile(
    r"(?i)(do\s+shell\s+script|\bkeystroke\b|key\s+code|system\s+events|run\s+script|load\s+script|"
    r"open\s+location|\bcurl\b|\bwget\b|https?://|keychain|security\s+find|password|"
    r"currentApplication\(\)\.doShellScript|ObjC\.import|NSTask|\bsudo\b|do\s+script|administrator\s+privileges)")
_DELETE = re.compile(r"(?i)(\bdelete\b|empty\s+(the\s+)?trash|move\s+.*\bto\s+(the\s+)?trash|\berase\b|\bremove\b)")
_SEND = re.compile(r"(?i)(\bsend\b\s+(?:\w+\s+)?(?:message|outgoing)|\bsend\s+msg\b)")


def script_block(app: str, code: str, goal: str) -> str | None:
    name = (app or "").strip().lower().removesuffix(".app")
    if name not in SCRIPTABLE:
        return f"refused: {app!r} is not a scriptable app I may drive (allowed: {', '.join(SCRIPTABLE)})"
    if name not in goal.lower():
        return f"refused: the task does not mention {app}; scripts only run against apps the task names"
    if not code.strip() or len(code) > MAX_SCRIPT:
        return "refused: script empty or too long"
    if _ALWAYS_FORBIDDEN.search(code):
        return "refused: script uses shell/GUI-scripting/network/keychain features that are not allowed"
    if _DELETE.search(code) and not re.search(r"(?i)\b(delete|trash|remove|erase)\b", goal):
        return "refused: the task does not ask to delete anything"
    if name == "mail" and _SEND.search(code) and not re.search(r"(?i)\bsend\b", goal):
        return "refused: Mail may only create drafts unless the task says to send"
    if name not in code.lower():
        return f"refused: the script must address {app} (tell application \"{app}\")"
    # The outer app field is not authority for a nested/dynamically addressed app.
    # Reject calculated targets; require every explicit target to be this app.
    targets = re.findall(r'(?i)\bapplication\s+"([^"\n]+)"', code)
    jxa_targets = re.findall(r'\bApplication\(\s*[\'\"]([^\'\"]+)[\'\"]\s*\)', code)
    if any(t.lower().removesuffix(".app") != name for t in targets + jxa_targets):
        return "refused: script addresses a different app"
    scrubbed = re.sub(r'(?i)\bapplication\s+"[^"\n]+"', '', code)
    scrubbed = re.sub(r'\bApplication\(\s*[\'\"][^\'\"]+[\'\"]\s*\)', '', scrubbed)
    if re.search(r'(?i)\bapplication\b|\bApplication\s*\(', scrubbed):
        return "refused: dynamic application targets are not allowed"
    if not targets and not jxa_targets:
        return "refused: script must explicitly address its app"
    return None


def _run_cancellable(cmd, stop, timeout):
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if stop.is_set():
                proc.kill()
                proc.communicate()
                return None
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(cmd, timeout)
            try:
                stdout, stderr = proc.communicate(timeout=min(0.1, remaining))
                return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()


def run_script(app: str, code: str, goal: str, language: str = "applescript", timeout: float = 20, stop=None, *, trusted=False) -> str:
    if not trusted:
        return "refused: raw model scripts are not executable; choose a typed template or AX/browser actions"
    why = _template_app_block(app, goal)
    if why:
        return why
    cmd = ["osascript"] + (["-l", "JavaScript"] if language.lower() in {"jxa", "javascript"} else []) + ["-e", code]
    try:
        proc = (_run_cancellable(cmd, stop, timeout) if stop is not None
                else subprocess.run(cmd, capture_output=True, text=True, timeout=timeout))
        if proc is None:
            return "script cancelled; any already dispatched app effect is uncertain; verify before retry"
    except subprocess.TimeoutExpired:
        return "script timed out (an Automation permission prompt may be waiting for the founder)"
    except OSError:
        return "refused: script executor is unavailable before dispatch"
    if proc.returncode != 0:
        # osascript errors can quote the script, document body or credentials.
        return f"script error (exit {proc.returncode})" + PRIVATE + (proc.stderr or "").strip()[:MAX_RESULT]
    out = (proc.stdout or "").strip()
    # Typed scripts can reject preconditions or fail their own postcondition.
    # osascript exit 0 is a transport success, not an effect receipt.
    if out.startswith("refused:"):
        return "refused: template precondition was not met" + PRIVATE + out[:MAX_RESULT]
    if out.startswith("unverified:"):
        return "script cancelled; template effect is unconfirmed; verify before retry" + PRIVATE + out[:MAX_RESULT]
    return f"script ok ({len(out)} chars)" + (PRIVATE + out[:MAX_RESULT] if out else "")


def _literal(value: str) -> str:
    """AppleScript quoted literal, never raw model code."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\r", '" & return & "').replace("\n", '" & linefeed & "') + '"'


def _template_app_block(app: str, goal: str):
    name = app.strip().lower().removesuffix(".app")
    if name not in SCRIPTABLE or name not in goal.lower():
        return "refused: template app is not an allowed app named in the task"
    return None


def run_template(name: str, payload: str, goal: str, *, touched=None, stop=None) -> str:
    """Reviewed recipes: deterministic script syntax and filesystem scope.

    The catalog (`TEMPLATES`, see cu_ai_templates) covers notes, calendar, reminders, preview export and
    finder operations; every parameter is a string that is escaped into a literal, never run as code.

    New TextEdit documents are refused before creating bytes: native opening
    cannot currently preserve the user's foreground app continuously.
    """
    parameter_error = template_parameter_error(name, payload, goal)
    if parameter_error:
        return parameter_error
    try:
        args = json.loads(payload)
    except (ValueError, TypeError):
        return "refused: template parameters must be a JSON object"
    if not isinstance(args, dict) or any(not isinstance(v, str) for v in args.values()):
        return "refused: template parameters must be strings"
    from coding_agents.cu_ai_templates import _method_only
    if _method_only(goal):
        return "refused: the task requires a particular input method; a template cannot prove that method"
    if name == "textedit_new_save" and set(args) == {"path", "text", "plain"}:
        if args["plain"].lower() != "true":
            return "refused: only plain text documents can be created exactly; use plain=true"
        return run_template("text_file_open", json.dumps({"app": "TextEdit", "path": args["path"], "text": args["text"]}),
                            goal, touched=touched, stop=stop)
    if name == "text_file_open" and set(args) == {"app", "path", "text"}:
        app, path = args["app"], args["path"]
        why = _template_app_block(app, goal)
        if why:
            return why
        if app.strip().lower().removesuffix(".app") == "textedit":
            return "refused: creating a new TextEdit document is unavailable in background mode because opening it can interrupt your current app; no file changes were made"
        why = fs_block("write_text", path, args["text"], goal)
        if why:
            return why
        if stop is not None and stop.is_set():
            return "template cancelled before creation"
        result = run_fs("write_text", path, args["text"], goal, touched, stop=stop)
        if result != "fs write_text ok":
            return result
        if stop is not None and stop.is_set():
            return "template cancelled after file creation; verify before retry"
        code = f'do shell script "/usr/bin/open -g -a " & quoted form of {_literal(app)} & " " & quoted form of {_literal(str(_resolve(path)))}'
        return "file created; " + run_script(app, code, goal, stop=stop, trusted=True)
    return _run_catalog_template(name, args, goal, touched=touched, stop=stop)


def template_parameter_error(name: str, payload: str, goal: str) -> str | None:
    """Pure Mail admission, also used before the engine can dismiss a sheet."""
    if name != "mail_draft":
        return None
    from coding_agents.cu_ai_templates import MAIL_PARAMETER_ERRORS, mail_draft_parameter_error
    try:
        args = json.loads(payload)
    except (ValueError, TypeError):
        return MAIL_PARAMETER_ERRORS["mail_draft_json_invalid"]
    return mail_draft_parameter_error(args, goal)


# --- fs ----------------------------------------------------------------------------------------

def _home() -> Path:
    return Path(os.path.expanduser("~")).resolve()


def named_paths(goal: str) -> list[Path]:
    """Only paths named by the reviewed task, inside the home folder. No implicit QA scope."""
    roots = re.findall(r"""(?:(?<=[\s'"(])|^)((?:~|/Users)/[^\s'"),;]+)""", goal)
    out = []
    for raw in roots:
        p = Path(os.path.expanduser(raw.rstrip(".:"))).resolve()
        if str(p).startswith(str(_home()) + os.sep) or p == _home():
            if p != _home():
                out.append(p)
    return out


def _inside(path: Path, roots: list[Path]) -> bool:
    return any(path == r or (not r.suffix and r in path.parents) for r in roots)


def _resolve(raw: str) -> Path:
    return Path(os.path.expanduser((raw or "").strip())).resolve()


def fs_block(verb: str, src: str, dest: str, goal: str, *, allow_overwrite: bool = False) -> str | None:
    if verb not in FS_VERBS:
        return f"refused: fs verbs are {', '.join(FS_VERBS)} (no delete)"
    roots = named_paths(goal)
    paths = [src] + ([dest] if verb in {"move", "copy", "rename"} else [])
    for raw in paths:
        if not raw or not re.match(r"(~|/)", raw.strip()):
            return "refused: give an absolute or ~ path"
        p = _resolve(raw)
        if not str(p).startswith(str(_home()) + os.sep):
            return "refused: outside the home folder"
        if not _inside(p, roots):
            return f"refused: {raw} is not a path the task names (task paths: {', '.join(str(r) for r in roots[:4])})"
    if verb in {"move", "copy", "rename"}:
        d = _resolve(dest)
        if d.exists() and allow_overwrite is not True:
            return "refused: destination already exists"
    if verb == "write_text":
        if _resolve(src).exists() and allow_overwrite is not True:
            return "refused: file already exists; explicit reviewed overwrite permission is required (nothing was changed; if it was created earlier in this task it is already done: verify and finish, do not retry)"
        if len(dest) > MAX_FILE:
            return "refused: text too large"
    return None


def _parent_fd(path: Path, roots, *, create=False):
    """Hold the parent chain; no pathname-following effects after validation."""
    home = _home()
    if not _inside(path, roots) or home not in path.parents:
        raise PermissionError("filesystem_scope_changed")
    parts = path.relative_to(home).parts
    fd = os.open(home, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            if create:
                try:
                    os.mkdir(part, 0o700, dir_fd=fd)
                except FileExistsError:
                    pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, parts[-1]
    except BaseException:
        os.close(fd)
        raise


def _rename_exclusive(src_fd, src, dest_fd, dest, *, overwrite=False):
    import ctypes
    libc = ctypes.CDLL(None, use_errno=True)
    name = "renameatx_np" if sys.platform == "darwin" else "renameat2"
    fn = getattr(libc, name, None)
    if fn is None:
        raise OSError("exclusive_rename_unavailable")
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    fn.restype = ctypes.c_int
    flags = 0 if overwrite else (4 if sys.platform == "darwin" else 1)
    if fn(src_fd, os.fsencode(src), dest_fd, os.fsencode(dest), flags) != 0:
        raise OSError(ctypes.get_errno(), "exclusive_rename_failed")


def run_fs(verb: str, src: str, dest: str, goal: str, touched: set | None = None, stop=None,
           *, allow_overwrite: bool = False) -> str:
    # This flag is native/internal authority, absent from model action schemas.
    # Neither positive keywords nor "do not overwrite" can grant replacement.
    why = fs_block(verb, src, dest, goal, allow_overwrite=allow_overwrite)
    if why:
        return why
    roots = named_paths(goal)
    s = _resolve(src)
    overwrite = allow_overwrite is True
    handles = []
    owned_temp = None
    try:
        if stop is not None and stop.is_set():
            return "fs cancelled before effect"
        parent, leaf = _parent_fd(s, roots, create=verb in {"mkdir", "write_text"})
        handles.append(parent)
        if verb == "mkdir":
            try:
                os.mkdir(leaf, 0o700, dir_fd=parent)
            except FileExistsError:
                if not stat.S_ISDIR(os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode):
                    raise OSError("not_directory")
        elif verb in {"move", "rename", "copy"}:
            d = _resolve(dest)
            target, name = _parent_fd(d, roots, create=True)
            handles.append(target)
            mode = os.stat(leaf, dir_fd=parent, follow_symlinks=False).st_mode
            if stat.S_ISLNK(mode):
                raise OSError("symlink_source_refused")
            if verb in {"move", "rename"}:
                _rename_exclusive(parent, leaf, target, name, overwrite=overwrite)
            else:
                if not stat.S_ISREG(mode):
                    return "refused: copy currently supports regular files only"
                source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                handles.append(source)
                if not stat.S_ISREG(os.fstat(source).st_mode):
                    raise OSError("not_regular_file")
                if os.fstat(source).st_size > MAX_COPY:
                    return "refused: copy exceeds the 16 MiB direct-tool limit; use a reviewed alternative"
                temp_name = ".supymem-cu-" + uuid.uuid4().hex
                output = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=target)
                handles.append(output)
                owned_temp = (target, temp_name)
                total = 0
                while block := os.read(source, 65536):
                    if stop is not None and stop.is_set():
                        return "fs cancelled; destination unchanged"
                    total += len(block)
                    if total > MAX_COPY:
                        return "refused: source grew beyond direct-tool copy limit"
                    view = memoryview(block)
                    while view:
                        view = view[os.write(output, view):]
                if stop is not None and stop.is_set():
                    return "fs cancelled; destination unchanged"
                os.fsync(output)
                if stop is not None and stop.is_set():
                    return "fs cancelled before publication; destination unchanged"
                if overwrite:
                    os.replace(temp_name, name, src_dir_fd=target, dst_dir_fd=target)
                    owned_temp = None
                else:
                    os.link(temp_name, name, src_dir_fd=target, dst_dir_fd=target, follow_symlinks=False)
        elif verb == "write_text":
            temp_name = ".supymem-cu-" + uuid.uuid4().hex
            output = os.open(temp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
            owned_temp = (parent, temp_name)
            with os.fdopen(output, "w", encoding="utf-8") as stream:
                stream.write(dest)
                stream.flush()
                os.fsync(stream.fileno())
            if stop is not None and stop.is_set():
                return "fs cancelled; destination unchanged"
            if overwrite:
                os.replace(temp_name, leaf, src_dir_fd=parent, dst_dir_fd=parent)
                owned_temp = None
            else:
                os.link(temp_name, leaf, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        elif verb == "read_text":
            source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(source, "r", encoding="utf-8", errors="replace") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode):
                    return "refused: read_text requires a regular file"
                data = stream.read(MAX_RESULT)
            return f"read ({info.st_size} bytes)" + PRIVATE + data
        elif verb == "list":
            directory = os.open(leaf, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            handles.append(directory)
            names = sorted(os.listdir(directory))[:80]
            return f"listed {len(names)} entries" + PRIVATE + ", ".join(names)
    except (OSError, ValueError) as error:
        if isinstance(error, FileNotFoundError):
            return ("fs error: source_missing; the reviewed path or parent does not exist. "
                    "If the task asks to create it, perform that creation before reading; "
                    "otherwise reobserve or choose an authorized alternative. Repeating the read cannot create it.")
        return f"fs error: {type(error).__name__}"
    finally:
        if owned_temp is not None:
            try:
                os.unlink(owned_temp[1], dir_fd=owned_temp[0])
            except FileNotFoundError:
                pass
        for fd in reversed(handles):
            os.close(fd)
    if touched is not None:
        touched.add(str(_resolve(dest) if verb in {"move", "rename", "copy"} else s))
    if stop is not None and stop.is_set():
        return "fs effect committed before Stop; verify before retry"
    return f"fs {verb} ok"


def evidence(paths) -> str:
    """Bounded fresh read-back of tool-touched paths, not proof of their history."""
    lines = []
    for raw in sorted(paths)[:8]:
        p = _resolve(raw)
        parent = source = None
        try:
            parent, leaf = _parent_fd(p, [p])
            source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            info = os.fstat(source)
            if stat.S_ISREG(info.st_mode):
                data = bytearray()
                while len(data) <= MAX_FILE:
                    block = os.read(source, min(65536, MAX_FILE + 1 - len(data)))
                    if not block:
                        break
                    data.extend(block)
                kind = "sha256" if len(data) <= MAX_FILE else "bounded_sha256"
                lines.append(f"{p}: file, {info.st_size} bytes, {kind} {hashlib.sha256(data).hexdigest()[:12]},"
                             f" head={bytes(data[:400]).decode(errors='replace')!r}")
            elif stat.S_ISDIR(info.st_mode):
                lines.append(f"{p}: folder with {len(os.listdir(source))} entries")
            else:
                lines.append(f"{p}: unsupported file type")
        except FileNotFoundError:
            lines.append(f"{p}: does not exist")
        except OSError:
            lines.append("evidence unavailable; path changed or inaccessible")
        finally:
            if source is not None:
                os.close(source)
            if parent is not None:
                os.close(parent)
    return "\n".join(lines)


def required_outputs(goal: str) -> dict[Path, str]:
    """Conservative explicit output clauses; not a parser for arbitrary user intent.

    Unknown/implicit destinations remain the independent goal verifier's job.
    Read/input paths and negated save instructions never become output obligations.
    """
    authorized = set(named_paths(goal))
    outputs = {}
    boundary = 0
    for match in re.finditer(r"(?:(?<=[\s'\"(])|^)((?:~|/Users)/[^\s'\"),;]+)", goal):
        prefix = goal[boundary:match.start()]
        prefix = re.split(r"[;\n]|\.(?=\s+[A-Z])", prefix)[-1]
        verbs = list(re.finditer(r"\b(save|export|write|create|generate|download)\b", prefix, re.I))
        positive = [verb for verb in verbs if not re.search(
            r"\b(?:do not|don't|never|avoid|without|no)\s+(?:\w+\s+){0,2}$", prefix[:verb.start()], re.I)]
        if positive:
            verb = positive[-1][1].lower()
            destination = re.search(r"\b(?:to|as|at|into|in)\s*$", prefix.rstrip(" '\"("), re.I)
            explicit_object = re.search(r"\b(?:file|folder|directory)\b", prefix, re.I)
            direct_object = re.fullmatch(r"\s*(?:(?:a|the)\s+)?(?:(?:file|folder|directory)(?:\s+named)?\s*)?", prefix[positive[-1].end():].rstrip(" '\"("), re.I)
            if (verb == "write" and not destination
                    or verb in {"save", "export", "download"} and not (destination or direct_object)
                    or verb in {"create", "generate"} and not (explicit_object and (destination or direct_object))):
                positive = []
        if positive and positive[-1][1].lower() in {"create", "generate"}:
            tail = re.split(r"[;\n]", goal[match.end():])[0]
            if re.search(r"\btitled\b", prefix, re.I) or (re.search(r"\b(?:do not|don't|never)\s+save\b", tail, re.I)
                    and not re.search(r"\b(?:file|folder|directory)\b", prefix, re.I)):
                positive = []
        path = _resolve(match[1].rstrip(".:"))
        if positive and path in authorized:
            outputs[path] = "file" if positive[-1][1].lower() in {"save", "export", "write", "download"} else "file_or_directory"
        boundary = match.end()
    return outputs


def output_present(path: Path, kind: str) -> bool:
    """Read-only, no-follow descriptor check of a required durable output."""
    parent = source = None
    try:
        parent, leaf = _parent_fd(path, [path])
        source = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(source)
        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino):
            return False
        return stat.S_ISREG(info.st_mode) or (kind == "file_or_directory" and stat.S_ISDIR(info.st_mode))
    except OSError:
        return False
    finally:
        if source is not None:
            os.close(source)
        if parent is not None:
            os.close(parent)


def readback(result: str, touched=None) -> str | None:
    """Deterministic (no-model) proof of a tool's effect, or None.

    Script templates read their object back inside the same script ("verified: ..."); file tools are proved by a
    fresh descriptor read of what they touched. A model's claim or a bare "ok" never counts.
    """
    if not isinstance(result, str):
        return None
    detail = result.partition(PRIVATE)[2].strip()
    public = result.partition(PRIVATE)[0]
    if detail.startswith("verified:") and public.startswith(("script ok (", "preview_export ok")):
        return detail[:200]
    if re.match(r"(fs (mkdir|move|copy|rename|write_text) ok|file created;)", result) and touched:
        present = [p for p in sorted(touched)[:8] if output_present(_resolve(p), "file_or_directory")]
        if present:
            return f"verified: {len(present)} file-tool target(s) present on disk"
    return None


def _run_catalog_template(name, args, goal, *, touched=None, stop=None):
    from coding_agents import cu_ai_templates as catalog
    return catalog.run(name, args, goal, touched=touched, stop=stop)
