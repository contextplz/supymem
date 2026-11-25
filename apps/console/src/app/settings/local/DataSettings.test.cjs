const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');
const tick = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };

test('your backups distinguish loading, failure, empty success and requested transfers across domains', async () => {
  for (const domain of ['Freight scheduling', 'Garden journaling']) {
    const states = [], effects = [], exports = {};
    let cursor = 0, started = false, finish;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/DataSettings.tsx`, 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports, fetch: () => new Promise(resolve => {finish = resolve;}), require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id.endsWith('/ErrorNotice')) return {ErrorNotice: 'error-notice'};
      if (id.includes('/desktop')) return {isDesktopShell: () => false};
      if (id === 'react') return {
        useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
        useRef(initial) {const index = cursor++; return states[index] ||= {current: initial};},
        useEffect(callback) {if (!started) effects.push(callback);},
      };
      return {default: {}};
    }});
    const render = () => {cursor = 0; const tree = exports.DataSettings(); if (!started) {started = true; effects.splice(0).forEach(effect => effect());} return tree;};
    render();
    assert.match(JSON.stringify(render()), /Loading your backups/, domain);
    finish(new Response('', {status: 503})); await tick();
    assert.match(JSON.stringify(render()), /could not be loaded/, domain);
    assert.ok(nodes(render()).some(node => node.type === 'error-notice' && /could not be loaded/.test(node.props.error)), domain);
    nodes(render()).find(node => node.props.children === 'Refresh your backups').props.onClick();
    finish(new Response(JSON.stringify({backups: []}))); await tick();
    assert.doesNotMatch(JSON.stringify(render()), /could not be loaded|Loading your backups/);
    assert.ok(nodes(render()).every(node => node.type !== 'error-notice' || !node.props.error), domain);
    assert.match(JSON.stringify(render()), /No backups yet.*Create backup/);
    nodes(render()).find(node => node.props.children === 'Export my data').props.onClick();
    assert.match(JSON.stringify(render()), /Exporting your data/);
    finish(new Response('', {status: 503})); await tick();
  }
});
function nodes(tree) {
  if (!tree || typeof tree !== 'object') return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return [tree, ...nodes(tree.props?.children)];
}

test('your data transfers run once when requested and offer recovery in unrelated domains', async () => {
  for (const domain of ['quartz-dispatch.test', 'puddle-kites.test']) for (const action of ['export', 'import', 'restore', 'backup']) for (const desktop of ['import', 'restore'].includes(action) ? [true, false] : [true]) {
    const states = [], effects = [], calls = [], commands = [], downloads = [];
    let cursor = 0, started = false, fail = false, failRelaunch = false, skipped = false;
    const maintenance = action === 'restore' ? 'Choose one of your listed backups, then retry your restore.'
      : 'Choose your ZIP data export, then retry the import.';
    const exports = {}, jsx = (type, props) => ({ type, props });
    vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/DataSettings.tsx`, 'utf8'), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    }).outputText, { exports, Blob, URL: { createObjectURL: () => 'blob:export', revokeObjectURL() {} },
      window: { location: { reload() { commands.push('reload'); } } },
      document: { createElement: () => ({ click() { downloads.push(domain); } }) }, setTimeout: callback => callback(),
      fetch: async (path, init = {}) => {
        calls.push({ path, init });
        if (path.endsWith('/backups')) return new Response(JSON.stringify({ backups: ['backup-20261106T120000000000Z.tar.gz'] }));
        return new Response(path.endsWith('/export') ? domain : JSON.stringify(fail
          ? { detail: ['restore', 'import'].includes(action) ? maintenance : 'untrusted diagnostic' } : { message: 'Your data is ready.' }), {
          status: fail ? 422 : 200, headers: skipped ? { 'X-Supymem-Skipped-Files': '1' } : {},
        });
      }, require(id) {
        if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
        if (id.endsWith('/ErrorNotice')) return { ErrorNotice: 'error-notice' };
        if (id === 'react') return {
          useState(initial) { const index = cursor++; if (!(index in states)) states[index] = initial;
            return [states[index], value => { states[index] = typeof value === 'function' ? value(states[index]) : value; }]; },
          useRef(initial) { const index = cursor++; return states[index] ||= { current: initial }; },
          useEffect(callback) { if (!started) effects.push(callback); },
        };
        if (id.includes('/desktop')) return { isDesktopShell: () => desktop,
          desktopInvokeOrThrow: async command => { commands.push(command); if (failRelaunch) throw new Error('untrusted diagnostic'); } };
        return { default: {} };
      } });
    const render = () => { cursor = 0; const tree = exports.DataSettings(); if (!started) { started = true; effects.splice(0).forEach(effect => effect()); } return tree; };
    render(); await tick();
    assert.equal(calls.filter(call => !call.path.endsWith('/backups')).length, 0);
    const run = tree => {
      if (action === 'import') {
        const before = calls.length;
        nodes(tree).find(node => node.type === 'input' && node.props.type === 'file').props.onChange({ target: { files: [new Blob([domain], { type: 'application/zip' })], value: domain } });
        assert.equal(calls.length, before, 'Choosing a file must not trigger replacement');
        tree = render();
        assert.match(JSON.stringify(tree), /replaces your current workspace.*does not merge.*backed up first/i);
      }
      nodes(tree).find(node => node.type === 'button' && String(node.props.children).includes(action === 'import' ? 'Replace workspace with this ZIP' : action === 'export' ? 'Export my data' : action === 'backup' ? 'Create backup' : 'Restore')).props.onClick();
    };
    run(render()); run(render()); await tick();
    assert.equal(calls.filter(call => call.path.endsWith(`/${action}`)).length, 1);
    const expectedCommands = desktop && ['import', 'restore'].includes(action) ? ['relaunch_app'] : [];
    assert.deepEqual(commands, expectedCommands);
    if (!desktop) assert.match(JSON.stringify(render()), /Ctrl\+C.*\.\/start --web.*\.\\\\start\.ps1 --web/);
    if (action === 'backup') {
      const call = calls.find(call => call.path.endsWith('/backup'));
      assert.equal(call.init.method, 'POST');
      assert.equal(call.init.credentials, 'same-origin');
      assert.deepEqual(JSON.parse(call.init.body), {});
      assert.equal(calls.filter(call => call.path.endsWith('/backups')).length, 2);
      assert.match(JSON.stringify(render()), /Your backup is ready/);
    }
    assert.equal(downloads.length, action === 'export' ? 1 : 0);
    if (['export', 'backup'].includes(action)) {
      skipped = true; run(render()); await tick();
      assert.match(JSON.stringify(render()), /partial.*1 linked or special file.*regular files.*try again/i, domain);
    }
    fail = true; run(render()); await tick();
    assert.match(JSON.stringify(render()), /try again|retry|create backup again/i);
    const error = nodes(render()).find(node => node.type === 'error-notice' && node.props.intent === 'data.transfer').props.error;
    if (['restore', 'import'].includes(action)) assert.equal(error, maintenance);
    else assert.match(error, /could not/);
    assert.doesNotMatch(JSON.stringify(render()), /Exporting your data|Importing your data|Restoring your backup/);
    assert.deepEqual(commands, expectedCommands);
    fail = false; failRelaunch = true; run(render());
    assert.ok(nodes(render()).every(node => node.type !== 'error-notice' || !node.props.error), `${domain}: ${action}`);
    await tick();
    if (['restore', 'import'].includes(action)) {
      assert.match(JSON.stringify(render()), desktop ? /ready.*Quit and reopen your app/ : /Ctrl\+C.*\.\/start --web/);
      assert.ok(nodes(render()).every(node => node.type !== 'error-notice' || !node.props.error));
    }
  }
  assert.match(readFileSync(`${__dirname}/LocalSettings.tsx`, 'utf8'), /<DataSettings\s*\/>/);
});
