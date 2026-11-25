"""Typed import storage uses actual shared immutable S3 receipt implementation."""
import hashlib

import pytest
from tests.unit.builder.test_qa_storage import store as store


def test_import_conditional_receipt_and_exact_readback(store):
    api, client, _, _, blob, _, _error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="import-one")
    sha = hashlib.sha256(blob).hexdigest()
    client.lose_put_ack = True
    receipt = api.persist_import(blob, scope, kind="raw", object_digest=sha)
    assert receipt["key"] == f"managed-builds/imports/workspace/venture/app/import-one/raw/{sha}.zip"
    assert api.read_import(receipt, scope, kind="raw", object_digest=sha) == blob
    puts = len([c for c in client.calls if c[0] == "put"])
    assert api.persist_import(blob, scope, kind="raw", object_digest=sha) == receipt
    assert len([c for c in client.calls if c[0] == "put"]) == puts


@pytest.mark.parametrize("change", [{"venture_id": "foreign"}, {"app_id": "../escape"}, {"import_id": "other"}])
def test_import_scope_cannot_read_other_object(store, change):
    api, _client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="import-one")
    receipt = api.persist_import(blob, scope, kind="source", object_digest="a" * 64)
    with pytest.raises(error):
        api.read_import(receipt, {**scope, **change}, kind="source", object_digest="a" * 64)


def test_import_wrong_bytes_and_manifest_identity_do_not_overwrite(store):
    api, client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="import-one")
    with pytest.raises(error, match="input_mismatch"):
        api.persist_import(blob, scope, kind="raw", object_digest="a" * 64)
    receipt = api.persist_import(blob, scope, kind="source", object_digest="a" * 64)
    with pytest.raises(error, match="verification_failed"):
        api.persist_import(blob + b"changed", scope, kind="source", object_digest="a" * 64)
    assert api.read_import(receipt, scope, kind="source", object_digest="a" * 64) == blob
    with pytest.raises(error, match="receipt_invalid"):
        api.read_import(receipt, scope, kind="raw", object_digest="a" * 64)


def test_import_unconfirmed_write_does_not_become_verified(store):
    api, client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="import-one")
    client.lose_head_after_put = True
    with pytest.raises(error, match="outcome_uncertain"):
        api.persist_import(blob, scope, kind="source", object_digest="a" * 64)
    puts = len([c for c in client.calls if c[0] == "put"])
    recovered = api.persist_import(blob, scope, kind="source", object_digest="a" * 64)
    assert api.read_import(recovered, scope, kind="source", object_digest="a" * 64) == blob
    assert len([c for c in client.calls if c[0] == "put"]) == puts


def test_probe_known_intent_reads_but_never_writes(store):
    api, client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", import_id="probe")
    sha = hashlib.sha256(blob).hexdigest()
    args = dict(kind="raw", object_digest=sha, sha256=sha, size_bytes=len(blob))
    before = len([c for c in client.calls if c[0] == "put"])
    assert api.probe_import(scope, **args) is None
    assert len([c for c in client.calls if c[0] == "put"]) == before
    receipt = api.persist_import(blob, scope, kind="raw", object_digest=sha)
    assert api.probe_import(scope, **args) == receipt
    with pytest.raises(error, match="verification_failed"):
        api.probe_import(scope, **{**args, "size_bytes": len(blob) + 1})
