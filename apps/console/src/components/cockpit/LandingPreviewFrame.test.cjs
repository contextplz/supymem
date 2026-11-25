const assert = require('node:assert/strict');
const test = require('node:test');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

function compile(filename, imports) {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
  }).outputText, {exports, require: imports, URL, URLSearchParams, AbortController, window: {
    setTimeout() {}, clearTimeout() {}, addEventListener() {}, removeEventListener() {},
  }});
  return exports;
}
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];

test('private page preview fetches with the owner session and rejects a retired response across domains', async () => {
  const domains = [['procurement', 'Supplier terms'], ['music-lessons', 'Learn guitar']];
  for (const [venture, title, retired] of domains.flatMap(([venture, title]) => [false, true].map(retired => [venture, title, retired]))) {
    let cursor = 0, epoch = 1, resolve;
    const hooks = [], effects = [], requests = [];
    const preview = compile(path.join(__dirname, '../../lib/firstlookPreview.ts'), require);
    const module = compile(path.join(__dirname, 'LandingPreviewFrame.tsx'), name => {
      if (name === 'react') return {
        useState(initial) {const index = cursor++; if (!(index in hooks)) hooks[index] = typeof initial === 'function' ? initial() : initial;
          return [hooks[index], value => {hooks[index] = value;}];},
        useRef(initial) {const index = cursor++; return hooks[index] ||= {current: initial};},
        useEffect(effect) {const index = cursor++; if (!(index in hooks)) {hooks[index] = true; effects.push(effect);}},
      };
      if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
      if (name === '@/lib/client') return {verifiedSessionSubject: () => 'owner', verifiedSessionEpoch: () => epoch,
        api: (request, init) => {requests.push({request, init}); return new Promise(done => {resolve = done;});}};
      if (name === '@/lib/firstlookPreview') return preview;
      if (name === '@/lib/localMode') return {isLocalConsole: () => true};
      if (name === '@/lib/previewNavigation') return {openablePreviewUrl: () => null};
      if (name === '@/components/primitives/ErrorNotice') return {ErrorNotice: 'error-notice'};
      if (name.endsWith('.css')) return {default: {}};
      return require(name);
    });
    const src = `/api/ventures/${venture}/pagebuilder/jobs/saved/preview?variant=1`;
    const render = () => {cursor = 0; return module.LandingPreviewFrame({src, title});};
    assert.equal(nodes(render()).some(node => node.type === 'iframe'), false);
    effects.splice(0).forEach(effect => effect());
    assert.equal(requests[0].request, src.replace('/preview?', '/preview-document?'));
    assert.equal(requests[0].init.signal.aborted, false);
    if (retired) epoch++;
    resolve({ok: true, data: {html: `<h1>${title}</h1>`}});
    await new Promise(done => setImmediate(done));
    if (retired) {
      assert.equal(nodes(render()).some(node => node.type === 'iframe'), false);
      continue;
    }
    const frame = nodes(render()).find(node => node.type === 'iframe');
    assert.equal(frame.props.srcDoc, `<h1>${title}</h1>`);
    assert.equal(frame.props.src, undefined);
    assert.equal(frame.props.sandbox, '');
    epoch++;
    assert.equal(nodes(render()).some(node => node.type === 'iframe'), false);
  }
});

test('local pages show an explicit link instead of loading a saved external preview', () => {
  const preview = compile(path.join(__dirname, '../../lib/firstlookPreview.ts'), require);
  const module = compile(path.join(__dirname, 'LandingPreviewFrame.tsx'), name => {
    if (name === 'react') return {useState: initial => [initial, () => {}], useRef: () => ({}), useEffect() {}};
    if (name === 'react/jsx-runtime') return {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})};
    if (name === '@/lib/firstlookPreview') return preview;
    if (name === '@/lib/localMode') return {isLocalConsole: () => true};
    if (name === '@/lib/previewNavigation') return {openablePreviewUrl: url => url};
    return {};
  });
  for (const domain of ['procurement.example', 'gardening.example']) {
    const src = `https://${domain}/landing`;
    const rendered = nodes(module.LandingPreviewFrame({src}));
    assert.equal(rendered.some(node => node.type === 'iframe'), false);
    assert.equal(rendered.find(node => node.type === 'a').props.href, src);
  }
});
