"""Trusted bounded intake child. No source directory enters Python's import path."""
from pathlib import Path
import json
import os
import resource
import sys


def main():
    # Limits are imposed before loading parsers/providers or untrusted metadata.
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_CPU, (10, 10))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (40 * 1024 * 1024, 40 * 1024 * 1024))
    code = Path(__file__).resolve()
    project = code.parents[4]
    # Fixed reviewed application roots. No PYTHONPATH or caller-provided import root.
    sys.path[:0] = [str(code.parents[2]), str(project / "firstparty"), str(project / "packages")]
    from app.imports.contracts import GitHubSource, ImportFailure, ResolvedGitHub, canonical
    from core.artifact_policy import ArtifactLimits, read_file, write_file
    raw = sys.stdin.buffer.read(8193)
    if len(raw) > 8192:
        raise ImportFailure("import_process_input_limit")
    request = json.loads(raw)
    if not isinstance(request, dict) or set(request) != {"kind", "input"}:
        raise ImportFailure("import_process_input_invalid")
    root = Path.cwd().resolve()
    kind = request["kind"]
    if kind == "resolve":
        from app.imports.acquisition import PublicGitHub
        with PublicGitHub() as github:
            resolved = github.resolve(GitHubSource.model_validate(request["input"]))
            result = {"resolved": resolved.model_dump(), "requests": github.calls, "bytes_received": github.bytes_received}
    elif kind == "download":
        from app.imports.acquisition import PublicGitHub
        with PublicGitHub() as github:
            blob = github.archive(ResolvedGitHub.model_validate(request["input"]))
            write_file(root, "output.zip", blob, limits=ArtifactLimits(max_file_bytes=32 * 1024 * 1024))
            import hashlib
            result = {"sha256": hashlib.sha256(blob).hexdigest(), "size_bytes": len(blob),
                      "requests": github.calls, "bytes_received": github.bytes_received}
    elif kind == "analyze":
        from app.imports.analysis import analyze_archive
        data = request["input"]
        if (not isinstance(data, dict) or set(data) != {"github_wrapper", "selected_root"}
                or type(data["github_wrapper"]) is not bool):
            raise ImportFailure("import_process_input_invalid")
        blob = read_file(root, "input.zip", limits=ArtifactLimits(max_file_bytes=32 * 1024 * 1024))
        source = analyze_archive(blob, github_wrapper=data["github_wrapper"], selected_root=data["selected_root"])
        write_file(root, "output.zip", source.archive, limits=ArtifactLimits(max_file_bytes=32 * 1024 * 1024))
        result = {"manifest": source.manifest, "report": source.report, "provenance": source.provenance}
    else:
        raise ImportFailure("import_process_kind_invalid")
    output = canonical({"ok": True, "result": result})
    if len(output) > 4 * 1024 * 1024:
        raise ImportFailure("import_process_output_limit")
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        # Never print a traceback, source paths, remote body or credential.
        error = getattr(exc, "code", None)
        import re
        if not isinstance(error, str) or not re.fullmatch(r"(?:import|artifact)_[a-z0-9_]{1,80}", error):
            error = "import_process_failed"
        sys.stdout.write(json.dumps({"ok": False, "error": error,
                                    "retry_after": getattr(exc, "retry_after", None), "status": getattr(exc, "status", 422)}))
        sys.stdout.flush()
        os._exit(1)

