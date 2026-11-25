"""One engine, two backends: web steps go to the DOM/CDP backend, native steps to AX/vision; CAPTCHA and
refusals (password/payment) pause for the founder instead of being worked around."""
import threading
from types import SimpleNamespace

import pytest
from coding_agents.cu_ai_loop import Action, ActionError, parse_reply
from desktop_runtime.cu_ai_browser import BrowserMac, RoutedMac, playwright_key
from desktop_runtime.cu_ai_engine import NeedsHuman, Observation, run_loop, wait_for_human


class FakeBackend:
    def __init__(self, status="ok"):
        self.sent, self.status, self.captcha = [], status, False

    def observe(self):
        return SimpleNamespace(url="https://fixture-fitness.supymem.com", title="Club Attendance", text="Headline here",
                               tabs=[{"i": 0, "title": "Gyms", "active": True}], captcha=self.captcha,
                               elements=[{"i": 3, "tag": "input", "label": "Email", "placeholder": "you@x.com"},
                                         {"i": 4, "tag": "a", "text": "Pricing", "href": "/p"}])

    def act(self, payload):
        self.sent.append(payload)
        return {"status": self.status, "action": payload["action"], "error": "password field", "seconds": 0.1}


def a(op, **kw):
    return Action(op, **kw)


def test_navigate_requires_an_http_url():
    base = {"thought": "", "memory": "", "subgoal_done": False}
    ok = {"op": "navigate", "text": "https://example.com/a", "element_id": "", "x": 0, "y": 0, "chord": "", "direction": "",
          "amount": 0, "app": "", "ms": 0, "summary": ""}
    assert parse_reply({**base, "actions": [ok]}).actions[0].op == "navigate"
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "example.com", ""):
        with pytest.raises(ActionError):
            parse_reply({**base, "actions": [{**ok, "text": bad}]})


def test_observation_maps_dom_to_b_ids_with_page_text_and_no_screen_capture():
    backend = FakeBackend()
    page = backend.observe()
    mac = BrowserMac(backend)
    obs = mac.observe()
    assert [e["id"] for e in obs.elements] == ["b3", "b4"] and obs.png == b"" and "Headline here" in obs.notes
    assert obs.frontmost == "Chrome (browser)" and obs.window_title == f"{page.url} | {page.title}"


def test_actions_map_to_cdp_never_global_keystrokes():
    backend = FakeBackend()
    mac = BrowserMac(backend)
    obs = mac.observe()
    mac.act(a("navigate", text="https://x.com"), obs)
    obs = mac.observe()
    mac.act(a("click", element_id="b4"), obs)
    mac.act(a("type", element_id="b3", text="me@x.com"), obs)
    mac.act(a("key", chord="cmd+shift+t"), obs)
    mac.act(a("key", chord="enter"), obs)
    assert [p["action"] for p in backend.sent] == ["navigate", "click", "type", "press", "press"]
    assert backend.sent[3]["key"] == "Meta+Shift+t" and backend.sent[4]["key"] == "Enter"
    with pytest.raises(RuntimeError):
        mac.act(a("menu", text="File > Save"), obs)
    assert playwright_key("ctrl+a") == "Control+a"


def test_routed_external_invalidation_reaches_native_and_browser_without_effect():
    invalidated = []
    native = SimpleNamespace(invalidate_observation=lambda: invalidated.append(True))
    backend = FakeBackend()
    browser = BrowserMac(backend)
    obs = browser.observe()
    routed = RoutedMac(native, browser)
    routed.invalidate_observation()
    assert invalidated == [True] and browser.last is None
    with pytest.raises(RuntimeError, match="fresh_browser_observation_required"):
        browser.act(a("click", element_id="b3"), obs)
    assert backend.sent == []
    browser.act(a("click", element_id="b3"), browser.observe())
    assert len(backend.sent) == 1


def test_captcha_and_refusal_raise_needs_human():
    for status, kind in (("captcha", "captcha"), ("refused", "refused")):
        mac = BrowserMac(FakeBackend(status))
        with pytest.raises(NeedsHuman) as caught:
            mac.act(a("click", element_id="b3"), mac.observe())
        assert caught.value.kind == kind


def test_routing_switches_per_step_and_browser_apps_are_not_launched_natively():
    native = SimpleNamespace(calls=[], observe=lambda: Observation(b"n", [], "TextEdit", "Doc", "", False, 1),
                             frontmost=lambda: ("TextEdit", 1), focused_role=lambda: None, activate=lambda n: None)
    native.act = lambda action, obs: native.calls.append(action.op) or "native ok"
    backend = FakeBackend()
    mac = RoutedMac(native, BrowserMac(backend))
    obs = mac.observe()
    assert obs.frontmost == "TextEdit"
    assert "browser mode" in mac.act(a("open_app", app="Google Chrome"), obs) and mac.mode == "browser"
    assert mac.observe().frontmost == "Chrome (browser)"
    mac.act(a("navigate", text="https://fixture-fitness.supymem.com"), obs)
    mac.act(a("open_app", app="TextEdit"), obs)            # back to a native app
    assert mac.mode == "native" and native.calls == ["open_app"] and len(backend.sent) == 1
    mac.act(a("type", text="headline"), obs)
    assert native.calls == ["open_app", "type"]


def test_captcha_on_a_page_pauses_the_loop_notifies_and_resumes():
    backend = FakeBackend()
    backend.captcha = True
    mac = BrowserMac(backend)
    sent, resume = [], threading.Event()
    threading.Timer(0.05, resume.set).start()

    def pause(kind, prompt, obs):
        backend.captcha = False                      # the founder solved it
        return wait_for_human(kind, prompt, mac, threading.Event(), resume, notify=sent.append, poll=0.01)
    done = {"thought": "", "memory": "", "subgoal_done": False, "actions": [
        {"op": "done", "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0,
         "app": "", "ms": 0, "summary": "signed up"}]}
    steps = iter([done, done, done])    # a `done` while the CAPTCHA is up is rejected, then the founder is asked
    result = run_loop("sign up", lambda p: (next(steps), {}), mac, threading.Event(), pause=pause, verify=lambda *args: (True, ""))
    assert sent and "Continue" in sent[0] and result.outcome == "done"


def test_routed_close_attempts_native_after_browser_failure():
    import pytest
    from types import SimpleNamespace
    from desktop_runtime.cu_ai_browser import RoutedMac
    closed = []
    def broken():
        closed.append("browser")
        raise RuntimeError("private backend detail")
    mac = RoutedMac(SimpleNamespace(close=lambda: closed.append("native")), SimpleNamespace(close=broken))
    with pytest.raises(RuntimeError, match="cu_backend_close_unconfirmed") as error:
        mac.close()
    assert closed == ["browser", "native"] and "private" not in str(error.value)


def test_browser_close_attempts_owned_process_even_when_cdp_close_fails():
    closed = []
    def broken():
        closed.append("cdp")
        raise RuntimeError("private error")
    mac = BrowserMac(backend=SimpleNamespace(close=broken), factory=SimpleNamespace(stop=lambda: closed.append("owned_chrome")))
    with pytest.raises(RuntimeError, match="cu_browser_close_unconfirmed"):
        mac.close()
    assert closed == ["cdp", "owned_chrome"]


def test_browser_scoped_form_state_survives_projection_and_four_choice_actions():
    from coding_agents.cu_ai_loop import Caps, element_lines
    class Choices(FakeBackend):
        def __init__(self):
            super().__init__()
            self.checked = [False] * 4
        def observe(self):
            raw = super().observe()
            raw.elements = [{'i': i, 'tag': 'input', 'type': 'checkbox', 'label': f'Choice {i}',
                             'value': 'on', 'checked': checked, 'selected': None, 'focused': False}
                            for i, checked in enumerate(self.checked)]
            return raw
        def act(self, payload):
            assert payload['action'] == 'click'
            self.checked[payload['index']] = not self.checked[payload['index']]
            return super().act(payload)
    backend, turns = Choices(), []
    mac = BrowserMac(backend)
    first = mac.observe()
    assert first.elements[0]['checked'] is False and first.elements[0]['focused'] is False
    assert 'checked=false' in element_lines(first.elements)[0]
    def decide(payload):
        turns.append(payload)
        n = len(turns) - 1
        action = {'op': 'click', 'element_id': f'b{n}'} if n < 4 else {'op': 'done', 'summary': 'choices selected'}
        return {'thought': '', 'memory': '', 'subgoal_done': True, 'actions': [action]}, {}
    result = run_loop('Select the reviewed choices in the browser', decide, mac, threading.Event(),
                      caps=Caps(max_steps=10, repeat_fail=99, no_progress_fail=99, explicit=True),
                      verify=lambda *args: (all(backend.checked), 'independent synthetic backend readback'))
    assert result.outcome == 'done' and len(backend.sent) == 4
    last = mac.observe()
    assert all(e['checked'] is True for e in last.elements)
    assert 'checked=true' in element_lines(last.elements)[0]


def test_browser_select_and_radio_selection_are_meaningful_scoped_state():
    from desktop_runtime.cu_ai_retries import semantic_state
    backend = FakeBackend()
    raw = backend.observe()
    raw.elements = [{'i': 0, 'tag': 'select', 'value': 'first', 'focused': True},
                    {'i': 1, 'tag': 'input', 'type': 'radio', 'name': 'choice', 'checked': True},
                    {'i': 2, 'tag': 'input', 'type': 'radio', 'name': 'choice', 'checked': False}]
    backend.observe = lambda: raw
    mac = BrowserMac(backend)
    before = semantic_state(mac.observe())
    raw.elements[1]['checked'], raw.elements[2]['checked'] = False, True
    radio_changed = semantic_state(mac.observe())
    assert radio_changed != before  # swapping two same-role checks is not a sorted-multiset noop
    raw.elements[0]['value'] = 'second'
    assert semantic_state(mac.observe()) != radio_changed


def test_completion_evidence_does_not_launch_or_create_browser():
    from desktop_runtime.cu_ai_browser import BrowserMac, RoutedMac
    def forbidden():
        raise AssertionError("evidence must not manufacture a browser")
    browser = BrowserMac(factory=forbidden)
    browser.reset_completion_evidence()
    assert browser.completion_evidence() is None
    routed = RoutedMac(object(), browser)
    routed.reset_completion_evidence()
    assert routed.completion_evidence() is None


def test_password_value_and_text_are_redacted_at_adapter_boundary():
    import json
    secret = 'synthetic-password-never-provider'
    backend = FakeBackend()
    backend.observe = lambda: SimpleNamespace(url='https://owned.test', title='Private form', text='Form',
        tabs=[], captcha=False, elements=[{'i': 1, 'tag': 'input', 'type': 'PASSWORD',
                                           'text': secret, 'value': secret}])
    observed = BrowserMac(backend=backend).observe()
    assert secret not in json.dumps(observed.elements) + observed.notes
    assert observed.elements[0]['value'] == ''
