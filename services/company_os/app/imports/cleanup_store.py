"""Exact-key, version-aware import erasure with a retained zero-byte write fence.

No bucket scans, generic delete URLs, retention bypass or source-byte reads.
The trusted controller persists each returned fence/plan before the next phase.
"""
from __future__ import annotations

import base64
import hashlib
import re
import time

from app.builder.artifact_store import S3ArtifactStore
from app.builder.model_proxy import BuildModelError
from .contracts import digest

POLICY = "import-cleanup-v1"
MEDIA = "application/vnd.supymem.import-deletion-fence"
EMPTY_SHA = hashlib.sha256(b"").hexdigest()
EMPTY_CHECKSUM = base64.b64encode(bytes.fromhex(EMPTY_SHA)).decode()
MAX_VERSIONS = 32
MAX_REQUESTS = 80
MAX_PAGES = 4


def _version(value):
    if not isinstance(value, str) or value == "null" or not re.fullmatch(r"[A-Za-z0-9_+./=-]{1,1024}", value):
        raise BuildModelError("import_cleanup_version_invalid")
    return value


class ImportObjectCleaner:
    def __init__(self, store: S3ArtifactStore, scope, *, kind, intent, deadline=None):
        self.store, self.config, self.client = store, store.config, store.client
        self.key, self.data_tags = store._import_object(scope, kind=kind, object_digest=intent.get("object_digest"))
        self.scope, self.kind, self.intent = dict(scope), kind, dict(intent)
        if (set(intent) != {"object_digest", "sha256", "size_bytes"}
                or not re.fullmatch(r"[a-f0-9]{64}", intent.get("sha256", ""))
                or type(intent.get("size_bytes")) is not int or not 0 < intent["size_bytes"] <= 32 * 1024 * 1024
                or kind == "raw" and intent["object_digest"] != intent["sha256"]):
            raise BuildModelError("import_cleanup_intent_invalid")
        self.target_digest = digest({"scope": scope, "kind": kind, "intent": intent, "config": self.config.fingerprint, "policy": POLICY})
        self.fence_tags = {**scope, "kind": "import_deletion_fence", "retired_kind": kind,
            "object_digest": intent["object_digest"], "target_digest": self.target_digest, "policy": POLICY, "sha256": EMPTY_SHA}
        self.data_tags = {**self.data_tags, "sha256": intent["sha256"]}
        self.deadline, self.requests = deadline or time.monotonic() + 90, 0

    def _call(self, name, **kwargs):
        self.requests += 1
        if self.requests > MAX_REQUESTS or time.monotonic() >= self.deadline:
            raise BuildModelError("import_cleanup_operation_limit")
        result = getattr(self.client, name)(Bucket=self.config.bucket, ExpectedBucketOwner=self.config.expected_owner, **kwargs)
        if time.monotonic() >= self.deadline:
            raise BuildModelError("import_cleanup_operation_limit")
        return result

    def _head(self, version=None, *, missing=False):
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self._call("head_object", Key=self.key, ChecksumMode="ENABLED", **({"VersionId": _version(version)} if version else {}))
        except ClientError as exc:
            if missing and str(exc.response.get("Error", {}).get("Code")) in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
                return None
            raise BuildModelError("import_cleanup_read_unconfirmed") from exc
        except BotoCoreError as exc:
            raise BuildModelError("import_cleanup_read_unconfirmed") from exc

    def _metadata(self, observed, *, fence):
        tags = self.fence_tags if fence else self.data_tags
        sha = EMPTY_CHECKSUM if fence else base64.b64encode(bytes.fromhex(self.intent["sha256"])).decode()
        if (not isinstance(observed, dict) or observed.get("Metadata") != tags
                or observed.get("ContentLength") != (0 if fence else self.intent["size_bytes"])
                or observed.get("ContentType") != (MEDIA if fence else "application/zip")
                or observed.get("ChecksumSHA256") != sha or observed.get("ContentEncoding") not in {None, "identity"}
                or observed.get("ServerSideEncryption") != "aws:kms" or observed.get("SSEKMSKeyId") != self.config.kms_key_arn
                or observed.get("ObjectLockMode") or observed.get("ObjectLockRetainUntilDate")
                or observed.get("ObjectLockLegalHoldStatus") == "ON"):
            raise BuildModelError("import_cleanup_metadata_unverified")
        _version(observed.get("VersionId"))
        if not re.fullmatch(r'"[A-Za-z0-9_-]{1,128}"', observed.get("ETag", "")):
            raise BuildModelError("import_cleanup_etag_invalid")
        return observed

    def _receipt(self, observed):
        return {"bucket": self.config.bucket, "key": self.key, "version_id": observed["VersionId"],
            "sha256": EMPTY_SHA, "size_bytes": 0, "etag": observed["ETag"], "storage_config_digest": self.config.fingerprint,
            "target_digest": self.target_digest, "policy": POLICY, "content_free_fence": True}

    def fence(self):
        """Install/reconcile an immutable current fence; old data remains versioned."""
        from botocore.exceptions import BotoCoreError, ClientError

        observed = self._head(missing=True)
        if observed and observed.get("Metadata", {}).get("kind") == "import_deletion_fence":
            return self._receipt(self._metadata(observed, fence=True))
        condition = {"IfNoneMatch": "*"}
        if observed:
            self._metadata(observed, fence=False)
            condition = {"IfMatch": observed["ETag"]}
        try:
            self._call("put_object", Key=self.key, Body=b"", ContentLength=0, ContentType=MEDIA,
                ChecksumSHA256=EMPTY_CHECKSUM, Metadata=self.fence_tags,
                ServerSideEncryption="aws:kms", SSEKMSKeyId=self.config.kms_key_arn, **condition)
        except (ClientError, BotoCoreError):
            # Reconcile the same deterministic fence after a lost ACK or race.
            pass
        current = self._head(missing=True)
        if current is None or current.get("Metadata", {}).get("kind") != "import_deletion_fence":
            raise BuildModelError("import_cleanup_fence_unconfirmed")
        return self._receipt(self._metadata(current, fence=True))

    def _verify_fence(self, receipt):
        observed = self._metadata(self._head(), fence=True)
        if receipt != self._receipt(observed):
            raise BuildModelError("import_cleanup_fence_changed")
        return observed

    def _versions(self):
        from botocore.exceptions import BotoCoreError, ClientError

        entries, seen, markers = [], set(), {}
        try:
            for _ in range(MAX_PAGES):
                page = self._call("list_object_versions", Prefix=self.key, MaxKeys=MAX_VERSIONS + 1, **markers)
                if (page.get("Name") != self.config.bucket or page.get("Prefix") != self.key
                        or type(page.get("IsTruncated")) is not bool):
                    raise BuildModelError("import_cleanup_listing_invalid")
                for collection, kind in (("Versions", "source"), ("DeleteMarkers", "delete_marker")):
                    values = page.get(collection, [])
                    if not isinstance(values, list):
                        raise BuildModelError("import_cleanup_listing_invalid")
                    for item in values:
                        if not isinstance(item, dict) or item.get("Key") != self.key or type(item.get("IsLatest")) is not bool:
                            raise BuildModelError("import_cleanup_foreign_key")
                        version = _version(item.get("VersionId"))
                        if version in seen:
                            raise BuildModelError("import_cleanup_listing_repeated")
                        seen.add(version)
                        entries.append({"version_id": version, "kind": kind, "is_latest": item["IsLatest"]})
                        if len(entries) > MAX_VERSIONS:
                            raise BuildModelError("import_cleanup_version_limit")
                if not page["IsTruncated"]:
                    if sum(item["is_latest"] for item in entries) != 1:
                        raise BuildModelError("import_cleanup_listing_invalid")
                    return entries
                next_markers = {"KeyMarker": page.get("NextKeyMarker"), "VersionIdMarker": page.get("NextVersionIdMarker")}
                if next_markers["KeyMarker"] != self.key or next_markers == markers:
                    raise BuildModelError("import_cleanup_pagination_invalid")
                _version(next_markers["VersionIdMarker"])
                markers = next_markers
        except (ClientError, BotoCoreError) as exc:
            raise BuildModelError("import_cleanup_listing_unconfirmed") from exc
        raise BuildModelError("import_cleanup_page_limit")

    def plan(self, fence):
        """Validate all versions before any source deletion, then persist this plan."""
        self._verify_fence(fence)
        entries = self._versions()
        target = []
        for item in entries:
            if item["version_id"] == fence["version_id"]:
                if not item["is_latest"] or item["kind"] != "source":
                    raise BuildModelError("import_cleanup_fence_changed")
                continue
            if item["is_latest"]:
                raise BuildModelError("import_cleanup_fence_changed")
            size = 0
            if item["kind"] == "source":
                observed = self._head(item["version_id"])
                # An earlier identical cleanup fence has no source contents.
                is_fence = observed.get("Metadata", {}).get("kind") == "import_deletion_fence"
                self._metadata(observed, fence=is_fence)
                size = observed["ContentLength"]
            target.append({"version_id": item["version_id"], "kind": item["kind"], "size_bytes": size})
        value = {"target_digest": self.target_digest, "fence_version_id": fence["version_id"], "versions": sorted(target, key=lambda item: item["version_id"])}
        return {**value, "plan_digest": digest(value)}

    def erase(self, fence, plan):
        """Delete only a persisted exact plan, then prove only the fence remains."""
        from botocore.exceptions import BotoCoreError, ClientError

        self._verify_fence(fence)
        value = {k: plan.get(k) for k in ("target_digest", "fence_version_id", "versions")}
        versions = value["versions"]
        if (set(plan) != {*value, "plan_digest"} or value["target_digest"] != self.target_digest
                or value["fence_version_id"] != fence["version_id"] or digest(value) != plan["plan_digest"]
                or not isinstance(versions, list) or len(versions) >= MAX_VERSIONS):
            raise BuildModelError("import_cleanup_plan_invalid")
        known = {item["version_id"]: item for item in self._versions()}
        expected = set()
        for item in versions:
            if (not isinstance(item, dict) or set(item) != {"version_id", "kind", "size_bytes"}
                    or item["kind"] not in {"source", "delete_marker"} or type(item["size_bytes"]) is not int
                    or item["size_bytes"] not in {0, self.intent["size_bytes"]}):
                raise BuildModelError("import_cleanup_plan_invalid")
            version = _version(item["version_id"])
            if version == fence["version_id"] or version in expected:
                raise BuildModelError("import_cleanup_plan_invalid")
            expected.add(version)
            if version in known and (known[version]["kind"] != item["kind"] or known[version]["is_latest"]):
                raise BuildModelError("import_cleanup_version_changed")
        if set(known) - expected - {fence["version_id"]}:
            raise BuildModelError("import_cleanup_unplanned_version")
        # Validate every remaining data version before deleting any of this plan.
        for item in versions:
            if item["version_id"] in known and item["kind"] == "source":
                self._metadata(self._head(item["version_id"]), fence=item["size_bytes"] == 0)
        try:
            for item in versions:
                if item["version_id"] in known:
                    self._call("delete_object", Key=self.key, VersionId=item["version_id"])
        except (ClientError, BotoCoreError) as exc:
            raise BuildModelError("import_cleanup_delete_unconfirmed") from exc
        self._verify_fence(fence)
        remaining = self._versions()
        if remaining != [{"version_id": fence["version_id"], "kind": "source", "is_latest": True}]:
            raise BuildModelError("import_cleanup_data_remains")
        return {"policy": POLICY, "target_digest": self.target_digest, "fence": fence,
            "plan_digest": plan["plan_digest"], "source_data_versions_absent": True,
            "content_free_fence_retained": True, "release_bound_bytes": self.intent["size_bytes"],
            "observed_data_versions": sum(item["size_bytes"] > 0 for item in versions),
            "requests": self.requests}
