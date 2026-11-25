"""DeviceWorker.run_ai_loop: claim -> plan -> steps -> verified stop receipt; truthful failures; Needs-you; Stop."""
import json
import threading

import pytest

from desktop_runtime import cu_ai_engine as eng
from desktop_runtime.device_worker import DeviceWorker, cu_engine

MANIFEST = {"applications": ["com.apple.TextEdit"], "origins": [], "actions": ["desktop.read", "desktop.interact"],
            "duration_seconds": 600, "instruction": "Write a note in TextEdit",
            "desktop_verification": {"operation": "desktop_goal_v1", "application_bundle": "com.apple.TextEdit",
                                     "process_id": 1, "process_start_identity": "1", "window_id": 1,
                                     "window_title": "x", "delivery_mode": "background", "max_steps": 5,
                                     "reviewed_text": [], "expected_visible_text": "synthetic result",
                                     "founder_goal": "Write a note in TextEdit"}}


@pytest.fixture(autouse=True)
def _owned_worker_checkpoint_directory(tmp_path, monkeypatch):
    # Reused synthetic order IDs must never read/write the installed app's state.
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path / "owned_worker_state"))
    from desktop_runtime.peekaboo_driver import AppChoice, PeekabooDriver
    monkeypatch.setattr(PeekabooDriver, "running_apps", lambda: [AppChoice("TextEdit", "com.apple.TextEdit", 1, "1")])
    monkeypatch.setattr(PeekabooDriver, "running_windows", lambda app: [{"window_id": 1}])


def action(op, **kw):
    return {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0,
            "app": "", "ms": 0, "summary": "", **kw}


def reply(*actions):
    return json.dumps({"thought": "t", "memory": "m", "subgoal_done": False, "actions": list(actions)})


class FakeMac:
    def __init__(self, screens=None):
        self.n, self.acted = 0, []
        self.screens = screens or [[]]

    def observe(self):
        elements = self.screens[min(self.n, len(self.screens) - 1)]
        self.n += 1
        self._observation = eng.Observation(f"png{self.n}".encode(), elements, "TextEdit", "Doc", "s", False, 1)
        self.pid, self.ax_window, self.shot_window = 1, 1, 1
        self._observation_at = eng.time.monotonic()
        self.raw = [{'pid':1,'window_id':1,'role':'AXStaticText','label':'synthetic result'}]
        return self._observation

    def reviewed_goal_evidence(self, *args):
        from desktop_runtime.cu_ai_cua import CuaMac
        return CuaMac.reviewed_goal_evidence(self, *args)

    def frontmost(self):
        return ("TextEdit", 1)

    def focused_role(self):
        return None

    def activate(self, name):
        pass

    def act(self, a, obs):
        self.acted.append(a.op)
        return "ok"


class Gateway:
    order_id, venture_id, execution_id = "dwo_ai", "ven_x", None
    claim_attempted = False

    def __init__(self, replies, verdict='{"passed": true, "reason": "title shows the file"}'):
        self.replies, self.verdict, self.kinds, self.receipt = list(replies), verdict, [], None

    def claim(self):
        from datetime import datetime, timezone
        self.claim_attempted, self.execution_id = True, "dex_ai"
        return {'id':self.order_id,'venture_id':self.venture_id,'execution_id':self.execution_id,
                'workspace_id':'ws_ai','actor_id':'actor_ai','device_id':'device_ai','review_digest':'a'*64,
                'reservation_id':'reservation_ai','claimed_at':datetime.now(timezone.utc).isoformat(),
                'manifest':self.claim_manifest}

    def current(self):
        return {"state": "claimed"}

    def ai_step(self, invocation_id, kind, context, png=None, **kwargs):
        self.kinds.append(kind)
        base = {"authority": "proposal_only", "model": "m", "input_tokens": 10, "output_tokens": 5}
        if kind == "plan":
            return {**base, "text": '{"subgoals": ["write the note", "save it"]}'}
        if kind == "verify":
            return {**base, "text": self.verdict}
        return {**base, "reply": self.replies.pop(0)}

    def stop_receipt(self, digest, stopped, *, task_verified=False, **extra):
        self.receipt = {"task_verified": task_verified, **extra}
        return {"state": "stopped"}


def worker(monkeypatch, gateway, mac):
    saved = []
    w = DeviceWorker(gateway, saved.append)
    monkeypatch.setattr(w, "_ai_backend", lambda: mac)
    monkeypatch.setattr("desktop_runtime.local_notices.push_task_finished", lambda **k: None)
    monkeypatch.setattr("desktop_runtime.local_notices.push_needs_you", lambda **k: None)
    monkeypatch.setenv("SUPYMEM_CU_ENGINE", "ai_loop")
    monkeypatch.setattr(eng.time, "sleep", lambda s: None)
    actual_run = w.run_ai_loop
    def run(saved_order, manifest):
        gateway.claim_manifest = manifest  # Closed fake Core returns its saved reviewed manifest.
        return actual_run(saved_order, manifest)
    monkeypatch.setattr(w, 'run_ai_loop', run)
    return w, saved


def test_engine_flag_defaults_to_ai_loop_with_legacy_fallback(monkeypatch):
    monkeypatch.delenv("SUPYMEM_CU_ENGINE", raising=False)
    assert cu_engine() == "ai_loop"
    monkeypatch.setenv("SUPYMEM_CU_ENGINE", "legacy")
    assert cu_engine() == "legacy"


def test_done_is_verified_by_a_separate_judgement_and_reported_truthfully(monkeypatch):
    gateway = Gateway([reply(action("done", summary="saved")), reply(action("key", chord="cmd+s")),
                       reply(action("done", summary="saved"))])
    w, saved = worker(monkeypatch, gateway, FakeMac())
    w.run_ai_loop({"state": "approved"}, MANIFEST)
    assert gateway.receipt == {"task_verified": True}
    assert "plan" in gateway.kinds and gateway.kinds[-1] == "verify"
    assert w.last["state"] == "stopped" and w.last["task_verified"] is True
    assert all("text" not in step for step in w.last["evidence"]["steps"])  # no typed/screen text in receipts


def test_verifier_veto_means_not_verified(monkeypatch):
    gateway = Gateway([reply(action("done", summary="saved"))] * 8, verdict='{"passed": false, "reason": "no file"}')
    w, _ = worker(monkeypatch, gateway, FakeMac([[{"id": "e1", "label": "a"}], [{"id": "e1", "label": "b"}]]))
    w.run_ai_loop({"state": "approved"}, {**MANIFEST, "duration_seconds": 600})
    assert gateway.receipt["task_verified"] is False
    assert w.last["outcome"] == "execution_unconfirmed"


def test_model_failure_report_stops_with_reason_not_done(monkeypatch):
    gateway = Gateway([reply(action("fail", summary="cannot find the app"))])
    w, _ = worker(monkeypatch, gateway, FakeMac())
    w.run_ai_loop({"state": "approved"}, MANIFEST)
    assert gateway.receipt["task_verified"] is False
    assert w.last["failure_code"] == "ai_loop_model_reported_failure"


def test_hard_block_stops_the_task(monkeypatch):
    gateway = Gateway([reply(action("type", text="4111 1111 1111 1111"))])
    w, _ = worker(monkeypatch, gateway, FakeMac())
    w.run_ai_loop({"state": "approved"}, MANIFEST)
    assert w.last["failure_code"] == "ai_loop_blocked_credential_or_card_text"


def test_stop_button_ends_the_loop_before_any_action(monkeypatch):
    mac = FakeMac()
    gateway = Gateway([reply(action("key", chord="tab"))] * 5)
    w, _ = worker(monkeypatch, gateway, mac)
    w.cancel()
    w.run_ai_loop({"state": "approved"}, MANIFEST)
    assert mac.acted == [] and w.last["outcome"] == "cancelled"


def test_captcha_pauses_notifies_and_resumes_on_continue(monkeypatch):
    notices = []
    gate = [{"id": "e1", "ax_role": "AXCheckBox", "label": "I'm not a robot"}]
    mac = FakeMac([gate, gate, gate, [{"id": "e1", "label": "Welcome"}]])
    gateway = Gateway([reply(action("key", chord="tab")), reply(action("done", summary="signed up"))])
    w, saved = worker(monkeypatch, gateway, mac)
    monkeypatch.setattr("desktop_runtime.local_notices.push_needs_you", lambda **k: notices.append(k["body"]))
    t = threading.Timer(0.1, lambda: (w.continue_founder_handoff(), None))
    t.start()
    w.run_ai_loop({"state": "approved"}, {**MANIFEST, "instruction": "Create the account"})
    t.cancel()
    assert notices and "Continue" in notices[0]
    paused = [s for s in saved if s.get("state") == "needs_you"]
    assert paused and paused[0]["needs_you"]["reason"] == "captcha" and "Continue" in paused[0]["needs_you"]["resume"]
    assert gateway.receipt["task_verified"] is True      # resumed after the human step


def test_scope_mismatch_never_claims(monkeypatch):
    gateway = Gateway([])
    w, _ = worker(monkeypatch, gateway, FakeMac())
    w.run_ai_loop({"state": "approved"}, {**MANIFEST, "origins": ["https://example.com"]})
    assert gateway.claim_attempted is False and w.last["state"] == "not_started"


def test_stop_with_failed_owned_cleanup_still_reports_receipt_truthfully(monkeypatch):
    class BrokenClose(FakeMac):
        def close(self):
            raise RuntimeError("private cleanup detail")
    class ReceiptGateway(Gateway):
        def stop_receipt(self, digest, stopped, **kwargs):
            self.stopped_argument = stopped
            return super().stop_receipt(digest, stopped, **kwargs)
    mac = BrokenClose()
    gateway = ReceiptGateway([reply(action("key", chord="tab"))])
    w, saved = worker(monkeypatch, gateway, mac)
    w.cancel()
    w.run_ai_loop({"state": "approved"}, MANIFEST)
    assert mac.acted == [] and w.last["outcome"] == "cancelled"
    assert not w.closed and gateway.stopped_argument is False
    assert w.last["state"] == "reconciliation_required"
    assert gateway.receipt is not None and gateway.receipt["task_verified"] is False
    assert "private cleanup detail" not in str(saved)


def test_actual_ai_backend_keeps_idempotent_owned_cua_close_guard(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from desktop_runtime.cu_ai_cua import CuaMac
    calls = []
    admitted = []
    session = SimpleNamespace(call=lambda *_a, **_k: None, close=lambda: calls.append("owned_proxy"))
    def scoped_session(*args, **kwargs):
        admitted.append(kwargs)
        return session
    monkeypatch.setattr("desktop_runtime.cua_session.CuaSession", scoped_session)
    monkeypatch.setattr(eng, "PeekabooMac", lambda: None)
    monkeypatch.setenv("SUPYMEM_DATA_DIR", str(tmp_path))
    w = DeviceWorker(Gateway([]), lambda _: None, cua_socket="synthetic-owned-socket")
    from desktop_runtime.cu_reviewed_scope import ReviewedDesktopScope
    w._ai_scope = ReviewedDesktopScope.from_manifest(MANIFEST)
    mac = w._ai_backend()
    assert mac.close.__func__ is CuaMac.close
    assert admitted[0]['reviewed_surface'] == (1,1)
    assert mac.pid == 1 and mac.app == 'TextEdit'
    mac.close()
    mac.close()
    assert calls == ["owned_proxy"]


def test_worker_unknown_exception_body_is_not_failure_code_or_traceback(monkeypatch, caplog):
    import logging
    gateway = Gateway([])
    w, saved = worker(monkeypatch, gateway, FakeMac())
    def failed(*args, **kwargs):
        raise RuntimeError('private_api_token')
    monkeypatch.setattr(eng, 'run_loop', failed)
    with caplog.at_level(logging.ERROR):
        w.run_ai_loop({'state': 'approved'}, MANIFEST)
    assert w.last['failure_code'] == 'ai_loop_failed'
    assert 'private_api_token' not in str(saved) + caplog.text
    assert not any(record.exc_info for record in caplog.records)


def test_observed_control_state_reaches_both_step_and_independent_verifier(monkeypatch):
    fields = {'id': 'e1', 'ax_role': 'AXCheckBox', 'label': 'synthetic checkbox', 'value': '',
              'type': 'checkbox', 'name': 'synthetic-consent', 'checked': True, 'selected': False, 'focused': True,
              'untrusted_extra': 'must not forward'}
    class ContextGateway(Gateway):
        def ai_step(self, invocation_id, kind, context, png=None, **kwargs):
            if kind in {'step', 'verify'}:
                self.contexts[kind] = context
            return super().ai_step(invocation_id, kind, context, png, **kwargs)
    gateway = ContextGateway([reply(action('done', summary='synthetic'))])
    gateway.contexts = {}
    w, saved = worker(monkeypatch, gateway, FakeMac([[fields]]))
    w.run_ai_loop({'state': 'approved'}, MANIFEST)
    for kind in ('step', 'verify'):
        projected = gateway.contexts[kind]['elements'][0]
        for name in ('type', 'name', 'checked', 'selected', 'focused'):
            assert projected[name] == fields[name]
        assert 'untrusted_extra' not in projected
    assert gateway.receipt['task_verified'] is True
    assert 'synthetic-consent' not in str(saved)


@pytest.mark.parametrize('field,value', [('process_id',2), ('process_start_identity','2'), ('window_id',2),
                                       ('operation','desktop_goal_v2')])
def test_changed_reviewed_surface_refuses_before_backend_claim_or_provider(monkeypatch, field, value):
    gateway = Gateway([])
    w, _ = worker(monkeypatch, gateway, FakeMac())
    backend = []
    monkeypatch.setattr(w, '_ai_backend', lambda: backend.append(True))
    changed = {**MANIFEST, 'desktop_verification': {**MANIFEST['desktop_verification'], field:value}}
    w.run_ai_loop({'state':'approved'}, changed)
    assert backend == []
    assert gateway.claim_attempted is False
    assert gateway.kinds == []


def test_reviewed_desktop_rejects_ungranted_filesystem_before_executor(monkeypatch):
    gateway = Gateway([reply(action('fs', fs_op='write_text', path='/tmp/owned-note.txt', text='x'))])
    mac = FakeMac()
    w, _ = worker(monkeypatch, gateway, mac)
    effects = []
    monkeypatch.setattr('coding_agents.cu_ai_tools.run_fs', lambda *args, **kwargs: effects.append(True))
    w.run_ai_loop({'state':'approved'}, MANIFEST)
    assert effects == []
    assert mac.acted == []
    assert gateway.receipt['task_verified'] is False
    assert w.last['failure_code'] == 'ai_loop_desktop_scope_expansion_required'


@pytest.mark.parametrize('local_cap,expected', [('100',1), ('0',1), ('1',1)])
def test_actual_worker_local_step_cap_cannot_expand_reviewed_limit(monkeypatch,local_cap,expected):
    from types import SimpleNamespace
    gateway=Gateway([])
    w,_=worker(monkeypatch,gateway,FakeMac())
    captured=[]
    def loop(*args,**kwargs):
        captured.append(kwargs['caps'].max_steps)
        return SimpleNamespace(outcome='failed',reason='steps_cap',steps=0,seconds=0)
    monkeypatch.setattr(eng,'run_loop',loop)
    monkeypatch.setenv('SUPYMEM_CU_MAX_STEPS',local_cap)
    manifest={**MANIFEST,'desktop_verification':{**MANIFEST['desktop_verification'],'max_steps':1}}
    w.run_ai_loop({'state':'approved'},manifest)
    assert captured==[expected]


@pytest.mark.parametrize('bad_cap', [-1,0,True,'8',49,None])
def test_invalid_reviewed_step_limit_refuses_before_backend_and_claim(monkeypatch,bad_cap):
    gateway=Gateway([])
    w,_=worker(monkeypatch,gateway,FakeMac())
    started=[]
    monkeypatch.setattr(w,'_ai_backend',lambda:started.append(True))
    manifest={**MANIFEST,'desktop_verification':{**MANIFEST['desktop_verification'],'max_steps':bad_cap}}
    w.run_ai_loop({'state':'approved'},manifest)
    assert started==[] and gateway.claim_attempted is False


@pytest.mark.parametrize('local_cap', ['-1','invalid','', '１'])
def test_invalid_local_step_limit_cannot_start_or_claim(monkeypatch,local_cap):
    gateway=Gateway([])
    w,_=worker(monkeypatch,gateway,FakeMac())
    started=[]
    monkeypatch.setattr(w,'_ai_backend',lambda:started.append(True))
    monkeypatch.setenv('SUPYMEM_CU_MAX_STEPS',local_cap)
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert started==[] and gateway.claim_attempted is False


def test_local_step_limit_can_reduce_and_reviewed_cap_cannot_autoscale(monkeypatch):
    from types import SimpleNamespace
    gateway=Gateway([])
    w,_=worker(monkeypatch,gateway,FakeMac())
    captured=[]
    def loop(*args,**kwargs):
        cap=kwargs['caps']
        captured.append((cap.max_steps,cap.explicit))
        return SimpleNamespace(outcome='failed',reason='steps_cap',steps=0,seconds=0)
    monkeypatch.setattr(eng,'run_loop',loop)
    monkeypatch.setenv('SUPYMEM_CU_MAX_STEPS','2')
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert captured==[(2,True)]

@pytest.mark.parametrize('local_seconds', ['-1','invalid','','１'])
def test_seconds_setting_refuses_before_backend_and_claim(monkeypatch,local_seconds):
    gateway=Gateway([])
    w,_=worker(monkeypatch,gateway,FakeMac())
    started=[]
    monkeypatch.setattr(w,'_ai_backend',lambda:started.append(True))
    monkeypatch.setenv('SUPYMEM_CU_MAX_SECONDS',local_seconds)
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert started==[] and gateway.claim_attempted is False


def test_actual_stop_after_input_preserves_reserved_step(tmp_path,monkeypatch):
    gateway=Gateway([reply(action('key',chord='cmd+s'))])
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    def act_and_stop(a,obs):
        mac.acted.append(a.op)
        w.cancelled.set()
        return 'pressed cmd+s'
    mac.act=act_and_stop
    w.run_ai_loop({'state':'approved'},MANIFEST)
    path=tmp_path/'owned_worker_state/cu_checkpoints/dwo_ai.json'
    saved=json.loads(path.read_text())
    assert mac.acted==['key'] and saved['steps']==1
    assert saved['pending_dispatch'] is False
    assert saved['authority_binding']['execution_id']=='dex_ai'
    assert w.last['evidence']['steps_taken']==1
    assert gateway.receipt['task_verified'] is False


def test_foreign_checkpoint_rejects_before_model_or_input(tmp_path,monkeypatch):
    from coding_agents.cu_ai_loop import LoopState
    checkpoint=eng.FileCheckpoint(tmp_path/'owned_worker_state/cu_checkpoints/dwo_ai.json')
    checkpoint.save({**LoopState().snapshot(MANIFEST['instruction']),'pending_dispatch':False,
                     'authority_binding':{'execution_id':'foreign'}})
    gateway=Gateway([])
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert gateway.kinds==[] and mac.n==0 and mac.acted==[]
    assert gateway.receipt['task_verified'] is False


@pytest.mark.parametrize('predicate', ['missing','unmet','partial','stale','foreign','document_unmet'])
def test_model_done_cannot_override_missing_or_unmet_machine_predicate(monkeypatch,predicate):
    gateway=Gateway([reply(action('done',summary='done'))]*10)
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    spec={**MANIFEST['desktop_verification']}
    if predicate=='missing':
        spec.pop('expected_visible_text')
    if predicate=='unmet':
        spec['expected_visible_text']='required different result'
    if predicate in {'partial','document_unmet'}:
        spec.update(document_text_index=0,reviewed_text=['reviewed complete text'])
    observe=mac.observe
    def adjusted_observation():
        obs=observe()
        if predicate=='stale':
            mac._observation_at-=21
        if predicate=='foreign':
            mac.raw[0]['window_id']=99
        if predicate=='document_unmet':
            mac.raw.append({'pid':1,'window_id':1,'role':'AXTextArea','value':'different text',
                            'element_index':1,'element_token':'owned-token','enabled':True})
        return obs
    mac.observe=adjusted_observation
    w.run_ai_loop({'state':'approved'},{**MANIFEST,'desktop_verification':spec})
    assert gateway.receipt['task_verified'] is False
    assert 'verify' not in gateway.kinds
    assert w.last.get('verification_failure_code') in {
        'reviewed_goal_verification_required','reviewed_goal_evidence_unavailable','reviewed_goal_predicate_unmet'}
    assert mac.acted==[]


def test_all_reviewed_machine_predicates_then_independent_judge(monkeypatch):
    gateway=Gateway([reply(action('done',summary='done'))])
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    observe=mac.observe
    def observed_document():
        obs=observe()
        mac.raw.append({'pid':1,'window_id':1,'role':'AXTextArea','value':'reviewed complete text',
                        'element_index':1,'element_token':'owned-token','enabled':True})
        return obs
    mac.observe=observed_document
    manifest={**MANIFEST,'desktop_verification':{**MANIFEST['desktop_verification'],
              'document_text_index':0,'reviewed_text':['reviewed complete text']}}
    w.run_ai_loop({'state':'approved'},manifest)
    assert gateway.receipt['task_verified'] is True and gateway.kinds[-1]=='verify'


def test_claim_absolute_deadline_never_resets_from_worker_start(monkeypatch):
    from datetime import datetime,timedelta,timezone
    gateway=Gateway([])
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    claim=gateway.claim
    def expired_claim():
        value=claim()
        value['claimed_at']=(datetime.now(timezone.utc)-timedelta(seconds=601)).isoformat()
        return value
    gateway.claim=expired_claim
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert gateway.kinds==[] and mac.n==0 and mac.acted==[]
    assert gateway.receipt['task_verified'] is False


def test_goal_change_during_model_judge_cannot_verify_stale_predicate(monkeypatch):
    gateway=Gateway([reply(action('done',summary='done'))]*10)
    mac=FakeMac()
    w,_=worker(monkeypatch,gateway,mac)
    observe=mac.observe
    def changed_final_observation():
        obs=observe()
        if 'verify' in gateway.kinds:
            mac.raw[0]['label']='changed result'
        return obs
    mac.observe=changed_final_observation
    w.run_ai_loop({'state':'approved'},MANIFEST)
    assert 'verify' in gateway.kinds and gateway.receipt['task_verified'] is False
    assert mac.acted==[]
