"""Local coding source custody requires exact immutable S3 readback."""
import hashlib

import pytest

from tests.unit.builder.test_qa_storage import store as store


def test_coding_source_conditional_write_and_exact_readback(store):
    api, client, _, _, blob, _, _error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", order_id="dwo_1")
    sha = hashlib.sha256(blob).hexdigest()
    manifest = "a" * 64
    receipt = api.persist_coding_source(blob, scope, sha256=sha, manifest_digest=manifest)
    assert receipt["key"] == f"managed-builds/coding_sources/workspace/venture/app/dwo_1/{sha}.zip"
    assert api.read_coding_source(receipt, scope, sha256=sha, manifest_digest=manifest) == blob
    puts = len([call for call in client.calls if call[0] == "put"])
    assert api.persist_coding_source(blob, scope, sha256=sha, manifest_digest=manifest) == receipt
    assert len([call for call in client.calls if call[0] == "put"]) == puts


@pytest.mark.parametrize("change", [{"workspace_id": "foreign"}, {"venture_id": "other"},
                                     {"app_id": "other"}, {"order_id": "dwo_other"}])
def test_coding_source_cannot_cross_scope(store, change):
    api, _client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", order_id="dwo_1")
    sha, manifest = hashlib.sha256(blob).hexdigest(), "a" * 64
    receipt = api.persist_coding_source(blob, scope, sha256=sha, manifest_digest=manifest)
    with pytest.raises(error):
        api.read_coding_source(receipt, {**scope, **change}, sha256=sha, manifest_digest=manifest)


def test_coding_source_rejects_changed_manifest_and_uncertain_write(store):
    api, client, _, _, blob, _, error = store
    scope = dict(workspace_id="workspace", venture_id="venture", app_id="app", order_id="dwo_1")
    sha = hashlib.sha256(blob).hexdigest()
    client.lose_head_after_put = True
    with pytest.raises(error, match="outcome_uncertain"):
        api.persist_coding_source(blob, scope, sha256=sha, manifest_digest="a" * 64)
    receipt = api.persist_coding_source(blob, scope, sha256=sha, manifest_digest="a" * 64)
    with pytest.raises(error):
        api.read_coding_source(receipt, scope, sha256=sha, manifest_digest="b" * 64)
