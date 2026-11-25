"""Console build cost controls retain digest rollout and runtime chunk continuity."""
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]


def workflow():
    return yaml.safe_load((ROOT / '.github/workflows/hotfix-console.yml').read_text())


def test_manual_console_rolls_serialize_without_canceling_active_or_pending_runs():
    spec = workflow()
    assert spec['concurrency'] == {
        'group': 'production-console-hotfix', 'cancel-in-progress': False, 'queue': 'max'}
    assert 'github.ref' not in spec['concurrency']['group']
    job = spec['jobs']['build-push-roll']
    assert job['environment'] == 'production'
    steps = job['steps']
    build = next(s for s in steps if s.get('name') == 'Build and push console')
    assert build.get('continue-on-error') is not True and 'if' not in build
    assert build['with']['context'] == 'apps/console'
    assert build['with']['push'] is True and build['with']['platforms'] == 'linux/amd64'
    assert build['with']['tags'].strip().endswith(':${{ github.sha }}')
    deploy = next(s for s in steps if s.get('name') == 'Deploy the built Console digest')
    assert '@${{ steps.image.outputs.digest }}' in deploy['env']['CONSOLE_IMAGE']
    assert '--image "$CONSOLE_IMAGE" --apply' in deploy['run']
    assert deploy.get('continue-on-error') is not True
    stable = next(s for s in steps if s.get('name') == 'Wait until console is stable')
    assert 'aws ecs wait services-stable' in stable['run']
    assert stable.get('continue-on-error') is not True


def test_cache_export_is_bounded_nonfatal_but_cannot_skip_build_or_rollout():
    steps = workflow()['jobs']['build-push-roll']['steps']
    args = next(s for s in steps if s.get('name') == 'Build and push console')['with']
    scope = 'hotfix-console-amd64-v1'
    assert args['cache-from'] == f'type=gha,version=2,scope={scope},timeout=2m'
    assert args['cache-to'] == f'type=gha,version=2,scope={scope},mode=max,ignore-error=true,timeout=2m'
    assert 'NEXT_PUBLIC_GATEWAY_URL=https://gateway.supymem.com' in args['build-args']
    assert args['file'] == 'apps/console/Dockerfile'


def test_runtime_chunks_do_not_invalidate_builder_inputs_or_override_current_chunks():
    recipe = (ROOT / 'apps/console/Dockerfile').read_text()
    assert recipe.startswith('# syntax=docker/dockerfile:1.19\n')
    builder = recipe.split(' AS builder\n', 1)[1].split(' AS runner\n', 1)[0]
    assert 'COPY --exclude=carry-static . .' in builder
    assert '\nCOPY . .\n' not in builder
    assert 'RUN pnpm build' in builder
    assert 'COPY package.json pnpm-lock.yaml pnpm-workspace.yaml ./' in recipe
    assert 'RUN pnpm install --frozen-lockfile' in recipe
    runner = recipe.split(' AS runner\n', 1)[1]
    old = 'COPY --chown=nextjs:nodejs carry-static ./.next/static'
    current = 'COPY --from=builder --chown=nextjs:nodejs /app/.next/static ./.next/static'
    assert runner.index(old) < runner.index(current)
    assert 'USER nextjs' in runner and 'HEALTHCHECK --interval=10s' in runner
    # Exclude from the builder COPY only: global ignore would lose old chunks.
    assert 'carry-static' not in (ROOT / 'apps/console/.dockerignore').read_text()
    carry = next(s for s in workflow()['jobs']['build-push-roll']['steps']
                 if s.get('name') == "Carry forward previous release's static chunks")
    assert 'docker cp "$id:/app/.next/static/." apps/console/carry-static/' in carry['run']
    assert '-mtime +30 -delete' in carry['run']
