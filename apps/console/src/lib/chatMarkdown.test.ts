import assert from "node:assert/strict";
import test from "node:test";

import { renderLightMd, splitFences } from "./chatMarkdown.ts";

test("splits fenced blocks from prose", () => {
  assert.deepEqual(splitFences("Intro\n\n```text\nDraft 1\nHi\n```\n\n```text\nDraft 2\n```"), [
    { code: false, text: "Intro\n\n" },
    { code: true, text: "Draft 1\nHi" },
    { code: true, text: "Draft 2" },
  ]);
  assert.deepEqual(splitFences("plain **text**"), [{ code: false, text: "plain **text**" }]);
});

test("renders a saved task link beside its evidence text", () => {
  assert.equal(
    renderLightMd("Its outcome remains unverified. [Review the saved task](/v/ven_1/device-work-orders/ord_2) for the exact result."),
    'Its outcome remains unverified. <a href="/v/ven_1/device-work-orders/ord_2">Review the saved task</a> for the exact result.',
  );
});

test("escapes HTML in text, labels, and link destinations", () => {
  assert.equal(
    renderLightMd('<img src=x onerror=alert(1)> [<b>Review</b>](/v/ven_1?x=1&y="2")'),
    '&lt;img src=x onerror=alert(1)&gt; <a href="/v/ven_1?x=1&amp;y=&quot;2&quot;">&lt;b&gt;Review&lt;/b&gt;</a>',
  );
  assert.equal(renderLightMd('[Run](javascript:alert) [Bad](//evil.test)'), '[Run](javascript:alert) [Bad](//evil.test)');
});

test("links bare http(s) URLs, trimming trailing punctuation", () => {
  const a = (u: string) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`;
  assert.equal(
    renderLightMd("Live at https://fixture-grooming.supymem.com/ and https://gateway.supymem.com/public/waitlists/ven_sample000001."),
    `Live at ${a("https://fixture-grooming.supymem.com/")} and ${a("https://gateway.supymem.com/public/waitlists/ven_sample000001")}.`,
  );
  assert.equal(renderLightMd("(see https://a.test/x), ok"), `(see ${a("https://a.test/x")}), ok`);
  assert.equal(renderLightMd("https://en.wikipedia.org/wiki/A_(b)"), a("https://en.wikipedia.org/wiki/A_(b)"));
  assert.equal(renderLightMd("https://a.test/?x=1&y=2"), '<a href="https://a.test/?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">https://a.test/?x=1&amp;y=2</a>');
});

test("does not double-link URLs in markdown links or code, nor accept other schemes", () => {
  assert.equal(renderLightMd("[site](https://a.test) `https://b.test`"), '<a href="https://a.test" target="_blank" rel="noopener noreferrer">site</a> <code>https://b.test</code>');
  assert.equal(renderLightMd("javascript:alert(1) ftp://x.test https://"), "javascript:alert(1) ftp://x.test https://");
});

test("keeps Markdown link syntax inside code and renders existing emphasis", () => {
  assert.equal(renderLightMd('`[example](/v/test)` and **strong** *em*'), '<code>[example](/v/test)</code> and <strong>strong</strong> <em>em</em>');
});

test("normalizes displayed prose and link labels while preserving raw URLs and inline code", () => {
  const raw = '**Saved — review it** [Open—review](/v/ven_1?value=a—b) https://example.test/a—b `scope—token`';
  assert.equal(renderLightMd(raw), '<strong>Saved, review it</strong> <a href="/v/ven_1?value=a—b">Open, review</a> <a href="https://example.test/a—b" target="_blank" rel="noopener noreferrer">https://example.test/a—b</a> <code>scope—token</code>');
  assert.equal(raw, '**Saved — review it** [Open—review](/v/ven_1?value=a—b) https://example.test/a—b `scope—token`');
  assert.equal(renderLightMd('[Run — again](javascript:unsafe—token)'), '[Run, again](javascript:unsafe—token)');
});

test("historical prose rendering never changes fenced code or stored message segments", () => {
  const raw = 'Before — after\n```js\nconst id = "actor—one";\n```\nSaved — pending';
  const segments = splitFences(raw);
  assert.deepEqual(segments, [{code: false, text: 'Before — after\n'},
    {code: true, text: 'const id = "actor—one";'}, {code: false, text: '\nSaved — pending'}]);
  assert.equal(renderLightMd(segments[0].text), 'Before, after\n');
  assert.equal(renderLightMd(segments[2].text), '\nSaved, pending');
  assert.equal(segments[1].text, 'const id = "actor—one";');
});
