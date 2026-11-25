"""Real native adapter boundary with mocked read-only process inventory; no GUI."""
from types import SimpleNamespace

import pytest

from desktop_runtime.cu_ai_cua import CuaMac
from desktop_runtime.cu_reviewed_scope import ReviewedDesktopScope
from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver


@pytest.fixture
def scope(monkeypatch):
    scope = ReviewedDesktopScope('com.apple.TextEdit', 11, '123', 42)
    monkeypatch.setattr(PeekabooDriver, 'running_apps', lambda: [AppChoice('TextEdit', scope.bundle, 11, '123')])
    monkeypatch.setattr(PeekabooDriver, 'running_windows', lambda app: [{'window_id':42}, {'window_id':99}])
    return scope


def test_real_adapter_observes_reviewed_window_even_if_other_window_is_front(scope):
    calls = []
    def driver(tool, args):
        calls.append((tool, args))
        if tool == 'list_windows':
            return {'windows':[{'pid':11, 'window_id':n, 'layer':0, 'bounds':{'width':500,'height':500},
                                'z_index':n, 'is_on_screen':True} for n in (42,99)]}
        if tool == 'get_window_state':
            return {'pid':11,'window_id':args['window_id'],'elements':[], 'degraded':True}
        return {}
    mac = CuaMac(scope.caller(driver))
    mac.set_target(11, 'TextEdit')
    mac.observe()
    windows = [args['window_id'] for tool,args in calls if tool == 'get_window_state']
    assert windows and set(windows) == {42}
    assert mac.shot_window == 42


@pytest.mark.parametrize('tool,args', [
    ('click', {'target':{'kind':'window','pid':11,'window_id':99}}),
    ('click', {'target':{'kind':'window','pid':22,'window_id':42}}),
    ('get_window_state', {'pid':11,'window_id':99}),
    ('set_value', {'pid':11,'window_id':99}),
    ('list_windows', {'pid':22}),
    ('launch_app', {'bundle_id':'com.apple.Notes'}),
    ('invoke_menu', {'pid':11,'window_id':42}),
])
def test_scope_refuses_before_native_dispatch(scope, tool, args):
    dispatched = []
    call = scope.caller(lambda *args: dispatched.append(args))
    with pytest.raises(ValueError, match='desktop_scope_expansion_required'):
        call(tool,args)
    assert dispatched == []


def test_process_replacement_after_admission_cannot_receive_input(scope, monkeypatch):
    dispatched = []
    call = scope.caller(lambda *args: dispatched.append(args))
    monkeypatch.setattr(PeekabooDriver,'running_apps',lambda:[AppChoice('TextEdit',scope.bundle,11,'124')])
    with pytest.raises(ValueError, match='desktop_app_identity_changed'):
        call('click', {'target':{'kind':'window','pid':11,'window_id':42}})
    assert dispatched == []


@pytest.mark.parametrize('op', ['fs','script','template','navigate','open_path','new_tab','switch_tab'])
def test_desktop_review_does_not_grant_other_domain_executors(scope, op):
    assert scope.admit(SimpleNamespace(op=op),None) == 'desktop_scope_expansion_required'


@pytest.mark.parametrize('tool,args', [('click',{'target':{'pid':11,'window_id':99}}),
                                     ('get_window_state',{'pid':22,'window_id':42}),
                                     ('launch_app',{'bundle_id':'com.apple.Notes'})])
def test_actual_session_rejects_cross_scope_before_future_or_transport(tool, args):
    from desktop_runtime.cua_session import CuaSession
    # No thread/session exists: reaching transport would error differently.
    session = object.__new__(CuaSession)
    session.reviewed_surface = (11,42)
    with pytest.raises(ValueError,match='desktop_scope_expansion_required'):
        session.call(tool,args)


def test_stop_during_identity_inventory_refuses_dispatch(scope, monkeypatch):
    import threading
    stop = threading.Event()
    def inventory(app):
        stop.set()
        return [{'window_id':42}]
    monkeypatch.setattr(PeekabooDriver,'running_windows',inventory)
    dispatched=[]
    with pytest.raises(ValueError,match='stopped_by_founder'):
        scope.caller(lambda *args:dispatched.append(args),stop=stop)(
            'click',{'target':{'kind':'window','pid':11,'window_id':42}, 'delivery_mode':'background'})
    assert dispatched == []


def test_reviewed_window_admits_only_explicit_background_window_delivery(scope):
    dispatched=[]
    call=scope.caller(lambda *args:dispatched.append(args) or {'effect':'confirmed'})
    args={'target':{'kind':'window','pid':11,'window_id':42},'delivery_mode':'background'}
    assert call('click',args) == {'effect':'confirmed'}
    assert len(dispatched)==1
    for invalid in [{**args,'delivery_mode':'foreground'},
                    {**args,'target':{'kind':'application','pid':11,'window_id':42}}]:
        with pytest.raises(ValueError,match='desktop_scope_expansion_required'):
            call('click',invalid)
    assert len(dispatched)==1


def v2_manifest():
    rows=[{'application_bundle':'com.apple.TextEdit','process_id':11,'process_start_identity':'123',
           'window_id':42,'window_title':'Owned A'},
          {'application_bundle':'com.apple.Notes','process_id':22,'process_start_identity':'234',
           'window_id':55,'window_title':'Owned B'}]
    return {'applications':['com.apple.TextEdit','com.apple.Notes'],'origins':[],
            'actions':['desktop.read','desktop.interact'],
            'desktop_verification':{**rows[0],'operation':'desktop_goal_v2','delivery_mode':'background','max_steps':48,
                                    'reviewed_surfaces':rows}}


def test_v2_gate_is_off_and_never_inferred_from_task(monkeypatch):
    monkeypatch.delenv('SUPYMEM_REVIEWED_DESKTOP_V2',raising=False)
    with pytest.raises(ValueError,match='desktop_scope_unsupported'):
        ReviewedDesktopScope.from_manifest(v2_manifest())


def test_v2_actual_adapter_switches_only_declared_existing_app_surfaces(monkeypatch):
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    scope=ReviewedDesktopScope.from_manifest(v2_manifest())
    monkeypatch.setattr(PeekabooDriver,'running_apps',lambda:[AppChoice('TextEdit',scope.bundle,11,'123'),
                                                           AppChoice('Notes','com.apple.Notes',22,'234')])
    monkeypatch.setattr(PeekabooDriver,'running_windows',lambda app:[{'window_id':42 if app.pid==11 else 55}])
    dispatched=[]
    def driver(tool,args):
        dispatched.append((tool,args))
        if tool=='list_apps':
            return {'apps':[{'name':'Notes','bundle_id':'com.apple.Notes','pid':22,'running':True},
                            {'name':'Other','bundle_id':'com.example.Other','pid':33,'running':True}]}
        if tool=='list_windows':
            return {'windows':[{'pid':22,'window_id':wid,'layer':0,'bounds':{'width':500,'height':500},'z_index':wid}
                               for wid in (55,99)]}
        return {'effect':'confirmed','verified':True}
    mac=CuaMac(scope.caller(driver))
    mac.set_target(11,'TextEdit')
    mac.switch_app('com.apple.Notes')
    assert mac.pid==22
    assert [w['window_id'] for w in mac._windows(22)]==[55]
    assert scope.caller(driver)('click',{'target':{'kind':'window','pid':22,'window_id':55},
                                      'delivery_mode':'background'})['verified'] is True
    before=len(dispatched)
    with pytest.raises(ValueError):
        scope.caller(driver)('click',{'target':{'kind':'window','pid':22,'window_id':99},'delivery_mode':'background'})
    assert len(dispatched)==before


@pytest.mark.parametrize('tamper',['primary','applications','duplicate','identity','unknownfield'])
def test_v2_rejects_inconsistent_saved_manifest(monkeypatch,tamper):
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    manifest=v2_manifest()
    spec=manifest['desktop_verification']
    rows=spec['reviewed_surfaces']
    if tamper=='primary':
        spec['window_id']=99
    if tamper=='applications':
        manifest['applications'].append('com.example.Other')
    if tamper=='duplicate':
        rows.append(dict(rows[0]))
    if tamper=='identity':
        rows[1]['process_id']=11
    if tamper=='unknownfield':
        rows[1]['cold_launch']=True
    with pytest.raises(ValueError,match='desktop_scope_unsupported'):
        ReviewedDesktopScope.from_manifest(manifest)


@pytest.mark.parametrize('chord',['command+n','COMMAND+SHIFT+n','shift+command+n','control+n','shift+control+n'])
def test_supported_new_window_chord_aliases_refuse_before_native_dispatch(scope,chord):
    from coding_agents.cu_ai_loop import parse_action
    action=parse_action({'op':'key','chord':chord})
    assert scope.admit(action,None)=='desktop_scope_expansion_required'


def test_super_modifier_is_not_supported_by_current_dsl_and_cannot_dispatch(scope):
    from coding_agents.cu_ai_loop import parse_action, ActionError
    with pytest.raises(ActionError):
        parse_action({'op':'key','chord':'super+n'})


def test_actual_worker_backend_binds_entire_reviewed_v2_list(monkeypatch):
    from desktop_runtime.device_worker import DeviceWorker
    from desktop_runtime.cua_session import AI_LOOP_TOOLS
    import threading
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    scope=ReviewedDesktopScope.from_manifest(v2_manifest())
    monkeypatch.setattr(PeekabooDriver,'running_apps',lambda:[AppChoice('TextEdit',scope.bundle,11,'123'),
                                                           AppChoice('Notes','com.apple.Notes',22,'234')])
    monkeypatch.setattr(PeekabooDriver,'running_windows',lambda app:[{'window_id':42 if app.pid==11 else 55}])
    admitted=[]
    session=SimpleNamespace(call=lambda *args:None,close=lambda:None)
    def factory(*args,**kwargs):
        admitted.append(kwargs)
        return session
    monkeypatch.setattr('desktop_runtime.cua_session.CuaSession',factory)
    worker=object.__new__(DeviceWorker)
    worker._ai_scope=scope
    worker.cua_socket='synthetic-private-socket'
    worker.cancelled=threading.Event()
    backend=worker._ai_backend()
    assert backend.pid==11
    assert admitted==[{'tools':AI_LOOP_TOOLS,'reviewed_surfaces':((11,42),(22,55))}]
    backend.close()


@pytest.mark.parametrize('tool,args',[
    ('set_value',{'target':{'kind':'window','pid':11,'window_id':42},'pid':22,'window_id':99,'value':'synthetic'}),
    ('get_window_state',{'target':{'kind':'window','pid':11,'window_id':42},'pid':22,'window_id':99}),
    ('click',{'target':{'kind':'window','pid':11,'window_id':42},'pid':22,'window_id':99,'delivery_mode':'background'}),
])
def test_conflicting_native_identity_envelopes_refuse_before_caller_and_transport(scope,tool,args):
    from desktop_runtime.cua_session import CuaSession
    dispatched=[]
    with pytest.raises(ValueError,match='desktop_scope_expansion_required'):
        scope.caller(lambda *args:dispatched.append(args))(tool,args)
    assert dispatched==[]
    session=object.__new__(CuaSession)
    session.reviewed_surface=(11,42)
    with pytest.raises(ValueError,match='desktop_scope_expansion_required'):
        session.call(tool,args)


def test_native_top_level_set_value_and_read_only_shapes_still_admit(scope):
    dispatched=[]
    call=scope.caller(lambda tool,args:dispatched.append((tool,args)) or {})
    call('set_value',{'pid':11,'window_id':42,'element_token':'synthetic','value':'synthetic'})
    call('get_window_state',{'pid':11,'window_id':42})
    call('list_windows',{'pid':11,'on_screen_only':False})
    assert [tool for tool,_ in dispatched]==['set_value','get_window_state','list_windows']


def modal_manifest():
    manifest=v2_manifest()
    manifest['desktop_verification']['modal_policy']={
        'operation':'exact_parent_children_v1','parents':[
            {key:manifest['desktop_verification'][key] for key in
             ('application_bundle','process_id','process_start_identity','window_id')}]}
    return manifest


def modal_setup(monkeypatch):
    import threading
    from desktop_runtime.cu_modal_authority import ReviewedModalAuthority
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    monkeypatch.setenv('SUPYMEM_REVIEWED_MODAL_CHILDREN','1')
    scope=ReviewedDesktopScope.from_manifest(modal_manifest())
    monkeypatch.setattr(PeekabooDriver,'running_apps',lambda:[AppChoice('TextEdit',scope.bundle,11,'123'),
                    AppChoice('Notes','com.apple.Notes',22,'234')])
    monkeypatch.setattr(PeekabooDriver,'running_windows',lambda app:[{'window_id':42 if app.pid==11 else 55}])
    stop=threading.Event()
    authority=ReviewedModalAuthority(scope,stop=stop)
    class Driver:
        child=99
        protocol='supymem_exact_modal_v1'
        foreign=False
        def __init__(self):
            self.inputs=[]
        def __call__(self,tool,args):
            if tool=='list_windows':
                return {'windows':[{'pid':pid,'window_id':wid,'layer':0,'is_on_screen':True,'z_index':z,
                                    'bounds':{'width':500,'height':400}} for pid,wid,z in
                                   [(11,42,5),(12 if self.foreign else 11,self.child,10),(11,77,20)]]}
            if tool=='get_window_state':
                wid=args['window_id']
                return {'pid':11,'window_id':wid,'snapshot_id':'owned'+str(wid),'degraded':False,
                    'modal_protocol':self.protocol,'element_count':1,
                    'elements':[{'pid':11,'window_id':wid,'element_index':0,'element_token':'owned-token',
                                 'role':'AXButton','enabled':True}],
                    'modal_windows':([{'pid':11,'window_id':self.child,'parent_window_id':42}]
                                     if wid==42 and self.child else [])}
            self.inputs.append((tool,args))
            return {'effect':'confirmed','verified':True}
    driver=Driver()
    call=scope.caller(driver,stop=stop,modal_authority=authority)
    return scope,authority,driver,call,stop


def test_modal_review_flag_is_off_and_task_prose_does_not_grant_it(monkeypatch):
    monkeypatch.setenv('SUPYMEM_REVIEWED_DESKTOP_V2','1')
    monkeypatch.delenv('SUPYMEM_REVIEWED_MODAL_CHILDREN',raising=False)
    with pytest.raises(ValueError,match='desktop_scope_unsupported'):
        ReviewedDesktopScope.from_manifest(modal_manifest())


def test_actual_adapter_resolves_only_reviewed_direct_child_and_refuses_decoy(monkeypatch):
    from coding_agents.cu_ai_loop import Action
    scope,authority,driver,call,stop=modal_setup(monkeypatch)
    mac=CuaMac(call)
    mac.set_target(11,'TextEdit')
    obs=mac.observe()
    assert mac.ax_window==99 and mac.shot_window==99 and mac.sheet_active is True
    mac.act(Action('click',element_id='e0'),obs)
    assert len(driver.inputs)==1 and driver.inputs[0][1]['target']=={'kind':'window','pid':11,'window_id':99}
    assert authority.children=={}  # even confirmed effects retire old child observation
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman) as refused:
        call('click',{'target':{'kind':'window','pid':11,'window_id':77},'delivery_mode':'background'})
    assert refused.value.dispatch_state=='not_dispatched'
    assert len(driver.inputs)==1


@pytest.mark.parametrize('change', ['closed','replacement','foreign','protocol','process','stop'])
def test_fresh_child_dispatch_refuses_changed_identity_before_input(monkeypatch,change):
    scope,authority,driver,call,stop=modal_setup(monkeypatch)
    call('list_windows',{'pid':11,'on_screen_only':False})
    assert (11,99) in authority.children
    if change=='closed':
        driver.child=None
    if change=='replacement':
        driver.child=100
    if change=='foreign':
        driver.foreign=True
    if change=='protocol':
        driver.protocol='untrusted-model-json'
    if change=='process':
        monkeypatch.setattr(PeekabooDriver,'running_apps',lambda:[AppChoice('TextEdit',scope.bundle,11,'999'),
                         AppChoice('Notes','com.apple.Notes',22,'234')])
    if change=='stop':
        stop.set()
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman) as held:
        call('click',{'target':{'kind':'window','pid':11,'window_id':99},'delivery_mode':'background'})
    assert held.value.dispatch_state=='not_dispatched'
    assert driver.inputs==[]


def test_actual_transport_child_dispatch_requires_single_use_wrapper_lease(monkeypatch):
    import asyncio
    from desktop_runtime.cua_session import CuaSession,AI_LOOP_TOOLS
    scope,authority,driver,call,stop=modal_setup(monkeypatch)
    call('list_windows',{'pid':11,'on_screen_only':False})
    session=object.__new__(CuaSession)
    session.reviewed_modal_authority=authority
    session.reviewed_surfaces=scope.pairs
    args={'target':{'kind':'window','pid':11,'window_id':99},'delivery_mode':'background'}
    with pytest.raises(ValueError,match='desktop_modal_fresh_lease_required'):
        session.call('click',args)
    dispatched=[]
    async def call_tool(name,args):
        return None
    session.tools=AI_LOOP_TOOLS
    session._failure=None
    session._thread=SimpleNamespace(is_alive=lambda:True)
    session._loop=object()
    session._session=SimpleNamespace(call_tool=call_tool)
    def submit(coro,loop):
        coro.close()
        dispatched.append(True)
        return SimpleNamespace(result=lambda **kw:SimpleNamespace(structuredContent={'effect':'confirmed'}))
    monkeypatch.setattr(asyncio,'run_coroutine_threadsafe',submit)
    authority.arm('click',args)
    assert session.call('click',args).structuredContent['effect']=='confirmed'
    with pytest.raises(ValueError,match='desktop_modal_fresh_lease_required'):
        session.call('click',args)
    assert dispatched==[True]


def test_raw_parent_metadata_cannot_mint_undeclared_child_input(monkeypatch):
    scope,authority,driver,call,stop=modal_setup(monkeypatch)
    from desktop_runtime.cu_ai_engine import NeedsHuman
    with pytest.raises(NeedsHuman) as held:
        call('click',{'target':{'kind':'window','pid':11,'window_id':99,'parent_window_id':42},
                      'modal_protocol':'supymem_exact_modal_v1','delivery_mode':'background'})
    assert held.value.dispatch_state=='not_dispatched'
    assert driver.inputs==[]


def test_child_lease_expiry_and_close_retire_authority(monkeypatch):
    scope,authority,driver,call,stop=modal_setup(monkeypatch)
    call('list_windows',{'pid':11,'on_screen_only':False})
    args={'target':{'kind':'window','pid':11,'window_id':99},'delivery_mode':'background'}
    authority.arm('click',args)
    tool,pid,wid,stamp=authority.armed
    authority.armed=(tool,pid,wid,stamp-21)
    with pytest.raises(ValueError,match='desktop_modal_fresh_lease_required'):
        authority.check_transport('click',args)
    call.close()
    assert authority.children=={} and authority.armed is None


def test_backend_factory_shares_one_reviewed_modal_transport_authority(monkeypatch):
    from desktop_runtime.device_worker import DeviceWorker
    from desktop_runtime.cu_modal_authority import ReviewedModalAuthority
    scope,_,driver,_,stop=modal_setup(monkeypatch)
    admitted=[]
    session=SimpleNamespace(call=lambda *args:None,close=lambda:None)
    monkeypatch.setattr('desktop_runtime.cua_session.CuaSession',lambda *args,**kwargs: admitted.append(kwargs) or session)
    worker=object.__new__(DeviceWorker)
    worker._ai_scope,worker.cua_socket,worker.cancelled=scope,'synthetic-private-socket',stop
    backend=worker._ai_backend()
    authority=admitted[0]['reviewed_modal_authority']
    assert type(authority) is ReviewedModalAuthority
    assert authority.scope is scope and authority.stop is stop
    assert admitted[0]['reviewed_surfaces']==scope.pairs
    assert backend.pid==scope.pid
