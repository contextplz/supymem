const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('you can download your draft as Markdown in unrelated domains without a publishing connection', () => {
  const source = ts.createSourceFile('ContentStudio.tsx', readFileSync(__dirname + '/ContentStudio.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'downloadPost') method = node.getText(source); ts.forEachChild(node, visit); }
  visit(source);
  assert.ok(method, 'downloadPost exists');
  for (const domain of ['Factory dispatch', 'Family gardening']) {
    let downloaded, content, revoked, removed, error;
    const anchor = {click() {downloaded = this.download;}, remove() {removed = true;}};
    const context = {Blob: class {constructor(parts) {content = parts.join('');}}, setError: value => {error = value;},
      URL: {createObjectURL: () => 'blob:local-draft', revokeObjectURL: value => {revoked = value;}},
      document: {createElement: () => anchor, body: {appendChild() {}}}, window: {setTimeout: callback => callback()}};
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.run = downloadPost;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    context.run({id: 'post', title: domain, body: 'Your draft'});
    assert.equal(downloaded, 'post.md');
    assert.equal(content, `# ${domain}\n\nYour draft\n`);
    assert.equal(revoked, 'blob:local-draft');
    assert.equal(removed, true);
    context.run({id: 'post', title: null, body: domain});
    assert.equal(content, `${domain}\n`);
    context.run({id: 'post', body: 'Your draft', handoff: {markdown: `## ${domain}\n`, filename: 'saved-draft.md'}});
    assert.equal(downloaded, 'saved-draft.md');
    assert.equal(content, `## ${domain}\n`);
    anchor.click = () => {throw new Error('download unavailable');};
    context.run({id: 'post', body: domain});
    assert.match(error, /Copy draft in Content/);
    assert.equal(revoked, 'blob:local-draft');
    assert.equal(removed, true);
  }
});

test('your local content errors name the feature and model settings across unrelated domains', () => {
  const context = {exports: {}, Error, require: name => {
    assert.ok(['./localMode.ts', './credits.ts'].includes(name));
    return name === './localMode.ts' ? {isLocalConsole: () => true} : {usageLimitMessage: () => 'Buy more usage.'};
  }};
  const source = readFileSync(__dirname + '/../../lib/errorCopy.ts', 'utf8');
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022}}).outputText, context);
  for (const domain of ['warehouse-inspections', 'home-fitness']) {
    assert.match(context.exports.opsErrorMessage(new Error('API 409: content_disabled'), domain), /Content in Capabilities/);
    for (const code of ['content_provider_unavailable', 'content_provider_offline'])
      assert.match(context.exports.opsErrorMessage(new Error(`API 409: ${code}`), domain), /provider, key and chat model in Keys and connections/);
  }
});

test('your requested channel disconnection runs without a second confirmation in unrelated domains', async () => {
  const source = ts.createSourceFile('ContentStudio.tsx', readFileSync(__dirname + '/ContentStudio.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  let method;
  function visit(node) { if (ts.isFunctionDeclaration(node) && node.name?.text === 'forgetAll') method = node.getText(source); ts.forEachChild(node, visit); }
  visit(source);
  for (const domain of ['Factory dispatch', 'Family gardening']) {
    const calls = [], context = {window: {confirm: () => false}, setBusy() {}, setError() {}, setSaved() {},
      forgetContentAccounts: async () => {calls.push(domain); return {};}, refresh: async () => {}};
    vm.runInNewContext(ts.transpileModule(method + ';globalThis.run = forgetAll;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run();
    assert.deepEqual(calls, [domain]);
  }
});

test('your posting controls stay hidden locally while hosted posting keeps its existing checks', () => {
  const source = ts.createSourceFile('ContentStudio.tsx', readFileSync(__dirname + '/ContentStudio.tsx', 'utf8'), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const conditions = [];
  function visit(node) {
    if (ts.isJsxExpression(node) && ts.isBinaryExpression(node.expression)) {
      const body = node.expression.right.getText(source);
      if (body.includes('<h2>3. Posting channels</h2>') || body.includes('Review for publish') || body.includes('className={styles.externalReview}')) conditions.push(node.expression.left.getText(source));
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  assert.equal(conditions.length, 3);
  for (const domain of ['Factory dispatch', 'Family gardening']) for (const local of [true, false]) {
    for (const condition of conditions) assert.equal(Boolean(vm.runInNewContext(condition, {posting: !local,
      post: {id: 'saved', state: 'scheduled'}, accounts: [], publishOffer: () => ({canPublish: true}), externalReviews: {saved: {}}})), !local, domain);
  }
});

test('your requested publication records the exact approval and publishes without another click in unrelated domains', async () => {
  const source = ts.createSourceFile('ContentStudio.tsx', readFileSync(__dirname + '/ContentStudio.tsx', 'utf8'), ts.ScriptTarget.Latest, true);
  const methods = [];
  function visit(node) { if (ts.isFunctionDeclaration(node) && ['decidePostReview', 'executePostReview'].includes(node.name?.text)) methods.push(node.getText(source)); ts.forEachChild(node, visit); }
  visit(source);
  for (const domain of ['Factory dispatch', 'Family gardening']) {
    const calls = [], context = {externalReviews: {post: {approval: {id: 'exact-approval', status: 'pending'}, review: {body: domain}}},
      setBusy() {}, setError() {}, setExternalReviews() {}, opsErrorMessage: error => String(error),
      opsApi: {post: async (path, body) => {calls.push({path, body}); return path.endsWith('/approval') ? {id: 'exact-approval', status: 'approved'} : {ok: true};}}};
    vm.runInNewContext(ts.transpileModule(methods.join('\n') + ';globalThis.run = decidePostReview;', {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
    await context.run('post', 'approved');
    assert.deepEqual(calls.map(call => call.path), ['/content/posts/post/approval', '/content/posts/post/execute']);
    assert.equal(calls[1].body.approval_id, 'exact-approval');
    calls.length = 0; await context.run('post', 'rejected');
    assert.deepEqual(calls.map(call => call.path), ['/content/posts/post/approval']);
  }
});
