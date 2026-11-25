"""Bounded private task control. No caller-selected URL or command credential."""
import json
import time

import httpx

from app.builder import intent_bridge
from app.builder.model_proxy import BuildModelError


def _request(run_id, generation, method, path, body=None):
    endpoint, token, _ = intent_bridge._context(run_id, generation)
    headers = {"Authorization": "Bearer " + token, "X-Supymem-Build-ID": run_id,
               "X-Supymem-Generation": str(generation), "Accept-Encoding": "identity"}
    try:
        timeout = 90 if path == "/v1/source" else 30
        deadline = time.monotonic() + timeout
        client_context = (httpx.Client(timeout=httpx.Timeout(float(timeout), connect=3.0),
                                       follow_redirects=False, trust_env=False)
                          if path == "/v1/source" else intent_bridge._client())
        with client_context as client, client.stream(
                              method, endpoint + path, headers=headers, json=body) as response:
            if time.monotonic() > deadline:
                raise BuildModelError("build_task_control_response_limit")
            if path == "/v1/source" and response.status_code in {409, 413, 422}:
                raise BuildModelError("build_source_seed_rejected")
            if (response.status_code not in {200, 202}
                    or response.headers.get("content-type", "").split(";", 1)[0] != "application/json"
                    or response.headers.get("content-encoding", "identity") != "identity"):
                raise BuildModelError("build_task_control_unavailable")
            length = response.headers.get("content-length")
            if length is not None and (not length.isdecimal() or int(length) > 262144):
                raise BuildModelError("build_task_control_response_limit")
            content = bytearray()
            for chunk in response.iter_raw():
                content.extend(chunk)
                if len(content) > 262144 or time.monotonic() > deadline:
                    raise BuildModelError("build_task_control_response_limit")
            if time.monotonic() > deadline:
                raise BuildModelError("build_task_control_response_limit")
            if length is not None and int(length) != len(content):
                raise BuildModelError("build_task_control_response_invalid")
            value = json.loads(content)
            if not isinstance(value, dict):
                raise BuildModelError("build_task_control_response_invalid")
            return value
    except BuildModelError:
        raise
    except (httpx.HTTPError, ValueError) as exc:
        raise BuildModelError("build_task_control_unavailable") from exc


def state(run_id, generation):
    result = _request(run_id, generation, "GET", "/v1/state")
    if (result.get("build_id") != run_id or type(result.get("generation")) is not int
            or result["generation"] != generation or result.get("persistence_available") is not True
            or not isinstance(result.get("commands"), list) or len(result["commands"]) > 100):
        raise BuildModelError("build_task_control_scope_invalid")
    return result


def submit(run_id, generation, body):
    result = _request(run_id, generation, "POST", "/v1/commands", body)
    if result.get("command_id") != body["command_id"] or result.get("kind") != body["kind"]:
        raise BuildModelError("build_task_control_scope_invalid")
    return result


def seed_source(run_id, generation, *, profile, source_artifact_id, sha256, manifest, blob):
    """Send a verified, owner-scoped prior artifact to the bound private task."""
    import base64
    if len(blob) > 32 * 1024 * 1024:
        raise BuildModelError("build_source_transfer_limit")
    body = {"profile": profile, "source_artifact_id": source_artifact_id, "sha256": sha256,
            "manifest": manifest, "archive_base64": base64.b64encode(blob).decode("ascii")}
    result = _request(run_id, generation, "POST", "/v1/source", body)
    from core.artifact_snapshot import manifest_digest
    if result != {"profile": profile, "source_artifact_id": source_artifact_id, "sha256": sha256,
                   "manifest_digest": manifest_digest(manifest)}:
        raise BuildModelError("build_source_receipt_invalid")
    return result
