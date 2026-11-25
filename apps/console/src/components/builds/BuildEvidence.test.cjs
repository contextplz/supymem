const assert = require('node:assert/strict');
const {test} = require('node:test');
const {resourceReadFailure} = require('../../lib/apiFailure.ts');

test('your local preview failures give a recovery step without raw codes or private details across domains', async () => {
  const previous = process.env.NEXT_PUBLIC_SUPYMEM_MODE;
  process.env.NEXT_PUBLIC_SUPYMEM_MODE = 'local';
  try {
    for (const resource of ['Your warehouse preview', 'Your garden lesson report']) {
      for (const [status, code] of [[503, 'keychain_unavailable'], [404, 'missing_report'], [409, 'hosting_inputs_changed'], [401, 'unauthorized'], [403, 'device_coding_loop_unavailable'], [429, 'research_provider_rate_limited']]) {
        const response = new Response(JSON.stringify({error: {code, message: 'private-detail'}}),
          {status, headers: {'content-type': 'application/json'}});
        const message = await resourceReadFailure(response, resource);
        assert.ok(message.startsWith(`${resource} could not be read.`));
        assert.match(message, /Open|Refresh|Restart/i);
        assert.doesNotMatch(message, /private-detail|keychain_|missing_report|hosting_|unauthorized|\b(?:401|403|404|409|429|503)\b|AI service settings|not enabled for this account yet/);
        assert.equal(response.bodyUsed, false);
      }
    }
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SUPYMEM_MODE;
    else process.env.NEXT_PUBLIC_SUPYMEM_MODE = previous;
  }
});
