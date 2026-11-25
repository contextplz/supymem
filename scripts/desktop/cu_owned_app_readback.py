"""QA-only, read-only numeric receipts for exact UUID-owned app objects.

No script creates, edits, deletes, lists personal titles, or changes permissions.
Actual UI actions belong to the general runtime. Missing readback stays unknown.
"""
from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


def clock_signature() -> tuple:
    local = time.localtime()
    return os.environ.get("TZ"), local.tm_zone, local.tm_gmtoff


def numbers(raw: str | None) -> list[int] | None:
    if raw is None or not re.fullmatch(r"[0-9]+(?:\t[0-9]+)*", raw):
        return None
    parts = raw.split("\t")
    if len(parts) > 18 or any(len(p) > 10 for p in parts):
        return None
    return [int(p) for p in parts]


# Gregorian civil components avoid locale-formatted date strings and epoch guesses.
DATE_FIELDS = """
on dateParts(d)
    return {(year of d) as integer, (month of d) as integer, (day of d) as integer, (hours of d) as integer, (minutes of d) as integer, (seconds of d) as integer}
end dateParts
on receipt(values)
    set AppleScript's text item delimiters to ASCII character 9
    return values as text
end receipt
"""


class OwnedAppReadback:
    def __init__(self, run_id: str, read: Callable[[str], str | None], *,
                 now: Callable[[], datetime] = datetime.now,
                 zone: Callable[[], tuple] = clock_signature,
                 proof_path: Path | None = None):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise ValueError("qa_invalid_owned_uuid")
        self.read, self.now, self.zone = read, now, zone
        self.run_id, self.proof_path = run_id, proof_path
        self.day, self.zone_at_start = now().date(), zone()
        self.start = datetime.combine(self.day + timedelta(days=1), datetime.min.time()).replace(hour=10)
        self.end = self.start + timedelta(hours=1)
        self.calendar = "Supymem QA calendar " + run_id
        self.event = "Supymem QA event " + run_id
        self.reminder_list = "Supymem QA list " + run_id
        self.reminder = "Supymem QA reminder " + run_id
        self.armed: set[str] = set()
        self.creation_proof: tuple[int, ...] | None = None

    def clock_ok(self) -> bool:
        return self.now().date() == self.day and self.zone() == self.zone_at_start

    @staticmethod
    def parts(d: datetime) -> list[int]:
        return [d.year, d.month, d.day, d.hour, d.minute, d.second]

    def script(self, kind: str) -> str:
        if kind == "calendar":
            return DATE_FIELDS + f'''
tell application "Calendar"
    set owned to calendars whose name is "{self.calendar}"
    if (count of owned) is not 1 then return my receipt({{count of owned}})
    set ownedCalendar to item 1 of owned
    set total to count of events of ownedCalendar
    set matches to events of ownedCalendar whose summary is "{self.event}"
    if (count of matches) is not 1 then return my receipt({{1, total, count of matches}})
    set e to item 1 of matches
    set allDayFlag to 0
    if allday event of e then set allDayFlag to 1
    set recurringFlag to 0
    if recurrence of e is not missing value and recurrence of e is not "" then set recurringFlag to 1
    set durationSeconds to (end date of e) - (start date of e)
    return my receipt({{1, total, 1}} & my dateParts(start date of e) & my dateParts(end date of e) & {{allDayFlag, recurringFlag, durationSeconds}})
end tell
'''
        if kind == "reminders":
            return DATE_FIELDS + f'''
tell application "Reminders"
    set owned to lists whose name is "{self.reminder_list}"
    if (count of owned) is not 1 then return my receipt({{count of owned}})
    set ownedList to item 1 of owned
    set total to count of reminders of ownedList
    set matches to reminders of ownedList whose name is "{self.reminder}"
    if (count of matches) is not 1 then return my receipt({{1, total, count of matches}})
    set r to item 1 of matches
    set completedFlag to 0
    if completed of r then set completedFlag to 1
    if due date of r is missing value then return my receipt({{1, total, 1, completedFlag, 0}})
    return my receipt({{1, total, 1, completedFlag, 1}} & my dateParts(due date of r))
end tell
'''
        raise ValueError("qa_invalid_readback_kind")

    def snapshot(self, kind: str) -> list[int] | None:
        if not self.clock_ok():
            return None
        try:
            value = numbers(self.read(self.script(kind)))
        except Exception:
            return None
        if not self.clock_ok() or value is None:
            return None
        if len(value) == 1 and value[0] != 1:
            return value
        if len(value) == 3 and value[0] == 1 and value[2] != 1:
            return value
        if kind == "calendar" and len(value) == 18:
            return value
        if kind == "reminders" and (len(value) == 11 or (len(value) == 5 and value[4] == 0)):
            return value
        return None

    def prepare(self, kind: str) -> None:
        # Refuse reuse, even if a pre-existing matching object could look correct.
        self.armed.discard(kind)
        if kind == "calendar":
            self.creation_proof = None
        value = self.snapshot(kind)
        if value is None:
            raise RuntimeError("qa_owned_readback_unavailable")
        if value != [0]:
            raise RuntimeError("qa_owned_scope_not_fresh")
        self.armed.add(kind)

    def calendar_expected(self) -> list[int]:
        return [1, 1, 1] + self.parts(self.start) + self.parts(self.end) + [0, 0, 3600]

    def verify_calendar(self) -> tuple[bool | None, str]:
        if "calendar" not in self.armed:
            return False, "qa_owned_creation_not_armed"
        value = self.snapshot("calendar")
        if value is None:
            return None, "qa_owned_readback_unavailable"
        ok = value == self.calendar_expected()
        self.creation_proof = tuple(value) if ok else None
        if ok and self.proof_path is not None:
            try:
                # Private fresh-run evidence only; no app IDs or personal text.
                fd = os.open(self.proof_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, "w") as receipt:
                    json.dump({"run_id": self.run_id, "receipt": value}, receipt)
            except OSError:
                self.creation_proof = None
                return None, "qa_creation_history_unavailable"
        return ok, "qa_calendar_exact_creation"

    def history_valid(self) -> bool:
        if self.creation_proof != tuple(self.calendar_expected()):
            return False
        if self.proof_path is None:
            return True
        try:
            if self.proof_path.is_symlink() or self.proof_path.stat().st_mode & 0o077:
                return False
            return json.loads(self.proof_path.read_text()) == {
                "run_id": self.run_id, "receipt": list(self.creation_proof)}
        except (OSError, ValueError):
            return False

    def prepare_delete(self) -> None:
        self.armed.discard("calendar_delete")
        if not self.history_valid():
            raise RuntimeError("qa_calendar_creation_unproven")
        value = self.snapshot("calendar")
        if value is None:
            raise RuntimeError("qa_owned_readback_unavailable")
        if tuple(value) != self.creation_proof:
            raise RuntimeError("qa_calendar_creation_changed")
        self.armed.add("calendar_delete")

    def verify_delete(self) -> tuple[bool | None, str]:
        if "calendar_delete" not in self.armed or not self.history_valid():
            return False, "qa_calendar_creation_unproven"
        value = self.snapshot("calendar")
        if value is None:
            return None, "qa_owned_readback_unavailable"
        # Require the exact private calendar still exists and is now empty.
        return value == [1, 0, 0], "qa_calendar_owned_deletion"

    def verify_reminder(self) -> tuple[bool | None, str]:
        if "reminders" not in self.armed:
            return False, "qa_owned_creation_not_armed"
        value = self.snapshot("reminders")
        if value is None:
            return None, "qa_owned_readback_unavailable"
        return value == [1, 1, 1, 0, 1] + self.parts(self.start), "qa_reminder_exact_due_date"

    def task(self, kind: str) -> str:
        date = self.start.strftime("%Y-%m-%d")
        if kind == "calendar":
            return (f"In Calendar create a new private calendar named '{self.calendar}', then in that calendar "
                    f"create exactly one non-recurring, non-all-day event titled '{self.event}' on {date} "
                    "at 10:00:00 local time ending 11:00:00 (one hour). Leave it saved for independent verification. "
                    "Do not modify any other calendar or event. Stop if you cannot create this private calendar.")
        if kind == "calendar_delete":
            return (f"In Calendar delete only the independently verified event '{self.event}' from the private "
                    f"calendar '{self.calendar}'. Leave that calendar present and empty. Do not modify any other data.")
        if kind == "reminders":
            return (f"In Reminders create a new private list named '{self.reminder_list}' and in that list "
                    f"add exactly one incomplete reminder titled '{self.reminder}' due {date} at 10:00:00 local time. "
                    "Do not modify any other list or reminder. Stop if you cannot create this private list.")
        raise ValueError("qa_invalid_readback_kind")
