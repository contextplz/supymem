const assert = require('node:assert/strict'), test = require('node:test');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm'), ts = require('typescript');
const read = file => fs.readFileSync(path.resolve(__dirname, file), 'utf8');
const nodes = node => !node || typeof node !== 'object' ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
function render(mode, messages, name, proposal, config = {}) {
  const exports = {}, jsx = (type, props) => ({type, props});
  const mocks = {
    react: {useContext: () => mode, useState: value => [typeof value === 'function' ? value() : value, () => {}], useRef: current => ({current}), useEffect() {}},
    'react/jsx-runtime': {jsx, jsxs: jsx}, '@/lib/founderUI': {FounderUIContext: {}},
    '@/components/chat/useVentureChat': {useVentureChat: () => ({messages, contextCurrent: () => true})},
    '@/components/chat/useChatHistory': {useChatHistory() {}}, '@/components/chat/ChatDock': {ChatDock: 'dock'},
    '@/components/chat/ChatLink': {default: 'a'}, '@/components/primitives/ErrorNotice': {ErrorNotice: 'error'},
    '@/lib/apiFailure': {apiFailure: () => 'You have reached your usage limit. Buy more in Settings, then retry.'},
    '@/lib/chatEntry': {committedEntryHref: () => '/v/id/idea', entryReviewLabel: () => 'Review idea details', resumableEntry: () => true},
    '@/lib/ideaChatPoll': {ideaConversationActivity: () => null},
    ...config.mocks,
  };
  vm.runInNewContext(ts.transpileModule(read('ConfirmedConversation.tsx'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports, crypto: require('node:crypto').webcrypto, require: id => id.endsWith('.css') ? {default: new Proxy({}, {get: (_, key) => key})} : mocks[id] || {}});
  return exports.ConfirmedConversation({initialName: name, receipt: {venture_id: 'id', workspace_id: 'workspace', route: config.route || 'startup', action: {href: '/v/id/idea'}},
    initialDirection: proposal && {text: name, proposal: {...proposal, target_venture_id: 'id'}}});
}

test('05: your entry conversation passes chat actions through only in the current conversation', async () => {
  for (const domain of ['Water quality monitoring', 'Family recipe sharing']) {
    const states = [], calls = []; let cursor = 0, current = true;
    const mocks = {
      react: {useContext: () => 'home', useRef: initial => ({current: initial}), useEffect() {},
        useState(initial) {const index = cursor++; if (!(index in states)) states[index] = typeof initial === 'function' ? initial() : initial;
          return [states[index], value => {states[index] = typeof value === 'function' ? value(states[index]) : value;}];}},
      '@/components/chat/useVentureChat': {useVentureChat: () => ({messages: [], contextCurrent: () => current,
        confirmChatAction: async action => {calls.push(action); return true;}})},
    };
    const dock = () => {cursor = 0; return nodes(render('home', [], domain, null, {mocks})).find(node => node.type === 'dock');};
    assert.equal(await dock().props.onAction('chat.prefill', {text: domain}), true);
    assert.deepEqual(calls, ['chat.prefill']);
    current = false; assert.equal(await dock().props.onAction('chat.prefill', {text: domain}), false);
    assert.deepEqual(calls, ['chat.prefill']);
  }
});

test('your requested direction commits its saved proposal directly for typed and voice requests in unrelated domains', async () => {
  for (const domain of ['Factory procurement', 'Family cycling']) for (const voice of [false, true]) {
    const requests = [], proposal = {id: 'requested-direction', route: 'gtm', target_venture_id: 'id', state: 'pending', message: domain};
    const receipt = {status: 'committed', venture_id: 'id', workspace_id: 'workspace', route: 'gtm', next_step: 'gtm',
      action: {kind: 'gtm_review', venture_id: 'id', href: '/gtm?venture=id'}, message: `Your direction for ${domain} is saved. Ask for your next step.`};
    const dock = nodes(render('home', [], domain, null, {mocks: {
      '@/lib/chatEntry': require('../../lib/chatEntry.ts'),
      '@/lib/client': {api: async (url, init) => {requests.push({url, init}); return {ok: true,
        data: url.endsWith('/preview') ? {status: 'proposal', proposal} : url.endsWith('/confirm') ? receipt : {proposal}};}},
      '@/components/chat/useVentureChat': {useVentureChat: () => ({messages: [], contextCurrent: () => true, reloadHistory: async () => {}})},
    }})).find(node => node.type === 'dock');
    const result = voice ? await dock.props.onVoiceSend(`switch to ${domain}`) : await dock.props.onSend(`switch to ${domain}`);
    assert.equal(requests.filter(request => request.url.endsWith('/confirm')).length, 1);
    if (voice) {assert.equal(result.admitted, true); assert.equal(result.body_md, receipt.message); assert.deepEqual(JSON.parse(JSON.stringify(result.actions)), []);}
    else assert.equal(result, true);
  }
});

test('typed and voice-origin turns share entry routing, ordinary requests and GTM followups across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    for (const scenario of ['ordinary', 'switch', 'proposal', 'committing', 'clarify', 'clarify_no_id', 'followup']) {
      const runs = [];
      for (const voiceOrigin of [false, true]) {
        const requests = [], admitted = [], response = {id: 'reply', role: 'assistant', body_md: `17 reported responses for ${domain}, not verified users.`,
          actions: [{label: 'Review', href: '/v/id/review'}], citations: [{label: 'source.example', href: 'https://source.example/report'}]};
        const proposal = {id: 'proposal', route: 'startup', target_venture_id: 'id', message: response.body_md, state: scenario === 'committing' ? 'committing' : 'pending'};
        const receipt = {status: 'committed', route: 'startup', venture_id: 'id', workspace_id: 'workspace', next_step: 'idea',
          action: {kind: 'idea', venture_id: 'id', href: '/v/id/idea'}, message: response.body_md};
        const preview = ['proposal', 'committing'].includes(scenario) ? {status: 'proposal', proposal}
          : ['clarify', 'clarify_no_id'].includes(scenario) ? {status: 'clarify', ...(scenario === 'clarify' ? {id: 'question'} : {}), question: `Which audience for ${domain}?`, choices: ['Teams', 'Individuals']}
          : {status: 'reply', ...response};
        const dock = nodes(render('home', [], domain, null, {route: scenario === 'followup' ? 'gtm' : 'startup', mocks: {
          '@/lib/chatEntry': require('../../lib/chatEntry.ts'),
          '@/lib/client': {api: async (url, options) => {requests.push({url, body: JSON.parse(options?.body || '{}')}); return {ok: true,
            data: url.endsWith('/preview') ? preview : url.endsWith('/confirm') ? receipt : url.includes('/proposals/') ? {proposal} : response};}},
          '@/lib/gtmConversationSend': {gtmSendMode: () => 'followup'},
          '@/components/chat/useVentureChat': {useVentureChat: () => ({messages: [], contextCurrent: () => true, reloadHistory: async () => {},
            sendChat: async (text, options) => {admitted.push({text, options}); options?.onReply?.(response); return true;}})},
        }})).find(node => node.type === 'dock');
        const text = ['switch', 'proposal', 'committing', 'clarify', 'clarify_no_id'].includes(scenario) ? `switch to ${domain}` : `What did you find for ${domain}?`;
        const reply = voiceOrigin ? await dock.props.onVoiceSend(text, {idempotencyKey: 'voice_turn'}) : await dock.props.onSend(text);
        if (voiceOrigin) {
          assert.equal(reply.admitted, true);
          if (['proposal', 'committing'].includes(scenario)) assert.deepEqual(JSON.parse(JSON.stringify(reply.actions)), []);
          else if (['clarify', 'clarify_no_id'].includes(scenario)) {
            assert.equal(reply.body_md, preview.question);
            assert.equal(reply.actions.length, 2);
            assert.equal(reply.actions[0].payload.question_id, reply.id);
            if (scenario === 'clarify') assert.equal(reply.id, 'entry-direction:question');
            else assert.notEqual(reply.id, 'entry-direction:undefined');
          } else assert.deepEqual(JSON.parse(JSON.stringify(reply.actions)), response.actions);
        }
        if (scenario === 'ordinary') {
          assert.equal(admitted[0].text, text);
          if (voiceOrigin) {assert.equal(admitted[0].options.source, 'voice'); assert.equal(admitted[0].options.idempotencyKey, 'voice_turn');}
        } else assert.equal(admitted.length, 0);
        runs.push(requests.map(request => ({url: request.url, body: {...request.body, request_id: undefined, source_kind: undefined}})));
      }
      assert.deepEqual(runs[0], runs[1]);
    }
  }
});

test('your spoken diagnosis request uses the same direct work path across domains', async () => {
  for (const domain of ['Supplier compliance reviews', 'Neighborhood plant sharing']) {
    let calls = 0;
    const dock = nodes(render('home', [], domain, null, {route: 'gtm', mocks: {
      '@/lib/gtmConversationSend': {gtmSendMode: () => 'quote'},
      '@/lib/client': {api: async url => {calls++; assert.equal(url, '/api/ventures/id/chat/gtm/quote'); return {ok: true,
        data: {id: 'requested', role: 'assistant', body_md: `Your diagnosis for ${domain} has started. Check its progress.`, actions: []}};}},
      '@/components/chat/useVentureChat': {useVentureChat: () => ({messages: [], contextCurrent: () => true, reloadHistory: async () => {}})},
    }})).find(node => node.type === 'dock');
    const reply = await dock.props.onVoiceSend(`Prepare a diagnosis for ${domain}`, {idempotencyKey: 'voice_turn'});
    assert.equal(calls, 1);
    assert.equal(reply.admitted, true);
    assert.match(reply.body_md, /has started/);
    assert.deepEqual(Array.from(reply.actions), []);
  }
});

test('entry proposal copy and continuation actions render as a Charlie message, never above the conversation', () => {
  for (const name of ['Warehouse invoice reconciliation', 'Family recipe sharing']) {
    for (const state of ['pending', 'committing']) {
      const message = `Review your direction for ${name}.`;
      const all = nodes(render('home', [{id: 'earlier', role: 'assistant', body_md: 'Your saved conversation'}], name,
        {id: 'proposal', state, message}));
      const dock = all.find(node => node.type === 'dock');
      const proposal = dock.props.messages.at(-1);
      assert.equal(proposal.role, 'assistant');
      assert.equal(proposal.id, 'entry-direction-proposal:proposal');
      assert.equal(proposal.body_md.startsWith(message), true);
      assert.equal(proposal.actions[0].action, 'entry.direction.confirm');
      assert.equal(proposal.actions[0].style, 'primary');
      assert.equal(proposal.actions.some(action => action.action === 'entry.direction.back'), state === 'pending');
      assert.equal(all.some(node => node.type === 'article' || node.type === 'button' || node.type === 'p' && node.props.children === message), false);
      const source = read('../../components/chat/ChatDock.tsx');
      assert.match(source, /own \? styles\.bubbleOwn : styles\.bubble/);
      assert.match(source, /actionPlan\.visible\.length > 0/);
    }
  }
});

test('short and long new conversations retain a shrinking flex chain to the scrolling list and docked composer', () => {
  for (const idea of ['Warehouse invoice reconciliation', 'Family recipe sharing']) {
    for (const count of [1, 120]) {
      const tree = render('home', Array.from({length: count}, (_, index) => ({id: String(index), body_md: idea})), idea);
      const all = nodes(tree), wrapper = all.find(node => node.props?.className === 'chatDock');
      assert.equal(wrapper.props.children.type, 'dock');
      assert.equal(wrapper.props.children.props.messages.length, count);
      assert.equal(all.find(node => node.props?.className === 'ventureThread').props.children.flat().includes(wrapper), true);
      const css = read('page.module.css');
      for (const selector of ['confirmedChat', 'ventureThread', 'chatDock']) {
        const rule = css.match(new RegExp(`\\.${selector}\\s*\\{([^}]+)`))[1];
        assert.match(rule, /display: flex/); assert.match(rule, /flex-direction: column/); assert.match(rule, /min-height: 0/);
      }
      const dock = read('../../components/chat/ChatDock.module.css');
      assert.match(dock, /\.homePanel\s*\{[^}]*flex: 1;[^}]*height: auto/);
      assert.match(dock, /\.messages\s*\{[^}]*flex: 1;[^}]*min-height: 0;[^}]*overflow: auto/);
      assert.match(dock, /\.composer\s*\{[^}]*flex-shrink: 0/);
    }
  }
});

test('chat-first conversations omit the legacy venture header and its review-page link', () => {
  for (const name of ['Warehouse invoice reconciliation', 'Family recipe sharing']) {
    const home = nodes(render('home', [], name));
    assert.equal(home.some(node => node.type === 'header' || node.type === 'a'), false);
    const classic = nodes(render('classic', [], name));
    assert.equal(classic.find(node => node.type === 'h2').props.children, name);
    assert.equal(classic.find(node => node.type === 'a').props.href, '/v/id/idea');
    assert.match(read('page.tsx'), /!home && <header className=\{styles.context\}/);
  }
});
