"""SDK-shaped immutable scenario transport, not a cloud execution receipt."""

import base64
import hashlib
import io
import json

import boto3
import pytest
from botocore.stub import Stubber
from core.product_qa_contract import validate_product_journey
from tests.company_os_harness import load_company_os_app
from tests.unit.product import test_native_product_source as foundation


@pytest.mark.parametrize(
    "change",
    [
        None,
        "foreign_order",
        "null_version",
        "wrong_metadata",
        "changed_body",
        "noncanonical",
    ],
)
def test_exact_scenario_version_scope_and_body(change):
    load_company_os_app()
    from app.builder.artifact_store import ArtifactStoreConfig, S3ArtifactStore
    from app.builder.model_proxy import BuildModelError

    scenario = foundation.task.__wrapped__()["product_source_expectations"][
        "journey_json"
    ]
    _value, blob, sha = validate_product_journey(scenario.encode())
    scope = {
        "workspace_id": "w_one",
        "venture_id": "v_one",
        "app_id": "a_one",
        "order_id": "dwo_one",
    }
    config = ArtifactStoreConfig(
        "test-artifacts",
        "managed-builds",
        "arn:aws:kms:us-east-1:123456789012:key/11111111-1111-1111-1111-111111111111",
        "us-east-1",
        "123456789012",
    )
    client = boto3.client(
        "s3",
        region_name="us-east-1",
        aws_access_key_id="synthetic",
        aws_secret_access_key="synthetic",
    )
    store = S3ArtifactStore(config, client)
    if change == "noncanonical":
        noncanonical = json.dumps(json.loads(blob), indent=2).encode()
        with pytest.raises(BuildModelError, match="input_mismatch"):
            store.persist_product_scenario(
                noncanonical, scope, sha256=hashlib.sha256(noncanonical).hexdigest()
            )
        return
    key = (
        "managed-builds/product-qa/scenarios/w_one/v_one/a_one/dwo_one/" + sha + ".json"
    )
    base = {
        "Bucket": config.bucket,
        "Key": key,
        "ExpectedBucketOwner": config.expected_owner,
    }
    tags = {**scope, "kind": "product_scenario", "sha256": sha}
    checksum = base64.b64encode(bytes.fromhex(sha)).decode()
    metadata = {
        "ContentLength": len(blob),
        "ChecksumSHA256": checksum,
        "Metadata": tags,
        "ContentType": "application/json",
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": config.kms_key_arn,
        "VersionId": "exact-version",
    }
    with Stubber(client) as stub:
        stub.add_client_error(
            "head_object",
            service_error_code="404",
            http_status_code=404,
            expected_params={**base, "ChecksumMode": "ENABLED"},
        )
        stub.add_response(
            "put_object",
            {"VersionId": "exact-version"},
            {
                **base,
                "Body": blob,
                "ContentLength": len(blob),
                "ContentType": "application/json",
                "ChecksumSHA256": checksum,
                "Metadata": tags,
                "IfNoneMatch": "*",
                "ServerSideEncryption": "aws:kms",
                "SSEKMSKeyId": config.kms_key_arn,
            },
        )
        stub.add_response("head_object", metadata, {**base, "ChecksumMode": "ENABLED"})
        receipt = store.persist_product_scenario(blob, scope, sha256=sha)
        if change == "foreign_order":
            with pytest.raises(BuildModelError, match="receipt_invalid"):
                store.read_product_scenario(
                    receipt, {**scope, "order_id": "dwo_other"}, sha256=sha
                )
        elif change == "null_version":
            with pytest.raises(BuildModelError, match="receipt_invalid"):
                store.read_product_scenario(
                    {**receipt, "version_id": "null"}, scope, sha256=sha
                )
        else:
            response = {
                **metadata,
                "Body": io.BytesIO(
                    b"x" * len(blob) if change == "changed_body" else blob
                ),
            }
            if change == "wrong_metadata":
                response["Metadata"] = {**tags, "order_id": "dwo_other"}
            stub.add_response(
                "get_object",
                response,
                {**base, "VersionId": "exact-version", "ChecksumMode": "ENABLED"},
            )
            if change:
                with pytest.raises(BuildModelError):
                    store.read_product_scenario(receipt, scope, sha256=sha)
            else:
                assert store.read_product_scenario(receipt, scope, sha256=sha) == blob
        stub.assert_no_pending_responses()
