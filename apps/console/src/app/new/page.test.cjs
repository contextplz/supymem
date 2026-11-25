const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), vm = require('node:vm'), ts = require('typescript');

test('01: typing a credential never writes it to entry draft storage', () => {
  const page = fs.readFileSync(`${__dirname}/page.tsx`, 'utf8');
  const action = page.slice(page.indexOf('  function setDraft('), page.indexOf('  const owned ='));
  const composer = fs.readFileSync(`${__dirname}/../../lib/chatComposer.ts`, 'utf8');
  const entry = fs.readFileSync(`${__dirname}/../../lib/chatEntry.ts`, 'utf8');
  const exports = {};
  const values = new Map();
  const storage = {getItem: key => values.get(key), setItem: (key, value) => values.set(key, value), removeItem: key => values.delete(key)};
  const load = (source, extra = {}) => {
    const module = {exports: {}};
    vm.runInNewContext(ts.transpileModule(source, {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
      {exports: module.exports, ...extra});
    return module.exports;
  };
  const helpers = load(composer);
  Object.assign(exports, load(entry, {require: () => helpers}));
  for (const domain of ['rivetworks.invalid', 'mossmornings.invalid']) {
    const scope = { ...helpers, ...exports, updateDraft() {}, draftRef: {}, storage, draftKey: domain,
      draftSearch: '', verifiedSessionSubject: () => 'owner', targetVentureId: undefined };
    const {setDraft} = load(`${action}\nexport {setDraft};`, scope);
    setDraft(`Build a site for ${domain}`);
    assert.equal(values.get(domain), `Build a site for ${domain}`);
    setDraft(`Build a site for ${domain}\npassword = fixture-value-for-${domain}`);
    assert.equal(values.has(domain), false);
  }
});

test('every entry request immediately shows Charlie working until it ends across domains', () => {
  for (const domain of ['Warehouse routing', 'Family gardening']) {
    const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
    let indicator;
    function find(node) {
      if (ts.isJsxExpression(node) && node.expression?.getText(tree).includes('busy && <ChatWorking')) indicator = node.expression;
      ts.forEachChild(node, find);
    }
    find(tree); assert.ok(indicator, domain);
    for (const busy of [false, true]) {
      const context = {exports: {}, busy, ChatWorking: 'working', React: {createElement: (type, props) => ({type, props})}};
      vm.runInNewContext(ts.transpileModule(`exports.render = () => (${indicator.getText(tree)});`, {compilerOptions: {jsx: ts.JsxEmit.React}}).outputText, context);
      assert.equal(context.exports.render()?.type, busy ? 'working' : undefined);
    }
  }
});

test('fresh conversation loading does not imply saved history across domains', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let copy;
  function find(node) {
    if (!copy && ts.isConditionalExpression(node) && ts.isStringLiteral(node.whenTrue)
      && node.whenFalse.getText(tree).includes('Preparing your new conversation')) copy = node.getText(tree);
    ts.forEachChild(node, find);
  }
  find(tree); assert.ok(copy);
  const context = {exports: {}};
  vm.runInNewContext(`exports.copy = (targetVentureId, resume) => ${copy};`, context);
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    assert.equal(context.exports.copy(undefined, null), 'Preparing your new conversation...');
    assert.equal(context.exports.copy(domain, null), 'Opening your conversation...');
    assert.equal(context.exports.copy(undefined, domain), 'Opening your saved conversation...');
  }
});

function pageElement(className) {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let element;
  function find(node) {
    if (ts.isJsxElement(node) && node.openingElement.getText(tree).includes(`styles.${className}`)) element = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(element, className);
  return element;
}

function renderElement(element, scope) {
  const context = {exports: {}, require, styles: new Proxy({}, {get: (_, key) => key}), ...scope};
  vm.runInNewContext(ts.transpileModule(`exports.view = (${element.getText()});`, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, context);
  return context.exports.view;
}

test('20: a fresh conversation does not claim you already have a saved one', () => {
  const source = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let pending;
  function find(node) {
    if (ts.isJsxElement(node) && node.openingElement.getText(source).includes('role="status"')
      && node.getText(source).includes('Preparing your new conversation')) pending = node;
    ts.forEachChild(node, find);
  }
  find(source); assert.ok(pending);
  for (const domain of ['B2B freight', 'Community running']) {
    assert.equal(renderElement(pending, {targetVentureId: null, resume: null, domain}).props.children, 'Preparing your new conversation...');
    assert.equal(renderElement(pending, {targetVentureId: `ven_${domain}`, resume: null, domain}).props.children, 'Opening your conversation...');
    assert.equal(renderElement(pending, {targetVentureId: null, resume: domain, domain}).props.children, 'Opening your saved conversation...');
  }
});

test('model questions do not imply a fixed audience step and unavailable names keep free text', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let number, naming, wire, fallback;
  function find(node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'questionNumber') number = node.initializer;
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'naming') naming = node.initializer;
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === 'text' && node.initializer?.getText(tree).includes('clarification.original')) wire = node.initializer;
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'response.ok && suggestion?.source === "free_text"') fallback = node.thenStatement;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(number);
  assert.ok(naming);
  assert.ok(wire);
  assert.ok(fallback);
  const library = ts.createSourceFile('chatEntry.ts', fs.readFileSync(`${__dirname}/../../lib/chatEntry.ts`, 'utf8'), ts.ScriptTarget.Latest, true);
  const words = library.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'founderWords');
  assert.ok(words);
  for (const [question, clarificationCount] of [['Which purchasing problem matters first?', 1], ['Which lesson format would you test?', 5]]) {
    let focused = 0;
    const context = {exports: {}, editableIdeaAnswer: text => text, founderChoiceWords: text => text, clarificationCount, clarification: {question, field: 'context'}, nameRequest: {current: 'pending'},
      nameInput: {current: {focus: () => focused++}}};
    vm.runInNewContext(ts.transpileModule(`${words.getText(library)}\nexports.number = ${number.getText(tree)}; exports.naming = (proposal, ventureName) => ${naming.getText(tree)}; exports.wire = (clarification, answer) => ${wire.getText(tree)}; exports.fallback = () => ${fallback.getText(tree)};`, {
      compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
    }).outputText, context);
    assert.equal(context.exports.number, clarificationCount + 1);
    assert.equal(context.exports.naming({id: question}, ''), true);
    assert.equal(context.exports.naming({id: question}, question), false);
    assert.equal(context.exports.naming({id: question, target_venture_id: question}, ''), false);
    assert.equal(context.exports.wire({original: question, field: 'context'}, 'Your background'), `${question}\nContext: Your background`);
    assert.equal(context.exports.founderWords(`${question}\nContext: Your background`, question), 'Your background');
    assert.equal(context.exports.wire({original: question, field: 'offer'}, 'Your product'), `${question}\nClarification: Your product`);
    context.exports.fallback();
    assert.equal(context.nameRequest.current, null);
    assert.equal(focused, 1);
  }
  assert.doesNotMatch(tree.getFullText(), /Describe who it helps\./);
  assert.doesNotMatch(tree.getFullText(), /questionNumber === 2/);
});

test('model-chosen question progress stays stable through same-field follow-ups and Back across domains', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  const declarations = new Map();
  function find(node) {
    if (ts.isVariableDeclaration(node) && ['clarificationCount', 'questionNumber'].includes(node.name.getText(tree))) {
      declarations.set(node.name.getText(tree), node.getText(tree));
    }
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.equal(declarations.size, 2);
  const context = {exports: {}};
  vm.runInNewContext(ts.transpileModule(`exports.number = turns => { const ${declarations.get('clarificationCount')}; const ${declarations.get('questionNumber')}; return questionNumber; };`, {
    compilerOptions: {target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS},
  }).outputText, context);
  for (const [idea, audience, followup] of [
    ['A stock tracker for warehouses', 'Warehouse managers', 'Small distribution centers'],
    ['A meal-planning app for families', 'Parents', 'Families with young children'],
  ]) {
    const turns = [
      {id: 'idea', role: 'user', text: idea},
      {id: 'context', role: 'assistant', text: idea, proposal: {state: 'clarify', onboarding_field: 'context'}},
      {id: 'audience', role: 'assistant', text: audience, proposal: {state: 'pending', onboarding_question: audience, onboarding_field: 'customer'}},
      {id: 'followup', role: 'assistant', text: followup, proposal: {state: 'clarify', onboarding_field: 'customer'}},
    ];
    assert.equal(context.exports.number([]), 1);
    assert.equal(context.exports.number(turns.slice(0, 2)), 2);
    assert.equal(context.exports.number(turns.slice(0, 3)), 3);
    assert.equal(context.exports.number(turns), 3);
    assert.equal(context.exports.number(turns.slice(0, -1)), 3, 'Back to the same field keeps its question number');
    const offer = {id: 'offer', role: 'assistant', text: idea, proposal: {state: 'pending', onboarding_question: idea, onboarding_field: 'offer'}};
    assert.equal(context.exports.number([...turns, offer]), 4, 'the model can ask about the offer after the audience');
    const untyped = [{id: 'other1', role: 'assistant', proposal: {state: 'clarify'}}, {id: 'other2', role: 'assistant', proposal: {state: 'clarify'}}];
    assert.equal(context.exports.number([...turns, ...untyped]), 5, 'questions without a persisted field remain separate');
  }
});

test('clarification keeps the domain-derived name without replacing your edit', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let branch;
  function find(node) {
    if (ts.isIfStatement(node) && node.expression.getText(tree) === 'response.status === "clarify"') branch = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  const keepName = branch.thenStatement.statements.find(node => node.getText(tree).startsWith('setVentureName('));
  assert.ok(keepName);
  for (const name of ['Invoice Harbor', 'Pantry Bloom']) {
    for (const previous of ['', 'Your edited name']) {
      let saved;
      vm.runInNewContext(ts.transpileModule(keepName.getText(tree), {}).outputText, {
        response: {venture_name: name}, setVentureName: update => {saved = update(previous);},
      });
      assert.equal(saved, previous || name);
    }
  }
  assert.match(branch.thenStatement.getText(tree), /venture_name: response\.venture_name/);
});

test('website assumptions stay visible beside a free-text gap across unrelated domains', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let details;
  function find(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'details' &&
        node.getText(tree).includes('Assumptions from your website')) details = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(details);
  for (const [domain, offer, audience] of [['invoice-harbor.io', 'Supplier invoice matching', 'Finance teams at manufacturers'],
    ['pantry-bloom.app', 'Save recipes and plan dinners', 'Home cooks planning weekly dinners']]) {
    const view = renderElement(details, {turn: {proposal: {id: domain, website_answers: {offer, customer: audience, pricing_hypothesis: '$9/month', positioning: offer}}},
      websiteLabels: {offer: 'What it does', customer: "Who it's for", pricing_hypothesis: 'Pricing', positioning: 'Positioning'}});
    assert.equal(view.props.children[0].props.children, 'Assumptions from your website');
    assert.match(view.props.children[1].props.children, /only ask what's missing.*correct/);
    assert.deepEqual(Array.from(view.props.children[2], row => row.props.children.at(-1)), [offer, audience, '$9/month', offer]);
    const name = renderElement(pageElement('ventureName'), {turn: {text: 'Your next step is to find customers.'}, ventureName: domain,
      composerStyles: {option: 'option'}, composerDisabled: false, attachments: {uploading: false}, busy: false, confirm() {}, suggestName() {}});
    assert.equal(name.props.children[0].props.children, 'You can keep this name or change it.');
  }
  assert.match(details.parent.getText(tree), /clarification\?\.id === turn\.proposal\.id/);
  assert.match(tree.getFullText(), /proposal\?\.route === "gtm" \? "Continue to find customers"/);
});

test('onboarding helpers share a small left-aligned row above the full-width answer across domains', () => {
  const helpers = pageElement('entryHelpers');
  let composer = helpers.parent;
  while (!ts.isJsxElement(composer) || !composer.openingElement.getText().includes('styles.entryComposer')) composer = composer.parent;
  const form = composer.children.find(node => ts.isJsxElement(node) && node.openingElement.tagName.getText() === 'form');
  assert.ok(form);
  assert.ok(helpers.getStart() < form.getStart());
  assert.equal(form.parent, composer);
  for (const [question, draft] of [['Which clinics need this?', 'Clinic scheduling teams'], ['Who would use this outdoors?', 'Weekend hikers']]) {
    for (const busy of [false, true]) {
      const answers = [];
      const row = renderElement(helpers, {clarification: {question, field: 'customer', choices: []}, clarificationCount: 5,
        turns: [], busy, draft, selectedChoices: [], answerQuestion: answer => answers.push(answer), composeIdeaAnswer: () => draft, back() {}});
      assert.equal(row.type, 'div');
      assert.equal(row.props.className, 'entryHelpers');
      const [hint, skip, back] = row.props.children;
      assert.equal(hint.props.id, 'entry-answer-hint');
      assert.equal(hint.props.role, 'status');
      assert.equal(skip.props['data-usage-control'], 'onboarding_skip');
      assert.equal(back.props['data-usage-control'], 'onboarding_back');
      assert.equal(skip.props.disabled, busy);
      assert.equal(back.props.disabled, busy);
      if (!busy) {
        skip.props.onClick();
        assert.deepEqual(answers, [`${draft}\nSkip this question`]);
      }
    }
  }
  const css = fs.readFileSync(`${__dirname}/page.module.css`, 'utf8');
  assert.match(css, /\.chat \.entryComposer \{[^}]*flex-direction: column/);
  assert.match(css, /\.chat \.entryComposer > form \{[^}]*flex: none;[^}]*width: 100%;[^}]*min-width: 0/);
  assert.match(css, /\.entryHelpers \{[^}]*display: flex;[^}]*flex-wrap: wrap;[^}]*gap: 8px;[^}]*width: 100%;[^}]*font: var\(--t-meta\);[^}]*text-align: left/);
});

test('earlier conversations stay at the top of the thread with spacing across domains', () => {
  const toggle = pageElement('historyToggle');
  const conditional = toggle.parent.parent;
  const thread = conditional.parent;
  assert.ok(thread.openingElement.getText().includes('ref={threadRef}'));
  assert.equal(thread.children.filter(ts.isJsxExpression)[0], conditional);
  for (const idea of ['Track purchase orders for builders', 'Organize family board game nights']) {
    let expanded = false;
    const scope = {earlier: [{role: 'user', text: idea}], showEarlier: expanded, setShowEarlier: next => {expanded = next(expanded);}};
    const button = renderElement(conditional.expression, scope);
    assert.equal(button.props.className, 'ghost historyToggle');
    assert.equal(button.props.children, 'Show earlier conversations');
    button.props.onClick();
    assert.equal(expanded, true);
    assert.equal(renderElement(conditional.expression, {...scope, showEarlier: expanded}).props.children, 'Hide earlier conversations');
    assert.equal(renderElement(conditional.expression, {...scope, earlier: []}), false);
  }
  assert.match(fs.readFileSync(`${__dirname}/page.module.css`, 'utf8'), /\.historyToggle \{[^}]*align-self: flex-start;[^}]*flex: none;[^}]*margin-bottom: 12px/);
});

test('the idea welcome keeps its heading and help without example chips across domains', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  const intro = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === 'WelcomeIntro');
  const context = {exports: {}, require, styles: new Proxy({}, {get: (_, key) => key})};
  vm.runInNewContext(ts.transpileModule(`${intro.getText()}\nexports.intro = WelcomeIntro;`, {compilerOptions: {
    target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, context);
  const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
  for (const draft of ['Coordinate warehouse inspections', 'Plan family camping trips']) {
    const welcome = renderElement(pageElement('welcome'), {draft, composerDisabled: false,
      composerStyles: {}, ChatMessageRow: 'row', CharlieMascot: 'mascot', turns: [], WelcomeIntro: context.exports.intro, fileRead: {current: 0}, setDraft() {}, answerRef: {current: null}});
    assert.equal(nodes(welcome).filter(node => node.type === 'button').length, 0);
    const content = nodes(context.exports.intro());
    assert.equal(content.find(node => node.type === 'h2').props.children, 'What is your idea?');
    assert.ok(content.some(node => node.type === 'p' && node.props.className === 'value'));
    assert.ok(content.some(node => node.type === 'p' && node.props.className === 'hint'));
  }
});

test('new conversations keep the real Charlie in their welcome and reply portraits across domains', () => {
  for (const audience of ['Industrial purchasing teams', 'Community choir singers']) {
    const welcome = renderElement(pageElement('welcome'), {composerStyles: {}, ChatMessageRow: 'row', CharlieMascot: 'mascot', WelcomeIntro: 'intro', turns: []});
    assert.equal(welcome.props.children.props.portrait.type, 'mascot', audience);
    const source = fs.readFileSync(`${__dirname}/page.tsx`, 'utf8');
    assert.doesNotMatch(source, /charlie-mark\.png|CharlieFace/);
    assert.match(source, /setCharlieMascot\(busy/);
  }
});

test('entry answer chips reuse the visible bordered chat option style across domains', () => {
  const tree = ts.createSourceFile('page.tsx', fs.readFileSync(`${__dirname}/page.tsx`, 'utf8'), ts.ScriptTarget.Latest, true);
  let chip;
  function find(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(tree) === 'button' && node.openingElement.getText(tree).includes('"onboarding_choice"')) chip = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  for (const choice of ['Warehouse safety teams', 'Family campers']) {
    const sent = [];
    const button = renderElement(chip, {choice, busy: false, selectedChoices: [], draftRef: {current: ''},
      composerStyles: {option: 'chat-option'}, answerQuestion: answer => sent.push(answer), composeIdeaAnswer: options => options.join(', ')});
    assert.equal(button.props.className, 'chat-option');
    assert.equal(button.props['aria-label'], choice);
    button.props.onClick();
    assert.deepEqual(sent, [choice]);
  }
  assert.match(fs.readFileSync(`${__dirname}/../../components/chat/ChatDock.module.css`, 'utf8'), /\.option \{[^}]*border: 1px solid var\(--border, var\(--bg-2\)\)/);
});

