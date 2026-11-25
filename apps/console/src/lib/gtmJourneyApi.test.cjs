const assert = require('node:assert/strict'), test = require('node:test');

test('F056: local journey setup errors use your saved email and booking settings', async () => {
  const { createJourneyClient, journeySendReason } = await import('./gtmJourneyApi.ts');
  const previous = global.window;
  global.window = { __SUPYMEM__: { mode: 'local' } };
  try {
    for (const ventureId of ['industrial_scheduling', 'home_gardening']) {
      const owner = { actorId: 'owner', workspaceId: 'workspace', ventureId };
      for (const [code, expected] of [
        ['gtm_gmail_required', /Keys and connections > Email/],
        ['gtm_gmail_missing', /Keys and connections > Email/],
        ['outbound_execution_disabled', /Send email in Capabilities/],
        ['outbound_rate_configuration_missing', /Keys and connections > Email/],
        ['gtm_booking_link_required', /Keys and connections > Email > Booking link/],
        ['gtm_booking_link_invalid', /full https link.*Keys and connections > Email > Booking link/],
        ['gtm_llm_unavailable', /Keys and connections > Your AI provider/],
      ]) {
        const copy = journeySendReason(code);
        assert.match(copy, expected);
        assert.doesNotMatch(copy, /Gmail|Ask Supymem|vendor|\u2014/);
        const client = createJourneyClient(async () => ({ ok: false, status: 409, data: { detail: { code } } }));
        await assert.rejects(client.get(owner), error => error.message === copy);
      }
      const client = createJourneyClient(async () => ({ ok: false, status: 401, data: null }));
      await assert.rejects(client.get(owner), /restart your local app/i);
    }
  } finally { global.window = previous; }
});

test('F050: send cap and sample blockers keep actionable safe recovery without error detail', async () => {
  const { createJourneyClient, journeySendReason } = await import('./gtmJourneyApi.ts');
  const previous = global.window;
  global.window = { __SUPYMEM__: { mode: 'local' } };
  try {
    for (const ventureId of ['industrial_scheduling', 'home_gardening']) {
      const owner = { actorId: 'owner', workspaceId: 'workspace', ventureId };
      const copy = journeySendReason('gtm_send_blocked');
      assert.match(copy, /Find customers/);
      assert.match(copy, /daily sending limit|sample/i);
      const client = createJourneyClient(async () => ({ ok: false, status: 409,
        data: { detail: { code: 'gtm_send_blocked', message: 'private provider detail' } } }));
      await assert.rejects(client.send(owner, { batch_id: 'batch', approval_digest: 'digest' }, ['draft'], 1), error => error.message === copy);
      assert.doesNotMatch(copy, /private|gtm_send_blocked|\u2014/);
      assert.match(journeySendReason('gtm_prospect_blocked'), /source, country and permission/);
    }
  } finally { global.window = previous; }
});
