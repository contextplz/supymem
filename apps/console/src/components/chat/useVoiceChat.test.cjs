const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");
const ts = require("typescript");

function compiled(file, mocks = {}) {
  const filename = path.join(__dirname, file);
  const output = ts.transpileModule(readFileSync(filename, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  const loaded = new Module(filename, module);
  loaded.filename = filename; loaded.paths = Module._nodeModulePaths(path.dirname(filename));
  const native = loaded.require.bind(loaded);
  loaded.require = id => mocks[id] ?? native(id);
  loaded._compile(output, filename);
  return loaded.exports;
}
const voice = compiled("../../lib/voice.ts");
const captions = compiled("../../lib/voiceTranscript.ts");
const mascotMocks = { "@/lib/copy": compiled("../../lib/copy.ts"), "@/lib/charlieMascot": { setCharlieMascot() {} }, "@/lib/charlieGreeting": compiled("../../lib/charlieGreeting.ts") };

test('voice setup and session failures retain the exact saved-setting recovery across domains', async () => {
  const setup = 'Open Settings > Keys and connections > Your AI provider, save your OpenAI key and Realtime model, then try voice again. You can keep typing.';
  for (const audience of ['Warehouse dispatch teams', 'People planning garden beds']) {
    for (const entry of [true, false]) {
      for (const failure of ['setup', 'session', 'connection']) {
        const opened = await openVoiceByDefault({entry, screen: {step: audience},
          configApi: () => failure === 'connection' ? Promise.reject(new Error('offline')) :
            {ok: true, data: {voice_enabled: failure !== 'setup', message: setup}},
          sessionApi: () => ({ok: false, status: 502, data: {detail: {error: {code: 'voice_provider_error', message: setup}}}}),
        });
        try {
          if (failure === 'session') await opened.h.hook.toggle();
          const state = opened.h.rerender();
          assert.equal(state.phase, 'idle');
          assert.equal(state.enabled, failure === 'session');
          if (failure === 'connection') assert.match(state.note, /restart your app.*try voice again/i);
          else assert.equal(failure === 'session' ? state.error : state.note, setup);
          assert.equal(opened.h.micStarts(), failure === 'session' ? 1 : 0);
        } finally {opened.cleanup();}
      }
    }
  }
});

test('saving your voice settings refreshes availability without capturing audio or accepting an older probe across domains', async () => {
  for (const audience of ['Warehouse dispatch teams', 'People planning garden beds']) {
    let ready = false, complete;
    const opened = await openVoiceByDefault({screen: {step: audience}, configApi: () =>
      ready ? {ok: true, data: {voice_enabled: true}} : new Promise(resolve => {complete = resolve;})});
    try {
      assert.equal(opened.h.rerender().enabled, false);
      ready = true;
      opened.h.listeners.get('supymem-local-settings-changed')(); await flush();
      assert.equal(opened.h.rerender().enabled, true);
      complete({ok: true, data: {voice_enabled: false, message: 'Save your OpenAI key.'}}); await flush();
      assert.equal(opened.h.rerender().enabled, true);
      assert.equal(opened.h.rerender().note, '');
      assert.equal(opened.h.micStarts(), 0);
      assert.equal(opened.h.requests.some(url => url.endsWith('/voice/session')), false);
    } finally {opened.cleanup();}
  }
});

test('a lost start response retries the real Mac API with the same body key across domains', async () => {
  const client = await import('../../lib/client.ts');
  for (const audience of ['Freight operations teams', 'Families arranging neighborhood walks']) {
    const starts = [], minted = new Map();
    let lostResponses = 2, debits = 0;
    const options = { desktop: true, conversationId: `private-${audience}`, screen: {page: audience, step: 'settings'},
      sessionApi: (url, init) => client.api(url, init) };
    const call = harness(null, undefined, true, options);
    const connectedFetch = global.fetch;
    let lostConnectionResponse = true;
    global.fetch = async (...args) => {
      if (lostConnectionResponse) { lostConnectionResponse = false; throw new TypeError('Connection response lost'); }
      return connectedFetch(...args);
    };
    window.dispatchEvent = () => true;
    window.__TAURI_INTERNALS__ = { invoke: async (command, request) => {
      assert.equal(command, 'desktop_api_request');
      assert.equal(request.path, '/api/ventures/ven_test/voice/session');
      assert.deepEqual(request.headers, {}, 'Mac 0.1.108 must not receive a new bridge header');
      assert.equal(request.method, 'POST');
      const body = request.body;
      assert.match(body.idempotency_key, /^[0-9a-f-]{36}$/);
      starts.push(body);
      if (!minted.has(body.idempotency_key)) {
        debits++;
        minted.set(body.idempotency_key, {client_secret: `ephemeral-${debits}`, voice_telemetry_id: `session-${debits}`, instructions: 'Approval rules'});
      }
      if (lostResponses > 0) { lostResponses--; throw new Error('Response lost after minting'); }
      return {status: 200, data: minted.get(body.idempotency_key)};
    } };
    try {
      await call.hook.toggle();
      assert.match(call.rerender().error, /restart your app and try voice again/);
      assert.equal(debits, 1);
      options.screen = {page: audience, step: 'overview'};
      await call.rerender().retry();
      assert.match(call.rerender().error, /restart your app and try voice again/);
      await call.rerender().toggle();
      assert.match(call.rerender().error, /restart your app and try voice again/);
      await call.rerender().retry();
      assert.equal(call.rerender().error, '');
      assert.equal(debits, 1);
      assert.equal(starts.length, 4);
      for (const start of starts) assert.deepEqual(start, starts[0]);
      assert.equal(starts[1].conversation_id, options.conversationId);
      assert.equal(starts[1].screen.step, 'settings');
      call.hook.stop();
      await call.rerender().toggle();
      assert.notEqual(starts[4].idempotency_key, starts[0].idempotency_key);
      assert.equal(debits, 2);
    } finally { call.hook.stop(); global.fetch = connectedFetch; delete window.__TAURI_INTERNALS__; }
  }
});
const flush = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

test('voice greeting instructions and displayed streamed transcripts reject long dashes across domains', async () => {
  const server = readFileSync(path.join(__dirname, '../../../../../services/company_os/app/api/voice_api.py'), 'utf8');
  assert.match(server, /Never use em or en dashes/);
  assert.match(server, /VOICE_RULES =[\s\S]*?\+ TURN_RULES/);
  assert.match(server, /ENTRY_RULES =[\s\S]*?\+ TURN_RULES/);
  for (const domain of ['supplier audits', 'garden swaps']) {
    const h = harness();
    try {
      await h.hook.toggle();
      await h.message({type: 'session.updated', session: {audio: {input: {turn_detection: voice.VAD_ON}}}});
      const greeting = h.greetings[0];
      assert.match(greeting.response.instructions, /Never use em or en dashes/);
      await h.message({type: 'response.output_audio_transcript.delta', delta: `${domain}\u2014ready`});
      await h.message({type: 'response.output_audio_transcript.delta', delta: '\u2013today'});
      assert.doesNotMatch(h.rerender().live.map(turn => turn.text).join(' '), /[\u2013\u2014]/);
      await h.message({type: 'response.output_audio_transcript.done', transcript: `${domain}\u2014ready\u2013today`});
      assert.equal(h.rerender().live.at(-1).text, `${domain} - ready-today`);
      await h.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'founder', transcript: `${domain}\u2014next\u2013week`});
      assert.doesNotMatch(h.rerender().live.map(turn => turn.text).join(' '), /[\u2013\u2014]/);
    } finally {h.hook.stop(); await flush();}
  }
});

function fakeTimers() {
  const nativeSet = global.setTimeout, nativeClear = global.clearTimeout, timers = [];
  global.setTimeout = (run, ms) => { const timer = {run, ms, cleared: false}; timers.push(timer); return timer; };
  global.clearTimeout = timer => { if (timer) timer.cleared = true; };
  return {timers, restore() { global.setTimeout = nativeSet; global.clearTimeout = nativeClear; }};
}

test("chat wait returns at 12 seconds without cancelling or duplicating the admitted nonce", async () => {
  const clock = fakeTimers(), sent = []; let finish;
  const h = harness(null, (text, options) => {
    sent.push({text, options}); return new Promise(resolve => {finish = resolve;});
  });
  try {
    await h.hook.toggle();
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "slow", transcript: "Check the market"});
    await h.message({type: "response.function_call_arguments.done", call_id: "slow_write", name: "post_to_chat", arguments: '{"text":"Check it"}'});
    assert.equal(sent.length, 1);
    clock.timers.find(timer => timer.ms > 11000 && timer.ms <= voice.VOICE_CHAT_WAIT_MS && !timer.cleared).run(); await flush();
    const outputs = () => h.events.filter(event => event.item?.type === "function_call_output");
    assert.equal(outputs()[0].item.output, voice.VOICE_CHAT_WAIT_NOTE);
    assert.equal(h.rerender().note, voice.VOICE_CHAT_WAIT_NOTE);
    await h.message({type: "response.done", response: {id: "slow_response", status: "completed"}});
    assert.deepEqual(h.events.filter(event => event.type === "response.create").at(-1).response, voice.VOICE_CHAT_WAIT_RESPONSE);
    await h.message({type: "response.function_call_arguments.done", call_id: "second_write", name: "post_to_chat", arguments: '{"text":"Check it"}'});
    finish({body_md: "Market checked", admitted: true}); await flush();
    assert.equal(sent.length, 1);
    assert.equal(outputs().at(-1).item.output, "Market checked");
    h.hook.stop(); await flush();
    const summary = h.requestBodies.find(request => request.url.endsWith("/voice/summary")).body;
    assert.equal(summary.turns[0].posted, true);
    assert.equal(sent[0].options.idempotencyKey, `voice_ane_test_${summary.turns[0].id}`);
  } finally {h.hook.stop(); clock.restore();}
});

test('late answers are spoken on arrival, once, with facts and cards across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    const clock = fakeTimers(), sent = []; let finish;
    const h = harness(null, (text, options) => {sent.push({text, options}); return new Promise(resolve => {finish = resolve;});});
    try {
      await h.hook.toggle();
      await h.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'slow', transcript: `Check ${domain}`});
      await h.message({type: 'response.function_call_arguments.done', call_id: 'slow_call', name: 'supymem_chat', arguments: '{"text":"Check"}'});
      clock.timers.find(timer => timer.ms > 11000 && timer.ms <= voice.VOICE_CHAT_WAIT_MS && !timer.cleared).run(); await flush();
      await h.message({type: 'response.done', response: {id: 'request', status: 'completed'}});
      await h.message({type: 'response.created', response: {id: 'waiting'}});
      const reply = {body_md: `Estimated 17 responses for ${domain}, not verified users.`,
        citations: [{label: 'source.example', href: 'https://source.example/report'}],
        actions: [{label: 'Review', action: 'chat.reply', text: `Review ${domain}`}], admitted: true};
      finish(reply); await flush();
      await h.message({type: 'response.done', response: {id: 'waiting', status: 'completed'}});
      const late = h.events.filter(event => event.type === 'response.create' && event.response?.instructions?.startsWith('The result has arrived'));
      assert.equal(late.length, 1);
      assert.equal(late[0].response.instructions.includes(reply.body_md), true);
      assert.match(late[0].response.instructions, /source.example.*Review/);
      assert.deepEqual(late[0].response.tools, []);
      assert.equal(sent.length, 1);
      assert.equal(h.events.filter(event => event.item?.type === 'function_call_output').length, 1);
      assert.equal(h.rerender().note, '');
    } finally {h.hook.stop(); await flush(); clock.restore();}
  }
});

test('late answers never cross a newer founder turn or a changed owner across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    for (const changed of ['turn', 'owner']) {
      const clock = fakeTimers(); let finish;
      const h = harness(null, () => new Promise(resolve => {finish = resolve;}));
      try {
        await h.hook.toggle();
        await h.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'slow', transcript: `Check ${domain}`});
        await h.message({type: 'response.function_call_arguments.done', call_id: 'slow_call', name: 'post_to_chat', arguments: '{"text":"Check"}'});
        clock.timers.find(timer => timer.ms > 11000 && timer.ms <= voice.VOICE_CHAT_WAIT_MS && !timer.cleared).run(); await flush();
        if (changed === 'owner') h.owner.subject = 'usr_other';
        else await h.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'new', transcript: `Stop checking ${domain}`});
        finish({body_md: 'Old result', admitted: true}); await flush();
        assert.equal(h.events.some(event => event.response?.instructions?.startsWith('The result has arrived')), false);
      } finally {h.hook.stop(); await flush(); clock.restore();}
    }
  }
});

test('voice session mint selects the active private conversation across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    const h = harness(null, undefined, true, {conversationId: `private:${domain}`});
    try {
      await h.hook.toggle();
      const mint = h.requestBodies.find(request => request.url.endsWith('/voice/session'));
      assert.equal(mint.body.conversation_id, `private:${domain}`);
    } finally {h.hook.stop(); await flush();}
  }
});

test('a read-tool-selected substantive turn still uses the typed pipeline and complete founder words across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    const sent = [], reply = {body_md: `17 reported responses for ${domain}, not verified users.`,
      actions: [{label: 'Review', action: 'chat.reply', text: `Review ${domain}`}],
      citations: [{label: 'source.example', href: 'https://source.example/report'}]};
    const h = harness(null, async (text, options) => {sent.push({text, options}); return reply;});
    try {
      await h.hook.toggle();
      const words = `Find sources for ${domain}, but do not send anything yet.`;
      await h.message({type: 'conversation.item.input_audio_transcription.completed', item_id: 'founder', transcript: words});
      await h.message({type: 'response.function_call_arguments.done', call_id: 'read', name: 'web_search', arguments: '{"query":"shortened request"}'});
      assert.equal(sent.length, 1);
      assert.equal(sent[0].text, words);
      assert.equal(h.requests.some(url => url.includes('/voice/tools/')), false);
      const output = h.events.find(event => event.item?.type === 'function_call_output').item.output;
      assert.equal(output, voice.toolResultEvents('read', reply)[0].item.output);
    } finally {h.hook.stop(); await flush();}
  }
});

test("real partial barge-in clears playback immediately but final negation alone owns writes", async () => {
  const written = [], clock = fakeTimers(); const h = harness(null, async text => {written.push(text); return {body_md: "Done"};});
  try {
    await h.hook.toggle();
    await h.message({type: "response.created", response: {id: "talking"}});
    await h.message({type: "output_audio_buffer.started"}); await h.remote(); h.playbackTick();
    await h.message({type: "input_audio_buffer.speech_started", item_id: "correction"});
    await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "correction", delta: "Change the price"});
    assert.equal(h.events.filter(event => event.type === "response.cancel").length, 1);
    assert.equal(h.events.filter(event => event.type === "output_audio_buffer.clear").length, 1);
    h.playbackTick(); assert.notEqual(h.rerender().phase, "speaking");
    assert.deepEqual(written, []);
    await h.message({type: "response.function_call_arguments.done", call_id: "premature", name: "post_to_chat", arguments: '{"text":"Change the price"}'});
    assert.deepEqual(written, []);
    await h.message({type: "input_audio_buffer.speech_stopped", item_id: "correction"});
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "correction", transcript: "Don't change the price"});
    assert.equal(h.events.filter(event => event.type === "response.cancel").length, 1);
    await h.message({type: "response.function_call_arguments.done", call_id: "final_write", name: "post_to_chat", arguments: '{"text":"Change the price"}'});
    assert.deepEqual(written, ["Don't change the price"]);
  } finally {h.hook.stop(); await flush(); clock.restore();}
});

test("one real word needs 600ms of active speech; prolonged pure filler never interrupts", async () => {
  const clock = fakeTimers(), nativeNow = Date.now; let now = 1000; Date.now = () => now;
  const h = harness();
  try {
    await h.hook.toggle();
    await h.message({type: "response.created", response: {id: "talking"}});
    await h.message({type: "output_audio_buffer.started"});
    for (const [item, words] of [["noise", "um uh"], ["word", "wait"]]) {
      await h.message({type: "input_audio_buffer.speech_started", item_id: item});
      await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: item, delta: words});
      assert.equal(h.events.filter(event => event.type === "response.cancel").length, 0);
      now += 600; clock.timers.filter(timer => timer.ms === 600 && !timer.cleared).at(-1).run();
      if (item === "noise") {
        assert.equal(h.events.filter(event => event.type === "response.cancel").length, 0);
        await h.message({type: "input_audio_buffer.speech_stopped", item_id: item});
        await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: item, transcript: words});
      }
    }
    assert.equal(h.events.filter(event => event.type === "response.cancel").length, 1);
  } finally {h.hook.stop(); await flush(); Date.now = nativeNow; clock.restore();}
});

test("push-to-talk interrupts pending Charlie audio instantly without a speech timer", async () => {
  const h = harness(); await h.hook.toggle();
  await h.message({type: "output_audio_buffer.started"}); await h.hook.pttStart();
  assert.equal(h.events.filter(event => event.type === "output_audio_buffer.clear").length, 1);
  assert.equal(h.events.filter(event => event.type === "response.cancel").length, 0);
  h.hook.stop();
});

test("committed partial words still interrupt before final without counting transcription delay as speech", async () => {
  const clock = fakeTimers(), nativeNow = Date.now; let now = 1000; Date.now = () => now;
  const h = harness();
  try {
    await h.hook.toggle(); await h.message({type: "response.created", response: {id: "talking"}});
    await h.message({type: "output_audio_buffer.started"});
    await h.message({type: "input_audio_buffer.speech_started", item_id: "short"});
    now += 200; await h.message({type: "input_audio_buffer.speech_stopped", item_id: "short"});
    now += 1000; await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "short", delta: "wait"});
    assert.equal(h.events.filter(event => event.type === "response.cancel").length, 0);
    await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "short", delta: " please"});
    assert.equal(h.events.filter(event => event.type === "response.cancel").length, 1);
    assert.equal(h.events.filter(event => event.type === "response.create").length, 0);
  } finally {h.hook.stop(); await flush(); Date.now = nativeNow; clock.restore();}
});

test("idle reply warns aloud and visibly at 20 seconds and ends only after 30 seconds", async () => {
  const h = harness(), nativeNow = Date.now; let now = 1000; Date.now = () => now;
  try {
    await h.hook.toggle();
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "question", transcript: "What should I do next?"});
    await h.message({type: "response.done", response: {id: "answer", status: "completed"}});
    now = 11000; h.tick(); assert.notEqual(h.rerender().phase, "idle");
    now = 21000; h.tick(); assert.equal(h.rerender().note, voice.AUTO_END_WARNING);
    assert.deepEqual(h.events.filter(event => event.type === "response.create").at(-1).response, voice.AUTO_END_WARNING_RESPONSE);
    await h.message({type: "response.done", response: {id: "warning", status: "completed"}});
    now = 30999; h.tick(); assert.notEqual(h.rerender().phase, "idle");
    now = 31000; h.tick(); assert.equal(h.rerender().phase, "idle");
    assert.equal(h.rerender().recovery, "ended");
  } finally {h.hook.stop(); Date.now = nativeNow;}
});

test("acknowledgements remain normal turns and speech resets the idle warning", async () => {
  const h = harness(), nativeNow = Date.now; let now = 1000; Date.now = () => now;
  try {
    await h.hook.toggle();
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "question", transcript: "What should I do next?"});
    await h.message({type: "response.done", response: {id: "answer"}});
    now = 21000; h.tick();
    await h.message({type: "response.done", response: {id: "warning"}});
    for (const [item, text] of [["thanks", "thank you"], ["goodbye", "bye"]]) {
      await h.message({type: "input_audio_buffer.speech_started", item_id: item});
      assert.equal(h.rerender().note, "");
      await h.message({type: "input_audio_buffer.speech_stopped", item_id: item});
      await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: item, transcript: text});
      assert.ok(h.rerender().live.some(turn => turn.text === text && turn.final));
      assert.equal(h.events.some(event => event.type === "conversation.item.delete" && event.item_id === item), false);
      await h.message({type: "response.done", response: {id: item}});
    }
    now = 31000; h.tick(); assert.notEqual(h.rerender().phase, "idle");
  } finally {h.hook.stop(); await flush(); Date.now = nativeNow;}
});

test("hallucinated acknowledgements during Charlie audio never interrupt or authorize chat writes", async () => {
  const h = harness(null, async () => assert.fail("Noise cannot authorize chat"));
  try {
    await h.hook.toggle();
    await h.message({type: "response.created", response: {id: "talking"}});
    await h.message({type: "output_audio_buffer.started"});
    for (const phrase of ["Thank you.", "you", "bye", "thanks for watching", "thanks", "ok bye"]) {
      await h.message({type: "input_audio_buffer.speech_started", item_id: phrase});
      await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: phrase, delta: phrase});
      await h.message({type: "input_audio_buffer.speech_stopped", item_id: phrase});
      await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: phrase, transcript: phrase});
      assert.equal(h.rerender().live.some(turn => turn.text === phrase && turn.final), false);
      assert.ok(h.events.some(event => event.type === "conversation.item.delete" && event.item_id === phrase));
    }
    assert.equal(h.events.some(event => event.type === "response.cancel" || event.type === "output_audio_buffer.clear" || event.type === "response.create"), false);
    await h.message({type: "response.function_call_arguments.done", call_id: "noise_write", name: "post_to_chat", arguments: '{"text":"Change the price"}'});
    assert.match(h.events.find(event => event.item?.type === "function_call_output").item.output, /repeat your answer or type/);
  } finally {h.hook.stop(); await flush();}
});

test("quiet acknowledgements wait for final transcript instead of rejecting their own early response", async () => {
  const h = harness();
  try {
    await h.hook.toggle();
    await h.message({type: "input_audio_buffer.speech_started", item_id: "thanks"});
    await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "thanks", delta: "Thank you."});
    await h.message({type: "input_audio_buffer.speech_stopped", item_id: "thanks"});
    await h.message({type: "input_audio_buffer.committed", item_id: "thanks"});
    assert.equal(h.events.filter(event => event.type === "response.create").length, 0);
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "thanks", transcript: "Thank you."});
    assert.equal(h.rerender().live[0].final, true);
    assert.equal(h.events.filter(event => event.type === "response.create").length, 1);
  } finally {h.hook.stop(); await flush();}
});

test("desktop focus changes keep mic and playback running in the same session", async () => {
  const h = harness(null, undefined, true, {desktop: true}); const cleanups = h.effects.map(effect => effect());
  try {
    await h.hook.toggle(); const peer = h.peers[0];
    await h.message({type: "response.created", response: {id: "answer"}});
    await h.message({type: "output_audio_buffer.started"}); await h.remote(); h.playbackTick();
    assert.equal(h.listeners.has("blur"), false);
    assert.equal(h.listeners.has("visibilitychange"), false);
    document.hidden = true; document.hasFocus = () => false;
    h.playbackTick(); assert.equal(h.rerender().phase, "speaking");
    assert.equal(h.rerender().recovery, ""); assert.equal(h.rerender().note, "");
    assert.equal(h.track.enabled, true); assert.equal(h.track.stopped, false); assert.equal(peer.closed, undefined);
    assert.equal(h.element.volume, 1);
    assert.equal(h.events.some(event => event.type === "response.cancel" || event.type === "output_audio_buffer.clear"), false);
    assert.equal(h.requests.filter(url => url.endsWith("/voice/session")).length, 1);
  } finally {cleanups.forEach(cleanup => cleanup?.());}
});

test("disconnected shows Reconnecting then recovers in place or offers Resume on drop", async () => {
  const clock = fakeTimers(), h = harness();
  try {
    await h.hook.toggle(); const peer = h.peers[0];
    peer.connectionState = "disconnected"; peer.onconnectionstatechange();
    assert.equal(h.rerender().recovery, "reconnecting"); assert.match(h.rerender().note, /Reconnecting…/);
    assert.equal(h.track.enabled, false);
    const grace = clock.timers.find(timer => timer.ms === 8000);
    peer.connectionState = "connected"; peer.onconnectionstatechange();
    assert.equal(grace.cleared, true); assert.equal(h.track.enabled, true); assert.equal(h.rerender().recovery, "");
    peer.connectionState = "disconnected"; peer.onconnectionstatechange();
    clock.timers.filter(timer => timer.ms === 8000 && !timer.cleared).at(-1).run();
    assert.equal(h.rerender().phase, "idle"); assert.equal(h.rerender().note, "Call dropped - Resume");
    await h.hook.resume(); assert.equal(h.requests.filter(url => url.endsWith("/voice/session")).length, 2);
    assert.equal(h.rerender().recovery, "");
    h.peers.at(-1).channel.onclose(); assert.equal(h.rerender().recovery, "dropped");
    assert.match(h.rerender().error, /Resume or type/);
  } finally {h.hook.stop(); clock.restore();}
});

test("failed response.done leaves Retry, never successful idle completion or another business request", async () => {
  const h = harness(null, async () => assert.fail("Retry must not send chat")), nativeNow = Date.now;
  try {
    await h.hook.toggle();
    await h.message({type: "response.created", response: {id: "failed"}});
    await h.message({type: "response.done", response: {id: "failed", status: "failed", status_details: {error: {message: "PRIVATE_ERROR"}}}});
    assert.match(h.rerender().error, /Retry or type/); assert.doesNotMatch(h.rerender().error, /PRIVATE/);
    Date.now = () => nativeNow() + 31000; h.tick(); assert.notEqual(h.rerender().phase, "idle");
    await h.hook.retry();
    assert.equal(h.rerender().error, "");
    const response = h.events.filter(event => event.type === "response.create").at(-1).response;
    assert.deepEqual(response.tools, []); assert.equal(response.tool_choice, "none");
  } finally {h.hook.stop(); Date.now = nativeNow;}
});

test("ending a call or choosing typing clears a failed speaking notice", async () => {
  for (const action of ["stop", "typeInstead"]) {
    const h = harness();
    try {
      await h.hook.toggle();
      await h.message({type: "response.created", response: {id: "failed"}});
      await h.message({type: "response.done", response: {id: "failed", status: "failed"}});
      assert.match(h.rerender().error, /couldn't finish speaking/);
      h.hook[action]();
      assert.equal(h.rerender().error, "");
      assert.equal(h.rerender().phase, "idle");
    } finally { h.hook.stop(); }
  }
});

test("failed transcription shows a retry hint without promoting partial words or creating an answer", async () => {
  const h = harness(); await h.hook.toggle();
  await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "bad", delta: "Change the price"});
  await h.message({type: "conversation.item.input_audio_transcription.failed", item_id: "bad"});
  assert.match(h.rerender().error, /Retry.*saying it again.*type/);
  assert.equal(h.events.filter(event => event.type === "response.create").length, 0);
  assert.equal(h.rerender().live[0].final, false); h.hook.stop(); await flush();
});

test("filler audio does not consume first-answer turn latency and uses only accepted telemetry labels", async () => {
  const clock = fakeTimers(), nativeNow = Date.now; let now = 1000, finish; Date.now = () => now;
  const h = harness(null, () => new Promise(resolve => {finish = resolve;}));
  try {
    await h.hook.toggle();
    await h.message({type: "input_audio_buffer.speech_started", item_id: "question"});
    await h.message({type: "input_audio_buffer.speech_stopped", item_id: "question"});
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "question", transcript: "Check the market"});
    await h.message({type: "response.function_call_arguments.done", call_id: "read", name: "web_search", arguments: '{"query":"market"}'});
    now = 1500; clock.timers.find(timer => timer.ms === 500 && !timer.cleared).run();
    await h.message({type: "response.done", response: {id: "question"}});
    await h.message({type: "output_audio_buffer.started"}); await h.remote(); h.playbackTick();
    assert.equal(h.metrics.filter(metric => metric.metric === "turn_latency").length, 0);
    assert.equal(h.metrics.find(metric => metric.metric === "first_remote_audio_ms").duration_ms, 500);
    await h.message({type: "output_audio_buffer.stopped"}); h.amplitude(0); now = 1900; h.playbackTick();
    await h.message({type: "response.done", response: {id: "filler"}});
    finish({body_md: "Market checked"}); await flush();
    now = 3000; await h.message({type: "output_audio_buffer.started"}); h.amplitude(20); h.playbackTick();
    assert.equal(h.metrics.find(metric => metric.metric === "turn_latency").duration_ms, 2000);
    assert.equal(h.metrics.filter(metric => metric.metric === "turn_latency").length, 1);
  } finally {h.hook.stop(); await flush(); Date.now = nativeNow; clock.restore();}
});

test("delayed filler playback keeps its response identity after an answer was queued", async () => {
  let finish;
  const clock = fakeTimers(), h = harness(null, () => new Promise(resolve => {finish = resolve;}));
  try {
    await h.hook.toggle();
    await h.message({type: "input_audio_buffer.speech_started", item_id: "question"});
    await h.message({type: "input_audio_buffer.speech_stopped", item_id: "question"});
    await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "question", transcript: "Check the market"});
    await h.message({type: "response.created", response: {id: "question"}});
    await h.message({type: "response.function_call_arguments.done", call_id: "write", name: "post_to_chat", arguments: '{"text":"Check it"}'});
    clock.timers.find(timer => timer.ms === 500 && !timer.cleared).run();
    await h.message({type: "response.done", response: {id: "question"}});
    await h.message({type: "response.created", response: {id: "filler"}});
    finish({body_md: "Checked"}); await flush();
    await h.message({type: "response.done", response: {id: "filler"}});
    await h.message({type: "response.created", response: {id: "answer"}});
    await h.message({type: "output_audio_buffer.started", response_id: "filler"});
    await h.remote(); h.playbackTick();
    assert.equal(h.metrics.filter(metric => metric.metric === "turn_latency").length, 0);
    await h.message({type: "output_audio_buffer.stopped"});
    h.audioEvent("waiting"); h.amplitude(0); h.playbackTick();
    await h.message({type: "output_audio_buffer.started", response_id: "answer"});
    await h.hook.enableSound(); h.amplitude(20); h.playbackTick();
    assert.equal(h.metrics.filter(metric => metric.metric === "turn_latency").length, 1);
  } finally {h.hook.stop(); await flush(); clock.restore();}
});

async function openVoiceByDefault(options = {}, permission = 'granted', stored = null) {
  const values = options.values ?? new Map(stored ? [['supymem.charlie.inputMode.v1', stored]] : []);
  options.values = values;
  const h = harness(options.getUserMedia, undefined, true, options);
  let permissionQueries = 0;
  navigator.permissions = {query: async () => {permissionQueries++; return {state: permission};}};
  const cleanups = h.effects.map(effect => effect());
  await flush();
  const start = h.effects.length; h.rerender();
  cleanups.push(...h.effects.slice(start).map(effect => effect()));
  for (let index = 0; index < 5; index++) await flush();
  assert.equal(permissionQueries, 0);
  return {h, values, cleanup: () => cleanups.forEach(run => run?.())};
}

for (const stored of [null, 'voice', 'typing']) {
  test(`mount and reload never capture audio, even with granted permission and preference=${stored}`, async () => {
    const first = await openVoiceByDefault({}, 'granted', stored);
    for (const opened of [first, await openVoiceByDefault({values: first.values}, 'granted')]) {
      assert.equal(opened.h.rerender().phase, 'idle');
      assert.equal(opened.h.rerender().voiceFirst, stored !== 'typing');
      assert.equal(opened.h.micStarts(), 0);
      assert.equal(opened.h.requests.some(url => /\/voice\/(session|summary)$/.test(url)), false);
      assert.equal(opened.h.peers.length, 0);
      opened.cleanup();
    }
  });
}

test('unavailable voice keeps typing available; microphone permission is requested only on tap', async () => {
  for (const options of [{configUnavailable: true}, {sessionUnavailable: true}, {
    getUserMedia: async () => {throw Object.assign(new Error('denied'), {name: 'NotAllowedError'});},
  }]) {
    const {h, cleanup} = await openVoiceByDefault(options, 'denied');
    assert.equal(h.micStarts(), 0);
    if (!options.configUnavailable) await h.hook.toggle();
    const state = h.rerender();
    assert.equal(state.phase, 'idle');
    if (options.configUnavailable) assert.match(state.note, /Keys and connections > Your AI provider.*save your key/);
    else {
      assert.ok(state.error);
      assert.equal(h.reported.length, 1);
      assert.equal(h.reported[0].source, options.sessionUnavailable ? 'voice.session_mint' : 'voice.mic');
      assert.ok(h.reported[0].message);
    }
    state.typeInstead();
    assert.equal(h.rerender().voiceFirst, false);
    cleanup();
  }
});

test('tap starts a live session; typing exit remembers presentation, not listening consent', async () => {
  const {h, values, cleanup} = await openVoiceByDefault({}, 'granted', 'typing');
  const ui = micButton(h);
  window.localStorage = {getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)};
  ui.button.props.onPointerDown({currentTarget: {setPointerCapture() {}}, pointerId: 1});
  ui.button.props.onPointerUp();
  await flush(); await flush();
  assert.equal(h.rerender().phase, 'listening');
  assert.equal(h.micStarts(), 1);
  assert.equal(h.requests.filter(url => url.endsWith('/voice/session')).length, 1);
  assert.equal(values.get('supymem.charlie.inputMode.v1'), 'voice');
  h.rerender().typeInstead();
  assert.equal(h.rerender().phase, 'idle');
  assert.equal(values.get('supymem.charlie.inputMode.v1'), 'typing');
  ui.cleanup(); cleanup();
});

test('hold Space explicitly starts push-to-talk; releasing it stops audio, not the presentation', async () => {
  const {h, cleanup} = await openVoiceByDefault();
  const ui = micButton(h), previousElement = global.Element;
  global.Element = class {closest() {return null;}};
  let prevented = 0;
  try {
    const event = {code: 'Space', key: ' ', repeat: false, target: new Element(), preventDefault() {prevented++;}};
    ui.listeners.get('keydown')(event);
    await flush(); await flush();
    assert.equal(h.micStarts(), 1);
    assert.equal(h.rerender().pushToTalk, true);
    assert.equal(h.track.enabled, true);
    ui.listeners.get('keyup')(event);
    assert.equal(h.track.enabled, false);
    assert.equal(prevented, 2);
    h.hook.toggle();
    assert.equal(h.rerender().voiceFirst, false);
    assert.equal(h.rerender().phase, 'idle');
  } finally {global.Element = previousElement; ui.cleanup(); cleanup();}
});

test('background, unfocused and inactive controls cannot mint; retry requires a new gesture', async () => {
  for (const blocked of [{active: false}, {focused: false}, {hidden: true}]) {
    const options = {...blocked}, {h, cleanup} = await openVoiceByDefault(options);
    document.hidden = !!blocked.hidden;
    await h.hook.pttStart();
    assert.equal(h.micStarts(), 0);
    options.active = options.focused = true; document.hidden = false;
    await h.rerender().pttStart();
    assert.equal(h.micStarts(), 1);
    cleanup();
  }
});

for (const event of ['blur', 'background', 'pagehide']) {
  test(`live sessions ${event === 'pagehide' ? 'end' : 'continue'} on ${event} without automatic new sessions`, async () => {
    const options = {active: true};
    const {h, values, cleanup} = await openVoiceByDefault(options);
    await h.hook.toggle();
    const peer = h.peers[0];
    if (event === 'background') {document.hidden = true; h.listeners.get('visibilitychange')?.();}
    else h.listeners.get(event)?.();
    const ended = event === 'pagehide';
    assert.equal(h.track.stopped, ended);
    if (!ended) assert.equal(h.track.enabled, true);
    assert.equal(peer.closed, ended ? true : undefined);
    assert.equal(peer.channel.readyState, ended ? 'closed' : 'open');
    assert.equal(h.rerender().phase, ended ? 'idle' : 'listening');
    assert.equal(h.rerender().recovery, ended ? 'ended' : '');
    if (ended) assert.match(h.rerender().note, /page was closed.*Resume or type/);
    else assert.equal(h.rerender().note, '');
    assert.equal(values.get('supymem.charlie.inputMode.v1'), 'voice');
    document.hidden = false;
    h.listeners.get('visibilitychange')?.();
    h.listeners.get('focus')?.();
    if (ended) {peer.connectionState = 'failed'; peer.onconnectionstatechange();}
    for (const active of [false, true]) {
      options.active = active;
      const start = h.effects.length; h.rerender();
      h.effects.slice(start).find(effect => effect.toString().includes('options.active === false'))();
    }
    await flush();
    assert.equal(h.micStarts(), 1);
    assert.equal(h.requests.filter(url => url.endsWith('/voice/session')).length, 1);
    cleanup();
  });

  test(`${event} during a permission prompt ${event === 'pagehide' ? 'retires' : 'preserves'} the grant`, async () => {
    let grant;
    const {h, cleanup} = await openVoiceByDefault({getUserMedia: stream => new Promise(resolve => {grant = () => resolve(stream);})});
    const pending = h.hook.toggle();
    if (event === 'background') {document.hidden = true; h.listeners.get('visibilitychange')?.();}
    else h.listeners.get(event)?.();
    grant(); await pending;
    assert.equal(h.track.stopped, event === 'pagehide');
    assert.equal(h.rerender().phase, event === 'pagehide' ? 'idle' : 'listening');
    assert.equal(h.requests.some(url => url.endsWith('/voice/session')), event !== 'pagehide');
    cleanup();
  });
}

async function commitInputs(h) {
  await h.message({type: "input_audio_buffer.committed", item_id: "item_A", previous_item_id: null});
  await h.message({type: "input_audio_buffer.committed", item_id: "item_B", previous_item_id: "item_A"});
}

function harness(getUserMedia, onVoiceSend = async () => ({ body_md: "Done" }), acknowledge = true, options = {}) {
  const events = [], greetings = [], metrics = [], requests = [], requestBodies = [], peers = [], ticks = [], reported = [];
  global.setInterval = run => { const tick = { run, cleared: false }; ticks.push(tick); return tick; };
  global.clearInterval = tick => { if (tick) tick.cleared = true; };
  const owner = { subject: "usr_test", epoch: 1 };
  const track = { enabled: true, stopped: false, stop() { this.stopped = true; } };
  const stream = { getTracks: () => [track], getAudioTracks: () => [track] };
  const effects = [], cells = [], listeners = new Map(); let cursor = 0, micStarts = 0;
  const values = options.values ?? new Map();
  global.window = {addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {}, localStorage: {
    getItem: key => values.get(key) ?? null, setItem: (key, value) => values.set(key, value)}};
  const cell = value => { const at = cursor++; return cells[at] ??= value; };
  const react = { useRef: current => cell({ current }), useState: value => {
    const at = cursor++; cells[at] ??= typeof value === 'function' ? value() : value;
    return [cells[at], next => { cells[at] = typeof next === "function" ? next(cells[at]) : next; }];
  },
    useCallback: value => value, useEffect: callback => effects.push(callback) };
  Object.defineProperty(global, "navigator", { configurable: true, value: { mediaDevices: {
    getUserMedia: () => {micStarts++; return getUserMedia ? getUserMedia(stream) : Promise.resolve(stream);},
  } } });
  const audioEvents = new Map(); let amplitude = 0;
  const element = { autoplay: false, volume: 1, srcObject: null, setAttribute() {}, remove() {}, pause() {},
    addEventListener: (name, fn) => audioEvents.set(name, fn), removeEventListener: name => audioEvents.delete(name),
    play: async () => { if (options.blocked) throw Object.assign(new Error("blocked"), {name: "NotAllowedError"}); audioEvents.get("playing")?.(); } };
  global.document = { hidden: false, hasFocus: () => options.focused !== false,
    addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {},
    body: {appendChild() {}}, createElement: () => element };
  global.AudioContext = class { createAnalyser() { return { fftSize: 512, getByteTimeDomainData: data => data.fill(128 + amplitude) }; }
    createMediaStreamSource() { return { connect() {} }; } close() { return Promise.resolve(); } resume() { return Promise.resolve(); } };
  global.RTCPeerConnection = class {
    constructor() { peers.push(this); this.connectionState = "new"; }
    addTrack() {} createOffer() { return Promise.resolve({ type: "offer", sdp: "offer" }); }
    setLocalDescription(value) { this.localDescription = value; return Promise.resolve(); }
    setRemoteDescription() { this.channel.readyState = "open"; this.channel.onopen(); return Promise.resolve(); }
    createDataChannel() { this.channel = { readyState: "connecting", send: raw => {
      const event = JSON.parse(raw);
      if (event.type === "response.create" && event.response?.instructions?.startsWith("Greet the founder first")) {
        greetings.push(event);
        const id = `greeting_${greetings.length}`;
        Promise.resolve().then(() => {
          this.channel.onmessage({data: JSON.stringify({type: "response.created", response: {id}})});
          this.channel.onmessage({data: JSON.stringify({type: "response.done", response: {id, status: "completed"}})});
        });
      } else events.push(event);
      if (acknowledge && event.type === "session.update") Promise.resolve().then(() => this.channel.onmessage({ data: JSON.stringify({ type: "session.updated", session: event.session }) }));
    },
      close() { this.readyState = "closed"; } }; return this.channel; }
    close() { this.closed = true; }
  };
  global.fetch = async () => ({ ok: true, text: async () => "answer" });
  const api = async (url, init) => {
    requests.push(url);
    requestBodies.push({ url, body: init?.body ? JSON.parse(init.body) : null });
    if (url.endsWith("telemetry")) { metrics.push(JSON.parse(init.body)); return { ok: true }; }
    if (url.endsWith("/voice/config")) return options.configApi?.() ?? {ok: true, data: {voice_enabled: !options.configUnavailable}};
    if (url.endsWith("/voice/session") && options.sessionUnavailable) return {ok: false, status: 503};
    if (url.endsWith("/voice/session") && options.sessionApi) return options.sessionApi(url, init);
    if (url.endsWith("/voice/summary")) {
      const body=JSON.parse(init.body);
      return options.summaryApi?.(url,body) ?? {ok:true,data:{pending:false,needs_edit:false,id:"entry-voice-note_"+"a".repeat(64),
        body_md:"From your voice call:\n\n"+body.turns.map(turn=>(turn.role==="founder"?"You: ":"I: ")+(turn.final?"":"Unconfirmed speech, edit before sending: ")+turn.text).join("\n\n")+"\n\nThis is your voice transcript, not evidence of completed work. Review any prepared action before continuing."}};
    }
    if (url.includes("/voice/tools/")) return options.toolApi?.(url, JSON.parse(init.body)) ?? { ok: true, data: { body_md: "A saved result", actions: [], partial: false } };
    return { ok: true, data: { client_secret: "ephemeral-test", voice_telemetry_id: "ane_test", instructions: "System approval rules" } };
  };
  const { useVoiceChat, useVoiceKeys, voiceCallActive, claimVoiceCall, releaseVoiceCall } = compiled("useVoiceChat.ts", { react, "@/lib/client": { api, verifiedSessionSubject: () => owner.subject, verifiedSessionEpoch: () => owner.epoch },
    "@/lib/userError": { reportUserError(error) { reported.push(error); } }, "@/lib/usageControl": { announceUsageControl() {} },
    "@/lib/voicePreference": compiled("../../lib/voicePreference.ts"),
    "@/lib/voice": voice, "@/lib/voiceTranscript": captions, ...mascotMocks,
    "@/lib/desktop": { isDesktopShell: () => options.desktop === true },
    "@/lib/charlieMascot": {setCharlieMascot: (state, level) => options.mascot?.(state, level)} });
  const hook = useVoiceChat(options.entry ? null : "ven_test", onVoiceSend, options);
  const message = async (event, peer = peers.at(-1)) => { peer.channel.onmessage({ data: JSON.stringify(event) }); await flush(); };
  return { hook, track, element, events, greetings, metrics, requests, requestBodies, peers, message, owner, ticks, reported, react, effects, useVoiceKeys, listeners, micStarts: () => micStarts,
    voiceCallActive, claimVoiceCall, releaseVoiceCall,
    rerender: () => { cursor = 0; return useVoiceChat(options.entry ? null : "ven_test", onVoiceSend, options); },
    tick: () => { const t = ticks.at(-1); if (t && !t.cleared) t.run(); },
    remote: async (level = 20) => { amplitude = level; peers.at(-1).ontrack({streams: [stream]}); await flush(); },
    playbackTick: () => { const t = ticks.at(-2); if (t && !t.cleared) t.run(); },
    audioEvent: name => audioEvents.get(name)?.(), amplitude: value => { amplitude = value; } };
}

function micButton(h) {
  const listeners = new Map();
  global.window = { addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {} };
  Object.assign(global.document, { hidden: false, addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {} });
  const firstEffect = h.effects.length;
  const { VoiceMic } = compiled("VoiceMic.tsx", {
    react: h.react, "@/lib/voice": voice,
    "./useVoiceChat": { useVoiceChat: () => ({ ...h.hook, enabled: true }), useVoiceKeys: h.useVoiceKeys, voiceCallActive: h.voiceCallActive },
    "./VoiceMic.module.css": { default: {} }, "./VoiceOrb": { VoiceOrb: "voice-orb" }, "@/components/primitives/ErrorNotice": { ErrorNotice: () => null },
    "@/lib/desktop": { isDesktopShell: () => false },
  });
  const tree = VoiceMic({ ventureId: "ven_test", onVoiceSend: async () => null });
  const cleanups = h.effects.slice(firstEffect).map(effect => effect());
  return { button: tree.props.children[0], listeners, cleanup: () => cleanups.forEach(run => run?.()) };
}

test("only one live voice call app-wide: a second panel cannot start another call over it", async () => {
  for (const domain of ["supplier audits", "garden swaps"]) {
    const h = harness();
    try {
      const otherPanel = {};
      await h.hook.toggle();
      assert.equal(h.voiceCallActive(), true, domain);
      assert.equal(h.claimVoiceCall(otherPanel), false, "a second panel must not open a parallel call");
      assert.equal(h.requests.filter(url => url.endsWith("/voice/session")).length, 1);
      h.hook.stop(); await flush();
      assert.equal(h.voiceCallActive(), false, "ending the call frees it");
      assert.equal(h.claimVoiceCall(otherPanel), true);
      assert.equal(await h.rerender().toggle().then(() => h.requests.filter(url => url.endsWith("/voice/session")).length), 1,
        "this panel cannot start while another owns the call");
      h.releaseVoiceCall(otherPanel);
    } finally { h.hook.stop(); await flush(); }
  }
});

test("server streaming is not speaking; only local playing plus amplitude counts", async () => {
  const mascot = [];
  const h = harness(null, undefined, true, {mascot: (state, level) => mascot.push({state, level})}); await h.hook.toggle();
  await h.message({type: "input_audio_buffer.speech_started"});
  assert.equal(mascot.at(-1).state, "listening");
  await h.message({type: "input_audio_buffer.speech_stopped"});
  await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "question", transcript: "Hello Charlie"});
  await h.message({type: "response.created", response: {id: "resp_audio"}});
  await h.message({type: "output_audio_buffer.started"});
  assert.notEqual(h.rerender().phase, "speaking");
  assert.equal(mascot.some(value => value.state === "speaking"), false);
  await h.remote(0); h.playbackTick(); assert.notEqual(h.rerender().phase, "speaking");
  h.amplitude(20); h.playbackTick(); assert.equal(h.rerender().phase, "speaking");
  assert.equal(mascot.at(-1).state, "speaking");
  assert.ok(mascot.at(-1).level > 0 && mascot.at(-1).level <= 1);
  h.audioEvent("waiting"); assert.notEqual(h.rerender().phase, "speaking");
  for (const name of ["mic_permission_ms", "mint_ms", "sdp_ms", "first_remote_audio_ms", "local_playing_ms"]) {
    assert.ok(h.metrics.some(value => value.metric === name && Number.isInteger(value.duration_ms)));
  }
  h.hook.stop();
  assert.equal(mascot.at(-1).state, "idle");
});

test("blocked audio surfaces Enable sound state and gesture retries playback", async () => {
  const mascot = [];
  const options = {blocked: true, mascot: (state, level) => mascot.push({state, level})}; const h = harness(null, undefined, true, options);
  await h.hook.toggle();
  await h.message({type: "response.created", response: {id: "resp_blocked"}});
  await h.message({type: "output_audio_buffer.started"});
  await h.remote(); h.playbackTick();
  assert.equal(h.rerender().playbackBlocked, true); assert.notEqual(h.rerender().phase, "speaking");
  assert.equal(mascot.some(value => value.state === "speaking"), false);
  assert.equal(h.metrics.filter(value => value.metric === "playback_blocked").length, 1);
  options.blocked = false; await h.hook.enableSound(); h.playbackTick();
  assert.equal(h.rerender().playbackBlocked, false); assert.equal(h.rerender().phase, "speaking"); h.hook.stop();
});

test("committed real partial starts response before final but writes still wait for final transcript", async () => {
  const written = []; const h = harness(null, async text => {written.push(text); return {body_md: "Done"};});
  await h.hook.toggle();
  await h.message({type: "input_audio_buffer.speech_started", item_id: "early"});
  await h.message({type: "conversation.item.input_audio_transcription.delta", item_id: "early", delta: "Change the price"});
  await h.message({type: "input_audio_buffer.speech_stopped", item_id: "early"});
  await h.message({type: "input_audio_buffer.committed", item_id: "early"});
  assert.equal(h.events.filter(event => event.type === "response.create").length, 1);
  await h.message({type: "response.function_call_arguments.done", call_id: "early_write", name: "supymem_chat", arguments: JSON.stringify({text: "Change the price"})});
  assert.deepEqual(written, []);
  await h.message({type: "conversation.item.input_audio_transcription.completed", item_id: "early", transcript: "Don't change the price"});
  assert.deepEqual(written, ["Don't change the price"]);
  assert.equal(h.events.filter(event => event.type === "response.create").length, 1);
  h.hook.stop();
});

test("mic permission guidance has a deadline and a late grant cannot mint or retain tracks", async () => {
  const setTimeoutBefore = global.setTimeout, clearTimeoutBefore = global.clearTimeout;
  const timers = [];
  global.setTimeout = (run, ms) => {const timer = {run, ms, cleared: false}; timers.push(timer); return timer;};
  global.clearTimeout = timer => {if (timer) timer.cleared = true;};
  let release;
  const h = harness(stream => new Promise(resolve => {release = () => resolve(stream);}));
  try {
    const connecting = h.hook.toggle(); await flush();
    timers.find(timer => timer.ms === 3000).run();
    assert.match(h.rerender().note, /Allow microphone access/);
    timers.find(timer => timer.ms === 20000).run(); await connecting;
    assert.equal(h.rerender().phase, "idle"); assert.match(h.rerender().error, /Allow microphone access/);
    release(); await flush();
    assert.equal(h.track.stopped, true);
    assert.equal(h.requests.some(url => url.endsWith("/voice/session")), false);
  } finally {h.hook.stop(); global.setTimeout = setTimeoutBefore; global.clearTimeout = clearTimeoutBefore;}
});

for (const leave of ["blur", "hidden", "button blur"]) {
  test(`pending mic pointer hold cannot mint after ${leave}`, async () => {
    let permissions = 0;
    const h = harness(async stream => { permissions++; return stream; });
    const ui = micButton(h);
    const nativeTimer = global.setTimeout, timers = [];
    global.setTimeout = run => { const timer = { run, cleared: false }; timers.push(timer); return timer; };
    const nativeClear = global.clearTimeout;
    global.clearTimeout = timer => { if (timer) timer.cleared = true; };
    try {
      ui.button.props.onPointerDown({ currentTarget: { setPointerCapture() {} }, pointerId: 1 });
      if (leave === "button blur") ui.button.props.onBlur();
      else if (leave === "hidden") { document.hidden = true; ui.listeners.get("visibilitychange")?.(); }
      else { document.hasFocus = () => false; ui.listeners.get("blur")?.(); }
      for (const timer of timers) if (!timer.cleared) timer.run();
      await flush();
      assert.equal(permissions, 0);
      assert.equal(h.requests.some(url => url.endsWith("session")), false);
    } finally { ui.cleanup(); h.hook.stop(); global.setTimeout = nativeTimer; global.clearTimeout = nativeClear; }
  });
}

test("replacement pointer timer cannot open a hold and explicit release closes a live hold", async () => {
  const h = harness(), ui = micButton(h);
  const nativeTimer = global.setTimeout, nativeClear = global.clearTimeout, timers = [];
  global.setTimeout = run => { const timer = { run, cleared: false }; timers.push(timer); return timer; };
  global.clearTimeout = timer => { if (timer) timer.cleared = true; };
  try {
    const down = pointerId => ui.button.props.onPointerDown({ currentTarget: { setPointerCapture() {} }, pointerId });
    down(1); down(2);
    timers[0].run(); // a retired callback already queued by the browser must also be harmless
    await flush();
    assert.equal(h.requests.some(url => url.endsWith("session")), false);
    timers[1].run(); await flush(); await flush();
    assert.equal(h.track.enabled, true);
    assert.equal(h.requests.filter(url => url.endsWith("session")).length, 1);
    ui.listeners.get("blur")?.();
    assert.equal(h.track.enabled, true);
    ui.button.props.onPointerUp();
    assert.equal(h.track.enabled, false);
    assert.equal(h.track.stopped, false); // releasing a hold keeps the same session
  } finally { ui.cleanup(); h.hook.stop(); global.setTimeout = nativeTimer; global.clearTimeout = nativeClear; }
});

test("denied microphone never mints a paid session; stop retires late permission results", async () => {
  const denied = harness(async () => { throw Object.assign(new Error(), { name: "NotAllowedError" }); });
  await denied.hook.toggle();
  assert.equal(denied.requests.some(url => url.endsWith("session")), false);
  assert.equal(denied.reported.length, 1); // clearing the old error reports nothing
  assert.equal(denied.reported[0].source, "voice.mic");
  assert.equal(denied.reported[0].kind, "voice");
  let release;
  const delayed = harness(stream => new Promise(resolve => { release = () => resolve(stream); }));
  const connecting = delayed.hook.toggle();
  delayed.hook.stop(); release(); await connecting;
  assert.equal(delayed.track.stopped, true);
  assert.equal(delayed.peers.length, 0);
  assert.equal(delayed.requests.length, 0);
});

test("hold-to-talk mutes on release and waits for one transcript before replying", async () => {
  const h = harness();
  await h.hook.pttStart();
  assert.equal(h.track.enabled, true);
  assert.equal(h.events.find(e => e.type === "session.update").session.audio.input.turn_detection, null);
  // Provider commits need at least 100 ms of audio; advance just this hold's clock.
  const now = Date.now;
  Date.now = () => now() + 250;
  h.hook.pttEnd(); Date.now = now;
  assert.equal(h.track.enabled, false);
  assert.equal(h.events.filter(e => e.type === "input_audio_buffer.commit").length, 1);
  assert.equal(h.events.filter(e => e.type === "response.create").length, 0);
  const transcript = { type: "conversation.item.input_audio_transcription.completed", item_id: "item_1", transcript: "What about pricing?" };
  await h.message(transcript); await h.message(transcript);
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  assert.equal(h.track.enabled, false);
  assert.equal(h.metrics.filter(m => m.metric === "duplicate_response_blocked").length, 1);
  assert.equal(h.events.some(e => e.type === "session.update" && e.session.audio?.input.turn_detection), false);
  h.hook.stop();
});

test("release while connecting cannot reopen the microphone later", async () => {
  let release;
  const h = harness(stream => new Promise(resolve => { release = () => resolve(stream); }));
  const connecting = h.hook.pttStart(); h.hook.pttEnd(); release(); await connecting;
  assert.equal(h.track.enabled, false);
  assert.equal(h.events.some(e => e.type === "input_audio_buffer.commit"), false);
  h.hook.stop();
});

test("noise and failed transcription do not restart speech; real interruption replies once", async () => {
  const h = harness(); await h.hook.toggle();
  await h.message({ type: "response.created", response: { id: "resp_1" } });
  await h.message({ type: "output_audio_buffer.started" });
  for (const [item_id, extra] of [["noise", { type: "conversation.item.input_audio_transcription.completed", transcript: "um" }],
    ["failed", { type: "conversation.item.input_audio_transcription.failed" }]]) {
    await h.message({ type: "input_audio_buffer.speech_started" });
    await h.message({ type: "input_audio_buffer.speech_stopped" });
    await h.message({ item_id, ...extra });
  }
  assert.equal(h.events.some(e => e.type === "response.cancel" || e.type === "response.create"), false);
  assert.equal(h.metrics.filter(m => m.metric === "barge_in_ignored").length, 2);
  assert.equal(h.metrics.filter(m => m.metric === "transcription_failed").length, 1);
  await h.message({ type: "input_audio_buffer.speech_started" });
  await h.message({ type: "input_audio_buffer.speech_stopped" });
  await h.message({ type: "conversation.item.input_audio_transcription.completed", item_id: "real", transcript: "Wait, change the price" });
  const done = { type: "response.done", response: { id: "resp_1", status: "cancelled" } };
  await h.message(done); await h.message(done);
  assert.equal(h.events.filter(e => e.type === "response.cancel").length, 1);
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  await h.message({ type: "output_audio_buffer.started" });
  await h.remote(); h.playbackTick();
  assert.equal(h.metrics.filter(m => m.metric === "turn_latency").length, 1);
  assert.equal(h.metrics.filter(m => m.metric === "barge_in_accepted").length, 1);
  assert.equal(h.metrics.filter(m => m.metric === "response_cancelled").length, 1);
  assert.ok(h.metrics.every(m => !JSON.stringify(m).includes("change the price")));
  h.hook.stop();
});

test("replayed tool calls run once and a stopped tool cannot send into a new session", async () => {
  let complete, calls = 0;
  const h = harness(null, async () => { calls++; return new Promise(resolve => { complete = resolve; }); });
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"request",transcript:"Make the plan"});
  const tool = { type: "response.function_call_arguments.done", call_id: "call_1", name: "supymem_chat", arguments: '{"text":"Make the plan"}' };
  await h.message(tool); await h.message(tool);
  assert.equal(calls, 1);
  h.hook.stop(); await h.hook.toggle();
  complete({ body_md: "Old reply" }); await flush();
  assert.equal(h.events.some(e => e.item?.type === "function_call_output"), false);
  h.hook.stop();
});

test("shared tools round-trip through authenticated API once and all outputs precede one continuation", async () => {
  const pending = [], bodies = [];
  let refreshed = 0;
  const h = harness(null, async () => assert.fail("shared reads must not become business requests"), true, {
    conversationId: "cnv_private", onToolsComplete: () => refreshed++,
    toolApi: (url, body) => { bodies.push(body); return new Promise(resolve => pending.push(resolve)); },
  });
  await h.hook.toggle();
  const startKey = h.requestBodies.find(request => request.url.endsWith("/voice/session")).body.idempotency_key;
  assert.match(startKey, /^[0-9a-f-]{36}$/);
  assert.deepEqual(h.requestBodies.find(r => r.url.endsWith("/voice/session")), {
    url: "/api/ventures/ven_test/voice/session", body: { tool_capability: "shared_tools_v1", conversation_id: "cnv_private", idempotency_key: startKey },
  });
  await h.message({ type: "response.created", response: { id: "resp_tools" } });
  const tools = [{ type: "response.function_call_arguments.done", call_id: "read", name: "read_url", arguments: '{"url":"https://example.com"}' },
    { type: "response.function_call_arguments.done", call_id: "next", name: "suggest_next_step", arguments: "{}" }];
  await h.message(tools[0]);
  pending[0]({ ok: true, data: { body_md: "Untrusted reference data" } }); await flush();
  // A fast first call queues a continuation before the provider announces its slower second call.
  await h.message(tools[1]); await h.message(tools[0]);
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0].arguments, tools[0].arguments);
  assert.equal(bodies[0].conversation_id, "cnv_private");
  assert.equal(bodies[0].session_event_id, "ane_test");
  assert.equal(bodies[0].source, "voice");
  await h.message({ type: "response.done", response: { id: "resp_tools" } });
  assert.equal(h.events.filter(e => e.type === "response.create").length, 0);
  pending[1]({ ok: true, data: { body_md: "Attached a button", actions: [{ label: "Review" }] } }); await flush();
  assert.equal(h.events.filter(e => e.item?.type === "function_call_output").length, 2);
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  assert.equal(refreshed, 1);
  await h.message({ type: "response.done", response: { id: "resp_answer" } });
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  h.hook.stop();
});

test("one tools-free filler speaks while waiting and partial timeout gets one tools-free answer", async () => {
  let complete;
  const h = harness(null, undefined, true, { toolApi: () => new Promise(resolve => { complete = resolve; }) });
  await h.hook.toggle();
  const nativeTimer = global.setTimeout, nativeClear = global.clearTimeout, timers = [];
  global.setTimeout = (run, ms) => { const timer = { run, ms, cleared: false }; timers.push(timer); return timer; };
  global.clearTimeout = timer => { if (timer) timer.cleared = true; };
  try {
    await h.message({ type: "response.function_call_arguments.done", call_id: "slow", name: "web_search", arguments: '{"query":"market"}' });
    timers.find(t => t.ms === 500).run();
    const filler = h.events.find(e => e.type === "response.create");
    assert.equal(filler.response.conversation, "none");
    assert.equal(filler.response.tool_choice, "none");
    assert.deepEqual(filler.response.tools, []);
    complete({ ok: true, data: { body_md: voice.VOICE_TOOL_PARTIAL, partial: true } }); await flush();
    const done = { type: "response.done", response: { id: "resp_filler" } };
    await h.message(done); await h.message(done);
    const responses = h.events.filter(e => e.type === "response.create");
    assert.equal(responses.length, 2);
    assert.equal(responses[1].response.tool_choice, "none");
    assert.deepEqual(responses[1].response.tools, []);
    assert.match(h.events.find(e => e.item?.type === "function_call_output").item.output, /could not finish/);
  } finally { h.hook.stop(); global.setTimeout = nativeTimer; global.clearTimeout = nativeClear; }
});

test("shared tools cap each turn and entry voice never gets venture tools", async () => {
  const h = harness(); await h.hook.toggle();
  await h.message({ type: "response.created", response: { id: "resp_cap" } });
  for (let i = 0; i < 5; i++) await h.message({ type: "response.function_call_arguments.done", call_id: `call_${i}`, name: "venture_context", arguments: '{"section":"research"}' });
  assert.equal(h.requests.filter(url => url.includes("/voice/tools/")).length, 4);
  await h.message({ type: "response.done", response: { id: "resp_cap" } });
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  assert.equal(h.events.find(e => e.type === "response.create").response.tool_choice, "none");
  h.hook.stop();
  const entry = harness(null, undefined, true, { entry: true }); await entry.hook.toggle();
  assert.deepEqual(entry.requestBodies.find(r => r.url.endsWith("/voice/session")), {
    url: "/api/entry/voice/session", body: {},
  });
  await entry.message({ type: "response.function_call_arguments.done", call_id: "forbidden", name: "venture_context", arguments: '{"section":"credits"}' });
  assert.equal(entry.requests.some(url => url.includes("/voice/tools/")), false);
  assert.match(entry.events.find(e => e.item?.type === "function_call_output").item.output, /could not finish/);
  entry.hook.stop();
});

test("a stalled native tool request reaches the wall deadline and speaks a partial answer", async () => {
  let complete;
  const h = harness(null, undefined, true, { toolApi: () => new Promise(resolve => { complete = resolve; }) });
  await h.hook.toggle();
  const nativeTimer = global.setTimeout, nativeClear = global.clearTimeout, timers = [];
  global.setTimeout = (run, ms) => { const timer = { run, ms, cleared: false }; timers.push(timer); return timer; };
  global.clearTimeout = timer => { if (timer) timer.cleared = true; };
  try {
    await h.message({ type: "response.function_call_arguments.done", call_id: "stalled", name: "read_url", arguments: '{"url":"https://example.com"}' });
    const timeout = timers.find(t => t.ms > 500);
    assert.ok(timeout.ms <= 20_000);
    timeout.run(); await flush();
    assert.match(h.events.find(e => e.item?.type === "function_call_output").item.output, /could not finish/);
    assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
    assert.equal(h.events.find(e => e.type === "response.create").response.tool_choice, "none");
    complete({ ok: true, data: { body_md: "Late unavailable data" } }); await flush();
    assert.equal(h.events.filter(e => e.item?.type === "function_call_output").length, 1);
  } finally { h.hook.stop(); global.setTimeout = nativeTimer; global.clearTimeout = nativeClear; }
});

test("caption-gated chat and shared reads share the same per-turn step ceiling", async () => {
  let businessCalls = 0;
  const h = harness(null, async () => { businessCalls++; return { body_md: "Saved chat reply" }; });
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"request",transcript:"What is next?"});
  h.events.length = 0; // Measure only the tool continuations after the caption-owned response.
  await h.message({ type: "response.created", response: { id: "resp_mixed" } });
  for (let i = 0; i < 5; i++) await h.message({ type: "response.function_call_arguments.done", call_id: `mixed_${i}`,
    name: i % 2 ? "supymem_chat" : "venture_context", arguments: i % 2 ? '{"text":"What is next?"}' : '{"section":"research"}' });
  assert.equal(businessCalls, 1); // Both write-tool calls share the same confirmed founder turn.
  assert.equal(h.requests.filter(url => url.includes("/voice/tools/")).length, 1);
  await h.message({ type: "response.done", response: { id: "resp_mixed" } });
  assert.equal(h.events.filter(e => e.type === "response.create").length, 1);
  h.hook.stop();
});

test("an account change retires direct shared-tool outputs and their card refresh", async () => {
  let complete, refreshed = 0;
  const h = harness(null, undefined, true, { onToolsComplete: () => refreshed++, toolApi: () => new Promise(resolve => { complete = resolve; }) });
  await h.hook.toggle();
  await h.message({ type: "response.function_call_arguments.done", call_id: "private", name: "venture_context", arguments: '{"section":"research"}' });
  h.owner.subject = "usr_other"; h.owner.epoch++;
  complete({ ok: true, data: { body_md: "Private old account result", actions: [{ label: "Private card" }] } }); await flush();
  assert.equal(h.events.some(e => e.item?.type === "function_call_output"), false);
  assert.equal(refreshed, 0);
  h.hook.stop();
});

test("a new accepted turn retires an older tool reply without starting another response", async () => {
  let complete;
  const h = harness(null, async () => new Promise(resolve => { complete = resolve; }));
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"request",transcript:"Old request"});
  h.events.length = 0; // The initial response was requested by this confirmed caption.
  await h.message({ type: "response.created", response: { id: "resp_tool" } });
  await h.message({ type: "response.function_call_arguments.done", call_id: "old_tool", name: "supymem_chat", arguments: '{"text":"Old request"}' });
  await h.message({ type: "response.done", response: { id: "resp_tool", status: "completed" } });
  await h.message({ type: "conversation.item.input_audio_transcription.failed", item_id: "cough_during_tool" });
  await h.message({ type: "conversation.item.input_audio_transcription.completed", item_id: "filler_during_tool", transcript: "um" });
  assert.equal(h.events.some(e => e.type === "response.create"), false);
  await h.message({ type: "conversation.item.input_audio_transcription.completed", item_id: "new_turn", transcript: "Wait, do a different task" });
  const responses = h.events.filter(e => e.type === "response.create").length;
  complete({ body_md: "Stale private reply" }); await flush();
  assert.equal(responses, 1);
  assert.equal(h.events.filter(e => e.type === "response.create").length, responses);
  const output = h.events.find(e => e.item?.type === "function_call_output");
  assert.ok(output.item.output.includes("latest request"));
  assert.equal(output.item.output.includes("Stale private reply"), false);
  h.hook.stop();
});


test("retired account callbacks cannot use the new account handler or complete an old tool", async () => {
  let calls = 0, complete;
  const h = harness(null, async () => { calls++; return new Promise(resolve => { complete = resolve; }); });
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"request",transcript:"Old request"});
  const tool = { type: "response.function_call_arguments.done", call_id: "old_tool", name: "supymem_chat", arguments: '{"text":"Old request"}' };
  await h.message(tool);
  h.owner.subject = "usr_other"; h.owner.epoch++;
  await h.message({ ...tool, call_id: "retired_tool" });
  complete({ body_md: "Private old result" }); await flush();
  assert.equal(calls, 1);
  assert.equal(h.events.some(e => e.item?.type === "function_call_output"), false);
  // A to B to A cannot resurrect the old call.
  h.owner.subject = "usr_test"; h.owner.epoch++;
  await h.message({ ...tool, call_id: "resurrected_tool" });
  assert.equal(calls, 1);
  h.hook.stop();
});


test("microphone stays muted until the server acknowledges manual responses", async () => {
  const h = harness(null, undefined, false);
  await h.hook.toggle();
  assert.equal(h.track.enabled, false);
  await h.message({ type: "session.updated", session: { audio: { input: { turn_detection: { create_response: true, interrupt_response: false } } } } });
  assert.equal(h.track.enabled, false);
  await h.message({ type: "session.updated", session: { audio: { input: { turn_detection: { create_response: false, interrupt_response: false } } } } });
  assert.equal(h.track.enabled, true);
  h.hook.stop();
});

test("exactly one greeting per call, only after readiness and never on reconnect or Resume", async () => {
  for (const options of [{}, {entry: true}, {hold: true}]) {
    const h = harness(null, undefined, false, options);
    const ready = {type: "session.updated", session: {audio: {input: {turn_detection: options.hold ? null : voice.VAD_ON}}}};
    try {
      await (options.hold ? h.hook.pttStart() : h.hook.toggle());
      assert.equal(h.greetings.length, 0);
      await h.message({type: "session.created", session: ready.session});
      assert.equal(h.greetings.length, 0);
      await h.message(ready); await h.message(ready);
      assert.equal(h.greetings.length, 1);
      assert.deepEqual(h.greetings[0].response.tools, []);
      const peer = h.peers[0];
      peer.channel.onopen();
      peer.connectionState = "disconnected"; peer.onconnectionstatechange();
      peer.connectionState = "connected"; peer.onconnectionstatechange();
      await h.message(ready); await h.hook.resume();
      assert.equal(h.greetings.length, 1);
      peer.channel.onclose(); await h.hook.resume(); await h.message(ready);
      assert.equal(h.greetings.length, 1);
      h.hook.stop(); await h.hook.toggle(); await h.message(ready);
      assert.equal(h.greetings.length, 2);
    } finally {h.hook.stop();}
  }
});


test("the renewal threshold ends audio without purchasing unused credentials across domains", async () => {
  const nativeNow = Date.now;
  let now = 1000;
  Date.now = () => now;
  try {
    for (const domain of ["Warehouse inventory planning", "Family music lessons"]) {
      now = 1000;
      let starts = 0;
      const call = harness(null, undefined, true, {conversationId: `private:${domain}`, sessionApi: async () => {
        starts++;
        return {ok: true, data: {client_secret: `secret_${starts}`, voice_telemetry_id: `ane_${starts}`}};
      }});
      try {
        await call.hook.pttStart();
        assert.equal(starts, 1);
        now += voice.RENEW_SESSION_MS;
        call.tick(); call.tick(); await flush();
        assert.equal(starts, 1);
        assert.equal(call.track.stopped, true);
        assert.equal(call.rerender().recovery, "ended");
        assert.match(call.rerender().note, /Start a new call or type instead/);
        assert.equal(call.requests.filter(url => url.endsWith("/voice/session")).length, 1);
        await call.hook.pttStart();
        assert.equal(starts, 2);
        const mint = call.requestBodies.filter(request => request.url.endsWith("/voice/session")).at(-1);
        assert.equal(mint.body.conversation_id, `private:${domain}`);
      } finally {call.hook.stop(); await flush();}
    }
  } finally {Date.now = nativeNow;}
});

test("active speech survives the silence guard; hard cap and unacknowledged setup still close", async () => {
  const now = Date.now;
  const h = harness(); await h.hook.pttStart();
  Date.now = () => now() + 60001;
  h.tick();
  assert.equal(h.track.stopped, false);
  assert.equal(h.track.enabled, true);
  Date.now = () => now() + 600001;
  h.tick();
  assert.equal(h.track.stopped, true);
  assert.equal(h.ticks.at(-1).cleared, true);
  Date.now = now;
  const pending = harness(null, undefined, false); await pending.hook.toggle();
  Date.now = () => now() + 15001;
  pending.tick(); Date.now = now;
  assert.equal(pending.track.stopped, true);
  assert.equal(pending.ticks.at(-1).cleared, true);
});

test("window blur and hidden tabs leave holds open; teardown releases them", () => {
  const listeners = new Map();
  global.window = { addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {} };
  global.document = { hidden: false, addEventListener: (name, run) => listeners.set(name, run), removeEventListener() {} };
  let cleanup, open = true;
  const { useVoiceKeys } = compiled("useVoiceChat.ts", {
    ...mascotMocks,
    "@/lib/voicePreference": compiled("../../lib/voicePreference.ts"),
    react: { useEffect: run => { cleanup = run(); } },
    "@/lib/client": {}, "@/lib/userError": {}, "@/lib/usageControl": {},
    "@/lib/desktop": { isDesktopShell: () => false },
    "@/lib/voice": voice, "@/lib/voiceTranscript": captions,
  });
  useVoiceKeys(true, async () => {}, () => { open = false; }, () => {});
  assert.equal(listeners.has("blur"), false);
  listeners.get("blur")?.(); assert.equal(open, true);
  open = true; document.hidden = true;
  assert.equal(listeners.has("visibilitychange"), false);
  listeners.get("visibilitychange")?.(); assert.equal(open, true);
  cleanup(); assert.equal(open, false);
});


test("call teardown posts all founder captions exactly once even after provider failure", async () => {
  const h = harness(); await h.hook.toggle();
  for (let i = 0; i < 7; i++) {
    await h.message({type:"conversation.item.input_audio_transcription.completed", item_id:`founder_${i}`, transcript:`Use audience ${i}, do not publish.`});
    await h.message({type:"response.output_audio_transcript.done", transcript:"Your answer is noted."});
  }
  await h.message({type:"error",error:{message:"fatal"}}); h.hook.stop(); await flush();
  const requests = h.requestBodies.filter(r => r.url.endsWith("/voice/summary"));
  assert.equal(requests.length, 1);
  assert.equal(requests[0].body.turns.filter(t => t.role === "founder").length, 7);
  assert.equal(requests[0].body.turns[0].text, "Use audience 0, do not publish.");
});

test("post_to_chat uses actual founder words and one stable key, then summary excludes repost", async () => {
  const sent = [];
  const h = harness(null, async (text, options) => {sent.push({text,options}); return {body_md:"Saved"};});
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed", item_id:"answer", transcript:"For architects. Do not publish yet."});
  for (const [name,call_id] of [["post_to_chat","one"],["supymem_chat","two"]])
    await h.message({type:"response.function_call_arguments.done",name,call_id,arguments:'{"text":"Publish it"}'});
  assert.equal(sent.length,1); assert.equal(sent[0].text,"For architects. Do not publish yet.");
  assert.match(sent[0].options.idempotencyKey, /^voice_ane_test_/);
  h.hook.stop(); await flush();
  const summary = h.requestBodies.find(r => r.url.endsWith("/voice/summary"));
  assert.equal(summary.body.turns[0].posted,true);
});

test("empty and retired account calls never send a summary", async () => {
  const h = harness(); await h.hook.toggle(); h.hook.stop(); await flush();
  assert.equal(h.requests.some(url => url.endsWith("/voice/summary")),false);
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"private",transcript:"Private words"});
  h.owner.subject="usr_other"; h.owner.epoch++; h.hook.stop(); await flush();
  assert.equal(h.requests.some(url => url.endsWith("/voice/summary")),false);
});

test("screen context is included in the session request and current instructions", async () => {
  const screen={page:"/new",step:"clarify",visible_actions:["Submit","Back"],expected_next:"Answer the question"};
  const h=harness(null,undefined,true,{entry:true,screen}); await h.hook.toggle();
  assert.deepEqual(h.requestBodies.find(r=>r.url.endsWith("/voice/session")).body.screen,screen);
  assert.ok(h.events.some(e=>e.type==="session.update" && e.session.instructions?.includes('"Back"')));
  h.hook.stop();
});


test("step changes update a live session while preserving server rules", async () => {
  const options={screen:{page:"/new",step:"clarify",visible_actions:["Submit"]}};
  const h=harness(null,undefined,true,options); await h.hook.toggle();
  options.screen={page:"/new",step:"review",visible_actions:["Continue","Back"]};
  const firstEffect = h.effects.length; h.rerender();
  h.effects.slice(firstEffect).find(effect => effect.toString().includes('screenUpdate'))();
  const updates=h.events.filter(e=>e.session?.instructions);
  assert.match(updates.at(-1).session.instructions, /Continue/);
  assert.match(updates.at(-1).session.instructions, /System approval rules/);
  assert.doesNotMatch(updates.at(-1).session.instructions, /Submit/);
  assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,1);
  h.hook.stop();
});

test("unconfirmed chat retries use the same voice key and summary save can be retried", async () => {
  const sent=[];
  const h=harness(null,async (text,opts)=>{sent.push(opts); return null;}); await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"answer",transcript:"Change the audience to architects"});
  await h.message({type:"response.function_call_arguments.done",name:"post_to_chat",call_id:"save",arguments:'{"text":"Change it"}'});
  h.hook.stop(); await flush();
  const summary=h.requestBodies.find(r=>r.url.endsWith("/voice/summary"));
  assert.equal(summary.body.turns[0].posted,false);
  assert.equal(sent[0].idempotencyKey, `voice_ane_test_${summary.body.turns[0].id}`);
});


test("entry hangup previews an unposted real idea once and writes a summary without committing", async () => {
  const sent=[], summaries=[];
  const h=harness(null,async (text,options)=>{sent.push({text,options});return {body_md:"Continue on screen"};},true,{entry:true,onSummary:text=>summaries.push(text)});
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A scheduling app for independent architects"});
  h.hook.stop(); h.hook.stop(); await flush();
  assert.equal(sent.length,1);assert.equal(summaries.length,1);
  assert.equal(sent[0].text,"A scheduling app for independent architects");
  assert.match(sent[0].options.idempotencyKey,/^voice_ane_test_/);
  assert.equal(h.requests.some(url=>url.includes("confirm")),false);
});


test("calls without meaningful final founder speech do not post notes or review toasts", async () => {
  for (const domain of ["Supplier invoice reconciliation", "Family travel planning"]) {
    for (const entry of [true, false]) for (const caption of [null, "E?", "um", `Maybe help ${domain}`]) {
      let sends = 0; const summaries = [];
      const call = harness(null, async () => { sends++; return {body_md: "Saved"}; }, true,
        {entry, onSummary: text => summaries.push(text)});
      await call.hook.toggle();
      await call.message({type: "response.output_audio_transcript.done", transcript: "Hello. What is on your mind?"});
      if (caption) await call.message({type: "conversation.item.input_audio_transcription.delta", delta: caption});
      call.hook.stop(); await flush();
      assert.equal(sends, 0); assert.equal(summaries.length, 0);
      assert.equal(call.requestBodies.some(request => request.url.endsWith("/voice/summary")), false);
      assert.equal(call.rerender().error, "");
      assert.equal(call.reported.length, 0);
    }
    const call = harness(null, undefined, true, {entry: true});
    await call.hook.toggle();
    await call.message({type: "conversation.item.input_audio_transcription.completed", item_id: "real", transcript: `Help me test ${domain}`});
    await call.message({type: "conversation.item.input_audio_transcription.delta", item_id: "partial", delta: "Do not publish until"});
    call.hook.stop(); await flush();
    const saved = call.requestBodies.find(request => request.url.endsWith("/voice/summary"));
    assert.ok(saved); assert.equal(saved.body.turns.filter(turn => turn.role === "founder").length, 2);
  }
});


test("write tools cannot invent an idea before a completed founder caption", async () => {
  for (const entry of [true,false]) for (const name of ["supymem_chat","post_to_chat"]) {
    const sent=[];
    const h=harness(null,async text=>{sent.push(text);return {body_md:"What is your idea?"};},true,{entry});
    await h.hook.toggle();
    await h.message({type:"response.function_call_arguments.done",call_id:"invented",name,arguments:'{"text":"A drone delivery service for restaurants"}'});
    assert.deepEqual(sent,[]);
    const output=h.events.find(event=>event.item?.call_id==="invented")?.item.output;
    assert.match(output,/repeat.*answer|type.*chat/i);
    await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"real_hello",transcript:"Hello"});
    await h.message({type:"response.function_call_arguments.done",call_id:"real",name,arguments:'{"text":"A drone delivery service for restaurants"}'});
    assert.deepEqual(sent,["Hello"]);
    h.hook.stop();await flush();
  }
});

test("invalid, noise and failed final captions retire earlier write authority", async () => {
  for (const caption of [
    {type:"conversation.item.input_audio_transcription.completed",transcript:""},
    {type:"conversation.item.input_audio_transcription.completed",transcript:"um"},
    {type:"conversation.item.input_audio_transcription.failed"},
  ]) {
    const sent=[];const h=harness(null,async text=>{sent.push(text);return {body_md:"Saved"};});await h.hook.toggle();
    await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"earlier",transcript:"Hello"});
    await h.message({item_id:"bad_final",...caption});
    await h.message({type:"response.function_call_arguments.done",call_id:"unsafe",name:"supymem_chat",arguments:'{"text":"Publish the page"}'});
    assert.deepEqual(sent,[]);
    assert.match(h.events.find(event=>event.item?.call_id==="unsafe")?.item.output,/repeat.*answer|type.*chat/i);
    h.hook.stop();await flush();
  }
});


test("a failed summary blocks a new call and retains its same-session retry payload", async () => {
  let permissions=0,attempts=0,saved=0;
  const h=harness(async stream=>{permissions++;return stream;},undefined,true,{
    summaryApi:async()=>{attempts++;if(attempts===1)return {ok:false};saved++;return {ok:true,data:{pending:false}};},
  });
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A scheduling app for architects"});
  h.hook.stop();await flush();
  assert.equal(attempts,1);
  await h.hook.toggle();await h.hook.pttStart();
  assert.equal(permissions,1);assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,1);
  await h.hook.retrySummary();await flush();
  assert.equal(attempts,2);assert.equal(saved,1);
  const bodies=h.requestBodies.filter(request=>request.url.endsWith("/voice/summary")).map(request=>request.body);
  assert.deepEqual(bodies[1],bodies[0]);
  await h.hook.toggle();assert.equal(permissions,2);
  assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);
  h.hook.stop();
});

test("stop blocks a new call immediately while an admitted chat and automatic summary save are delayed", async () => {
  let finishChat,finishSave,permissions=0;
  const h=harness(async stream=>{permissions++;return stream;},async()=>new Promise(resolve=>{finishChat=resolve;}),true,{
    summaryApi:()=>new Promise(resolve=>{finishSave=resolve;}),
  });
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"answer",transcript:"The audience is architects"});
  await h.message({type:"response.function_call_arguments.done",call_id:"post",name:"post_to_chat",arguments:'{"text":"The audience is architects"}'});
  h.hook.stop();
  await h.hook.toggle();await h.hook.pttStart();
  assert.equal(permissions,1);assert.equal(h.requests.some(url=>url.endsWith("/voice/summary")),false);
  finishChat({body_md:"Saved your answer"});await flush();
  assert.equal(typeof finishSave,"function");
  await h.hook.toggle();await h.hook.pttStart();assert.equal(permissions,1);
  finishSave({ok:true,data:{pending:false}});await flush();
  await h.hook.pttStart();assert.equal(permissions,2);
  h.hook.stop();
});

test("successful entry notes and retired ownership release the prior-summary call guard", async () => {
  const entry=harness(null,undefined,true,{entry:true,onSummary:()=>{}});await entry.hook.toggle();
  await entry.message({type:"conversation.item.input_audio_transcription.completed",item_id:"entry_idea",transcript:"An app for independent architects"});
  entry.hook.stop();await flush();await entry.hook.toggle();
  assert.equal(entry.requests.filter(url=>url.endsWith("/voice/session")).length,2);entry.hook.stop();
  const retired=harness(null,undefined,true,{summaryApi:async()=>({ok:false})});await retired.hook.toggle();
  await retired.message({type:"conversation.item.input_audio_transcription.completed",item_id:"private",transcript:"Private founder answer"});
  retired.hook.stop();await flush();
  retired.owner.subject="usr_other";retired.owner.epoch++;await retired.hook.toggle();
  assert.equal(retired.requests.filter(url=>url.endsWith("/voice/session")).length,2);
  await retired.hook.retrySummary();
  assert.equal(retired.requests.filter(url=>url.endsWith("/voice/summary")).length,1);retired.hook.stop();
});


test("a saved summary needing an edit releases the call guard without dropping its complete note", async () => {
  let saves=0;
  const h=harness(null,undefined,true,{summaryApi:async()=>{saves++;return {ok:true,data:{needs_edit:true,pending:false}};}});
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"complete",transcript:"Keep the saved direction"});
  await h.message({type:"conversation.item.input_audio_transcription.delta",delta:"Publish only after"});
  h.hook.stop();await flush();
  assert.equal(saves,1);
  const body=h.requestBodies.find(request=>request.url.endsWith("/voice/summary")).body;
  assert.equal(body.turns[1].text,"Publish only after");assert.equal(body.turns[1].final,false);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);h.hook.stop();
});

test("B5 review: reversed finals keep spoken order and drain older unposted words before the latest write", async () => {
  const sent=[];const h=harness(null,async (text,opts)=>{sent.push({text,key:opts.idempotencyKey});return {body_md:"Saved"};});
  await h.hook.toggle();await commitInputs(h);
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_B",transcript:"Actually the audience is architects"});
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"The audience is teachers"});
  await h.message({type:"response.function_call_arguments.done",call_id:"write_B",name:"post_to_chat",arguments:'{"text":"Model paraphrase"}'});
  await flush();
  assert.deepEqual(sent.map(t=>t.text),["The audience is teachers","Actually the audience is architects"]);
  assert.equal(new Set(sent.map(t=>t.key)).size,2);
  h.hook.stop();await flush();
  const turns=h.requestBodies.find(r=>r.url.endsWith("/voice/summary")).body.turns.filter(t=>t.role==="founder");
  assert.deepEqual(turns.map(t=>t.text),sent.map(t=>t.text));assert.ok(turns.every(t=>t.posted));
});

test("B5 review: interleaved item deltas remain separate exact notes", async () => {
  const h=harness();await h.hook.toggle();await commitInputs(h);
  for(const [item_id,delta] of [["item_A","Teach"],["item_B","Arch"],["item_A","ers"],["item_B","itects"]])
    await h.message({type:"conversation.item.input_audio_transcription.delta",item_id,delta});
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"Teachers"});
  h.hook.stop();await flush();
  const turns=h.requestBodies.find(r=>r.url.endsWith("/voice/summary")).body.turns.filter(t=>t.role==="founder");
  assert.deepEqual(turns.map(t=>t.text),["Teachers","Architects"]);assert.deepEqual(turns.map(t=>t.final),[true,false]);
});

test("B5 review: late older final preserves the latest response and pending tool", async () => {
  let finish;const sent=[];const h=harness(null,text=>{sent.push(text);return text==="Use teachers"?Promise.resolve({body_md:"Earlier answer recorded"}):new Promise(resolve=>{finish=resolve;});});
  await h.hook.toggle();await commitInputs(h);
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_B",transcript:"Use architects"});
  await h.message({type:"response.function_call_arguments.done",call_id:"read_B_wait",name:"web_search",arguments:'{"query":"architects"}'});
  assert.equal(sent.length,0);
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"Use teachers"});
  await h.message({type:"response.function_call_arguments.done",call_id:"read_B",name:"web_search",arguments:'{"query":"architects"}'});
  assert.deepEqual(sent,["Use teachers","Use architects"]);
  finish({body_md:"The latest check is complete",partial:false});await flush();
  const output=h.events.find(e=>e.item?.call_id==="read_B")?.item.output;
  assert.match(output,/latest check is complete/);assert.doesNotMatch(output,/changed direction/);
  await h.message({type:"response.done",response:{id:"reply_B"}});
  assert.equal(h.events.filter(e=>e.type==="response.create").length,2);h.hook.stop();await flush();
});

test("B5 review: latest failed caption retires authority but late older failure does not", async () => {
  for(const failLatest of [false,true]) {
    const sent=[];const h=harness(null,async text=>{sent.push(text);return {body_md:"Saved"};});await h.hook.toggle();await commitInputs(h);
    await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_B",transcript:"Use architects"});
    if(failLatest) {
      await h.message({type:"input_audio_buffer.committed",item_id:"item_C",previous_item_id:"item_B"});
      await h.message({type:"conversation.item.input_audio_transcription.failed",item_id:"item_C"});
    }
    await h.message({type:"conversation.item.input_audio_transcription.failed",item_id:"item_A"});
    await h.message({type:"response.function_call_arguments.done",call_id:"write",name:"supymem_chat",arguments:'{"text":"Invented"}'});
    assert.deepEqual(sent,failLatest?[]:["Use architects"]);h.hook.stop();await flush();
  }
});

test("B5 review: duplicate final is idempotent and an earlier committed transcript holds later writes", async () => {
  const sent=[];const h=harness(null,async text=>{sent.push(text);return {body_md:"Saved"};});await h.hook.toggle();await commitInputs(h);
  const b={type:"conversation.item.input_audio_transcription.completed",item_id:"item_B",transcript:"Use architects"};
  await h.message(b);await h.message(b);
  await h.message({type:"response.function_call_arguments.done",call_id:"too_early",name:"post_to_chat",arguments:'{"text":"Ignored"}'});
  assert.deepEqual(sent,[]);
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"Use teachers"});
  await h.message({type:"response.function_call_arguments.done",call_id:"ready",name:"post_to_chat",arguments:'{"text":"Ignored"}'});
  assert.deepEqual(sent,["Use teachers","Use architects"]);h.hook.stop();await flush();
  const turns=h.requestBodies.find(r=>r.url.endsWith("/voice/summary")).body.turns.filter(t=>t.role==="founder");assert.equal(turns.length,2);
});

test("definitive provider output failure clears summary retry and permits a new call", async () => {
  const h=harness(null,undefined,true,{summaryApi:async()=>({ok:true,data:{pending:false,provider_error:"provider_output_invalid"}})});
  await h.hook.toggle();await commitInputs(h);
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"failed",transcript:"Explain my saved direction"});
  h.hook.stop();await flush();
  const state=h.rerender();
  assert.equal(state.summaryPending,false);
  assert.match(state.error,/saved your voice notes.*type the request again/);
  assert.doesNotMatch(state.error,/unconfirmed/);
  await state.toggle();assert.equal(h.peers.length,2);h.hook.stop();
});

test("B5 review: pending plus needs_edit retains save retry and prevents a new call", async () => {
  const h=harness(null,undefined,true,{summaryApi:async()=>({ok:true,data:{pending:true,needs_edit:true}})});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"Use architects"});
  h.hook.stop();await flush();await h.hook.toggle();await h.hook.pttStart();
  assert.equal(h.requests.filter(r=>r.endsWith("/voice/session")).length,1);
  await h.hook.retrySummary();assert.equal(h.requests.filter(r=>r.endsWith("/voice/summary")).length,2);
});

test("B5 review: an uncertain prefix never admits the newer turn or replaces its predecessor key", async () => {
  const sent=[];const h=harness(null,async (text,opts)=>{sent.push({text,key:opts.idempotencyKey});return null;},true,
    {summaryApi:async()=>({ok:true,data:{pending:true}})});
  await h.hook.toggle();await commitInputs(h);
  for(const [item_id,transcript] of [["item_A","Use teachers"],["item_B","Use architects"]])
    await h.message({type:"conversation.item.input_audio_transcription.completed",item_id,transcript});
  for(const call_id of ["first_try","retry"])
    await h.message({type:"response.function_call_arguments.done",call_id,name:"post_to_chat",arguments:'{"text":"Ignored"}'});
  assert.equal(sent.length,1);assert.equal(sent[0].text,"Use teachers");
  h.hook.stop();await flush();
  const body=h.requestBodies.find(r=>r.url.endsWith("/voice/summary")).body;
  assert.deepEqual(body.turns.filter(t=>t.role==="founder").map(t=>t.posted),[false,false]);
  assert.equal(sent[0].key,`voice_ane_test_${body.turns.find(t=>t.role==="founder").id}`);
  await h.hook.toggle();assert.equal(h.requests.filter(r=>r.endsWith("/voice/session")).length,1);
});

test("B5 review: predecessor links reconcile a commit arriving before its predecessor", async () => {
  const sent=[];const h=harness(null,async text=>{sent.push(text);return {body_md:"Saved"};});await h.hook.toggle();
  await h.message({type:"input_audio_buffer.committed",item_id:"item_B",previous_item_id:"item_A"});
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_B",transcript:"Use architects"});
  await h.message({type:"input_audio_buffer.committed",item_id:"item_A",previous_item_id:null});
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"item_A",transcript:"Use teachers"});
  await h.message({type:"response.function_call_arguments.done",call_id:"write",name:"post_to_chat",arguments:'{"text":"Ignored"}'});
  assert.deepEqual(sent,["Use teachers","Use architects"]);h.hook.stop();await flush();
});


test("entry hangup persists complete and partial notes before previewing, with one stable note identity", async () => {
  const order=[],summaries=[];const noteId="entry-voice-note_"+"a".repeat(64);
  const h=harness(null,async text=>{order.push("preview:"+text);return {body_md:"Review the idea"};},true,{entry:true,targetVentureId:"ven_owned",
    onSummary:(text,id)=>summaries.push({text,id}),summaryApi:async(_url,body)=>{order.push("save");return {ok:true,data:{id:noteId,body_md:"From your voice call:\nA tool for architects\nUnconfirmed speech, edit before sending: Do not publish until",pending:false,needs_edit:true}};}});
  await h.hook.toggle();
  await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"complete",transcript:"A tool for architects"});
  await h.message({type:"conversation.item.input_audio_transcription.delta",item_id:"partial",delta:"Do not publish until"});
  h.hook.stop();h.hook.stop();await flush();
  const calls=h.requestBodies.filter(request=>request.url.endsWith("/entry/voice/summary"));
  assert.equal(calls.length,1);assert.equal(calls[0].body.target_venture_id,"ven_owned");
  assert.deepEqual(calls[0].body.turns.filter(turn=>turn.role==="founder").map(turn=>[turn.text,turn.final]),[["A tool for architects",true],["Do not publish until",false]]);
  assert.deepEqual(order,["save","preview:A tool for architects"]);assert.equal(summaries.length,1);assert.equal(summaries[0].id,noteId);
  assert.equal(h.requests.some(url=>url.includes("confirm")),false);
});

test("entry persistence failure keeps the original retry and never claims saved or previews before storage succeeds", async () => {
  let attempts=0,previews=0;const summaries=[],bodies=[];const noteId="entry-voice-note_"+"b".repeat(64);
  const h=harness(null,async()=>{previews++;return {body_md:"Review"};},true,{entry:true,onSummary:(text,id)=>summaries.push({text,id}),
    summaryApi:async(_url,body)=>{bodies.push(body);attempts++;return attempts===1?{ok:false}:{ok:true,data:{id:noteId,body_md:"Saved exact founder words",pending:false,needs_edit:false}};}});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A tool for architects"});
  h.hook.stop();await flush();assert.equal(attempts,1);assert.equal(previews,0);assert.equal(summaries.length,0);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,1);
  await h.hook.retrySummary();await flush();assert.equal(attempts,2);assert.equal(previews,1);assert.equal(summaries.length,1);
  assert.deepEqual(bodies[1],bodies[0]);assert.equal(summaries[0].id,noteId);
});

test("entry note and preview callbacks never enter a retired owner after a delayed save", async () => {
  let finish;const summaries=[],previews=[];
  const h=harness(null,async text=>{previews.push(text);return {body_md:"Review"};},true,{entry:true,onSummary:(text,id)=>summaries.push({text,id}),
    summaryApi:()=>new Promise(resolve=>{finish=resolve;})});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"private",transcript:"Private founder idea"});
  h.hook.stop();await flush();assert.equal(typeof finish,"function");
  h.owner.subject="usr_other";h.owner.epoch++;
  finish({ok:true,data:{id:"entry-voice-note_"+"c".repeat(64),body_md:"Private founder idea",pending:false,needs_edit:false}});await flush();
  assert.deepEqual(summaries,[]);assert.deepEqual(previews,[]);
});

test("a known blocked correction remains unposted and durable without replacing or confirming the earlier quote", async () => {
  const summaries=[],stored=[];const sent=[];const noteId="entry-voice-note_"+"d".repeat(64);
  const h=harness(null,async text=>{sent.push(text);return {body_md:"Use Back to revise the reviewed direction",admitted:false};},true,{entry:true,onSummary:(text,id)=>summaries.push({text,id}),
    summaryApi:async(_url,body)=>{stored.push(body);return {ok:true,data:{id:noteId,body_md:"Actually, the audience is architects",pending:false,needs_edit:true}};}});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"correction",transcript:"Actually, the audience is architects"});
  await h.message({type:"response.function_call_arguments.done",call_id:"post",name:"post_to_chat",arguments:'{"text":"Use teachers"}'});
  h.hook.stop();await flush();
  assert.equal(stored.length,1);assert.equal(stored[0].turns.find(turn=>turn.role==="founder").posted,false);
  assert.equal(summaries[0].id,noteId);assert.ok(summaries[0].text.includes("architects"));
  assert.equal(h.requests.some(url=>url.includes("confirm")),false);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);h.hook.stop();
});


test("entry pending takes precedence over review flags and keeps the exact durable save retry", async () => {
  let attempts=0,previews=0;const summaries=[],bodies=[];const noteId="entry-voice-note_"+"e".repeat(64);
  const h=harness(null,async()=>{previews++;return {body_md:"Review"};},true,{entry:true,onSummary:(text,id)=>summaries.push(id),
    summaryApi:async(_url,body)=>{bodies.push(body);return {ok:true,data:{id:noteId,body_md:"You: A tool for architects",pending:++attempts===1,needs_edit:true}};}});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A tool for architects"});
  h.hook.stop();await flush();assert.equal(previews,0);await h.hook.toggle();
  assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,1);
  await h.hook.retrySummary();await flush();assert.equal(previews,1);assert.deepEqual(bodies[1],bodies[0]);assert.deepEqual(summaries,[noteId,noteId]);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);h.hook.stop();
});

test("entry preview failures retry the same source key after the note is durably stored", async () => {
  const sent=[],savedIds=[];let previews=0;const noteId="entry-voice-note_"+"f".repeat(64);
  const h=harness(null,async(text,options)=>{sent.push({text,key:options.idempotencyKey});return ++previews===1?null:{body_md:"Review"};},true,{entry:true,onSummary:(_text,id)=>savedIds.push(id),
    summaryApi:async()=>({ok:true,data:{id:noteId,body_md:"You: A tool for architects",pending:false,needs_edit:true}})});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A tool for architects"});
  h.hook.stop();await flush();assert.equal(savedIds.length,1);
  await h.hook.retrySummary();await flush();assert.equal(sent.length,2);assert.deepEqual(sent[1],sent[0]);assert.deepEqual(savedIds,[noteId,noteId]);
});

test("expired entry note recovery keeps the exact lost-ACK payload and never previews", async () => {
  let attempts=0,previews=0;const summaries=[],bodies=[];const noteId="entry-voice-note_"+"a".repeat(64);
  const h=harness(null,async()=>{previews++;return {body_md:"Review"};},true,{entry:true,targetVentureId:"ven_owned",
    onSummary:(text,id)=>summaries.push({text,id}),summaryApi:async(_url,body)=>{bodies.push(body);return ++attempts===1?{ok:false}:
      {ok:true,data:{id:noteId,body_md:"You: A tool for architects\nSupymem: Review your idea",pending:false,needs_edit:true,read_only:true}};}});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A tool for architects"});
  h.hook.stop();await flush();assert.equal(summaries.length,0);assert.equal(previews,0);
  await h.hook.retrySummary();await flush();assert.deepEqual(bodies[1],bodies[0]);assert.equal(bodies[1].target_venture_id,"ven_owned");
  assert.equal(previews,0);assert.deepEqual(summaries,[{text:"You: A tool for architects\nSupymem: Review your idea",id:noteId}]);
  await h.hook.retrySummary();assert.equal(attempts,2);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);h.hook.stop();
});

test("pending expired entry recovery retains its original retry until resolved and never previews", async () => {
  let attempts=0,previews=0;const bodies=[],savedIds=[];const noteId="entry-voice-note_"+"b".repeat(64);
  const h=harness(null,async()=>{previews++;return {body_md:"Review"};},true,{entry:true,onSummary:(_text,id)=>savedIds.push(id),
    summaryApi:async(_url,body)=>{bodies.push(body);return {ok:true,data:{id:noteId,body_md:"You: A tool for architects",pending:++attempts===1,needs_edit:true,read_only:true}};}});
  await h.hook.toggle();await h.message({type:"conversation.item.input_audio_transcription.completed",item_id:"idea",transcript:"A tool for architects"});
  h.hook.stop();await flush();await h.hook.toggle();
  assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,1);assert.equal(previews,0);
  await h.hook.retrySummary();await flush();assert.deepEqual(bodies[1],bodies[0]);assert.deepEqual(savedIds,[noteId,noteId]);assert.equal(previews,0);
  await h.hook.retrySummary();assert.equal(attempts,2);
  await h.hook.toggle();assert.equal(h.requests.filter(url=>url.endsWith("/voice/session")).length,2);h.hook.stop();
});
