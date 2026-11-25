const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const { readFileSync } = require('node:fs'), ts = require('typescript');
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node?.props ? [node, ...nodes(node.props.children)] : [];
test('outreach intake asks only the next missing field without overwriting saved answers or email safety', async () => {
  const jsx = (type, props) => ({ type, props }), exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/OnboardingIcp.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, FormData: class { constructor(form) { this.form = form; } get(field) { return this.form[field]; } }, require(id) {
    if (id === 'react') return { useState: value => [value, () => {}] };
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id === './useOutreachAction') return { useOutreachAction: () => ({ pending: false, run: fn => fn() }), ActionFeedback: 'feedback' };
    return { default: {}, ChatAnchor: 'a' };
  }});
  for (const audience of ['Freight dispatch teams', 'Weekend hikers']) {
    let saved;
    const flags = { icp: false, excludes: true, sender_geo: true, send_path: true, voice: true, evidence: true, ready: false, outbound_mode: 'gmail_composio' };
    const render = () => exports.OnboardingIcp({ flags, suggestedWho: audience, onSave: async body => { saved = body; } });
    let tree = render(), fields = nodes(tree).filter(node => ['input', 'textarea', 'select'].includes(node.type));
    assert.equal(fields.length, 1); assert.equal(fields[0].props.defaultValue, audience);
    await nodes(tree).find(node => node.type === 'form').props.onSubmit({ preventDefault() {}, currentTarget: { who: audience } });
    assert.equal(JSON.stringify(saved), JSON.stringify({ who: audience }));
    flags.icp = true; flags.sender_geo = false;
    fields = nodes(render()).filter(node => ['input', 'textarea', 'select'].includes(node.type));
    assert.equal(fields.length, 1); assert.equal(fields[0].props.name, 'sender_postal_address'); assert.equal(fields[0].props.required, true);
    flags.sender_postal_address = 'A verified sender address'; flags.sender_geo = true;
    assert.equal(nodes(render()).some(node => node.type === 'form'), false);
  }
});

test('known outreach context skips questions and offers source choices without relaxing sender checks', async () => {
  const jsx = (type, props) => ({ type, props }), exports = {};
  let editing = null, saved;
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/OnboardingIcp.tsx`, 'utf8'), {
    compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX }
  }).outputText, { exports, require(id) {
    if (id === 'react') return { useState: () => [editing, value => { editing = value; }] };
    if (id === 'react/jsx-runtime') return { jsx, jsxs: jsx };
    if (id.endsWith('/KnownAnswer')) return { KnownAnswer: 'known-answer' };
    if (id === './useOutreachAction') return { useOutreachAction: () => ({ pending: false, run: fn => fn() }), ActionFeedback: 'feedback' };
    return { default: {}, ChatAnchor: 'a' };
  }});
  for (const audience of ['Municipal water auditors', 'Teenage chess learners']) {
    for (const status of ['known', 'confirm', 'conflict']) {
      editing = null;
      const flags = { icp: status === 'known', excludes: false, sender_geo: false, send_path: true, voice: false,
        evidence: false, known_checks: { audience: { status, value: audience, source: 'Your saved context' } }, segment: { who: audience } };
      const render = () => exports.OnboardingIcp({ flags, onSave: async value => { saved = value; } });
      let tree = render();
      const card = nodes(tree).find(node => node.type === 'known-answer');
      assert.equal(card.props.check.value, audience);
      assert.equal(nodes(tree).some(node => node.type === 'textarea' && node.props.name === 'who'), false);
      if (status !== 'known') { await card.props.onSelect(audience); assert.equal(saved.who, audience); }
      card.props.onEdit();
      tree = render();
      assert.equal(nodes(tree).find(node => node.type === 'textarea').props.defaultValue, audience);
      assert.equal(flags.sender_geo, false);
    }
  }
});
