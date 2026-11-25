"""Real cleaner logic against bounded version-aware S3 protocol fixtures."""
import base64
import hashlib
import io

import pytest
from botocore.exceptions import ClientError
from tests.integration.test_postgres_auth_security import service_modules


class VersionedS3:
    def __init__(self):
        self.data, self.order, self.calls = {}, [], []
        self.lost_put, self.lost_delete, self.before_put = False, False, None

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        key = kwargs["Key"]
        ids = [v for v in self.order if self.data[v]["Key"] == key]
        version = kwargs.get("VersionId") or (ids[-1] if ids else None)
        if not version or version not in self.data or self.data[version].get("DeleteMarker"):
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {k: v for k, v in self.data[version].items() if k not in {"Body", "Key"}}

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        assert kwargs["ExpectedBucketOwner"] == "123456789012"
        if self.before_put:
            callback, self.before_put = self.before_put, None
            callback()
        current = [v for v in self.order if self.data[v]["Key"] == kwargs["Key"]]
        current = self.data[current[-1]] if current else None
        exists = current is not None and not current.get("DeleteMarker")
        if kwargs.get("IfNoneMatch") == "*" and exists or kwargs.get("IfMatch") and (not exists or kwargs["IfMatch"] != current["ETag"]):
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        assert "IfMatch" in kwargs or kwargs.get("IfNoneMatch") == "*"
        body = kwargs["Body"]
        assert len(body) == kwargs["ContentLength"]
        assert base64.b64encode(hashlib.sha256(body).digest()).decode() == kwargs["ChecksumSHA256"]
        version = "v" + str(len(self.calls))
        self.order.append(version)
        self.data[version] = {"Key": kwargs["Key"], "Body": body, "VersionId": version,
            "ETag": '"' + hashlib.md5(body, usedforsecurity=False).hexdigest() + '"',
            **{k: kwargs[k] for k in ("ContentLength", "ContentType", "ChecksumSHA256", "Metadata", "ServerSideEncryption", "SSEKMSKeyId")}}
        if self.lost_put:
            self.lost_put = False
            raise ClientError({"Error": {"Code": "RequestTimeout"}}, "PutObject")
        return {"VersionId": version}

    def list_object_versions(self, **kwargs):
        self.calls.append(("list", kwargs))
        assert kwargs["ExpectedBucketOwner"] == "123456789012" and kwargs["MaxKeys"] <= 33
        ids = [v for v in self.order if self.data[v]["Key"].startswith(kwargs["Prefix"])]
        rows, markers = [], []
        for version in reversed(ids):
            data = self.data[version]
            item = {"Key": data["Key"], "VersionId": version,
                "IsLatest": version == next(v for v in reversed(self.order) if self.data[v]["Key"] == data["Key"])}
            (markers if data.get("DeleteMarker") else rows).append(item)
        return {"Name": kwargs["Bucket"], "Prefix": kwargs["Prefix"], "IsTruncated": False, "Versions": rows, "DeleteMarkers": markers}

    def delete_object(self, **kwargs):
        self.calls.append(("delete", kwargs))
        assert kwargs["ExpectedBucketOwner"] == "123456789012" and "VersionId" in kwargs
        assert "BypassGovernanceRetention" not in kwargs
        version = kwargs["VersionId"]
        if version in self.data:
            assert self.data[version]["Key"] == kwargs["Key"]
            assert self.data[version].get("ContentLength") != 0 or self.data[version].get("DeleteMarker")
            del self.data[version]
            self.order.remove(version)
        if self.lost_delete:
            self.lost_delete = False
            raise ClientError({"Error": {"Code": "RequestTimeout"}}, "DeleteObject")
        return {"VersionId": version}

    def get_object(self, **kwargs):
        from botocore.response import StreamingBody
        metadata = self.head_object(**kwargs)
        body = self.data[metadata["VersionId"]]["Body"]
        return {**metadata, "Body": StreamingBody(io.BytesIO(body), len(body))}


@pytest.fixture()
def cleanup():
    with service_modules("company_os"):
        from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
        from app.imports.cleanup_store import ImportObjectCleaner
        from app.builder.model_proxy import BuildModelError
        config = ArtifactStoreConfig("test-imports", "source-intake", "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111", "us-east-1", "123456789012")
        client = VersionedS3()
        store = S3ArtifactStore(config, client)
        scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="import")
        blob = b"Controlled quarantined source bytes"
        sha = hashlib.sha256(blob).hexdigest()
        intent = dict(object_digest=sha, sha256=sha, size_bytes=len(blob))
        def cleaner():
            return ImportObjectCleaner(store, scope, kind="raw", intent=intent)
        yield dict(store=store, client=client, scope=scope, blob=blob, intent=intent, cleaner=cleaner, error=BuildModelError)


def _write(d):
    return d["store"].persist_import(d["blob"], d["scope"], kind="raw", object_digest=d["intent"]["object_digest"])


def test_fence_then_all_data_versions_erased_and_late_writer_refused(cleanup):
    d = cleanup
    original = _write(d)
    cleaner = d["cleaner"]()
    fence = cleaner.fence()
    assert fence["version_id"] != original["version_id"] and fence["size_bytes"] == 0
    plan = cleaner.plan(fence)
    result = cleaner.erase(fence, plan)
    assert result["source_data_versions_absent"] and result["content_free_fence_retained"]
    assert result["release_bound_bytes"] == len(d["blob"])
    with pytest.raises(d["error"], match="verification_failed"):
        _write(d)
    assert len(d["client"].data) == 1
    assert all(row["Body"] == b"" for row in d["client"].data.values())
    assert d["cleaner"]().erase(fence, plan)["source_data_versions_absent"]


def test_unknown_fence_put_and_delete_ack_recover_same_identity(cleanup):
    d = cleanup
    _write(d)
    d["client"].lost_put = True
    fence = d["cleaner"]().fence()
    assert d["cleaner"]().fence() == fence
    plan = d["cleaner"]().plan(fence)
    d["client"].lost_delete = True
    with pytest.raises(d["error"], match="delete_unconfirmed"):
        d["cleaner"]().erase(fence, plan)
    assert d["cleaner"]().erase(fence, plan)["source_data_versions_absent"]


def test_absent_key_is_fenced_without_claiming_a_data_version_existed(cleanup):
    d = cleanup
    cleaner = d["cleaner"]()
    fence = cleaner.fence()
    plan = cleaner.plan(fence)
    assert plan["versions"] == []
    assert cleaner.erase(fence, plan)["observed_data_versions"] == 0


def test_source_writer_wins_race_before_absent_fence_retry(cleanup):
    d = cleanup
    d["client"].before_put = lambda: _write(d)
    with pytest.raises(d["error"], match="fence_unconfirmed"):
        d["cleaner"]().fence()
    fence = d["cleaner"]().fence()
    plan = d["cleaner"]().plan(fence)
    assert d["cleaner"]().erase(fence, plan)["observed_data_versions"] == 1


@pytest.mark.parametrize("field,value", [("Metadata", {}), ("ChecksumSHA256", "changed"), ("SSEKMSKeyId", "other"),
    ("ObjectLockMode", "GOVERNANCE"), ("ObjectLockLegalHoldStatus", "ON")])
def test_foreign_or_locked_current_data_never_overwritten_or_deleted(cleanup, field, value):
    d = cleanup
    receipt = _write(d)
    d["client"].data[receipt["version_id"]][field] = value
    before = len([c for c in d["client"].calls if c[0] == "put"])
    with pytest.raises(d["error"], match="metadata_unverified"):
        d["cleaner"]().fence()
    assert not any(c[0] == "delete" for c in d["client"].calls)
    assert len([c for c in d["client"].calls if c[0] == "put"]) == before


def test_unverified_noncurrent_version_blocks_all_deletion(cleanup):
    d = cleanup
    old = _write(d)
    fence = d["cleaner"]().fence()
    d["client"].data[old["version_id"]]["Metadata"] = {"foreign": "value"}
    with pytest.raises(d["error"], match="metadata_unverified"):
        d["cleaner"]().plan(fence)
    assert not any(c[0] == "delete" for c in d["client"].calls)


def test_exact_prefix_listing_never_deletes_neighbor_key(cleanup, monkeypatch):
    d = cleanup
    _write(d)
    cleaner = d["cleaner"]()
    fence = cleaner.fence()
    original = d["client"].list_object_versions
    def foreign(**kwargs):
        result = original(**kwargs)
        result["Versions"].append({"Key": cleaner.key + "-other", "VersionId": "foreign", "IsLatest": True})
        return result
    monkeypatch.setattr(d["client"], "list_object_versions", foreign)
    with pytest.raises(d["error"], match="foreign_key"):
        cleaner.plan(fence)
    assert not any(c[0] == "delete" for c in d["client"].calls)


def test_changed_plan_or_new_version_cannot_gain_delete_authority(cleanup):
    d = cleanup
    _write(d)
    cleaner = d["cleaner"]()
    fence, plan = cleaner.fence(), None
    plan = cleaner.plan(fence)
    with pytest.raises(d["error"], match="plan_invalid"):
        cleaner.erase(fence, {**plan, "versions": []})
    assert not any(c[0] == "delete" for c in d["client"].calls)


def test_exact_old_delete_marker_erased_without_creating_new_marker(cleanup):
    d = cleanup
    original = _write(d)
    key = original["key"]
    d["client"].data["marker"] = {"Key": key, "VersionId": "marker", "DeleteMarker": True}
    d["client"].order.append("marker")
    cleaner = d["cleaner"]()
    fence = cleaner.fence()
    plan = cleaner.plan(fence)
    assert any(x["kind"] == "delete_marker" for x in plan["versions"])
    assert cleaner.erase(fence, plan)["source_data_versions_absent"]
    assert len(d["client"].data) == 1


def test_version_cardinality_is_bounded_before_any_delete(cleanup):
    d = cleanup
    original = _write(d)
    for i in range(33):
        version = "extra" + str(i)
        d["client"].data[version] = {**d["client"].data[original["version_id"]], "VersionId": version}
        d["client"].order.append(version)
    cleaner = d["cleaner"]()
    fence = cleaner.fence()
    with pytest.raises(d["error"], match="version_limit"):
        cleaner.plan(fence)
    assert not any(name == "delete" for name, _ in d["client"].calls)


def test_deadline_prevents_even_first_cloud_request(cleanup):
    import time
    d = cleanup
    cleaner = d["cleaner"]()
    cleaner.deadline = time.monotonic() - 1
    with pytest.raises(d["error"], match="operation_limit"):
        cleaner.fence()
    assert not d["client"].calls
