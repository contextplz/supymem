const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];
const tick = () => new Promise(resolve => setImmediate(resolve));

test('22: independent Mac reads start together and a missing optional result does not discard the others', async () => {
  for (const domain of ['Warehouse routing', 'Family meals']) {
    const exports = {}, effects = [], calls = [], pending = new Map(), values = []; let cursor = 0;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/DesktopSettings.tsx`, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, require(id) {
      if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState: initial => {const index = cursor++; values[index] = initial; return [initial, value => {values[index] = value;}];},
        useEffect: callback => effects.push(callback)};
      if (id.endsWith('/desktop')) return {isDesktopShell: () => true,
        desktopInvoke: command => {calls.push(command); return new Promise(resolve => pending.set(command, resolve));},
        desktopInvokeOrThrow: command => {calls.push(command); return new Promise(resolve => pending.set(command, resolve));}};
      return {};
    }});
    exports.DesktopSettings({ventureId: domain}); const cleanups = effects.map(effect => effect());
    assert.equal(calls.length, 7);
    pending.get('data_dir_path')(`/saved/${domain}`); pending.get('backend_status')([]);
    pending.get('app_version')('0.1.108'); pending.get('docker_status')(null); pending.get('mac_notice_diagnostic')(null);
    pending.get('autostart_enabled')(false); pending.get('agent_cursor_prefs')({enabled: false, idle_hide_secs: 8});
    await tick(); assert.ok(values.includes(`/saved/${domain}`)); assert.ok(values.includes('0.1.108'));
    cleanups.forEach(cleanup => cleanup?.());
  }
});

test('15: you cannot overwrite unknown Mac preferences and can reread them without a mutation', async () => {
  for (const domain of ['Business bookkeeping', 'Family gardening']) for (const invalid of [null, 'offline']) {
    const states = [], effects = [], calls = [], exports = {}; let cursor = 0, valid = false;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/DesktopSettings.tsx`, 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, require(id) {
      if (id.endsWith('/LocalModeBoundary')) return {useLocalMode: () => false};
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial;
        return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
        useEffect(callback, deps) {const index = cursor++; if (!states[index] || deps.some((value, offset) => states[index].deps[offset] !== value)) {
          states[index]?.cleanup?.(); states[index] = {deps}; effects.push(() => {states[index].cleanup = callback();});}}};
      if (id.endsWith('/desktop')) return {isDesktopShell: () => true, desktopInvoke: async () => null,
        desktopInvokeOrThrow: async command => {calls.push(command); if (!valid) {if (invalid === 'offline') throw new Error(domain); return null;}
          return command === 'autostart_enabled' ? false : {enabled: false, idle_hide_secs: 15};}};
      if (id.endsWith('/macNoticeDiagnostic')) return {macNoticeDiagnosticLine: () => 'Notifications'};
      return {};
    }});
    const render = () => {cursor = 0; const tree = exports.DesktopSettings({ventureId: 'ven_domain'}); effects.splice(0).forEach(effect => effect()); return tree;};
    render(); let tree = render(); assert.equal(nodes(tree).some(node => node.props['data-usage-control'] === 'autostart_toggle'), false);
    await tick(); tree = render(); assert.match(JSON.stringify(tree), /Couldn’t read your saved Mac settings/);
    valid = true; nodes(tree).find(node => node.props.children === 'Refresh saved settings').props.onClick(); render(); await tick();
    const toggle = nodes(render()).find(node => node.props['data-usage-control'] === 'autostart_toggle');
    assert.equal(toggle.props.checked, false); assert.equal(toggle.props.disabled, false);
    assert.ok(calls.every(command => ['autostart_enabled', 'agent_cursor_prefs'].includes(command)));
  }
});

test('32: revealing your venture files either opens the confirmed folder or explains an unavailable path', async () => {
  const source = ts.createSourceFile('DesktopSettings.tsx', readFileSync(`${__dirname}/DesktopSettings.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let handler;
  function find(node) {
    if (ts.isJsxAttribute(node) && node.name.getText(source) === 'onClick' && node.getText(source).includes('venture_workspace_path')) handler = node.initializer.expression;
    ts.forEachChild(node, find);
  }
  find(source); assert.ok(handler);
  for (const domain of ['Trade logistics', 'Dog training']) for (const path of [null, '/saved/folder', new Error('offline')]) {
    const calls = [], opened = [], exports = {}; let note = '';
    vm.runInNewContext(ts.transpileModule(`exports.click = (${handler.getText(source)});`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText,
      {exports, local: false, ventureId: domain, dataDir: '', setNote: value => {note = value;}, reveal: async value => opened.push(value),
        desktopInvokeOrThrow: async (command, args) => {calls.push({command, args}); if (path instanceof Error) throw path; return path;}});
    exports.click(); await tick(); assert.equal(calls.length, 1); assert.equal(calls[0].command, 'venture_workspace_path');
    assert.equal(opened.length, path === '/saved/folder' ? 1 : 0);
    if (!opened.length) assert.match(note, /Couldn’t locate your venture files/);
  }
});

test('36: internal Mac helper details are labeled for support and collapsed by default', () => {
  const source = ts.createSourceFile('DesktopSettings.tsx', readFileSync(`${__dirname}/DesktopSettings.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let details;
  function find(node) {if (ts.isJsxElement(node) && node.openingElement.getText(source) === '<details>'
    && node.getText(source).includes('Local helper details for support')) details = node; ts.forEachChild(node, find);}
  find(source); assert.ok(details); const jsx = (type, props) => ({type, props});
  for (const domain of ['B2B dispatch', 'Home gardening']) {
    const exports = {}; vm.runInNewContext(ts.transpileModule(`exports.view = (${details.getText(source)});`, {compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, roles: [{role: domain, port: 3101, healthy: true, owned: false}], require: () => ({jsx, jsxs: jsx})});
    assert.equal(exports.view.type, 'details'); assert.equal(exports.view.props.open, undefined);
    assert.match(JSON.stringify(exports.view), /Local helper details for support/); assert.match(JSON.stringify(exports.view), /Responding/);
    assert.doesNotMatch(JSON.stringify(exports.view), /attached/);
  }
});

test('your local desktop settings never mount hosted account copy or hosted recovery across domains', () => {
  const source = readFileSync(__dirname + '/DesktopSettings.tsx', 'utf8');
  const shell = readFileSync(__dirname + '/../layout/AppShell.tsx', 'utf8');
  for (const domain of ['Invoice planning', 'Garden journals']) {
    assert.match(source, /!local && <DesktopAccountConnection/, domain);
    assert.match(source, /local \? "Your desktop app" : "This Mac app"/, domain);
    assert.match(source, /local \?[^:]*Your work runs on your machine/, domain);
    assert.match(source, /local \?[^:]*Your report was not confirmed/, domain);
    assert.match(source, /local \?[^:]*Start Docker/, domain);
    assert.match(shell, /aria-label=\{local \? "Your settings"/, domain);
  }
});
