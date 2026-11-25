const assert = require('node:assert/strict');
const test = require('node:test');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const settings = { windows: ['08:30', '17:30'], quiet_hours: { start: '21:00', end: '08:00' }, email_copy: false };
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness() {
  const slots = [], effects = [], cleanups = [], calls = [], listeners = new Map();
  let cursor = 0, subject = 'owner_b2b', epoch = 1, venture = 'ven_b2b', workspace = 'ws_b2b';
  const react = {
    useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => { slots[index] = typeof value === 'function' ? value(slots[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
    useEffect(callback, deps) { const index = cursor++, before = slots[index]; if (!before || deps.some((value, offset) => value !== before[offset])) {
      slots[index] = deps; effects.push(() => { cleanups[index]?.(); cleanups[index] = callback(); }); } },
  };
  const exported = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/useInboxBriefing.ts`, 'utf8'), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText, { exports: exported, crypto: require('node:crypto').webcrypto,
    window: { addEventListener: (event, callback) => listeners.set(event, callback), removeEventListener: event => listeners.delete(event) },
    require: id => id === 'react' ? react : { verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
      api: (path, init) => new Promise(resolve => calls.push({ path, init, resolve })) },
  });
  const render = () => { cursor = 0; return exported.useInboxBriefing(venture, workspace); };
  const flush = async () => { while (effects.length) effects.shift()(); await tick(); };
  return { render, flush, calls, valid: exported.validBriefingSettings,
    switchOwner() { subject = subject === 'owner_b2b' ? 'owner_consumer' : 'owner_b2b'; epoch += 1;
      venture = subject === 'owner_b2b' ? 'ven_b2b' : 'ven_consumer'; workspace = subject === 'owner_b2b' ? 'ws_b2b' : 'ws_consumer'; listeners.get('supymem-session-changed')?.(); },
    switchVenture() { venture = 'ven_other'; workspace = 'ws_other'; },
  };
}

test('briefing settings validate clock times, distinct windows and overnight quiet hours for unrelated domains', () => {
  const { valid } = harness();
  for (const idea of ['Inventory alerts for warehouse teams', 'Hiking groups for families']) {
    assert.equal(valid({ ...settings, idea }), true);
    for (const windows of [[], ['24:00'], ['8:30'], ['08:60'], ['08:30', '08:30'], ['08:30', '12:00', '17:30']]) assert.equal(valid({ ...settings, windows, idea }), false);
    assert.equal(valid({ ...settings, quiet_hours: { start: '24:00', end: '08:00' }, idea }), false);
    assert.equal(valid({ ...settings, email_copy: 'true', idea }), false);
  }
});

test('ui config fails closed and scoped consent/settings writes never cross founders or inboxes', async () => {
  const runtime = harness();
  runtime.render(); await runtime.flush();
  runtime.calls[0].resolve({ ok: true, data: { founder_ui: 'home' } }); await tick();
  assert.equal(runtime.render().enabled, false);
  assert.equal(await runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'a', consent: true } }), false);
  runtime.switchOwner(); await runtime.flush();
  runtime.calls.at(-1).resolve({ ok: true, data: { inbox_briefing_v1: true, inbox_briefing: { settings,
    mailboxes: [{ mailbox_id: 'a', email: 'a@walks.example', status: 'active', consent: false }, { mailbox_id: 'b', email: 'b@photos.example', status: 'active', consent: false }] } } }); await tick();
  runtime.render(); await runtime.flush();
  runtime.calls.at(-1).resolve({ ok: true, data: { inbox_briefing_v1: true, inbox_briefing: { settings,
    mailboxes: [{ mailbox_id: 'a', email: 'a@walks.example', status: 'active', consent: false }, { mailbox_id: 'b', email: 'b@photos.example', status: 'active', consent: false }] } } }); await tick();
  const before = runtime.calls.length;
  assert.equal(await runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'foreign', consent: true } }), false);
  assert.equal(runtime.calls.length, before);
  const saving = runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'a', consent: true } });
  assert.equal(await runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'b', consent: true } }), false);
  const call = runtime.calls.at(-1);
  assert.equal(call.path, '/api/ventures/ven_consumer/chat/confirm'); assert.equal(call.init.headers['X-Workspace-Id'], 'ws_consumer');
  assert.deepEqual(JSON.parse(call.init.body).payload, { mailbox_id: 'a', consent: true });
  call.resolve({ ok: true, data: {} }); assert.equal(await saving, true);
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.render().briefing.mailboxes.map(item => item.consent))), [true, false]);
  const revoking = runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'a', consent: false } });
  runtime.calls.at(-1).resolve({ ok: false }); assert.equal(await revoking, false);
  assert.equal(runtime.render().briefing.mailboxes[0].consent, true); assert.match(runtime.render().error, /could not be saved/);
  const schedule = runtime.render().save({ action: 'inbox.briefing.settings', payload: { ...settings, windows: ['09:15'] } });
  runtime.calls.at(-1).resolve({ ok: true }); assert.equal(await schedule, true); assert.equal(runtime.render().briefing.settings.windows[0], '09:15');
  const stale = runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'b', consent: true } });
  const delayed = runtime.calls.at(-1); runtime.switchOwner(); await runtime.flush(); delayed.resolve({ ok: true }); assert.equal(await stale, false);
  assert.equal(runtime.render().enabled, false);
});

test('stale config and writes are discarded on venture changes, including off and failed responses', async () => {
  const runtime = harness(); runtime.render(); await runtime.flush(); const old = runtime.calls[0];
  runtime.switchVenture(); runtime.render(); await runtime.flush();
  old.resolve({ ok: true, data: { inbox_briefing_v1: true } }); await tick(); assert.equal(runtime.render().enabled, false);
  runtime.calls.at(-1).resolve({ ok: false, data: { inbox_briefing_v1: true } }); await tick(); assert.equal(runtime.render().enabled, false);
});

test('server chat opt-in exposes saved briefing settings and uses consent for revocation across unrelated founders', async () => {
  for (const [consumer, email] of [[false, 'you@freight.example'], [true, 'you@ceramics.example']]) {
    const runtime = harness();
    if (consumer) runtime.switchOwner();
    runtime.render(); await runtime.flush();
    assert.equal(runtime.calls.at(-1).path, '/api/ui-config');
    const mailboxId = consumer ? 'mail_ceramics' : 'mail_freight';
    const savedSettings = { ...settings, timezone: 'UTC', poll_minutes: 60 };
    runtime.calls.at(-1).resolve({ ok: true, data: { founder_ui: 'chat', inbox_briefing_v1: true,
      inbox_briefing: { settings: savedSettings, mailboxes: [{ mailbox_id: mailboxId, email, status: 'active', consent: true }] } } });
    await tick();
    assert.equal(runtime.render().enabled, true);
    assert.equal(runtime.valid(runtime.render().briefing.settings), true);
    assert.equal(runtime.render().briefing.settings, savedSettings);
    assert.equal(runtime.render().briefing.mailboxes[0].consent, true);
    for (const consent of [false, true]) {
      const saving = runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: mailboxId, consent } });
      const call = runtime.calls.at(-1), body = JSON.parse(call.init.body);
      assert.equal(call.path, `/api/ventures/${consumer ? 'ven_consumer' : 'ven_b2b'}/chat/confirm`);
      assert.equal(call.init.headers['X-Workspace-Id'], consumer ? 'ws_consumer' : 'ws_b2b');
      assert.deepEqual(body.payload, { mailbox_id: mailboxId, consent });
      assert.equal(body.idempotency_key, call.init.headers['Idempotency-Key']);
      call.resolve({ ok: true, data: { inbox_briefing: { mailbox_id: mailboxId, consent } } });
      assert.equal(await saving, true);
      assert.equal(runtime.render().briefing.mailboxes[0].consent, consent);
    }
    runtime.switchVenture(); runtime.render(); await runtime.flush();
    runtime.calls.at(-1).resolve({ ok: true, data: { founder_ui: 'chat', inbox_briefing_v1: false, multi_gmail_v1: false } });
    await tick();
    assert.equal(runtime.render().enabled, false);
    assert.equal(runtime.render().briefing, undefined);
    const before = runtime.calls.length;
    assert.equal(await runtime.render().save({ action: 'inbox.briefing.consent', payload: { mailbox_id: mailboxId, consent: true } }), false);
    assert.equal(runtime.calls.length, before);
  }
});

test('retired consent callbacks never dispatch writes to your previous venture across unrelated domains', async () => {
  for (const consumer of [false, true]) {
    const runtime = harness();
    if (consumer) runtime.switchOwner();
    runtime.render(); await runtime.flush();
    runtime.calls.at(-1).resolve({ ok: true, data: { inbox_briefing_v1: true, inbox_briefing: { settings,
      mailboxes: [{ mailbox_id: 'a', email: consumer ? 'you@trail.example' : 'you@warehouse.example', status: 'active', consent: false }] } } });
    await tick();
    const retained = runtime.render().save;
    runtime.switchVenture(); runtime.render();
    const before = runtime.calls.length;
    assert.equal(await retained({ action: 'inbox.briefing.consent', payload: { mailbox_id: 'a', consent: true } }), false);
    assert.equal(runtime.calls.length, before);
  }
});
