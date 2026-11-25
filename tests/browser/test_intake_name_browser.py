"""Optional rendered intake regressions against a running local Console.

Set INTAKE_CONSOLE_URL (for example http://127.0.0.1:3110). Native API
fixtures never create real ventures or spend credits. Installed Chrome runs
with a fresh temporary profile, never the founder's Default profile.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import pytest

pytestmark = pytest.mark.skipif(not os.getenv("INTAKE_CONSOLE_URL"), reason="local Console URL not supplied")

_NATIVE_FIXTURE = r"""
const key = 'intake-browser-fixture';
const state = JSON.parse(sessionStorage.getItem(key) || 'null') || {
  actor:'usr_fixture_a', proposal:null, history:[], requests:[], failures:[], suppressHistory:false
};
const save = () => sessionStorage.setItem(key, JSON.stringify(state));
const listeners = {};
window.__fixture = state;
window.__requests = state.requests;
window.__verifiedActors = [];
window.addEventListener('supymem-session-changed', event => {
  if (event.detail?.subject) window.__verifiedActors.push(event.detail.subject);
});
window.__setFailures = failures => { state.failures = failures; save(); };
window.__setOverview = (path, step) => { state.overviewPath = path; state.overviewStep = step; save(); };
window.__enableVoice = () => { state.voiceEnabled = true; save(); };
window.__changeActor = actor => {
  const previous = state.actor;
  state.actor = actor; state.suppressHistory = true; save();
  // The actual native auth listener clears the real client memo and epoch;
  // /auth/session then verifies the fixture actor. No forged verified event.
  for (const handler of [...(listeners['supymem-native-auth'] || [])]) {
    handler({payload:{state:'signed_out',subject:previous}});
  }
};
window.__hasNativeAuthListener = () => Boolean(listeners['supymem-native-auth']?.length);
window.__TAURI__ = { core: { invoke: async (command, args) => {
  if (command === 'gateway_url') return 'http://127.0.0.1:3200';
  if (command === 'desktop_auth_status') return {state:'connected',subject:state.actor};
  if (command === 'desktop_permission_status') return {permissions:{screen_recording:true,accessibility:true,event_synthesizing:true}};
  if (command !== 'desktop_api_request') return null;
  const path = args.path, body = args.body || {};
  state.requests.push({actor:state.actor,method:args.method,path,body}); save();
  if (path === '/auth/session') return {status:200,data:{authenticated:true,subject:state.actor}};
  if (path.endsWith('/voice/config')) return {status:200,data:{voice_enabled:Boolean(state.voiceEnabled)}};
  if (path.startsWith('/api/entry/chat/history')) return {status:200,data:{items:state.suppressHistory ? [] : state.history}};
  if (path === '/api/entry/chat/preview') {
    const needsAudience = !body.text.includes('\nAudience:') && !/^https?:\/\//.test(body.text);
    state.proposal = {id:needsAudience ? 'pr_audience' : 'pr_fixture',route:needsAudience ? 'clarify' : 'startup',
      state:needsAudience ? 'clarify' : 'pending',message:needsAudience ? 'Who is it for?' : 'Let us shape this booking idea.',
      choices:[],button_label:'Continue',skipped_steps:[],next_step:needsAudience ? 'clarify' : 'idea',expires_at:'2099-01-01T00:00:00Z'};
    state.history.push({text:body.text,proposal:state.proposal}); save();
    if (needsAudience) return {status:200,data:{status:'clarify',id:state.proposal.id,question:state.proposal.message,choices:[]}};
    return {status:200,data:{status:'proposal',proposal:state.proposal}};
  }
  if (path.endsWith('/cancel')) {
    const proposal = state.history.findLast(item => item.proposal.id === path.split('/').at(-2)).proposal;
    proposal.state = 'cancelled'; save();
    return {status:200,data:{status:'cancelled'}};
  }
  if (path === '/api/entry/chat/proposals/pr_fixture/name') {
    const failure = state.failures.shift(); save();
    if (failure === 0) throw new Error('Synthetic transport interruption');
    if (failure) return {status:failure,data:{detail:{error:{code:'name_unavailable',message:'Synthetic name suggestion unavailable'}}}};
    const name = state.suggestedName || 'Slotwise';
    state.proposal.venture_name = name; state.history.at(-1).proposal = state.proposal; save();
    return {status:200,data:{name}};
  }
  if (path === '/api/entry/chat/proposals/pr_fixture') return {status:200,data:{proposal:state.proposal}};
  if (path === '/api/entry/chat/confirm') {
    const receipt = {status:'committed',route:'startup',venture_id:'ven_fixture',workspace_id:'ws_fixture',next_step:'idea',
      action:{kind:'idea',venture_id:'ven_fixture',href:'/v/ven_fixture/idea'},message:'Your booking idea is saved.'};
    state.proposal.state = 'committed'; state.proposal.receipt = receipt;
    state.proposal.venture_name = body.name; state.history.at(-1).proposal = state.proposal; save();
    return {status:200,data:receipt};
  }
  if (path === '/api/ventures') return {status:200,data:{items:state.suppressHistory ? [] : state.proposal?.receipt ? [{id:'ven_fixture',name:state.proposal.venture_name}] : []}};
  if (path === '/api/ventures/ven_fixture') return state.suppressHistory
    ? {status:403,data:{detail:{code:'forbidden'}}}
    : {status:200,data:{id:'ven_fixture',workspace_id:'ws_fixture',name:state.proposal?.venture_name || 'Fixture venture',
      raw_idea:state.history.at(0)?.text || '',intake_path:state.overviewPath,stage:'idea',entry_mode:'idea'}};
  if (path === '/api/ventures/ven_fixture/dashboard') return {status:200,data:{venture:{id:'ven_fixture'},next_step:state.overviewStep}};
  if (path === '/api/ventures/ven_fixture/idea/sessions') return {status:200,data:{items:[{id:'idea_fixture',state:'confirmed'}]}};
  if (['/api/ventures/ven_fixture/plans','/api/ventures/ven_fixture/apps'].includes(path) || path.startsWith('/api/workspaces/ws_fixture/jobs?')) return {status:200,data:{items:[]}};
  if (path === '/api/ventures/ven_fixture/onboarding') return {status:404,data:{}};
  if (path.startsWith('/api/ventures/ven_fixture/chat/conversations')) return {status:200,data:{
    venture_id:'ven_fixture',workspace_id:'ws_fixture',actor_id:state.actor,legacy_conversation_id:'conv_fixture',
    items:[{id:'conv_fixture',venture_id:'ven_fixture',workspace_id:'ws_fixture',visibility:'legacy_shared',title:'Saved chat',created_at:null,updated_at:null}]
  }};
  if (path.startsWith('/api/ventures/ven_fixture/chat')) return {status:200,data:{messages:[],conversation_id:'conv_fixture'}};
  return {status:200,data:[]};
}}, event:{listen:async (event, handler) => {
  (listeners[event] ||= []).push(handler);
  return () => { listeners[event] = listeners[event].filter(item => item !== handler); };
}}};
"""

_REROUTE_FIXTURE = r"""
const originalInvoke = window.__TAURI__.core.invoke;
window.__TAURI__.core.invoke = async (command, args) => {
  if (command !== 'desktop_api_request') return originalInvoke(command, args);
  const state = window.__fixture, path = args.path, body = args.body || {};
  const reroute = path === '/api/entry/chat/preview' && body.target_venture_id;
  const directionRequest = path.startsWith('/api/entry/chat/proposals/pr_direction') ||
    (path === '/api/entry/chat/confirm' && body.proposal_id === 'pr_direction');
  if (!reroute && !directionRequest) return originalInvoke(command, args);
  state.requests.push({actor:state.actor,method:args.method,path,body});
  const save = () => {
    const saved = state.history.findLast(item => item.proposal.id === 'pr_direction');
    if (saved) saved.proposal = state.direction;
    sessionStorage.setItem('intake-browser-fixture', JSON.stringify(state));
  };
  if (reroute) {
    const clarify = state.rerouteState === 'clarify' && !body.text.includes('Clarification:');
    state.direction = {id:'pr_direction',route:clarify ? 'clarify' : 'personal_build',
      state:clarify ? 'clarify' : state.rerouteState === 'committing' ? 'committing' : 'pending',
      target_venture_id:body.target_venture_id,intake_path:'personal_build',
      message:clarify ? 'Is this a personal tool or a startup?' : 'Build a private scheduling tool.',
      button_label:'Review build',choices:clarify ? ['Personal tool','Startup'] : [],
      skipped_steps:[],next_step:clarify ? 'clarify' : 'build',expires_at:'2099-01-01T00:00:00Z'};
    state.history.push({text:body.text,proposal:state.direction}); save();
    return {status:200,data:clarify
      ? {status:'clarify',question:state.direction.message,choices:state.direction.choices}
      : {status:'proposal',proposal:state.direction}};
  }
  if (path.endsWith('/cancel')) {
    state.direction.state = 'cancelled'; save();
    return {status:200,data:{status:'cancelled'}};
  }
  if (path === '/api/entry/chat/confirm') {
    const receipt = {status:'committed',route:'personal_build',venture_id:'ven_fixture',
      workspace_id:'ws_fixture',next_step:'build',intake_path:'personal_build',
      action:{kind:'build_review',venture_id:'ven_fixture',href:'/v/ven_fixture/idea'},
      message:'Your personal build direction is saved.'};
    state.direction.state = 'committed'; state.direction.receipt = receipt; save();
    return {status:200,data:receipt};
  }
  save(); return {status:200,data:{proposal:state.direction}};
};
"""

_NATIVE_FIXTURE += _REROUTE_FIXTURE

_VOICE_TOOL_FIXTURE = r"""
(() => {
  const state = window.__fixture, invoke = window.__TAURI__.core.invoke;
  const voice = window.__voice = {sent:[],captures:0,sdpExchanges:0,saved:false};
  const action = {label:'Review voice project',href:'/v/ven_fixture/idea',style:'primary'};
  const reply = {body_md:'Your project is ready for review.',actions:[action],partial:false};
  const save = () => sessionStorage.setItem('intake-browser-fixture', JSON.stringify(state));
  window.__TAURI__.core.invoke = async (command, args) => {
    if (command !== 'desktop_api_request') return invoke(command, args);
    const path = args.path, body = args.body || {};
    if (path === '/api/ventures/ven_fixture/voice/session' ||
        path === '/api/ventures/ven_fixture/voice/tools/suggest_next_step') {
      state.requests.push({actor:state.actor,method:args.method,path,body}); save();
      if (path.endsWith('/voice/session')) return {status:200,data:{client_secret:'ek_fixture',
        voice_telemetry_id:'ane_voice_fixture',voice_session_id:'vsn_voice_fixture'}};
      if (body.call_id === 'call_retired_owner') return new Promise(resolve => {
        window.__finishVoiceTool = () => resolve({status:200,data:{body_md:'Former founder private result',
          actions:[{...action,label:'Former founder private action'}],partial:false}});
      });
      voice.saved = true;
      return {status:200,data:reply};
    }
    if (voice.saved && args.method === 'GET' &&
        path === '/api/ventures/ven_fixture/chat?conversation_id=conv_fixture' && state.actor === 'usr_fixture_a') {
      state.requests.push({actor:state.actor,method:args.method,path,body}); save();
      return {status:200,data:{conversation_id:'conv_fixture',items:[{id:'msg_voice_fixture',role:'assistant',
        body_md:'Here is your next step. Nothing has run; choose the button to continue.',actions:[action]}]}};
    }
    return invoke(command, args);
  };
  navigator.mediaDevices.getUserMedia = async () => {
    voice.captures++;
    const track = {enabled:true,stop(){}};
    return {getTracks:() => [track],getAudioTracks:() => [track]};
  };
  window.AudioContext = class {
    createAnalyser() { return {fftSize:512,getByteTimeDomainData:buffer => buffer.fill(128)}; }
    createMediaStreamSource() { return {connect(){}}; }
    async close() {}
  };
  window.RTCPeerConnection = class {
    addTrack() {}
    createDataChannel() {
      const channel = {readyState:'connecting',send(raw) {
        const event = JSON.parse(raw); voice.sent.push(event);
        if (event.type === 'session.update') queueMicrotask(() => channel.onmessage?.({
          data:JSON.stringify({type:'session.updated',session:event.session})}));
      },close() { this.readyState = 'closed'; }};
      voice.channel = channel;
      voice.emit = event => channel.onmessage?.({data:JSON.stringify(event)});
      return channel;
    }
    async createOffer() { return {type:'offer',sdp:'synthetic-offer'}; }
    async setLocalDescription(offer) { this.localDescription = offer; }
    async setRemoteDescription() {
      voice.channel.readyState = 'open';
      queueMicrotask(() => voice.channel.onopen?.());
    }
    close() {}
  };
  const fetch = window.fetch;
  window.fetch = (url, options) => {
    if (url === 'https://api.openai.com/v1/realtime/calls') {
      voice.sdpExchanges++;
      return Promise.resolve(new Response('synthetic-answer', {status:200}));
    }
    return fetch(url, options);
  };
})()
"""


@pytest.fixture(scope="module")
def isolated_chrome():
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = os.getenv("INTAKE_CHROME_EXECUTABLE") or (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        if Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome").exists()
        else shutil.which("google-chrome") or shutil.which("google-chrome-stable")
    )
    if not chrome:
        pytest.skip("installed Chrome unavailable")
    with playwright.sync_playwright() as runner:
        # Playwright creates and cleans a fresh temporary profile. Reuse the
        # process to avoid repeated Chrome startup on memory-constrained Macs;
        # every test still gets a separate context and storage partition.
        browser = runner.chromium.launch(executable_path=chrome, headless=True, args=["--disable-gpu"])
        yield browser, playwright.expect
        browser.close()


@pytest.fixture
def console_page(isolated_chrome):
    browser, expect = isolated_chrome
    base = os.environ["INTAKE_CONSOLE_URL"].rstrip("/")
    parsed = urlsplit(base)
    assert parsed.scheme in {"http", "https"} and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
    with browser.new_context(viewport={"width": 1280, "height": 900}) as context:
        context.route("**/*", lambda route: route.continue_() if urlsplit(route.request.url).netloc == parsed.netloc else route.abort())
        page = context.new_page()
        page.add_init_script(_NATIVE_FIXTURE)
        page.goto(f"{base}/new", timeout=60_000, wait_until="domcontentloaded")
        expect(page.get_by_role("heading", name="What is your idea?")).to_be_visible(timeout=30_000)
        page.wait_for_function("window.__verifiedActors.includes('usr_fixture_a') && window.__requests.some(r => r.path === '/api/entry/chat/history')")
        page.wait_for_function("window.__hasNativeAuthListener()")
        # Hide only local Reticle QA instrumentation; click shipping controls
        # normally so an app overlay or disabled button still fails this test.
        page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
        yield page, expect


@pytest.fixture(params=[
    pytest.param({"idea": "Inventory replenishment for small factories", "audience": "Factory purchasing teams",
                  "prefilled": "Stock Loom", "chosen": "Parts Atlas", "website": "https://stock-loom.example",
                  "corrected_audience": "Warehouse purchasing teams"}, id="manufacturing"),
    pytest.param({"idea": "Weekend hiking plans for families", "audience": "Families planning day hikes",
                  "prefilled": "Trail Nest", "chosen": "Summit Club", "website": "https://trail-nest.example",
                  "corrected_audience": "Parents planning outdoor trips"}, id="outdoor-recreation"),
])
def name_case(request):
    return request.param


def _preview(page, expect, idea="Booking software for independent tutors", audience="Independent tutors"):
    page.get_by_role("textbox", name="What is your idea?", exact=True).fill(idea)
    page.get_by_role("button", name="Continue", exact=True).click()
    _audience(page, expect, audience)


def _audience(page, expect, audience_name="Independent tutors"):
    audience = page.get_by_role("textbox", name="Who is it for?")
    expect(audience).to_be_visible()
    audience.fill(audience_name)
    audience.press("Enter")
    expect(page.get_by_text("What should we call it?", exact=True)).to_be_visible()
    expect(page.get_by_role("textbox", name="Your venture name", exact=True)).to_be_visible()


def _requests(page, path):
    return [request for request in page.evaluate("window.__requests") if request["path"] == path]


def test_questionnaire_progress_focus_back_and_no_forced_skips(console_page):
    page, expect = console_page
    idea = page.get_by_role("textbox", name="What is your idea?")
    expect(page.get_by_text("Question 1 of 3", exact=True)).to_be_visible()
    expect(idea).to_be_focused()
    expect(page.get_by_role("textbox")).to_have_count(1)
    idea.fill("A booking tool")
    idea.press("Enter")
    audience = page.get_by_role("textbox", name="Who is it for?")
    expect(page.get_by_text("Question 2 of 3", exact=True)).to_be_visible()
    expect(audience).to_be_focused()
    expect(page.get_by_role("textbox")).to_have_count(1)
    audience.fill("Independent tutors")
    page.get_by_role("button", name="Back", exact=True).click()
    expect(idea).to_have_value("A booking tool")
    expect(idea).to_be_focused()
    idea.press("Enter")
    expect(audience).to_have_value("Independent tutors")
    audience.press("Enter")
    name = page.get_by_role("textbox", name="Your venture name", exact=True)
    expect(page.get_by_text("Question 3 of 3", exact=True)).to_be_visible()
    expect(name).to_be_focused()
    expect(page.get_by_role("textbox")).to_have_count(1)
    expect(page.get_by_role("button", name="Skip", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Not now", exact=True)).to_have_count(0)
    expect(page.get_by_role("button", name="Later", exact=True)).to_have_count(0)
    expect(page.get_by_text("What have you already started?", exact=True)).to_have_count(0)
    assert len(_requests(page, "/api/entry/chat/preview")) == 3
    name.fill("Tutor Harbor")
    name.press("Enter")
    expect(page.get_by_role("heading", name="Tutor Harbor", exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/confirm")) == 1
    assert not _requests(page, "/api/entry/chat/proposals/pr_fixture/name")


@pytest.mark.parametrize("state", ["pending", "committing", "clarify"])
def test_same_venture_direction_review_survives_reload(console_page, state):
    page, expect = console_page
    _preview(page, expect)
    page.get_by_role("textbox", name="Your venture name", exact=True).fill("Tutor Grove")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    page.wait_for_url("**/new?venture=ven_fixture")
    page.evaluate("state => { window.__fixture.rerouteState = state; }", state)
    composer = page.get_by_role("textbox", name="Message the team", exact=True)
    expect(composer).to_be_enabled()
    composer.fill("Change direction to a private tool for my own scheduling")
    page.get_by_role("button", name="Send", exact=True).click()
    card = page.get_by_role("article", name="Clarify new direction" if state == "clarify" else "Review new direction", exact=True)
    expect(card).to_be_visible()
    page.reload()
    expect(card).to_be_visible()
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/preview")) == 3
    if state == "clarify":
        card.get_by_role("button", name="Personal tool", exact=True).click()
        card = page.get_by_role("article", name="Review new direction", exact=True)
        expect(card).to_be_visible()
        preview_body = _requests(page, "/api/entry/chat/preview")[-1]["body"]
        assert preview_body["target_venture_id"] == "ven_fixture"
        assert preview_body["use_original"] is True
        assert preview_body["text"].endswith("Clarification: Personal tool")
    card.get_by_role("button", name="Check saved direction and continue" if state == "committing" else "Review build", exact=True).click()
    expect(page.get_by_text("Preparing your build", exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/confirm")) == 2
    assert _requests(page, "/api/entry/chat/confirm")[-1]["body"] == {"proposal_id": "pr_direction"}
    page.reload()
    expect(page.get_by_text("Preparing your build", exact=True)).to_be_visible()
    expect(page.get_by_role("article", name="Review new direction", exact=True)).to_have_count(0)


def test_restored_direction_cancel_and_account_change_do_not_reveal_old_review(console_page):
    page, expect = console_page
    _preview(page, expect)
    page.get_by_role("textbox", name="Your venture name", exact=True).fill("Tutor Grove")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    page.wait_for_url("**/new?venture=ven_fixture")
    composer = page.get_by_role("textbox", name="Message the team", exact=True)
    expect(composer).to_be_enabled()
    composer.fill("Change direction to a private tool for my own scheduling")
    page.get_by_role("button", name="Send", exact=True).click()
    card = page.get_by_role("article", name="Review new direction", exact=True)
    expect(card).to_be_visible()
    page.reload()
    expect(card).to_be_visible()
    card.get_by_role("button", name="Not right? Tell me", exact=True).click()
    expect(card).to_have_count(0)
    page.reload()
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    expect(card).to_have_count(0)
    assert len(_requests(page, "/api/entry/chat/confirm")) == 1
    composer.fill("Change direction to a private tool for my own scheduling")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(card).to_be_visible()
    page.reload()
    expect(card).to_be_visible()
    before = page.evaluate("window.__verifiedActors.length")
    page.evaluate("window.__changeActor('usr_fixture_b')")
    page.wait_for_function("before => window.__verifiedActors.slice(before).includes('usr_fixture_b')", arg=before)
    expect(card).to_have_count(0)
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_have_count(0)
    expect(page.get_by_text("Build a private scheduling tool.", exact=True)).to_have_count(0)


@pytest.mark.parametrize("leave", ["window-blur", "button-blur"])
def test_pending_mic_pointer_hold_cannot_start_after_blur(console_page, leave):
    page, expect = console_page
    page.evaluate("window.__enableVoice()")
    page.reload()
    mic = page.get_by_role("button", name="Tap to talk, or hold Space or the mic", exact=True)
    expect(mic).to_be_visible()
    # Synthetic capture never accesses hardware; a retired hold must not even
    # request this stream or call the native session-mint transport.
    page.evaluate("""() => {
      window.__micPermissionAttempts = 0;
      navigator.mediaDevices.getUserMedia = async () => {
        window.__micPermissionAttempts++;
        return new MediaStream();
      };
    }""")
    box = mic.bounding_box()
    assert box
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    page.mouse.down()
    if leave == "button-blur":
        page.get_by_role("textbox", name="What is your idea?").focus()
    else:
        page.evaluate("window.dispatchEvent(new Event('blur'))")
    page.wait_for_timeout(550)  # pass the actual 400ms pointer-hold timer
    assert page.evaluate("window.__micPermissionAttempts") == 0
    assert not _requests(page, "/api/entry/voice/session")
    expect(mic).to_have_attribute("aria-pressed", "false")
    page.mouse.up()


def test_mic_shared_tool_uses_owned_conversation_and_retires_late_account_result(console_page):
    page, expect = console_page
    page.evaluate(_VOICE_TOOL_FIXTURE)
    page.evaluate("window.__enableVoice()")
    _preview(page, expect)
    page.get_by_role("textbox", name="Your venture name", exact=True).fill("Tutor Grove")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_be_visible()
    page.wait_for_url("**/new?venture=ven_fixture")
    history_path = "/api/ventures/ven_fixture/chat?conversation_id=conv_fixture"
    page.wait_for_function("path => window.__requests.some(r => r.path === path)", arg=history_path)
    mic = page.get_by_role("button", name="Tap to talk, or hold Space or the mic", exact=True)
    expect(mic).to_be_visible()
    mic.click()  # real pointer controls, permission/session/SDP/configuration path
    expect(page.get_by_text("Listening…", exact=True)).to_be_visible()
    assert page.evaluate("[window.__voice.captures, window.__voice.sdpExchanges]") == [1, 1]
    session_request, = _requests(page, "/api/ventures/ven_fixture/voice/session")
    assert session_request["body"] == {"tool_capability": "shared_tools_v1"}
    history_before = len(_requests(page, history_path))
    arguments = " { } "  # preserve the provider's raw arguments, without coercion
    call = {"type": "response.function_call_arguments.done", "name": "suggest_next_step",
            "call_id": "call_voice_owned", "arguments": arguments}
    page.evaluate("""call => {
      window.__voice.emit({type:'conversation.item.input_audio_transcription.completed',
        item_id:'item_voice_owned',transcript:'What should I do next?'});
      window.__voice.emit({type:'response.created',response:{id:'resp_voice_owned'}});
      window.__voice.emit(call);
      window.__voice.emit({type:'response.done',response:{id:'resp_voice_owned',status:'completed'}});
    }""", call)
    tool_path = "/api/ventures/ven_fixture/voice/tools/suggest_next_step"
    page.wait_for_function("window.__voice.sent.filter(e => e.type === 'response.create').length === 2")
    expect(page.get_by_role("link", name="Review voice project", exact=True)).to_be_visible()
    assert len(_requests(page, history_path)) > history_before
    tool_request, = _requests(page, tool_path)
    assert tool_request["actor"] == "usr_fixture_a"
    assert tool_request["body"] == {"session_event_id": "ane_voice_fixture", "turn_id": "turn:1",
                                    "call_id": "call_voice_owned", "arguments": arguments,
                                    "conversation_id": "conv_fixture", "source": "voice"}
    outputs = page.evaluate("window.__voice.sent.filter(e => e.item?.type === 'function_call_output')")
    assert outputs == [{"type": "conversation.item.create", "item": {"type": "function_call_output",
                       "call_id": "call_voice_owned", "output": "Your project is ready for review.\n\n"
                       "Actions shown as cards (founder must click): Review voice project"}}]
    assert page.evaluate("window.__voice.sent.filter(e => e.type === 'response.create')") == [
        {"type": "response.create"}, {"type": "response.create"}]
    page.evaluate("call => window.__voice.emit(call)", call)
    assert len(_requests(page, tool_path)) == 1
    assert page.evaluate("window.__voice.sent.filter(e => e.type === 'response.create').length") == 2

    # A request admitted for A may finish after B verifies. Use the real native
    # auth listener/epoch reset; the old provider result must publish nothing.
    assert page.evaluate("window.__voice.channel.readyState") == "open"
    page.evaluate("""() => {
      window.__voice.emit({type:'response.done',response:{id:'resp_voice_final',status:'completed'}});
      window.__voice.emit({type:'conversation.item.input_audio_transcription.completed',
        item_id:'item_retired_owner',transcript:'Find another next step for my project'});
      window.__voice.emit({type:'response.function_call_arguments.done',name:'suggest_next_step',
        call_id:'call_retired_owner',arguments:'{}'});
    }""")
    page.wait_for_function("""typeof window.__finishVoiceTool === 'function' ||
      window.__voice.sent.some(e => e.item?.call_id === 'call_retired_owner')""")
    assert page.evaluate("typeof window.__finishVoiceTool === 'function'"), page.evaluate("window.__voice.sent")
    before = page.evaluate("window.__verifiedActors.length")
    page.evaluate("window.__changeActor('usr_fixture_b')")
    page.wait_for_function("before => window.__verifiedActors.slice(before).includes('usr_fixture_b')", arg=before)
    expect(page.get_by_role("heading", name="Tutor Grove", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Review voice project", exact=True)).to_have_count(0)
    assert page.evaluate("window.__voice.channel.readyState") == "closed"
    sent_before = page.evaluate("window.__voice.sent")
    owned_history_before = [r for r in _requests(page, history_path) if r["actor"] == "usr_fixture_a"]
    page.evaluate("""async () => {
      window.__finishVoiceTool();
      await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame);
    }""")
    assert page.evaluate("window.__voice.sent") == sent_before
    assert [r for r in _requests(page, history_path) if r["actor"] == "usr_fixture_a"] == owned_history_before
    expect(page.get_by_text("Former founder private result", exact=True)).to_have_count(0)
    expect(page.get_by_role("link", name="Former founder private action", exact=True)).to_have_count(0)
    assert not [r for r in page.evaluate("window.__requests") if r["method"] == "POST" and "/chat/actions" in r["path"]]


def test_name_required_and_manual_name_sent_on_continue(console_page, name_case):
    page, expect = console_page
    _preview(page, expect, name_case["idea"], name_case["audience"])
    name = page.get_by_role("textbox", name="Your venture name", exact=True)
    continuation = page.get_by_role("button", name="Continue", exact=True)
    expect(page.locator("textarea#entry-message")).to_have_count(1)
    expect(name).to_have_attribute("id", "entry-message")
    expect(name).to_have_attribute("maxlength", "48")
    expect(name).to_have_value("")
    expect(page.locator('button[data-usage-control="onboarding_name"]')).to_have_count(0)
    expect(page.get_by_role("button", name="Suggest a name", exact=True)).to_be_enabled()
    expect(continuation).to_be_disabled()
    name.fill("   ")
    expect(continuation).to_be_disabled()
    assert not _requests(page, "/api/entry/chat/confirm")
    name.fill(name_case["chosen"])
    expect(continuation).to_be_enabled()
    continuation.click()
    expect(page.get_by_role("heading", name=name_case["chosen"], exact=True)).to_be_visible()
    confirmation, = _requests(page, "/api/entry/chat/confirm")
    assert confirmation["body"] == {"proposal_id": "pr_fixture", "name": name_case["chosen"]}
    assert not _requests(page, "/api/entry/chat/proposals/pr_fixture/name")
    page.reload()
    expect(page.get_by_role("heading", name=name_case["chosen"], exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/confirm")) == 1


def test_website_name_summary_and_editable_defaults(console_page, name_case):
    page, expect = console_page
    page.evaluate("""example => {
      const invoke = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = async (command, args) => {
        const result = await invoke(command, args);
        if (command === 'desktop_api_request' && args.path === '/api/entry/chat/preview') {
          const proposal = window.__fixture.proposal;
          proposal.venture_name = example.prefilled;
          proposal.website_answers = {offer: example.idea, customer: example.audience};
          sessionStorage.setItem('intake-browser-fixture', JSON.stringify(window.__fixture));
          result.data.proposal = proposal;
        }
        return result;
      };
    }""", name_case)
    page.get_by_role("textbox", name="What is your idea?", exact=True).fill(name_case["website"])
    page.get_by_role("button", name="Continue", exact=True).click()
    name = page.get_by_role("textbox", name="Your venture name", exact=True)
    expect(page.get_by_text("What should we call it?", exact=True)).to_be_visible()
    expect(page.locator('button[data-usage-control="onboarding_name"]')).to_have_text(name_case["prefilled"])
    expect(name).to_have_value("")
    expect(page.get_by_role("button", name="Another name", exact=True)).to_be_enabled()
    expect(page.get_by_role("button", name="Continue", exact=True)).to_be_enabled()
    expect(page.get_by_text(name_case["idea"], exact=True)).to_be_visible()
    page.get_by_text("From your website, review or correct", exact=True).click()
    page.get_by_role("textbox", name="Who it's for, From your website", exact=True).fill(name_case["corrected_audience"])
    name.fill(name_case["chosen"])
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name=name_case["chosen"], exact=True)).to_be_visible()
    confirmation, = _requests(page, "/api/entry/chat/confirm")
    assert confirmation["body"]["name"] == name_case["chosen"]
    assert confirmation["body"]["website_answers"] == {"customer": name_case["corrected_audience"]}


def test_suggested_name_is_editable_and_saved_pending_name_survives_reload(console_page, name_case):
    page, expect = console_page
    page.evaluate("name => { window.__fixture.suggestedName = name; }", name_case["prefilled"])
    _preview(page, expect, name_case["idea"], name_case["audience"])
    page.get_by_role("button", name="Suggest a name", exact=True).click()
    name = page.get_by_role("textbox", name="Your venture name", exact=True)
    chip = page.locator('button[data-usage-control="onboarding_name"]')
    expect(chip).to_have_text(name_case["prefilled"])
    expect(name).to_have_value("")
    name.fill(name_case["chosen"])
    expect(name).to_have_value(name_case["chosen"])
    page.reload()
    expect(chip).to_have_text(name_case["prefilled"])
    expect(name).to_have_value(name_case["chosen"])
    page.add_style_tag(content="[data-reticle-overlay]{display:none!important}")
    page.get_by_role("button", name="Back", exact=True).click()
    audience = page.get_by_role("textbox", name="Who is it for?", exact=True)
    expect(audience).to_have_value(name_case["audience"])
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(chip).to_have_text(name_case["chosen"])
    expect(name).to_have_value("")
    page.get_by_role("button", name="Continue", exact=True).click()
    expect(page.get_by_role("heading", name=name_case["chosen"], exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/preview")) == 3
    assert len(_requests(page, "/api/entry/chat/proposals/pr_fixture/name")) == 1
    confirmation, = _requests(page, "/api/entry/chat/confirm")
    assert confirmation["body"]["name"] == name_case["chosen"]
    page.reload()
    expect(page.get_by_role("heading", name=name_case["chosen"], exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/confirm")) == 1


def test_prefilled_name_chip_confirms_once_and_survives_reload(console_page, name_case):
    page, expect = console_page
    page.evaluate("name => { window.__fixture.suggestedName = name; }", name_case["prefilled"])
    _preview(page, expect, name_case["idea"], name_case["audience"])
    page.get_by_role("button", name="Suggest a name", exact=True).click()
    chip = page.get_by_role("button", name=name_case["prefilled"], exact=True)
    expect(chip).to_have_attribute("data-usage-control", "onboarding_name")
    expect(page.get_by_role("textbox", name="Your venture name", exact=True)).to_have_value("")
    page.evaluate("""() => {
      const invoke = window.__TAURI__.core.invoke;
      window.__TAURI__.core.invoke = async (command, args) => {
        const result = await invoke(command, args);
        if (command === 'desktop_api_request' && args.path === '/api/entry/chat/confirm')
          return new Promise(resolve => { window.__finishNameConfirm = () => resolve(result); });
        return result;
      };
    }""")
    chip.click(click_count=2)
    page.wait_for_function("typeof window.__finishNameConfirm === 'function'")
    expect(chip).to_be_disabled()
    expect(page.locator('button[data-usage-control="onboarding_continue"]')).to_be_disabled()
    confirmation, = _requests(page, "/api/entry/chat/confirm")
    assert confirmation["body"] == {"proposal_id": "pr_fixture", "name": name_case["prefilled"]}
    page.evaluate("window.__finishNameConfirm()")
    expect(page.get_by_role("heading", name=name_case["prefilled"], exact=True)).to_be_visible()
    page.reload()
    expect(page.get_by_role("heading", name=name_case["prefilled"], exact=True)).to_be_visible()
    assert len(_requests(page, "/api/entry/chat/confirm")) == 1


def test_another_name_is_a_new_explicit_request(console_page):
    page, expect = console_page
    _preview(page, expect)
    page.get_by_role("button", name="Suggest a name", exact=True).click()
    expect(page.get_by_role("button", name="Another name", exact=True)).to_be_enabled()
    page.get_by_role("button", name="Another name", exact=True).click()
    expect(page.locator('button[data-usage-control="onboarding_name"]')).to_have_text("Slotwise")
    expect(page.get_by_role("textbox", name="Your venture name", exact=True)).to_have_value("")
    attempts = _requests(page, "/api/entry/chat/proposals/pr_fixture/name")
    assert len(attempts) == 2
    assert attempts[0]["body"]["request_id"] != attempts[1]["body"]["request_id"]


@pytest.mark.parametrize("status", [422, 0, 500], ids=["known-failure", "transport-uncertain", "server-uncertain"])
def test_name_retry_nonce_matches_failure_certainty(console_page, status):
    page, expect = console_page
    _preview(page, expect)
    page.evaluate("status => window.__setFailures([status])", status)
    suggestion = page.get_by_role("button", name="Suggest a name", exact=True)
    suggestion.click()
    expect(suggestion).to_be_enabled()
    expect(page.get_by_role("textbox", name="Your venture name", exact=True)).to_have_value("")
    expect(page.locator('button[data-usage-control="onboarding_name"]')).to_have_count(0)
    suggestion.click()
    expect(page.locator('button[data-usage-control="onboarding_name"]')).to_have_text("Slotwise")
    expect(page.get_by_role("textbox", name="Your venture name", exact=True)).to_have_value("")
    attempts = _requests(page, "/api/entry/chat/proposals/pr_fixture/name")
    assert len(attempts) == 2
    first, second = (attempt["body"]["request_id"] for attempt in attempts)
    assert first and second
    assert (first != second) is (status == 422)


@pytest.mark.parametrize("old_state", ["draft", "proposal", "receipt"])
def test_a_b_a_verified_session_events_clear_old_intake_state(console_page, old_state):
    page, expect = console_page
    marker = "Private founder A draft"
    if old_state == "draft":
        page.get_by_role("textbox", name="What is your idea?").fill(marker)
    else:
        _preview(page, expect)
        page.get_by_role("textbox", name="Your venture name", exact=True).fill(marker)
        if old_state == "receipt":
            page.get_by_role("button", name="Continue", exact=True).click()
            expect(page.get_by_role("heading", name=marker, exact=True)).to_be_visible()
    page.evaluate("window.__sameDocument = 'mounted-session-proof'")
    for actor in ["usr_fixture_b", "usr_fixture_a"]:
        before = page.evaluate("window.__verifiedActors.length")
        page.evaluate("actor => window.__changeActor(actor)", actor)
        page.wait_for_function("({actor,before}) => window.__verifiedActors.slice(before).includes(actor)", arg={"actor": actor, "before": before})
        expect(page.get_by_role("heading", name="What is your idea?")).to_be_visible()
        expect(page.get_by_role("textbox", name="What is your idea?")).to_have_value("")
        assert not page.get_by_role("textbox", name="Your venture name", exact=True).count()
        assert not page.get_by_role("heading", name=marker, exact=True).count()
        assert not page.get_by_text("Let us shape this booking idea.", exact=True).count()
        assert page.evaluate("window.__sameDocument") == "mounted-session-proof"
    assert page.evaluate("window.__verifiedActors.slice(-2)") == ["usr_fixture_b", "usr_fixture_a"]


@pytest.mark.parametrize("attachment", ["picker", "drop"])
def test_text_attachment_preserves_full_document_in_preview(console_page, attachment):
    page, expect = console_page
    document = "# Tutor booking brief\nAudience: independent tutors\n" + "Workflow detail\n" * 150 + "Pricing: $12 monthly"
    composer = page.get_by_role("textbox", name="What is your idea?")
    if attachment == "picker":
        attachment_button = page.get_by_role("button", name="Attach a .md or .txt file", exact=True)
        attachment_button.focus()
        expect(attachment_button).to_be_focused()
        with page.expect_file_chooser() as chooser:
            attachment_button.press("Enter")
        chooser.value.set_files({"name": "brief.md", "mimeType": "text/markdown", "buffer": document.encode()})
    else:
        composer.evaluate("""(el, text) => {
          const data = new DataTransfer();
          data.items.add(new File([text], 'brief.txt', {type:'text/plain'}));
          el.dispatchEvent(new DragEvent('drop', {bubbles:true,cancelable:true,dataTransfer:data}));
        }""", document)
    expect(composer).to_have_value(document)
    assert not _requests(page, "/api/entry/chat/preview")
    page.get_by_role("button", name="Continue", exact=True).click()
    _audience(page, expect)
    assert _requests(page, "/api/entry/chat/preview")[-1]["body"]["text"] == document + "\nAudience: Independent tutors"


def test_late_attachment_read_cannot_replace_a_new_composer_edit(console_page):
    page, expect = console_page
    page.evaluate("""() => {
      const read = File.prototype.text;
      File.prototype.text = function () {
        return this.name === 'slow.txt'
          ? new Promise(resolve => { window.__finishFileRead = resolve; })
          : read.call(this);
      };
    }""")
    page.locator('input[type="file"]').set_input_files({"name": "slow.txt", "mimeType": "text/plain", "buffer": b"Older attachment"})
    page.wait_for_function("typeof window.__finishFileRead === 'function'")
    composer = page.get_by_role("textbox", name="What is your idea?")
    composer.fill("Founder edit while the file was loading")
    page.evaluate("""async () => {
      window.__finishFileRead('Older attachment');
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
    }""")
    expect(composer).to_have_value("Founder edit while the file was loading")
    page.get_by_role("button", name="Continue", exact=True).click()
    _audience(page, expect)
    assert _requests(page, "/api/entry/chat/preview")[-1]["body"]["text"] == "Founder edit while the file was loading\nAudience: Independent tutors"


@pytest.mark.parametrize("path, title, key", [
    ("research", "Research your idea", "research.start"),
    ("personal_build", "Review a local build", "build.first"),
    ("repo_build", "Review a local build", "build.first"),
])
def test_focused_intake_overview_uses_actual_next_step_without_startup_rows(console_page, path, title, key):
    page, expect = console_page
    step = {"key": key, "stage": "Research" if path == "research" else "Build your project", "title": title,
            "detail": "Review the saved evidence and scope before continuing.", "cta_label": "Review next step",
            "chat_action": None, "payload": None, "href": f"/v/ven_fixture/{'research' if path == 'research' else 'idea'}"}
    page.evaluate("({path,step}) => window.__setOverview(path,step)", {"path": path, "step": step})
    base = os.environ["INTAKE_CONSOLE_URL"].rstrip("/")
    page.goto(f"{base}/v/ven_fixture", wait_until="domcontentloaded", timeout=60_000)
    expect(page.locator("#step-current").get_by_role("heading", name=title, exact=True)).to_be_visible(timeout=30_000)
    assert page.locator('section[id^="step-"]').count() == 1
    assert not page.get_by_role("heading", name="Your concept landing page", exact=True).count()
    assert not page.get_by_text("Done.", exact=True).count()


def test_canonical_gtm_overview_does_not_claim_page_steps_done_without_entry_goal(console_page):
    page, expect = console_page
    step = {"key": "gtm.outreach", "stage": "Find customers", "title": "Reach your first customers",
            "detail": "Review the audience and outreach drafts before anything is sent.", "cta_label": "Find customers",
            "chat_action": None, "payload": None, "href": "/gtm?venture=ven_fixture"}
    page.evaluate("step => window.__setOverview('gtm',step)", step)
    base = os.environ["INTAKE_CONSOLE_URL"].rstrip("/")
    page.goto(f"{base}/v/ven_fixture", wait_until="domcontentloaded", timeout=60_000)
    expect(page.locator("#step-outreach").get_by_role("heading", name=step["title"], exact=True)).to_be_visible(timeout=30_000)
    for stage in ["page", "publish", "waitlist"]:
        expect(page.locator(f"#step-{stage}")).to_have_attribute("data-status", "locked")
        assert not page.locator(f"#step-{stage}").get_by_text("Done.", exact=True).count()
