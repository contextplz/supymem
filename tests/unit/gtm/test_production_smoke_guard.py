"""Release smoke cannot broaden the reviewed target or manufacture approval."""
import copy
import pytest
from scripts.gtm.production_smoke import reviewed_request


@pytest.fixture(autouse=True)
def synthetic_recipient(monkeypatch):
    monkeypatch.setitem(reviewed_request.__globals__, "ALLOWED_RECIPIENT", "fixture-founder@smoke.example")


def fixture():
    return {"can_send": True, "batches": [{"batch_id": "fixture", "draft_ids": ["doc"], "approval_digest": "sealed", "cap": 1}],
            "drafts": [{"doc_id": "doc", "review_state": "approved", "state": "draft", "to": "fixture-founder@smoke.example"}]}


def test_smoke_uses_exact_reviewed_authority():
    view = fixture()
    before = copy.deepcopy(view)
    assert reviewed_request(view, "fixture") == {"batch_id": "fixture", "draft_ids": ["doc"], "approval_digest": "sealed", "cap": 1}
    assert view == before


@pytest.mark.parametrize("field,value", [("to", "another@example.test"), ("review_state", "draft"), ("state", "delivery_unconfirmed")])
def test_smoke_refuses_another_recipient_or_unreviewed_uncertain_delivery(field, value):
    view = fixture()
    view["drafts"][0][field] = value
    with pytest.raises(ValueError):
        reviewed_request(view, "fixture")


def test_smoke_preserves_cap_and_execution_gate():
    view = fixture()
    view["batches"][0]["cap"] = 0
    with pytest.raises(ValueError, match="over_cap"):
        reviewed_request(view, "fixture")
    view = fixture()
    view["can_send"] = False
    with pytest.raises(ValueError, match="not_ready"):
        reviewed_request(view, "fixture")


def test_cli_is_runnable_from_only_the_packaged_app_copy(tmp_path):
    import shutil
    import subprocess
    import sys
    from pathlib import Path
    from app.gtm import proof

    app = Path(proof.__file__).resolve().parents[1]
    target = tmp_path / "app" / "gtm"
    target.mkdir(parents=True)
    for source, destination in ((app / "__init__.py", target.parent / "__init__.py"),
                                (app / "gtm" / "__init__.py", target / "__init__.py"),
                                (Path(proof.__file__), target / "proof.py")):
        shutil.copyfile(source, destination)
    # Match Core's app-only COPY, without checkout scripts or editable installs.
    result = subprocess.run([sys.executable, "-S", "-m", "app.gtm.proof", "--help"],
                            cwd=tmp_path, env={"PYTHONPATH": str(tmp_path)},
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert "--venture-id" in result.stdout and "--send-reviewed" in result.stdout
    assert result.stderr == ""
