"""The actual new router requires a request-bound signed human identity."""
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests.unit.simulation.test_rehearse import env as env  # noqa: F401

SECRET = "offline-rehearse-assertion-secret-32-bytes"


def test_signed_route_hides_foreign_scope_and_flag_off(env, monkeypatch):
    from app.api.simulation_api import router
    from core.assertion import sign_assertion
    d = env
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    path = f"/api/v1/ventures/{d['vid']}/simulation/rehearse/quote"
    url = path + "?workspace_id=" + d["wid"]
    body = {"drafts": d["refs"]}
    def headers(actor=d["actor"], role="user", signed_path=path):
        return {"X-COS-Assertion": sign_assertion(SECRET, sub=actor, email="", role=role, path=signed_path, method="POST")}
    assert client.post(url, json=body).status_code == 401
    assert client.post(url, json=body, headers={"X-Subject": d["actor"]}).status_code == 401
    assert client.post(url, json=body, headers=headers(signed_path=path + "-wrong")).status_code == 401
    assert client.post(url, json=body, headers=headers(role="system")).status_code == 403
    assert client.post(url, json=body, headers=headers(actor="usr_foreign")).status_code == 404
    quote = client.post(url, json=body, headers=headers())
    assert quote.status_code == 200 and quote.json()["actor_id"] == d["actor"]
    monkeypatch.setenv("SUPYMEM_SIMULATION_ENABLED", "false")
    assert client.post(url, json=body, headers=headers()).status_code == 404


@pytest.mark.parametrize("field", ["purpose", "body", "candidate", "customer_evidence"])
def test_unreviewed_input_fields_are_rejected(env, monkeypatch, field):
    from app.api.simulation_api import router
    from core.assertion import sign_assertion
    monkeypatch.setenv("COS_INTERNAL_ASSERTION_SECRET", SECRET)
    app = FastAPI()
    app.include_router(router)
    path = f"/api/v1/ventures/{env['vid']}/simulation/rehearse/quote"
    response = TestClient(app).post(path + "?workspace_id=" + env["wid"],
        headers={"X-COS-Assertion": sign_assertion(SECRET, sub=env["actor"], email="", role="user", path=path, method="POST")},
        json={"drafts": env["refs"], field: "not a saved draft"})
    assert response.status_code == 422
