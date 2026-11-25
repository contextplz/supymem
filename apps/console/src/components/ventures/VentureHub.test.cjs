const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');

test('the venture picker and venture cards request available artwork across domains', () => {
  const source = readFileSync(`${__dirname}/ventureCards.ts`, 'utf8');
  assert.match(readFileSync(`${__dirname}/VentureHub.tsx`, 'utf8'), /src=\{cardImage\(\{ \.\.\.choice, art \}\)\}/);
  const exports = {};
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText, {exports});
  for (const [id, name] of [['warehouse.example', 'Stock planning'], ['trails.example', 'Hiking journal']]) {
    const choice = exports.defaultChoice(id, name);
    for (const art of exports.CARD_ART) {
      const asset = exports.cardImage({...choice, art});
      assert.equal(asset, `/venture-cards/${art}.png`);
      assert.equal(readFileSync(`${__dirname}/../../../public${asset}`).subarray(1, 4).toString(), 'PNG');
    }
  }
});

test('resume restores the paused venture on 404 and points you back to All ventures', async () => {
  const page = readFileSync(`${__dirname}/VentureHub.tsx`, 'utf8');
  const action = page.slice(page.indexOf('  async function resumeVenture('), page.indexOf('  async function restoreVenture('));
  const code = ts.transpileModule(`export ${action.trim()}`, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText;
  for (const id of ['ven_inventory', 'ven_pets']) {
    const exports = {}, venture = {id, paused: true};
    let ventures = [venture], errors = {};
    vm.runInNewContext(code, {exports, setBusy() {}, setCardErrors: update => {errors = update(errors);},
      setVentures: update => {ventures = update(ventures);}, api: async path => {
        assert.equal(path, `/api/ventures/${id}/resume`);
        assert.equal(ventures[0].paused, false);
        return {ok: false, status: 404};
      },
    });
    assert.equal(await exports.resumeVenture(venture), false);
    assert.equal(ventures[0], venture);
    assert.match(errors[id], /couldn’t resume.*refresh All ventures/i);
  }
});
test('polish3: your venture picker uses the current light shell scale and readable controls across domains', async () => {
  const css = readFileSync(`${__dirname}/VentureHubHome.module.css`, 'utf8');
  assert.match(css, /\.title \{[^}]*font: 650 22px \/ 1\.25 var\(--nf-font\)/);
  assert.match(css, /\.page \{[^}]*gap: 24px;[^}]*padding: 72px 24px 32px/);
  assert.match(css, /\.manage \{[^}]*border-radius: 12px/);
  assert.doesNotMatch(css, /text-transform: uppercase/);
  assert.match(css, /\.manageActive \{[^}]*color: var\(--nf-text\)/);
  assert.match(css, /\.manage:focus-visible \{[^}]*outline: 2px solid var\(--focus-ring\)/);
  for (const domain of ['Supply planning', 'Trail walking']) {
    const app = hubFixture(domain); app.setActive({ok: true, data: {items: [{id: `ven_${domain}`, name: domain, stage: 'idea'}]}});
    app.render(); app.load(); await new Promise(resolve => setImmediate(resolve));
    assert.match(JSON.stringify(app.render()), /Which venture\?/);
    assert.match(JSON.stringify(app.render()), /Manage Ventures/);
  }
  const source = readFileSync(`${__dirname}/../layout/AppShell.tsx`, 'utf8');
  const tree = ts.createSourceFile('AppShell.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let office;
  function visit(node) {
    if (ts.isConditionalExpression(node) && ts.isJsxElement(node.whenTrue) && node.whenTrue.openingElement.tagName.getText(tree) === 'CompanyOffice') office = node.condition.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(office);
  for (const domain of ['Supply planning', 'Trail walking']) assert.equal(vm.runInNewContext(office,
    {effectiveVentureId: '', entryPage: false, local: true, localBuildActivity: false, pathname: '/'}), false, `${domain}: no company sidebar before choosing a venture`);
});
test('your empty-home guidance meets normal text contrast across domains', () => {
  const css = readFileSync(`${__dirname}/VentureHubHome.module.css`, 'utf8');
  const hex = css.match(/\.empty\s*\{[^}]*color:\s*#([0-9a-f]{6})/i)[1];
  const channels = hex.match(/../g).map(value => parseInt(value, 16) / 255);
  const linear = channels.map(value => value <= 0.04045 ? value / 12.92 : ((value + 0.055) / 1.055) ** 2.4);
  const luminance = linear[0] * 0.2126 + linear[1] * 0.7152 + linear[2] * 0.0722;
  for (const domain of ['warehouse teams', 'weekend walkers']) assert.ok(1.05 / (luminance + 0.05) >= 4.5, domain);
});
test('your home passes local styling without changing hosted startup across domains', () => {
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    const exported = {}, jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/../../app/page.tsx`, 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports: exported, require(id) {
      if (id === 'react') return {useEffect() {}, useState: () => [false, () => {}]};
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      return {useLocalMode: () => local, AppShell: 'shell', VentureHub: 'ventures'};
    }});
    assert.equal(exported.default().props.children.props.local, local, domain);
  }
});

test('your local desktop start button reuses the readable primary style across domains', () => {
  const source = readFileSync(`${__dirname}/VentureHub.tsx`, 'utf8');
  const tree = ts.createSourceFile('VentureHub.tsx', source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let className;
  const visit = node => {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'NewVentureLink'
        && node.openingElement.attributes.getText(tree).includes('onboarding_start')) {
      className = node.openingElement.attributes.properties.find(property => property.name?.text === 'className').initializer.expression.getText(tree);
    }
    ts.forEachChild(node, visit);
  };
  visit(tree);
  for (const domain of ['warehouse teams', 'weekend walkers']) for (const local of [false, true]) {
    assert.equal(vm.runInNewContext(className, {local, classicStyles: {newButton: 'primary'}, styles: {newButton: 'home'}}), local ? 'primary' : 'home', domain);
  }
  const css = readFileSync(`${__dirname}/VentureHub.module.css`, 'utf8');
  assert.match(css, /\.newButton \{[^}]*background: var\(--accent\);[^}]*color: var\(--accent-ink\)/);
});
test('your empty ventures screen explains the workspace and offers one way to start', async () => {
  const copy = await import('../../lib/betaCopy.ts');
  for (const scenario of [
    { audience: 'Finance teams at manufacturers', layout: 'home' },
    { audience: 'Parents planning accessible holidays', layout: 'chat' },
  ]) {
  const state = []; state[4] = false; let cursor = 0;
  const jsx = (type, props) => ({ type, props }), exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/VentureHub.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, require(id) {
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === 'react') return { useState(initial) { const index = cursor++; if (!(index in state)) state[index] = typeof initial === 'function' ? initial() : initial; return [state[index], () => {}]; }, useEffect() {}, useContext: () => scenario.layout, useRef: current => ({ current }), useCallback: fn => fn };
    if (id === 'next/navigation') return { useRouter: () => ({}) };
    if (id.endsWith('/betaCopy')) return copy;
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
    return { default: {}, NewVentureLink: 'start', ErrorNotice: 'error' };
  }});
  const tree = exports.VentureHub();
  const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
  const starts = nodes(tree).filter(node => node.type === 'start');
  assert.equal(starts.length, 1, scenario.audience); assert.equal(starts[0].props['data-usage-control'], 'onboarding_start');
  assert.match(JSON.stringify(tree), /A venture is the workspace for your idea/);
  if (scenario.layout === 'home') assert.match(JSON.stringify(tree), /Start with your idea/);
  assert.doesNotMatch(JSON.stringify(tree), /Which venture\?/);
  assert.doesNotMatch(JSON.stringify(tree), /\u2014/);
  }
});

function hubFixture(domain, desktop = false, layout = 'home', entryHelpers = {}, storage = null) {
  const states = [], refs = [], effects = [], redirects = [], exports = {};
  let cursor = 0, refCursor = 0, session = async () => {}, active = {ok: true, data: {items: []}}, load;
  let trash = async () => ({ok: true, data: {items: [{id: `deleted_${domain}`, name: domain, stage: 'deleted'}]}});
  let entry = async () => ({ok: true, data: {}});
  const router = {replace: href => redirects.push(href)}, jsx = (type, props) => ({type, props});
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/VentureHub.tsx`, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
  }).outputText, {exports, AbortController, window: {addEventListener() {}, removeEventListener() {}}, require(id) {
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id === 'react') return {useContext: () => layout, useCallback: callback => {if (callback.toString().includes('/api/ventures/deleted')) load = callback; return callback;},
      useState(initial) {const index = cursor++; if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
        return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
      useRef: initial => refs[refCursor++] ??= {current: initial}, useEffect: callback => effects.push(callback)};
    if (id === 'next/navigation') return {useRouter: () => router};
    if (id.endsWith('/desktop')) return {isDesktopShell: () => desktop};
    if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
    if (id.endsWith('/client')) return {ensureSession: () => session(), verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => 1,
      api: async () => entry(), apiSwr: async (path, publish) => publish(path.includes('/deleted') ? await trash() : active)};
    if (id.endsWith('/charlieGreeting')) return {loadGreeting: async () => ({greetedAt: '2026-10-01'})};
    if (id.endsWith('/format')) return {displayName: name => name};
    if (id.endsWith('/chatEntry')) return {splitEntryHistory: items => ({fresh: items}), unsentEntryDraftHref: () => null, ...entryHelpers};
    if (id.endsWith('/chatComposer')) return {draftStorage: () => storage};
    if (id.endsWith('/ventureCards')) return {choiceFromVenture: () => ({}), cardImage: () => 'card.png'};
    return {default: {}, ErrorNotice: 'error', NewVentureLink: 'start'};
  }});
  const render = () => {cursor = refCursor = 0; effects.length = 0; return exports.VentureHub();};
  return {redirects, render, load: () => effects[0](), quietLoad: () => load(true), redirect: () => effects[1](),
    entryLoad: () => effects[2](), setEntry: value => {entry = value;},
    setActive: value => {active = value;}, setTrash: value => {trash = value;}, setSession: value => {session = value;}};
}

test('08: Continue your draft opens the unsent Add Venture route', async () => {
  const {entryDraftKey, readEntryDraft, unsentEntryDraftHref, writeEntryDraft} = await import('../../lib/chatEntry.ts');
  for (const domain of ['rivetworks.invalid', 'mossmornings.invalid']) {
    const search = `?fresh=${domain.split('.')[0]}`;
    const map = new Map();
    const storage = {getItem: key => map.get(key) ?? null, setItem: (key, value) => map.set(key, value), removeItem: key => map.delete(key)};
    const key = entryDraftKey('owner', {pathname: '/new', search});
    const text = `An idea for ${domain}`;
    writeEntryDraft(storage, key, text, 'owner', search);
    const app = hubFixture(domain, true, 'home', {unsentEntryDraftHref}, storage);
    app.render(); app.entryLoad(); await new Promise(resolve => setImmediate(resolve));
    const banner = app.render().props.children.find(node => node?.props?.['aria-label'] === 'Continue where you left off');
    assert.equal(banner.props.style.visibility, 'visible');
    const href = banner.props.children.find(node => node?.props?.href)?.props.href;
    assert.equal(href, `/new${search}`);
    assert.equal(readEntryDraft(storage, entryDraftKey('owner', {pathname: '/new', search: href.slice('/new'.length)})), text);
    assert.equal(readEntryDraft(storage, entryDraftKey('owner', {pathname: '/new', search: '?venture=ven_other'}, 'ven_other')), '');
    assert.equal(unsentEntryDraftHref(storage, 'other-owner'), null);
    writeEntryDraft(storage, key, '', 'owner', search);
    assert.equal(unsentEntryDraftHref(storage, 'owner'), null);
  }
});

test('the venture picker has a named main landmark that can receive navigation focus', () => {
  for (const domain of ['Wholesale dispatch', 'Home baking']) {
    const picker = hubFixture(domain).render();
    const heading = picker.props.children.find(node => node?.type === 'h1');
    assert.equal(picker.type, 'main', domain);
    assert.equal(picker.props.tabIndex, -1, domain);
    assert.ok(heading?.props.id, domain);
    assert.equal(picker.props['aria-labelledby'], heading.props.id, domain);
  }
});

test('a draft that loads late moves no venture tile on web or Mac, in either layout', async () => {
  const draftOf = nodes => nodes.findIndex(node => node?.props?.['aria-label'] === 'Continue where you left off');
  for (const domain of ['Fleet auditing', 'Pet walking']) for (const desktop of [false, true]) for (const layout of ['home', 'chat']) {
    const app = hubFixture(domain, desktop, layout);
    app.setActive({ok: true, data: {items: [{id: 'first', name: domain, stage: 'ideating'}, {id: 'second', name: `${domain} two`, stage: 'ideating'}]}});
    app.setTrash(async () => ({ok: true, data: {items: []}}));
    let finish;
    app.setEntry(() => new Promise(resolve => {finish = resolve;}));
    app.render(); app.load(); app.entryLoad(); await new Promise(resolve => setImmediate(resolve));
    const before = app.render().props.children;
    assert.ok(draftOf(before) >= 0, `${domain}: the slot exists before the draft loads`);
    assert.equal(before[draftOf(before)].props.style.visibility, 'hidden');
    finish({ok: true, data: {items: [{proposal: {state: 'pending'}}]}});
    await new Promise(resolve => setImmediate(resolve));
    const after = app.render().props.children;
    assert.equal(draftOf(after), draftOf(before));
    assert.equal(after[draftOf(after)].props.style.visibility, 'visible');
    assert.equal(after[draftOf(after)].props.className, before[draftOf(before)].props.className);
    const tiles = after.findIndex(node => /^(Ventures|\d+ ventures?)$/.test(node?.props?.['aria-label'] || ''));
    assert.ok(tiles >= 0 && tiles < draftOf(after), `${domain}: tiles come first`);
    assert.equal(JSON.stringify(after.slice(0, draftOf(after))), JSON.stringify(before.slice(0, draftOf(before))));
  }
});

test('01: you can restore your last deleted venture after both lists settle on web and Mac', async () => {
  for (const domain of ['Fleet auditing', 'Pet walking']) for (const desktop of [false, true]) {
    const app = hubFixture(domain, desktop);
    let resolveTrash;
    app.setTrash(() => new Promise(resolve => {resolveTrash = resolve;}));
    app.render(); app.load(); await new Promise(resolve => setImmediate(resolve));
    app.render(); app.redirect(); assert.equal(app.redirects.length, 0);
    resolveTrash({ok: true, data: {items: [{id: `deleted_${domain}`, name: domain, stage: 'deleted'}]}});
    await new Promise(resolve => setImmediate(resolve));
    const tree = app.render(); app.redirect(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(app.redirects.length, 0); assert.match(JSON.stringify(tree), /Restore/);
    app.setTrash(async () => ({ok: false})); await app.quietLoad();
    assert.match(JSON.stringify(app.render()), /Refresh recently deleted ventures/);
    app.redirect(); assert.equal(app.redirects.length, 0);
  }
});

test('02: retrying a failed list stays here until a successful empty portfolio is confirmed', async () => {
  for (const domain of ['Fleet auditing', 'Pet walking']) {
    const app = hubFixture(domain); app.setActive({ok: false}); app.setTrash(async () => ({ok: true, data: {items: []}}));
    app.render(); app.load(); await new Promise(resolve => setImmediate(resolve));
    let resume; app.setSession(() => new Promise(resolve => {resume = resolve;}));
    app.render(); app.load(); app.render(); app.redirect(); assert.equal(app.redirects.length, 0);
    app.setActive({ok: true, data: {items: []}}); resume(); await new Promise(resolve => setImmediate(resolve));
    app.render(); app.redirect(); assert.deepEqual(app.redirects, ['/new']);
  }
});

test('42: your light venture management and restoration surfaces use the home palette without a layout redesign', async () => {
  const css = readFileSync(`${__dirname}/VentureHubHome.module.css`, 'utf8');
  for (const domain of ['Fleet auditing', 'Pet walking']) {
    assert.match(css, /\.managePanel \{[^}]*background: rgba\(0, 0, 0, 0\.72\)/);
    const app = hubFixture(domain); app.render(); app.load(); await new Promise(resolve => setImmediate(resolve));
    assert.match(JSON.stringify(app.render()), /Restore/);
    for (const selector of ['banner', 'manageCard', 'trashRow']) {
      const rule = new RegExp(`\\.${selector} \\{([^}]+)\\}`).exec(css)[1];
      assert.match(rule, /background: var\(--nf-bg\)/); assert.match(rule, /border: 1px solid var\(--nf-border\)/);
      assert.doesNotMatch(rule, /#181818|color: #fff/);
    }
    assert.match(css, /--nf-bg: #fff/); assert.match(css, /--nf-text: #1c1c1e/);
    assert.match(css, /width: min\(420px, 100%\)/); assert.match(css, /width: min\(320px, calc\(100vw - 36px\)\)/);
  }
});
