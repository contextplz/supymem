const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test("management copy promises only the hosting controls available after publication", () => {
  const source = readFileSync(join(__dirname, "../../app/v/[ventureId]/site/page.tsx"), "utf8");
  const expression = source.match(/id="site-more"[^\n]*body=\{([^\n]+)\}>/)[1];
  const privateCopy = vm.runInNewContext(expression, { displayedPublished: false });
  const publicCopy = vm.runInNewContext(expression, { displayedPublished: true });
  assert.equal(privateCopy, "Hosting status and offline controls become available after publishing.");
  assert.equal(publicCopy, "Review hosting status or take your landing page offline.");
  assert.doesNotMatch(privateCopy + publicCopy, /audit|history|address|waitlist|versions/);
  assert.match(source, /!displayedPublished && <p[^>]*>Publish a landing page first\./);
  assert.match(source, /<summary>Landing page hosting status<\/summary>/);
  assert.match(source, /<UnpublishSite/);
});

test("your requested unpublish runs directly for the exact live deployment in unrelated domains", async () => {
  const source = readFileSync(join(__dirname, 'UnpublishSite.tsx'), 'utf8');
  for (const domain of ['Factory procurement', 'Family cycling']) {
    const deployment = {id: 'deployment', app_id: 'app', state: 'live', url: domain, version: 1};
    const states = [[deployment], false, '', ''], calls = [], exports = {};
    let cursor = 0;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS,
      target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText, {exports, require(name) {
      if (name === 'react') return {useState: () => [states[cursor++], () => {}], useRef: value => ({current: value === false ? true : value}), useCallback: callback => callback, useEffect() {}};
      if (name === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (name.endsWith('/client')) return {api: async (url, init) => {calls.push({url, init}); return {ok: true, data: init ? deployment : {items: []}};}};
      if (name.endsWith('/ErrorNotice')) return {ErrorNotice: 'error'};
      throw new Error(name);
    }});
    const tree = exports.UnpublishSite({ventureId: 'venture', onOfflineUrls() {}});
    const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value && typeof value === 'object' ? [value, ...nodes(value.props?.children)] : [];
    assert.equal(calls.length, 0);
    nodes(tree).find(node => node.type === 'button' && node.props.children === 'Unpublish landing page').props.onClick();
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls.filter(call => call.init).map(call => [call.url, call.init.method]), [['/api/apps/app/deployments/deployment/unpublish', 'POST']]);
    assert.equal(nodes(tree).some(node => node.props?.role === 'dialog'), false);
  }
});

test("accepted preview errors retry the existing scoped loader without reopening the page", () => {
  const source = readFileSync(join(__dirname, "../../app/v/[ventureId]/site/page.tsx"), "utf8");
  assert.match(source, /disabled=\{acceptedPreviewBusy\} onClick=\{\(\) => void load\(\)\}>Retry preview/);
  assert.doesNotMatch(source, /Reopen this page to try again/);
});
