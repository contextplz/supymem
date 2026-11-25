"""Direct QA transport source/mock contracts; no driver process or desktop calls."""
import asyncio
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from desktop_runtime.cu_qa_direct_session import QADirectSession
from desktop_runtime.cua_session import AI_LOOP_TOOLS, OBSERVATION_PROTOCOL, CuaSession, CuaSessionError


def bare():
    session = object.__new__(QADirectSession)
    session.tools = AI_LOOP_TOOLS
    session.observation_protocol = OBSERVATION_PROTOCOL
    session.observation_pid = 42
    session._failure = session._loop = session._session = session._closing = None
    session._requested_close = threading.Event()
    session._ready = threading.Event()
    session._thread = SimpleNamespace(is_alive=lambda: True)
    return session


def test_direct_owned_stdio_argv_no_socket_and_eof_cleanup(monkeypatch):
    import desktop_runtime.cu_qa_direct_session as mod
    session, stages = bare(), []
    monkeypatch.setenv("OPENAI_DEVICE_VISION_API_KEY", "synthetic-provider-key-do-not-inherit")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic-cloud-key-do-not-inherit")
    class Ready:
        ready = False
        def set(self):
            self.ready = True
            session._closing.set()
        def is_set(self):
            return self.ready
    session._ready = Ready()
    @asynccontextmanager
    async def transport(params, *, errlog):
        assert params.args == ['mcp', '--direct']
        assert '--socket' not in params.args
        assert params.env['CUA_DRIVER_EMBEDDED'] == '1'
        assert params.env['CUA_DRIVER_RS_TELEMETRY_ENABLED'] == '0'
        assert 'OPENAI_DEVICE_VISION_API_KEY' not in params.env
        assert 'AWS_SECRET_ACCESS_KEY' not in params.env
        assert errlog.name == mod.os.devnull
        stages.append('owned_stdio_enter')
        try:
            yield 'read', 'write'
        finally:
            stages.append('owned_eof_cleanup')
    class Client:
        def __init__(self, *args):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            stages.append('client_close')
        async def initialize(self):
            stages.append('initialized')
    monkeypatch.setattr(mod, 'stdio_client', transport)
    monkeypatch.setattr(mod, 'ClientSession', Client)
    asyncio.run(session._serve('/owned/driver', None))
    assert session._ready.is_set()
    assert stages == ['owned_stdio_enter', 'initialized', 'client_close', 'owned_eof_cleanup']


def test_direct_initialization_failure_never_reconnects_or_uses_daemon(monkeypatch):
    import desktop_runtime.cu_qa_direct_session as mod
    session, attempts = bare(), []
    @asynccontextmanager
    async def fail(params, **kwargs):
        attempts.append(params.args)
        raise ConnectionError('private child failure')
        yield
    monkeypatch.setattr(mod, 'stdio_client', fail)
    with pytest.raises(ConnectionError):
        asyncio.run(session._serve('/owned/driver', None))
    assert attempts == [['mcp', '--direct']]
    assert not session._ready.is_set()


@pytest.mark.parametrize('name,args', [
    ('list_windows', {'pid': 7, 'on_screen_only': False}),
    ('get_window_state', {'pid': True, 'window_id': 10}),
    ('click', {'target': {'kind': 'window', 'pid': 7, 'window_id': 10}, 'delivery_mode': 'background'}),
    ('hotkey', {'pid': 42, 'keys': ['cmd', 'a']}),
    ('launch_app', {'name': 'anything'}), ('invoke_menu', {'pid': 42})])
def test_bound_direct_scope_refuses_before_dispatch(monkeypatch, name, args):
    monkeypatch.setattr(CuaSession, 'call', lambda *a, **kw: pytest.fail('out of scope dispatched'))
    with pytest.raises(CuaSessionError, match='scope_invalid'):
        bare().call(name, args)


def test_bound_direct_readonly_scope_and_tool_receipt_preserved(monkeypatch):
    calls = []
    result = SimpleNamespace(isError=False, structuredContent={'pid': 42})
    monkeypatch.setattr(CuaSession, 'call', lambda self, name, args, **kw: (calls.append((name, args)), result)[1])
    assert bare().call('get_window_state', {'pid': 42, 'window_id': 10}) is result
    assert calls == [('get_window_state', {'pid': 42, 'window_id': 10})]


def test_direct_transport_error_keeps_fixed_code_without_private_detail(monkeypatch):
    def fail(*args, **kwargs):
        raise CuaSessionError('desktop_cua_transport_unavailable', 'private AX content')
    monkeypatch.setattr(CuaSession, 'call', fail)
    with pytest.raises(CuaSessionError) as caught:
        bare().call('check_permissions', {})
    assert str(caught.value) == 'desktop_cua_transport_unavailable'
    assert caught.value.detail is None
    assert caught.value.__suppress_context__


def test_direct_close_is_bounded_owned_only_and_uncertainty_is_not_success():
    session, calls = bare(), []
    session._closing = SimpleNamespace(set=lambda: None)
    session._loop = SimpleNamespace(call_soon_threadsafe=lambda fn: calls.append('owned_stop'))
    session._thread = SimpleNamespace(is_alive=lambda: True, join=lambda seconds: calls.append(seconds))
    with pytest.raises(CuaSessionError, match='close_unconfirmed'):
        session.close()
    assert calls == ['owned_stop', 5]
    assert session._requested_close.is_set()


def test_direct_refuses_unknown_tool_set_and_invalid_protocol_before_thread(tmp_path):
    binary = tmp_path / 'driver'
    binary.write_bytes(b'not executable; no launch')
    for kwargs in [{'tools': frozenset({'capture_screen'})}, {'observation_protocol': 'foreign'},
                   {'observation_pid': True}, {'observation_protocol': OBSERVATION_PROTOCOL}]:
        with pytest.raises(CuaSessionError, match='scope_invalid'):
            QADirectSession(str(binary), **kwargs)


def test_default_trial_still_uses_owned_daemon_and_explicit_direct_never_starts_it(monkeypatch):
    import importlib.util
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / 'scripts/desktop'))
    spec = importlib.util.spec_from_file_location('qa_direct_trial_contract', root / 'scripts/desktop/cu_ai_trial.py')
    trial = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trial)
    import cu_trial_daemon
    import desktop_runtime.cua_session as production
    import desktop_runtime.cu_qa_direct_session as direct
    import desktop_runtime.cu_ai_browser as browser
    calls = []
    class Session:
        def __init__(self, *args, **kw):
            calls.append(('session', args, kw))
        def close(self):
            calls.append(('session_close',))
    class Daemon:
        def start(self, binary):
            calls.append(('daemon_start',))
            return '/private/tmp/supymem-cua-owned-test/driver.sock'
        def close(self):
            calls.append(('daemon_close',))
    monkeypatch.setattr(production, 'CuaSession', Session)
    monkeypatch.setattr(direct, 'QADirectSession', Session)
    monkeypatch.setattr(cu_trial_daemon, 'TrialDaemon', Daemon)
    monkeypatch.setattr(trial, 'CuaMac', lambda caller, **kw: SimpleNamespace(close=caller.close))
    monkeypatch.setattr(trial, 'PeekabooMac', lambda *a: None)
    monkeypatch.setattr(browser, 'RoutedMac', lambda native, page: native)
    monkeypatch.setattr(browser, 'BrowserMac', lambda **kw: None)
    mac = trial.make_mac('cua')
    assert calls[0] == ('daemon_start',)
    assert len(calls[1][1]) == 2
    mac.close()
    assert ('daemon_close',) in calls
    calls.clear()
    mac = trial.make_mac('cua', qa_direct=True)
    assert len(calls[0][1]) == 1
    assert calls[0][2]['tools'] == AI_LOOP_TOOLS
    mac.close()
    assert all(row[0] not in {'daemon_start', 'daemon_close'} for row in calls)
    with pytest.raises(ValueError, match='requires_cua'):
        trial.make_mac('peekaboo', qa_direct=True)


def test_owned_close_interrupts_initialization_and_runs_both_eof_contexts(monkeypatch):
    import desktop_runtime.cu_qa_direct_session as mod
    session, stages = bare(), []
    @asynccontextmanager
    async def transport(*a, **kw):
        try:
            yield None, None
        finally:
            stages.append('owned_eof')
    class Client:
        def __init__(self, *a):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            stages.append('client_closed')
        async def initialize(self):
            session._closing.set()
            try:
                await asyncio.Future()
            finally:
                stages.append('initialize_cancelled')
    monkeypatch.setattr(mod, 'stdio_client', transport)
    monkeypatch.setattr(mod, 'ClientSession', Client)
    with pytest.raises(CuaSessionError, match='handshake_unavailable'):
        asyncio.run(session._serve('/owned/driver', None))
    assert not session._ready.is_set()
    assert stages == ['initialize_cancelled', 'client_closed', 'owned_eof']


@pytest.mark.parametrize('code', ['private AX label', 'desktop_cua_transport_unavailable: private value'])
def test_direct_unknown_exception_message_is_never_public(monkeypatch, code):
    def fail(*args, **kwargs):
        raise CuaSessionError(code, 'private AX content')
    monkeypatch.setattr(CuaSession, 'call', fail)
    with pytest.raises(CuaSessionError) as caught:
        bare().call('check_permissions', {})
    assert caught.value.args == ('desktop_cua_transport_unavailable',)
    assert caught.value.detail is None


def test_direct_code_attribute_is_allowlisted_without_stringifying(monkeypatch):
    class PrivateError(CuaSessionError):
        # CuaSessionError.__init__ would overwrite a class-level code with args[0].
        def __init__(self, body):
            RuntimeError.__init__(self, body)
            self.code = 'desktop_cua_response_invalid'
        def __str__(self):
            pytest.fail('private exception stringified')
    def fail(*args, **kwargs):
        raise PrivateError('private body')
    monkeypatch.setattr(CuaSession, 'call', fail)
    with pytest.raises(CuaSessionError) as caught:
        bare().call('check_permissions', {})
    assert caught.value.args == ('desktop_cua_response_invalid',)
