const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const {test} = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('polish3: Settings starts at the workspace edge without an empty band or a centered inset card', () => {
  const shell = readFileSync(__dirname + '/../../../components/layout/AppShell.tsx', 'utf8');
  const tokens = readFileSync(__dirname + '/../../../styles/tokens.css', 'utf8');
  const office = readFileSync(__dirname + '/../../../components/office/CompanyOffice.module.css', 'utf8');
  const css = readFileSync(__dirname + '/local.module.css', 'utf8');
  for (const domain of ['Procurement teams', 'Home bakers']) {
    assert.match(shell, /local && pathname\.startsWith\("\/settings\/local"\) && "local-settings-shell"/, domain);
    assert.match(tokens, /\.app-shell\.home-shell\.local-settings-shell \{[^}]*grid-template-columns: minmax\(0, 1fr\);[^}]*grid-template-rows: minmax\(0, 1fr\)/);
    assert.match(tokens, /\.local-settings-shell \.workspace \{[^}]*grid-column: 1;[^}]*grid-row: 1/);
    assert.match(office, /\.home:global\(\.office-settings\) \{[^}]*padding: 0/);
    assert.match(office, /:global\(\.office-settings\) \.main(?:, [^{]+)? \{[^}]*grid-template-rows: auto minmax\(0, 1fr\);[^}]*border-radius: 0/);
    assert.match(tokens, /\.local-settings-shell \.cos-rail \{[^}]*padding-top: 48px/);
    assert.match(css, /\.page\{[^}]*margin:0;[^}]*padding:0/);
  }
});

async function render(catalog) {
  const slots = [], effects = [];
  let cursor = 0;
  const exported = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/LocalSettings.tsx', 'utf8'), {
    compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022},
  }).outputText, {exports: exported, require: name => name === 'react/jsx-runtime'
    ? {jsx: (type, props) => ({type, props}), jsxs: (type, props) => ({type, props})}
    : name === 'react' ? {Fragment: 'fragment', useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
      return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];}, useEffect: effect => effects.push(effect)}
    : name === '@/lib/client' ? {api: async () => ({ok: true, data: catalog})}
    : name === '@/components/layout/LocalModeBoundary' ? {useFeatureVisibility: () => () => true}
    : name.endsWith('.css') ? {default: {loading: 'loading'}} : new Proxy({}, {get: (_target, key) => String(key)})});
  exported.LocalSettings({}); effects.splice(0).forEach(effect => effect());
  await new Promise(resolve => setImmediate(resolve)); cursor = 0;
  return exported.LocalSettings({});
}

test('your settings landmark is named and focusable when you open it', async () => {
  for (const title of ['Inventory tracking', 'Holiday planning']) {
    const tree = await render({env_path: '/local/.env', sections: [{id: 'smtp', title, fields: []}]});
    assert.equal(tree.type, 'main', title);
    assert.equal(tree.props.tabIndex, -1, title);
    const heading = tree.props.children[0].props.children[0];
    assert.equal(heading.type, 'h1', title);
    assert.ok(heading.props.id, title);
    assert.equal(tree.props['aria-labelledby'], heading.props.id, title);
  }
});

test('only an unreadable saved key asks you to re-enter that named key across domains', async () => {
  for (const domain of ['Fleet coordination', 'Family holidays']) {
    for (const state of ['from .env', 'not set', 'unreadable']) {
      const catalog = {env_path: '/local/.env', sections: [{id: 'smtp', title: domain,
        fields: [{name: 'password', label: 'Mail password', secret: true, state, env: []}]}],
      storage_notice: 'Open Keys and connections and re-enter your saved keys. Your previous encrypted files are kept in your data folder.'};
      const markup = JSON.stringify(await render(catalog));
      assert.equal(/Re-enter your /.test(markup), state === 'unreadable');
      if (state === 'unreadable') {assert.match(markup, /Mail password/); assert.ok(markup.includes(domain));}
      assert.match(markup, /Keep your data folder/);
    }
  }
});

test('your protected-file notice is visible across domains without blocking setup', async () => {
  for (const domain of ['Equipment scheduling', 'Pet care']) {
    const notice = 'Your keys are encrypted in a protected file because no system keychain was found. Keep your data folder private.';
    const catalog = {env_path: '/local/.env', sections: [{id: 'smtp', title: domain, fields: []}]};
    assert.ok(!JSON.stringify(await render(catalog)).includes(notice));
    const markup = JSON.stringify(await render({...catalog, storage_notice: notice}));
    assert.ok(markup.includes(notice));
    assert.match(markup, /"role":"status"/);
  }
});

test('backup guidance distinguishes saved connections from the recovery key', async () => {
  for (const title of ['Factory purchasing', 'Family budgeting']) {
    const markup = JSON.stringify(await render({env_path: '/local/.env', sections: [{id: 'smtp', title, fields: []}]}));
    assert.match(markup, /Full backups include your encrypted settings, not your recovery key/);
    assert.match(markup, /Your data and recovery/);
  }
});
