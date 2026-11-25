"""Immutable, checked S3 artifact writes by the trusted Core controller only."""
from __future__ import annotations

import base64
from dataclasses import asdict, dataclass
import hashlib
import os
import re
import time

from .model_proxy import BuildModelError, digest


@dataclass(frozen=True)
class ArtifactStoreConfig:
    bucket: str
    prefix: str
    kms_key_arn: str
    region: str
    expected_owner: str

    def __post_init__(self):
        if (not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", self.bucket)
                or not re.fullmatch(r"[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+){0,8}", self.prefix)
                or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-\d", self.region)
                or not re.fullmatch(r"\d{12}", self.expected_owner)
                or not re.fullmatch(rf"arn:aws:kms:{re.escape(self.region)}:{self.expected_owner}:key/[a-f0-9-]{{36}}", self.kms_key_arn)):
            raise BuildModelError("artifact_store_configuration_invalid")

    @classmethod
    def from_environment(cls):
        try:
            return cls(*(os.environ[key] for key in ("BUILD_ARTIFACT_S3_BUCKET", "BUILD_ARTIFACT_S3_PREFIX", "BUILD_ARTIFACT_KMS_KEY_ARN",
                                                     "BUILD_ARTIFACT_AWS_REGION", "BUILD_ARTIFACT_S3_EXPECTED_OWNER")))
        except KeyError as exc:
            raise BuildModelError("artifact_store_unavailable") from exc

    @property
    def fingerprint(self):
        return digest(asdict(self))

    def key(self, scope, manifest_digest):
        parts = [scope[k] for k in ("workspace_id", "venture_id", "app_id", "build_id")]
        if (any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in parts)
                or type(scope["generation"]) is not int or scope["generation"] < 1
                or not isinstance(manifest_digest, str) or not re.fullmatch(r"[a-f0-9]{64}", manifest_digest)):
            raise BuildModelError("artifact_store_scope_invalid")
        return "/".join([self.prefix, *parts, "g" + str(scope["generation"]), manifest_digest + ".zip"])


class S3ArtifactStore:
    def __init__(self, config: ArtifactStoreConfig, client):
        self.config, self.client = config, client

    @classmethod
    def configured(cls):
        config = ArtifactStoreConfig.from_environment()  # fail before AWS credential lookup
        import boto3
        from botocore.config import Config
        client = boto3.client("s3", region_name=config.region,
                              config=Config(connect_timeout=5, read_timeout=30, retries={"total_max_attempts": 1},
                                            ignore_configured_endpoint_urls=True, proxies={}))
        return cls(config, client)

    def _persist_object(self, blob, *, key, tags, media, limit, prefix):
        """Shared no-overwrite write and exact receipt; caller derives scope/key."""
        from botocore.exceptions import BotoCoreError, ClientError
        if not isinstance(blob, bytes) or not 0 < len(blob) <= limit:
            raise BuildModelError(prefix + "_object_limit")
        sha = hashlib.sha256(blob).hexdigest()
        tags = {**tags, "sha256": sha}
        checksum = base64.b64encode(bytes.fromhex(sha)).decode()
        base = {"Bucket": self.config.bucket, "Key": key, "ExpectedBucketOwner": self.config.expected_owner}
        def head():
            return self.client.head_object(**base, ChecksumMode="ENABLED")
        try:
            response = head()
        except ClientError as exc:
            # S3 may return 403 for missing keys when ListBucket is denied.
            if str(exc.response.get("Error", {}).get("Code")) not in {"404", "NoSuchKey", "NotFound", "403", "AccessDenied"}:
                raise BuildModelError(prefix + "_read_failed") from exc
            try:
                self.client.put_object(**base, Body=blob, ContentLength=len(blob), ContentType=media,
                    ChecksumSHA256=checksum, Metadata=tags, IfNoneMatch="*",
                    ServerSideEncryption="aws:kms", SSEKMSKeyId=self.config.kms_key_arn)
            except (ClientError, BotoCoreError):
                # A lost ACK or conditional conflict resolves only the same key.
                pass
            try:
                response = head()
            except (ClientError, BotoCoreError) as err:
                raise BuildModelError(prefix + "_outcome_uncertain") from err
        except BotoCoreError as exc:
            raise BuildModelError(prefix + "_read_failed") from exc
        if (response.get("ContentLength") != len(blob) or response.get("ChecksumSHA256") != checksum
                or response.get("Metadata") != tags or response.get("ContentType") != media
                or response.get("ServerSideEncryption") != "aws:kms" or response.get("SSEKMSKeyId") != self.config.kms_key_arn
                or not response.get("VersionId") or response.get("VersionId") == "null"):
            raise BuildModelError(prefix + "_verification_failed")
        return {"bucket": self.config.bucket, "key": key, "version_id": response["VersionId"], "sha256": sha,
                "size_bytes": len(blob), "storage_config_digest": self.config.fingerprint}

    def persist(self, blob: bytes, metadata: dict, scope: dict) -> dict:
        from core.artifact_policy import DEFAULT_LIMITS
        if hashlib.sha256(blob).hexdigest() != metadata["archive_sha256"] or len(blob) != metadata["size_bytes"]:
            raise BuildModelError("artifact_store_input_mismatch")
        key = self.config.key(scope, metadata["manifest_digest"])
        tags = {**{k: str(v) for k, v in scope.items()}, "manifest_digest": metadata["manifest_digest"]}
        receipt = self._persist_object(blob, key=key, tags=tags, media="application/zip",
                                       limit=DEFAULT_LIMITS.max_archive_bytes, prefix="artifact_store")
        return {**receipt, "manifest_digest": metadata["manifest_digest"]}

    def _import_object(self, scope, *, kind, object_digest):
        fields = ("workspace_id", "venture_id", "app_id", "import_id")
        if (not isinstance(scope, dict) or set(scope) != set(fields)
                or any(not isinstance(scope[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope[k]) for k in fields)
                or kind not in {"raw", "source"} or not re.fullmatch(r"[a-f0-9]{64}", object_digest or "")):
            raise BuildModelError("import_store_scope_invalid")
        key = "/".join([self.config.prefix, "imports", *(scope[k] for k in fields), kind, object_digest + ".zip"])
        return key, {**scope, "kind": "import_" + kind, "object_digest": object_digest}

    def persist_import(self, blob, scope, *, kind, object_digest):
        from core.artifact_policy import DEFAULT_LIMITS
        key, tags = self._import_object(scope, kind=kind, object_digest=object_digest)
        if kind == "raw" and hashlib.sha256(blob).hexdigest() != object_digest:
            raise BuildModelError("import_store_input_mismatch")
        receipt = self._persist_object(blob, key=key, tags=tags, media="application/zip",
                                       limit=DEFAULT_LIMITS.max_archive_bytes, prefix="import_store")
        return {**receipt, "kind": kind, "object_digest": object_digest}

    def read_import(self, receipt, scope, *, kind, object_digest):
        from core.artifact_policy import DEFAULT_LIMITS
        key, tags = self._import_object(scope, kind=kind, object_digest=object_digest)
        if receipt.get("kind") != kind or receipt.get("object_digest") != object_digest:
            raise BuildModelError("import_store_receipt_invalid")
        tags["sha256"] = receipt.get("sha256")
        return self._download(receipt, key=key, tags=tags, content_type="application/zip", limit=DEFAULT_LIMITS.max_archive_bytes)

    def probe_import(self, scope, *, kind, object_digest, sha256, size_bytes):
        """Reconcile one known write intent without any write, listing or key guess."""
        from botocore.exceptions import BotoCoreError, ClientError
        from core.artifact_policy import DEFAULT_LIMITS
        key, tags = self._import_object(scope, kind=kind, object_digest=object_digest)
        if (not re.fullmatch(r"[a-f0-9]{64}", sha256 or "") or type(size_bytes) is not int
                or not 0 < size_bytes <= DEFAULT_LIMITS.max_archive_bytes):
            raise BuildModelError("import_store_input_mismatch")
        try:
            observed = self.client.head_object(Bucket=self.config.bucket, Key=key,
                ExpectedBucketOwner=self.config.expected_owner, ChecksumMode="ENABLED")
        except ClientError as exc:
            if str(exc.response.get("Error", {}).get("Code")) in {"404", "NotFound", "NoSuchKey"}:
                return None
            # A 403 is not proof of missing bytes; keep the outcome unresolved.
            raise BuildModelError("import_store_outcome_uncertain") from exc
        except BotoCoreError as exc:
            raise BuildModelError("import_store_outcome_uncertain") from exc
        receipt = {"bucket": self.config.bucket, "key": key, "version_id": observed.get("VersionId"),
                   "sha256": sha256, "size_bytes": size_bytes, "storage_config_digest": self.config.fingerprint,
                   "kind": kind, "object_digest": object_digest}
        # The shared exact-version read checks all metadata, byte/hash and KMS
        # properties against the saved intent, rather than trusting this HEAD.
        self.read_import(receipt, scope, kind=kind, object_digest=object_digest)
        return receipt

    def _download(self, receipt, *, key, tags, content_type, limit):
        """Exact version, scope, KMS/checksum and bounded unbuffered body reads."""
        from botocore.exceptions import BotoCoreError, ClientError
        from urllib3.exceptions import HTTPError
        config = self.config
        size, checksum_hex = receipt.get("size_bytes"), receipt.get("sha256")
        if (receipt.get("storage_config_digest") != config.fingerprint or receipt.get("bucket") != config.bucket
                or receipt.get("key") != key or not isinstance(receipt.get("version_id"), str)
                or not 1 <= len(receipt["version_id"]) <= 1024 or receipt["version_id"] == "null"
                or type(size) is not int or not 0 < size <= limit or not re.fullmatch(r"[a-f0-9]{64}", checksum_hex or "")):
            raise BuildModelError("artifact_store_receipt_invalid")
        checksum = base64.b64encode(bytes.fromhex(checksum_hex)).decode()
        deadline, body = time.monotonic() + 45, None
        try:
            response = self.client.get_object(Bucket=config.bucket, Key=key, VersionId=receipt["version_id"],
                                              ExpectedBucketOwner=config.expected_owner, ChecksumMode="ENABLED")
            body = response.get("Body")
            if (time.monotonic() > deadline or response.get("VersionId") != receipt["version_id"]
                    or response.get("ContentLength") != size or response.get("ChecksumSHA256") != checksum
                    or response.get("Metadata") != tags or response.get("ContentType") != content_type
                    or response.get("ServerSideEncryption") != "aws:kms" or response.get("SSEKMSKeyId") != config.kms_key_arn
                    or response.get("ContentEncoding") not in {None, "identity"}):
                raise BuildModelError("artifact_store_verification_failed")
            # StreamingBody.read/iter_chunks may buffer until `amt` bytes while
            # a slow peer continually resets its socket timeout. urllib3 read1
            # makes one underlying read; our independent size/hash verification
            # replaces StreamingBody's buffered content-length check.
            reader = getattr(getattr(body, "_raw_stream", body), "read1", None)
            if not callable(reader):
                raise BuildModelError("artifact_store_stream_unsupported")
            chunks, count = [], 0
            while True:
                chunk = reader(65536)
                if time.monotonic() > deadline:
                    raise BuildModelError("artifact_store_transfer_limit")
                if not chunk:
                    break
                count += len(chunk)
                if count > size:
                    raise BuildModelError("artifact_store_transfer_limit")
                chunks.append(chunk)
            blob = b"".join(chunks)
            if len(blob) != size or hashlib.sha256(blob).hexdigest() != checksum_hex:
                raise BuildModelError("artifact_store_body_mismatch")
            return blob
        except BuildModelError:
            raise
        except (BotoCoreError, ClientError, HTTPError, OSError, ValueError) as exc:
            raise BuildModelError("artifact_store_read_failed") from exc
        finally:
            if body is not None:
                body.close()

    def source(self, receipt, scope):
        from core.artifact_policy import DEFAULT_LIMITS
        manifest = receipt.get("manifest_digest")
        key = self.config.key(scope, manifest)
        tags = {**{k: str(v) for k, v in scope.items()}, "manifest_digest": manifest, "sha256": receipt.get("sha256")}
        return self._download(receipt, key=key, tags=tags, content_type="application/zip", limit=DEFAULT_LIMITS.max_archive_bytes)

    def _coding_object(self, scope, sha256, manifest_digest):
        fields = ("workspace_id", "venture_id", "app_id", "order_id")
        if (not isinstance(scope, dict) or set(scope) != set(fields)
                or any(not isinstance(scope[k], str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", scope[k]) for k in fields)
                or not re.fullmatch(r"[a-f0-9]{64}", sha256 or "")
                or not re.fullmatch(r"[a-f0-9]{64}", manifest_digest or "")):
            raise BuildModelError("coding_store_scope_invalid")
        key = "/".join([self.config.prefix, "coding_sources", *(scope[k] for k in fields), sha256 + ".zip"])
        tags = {**scope, "kind": "coding_source", "manifest_digest": manifest_digest, "sha256": sha256}
        return key, tags

    def persist_coding_source(self, blob, scope, *, sha256, manifest_digest):
        from core.artifact_policy import DEFAULT_LIMITS
        key, tags = self._coding_object(scope, sha256, manifest_digest)
        if hashlib.sha256(blob).hexdigest() != sha256:
            raise BuildModelError("coding_store_input_mismatch")
        receipt = self._persist_object(blob, key=key, tags={k: v for k, v in tags.items() if k != "sha256"},
                                       media="application/zip", limit=DEFAULT_LIMITS.max_archive_bytes,
                                       prefix="coding_store")
        return {**receipt, "manifest_digest": manifest_digest}

    def read_coding_source(self, receipt, scope, *, sha256, manifest_digest):
        from core.artifact_policy import DEFAULT_LIMITS
        key, tags = self._coding_object(scope, sha256, manifest_digest)
        return self._download(receipt, key=key, tags=tags,
                              content_type="application/zip", limit=DEFAULT_LIMITS.max_archive_bytes)

    def _product_scenario_object(self, scope, sha256):
        # Reuse the exact four owned source-scope fields, never accept a key.
        self._coding_object(scope, sha256, sha256)
        key = "/".join([self.config.prefix, "product-qa", "scenarios",
                         *(scope[k] for k in ("workspace_id", "venture_id", "app_id", "order_id")),
                         sha256 + ".json"])
        return key, {**scope, "kind": "product_scenario", "sha256": sha256}

    def persist_product_scenario(self, blob, scope, *, sha256):
        from core.product_qa_contract import validate_product_journey
        _scenario, canonical, found = validate_product_journey(blob)
        if blob != canonical or found != sha256:
            raise BuildModelError("product_scenario_input_mismatch")
        key, tags = self._product_scenario_object(scope, sha256)
        return self._persist_object(blob, key=key,
                                    tags={k: v for k, v in tags.items() if k != "sha256"},
                                    media="application/json", limit=65536, prefix="product_scenario")

    def read_product_scenario(self, receipt, scope, *, sha256):
        key, tags = self._product_scenario_object(scope, sha256)
        blob = self._download(receipt, key=key, tags=tags,
                              content_type="application/json", limit=65536)
        from core.product_qa_contract import validate_product_journey
        _scenario, canonical, found = validate_product_journey(blob)
        if blob != canonical or found != sha256:
            raise BuildModelError("product_scenario_readback_mismatch")
        return blob

    def _qa_object(self, scope, qa_id, input_digest, object_id):
        from uuid import UUID
        try:
            if str(UUID(qa_id)) != qa_id or not re.fullmatch(r"[a-f0-9]{64}", input_digest):
                raise ValueError()
        except (ValueError, TypeError, AttributeError) as exc:
            raise BuildModelError("qa_store_scope_invalid") from exc
        root = self.config.key(scope, input_digest).rsplit("/", 1)[0]
        if object_id == "report.json":
            kind, media, limit = "qa_report", "application/json", 1024 * 1024
        elif re.fullmatch(r"(?:desktop|mobile)-[0-9]\.png", object_id or ""):
            kind, media, limit = "qa_evidence", "image/png", 8 * 1024 * 1024
        else:
            raise BuildModelError("qa_store_object_invalid")
        key = f"{root}/qa/{qa_id}/{input_digest}/{object_id}"
        tags = {**{k: str(v) for k, v in scope.items()}, "qa_id": qa_id, "input_digest": input_digest, "kind": kind}
        return key, tags, media, limit

    def persist_qa(self, blob, scope, *, qa_id, input_digest, object_id):
        key, tags, media, limit = self._qa_object(scope, qa_id, input_digest, object_id)
        receipt = self._persist_object(blob, key=key, tags=tags, media=media, limit=limit, prefix="qa_store")
        return {**receipt, "kind": tags["kind"], "object_id": object_id}

    def qa(self, receipt, scope, *, qa_id, input_digest, object_id):
        key, tags, media, limit = self._qa_object(scope, qa_id, input_digest, object_id)
        if receipt.get("kind") != tags["kind"] or receipt.get("object_id") != object_id:
            raise BuildModelError("qa_store_receipt_invalid")
        tags["sha256"] = receipt.get("sha256")
        return self._download(receipt, key=key, tags=tags, content_type=media, limit=limit)
