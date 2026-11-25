const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../../src-tauri/splashscreen/update.html'), 'utf8');
const script = html.match(/<script>([\s\S]*?)<\/script>/)[1];
const flush = async () => { await new Promise(setImmediate); await new Promise(setImmediate); };
function dialog(invoke, flags = {}) {
  const elements = new Map();
  const calls = [];
  const document = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { textContent: '', hidden: false, disabled: false, removeAttribute(key) { delete this[key]; } });
    return elements.get(id);
  } };
  const events = new Map();
  const api = {
    core: { invoke: async (cmd, args) => { calls.push({ cmd, args }); return invoke(cmd, args); } },
    event: { listen: async (name, handler) => { events.set(name, handler); return () => events.delete(name); } },
    window: { getCurrentWindow: () => ({ close: async () => calls.push({ cmd: 'window.close' }) }) },
  };
  vm.runInNewContext(script, { window: { __TAURI__: api, addEventListener() {}, ...flags }, document,
    setTimeout() { throw new Error('Updater must not start an automatic retry timer'); } });
  return { element: id => document.getElementById(id), calls, events };
}
const update = { available: true, version: '0.1.32', current_version: '0.1.31', check_id: 'native-receipt', force: false };

test('local builds without a release channel explain why updates are off', async () => {
  for (const domain of ['warehouse.example', 'cooking.example']) {
    const honesty = `Update checks are off for your build of ${domain}. Ask for a newer installer.`;
    const ui = dialog(async () => ({ available: false, honesty }), {
      __SUPYMEM_LOCAL_UPDATES__: true, __SUPYMEM_UPDATE_REPO_CONFIGURED__: false,
    });
    await flush();
    assert.equal(ui.calls.length, 1);
    assert.equal(ui.calls[0].cmd, 'check_for_update');
    assert.equal(ui.calls[0].args.allowGithub, false);
    assert.equal(ui.element('status').textContent, honesty);
    assert.equal(ui.element('close').textContent, 'Close');
  }
});

test('unsigned local releases offer a link without installing', async () => {
  for (const domain of ['Warehouse inventory', 'Home cooking']) {
    const ui = dialog(async () => ({ ...update, check_id: null, notes: domain,
      honesty: 'A new version is available. Open the release page to install it.',
      release_url: 'https://github.com/example/project/releases/tag/v1.0.1' }));
    await flush();
    assert.equal(ui.element('install').hidden, true);
    assert.equal(ui.element('release').hidden, false);
    assert.equal(ui.element('release').href, 'https://github.com/example/project/releases/tag/v1.0.1');
    assert.match(ui.element('status').textContent, /A new version is available/);
    assert.match(ui.element('assurance').textContent, /install.*yourself|manually/);
    assert.equal(ui.calls.filter(call => call.cmd === 'install_update').length, 0);
  }
});

test('failed check is visible, bounded, and resumes only on a manual check', async () => {
  let checks = 0;
  const ui = dialog(async cmd => {
    if (cmd === 'check_for_update') {
      checks++;
      if (checks === 1) throw new Error('Connection unavailable');
      return { available: false, honesty: 'No published release on this channel.' };
    }
  });
  await flush();
  assert.equal(checks, 1);
  assert.equal(ui.element('error').textContent, 'Connection unavailable');
  assert.equal(ui.element('check').disabled, false);
  await ui.element('check').onclick();
  assert.equal(checks, 2);
  assert.equal(ui.element('status').textContent, 'No published release on this channel.');
  assert.equal(ui.element('title').textContent, 'Update check unavailable');
});

test('verified channel with no newer release reports up to date', async () => {
  const ui = dialog(async () => ({
    available: false,
    channel_verified: true,
    honesty: "You're on 0.1.38. No newer signed release.",
  }));
  await flush();
  assert.equal(ui.element('title').textContent, 'Supymem is up to date');
  assert.match(ui.element('status').textContent, /No newer signed release/);
});

test('duplicate install clicks use exactly one confirmed receipt and preserve failure', async () => {
  let reject;
  const ui = dialog(async cmd => {
    if (cmd === 'check_for_update') return update;
    if (cmd === 'install_update') return new Promise((_, no) => { reject = no; });
  });
  await flush();
  const install = ui.element('install').onclick();
  await flush();
  await ui.element('install').onclick();
  const installs = ui.calls.filter(call => call.cmd === 'install_update');
  assert.equal(installs.length, 1);
  assert.equal(installs[0].args.checkId, 'native-receipt');
  reject(new Error('Signature verification failed. Nothing was installed.'));
  await install;
  assert.equal(ui.element('error').hidden, false);
  assert.match(ui.element('error').textContent, /Nothing was installed/);
  assert.equal(ui.element('install').disabled, false);
  assert.equal(ui.calls.filter(call => call.cmd === 'check_for_update').length, 1);
});

test('required policy hides close and progress comes from native download events', async () => {
  const ui = dialog(async () => ({ ...update, force: true }));
  await flush();
  assert.equal(ui.element('close').hidden, true);
  assert.equal(ui.element('title').textContent, 'Update required');
  ui.events.get('supymem-update-progress')({ payload: { phase: 'downloading', downloaded: 2097152, total: 4194304 } });
  assert.match(ui.element('status').textContent, /2 MB received/);
  assert.equal(ui.element('progress').value, 2097152);
  ui.events.get('supymem-update-progress')({ payload: { phase: 'installing', downloaded: 4194304, total: 4194304 } });
  assert.match(ui.element('status').textContent, /Signature verified/);
});
