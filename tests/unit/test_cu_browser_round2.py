"""Round-2 browser fixes in a real headless Chrome: shadow/tooltip text, hover, native dialogs, pre-delivery errors,
find-in-page, upload, lazy element binding, exact table sums; plus pure-logic checks for keys and the engine guards."""
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from coding_agents.cu_ai_loop import Action, parse_action
from desktop_runtime.cu_ai_browser import BrowserMac, playwright_key
from desktop_runtime.cu_ai_engine import Observation, run_loop
from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
from desktop_runtime.cu_browser.chrome import CHROME_BIN

needs_chrome = pytest.mark.skipif(not Path(CHROME_BIN).exists(), reason="needs Chrome")


@pytest.fixture
def chrome(tmp_path):
    session = ChromeSession(tmp_path / "profile", headless=True)
    backend = BrowserBackend(session.start(), download_dir=tmp_path / "downloads")
    try:
        yield backend
    finally:
        try:
            backend.close()
        finally:
            session.stop()


def idx(observed, text):
    return next(e["i"] for e in observed.elements if text in (e.get("text") or e.get("label") or ""))


@needs_chrome
def test_shadow_dom_text_and_tooltips_reach_the_observation(chrome):
    chrome.page.set_content("<h1>Status board</h1><my-card id=c></my-card><span title='Deploy finished 14:02 UTC'>Last run</span>"
                            "<script>c.attachShadow({mode:'open'}).innerHTML='<style>p{color:red}</style><p>Shadow secret: TKN-77</p>'</script>")
    observed = chrome.observe()
    assert "TKN-77" not in observed.text            # innerText cannot see it
    assert "Shadow secret: TKN-77" in observed.extra_text
    assert "Deploy finished 14:02 UTC" in observed.extra_text and "color:red" not in observed.extra_text


@needs_chrome
def test_hover_reveals_a_menu_item_then_it_is_clickable(chrome):
    chrome.page.set_content("<style>#m{display:none}#w:hover #m{display:block}</style><div id=w><button id=t>Products</button>"
                            "<div id=m><a href='#x' id=l>Pricing page</a></div></div>")
    first = chrome.observe()
    assert not any("Pricing page" in (e.get("text") or "") for e in first.elements)
    assert chrome.act({"action": "hover", "index": idx(first, "Products")})["status"] == "ok"
    second = chrome.observe()
    assert chrome.act({"action": "click", "index": idx(second, "Pricing page")})["status"] == "ok"
    assert chrome.page.url.endswith("#x")


@needs_chrome
def test_native_confirm_is_decided_by_policy_and_reported_never_silently_dismissed(chrome):
    chrome.page.set_content("<button onclick=\"document.title = confirm('Delete this draft?') ? 'deleted' : 'kept'\">Remove</button>")
    for allow, title in ((True, "deleted"), (False, "kept")):
        chrome.dialog_policy = lambda kind, message, allow=allow: allow
        observed = chrome.observe()
        assert chrome.act({"action": "click", "index": idx(observed, "Remove")})["status"] == "ok"
        assert chrome.page.title() == title
        assert chrome.dialogs[-1] == {"type": "confirm", "message": "Delete this draft?", "accepted": allow}


@needs_chrome
def test_browser_mac_dialog_policy_follows_task_authority(chrome):
    mac = BrowserMac(chrome)
    mac.set_goal("Remove the draft named Old notes")
    mac._ensure()
    assert chrome.dialog_policy("confirm", "Delete this draft?") is True          # the task authorizes deleting
    mac.set_goal("Read the page")
    assert chrome.dialog_policy("confirm", "Delete this draft?") is False         # nobody asked for it
    assert chrome.dialog_policy("prompt", "Delete this draft?") is False


@needs_chrome
def test_fill_on_non_text_controls_is_a_retryable_pre_delivery_error(chrome):
    chrome.page.set_content("<input type=checkbox><input type=file><button>Go</button><input id=t>")
    observed = chrome.observe()

    def by(**want):
        return next(e["i"] for e in observed.elements if all(e.get(k) == v for k, v in want.items()))
    for index in (by(type="checkbox"), by(type="file"), by(tag="button")):
        result = chrome.act({"action": "type", "index": index, "text": "x"})
        assert result["status"] == "error" and result["no_effect"] is True
    assert chrome.act({"action": "type", "index": by(tag="input", type=""), "text": "hello"})["status"] == "ok"
    assert chrome.page.input_value("#t") == "hello"


@needs_chrome
def test_blocked_navigation_is_not_effect_uncertain(chrome):
    chrome.context.route("**/*", lambda route: route.abort())
    result = chrome.act({"action": "navigate", "url": "http://127.0.0.1:9/blocked"})
    assert result["status"] == "error" and result["no_effect"] is True


@needs_chrome
def test_unknown_key_is_pre_delivery(chrome):
    chrome.page.set_content("<input id=t>")
    observed = chrome.observe()
    chrome.act({"action": "click", "index": observed.elements[0]["i"]})
    result = chrome.act({"action": "press", "key": "NotAKey"})
    assert result["status"] == "error" and result["no_effect"] is True


@needs_chrome
def test_extract_finds_text_anywhere_on_a_long_page_and_upload_sets_the_file(chrome, tmp_path):
    chrome.page.set_content("<h1>Long</h1>" + "<p>filler</p>" * 400 + "<p>The tower is 330 m tall.</p><input type=file id=f>")
    observed = chrome.observe()
    hit = chrome.act({"action": "extract", "query": "tower is"})
    assert "330 m tall" in hit["data"]
    assert "no match" in chrome.act({"action": "extract", "query": "zzzz-missing"})["data"]
    file = tmp_path / "avatar.png"
    file.write_bytes(b"png-bytes")
    file_index = next(e["i"] for e in observed.elements if e.get("type") == "file")
    assert chrome.act({"action": "upload", "index": file_index, "path": str(tmp_path / "missing.png")})["no_effect"] is True
    assert chrome.act({"action": "upload", "index": file_index, "path": str(file)})["status"] == "ok"
    assert chrome.page.evaluate("document.getElementById('f').files[0].name") == "avatar.png"


@needs_chrome
def test_lazy_binding_acts_on_the_right_element_among_hundreds(chrome):
    chrome.page.set_content("".join(f"<a href='#n{i}'>Link {i}</a>" for i in range(190)) + "<button onclick=\"document.title='clicked'\">Target</button>")
    observed = chrome.observe()
    assert observed.timings["total_s"] < 8   # bounded walk; loose enough for a loaded CI runner
    assert chrome.act({"action": "click", "index": idx(observed, "Target")})["status"] == "ok"
    assert chrome.page.title() == "clicked"


@needs_chrome
def test_browser_mac_adds_exact_column_sums_and_extras_to_the_notes(chrome):
    chrome.page.set_content("<table><tr><th>Memo</th><th>Amount</th></tr><tr><td>a</td><td>$1,000.10</td></tr><tr><td>b</td><td>$2,000.20</td></tr>"
                            "<tr><td>c</td><td>$0.30</td></tr></table>")
    mac = BrowserMac(chrome)
    notes = mac.observe().notes
    assert "HARNESS ARITHMETIC" in notes and "this page 3,000.60 (3 rows)" in notes
    assert "3,000.60" in mac.arithmetic_line()   # the same line is appended to the verifier's browser readback


@needs_chrome
def test_submit_identity_uses_live_form_values_including_files(chrome, tmp_path):
    # A batch "upload; click" and a later "click" on the already-filled form are the same submission.
    chrome.page.set_content("<form><input name=n><input type=file name=f><button type=submit>Upload</button></form>")
    mac = BrowserMac(chrome)
    stale = mac.observe()
    button = next(e["id"] for e in stale.elements if e.get("submits_form"))
    click = Action(op="click", element_id=button)
    empty = mac.submit_key(click, stale)
    file = tmp_path / "avatar.png"
    file.write_bytes(b"png-bytes")
    chrome.page.set_input_files("input[type=file]", str(file))
    in_batch = mac.submit_key(click, stale)          # observation predates the upload
    later = mac.submit_key(click, mac.observe())     # fresh observation on a later turn
    assert in_batch == later != empty
    chrome.page.fill("input[name=n]", "x")
    assert mac.submit_key(click, stale) != later     # different values are a different submission


def test_key_names_from_the_model_are_accepted_and_mapped():
    for chord, key in (("ARROWRIGHT", "ArrowRight"), ("PageDown", "PageDown"), ("HOME", "Home"), ("Shift+ArrowLeft", "Shift+ArrowLeft"),
                       ("ctrl+End", "Control+End")):
        action = parse_action({"op": "key", "chord": chord})
        assert playwright_key(action.chord) == key


def test_new_ops_validate_their_arguments():
    assert parse_action({"op": "hover", "element_id": "b3"}).op == "hover"
    assert parse_action({"op": "extract", "text": "tower"}).op == "extract"
    assert parse_action({"op": "upload", "element_id": "b2", "text": "/tmp/a.png"}).text == "/tmp/a.png"
    for bad in ({"op": "hover"}, {"op": "upload", "element_id": "b2"}):
        with pytest.raises(Exception):
            parse_action(bad)


class SubmitMac:
    """A page with one form: the first submit is accepted by the site; a bare 'Received' page follows."""

    def __init__(self):
        self.acts, self.accepted = [], False

    def frontmost(self):
        return ("Chrome (CDP)", 0)

    def focused_role(self):
        return None

    def observe(self):
        els = [{"id": "b1", "ax_role": "input", "label": "Email", "value": "qa@example.invalid", "name": "email", "form_scope": "form:0:0", "type": "email"},
               {"id": "b2", "ax_role": "button", "label": "Save", "value": "", "name": "", "form_scope": "form:0:0", "type": "submit", "submits_form": True}]
        obs = Observation(b"", els, "Chrome (browser)", "http://x/form | Form", "")
        obs.notes = "PAGE URL: http://x/form\nPAGE TEXT: Received" if self.accepted else "PAGE URL: http://x/form\nPAGE TEXT: Form"
        return obs

    def act(self, action, obs):
        self.acts.append(action.op)
        self.accepted = True
        return "click ok (0.0s)"

    def submit_key(self, action, obs):
        return "same-form-same-values" if action.element_id == "b2" else None

    def submit_accepted(self):
        return True


def _step(op, **kw):
    item = {"op": op, "element_id": "", "x": 0, "y": 0, "text": "", "chord": "", "direction": "", "amount": 0, "app": "", "ms": 0, "summary": ""}
    item.update(kw)
    return {"thought": "", "memory": "", "subgoal_done": False, "actions": [item]}


def test_an_accepted_submit_is_never_posted_again_and_triggers_a_receipt_based_check():
    mac, verdicts = SubmitMac(), []
    replies = iter([_step("click", element_id="b2"), _step("done", summary="saved"), _step("click", element_id="b2"), _step("done", summary="saved")])

    def verify(claim, obs, context):
        verdicts.append(claim)
        return (len(verdicts) >= 2, "bare confirmation page" if len(verdicts) < 2 else "")
    result = run_loop("Save the form with qa@example.invalid", lambda p: (next(replies), {}), mac, threading.Event(), verify=verify)
    assert mac.acts == ["click"], "the identical second submit must be refused before delivery"
    assert result.outcome == "done" and len(verdicts) >= 2


def test_pure_waits_do_not_spend_the_step_cap_and_an_unchanged_page_triggers_verification():
    mac, checks = SubmitMac(), []
    replies = iter([_step("wait", ms=1)] * 2 + [_step("done", summary="ok")])   # the 3rd reply is the answer-only call after the check

    def verify(claim, obs, context):
        checks.append(claim)
        return True, ""
    from coding_agents.cu_ai_loop import Caps
    result = run_loop("Look at the form", lambda p: (next(replies), {}), mac, threading.Event(), verify=verify,
                      caps=Caps(max_steps=3, explicit=True))
    assert result.outcome == "done" and checks
