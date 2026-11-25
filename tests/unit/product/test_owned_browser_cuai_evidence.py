"""Owned headless DOM evidence; no provider, founder browser or form POST."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from coding_agents.computer_use_gate import EffectUncertain
from coding_agents.jev_decision import Candidate
from coding_agents.owned_browser import OwnedBrowser, _CUAI_SOURCE_JS, _cuai_source


@pytest.fixture
def v2_browser(tmp_path):
    admissions = []
    used = set()
    def admit(effect, digest):
        admissions.append(effect)
        if effect['invocation'] in used:
            return False
        used.add(effect['invocation'])
        return True
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*_):
            pass
        def do_GET(self):
            body = ('<h1>Owned source</h1><a href="/two">Second source</a>'
                    '<a href="https://foreign.invalid/">Outside</a>'
                    '<p>Seats: 23. License: open reference.</p>'
                    '<p hidden>HIDDEN_REFERENCE</p><form><p>PRIVATE_FORM_REFERENCE</p><input value="PRIVATE_INPUT"></form>'
                    '<table><tr><td>Plan</td><td>23 seats</td></tr></table>'
                    + '<p>Ordinary readable filler</p>'*80 + '<p style="margin-top:1800px">Footer reference seats 42</p>').encode()
            self.send_response(200);self.send_header('Content-Length',str(len(body)))
            self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
    server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    root = f'http://127.0.0.1:{server.server_port}'
    browser = OwnedBrowser(order_id='dwo_v2',goal='Compare source seats',origins=[root],
        authorize=lambda *_:True, admit_effect=admit, general_mode=True, cuai_mode=True,
        cuai_version='browser_cuai_text_v2', profile_dir=tmp_path/'v2-profile', allow_loopback_for_tests=True)
    browser.start(root+'/one')
    try:
        yield browser, admissions
    finally:
        browser.close();server.shutdown();server.server_close();thread.join(timeout=1)
    assert browser._browser is None and not browser._cuai_reads


def document_candidate(browser, obs, action):
    from coding_agents.owned_browser import cuai_document_target
    return Candidate(action.op,cuai_document_target(browser.last_load_evidence,action),obs.digest())


def test_v2_real_document_reads_scroll_navigation_and_source_cleanup(v2_browser):
    from coding_agents.cu_ai_loop import Action
    browser, admissions = v2_browser
    obs = browser.observe()
    links = browser.cuai_navigation_targets(obs)
    assert len(links)==1 and links[0]['href'].endswith('/two')
    browser.click(Candidate('navigate',links[0]['id'],obs.digest()),'nav_one',navigate_url=links[0]['href'])
    obs = browser.observe();assert obs.location.endswith('/two')
    action = Action(op='extract',text='Seats')
    browser.document_action(document_candidate(browser,obs,action),'read_one',action)
    obs = browser.observe()
    source = json.loads(browser.cuai_readback(obs))
    assert source['schema_version']==2 and 'Seats: 23' in json.dumps(source['current']['source']['reads'])
    assert 'HIDDEN_REFERENCE' not in json.dumps(source) and 'PRIVATE_INPUT' not in json.dumps(source)
    action = Action(op='scroll',direction='down',amount=5)
    browser.document_action(document_candidate(browser,obs,action),'scroll_one',action)
    assert browser._page.evaluate('scrollY')>0
    obs = browser.observe();source = json.loads(browser.cuai_readback(obs))
    assert source['current']['source']['reads'] and len(browser.cuai_readback(obs).encode())<=5000
    assert [e['operation'] for e in admissions]==['navigate','extract','scroll']
    assert all('text' not in e['control'] and 'value' not in e['control'] for e in admissions)


def test_v2_actual_read_is_literal_redacted_and_one_use(v2_browser):
    from coding_agents.cu_ai_loop import Action
    browser, admissions = v2_browser
    obs = browser.observe(); action = Action(op='extract',text='PRIVATE_FORM_REFERENCE')
    candidate = document_candidate(browser,obs,action)
    browser.document_action(candidate,'read_one',action)
    with pytest.raises(PermissionError):
        browser.document_action(candidate,'read_one',action)
    obs = browser.observe(); assert json.loads(browser.cuai_readback(obs))['current']['source']['reads'][0]['passages']==[]
    assert len(admissions)==1
    old = document_candidate(browser,obs,Action(op='scroll',direction='down',amount=1))
    browser._page.reload(); fresh = browser.observe()
    with pytest.raises(PermissionError):
        browser.document_action(old,'stale_scroll',Action(op='scroll',direction='down',amount=1))
    assert len(admissions)==1 and fresh.digest()!=obs.digest()


def test_v2_target_change_during_admission_proves_no_dispatch(v2_browser):
    from coding_agents.cu_ai_loop import Action
    from coding_agents.owned_browser import BrowserPreDispatchRefused
    browser, admissions = v2_browser
    obs = browser.observe(); action=Action(op='scroll',direction='down',amount=2)
    candidate=document_candidate(browser,obs,action)
    def change(effect,digest):
        admissions.append(effect);browser._page.reload();return True
    browser.admit_effect=change
    with pytest.raises(BrowserPreDispatchRefused):
        browser.document_action(candidate,'changed',action)
    assert browser._page.evaluate('scrollY')==0 and not browser._uncertain


def test_v2_unknown_after_actual_scroll_prohibits_any_replay(v2_browser,monkeypatch):
    from coding_agents.cu_ai_loop import Action
    browser, admissions=v2_browser
    obs=browser.observe();action=Action(op='scroll',direction='down',amount=2)
    candidate=document_candidate(browser,obs,action)
    handle=browser._page.locator('html').element_handle()
    original=handle.evaluate
    def evaluate(expression,arg=None):
        result=original(expression,arg)
        if 'defaultView.scrollBy' in expression:
            raise RuntimeError('lost native dispatch acknowledgment')
        return result
    monkeypatch.setattr(handle,'evaluate',evaluate)
    monkeypatch.setattr(browser._page,'locator',lambda _:type('Locator',(),{'element_handle':lambda _:handle})())
    with pytest.raises(EffectUncertain):browser.document_action(candidate,'unknown',action)
    assert browser._page.evaluate('scrollY')>0 and browser._uncertain
    with pytest.raises(PermissionError):browser.observe()
    with pytest.raises(PermissionError):browser.document_action(candidate,'retry',action)
    assert len(admissions)==1


@pytest.fixture
def site():
    values = {"name": "Ada", "label": "Name"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):
            name = "Ben" if self.path == "/two" else values["name"]
            body = (
                f"<h1>Owned current facts</h1><p>Seats: 23<span hidden>PRIVATE_HIDDEN</span></p>"
                f'<label>{values["label"]}<span style="opacity:0">PRIVATE_LABEL</span><input name="name" value="{name}"></label>'
                '<input type="password" value="PRIVATE_PASSWORD"><input name="card" value="4111111111111111">'
                "<p>Reference card 4111 1111 1111 1111</p>"
                '<button type="button" onclick="location.href=\'/two\'">Next</button>'
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", values
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1)


@pytest.fixture
def browser(site, tmp_path):
    pytest.importorskip("playwright.sync_api")
    b = OwnedBrowser(
        order_id="dwo_cuai_evidence",
        goal="Read the owned profile",
        origins=[site[0]],
        authorize=lambda *_: True,
        admit_effect=lambda *_: True,
        general_mode=True,
        cuai_mode=True,
        fill_values={"name": "Ada", "text": "Ben"},
        profile_dir=tmp_path / "profile",
        allow_loopback_for_tests=True,
    )
    b.start(site[0] + "/one")
    try:
        yield b
    finally:
        b.close()


def test_actual_whole_fields_and_facts_exclude_password_card_and_hidden_label(browser):
    obs = browser.observe()
    # Visible password requires human handoff; the adapter never sends a
    # confirmation-pending observation to a provider.
    data = browser.cuai_readback(obs)
    parsed = json.loads(data)
    fields = parsed["current"]["source"]["fields"]
    assert fields == [
        {"label": "Name", "type": "text", "populated": True, "reviewed_value_index": 0}
    ]
    assert "Seats: 23" in data and all(
        x not in data
        for x in (
            "PRIVATE_HIDDEN",
            "PRIVATE_LABEL",
            "PRIVATE_PASSWORD",
            "4111111111111111",
            "4111 1111 1111 1111",
        )
    )
    assert "[REDACTED]" in data and obs.reviewed_text == ["Ada", "Ben"]


def test_actual_oversize_whole_label_and_value_are_unknown_not_prefixes(browser, site):
    site[1]["label"] = "L" * 161
    site[1]["name"] = "V" * 321
    browser._page.reload(wait_until="load")
    obs = browser.observe()
    source = json.loads(browser.cuai_readback(obs))["current"]["source"]
    assert source["fields"] == [] and source["omitted"]["fields"] >= 1
    assert "L" * 160 not in json.dumps(source)
    site[1]["label"] = "Name"
    browser._page.reload(wait_until="load")
    obs = browser.observe()
    source = json.loads(browser.cuai_readback(obs))["current"]["source"]
    assert source["fields"] == [
        {
            "label": "Name",
            "type": "text",
            "populated": True,
            "reviewed_value_index": None,
        }
    ]
    assert source["omitted"]["values"] == 1 and "V" * 320 not in json.dumps(source)


def test_actual_two_input_snapshots_keep_chronology_without_acceptance_claim(
    browser, site
):
    # Remove the synthetic password before FIRST observation; this is not a
    # review/oracle modification or a test of automated secret entry.
    browser._page.locator("input[type=password]").evaluate("e=>e.remove()")
    first = browser.observe()
    target = next(c.id for c in first.controls if c.label == "Next")
    browser.click(Candidate("click", target, first.digest()), "bai_owned_a0")
    browser._page.locator("input[type=password]").evaluate("e=>e.remove()")
    second = browser.observe()
    data = json.loads(browser.cuai_readback(second))
    assert data["current"]["source"]["fields"][0]["reviewed_value_index"] == 1
    assert data["input_history"][0]["source"]["fields"][0]["reviewed_value_index"] == 0
    assert data["input_history"][0]["invocation_id"] == "bai_owned_a0"
    assert data["input_history"][0]["delivery"] == "input_delivered"
    assert data["input_history"][0]["acceptance"] == "unknown"
    target = next(c.id for c in second.controls if c.label == "Next")
    browser.click(Candidate("click", target, second.digest()), "bai_owned2_a0")
    browser._page.locator("input[type=password]").evaluate("e=>e.remove()")
    third = browser.observe()
    history = json.loads(browser.cuai_readback(third))["input_history"]
    assert [
        entry["source"]["fields"][0]["reviewed_value_index"] for entry in history
    ] == [0, 1]
    assert [entry["invocation_id"] for entry in history] == [
        "bai_owned_a0",
        "bai_owned2_a0",
    ]
    assert all(entry["acceptance"] == "unknown" for entry in history)
    with pytest.raises(PermissionError, match="evidence_unavailable"):
        browser.cuai_readback(first)


def test_actual_unknown_click_keeps_input_record_and_closes_readback_authority(browser):
    browser._page.locator("input[type=password]").evaluate("e=>e.remove()")
    obs = browser.observe()
    target = next(c.id for c in obs.controls if c.label == "Next")
    handle, original = browser._handles[target]

    class UnknownHandle:
        def evaluate(self, code):
            return handle.evaluate(code)

        def is_visible(self):
            return handle.is_visible()

        def is_enabled(self):
            return handle.is_enabled()

        def click(self, **_kwargs):
            raise RuntimeError("Unknown delivery")

    browser._handles[target] = (UnknownHandle(), original)
    with pytest.raises(EffectUncertain):
        browser.click(Candidate("click", target, obs.digest()), "bai_unknown_a0")
    assert browser._cuai_inputs[-1]["delivery"] == "delivery_unconfirmed"
    assert browser._cuai_inputs[-1]["acceptance"] == "unknown"
    assert browser._cuai_inputs[-1]["source"]["fields"][0]["reviewed_value_index"] == 0
    with pytest.raises(PermissionError):
        browser.cuai_readback(obs)


def test_source_budget_drops_whole_fact_and_field_entries_with_omissions():
    value = {
        "facts": ["Fact " + str(n) + " " + "x" * 200 for n in range(20)],
        "fields": [
            {
                "label": "Name " + str(n),
                "type": "text",
                "populated": True,
                "reviewed_value_index": 0,
            }
            for n in range(20)
        ],
        "omitted": {"facts": 0, "fields": 0, "values": 0},
    }
    result = _cuai_source(value, limit=500)
    assert (
        len(json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode())
        <= 500
    )
    assert result["omitted"]["facts"] > 0 and result["omitted"]["fields"] > 0
    assert all(f in value["facts"] for f in result["facts"])
    assert all(f in value["fields"] for f in result["fields"])


def test_old_document_or_session_cannot_authorize_historical_source(browser):
    obs = browser.observe()
    browser.session_id = "f" * 32
    with pytest.raises(PermissionError, match="evidence_unavailable"):
        browser.cuai_readback(obs)
    assert browser._cuai_pages  # retained history grants no current authority


def test_changed_field_value_refuses_before_effect_admission(browser):
    browser._page.locator("input[type=password]").evaluate("e=>e.remove()")
    field = browser._page.locator("input[name=name]")
    field.evaluate("e=>e.value=''")
    obs = browser.observe()
    target = next(c.id for c in obs.controls if "type" in c.operations)
    admissions = []
    browser.admit_effect = lambda *args: admissions.append(args) or True
    field.evaluate("e=>e.value='Source changed value'")
    with pytest.raises(PermissionError, match="observation_stale"):
        browser.type_text(
            Candidate("type", target, obs.digest(), value_index=0),
            "bai_stale_field_a0",
            "Ada",
        )
    assert admissions == []
    assert field.input_value() == "Source changed value"
    assert browser._cuai_inputs == []


@pytest.mark.parametrize("label", ["One-time code", "API key"])
def test_sensitive_sole_visible_label_omits_whole_filled_field(browser, label):
    browser._page.locator("body").evaluate(
        """(body,label)=>{
          body.replaceChildren();
          const heading=document.createElement('h1');heading.textContent='Owned review';body.append(heading);
          const fieldLabel=document.createElement('label');fieldLabel.append(label);
          const field=document.createElement('input');field.value='736281';fieldLabel.append(field);body.append(fieldLabel);
          const link=document.createElement('a');link.href=location.href;link.textContent='Safe reference';body.append(link);
        }""",
        label,
    )
    obs = browser.observe()
    readback = browser.cuai_readback(obs)
    source = json.loads(readback)["current"]["source"]
    assert source["fields"] == [] and source["omitted"]["fields"] == 1
    assert "736281" not in readback


@pytest.mark.parametrize("label_source", ["second_associated_label", "aria_labelledby"])
def test_all_sensitive_source_labels_omit_whole_field(browser, label_source):
    browser._page.locator("body").evaluate(
        """(body,source)=>{
          body.replaceChildren();
          const heading=document.createElement('h1');heading.textContent='Owned review';body.append(heading);
          const field=document.createElement('input');field.id='field';field.name='field';field.value='736281';body.append(field);
          const first=document.createElement('label');first.htmlFor='field';first.textContent='Reference';body.append(first);
          const sensitive=document.createElement(source==='aria_labelledby'?'span':'label');
          sensitive.id='secretLabel';sensitive.textContent='One-time code';body.append(sensitive);
          if(source==='aria_labelledby')field.setAttribute('aria-labelledby','secretLabel');
          else sensitive.htmlFor='field';
          const link=document.createElement('a');link.href=location.href;link.textContent='Safe reference';body.append(link);
        }""",
        label_source,
    )
    readback = browser.cuai_readback(browser.observe())
    source = json.loads(readback)["current"]["source"]
    assert source["fields"] == [] and source["omitted"]["fields"] == 1
    assert "736281" not in readback


def test_generic_unknown_field_value_never_leaves_dom_but_reviewed_equality_does(
    browser,
):
    field = browser._page.locator("input[name=name]")
    field.evaluate("e=>e.value='736281'")
    raw = browser._page.evaluate(_CUAI_SOURCE_JS, ["Ada", "Ben"])
    parsed = _cuai_source(raw)
    assert parsed["fields"] == [
        {
            "label": "Name",
            "type": "text",
            "populated": True,
            "reviewed_value_index": None,
        }
    ]
    assert "736281" not in json.dumps(raw) and parsed["omitted"]["values"] == 1
    field.evaluate("e=>e.value='Ben'")
    raw = browser._page.evaluate(_CUAI_SOURCE_JS, ["Ada", "Ben"])
    assert raw["fields"][0] == {
        "label": "Name",
        "type": "text",
        "populated": True,
        "reviewed_value_index": 1,
    }
    assert "Ben" not in json.dumps(raw)


@pytest.mark.parametrize("value", ["4111 1111 1111 1111", "sk-" + "X" * 24])
def test_reviewed_fill_does_not_export_card_or_secret_value(site, tmp_path, value):
    with pytest.raises(PermissionError, match="sensitive_fill_refused"):
        OwnedBrowser(
            order_id="dwo_secret_qa",
            goal="Read",
            origins=[site[0]],
            authorize=lambda *_: True,
            admit_effect=lambda *_: True,
            general_mode=True,
            cuai_mode=True,
            fill_values={"text": value},
            profile_dir=tmp_path / "secret_profile",
            allow_loopback_for_tests=True,
        )
