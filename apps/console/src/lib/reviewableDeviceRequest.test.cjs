const assert = require('node:assert/strict'), test = require('node:test');

test('local coding requests stay in server chat while Mac controls remain device tasks across domains', async () => {
  const {reviewableDeviceRequest} = await import('./reviewableDeviceRequest.ts');
  for (const request of [
    'Rebuild my landing page with Claude Code',
    'Change the headline on my page with Codex',
    'Use Cursor to rebuild my page',
    'build the first version of my product with Claude Code',
    'update my site',
    'Redo the warehouse scheduling website with OpenCode',
    'Fix the family recipe app with Cursor on this Mac',
    'Improve the code for supplier invoices with Codex',
    'Edit the page for home gardening with Claude Code',
    'Create a scraper for wholesale catalogues with OpenCode',
    'Write a small experiment tool for music lessons with Codex',
  ]) assert.equal(reviewableDeviceRequest(request, true), false, request);
  for (const request of [
    'Open Notes on this Mac',
    'Use my Mac to open Notes and click its new note button',
    'Use Claude Code to click Settings on this Mac',
    'Use my Mac to open https://example.test and report its page title. Do not edit or change the page.',
    'Inspect the workspace with Claude Code, read-only; do not edit files.',
  ]) assert.equal(reviewableDeviceRequest(request, true), true, request);
  assert.equal(reviewableDeviceRequest('Build the warehouse scheduling app with Codex'), true);
});
