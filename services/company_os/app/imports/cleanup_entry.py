"""Private fixed cleanup entry; no source files, generic commands or cloud URLs."""
import json
import os
from pathlib import Path
import resource
import sys


def main():
    resource.setrlimit(resource.RLIMIT_AS, (768 * 1024 * 1024,) * 2)
    resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_FSIZE, (128 * 1024,) * 2)
    code = Path(__file__).resolve()
    sys.path[:0] = [str(code.parents[2]), str(code.parents[4] / "firstparty")]
    from app.imports.storage import cleanup_ready, configured
    from app.imports.cleanup_store import ImportObjectCleaner
    from app.imports.contracts import canonical
    cleanup_ready()  # Refuse before any AWS client/credential discovery.
    raw = sys.stdin.buffer.read(128 * 1024 + 1)
    request = json.loads(raw)
    if (len(raw) > 128 * 1024 or not isinstance(request, dict)
            or set(request) != {"operation", "target", "fence", "plan"}
            or request["operation"] not in {"fence", "plan", "erase"}):
        raise ValueError()
    target = request["target"]
    if not isinstance(target, dict) or set(target) != {"scope", "kind", "intent", "storage_config_digest"}:
        raise ValueError()
    store = configured()
    if store.config.fingerprint != target["storage_config_digest"]:
        raise ValueError()
    cleaner = ImportObjectCleaner(store, target["scope"], kind=target["kind"], intent=target["intent"])
    op = request["operation"]
    result = cleaner.fence() if op == "fence" else cleaner.plan(request["fence"]) if op == "plan" else cleaner.erase(request["fence"], request["plan"])
    output = canonical({"ok": True, "result": result})
    if len(output) > 128 * 1024:
        raise ValueError()
    sys.stdout.buffer.write(output)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        import re
        code = str(exc)
        if not re.fullmatch(r"import_cleanup_[a-z_]{1,80}", code):
            code = "import_cleanup_process_outcome_unconfirmed"
        sys.stdout.write(json.dumps({"ok": False, "error": code}))
        sys.stdout.flush()
        os._exit(1)
