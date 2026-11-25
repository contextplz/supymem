const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
const text = tree => Array.isArray(tree) ? tree.map(text).join('') : tree?.props ? text(tree.props.children) : typeof tree === 'string' || typeof tree === 'number' ? String(tree) : '';

async function fixture(domain, local = false) {
  const failures = await import('../../lib/apiFailure.ts');
  const states = [{subject: domain, email: `owner@${domain}.example`}, [{id: 'venture', name: domain}], 'venture',
    null, '', false, 'Sam', '', '', 0], refs = [], exports = {};
  const effects = [];
  let cursor = 0, refCursor = 0, response = {ok: false, status: 500, data: {}};
  const jsx = (type, props) => ({type, props});
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/AccountSettings.tsx`, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports, require(id) {
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial;
      return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
      useRef: initial => refs[refCursor++] ??= {current: initial}, useEffect() {}};
    if (id.endsWith('/apiFailure')) return failures;
    if (id.endsWith('/client')) return {api: async () => {if (response instanceof Error) throw response; return response;}};
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => local};
    return new Proxy({}, {get: (_target, name) => String(name)});
  }});
  return {render() {cursor = refCursor = 0; effects.length = 0; return exports.AccountSettings();},
    setResponse: value => {response = value;},
    clearAccount: () => {states[0] = null;},
    addVenture: venture => states[1].push(venture)};
}

test('your local account hides purchases, billing and hosted invitations across unrelated domains', async () => {
  for (const domain of ['procurement', 'surfing']) {
    const tree = (await fixture(domain, true)).render();
    assert.doesNotMatch(text(tree), /Buy more|Billing|Invite friends|successful invitations/);
    assert.equal(nodes(tree).some(node => ['buy', 'request'].includes(node.type)), false);
    assert.ok(nodes(tree).some(node => node.type === 'LocalSettings'));
  }
});

test('your local account opens your existing data and AI settings without waiting for a hosted account', async () => {
  for (const domain of ['Supplier audit scheduling', 'Neighborhood cooking classes']) {
    const app = await fixture(domain, true);
    app.clearAccount();
    const tree = app.render();
    assert.ok(nodes(tree).some(node => node.type === 'LocalSettings'), domain);
    assert.doesNotMatch(text(tree), /Loading your account|Supymem Mac app|Log out|Sign in/);
    const settings = readFileSync(`${__dirname}/../../app/settings/local/layout.tsx`, 'utf8');
    assert.match(settings, /<AppShell title="Settings">\{children\}<\/AppShell>/);
    const office = readFileSync(`${__dirname}/../office/CompanyOffice.tsx`, 'utf8');
    assert.match(office, /onClick=\{\(\) => router\.push\("\/"\)\}>All ventures<\/button>/);
  }
});

test('28: only a name validation response blames your input, while connection and server failures stay actionable', async () => {
  for (const domain of ['procurement', 'surfing']) {
    const app = await fixture(domain);
    for (const response of [{ok: false, status: 422, data: {detail: {error: {code: 'invalid_first_name'}}}},
      {ok: false, status: 500, data: {}}, {ok: false, status: 0, data: null}, new Error('transport offline')]) {
      app.setResponse(response);
      nodes(app.render()).find(node => node.type === 'form').props.onSubmit({preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
      const copy = text(app.render()); assert.equal(copy.includes('Use letters only'), response.status === 422);
      if (response.status !== 422) assert.match(copy, /saving your name|save your name/);
    }
  }
});

test('shared account settings keep your identity and preferences without hosted billing or referrals', async () => {
  for (const domain of ['procurement', 'surfing']) {
    const copy = text((await fixture(domain)).render());
    assert.match(copy, new RegExp(`owner@${domain}`));
    assert.match(copy, /Choose what Charlie emails you/);
    assert.doesNotMatch(copy, /Buy more|Billing|Invite friends|successful invitations|balance and usage|future spending|model usage/);
  }
});

test('switching ventures never labels the previous company usage as the new company, including failed loads', async () => {
  for (const domain of ['industrial inspection', 'home gardening']) {
    const app = await fixture(domain);
    app.addVenture({id: 'venture-next', name: `${domain} second company`});
    app.setResponse({ok: true, data: {tiles: {cost: {calls: 17}}}});
    assert.equal(nodes(app.render()).some(node => node.type === 'TokenMeter'), false);
    nodes(app.render()).find(node => node.type === 'select').props.onChange({target: {value: 'venture-next'}});
    app.setResponse({ok: false});
    let tree = app.render();
    assert.equal(nodes(tree).some(node => node.type === 'TokenMeter'), false, domain);
    tree = app.render(); assert.equal(nodes(tree).some(node => node.type === 'TokenMeter'), false);
    assert.doesNotMatch(text(tree), /model usage|17/);
    assert.equal(nodes(tree).find(node => node.type === 'InboxBriefingSettings').props.ventureId, 'venture-next');
    nodes(tree).find(node => node.type === 'button' && text(node) === 'Refresh').props.onClick();
    app.setResponse({ok: true, data: {tiles: {cost: {calls: 3}}}});
    assert.equal(nodes(app.render()).some(node => node.type === 'TokenMeter'), false);
  }
});
