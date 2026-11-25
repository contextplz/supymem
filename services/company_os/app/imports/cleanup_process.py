"""Fixed trusted cloud-metadata process. Timeout leaves a recoverable outcome."""
from __future__ import annotations
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from threading import BoundedSemaphore

from app.builder.model_proxy import BuildModelError
from .contracts import canonical
from .storage import ENV_FIELDS

_SLOT = BoundedSemaphore(1)


def run(operation, target, *, fence=None, plan=None):
    if sys.platform != "linux":
        raise BuildModelError("import_cleanup_runtime_unsupported")
    if operation not in {"fence", "plan", "erase"}:
        raise BuildModelError("import_cleanup_operation_invalid")
    value = canonical({"operation": operation, "target": target, "fence": fence, "plan": plan})
    if len(value) > 128 * 1024:
        raise BuildModelError("import_cleanup_input_limit")
    if not _SLOT.acquire(blocking=False):
        raise BuildModelError("import_cleanup_capacity")
    try:
        with tempfile.TemporaryDirectory(prefix="supymem-import-cleanup-") as path:
            env = {key: os.environ[key] for key in (*ENV_FIELDS, "IMPORT_CLEANUP_ENABLED", "IMPORT_CLEANUP_ACCEPTANCE") if key in os.environ}
            env.update(LANG="C.UTF-8", HOME=path, TMPDIR=path, AWS_EC2_METADATA_DISABLED="true")
            # ECS supplies only its credential URI to the same trusted worker.
            # No arbitrary endpoint, developer credential or provider secret.
            relative = os.getenv("AWS_CONTAINER_CREDENTIALS_RELATIVE_URI")
            if relative:
                if not re.fullmatch(r"/v2/credentials/[A-Za-z0-9-]{1,128}", relative):
                    raise BuildModelError("import_cleanup_credential_endpoint_invalid")
                env["AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"] = relative
            try:
                result = subprocess.run([sys.executable, "-I", str(Path(__file__).with_name("cleanup_entry.py").resolve())],
                    input=value, capture_output=True, cwd=path, env=env, timeout=120)
            except (subprocess.TimeoutExpired, OSError):
                raise BuildModelError("import_cleanup_process_outcome_unconfirmed") from None
            if len(result.stdout) > 128 * 1024:
                raise BuildModelError("import_cleanup_output_limit")
            try:
                response = json.loads(result.stdout)
                if not isinstance(response, dict) or type(response.get("ok")) is not bool:
                    raise ValueError()
            except (ValueError, RecursionError):
                raise BuildModelError("import_cleanup_process_outcome_unconfirmed") from None
            if result.returncode or not response["ok"]:
                code = response.get("error")
                if not isinstance(code, str) or not re.fullmatch(r"import_cleanup_[a-z_]{1,80}", code):
                    code = "import_cleanup_process_outcome_unconfirmed"
                raise BuildModelError(code)
            if not isinstance(response.get("result"), dict):
                raise BuildModelError("import_cleanup_output_invalid")
            return response["result"]
    finally:
        _SLOT.release()
