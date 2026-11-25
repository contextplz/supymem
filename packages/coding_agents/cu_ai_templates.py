"""Verified, parameterised script templates for the AI loop (no free-form AppleScript).

The model calls `op=template, chord=<name>, text=<JSON of string params>`. Every param is escaped into an AppleScript
literal (never code), must be NAMED in the reviewed task (so a template can only touch objects the founder approved),
and the app must be named in the task. Deleting needs the task to ask for it. Results never echo personal content.

  notes_create{title,body}  notes_rename{title,new_title}  notes_append{title,text}
  calendar_create_event{calendar,title,start,end | all_day:"true" with start YYYY-MM-DD}  calendar_delete_event{calendar,title}
  reminders_create_list{list}  reminders_add{list,title,due?}  reminders_complete{list,title}
  preview_export{src,dest,format}    (sips; the sandboxed Preview export sheet is never needed)
  finder_mkdir{path}  finder_move{src,dest}  finder_rename{path,new_name}  finder_copy{src,dest}
  textedit_edit{path,mode,text}  (an already-open exact reviewed document only)
  New TextEdit documents are unavailable in strict background mode: opening can steal focus.
"""
from __future__ import annotations

import html
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import uuid
from decimal import Decimal, InvalidOperation
from pathlib import Path

from coding_agents import cu_ai_tools as tools

NAMES = ("textedit_new_save", "text_file_open", "notes_create", "notes_rename", "notes_append",
         "calendar_create_event", "calendar_delete_event", "reminders_create_list", "reminders_add",
         "reminders_complete", "preview_export", "finder_mkdir", "finder_move", "finder_rename", "finder_copy",
         "clipboard_write", "clipboard_read", "textedit_edit", "textedit_copy_save", "mail_draft", "numbers_csv")
_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?")
_FORMATS = {"png": "png", "jpg": "jpeg", "jpeg": "jpeg", "tiff": "tiff", "gif": "gif", "bmp": "bmp", "heic": "heic"}
_SIGNATURES = {"png": (b"\x89PNG",), "jpeg": (b"\xff\xd8",), "gif": (b"GIF8",), "bmp": (b"BM",),
               "tiff": (b"II*\x00", b"MM\x00*"), "heic": (b"",)}
_HEAD = '''on head(t)
    if (count of t) > 300 then return text 1 thru 300 of t
    return t
end head
'''
_ISO = '''on pad(n)
    return text -2 thru -1 of ("0" & (n as integer))
end pad
on iso(d)
    if d is missing value then return "none"
    return (year of d as integer) & "-" & my pad(month of d as integer) & "-" & my pad(day of d) & " " & my pad(hours of d) & ":" & my pad(minutes of d)
end iso
'''
_MAKE_DATE = '''on mkdate(y, mo, d, h, mi, s)
    set t to current date
    set day of t to 1
    set year of t to y
    set month of t to mo
    set day of t to d
    set hours of t to h
    set minutes of t to mi
    set seconds of t to s
    return t
end mkdate
'''

# Exact source-owned validation results: no parameter values enter public errors.
# These all precede app/script delivery; callers may request parameter correction.
MAIL_PARAMETER_ERRORS = {
    "mail_draft_json_invalid": "refused: mail_draft_json_invalid: use a JSON object with exactly to, subject, body; all three values must be strings.",
    "mail_draft_keys_invalid": "refused: mail_draft_keys_invalid: use exactly to, subject, body; no extra keys. Put all requested To addresses in the single to string.",
    "mail_draft_types_invalid": "refused: mail_draft_types_invalid: to, subject, body must be strings; to is comma-separated bare addresses, not an array.",
    "mail_draft_duplicate_recipient": "refused: mail_draft_duplicate_recipient: a recipient is repeated. List each requested To address exactly once in the to string.",
    "mail_draft_recipients_invalid": "refused: mail_draft_recipients_invalid: to must contain 1-20 distinct bare addresses named in the original task, separated by commas.",
    "mail_draft_subject_invalid": "refused: mail_draft_subject_invalid: use a nonempty subject exactly named in the original task, at most 300 characters.",
    "mail_draft_body_invalid": "refused: mail_draft_body_invalid: body exceeds the 16000-character direct-tool limit; correct the parameters within the original task.",
}


def mail_parameter_feedback(result: str) -> tuple[str, str] | None:
    """Admit only exact owned pre-delivery validation strings, never error prose."""
    return next(((code, message) for code, message in MAIL_PARAMETER_ERRORS.items() if result == message), None)


def mail_draft_parameter_error(args, goal: str) -> str | None:
    """Pure argument contract; it neither reads app state nor changes authority."""
    if not isinstance(args, dict):
        return MAIL_PARAMETER_ERRORS["mail_draft_json_invalid"]
    if set(args) != {"to", "subject", "body"}:
        return MAIL_PARAMETER_ERRORS["mail_draft_keys_invalid"]
    if any(not isinstance(value, str) for value in args.values()):
        return MAIL_PARAMETER_ERRORS["mail_draft_types_invalid"]
    if not args["subject"].strip() or len(args["subject"]) > 300 or args["subject"] not in goal:
        return MAIL_PARAMETER_ERRORS["mail_draft_subject_invalid"]
    recipients = _mail_recipients(args["to"])
    if len(set(recipients)) != len(recipients):
        return MAIL_PARAMETER_ERRORS["mail_draft_duplicate_recipient"]
    if (not 1 <= len(recipients) <= 20
            or any(not re.fullmatch(r"[^\s@,<>]+@[^\s@,<>]+\.[^\s@,<>]+", r) or r not in goal for r in recipients)):
        return MAIL_PARAMETER_ERRORS["mail_draft_recipients_invalid"]
    if len(args["body"]) > 16_000:
        return MAIL_PARAMETER_ERRORS["mail_draft_body_invalid"]
    return None


def _mail_recipients(value: str) -> list[str]:
    return [r.strip() for r in re.split(r"\s*(?:,|;|\n|\s+and\s+)\s*", value) if r.strip()]


def _date(raw: str):
    m = _DATE.fullmatch(raw.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(g or 0) for g in m.groups())
    if not (1 <= mo <= 12 and 1 <= d <= 31 and h < 24 and mi < 60 and s < 60):
        return None
    return f"my mkdate({y}, {mo}, {d}, {h}, {mi}, {s})"


def _named(goal: str, *values: str) -> str | None:
    for value in values:
        if not value.strip() or len(value) > 300:
            return "refused: template parameter empty or too long"
        if value not in goal:
            return "refused: the task does not name that title/list/calendar; templates only touch objects the task names"
    return None


def _need(args: dict, *keys: str, optional: tuple = ()) -> bool:
    return set(keys) <= set(args) <= set(keys) | set(optional)


def _osa(app: str, body: str, goal: str, stop) -> str:
    return tools.run_script(app, body, goal, stop=stop, trusted=True)


def _wants_delete(goal: str) -> bool:
    return bool(re.search(r"(?i)\b(delete|remove|trash|erase)\b", goal))


def run(name: str, args: dict, goal: str, *, touched=None, stop=None) -> str:
    L = tools._literal
    if name in {"clipboard_write", "clipboard_read"}:
        if not _need(args, *(('text',) if name == "clipboard_write" else ())):
            return "refused: unknown template or parameter keys"
        return _clipboard(name, args, goal, stop)
    if name == "textedit_edit" and _need(args, "path", "mode", "text"):
        return _textedit_edit(args, goal, touched, stop)
    if name == "textedit_copy_save" and _need(args, "src", "dest"):
        return _textedit_copy_save(args, goal, touched, stop)
    if name == "mail_draft":
        return _mail_draft(args, goal, stop)
    if name == "numbers_csv" and _need(args, "path", "csv"):
        return _numbers_csv(args, goal, touched, stop)
    if name == "notes_create" and _need(args, "title", "body"):
        why = tools._template_app_block("Notes", goal)
        if why:
            return why
        body = html.escape(args["body"]).replace("\n", "<br>")   # Notes stores HTML; plain text stays literal
        code = _HEAD + f'''tell application "Notes"
    set n to make new note with properties {{name:{L(args["title"])}, body:{L(body)}}}
    if (count of (notes whose name is {L(args["title"])})) < 1 then return "unverified: note not found after create"
    return "verified: " & (count of (notes whose name is {L(args["title"])})) & " note(s) titled as requested; plaintext body: " & my head(plaintext of n)
end tell'''
        return _osa("Notes", code, goal, stop)
    if name in {"notes_rename", "notes_append"}:
        keys = ("title", "new_title") if name == "notes_rename" else ("title", "text")
        if not _need(args, *keys):
            return "refused: unknown template or parameter keys"
        why = tools._template_app_block("Notes", goal) or _named(goal, args["title"])
        if why:
            return why
        found = f'set hits to notes whose name is {L(args["title"])}\n    if (count of hits) is not 1 then return "refused: expected exactly one note with that title, found " & (count of hits)\n    set n to item 1 of hits\n'
        if name == "notes_rename":
            act = (f'set name of n to {L(args["new_title"])}\n    if (count of (notes whose name is {L(args["new_title"])})) < 1 then return "unverified: renamed note not found"\n'
                   '    return "verified: note renamed; now titled as requested; plaintext body: " & my head(plaintext of n)')
        else:
            add = html.escape(args["text"]).replace("\n", "<br>")
            act = (f'set body of n to (body of n) & "<br>" & {L(add)}\n    if (body of n) does not contain {L(add)} then return "unverified: appended text not found"\n'
                   '    return "verified: text appended; title " & (name of n) & "; plaintext body: " & my head(plaintext of n)')
        out = _osa("Notes", _HEAD + f'tell application "Notes"\n    {found}    {act}\nend tell', goal, stop)
        return out
    if name == "calendar_create_event" and _need(args, "calendar", "title", "start", optional=("end", "all_day")):
        all_day = args.get("all_day", "").lower() == "true"
        if all_day:   # start is a plain date (YYYY-MM-DD); the event spans that whole day
            day = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", args["start"].strip())
            if not day:
                return "refused: all_day start must look like YYYY-MM-DD"
            from datetime import date, timedelta
            try:
                d0 = date(int(day[1]), int(day[2]), int(day[3]))
            except ValueError:
                return "refused: invalid date"
            d1 = d0 + timedelta(days=1)
            sd, ed = _date(f"{d0} 00:00"), _date(f"{d1} 00:00")
            flag = "true"
        else:
            sd, ed = _date(args["start"]), _date(args.get("end", ""))
            flag = "false"
        why = tools._template_app_block("Calendar", goal) or _named(goal, args["calendar"], args["title"])
        if why or not (sd and ed):
            return why or "refused: start/end must look like YYYY-MM-DD HH:MM (or all_day=true with start YYYY-MM-DD)"
        code = _MAKE_DATE + _ISO + f'''tell application "Calendar"
    set cals to calendars whose name is {L(args["calendar"])}
    if (count of cals) is 0 then
        make new calendar with properties {{name:{L(args["calendar"])}}}
        delay 2   -- a brand-new calendar needs a moment before events can be saved into it
    end if
    set c to first calendar whose name is {L(args["calendar"])}
    tell c to set ev to make new event with properties {{summary:{L(args["title"])}, start date:{sd}, end date:{ed}, allday event:{flag}}}
    set found to events of c whose summary is {L(args["title"])}
    if (count of found) is not 1 then return "unverified: expected 1 event with that title, found " & (count of found)
    set e to item 1 of found
    return "verified: calendar has " & (count of events of c) & " event(s); event titled as requested, start " & my iso(start date of e) & ", end " & my iso(end date of e) & ", all-day " & (allday event of e)
end tell'''
        return _osa("Calendar", code, goal, stop)
    if name == "calendar_delete_event" and _need(args, "calendar", "title"):
        why = tools._template_app_block("Calendar", goal) or _named(goal, args["calendar"], args["title"])
        if why:
            return why
        if not _wants_delete(goal):
            return "refused: the task does not ask to delete anything"
        code = f'''tell application "Calendar"
    set cals to calendars whose name is {L(args["calendar"])}
    if (count of cals) is not 1 then return "refused: expected exactly one such calendar, found " & (count of cals)
    set hits to events of (item 1 of cals) whose summary is {L(args["title"])}
    if (count of hits) is not 1 then return "refused: expected exactly one such event, found " & (count of hits)
    delete (item 1 of hits)
    if (count of (events of (item 1 of cals) whose summary is {L(args["title"])})) is not 0 then return "unverified: event still present after delete"
    return "verified: event deleted, 0 remaining"
end tell'''
        return _osa("Calendar", code, goal, stop)
    if name == "reminders_create_list" and _need(args, "list"):
        why = tools._template_app_block("Reminders", goal) or _named(goal, args["list"])
        if why:
            return why
        code = f'''tell application "Reminders"
    if (count of (lists whose name is {L(args["list"])})) is 0 then make new list with properties {{name:{L(args["list"])}}}
    if (count of (lists whose name is {L(args["list"])})) < 1 then return "unverified: list not found"
    return "verified: list exists, id " & (id of (first list whose name is {L(args["list"])}))
end tell'''
        return _osa("Reminders", code, goal, stop)
    if name in {"reminders_add", "reminders_complete"} and _need(args, "list", "title", optional=("due",) if name == "reminders_add" else ()):
        why = tools._template_app_block("Reminders", goal) or _named(goal, args["list"], args["title"])
        if why:
            return why
        due = _date(args["due"]) if args.get("due") else None
        if args.get("due") and not due:
            return "refused: due must look like YYYY-MM-DD HH:MM"
        pick = f'set ls to lists whose name is {L(args["list"])}\n    if (count of ls) is not 1 then return "refused: expected exactly one such list, found " & (count of ls)\n    set l to item 1 of ls\n'
        if name == "reminders_add":
            props = f'name:{L(args["title"])}' + (f", due date:{due}" if due else "")
            act = (f'tell l to set r to make new reminder with properties {{{props}}}\n    set found to reminders of l whose name is {L(args["title"])}\n'
                   '    if (count of found) is not 1 then return "unverified: expected 1 reminder with that title, found " & (count of found)\n'
                   '    return "verified: list has " & (count of reminders of l) & " reminder(s); reminder titled as requested, completed " & (completed of r) & ", due " & my iso(due date of r)')
        else:
            act = (f'set hits to reminders of l whose name is {L(args["title"])}\n    if (count of hits) is not 1 then return "refused: expected exactly one such reminder, found " & (count of hits)\n'
                   '    set completed of (item 1 of hits) to true\n    if completed of (item 1 of hits) is not true then return "unverified: reminder not completed"\n'
                   '    return "verified: reminder titled as requested is completed; list has " & (count of (reminders of l whose completed is false)) & " open and " & (count of (reminders of l whose completed is true)) & " completed reminder(s)"')
        return _osa("Reminders", (_MAKE_DATE if due else "") + _ISO + f'tell application "Reminders"\n    {pick}    {act}\nend tell', goal, stop)
    if name == "preview_export" and _need(args, "src", "dest", "format"):
        fmt = _FORMATS.get(args["format"].strip().lower().lstrip("."))
        if not fmt:
            return "refused: format must be one of " + ", ".join(sorted(_FORMATS))
        return _sips(args["src"], args["dest"], fmt, goal, touched, stop)
    finder = {"finder_mkdir": ("mkdir", ("path",)), "finder_move": ("move", ("src", "dest")),
              "finder_copy": ("copy", ("src", "dest")), "finder_rename": ("rename", ("path", "new_name"))}
    if name in finder and _need(args, *finder[name][1]):
        verb = finder[name][0]
        if name == "finder_mkdir":
            return tools.run_fs("mkdir", args["path"], "", goal, touched, stop=stop)
        if name == "finder_rename":
            if "/" in args["new_name"] or not args["new_name"].strip():
                return "refused: new_name must be a bare file name"
            dest = str(Path(tools._resolve(args["path"]).parent) / args["new_name"])
            return tools.run_fs("rename", args["path"], dest, goal, touched, stop=stop)
        return tools.run_fs(verb, args["src"], args["dest"], goal, touched, stop=stop)
    return "refused: unknown template or parameter keys"


def _sips(src: str, dest: str, fmt: str, goal: str, touched, stop) -> str:
    """Image conversion with the macOS `sips` tool (no shell). Scope: both paths named by the task; never overwrites."""
    why = tools.fs_block("copy", src, dest, goal)
    if why:
        return why
    s, d = tools._resolve(src), tools._resolve(dest)
    if not s.is_file() or not d.parent.is_dir():
        return "fs error: source_missing; the source image or destination folder does not exist"
    tmp = d.parent / (".supymem-cu-" + uuid.uuid4().hex + "." + (d.suffix.lstrip(".") or fmt))
    try:
        if stop is not None and stop.is_set():
            return "export cancelled before effect"
        proc = subprocess.run(["/usr/bin/sips", "-s", "format", fmt, str(s), "--out", str(tmp)],
                              capture_output=True, text=True, timeout=30)
        if proc.returncode != 0 or not tmp.is_file():
            return f"export error (exit {proc.returncode})"
        head = tmp.read_bytes()[:12]
        if not any(head.startswith(sig) for sig in _SIGNATURES.get(fmt, (b"",))):
            return "export error: output_signature_mismatch"
        os.link(tmp, d)   # exclusive publish: fails if dest appeared meanwhile
    except FileExistsError:
        return "refused: destination already exists"
    except (OSError, subprocess.TimeoutExpired) as error:
        return f"export error: {type(error).__name__}"
    finally:
        tmp.unlink(missing_ok=True)
    if touched is not None:
        touched.add(str(d))
    return "preview_export ok" + tools.PRIVATE + f"verified: {fmt} file exported, {d.stat().st_size} bytes, signature matches"


def _method_only(goal: str) -> bool:
    """A requested input method is part of the task, not permission to replace it."""
    return bool(re.search(r"(?i)(\bonly\s+(?:the\s+)?(?:menu|keyboard)|(?:menu bar|keyboard shortcuts?)\s+only|\bby keyboard\b|\bvia the (?:open|save) dialog\b|\bfrom the save dialog\b|\buse (?:file|format)\s*>)", goal))


def _clipboard(name: str, args: dict, goal: str, stop, *, full_read=False) -> str:
    write = name == "clipboard_write"
    if _method_only(goal):
        return "refused: the task requires an input method; this template cannot prove that method"
    if not re.search(r"(?i)\bclipboard\b|\bcopy\b.*\bpaste\b", goal):
        return "refused: the task does not authorize clipboard access"
    if write and not re.search(r"(?i)\b(copy|put|set|write|leave)\b", goal):
        return "refused: the task does not authorize writing the clipboard"
    if not write and not re.search(r"(?i)\b(read|paste|inspect|show|get)\b", goal):
        return "refused: the task does not authorize reading the clipboard"
    if write and len(args["text"].encode("utf-8")) > tools.MAX_FILE:
        return "refused: clipboard text exceeds the direct-tool limit"
    if stop is not None and stop.is_set():
        return "refused: stopped before clipboard access"
    dispatched = False
    try:
        import AppKit
        board = AppKit.NSPasteboard.generalPasteboard()
        if write:
            dispatched = True
            board.clearContents()
            if not board.setString_forType_(args["text"], AppKit.NSPasteboardTypeString):
                return "script cancelled; clipboard write is unconfirmed; verify before retry"
        version = board.changeCount()
        text = board.stringForType_(AppKit.NSPasteboardTypeString)
        if board.changeCount() != version:
            return "script cancelled; clipboard changed during readback; verify before retry"
        if text is None:
            return "refused: the clipboard has no plain text" if not write else "script cancelled; clipboard write is unconfirmed"
        text = str(text)
        if write and text != args["text"]:
            return "script cancelled; clipboard write readback mismatched; verify before retry"
        if stop is not None and stop.is_set():
            return "script cancelled; Stop arrived after clipboard access; verify before retry"
        detail = (f"verified: clipboard text matches exactly ({len(text)} chars); text (untrusted): "
                  + repr(text)[:300] + "; sha256 " + hashlib.sha256(text.encode()).hexdigest()) if write else "clipboard text (untrusted):\n" + (text if full_read else text[:tools.MAX_RESULT])
        return f"script ok ({len(text)} chars)" + tools.PRIVATE + detail
    except Exception:
        return ("script cancelled; clipboard effect is unconfirmed; verify before retry" if dispatched
                else "refused: native clipboard is unavailable")


def _read_exact_file(path: Path, goal: str, *, owned_export=False) -> str:
    """Bounded no-follow file read; no app name or document index can broaden scope."""
    import stat
    parent = fd = None
    try:
        # owned_export is internal custody for this invocation's generated temporary;
        # it is never a model parameter or permission to read arbitrary paths.
        parent, leaf = tools._parent_fd(path, [path] if owned_export else tools.named_paths(goal))
        fd = os.open(leaf, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        info = os.fstat(fd)
        current = os.stat(leaf, dir_fd=parent, follow_symlinks=False)
        if (not stat.S_ISREG(info.st_mode) or info.st_size > 32_000
                or (info.st_dev, info.st_ino) != (current.st_dev, current.st_ino)):
            raise ValueError("exact_regular_file_required")
        data = b""
        while block := os.read(fd, 8192):
            data += block
            if len(data) > 32_000:
                raise ValueError("file_grew_beyond_limit")
        return data.decode("utf-8")
    finally:
        if fd is not None:
            os.close(fd)
        if parent is not None:
            os.close(parent)


def _textedit_edit(args: dict, goal: str, touched, stop) -> str:
    why = tools._template_app_block("TextEdit", goal) or tools.fs_block("read_text", args["path"], "", goal)
    if why:
        return why
    if _method_only(goal):
        return "refused: the task requires an input method; this template cannot prove that method"
    mode = args["mode"]
    verbs = r"\b(append|add|insert)\b" if mode == "append" else r"\b(replace|overwrite)\b" if mode == "replace" else None
    if (verbs is None or not re.search(verbs, goal, re.I)
            or re.search(r"(?i)\b(?:do not|never|don't)\s+(?:replace|overwrite)\b", goal) and mode == "replace"):
        return "refused: the task does not authorize that in-place edit"
    named_text = args["text"] in goal or (bool(re.search(r"(?i)\b(line|paragraph)\b", goal))
                                         and bool(args["text"].strip("\r\n")) and args["text"].strip("\r\n") in goal)
    if not args["text"] or not named_text or len(args["text"].encode()) > 16_000:
        return "refused: edit text must be named in the task and within the direct-tool limit"
    path = tools._resolve(args["path"])
    if path.suffix.lower() not in {".txt", ".md", ".csv"} or path not in tools.named_paths(goal):
        return "refused: in-place edits require an exactly named plain text file"
    try:
        before = _read_exact_file(path, goal)
    except (OSError, ValueError, UnicodeError):
        return "refused: the named UTF-8 file is unavailable or exceeds the direct-tool limit"
    separator = "\n" if (mode == "append" and before and not before.endswith("\n")
                          and not args["text"].startswith("\n") and re.search(r"(?i)\b(?:new|last) line\b", goal)) else ""
    expected = before + separator + args["text"] if mode == "append" else args["text"]
    if len(expected.encode()) > 32_000:
        return "refused: edited file exceeds the direct-tool limit"
    L = tools._literal
    code = _textedit_document(path, before) + f'''
    set text of d to {L(expected)}
    save d
    if (text of d as text) is not {L(expected)} then return "unverified: document text readback mismatched"
    if path of d is not {L(str(path))} then return "unverified: saved document path changed"
    return "verified: exact reviewed TextEdit document edited and saved"
end tell'''
    result = _osa("TextEdit", code, goal, stop)
    if not tools.readback(result):
        return result
    try:
        actual = _read_exact_file(path, goal)
    except (OSError, ValueError, UnicodeError):
        return "script cancelled; saved-file readback is unavailable; verify before retry"
    if actual != expected:
        return "script cancelled; app and saved-file readbacks disagree; verify before retry"
    if touched is not None:
        touched.add(str(path))
    return "script ok (verified edit)" + tools.PRIVATE + f"verified: exact TextEdit document and UTF-8 file match ({len(actual)} chars); sha256 " + hashlib.sha256(actual.encode()).hexdigest()


def _textedit_document(path: Path, expected: str) -> str:
    """Address only an already-open exact document, without launching or opening."""
    L = tools._literal
    return f'''if application "TextEdit" is not running then return "refused: TextEdit background editing requires the exact reviewed document to be already open; opening it can interrupt your current app"
tell application "TextEdit"
    set hits to documents whose path is {L(str(path))}
    if (count of hits) is 0 then return "refused: TextEdit background editing requires the exact reviewed document to be already open; opening it can interrupt your current app"
    if (count of hits) is not 1 then return "refused: expected one already-open document with the exact reviewed path"
    set d to item 1 of hits
    if (text of d as text) is not {L(expected)} then return "refused: document has unsaved or concurrent changes"
'''


def textedit_open_readback(path: Path, expected: str, goal: str, stop) -> str:
    """Read an already-open exact app document; never launch or open it."""
    code = _textedit_document(path, expected) + '''
    return "verified: exact TextEdit document path and text match the reviewed file"
end tell'''
    return _osa("TextEdit", code, goal, stop)


def _textedit_copy_save(args: dict, goal: str, touched, stop) -> str:
    why = (tools._template_app_block("TextEdit", goal)
           or tools.fs_block("read_text", args["src"], "", goal)
           or tools.fs_block("write_text", args["dest"], "", goal))
    if why:
        return why
    if _method_only(goal) or not re.search(r"(?i)\bcopy\b.*\bpaste\b", goal):
        return "refused: the task must authorize copying and pasting without prescribing another input method"
    src, dest = tools._resolve(args["src"]), tools._resolve(args["dest"])
    if src == dest or any(p.suffix.lower() not in {".txt", ".md", ".csv"} for p in (src, dest)):
        return "refused: clipboard transfer requires distinct plain-text source and destination files"
    return "refused: creating a new TextEdit document is unavailable in background mode because opening it can interrupt your current app; no clipboard or file changes were made"


def _mail_draft(args: dict, goal: str, stop) -> str:
    why = tools._template_app_block("Mail", goal)
    if why:
        return why
    if _method_only(goal) or not re.search(r"(?i)\bdraft\b", goal):
        return "refused: the task must authorize a Mail draft without prescribing another input method"
    parameter_error = mail_draft_parameter_error(args, goal)
    if parameter_error:
        return parameter_error
    recipients = _mail_recipients(args["to"])
    L = tools._literal
    addresses = "{" + ", ".join(L(r) for r in recipients) + "}"
    code = f'''tell application "Mail"
    if (count of (messages of drafts mailbox whose subject is {L(args["subject"])} and deleted status is false)) is not 0 then return "refused: a draft with that subject already exists; inspect it before retry"
    if (count of (outgoing messages whose subject is {L(args["subject"])})) is not 0 then return "refused: an outgoing message with that subject already exists; inspect it before retry"
    set m to make new outgoing message with properties {{subject:{L(args["subject"])}, content:{L(args["body"])}, visible:false}}
    set expectedAddresses to {addresses}
    repeat with addressText in expectedAddresses
        tell m to make new to recipient at end of to recipients with properties {{address:contents of addressText}}
    end repeat
    save m
    repeat 20 times
        set hits to messages of drafts mailbox whose subject is {L(args["subject"])} and deleted status is false
        if (count of hits) is 1 then exit repeat
        delay 0.1
    end repeat
    if (count of hits) is not 1 then return "unverified: draft persistence is unconfirmed"
    set savedDraft to item 1 of hits
    set actualAddresses to address of every to recipient of savedDraft
    if actualAddresses is not expectedAddresses then return "unverified: saved draft recipients mismatched"
    if (content of savedDraft as text) does not contain {L(args["body"])} then return "unverified: saved draft body mismatched"
    return "verified: one persisted Mail draft with requested subject, body and " & (count of actualAddresses) & " recipients; no send command executed"
end tell'''
    return _osa("Mail", code, goal, stop)


def _numbers_csv(args: dict, goal: str, touched, stop) -> str:
    why = tools._template_app_block("Numbers", goal) or tools.fs_block("write_text", args["path"], args["csv"], goal)
    if why:
        return why
    if _method_only(goal) or not re.search(r"(?i)\b(create|make|enter|spreadsheet)\b", goal):
        return "refused: the task does not authorize creating a Numbers spreadsheet by template"
    try:
        rows = list(csv.reader(io.StringIO(args["csv"], newline=""), strict=True))
    except csv.Error:
        return "refused: malformed CSV"
    if (not rows or not rows[0] or len(rows) > 100 or len(rows[0]) > 26 or len(args["csv"]) > 16_000
            or any(len(row) != len(rows[0]) for row in rows)):
        return "refused: CSV must be a bounded rectangular table (100 rows, 26 columns)"
    expected_rows = [list(row) for row in rows]
    # Only bounded numeric SUM references are accepted. Validate and calculate
    # them independently; a CSV containing any number is not formula proof.
    for ri, row in enumerate(rows):
        for ci, value in enumerate(row):
            if not value.startswith(("=", "+", "@")):
                continue
            match = re.fullmatch(r"=SUM\(([A-Z])([1-9][0-9]*):([A-Z])([1-9][0-9]*)\)", value, re.I)
            if not match:
                return "refused: only bounded numeric SUM cell-range formulas are supported"
            sc, sr, ec, er = ord(match[1].upper()) - 65, int(match[2]) - 1, ord(match[3].upper()) - 65, int(match[4]) - 1
            if (not 0 <= sc <= ec < len(rows[0]) or not 0 <= sr <= er < len(rows)
                    or sr <= ri <= er and sc <= ci <= ec):
                return "refused: SUM range is outside the table or includes its own cell"
            try:
                numbers = [Decimal(rows[r][c]) for r in range(sr, er + 1) for c in range(sc, ec + 1)]
                if any(not n.is_finite() for n in numbers):
                    raise InvalidOperation
                expected_rows[ri][ci] = str(sum(numbers))
            except InvalidOperation:
                return "refused: SUM requires finite literal numeric cells"
    path = tools._resolve(args["path"])
    if path.suffix.lower() != ".csv" or not path.parent.is_dir():
        return "refused: export requires a .csv path with an existing reviewed parent folder"
    temporary = path.parent / (".supymem-cu-" + uuid.uuid4().hex + ".csv")
    L = tools._literal
    assignments = []
    for r, row in enumerate(rows, 1):
        for c, value in enumerate(row):
            cell = f"{chr(65 + c)}{r}"
            assignments.append(f'set value of cell "{cell}" of t to {L(value)}')
            if value.startswith("="):
                assignments.append(f'if (formula of cell "{cell}" of t as text) is not {L(value)} then return "unverified: cell formula mismatched"')
            else:
                assignments.append(f'if (formatted value of cell "{cell}" of t as text) is not {L(value)} then return "unverified: cell value mismatched"')
    code = f'''tell application "Numbers"
    set d to make new document
    set t to first table of first sheet of d
    set header row count of t to 0
    set header column count of t to 0
    set row count of t to {len(rows)}
    set column count of t to {len(rows[0])}
    {chr(10).join(assignments)}
    export d to POSIX file {L(str(temporary))} as CSV
    close d saving no
    return "verified: owned Numbers cells read back and exported"
end tell'''
    try:
        result = _osa("Numbers", code, goal, stop)
        if not tools.readback(result):
            return result
        if stop is not None and stop.is_set():
            return "script cancelled; Numbers export occurred before Stop; verify before retry"
        exported = _read_exact_file(temporary, goal, owned_export=True)
        actual = list(csv.reader(io.StringIO(exported, newline="")))
        if len(actual) != len(rows) or any(len(row) != len(rows[0]) for row in actual):
            return "script cancelled; Numbers export dimensions mismatched; verify before retry"
        for r, row in enumerate(expected_rows):
            for c, value in enumerate(row):
                if actual[r][c] != value:
                    return "script cancelled; Numbers CSV readback mismatched; verify before retry"
        published = tools.run_fs("write_text", args["path"], exported, goal, touched, stop=stop)
        if published != "fs write_text ok":
            return published
        return "script ok (verified export)" + tools.PRIVATE + f"verified: Numbers cells and exported CSV agree ({len(rows)} rows, {len(rows[0])} columns); CSV (untrusted):\n" + repr(actual)[:tools.MAX_RESULT]
    except FileExistsError:
        return "refused: destination already exists; the owned Numbers export was not published"
    except (OSError, ValueError, UnicodeError, csv.Error):
        return "script cancelled; Numbers export readback is unconfirmed; verify before retry"
    finally:
        temporary.unlink(missing_ok=True)
