"""Browser harness behaviour added for speed/accuracy: recoverable action errors, batching, compact schema, readback."""
import json
from types import SimpleNamespace

import pytest
from coding_agents import cu_ai_loop as loop
from desktop_runtime.cu_ai_browser import BrowserMac
from desktop_runtime.cu_ai_cua import CuaError
from desktop_runtime.cu_ai_retries import semantic_state


class Backend:
    downloads: list = []

    def __init__(self, status="ok", url="https://x.test/form"):
        self.status, self.url, self.page = status, url, SimpleNamespace(url=url)

    def observe(self):
        return SimpleNamespace(url=self.url, title="T", text="Count: 1", captcha=False,
                               tabs=[{"i": 0, "title": "A", "active": True, "id": "t" + "a" * 32},
                                     {"i": 1, "title": "B", "active": False, "id": "t" + "b" * 32}],
                               elements=[{"i": 0, "tag": "input", "label": "Project", "name": "project"},
                                         {"i": 1, "tag": "input", "type": "password", "label": "Pw"}])

    def act(self, payload):
        return {"status": self.status, "error": "TimeoutError: element not visible", "no_effect": True, "seconds": 0.1}


def act(mac, op, **kw):
    obs = mac.observe()
    return mac.act(loop.Action(op, **kw), obs)


def test_admitted_pre_dispatch_error_stays_retryable():
    with pytest.raises(CuaError) as error:
        act(BrowserMac(Backend(status="error")), "click", element_id="b0")
    assert error.value.no_effect is True
    with pytest.raises(CuaError) as stale:
        BrowserMac(Backend()).act(loop.Action("click", element_id="b0"), None)
    assert stale.value.no_effect is True


def test_error_without_backend_no_effect_proof_is_not_retryable():
    class Uncertain(Backend):
        def act(self, payload):
            return {"status": "error", "error": "TimeoutError: element not visible", "seconds": 0.1}
    with pytest.raises(CuaError) as caught:
        act(BrowserMac(Uncertain()), "click", element_id="b0")
    assert caught.value.no_effect is not True


def test_new_task_reset_clears_old_browser_content_evidence():
    mac = BrowserMac(Backend())
    act(mac, "type", element_id="b0", text="previous task")
    assert mac.browser_readback()
    mac.reset_completion_evidence()
    assert mac.browser_readback() == ""


def test_readback_retains_footer_of_observed_long_page():
    class LongPage(Backend):
        def observe(self):
            observation = super().observe()
            observation.text = "Paragraph " * 200 + " Footer token: synthetic-footer-token"
            return observation
    mac = BrowserMac(LongPage())
    mac.observe()
    assert "synthetic-footer-token" in mac.browser_readback()


def test_actor_can_carry_observed_response_to_next_page_with_task_owned_provenance():
    class Workflow(Backend):
        def observe(self):
            observation = super().observe()
            observation.text = ('Accepted; reference: synthetic-previous-response' if self.url.endswith('/first')
                                else 'Next page ' + 'ordinary page text ' * 100)
            return observation

    backend = Workflow(url='https://authorized.example/first')
    mac = BrowserMac(backend)
    mac.observe()
    backend.url = backend.page.url = 'https://authorized.example/second'
    observation = mac.observe()
    assert 'PRIOR PAGE READBACK' in observation.notes
    assert 'https://authorized.example/first' in observation.notes
    assert 'synthetic-previous-response' in observation.notes
    request = loop.build_user_text('continue the workflow', [], 0, '', [], observation.frontmost,
                                   observation.window_title, observation.elements, notes=observation.notes)
    assert 'synthetic-previous-response' in request
    assert 'untrusted historical excerpts, not current state' in request
    mac.reset_completion_evidence()
    assert 'synthetic-previous-response' not in mac.observe().notes


def test_latest_form_state_stays_explicit_after_long_history():
    class Form(Backend):
        def form_state(self):
            return "Project='Beacon'; Email='qa@example.invalid'; Code='ABC123'"
        def observe(self):
            observation = super().observe()
            observation.elements = [{"i": 0, "tag": "button", "type": "submit", "text": "Submit"}]
            observation.text = "Long paragraph " * 100 + " Accepted synthetic form"
            return observation
    mac = BrowserMac(Form())
    mac.did = ["old action " + "界" * 150] * 16
    act(mac, "click", element_id="b0")
    readback = mac.browser_readback()
    assert "LATEST FORM CONTROLS BEFORE CLICK" in readback and "Code='ABC123'" in readback
    assert "Accepted synthetic form" in readback and len(readback.encode()) <= 1500


def test_tabs_show_switch_tokens_only_when_several_tabs():
    notes = BrowserMac(Backend()).observe().notes
    assert "t" + "b" * 32 in notes


def test_readback_records_typed_values_but_never_passwords_and_pages_seen():
    mac = BrowserMac(Backend())
    obs = mac.observe()
    mac.act(loop.Action("type", element_id="b0", text="Atlas"), obs)
    mac.act(loop.Action("type", element_id="b1", text="hunter2"), obs)
    text = mac.browser_readback()
    assert "'Atlas'" in text and "hunter2" not in text and "[hidden]" in text and "Count: 1" in text
    assert len(text) <= 1800
    assert BrowserMac(Backend()).browser_readback() == ""


def test_page_text_is_progress_for_the_retry_guard():
    def obs(count):
        return SimpleNamespace(frontmost="Chrome (browser)", pid=None, notes=f"PAGE URL: u\nTABS: t\nPAGE TEXT: Count: {count}",
                               elements=[{"ax_role": "button", "id": "b0"}])
    assert semantic_state(obs(1)) != semantic_state(obs(2)) and semantic_state(obs(1)) == semantic_state(obs(1))


def reply(*actions):
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": list(actions)}


def test_form_fill_batches_fields_then_one_final_click():
    fill = [{"op": "type", "element_id": f"b{i}", "text": "v"} for i in range(3)]
    parsed = loop.parse_reply(reply(*fill, {"op": "click", "element_id": "b3"}))
    assert [a.op for a in parsed.actions] == ["type", "type", "type", "click"]
    # a final web click ends the batch; a non-web click never joins one
    assert len(loop.parse_reply(reply({"op": "click", "element_id": "b1"}, {"op": "click", "element_id": "b2"})).actions) == 2
    assert len(loop.parse_reply(reply({"op": "type", "element_id": "b1", "text": "x"}, {"op": "click", "element_id": "e4"})).actions) == 1
    assert len(loop.parse_reply(reply({"op": "type", "element_id": "b1", "text": "x"},
                                      {"op": "click", "element_id": "b2"}, {"op": "type", "element_id": "b3", "text": "y"})).actions) == 2


def test_browser_requests_use_the_compact_schema_and_prompt_desktop_requests_do_not():
    web = loop.build_request_body("m", "x\n" + loop.BROWSER_MARKER + " | WINDOW: a", "")
    desk = loop.build_request_body("m", "FRONTMOST APP: TextEdit | WINDOW: a", "")
    assert web["text"]["format"]["schema"] is loop.BROWSER_ACTION_SCHEMA
    assert loop.BROWSER_PROMPT in web["input"][0]["content"][0]["text"] and loop.BROWSER_PROMPT not in desk["input"][0]["content"][0]["text"]
    assert desk["text"]["format"]["schema"] is loop.ACTION_SCHEMA
    # compact replies must parse with the same parser (missing fields default)
    compact = {"actions": [{"op": "navigate", "text": "https://example.com"}], "memory": "", "subgoal_done": False}
    assert loop.parse_reply(json.dumps(compact)).actions[0].op == "navigate"
    json.dumps(loop.BROWSER_ACTION_SCHEMA)


def test_verifier_request_stays_bounded_with_browser_evidence():
    request = loop.verify_text("t" * 9000, "c" * 900, "Chrome (browser)", "w" * 400, [{"ax_role": "b", "value": "x" * 3000}],
                               "E" * 6000, "p" * 4000)
    assert len(request.encode()) <= 10240


def test_browser_verifier_sees_facts_after_old_fixed_head_cut():
    request = loop.verify_text("read the fact", "synthetic fact", "Chrome (browser)", "Page", [], "",
                               "x" * 1400 + " synthetic-fact-from-current-viewport")
    assert "synthetic-fact-from-current-viewport" in request


def test_verifier_sees_complete_terminal_answer_and_qualifications_past_100_bytes():
    claim = ('I opened every listed page and compared the entries and their amounts. ' * 2 +
             'The minimum entry is synthetic-wrong-final-answer.')
    request = loop.verify_text('Identify the minimum entry.', claim, 'Chrome (browser)', 'Synthetic page', [], '',
                               'Page lists synthetic-correct-final-answer.')
    assert claim in request
    assert 'synthetic-wrong-final-answer' in request
    assert len(request.encode()) <= 10240


def test_complete_unicode_terminal_claim_preserves_receipt_context_within_request_bound():
    claim = '界' * 499 + '終'
    context = 'source-owned-receipt-prefix ' + 'x' * 4800 + ' source-owned-receipt-tail'
    request = loop.verify_text('task ' * 4000, claim, 'Chrome (browser)', 'window', [], context, 'page ' * 1000)
    assert claim in request and context in request
    assert '[task truncated; omitted content is not verified]' in request
    assert len(request.encode()) <= 10240
