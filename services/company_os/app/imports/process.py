"""Supervise trusted intake code, including blocking DNS, with no inherited secrets."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore

from core.artifact_policy import ArtifactLimits, ArtifactRejected, read_file, write_file
from .contracts import ImportFailure, MAX_ARCHIVE_BYTES, canonical


@dataclass(frozen=True)
class ProcessResult:
    data: dict
    archive: bytes | None = None


_CHILD_SLOT = BoundedSemaphore(1)


def run(kind: str, data: dict, *, archive: bytes | None = None) -> ProcessResult:
    # Eight worker leases must not become eight independent 512 MiB parsers.
    if not _CHILD_SLOT.acquire(blocking=False):
        raise ImportFailure("import_process_capacity", status=503)
    try:
        return _run(kind, data, archive=archive)
    finally:
        _CHILD_SLOT.release()


def _run(kind: str, data: dict, *, archive: bytes | None = None) -> ProcessResult:
    if kind not in {"resolve", "download", "analyze"}:
        raise ImportFailure("import_process_kind_invalid")
    if sys.platform != "linux":
        raise ImportFailure("import_parser_platform_unsupported", status=503)
    request = canonical({"kind": kind, "input": data})
    if len(request) > 8192:
        raise ImportFailure("import_process_input_limit")
    with tempfile.TemporaryDirectory(prefix="supymem-import-") as temporary:
        root = Path(temporary).resolve()
        if archive is not None:
            try:
                write_file(root, "input.zip", archive, limits=ArtifactLimits(max_file_bytes=MAX_ARCHIVE_BYTES))
            except ArtifactRejected as exc:
                raise ImportFailure(str(exc)) from None
        # Only fixed interpreter/script. No source-dependent argv or shell.
        try:
            result = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("process_entry.py").resolve())],
                input=request, cwd=root, capture_output=True, timeout=65 if kind in {"resolve", "download"} else 20,
                env={"LANG": "C.UTF-8", "HOME": str(root), "TMPDIR": str(root)})
        except subprocess.TimeoutExpired:
            raise ImportFailure("import_process_deadline", status=503) from None
        except OSError:
            raise ImportFailure("import_process_unavailable", status=503) from None
        if len(result.stdout) > 4 * 1024 * 1024:
            raise ImportFailure("import_process_output_limit")
        try:
            parsed = json.loads(result.stdout)
            if not isinstance(parsed, dict) or type(parsed.get("ok")) is not bool:
                raise ValueError()
        except (ValueError, RecursionError):
            raise ImportFailure("import_process_failed", status=503) from None
        if result.returncode or not parsed["ok"]:
            code = parsed.get("error", "import_process_failed")
            if not isinstance(code, str) or not code.startswith(("import_", "artifact_")):
                code = "import_process_failed"
            retry = parsed.get("retry_after")
            raise ImportFailure(code, status=503 if parsed.get("status") in {429, 503} else 422, retry_after=retry if type(retry) is int and 0 < retry <= 900 else None)
        if not isinstance(parsed.get("result"), dict):
            raise ImportFailure("import_process_output_invalid")
        output = None
        if kind in {"download", "analyze"}:
            try:
                output = read_file(root, "output.zip", limits=ArtifactLimits(max_file_bytes=MAX_ARCHIVE_BYTES))
            except (ArtifactRejected, OSError):
                raise ImportFailure("import_process_output_invalid") from None
        return ProcessResult(parsed["result"], output)
