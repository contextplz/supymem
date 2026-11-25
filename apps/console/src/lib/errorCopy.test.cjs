const assert = require('node:assert/strict'), test = require('node:test');

test('F055: local owner authorization has session recovery across error entry points', async () => {
  const { errorCodeCopy, opsErrorMessage } = await import('./errorCopy.ts');
  const { apiFailure, resourceReadFailure } = await import('./apiFailure.ts');
  const previous = global.window;
  global.window = { __SUPYMEM__: { mode: 'local' } };
  try {
    for (const action of ['saving your inventory report', 'saving your trail journal']) {
      for (const code of ['unauthorized', 'content_actor_required', 'signed_out', 'account_changed_during_request']) {
        const copy = errorCodeCopy(code);
        assert.match(copy, /restart your local app/i);
        assert.doesNotMatch(copy, /\bAI\b|model|key|provider|sign in/i);
        if (code === 'account_changed_during_request') assert.match(copy, /Check its saved status before trying again/);
        assert.equal(opsErrorMessage(new Error(`API 401: ${code}`)), copy);
        assert.equal(apiFailure({ ok: false, status: 401, data: { error: { code } } }, action), copy);
      }
      for (const status of [401, 403, 423]) {
        const copy = apiFailure({ ok: false, status, data: null }, action);
        assert.match(copy, /restart your local app/i);
        assert.doesNotMatch(copy, /\bAI\b|model|key|provider|account status/i);
      }
      for (const code of ['content_write_forbidden', 'gtm_write_forbidden', 'import_write_forbidden']) {
        const copy = apiFailure({ ok: false, status: 403, data: { detail: { code } } }, action);
        assert.match(copy, /All ventures/);
        assert.match(copy, /restart your local app/i);
      }
      assert.match(await resourceReadFailure(new Response('{}', { status: 401 }), action), /Restart your local app/);
    }
  } finally { global.window = previous; }
});

test('F055/F037: local provider and publication setup identify the saved screen and fields', async () => {
  const { errorCodeCopy, opsErrorMessage } = await import('./errorCopy.ts');
  const { apiFailure } = await import('./apiFailure.ts');
  const previous = global.window;
  global.window = { __SUPYMEM__: { mode: 'local' } };
  try {
    for (const action of ['drafting your factory checklist', 'drafting your gardening post']) {
      for (const code of ['content_provider_unavailable', 'content_provider_offline', 'gtm_provider_unavailable', 'gtm_llm_unavailable']) {
        const copy = apiFailure({ ok: false, status: 401, data: { detail: { code } } }, action);
        assert.match(copy, /Keys and connections > Your AI provider/);
        assert.match(copy, /key and chat model/);
        assert.doesNotMatch(copy, /owner session/);
      }
      const copy = errorCodeCopy('external_execution_disabled');
      assert.match(copy, /Capabilities/);
      assert.match(copy, /Keys and connections > Connected apps/);
      assert.equal(opsErrorMessage(new Error('API 409: external_execution_disabled')), copy);
      assert.doesNotMatch(copy, /external_execution_disabled|\u2014/);
    }
  } finally { global.window = previous; }
});
