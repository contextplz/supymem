"""Source QA policy cannot authorize a builder or model reservation."""
import copy
import json
import pytest
from tests.company_os_harness import load_company_os_app
from tests.lifecycle_fixtures import runtime


def _policy(monkeypatch):
    load_company_os_app()
    from app.build import subscription_static_policy as policy
    fixture = runtime()
    monkeypatch.setenv('BUILD_QA_FARGATE_PROFILE', json.dumps(fixture['qa_profile']))
    monkeypatch.setenv('BUILD_FARGATE_TASK_PRICING', json.dumps(fixture['task_pricing']))
    monkeypatch.delenv('BUILD_FARGATE_PROFILE', raising=False)
    return policy, policy.configured()


def test_only_qa_compute_is_reserved_without_builder_configuration(monkeypatch):
    policy, quote = _policy(monkeypatch)
    assert quote['model_ceiling_micro'] == quote['builder_compute_ceiling_micro'] == 0
    assert quote['ceiling_micro'] == quote['qa_compute_ceiling_micro'] == 1020
    assert set(quote['task_pricing']) == {'qa'}
    assert 'model_config' not in quote and 'builder_profile' not in quote
    assert policy.validate(quote) == quote


@pytest.mark.parametrize('patch', [
    {'model_ceiling_micro': 1}, {'builder_compute_ceiling_micro': 1},
    {'model_ceiling_micro': False}, {'artifact_ceiling_micro': 1},
    {'model_config': {'model': 'DeepSeek-V4-Pro'}}, {'builder_profile': {}},
    {'ceiling_micro': 1}, {'lifecycle_version': 2}, {'artifact_profile': 'founder_product_v1'},
])
def test_paid_or_conflicting_policy_is_rejected(monkeypatch, patch):
    policy, quote = _policy(monkeypatch)
    with pytest.raises(Exception, match='^subscription_static_qa_policy_invalid$'):
        policy.validate({**copy.deepcopy(quote), **patch})


def test_expired_qa_rate_cannot_admit_source_execution(monkeypatch):
    policy, quote = _policy(monkeypatch)
    quote['task_pricing']['qa']['expires_at'] = '2020-01-01T00:00:00+00:00'
    with pytest.raises(Exception, match='^subscription_static_qa_policy_invalid$'):
        policy.validate(quote)
