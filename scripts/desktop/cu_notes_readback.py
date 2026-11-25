"""Fresh owned Notes readback. Raw scoped text remains private to this oracle."""

from __future__ import annotations


def quoted(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def canonical_text(text):
    return text.replace("\r\n", "\n").replace("\r", "\n").removesuffix("\n")


class OwnedNotesReadback:
    def __init__(self, reader, title, edited_title, body, added_line):
        self.reader, self.title, self.edited_title = reader, title, edited_title
        self.body, self.added_line = body, added_line
        self.armed = False
        self.note_id = None
        self.edit_armed = False

    def snapshot(self, title):
        script = (
            'tell application "Notes"\n'
            f"set matches to every note whose name is {quoted(title)}\n"
            "if (count of matches) is not 1 then return (count of matches) as text\n"
            "set n to item 1 of matches\n"
            'return "1" & (ASCII character 30) & (id of n) & (ASCII character 30) & (plaintext of n)\n'
            "end tell"
        )
        value = self.reader(script)
        if value is None:
            return None
        parts = value.split(chr(30))
        if parts == ["0"]:
            return (0, None, None)
        if len(parts) != 3 or parts[0] != "1" or not parts[1]:
            return (None, None, None)
        return (1, parts[1], canonical_text(parts[2]))

    def prepare_create(self):
        self.armed = False
        self.note_id = None
        value = self.snapshot(self.title)
        if value is None or value[0] != 0:
            raise RuntimeError("qa_owned_notes_scope_not_fresh")
        self.armed = True

    def verify_create(self):
        value = self.snapshot(self.title)
        if value is None:
            return None, "qa_owned_notes_readback_unavailable"
        ok = self.armed and value[0] == 1 and value[2] == self.title + "\n" + self.body
        if self.note_id is not None:
            ok = ok and value[1] == self.note_id
        if ok:
            self.note_id = value[1]
        return bool(ok), "qa_owned_notes_exact_content"

    def prepare_edit(self):
        self.edit_armed = False
        if self.verify_create()[0] is not True or self.snapshot(self.edited_title) != (
            0,
            None,
            None,
        ):
            raise RuntimeError("qa_owned_note_edit_unproven")
        self.edit_armed = True

    def verify_edit(self):
        value = self.snapshot(self.edited_title)
        if value is None:
            return None, "qa_owned_notes_readback_unavailable"
        expected = self.edited_title + "\n" + self.body + "\n" + self.added_line
        ok = (
            self.edit_armed
            and value[0] == 1
            and value[1] == self.note_id
            and value[2] == expected
        )
        return bool(
            ok and self.snapshot(self.title) == (0, None, None)
        ), "qa_owned_notes_exact_edit"
