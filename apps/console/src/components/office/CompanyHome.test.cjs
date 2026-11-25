const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');
const source = readFileSync(__dirname + '/CompanyHome.tsx', 'utf8');
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : tree?.props ? [tree, ...nodes(tree.props.children)] : [];

function harness(title) {
  const values = [], effects = [], exports = {}, focused = [];
  let cursor = 0;
  const data = {ventureId: title, archives: [{id: title, title, href: '/', document: true}], agents: [], pins: [], cards: [], errors: []};
  const jsx = (type, props) => ({type, props});
  vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX}}).outputText, {
    exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {createContext: () => ({}), useContext: () => ({data, owner: title, current: () => true, retry() {}}),
        useState(initial) {const index = cursor++; if (!(index in values)) values[index] = initial; return [values[index], value => {values[index] = typeof value === 'function' ? value(values[index]) : value;}];},
        useRef(initial) {const index = cursor++; return values[index] ||= {current: initial};}, useId: () => 'document-title',
        useEffect(callback, deps) {const index = cursor++; if (!values[index] || deps.some((value, offset) => !Object.is(value, values[index][offset]))) {values[index] = deps; effects.push(callback);}}};
      if (id === '@/lib/client') return {api: async () => ({ok: true, data: {id: title, title, text: title}})};
      if (id === '@/lib/companyHome') return {companyDepartments: () => [], archiveDepartment: () => 'Reports', departmentFor: () => 'Reports'};
      if (id === './OfficeProvider') return {useOfficeOptional: () => null};
      if (id === 'next/navigation') return {useRouter: () => ({})};
      if (id === '@/lib/chatRouteAction') return {chatNavigation: router => router};
      if (id === '@/lib/founderRoutes') return {founderRouteLinks: () => []};
      if (id === './CompanyOffice.module.css') return {default: new Proxy({}, {get: (_, key) => key})};
      return {};
    },
  });
  return {data, focused, render() {cursor = 0; const tree = exports.CompanyHome({tab: 'archives'}); for (const node of nodes(tree)) if (node.props.ref && typeof node.props.ref === 'object') node.props.ref.current = {focus() {focused.push('document');}}; effects.splice(0).forEach(callback => callback()); return tree;}};
}

test('your document reader takes and restores focus on Close and Escape across domains', async () => {
  for (const title of ['Procurement_report.pdf', 'Garden_notes.txt']) for (const close of ['button', 'escape']) {
    const view = harness(title);
    view.render();
    const read = nodes(view.render()).find(node => node.props.children === 'Read saved document');
    const invoker = {isConnected: true, focus() {view.focused.push('invoker');}};
    await read.props.onClick({currentTarget: invoker});
    await new Promise(setImmediate);
    const tree = view.render();
    const reader = nodes(tree).find(node => node.type === 'section' && node.props.tabIndex === -1);
    assert.ok(reader, title);
    assert.equal(reader.props['aria-labelledby'], 'document-title');
    assert.equal(view.focused.at(-1), 'document');
    if (close === 'button') nodes(tree).find(node => node.props.children === 'Close document').props.onClick();
    else reader.props.onKeyDown({key: 'Escape', preventDefault() {}});
    assert.equal(view.focused.at(-1), 'invoker');
    assert.equal(nodes(view.render()).some(node => node.type === 'pre'), false);
  }
});

test('your archive failure does not also announce loading across domains', () => {
  for (const title of ['Procurement report', 'Garden notes']) {
    const view = harness(title);
    view.data.archives = null; view.data.errors = ['Tuesday Archives'];
    assert.doesNotMatch(JSON.stringify(view.render()), /Loading saved work/);
  }
});

test('your long document and folder text wraps without changing the three-column design', () => {
  const css = readFileSync(__dirname + '/CompanyOffice.module.css', 'utf8');
  for (const title of ['procurement'.repeat(60), 'gardening'.repeat(60)]) {
    assert.match(css, /\.fileBody[^}]*overflow-wrap: anywhere/, title);
    assert.match(css, /\.fileBody footer span[^}]*min-width: 0/, title);
    assert.match(css, /\.pocketFront[^}]*overflow-wrap: anywhere/, title);
    assert.match(css, /grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/, title);
  }
});

test('your repeated document and settings buttons include their current data in the name', () => {
  const section = readFileSync(__dirname + '/../../app/settings/local/SettingsSection.tsx', 'utf8');
  for (const title of ['Procurement report', 'Garden notes']) {
    const read = nodes(harness(title).render()).find(node => node.props.children === 'Read saved document');
    assert.equal(read.props['aria-label'], `Read saved document: ${title}`);
    assert.match(section, /aria-label=\{`Save your settings: \$\{section\.title\}`\}/);
  }
});
