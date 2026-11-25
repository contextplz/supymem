"""QA-only actual OwnedBrowser/Jev worker contracts; no installed/account proof.

The Core account/order/effect service is an explicit owned fake. The actual
WorkOrderGateway, DeviceWorker, OwnedBrowser, Jev request/parser and direct
provider transport are used unchanged. Loopback is the existing test-only
browser option. General DONE remains review-required, never task verification.
"""
from __future__ import annotations

import argparse
import base64
import copy
from datetime import datetime, timezone
import hashlib
import html
import json
import os
from pathlib import Path
import random
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[2]
for path in (ROOT / 'packages', ROOT / 'services/company_os', ROOT / 'scripts/desktop'):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from app.build import device_jev as core_jev  # noqa: E402
from coding_agents import jev_decision  # noqa: E402
from desktop_runtime.device_gateway import WorkOrderGateway  # noqa: E402
from desktop_runtime import device_worker as worker_module  # noqa: E402
from desktop_runtime.owned_processes import OwnedProcesses  # noqa: E402
from cu_browser_suite import Fx, TASKS  # noqa: E402

CASES = ('navigation_2', 'navigation_3', 'navigation_4', 'navigation_6',
         'heldout_navigation_7', 'general_done_review', 'login_creds', 'captcha_wall')
UNSUPPORTED = {'fact_answers': 'worker_has_no_answer_return_contract',
               'form_commits': 'owned_browser_blocks_post',
               'downloads': 'owned_browser_withholds_downloads'}
LIVE_RUN_ADMITTED = False


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def source_receipt():
    from build_runtime import source_digest as runtime_input_digest
    paths = ['packages/coding_agents/owned_browser.py', 'packages/coding_agents/jev_decision.py',
             'packages/coding_agents/browser_pause.py', 'packages/desktop_runtime/device_worker.py',
             'packages/desktop_runtime/device_gateway.py', 'packages/desktop_runtime/owned_processes.py',
             'services/company_os/app/build/device_jev.py', 'scripts/desktop/cu_browser_suite.py',
             'scripts/desktop/cu_tasks/fixture_server.py', 'scripts/desktop/cu_jev_suite.py',
             'scripts/desktop/cu_jev_provider_budget.py']
    files = {p: hashlib.sha256((ROOT / p).read_bytes()).hexdigest() for p in paths}
    inputs = runtime_input_digest()
    return {'source_sha256': digest({'seams': files, 'runtime_inputs': inputs}),
            'runtime_input_sha256': inputs, 'seam_files': files}


class NavigationFixture(Fx):
    def __init__(self, uid, pages):
        super().__init__(uid)
        rng = random.Random(uid)
        self.paths = ['/'] + [f'/qa/{uuid4().hex[:12]}' for _ in range(pages - 1)]
        self.titles = [f'{rng.choice(["Harbor", "Maple", "Orchid", "Cedar"])} {rng.randrange(10000, 99999)}'
                       for _ in self.paths]
        self.layout = rng.randrange(3)
        self.forward_first = rng.choice([True, False])
        self.qualified_pages = []
        self.failed_paths = set()
        self.blank_paths = set()

    def get(self, h, path, query):
        if path not in self.paths and not path.startswith('/qa/decoy/'):
            return super().get(h, path, query)
        if path.startswith('/qa/decoy/'):
            return h.page('Reference', '<h1>Reference page</h1><a href="/">Return to directory</a>')
        if path in self.failed_paths:
            return h.page('Unavailable', '<h1>Page unavailable</h1>', 404)
        if path in self.blank_paths:
            return h.out(200, '')
        index = self.paths.index(path)
        links = [(f'/qa/decoy/{index}-{n}', f'Reference {index}-{n}') for n in range(2)]
        if index + 1 < len(self.paths):
            forward = (self.paths[index + 1], self.titles[index + 1])
            links.insert(0 if self.forward_first else len(links), forward)
        body = ''.join(f'<a href="{html.escape(url)}">{html.escape(label)}</a>' for url, label in links)
        if self.layout == 1:
            body = '<nav>' + body + '</nav>'
        elif self.layout == 2:
            body = '<ul>' + ''.join(f'<li><a href="{url}">{label}</a></li>' for url, label in links) + '</ul>'
        result = h.page(self.titles[index], f'<h1>{html.escape(self.titles[index])}</h1>{body}')
        with self.lock:
            self.qualified_pages.append(path)
        return result


class PrivateCalls:
    """Explicit owned-fixture payloads only. No headers/env/provider credentials."""
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(mode=0o700, parents=False, exist_ok=False)
        self.calls = []
        self.bytes = 0

    def write(self, value):
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode()
        if len(raw) > 150000 or self.bytes + len(raw) > 20000000 or len(self.calls) >= 232:
            raise ValueError('qa_private_capture_bounds')
        name = f'{len(self.calls) + 1:04d}-{uuid4().hex}.json'
        fd = os.open(self.directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(fd, 'wb') as out:
                out.write(raw)
                out.flush()
                os.fsync(out.fileno())
                os.fchmod(out.fileno(), 0o400)
        except BaseException:
            raise
        item = {'file': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'size_bytes': len(raw)}
        self.calls.append(item)
        self.bytes += len(raw)
        return item

    def seal(self):
        raw = json.dumps({'schema_version': 1, 'calls': self.calls}, sort_keys=True).encode()
        fd = os.open(self.directory / 'manifest.tmp', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
            os.fchmod(out.fileno(), 0o400)
        os.replace(self.directory / 'manifest.tmp', self.directory / 'manifest.json')
        os.chmod(self.directory, 0o500)
        return hashlib.sha256(raw).hexdigest()


class GuardedProvider:
    """Capture forwarded actual chunks; production transport/parser are unchanged."""
    def __init__(self, budget, recorder, *, client_factory=None):
        self.budget, self.recorder = budget, recorder
        self.client_factory = client_factory or core_jev.httpx.Client
        self.receipts = []
        self.scopes = {}

    def bind(self, trial_id, fixture, goal):
        if (not isinstance(fixture, Fx) or fixture.httpd is None or not fixture.base
                or trial_id in self.scopes or not isinstance(goal, str)):
            raise ValueError('qa_capture_fixture_scope_invalid')
        self.scopes[trial_id] = (fixture, goal, fixture.base)

    def call(self, body, *, trial_id):
        exact = json.loads(json.dumps(body))
        scope = self.scopes.get(trial_id)
        location = urlsplit(str(exact.get('state', {}).get('location', '')))
        if (scope is None or scope[0].httpd is None or scope[0].base != scope[2]
                or exact.get('state', {}).get('goal') != scope[1]
                or location.scheme + '://' + location.netloc != scope[2]
                or location.username is not None or location.password is not None):
            raise ValueError('qa_capture_fixture_scope_changed')
        ticket = self.budget.reserve(exact)
        raw = bytearray()
        status = None
        complete = False
        factory = self.client_factory

        class Client:
            def __init__(self, **kwargs):
                self.client = factory(**kwargs)
            def __enter__(self):
                self.client.__enter__()
                return self
            def __exit__(self, *args):
                return self.client.__exit__(*args)
            def stream(self, method, endpoint, **kwargs):
                if method != 'POST' or endpoint != core_jev.ENDPOINT or kwargs.get('json') != exact:
                    raise ValueError('qa_provider_request_changed')
                context = self.client.stream(method, endpoint, **kwargs)

                class Stream:
                    def __enter__(self):
                        nonlocal status
                        response = context.__enter__()
                        status = response.status_code
                        self.response = response
                        return self
                    @property
                    def status_code(self):
                        return self.response.status_code
                    def iter_bytes(self):
                        nonlocal complete
                        for chunk in self.response.iter_bytes():
                            if len(raw) + len(chunk) > core_jev.MAX_RESPONSE_BYTES:
                                raise ValueError('qa_provider_response_bounds')
                            raw.extend(chunk)
                            yield chunk
                        complete = True
                    def __exit__(self, *args):
                        return context.__exit__(*args)
                return Stream()

        invocation_id = uuid4().hex
        error_code = None
        confirmed = False
        try:
            with patch.object(core_jev, 'httpx', SimpleNamespace(Client=Client, Timeout=core_jev.httpx.Timeout)):
                value = core_jev._provider_call(exact)
            confirmed = self.budget.settle(ticket, bytes(raw), http_status=status, transport_complete=complete)
            if not confirmed:
                raise ValueError('qa_provider_usage_unconfirmed')
            return value
        except BaseException:
            self.budget.hold(ticket)
            error_code = 'qa_provider_unconfirmed'
            raise
        finally:
            receipt = self.recorder.write({'schema_version': 1, 'trial_id': trial_id,
                'invocation_id': invocation_id, 'request': exact,
                'response_bytes_base64': base64.b64encode(raw).decode('ascii'),
                'response_sha256': hashlib.sha256(raw).hexdigest(),
                'http_status': status, 'transport_complete': complete,
                'usage_confirmed': confirmed, 'error_code': error_code})
            self.receipts.append({'trial_id': trial_id, 'invocation_id': invocation_id,
                                  'usage_confirmed': confirmed, 'payload_sha256': receipt['sha256']})


class FakeAuthority:
    """Owned QA authority only: never claims production Core admission coverage."""
    def __init__(self, manifest, provider, trial_id):
        self.manifest, self.provider, self.trial_id = manifest, provider, trial_id
        self.state = 'approved'
        self.review_digest = digest(manifest)
        self.effects, self.decisions, self.stop = {}, [], None
        self.order = 'dwo_' + uuid4().hex
        self.execution = 'dex_' + uuid4().hex

    def saved(self):
        return {'id': self.order, 'venture_id': 'ven_qa', 'device_id': 'dev_qa',
                'state': self.state, 'review_digest': self.review_digest,
                'execution_id': self.execution, 'manifest': self.manifest}

    def request(self, method, path, body):
        base = f'/api/ventures/ven_qa/device-work-orders/{self.order}'
        if method == 'GET' and path == base:
            return self.saved()
        if method == 'GET' and path == base + '/capabilities':
            return {'available': True}
        if method == 'GET' and path == base + '/authority':
            return {'current': self.state == 'claimed'}
        if method == 'POST' and path == base + '/claim':
            if self.state != 'approved' or body != {'review_digest': self.review_digest}:
                raise ValueError('qa_claim_replay_or_scope')
            self.state = 'claimed'
            return self.saved()
        if method == 'POST' and path == base + '/browser-decision':
            observation = jev_decision.Observation.model_validate(body['observation'])
            location = urlsplit(observation.location)
            if (observation.goal != self.manifest['instruction']
                    or location.scheme + '://' + location.netloc != self.manifest['origins'][0]):
                raise ValueError('qa_decision_scope_changed')
            request = jev_decision.request(observation)
            value = self.provider.call(request, trial_id=self.trial_id)
            core_jev._actual(value)
            candidate = core_jev._candidate(value, observation,
                has_browser_verifier=bool(self.manifest.get('browser_verification')),
                reviewed_type=False)
            self.decisions.append({'invocation_id': body['invocation_id'],
                'observation_sha256': observation.digest(), 'operation': candidate.operation})
            return {'authority': 'proposal_only', 'candidate': vars(candidate)}
        if method == 'POST' and path == base + '/effects/admit':
            if self.state != 'claimed' or body['invocation_id'] in self.effects:
                return {'admitted_now': False}
            self.effects[body['invocation_id']] = {'digest': body['effect_digest'], 'outcome': None}
            return {'admitted_now': True}
        if method == 'POST' and path == base + '/effects/receipt':
            record = self.effects[body['invocation_id']]
            if body['effect_digest'] != record['digest'] or record['outcome'] is not None:
                raise ValueError('qa_effect_receipt_scope')
            record['outcome'] = body['outcome']
            return {'recorded': True}
        if method == 'POST' and path == base + '/stop-receipt':
            self.stop = dict(body)
            self.state = 'stopped' if body['processes_stopped'] else 'reconciliation_required'
            return {'state': self.state}
        raise ValueError('qa_authority_route_unsupported')


def ordered_paths(expected, actual):
    position = 0
    for path in actual:
        if position < len(expected) and path == expected[position]:
            position += 1
    return position == len(expected)


def normalize_observed_url(value):
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path,
        urlencode([(key, item) for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                   if key != 'supymem_check']), parsed.fragment))


def qualified_locations(receipts):
    """Independently check closed source metadata, without another observation."""
    locations = []
    for item in receipts:
        proof = item.get('load') or {}
        response = proof.get('response') or {}
        if (proof.get('qualified') is True and proof.get('visible_text') is True
                and proof.get('ready_state') == 'complete' and response.get('complete') is True
                and type(response.get('status')) is int and 200 <= response['status'] < 300
                and response['status'] not in {204, 205}
                and response.get('url') == proof.get('location') == item.get('location')
                and proof.get('observation_digest') == item.get('observation_digest')
                and type(proof.get('sequence')) is int and proof['sequence'] > 0
                and response.get('sequence') == proof['sequence']
                and isinstance(proof.get('document_id'), str) and proof['document_id']):
            locations.append(normalize_observed_url(item['location']))
    return locations


def run_case(case, repetition, provider, *, source):
    pages = int(case.rsplit('_', 1)[-1]) if case.startswith(('navigation_', 'heldout_navigation_')) else 2
    fixture = NavigationFixture(uuid4().hex[:12], pages)
    try:
        fixture.start()
        return _run_case_open_fixture(case, repetition, provider, source=source, fixture=fixture)
    finally:
        if fixture.httpd is not None and fixture.thread is not None and not fixture.thread.is_alive():
            fixture.httpd.server_close()
            fixture.httpd = None
        else:
            fixture.close()


def _run_case_open_fixture(case, repetition, provider, *, source, fixture):
    trial_id = uuid4().hex
    if case in {'login_creds', 'captcha_wall'}:
        task = next(t for t in TASKS if t.id == case)
        goal = task.task.format(base=fixture.base)
        start = '/login' if case == 'login_creds' else '/captcha'
    else:
        goal = 'Visit these pages in order: ' + ', then '.join(fixture.titles) + '. Stop on the final page.'
        start = '/'
    general = case in {'general_done_review', 'login_creds', 'captcha_wall'}
    manifest = {'title': 'Owned QA browser contract', 'instruction': goal,
        'applications': ['com.google.Chrome'], 'origins': [fixture.base],
        'actions': ['browser.read', 'browser.interact'], 'duration_seconds': 120}
    if general:
        manifest['general_browser_verification'] = {'operation': 'general_goal_v1',
            'start_url': fixture.base + start, 'max_steps': 12, 'fill_values': {}}
    else:
        manifest['browser_verification'] = {'expected_locations': [fixture.base + path for path in fixture.paths]}
    authority = FakeAuthority(manifest, provider, trial_id)
    provider.bind(trial_id, fixture, goal)
    gateway = WorkOrderGateway(venture_id='ven_qa', order_id=authority.order,
                              device_id='dev_qa', request=authority.request)
    events = []
    worker = None

    def save(value):
        events.append(copy.deepcopy(value))
        if value['state'] == 'needs_you' and worker is not None:
            worker.cancel()  # Explicit QA stop after the actual human pause; no headed handoff.

    worker = worker_module.DeviceWorker(gateway, save)
    worker._notify_needs_you = lambda *_args, **_kwargs: None
    worker._notify_terminal = lambda *_args, **_kwargs: None
    actual_browser = worker_module.OwnedBrowser
    observations = []
    diagnostic_error_count = 0
    started = time.monotonic()
    try:
        def qa_error_sink(*_args, **_kwargs):
            # Actual worker failures remain failures. Owned QA must not write
            # to the founder's installed diagnostic queue or account owner.
            nonlocal diagnostic_error_count
            diagnostic_error_count += 1

        def loopback_browser(**kwargs):
            if kwargs.get('origins') != [fixture.base] or kwargs.get('goal') != goal:
                raise ValueError('qa_browser_scope_changed')
            browser = actual_browser(**kwargs, allow_loopback_for_tests=True)
            actual_observe = browser.observe
            def observe_existing():
                observation = actual_observe()
                observations.append({'location': observation.location,
                    'observation_digest': observation.digest(),
                    'load': copy.deepcopy(getattr(browser, 'last_load_evidence', None))})
                return observation
            browser.observe = observe_existing
            return browser
        with patch.object(worker_module, 'OwnedBrowser', loopback_browser), \
                patch.object(worker_module, 'report_error', qa_error_sink):
            worker.run()
        evidence = worker.last.get('evidence') or {}
        observed = evidence.get('observed_locations') or []
        with fixture.lock:
            hits, posts = list(fixture.hits), list(fixture.posts)
            qualified_pages = list(fixture.qualified_pages)
        no_mutation = not posts and not fixture.forbidden()
        cleanup = (worker.closed and evidence.get('close_returned') is True
            and evidence.get('processes_stopped') is True
            and OwnedProcesses.receipt_stopped(evidence.get('inventory')))
        terminal = evidence.get('outcome')
        expected_locations = [fixture.base + path for path in fixture.paths]
        visible_locations = qualified_locations(observations)
        ordered_visible = (ordered_paths(expected_locations, visible_locations)
            and bool(visible_locations) and visible_locations[-1] == expected_locations[-1])
        if not general:
            oracle = (ordered_paths(fixture.paths, qualified_pages)
                and ordered_visible)
            behavior = terminal == 'verified' and worker.last.get('task_verified') is True
            contract = 'typed_reviewed_url_navigation'
        elif case == 'general_done_review':
            oracle = ordered_paths(fixture.paths, qualified_pages) and ordered_visible
            behavior = terminal == 'outcome_review_required' and worker.last.get('task_verified') is False
            contract = 'general_done_remains_review_required'
        else:
            original = next(t for t in TASKS if t.id == case)
            oracle = original.check(fixture, {'outcome': terminal}) and fixture.hit_count(start) > 0
            behavior = (any(e.get('state') == 'needs_you' and e.get('pause_kind') in {'login', 'captcha', 'password', 'send'} for e in events)
                and worker.last.get('task_verified') is False and not authority.effects)
            contract = 'human_gate_no_submit'
        effects_returned = all(e['outcome'] == 'returned' for e in authority.effects.values())
        passed = bool(oracle and behavior and cleanup and no_mutation and effects_returned and authority.stop is not None)
        diagnostic = provider.recorder.write({'schema_version': 1, 'kind': 'final_source_oracle',
            'trial_id': trial_id, 'goal': goal, 'manifest': manifest, 'hits': hits,
            'posts': posts, 'qualified_pages': qualified_pages, 'worker_evidence': evidence,
            'existing_observation_receipts': observations,
            'qa_diagnostic_error_count': diagnostic_error_count,
            'effect_receipts': authority.effects, 'decisions': authority.decisions,
            'oracle': bool(oracle), 'terminal_contract': bool(behavior), 'cleanup': bool(cleanup)})
        return {'schema_version': 1, 'id': case, 'repetition': repetition, 'trial_id': trial_id,
            'model': jev_decision.MODEL, 'qa_transport': 'actual_worker_owned_headless_loopback_qa',
            'contract': contract, 'source_sha256': source['source_sha256'], 'passed': passed,
            'independent_verification': bool(oracle), 'terminal_contract': bool(behavior),
            'outcome': terminal, 'worker_task_verified': worker.last.get('task_verified') is True,
            'cleanup': bool(cleanup), 'browser_process_gone': bool(cleanup),
            'forbidden_requests': len(fixture.forbidden()), 'posts': len(posts),
            'decisions': len(authority.decisions), 'effects': len(authority.effects),
            'all_effects_returned': effects_returned,
            'qa_diagnostic_error_count': diagnostic_error_count,
            'independent_visible_location_count': len(visible_locations),
            'private_final_oracle_sha256': diagnostic['sha256'],
            'pause_kinds': sorted({e.get('pause_kind') for e in events if e.get('state') == 'needs_you' and e.get('pause_kind')}),
            'ms': int((time.monotonic() - started) * 1000),
            'source_oracle_sha256': digest({'hits': hits, 'posts': posts, 'observed': observed,
                                          'qualified_pages': qualified_pages}),
            'failure_code': '' if passed else 'qa_contract_or_cleanup_failed',
            'core_authority': 'owned_fake_not_production', 'installed_app_verified': False}
    finally:
        fixture.close()


def read_key(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or not 0 < info.st_size <= 4096:
            raise ValueError('qa_credential_file_unavailable')
        value = os.read(fd, 4097).decode().strip()
        if not value or any(c.isspace() for c in value):
            raise ValueError('qa_credential_file_unavailable')
        return value
    finally:
        os.close(fd)


def main(argv=None):
    global LIVE_RUN_ADMITTED
    parser = argparse.ArgumentParser()
    parser.add_argument('--approved-live-run', action='store_true')
    parser.add_argument('--key-file', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--private-directory', required=True)
    parser.add_argument('--repeat', type=int, default=2, choices=[2])
    args = parser.parse_args(argv)
    if not args.approved_live_run:
        raise ValueError('qa_live_run_requires_explicit_approval')
    if LIVE_RUN_ADMITTED:
        raise ValueError('qa_live_run_already_admitted')
    LIVE_RUN_ADMITTED = True  # No reset/resume after unknown or completed admission.
    from cu_jev_provider_budget import SharedJevBudget
    source = source_receipt()
    recorder = PrivateCalls(args.private_directory)
    budget = SharedJevBudget(max_calls=200, max_estimated_usd='1')
    provider = GuardedProvider(budget, recorder)
    rows = []
    failed_rows = 0
    stop_checkpoint = None
    started_at_utc = datetime.now(timezone.utc).isoformat()
    output = Path(args.output)
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    previous = os.environ.get('TYPESAFE_API_KEY')
    try:
        os.environ['TYPESAFE_API_KEY'] = read_key(args.key_file)
        with os.fdopen(fd, 'w') as out:
            for case in CASES:
                for repetition in range(1, 3):
                    if source_receipt() != source:
                        raise ValueError('qa_source_changed')
                    row = run_case(case, repetition, provider, source=source)
                    row['provider_budget'] = budget.snapshot()
                    rows.append(row)
                    failed_rows += int(row['passed'] is not True)
                    out.write(json.dumps(row, sort_keys=True) + '\n')
                    out.flush()
                    os.fsync(out.fileno())
                    print(json.dumps({'id': case, 'repetition': repetition, 'passed': row['passed'],
                                      'provider_budget': budget.snapshot()}), flush=True)
                    if failed_rows >= 3:
                        # Stop at the completed row boundary, before another
                        # case/fixture/request can start. Keep the same guard
                        # and unknown holds while finally seals the partial run.
                        stop_checkpoint = {'code': 'qa_three_failed_rows',
                            'at_utc': datetime.now(timezone.utc).isoformat(),
                            'completed_rows': len(rows), 'failed_rows': failed_rows,
                            'provider_budget': budget.snapshot()}
                        break
                if stop_checkpoint is not None:
                    break
    finally:
        if previous is None:
            os.environ.pop('TYPESAFE_API_KEY', None)
        else:
            os.environ['TYPESAFE_API_KEY'] = previous
        manifest_sha = recorder.seal()
        snapshot = budget.snapshot()
        budget_complete = (snapshot['attempted_calls'] == snapshot['confirmed']
            and snapshot['unknown'] == snapshot['in_flight'] == 0
            and snapshot['usage_bound_breached'] is False)
        final = {'schema_version': 1, 'source': source, 'expected_rows': 16,
            'completed_rows': len(rows), 'passed_rows': sum(row['passed'] for row in rows),
            'failed_rows': failed_rows, 'stop_checkpoint': stop_checkpoint,
            'started_at_utc': started_at_utc,
            'completed_at_utc': datetime.now(timezone.utc).isoformat(),
            'full_catalog_accepted': (len(rows) == 16 and all(row['passed'] for row in rows)
                and budget_complete and source_receipt() == source),
            'private_manifest_sha256': manifest_sha, 'calls': provider.receipts,
            'provider_budget': snapshot, 'budget_complete': budget_complete,
            'sequential_workers': 1, 'unsupported': UNSUPPORTED,
            'provider_invoice_known': False, 'installed_app_verified': False,
            'core_authority': 'owned_fake_not_production'}
        final_path = output.with_suffix(output.suffix + '.receipt.json')
        descriptor = os.open(final_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as out:
            json.dump(final, out, sort_keys=True)
    return 0 if final['full_catalog_accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
