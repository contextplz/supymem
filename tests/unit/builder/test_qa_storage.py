"""Exact-version S3 byte boundaries and passive PNG integrity, without AWS."""
from __future__ import annotations

import hashlib
from contextlib import nullcontext
import io
import json
import struct
from types import SimpleNamespace
from uuid import uuid4
import zlib

import pytest
from urllib3.exceptions import ProtocolError

from core.static_qa_contracts import VIEWPORTS
from tests.integration.test_postgres_auth_security import service_modules
from tests.qa_storage_fixtures import CONTRACT, POLICY, MemoryS3, png, report_fixture


@pytest.mark.parametrize("complete", [True, False])
def test_capture_preserves_report_completion_without_claiming_runtime_failure_is_content(monkeypatch, complete):
    from core.artifact_snapshot import canonical
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    blob, evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    document = json.loads(blob)
    if not complete:
        document.update(outcome="failed", evidence=[], cases=[{"id": "runtime:failure", "suite": "runtime",
            "target": "harness", "status": "failed", "observed": "Runner stopped", "expected": "Checks finish", "evidence_ids": []}])
        document["coverage"].update(complete=False, observed_cases=1)
        blob, evidence = canonical(document), {}
    row = SimpleNamespace(id=qa_id, build_run_id=build_id, input_digest=request.digest, workspace_id="synthetic",
        receipt_json=json.dumps({"trusted_task": True, "task_binding_digest": "binding", "scope": {},
            "input": request.model_dump(), "policy": POLICY}))
    findings = []
    session = SimpleNamespace(commit=lambda: None, add=findings.append)
    with service_modules("company_os"):
        from app.builder import qa_receipts
        monkeypatch.setattr(qa_receipts, "db", lambda: lambda: nullcontext(session))
        monkeypatch.setattr(qa_receipts, "_qa", lambda *_args: (SimpleNamespace(run=None), {}, row, json.loads(row.receipt_json)))
        monkeypatch.setattr(qa_receipts, "_event", lambda *_args: None)
        monkeypatch.setattr(qa_receipts, "serialize", lambda saved: json.loads(saved.receipt_json))
        monkeypatch.setattr(qa_receipts.S3ArtifactStore, "configured", lambda: SimpleNamespace(
            persist_qa=lambda data, _scope, **kwargs: {"key": kwargs["object_id"], "sha256": hashlib.sha256(data).hexdigest()}))
        result = qa_receipts.capture(qa_id, report_blob=blob, evidence=evidence, task_binding_digest="binding",
            generation=1, controller_id="synthetic", attempt=1)
    assert result["verified_report"] is True and result["report_complete"] is complete
    assert result["outcome"] == ("passed" if complete else "failed")
    assert len(findings) == (0 if complete else 1)


@pytest.fixture()
def store():
    with service_modules("company_os"):
        from app.builder import artifact_store
        from app.builder.model_proxy import BuildModelError
        config = artifact_store.ArtifactStoreConfig("test-artifacts", "managed-builds",
            "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
        client = MemoryS3()
        value = artifact_store.S3ArtifactStore(config, client)
        scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", build_id="build", generation=1)
        blob = b"isolated storage protocol bytes"
        receipt = value.persist(blob, dict(archive_sha256=hashlib.sha256(blob).hexdigest(),
                                          size_bytes=len(blob), manifest_digest="a" * 64), scope)
        yield value, client, receipt, scope, blob, artifact_store, BuildModelError


@pytest.mark.parametrize("field,value", [("VersionId", "wrong"), ("ContentLength", 0), ("ChecksumSHA256", "wrong"),
    ("ContentType", "text/html"), ("Metadata", {}), ("ServerSideEncryption", "AES256"),
    ("SSEKMSKeyId", "wrong-key"), ("ContentEncoding", "gzip")])
def test_exact_version_metadata_failure_closes_unread_body(store, monkeypatch, field, value):
    api, client, receipt, scope, _blob, _module, error = store
    response = client.get_object(Bucket=receipt["bucket"], Key=receipt["key"], VersionId=receipt["version_id"],
                                 ExpectedBucketOwner="123456789012", ChecksumMode="ENABLED")
    body = response["Body"]._raw_stream
    response[field] = value
    monkeypatch.setattr(client, "get_object", lambda **kwargs: response)
    with pytest.raises(error, match="verification_failed"):
        api.source(receipt, scope)
    assert body.closed


@pytest.mark.parametrize("mode", ["slow", "late_eof", "oversize", "protocol_error"])
def test_raw_storage_stream_deadline_size_and_protocol_errors_are_bounded(store, monkeypatch, mode):
    api, client, receipt, scope, blob, module, error = store
    elapsed, reads = [0], []
    class Raw(io.BytesIO):
        def read1(self, amount):
            reads.append(amount)
            if mode == "protocol_error":
                raise ProtocolError("isolated stream reset")
            elapsed[0] += 16 if mode == "slow" else 0
            result = super().read1(1 if mode == "slow" else amount)
            if not result and mode == "late_eof":
                elapsed[0] = 46
            return result
    raw = Raw(blob + (b"extra" if mode == "oversize" else b""))
    get = client.get_object
    def response(**kwargs):
        value = get(**kwargs)
        value["Body"].close()
        value["Body"] = raw
        return value
    monkeypatch.setattr(client, "get_object", response)
    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    with pytest.raises(error, match="read_failed" if mode == "protocol_error" else "transfer_limit"):
        api.source(receipt, scope)
    assert raw.closed and len(reads) <= 3


def test_qa_object_scope_and_immutable_conflict(store):
    api, client, _receipt, scope, _blob, _module, error = store
    identity = dict(qa_id=str(uuid4()), input_digest="d" * 64, object_id="report.json")
    receipt = api.persist_qa(b"{}", scope, **identity)
    assert api.qa(receipt, scope, **identity) == b"{}"
    puts = len([value for name, value in client.calls if name == "put"])
    with pytest.raises(error, match="verification_failed"):
        api.persist_qa(b'{"changed":true}', scope, **identity)
    assert len([value for name, value in client.calls if name == "put"]) == puts
    for changed in ({"object_id": "../report.json"}, {"object_id": "index.html"}, {"qa_id": str(uuid4())}):
        with pytest.raises(error):
            api.qa(receipt, scope, **{**identity, **changed})


def test_png_rejects_crc_trailing_data_invalid_filter_and_decompression_bomb():
    with service_modules("company_os"):
        from app.builder.qa_validation import png as validate
        from app.builder.model_proxy import BuildModelError
        dimensions = VIEWPORTS["mobile"]
        blob = png(dimensions)
        validate(blob, viewport=dimensions)
        def chunk(kind, body):
            return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body) & 0xffffffff)
        size = dimensions["height"] * (1 + dimensions["width"] * 3)
        for invalid in (blob + b"injected", blob[:-1] + b"x", blob[:33] + chunk(b"IDAT", zlib.compress(b"\x05" + b"\0" * (size - 1))) + blob[-12:],
                        blob[:33] + chunk(b"IDAT", zlib.compress(b"\0" * (size + 100))) + blob[-12:]):
            with pytest.raises(BuildModelError, match="qa_png_invalid"):
                validate(invalid, viewport=dimensions)


def test_combined_bundle_cannot_exceed_approved_included_overhead_limit():
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    report, _evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    with service_modules("company_os"):
        from app.builder.qa_validation import validate
        from app.builder.model_proxy import BuildModelError
        with pytest.raises(BuildModelError, match="qa_evidence_set_invalid"):
            validate(report, request=request, build_id=build_id, qa_id=qa_id, generation=1, policy=POLICY,
                     evidence={name: b"x" * (5 * 1024 * 1024) for name in ("desktop-0.png", "mobile-0.png")})


@pytest.mark.parametrize(("changes", "code"), [
    ({"browser_version": "different-browser"}, "qa_report_browser_version_mismatch"),
    ({"browser_sandbox_verified": False, "outcome": "failed"}, "qa_report_browser_sandbox_unverified"),
])
def test_qa_binding_failures_have_safe_actionable_codes(changes, code):
    """Operators can diagnose a rejected report without logging its contents."""
    from core.artifact_snapshot import canonical
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    report, evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    document = json.loads(report)
    document.update(changes)
    with service_modules("company_os"):
        from app.builder.qa_validation import validate
        from app.builder.model_proxy import BuildModelError
        with pytest.raises(BuildModelError, match=code):
            validate(canonical(document), request=request, build_id=build_id, qa_id=qa_id, generation=1,
                     policy=POLICY, evidence=evidence)


@pytest.mark.parametrize("mode", ["valid", "missing", "failed", "foreign_target"])
def test_landing_image_cases_are_required_bound_and_preserve_failure(mode):
    from core.artifact_snapshot import canonical
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    blob, evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    document = json.loads(blob)
    image_case = next(case for case in document["cases"] if case["id"] == "desktop-0:images")
    if mode == "missing":
        document["cases"].remove(image_case)
        document["coverage"]["observed_cases"] -= 1
    elif mode == "failed":
        image_case["status"] = "failed"
        document["outcome"] = "failed"
    elif mode == "foreign_target":
        image_case["target"] = "other.html"
    with service_modules("company_os"):
        from app.builder.qa_validation import validate
        from app.builder.model_proxy import BuildModelError
        args = dict(request=request, build_id=build_id, qa_id=qa_id, generation=1, policy=POLICY, evidence=evidence)
        if mode in {"missing", "foreign_target"}:
            with pytest.raises(BuildModelError, match="qa_report_cases_missing"):
                validate(canonical(document), **args)
        else:
            result, counts = validate(canonical(document), **args)
            assert result.outcome == ("failed" if mode == "failed" else "passed")
            assert counts["failed"] == (1 if mode == "failed" else 0)


@pytest.mark.parametrize("mode", ["missing", "failed"])
def test_public_copy_case_is_required_and_blocks_qa_pass(mode):
    from core.artifact_snapshot import canonical
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    blob, evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    document = json.loads(blob)
    case = next(case for case in document["cases"] if case["id"] == "desktop-0:public_copy")
    if mode == "missing":
        document["cases"].remove(case)
        document["coverage"]["observed_cases"] -= 1
    else:
        case["status"] = "failed"
        document["outcome"] = "failed"
    with service_modules("company_os"):
        from app.builder.qa_validation import validate
        from app.builder.model_proxy import BuildModelError
        args = dict(request=request, build_id=build_id, qa_id=qa_id, generation=1, policy=POLICY, evidence=evidence)
        if mode == "missing":
            with pytest.raises(BuildModelError, match="qa_report_cases_missing"):
                validate(canonical(document), **args)
        else:
            result, counts = validate(canonical(document), **args)
            assert result.outcome == "failed" and counts["failed"] == 1


@pytest.mark.parametrize("case_id", ["desktop-0:analytics_referrer", "desktop-0:analytics_conversion", "mobile-0:empty_footer"])
def test_landing_analytics_and_footer_cases_are_required(case_id):
    from core.artifact_snapshot import canonical
    from core.static_qa_contracts import Input
    request = Input(command_id=str(uuid4()), source_artifact_id="synthetic", source_sha256="a" * 64, size_bytes=100,
                    manifest=[{"path": "index.html", "size_bytes": 1, "sha256": "b" * 64}], contract=CONTRACT)
    qa_id, build_id = str(uuid4()), "synthetic-build"
    blob, evidence = report_fixture({"id": qa_id, "input": request.model_dump()}, build_id)
    document = json.loads(blob)
    document["cases"] = [case for case in document["cases"] if case["id"] != case_id]
    document["coverage"]["observed_cases"] -= 1
    with service_modules("company_os"):
        from app.builder.qa_validation import validate
        from app.builder.model_proxy import BuildModelError
        with pytest.raises(BuildModelError, match="qa_report_cases_missing"):
            validate(canonical(document), request=request, build_id=build_id, qa_id=qa_id,
                     generation=1, policy=POLICY, evidence=evidence)
