"""Bounded, local text extraction; office files need only ZIP/XML, PDF uses pypdf."""

from __future__ import annotations

import csv
import io
import json
import re
import subprocess
import sys
import zipfile
from pathlib import PurePosixPath
from xml.etree import ElementTree

MAX_FILES = 50
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_TEXT = 2_000_000


def source_label(name: str) -> str:
    name = name.replace("\\", "/")
    if not name or len(name) > 1024 or name.startswith("/") or ".." in PurePosixPath(name).parts or any(ord(char) < 32 for char in name):
        raise ValueError("Use a file with a normal relative name and try again.")
    return name


def extract_text(name: str, data: bytes) -> str:
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Over 25 MB. Choose a smaller file or paste its key sections.")
    suffix = PurePosixPath(source_label(name)).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise ValueError("Password-protected PDF. Export an unlocked copy or paste the text.")
        if len(reader.pages) > 500:
            raise ValueError("Over 500 pages. Split this PDF and try again.")
        parts, extracted_size = [], 0
        for number, page in enumerate(reader.pages, 1):
            text = page.extract_text() or ""
            parts.append(f"Page {number}\n{text}")
            extracted_size += len(parts[-1])
            if extracted_size > MAX_TEXT:
                raise ValueError("Too much extracted text. Split the file and try again.")
        if not any((page.split("\n", 1)[1]).strip() for page in parts):
            raise ValueError("Your PDF has no selectable text. Export it as text or a text-based PDF, or paste the text into chat.")
        text = "\n\n".join(parts)
    elif suffix in {".docx", ".pptx", ".xlsx"}:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 10_000 or sum(entry.file_size for entry in entries) > MAX_TOTAL_BYTES:
                raise ValueError("This document expands too much. Split or export it as text.")

            def xml(path):
                payload = archive.read(path)
                if len(payload) > MAX_FILE_BYTES or b"<!DOCTYPE" in payload.upper() or b"<!ENTITY" in payload.upper():
                    raise ValueError("This document cannot be safely read. Export it as text.")
                return ElementTree.fromstring(payload)

            def words(element):
                return "".join(node.text or "" for node in element.iter() if node.tag.rsplit("}", 1)[-1] == "t")

            paths = archive.namelist()
            if suffix == ".docx":
                paths = [path for path in paths if re.fullmatch(r"word/(?:document|footnotes|endnotes|header\d+|footer\d+)\.xml", path)]
                text = "\n".join(words(node) for path in sorted(paths) for node in xml(path).iter() if node.tag.endswith("}p"))
            elif suffix == ".pptx":
                paths = [path for path in paths if re.fullmatch(r"ppt/(?:slides/slide|notesSlides/notesSlide)\d+\.xml", path)]
                paths.sort(key=lambda path: ("notesSlides" in path, int(re.search(r"(\d+)\.xml$", path)[1])))
                parts = []
                for path in paths:
                    body = "\n".join(words(node) for node in xml(path).iter() if node.tag.endswith("}p")).strip()
                    if body:
                        parts.append(path + "\n" + body)
                text = "\n\n".join(parts)
            else:
                strings = [words(node) for node in xml("xl/sharedStrings.xml") if node.tag.endswith("}si")] if "xl/sharedStrings.xml" in paths else []
                sheets = sorted(path for path in paths if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", path))
                parts = []
                for path in sheets:
                    sheet_rows = []
                    for row in xml(path).iter():
                        if not row.tag.endswith("}row"):
                            continue
                        cells = []
                        for cell in row:
                            value = next((node.text or "" for node in cell if node.tag.endswith("}v")), "")
                            if cell.get("t") == "s":
                                value = strings[int(value)]
                            elif cell.get("t") == "inlineStr":
                                value = words(cell)
                            elif not value:
                                value = next((node.text or "" for node in cell if node.tag.endswith("}f")), "")
                            if value:
                                cells.append(f"{cell.get('r', '')}: {value}")
                        if cells:
                            sheet_rows.append("\t".join(cells))
                    if sheet_rows:
                        parts.extend([path, *sheet_rows])
                text = "\n".join(parts)
    else:
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".heic", ".ico", ".zip", ".exe", ".dmg", ".mp4", ".mp3", ".woff", ".woff2"}:
            raise ValueError("Images and binaries are skipped. Share a text description instead.")
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        try:
            text = data.decode(encoding)
        except UnicodeError as exc:
            raise ValueError("Not plain text. Export as UTF-8 text or a supported document.") from exc
        if any(ord(char) < 32 and char not in "\n\r\t\f" for char in text):
            raise ValueError("Binary content is skipped. Export as plain text instead.")
        if suffix == ".csv":
            text = "\n".join("\t".join(row) for row in csv.reader(io.StringIO(text)))
    if len(text) > MAX_TEXT:
        raise ValueError("Too much extracted text. Split the file and try again.")
    if not text.strip():
        raise ValueError("No readable text. Paste a description or choose another file.")
    return text.strip()


def read_document(name: str, data: bytes) -> str:
    """An untrusted parser runs in a disposable, CPU/memory/time-limited process."""
    source_label(name)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("Over 25 MB. Choose a smaller file or paste its key sections.")
    try:
        result = subprocess.run([sys.executable, __file__, name], input=data, capture_output=True,
                                timeout=15, env={})
        output = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        raise ValueError("Could not read this file within its limits. Export as text, split it, or retry.") from None
    if output.get("error"):
        raise ValueError(output["error"])
    return output["text"]


if __name__ == "__main__":
    import os
    import resource
    import threading
    import time

    if sys.platform != "darwin":
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    else:
        def memory_watch():
            # ponytail: macOS has no effective address-space limit; poll RSS, use a sandbox worker for a hard allocation ceiling.
            while True:
                if resource.getrusage(resource.RUSAGE_SELF).ru_maxrss > 512 * 1024 * 1024:
                    os._exit(1)
                time.sleep(0.05)
        threading.Thread(target=memory_watch, daemon=True).start()
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    try:
        print(json.dumps({"text": extract_text(sys.argv[1], sys.stdin.buffer.read(MAX_FILE_BYTES + 1))}))
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
    except Exception:
        print(json.dumps({"error": "Could not read this document. Export as text or try another copy."}))
