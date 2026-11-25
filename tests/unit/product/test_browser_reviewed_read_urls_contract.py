"""Pure DTO/wire checks for exact reviewed read URLs; no browser, database, or provider."""
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from tests.company_os_harness import load_company_os_app

load_company_os_app()
from app.build.device_orders import GeneralBrowserVerification, Manifest  # noqa: E402
from coding_agents.owned_browser import reviewed_read_url_origin  # noqa: E402
from core.browser_qualification_catalog import PUBLIC_RESEARCH_READ_URLS  # noqa: E402


def manifest():
    profile = json.loads((Path(__file__).parents[3] / 'config/browser_cuai_profile.json').read_text())
    return {'task_id': 'founder_browser_v_one', 'title': 'Public sources', 'instruction': 'Read public source metadata.',
        'applications': ['com.google.Chrome'], 'origins': ['https://pypi.org', 'https://github.com'],
        'actions': ['browser.read'], 'credit_ceiling_micro': 500_000, 'duration_seconds': 900,
        'general_browser_verification': {'operation': 'general_goal_v1', 'start_url': 'https://pypi.org/',
            'allow_type': False, 'max_steps': 32, 'fill_values': {}, 'execution_profile': profile}}


def test_six_canonical_urls_roundtrip_and_empty_default_preserves_old_wire():
    value = manifest()
    parsed = Manifest.model_validate(value)
    assert parsed.general_browser_verification.read_urls == []
    assert 'read_urls' not in parsed.model_dump(exclude_none=True)['general_browser_verification']
    value['general_browser_verification']['read_urls'] = list(PUBLIC_RESEARCH_READ_URLS)
    parsed = Manifest.model_validate(value)
    assert parsed.model_dump(exclude_none=True)['general_browser_verification']['read_urls'] == list(PUBLIC_RESEARCH_READ_URLS)
    assert all(reviewed_read_url_origin(url) in value['origins'] for url in PUBLIC_RESEARCH_READ_URLS)


@pytest.mark.parametrize('url', ['HTTPS://pypi.org/project/requests/', 'https://PyPI.org/project/requests/',
    'https://pypi.org', 'https://pypi.org:443/project/requests/', 'https://pypi.org/project/../requests/',
    'https://pypi.org/project/%2e%2e/requests/', 'https://pypi.org/project/caf\u00e9/',
    'http://pypi.org/project/requests/', 'https://name:password@pypi.org/project/requests/',
    'https://pypi.org/project/requests/?', 'https://pypi.org/project/requests/#'])
def test_noncanonical_or_unsafe_literal_refuses_before_paid_execution(url):
    with pytest.raises(PermissionError):
        reviewed_read_url_origin(url)
    with pytest.raises(ValidationError):
        GeneralBrowserVerification(start_url='https://pypi.org/', read_urls=[url])


@pytest.mark.parametrize('change', ['duplicate', 'unreviewed_origin', 'interact', 'no_profile', 'type', 'fills'])
def test_exact_read_urls_do_not_create_new_origin_profile_or_mutation_authority(change):
    value = manifest()
    general = value['general_browser_verification']
    general['read_urls'] = list(PUBLIC_RESEARCH_READ_URLS)
    if change == 'duplicate':
        general['read_urls'] = [PUBLIC_RESEARCH_READ_URLS[0]] * 2
    elif change == 'unreviewed_origin':
        general['read_urls'] = ['https://unreviewed.example/page']
    elif change == 'interact':
        value['actions'] = ['browser.read', 'browser.interact']
    elif change == 'no_profile':
        general['execution_profile'] = None
    elif change == 'type':
        general['allow_type'] = True
    else:
        general['fill_values'] = {'name': 'Ada'}
    with pytest.raises(ValidationError):
        Manifest.model_validate(value)
