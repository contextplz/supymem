"""Owned loopback protocol only; no browser, GUI, models or external requests."""

import json
import socket
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts/desktop"))
from cu_browser_form_fixture import BrowserFormFixture



def real_chrome_executable():
    """Use the same installed browser for every real fixture, including keys."""
    import shutil
    from desktop_runtime.cu_browser.chrome import CHROME_BIN
    if Path(CHROME_BIN).is_file():
        return CHROME_BIN
    return next((path for name in ("google-chrome", "chromium", "chromium-browser")
                 if (path := shutil.which(name))), None)


def test_real_browser_selection_prefers_chrome_consistently(monkeypatch):
    import shutil
    from desktop_runtime.cu_browser import chrome
    monkeypatch.setattr(chrome, "CHROME_BIN", "/missing/synthetic-browser")
    monkeypatch.setattr(shutil, "which", lambda name: {
        "google-chrome": "/owned/google-chrome", "chromium": "/owned/chromium"
    }.get(name))
    assert real_chrome_executable() == "/owned/google-chrome"


def stop_with_diagnostics(session):
    """Preserve hard cleanup failure and print only closed local attribution."""
    from desktop_runtime.cu_cleanup import cleanup_trace
    try:
        session.stop()
    except Exception as error:
        report = {'cleanup_trace': cleanup_trace(error),
                  'parent_exited': session.proc is not None and session.proc.poll() is not None,
                  'group_retired': bool(session._group_gone)}
        try:
            report['listener_absent'] = session._listener_alive() is False
        except Exception:
            report['listener_absent'] = None
        # No filenames, URLs, argv, document fields or raw exception bodies.
        error.add_note('OWNED_BROWSER_CLEANUP=' + json.dumps(report, sort_keys=True))
        raise


def test_cleanup_diagnostic_retains_failure_without_private_exception_text():
    class Session:
        proc = SimpleNamespace(poll=lambda: 0)
        _group_gone = True
        def stop(self):
            error = RuntimeError('browser_close_unconfirmed')
            error.cleanup_trace = [{'stage': 'listener_probe', 'outcome': 'error', 'at_monotonic': 1.0,
                                    'duration_ms': 1.0, 'error_type': 'RuntimeError',
                                    'secret_url': 'https://private.invalid/secret'}]
            raise error
        def _listener_alive(self):
            raise RuntimeError('private listener details')
    with pytest.raises(RuntimeError, match='browser_close_unconfirmed') as captured:
        stop_with_diagnostics(Session())
    report = json.loads(captured.value.__notes__[0].split('=', 1)[1])
    assert report['parent_exited'] is True and report['group_retired'] is True
    assert report['listener_absent'] is None
    assert report['cleanup_trace'][0]['stage'] == 'listener_probe'
    assert 'private' not in captured.value.__notes__[0]

class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


@pytest.fixture
def form():
    fixture = BrowserFormFixture().start()
    yield fixture
    fixture.close()


def post(form, body=None, **headers):
    body = form.expected if body is None else body
    request = Request(
        form.origin + form.submit_path,
        data=urlencode(body).encode(),
        headers={
            "Origin": form.origin,
            "Content-Type": "application/x-www-form-urlencoded",
            **headers,
        },
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=2) as response:
            return response.status
    except HTTPError as error:
        return error.code


def ack(form):
    with build_opener().open(form.origin + form.receipt_path, timeout=2) as response:
        return response.read().decode()


def browser(form, **changed):
    observed = SimpleNamespace(
        url=form.origin + form.receipt_path, text=form.marker, **{}
    )
    for key, value in changed.items():
        setattr(observed, key, value)
    return SimpleNamespace(b=SimpleNamespace(observe=lambda: observed))


def test_correct_single_post_and_fresh_owned_page_readback(form):
    assert (
        form.server.server_address[0] == "127.0.0.1"
        and form.server.server_address[1] > 0
    )
    assert post(form) == 303
    assert form.marker in ack(form)
    assert form.verify(browser(form))[0] is True
    assert form.final_verdict() is None  # cleanup is independently required
    form.close()
    assert form.final_verdict() is True
    receipt = form.public_receipt()
    assert receipt["synthetic"] and not receipt["counts_as_real_user"]
    assert receipt["form_posts"] == 1 and receipt["cleanup_verified"]


def test_missing_submission_or_ack_never_passes(form):
    assert form.verify(browser(form))[0] is False
    assert post(form) == 303
    assert form.verify(browser(form))[0] is False


def test_duplicate_submission_is_rejected_and_invalidates_receipt(form):
    assert post(form) == 303
    ack(form)
    assert post(form) == 400
    assert form.verify(browser(form))[0] is False
    assert form.public_receipt()["form_posts"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("nonce", "wrong"),
        ("project", "wrong"),
        ("email", "personal@example.com"),
        ("note", "wrong"),
        ("confirm", "no"),
        ("private_api_key", "never-log-this"),
    ],
)
def test_wrong_nonce_content_or_private_fields_rejected_without_echo(
    form, field, value, capsys
):
    body = {**form.expected, field: value}
    assert post(form, body) == 400
    assert form.verify(browser(form))[0] is False
    public = json.dumps(form.public_receipt())
    assert value not in public and "private_api_key" not in public
    assert not capsys.readouterr().err


def test_private_payload_never_appears_in_public_receipt(form):
    assert post(form) == 303
    ack(form)
    form.verify(browser(form))
    public = json.dumps(form.public_receipt())
    assert all(value not in public for value in form.expected.values())
    assert form.nonce not in public and form.receipt_id not in public


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://external.example"},
        {"Host": "external.example"},
        {"Content-Type": "application/json"},
    ],
)
def test_cross_origin_host_and_content_type_refused(form, headers):
    assert post(form, **headers) == 400
    assert form.public_receipt()["valid_posts"] == 0


def test_no_browser_or_wrong_page_is_unknown_or_failed(form):
    assert post(form) == 303
    ack(form)
    assert form.verify(None)[0] is None
    assert form.verify(browser(form, url="https://real.example/signup"))[0] is False
    assert form.verify(browser(form, text="model says done"))[0] is False


def test_form_html_is_self_contained_and_has_no_real_destination(form):
    with build_opener().open(form.origin + form.form_path, timeout=2) as response:
        source = response.read().decode()
        assert "form-action 'self'" in response.headers["Content-Security-Policy"]
    assert '<form method="post"' in source and '<button type="submit">' in source
    assert "<script" not in source and "https://" not in source
    assert "example.invalid" in form.task()


def test_teardown_cancels_partial_request_and_joins_owned_thread(form):
    client = socket.create_connection(form.server.server_address, timeout=1)
    client.sendall(b"POST /incomplete HTTP/1.1\r\n")
    time.sleep(0.02)
    started = time.monotonic()
    assert form.close()
    assert time.monotonic() - started < 2
    assert not form.thread.is_alive()
    client.close()
    assert form.close()


def test_duplicate_arriving_after_readback_still_fails_final_verdict(form):
    post(form)
    ack(form)
    assert form.verify(browser(form))[0] is True
    post(form)
    form.close()
    assert form.final_verdict() is False


def test_verifier_never_starts_an_unused_browser(form):
    post(form)
    ack(form)
    candidate = SimpleNamespace(
        b=None, factory=lambda: (_ for _ in ()).throw(AssertionError("no new browser"))
    )
    assert form.verify(candidate)[0] is None


def test_existing_general_browser_adapter_is_used_without_starting_chrome(form):
    from desktop_runtime.cu_ai_browser import BrowserMac

    post(form)
    ack(form)
    calls = []

    def readback():
        calls.append("observe-existing-owned-page")
        return SimpleNamespace(url=form.origin + form.receipt_path, text=form.marker)

    adapter = BrowserMac(
        backend=SimpleNamespace(observe=readback),
        factory=lambda: (_ for _ in ()).throw(AssertionError("must not launch")),
    )
    assert form.verify(adapter)[0] is True
    assert calls == ["observe-existing-owned-page"]


def test_fresh_page_readback_cannot_be_replaced_by_cached_or_model_claim(form):
    post(form)
    ack(form)
    owned = browser(form, text="Not accepted")
    owned.last = SimpleNamespace(url=form.origin + form.receipt_path, text=form.marker)
    assert form.verify(owned)[0] is False


def test_duplicate_fields_missing_fields_and_wrong_path_refused(form):
    body = {**form.expected, "nonce": [form.nonce, form.nonce]}
    data = urlencode(body, doseq=True).encode()
    request = Request(
        form.origin + form.submit_path,
        data=data,
        headers={
            "Origin": form.origin,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with pytest.raises(HTTPError) as error:
        build_opener(NoRedirect()).open(request, timeout=2)
    assert error.value.code == 400


def test_fresh_runs_have_distinct_nonce_receipt_and_payload(form):
    other = BrowserFormFixture().start()
    try:
        assert other.nonce != form.nonce and other.receipt_id != form.receipt_id
        assert (
            other.public_receipt()["payload_sha256"]
            != form.public_receipt()["payload_sha256"]
        )
        assert other.origin != form.origin
    finally:
        other.close()


def test_bounded_cleanup_uncertainty_never_verifies(form):
    post(form)
    ack(form)
    form.verify(browser(form))
    form.close()
    form.cleanup_verified = False
    assert form.final_verdict() is None


def test_missing_expected_field_rejected(form):
    body = dict(form.expected)
    del body["note"]
    assert post(form, body) == 400
    assert form.public_receipt()["valid_posts"] == 0


def test_wrong_owned_submit_path_rejected(form):
    request = Request(
        form.origin + "/submit/wrong",
        data=urlencode(form.expected).encode(),
        headers={
            "Origin": form.origin,
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    with pytest.raises(HTTPError) as error:
        build_opener(NoRedirect()).open(request, timeout=2)
    assert error.value.code == 400
    assert form.public_receipt()["valid_posts"] == 0


def test_receipt_page_before_creation_proof_cannot_ack(form):
    with pytest.raises(HTTPError) as error:
        build_opener().open(form.origin + form.receipt_path, timeout=2)
    assert error.value.code == 404
    assert form.public_receipt()["ack_page_reads"] == 0


def test_rejection_diagnostics_are_closed_and_never_echo_header_or_private_field(form):
    body = {**form.expected, "note": "private-never-echo", "unknown-private-field": "private-never-echo"}
    assert post(form, body, Origin="https://private-never-echo.example") == 400
    receipt = form.public_receipt()
    assert receipt["rejection_counts"] == {"origin_mismatch": 1, "field_unexpected": 1, "field_value_mismatch": 1}
    assert receipt["field_mismatch_counts"] == {"note": 1}
    assert "private-never-echo" not in json.dumps(receipt)


def test_null_origin_is_not_accepted_even_with_exact_fields(form):
    assert post(form, Origin="null") == 400
    assert form.public_receipt()["rejection_counts"] == {"origin_null": 1}
    assert form.public_receipt()["field_mismatch_counts"] == {}


def test_real_headless_browser_adapter_submits_exact_owned_form(tmp_path, form):
    """Real CDP/DOM actions and server receipt, no model or physical input."""
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip("real headless Chrome executable unavailable; not browser acceptance")
    session = ChromeSession(tmp_path / "owned-chrome", headless=True, chrome_bin=executable)
    adapter = None
    try:
        endpoint = session.start()
        backend = BrowserBackend(endpoint, download_dir=tmp_path / "owned-downloads")
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op="navigate", text=form.origin + form.form_path), None)
        password = 'synthetic-password-never-provider'
        backend.page.evaluate("value => { const input=document.createElement('input'); input.type='password'; input.value=value; document.body.appendChild(input); }", password)
        raw = backend.observe()
        assert password not in json.dumps(raw.elements) + raw.text
        private = adapter.observe()
        assert password not in json.dumps(private.elements) + private.notes
        for label, field in [("Project", "project"), ("Synthetic email", "email"), ("QA note", "note")]:
            observed = adapter.observe()
            element = next(e for e in observed.elements if e["label"] == label)
            adapter.act(Action(op="type", element_id=element["id"], text=form.expected[field]), observed)
        observed = adapter.observe()
        checkbox = next(e for e in observed.elements if e["label"] == "I understand this is synthetic QA")
        assert checkbox["checked"] is False
        adapter.act(Action(op="click", element_id=checkbox["id"]), observed)
        observed = adapter.observe()
        assert next(e for e in observed.elements if e["label"] == "I understand this is synthetic QA")["checked"] is True
        button = next(e for e in observed.elements if e["label"] == "Submit synthetic QA")
        adapter.act(Action(op="click", element_id=button["id"]), observed)
        assert form.verify(adapter)[0] is True, {"reasons": form.public_receipt()["rejection_counts"], "field_mismatches": form.public_receipt()["field_mismatch_counts"]}
        assert form.public_receipt()["form_posts"] == form.public_receipt()["valid_posts"] == 1
        assert form.public_receipt()["rejection_counts"] == {}
        evidence = adapter.completion_evidence()
        assert evidence["v"] == 1 and evidence["source"] == "owned_browser_backend"
        assert all(set(row[key]) == {"field_type", "checked"} for row in evidence["actions"] for key in ("before", "after") if key in row)
        filled = [row for row in evidence["actions"] if row["action"] == "type"]
        assert len(filled) == 3 and all(row["action_input_matched"] for row in filled)
        checks = [row for row in evidence["actions"] if row.get("before", {}).get("field_type") == "checkbox"]
        assert len(checks) == 1
        assert checks[0]["before"]["checked"] is False and checks[0]["after"]["checked"] is True
        assert any(row["method"] == "POST" and row["status"] == 303 for row in evidence["responses"])
        assert any(row["method"] == "GET" and row["status"] == 200 and row["resource"] == "document" for row in evidence["responses"])
        serialized = json.dumps(evidence)
        assert form.origin not in serialized and form.nonce not in serialized
        assert all(form.expected[field] not in serialized for field in ("project", "email", "note"))
        evidence["actions"].clear()
        assert adapter.completion_evidence()["actions"]  # callers cannot alter owned receipt history
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)


@pytest.mark.parametrize('change', ['replace_node', 'switch_page', 'independent_reindex'])
def test_real_browser_old_index_cannot_address_replaced_or_other_page(tmp_path, form, change):
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    adapter = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op='navigate', text=form.origin + form.form_path), None)
        observation = adapter.observe()
        target = next(e for e in observation.elements if e['label'] == 'Project')
        if change in {'replace_node', 'independent_reindex'}:
            backend.page.evaluate("() => {const old=document.querySelector('input[name=project]'); old.replaceWith(old.cloneNode(true));}")
            if change == 'independent_reindex':
                backend.observe()
        else:
            replacement = backend.context.new_page()
            replacement.set_content('<input name="replacement" data-cu-idx="' + target['id'][1:] + '">')
            backend.page = replacement
        with pytest.raises(RuntimeError, match='fresh_browser_observation_required|stale_browser_element'):
            adapter.act(Action(op='type', element_id=target['id'], text='must-not-reach-replacement'), observation)
        assert backend.page.locator('input').first.input_value() == ''
        fresh = adapter.observe()
        next_input = next(e for e in fresh.elements if e.get('name') in {'project', 'replacement'})
        adapter.act(Action(op='type', element_id=next_input['id'], text='fresh-input'), fresh)
        assert backend.page.locator('input').first.input_value() == 'fresh-input'
        adapter.act(Action(op='type', element_id=next_input['id'], text='same-node-followup'), fresh)
        assert backend.page.locator('input').first.input_value() == 'same-node-followup'
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)


@pytest.mark.parametrize('operation', ['key', 'scroll', 'single_char_type'])
def test_real_browser_same_url_reload_retires_old_document(tmp_path, form, operation):
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    adapter = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        backend.page.add_init_script("document.addEventListener('DOMContentLoaded',()=>{document.body.style.height='5000px';document.querySelector('input[name=project]')?.focus();});")
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op='navigate', text=form.origin + form.form_path), None)
        previous = adapter.observe()
        action = Action(op='scroll', direction='down', amount=3) if operation == 'scroll' else Action(op='type', text='a') if operation == 'single_char_type' else Action(op='key', chord='a')
        backend.page.reload(wait_until='domcontentloaded')
        with pytest.raises(RuntimeError, match='fresh_browser_observation_required'):
            adapter.act(action, previous)
        assert backend.page.locator('input[name=project]').input_value() == ''
        assert backend.page.evaluate('scrollY') == 0
        current = adapter.observe()
        adapter.act(action, current)
        if operation == 'scroll':
            assert backend.page.evaluate('scrollY') > 0
        else:
            assert backend.page.locator('input[name=project]').input_value() == 'a'
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)


def test_real_browser_keyboard_custody_and_password_refusal(tmp_path, form):
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    adapter = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op='navigate', text=form.origin + form.form_path), None)
        backend.page.evaluate("() => {const p=document.createElement('input');p.type='password';p.id='synthetic-secret';document.body.append(p);document.querySelector('input[name=project]').focus();}")
        current = adapter.observe()
        backend.page.locator('#synthetic-secret').focus()
        adapter.act(Action(op='key', chord='a'), current)
        assert backend.page.locator('input[name=project]').input_value() == 'a'
        assert backend.page.locator('#synthetic-secret').input_value() == ''
        backend.page.locator('#synthetic-secret').focus()
        password_observation = adapter.observe()
        with pytest.raises(RuntimeError, match='password fields need explicit task authorization'):
            adapter.act(Action(op='key', chord='b'), password_observation)
        assert backend.page.locator('#synthetic-secret').input_value() == ''
        backend.page.locator('input[name=project]').focus()
        keyboard_observation = adapter.observe()
        adapter.act(Action(op='key', chord='Tab'), keyboard_observation)
        adapter.act(Action(op='key', chord='c'), keyboard_observation)
        assert backend.page.locator('input[name=project]').input_value() == 'a'
        assert backend.page.locator('input[name=email]').input_value() == 'c'
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)


def test_real_browser_download_uses_owned_generated_destination(tmp_path, form):
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    backend = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        backend.page.goto(form.origin + form.form_path, wait_until='domcontentloaded')
        with backend.page.expect_download():
            backend.page.evaluate("() => {const a=document.createElement('a');a.href=URL.createObjectURL(new Blob(['synthetic owned output'],{type:'text/plain'}));a.download='../../untrusted-name.txt';document.body.append(a);a.click();}")
        assert len(backend.downloads) == 1
        destination = Path(backend.downloads[0])
        assert destination.parent == tmp_path / 'downloads'
        assert destination.name.startswith('download-')
        assert destination.suffix == '.txt'
        assert destination.read_text() == 'synthetic owned output'
        assert not list(destination.parent.glob('.partial-*'))
    finally:
        if backend:
            backend.close()
        stop_with_diagnostics(session)


def test_real_browser_select_and_history_navigation(tmp_path, form):
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    adapter = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op='navigate', text=form.origin + form.form_path), None)
        backend.page.evaluate("() => {const select=document.createElement('select'); select.name='plan'; select.innerHTML='<option>Basic</option><option>Team</option>'; document.body.appendChild(select);}")
        observed = adapter.observe()
        dropdown = next(e for e in observed.elements if e.get('name') == 'plan')
        adapter.act(Action(op='type', element_id=dropdown['id'], text='Team'), observed)
        assert backend.page.locator('select').input_value() == 'Team'
        assert adapter.completion_evidence()['actions'][-1]['action_input_matched'] is True
        adapter.act(Action(op='navigate', text=form.origin + '/owned-missing-page'), None)
        observed = adapter.observe()
        adapter.act(Action(op='key', chord='alt+left'), observed)
        assert backend.page.url == form.origin + form.form_path
        observed = adapter.observe()
        adapter.act(Action(op='key', chord='alt+right'), observed)
        assert backend.page.url == form.origin + '/owned-missing-page'
        from desktop_runtime.cu_completion_evidence import validated_evidence
        assert validated_evidence(adapter.completion_evidence()) is not None
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)


def test_real_browser_eperm_probe_requires_actual_empty_kernel_group(tmp_path, form, monkeypatch):
    import errno
    import signal
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    from desktop_runtime.cu_browser import chrome
    if sys.platform != 'darwin':
        pytest.skip('actual Darwin EPERM retirement contract')
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    backend = None
    probes = []
    original = chrome.os.killpg
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        backend.page.goto(form.origin + form.form_path, wait_until='domcontentloaded')
        backend.close()
        backend = None
        def eperm_probe(group, sig):
            assert group == session._owned_group
            probes.append(sig)
            if sig == 0:
                raise PermissionError(errno.EPERM, 'synthetic probe with actual owned kernel inventory')
            return original(group, sig)
        monkeypatch.setattr(chrome.os, 'killpg', eperm_probe)
        stop_with_diagnostics(session)
        assert session.closed and session._group_gone and session._group_permission_unconfirmed
        assert session.proc.poll() is not None and session._listener_alive() is False
        assert any(row.get('group_probe_reason') == 'kernel_group_empty' and row.get('group_member_count') == 0
                   for row in session.cleanup_trace)
        assert probes == [signal.SIGTERM, 0]
        session._signal_group(signal.SIGKILL)
        assert probes == [signal.SIGTERM, 0]
    finally:
        if backend:
            backend.close()
        stop_with_diagnostics(session)


@pytest.mark.parametrize('operation', ['double_click', 'right_click'])
def test_real_browser_pointer_operation_preserves_requested_semantics(tmp_path, form, operation):
    from coding_agents.cu_ai_loop import Action
    from desktop_runtime.cu_ai_browser import BrowserMac
    from desktop_runtime.cu_browser import BrowserBackend, ChromeSession
    from desktop_runtime.cu_completion_evidence import validated_evidence
    executable = real_chrome_executable()
    if not executable:
        pytest.skip('real owned headless Chrome unavailable; not browser acceptance')
    session = ChromeSession(tmp_path / 'owned-chrome', headless=True, chrome_bin=executable)
    adapter = None
    try:
        backend = BrowserBackend(session.start(), download_dir=tmp_path / 'downloads')
        adapter = BrowserMac(backend=backend)
        adapter.act(Action(op='navigate', text=form.origin + form.form_path), None)
        backend.page.evaluate("""() => {
          window.syntheticEvents={left:0,double:0,context:0};
          const button=document.createElement('button');button.type='button';button.textContent='Owned pointer target';
          button.addEventListener('click',()=>window.syntheticEvents.left++);
          button.addEventListener('dblclick',()=>window.syntheticEvents.double++);
          button.addEventListener('contextmenu',event=>{event.preventDefault();window.syntheticEvents.context++;});
          document.body.prepend(button);
        }""")
        observed = adapter.observe()
        target = next(e for e in observed.elements if e['label'] == 'Owned pointer target')
        adapter.act(Action(op=operation, element_id=target['id']), observed)
        actual = backend.page.evaluate('syntheticEvents')
        assert actual == ({'left': 2, 'double': 1, 'context': 0} if operation == 'double_click'
                          else {'left': 0, 'double': 0, 'context': 1})
        evidence = adapter.completion_evidence()
        assert evidence['actions'][-1]['action'] == operation
        assert evidence['actions'][-1]['status'] == 'ok'
        assert validated_evidence(evidence) is not None
    finally:
        if adapter:
            adapter.close()
        stop_with_diagnostics(session)
