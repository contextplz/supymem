const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test.describe("your hosted beta admission", {
  skip: process.env.SUPYMEM_MODE === "local" && "Hosted-only release checks. Run these tests in your hosted checkout.",
}, () => {
  const landing = readFileSync(resolve(__dirname, '../../../../marketing/src/site/Landing.tsx'), 'utf8');
  const staticPage = readFileSync(resolve(__dirname, '../../../public/marketing/index.html'), 'utf8');
  const submit = landing.slice(landing.indexOf('  async function onAccessSubmit('), landing.indexOf('  async function onWaitlistSubmit('));
  const compiled = ts.transpileModule(submit, { compilerOptions: { target: ts.ScriptTarget.ES2022 } }).outputText;
  // The submit handler uses the landing page's own access-code helpers; load the real module, not a copy.
  const helpers = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(resolve(__dirname, '../../../../marketing/src/site/access-code.ts'), 'utf8'),
    { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS } }).outputText, { exports: helpers, URLSearchParams });

  function access(fetchResponse, handoff = () => {}) {
    const state = { code: 'ABCD2345', error: null, busy: false, done: false, url: null, timer: null, calls: 0 };
    const context = { URL, AbortController, accessCode: state.code, submitting: false, ...helpers,
      setAccessCode: value => { state.code = value; },
      setAccessError: value => { state.error = value; }, setSubmitting: value => { state.busy = value; },
      setAccessDone: value => { state.done = value; }, setAccessDownload: value => { state.url = value; }, track() {},
      betaApi: action => `https://gateway.supymem.com/public/beta/${action}`,
      fetch: (...args) => { state.calls++; return fetchResponse(...args); },
      window: { setTimeout: (callback, ms) => { state.timer = { callback, ms }; return 1; }, clearTimeout: () => { state.timer = null; }, location: { assign: handoff } } };
    vm.createContext(context); vm.runInContext(compiled, context);
    return { state, context, submit: () => { context.accessCode = state.code; context.submitting = state.busy; return context.onAccessSubmit({ preventDefault() {} }); } };
  }
  const response = (status, data) => ({ ok: status >= 200 && status < 300, status, json: async () => data });

  test('beta admission waits for verification and handoff, never asks for the code again', async () => {
    let finish;
    const ui = access(() => new Promise(resolve => { finish = resolve; }), url => {
      assert.equal(ui.state.busy, true); assert.equal(ui.state.done, false); assert.equal(ui.state.error, null);
      assert.equal(url, 'https://gateway.supymem.com/public/beta/download?code=ABCD2345');
    });
    const pending = ui.submit();
    assert.equal(ui.state.busy, true); assert.equal(ui.state.done, false); assert.equal(ui.state.error, null);
    assert.equal(ui.state.timer.ms, 10000);
    await ui.submit(); assert.equal(ui.state.calls, 1);
    finish(response(200, { ok: true, download_url: 'https://gateway.supymem.com/public/beta/download?code=ABCD2345' }));
    await pending;
    assert.equal(ui.state.busy, false); assert.equal(ui.state.done, true); assert.equal(ui.state.timer, null);
    // Founder 2026-10-24: after a verified download, offer a separate, clearly worded retry.
    assert.match(landing, /Didn't the download start\? Enter the code again\./);
    assert.match(landing, /submitting \? "Checking your code…"/);
    assert.match(landing, /Download again/);
    assert.match(landing, /if \(!submitting && !accessDone\)/);
  });

  for (const [reason, message] of [['expired', 'expired'], ['disabled', 'no longer active'], ['exhausted', 'already been used'], ['mistyped', 'letters and numbers'], ['unknown', 'try again']]) {
    test(`beta admission gives an actionable failure for ${reason}`, async () => {
      const ui = access(async () => response(404, { ok: false, error: 'invalid_code', reason }));
      await ui.submit();
      assert.match(ui.state.error, /^That code didn't work\. Check it and try again\./);
      assert.ok(ui.state.error.includes(message));
      assert.equal(ui.state.done, false); assert.equal(ui.state.busy, false);
    });
  }

  test('timeouts, network errors and failed handoffs do not claim the code was rejected', async () => {
    const ui = access((url, { signal }) => new Promise((resolve, reject) => signal.addEventListener('abort', () => reject(new Error('timeout')))));
    const pending = ui.submit(); ui.state.timer.callback(); await pending;
    assert.match(ui.state.error, /took too long\. Try again/); assert.equal(ui.state.busy, false);
    for (const action of [() => Promise.reject(new Error('offline')), async () => response(503, {})]) {
      const failed = access(action); await failed.submit();
      assert.doesNotMatch(failed.state.error, /That code didn't work/); assert.equal(failed.state.done, false);
    }
    const failed = access(async () => response(200, { ok: true, download_url: 'https://gateway.supymem.com/public/beta/download?code=ABCD2345' }), () => { throw Error('blocked'); });
    await failed.submit(); assert.equal(failed.state.done, false); assert.match(failed.state.error, /Could not start the download/);
  });

  test('verification rejects malformed codes and untrusted handoff URLs', async () => {
    const malformed = access(() => { throw Error('must not fetch'); }); malformed.state.code = 'short'; await malformed.submit();
    assert.equal(malformed.state.calls, 0); assert.match(malformed.state.error, /8 letters and numbers/);
    for (const download_url of ['https://gateway.supymem.com/public/beta/download-evil', 'https://gateway.supymem.com.evil/public/beta/download', 'https://user:secret@gateway.supymem.com/public/beta/download']) {
      const ui = access(async () => response(200, { ok: true, download_url }), () => assert.fail('untrusted URL launched'));
      await ui.submit(); assert.equal(ui.state.done, false); assert.match(ui.state.error, /Try again/);
    }
  });

  test('static beta access keeps the same progress, handoff and error contract', () => {
    assert.match(staticPage, /Checking your code…/);
    assert.match(staticPage, /controller\.abort\(\); \}, 10000\)/);
    assert.match(staticPage, /target\.origin === expected\.origin && target\.pathname === expected\.pathname/);
    assert.match(staticPage, /That code didn't work\. Check it and try again\./);
    assert.match(staticPage, /id="access-download" hidden>Download again/);
    assert.match(staticPage, /max-width: 100%;[\s\S]*white-space: normal;/);
    const tree = ts.createSourceFile('Landing.tsx', landing, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
    assert.equal(tree.parseDiagnostics.length, 0);
  });
});
