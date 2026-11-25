"""Trusted Core QA input/provenance/evidence receipts. No launch or finalization.

Mutation helpers are controller-only Python seams, not founder/API endpoints.
The later cloud adapter must supply observations from its attested task, never
pass report self-claims as TaskBinding. Account→run→pool→QA is the lock order.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
from pathlib import Path, PurePosixPath
import re
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from pydantic import Field, model_validator
from sqlalchemy import select

from core.artifact_snapshot import import_source, manifest_digest, validate_manifest
from core.application_source import validate_product_manifest
from core.static_qa_contracts import Contract, Input, STATIC_MEDIA, Strict
from app.credits._txn import naive_utc
from app.db.supymem_models import BuildArtifact, BuildRun, QaFinding, QaRun
from app.state import db
from .artifact_store import S3ArtifactStore
from .execution import _app, _event
from .model_proxy import BuildModelError, _authority, _locked, digest
from .qa_validation import validate


class Policy(Strict):
    image_digest: str = Field(pattern=r"^sha256:[a-f0-9]{64}$")
    browser_version: str = Field(pattern=r"^153\.0$")
    cluster_arn: str = Field(pattern=r"^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:cluster/[A-Za-z0-9_-]+$")
    task_definition_arn: str = Field(pattern=r"^arn:aws:ecs:[a-z0-9-]+:[0-9]{12}:task-definition/[A-Za-z0-9_-]+:[1-9][0-9]*$")
    subnet_ids: list[str] = Field(min_length=1, max_length=3)
    security_group_ids: list[str] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def scope(self):
        if self.cluster_arn.split(":")[:5] != self.task_definition_arn.split(":")[:5]:
            raise ValueError("qa_policy_region_account_mismatch")
        for values, pattern in ((self.subnet_ids, r"subnet-[a-f0-9]{8,17}"), (self.security_group_ids, r"sg-[a-f0-9]{8,17}")):
            if len(set(values)) != len(values) or any(not re.fullmatch(pattern, value) for value in values):
                raise ValueError("qa_policy_network_invalid")
        return self


class TaskBinding(Strict):
    task_arn: str
    task_definition_arn: str
    cluster_arn: str
    image_digest: str
    eni_id: str = Field(pattern=r"^eni-[a-f0-9]{8,17}$")
    private_ip: str
    subnet_id: str
    security_group_ids: list[str] = Field(min_length=1, max_length=3)
    observed_at: str


def _fence(ctx, generation, controller_id, attempt):
    spec = json.loads(ctx.run.spec or "{}")
    expected = spec.get("controller_fence", {})
    if (generation, controller_id, attempt) != (spec.get("generation"), expected.get("id"), expected.get("attempt")):
        raise BuildModelError("qa_controller_fence_lost")
    if ctx.run.runtime_version != 1 or ctx.run.closed_at:
        raise BuildModelError("qa_build_not_open")
    return spec


def _context(session, run_id, generation, controller_id, attempt, *, active=False):
    ctx = _locked(session, run_id)
    spec = _fence(ctx, generation, controller_id, attempt)
    if active:
        _authority(session, ctx)
        if (ctx.run.state not in {"running", "syncing_artifacts"} or ctx.account.status != "active" or ctx.pool.status != "active"
                or naive_utc(ctx.pool.expires_at) <= naive_utc()):
            raise BuildModelError("qa_authority_unavailable")
    return ctx, spec


def _qa(session, qa_id, fence, *, active=False):
    run_id = session.scalar(select(QaRun.build_run_id).where(QaRun.id == qa_id, QaRun.runtime_version == 1))
    if not run_id:
        raise BuildModelError("qa_receipt_not_found")
    ctx, spec = _context(session, run_id, **fence, active=active)
    row = session.scalar(select(QaRun).where(QaRun.id == qa_id).with_for_update().execution_options(populate_existing=True))
    if row.generation != fence["generation"] or (row.workspace_id, row.venture_id, row.app_id) != (ctx.run.workspace_id, ctx.run.venture_id, ctx.run.app_id):
        raise BuildModelError("qa_receipt_scope_invalid")
    receipt = json.loads(row.receipt_json)
    if receipt.get("scope") != _row_scope(row):
        raise BuildModelError("qa_receipt_scope_invalid")
    return ctx, spec, row, receipt


def _row_scope(row):
    return {"workspace_id": row.workspace_id, "venture_id": row.venture_id, "app_id": row.app_id,
            "build_id": row.build_run_id, "generation": row.generation}


def _source(session, ctx, spec):
    capture = spec.get("artifact_capture", {})
    row = session.get(BuildArtifact, ctx.run.artifact_id) if ctx.run.artifact_id else None
    if (not row or spec.get("artifact_verified") is not True or capture.get("state") != "stored"
            or capture.get("artifact_id") != row.id or row.kind != "source_zip"
            or (row.build_run_id, row.workspace_id, row.app_id) != (ctx.run.id, ctx.run.workspace_id, ctx.run.app_id)
            or (row.sha256, row.size_bytes, row.s3_key) != (capture.get("sha256"), capture.get("size_bytes"), capture.get("key"))):
        raise BuildModelError("qa_source_unverified")
    validate_manifest(capture.get("manifest"))
    if manifest_digest(capture["manifest"]) != capture.get("manifest_digest"):
        raise BuildModelError("qa_source_manifest_invalid")
    scope = {"workspace_id": ctx.run.workspace_id, "venture_id": ctx.run.venture_id, "app_id": ctx.run.app_id,
             "build_id": ctx.run.id, "generation": spec["generation"]}
    return row, capture, scope


def prepare(run_id, *, contract, policy, generation, controller_id, attempt):
    """Called with approved server-owned contract/policy; not exposed to founders."""
    contract, policy = Contract.model_validate(contract), Policy.model_validate(policy)
    fence = dict(generation=generation, controller_id=controller_id, attempt=attempt)
    with db()() as session:
        ctx, spec = _context(session, run_id, **fence, active=True)
        artifact, capture, scope = _source(session, ctx, spec)
        identity = digest({"scope": scope, "source": capture, "contract": contract.model_dump(), "policy": policy.model_dump()})
        qa_id = str(uuid5(NAMESPACE_URL, "supymem-qa:" + identity))
        command_id = str(uuid5(NAMESPACE_URL, "supymem-qa-command:" + qa_id))
        request = Input(command_id=command_id, source_artifact_id=artifact.id, source_sha256=artifact.sha256,
                        size_bytes=artifact.size_bytes, manifest=capture["manifest"], contract=contract)
        row = session.get(QaRun, qa_id)
        if row:
            receipt = json.loads(row.receipt_json)
            if receipt.get("source_verified") is True:
                return {"id": qa_id, "input": receipt["input"], "input_digest": row.input_digest}
        else:
            row = QaRun(id=qa_id, app_id=ctx.run.app_id, build_run_id=run_id, managed_build_id=run_id,
                        workspace_id=ctx.run.workspace_id, venture_id=ctx.run.venture_id, runtime_version=1, generation=generation,
                        source_artifact_id=artifact.id, source_sha256=artifact.sha256, source_manifest_digest=request.source_manifest_digest,
                        contract_digest=contract.digest, input_digest=request.digest, state="queued", created_at=naive_utc(),
                        receipt_json=json.dumps({"input": request.model_dump(mode="json"), "source": capture, "scope": scope,
                                                 "policy": policy.model_dump(), "phase": "verifying_source"}, sort_keys=True))
            session.add(row)
        session.commit()
    store = S3ArtifactStore.configured()
    blob = store.source(capture, scope)
    with TemporaryDirectory(prefix="supymem-qa-source-") as temporary:
        import_source(blob, Path(temporary).resolve() / "source", manifest=request.manifest,
                      sha256=request.source_sha256, size_bytes=request.size_bytes)
        if contract.profile == "founder_product_v1":
            try:
                validate_product_manifest(request.manifest)
            except Exception as exc:
                raise BuildModelError("qa_product_source_unsupported") from exc
            pages = [item["path"].removeprefix("product/static/") for item in request.manifest
                     if item["path"].startswith("product/static/") and item["path"].endswith(".html")]
            if "index.html" not in pages or len(pages) > 10:
                raise BuildModelError("qa_product_source_unsupported")
        else:
            pages = [item["path"] for item in request.manifest if item["path"].endswith(".html")]
            if ("index.html" not in pages or len(pages) > 10
                    or any(PurePosixPath(item["path"]).suffix.lower() not in STATIC_MEDIA for item in request.manifest)):
                raise BuildModelError("qa_static_source_unsupported")
    with db()() as session:
        ctx, spec, row, receipt = _qa(session, qa_id, fence, active=True)
        if _source(session, ctx, spec)[1] != receipt["source"]:
            raise BuildModelError("qa_source_changed")
        if not receipt.get("source_verified"):
            receipt.update(source_verified=True, phase="prepared")
            row.receipt_json = json.dumps(receipt, sort_keys=True)
            _event(session, ctx.run, "qa_source_prepared", {"qa_id": qa_id, "source_sha256": row.source_sha256, "input_digest": row.input_digest})
            session.commit()
    return {"id": qa_id, "input": request.model_dump(mode="json"), "input_digest": request.digest}


def bind_task(qa_id, *, observation, generation, controller_id, attempt):
    """Trusted cloud-observer seam. Report fields can never call or populate it."""
    binding = TaskBinding.model_validate(observation)
    fence = dict(generation=generation, controller_id=controller_id, attempt=attempt)
    with db()() as session:
        ctx, _spec, row, receipt = _qa(session, qa_id, fence, active=True)
        policy = Policy.model_validate(receipt["policy"])
        prefix = policy.cluster_arn.replace(":cluster/", ":task/") + "/"
        ip = ipaddress.ip_address(binding.private_ip)
        observed = datetime.fromisoformat(binding.observed_at)
        if (not receipt.get("source_verified") or binding.image_digest != policy.image_digest
                or binding.cluster_arn != policy.cluster_arn or binding.task_definition_arn != policy.task_definition_arn
                or not binding.task_arn.startswith(prefix) or not re.fullmatch(r"[a-f0-9]{32}", binding.task_arn[len(prefix):])
                or binding.subnet_id not in policy.subnet_ids or sorted(binding.security_group_ids) != sorted(policy.security_group_ids)
                or ip.version != 4 or not ip.is_private or ip.is_loopback or ip.is_link_local
                or observed.tzinfo is None or not datetime.now(timezone.utc) - timedelta(minutes=15) <= observed <= datetime.now(timezone.utc) + timedelta(seconds=60)):
            raise BuildModelError("qa_task_observation_invalid")
        value = binding.model_dump()
        prior = receipt.get("trusted_task")
        if prior and prior != value:
            raise BuildModelError("qa_task_binding_conflict")
        if prior:
            return {"id": row.id, "task_binding_digest": receipt["task_binding_digest"]}
        receipt.update(trusted_task=value, task_binding_digest=digest(value), phase="awaiting_report")
        row.state, row.receipt_json = "running", json.dumps(receipt, sort_keys=True)
        session.commit()
        return {"id": row.id, "task_binding_digest": receipt["task_binding_digest"]}


def capture(qa_id, *, report_blob, evidence, task_binding_digest, generation, controller_id, attempt):
    """The attested task's exact bytes, never uploaded through a founder route."""
    fence = dict(generation=generation, controller_id=controller_id, attempt=attempt)
    with db()() as session:
        _ctx, _spec, row, receipt = _qa(session, qa_id, fence)
        if not receipt.get("trusted_task") or receipt.get("task_binding_digest") != task_binding_digest:
            raise BuildModelError("qa_task_binding_required")
        request = Input.model_validate(receipt["input"])
        report, counts = validate(report_blob, request=request, build_id=row.build_run_id, qa_id=qa_id, generation=generation,
                                  policy=receipt["policy"], evidence=evidence)
        report_digest = hashlib.sha256(report_blob).hexdigest()
        objects = {**{key: hashlib.sha256(blob).hexdigest() for key, blob in evidence.items()}, "report.json": report_digest}
        upload = {"task_binding_digest": task_binding_digest, "input_digest": row.input_digest, "objects": objects}
        prior = receipt.get("upload_intent")
        if prior and prior != upload:
            raise BuildModelError("qa_report_conflict")
        if receipt.get("phase") == "stored":
            return serialize(row)
        receipt.update(phase="uploading", upload_intent=upload)
        row.receipt_json = json.dumps(receipt, sort_keys=True)
        session.commit()  # upload intent before any S3 mutation
        scope = receipt["scope"]
    store = S3ArtifactStore.configured()
    stored = {}
    for object_id, blob in [*sorted(evidence.items()), ("report.json", report_blob)]:
        with db()() as session:
            _ctx, _spec, _row, current = _qa(session, qa_id, fence)
            if current.get("upload_intent") != upload or current.get("task_binding_digest") != task_binding_digest:
                raise BuildModelError("qa_capture_context_changed")
        stored[object_id] = store.persist_qa(blob, scope, qa_id=qa_id, input_digest=request.digest, object_id=object_id)
    with db()() as session:
        ctx, _spec, row, receipt = _qa(session, qa_id, fence)
        if receipt.get("upload_intent") != upload or receipt.get("task_binding_digest") != task_binding_digest:
            raise BuildModelError("qa_capture_context_changed")
        if receipt.get("phase") == "stored":
            return serialize(row)
        receipt.update(phase="stored", objects=stored, outcome=report.outcome, verified_report=True,
            report_complete=report.coverage.get("complete") is True)
        row.report_digest, row.report_s3_key = report_digest, stored["report.json"]["key"]
        row.passed, row.failed, row.unsupported = counts["passed"], counts["failed"], counts["unsupported"]
        row.suites = json.dumps(sorted({case.suite for case in report.cases}))
        row.state, row.finished_at, row.receipt_json = "report_ready", naive_utc(), json.dumps(receipt, sort_keys=True)
        for case in report.cases:
            if case.status == "passed":
                continue
            finding_id = "qaf_" + hashlib.sha256((qa_id + ":" + case.id).encode()).hexdigest()[:32]
            session.add(QaFinding(id=finding_id, qa_run_id=qa_id, workspace_id=row.workspace_id,
                                 severity="blocking", kind=case.suite, title=case.id,
                                 detail=json.dumps({"status": case.status, "observed": case.observed, "expected": case.expected})))
        _event(session, ctx.run, "qa_report_stored", {"qa_id": qa_id, "report_digest": report_digest, "outcome": report.outcome,
                                                   "passed": row.passed, "failed": row.failed, "unsupported": row.unsupported})
        session.commit()
        return serialize(row)


def serialize(row):
    try:
        receipt = json.loads(row.receipt_json or "{}")
        if not isinstance(receipt, dict):
            raise ValueError("qa_receipt_invalid")
        request = Input.model_validate(receipt["input"]) if row.runtime_version == 1 else None
        bound = bool(request and request.digest == row.input_digest and request.source_sha256 == row.source_sha256
                     and request.source_artifact_id == row.source_artifact_id and request.source_manifest_digest == row.source_manifest_digest
                     and request.contract.digest == row.contract_digest)
    except (ValueError, KeyError, TypeError):
        receipt, bound = {}, False
    verified = bool(row.runtime_version == 1 and receipt.get("verified_report") is True and receipt.get("phase") == "stored"
                    and bound and receipt.get("scope") == _row_scope(row)
                    and receipt.get("trusted_task") and receipt.get("task_binding_digest") == digest(receipt["trusted_task"])
                    and receipt.get("objects", {}).get("report.json", {}).get("sha256") == row.report_digest)
    return {"id": row.id, "runtime_version": row.runtime_version, "state": row.state if row.runtime_version == 1 else "not_run",
            "verified_report": verified, "verified_source_import": bool(verified and row.state == "passed"
                and receipt.get("completion") == {"build_run_id": row.build_run_id, "source_artifact_id": row.source_artifact_id,
                    "report_digest": row.report_digest, "verified_source_import": True,
                    "verified_execution": False, "generation": row.generation}), "verified_execution": bool(verified and row.state == "passed"
                and receipt.get("completion") == {"build_run_id": row.build_run_id, "source_artifact_id": row.source_artifact_id,
                    "report_digest": row.report_digest, "verified_execution": True, "generation": row.generation}), "outcome": receipt.get("outcome") if verified else None,
            "passed": row.passed if verified else None, "failed": row.failed if verified else None,
            "unsupported": row.unsupported if verified else None, "source_artifact_id": row.source_artifact_id,
            "source_sha256": row.source_sha256, "report_digest": row.report_digest if verified else None}


def read(qa_id, *, run_id, actor, object_id=None):
    with db()() as session:
        run = session.get(BuildRun, run_id)
        if not run:
            raise KeyError("build")
        _app(session, run.app_id, actor)
        row = session.get(QaRun, qa_id)
        if not row or (row.build_run_id, row.app_id, row.workspace_id, row.venture_id) != (run.id, run.app_id, run.workspace_id, run.venture_id):
            raise KeyError("qa")
        view = serialize(row)
        if object_id is None:
            return view
        if not view["verified_report"]:
            raise BuildModelError("qa_report_not_verified")
        receipt = json.loads(row.receipt_json)
        stored = receipt.get("objects", {}).get(object_id)
        if not stored:
            raise KeyError("qa_object")
        scope, input_digest = _row_scope(row), row.input_digest
    blob = S3ArtifactStore.configured().qa(stored, scope, qa_id=qa_id, input_digest=input_digest, object_id=object_id)
    return blob, stored["sha256"], "application/json" if object_id == "report.json" else "image/png"
