const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function compile(filename, imports = require) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
  }).outputText, {exports, require: imports, URL, URLSearchParams});
  return exports;
}
const preview = compile(path.join(__dirname, '../../lib/firstlookPreview.ts'));
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];

function harness(href) {
  let cursor = 0, success = true, opened = 0;
  const hooks = [], downloads = [];
  const module = compile(path.join(__dirname, 'PageResultLink.tsx'), name => {
    if (name === 'react') return {
      useState(initial) {const index = cursor++; if (!(index in hooks)) hooks[index] = initial;
        return [hooks[index], value => {hooks[index] = value;}];},
      useRef() {const index = cursor++; return hooks[index] ||= {current: {showModal() {opened++;}}};},
    };
    if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
    if (name === '@/lib/client') return {downloadFile: async (...args) => {downloads.push(args); return success;}};
    if (name === '@/lib/firstlookPreview') return preview;
    if (name === '@/components/cockpit/LandingPreviewFrame') return {LandingPreviewFrame: 'preview-frame'};
    if (name === '@/components/primitives/ErrorNotice') return {ErrorNotice: 'error-notice'};
    if (name.endsWith('.css')) return {default: {result: 'result', preview: 'preview', previewFrame: 'previewFrame'}};
    return require(name);
  });
  return {render() {cursor = 0; return module.PageResultLink({href});}, downloads,
    fail() {success = false;}, get opened() {return opened;}, kind: module.pageResultKind};
}

test('preview opens in the app through the exact private document endpoint across domains', async () => {
  for (const venture of ['retail-orders', 'swimming-lessons']) {
    const href = `/api/ventures/${venture}/pagebuilder/jobs/saved/preview?variant=2`;
    const view = harness(href);
    assert.equal(nodes(view.render()).some(node => node.type === 'preview-frame'), false, 'No preview fetch before you ask');
    await nodes(view.render()).find(node => node.type === 'button').props.onClick();
    assert.equal(view.opened, 1);
    const frame = nodes(view.render()).find(node => node.type === 'preview-frame');
    assert.equal(preview.firstlookPreviewPath(frame.props.src), href.replace('/preview?', '/preview-document?'));
    assert.equal(frame.props.sandbox, '');
    assert.equal(nodes(view.render()).some(node => node.type === 'a'), false);
  }
});

test('the preview frame fills a viewport-sized modal below its close control across domains', async () => {
  const css = fs.readFileSync(path.join(__dirname, 'PageResultLink.module.css'), 'utf8');
  for (const venture of ['freight-dispatch', 'home-baking']) {
    const view = harness(`/api/ventures/${venture}/pagebuilder/jobs/saved/preview`);
    await nodes(view.render()).find(node => node.type === 'button').props.onClick();
    const dialog = nodes(view.render()).find(node => node.type === 'dialog');
    const [form, frame] = dialog.props.children;
    assert.match(css, /\.preview\s*\{[^}]*;\s*height:\s*min\(90vh,/);
    assert.match(css, /\.preview\[open\]\s*\{[^}]*display:\s*flex;[^}]*flex-direction:\s*column/);
    assert.equal(frame.props.className, 'previewFrame');
    assert.match(css, /\.previewFrame\s*\{[^}]*flex:\s*1\s+1\s+0/);
    assert.match(css, /\.previewFrame\s*>\s*iframe\s*\{[^}]*flex:\s*1\s+1\s+0;[^}]*;\s*height:\s*100%/);
    assert.equal(view.opened, 1);
    assert.equal(form.props.method, 'dialog');
    assert.equal(form.props.children.props.children, 'Close preview');
  }
});

test('exports use authenticated download and failures give a retry across domains', async () => {
  for (const venture of ['retail-orders', 'swimming-lessons']) {
    for (const suffix of ['pagebuilder/jobs/saved/source?variant=1', 'device-work-orders/saved/source-archive']) {
      const href = `/api/ventures/${venture}/${suffix}`, view = harness(href);
      await nodes(view.render()).find(node => node.type === 'button').props.onClick();
      assert.deepEqual(view.downloads[0], [href, suffix.startsWith('pagebuilder') ? 'landing-page.zip' : 'build-source.zip']);
      assert.equal(nodes(view.render()).some(node => node.type === 'a'), false);
      view.fail();
      await nodes(view.render()).find(node => node.type === 'button').props.onClick();
      const notice = nodes(view.render()).find(node => node.type === 'error-notice');
      assert.equal(notice.props.source, 'chat.page_export');
      assert.match(notice.props.error, /Choose Export to try again/);
    }
  }
});

test('result actions reject foreign, malformed and executable links', () => {
  for (const href of ['javascript:alert(1)', 'https://other.example/api/ventures/v/pagebuilder/jobs/j/preview',
    '/api/ventures/v/pagebuilder/jobs/j/preview?variant=3', '/api/ventures/v/pagebuilder/jobs/j/source?redirect=remote',
    '/api/ventures/v/pagebuilder/jobs/j/source#remote', '/w/pagebuilder/site/']) {
    const view = harness(href);
    assert.equal(view.kind(href), null);
    assert.equal(view.render(), null);
  }
  const css = fs.readFileSync(path.join(__dirname, 'PageResultLink.module.css'), 'utf8');
  assert.match(css, /gap:\s*8px/);
});
