const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const { createElement } = require('react');
const { renderToStaticMarkup } = require('react-dom/server');
const ts = require('typescript');

const code = ts.transpileModule(readFileSync(`${__dirname}/DeleteVentureDialog.tsx`, 'utf8'), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022 },
}).outputText;

function renderDialog(venture, retentionDays, local) {
  const exports = {};
  const mocks = {
    '@/lib/client': {},
    '@/lib/format': { displayName: name => name },
    '@/components/layout/LocalModeBoundary': { useLocalMode: () => local },
    '@/components/primitives/ErrorNotice': { ErrorNotice: () => null },
    './DeleteVentureDialog.module.css': { default: {} },
  };
  vm.runInNewContext(code, { exports, require: id => mocks[id] ?? require(id) });
  return renderToStaticMarkup(createElement(exports.DeleteVentureDialog, {
    venture, retentionDays, onClose() {}, onDeleted() {},
  }));
}

test('local venture deletion explains Trash, retained idea history, and the scope of a full reset', () => {
  for (const venture of [
    { id: 'ven_freight', name: 'Freight dispatch for wholesalers' },
    { id: 'ven_bread', name: 'Bread recipes for home bakers' },
  ]) {
    const local = renderDialog(venture, 7, true);
    assert.ok(local.includes(venture.name));
    assert.match(local, /move this venture to Trash and hide it from your list right away/i);
    assert.match(local, /choose Restore under Recently deleted within 7 days/);
    assert.match(local, /cleanup removes this venture’s chat history, setup documents and saved proposals/);
    assert.match(local, /Past versions of your idea and other saved records stay on this computer/);
    assert.match(local, /Export my data under Your data and recovery/);
    assert.match(local, /Quit the app, then delete that folder/);
    assert.match(local, /This clears local data for all your ventures, not just this one/);
    assert.match(local, /Exports and backups are separate copies/);
    assert.doesNotMatch(local, /purged|permanently deleted|removed forever/i);
    assert.doesNotMatch(renderDialog(venture, 7, false), /delete that folder/);
  }
});
