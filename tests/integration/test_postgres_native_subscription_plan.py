"""Migrated plan approval keeps native source scope separate from paid builds."""
import json
import copy
import pytest

from tests.integration.test_postgres_auth_security import pg_database as pg_database
from tests.integration.test_postgres_journey_jobs import journey_db as journey_db
from tests.integration.test_postgres_planning import planning as planning, run


@pytest.mark.parametrize("count", [1, 2])
def test_paid_builder_pause_does_not_exclude_reviewed_native_landing(planning, monkeypatch, count):
    from tests.planning_fixtures import install_planner, proposal
    from app.planning import compiler, execution
    from app.build import device_orders
    from app.db.models import Job
    from app.db.supymem_models import PlanMilestone
    d = planning
    def native(context, _):
        authored = proposal(context)
        authored['milestones'][0]['tasks'][0]['capability_id'] = 'coding.subscription_static'
        if count == 2:
            second = copy.deepcopy(authored['milestones'][0]['tasks'][0])
            second.update(key='t2', title='Prepare a separate reviewed landing draft')
            authored['milestones'][0]['tasks'].append(second)
        return authored
    install_planner(monkeypatch, native)
    with d['factory']() as session:
        session.get(Job, d['job_id']).priority = -100
        session.commit()
    assert run(d).id == d['job_id']
    plan = compiler.get_plan(execution.identifier(d['job_id']))
    assert plan['milestones'][0]['scope'] == 'conditional'
    compiler.approve_plan(plan['id'], person_id=d['actor'], review_token=plan['review_token'])
    tid = plan['milestones'][0]['tasks'][0]['id']
    with d['factory']() as session:
        if count == 2:
            with pytest.raises(ValueError, match='^device_coding_task_ambiguous$'):
                device_orders._coding_task_snapshot(session, d['wid'], d['vid'], 'Build my landing page with Codex')
            return
        snapshot = device_orders._coding_task_snapshot(session, d['wid'], d['vid'], 'Build my landing page with Codex')
        assert snapshot['task_id'] == tid and snapshot['artifact_profile'] == 'static_landing_v1'
        assert json.loads(snapshot['tool_hints']) == ['coding.subscription_static']
        assert session.get(PlanMilestone, snapshot['milestone_id']).scope == 'conditional'
