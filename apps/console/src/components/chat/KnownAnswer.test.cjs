const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
const text = node => Array.isArray(node) ? node.map(text).join('') : node?.props ? text(node.props.children) : typeof node === 'string' ? node : '';
test('saved assumptions, confirmation and conflict choices remain editable across domains', () => {
  const jsx = (type, props) => ({ type, props }), exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/KnownAnswer.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, FormData: class { constructor(form) { this.form = form; } get(field) { return this.form[field]; } },
    require: () => ({ jsx, jsxs: jsx }) });
  for (const audience of ['Municipal water auditors', 'Teenage chess learners']) {
    let chosen, edited = false;
    const props = { field: 'audience', onSelect: value => { chosen = value; }, onEdit: () => { edited = true; } };
    for (const status of ['known', 'confirm', 'conflict', 'missing']) {
      const check = { status, value: audience, source: 'Your saved answer', values: status === 'conflict'
        ? [{ value: audience, source: 'Your message' }, { value: 'Museum visitors', source: 'Your selected plan' }] : undefined };
      const tree = exports.KnownAnswer({ ...props, check });
      if (status === 'missing') { assert.equal(tree, null); continue; }
      assert.match(text(tree), new RegExp(audience));
      nodes(tree).find(node => node.type === 'button' && text(node) === 'Edit audience').props.onClick();
      assert.equal(edited, true);
      if (status === 'known') assert.equal(nodes(tree).some(node => node.type === 'input'), false);
      if (status === 'confirm') {
        nodes(tree).find(node => node.type === 'button' && text(node) === 'Yes').props.onClick();
        assert.equal(chosen, audience);
      }
      if (status === 'conflict') {
        assert.equal(nodes(tree).filter(node => node.type === 'input' && node.props.required).length, 2);
        assert.match(text(tree), /Your message/); assert.match(text(tree), /Your selected plan/);
        nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {}, currentTarget: { knownValue: audience } });
        assert.equal(chosen, audience);
      }
    }
  }
});
