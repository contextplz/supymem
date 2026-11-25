"""Publish one accepted, QA-bound static artifact to Supymem hosting.

The database intent is committed before any provider effect. Public objects are
written below an immutable deployment prefix and the hostname is switched only
after every object has been verified. A retry therefore either resumes the same
intent or returns the already-live receipt.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import re
import ssl
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from core.artifact_policy import read_file
from core.artifact_snapshot import import_source
from core.hosting_contracts import (
    HostingRejected,
    check_mapping_capacity,
    host_mapping,
    public_hostname,
    public_manifest,
    validate_mapping,
)
from core.ids import new_id
from core.public_copy import public_copy_observation, public_html_text
from core.static_qa_contracts import Contract
from sqlalchemy import and_, func, or_, select

from app.audit.writer import audit
from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from app.credits._txn import naive_utc
from app.db.session import serialize_writes
from app.db.supymem_models import (
    AppDomain,
    BuildArtifact,
    BuildRun,
    Deployment,
    FounderApp,
    QaRun,
)
from app.state import db


class HostingError(RuntimeError):
    def __init__(self, code: str, *, status: int = 409):
        super().__init__(code)
        self.code, self.status = code, status


@dataclass(frozen=True)
class HostingConfig:
    bucket: str
    region: str
    expected_owner: str
    kvs_arn: str
    public_domain: str

    @classmethod
    def from_environment(cls) -> HostingConfig:
        from app.core.local_mode import is_local
        if is_local():
            raise HostingError("Open Website to publish with your saved account. Add it in Settings > Keys and connections > Publishing.")
        try:
            value = cls(
                bucket=os.environ["PUBLIC_SITE_S3_BUCKET"],
                region=os.environ["PUBLIC_SITE_AWS_REGION"],
                expected_owner=os.environ["PUBLIC_SITE_S3_EXPECTED_OWNER"],
                kvs_arn=os.environ["PUBLIC_SITE_KVS_ARN"],
                public_domain=os.environ.get("PUBLIC_SITE_DOMAIN", "supymem.com"),
            )
        except KeyError as exc:
            raise HostingError("hosting_provider_unavailable", status=503) from exc
        if (
            not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value.bucket)
            or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", value.region)
            or not re.fullmatch(r"\d{12}", value.expected_owner)
        ):
            raise HostingError("hosting_provider_configuration_invalid", status=503)
        if not re.fullmatch(
            rf"arn:aws:cloudfront::{re.escape(value.expected_owner)}:key-value-store/[A-Za-z0-9_-]+",
            value.kvs_arn,
        ):
            raise HostingError("hosting_provider_configuration_invalid", status=503)
        try:
            public_hostname("valid-host", value.public_domain)
        except HostingRejected as exc:
            raise HostingError(str(exc), status=503) from exc
        return value


def _json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _accepted_source(session, app: FounderApp, build_id: str, *, historical=False):
    run = session.get(BuildRun, build_id)
    if not run or (run.app_id, run.workspace_id, run.venture_id) != (
        app.id,
        app.workspace_id,
        app.venture_id,
    ):
        raise HostingError("hosting_build_not_found", status=404)
    spec = _json(run.spec)
    review = spec.get("lifecycle", {}).get("review", {})
    completion = spec.get("lifecycle", {}).get("completion", {})
    source_import = spec.get("lifecycle_version") == 3
    source_ready = False
    if source_import:
        from app.build.subscription_static_controller import verified_source_qa, verified_historical_source_qa
        source_ready = (verified_historical_source_qa if historical else verified_source_qa)(session, run, spec)
    if (
        run.runtime_version != 1
        or run.state != "succeeded"
        or not run.closed_at
        or (not source_ready if source_import else spec.get("execution_verified") is not True)
        or review.get("status") != "accepted"
        or review.get("decision") != "accept"
        or review.get("source_artifact_id") != run.artifact_id
        or completion.get("source_artifact_id") != run.artifact_id
        or (completion.get("verified_source_import") is not True if source_import
            else completion.get("verified_execution") is not True)
        or completion.get("verified_report") is not True
    ):
        raise HostingError("hosting_build_not_accepted")
    artifact = session.get(BuildArtifact, run.artifact_id)
    qa = session.get(QaRun, completion.get("qa_id"))
    capture = spec.get("artifact_capture", {})
    if (
        not artifact
        or artifact.kind != "source_zip"
        or (artifact.build_run_id, artifact.app_id, artifact.workspace_id)
        != (run.id, app.id, app.workspace_id)
        or capture.get("state") != "stored"
        or capture.get("artifact_id") != artifact.id
        or capture.get("sha256") != artifact.sha256
        or capture.get("size_bytes") != artifact.size_bytes
        or not qa
        or qa.state != "passed"
        or qa.source_artifact_id != artifact.id
        or qa.report_digest != review.get("report_digest")
        or qa.failed != 0
        or qa.unsupported != 0
        or qa.passed <= 0
    ):
        raise HostingError("hosting_source_receipt_invalid")
    try:
        manifest = public_manifest(capture.get("manifest"))
    except (HostingRejected, ValueError, TypeError) as exc:
        raise HostingError(str(exc)) from exc
    if manifest["source_manifest_digest"] != capture.get("manifest_digest"):
        raise HostingError("hosting_source_manifest_changed")
    return (
        run,
        artifact,
        qa,
        {**capture, "generation": spec.get("generation")},
        manifest,
    )


def _source_scope(run: BuildRun, capture: dict[str, Any]) -> dict[str, Any]:
    return {
        "workspace_id": run.workspace_id,
        "venture_id": run.venture_id,
        "app_id": run.app_id,
        "build_id": run.id,
        "generation": int(capture["generation"]),
    }


def _conversion_proof(session, app, run, qa):
    """Bind a real conversion receipt to the exact independently tested contract."""
    from app.conversion import waitlist
    try:
        contract = Contract.model_validate(_json(run.spec)["inputs"]["task"]["landing_expectations"]["contract"])
        if (contract.digest != qa.contract_digest
                or (qa.app_id, qa.workspace_id, qa.venture_id, qa.build_run_id, qa.source_sha256)
                != (app.id, app.workspace_id, app.venture_id, run.id, session.get(BuildArtifact, run.artifact_id).sha256)):
            raise ValueError("contract_changed")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise HostingError("hosting_conversion_contract_unverified") from exc
    url = waitlist.public_url(app.venture_id)
    if not any(action.kind == "external_link" and action.page == "index.html" and action.target == url for action in contract.actions):
        raise HostingError("hosting_conversion_link_required")
    try:
        proof = waitlist.verified_receipt(session, app.venture_id, app.workspace_id)
    except waitlist.WaitlistError as exc:
        raise HostingError("hosting_conversion_verification_required") from exc
    return {**proof, "build_id": run.id, "artifact_id": run.artifact_id, "contract_digest": contract.digest}


def verified_public_files(
    blob: bytes, capture: dict[str, Any], manifest: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return bytes only after the stored archive and every file re-verify."""
    with tempfile.TemporaryDirectory(prefix="supymem-publish-") as raw:
        # macOS exposes /var as a symlink to /private/var.  artifact_policy
        # deliberately rejects symlinked directory components, so canonicalize
        # the OS-created temporary directory before importing untrusted source.
        temporary_parent = Path(raw).resolve(strict=True)
        root = temporary_parent / "source"
        import_source(
            blob,
            root,
            manifest=capture["manifest"],
            sha256=capture["sha256"],
            size_bytes=capture["size_bytes"],
        )
        files = []
        for item in manifest["files"]:
            body = read_file(root, item["path"])
            if (
                len(body) != item["size_bytes"]
                or hashlib.sha256(body).hexdigest() != item["sha256"]
            ):
                raise HostingError("hosting_public_file_changed")
            if item["path"].endswith(".html"):
                try:
                    findings, _ = public_copy_observation(public_html_text(body.decode("utf-8")))
                except UnicodeDecodeError as exc:
                    raise HostingError("hosting_public_copy_unreadable") from exc
                if findings:
                    raise HostingError("hosting_public_copy_unreviewed")
            files.append({**item, "body": body})
        return files


class AwsStaticPublisher:
    def __init__(self, config: HostingConfig, s3, kvs):
        self.config, self.s3, self.kvs = config, s3, kvs

    @classmethod
    def configured(cls) -> AwsStaticPublisher:
        import boto3
        from botocore.config import Config

        config = HostingConfig.from_environment()
        client_config = Config(
            connect_timeout=5,
            read_timeout=30,
            retries={"total_max_attempts": 2},
            ignore_configured_endpoint_urls=True,
            proxies={},
        )
        return cls(
            config,
            boto3.client("s3", region_name=config.region, config=client_config),
            boto3.client(
                "cloudfront-keyvaluestore",
                region_name="us-east-1",
                config=client_config,
            ),
        )

    def put_files(
        self,
        *,
        app_id: str,
        deployment_id: str,
        artifact_id: str,
        files: list[dict[str, Any]],
    ) -> None:
        from botocore.exceptions import ClientError

        prefix = f"published/{app_id}/{deployment_id}/"
        for item in files:
            key = prefix + item["path"]
            sha = item["sha256"]
            checksum = base64.b64encode(bytes.fromhex(sha)).decode()
            common = {
                "Bucket": self.config.bucket,
                "Key": key,
                "ExpectedBucketOwner": self.config.expected_owner,
            }
            try:
                self.s3.put_object(
                    **common,
                    Body=item["body"],
                    ContentLength=len(item["body"]),
                    ContentType=item["content_type"],
                    CacheControl=(
                        "public,max-age=0,must-revalidate"
                        if item["path"].endswith(".html")
                        else "public,max-age=31536000,immutable"
                    ),
                    ChecksumSHA256=checksum,
                    Metadata={
                        "sha256": sha,
                        "deployment_id": deployment_id,
                        "artifact_id": artifact_id,
                    },
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                )
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) not in {
                    "PreconditionFailed",
                    "ConditionalRequestConflict",
                }:
                    raise HostingError(
                        "hosting_public_write_failed", status=503
                    ) from exc
            head = self.s3.head_object(**common, ChecksumMode="ENABLED")
            if (
                head.get("ContentLength") != len(item["body"])
                or head.get("ChecksumSHA256") != checksum
                or head.get("ContentType") != item["content_type"]
                or head.get("Metadata")
                != {
                    "sha256": sha,
                    "deployment_id": deployment_id,
                    "artifact_id": artifact_id,
                }
                or head.get("ServerSideEncryption") != "AES256"
            ):
                raise HostingError("hosting_public_verification_failed", status=503)

    def read_mapping(self, hostname: str) -> dict | None:
        from botocore.exceptions import ClientError

        try:
            response = self.kvs.get_key(KvsARN=self.config.kvs_arn, Key=hostname)
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"ResourceNotFoundException", "ResourceNotFound"}:
                return None
            raise HostingError("hosting_mapping_read_failed", status=503) from exc
        except Exception as exc:
            raise HostingError("hosting_mapping_read_failed", status=503) from exc
        try:
            return validate_mapping(response["Value"])
        except (HostingRejected, KeyError, TypeError) as exc:
            raise HostingError("hosting_mapping_invalid", status=503) from exc

    def switch(self, hostname: str, mapping: dict[str, Any], *,
               expected_previous_deployment_id: str | None = None,
               expected_absent: bool = False,
               expected_previous_mapping: dict[str, Any] | None = None) -> str | None:
        from botocore.exceptions import ClientError
        from core.artifact_snapshot import canonical

        frozen_previous = None
        if expected_previous_mapping is not None:
            try:
                frozen_previous = canonical(expected_previous_mapping).decode()
                previous = validate_mapping(frozen_previous)
                if (expected_absent or previous["deployment"] != expected_previous_deployment_id
                        or previous["app"] != mapping.get("app")):
                    raise HostingRejected("hosting_mapping_invalid")
            except HostingRejected as exc:
                raise HostingError("subscription_static_publication_baseline_changed") from exc
        desired = canonical(mapping).decode()
        validate_mapping(desired)
        for _attempt in range(3):
            observed = self.kvs.describe_key_value_store(KvsARN=self.config.kvs_arn)
            total = int(observed.get("TotalSizeInBytes", 0))
            old = ""
            try:
                old = self.kvs.get_key(KvsARN=self.config.kvs_arn, Key=hostname).get(
                    "Value", ""
                )
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) not in {
                    "ResourceNotFoundException",
                    "ResourceNotFound",
                }:
                    raise HostingError(
                        "hosting_mapping_read_failed", status=503
                    ) from exc
            if old and (expected_absent or frozen_previous is not None):
                try:
                    observed_mapping = validate_mapping(old)
                except HostingRejected as exc:
                    raise HostingError("hosting_mapping_invalid", status=503) from exc
                if canonical(observed_mapping).decode() == desired:
                    # A lost write acknowledgement must retain the original
                    # rollback predecessor, not treat our new mapping as it.
                    return frozen_previous
                if frozen_previous is not None and canonical(observed_mapping).decode() != frozen_previous:
                    raise HostingError("subscription_static_publication_baseline_changed")
            if old:
                if expected_absent:
                    raise HostingError("subscription_static_publication_baseline_changed")
                try:
                    previous = validate_mapping(old)
                except HostingRejected as exc:
                    raise HostingError("hosting_mapping_invalid", status=503) from exc
                if expected_previous_deployment_id and previous["deployment"] != expected_previous_deployment_id:
                    raise HostingError("hosting_revision_baseline_changed")
            elif expected_previous_deployment_id:
                raise HostingError("hosting_revision_baseline_changed")
            raw = check_mapping_capacity(
                hostname,
                mapping,
                total_bytes=total,
                replaced_bytes=len(hostname.encode()) + len(old.encode()) if old else 0,
            ).decode()
            try:
                self.kvs.put_key(
                    KvsARN=self.config.kvs_arn,
                    Key=hostname,
                    Value=raw,
                    IfMatch=observed["ETag"],
                )
                return old or None
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in {
                    "PreconditionFailedException",
                    "ConflictException",
                }:
                    continue
                raise HostingError("hosting_mapping_write_failed", status=503) from exc
        raise HostingError("hosting_mapping_conflict", status=503)

    def disable(self, hostname: str, expected: dict[str, Any]) -> None:
        """CAS-disable only the frozen deployment, recognizing a lost ACK."""
        from botocore.exceptions import ClientError
        from core.artifact_snapshot import canonical
        validate_mapping(canonical(expected).decode())
        if expected["enabled"] is not True:
            raise HostingError("hosting_unpublish_baseline_unverified")
        desired = {**expected, "enabled": False}
        for _attempt in range(3):
            observed = self.kvs.describe_key_value_store(KvsARN=self.config.kvs_arn)
            try:
                current = validate_mapping(self.kvs.get_key(KvsARN=self.config.kvs_arn, Key=hostname)["Value"])
            except (ClientError, HostingRejected, KeyError) as exc:
                raise HostingError("hosting_unpublish_mapping_unverified", status=503) from exc
            if current == desired:
                return
            if current != expected:
                raise HostingError("hosting_unpublish_baseline_changed")
            try:
                self.kvs.put_key(KvsARN=self.config.kvs_arn, Key=hostname,
                                 Value=canonical(desired).decode(), IfMatch=observed["ETag"])
                return
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in {"PreconditionFailedException", "ConflictException"}:
                    continue
                raise HostingError("hosting_unpublish_write_uncertain", status=503) from exc
        raise HostingError("hosting_unpublish_mapping_conflict", status=503)

    def verify_disabled_mapping(self, hostname: str, expected: dict[str, Any]) -> None:
        from botocore.exceptions import ClientError
        try:
            current = validate_mapping(self.kvs.get_key(KvsARN=self.config.kvs_arn, Key=hostname)["Value"])
        except (ClientError, HostingRejected, KeyError) as exc:
            raise HostingError("hosting_unpublish_mapping_unverified", status=503) from exc
        if current != {**expected, "enabled": False}:
            raise HostingError("hosting_unpublish_baseline_changed")

    def verify_offline(self, hostname: str, expected: dict[str, Any]) -> None:
        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None
        self.verify_disabled_mapping(hostname, expected)
        opener = build_opener(NoRedirect())
        url = f"https://{hostname}/"
        for delay in (0, 2, 4, 8, 12, 15):
            if delay:
                time.sleep(delay)
            try:
                request = Request(url, headers={"Cache-Control": "no-cache", "User-Agent": "Supymem-Unpublish-Health/1"})
                with opener.open(request, timeout=10):
                    pass
            except HTTPError as exc:
                offline = exc.code == 404 and exc.geturl() == url
                exc.close()
                if offline:
                    self.verify_disabled_mapping(hostname, expected)
                    return
            except (URLError, TimeoutError, OSError, ssl.SSLError):
                pass
        raise HostingError("hosting_unpublish_verification_pending", status=503)

    def restore(
        self,
        hostname: str,
        installed: dict[str, Any],
        previous: str | None,
    ) -> None:
        """Undo only our mapping; never overwrite a later publisher's change."""
        from botocore.exceptions import ClientError
        from core.artifact_snapshot import canonical

        expected = canonical(installed).decode()
        if previous:
            try:
                validate_mapping(previous)
            except HostingRejected as exc:
                raise HostingError("hosting_restore_invalid", status=503) from exc
        for _attempt in range(3):
            observed = self.kvs.describe_key_value_store(KvsARN=self.config.kvs_arn)
            try:
                current = self.kvs.get_key(
                    KvsARN=self.config.kvs_arn, Key=hostname
                ).get("Value", "")
            except ClientError as exc:
                raise HostingError("hosting_restore_read_failed", status=503) from exc
            if current != expected:
                raise HostingError("hosting_restore_conflict", status=503)
            try:
                if previous is None:
                    self.kvs.delete_key(
                        KvsARN=self.config.kvs_arn,
                        Key=hostname,
                        IfMatch=observed["ETag"],
                    )
                else:
                    self.kvs.put_key(
                        KvsARN=self.config.kvs_arn,
                        Key=hostname,
                        Value=previous,
                        IfMatch=observed["ETag"],
                    )
                return
            except ClientError as exc:
                if str(exc.response.get("Error", {}).get("Code")) in {
                    "PreconditionFailedException",
                    "ConflictException",
                }:
                    continue
                raise HostingError("hosting_restore_write_failed", status=503) from exc
        raise HostingError("hosting_restore_conflict", status=503)

    def verify_public(self, hostname: str, expected_sha256: str) -> None:
        """Verify the public TLS route serves the exact accepted entrypoint."""

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, headers, newurl):
                return None

        opener = build_opener(NoRedirect())
        url = f"https://{hostname}/"
        last_error: Exception | None = None
        for delay in (0, 2, 4, 8, 12, 15):
            if delay:
                time.sleep(delay)
            try:
                request = Request(
                    url,
                    headers={
                        "Accept": "text/html",
                        "Accept-Encoding": "identity",
                        "User-Agent": "Supymem-Publication-Health/1",
                    },
                )
                with opener.open(request, timeout=10) as response:
                    length = response.headers.get("Content-Length")
                    if length and (
                        not length.isdecimal() or int(length) > 8 * 1024 * 1024
                    ):
                        raise HostingError("hosting_public_health_invalid", status=503)
                    body = response.read(8 * 1024 * 1024 + 1)
                    if (
                        response.status != 200
                        or response.geturl() != url
                        or len(body) > 8 * 1024 * 1024
                        or hashlib.sha256(body).hexdigest() != expected_sha256
                    ):
                        raise HostingError("hosting_public_health_mismatch", status=503)
                return
            except (HTTPError, URLError, TimeoutError, OSError, ssl.SSLError) as exc:
                last_error = exc
            except HostingError as exc:
                last_error = exc
        raise HostingError("hosting_public_health_failed", status=503) from last_error


def _serialize(row: Deployment) -> dict[str, Any]:
    return {
        "id": row.id,
        "app_id": row.app_id,
        "build_artifact_id": row.build_artifact_id,
        "target": row.target,
        "state": row.state,
        "version": row.version,
        "url": row.url,
        "previous_deployment_id": row.previous_deployment_id,
        "started_at": row.started_at.isoformat() + "Z" if row.started_at else None,
        "live_at": row.live_at.isoformat() + "Z" if row.live_at else None,
        "error": row.error,
    }


def list_deployments(app_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(Deployment)
            .where(Deployment.app_id == app_id)
            .order_by(Deployment.version.desc())
        ).all()
        app = session.get(FounderApp, app_id)
        from app.hosting.unpublish import receipt
        return [{**_serialize(row), "unpublish": receipt(session, app, row.id) if app else None} for row in rows]


def audit_published_copy(app_id: str, deployment_id: str, *, s3=None,
                         config: HostingConfig | None = None) -> dict[str, Any]:
    """Read the live deployment's manifest-bound HTML; never change its source."""
    from botocore.exceptions import ClientError

    with db()() as session:
        deployment = session.get(Deployment, deployment_id)
        if (not deployment or deployment.app_id != app_id or deployment.state != "live"
                or deployment.target != "supymem_static" or not deployment.build_artifact_id):
            raise HostingError("hosting_deployment_not_live", status=404)
        if str(deployment.build_artifact_id).startswith("firstlook:"):
            return _audit_firstlook_copy(app_id, deployment, s3=s3, config=config)
        artifact = session.get(BuildArtifact, deployment.build_artifact_id)
        run = session.get(BuildRun, artifact.build_run_id) if artifact else None
        if (not artifact or not run or artifact.app_id != app_id or run.app_id != app_id
                or artifact.workspace_id != deployment.workspace_id
                or run.workspace_id != deployment.workspace_id
                or run.venture_id != deployment.venture_id):
            raise HostingError("hosting_public_copy_audit_unverified", status=503)
        capture = _json(run.spec).get("artifact_capture", {})
        if (capture.get("artifact_id") != artifact.id or capture.get("sha256") != artifact.sha256
                or capture.get("size_bytes") != artifact.size_bytes):
            raise HostingError("hosting_public_copy_audit_unverified", status=503)
        try:
            manifest = public_manifest(capture.get("manifest"))
        except (HostingRejected, ValueError, TypeError) as exc:
            raise HostingError("hosting_public_copy_audit_unverified", status=503) from exc
        if manifest["source_manifest_digest"] != capture.get("manifest_digest"):
            raise HostingError("hosting_public_copy_audit_unverified", status=503)
        pages = [item for item in manifest["files"] if item["path"].endswith(".html")]

    if s3 is None:
        provider = AwsStaticPublisher.configured()
        s3, config = provider.s3, provider.config
    assert config is not None
    try:
        findings = []
        for item in pages:
            response = s3.get_object(Bucket=config.bucket,
                                     Key=f"published/{app_id}/{deployment_id}/{item['path']}",
                                     ExpectedBucketOwner=config.expected_owner)
            body = response["Body"].read(8 * 1024 * 1024 + 1)
            metadata = response.get("Metadata", {})
            if (len(body) > 8 * 1024 * 1024 or len(body) != item["size_bytes"]
                    or metadata.get("deployment_id") != deployment_id
                    or metadata.get("artifact_id") != artifact.id
                    or metadata.get("sha256") != item["sha256"]
                    or item["sha256"] != hashlib.sha256(body).hexdigest()):
                raise HostingError("hosting_public_copy_audit_unverified", status=503)
            try:
                categories, _ = public_copy_observation(public_html_text(body.decode("utf-8")))
            except UnicodeDecodeError as exc:
                raise HostingError("hosting_public_copy_audit_unreadable", status=503) from exc
            if categories:
                findings.append({"path": item["path"], "categories": categories})
    except HostingError:
        raise
    except (ClientError, KeyError, OSError, ValueError) as exc:
        raise HostingError("hosting_public_copy_audit_unavailable", status=503) from exc
    return {"deployment_id": deployment_id, "state": "needs_review" if findings else "clear",
            "findings": findings}


def _audit_firstlook_copy(app_id: str, deployment: Deployment, *, s3=None, config: HostingConfig | None = None) -> dict[str, Any]:
    """Public-copy audit of a live firstlook page: its single index.html, read back from the published prefix."""
    from botocore.exceptions import ClientError

    deployment_id = deployment.id
    if s3 is None:
        provider = AwsStaticPublisher.configured()
        s3, config = provider.s3, provider.config
    assert config is not None
    try:
        response = s3.get_object(Bucket=config.bucket, Key=f"published/{app_id}/{deployment_id}/index.html",
                                 ExpectedBucketOwner=config.expected_owner)
        body = response["Body"].read(8 * 1024 * 1024 + 1)
        metadata = response.get("Metadata", {})
        if (len(body) > 8 * 1024 * 1024 or metadata.get("deployment_id") != deployment_id
                or metadata.get("sha256") != hashlib.sha256(body).hexdigest()):
            raise HostingError("hosting_public_copy_audit_unverified", status=503)
        categories, _ = public_copy_observation(public_html_text(body.decode("utf-8")))
    except HostingError:
        raise
    except UnicodeDecodeError as exc:
        raise HostingError("hosting_public_copy_audit_unreadable", status=503) from exc
    except (ClientError, KeyError, OSError, ValueError) as exc:
        raise HostingError("hosting_public_copy_audit_unavailable", status=503) from exc
    return {"deployment_id": deployment_id, "state": "needs_review" if categories else "clear",
            "findings": [{"path": "index.html", "categories": categories}] if categories else []}


def reconcile_uncertain_publications(*, now=None, provider=None, limit=50) -> int:
    from datetime import timedelta

    from core.clock import as_utc, utcnow

    from app.core.jobs import ACTIVE_JOB_STATUSES
    from app.db.models import Job, SiteVersion

    clock = as_utc(now or utcnow()).replace(tzinfo=None)
    cutoff = clock - timedelta(minutes=5)
    log = logging.getLogger(__name__)

    def active_publish(session, deployment_id):
        # ponytail: bounded sweep scans active intents, indexed payload lookup if the deploy queue grows.
        return any(job.dedupe_key == "static-publish:" + deployment_id
                   or _json(job.payload_json).get("deployment_id") == deployment_id
                   for job in session.scalars(select(Job).where(
                       Job.kind == "deploy.run", Job.status.in_(ACTIVE_JOB_STATUSES))))

    with db()() as session:
        ids = [row.id for row in session.scalars(select(Deployment).where(
            Deployment.state == "failed", Deployment.error == "hosting_public_state_uncertain",
            Deployment.started_at < cutoff).order_by(Deployment.started_at).limit(limit))
            if not active_publish(session, row.id)]
    if not ids:
        return 0
    try:
        provider = provider if provider is not None else AwsStaticPublisher.configured()
    except Exception as exc:  # noqa: BLE001 -- missing hosting configuration cannot stop maintenance
        log.warning("hosting_publication_readback_pending", extra={"error_type": type(exc).__name__})
        return 0

    resolved = 0
    for deployment_id in ids:
        try:
            with db()() as session:
                deployment = session.get(Deployment, deployment_id)
                serialize_writes(session, key="static-publish:" + deployment.app_id)
                session.refresh(deployment, with_for_update=True)
                if (deployment.state != "failed" or deployment.error != "hosting_public_state_uncertain"
                        or active_publish(session, deployment_id)):
                    continue
                app = session.get(FounderApp, deployment.app_id)
                if (not app or app.deleted_at is not None or deployment.target != "supymem_static"
                        or (deployment.workspace_id, deployment.venture_id) != (app.workspace_id, app.venture_id)):
                    raise HostingError("hosting_deployment_intent_invalid")
                hostname = public_hostname(app.slug, provider.config.public_domain)
                mapping = provider.read_mapping(hostname)
                live = list(session.scalars(select(Deployment).where(
                    Deployment.app_id == app.id, Deployment.state == "live")))
                if mapping is not None and (mapping["app"] != app.id or not mapping["enabled"]):
                    raise HostingError("hosting_public_state_uncertain")
                is_live = mapping is not None and mapping["deployment"] == deployment.id
                if is_live:
                    from app.hosting.unpublish import assert_no_takedown

                    assert_no_takedown(session, app)
                    previous = session.get(Deployment, deployment.previous_deployment_id) if deployment.previous_deployment_id else None
                    if previous and previous.app_id == app.id and previous.state == "live":
                        previous.state = "superseded"
                    domain = session.scalar(select(AppDomain).where(AppDomain.app_id == app.id, AppDomain.kind == "supymem"))
                    if not domain or domain.hostname != hostname:
                        raise HostingError("hosting_domain_conflict")
                    domain.state, domain.active_at = "active", clock
                    deployment.state, deployment.live_at, deployment.error = "live", clock, None
                    deployment.url = deployment.health_url = f"https://{hostname}/"
                    if (deployment.build_artifact_id or "").startswith("firstlook:"):
                        version_id = deployment.build_artifact_id.removeprefix("firstlook:")
                        site = session.get(SiteVersion, version_id)
                        if site and (site.workspace_id, site.venture_id) == (app.workspace_id, app.venture_id):
                            site.published = 1
                    else:
                        try:
                            with session.begin_nested():
                                from app.build.subscription_static_completion import (
                                    complete_at_publication,
                                )
                                from app.hosting.revisions import (
                                    assert_publication_baseline,
                                )

                                artifact = session.get(BuildArtifact, deployment.build_artifact_id)
                                if artifact is None:
                                    raise HostingError("hosting_source_changed")
                                run, artifact, qa, _, _ = _accepted_source(session, app, artifact.build_run_id)
                                if artifact.id != deployment.build_artifact_id:
                                    raise HostingError("hosting_source_changed")
                                assert_publication_baseline(session, app, run, artifact)
                                conversion = _conversion_proof(session, app, run, qa)
                                complete_at_publication(session, run, _json(run.spec), deployment, conversion)
                        except Exception as exc:  # noqa: BLE001 -- optional completion cannot undo proven public state
                            log.warning("hosting_publication_completion_pending", extra={
                                "deployment_id": deployment_id, "error_type": type(exc).__name__})
                elif (mapping is None and not live) or (mapping is not None and any(
                        row.id == mapping["deployment"] for row in live)):
                    deployment.error = "hosting_publication_interrupted"
                    from app.chat.milestone_hooks import deployment_resolved

                    deployment_resolved(app.venture_id, deployment.id, session=session)
                else:
                    raise HostingError("hosting_public_state_uncertain")
                venture_id, workspace_id, app_id, url = app.venture_id, app.workspace_id, app.id, deployment.url
                session.commit()
        except Exception as exc:  # noqa: BLE001 -- roll back all bookkeeping and retry evidence next sweep
            log.warning("hosting_publication_readback_pending", extra={
                "deployment_id": deployment_id, "error_type": type(exc).__name__})
            continue
        resolved += 1
        if is_live:
            try:
                from app.ventures.stage import after_evidence

                after_evidence(venture_id, "publication:" + deployment_id)
            except Exception:  # noqa: BLE001 -- stage projection cannot undo a durable publication
                log.warning("founder_stage_publication_pending")
            audit("hosting.publication_live", workspace_id=workspace_id, venture_id=venture_id,
                  actor_kind="system", surface="internal", target_ref=deployment_id,
                  payload={"app_id": app_id, "url": url, "resolved_by": "maintenance_readback"})
            _record_site_published(deployment_id, None, workspace_id, venture_id)
            from app.chat.milestone_hooks import deployment_live

            deployment_live(venture_id, deployment_id, url)
    return resolved


def _ensure_intent(
    session,
    app: FounderApp,
    build_id: str,
    config: HostingConfig,
    *,
    deployment_id: str | None = None,
):
    from app.hosting.unpublish import assert_no_takedown
    assert_no_takedown(session, app)
    run, artifact, qa, capture, manifest = _accepted_source(session, app, build_id)
    from app.hosting.revisions import assert_publication_baseline
    assert_publication_baseline(session, app, run, artifact)
    if deployment_id:
        deployment = session.get(Deployment, deployment_id)
        if (
            not deployment
            or deployment.app_id != app.id
            or deployment.build_artifact_id != artifact.id
            or deployment.target != "supymem_static"
            or deployment.state
            not in {
                "pending",
                "packaging",
                "pushing",
                "creating_infra",
                "health_checking",
                "failed",
                "live",
            }
        ):
            raise HostingError("hosting_deployment_intent_invalid")
    else:
        live = session.scalar(
            select(Deployment).where(
                Deployment.app_id == app.id,
                Deployment.build_artifact_id == artifact.id,
                Deployment.state == "live",
            )
        )
        pending = session.scalar(
            select(Deployment).where(
                Deployment.app_id == app.id,
                Deployment.build_artifact_id == artifact.id,
                or_(Deployment.state.in_(
                    ("pending", "packaging", "pushing", "creating_infra", "health_checking")),
                    and_(Deployment.state == "failed", Deployment.error == "hosting_public_state_uncertain")),
            )
        )
        deployment = live or pending
    conversion = None if deployment and deployment.state == "live" else _conversion_proof(session, app, run, qa)
    now = naive_utc()
    if not deployment:
        previous = session.scalar(
            select(Deployment)
            .where(Deployment.app_id == app.id, Deployment.state == "live")
            .order_by(Deployment.version.desc())
        )
        version = (
            int(
                session.scalar(
                    select(func.max(Deployment.version)).where(
                        Deployment.app_id == app.id
                    )
                )
                or 0
            )
            + 1
        )
        deployment = Deployment(
            id=new_id("dep"),
            app_id=app.id,
            workspace_id=app.workspace_id,
            venture_id=app.venture_id,
            build_artifact_id=artifact.id,
            target="supymem_static",
            state="pending",
            version=version,
            previous_deployment_id=previous.id if previous else None,
            started_at=now,
        )
        session.add(deployment)
    hostname = public_hostname(app.slug, config.public_domain)
    domain = session.scalar(
        select(AppDomain).where(AppDomain.app_id == app.id, AppDomain.kind == "supymem")
    )
    if domain and domain.hostname != hostname:
        raise HostingError("hosting_domain_conflict")
    if not domain:
        domain = AppDomain(
            id=new_id("dom"),
            app_id=app.id,
            workspace_id=app.workspace_id,
            hostname=hostname,
            kind="supymem",
            state="pending",
            created_at=now,
        )
        session.add(domain)
    session.flush()
    return deployment, run, artifact, qa, capture, manifest, hostname, conversion


def _record_site_published(deployment_id: str, actor: str | None, workspace_id: str, venture_id: str) -> None:
    """Count a publish that actually went live (the chat/UI publish controls only count attempts)."""
    try:
        from app.analytics.events import emit_once

        person = actor if actor and actor not in {"system", "founder", "local"} else None
        emit_once("site.published", event_key=f"deployment:{deployment_id}", person_id=person,
                  workspace_id=workspace_id, venture_id=venture_id,
                  props={"feature": "publish", "source": "hosting"})
    except Exception:  # noqa: BLE001 - telemetry never fails a live publish
        pass


def queue_accepted_build(app_id: str, build_id: str, *, actor: str, effect_fence=None) -> dict[str, Any]:
    """Persist publication intent and enqueue it; no provider call occurs here."""
    from app.core.local_mode import is_local
    if is_local():
        from app.pagebuilder.local_publish import publish_accepted
        return publish_accepted(app_id, build_id, actor=actor, effect_fence=effect_fence)
    config = HostingConfig.from_environment()
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app = session.get(FounderApp, app_id)
        if not app or app.deleted_at is not None:
            raise HostingError("hosting_app_not_found", status=404)
        if effect_fence is not None:
            effect_fence(session)
        deployment, *_rest = _ensure_intent(session, app, build_id, config)
        if deployment.state == "live":
            return {**_serialize(deployment), "job_id": None}
        from app.core.jobs import enqueue

        job_id = enqueue(
            session,
            "deploy.run",
            {
                "schema_version": 1,
                "deployment_id": deployment.id,
                "app_id": app.id,
                "build_id": build_id,
                "actor": actor,
            },
            workspace_id=app.workspace_id,
            venture_id=app.venture_id,
            dedupe_key="static-publish:" + deployment.id,
            effect_key="static-publish:" + deployment.id,
            priority=20,
            max_attempts=3,
        )
        session.commit()
        result = {**_serialize(deployment), "job_id": job_id}
        workspace_id, venture_id, deployment_id = (
            app.workspace_id,
            app.venture_id,
            deployment.id,
        )
    audit(
        "hosting.publication_queued",
        workspace_id=workspace_id,
        venture_id=venture_id,
        actor_id=actor,
        actor_kind="person",
        surface="api",
        target_ref=deployment_id,
        payload={"app_id": app_id, "build_id": build_id},
    )
    return result


def publish_accepted_build(
    deployment_id: str,
    app_id: str,
    build_id: str,
    *,
    actor: str,
    provider: AwsStaticPublisher | None = None,
) -> dict[str, Any]:
    from app.core.local_mode import is_local
    if is_local():
        from app.pagebuilder.local_publish import publish_accepted
        return publish_accepted(app_id, build_id, actor=actor, deployment_id=deployment_id)
    provider = provider or AwsStaticPublisher.configured()
    alias_done: list[tuple[str, str | None]] = []  # retired hosts re-pointed so far, for rollback
    with db()() as session:
        serialize_writes(session, key="static-publish:" + app_id)
        app = session.get(FounderApp, app_id)
        if not app or app.deleted_at is not None:
            raise HostingError("hosting_app_not_found", status=404)
        try:
            deployment, run, artifact, _qa, capture, manifest, hostname, conversion = _ensure_intent(
                session, app, build_id, provider.config, deployment_id=deployment_id,
            )
        except HostingError as exc:
            deployment = session.get(Deployment, deployment_id)
            run = session.get(BuildRun, build_id)
            if (deployment and run and deployment.state != "live"
                    and (deployment.app_id, deployment.workspace_id, deployment.venture_id, deployment.build_artifact_id)
                    == (app.id, app.workspace_id, app.venture_id, run.artifact_id)
                    and (run.app_id, run.workspace_id, run.venture_id) == (app.id, app.workspace_id, app.venture_id)):
                deployment.state, deployment.error = "failed", exc.code[:200]
                session.commit()
            raise
        if deployment.state == "live":
            return _serialize(deployment)
        deployment.state = "packaging"
        deployment.error = None
        scope = _source_scope(run, capture)
        artifact_id = artifact.id
        workspace_id, venture_id = app.workspace_id, app.venture_id
        session.commit()

    try:
        store = S3ArtifactStore.configured()
        blob = store.source(capture, scope)
        files = verified_public_files(blob, capture, manifest)
        with db()() as session:
            deployment = session.get(Deployment, deployment_id)
            deployment.state = "pushing"
            session.commit()
        provider.put_files(
            app_id=app_id,
            deployment_id=deployment_id,
            artifact_id=artifact_id,
            files=files,
        )
        with db()() as session:
            deployment = session.get(Deployment, deployment_id)
            deployment.state = "creating_infra"
            session.commit()
        mapping = host_mapping(
            app_id=app_id,
            deployment_id=deployment_id,
            generation=int(capture["generation"]),
            enabled=True,
        )
        # Recheck after upload: enablement may have changed while objects were written.
        with db()() as session:
            current_app = session.get(FounderApp, app_id)
            current_run, current_artifact, current_qa, *_ = _accepted_source(session, current_app, build_id)
            from app.hosting.revisions import assert_publication_baseline
            assert_publication_baseline(session, current_app, current_run, current_artifact)
            conversion = _conversion_proof(session, current_app, current_run, current_qa)
        revision = _json(current_run.spec).get("inputs", {}).get("revision")
        switch_attempted = True
        if revision and revision.get("kind") == "private_build_correction":
            previous_mapping = provider.switch(hostname, mapping, expected_absent=True)
        elif revision:
            previous_mapping = provider.switch(
                hostname, mapping,
                expected_previous_deployment_id=revision["deployment_id"],
            )
        elif _json(current_run.spec).get("lifecycle_version") == 3:
            baseline = _json(current_run.spec)["inputs"]["publication_baseline"]
            previous_mapping = provider.switch(hostname, mapping,
                expected_previous_deployment_id=baseline["deployment_id"],
                expected_absent=baseline["deployment_id"] is None,
                expected_previous_mapping=baseline["previous_mapping"])
        else:
            previous_mapping = provider.switch(hostname, mapping)
        switch_confirmed = True
        with db()() as session:
            deployment = session.get(Deployment, deployment_id)
            deployment.state = "health_checking"
            deployment.url = f"https://{hostname}/"
            deployment.health_url = f"https://{hostname}/"
            session.commit()
        index_sha = next(
            item["sha256"] for item in manifest["files"] if item["path"] == "index.html"
        )
        provider.verify_public(hostname, index_sha)
        from app.hosting.site_address import aliases

        for alias_host in aliases(app_id):  # retired addresses keep serving the current page
            alias_done.append((alias_host, provider.switch(alias_host, mapping)))
        with db()() as session:
            serialize_writes(session, key="static-publish:" + app_id)
            current_app = session.get(FounderApp, app_id)
            final_run, final_artifact, final_qa, final_capture, final_manifest = _accepted_source(
                session, current_app, build_id,
            )
            if (final_artifact.id != artifact_id
                    or final_capture != capture or final_manifest != manifest):
                raise HostingError("hosting_source_changed")
            # Upload, mapping switch and public readback can span external calls.
            # Revalidate the current reviewed authority before recording success;
            # a refusal follows the existing mapping rollback/reconciliation path.
            assert_publication_baseline(session, current_app, final_run, final_artifact)
            conversion = _conversion_proof(session, current_app, final_run, final_qa)
            deployment = session.get(Deployment, deployment_id)
            previous = (
                session.get(Deployment, deployment.previous_deployment_id)
                if deployment.previous_deployment_id
                else None
            )
            if previous and previous.state == "live":
                previous.state = "superseded"
            domain = session.scalar(
                select(AppDomain).where(
                    AppDomain.app_id == app_id, AppDomain.kind == "supymem"
                )
            )
            domain.state, domain.active_at = "active", naive_utc()
            deployment.state, deployment.live_at = "live", naive_utc()
            from app.build.subscription_static_completion import complete_at_publication
            complete_at_publication(session, final_run, _json(final_run.spec), deployment, conversion)
            session.commit()
            session.refresh(deployment)
            result = _serialize(deployment)
            publication_venture_id, publication_deployment_id = deployment.venture_id, deployment.id
        try:
            from app.ventures.stage import after_evidence
            after_evidence(publication_venture_id, "publication:" + publication_deployment_id)
        except Exception:  # stage projection must never roll back a durable live publication
            import logging
            logging.getLogger(__name__).warning("founder_stage_publication_pending")
        audit(
            "hosting.publication_live",
            workspace_id=workspace_id,
            venture_id=venture_id,
            actor_id=actor,
            actor_kind="person",
            surface="internal",
            target_ref=deployment_id,
            payload={"app_id": app_id, "build_id": build_id, "url": result["url"], "conversion": conversion},
        )
        _record_site_published(deployment_id, actor, workspace_id, venture_id)
        from app.chat.milestone_hooks import deployment_live
        deployment_live(venture_id, deployment_id, result["url"])  # never raises; once per deployment
        return result
    except Exception as exc:
        rollback_error: Exception | None = None
        if "mapping" in locals() and "previous_mapping" in locals():
            try:
                provider.restore(hostname, mapping, previous_mapping)
            except Exception as restore_exc:
                rollback_error = restore_exc
            for alias_host, alias_previous in alias_done:
                try:
                    provider.restore(alias_host, mapping, alias_previous)
                except Exception as restore_exc:
                    rollback_error = restore_exc
        if isinstance(exc, HostingError):
            failure_code = exc.code
        elif isinstance(exc, (BuildModelError, HostingRejected)):
            failure_code = str(exc)
        else:
            failure_code = "hosting_provider_failed"
        if (rollback_error or (locals().get("switch_attempted")
                and not locals().get("switch_confirmed")
                and (failure_code == "hosting_mapping_write_failed" or not isinstance(exc, HostingError)))):
            failure_code = "hosting_public_state_uncertain"
        with db()() as session:
            deployment = session.get(Deployment, deployment_id)
            if deployment and deployment.state != "live":
                deployment.state = "failed"
                deployment.error = failure_code[:200]
                session.commit()
        audit(
            "hosting.publication_failed",
            workspace_id=workspace_id,
            venture_id=venture_id,
            actor_id=actor,
            actor_kind="person",
            surface="internal",
            target_ref=deployment_id,
            outcome="failed",
            denied_reason=failure_code,
            payload={"app_id": app_id, "build_id": build_id},
        )
        from app.chat.milestone_hooks import deployment_failed
        deployment_failed(venture_id, deployment_id, failure_code)
        if isinstance(exc, HostingError) and failure_code == exc.code:
            raise
        raise HostingError(failure_code, status=503) from exc
