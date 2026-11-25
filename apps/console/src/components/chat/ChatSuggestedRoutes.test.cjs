const assert = require('node:assert/strict');
const test = require('node:test');
const {readFileSync} = require('node:fs');
const {resolve} = require('node:path');
const vm = require('node:vm');
const ts = require('typescript');

const exportsObject = {};
let local = false;
vm.runInNewContext(ts.transpileModule(readFileSync(resolve(__dirname, '../../lib/chatRouteAction.ts'), 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText, {exports: exportsObject, URL, require: id => id === './localMode.ts'
  ? {isLocalConsole: () => local} : require(resolve(__dirname, '../../lib', id))});

test('local work links open the right venture and saved result', () => {
  local = true;
  try {
    for (const venture of ['ven_stock', 'ven_walk']) {
      for (const path of ['builds', 'research?dossier=saved-report#sources', 'experiments']) {
        const href = `/v/${venture}/${path}`;
        const action = {label: 'Open', href};
        assert.equal(exportsObject.inChatActions([action], true, venture)[0], action);
        assert.equal(exportsObject.homeRouteAction(href, venture)?.href, href);
      }
      const activity = `/v/${venture}/device-work-orders/dwo_saved`;
      assert.equal(exportsObject.homeRouteAction(`/v/${venture}/builds?run=dwo_saved`, venture)?.href, activity);
      const invalidRun = `/v/${venture}/builds?run=dwo_%2Felsewhere`;
      assert.equal(exportsObject.homeRouteAction(invalidRun, venture)?.href, invalidRun);
      assert.equal(exportsObject.homeRouteAction(`/v/${venture}/experiments?experiment_id=saved`, venture)?.payload.experiment_id, 'saved');
      const report = `/v/${venture}/research?dossier=saved-report#sources`;
      assert.equal(exportsObject.chatRouteAction({role: 'assistant', body_md: `[Open report](${report})`}, venture)[0]?.href, report);
      assert.equal(exportsObject.chatRouteAction({role: 'assistant', body_md: report}, `${venture}_other`).length, 0);
      assert.equal(exportsObject.homeRouteAction(`/\\foreign.example${report}`, venture), null);
    }
  } finally { local = false; }
});

test('saved home suggestions dispatch next moves instead of navigating to the current page', () => {
  for (const [venture, idea] of [['ven_stock', 'Inventory alerts for warehouse operators'], ['ven_walk', 'Walking groups for neighborhood families']]) {
    for (const suffix of ['', '/', '?chat=1', '#next']) {
      const href = `/v/${venture}${suffix}`;
      const action = exportsObject.inChatActions([{label: `Next step for ${idea}`, href}])[0];
      assert.equal(action.action, 'next_moves', href);
      assert.equal(action.href, undefined);
      assert.equal(exportsObject.homeRouteAction(href, venture).action, 'next_moves');
    }
    assert.equal(exportsObject.homeRouteAction(`/v/${venture}/guide`, venture), null);
    const review = {label: 'Open review', href: `/v/${venture}#attention-review`};
    assert.deepEqual(Array.from(exportsObject.inChatActions([review])), [review]);
    assert.equal(exportsObject.homeRouteAction(review.href, venture), null);
    const target = exportsObject.homeRouteAction(`/v/${venture}_other`, venture);
    assert.ok(target.href.startsWith(`/v/${venture}_other?chat=1&chat_route=`));
  }
});

test('every server hidden suggestion route has the same console dispatcher in unrelated domains', () => {
  const server = readFileSync(resolve(__dirname, '../../../../../services/company_os/app/chat/navigation_map.py'), 'utf8');
  const rows = [...server.matchAll(/\((r"[^"]+"(?:\s*r"[^"]+")*), \{"label": "[^"]+", "action": "([^"]+)"/g)]
    .map(([, pattern, action]) => [[...pattern.matchAll(/r"([^"]+)"/g)].map(match => match[1]).join(''), action]);
  assert.ok(rows.length >= 10);
  for (const venture of ['ven_stock', 'ven_walk']) {
    for (const suffix of ['idea', 'research', 'connections', 'site?build=1', 'site', 'experiments', 'context',
      'suggestions', 'org', 'people', 'now', 'tasks', 'plan', 'builds']) {
      const href = `/v/${venture}${suffix ? `/${suffix}` : ''}`;
      const matched = rows.find(([pattern]) => new RegExp(pattern).test(href));
      assert.ok(matched, href);
      const action = exportsObject.inChatActions([{label: 'Review', href}])[0];
      assert.equal(action.action, matched[1], href);
    }
  }
});
