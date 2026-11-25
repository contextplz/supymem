const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

test('your empty saved idea gives you a direct next action across domains', () => {
  const page = readFileSync(join(__dirname, 'page.tsx'), 'utf8');
  const fragment = page.match(/!loading && !error && !page && (.+)}\n/)[1];
  for (const ventureId of ['ven_invoice', 'ven_walks']) {
    const exports = {}, jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(`export default function Empty() { return ${fragment}; }`, {compilerOptions: {
      module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, ventureId, encodeURIComponent, ChatAnchor: 'a', require: () => ({jsx, jsxs: jsx})});
    const link = nodes(exports.default()).find(node => node.type === 'a');
    assert.ok(Object.hasOwn(link.props, 'data-chat-route-native'), 'the questionnaire link must bypass chat route rewrites');
    assert.equal(link.props.href, `/v/${ventureId}/idea`);
    assert.equal(link.props.children, 'Open Idea and describe what you want to build.');
  }
});

function nodes(tree) {
  if (!tree) return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return typeof tree === "object" ? [tree, ...nodes(tree.props?.children)] : [];
}

test('a confirmed idea renders from its saved revision without a generated report', async () => {
  const output = ts.transpileModule(readFileSync(join(__dirname, 'page.tsx'), 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText;
  const jsx = (type, props) => typeof type === 'function' ? type(props) : {type, props};
  for (const [ventureId, problem, goal, audienceFields] of [
    ['ven_lab', 'Sample handoffs are late', {goal: 'Log every handoff'}, {audience: 'Lab coordinators'}],
    ['ven_garden', 'Seedlings dry out while away', {desired_outcome: 'Share watering reminders'}, {customer: 'First-time gardeners'}],
  ]) {
    const path = `/api/ventures/${ventureId}`;
    const fixtures = {
      [path]: {name: 'Idea', workspace_id: 'ws_fixture'},
      [`${path}/idea-page`]: {page: null, updated_at: null},
      [`${path}/idea/sessions`]: {items: [{id: 'discarded', state: 'abandoned'}, {id: 'saved', state: 'confirmed'}]},
      [`${path}/idea/sessions/saved`]: {id: 'saved', workspace_id: 'ws_fixture', venture_id: ventureId, state: 'confirmed',
        current_revision: {summary: 'Saved summary', structured: {problem, ...goal, ...audienceFields}}},
    };
    const exports = {}, states = [], prefills = [];
    let cursor = 0, load;
    vm.runInNewContext(output, {exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useEffect: effect => { load ??= effect; }, useState(initial) {
        const index = cursor++;
        if (!(index in states)) states[index] = initial;
        return [states[index], value => { states[index] = value; }];
      }};
      if (id === 'next/navigation') return {useParams: () => ({ventureId})};
      if (id.endsWith('/client')) return {ensureSession: async () => {}, api: async url => ({ok: url in fixtures, data: fixtures[url]})};
      if (id.endsWith('/format')) return {shortTitle: value => value};
      if (id.endsWith('/chatPrefill')) return {prefillChat: value => prefills.push(value)};
      return new Proxy({}, {get: (_target, name) => String(name)});
    }});
    exports.default();
    load();
    await new Promise(setImmediate);
    cursor = 0;
    const tree = exports.default();
    const rendered = JSON.stringify(tree);
    for (const value of ['Saved summary', 'Confirmed', problem, ...Object.values(goal), ...Object.values(audienceFields)]) {
      assert.ok(rendered.includes(value), `${ventureId}: ${value}`);
    }
    assert.doesNotMatch(rendered, /no saved idea summary/);
    const edit = nodes(tree).find(node => node.type === 'button' && node.props.children === 'Edit');
    edit.props.onClick();
    assert.match(prefills[0], /^Correction: /);
  }
});

test('21: missing target customers give you a general way to describe them without inventing an audience', () => {
  for (const audience of ['Hospital supply teams', 'Birdwatching clubs']) for (const current of [[], [audience]]) {
    const states = ['Idea', {summary: '', customers: {current}}, null, false, '', 0], prefills = [], exports = {};
    let cursor = 0; const jsx = (type, props) => typeof type === 'function' ? type(props) : {type, props};
    vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'page.tsx'), 'utf8'), {compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
    }}).outputText, {exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState: () => [states[cursor++], () => {}], useEffect() {}};
      if (id === 'next/navigation') return {useParams: () => ({ventureId: 'ven_domain'})};
      if (id.endsWith('/chatPrefill')) return {prefillChat: text => prefills.push(text)};
      return new Proxy({}, {get: (_target, name) => String(name)});
    }});
    const tree = exports.default(), ask = nodes(tree).find(node => node.props?.children === 'Tell Charlie the customers');
    assert.equal(Boolean(ask), current.length === 0); assert.doesNotMatch(JSON.stringify(tree), /"unknown"/);
    if (ask) {ask.props.onClick(); assert.equal(prefills.length, 1); assert.match(prefills[0], /who my idea is for/); assert.equal(prefills[0].includes(audience), false);}
  }
});

test("What we know renders each domain's recorded result as an experiment link", () => {
  for (const [ventureId, audience] of [["ven_dispatch", "freight dispatchers"], ["ven_hiking", "weekend hikers"]]) {
    const result = { text: `supported: ${audience} reported the problem | 3 recorded sources`,
      href: `/v/${ventureId}/experiments?experiment_id=exp_recorded` };
    const states = ["Idea", { summary: `An offer for ${audience}`, what_we_know: [result] }, null, false, ""];
    let cursor = 0;
    const exports = {};
    const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
    const output = ts.transpileModule(readFileSync(join(__dirname, "page.tsx"), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX },
    }).outputText;
    vm.runInNewContext(output, { exports, require(id) {
      if (id === "react") return { useState: () => [states[cursor++], () => {}], useEffect() {} };
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (id === "next/navigation") return { useParams: () => ({ ventureId }) };
      if (id.endsWith("AppShell")) return { AppShell: props => jsx("main", props) };
      if (id.endsWith("ErrorNotice")) return { ErrorNotice: () => null };
      if (id.endsWith("ChatLink")) return { ChatAnchor: props => jsx("a", props) };
      return {};
    } });
    const rendered = nodes(exports.default());
    assert.ok(rendered.some(node => node.type === "h3" && node.props.children === "What we know"));
    const link = rendered.find(node => node.type === "a" && node.props.href === result.href);
    assert.equal(link.props.children, result.text);
    assert.ok(rendered.some(node => node.type === "p" && node.props.children === `An offer for ${audience}`));
    const routes = {};
    const routeOutput = ts.transpileModule(readFileSync(join(__dirname, "../../../../../lib/chatRouteAction.ts"), "utf8"), {
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
    }).outputText;
    vm.runInNewContext(routeOutput, { exports: routes, URL, require: name => require(join(__dirname, '../../../../../lib', name)) });
    const action = routes.homeRouteAction(result.href, ventureId);
    assert.equal(action.action, "experiment.list");
    assert.equal(action.payload.experiment_id, "exp_recorded");
  }
});

test('06: you can retry a failed saved idea read without repeating any mutation', async () => {
  for (const domain of ['Factory scheduling', 'Community picnics']) {
    const slots = [], effects = [], requests = [], exports = {}; let cursor = 0, succeed = false;
    const jsx = (type, props) => typeof type === 'function' ? type(props) : {type, props};
    vm.runInNewContext(ts.transpileModule(readFileSync(join(__dirname, 'page.tsx'), 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports, require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {useState(initial) {const index = cursor++; if (!(index in slots)) slots[index] = initial;
        return [slots[index], value => {slots[index] = typeof value === 'function' ? value(slots[index]) : value;}];},
        useEffect(callback, deps) {const index = cursor++; if (!slots[index] || deps.some((value, offset) => slots[index].deps[offset] !== value)) {
          slots[index]?.cleanup?.(); slots[index] = {deps}; effects.push(() => {slots[index].cleanup = callback();});}}};
      if (id === 'next/navigation') return {useParams: () => ({ventureId: 'ven_domain'})};
      if (id.endsWith('/client')) return {ensureSession: async () => {}, api: async (path, options) => {
        requests.push({path, options}); return path.endsWith('/idea-page') ? {ok: succeed, data: {page: {summary: domain}}} : {ok: true, data: {name: domain}};}};
      if (id.endsWith('/format')) return {shortTitle: value => value};
      if (id.endsWith('/apiFailure')) return {apiFailure: () => 'Couldn’t load your idea.'};
      return new Proxy({}, {get: (_target, name) => String(name)});
    }});
    const render = () => {cursor = 0; const tree = exports.default(); effects.splice(0).forEach(effect => effect()); return tree;};
    render(); await new Promise(resolve => setImmediate(resolve));
    const retry = nodes(render()).find(node => node.props?.children === 'Try again');
    assert.equal(retry.props.disabled, false); succeed = true; retry.props.onClick(); render();
    await new Promise(resolve => setImmediate(resolve));
    assert.ok(nodes(render()).some(node => node.props?.children === domain));
    assert.equal(requests.filter(request => request.path.endsWith('/idea-page')).length, 2);
    assert.ok(requests.every(request => !request.options));
  }
});

test('22: your saved idea read starts with its title read and survives unavailable optional metadata', async () => {
  const source = ts.createSourceFile('page.tsx', readFileSync(join(__dirname, 'page.tsx'), 'utf8'), ts.ScriptTarget.Latest, true);
  let callback;
  function find(node) {if (ts.isCallExpression(node) && node.expression.getText(source) === 'useEffect') callback = node.arguments[0]; ts.forEachChild(node, find);}
  find(source);
  for (const domain of ['Factory scheduling', 'Community picnics']) {
    const exports = {}, requests = [], values = {}; let finish;
    vm.runInNewContext(ts.transpileModule(`exports.read = (${callback.getText(source)});`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText,
      {exports, ventureId: 'ven_domain', ensureSession: async () => {}, shortTitle: value => value, readConfirmedIdea: async () => null,
        setLoading: value => {values.loading = value;}, setError: value => {values.error = value;}, setTitle: value => {values.title = value;},
        setPage: value => {values.page = value;}, setUpdatedAt() {},
        api: path => {requests.push(path); return path.endsWith('/idea-page') ? Promise.resolve({ok: true, data: {page: {summary: domain}}})
          : new Promise((_resolve, reject) => {finish = reject;});}});
    const cleanup = exports.read(); await new Promise(resolve => setImmediate(resolve));
    assert.equal(requests.length, 2); finish(new Error('offline')); await new Promise(resolve => setImmediate(resolve));
    assert.equal(values.page.summary, domain); assert.equal(values.error, ''); assert.equal(values.loading, false); cleanup();
  }
});
