"""Adversarial tenant routing and public-source publication contract checks."""
import hashlib

import pytest

from core.artifact_snapshot import canonical
from core.hosting_contracts import (
    HostingRejected, KVS_CAPACITY_BYTES, KVS_HEADROOM_BYTES, RESERVED_SLUGS,
    check_mapping_capacity, host_mapping, public_hostname, public_manifest, public_path,
    valid_slug, validate_mapping,
)


@pytest.mark.parametrize('slug', sorted(RESERVED_SLUGS) + [
    'www.supymem.com', 'studio.', 'xn--studio', 'SUPYMEM', 'studio/../app',
    'stúdio', 'a', '-studio', 'studio-', '1' * 49, '12345', '127-0-0-1',
    'em2631', 'url6828', 'aws-metadata', 'internal-admin', 'supymem-assets',
])
def test_platform_and_noncanonical_hosts_cannot_be_reserved(slug):
    with pytest.raises(HostingRejected):
        valid_slug(slug)


def test_only_the_explicit_environment_domain_is_used():
    assert public_hostname('rehearsal-studio', 'supymem.com') == 'rehearsal-studio.supymem.com'
    assert public_hostname('rehearsal-studio', 'staging.supymem.com') == 'rehearsal-studio.staging.supymem.com'
    with pytest.raises(HostingRejected):
        public_hostname('rehearsal-studio', 'supymem.com.evil.example')
    with pytest.raises(HostingRejected):
        valid_slug('reserved-product', additional_reserved=frozenset({'reserved-product'}))


@pytest.mark.parametrize('path', ['../app/index.html', '/index.html', '.env', 'x/.hidden.html',
    'a//x.css', 'a\\x.css', 'a/%2e%2e/x.js', 'a/./x.css', 'a/../x.css',
    'assets/photo name.png', 'café.html', 'private.zip', 'server.py', 'package.json'])
def test_public_paths_never_escape_prefix_or_publish_source_configuration(path):
    with pytest.raises(HostingRejected):
        public_path(path)


def test_public_manifest_reuses_actual_qa_source_manifest_and_deterministic_mime():
    manifest = [{'path': 'index.html', 'size_bytes': 3, 'sha256': hashlib.sha256(b'abc').hexdigest()}]
    result = public_manifest(manifest)
    assert result['total_bytes'] == 3
    assert result['files'] == [{**manifest[0], 'content_type': 'text/html; charset=utf-8'}]
    assert result['source_manifest_digest'] == hashlib.sha256(canonical(manifest)).hexdigest()
    with pytest.raises(HostingRejected, match='entry_invalid'):
        public_manifest([{**manifest[0], 'path': 'page.html'}])


def mapping():
    return host_mapping(app_id='app_12345678', deployment_id='dep_12345678', generation=3, enabled=True)


def test_mapping_binds_a_single_tenant_and_generation():
    m = mapping()
    assert validate_mapping(canonical(m).decode()) == m
    for key, bad in [('prefix', 'published/app_other/dep_12345678'), ('v', True),
                     ('app', 'app_a/../b'), ('generation', True), ('generation', 0),
                     ('generation', 2**31), ('enabled', 'true')]:
        with pytest.raises(HostingRejected):
            validate_mapping(canonical({**m, key: bad}).decode())


def test_capacity_fails_before_any_provider_work_and_requires_measured_usage():
    m = mapping()
    host = 'studio.supymem.com'
    raw = check_mapping_capacity(host, m, total_bytes=10)
    assert raw == canonical(m)
    with pytest.raises(HostingRejected, match='capacity_exhausted'):
        check_mapping_capacity(host, m, total_bytes=KVS_CAPACITY_BYTES-KVS_HEADROOM_BYTES)
    for usage in (None, True, -1, KVS_CAPACITY_BYTES + 1):
        with pytest.raises(HostingRejected, match='capacity_unknown'):
            check_mapping_capacity(host, m, total_bytes=usage)
    with pytest.raises(HostingRejected, match='capacity_unknown'):
        check_mapping_capacity(host, m, total_bytes=10, replaced_bytes=11)
