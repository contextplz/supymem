const assert = require('node:assert/strict'), test = require('node:test'), vm = require('node:vm');
const {readFileSync} = require('node:fs'), ts = require('typescript');
test('36: your browser approval uses plain role names without losing provider, limits or data-transfer disclosures', async () => {
  const profileModule = await import('../../lib/browserExecutionProfile.ts');
  const exports = {}, jsx = (type, props) => ({type, props});
  vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/BrowserExecutionProfileReview.tsx`, 'utf8'), {compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  }}).outputText, {exports, require(id) {
    if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
    if (id.endsWith('/browserExecutionProfile')) return profileModule;
    if (id.endsWith('/localMode')) return {isLocalConsole: () => false};
    return {};
  }});
  for (const domain of ['Fleet operators', 'Outdoor hobbyists']) {
    const role = max_output_tokens => ({provider: 'openai', model: 'gpt-6-sol', service_tier: 'default', reasoning: 'none', max_output_tokens});
    const profile = {version: 'browser_cuai_text_v1', backend: 'cuai', policy_version: 1,
      roles: {plan: role(700), actor: role(600), verify: role(300)}, pricing_digest: 'a'.repeat(64),
      max_provider_calls: 64, max_request_bytes: 65536, completion_policy: 'browser_semantic_review_v1'};
    const tree = JSON.stringify(exports.BrowserExecutionProfileReview({value: profile, className: domain}));
    for (const label of ['Planning (Planner)', 'Taking actions (Actor)', 'Checking results (Verifier)', 'OpenAI', 'gpt-6-sol', 'output tokens', 'Page text']) assert.ok(tree.includes(label), label);
    assert.doesNotMatch(tree, /Saved backend|CUAI|"Actor"|"Verifier"/);
  }
});

test('your local browser guidance hides accounting while retaining limits and hosted copy across domains', async () => {
  const profileModule = await import('../../lib/browserExecutionProfile.ts');
  for (const domain of ['Warehouse routing', 'Weekend cooking']) {
    for (const local of [true, false]) {
      const exports = {}, jsx = (type, props) => ({type, props});
      vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/BrowserExecutionProfileReview.tsx`, 'utf8'), {compilerOptions: {
        module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
      }}).outputText, {exports, require(id) {
        if (id === 'react/jsx-runtime') return {jsx, jsxs: jsx};
        if (id.endsWith('/browserExecutionProfile')) return profileModule;
        if (id.endsWith('/localMode')) return {isLocalConsole: () => local};
        return {};
      }});
      const role = {provider: 'openai', model: 'gpt-6-sol', service_tier: 'default', reasoning: 'none', max_output_tokens: 300};
      const profile = {version: 'browser_cuai_text_v1', backend: 'cuai', policy_version: 1,
        roles: {plan: role, actor: role, verify: role}, pricing_digest: 'a'.repeat(64),
        max_provider_calls: 64, max_request_bytes: 65536, completion_policy: 'browser_semantic_review_v1'};
      const trialBinding = {schema_version: 1, actor_id: 'actor', device_id: 'device', workspace_id: 'workspace', venture_id: 'venture',
        trial_id: 'trial', task_id: 'task', order_id: 'order', expires_at: 2000000000,
        catalog_digest: 'a'.repeat(64), seed_digest: 'b'.repeat(64), review_digest: 'c'.repeat(64), profile_digest: 'd'.repeat(64), binding_digest: 'e'.repeat(64)};
      const tree = JSON.stringify(exports.BrowserExecutionProfileReview({value: profile, className: domain, trialBinding}));
      assert.match(tree, /Provider call limit/);
      assert.match(tree, /Page text/);
      assert.match(tree, /cannot be replayed/);
      if (local) assert.doesNotMatch(tree, /credits?|\$10|pricing/i);
      else assert.match(tree, /Failed or uncertain attempts may consume the limit and reserved credits/);
    }
  }
});
