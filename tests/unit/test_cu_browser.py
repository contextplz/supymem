import json
import os
from pathlib import Path

import pytest

from desktop_runtime.cu_browser import (BrowserBackend, ChromeSession, SafetyRefusal, find_profile_dir,
                                        is_browser_step, prepare_profile_copy)
from desktop_runtime.cu_browser.chrome import CHROME_BIN
from desktop_runtime.cu_browser.dom import render_elements


def test_render_elements_is_compact_and_marks_offscreen():
    out = render_elements([
        {"i": 0, "tag": "input", "type": "email", "name": "email", "placeholder": "owner@example.com", "label": "Email", "required": True, "inView": True},
        {"i": 1, "tag": "button", "type": "submit", "text": "Join", "inView": False},
        {"i": 2, "tag": "select", "options": ["A", "B"], "label": "Plan"}])
    assert out.splitlines() == [
        '[0]<input type=email name=email placeholder="owner@example.com" required> Email',
        "~[1]<button type=submit> Join",
        "[2]<select options=A|B> Plan"]


def test_field_validation_is_bounded_linked_and_secret_free():
    from desktop_runtime.cu_browser.dom import INDEX_JS, PAGE_TEXT_JS, validation_text
    rows = [{'id': 'b1', 'name': 'code', 'validation': 'Use UPPERCASE letters', 'type': 'text'},
            {'id': 'b2', 'name': 'password', 'type': 'password', 'validation': 'SECRET_PASSWORD'},
            {'id': 'b3', 'name': 'card_number', 'validation': 'SECRET_CARD'}]
    assert validation_text(rows) == 'code: Use UPPERCASE letters'
    assert len(validation_text([{'name': 'field', 'validation': 'x' * 5000}] * 8)) <= 1000
    for source in ('validationMessage', 'aria-invalid', 'aria-describedby', 'aria-errormessage', 'nextElementSibling'):
        assert source in INDEX_JS
    assert '[role=alert]' in PAGE_TEXT_JS and 'PAGE VALIDATION (untrusted)' in PAGE_TEXT_JS


def test_validation_dom_extraction_reads_native_aria_inline_and_alert_without_browser():
    import subprocess
    from desktop_runtime.cu_browser.dom import INDEX_JS, PAGE_TEXT_JS
    script = r'''
    global.innerHeight = 900;
    global.getComputedStyle = () => ({visibility:'visible',display:'block',opacity:'1'});
    const inline = {innerText:'Inline UPPERCASE error', matches:() => true, querySelectorAll:() => []};
    const described = {innerText:'Code must be uppercase'};
    const field = (name, type) => ({tagName:'INPUT',type,name,autocomplete:'',value:'abc123',validationMessage:'Native code validation',
      validity:{valid:false},labels:[],innerText:'',hasAttribute:() => false,
      getBoundingClientRect:() => ({width:100,height:30,bottom:30,top:0}),setAttribute:() => {},
      getAttribute:key => ({type,'aria-invalid':'true','aria-describedby':'description','aria-errormessage':'error'})[key] || '',
      getRootNode:() => ({getElementById:() => described}),nextElementSibling:inline,
      parentElement:{matches:() => false,querySelectorAll:() => [inline]}});
    const elements = [field('code','text'),field('password','password'),field('card_number','text')];
    global.document = {forms:[],body:{innerText:'Generic form'},
      querySelectorAll:selector => selector === '[data-cu-idx]' || selector === '*' ? [] : selector.includes('a[href]') ? elements : [inline]};
    const index = INDEX_FUNCTION;
    const page = PAGE_FUNCTION;
    console.log(JSON.stringify({elements:index(0),text:page(2000)}));
    '''.replace('INDEX_FUNCTION', INDEX_JS).replace('PAGE_FUNCTION', PAGE_TEXT_JS)
    output = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10, check=True)
    result = json.loads(output.stdout)
    assert result['elements'][0]['invalid'] is True
    assert result['elements'][0]['validation'] == 'Native code validation | Code must be uppercase | Inline UPPERCASE error'
    assert result['elements'][1]['validation'] == result['elements'][2]['validation'] == ''
    assert result['text'].startswith('PAGE VALIDATION (untrusted): Inline UPPERCASE error')


def test_owned_source_validation_retains_field_binding_without_exporting_secrets():
    import subprocess
    from coding_agents.owned_browser import _CUAI_SOURCE_JS
    script = r'''
    global.Node={TEXT_NODE:3,ELEMENT_NODE:1};
    const textNode=text=>({nodeType:3,textContent:text});
    const message=text=>({nodeType:1,tagName:'SPAN',childNodes:[textNode(text)],closest:()=>null,checkVisibility:()=>true,
      matches:()=>true,querySelectorAll:()=>[]});
    const described=message('Use uppercase letters'),inline=message('Code is rejected');
    const field=(name,type)=>({tagName:'INPUT',type,name,value:'abc123',labels:[],placeholder:'',autocomplete:'',
      validationMessage:'Native validation',closest:()=>null,checkVisibility:()=>true,
      getAttribute:key=>key==='aria-describedby'?'error':'',getRootNode:()=>({getElementById:()=>described}),
      nextElementSibling:inline,parentElement:null});
    global.document={getElementById:()=>null,querySelectorAll:selector=>selector==='input,select,textarea'
      ?[field('code','text'),field('password','password'),field('api_key','text')]:[]};
    const source=SOURCE_FUNCTION;
    console.log(JSON.stringify(source(['abc123'])));
    '''.replace('SOURCE_FUNCTION', _CUAI_SOURCE_JS)
    result = json.loads(subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10, check=True).stdout)
    assert result['facts'] == ['FIELD VALIDATION (untrusted): code: Native validation | Use uppercase letters | Code is rejected']
    assert len(result['fields']) == 1 and result['fields'][0]['reviewed_value_index'] == 0


def test_list_readback_distinguishes_results_from_cleared_inputs_without_browser():
    import subprocess
    from desktop_runtime.cu_browser.dom import LABELED_FACTS_JS
    script = r'''
    const item = {innerText:'milk',closest:()=>null,querySelector:()=>null,getBoundingClientRect:()=>({width:100,height:30})};
    global.getComputedStyle=()=>({display:'block',visibility:'visible'});
    global.document={querySelectorAll:selector=>selector==='li,[role=listitem]'?[item]:[]};
    console.log((SOURCE_FUNCTION)());
    '''.replace('SOURCE_FUNCTION', LABELED_FACTS_JS)
    text = subprocess.run(['node', '-e', script], capture_output=True, text=True, timeout=10, check=True).stdout.strip()
    assert text == 'List item: milk'


def test_profile_lookup_and_copy_skips_saved_passwords(tmp_path):
    root = tmp_path / "Chrome"
    (root / "Profile 2" / "Local Storage").mkdir(parents=True)
    (root / "Profile 2" / "Cookies").write_text("c")
    (root / "Profile 2" / "Login Data").write_text("secret")
    (root / "Local State").write_text(json.dumps({"profile": {"info_cache": {
        "Default": {"user_name": "other@x.com"}, "Profile 2": {"user_name": "Profile-Owner@Example.Test"}}}}))
    assert find_profile_dir("profile-owner@example.test", root) == "Profile 2"
    assert find_profile_dir("nobody@x.com", root) is None
    dest = prepare_profile_copy("profile-owner@example.test", tmp_path / "managed", root)
    assert (dest / "Default" / "Cookies").read_text() == "c"
    assert not (dest / "Default" / "Login Data").exists()
    assert oct(os.stat(dest).st_mode & 0o777) == "0o700"
    with pytest.raises(LookupError):
        prepare_profile_copy("nobody@x.com", tmp_path / "m2", root)


def test_chrome_args_bind_loopback_and_use_managed_profile(tmp_path):
    a = ChromeSession(tmp_path, headless=True, port=9333).args()
    assert "--remote-debugging-port=9333" in a and "--remote-debugging-address=127.0.0.1" in a
    assert f"--user-data-dir={tmp_path}" in a and "--headless=new" in a
    assert any(x.startswith("--user-agent=") and "Headless" not in x for x in a)
    assert "--start-minimized" in ChromeSession(tmp_path, headless=False).args()
    assert "--disable-features=BackForwardCache" in a
    assert "--disable-features=BackForwardCache" in ChromeSession(tmp_path, headless=False).args()


def test_routing():
    assert is_browser_step({"app": "Google Chrome"})
    assert is_browser_step({"url": "https://x.com"})
    assert is_browser_step({"backend": "browser"})
    assert not is_browser_step({"app": "Finder"})


class FakeLoc:
    def __init__(self, page, idx):
        self.page, self.idx = page, idx
        self.original = page.els.get(idx)

    def count(self):
        return 1 if self.idx in self.page.els else 0

    def evaluate(self, _js, *args):
        if _js.startswith("(e, i) =>"):
            return self.page.els.get(self.idx) is self.original
        if "isContentEditable" in _js:   # fillable-text preflight (see backend._FILLABLE_JS)
            return "ok"
        return self.page.els[self.idx]

    def click(self, **_):
        if not _.get("trial"):
            self.page.log.append(("click", self.idx))

    def fill(self, text, **_):
        self.page.log.append(("fill", self.idx, text))

    def press(self, key):
        self.page.log.append(("press", self.idx, key))

    def select_option(self, **kw):
        self.page.log.append(("select", self.idx, kw))


class FakePage:
    url = "https://shop.test/"

    def __init__(self, els):
        self.els, self.log = els, []

    def locator(self, sel):
        return FakeLoc(self, int(sel.split('"')[1]))

    def evaluate(self, js, *a):
        if js == '(d) => d === document':
            return True
        return False  # no captcha

    def evaluate_handle(self, *args):
        return type('EmptyFocus', (), {'as_element': lambda self: None})()

    def goto(self, url, **_):
        self.log.append(("goto", url))

    class keyboard:  # noqa: N801
        @staticmethod
        def press(k):
            pass


def backend(els, **kw):
    b = BrowserBackend.__new__(BrowserBackend)
    b.page, b.allow_passwords, b.text_limit = FakePage(els), kw.get("allow_passwords", False), 2000
    b.context = type("C", (), {"pages": [b.page]})()
    b._indexed_page, b._indexed_url = b.page, b.page.url
    b._element_bindings = {index: FakeLoc(b.page, index) for index in els}
    b._document_handle, b._keyboard_handle, b._indexed_generation = object(), None, 0
    b._evidence_state()
    return b


EL = {0: {"text": "Email", "type": "email"}, 1: {"type": "password", "name": "password"},
      2: {"text": "Place your order"}, 3: {"name": "cardnumber", "type": "text"}, 4: {"text": "Next"}}


def test_action_mapping():
    b = backend(EL)
    assert b.act({"action": "click", "index": 4})["status"] == "ok"
    assert b.act({"action": "type", "index": 0, "text": "a@b.co", "submit": True})["status"] == "ok"
    assert b.act({"action": "navigate", "url": "https://x.com"})["status"] == "ok"
    assert b.page.log == [("click", 4), ("fill", 0, "a@b.co"), ("press", 0, "Enter"), ("goto", "https://x.com")]
    assert b.act({"action": "click", "index": 99})["status"] == "error"  # stale index
    assert b.act({"action": "nope"})["status"] == "error"


def test_safety_refusals():
    b = backend(EL)
    assert b.act({"action": "type", "index": 1, "text": "pw"})["status"] == "refused"
    assert b.act({"action": "type", "index": 1, "text": "pw", "authorized": True})["status"] == "refused"  # task didn't allow
    assert backend(EL, allow_passwords=True).act({"action": "type", "index": 1, "text": "pw", "authorized": True})["status"] == "ok"
    assert b.act({"action": "click", "index": 2})["status"] == "refused"  # payment submit
    assert b.act({"action": "type", "index": 3, "text": "4242"})["status"] == "refused"  # card field
    assert b.act({"action": "navigate", "url": "file:///etc/passwd"})["status"] == "refused"
    assert b.act({"action": "navigate", "url": "javascript:alert(1)"})["status"] == "refused"
    with pytest.raises(SafetyRefusal):
        b.check_safety({"action": "navigate", "url": "ftp://x"})
    assert all(e[0] != "fill" for e in b.page.log)


@pytest.mark.parametrize("operation", ["click", "double_click", "right_click", "type", "select", "press", "upload"])
def test_current_human_challenge_blocks_every_input_before_dispatch(operation):
    b = backend(EL)
    evaluate = b.page.evaluate
    b.page.evaluate = lambda js, *args: True if "captcha" in js else evaluate(js, *args)
    result = b.act({"action": operation, "index": 0, "text": "synthetic", "key": "Enter", "value": "synthetic"})
    assert result["status"] == "refused" and result["no_effect"] is True
    assert b.page.log == []


@pytest.mark.parametrize("where", ["admission", "input", "readback"])
def test_only_pre_input_failure_can_claim_no_effect(where):
    b = backend(EL)

    class BrokenLoc(FakeLoc):
        def click(self, **options):
            if options.get("trial"):
                if where == "admission":
                    raise RuntimeError("synthetic covered element")
                return
            super().click(**options)
            if where == "input":
                raise RuntimeError("synthetic failure after click")
    b._element_bindings[4] = BrokenLoc(b.page, 4)
    if where == "readback":
        old_evaluate = b.page.evaluate
        def evaluate(js, *args):
            if "captcha" in js and b.page.log:
                raise RuntimeError("synthetic readback failure after click")
            return old_evaluate(js, *args)
        b.page.evaluate = evaluate
    result = b.act({"action": "click", "index": 4})
    assert result["status"] == "error"
    assert result["no_effect"] is (where == "admission")
    assert b.page.log == ([] if where == "admission" else [("click", 4)])


@pytest.mark.skipif(not Path(CHROME_BIN).exists(), reason="needs Chrome")
def test_real_chrome_index_click_and_captcha(tmp_path):
    chrome = ChromeSession(tmp_path / "p", headless=True)
    b = None
    try:
        b = BrowserBackend(chrome.start(), download_dir=tmp_path / "dl")
        html = ("<input name=q placeholder=Search><button onclick=\"document.title='clicked'\">Go</button>"
                "<input type=hidden name=h><div class=g-recaptcha></div>")
        b.page.set_content(html)
        o = b.observe()
        assert [e["tag"] for e in o.elements] == ["input", "button"] and o.captcha
        for action in ({"action": "type", "index": 0, "text": "gym"},
                       {"action": "click", "index": 1}, {"action": "press", "key": "Enter"}):
            result = b.act(action)
            assert result["status"] == "refused" and result["no_effect"] is True
        assert b.page.locator('input[name=q]').input_value() == ""
        assert b.observe().title != "clicked"

        # A plain-text transcription form is still a human-only challenge.
        b.page.set_content('<form>Type the letters XK7Q: <input name=answer>'
                           '<button onclick="document.title=\'submitted\'">Verify</button></form>')
        assert b.observe().captcha is True
        result = b.act({"action": "type", "index": 0, "text": "XK7Q"})
        assert result["status"] == "refused" and result["no_effect"] is True
        assert b.page.locator('input[name=answer]').input_value() == ""

        # Workflow data and documentation mentioning CAPTCHA do not become
        # a human challenge merely because there is an ordinary form nearby.
        b.page.set_content('<p>Admin CAPTCHA help documentation</p><form>'
                           'Enter the code from the previous step: <input name=previous>'
                           '<button>Next</button></form>')
        assert b.observe().captcha is False
        assert b.act({"action": "type", "index": 0, "text": "CODE1-ABCD"})["status"] == "ok"
        assert b.page.locator('input[name=previous]').input_value() == "CODE1-ABCD"
    finally:
        try:
            if b:
                b.close()
        finally:
            chrome.stop()


@pytest.mark.skipif(not Path(CHROME_BIN).exists(), reason="needs Chrome")
def test_real_browser_semantic_fact_readback_can_read_below_viewport_without_input(tmp_path):
    chrome = ChromeSession(tmp_path / "profile", headless=True)
    b = None
    try:
        b = BrowserBackend(chrome.start(), download_dir=tmp_path / "downloads")
        b.page.set_content("<h1>Reference page</h1>" + "<p>Generic paragraph.</p>" * 150 +
                           "<dl><dt>Reference</dt><dd>synthetic-current-document-fact</dd></dl>"
                           "<p><span>Version:</span> synthetic-current-version</p>"
                           "<section><h2>Category</h2><div><p>synthetic-current-category</p></div></section>"
                           "<p hidden><strong>Hidden:</strong> synthetic-hidden-value</p>"
                           "<form><p><strong>Form value:</strong> synthetic-form-value</p></form>")
        observed = b.observe()
        assert "synthetic-current-document-fact" not in observed.text
        assert "Reference: synthetic-current-document-fact" in observed.labeled_facts
        assert "Version: synthetic-current-version" in observed.labeled_facts
        assert "Category: synthetic-current-category" in observed.labeled_facts
        assert "synthetic-hidden-value" not in observed.labeled_facts
        assert "synthetic-form-value" not in observed.labeled_facts
        assert b.page.evaluate("scrollY") == 0
        assert b.completion_evidence()["actions"] == []
    finally:
        try:
            if b:
                b.close()
        finally:
            chrome.stop()


def test_completion_receipts_bounded_and_refusals_not_claimed_as_effects():
    b = backend(EL)
    for _ in range(20):
        b.act({"action": "type", "index": 1, "text": "private-never-record"})
    evidence = b.completion_evidence()
    assert len(evidence["actions"]) == 16
    assert all(row["status"] == "refused" for row in evidence["actions"])
    assert all("action_input_matched" not in row for row in evidence["actions"])
    assert "private-never-record" not in json.dumps(evidence)


def test_response_metadata_never_reads_payloads_headers_or_urls():
    b = backend(EL)
    b._evidence_state()
    b._active_action = 7
    class Request:
        method = "POST"
        resource_type = "fetch"
        def __getattr__(self, name):
            raise AssertionError("must not inspect sensitive request attribute")
    class Response:
        status = 202
        request = Request()
        def __getattr__(self, name):
            raise AssertionError("must not inspect sensitive response attribute")
    b._request(b.page, Response.request)
    b._active_action = 8  # late response retains its actual initiating action
    b._response(b.page, Response())
    row = b.completion_evidence()["responses"][0]
    assert row == {"action_seq": 7, "page_scope": row["page_scope"], "method": "POST", "status": 202, "resource": "fetch"}
    b._active_action = None
    b._response(b.page, Response())
    assert len(b.completion_evidence()["responses"]) == 1


def test_new_task_reset_drops_old_receipts_and_pending_responses():
    b = backend(EL)
    b.act({"action": "click", "index": 4})
    old_scope = b.completion_evidence()["actions"][0]["page_scope"]
    pending = object()
    b._active_action = 1
    b._request(b.page, pending)
    b.reset_completion_evidence()
    assert b.completion_evidence() == {"v": 1, "source": "owned_browser_backend", "actions": [], "responses": []}
    assert not b._pending_requests
    b.act({"action": "click", "index": 4})
    assert b.completion_evidence()["actions"][0]["seq"] == 1
    assert b.completion_evidence()["actions"][0]["page_scope"] != old_scope


@pytest.mark.parametrize('filename', ['../escaped.txt', 'absolute', '..\\escaped.txt'])
def test_download_filename_cannot_choose_destination(tmp_path, filename):
    if filename == 'absolute':
        filename = str(tmp_path / 'outside-owned-dir.txt')
    source = tmp_path / 'browser-artifact'
    source.write_bytes(b'owned synthetic download')
    b = BrowserBackend.__new__(BrowserBackend)
    b.download_dir = tmp_path / 'owned-downloads'
    b.download_dir.mkdir()
    b.downloads = []

    class Download:
        suggested_filename = filename

        def path(self):
            return source

        def save_as(self, path):
            Path(path).write_bytes(source.read_bytes())

    b._save(Download())
    assert len(b.downloads) == 1
    saved = Path(b.downloads[0])
    assert saved.parent == b.download_dir
    assert saved.read_bytes() == b'owned synthetic download'
    assert not (tmp_path / 'escaped.txt').exists()


@pytest.mark.parametrize('failure', ['large', 'source_symlink', 'directory_symlink', 'copy_error', 'collision', 'directory_swap'])
def test_download_refusal_preserves_existing_files_and_cleans_partial(tmp_path, monkeypatch, failure):
    import desktop_runtime.cu_browser.backend as module
    source = tmp_path / 'browser-artifact'
    source.write_bytes(b'synthetic bytes')
    b = BrowserBackend.__new__(BrowserBackend)
    b.download_dir = tmp_path / 'owned-downloads'
    b.download_dir.mkdir()
    b.downloads = []
    sentinel = b.download_dir / 'unrelated.txt'
    sentinel.write_text('preserve')
    preserved = sentinel
    if failure == 'large':
        with source.open('r+b') as stream:
            stream.truncate(64 * 1024 * 1024 + 1)
    elif failure == 'source_symlink':
        target = tmp_path / 'source-link'
        target.symlink_to(source)
        source = target
    elif failure == 'directory_symlink':
        b.download_dir = tmp_path / 'directory-link'
        b.download_dir.symlink_to(sentinel.parent, target_is_directory=True)
    elif failure == 'copy_error':
        monkeypatch.setattr(module.os, 'write', lambda *args: (_ for _ in ()).throw(OSError('synthetic copy failure')))
    elif failure == 'collision':
        class FixedUUID:
            hex = '0' * 32
        monkeypatch.setattr(module.uuid, 'uuid4', lambda: FixedUUID())
        (b.download_dir / ('download-' + '0' * 32 + '.txt')).symlink_to(sentinel)
    elif failure == 'directory_swap':
        original_link = module.os.link
        renamed = tmp_path / 'original-owned-downloads'
        preserved = renamed / 'unrelated.txt'
        def swapped_link(*args, **kwargs):
            b.download_dir.rename(renamed)
            b.download_dir.mkdir()
            return original_link(*args, **kwargs)
        monkeypatch.setattr(module.os, 'link', swapped_link)

    class Download:
        suggested_filename = 'synthetic.txt'
        def path(self):
            return source

    with pytest.raises((RuntimeError, OSError)):
        b._save(Download())
    assert b.downloads == []
    assert preserved.read_text() == 'preserve'
    assert not list(tmp_path.rglob('.partial-*'))
    assert not [path for path in tmp_path.rglob('download-*') if not path.is_symlink()]


@pytest.mark.parametrize('operation', ['click', 'double_click', 'right_click'])
def test_all_pointer_operation_variants_keep_payment_submission_floor(operation):
    b = backend(EL)
    assert b.act({'action': operation, 'index': 2})['status'] == 'refused'
    assert b.page.log == []
