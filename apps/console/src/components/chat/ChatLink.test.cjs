const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const ts = require('typescript');

const exportsUnderTest = {};
vm.runInNewContext(ts.transpileModule(fs.readFileSync(__dirname + '/ChatLink.tsx', 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
}).outputText, {exports: exportsUnderTest, require(name) {
  if (name === 'next/link') return 'next-link';
  if (name === '@/lib/chatRouteAction') return {routeInChat: () => false};
  if (name === './PageResultLink') return {PageResultLink: 'page-result', pageResultKind: href =>
    /^\/api\/ventures\/[^/]+\/(?:pagebuilder\/jobs\/[^/]+\/(?:preview|source)(?:\?variant=[0-2])?|device-work-orders\/[^/]+\/source-archive)$/.test(href || '')};
  if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props})};
  return require(name);
}});

test('chat result links use authenticated in-app actions rather than API navigation across domains', () => {
  for (const venture of ['supplier-invoices', 'family-gardens']) {
    for (const endpoint of ['preview?variant=2', 'source?variant=2']) {
      const href = `/api/ventures/${venture}/pagebuilder/jobs/saved/${endpoint}`;
      const link = exportsUnderTest.ChatAnchor({href, target: '_blank', download: true, children: venture, 'data-usage-control': 'website_export_html'});
      assert.equal(link.type, 'page-result');
      assert.equal(link.props.href, href);
      assert.equal(link.props.target, undefined);
      assert.equal(link.props['data-usage-control'], 'website_export_html');
    }
    assert.equal(exportsUnderTest.ChatAnchor({href: `/v/${venture}/research`}).type, 'a');
  }
});
