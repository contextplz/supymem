"""Rendered V10 recovery against a local Console with synthetic native receipts.

Set V10_CONSOLE_URL to opt in. All gateway calls use the existing native
fixture; external browser requests are blocked and no paid work is started.
"""
from __future__ import annotations

import os
from urllib.parse import urlsplit

import pytest

from tests.browser.test_intake_name_browser import (
    _NATIVE_FIXTURE,
    _VOICE_TOOL_FIXTURE,
    _preview,
    isolated_chrome as isolated_chrome,
)

pytestmark = pytest.mark.skipif(not os.getenv("V10_CONSOLE_URL"), reason="local Console URL not supplied")

_V10_FIXTURE = r"""
(() => {
  const key = 'v10-browser-fixture';
  const state = window.__v10 = JSON.parse(sessionStorage.getItem(key) || 'null') || {research:'leased'};
  const save = () => sessionStorage.setItem(key, JSON.stringify(state));
  window.__setResearch = status => { state.research = status; save(); };
  const invoke = window.__TAURI__.core.invoke, held = [];
  window.__releaseTargetHistory = () => {
    state.holdHistory = false; save(); held.splice(0).forEach(release => release());
  };
  const record = args => window.__requests.push({actor:window.__fixture.actor,method:args.method,path:args.path,body:args.body || {}});
  window.__TAURI__.core.invoke = async (command, args) => {
    if (command !== 'desktop_api_request') return invoke(command, args);
    const path = args.path, status = state.research, failed = status === 'failed_dead';
    if (path.startsWith('/api/entry/chat/history?target_venture_id=') && state.holdHistory) {
      record(args); state.historyHeld = true;
      return new Promise(resolve => held.push(() => resolve(invoke(command, args))));
    }
    const job = {id:'job_private_v10',kind:'research.deep',status,workspace_id:'ws_fixture',venture_id:'ven_fixture',
      revision_id:'revision_fixture',attempts:2,max_attempts:5,created_at:null,scheduled_for:null,
      started_at:null,locked_at:null,finished_at:null,
      error_code:failed ? 'research_boundary_blocked' : status === 'retry_wait' ? 'research_provider_rate_limited' : null};
    const report = {id:'dossier_private_v10',kind:'deep',venture_id:'ven_fixture',revision_id:'revision_fixture',
      state:failed ? 'failed' : 'running',job_id:job.id,created_at:'2026-10-06T12:42:42Z',findings:{},
      summary:{error_code:job.error_code,coverage_gaps:['Research did not finish.'],
        verdict:{verdict:'not_yet',title:'Not yet recommended',why:[],why_not:['Research did not finish.'],
          next_step:'Continue to plan',source_count:0,lead_count:0},usage:{credits_micro:0,has_unsettled:false}}};
    let data;
    if (path === '/api/ui-config') data = {founder_ui:'classic'};  // research/site/gtm are chat-first hidden in "home"
    else if (path === '/api/ventures/ven_fixture/research/dossiers') data = {items:[report]};
    else if (path === '/api/ventures/ven_fixture/research/dossiers/dossier_private_v10') data = report;
    else if (path === '/api/ventures/ven_fixture/idea/sessions/idea_fixture') data = {
      id:'idea_fixture',venture_id:'ven_fixture',workspace_id:'ws_fixture',state:'confirmed',
      current_revision:{id:'revision_fixture',structured:{project_intent:'startup'}}};
    else if (path.startsWith('/api/workspaces/ws_fixture/jobs?')) {
      const activeOnly = new URL(path, 'http://fixture.invalid').searchParams.get('active_only') === 'true';
      data = {items:failed && activeOnly ? [] : [job]};
    } else if (path === '/api/jobs/job_private_v10') data = job;
    else if (path === '/api/ventures/ven_fixture/dashboard') data = {
      venture:{id:'ven_fixture',name:'Fixture venture'},needs_you:[],
      run_status:{state:failed ? 'failed' : status === 'retry_wait' ? 'queued' : 'working',stage:'Research',
        activity:failed ? 'Research needs attention.' : status === 'retry_wait' ? 'Retrying in a moment' : 'Researching your idea.',
        action:{label:failed ? 'Run research again' : 'View progress',href:'/v/ven_fixture/research'}},
      next_step:{key:failed ? 'research.failed' : 'work.running',stage:'Research',title:'Research your idea',
        detail:'Review the saved research.',cta_label:failed ? 'Run research again' : 'View progress',
        href:'/v/ven_fixture/research',chat_action:null,payload:null}};
    else return invoke(command, args);
    record(args); return {status:200,data};
  };
})()
"""


@pytest.fixture
def v10_page(isolated_chrome):
    browser, expect = isolated_chrome
    base = os.environ["V10_CONSOLE_URL"].rstrip("/")
    parsed = urlsplit(base)
    assert parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).netloc == parsed.netloc else route.abort())
        page = context.new_page()
        page.add_init_script(_NATIVE_FIXTURE + _V10_FIXTURE)
        page.goto(f"{base}/new", timeout=60_000, wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="What is your idea?", exact=True)).to_be_visible(timeout=30_000)
        page.wait_for_function("window.__verifiedActors.includes('usr_fixture_a') && window.__requests.some(r => r.path === '/api/entry/chat/history')")
        page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
        yield page, expect, base


def _assert_no_internal_copy(page):
    text = page.locator("body").inner_text()
    for internal in ["job_private_v10", "dossier_private_v10", "research.deep", "research_boundary_blocked",
                     "research_provider_rate_limited", "failed_dead", "handler_failed", "ValueError"]:
        assert internal not in text


def _posts(page):
    return [request for request in page.evaluate("window.__requests")
            if request["method"] == "POST" and not request["path"].startswith("/api/telemetry")]


def test_slow_confirmed_history_preserves_read_only_receipt_without_welcome_flash(v10_page):
    page, expect, _ = v10_page
    _preview(page, expect)
    name = page.get_by_role("textbox", name="Your venture name", exact=True)
    expect(name).to_be_visible()
    name.fill("Tutor Grove")
    expect(page.get_by_role("heading", name="What is your idea?", exact=True)).to_have_count(0)
    page.evaluate("""() => {
      window.__v10.holdHistory = true;
      window.__welcomeFlashed = false;
      new MutationObserver(() => {
        if ([...document.querySelectorAll('h2')].some(el => el.textContent === 'What is your idea?'))
          window.__welcomeFlashed = true;
      }).observe(document.body, {childList:true,subtree:true,characterData:true});
    }""")
    page.get_by_role("button", name="Continue", exact=True).click()
    page.wait_for_url("**/new?venture=ven_fixture")
    page.wait_for_function("window.__v10.historyHeld")
    pending = page.get_by_role("region", name="Opening saved conversation", exact=True)
    expect(pending).to_be_visible()
    expect(pending.get_by_role("heading", name="Opening your conversation…", exact=True)).to_be_visible()
    expect(pending.get_by_text("Your booking idea is saved.", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Message the team", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="What is your idea?", exact=True)).to_have_count(0)
    assert not page.evaluate("window.__welcomeFlashed")
    assert [request["path"] for request in _posts(page)] == [
        "/api/entry/chat/preview", "/api/entry/chat/preview", "/api/entry/chat/confirm"]
    page.evaluate("window.__releaseTargetHistory()")
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Message the team", exact=True)).to_be_visible()
    assert not page.evaluate("window.__welcomeFlashed")
    _assert_no_internal_copy(page)


@pytest.mark.parametrize("status", ["leased", "retry_wait"])
def test_active_research_has_one_progress_card_and_no_verdict_or_planning(status, v10_page):
    page, expect, base = v10_page
    page.evaluate("status => window.__setResearch(status)", status)
    page.goto(f"{base}/v/ven_fixture/research", wait_until="domcontentloaded", timeout=60_000)
    expect(page.get_by_role("heading", name="Researching…", exact=True)).to_be_visible(timeout=30_000)
    if status == "retry_wait":
        expect(page.get_by_text("Retrying in a moment", exact=True).first).to_be_visible()
    for label in ["Not yet recommended", "Research didn't finish"]:
        expect(page.get_by_role("heading", name=label, exact=True)).to_have_count(0)
    expect(page.get_by_text("Top risk", exact=False)).to_have_count(0)
    expect(page.get_by_role("link", name="Continue to plan", exact=False)).to_have_count(0)
    expect(page.get_by_role("button", name="Run research again", exact=True)).to_have_count(0)
    expect(page.get_by_role("region", name="Next step", exact=True)).to_have_count(0)
    _assert_no_internal_copy(page)
    assert not _posts(page)


def test_failed_research_has_one_current_retry_and_no_raw_job_details(v10_page):
    page, expect, base = v10_page
    page.evaluate("window.__setResearch('failed_dead')")
    page.goto(f"{base}/v/ven_fixture/research", wait_until="domcontentloaded", timeout=60_000)
    expect(page.get_by_role("heading", name="Research didn't finish", exact=True)).to_be_visible(timeout=30_000)
    retry = page.get_by_role("button", name="Run research again", exact=True)
    expect(retry).to_have_count(1)
    expect(retry).to_be_enabled()
    expect(page.get_by_role("heading", name="Saved job needs attention", exact=True)).to_have_count(0)
    expect(page.get_by_role("heading", name="Not yet recommended", exact=True)).to_have_count(0)
    _assert_no_internal_copy(page)
    retry.click()
    expect(page.get_by_role("heading", name="Run research for your current idea?", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="Confirm research request", exact=True)).to_be_enabled()
    assert not _posts(page)  # Reviewing a retry never submits a job.


# Extend the existing synthetic voice peer. Hold the server configuration ack
# long enough to inspect Connecting, then drive its real phase machine with
# provider-shaped events. No hardware or provider request leaves this page.
_VOICE_LAYOUT_FIXTURE = r"""
(() => {
  const voice = window.__voice, invoke = window.__TAURI__.core.invoke;
  voice.holdConfig = true; voice.sample = 176;
  window.__TAURI__.core.invoke = async (command, args) => {
    if (command === 'desktop_api_request' && args.path === '/api/entry/voice/session') {
      window.__requests.push({actor:window.__fixture.actor,method:args.method,path:args.path,body:args.body || {}});
      return {status:200,data:{client_secret:'ek_fixture',voice_telemetry_id:'ane_voice_fixture'}};
    }
    return invoke(command, args);
  };
  const getUserMedia = navigator.mediaDevices.getUserMedia;
  navigator.mediaDevices.getUserMedia = async (...args) => {
    voice.stream = await getUserMedia(...args); return voice.stream;
  };
  const Audio = window.AudioContext;
  window.AudioContext = class extends Audio {
    createAnalyser() {
      const analyser = super.createAnalyser();
      analyser.getByteTimeDomainData = buffer => buffer.fill(voice.sample);
      return analyser;
    }
  };
  const Peer = window.RTCPeerConnection;
  window.RTCPeerConnection = class extends Peer {
    constructor(...args) { super(...args); voice.peer = this; }
    createDataChannel(...args) {
      const channel = super.createDataChannel(...args), send = channel.send;
      channel.send = raw => {
        const event = JSON.parse(raw);
        if (event.type === 'session.update' && voice.holdConfig) {
          voice.sent.push(event); voice.pendingSession = event.session; return;
        }
        send.call(channel, raw);
      };
      return channel;
    }
  };
  voice.releaseConfig = () => {
    voice.holdConfig = false;
    voice.emit({type:'session.updated',session:voice.pendingSession});
  };
  voice.remoteAudio = () => voice.peer.ontrack?.({streams:[new MediaStream()]});
})()
"""

_BUILD_FAILURE_FIXTURE = r"""
(() => {
  const invoke = window.__TAURI__.core.invoke, state = window.__v10;
  window.__setBuildFailures = count => {
    state.buildFailures = count;
    sessionStorage.setItem('v10-browser-fixture', JSON.stringify(state));
  };
  window.__TAURI__.core.invoke = async (command, args) => {
    if (command === 'list_local_tools') return [{name:'codex',label:'Codex',installed:true,cli_path:'/synthetic/codex'}];
    if (command === 'device_work_order_capabilities') return {pagebuilder_reference_v1:true};
    if (command !== 'desktop_api_request') return invoke(command, args);
    let data, status = 200;
    if (args.path === '/api/ventures/ven_fixture/pagebuilder/profile') data = {
      artifact_profile:'static_landing_v1',task_id:null,plan_id:null,quote:null,
      current:{job_id:'build_private_v10',state:'failed',error:'pagebuilder_renderer_unavailable',
        consecutive_failures:state.buildFailures || 1,queue_state:'failed_dead',native_ready:false,candidates:[]}};
    else if (args.path === '/api/ventures/ven_fixture/device-work-orders') data = {orders:[]};
    else if (args.path.startsWith('/api/ventures/ven_fixture/site/publication')) data = {
      state:'blocked',draft:null,deployment:null,blockers:[{code:'pagebuilder_candidate_required',message:'Build a page to continue.'}]};
    else if (args.path === '/api/ventures/ven_fixture/builder/choice') data = {};
    else if (args.path === '/api/workspaces/ws_fixture/site?venture_id=ven_fixture') { status = 404; data = {}; }
    else return invoke(command, args);
    window.__requests.push({actor:window.__fixture.actor,method:args.method,path:args.path,body:args.body || {}});
    return {status,data};
  };
})()
"""


def _voice_preview(page, expect):
    page.add_init_script("\n;\n".join([_VOICE_TOOL_FIXTURE, _VOICE_LAYOUT_FIXTURE]))
    page.evaluate("window.__enableVoice()")
    page.reload(wait_until="domcontentloaded")
    page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
    expect(page.get_by_role("button", name="Tap to talk, or hold Space or the mic", exact=True)).to_be_visible()
    _preview(page, expect)
    page.get_by_role("textbox", name="Your venture name", exact=True).fill("Tutor Grove")
    return page.get_by_role("button", name="Continue", exact=True)


def _assert_disjoint(left, right):
    assert left and right
    overlap = (min(left["x"] + left["width"], right["x"] + right["width"]) > max(left["x"], right["x"])
               and min(left["y"] + left["height"], right["y"] + right["height"]) > max(left["y"], right["y"]))
    assert not overlap, {"continue": left, "voice_status": right}


@pytest.mark.parametrize("width", [1280, 760], ids=["desktop", "compact"])
def test_voice_connecting_orb_and_transcript_never_cover_pending_continue(width, v10_page):
    page, expect, _ = v10_page
    page.set_viewport_size({"width": width, "height": 900})
    continuation = _voice_preview(page, expect)
    expect(continuation).to_be_enabled()
    mic = page.locator('[data-usage-control="voice_mic_press"]')
    mic.click()
    root = page.locator('[data-voice-phase="connecting"]')
    expect(root).to_be_visible()
    expect(mic).to_have_attribute("title", "Connecting…")
    expect(mic).to_have_attribute("aria-busy", "true")
    assert "connecting" in mic.get_attribute("class") and "listening" not in mic.get_attribute("class")
    spinner = mic.locator("span")
    expect(spinner).to_be_visible()
    assert spinner.evaluate("el => getComputedStyle(el).borderTopStyle") == "solid"
    expect(page.locator('[data-voice-orb]')).to_have_attribute("aria-hidden", "true")
    assert root.evaluate("el => el.parentElement === document.body")
    status = page.locator('[data-voice-status]')
    assert status.evaluate("el => getComputedStyle(el).position") == "fixed"
    _assert_disjoint(continuation.bounding_box(), status.bounding_box())

    page.wait_for_function("window.__voice.pendingSession")
    page.evaluate("window.__voice.releaseConfig()")
    expect(page.locator('[data-voice-phase="listening"]')).to_be_visible()
    expect(mic).to_have_attribute("aria-busy", "false")
    page.wait_for_function("Number(document.querySelector('[data-voice-phase]').style.getPropertyValue('--lvl')) > .5")
    page.evaluate("""() => {
      window.__voice.emit({type:'conversation.item.input_audio_transcription.delta',
        delta:'The booking workflow should let independent tutors choose available times. '.repeat(18)});
      window.__voice.remoteAudio();
      window.__voice.emit({type:'output_audio_buffer.started'});
      window.__voice.emit({type:'response.output_audio_transcript.delta',
        delta:'Your saved direction stays on screen while we talk. '.repeat(18)});
    }""")
    expect(page.locator('[data-voice-phase="speaking"]')).to_be_visible()
    expect(page.get_by_text("Charlie is speaking", exact=True)).to_be_visible()
    _assert_disjoint(continuation.bounding_box(), status.bounding_box())
    expect(continuation).to_be_enabled()
    assert page.evaluate("[window.__voice.captures,window.__voice.sdpExchanges]") == [1, 1]
    assert not [request for request in _posts(page) if request["path"] == "/api/entry/chat/confirm"]
    assert not page.evaluate("window.__voice.sent.some(event => event.type === 'response.create')")
    page.get_by_role("button", name="End voice chat", exact=True).filter(has_text="End").click()
    expect(page.locator('[data-voice-orb]')).to_have_count(0)
    expect(mic).to_have_attribute("aria-pressed", "false")


def test_voice_reduced_motion_keeps_orb_and_connecting_ring_static(v10_page):
    page, expect, _ = v10_page
    page.emulate_media(reduced_motion="reduce")
    _voice_preview(page, expect)
    mic = page.locator('[data-usage-control="voice_mic_press"]')
    mic.click()
    expect(page.locator('[data-voice-phase="connecting"]')).to_be_visible()
    assert mic.locator("span").evaluate("el => getComputedStyle(el).animationName") == "none"
    orb = page.locator('[data-voice-orb]')
    assert orb.evaluate("el => getComputedStyle(el).transform") == "none"
    assert orb.evaluate("el => getComputedStyle(el, '::after').animationName") == "none"
    page.wait_for_function("window.__voice.pendingSession")
    page.evaluate("window.__voice.releaseConfig()")
    expect(page.locator('[data-voice-phase="listening"]')).to_be_visible()
    assert page.locator('[data-voice-phase]').evaluate("el => el.style.getPropertyValue('--lvl')") == "0"
    page.evaluate("window.__voice.emit({type:'input_audio_buffer.speech_stopped'})")
    expect(page.locator('[data-voice-phase="thinking"]')).to_be_visible()
    assert orb.evaluate("el => getComputedStyle(el).animationName") == "none"
    assert orb.evaluate("el => getComputedStyle(el).rotate") in {"none", "0deg"}
    assert orb.evaluate("el => getComputedStyle(el).transform") == "none"
    assert not page.evaluate("window.__voice.sent.some(event => event.type === 'response.create')")
    page.get_by_role("button", name="End voice chat", exact=True).filter(has_text="End").click()
    expect(page.locator('[data-voice-orb]')).to_have_count(0)


def test_second_failed_page_build_prioritizes_customers_with_aligned_controls(v10_page):
    page, expect, base = v10_page
    page.add_init_script(_BUILD_FAILURE_FIXTURE)
    page.goto(f"{base}/v/ven_fixture/site", wait_until="domcontentloaded", timeout=60_000)
    page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
    card = page.locator("#site-build")
    expect(card.get_by_role("heading", name="The landing page build stopped", exact=True)).to_be_visible(timeout=30_000)
    retry = card.get_by_role("button", name="Build my landing page again", exact=True)
    customers = card.get_by_role("link", name="Find customers", exact=True)
    expect(retry).to_have_class("primary")
    expect(customers).to_have_class("secondary")
    page.evaluate("window.__setBuildFailures(2)")
    page.reload(wait_until="domcontentloaded")
    page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
    expect(customers).to_have_class("primary", timeout=30_000)
    expect(retry).to_have_class("secondary")
    expect(card.get_by_text("The landing page build did not work twice. Find customers now and try the page again later.", exact=True)).to_be_visible()
    expect(customers).to_have_attribute("href", "/gtm?venture=ven_fixture")
    expect(card.locator(".primary")).to_have_count(1)
    controls = [retry.bounding_box(), customers.bounding_box()]
    assert all(controls)
    assert abs(controls[0]["y"] - controls[1]["y"]) <= 2
    assert abs(controls[0]["height"] - controls[1]["height"]) <= 2
    for internal in ["build_private_v10", "pagebuilder_renderer_unavailable", "failed_dead"]:
        assert internal not in card.inner_text()
    assert not _posts(page)
    customers.click()
    page.wait_for_url("**/gtm?venture=ven_fixture")
    assert not [request for request in _posts(page) if "/pagebuilder/" in request["path"]]
