"""Fixed authenticated QA transfer; the task cannot select another URL or source."""
from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx

from core.static_qa_contracts import Input, Report, MAX_REPORT_BYTES, MAX_EVIDENCE_BYTES
from .intent_bridge import _endpoint
from .model_proxy import BuildModelError

_clock = time.monotonic


def _client():
    return httpx.AsyncClient(timeout=httpx.Timeout(10, connect=3), trust_env=False, follow_redirects=False)


def request(binding, method, path, **kwargs):
    # A wall deadline must cover slow uploads as well as responses. A socket's
    # per-write timeout alone can reset indefinitely when a peer drips reads.
    try:
        return asyncio.run(_request(binding, method, path, **kwargs))
    except TimeoutError as exc:
        raise BuildModelError("qa_task_deadline") from exc


async def _request(binding, method, path, *, body=None, media="application/json", cap=MAX_REPORT_BYTES, expected_hash=None, input_digest=None):
    base = _endpoint(binding["endpoint"])
    headers = {"Authorization": "Bearer " + binding["token"], "X-Supymem-Build-ID": binding["build_id"],
               "X-Supymem-QA-ID": binding["qa_id"], "X-Supymem-Generation": str(binding["generation"]), "Accept-Encoding": "identity"}
    if expected_hash:
        headers["X-Supymem-Artifact-SHA256"] = expected_hash
    if input_digest:
        headers["X-Supymem-Input-Digest"] = input_digest
    if body is not None:
        headers["Content-Type"] = "application/zip" if method == "PUT" else "application/json"
    deadline = _clock() + 30
    try:
        async with asyncio.timeout(30), _client() as client, client.stream(method, base + path, headers=headers, content=body) as response:
            length = response.headers.get("content-length")
            if (_clock() > deadline or response.status_code not in {200, 202}
                    or response.headers.get("content-encoding", "identity").lower() != "identity"
                    or response.headers.get("content-type", "").split(";", 1)[0] != media
                    or length is not None and (not length.isdecimal() or int(length) > cap)):
                raise BuildModelError("qa_task_response_invalid")
            result = bytearray()
            async for chunk in response.aiter_raw():
                result.extend(chunk)
                if len(result) > cap or _clock() > deadline:
                    raise BuildModelError("qa_task_response_limit")
            if (_clock() > deadline or length is not None and int(length) != len(result)
                    or expected_hash and (response.headers.get("x-supymem-artifact-sha256") != expected_hash
                        or hashlib.sha256(result).hexdigest() != expected_hash)):
                raise BuildModelError("qa_task_response_integrity")
            return bytes(result)
    except BuildModelError:
        raise
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
        raise BuildModelError("qa_task_response_unavailable") from exc


def state(binding, *, method="GET", path="/v1/qa/state", body=None, input_digest=None):
    try:
        value = json.loads(request(binding, method, path, body=body, input_digest=input_digest))
        if (not isinstance(value, dict) or (value.get("build_run_id"), value.get("qa_run_id"), value.get("generation"))
                != (binding["build_id"], binding["qa_id"], binding["generation"]) or type(value.get("generation")) is not int
                or value.get("phase") not in {"empty", "awaiting_input", "ready", "running", "completed", "interrupted", "failed"}):
            raise ValueError()
        return value
    except BuildModelError:
        raise
    except (ValueError, TypeError, KeyError) as exc:
        raise BuildModelError("qa_task_state_invalid") from exc


def progress(binding, request_input, source_reader, *, permit_execution):
    """Every mutating step is idempotent and rechecks trusted admission first."""
    spec = Input.model_validate(request_input)
    try:
        capabilities = json.loads(request(binding, "GET", "/health"))
    except (ValueError, TypeError) as exc:
        raise BuildModelError("qa_harness_contract_mismatch") from exc
    versions = capabilities.get("case_contract_versions", [capabilities.get("case_contract_version")]) if isinstance(capabilities, dict) else None
    if (not isinstance(capabilities, dict) or capabilities.get("status") != "ok"
            or type(capabilities.get("case_contract_version")) is not int
            or not isinstance(versions, list) or any(type(version) is not int for version in versions)
            or spec.contract.case_contract_version not in versions
            or not isinstance(capabilities.get("profiles"), list)
            or spec.contract.profile not in capabilities["profiles"]):
        raise BuildModelError("qa_harness_contract_mismatch")
    current = state(binding)
    if current["phase"] == "empty":
        permit_execution()
        current = state(binding, method="POST", path="/v1/qa/input-manifest", body=spec.model_dump_json().encode())
    if (current.get("input_digest"), current.get("command_id")) != (spec.digest, spec.command_id):
        raise BuildModelError("qa_task_input_mismatch")
    if current["phase"] == "awaiting_input":
        permit_execution()
        blob = source_reader()
        if len(blob) != spec.size_bytes or hashlib.sha256(blob).hexdigest() != spec.source_sha256:
            raise BuildModelError("qa_source_changed")
        permit_execution()
        current = state(binding, method="PUT", path="/v1/qa/input", body=blob, input_digest=spec.digest)
    if current["phase"] == "ready":
        permit_execution()
        current = state(binding, method="POST", path="/v1/qa/run", body=json.dumps({"command_id": spec.command_id, "input_digest": spec.digest}).encode())
    return current


def result(binding, current):
    raw = request(binding, "GET", "/v1/qa/report", expected_hash=current["report_sha256"])
    report = Report.model_validate_json(raw)
    if len(raw) != current["report_size_bytes"]:
        raise BuildModelError("qa_report_size_mismatch")
    evidence, size = {}, len(raw)
    for item in report.evidence:
        blob = request(binding, "GET", "/v1/qa/evidence/" + item.id, media="image/png", cap=item.size_bytes, expected_hash=item.sha256)
        size += len(blob)
        if size > MAX_EVIDENCE_BYTES:
            raise BuildModelError("qa_evidence_size_limit")
        evidence[item.id] = blob
    return raw, evidence
