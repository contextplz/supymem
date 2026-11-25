const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const ts = require('typescript');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');

const source = fs.readFileSync(path.join(__dirname, 'BrowserReadUrlsReview.tsx'), 'utf8');
const exportsObject = {};
vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
  jsx: ts.JsxEmit.ReactJSX, module: ts.ModuleKind.CommonJS,
} }).outputText, { exports: exportsObject, require });
const Review = exportsObject.BrowserReadUrlsReview;
const urls = ['https://pypi.org/project/requests/', 'https://pypi.org/project/httpx/',
  'https://pypi.org/project/aiohttp/', 'https://github.com/psf/requests',
  'https://github.com/encode/httpx', 'https://github.com/aio-libs/aiohttp'];

test('normal approval review shows all exact pages literally before acknowledgement', () => {
  const html = renderToStaticMarkup(createElement(Review, { urls }));
  assert.match(html, /Exact pages Supymem may read/);
  for (const url of urls) assert.ok(html.includes(`<li>${url}</li>`), url);
  assert.match(html, /No input access is granted/);
  assert.doesNotMatch(html, /<a\b/);
  const page = fs.readFileSync(path.join(__dirname, '../../app/v/[ventureId]/device-work-orders/[orderId]/page.tsx'), 'utf8');
  const reviewPosition = page.indexOf('<BrowserReadUrlsReview urls={order.manifest.general_browser_verification.read_urls}');
  assert.ok(reviewPosition >= 0 && reviewPosition < page.indexOf('I reviewed this task'));
});

test('legacy absent or empty list stays absent and URL text is escaped', () => {
  assert.equal(renderToStaticMarkup(createElement(Review)), '');
  assert.equal(renderToStaticMarkup(createElement(Review, { urls: [] })), '');
  const html = renderToStaticMarkup(createElement(Review, { urls: ['<script>private</script>'] }));
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /&lt;script&gt;private&lt;\/script&gt;/);
});
