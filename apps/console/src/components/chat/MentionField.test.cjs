const assert = require('node:assert/strict');
const {readFileSync} = require('node:fs');
const test = require('node:test');
const vm = require('node:vm');
const ts = require('typescript');

test('mentions preserve Tab and IME, dismiss until edited and link active options across domains', () => {
  for (const domain of ['Logistics analyst', 'Gardening guide']) {
    const states = [], exports = {};
    let cursor = 0;
    const jsx = (type, props) => ({type, props});
    vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/MentionField.tsx', 'utf8'), {
      compilerOptions: {module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX},
    }).outputText, {exports, requestAnimationFrame: callback => callback(), require(id) {
      if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
      if (id === 'react') return {
        useState(initial) {const index = cursor++; if (!(index in states)) states[index] = initial; return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];},
        useRef(initial) {const index = cursor++; return states[index] ||= {current: initial};},
        useMemo: callback => callback(), useId: () => 'mention-list',
      };
      if (id === './mentions') {
        const mentions = {};
        vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/mentions.ts', 'utf8'), {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
          {exports: mentions, require: () => ({prettyName: name => name, prettyRole: role => role})});
        return mentions;
      }
      if (id.endsWith('/chatComposer')) return require('../../lib/chatComposer.ts');
      if (id === './useComposerAutoGrow') return {useComposerAutoGrow: () => ({ref: {current: null}, sync() {}})};
      return {default: {}};
    }});
    const changes = [], sent = [];
    const props = {agents: [{id: 'guide', name: domain, role: 'guide'}], multiline: true, ariaLabel: 'Your message', onChange: value => changes.push(value), onSubmit: body => sent.push(body), value: '@gu'};
    const render = () => {cursor = 0; return exports.MentionField(props).props.children;};
    let [field] = render();
    field.props.onChange({target: {value: '@gu', selectionStart: 3}});
    let menu;
    [field, menu] = render();
    assert.equal(field.props['aria-controls'], menu.props.id);
    assert.equal(field.props['aria-expanded'], true);
    assert.equal(field.props['aria-activedescendant'], menu.props.children[0].props.children.props.id);
    for (const shiftKey of [false, true]) field.props.onKeyDown({key: 'Tab', shiftKey, nativeEvent: {}, preventDefault() {assert.fail('Tab must move focus');}});
    field.props.onKeyDown({key: 'Enter', nativeEvent: {isComposing: true}, preventDefault() {assert.fail('IME must keep composing');}});
    assert.deepEqual(sent, []);
    field.props.onKeyDown({key: 'Escape', nativeEvent: {}, preventDefault() {}});
    assert.equal(render()[1], false);
    field.props.onChange({target: {value: '@gui', selectionStart: 4}}); props.value = '@gui';
    [field, menu] = render();
    menu.props.children[0].props.children.props.onClick();
    assert.ok(changes.at(-1).includes('@'));
    assert.equal(changes.length, 3);
  }
});
