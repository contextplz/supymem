"""Filesystem proposals migrate independently of signed effect authority; no I/O."""
import pytest
from coding_agents import cu_ai_loop as loop
from desktop_runtime.device_gateway import WorkOrderGateway


def test_canonical_and_legacy_fs_parse_to_same_execution_fields_without_application_overload():
    canonical = loop.parse_action({'op': 'fs', 'path': '/tmp/reviewed', 'fs_op': 'write_text', 'text': 'contents'})
    legacy = loop.parse_action({'op': 'fs', 'app': '/tmp/reviewed', 'chord': 'write_text', 'text': 'contents'})
    assert canonical == legacy
    assert canonical.app == canonical.chord == ''
    assert (canonical.fs_path, canonical.fs_verb) == ('/tmp/reviewed', 'write_text')
    assert '/tmp/reviewed' not in canonical.brief()
    assert loop.safety_block(loop.parse_action({'op': 'fs', 'path': '/tmp/reviewed', 'fs_op': 'write_text',
                                               'text': '4111 1111 1111 1111'}), goal='g')


@pytest.mark.parametrize('fields', [
    {'path': '/tmp/one', 'app': '/tmp/another', 'fs_op': 'read_text'},
    {'path': '/tmp/one', 'fs_op': 'read_text', 'chord': 'write_text'},
])
def test_conflicting_aliases_refuse_without_selecting_wider_authority(fields):
    with pytest.raises(loop.ActionError, match='action_fs_contract_conflict'):
        loop.parse_action({'op': 'fs', **fields})


@pytest.mark.parametrize('version', [1, 2])
def test_versioned_strict_schema_matches_prompt_and_old_clients_need_no_new_fields(version):
    body = loop.build_request_body('model', 'task', '', action_contract_version=version)
    props = body['text']['format']['schema']['properties']['actions']['items']
    prompt = body['input'][0]['content'][0]['text']
    assert set(props['required']) == set(props['properties'])
    assert props['additionalProperties'] is False
    assert ('path' in props['properties']) is (version == 2)
    assert ('fs_op=' in prompt) is (version == 2)
    assert ('app=reviewed absolute/~ path' in prompt) is (version == 1)


def test_old_in_process_action_constructor_still_projects_same_scoped_runner_arguments():
    action = loop.Action('fs', app='/tmp/reviewed', chord='list')
    assert (action.fs_verb, action.fs_path, action.text) == ('list', '/tmp/reviewed', '')


@pytest.mark.parametrize('version', [0, 4, True, '2'])
def test_unknown_version_rejected_before_provider_request(version):
    with pytest.raises(ValueError, match='action_contract_version_invalid'):
        loop.build_request_body('model', 'task', '', action_contract_version=version)


def test_signed_gateway_selects_version_in_existing_context_without_extra_api_body_field():
    calls = []
    gateway = object.__new__(WorkOrderGateway)
    gateway.path = '/owned'
    gateway.request = lambda method, path, body: calls.append(body) or {'authority': 'proposal_only'}
    gateway.current = lambda **kw: None
    gateway.ai_step('call', 'step', {'elements': [], 'action_contract_version': 999})
    assert set(calls[0]) == {'invocation_id', 'kind', 'context'}
    assert calls[0]['context']['action_contract_version'] == 3
