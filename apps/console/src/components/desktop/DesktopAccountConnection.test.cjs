const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');
const test = require('node:test');
const source = readFileSync(join(__dirname, 'DesktopAccountConnection.tsx'), 'utf8');
const shell = readFileSync(join(__dirname, '../layout/AppShell.tsx'), 'utf8');

test('signed-out Mac offers one plain Sign in and a secondary copy link', () => {
  assert.match(shell, /<h1>Sign in to Supymem<\/h1>/);
  assert.match(shell, /Sign in with your browser to continue\./);
  assert.doesNotMatch(shell, /Connect to your company|Connecting to your company/);
  assert.doesNotMatch(source, /Connect this Mac|Restart browser sign-in/);
  assert.match(source, /"Start sign-in again" : "Sign in"/);
  assert.match(source, /className="ghost sm"[^>]*onClick=\{\(\) => void copyLoginLink\(\)\}/);
  assert.match(source, /!signIn && <h3/);
  assert.match(source, /act\("desktop_auth_start"\)/);
});

test('startup and shell never mount the active permission probe', () => {
  assert.doesNotMatch(shell, /<DesktopPermissionSetup/);
});

test('raw native auth codes such as refresh_rejected_or_uncertain are never shown', () => {
  assert.match(source, /status\?\.error && !\/\^\[a-z0-9_\.:-\]\+\$\/\.test\(status\.error\)/);
  const looksLikeCode = (value) => /^[a-z0-9_.:-]+$/.test(value);
  assert.equal(looksLikeCode('refresh_rejected_or_uncertain'), true);
  assert.equal(looksLikeCode('Authorization callback did not match this sign-in.'), false);
});
