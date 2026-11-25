const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('your usage limit notice offers a working buy action in unrelated domains', async () => {
  const copy = await import('../../lib/credits.ts');
  const {apiFailure} = await import('../../lib/apiFailure.ts');
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/ErrorNotice.tsx', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS},
  }).outputText, {exports, require(id) {
    if (id === 'react') return {useEffect() {}, createElement: (type, props, ...children) => ({type, props, children})};
    if (id === '@/lib/userError') return {reportUserError() {}};
    if (id === '@/lib/credits') return copy;
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
    throw new Error(id);
  }});
  for (const domain of ['Factory planning', 'Family hiking']) {
    const error = apiFailure({ok: false, status: 402, data: {error: {code: 'insufficient_credits'}}}, domain);
    assert.equal(error, copy.USAGE_LIMIT_MESSAGE);
    const notice = exports.ErrorNotice({error, source: domain});
    assert.equal(notice.props.role, 'alert');
    const action = notice.children.find(child => child?.type === 'a');
    assert.equal(action.props.href, '/account#account-buy');
    assert.equal(action.children[0], ' Buy more');
    assert.doesNotMatch(error, /\d|credits|\u2014/);
    const legacy = exports.ErrorNotice({error, source: domain, children: 'Your old quote uses 25 credits.'});
    assert.equal(legacy.children[0], copy.USAGE_LIMIT_MESSAGE);
  }
});

test('your local errors recover through your own AI settings across domains', async () => {
  const copy = await import('../../lib/credits.ts');
  const {apiFailure} = await import('../../lib/apiFailure.ts');
  const {serverReason} = await import('../../lib/serverReason.ts');
  const {errorCodeCopy} = await import('../../lib/errorCopy.ts');
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/ErrorNotice.tsx', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS},
  }).outputText, {exports, require(id) {
    if (id === 'react') return {useEffect() {}, createElement: (type, props, ...children) => ({type, props, children})};
    if (id === '@/lib/userError') return {reportUserError() {}};
    if (id === '@/lib/credits') return copy;
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => true};
    throw new Error(id);
  }});
  global.window = {__SUPYMEM__: {mode: 'local'}};
  try {
    for (const domain of ['Invoice reconciliation', 'Garden planning']) {
      const refusal = {ok: false, status: 402, data: {error: {code: 'research_insufficient_credits'}}};
      const notice = exports.ErrorNotice({error: copy.USAGE_LIMIT_MESSAGE, source: domain, children: 'Old purchase notice'});
      assert.equal(notice.children[0], 'Your service account has reached its limit. Add funds or raise your limit there, then try again.');
      assert.equal(notice.children.find(child => child?.type === 'a').props.href, '/settings/local#providers-title');
      assert.doesNotMatch(JSON.stringify(notice), /Buy more|account-buy|credits|\u2014/);
      assert.equal(apiFailure(refusal, domain), notice.children[0]);
      assert.equal(serverReason(refusal, domain), notice.children[0]);
      assert.equal(errorCodeCopy('research_insufficient_credits'), notice.children[0]);
      for (const code of ['unauthorized', 'device_product_build_parser_unavailable']) assert.doesNotMatch(errorCodeCopy(code), /Sign in again|Supymem Mac/);
      assert.doesNotMatch(apiFailure({ok: false, status: 401, data: null}, domain), /Sign in/);
      assert.doesNotMatch(apiFailure({ok: false, status: 503, data: {error: {code: 'capability_unavailable'}}}, domain), /hosted/);
    }
  } finally { delete global.window; }
});
