const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ts = require('typescript');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const load = (file, imports = require) => {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(file, 'utf8'), { compilerOptions: {
    jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022,
  } }).outputText, { exports, require: imports });
  return exports;
};
const progress = load(path.join(__dirname, '../../lib/loopProgress.ts'));
const markdown = load(path.join(__dirname, '../../lib/chatMarkdown.ts'));
const { ProductBuildReview } = load(path.join(__dirname, 'ProductBuildReview.tsx'),
  name => name === '@/lib/loopProgress' ? progress : name === '@/lib/chatMarkdown' ? markdown
    : name === '@/components/chat/ChatLink' ? {ChatAnchor: 'a'} : require(name));
const loop = { provider: 'claude', workspace_slug: 'dinner', profile: 'web_app_vercel_v1',
  phase: 'implement', max_iterations: 6, max_wall_seconds: 2700, per_call_seconds: 720, max_turns: 40 };

test('a checked local app offers preview before its folder, or the browser command', () => {
  for (const domain of ['Warehouse handovers', 'Garden diary']) {
    const props = { local: true, loop, onRevealFolder() {}, scope: { in_scope: [domain], not_in_this_build: [] },
      watch: { state: 'stopped', task_verified: true, evidence: { processes_stopped: true, product_build: { outcome: 'pass' } } } };
    const desktop = renderToStaticMarkup(createElement(ProductBuildReview, { ...props, onRetryPreview() {} }));
    assert.match(desktop, new RegExp(`<button[^>]*>Open preview</button>.*Open result folder.*${domain}`));
    const browser = renderToStaticMarkup(createElement(ProductBuildReview, { ...props, browserPreview: true, onRevealFolder: undefined }));
    assert.match(browser, /<code>PORT=4173 HOST=127\.0\.0\.1 node dev-server\.mjs<\/code>/);
    assert.match(browser, /http:\/\/127\.0\.0\.1:4173/);
    const preview = { status: 'ready', ready: true, url: 'http://127.0.0.1:43210', source_commit: 'a'.repeat(40),
      file_manifest_sha256: 'b'.repeat(64), expires_at: Date.now() / 1000 + 60, stop_available: true };
    const running = renderToStaticMarkup(createElement(ProductBuildReview, { ...props, preview, onStopPreview() {} }));
    assert.match(running, /href="http:\/\/127\.0\.0\.1:43210">Open preview<\/a>.*Stop preview<\/button>/);
  }
});

test('the summary includes recorded test counts and the collapsed spec hides machine checks', () => {
  for (const [domain, count] of [['Vendor onboarding', 3], ['Dog walking planner', 7]]) {
    const html = renderToStaticMarkup(createElement(ProductBuildReview, { local: true, loop,
      watch: { state: 'stopped', task_verified: true, evidence: { processes_stopped: true, product_build: { outcome: 'pass' } } },
      scope: { in_scope: [domain], not_in_this_build: [] }, checksPassed: count,
      plan: { spec: `# ${domain}\nUse \`start\` to begin.\n\n## Independent HTTP journey\n\`\`\`http-journey\n[{"internal":true}]\n\`\`\`\n\`\`\`json\n{"internal_test":true}\n\`\`\`\n## Your data\n<script>no</script>`, tasks: [] },
    }));
    assert.match(html, new RegExp(`Tests passed: ${count}/${count}.*<details><summary>Your product spec</summary>.*<h[1-6]>${domain}</h[1-6]>`));
    assert.match(html, /<code>start<\/code>.*<h[1-6]>Your data<\/h[1-6]>.*&lt;script&gt;/);
    assert.doesNotMatch(html, /Independent HTTP journey|internal_test|&quot;internal&quot;|<script>|```/);
  }
});

test('local saved build guidance returns you to chat instead of a removed screen across domains', () => {
  for (const domain of ['Maintenance logs for factories', 'Dance classes for teenagers']) {
    for (const watch of [{state: 'approved'}, {state: 'stopped', task_verified: true,
      evidence: {processes_stopped: true, product_build: {outcome: 'pass'}}}]) {
      const html = renderToStaticMarkup(createElement(ProductBuildReview, {local: true,
        loop: {...loop, phase: 'plan', workspace_slug: domain}, watch}));
      assert.doesNotMatch(html, /Return to Builds/);
      assert.match(html, /venture chat/);
    }
  }
});

test('landing card follows terminal page state even with a stale order watch', () => {
  for (const domain of ['Invoice matching', 'Meal planning']) {
    const html = renderToStaticMarkup(createElement(ProductBuildReview, {local: true, loop,
      inlineStage: 'Needs you', watch: {state: 'approved'}, scope: {idea_summary: domain}}));
    assert.match(html, /Your build needs another try/);
    assert.doesNotMatch(html, /getting ready/);
    const uncertain = renderToStaticMarkup(createElement(ProductBuildReview, {local: true, loop,
      inlineStage: 'Needs you', watch: {state: 'reconciliation_required'}, scope: {idea_summary: domain}}));
    assert.match(uncertain, /Your tool has not confirmed Stop/);
  }
});

test('landing card shows the running tool time left, not a finished countdown', () => {
  for (const domain of ['Invoice matching', 'Meal planning']) {
    const props = {local: true, loop, inlineStage: 'Codex is building',
      watch: {state: 'running_coding', build_deadline_at: Date.now() / 1000 + 510}, scope: {idea_summary: domain}};
    const html = renderToStaticMarkup(createElement(ProductBuildReview, props));
    assert.match(html, /Time left for your tool: 8m 30s/);
    assert.doesNotMatch(renderToStaticMarkup(createElement(ProductBuildReview, {...props, inlineStage: 'Ready'})), /Time left/);
  }
});

test('inline build review keeps safe result actions and plain stage across domains', () => {
  for (const domain of ['Invoice matching', 'Meal planning']) {
    const html = renderToStaticMarkup(createElement(ProductBuildReview, {local: true, loop, inlineStage: 'Ready',
      watch: {state: 'stopped', task_verified: true, evidence: {processes_stopped: true, product_build: {outcome: 'pass'}}},
      onRevealFolder() {}, previewHref: `/api/ventures/${encodeURIComponent(domain)}/pagebuilder/jobs/build/preview`,
      exportHref: `/api/ventures/${encodeURIComponent(domain)}/pagebuilder/jobs/build/source`}));
    assert.match(html, /Claude Code/); assert.match(html, /Ready/); assert.match(html, /Open preview/);
    assert.match(html, /Open folder/); assert.match(html, /Export/); assert.doesNotMatch(html, /max_iterations|web_app_vercel|Separate build approvals/);
    const unsafe = renderToStaticMarkup(createElement(ProductBuildReview, {local: true, loop, inlineStage: 'Ready',
      previewHref: 'javascript:alert(1)', exportHref: 'https://foreign.example/source'}));
    assert.doesNotMatch(unsafe, /javascript:|foreign\.example/);
  }
});

test('scoped coding work offers the checked files, not an app preview', () => {
  for (const domain of ['Renewal records', 'Pantry inventory']) {
    const props = {local: true, loop: {...loop, profile: 'workspace_utility_v1', phase: 'implement'},
      scope: {in_scope: [domain], not_in_this_build: []}, onRevealFolder() {}};
    const html = renderToStaticMarkup(createElement(ProductBuildReview, {...props,
      watch: {state: 'stopped', task_verified: true, evidence: {processes_stopped: true, outcome: 'pass'}}}));
    assert.match(html, /files passed the workspace checks/);
    assert.match(html, /Open result folder/);
    assert.doesNotMatch(html, /local app preview|try your preview|workspace_utility_v1/);
    const stopped = renderToStaticMarkup(createElement(ProductBuildReview, {...props, inlineStage: 'Stopped',
      watch: {state: 'stopped', evidence: {processes_stopped: true, outcome: 'stopped'}}}));
    assert.match(stopped, /resend your code request/);
  }
});

for (const domain of ['Supplier invoicing', 'Family trail planner']) {
  const renderWatch = (watch, controls = {}) => renderToStaticMarkup(createElement(ProductBuildReview, {
    loop, local: true, watch, ...controls, scope: {in_scope: [domain], not_in_this_build: []},
  }));
  test(`${domain}: Stop stays unconfirmed until the owned processes are verified stopped`, () => {
    for (const state of ['running_coding', 'stopping', 'stopped', 'reconciliation_required']) {
      const html = renderWatch({state, task_verified: true, evidence: {processes_stopped: false}}, {onStopBuild() {}});
      assert.match(html, /Stop|Check Stop/);
      assert.doesNotMatch(html, /Your build is finished|Your coding tool stopped/);
    }
    assert.doesNotMatch(renderWatch({state: 'running_coding'}, {busy: true, onStopBuild() {}}), /disabled=""/);
    assert.match(renderWatch({state: 'reconciliation_required', failure_code: 'coding_sign_in_required',
      evidence: {processes_stopped: false}}, {onStopBuild() {}}), /Check Stop/);
    const html = renderWatch({state: 'stopped', evidence: {processes_stopped: true, outcome: 'stopped'}});
    assert.match(html, /Your coding tool stopped/);
    assert.doesNotMatch(html, /coding loop|approval|credits|checkpoint|—/i);
  });
  test(`${domain}: failed builds offer a working retry and tool setup has a connection link`, () => {
    const html = renderWatch({state: 'stopped', evidence: {processes_stopped: true, outcome: 'tests_failed'}}, {onRetryBuild() {}});
    assert.match(html, /Your build needs another try|Retry build/);
    assert.doesNotMatch(html, /disabled=""/);
    for (const code of ['coding_provider_not_installed', 'coding_sign_in_required', 'device_authentication_required']) {
      const setup = renderWatch({state: 'not_started', failure_code: code});
      assert.match(setup, /Keys and connections|href="\/settings\/local"/);
      assert.doesNotMatch(setup, new RegExp(code));
    }
  });
  test(`${domain}: an inline failed or stopped product build names a reachable retry`, () => {
    for (const state of ['failed', 'cancelled', 'stopped']) {
      const watch = {state, evidence: {processes_stopped: true, outcome: 'stopped'}};
      const inlineStage = state === 'failed' ? 'Needs you' : 'Stopped';
      const reviewHref = `/v/${encodeURIComponent(domain)}/device-work-orders/saved`;
      assert.match(renderWatch(watch, {inlineStage, reviewHref}), /Choose Watch your tool.*Retry build/);
      assert.match(renderWatch(watch, {inlineStage}), /ask Charlie “Build my app with my coding tool”/);
    }
  });
  test(`${domain}: finished builds show your tool and result folder without another approval`, () => {
    for (const provider of ['claude', 'codex', 'cursor', 'opencode']) {
      const html = renderToStaticMarkup(createElement(ProductBuildReview, {local: true,
        loop: {...loop, provider}, watch: {state: 'stopped', task_verified: true,
          evidence: {processes_stopped: true, outcome: 'pass'}}, onRevealFolder() {},
      }));
      assert.match(html, /Your build is finished|Open result folder/);
      assert.match(html, new RegExp({claude: 'Claude Code', codex: 'Codex', cursor: 'Cursor', opencode: 'OpenCode'}[provider]));
      assert.doesNotMatch(html, /coding loop|approval|credits|checkpoint|—|background:#111/i);
    }
  });
}

test('product review makes the three gates and approved limits explicit', () => {
  const html = renderToStaticMarkup(createElement(ProductBuildReview, { loop,
    scope: { in_scope: ['A tip splitter'], not_in_this_build: ['Saved between devices needs a database'] },
    plan: { spec: '<script>private</script> /api/health', tasks: [{ id: 'one', title: 'Split tips',
      acceptance: [{ behaviour: 'A zero tip does not change the total' }] }] },
  }));
  for (const text of ['Build plan', 'Run the coding loop', 'Deploy to your account', 'Up to 6 tries',
    'at most 45 minutes', 'no internet access', 'separate coding review',
    'Saved between devices needs a database', 'A zero tip']) {
    assert.ok(html.includes(text), text);
  }
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;/);
  assert.equal(renderToStaticMarkup(createElement(ProductBuildReview, { loop: { ...loop, profile: 'static_html_v1' } })), '');
  assert.match(renderToStaticMarkup(createElement(ProductBuildReview, { loop: { ...loop, phase: 'plan' } })), /does not start coding/);
});

test('deploy review names the account effect, separate production gate and protected outcome', () => {
  const html = renderToStaticMarkup(createElement(ProductBuildReview, {
    deploy: { provider: 'vercel', target: 'preview', workspace_slug: 'dinner' },
    receipt: { target: 'preview', status: 'protected', url: 'https://dinner.vercel.app' },
  }));
  for (const text of ['your Vercel account', 'may create a project called dinner', 'does not grant permission',
    'environment files are excluded', 'protected by your Vercel settings', 'vercel login']) {
    assert.ok(html.includes(text), text);
  }
  assert.match(html, /href="https:\/\/dinner.vercel.app"/);
  const failed = renderToStaticMarkup(createElement(ProductBuildReview, {
    deploy: { provider: 'vercel', target: 'production', workspace_slug: 'dinner' },
    receipt: { status: 'failed', url: 'javascript:alert(1)' },
  }));
  assert.doesNotMatch(failed, /href=/);
  assert.match(failed, /not verified/);
});

for (const domain of [
  { title: 'Track outstanding invoices for bookkeepers', slug: 'accounting' },
  { title: 'Pack for a trail walk with friends', slug: 'trail-plans' },
]) {
  const preview = { status: 'ready', ready: true, url: 'http://127.0.0.1:43127',
    source_commit: 'a'.repeat(40), file_manifest_sha256: 'b'.repeat(64),
    expires_at: Date.now() / 1000 + 600, stop_available: true };
  test(`${domain.slug}: local preview is bound, stoppable and never grants deployment`, () => {
    const html = renderToStaticMarkup(createElement(ProductBuildReview, {
      loop: { ...loop, workspace_slug: domain.slug }, scope: { in_scope: [domain.title], not_in_this_build: [] },
      preview, onStopPreview: () => {},
    }));
    assert.match(html, /href="http:\/\/127\.0\.0\.1:43127"/);
    assert.match(html, /Stop preview<\/button>/);
    assert.doesNotMatch(html, /disabled=""/);
    assert.match(html, /This does not deploy it/);
    assert.match(html, /review any Vercel deployment separately/);
    assert.match(html, /Saved version identifier/);
    assert.ok(html.includes('b'.repeat(64)));
    assert.doesNotMatch(html, /—/);
  });

  test(`${domain.slug}: missing, failed, expired and stopped previews have no live link`, () => {
    for (const [status, message] of [
      ['unavailable', 'not available'], ['failed', 'could not start'],
      ['expired', 'has expired'], ['stopped', 'You stopped your preview'],
    ]) {
      const html = renderToStaticMarkup(createElement(ProductBuildReview, {
        loop: { ...loop, workspace_slug: domain.slug }, preview: { ...preview, status, ready: false, expires_at: 1, stop_available: false },
        onRetryPreview: () => {},
      }));
      assert.ok(html.includes(message), status);
      assert.doesNotMatch(html, /href="http:/);
      assert.match(html, /Try your preview again/);
      assert.doesNotMatch(html, /Stop preview/);
    }
    const unavailable = renderToStaticMarkup(createElement(ProductBuildReview, { loop }));
    assert.match(unavailable, /local preview is not available/);
  });

  test(`${domain.slug}: unconfirmed cleanup keeps Stop available without a live link`, () => {
    const html = renderToStaticMarkup(createElement(ProductBuildReview, { loop,
      preview: { ...preview, status: 'failed', ready: false },
      onStopPreview: () => {}, onRetryPreview: () => {},
    }));
    assert.match(html, /has not stopped cleanly/);
    assert.match(html, /Stop preview<\/button>/);
    assert.doesNotMatch(html, /disabled=""|Open your local app|Try your preview again|href="http:/);
  });

  test(`${domain.slug}: stale or unsafe preview URLs cannot look ready`, () => {
    for (const unsafe of [
      { url: 'http://localhost:43127' }, { url: 'http://127.0.0.1:65536' },
      { url: 'http://127.0.0.1:43127@remote.invalid' }, { url: 'https://remote.invalid' },
      { source_commit: '' }, { file_manifest_sha256: '' }, { expires_at: 1 },
      { expires_at: NaN }, { expires_at: undefined }, { stop_available: false },
    ]) {
      const html = renderToStaticMarkup(createElement(ProductBuildReview, { loop, preview: { ...preview, ...unsafe } }));
      assert.doesNotMatch(html, /Open your local app/);
      assert.doesNotMatch(html, /href="http:/);
      assert.doesNotMatch(html, /saved app is ready/);
    }
  });

  test(`${domain.slug}: deployment availability does not imply checked behavior`, () => {
    for (const [status, functional_status, expected] of [
      ['protected', 'verified', 'does not confirm your app works'],
      ['deployed', undefined, 'behavior has not been checked'],
      ['deployed', 'failed', 'did not return the results'],
      ['deployed', 'not_verified_on_live', 'not verified on the live deployment'],
      ['deployed', 'verified', 'returned the results in your approved plan'],
    ]) {
      const html = renderToStaticMarkup(createElement(ProductBuildReview, {
        deploy: { provider: 'vercel', target: 'preview', workspace_slug: domain.slug },
        receipt: { status, functional_status },
      }));
      assert.ok(html.includes(expected), `${status} ${functional_status}`);
    }
  });
}
