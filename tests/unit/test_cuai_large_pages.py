"""Synthetic DOM observations only; no browser or provider is started."""
import copy
import json
import re
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

from coding_agents.owned_browser import OwnedBrowser
from coding_agents.cu_page_context import bounded_page_observation, bounded_page_text
from desktop_runtime.device_gateway import WorkOrderGateway
from desktop_runtime.cu_owned_browser_adapter import CuOwnedBrowserAdapter
from tests.unit.product.test_cu_owned_browser_adapter import profile, profile_digest


@pytest.mark.parametrize("shape,title,heading,task,fact,label", [
    ("article", "Fleet analytics handbook", "Migration planning",
     "Read the fleet handbook title and migration latency for enterprise operators.",
     "Migration latency is bounded by the regional queue.", "界" * 300),
    ("table", "Community pottery schedule", "Weekend workshops",
     "Read the pottery schedule title and weekend availability for families.",
     "Weekend availability includes an afternoon family workshop.", '陶"\\' * 100),
], ids=["b2b-article", "consumer-table"])
@pytest.mark.parametrize("max_bytes", [65536, 26000])
def test_large_page_request_preserves_source_and_task_context(tmp_path, monkeypatch,
        shape, title, heading, task, fact, label, max_bytes):
    filler = ("Unrelated archival paragraph.\n" if shape == "article"
              else "Archived session\tclosed\tno seats\n") * 12000
    private = 'sk-' + 'a' * 32
    row = ('陶' * 2550 + ' ' + fact + ' ' + '陶' * 2550) if shape == 'table' else fact
    text = "FIRST SCREEN: current public overview\n" + private + "\n" + filler + row + "\n" + filler
    viewport_text = "FIRST SCREEN: current public overview\n" + "Current visible archive entries.\n" * 180
    headings = [heading, *(f'{heading.split()[0]} archive heading {index}' for index in range(120))]
    for budget in (64, 120, 1000, 4000):
        excerpt = bounded_page_text(text, task, budget)
        assert excerpt == bounded_page_text(text, task, budget)
        assert len(json.dumps(excerpt).encode()) <= budget
        marker = re.search(r'\n\[truncated ([0-9]+) chars\]$', excerpt)
        assert marker and marker.start() + int(marker.group(1)) == len(text)
        narrowed = bounded_page_text(excerpt, task, 64)
        marker = re.search(r'\n\[truncated ([0-9]+) chars\]$', narrowed)
        assert marker and marker.start() + int(marker.group(1)) == len(text)
    browser = OwnedBrowser(order_id="dwo_large", goal=task, origins=["https://owned.example"],
        authorize=lambda *_: True, admit_effect=lambda *_: pytest.fail("No effects"),
        general_mode=True, cuai_mode=True, bounded_reads=True, read_only=True,
        profile_dir=tmp_path / "unused-profile")
    handles = [SimpleNamespace(is_visible=lambda: True, is_enabled=lambda: True) for _ in range(5000)]
    class Page:
        url = "https://owned.example/public"
        def evaluate_handle(self, script, args):
            assert args[1] == 100 and "matches.length < limit" in script and "6000" in script
            return SimpleNamespace(get_properties=lambda: {
                str(index): SimpleNamespace(as_element=lambda handle=handle: handle)
                for index, handle in enumerate(handles[:args[1]])}, dispose=lambda: None)
        def locator(self, _selector):
            return SimpleNamespace(element_handles=lambda: handles)
        def evaluate(self, _script, _key):
            return {"text": text[:4000], "page_text": text, "title": title,
                "headings": headings, "viewport": viewport_text,
                "visible_text": True, "visibility_omitted": False, "location": self.url,
                "document_id": "c" * 32, "ready_state": "complete",
                "source": {"facts": [heading], "fields": [],
                    "omitted": {"facts": len(headings) - 1, "fields": 0, "values": 0}}}
    browser._page = Page()
    browser._load_pages[browser._page] = {"page_id": "b" * 32, "sequence": 1,
        "committed_sequence": 1, "document_id": "c" * 32,
        "response": {"url": browser._page.url, "status": 200, "complete": True, "sequence": 1}}
    monkeypatch.setattr(browser, "_check", lambda: None)
    monkeypatch.setattr(browser, "_finish_expected_navigation", lambda: None)
    monkeypatch.setattr(browser, "_snapshot", lambda _handle: {"label": label, "tag": "BUTTON",
        "href": None, "form": False, "submit": False, "input_type": "button",
        "name": "", "autocomplete": "", "role": "", "terms_checkbox": False, "captcha": False})
    observation = browser.observe()
    digest = observation.digest()
    assert browser.verified_load(observation)
    execution_profile = profile()
    execution_profile['max_request_bytes'] = max_bytes
    saved = {"manifest": {"instruction": task, "actions": ["browser.read"],
        "general_browser_verification": {"operation": "general_goal_v1",
            "start_url": browser._page.url, "execution_profile": execution_profile}}}
    captured = []
    replies = {"plan": {"subgoals": [task]}, "actor": {"actions": [{"op": "wait", "ms": 1}],
        "memory": "", "subgoal_done": False}, "verify": {"passed": False, "reason": "Needs review"}}
    gateway = WorkOrderGateway(venture_id="ven_large", order_id="dwo_large", device_id="dev_large",
        request=lambda method, path, body: captured.append(body) or {
            "authority": "proposal_only", "source_kind": "browser_cuai_text_v1",
            "invocation_id": body["invocation_id"], "phase": body["phase"],
            "profile_digest": profile_digest(execution_profile), "observation_digest": observation.digest(),
            "model": execution_profile["roles"][body["phase"]]["model"], "service_tier": "default",
            "input_tokens": 1, "output_tokens": 1, "reply": json.dumps(replies[body["phase"]]), "effect_proposals": []})
    gateway.browser_cuai_profile_digest = profile_digest(execution_profile)
    gateway.cancellable_request = lambda method, path, body, **_kwargs: gateway.request(method, path, body)
    monkeypatch.setattr(gateway, "current", lambda **_kwargs: saved)
    from tests.company_os_harness import load_company_os_app
    load_company_os_app()
    from app.build import device_browser_ai as ai
    adapter = CuOwnedBrowserAdapter(browser=browser, gateway=gateway, profile=execution_profile,
        goal=task, stop=threading.Event(), deadline=time.monotonic() + 60)
    adapter.subgoals = [task]
    for phase in ("plan", "actor", "verify"):
        adapter._call(phase, observation)
        wire = captured[-1]
        assert len(json.dumps(wire).encode()) <= execution_profile["max_request_bytes"]
        request = ai.BrowserAIBody.model_validate(wire)
        before = copy.deepcopy(request.model_dump())
        provider_body = ai.request_body(saved, request, ai.orders.BrowserAIProfile.model_validate(execution_profile),
            request.observation)
        assert len(json.dumps(provider_body).encode()) <= execution_profile["max_request_bytes"]
        sent_text = json.dumps(provider_body, ensure_ascii=False)
        assert all(value in sent_text for value in (title, heading, fact, browser._page.url, "FIRST SCREEN"))
        assert "[truncated " in sent_text
        assert private not in json.dumps(wire) and private not in sent_text
        assert request.model_dump() == before
        assert request.current_load.observation_digest == observation.digest()
        assert json.loads(request.context.notes)['current']['observation_digest'] == observation.digest()
        assert observation.location == browser._page.url and observation.goal == task
        assert [control.id for control in observation.controls] == [f'c{index}' for index in range(len(observation.controls))]
        if max_bytes == 65536:
            assert observation.digest() == digest
    assert len(captured) == 3 and browser.verified_load(observation)


@pytest.mark.parametrize("task", [
    "Read logistics capacity for warehouse operators.",
    "Read a hiking route title for families.",
], ids=["b2b", "consumer"])
def test_structural_observation_budget_includes_control_ids_operations_and_escaping(task):
    from coding_agents.jev_decision import Control, Observation
    observation = Observation.model_construct(goal=task, context="PAGE TITLE: " + task + "\n" + "界\"\\" * 100000,
        controls=[Control(id=f"c{index}", label="界\"\\" * 50, operations=["click", "type"],
            visible=True, enabled=True) for index in range(100)])
    projected = bounded_page_observation(observation, 1600)
    assert len(json.dumps(projected.context).encode()) + len(json.dumps(
        [control.model_dump() for control in projected.controls]).encode()) <= 1600
    assert len(projected.controls) < 100 and "absence is not verified" in projected.context
    assert projected.goal == task and observation.controls[0].label == "界\"\\" * 50


@pytest.mark.parametrize("title,task", [
    ("Manufacturing inventory handbook", "Read the handbook title for factory operators."),
    ("Coastal walking guide", "Read the guide title for weekend walkers."),
], ids=["b2b", "consumer"])
def test_long_dom_observation_is_bounded_before_transport_without_a_browser(tmp_path, monkeypatch, title, task):
    browser = OwnedBrowser(order_id="dwo_synthetic", goal=task, origins=["https://owned.example"],
        authorize=lambda *_: True, admit_effect=lambda *_: pytest.fail("No effects"),
        general_mode=True, cuai_mode=True, bounded_reads=True, read_only=True,
        profile_dir=tmp_path / "unused")
    browser.fill_values = {"literal": "CUAI_TERMS"}
    handles = [SimpleNamespace(is_visible=lambda: True, is_enabled=lambda: True) for _ in range(5000)]
    returned = []
    class Page:
        url = "https://owned.example/guide"
        def evaluate_handle(self, script, args):
            assert "matches.length < limit" in script and "element.shadowRoot" in script and args[1] == 100
            indices = self.evaluate(script, args)
            assert indices == [str(index) for index in range(100)]
            returned.extend(handles[int(index)] for index in indices)
            return SimpleNamespace(get_properties=lambda: {str(index): SimpleNamespace(
                as_element=lambda handle=handle: handle) for index, handle in enumerate(returned)}, dispose=lambda: None)
        def evaluate(self, script, key):
            assert task not in script
            program = r'''
const input=JSON.parse(require('fs').readFileSync(0,'utf8'));
input.title+=' detailed metadata'.repeat(60);
global.Node={TEXT_NODE:3,ELEMENT_NODE:1};global.NodeFilter={SHOW_TEXT:4,SHOW_ELEMENT:1};
global.innerHeight=800;global.innerWidth=1000;global.location={href:'https://owned.example/guide'};
const rect={left:0,top:0,right:500,bottom:20,width:500,height:20};
const element=index=>({nodeType:1,tagName:index===0?'H1':'P',innerText:index===0?input.title:'Public guide fact '+index,
  closest:()=>null,checkVisibility:()=>true,getBoundingClientRect:()=>rect,parentElement:null,
  childNodes:[{nodeType:3,textContent:index===0?input.title:'Public guide fact '+index}],
  querySelectorAll:()=>[],getAttribute:()=>null});
const nodes=Array.from({length:5000},(_,index)=>element(index));
const links=Array.from({length:5000},(_,index)=>({...element(index),id:String(index),tagName:'A',matches:()=>true}));
const host={matches:()=>false,shadowRoot:{children:[links[0]]}};
const largeLabel={...element(1),childNodes:nodes};
const fields=Array.from({length:5000},()=>({...element(1),tagName:'INPUT',type:'text',labels:[largeLabel]}));
const witness={data:input.title,parentElement:nodes[0]};
global.document={children:[host,...links.slice(1)],body:{innerText:'Public guide prose. '.repeat(16000)+input.task},title:input.title,readyState:'complete',
  querySelectorAll:selector=>selector==='input,select,textarea'?fields:nodes,
  createTreeWalker:(root,filter)=>{let index=0;const values=filter===NodeFilter.SHOW_TEXT?[witness]:root.children;
    return {nextNode:()=>values[index++]||null};},
  createRange:()=>({selectNodeContents:()=>{},getClientRects:()=>[rect]})};
document[input.key]='c'.repeat(32);
global.getComputedStyle=()=>({color:'black',webkitTextFillColor:'black',clipPath:'none',maskImage:'none',
  filter:'none',transform:'none',overflowX:'visible',overflowY:'visible'});
const result=eval('('+input.script+')')(input.key);
process.stdout.write(JSON.stringify(Array.isArray(result)?result.map(node=>node.id):result));
'''
            result = subprocess.run(["node", "-e", program],
                input=json.dumps({"script": script, "key": key, "title": title, "task": task}), text=True,
                capture_output=True, check=True, timeout=10)
            raw = json.loads(result.stdout)
            if isinstance(raw, list):
                return raw
            assert len(raw["page_text"]) < 12100 and "[truncated " in raw["page_text"]
            assert task in raw["page_text"]
            assert len(raw["title"]) <= 300 and title in raw["title"] and 'omitted text is not verified' in raw["title"]
            assert all(len(heading) <= 160 for heading in raw["headings"])
            assert len(raw["headings"]) <= 64 and len(raw["viewport"]) <= 6020
            assert len(raw["source"]["facts"]) <= 32 and raw["source"]["omitted"]["facts"] > 0
            assert raw["source"]["fields"] == [] and raw["source"]["omitted"]["fields"] == 5000
            assert len(result.stdout.encode()) < 30000
            return raw
    browser._page = Page()
    browser._load_pages[browser._page] = {"page_id": "b" * 32, "sequence": 1,
        "committed_sequence": 1, "document_id": "c" * 32,
        "response": {"url": browser._page.url, "status": 200, "complete": True, "sequence": 1}}
    monkeypatch.setattr(browser, "_check", lambda: None)
    monkeypatch.setattr(browser, "_finish_expected_navigation", lambda: None)
    monkeypatch.setattr(browser, "_snapshot", lambda _: {"label": "Next guide", "tag": "A",
        "href": browser._page.url, "form": False, "submit": False, "input_type": "", "name": "",
        "autocomplete": "", "role": "", "terms_checkbox": False, "captcha": False})
    observation = browser.observe()
    assert len(returned) == len(observation.controls) == 100 and browser.verified_load(observation)
    assert title in observation.context and len(json.dumps(observation.context).encode()) <= 4000
    assert len(browser.cuai_navigation_targets(observation)) == 16
    assert len(browser.cuai_readback(observation).encode()) <= 5000


@pytest.mark.parametrize("task", [
    "Read the manufacturing handbook title for factory operators.",
    "Read the walking guide title for weekend hikers.",
], ids=["b2b", "consumer"])
@pytest.mark.parametrize("complete", [False, True])
def test_pending_read_only_page_blocks_model_preflight_without_extra_load_waits(tmp_path, monkeypatch, task, complete):
    browser = OwnedBrowser(order_id="dwo_settle", goal=task, origins=["https://owned.example"],
        authorize=lambda *_: True, admit_effect=lambda *_: pytest.fail("No effects"),
        general_mode=True, cuai_mode=True, read_only=True, profile_dir=tmp_path / "unused")
    events = []
    class Page:
        url = "https://owned.example/source"
        ready_state = "complete" if complete else "interactive"
        def wait_for_load_state(self, state, *, timeout):
            pytest.fail("Ordinary read-only observations must not add a load wait")
        def evaluate_handle(self, script, args):
            events.append("snapshot")
            return SimpleNamespace(get_properties=lambda: {}, dispose=lambda: None)
        def evaluate(self, script, key):
            events.append("readback")
            return {"text": task, "page_text": task, "title": task, "headings": [], "viewport": task,
                "visible_text": True, "visibility_omitted": False, "location": self.url,
                "document_id": "c" * 32, "ready_state": self.ready_state,
                "source": {"facts": [task], "fields": [], "omitted": {"facts": 0, "fields": 0, "values": 0}}}
    browser._page = Page()
    browser._load_pages[browser._page] = {"page_id": "b" * 32, "sequence": 1, "committed_sequence": 1,
        "document_id": "c" * 32, "response": {"url": browser._page.url, "status": 200, "complete": True, "sequence": 1}}
    monkeypatch.setattr(browser, "_check", lambda: None)
    monkeypatch.setattr(browser, "_finish_expected_navigation", lambda: None)
    observation = browser.observe()
    assert events == ["snapshot", "readback"]
    assert browser.verified_load(observation) is complete
    sent = []
    def provider(invocation, phase, obs, **kwargs):
        sent.append(phase)
        return {"reply": json.dumps({"subgoals": [task]}), "input_tokens": 1, "output_tokens": 1}
    execution_profile = profile()
    gateway = SimpleNamespace(browser_cuai_profile_digest=profile_digest(execution_profile), order_id="dwo_settle",
        execution_id="dex_settle", current=lambda: None, browser_ai_call=provider)
    adapter = CuOwnedBrowserAdapter(browser=browser, gateway=gateway, profile=execution_profile, goal=task,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    if not complete:
        with pytest.raises(PermissionError, match="browser_cuai_evidence_unavailable"):
            adapter._call("plan", observation)
        assert adapter.calls == 0 and sent == []
    else:
        adapter._call("plan", observation)
        assert adapter.calls == 1 and sent == ["plan"]


@pytest.mark.parametrize('task', [
    'Read capacity estimates for warehouse operators.',
    'Read weekend class availability for families.',
], ids=['b2b', 'consumer'])
@pytest.mark.parametrize('failure,code', [
    ('scope', 'cu_browser_decision_scope_invalid'),
    ('private', 'cu_browser_decision_runtime_error'),
    ('deadline', 'cu_browser_decision_timeout'),
    ('provider', 'cu_browser_provider_outcome_unknown'),
    ('plan', 'cu_browser_plan_invalid'),
    ('busy', 'cu_browser_transport_busy'),
    ('transport', 'cu_browser_transport_unconfirmed'),
])
def test_unavailable_decision_stop_receipt_keeps_specific_private_safe_reason(task, failure, code):
    from desktop_runtime.cancellable_http import ModelRequestDeadline
    from desktop_runtime.device_gateway import UpstreamRejected
    from tests.unit.product.test_cu_owned_browser_adapter import Authority, gateway

    authority = Authority([])
    if failure == 'plan':
        authority.broker.replies = [{'subgoals': []}]
    broker = gateway(authority)
    broker.claim()
    authority.manifest['instruction'] = task
    from tests.unit.product.test_cu_owned_browser_adapter import Browser
    browser = Browser(goal=task)
    attempts = []
    def unavailable(method, path, body, **_kwargs):
        if not path.endswith('/browser-ai-call'):
            return authority.request(method, path, body)
        attempts.append(body['phase'])
        if failure == 'scope':
            raise PermissionError('native_model_request_scope_invalid')
        if failure == 'deadline':
            raise ModelRequestDeadline()
        if failure == 'provider':
            raise UpstreamRejected(409, 'provider_outcome_unknown')
        if failure == 'plan':
            return authority.request(method, path, body)
        if failure == 'busy':
            raise RuntimeError('native_model_transport_busy')
        if failure == 'transport':
            raise RuntimeError('native_worker_request_unconfirmed')
        raise RuntimeError('PRIVATE_ADDRESS@example.invalid SECRET_PROVIDER_BODY')
    broker.cancellable_request = unavailable
    adapter = CuOwnedBrowserAdapter(browser=browser, gateway=broker, profile=profile(), goal=task,
        stop=threading.Event(), deadline=time.monotonic() + 60)
    with pytest.raises((PermissionError, RuntimeError)):
        adapter.decide(browser.observe())
    broker.stop_receipt('a' * 64, True, reason_code='cu_browser_decision_unavailable')
    stops = [body for _, path, body in authority.requests if path.endswith('/stop-receipt')]
    assert len(stops) == 1 and stops[0]['reason_code'] == code
    assert attempts == ['plan'] and authority.effects == []
    assert 'PRIVATE_ADDRESS' not in json.dumps(stops) and 'SECRET_PROVIDER_BODY' not in json.dumps(stops)
