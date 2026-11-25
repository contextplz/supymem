"""Owned-record helpers for Calendar / Reminders / Notes / Mail / Numbers.

Every record is named with the task uid (calendar/list `SupymemCU-<uid>`, note/draft titles carry the uid),
created only by task setup and removed by exact name on teardown. Read-backs only look at those records.
"""
from __future__ import annotations

import os
import threading

from . import fmt

CAL = 'SupymemCU-{uid}'
ISO = ('on iso(d)\n return (year of d as text) & "-" & text -2 thru -1 of ("0" & ((month of d) as integer)) & "-" & '
       'text -2 thru -1 of ("0" & day of d) & " " & text -2 thru -1 of ("0" & hours of d) & ":" & text -2 thru -1 of ("0" & minutes of d)\nend iso\n')


def _guard(ctx, name):
    assert ctx.uid in name, "cleanup name must carry the task uid"
    return name


def _tell(ctx, app, body):
    return ctx.osa(f'tell application "{app}"\n{body}\nend tell')


def need_app(path):
    def setup(ctx):
        if not os.path.exists(path):
            raise RuntimeError("qa_owned_readback_unavailable")
    return setup


def textedit_seed_open(rel, content):
    """Prepare the explicitly already-open fixture before the focus baseline."""
    def setup(ctx):
        import json
        path = ctx.seed(rel, content)
        # Only this setup-created exact file is eligible for app cleanup.
        literal = json.dumps(str(path), ensure_ascii=False)
        if path.parent.resolve() != ctx.dir.resolve():
            raise RuntimeError("qa_owned_textedit_path_required")
        def cleanup():
            return ctx.osa(f'''if application "TextEdit" is not running then return "0"
tell application "TextEdit"
    repeat with d in (documents whose path is {literal})
        close d saving no
    end repeat
    return count of (documents whose path is {literal})
end tell''') == "0"
        ctx.cleanups.append(cleanup)  # registered before an open can take effect
        result = ctx.osa(f'''tell application "TextEdit"
    open POSIX file {literal}
    repeat 20 times
        set hits to documents whose path is {literal}
        if (count of hits) is 1 then exit repeat
        delay 0.1
    end repeat
    if (count of hits) is not 1 then return "unverified"
    if (text of item 1 of hits as text) is not {json.dumps(fmt(content, ctx.vars), ensure_ascii=False)} then return "unverified"
    return "verified"
end tell''')
        if result != "verified":
            raise RuntimeError("qa_owned_textedit_open_unverified")
    return setup


def calendar_setup(ctx):
    name = fmt(CAL, ctx.vars)
    if _tell(ctx, "Calendar", f'make new calendar with properties {{name:"{name}"}}\nreturn "ok"') is None:
        raise RuntimeError("qa_owned_readback_unavailable")
    ctx.cleanups.append(lambda: _tell(ctx, "Calendar", f'delete (calendars whose name is "{_guard(ctx, name)}")\nreturn count of (calendars whose name is "{name}")') == "0")


def calendar_seed_event(title, start="2031-03-04 10:00", hours=1):
    """Setup helper: owned event on the owned calendar (for the delete task)."""
    def setup(ctx):
        calendar_setup(ctx)
        name, t = fmt(CAL, ctx.vars), fmt(title, ctx.vars)
        _tell(ctx, "Calendar", f'tell calendar "{name}"\nmake new event with properties {{summary:"{t}", start date:date "{start_us(start)}", end date:date "{start_us(start, hours)}"}}\nend tell')
    return setup


def start_us(iso, plus_hours=0):
    import datetime
    d = datetime.datetime.strptime(iso, "%Y-%m-%d %H:%M") + datetime.timedelta(hours=plus_hours)
    return d.strftime("%B %-d, %Y %H:%M:%S")


def read_events(ctx):
    name = fmt(CAL, ctx.vars)
    script = (f'{ISO}tell application "Calendar"\nset out to ""\nrepeat with e in (events of calendar "{name}")\n'
              'set out to out & summary of e & "|" & my iso(start date of e) & "|" & my iso(end date of e) & "|" & (allday event of e) & linefeed\n'
              'end repeat\nreturn out\nend tell')
    return ctx.osa(script)


def parse_events(out):
    """'title|start|end|allday' lines -> list of tuples (title, start, end, allday)."""
    rows = [l.split("|") for l in (out or "").splitlines() if l.strip()]
    return [(r[0], r[1], r[2], r[3] == "true") for r in rows if len(r) == 4]


def events_check(expected):
    """expected: list of (title, start, end, allday); exact set equality on the owned calendar."""
    def check(ctx):
        out = read_events(ctx)
        if out is None:
            return None, "calendar read-back unavailable"
        want = sorted((fmt(t, ctx.vars), s, e, a) for t, s, e, a in expected)
        return sorted(parse_events(out)) == want, "owned calendar events exact"
    return check


def reminders_setup(ctx):
    name = fmt(CAL, ctx.vars)
    if _tell(ctx, "Reminders", f'make new list with properties {{name:"{name}"}}\nreturn "ok"') is None:
        raise RuntimeError("qa_owned_readback_unavailable")
    ctx.cleanups.append(lambda: _tell(ctx, "Reminders", f'delete (lists whose name is "{_guard(ctx, name)}")\nreturn count of (lists whose name is "{name}")') == "0")


def reminders_seed(*titles):
    def setup(ctx):
        reminders_setup(ctx)
        for t in titles:
            _tell(ctx, "Reminders", f'tell list "{fmt(CAL, ctx.vars)}" to make new reminder with properties {{name:"{fmt(t, ctx.vars)}"}}')
    return setup


def read_reminders(ctx):
    name = fmt(CAL, ctx.vars)
    return ctx.osa(f'tell application "Reminders"\nset out to ""\nrepeat with r in (reminders of list "{name}")\n'
                   'set out to out & name of r & "|" & (completed of r) & linefeed\nend repeat\nreturn out\nend tell')


def reminders_check(expected):
    """expected: {title: completed_bool}; exact."""
    def check(ctx):
        out = read_reminders(ctx)
        if out is None:
            return None, "reminders read-back unavailable"
        got = {l.split("|")[0]: l.split("|")[1] == "true" for l in out.splitlines() if "|" in l}
        return got == {fmt(k, ctx.vars): v for k, v in expected.items()}, "owned reminders exact"
    return check


def note_title(ctx, suffix=""):
    return f"SupymemCU {ctx.uid}{suffix}"


def notes_cleanup(ctx):
    ctx.cleanups.append(lambda: _tell(ctx, "Notes", f'delete (notes whose name starts with "SupymemCU {ctx.uid}")\nreturn count of (notes whose name starts with "SupymemCU {ctx.uid}")') == "0")


def notes_check(expected):
    """expected: {title_suffix: [required body lines]}; each owned note must exist exactly once with those lines in order."""
    def check(ctx):
        for suffix, lines in expected.items():
            t = note_title(ctx, suffix)
            out = ctx.osa(f'tell application "Notes"\nset ns to (notes whose name is "{t}")\nif (count of ns) is not 1 then return "COUNT" & (count of ns)\nreturn plaintext of item 1 of ns\nend tell')
            if out is None:
                return None, "notes read-back unavailable"
            body = [l.strip() for l in out.splitlines() if l.strip()]
            if body != [t] + [fmt(l, ctx.vars) for l in lines]:
                return False, f"note {suffix!r} body {body}"
        return True, "owned notes exact"
    return check


def notes_seed(suffix, body):
    def setup(ctx):
        notes_cleanup(ctx)
        t = note_title(ctx, suffix)
        if ctx.osa(f'tell application "Notes" to make new note with properties {{name:"{t}", body:"{t}<br>{fmt(body, ctx.vars)}"}}') is None:
            raise RuntimeError("qa_owned_readback_unavailable")
    return setup


def draft_read(ctx, subject):
    s = fmt(subject, ctx.vars)
    return ctx.osa(f'tell application "Mail"\nset d to (messages of drafts mailbox whose subject is "{s}" and deleted status is false)\nset n to (count of (messages of sent mailbox whose subject is "{s}"))\n'
                   'if (count of d) is 0 then return "0||||" & n\nset m to item 1 of d\nset r to ""\nrepeat with x in to recipients of m\nset r to r & address of x & ","\nend repeat\n'
                   'return "" & (count of d) & "|" & r & "|" & content of m & "|" & n\nend tell')


def parse_draft(out):
    """'n|to,|body|sent' -> (drafts, recipients, body, sent); body may contain '|'."""
    try:
        n, rest = out.split("|", 1)
        to, rest = rest.split("|", 1)
        body, sent = rest.rsplit("|", 1)
        return int(n), [a for a in to.split(",") if a], body.strip(), int(sent)
    except (AttributeError, ValueError):
        return None


def draft_check(subject, to, body_contains=()):
    def check(ctx):
        ctx.cleanups.append(lambda: cleanup_draft(ctx, subject))
        d = parse_draft(draft_read(ctx, subject))
        if d is None:
            return None, "mail read-back unavailable"
        n, rcpt, body, sent = d
        ok = n == 1 and sent == 0 and rcpt == [fmt(a, ctx.vars) for a in to] and all(fmt(b, ctx.vars) in body for b in body_contains)
        return ok, f"{n} draft(s), recipients {rcpt}, sent={sent}"
    return check


def sent_zero(subject):
    def check(ctx):
        out = draft_read(ctx, subject)
        d = parse_draft(out)
        ctx.cleanups.append(lambda: cleanup_draft(ctx, subject))
        return (None if d is None else d[3] == 0), "nothing in Sent for subject"
    return check


def cleanup_draft(ctx, subject):
    """Mail keeps soft-deleted messages in enumeration; prove no active draft."""
    s = _guard(ctx, fmt(subject, ctx.vars))
    query = f'messages of drafts mailbox whose subject is "{s}" and deleted status is false'
    code = (f'delete ({query})\nrepeat 20 times\n'
            f'if (count of ({query})) is 0 then return "0"\ndelay 0.1\nend repeat\n'
            f'return count of ({query})')
    return _tell(ctx, "Mail", code) == "0"


def interrupt_after(seconds, app="Finder"):
    """Setup helper: simulate the user switching apps mid-task (activates `app` after N seconds)."""
    def setup(ctx):
        t = threading.Timer(seconds, lambda: ctx.osa(f'tell application "{app}" to activate'))
        t.daemon = True
        t.start()
        def close_timer():
            t.cancel()
            t.join(timeout=3)
            return not t.is_alive()
        ctx.cleanups.append(close_timer)
    return setup
