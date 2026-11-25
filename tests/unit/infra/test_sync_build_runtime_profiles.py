import json

import pytest

from tests.company_os_harness import ROOT

if not (ROOT / "scripts/infra/sync_build_runtime_profiles.py").is_file():
    pytest.skip("Hosted runtime deployment tooling is not in the public local release.", allow_module_level=True)

from scripts.infra.sync_build_runtime_profiles import _replace_profiles

pytestmark = pytest.mark.hosted_only


def _profile(arn: str, image: str) -> dict:
    return {"task_definition_arn": arn, "image_uri": image, "region": "us-east-1"}


def test_replace_profiles_promotes_new_runtime_and_retains_prior_once():
    builder_old = _profile("arn:old:builder:6", "repo/builder@sha256:old")
    qa_old = _profile("arn:old:qa:4", "repo/qa@sha256:old")
    env = {
        "BUILD_FARGATE_PROFILE": json.dumps(builder_old),
        "BUILD_FARGATE_COMPAT_PROFILES": json.dumps([builder_old]),
        "BUILD_QA_FARGATE_PROFILE": json.dumps(qa_old),
        "BUILD_QA_FARGATE_COMPAT_PROFILES": "[]",
    }
    updated = _replace_profiles(
        env,
        {
            "managed-builder": ("arn:new:builder:7", "repo/builder@sha256:new"),
            "static-qa": ("arn:new:qa:5", "repo/qa@sha256:new"),
        },
    )
    assert json.loads(updated["BUILD_FARGATE_PROFILE"])["task_definition_arn"] == "arn:new:builder:7"
    assert json.loads(updated["BUILD_QA_FARGATE_PROFILE"])["task_definition_arn"] == "arn:new:qa:5"
    assert json.loads(updated["BUILD_FARGATE_COMPAT_PROFILES"]) == [builder_old]
    assert json.loads(updated["BUILD_QA_FARGATE_COMPAT_PROFILES"]) == [qa_old]


def test_replace_profiles_keeps_only_three_compatibility_records():
    primary = _profile("arn:builder:6", "repo/builder@sha256:6")
    env = {
        "BUILD_FARGATE_PROFILE": json.dumps(primary),
        "BUILD_FARGATE_COMPAT_PROFILES": json.dumps(
            [_profile(f"arn:builder:{n}", f"repo/builder@sha256:{n}") for n in (5, 4, 3)]
        ),
        "BUILD_QA_FARGATE_PROFILE": json.dumps(_profile("arn:qa:4", "repo/qa@sha256:4")),
        "BUILD_QA_FARGATE_COMPAT_PROFILES": "[]",
    }
    updated = _replace_profiles(
        env,
        {
            "managed-builder": ("arn:builder:7", "repo/builder@sha256:7"),
            "static-qa": ("arn:qa:5", "repo/qa@sha256:5"),
        },
    )
    compat = json.loads(updated["BUILD_FARGATE_COMPAT_PROFILES"])
    assert [row["task_definition_arn"] for row in compat] == [
        "arn:builder:6",
        "arn:builder:5",
        "arn:builder:4",
    ]


def test_missing_runtime_manifest_cannot_be_promoted(monkeypatch):
    import pytest
    from scripts.infra import sync_build_runtime_profiles as module
    arn = 'arn:aws:ecs:us-east-1:123456789012:task-definition/selfhost-os-prod-build-run:11'
    digest = 'sha256:' + 'a' * 64
    image = '123456789012.dkr.ecr.us-east-1.amazonaws.com/selfhost-os/agent-server@' + digest
    response = {'images': [], 'failures': [{'failureCode': 'ImageNotFound'}]}
    def fake(service, operation, **payload):
        if service == 'ecs':
            return {'taskDefinition': {'status': 'ACTIVE', 'taskDefinitionArn': arn,
                'containerDefinitions': [{'name': 'managed-builder', 'image': image}]}}
        assert service == 'ecr' and payload['repositoryName'] == 'selfhost-os/agent-server'
        assert payload['imageIds'] == [{'imageDigest': digest}]
        return response
    monkeypatch.setattr(module, 'aws', fake)
    with pytest.raises(module.ReleaseError, match='unavailable in ECR'):
        module._task(arn, 'managed-builder')
    response = {'images': [{'imageId': {'imageDigest': digest}, 'imageManifest': '{}'}]}
    assert module._task(arn, 'managed-builder')[1] == image
    # ECR returns the same digest once for every tag alias.
    response['images'].append({'imageId': {'imageDigest': digest, 'imageTag': 'main'}, 'imageManifest': '{}'})
    assert module._task(arn, 'managed-builder')[1] == image
    response['images'][1]['imageManifest'] = 'different'
    with pytest.raises(module.ReleaseError, match='unavailable in ECR'):
        module._task(arn, 'managed-builder')
