"""Pure, disabled product-source/scenario binding before storage or admission.

No caller-supplied scenario, report or copied static QA proof is accepted here.
The future durable controller must authorize custody/app/prior-publication state
and independently price/settle cloud QA before executing this prepared input.
"""
from __future__ import annotations

import hashlib
import json

from core.application_source import validate_product_manifest
from core.artifact_policy import ArtifactRejected
from core.artifact_snapshot import manifest_digest
from core.product_qa_contract import validate_product_journey

from app.build import native_product_source as native
from app.build.subscription_product_export import EXPORT_POLICY


def prepare(plan, selected: dict, projection: dict) -> dict:
    """Bind exact current approved scope to the projected immutable source.

    ``projection`` must be generated from server-read custody bytes by export;
    this pure helper is not an API admitting a client projection or source.
    """
    native.assert_current(plan, selected)
    try:
        bound = selected["product_source_contract"]
        if (projection["export_policy"] != EXPORT_POLICY
                or projection["artifact_profile"] != "founder_product_v1"
                or projection["runtime_qa_verified"] is not False
                or projection["hosting_authorized"] is not False
                or type(projection["size_bytes"]) is not int or projection["size_bytes"] <= 0):
            raise ValueError()
        for key in ("original_source_sha256", "original_manifest_digest", "archive_sha256", "manifest_digest"):
            value = projection[key]
            if (not isinstance(value, str) or len(value) != 64
                    or any(character not in "0123456789abcdef" for character in value)):
                raise ValueError()
        validate_product_manifest(projection["manifest"])
        if manifest_digest(projection["manifest"]) != projection["manifest_digest"]:
            raise ValueError()
        _scenario, raw, sha = validate_product_journey(json.dumps(bound["scenario"]).encode())
        if sha != bound["scenario_sha256"]:
            raise ValueError()
    except (KeyError, TypeError, ValueError, ArtifactRejected) as error:
        raise ValueError("subscription_product_prepared_inputs_invalid") from error
    identity = {"schema_version": 1, "policy_id": "subscription_product_source_qa_v1",
                "task_snapshot": selected, "source_projection": projection,
                "contract_sha256": bound["contract_sha256"], "scenario_sha256": sha,
                "runtime_qa_verified": False, "hosting_authorized": False}
    # Deep copy: prepared identities cannot mutate the approved input objects.
    frozen = json.loads(json.dumps(identity))
    canonical = json.dumps(frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    return {"identity": frozen, "fingerprint": hashlib.sha256(canonical).hexdigest(),
            "scenario_bytes": raw}
