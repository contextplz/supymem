"""Reviewed subscription scope retains the approved landing contract."""
import json
from types import SimpleNamespace

import pytest
from tests.company_os_harness import load_company_os_app
from tests.unit.product.test_device_orders import orders as orders


def test_landing_snapshot_requires_exact_saved_expectations():
    load_company_os_app()
    from app.build import device_orders as svc
    from app.builder.expectations import compile_inline
    expectations = compile_inline({'product_name': 'Landing', 'evidence': [{
        'summary': 'Exact heading visible', 'required_text': ['Landing'], 'actions': []
    }]}, ['Exact heading visible'])
    task = {'key': 'page', 'description': 'Static landing', 'acceptance': ['Exact heading visible'],
            'landing_expectations': expectations}
    doc = {'proposal': {'milestones': [{'key': 'landing', 'tasks': [task]}]},
           'entity_ids': {'tasks': {'landing:page': 'task_landing'}}}
    plan = SimpleNamespace(structured=json.dumps(doc), workspace_id="w_one", venture_id="v_one")
    milestone = SimpleNamespace(kind='landing')
    session = SimpleNamespace(get=lambda model, ident: milestone if ident == 'milestone' else plan,
                              scalars=lambda *_: SimpleNamespace(all=lambda: []))
    snapshot = {'plan_id': 'plan', 'milestone_id': 'milestone', 'task_id': 'task_landing'}
    scoped = svc._coding_snapshot_profile(session, snapshot)
    assert scoped['artifact_profile'] == 'static_landing_v1'
    assert scoped['landing_expectations'] == expectations
    assert snapshot.get('artifact_profile') is None
    task['landing_expectations']['contract']['product_name'] = 'Changed Landing'
    plan.structured = json.dumps(doc)
    assert svc._coding_snapshot_profile(session, snapshot) != scoped
    del task['landing_expectations']
    plan.structured = json.dumps(doc)
    with pytest.raises(ValueError, match='^device_landing_contract_unavailable$'):
        svc._coding_snapshot_profile(session, snapshot)


def test_product_snapshot_stays_product_scoped():
    load_company_os_app()
    from app.build import device_orders as svc
    session = SimpleNamespace(get=lambda *args: SimpleNamespace(kind='core'))
    snapshot = {'plan_id': 'plan', 'milestone_id': 'core', 'task_id': 'task_core'}
    assert svc._coding_snapshot_profile(session, snapshot) == snapshot


def test_reviewed_order_claim_refuses_changed_saved_contract(orders, monkeypatch):
    # The durable order/approve/claim gate is real; only the plan lookup is an
    # isolated approved-plan fixture, computed through the production helper.
    svc, caller, _manifest = orders
    from app.builder.expectations import compile_inline
    contract = compile_inline({'product_name': 'Landing', 'evidence': [{
        'summary': 'Exact heading visible', 'required_text': ['Landing'], 'actions': []
    }]}, ['Exact heading visible'])
    task = {'key': 'page', 'description': 'Static landing', 'acceptance': ['Exact heading visible'],
            'landing_expectations': contract}
    doc = {'proposal': {'milestones': [{'key': 'landing', 'tasks': [task]}]},
           'entity_ids': {'tasks': {'landing:page': 'task_one'}}}
    plan = SimpleNamespace(structured=json.dumps(doc), workspace_id="w_one", venture_id="v_one")
    session = SimpleNamespace(get=lambda model, ident: SimpleNamespace(kind='landing')
                              if ident == 'milestone' else plan,
                              scalars=lambda *_: SimpleNamespace(all=lambda: []))
    base = {'plan_id': 'approved_plan', 'milestone_id': 'milestone', 'task_id': 'task_one'}
    monkeypatch.setattr(svc, '_task_snapshot', lambda *args, **kwargs:
                        svc._coding_snapshot_profile(session, base))
    manifest = svc.Manifest(task_id='task_one', title='Build reviewed landing with Codex',
        instruction='Build the exact approved static landing contract',
        applications=['com.companyos.app'], origins=[], actions=['editor.edit', 'terminal.execute'],
        credit_ceiling_micro=1000, duration_seconds=60, coding_verification={
            'operation': 'workspace_edit_v1', 'provider': 'codex', 'workspace_slug': 'landing',
            'read_scope': 'device_filesystem', 'write_scope': 'venture_workspace',
            'command_network': 'disabled', 'offline_profile': 'static_html_v1'})
    order = svc.prepare('v_one', caller, manifest)
    svc.transition('v_one', caller, order['id'], order['review_digest'], 'approve')
    task['landing_expectations']['contract']['product_name'] = 'Changed Landing'
    plan.structured = json.dumps(doc)
    with pytest.raises(ValueError, match='^device_order_task_changed$'):
        svc.transition('v_one', caller, order['id'], order['review_digest'], 'claim')


def test_static_landing_prompt_tells_provider_where_private_checks_go():
    # Live Codex run (2026-10-05) wrote checks/*.py and import failed with
    # subscription_static_non_public_file; export only omits review/ evidence.
    from pathlib import Path
    from app.build.subscription_static_export import private_path
    source = (Path(__file__).parents[3] / 'services/company_os/app/build/device_orders.py').read_text()
    assert 'put any check script, note or log only under review/' in source
    assert private_path('review/check_page.py') and not private_path('checks/check_page.py')
