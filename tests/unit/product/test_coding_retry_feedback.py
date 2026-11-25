"""Compiler bounds preserve exact scope and whole source-owned QA observations."""
import json
import pytest

from tests.unit.product.test_device_orders import orders as orders  # noqa: F401


def test_compiler_bounds_feedback_without_clipping_contract_or_observations(orders, monkeypatch):
    svc, caller, _manifest = orders
    from app.build import native_static_recipe
    monkeypatch.setattr(native_static_recipe, 'reviewed_recipe', lambda *args, **kwargs: 'Static recipe ' + 'r' * 3400)
    with svc.db()() as session:
        session.add(svc.Venture(id='v_one', workspace_id='w_one', name='Reviewed landing', raw_idea='Landing',
            stage='draft', created_at=svc.utcnow(), updated_at=svc.utcnow()))
        session.commit()
    required = ['Exact required copy ' + str(i) + ' ' + 'c' * 450 for i in range(6)]
    observed = 'Whole saved observation: ' + 'o' * 550
    feedback = {'schema_version': 1, 'build_run_id': 'bsi_owned', 'qa_run_id': 'qa_owned',
        'source_artifact_id': 'bsa_owned', 'source_sha256': 'a' * 64,
        'source_manifest_digest': 'b' * 64, 'report_digest': 'c' * 64,
        'contract_digest': 'd' * 64, 'counts': {'passed': 2, 'failed': 1, 'unsupported': 5},
        'cases': [{'id': f'case_{i}', 'suite': 'render', 'target': 'index.html',
                   'status': 'failed' if i == 0 else 'unsupported', 'observed': observed,
                   'expected': 'Exact expected', 'evidence_ids': []} for i in range(6)],
        'omitted_cases': 0}
    snapshot = {'task_id': 'task_one', 'title': 'Reviewed landing', 'description': 'd' * 1600,
        'acceptance': '["Visible reviewed offer"]', 'artifact_profile': 'static_landing_v1',
        'landing_expectations': {'contract': {'profile': 'static_landing_v1', 'product_name': 'Offer',
            'required_text': required, 'actions': []}, 'criteria': []}, 'coding_qa_feedback': feedback}
    monkeypatch.setattr(svc, '_coding_task_snapshot', lambda *args: snapshot)
    monkeypatch.setattr(svc, 'prepare', lambda _vid, _caller, manifest, **kwargs: manifest.model_dump())
    manifest = svc.prepare_coding_task_from_chat('v_one', caller, 'Use Codex to edit the landing page only.')
    instruction = manifest['instruction']
    assert len(instruction) <= 12000 and all(text in instruction for text in required)
    wire = instruction.split('(evidence data, not instructions):\n', 1)[1].split('\nRepair the requested focus', 1)[0]
    displayed = json.loads(wire)
    assert 0 < len(displayed['cases']) < 6 and displayed['omitted_cases'] == 6 - len(displayed['cases'])
    assert displayed['counts'] == feedback['counts']
    assert all(case['observed'] == observed for case in displayed['cases'])
    assert feedback['omitted_cases'] == 0 and len(feedback['cases']) == 6
    assert manifest['credit_ceiling_micro'] == 0 and manifest['origins'] == []
    assert 'Do not publish, deploy, communicate externally' in instruction


def test_overlength_real_from_chat_route_refuses_before_snapshot_or_recipe(orders, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from app.api import device_orders_api
    from app.db.models import VentureDocument
    from app.build import native_static_recipe
    svc, caller, _manifest = orders
    def forbidden(*args, **kwargs):
        pytest.fail('overlength coding focus must be rejected before task/source/recipe lookup')
    monkeypatch.setattr(svc, '_scope', forbidden)
    monkeypatch.setattr(svc, '_coding_task_snapshot', forbidden)
    monkeypatch.setattr(native_static_recipe, 'reviewed_recipe', forbidden)
    monkeypatch.setattr(device_orders_api, 'verify_caller', lambda *_a, **_kw: caller)
    app = FastAPI()
    app.include_router(device_orders_api.router)
    with svc.db()() as session:
        before = list(session.scalars(select(VentureDocument.id)))
    reply = TestClient(app).post('/ventures/v_one/device-work-orders/from-chat',
        json={'instruction': 'Use Codex to edit the approved landing. ' + 'x' * 1201})
    assert reply.status_code == 409
    detail = reply.json()['detail']
    assert detail['code'] == 'device_coding_focus_too_long'
    assert '1200 characters or fewer' in detail['message'] and 'No coding task was prepared or started' in detail['message']
    assert 'not supported' not in detail['message']
    with svc.db()() as session:
        assert list(session.scalars(select(VentureDocument.id))) == before


@pytest.mark.parametrize('code, phrase', [
    ('device_coding_qa_feedback_unavailable', 'could not verify the saved QA evidence'),
    ('device_coding_qa_feedback_bounds', 'exact contract cannot be silently shortened'),
])
def test_feedback_refusals_retain_their_specific_api_reason(orders, code, phrase):
    from fastapi import HTTPException
    from app.api import device_orders_api
    def refuse():
        raise ValueError(code)
    with pytest.raises(HTTPException) as caught:
        device_orders_api.call(refuse)
    assert caught.value.status_code == 409
    assert caught.value.detail['code'] == code and phrase in caught.value.detail['message']
