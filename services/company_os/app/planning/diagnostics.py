"""Read-only planning request and boundary diagnostic helpers."""

from contextlib import contextmanager
import hashlib
import json
from unittest.mock import patch


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def prepared(document):
    """Pure transformation; same aliasing/prompt/schema as the normal compiler."""
    from app.planning import compiler, request_policy
    runtime = document["_runtime"]
    original = runtime["context"]
    projected, sources, assets = compiler._project(original)
    request = request_policy.prepare(projected)
    return request, projected, {
        "context_sha256": digest(original),
        "projected_context_sha256": digest(projected),
        "request_sha256": digest({"messages": request.messages, "schema": request.schema,
                                  "max_tokens": request.max_output_tokens, "purpose": request_policy.PURPOSE,
                                  "provider_timeout_seconds": request.provider_timeout_seconds}),
        "schema_sha256": digest(request.schema), "source_count": len(sources), "resource_count": len(assets),
        "request_bytes": len(json.dumps(request.messages, ensure_ascii=False).encode()),
        "max_output_tokens": request.max_output_tokens, "policy": request_policy.public_policy(),
    }


@contextmanager
def diagnostic_boundary_log():
    """Keep the real scanner; emit its non-sensitive receipt before the call.

    A read-only operator diagnostic cannot insert the normal founder ledger.
    Only this standalone process's audit destination is changed. Classification,
    redaction, re-scan and refusal run through the unchanged production filter.
    """
    from app.boundary import filter as boundary_filter
    records = []
    def record(**value):
        safe = {"event": "operator_model_boundary", "purpose": value["purpose"],
                "classes": list(value["classes"]), "bytes_out": value["bytes_out"],
                "redactions": value.get("redactions", 0), "blocked": value["blocked"]}
        records.append(safe)
        print(json.dumps(safe, sort_keys=True), flush=True)
        return None
    def audit(*args, **kwargs):
        # The scanner already emitted the bounded refusal record above.
        return None
    with patch.object(boundary_filter, "record", record), patch.object(boundary_filter, "audit", audit):
        yield records
