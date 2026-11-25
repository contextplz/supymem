const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');
const {createElement} = require('react');
const {renderToStaticMarkup} = require('react-dom/server');
function load(file, imports = require) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports, require: imports});
  return exports;
}
const progress = load(path.join(__dirname, '../../lib/loopProgress.ts'));
const control = load(path.join(__dirname, '../../lib/codingControl.ts'), name =>
  name.includes('loopProgress') ? progress : require(name));
const {LoopProgress} = load(path.join(__dirname, 'LoopProgress.tsx'), name =>
  name === '@/lib/loopProgress' ? progress : require(name));

test('build activity collapses files, tries, and the complete plain-language log in order', () => {
  for (const domain of ['Warehouse handovers', 'Garden diary']) {
    const events = Array.from({length: 52}, (_, at) => ({at, text: `Running: /bin/sh -lc 'record ${domain} ${at}'`}));
    events[0].text = `Started building ${domain}.`;
    const html = renderToStaticMarkup(createElement(LoopProgress, {maximum: 3, collapsed: true, events,
      files: [{path: 'index.html'}], iterations: [{index: 0, phase: 'code', outcome: 'pass'}]}));
    assert.deepEqual([...html.matchAll(/<details><summary>(.*?)<\/summary>/g)].map(match => match[1]),
      ['Files changed', 'Saved tries', 'Full activity log']);
    assert.match(html, new RegExp(`Started building ${domain}`));
    assert.equal((html.match(/Your tool ran a command/g) || []).length, 51);
    assert.doesNotMatch(html, /Running:|\/bin\/sh| -lc /);
  }
});

test('inline activity shows only the last five scrubbed lines across domains', () => {
  for (const domain of ['Supplier invoices', 'Family recipes']) {
    const html = renderToStaticMarkup(createElement(LoopProgress, {maximum: 2, compact: true,
      events: Array.from({length: 8}, (_, index) => ({at: index, text: index === 7 ? 'api_key=private-fixture' : `${domain} update ${index}`}))}));
    assert.doesNotMatch(html, /update [012]|private-fixture|Files changed/);
    assert.match(html, /update 3/); assert.match(html, /private detail was removed/);
  }
});

test('page checks and saved failures use plain words in both activity views', () => {
  for (const domain of ['Supplier invoices', 'Family recipes']) {
    for (const compact of [false, true]) {
      const html = renderToStaticMarkup(createElement(LoopProgress, {maximum: 2, compact,
        events: [{at: 1, text: `${domain}: pagebuilder_plan_copy_rejected:${JSON.stringify([
          {field_id: 'sections[0].body[0]', reason: 'Your offer contains an unsupported claim.'}])}`}],
        iterations: [{index: 0, phase: 'code', outcome: 'tests_failed',
          summary: 'Your page needs a fix. ' + domain.repeat(15).slice(0, 190) + '. On Website, choose Retry.',
          diagnosis: {failures: ['required_text_missing:Your signup label is missing.', 'password=private-fixture']}}]}));
      assert.doesNotMatch(html, /pagebuilder_|required_text_missing|private-fixture|field_id|sections\[0\]/);
      assert.match(html, /unsupported claim/);
      assert.match(html, /Your signup label is missing/);
      assert.match(html, /On Website, choose Retry/);
    }
  }
});
const domains = ['Invoice reconciliation', 'Trail packing'];

test('your page activity hides truncated checker fields and names the heading repair', () => {
  for (const domain of domains) {
    for (const text of [
      `${domain}: pagebuilder_plan_copy_rejected:[{"field_id":"brief.promise","reason":"Unsupported claim`,
      `${domain}: Your page check: sections.0.headline: String should have at most 140 characters`,
      `${domain}: honest_copy:reviewed_body_missing:hero`,
    ]) {
      const message = progress.safeLoopText(text, 500);
      assert.doesNotMatch(message, /field_id|brief\.promise|sections\.0|reviewed_body_missing|honest_copy|[{}\[\]]/);
      assert.match(message, /Website/);
      assert.match(message, /Retry/);
      if (text.includes('140')) assert.match(message, /heading.*140/);
    }
  }
});

test('your activity and tool transcript scrub credentials and terminal escapes across domains', () => {
  const pem = ['-----BEGIN', 'PRIVATE KEY-----\nprivate-fixture\n-----END', 'PRIVATE KEY-----'].join(' ');
  for (const domain of domains) {
    for (const text of ['{"password":"private-fixture"}', 'Authorization: Basic cHJpdmF0ZQ==',
      'curl --api-key private-fixture', pem]) {
      const html = renderToStaticMarkup(createElement(LoopProgress, {
        maximum: 6, events: [{at: 1, text: `${domain}: ${text}`}],
      }));
      assert.doesNotMatch(html, /private-fixture|cHJpdmF0ZQ/);
      for (const provider of ['claude', 'codex', 'cursor', 'opencode']) {
        const parsed = control.parseControlLine(provider, JSON.stringify({type: 'assistant',
          message: {content: [{type: 'text', text: `${domain}: ${text}`}]}}));
        assert.doesNotMatch(JSON.stringify(parsed.items), /private-fixture|cHJpdmF0ZQ/);
        const raw = control.parseControlLine(provider, `${domain}: ${text}`);
        assert.doesNotMatch(JSON.stringify(raw.items), /private-fixture|cHJpdmF0ZQ/);
      }
    }
    assert.equal(progress.safeLoopText('\u001b[31mnpm test\u001b[0m'), 'npm test');
    for (const file of ['file:///Users/demo-owner/app.js', 'file:///C:/Users/demo-owner/app.js', '/var/tmp/private/app.js']) {
      assert.equal(progress.safeLoopText(`Editing ${file}`), 'Editing a local file');
    }
    assert.equal(progress.safeLoopText(`Editing tests/${domain.replaceAll(' ', '-').toLowerCase()}.test.js`),
      `Editing tests/${domain.replaceAll(' ', '-').toLowerCase()}.test.js`);
    for (const provider of ['claude', 'codex', 'cursor', 'opencode']) {
      assert.doesNotThrow(() => control.parseControlLine(provider, 'null'));
      assert.doesNotMatch(JSON.stringify(control.parseControlChunk(provider, 'token=\nprivate-fixture').items), /private-fixture/);
    }
    assert.match(JSON.stringify(control.parseControlLine('claude', JSON.stringify({type: 'assistant', message: {
      content: [{type: 'tool_use', name: 'Bash', input: {command: 'npm test'}}],
    }})).items), /npm test/);
  }
});

test('your changed files use bounded project-relative paths and distinguish missing evidence', () => {
  for (const domain of domains) {
    const html = renderToStaticMarkup(createElement(LoopProgress, {maximum: 6,
      files: [{path: 'src/main.js'}, {path: 'src/home/main.js'}, {path: 'old.js', deleted: true}, {path: '../private-fixture'},
        {path: '/Users/demo-owner'}, {path: '.env'}, {path: 'secrets.json'}],
      events: [{at: 1, text: `${domain}: npm test`}],
    }));
    for (const text of [/Files changed/, /src\/main.js/, /src\/home\/main.js/, /old.js.*removed/, /npm test/]) assert.match(html, text);
    assert.doesNotMatch(html, /private-fixture|\.env|secrets.json/);
    assert.match(renderToStaticMarkup(createElement(LoopProgress, {maximum: 6})), /not reported.*files/i);
  }
});

test('you see scrubbed commands from each coding tool without exposing its permission details', () => {
  for (const domain of domains) {
    for (const command of [`node tests/${domain.replaceAll(' ', '-').toLowerCase()}.test.js`, 'curl --api-key private-fixture']) {
      const records = [
        ['claude', {type: 'assistant', message: {content: [{type: 'tool_use', name: 'Bash', input: {command}}]}}],
        ['codex', {type: 'item.started', item: {type: 'command_execution', command}}],
        ['cursor', {method: 'session/update', params: {update: {sessionUpdate: 'tool_call', title: command}}}],
        ['opencode', {type: 'tool_use', sessionID: 'session-fixture', part: {type: 'tool', tool: 'bash', state: {status: 'running', input: {command}}}}],
      ];
      for (const [provider, record] of records) {
        const parsed = control.parseControlLine(provider, JSON.stringify(record));
        assert.equal(parsed.items.length, 1, provider);
        assert.doesNotMatch(JSON.stringify(parsed.items), /private-fixture/);
        if (command.startsWith('node')) assert.equal(parsed.items[0].text, command);
      }
    }
  }
  const permission = control.parseControlLine('cursor', JSON.stringify({method: 'session/request_permission', id: 1,
    params: {title: 'password=private-fixture'}}));
  assert.doesNotMatch(JSON.stringify(permission), /private-fixture/);
});
