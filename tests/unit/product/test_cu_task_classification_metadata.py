"""Additive persistence/view metadata never changes reviewed execution authority."""
import json
import pytest

from tests.unit.product.test_computer_use_jobs import _client
from tests.unit.product.test_device_orders import orders as cu_orders

orders = cu_orders


@pytest.fixture
def local_client(tmp_path, monkeypatch):
    from tests.unit.test_local_mode import _owner_client

    # shortcut: reuse can seed a hosted SQLite template, enable it here after the harness caches templates by mode.
    monkeypatch.setenv("COS_TEST_APP_REUSE", "0")
    client = _client(tmp_path, monkeypatch)
    with _owner_client(client.app, base_url="http://127.0.0.1", client=("127.0.0.1", 50000)) as owner:
        yield owner


def test_order_class_persists_and_does_not_change_review_or_claim(orders):
    service, caller, manifest = orders
    order = service.prepare("v_one", caller, manifest)
    expected = {"cu_task_class": "browser_public", "cu_task_class_reason": "public_url"}
    assert {key: order[key] for key in expected} == expected
    from app.db.models import VentureDocument
    with service.db()() as session:
        saved = json.loads(session.get(VentureDocument, order["id"]).provenance_json)
    assert {key: saved[key] for key in expected} == expected
    assert {key: service.list_orders("v_one", caller)[0][key] for key in expected} == expected
    assert service.retrieve("v_one", caller, order["id"]) == order
    without_metadata = {key: value for key, value in order.items() if key not in expected}
    assert service._digest(order) == service._digest(without_metadata) == order["review_digest"]
    service.transition("v_one", caller, order["id"], order["review_digest"], "approve")
    claimed = service.transition("v_one", caller, order["id"], order["review_digest"], "claim")
    assert claimed["device_id"] == order["device_id"] == caller.device_id
    assert claimed["manifest"] == manifest.model_dump(exclude_none=True)
    assert {key: claimed[key] for key in expected} == expected


def test_old_orders_have_null_class_without_backfill(orders):
    service, caller, manifest = orders
    order = service.prepare("v_one", caller, manifest)
    from app.db.models import VentureDocument
    with service.db()() as session:
        row = session.get(VentureDocument, order["id"])
        body = json.loads(row.provenance_json)
        del body["cu_task_class"], body["cu_task_class_reason"]
        row.provenance_json = json.dumps(body)
        session.commit()
    assert service.list_orders("v_one", caller)[0]["cu_task_class"] is None
    assert service.list_orders("v_one", caller)[0]["cu_task_class_reason"] is None
    assert service.retrieve("v_one", caller, order["id"])["cu_task_class"] is None
    service.transition("v_one", caller, order["id"], order["review_digest"], "approve")


def test_job_class_persists_in_task_view_and_claim(local_client):
    client = local_client
    response = client.post("/ventures", json={"raw_idea": "A bakery waitlist", "start": False})
    assert response.status_code == 201, response.text
    venture = response.json()["id"]
    from app.build.computer_use import enqueue_computer_use
    from app.db.models import VentureDocument
    from app.state import db

    job = enqueue_computer_use(venture, prompt="Read the public pricing page at https://stripe.com/pricing")
    assert job["cu_task_class"] == "browser_public"
    assert job["cu_task_class_reason"] == "public_url"
    with db()() as session:
        saved = json.loads(session.get(VentureDocument, job["id"]).provenance_json)
    assert saved == job
    assert client.get(f"/ventures/{venture}/computer-use/jobs").json()["items"] == [job]
    claim = client.post(f"/ventures/{venture}/computer-use/jobs/{job['id']}/claim").json()
    assert claim["cu_task_class"] == "browser_public"
    assert claim["prompt"] == job["prompt"]
    assert client.post(f"/ventures/{venture}/computer-use/jobs/{job['id']}/claim").status_code == 409


def test_old_jobs_have_null_class_without_backfill(local_client):
    client = local_client
    response = client.post("/ventures", json={"raw_idea": "A bakery waitlist", "start": False})
    assert response.status_code == 201, response.text
    venture = response.json()["id"]
    from app.build.computer_use import enqueue_computer_use
    from app.db.models import VentureDocument
    from app.state import db

    job = enqueue_computer_use(venture, prompt="Book a Calendar slot")
    with db()() as session:
        row = session.get(VentureDocument, job["id"])
        body = json.loads(row.provenance_json)
        del body["cu_task_class"], body["cu_task_class_reason"]
        row.provenance_json = json.dumps(body)
        session.commit()
    listed = client.get(f"/ventures/{venture}/computer-use/jobs").json()["items"][0]
    assert listed["cu_task_class"] is None
    assert listed["cu_task_class_reason"] is None
    assert client.post(f"/ventures/{venture}/computer-use/jobs/{job['id']}/claim").status_code == 200
