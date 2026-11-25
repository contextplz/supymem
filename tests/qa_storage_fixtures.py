"""Synthetic storage/protocol fixtures; these are not browser acceptance reports."""
from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import io
import struct
import zlib

from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from core.artifact_snapshot import canonical
from core.static_qa_contracts import Case, Evidence, Input, Report, VIEWPORTS

POLICY = {"image_digest": "sha256:" + "a" * 64, "browser_version": "153.0",
          "cluster_arn": "arn:aws:ecs:us-east-1:123456789012:cluster/qa-fixture",
          "task_definition_arn": "arn:aws:ecs:us-east-1:123456789012:task-definition/qa-fixture:1",
          "subnet_ids": ["subnet-12345678"], "security_group_ids": ["sg-12345678"]}
CONTRACT = {"profile": "static_landing_v1", "product_name": "Acorn", "required_text": ["Reading lists"], "actions": []}


def binding():
    return {"task_arn": "arn:aws:ecs:us-east-1:123456789012:task/qa-fixture/" + "1" * 32,
            "cluster_arn": POLICY["cluster_arn"], "task_definition_arn": POLICY["task_definition_arn"], "image_digest": POLICY["image_digest"],
            "eni_id": "eni-12345678", "private_ip": "10.0.1.6", "subnet_id": "subnet-12345678", "security_group_ids": ["sg-12345678"],
            "observed_at": datetime.now(timezone.utc).isoformat()}


class MemoryS3:
    """Network fixture with actual SDK request shapes, versions and lost ACKs."""
    def __init__(self):
        self.objects, self.latest, self.calls = {}, {}, []
        self.lose_put_ack = False
        self.lose_head_after_put = False
        self.fail_next_head = False
        self.after_put = None

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if self.fail_next_head:
            self.fail_next_head = False
            raise ClientError({"Error": {"Code": "ServiceUnavailable"}}, "HeadObject")
        key = (kwargs["Bucket"], kwargs["Key"])
        if key not in self.latest:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        value = self.objects[(*key, self.latest[key])]
        return {k: v for k, v in value.items() if k != "body"}

    def put_object(self, **kwargs):
        self.calls.append(("put", kwargs))
        assert kwargs["IfNoneMatch"] == "*" and kwargs["ServerSideEncryption"] == "aws:kms"
        assert kwargs["ExpectedBucketOwner"] == "123456789012"
        key = (kwargs["Bucket"], kwargs["Key"])
        if key in self.latest:
            raise ClientError({"Error": {"Code": "PreconditionFailed"}}, "PutObject")
        body = kwargs["Body"]
        assert base64.b64encode(hashlib.sha256(body).digest()).decode() == kwargs["ChecksumSHA256"]
        version = "version-" + str(len(self.objects) + 1)
        self.latest[key] = version
        self.objects[(*key, version)] = {"body": body, "VersionId": version, "ContentLength": len(body),
            **{key: kwargs[key] for key in ("ContentType", "Metadata", "ChecksumSHA256", "ServerSideEncryption", "SSEKMSKeyId")}}
        if self.after_put:
            self.after_put()
        if self.lose_head_after_put:
            self.lose_head_after_put, self.fail_next_head = False, True
        if self.lose_put_ack:
            self.lose_put_ack = False
            raise ClientError({"Error": {"Code": "RequestTimeout"}}, "PutObject")
        return {"VersionId": version}

    def get_object(self, **kwargs):
        self.calls.append(("get", kwargs))
        assert kwargs["ExpectedBucketOwner"] == "123456789012" and kwargs["ChecksumMode"] == "ENABLED"
        value = self.objects[(kwargs["Bucket"], kwargs["Key"], kwargs["VersionId"])]
        return {**{k: v for k, v in value.items() if k != "body"}, "Body": StreamingBody(io.BytesIO(value["body"]), len(value["body"]))}


def png(viewport):
    width, height = viewport["width"], viewport["height"]
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"\0" * (height * (1 + width * 3))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")


def report_fixture(prepared, build_id):
    request = Input.model_validate(prepared["input"])
    cases = []
    def add(key, suite, target, evidence=None):
        cases.append(Case(id=key, suite=suite, target=target, status="passed", observed="Synthetic fixture observation",
                          expected="Protocol integrity test", evidence_ids=evidence or []))
    add("artifact:source", "artifact", "source")
    for index, item in enumerate(request.manifest):
        add(f"http:{index}", "http", item["path"])
    add("runtime:sandbox", "runtime", "browser")
    for action in request.contract.actions:
        add("declared:" + action.id, "coverage", action.page)
    evidence, images = [], {}
    for viewport, dimensions in VIEWPORTS.items():
        key = viewport + "-0.png"
        images[key] = png(dimensions)
        evidence.append(Evidence(id=key, media_type="image/png", sha256=hashlib.sha256(images[key]).hexdigest(), size_bytes=len(images[key])))
        for suffix, suite in (("render", "render"), ("public_copy", "render"), ("offer", "render"), ("images", "render"), ("empty_footer", "render"), ("analytics_referrer", "render"), ("analytics_conversion", "actions"), ("layout", "render"), ("network", "network"), ("javascript", "render")):
            add(f"{viewport}-0:{suffix}", suite, "index.html", [key] if suffix == "render" else None)
        for action in request.contract.actions:
            for mode in ("pointer", "keyboard"):
                add(f"{viewport}-0:action:{action.id}:{mode}", "actions", action.id)
    add("artifact:unchanged", "artifact", "source")
    now = datetime.now(timezone.utc).isoformat()
    report = Report(qa_run_id=prepared["id"], build_run_id=build_id, generation=1, source_artifact_id=request.source_artifact_id,
                    source_sha256=request.source_sha256, source_manifest_digest=request.source_manifest_digest, contract_digest=request.contract.digest,
                    input_digest=request.digest, browser_version=POLICY["browser_version"], browser_sandbox_verified=True, runtime_uid=10001,
                    started_at=now, finished_at=now, outcome="passed", cases=cases, evidence=evidence,
                    coverage={"complete": True, "declared_actions": len(request.contract.actions), "visible_controls": 0, "observed_cases": len(cases), "unsupported_cases": 0},
                    exclusions=["Synthetic transport fixture, not a real browser result."])
    return canonical(report.model_dump(mode="json")), images
