import assert from "node:assert/strict";
import test from "node:test";
import { sanitizeDiagramSvg } from "./diagramSanitize.ts";

test("strips scripts, foreign objects, iframes and their contents", () => {
  const sanitized = sanitizeDiagramSvg('<svg><script>alert(1)</script><foreignObject><div>hidden</div></foreignObject><iframe>hidden</iframe><rect width="20"/></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /script|alert|foreignObject|iframe|hidden/);
  assert.match(sanitized, /<rect width="20"\/>/);
});

test("removes external image/use nodes and hrefs while retaining fragment references", () => {
  const sanitized = sanitizeDiagramSvg('<svg xmlns:xlink="http://www.w3.org/1999/xlink"><image href="https://example.com/x"/><use xlink:href="//example.com/x"/><a href="javascript:alert(1)">safe text</a><image href="data:image/png;base64,AA"/><use href="#local"/><use xlink:href="#other"/></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /https:|example|javascript|<image|xlink/);
  assert.match(sanitized, /<a>safe text<\/a>/);
  assert.match(sanitized, /<use href="#local"\/>/);
  assert.match(sanitized, /<use href="#other"\/>/);
});

test("strips all event handlers except an exact plain-text sendPrompt onclick", () => {
  const sanitized = sanitizeDiagramSvg(`<svg onload="alert(1)"><g onclick="sendPrompt('Explain this node')" onmouseover="alert(2)" onfocus="sendPrompt('No')"><text>Node</text></g></svg>`);
  assert.ok(sanitized);
  assert.match(sanitized, /onclick="sendPrompt\('Explain this node'\)"/);
  assert.doesNotMatch(sanitized, /onload|onmouseover|onfocus|alert/);
});

test("rejects handler injection, forbidden characters, whitespace and oversized prompts", () => {
  for (const handler of ["sendPrompt('x');alert(1)", "sendPrompt('a\\b')", "sendPrompt('(x)')", "sendPrompt('&lt;x&gt;')", "sendPrompt('&quot;x&quot;')", " sendPrompt('x')", `sendPrompt('${"x".repeat(201)}')`, "sendPrompt('x')&#10;", "sendPrompt('a&#39;b')"]) {
    assert.doesNotMatch(sanitizeDiagramSvg(`<svg><g onclick="${handler}"/></svg>`) || "", /onclick/, handler);
  }
  assert.match(sanitizeDiagramSvg(`<svg onclick="sendPrompt('${"x".repeat(200)}')"/>`) || "", /onclick/);
});

test("decodes entities before checking attributes and safely re-escapes text", () => {
  const sanitized = sanitizeDiagramSvg('<svg><a href="&#106;avascript:alert(1)"/><g onclick="sendPrompt(&apos;Sales &amp; support&apos;)"><text>&lt;safe&gt; &amp; &#128512;</text></g></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /href=/);
  assert.match(sanitized, /onclick="sendPrompt\('Sales &amp; support'\)"/);
  assert.match(sanitized, /&lt;safe&gt; &amp; 😀/);
});

test("strips unsafe inline styles but retains ordinary styles", () => {
  const sanitized = sanitizeDiagramSvg('<svg><rect style="fill:url(#local)"/><rect style="fill:URL (https://example.com)"/><rect style="width:expression(alert(1))"/><rect style="fill:u\\72l(#x)"/><rect style="fill:red;stroke-width:2"/></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /url|URL|expression|\\72/);
  assert.match(sanitized, /style="fill:red;stroke-width:2"/);
});

test("drops animation, untrusted styles, HTML breakout nodes and external namespace bases", () => {
  const sanitized = sanitizeDiagramSvg('<svg xml:base="https://example.com"><style>@import "https://example.com";</style><set attributeName="onclick" to="alert(1)"/><animate attributeName="href" values="javascript:alert(1)"/><textarea>&lt;/svg&gt;</textarea><g xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></g><text>Kept</text></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /style|set |animate|textarea|script|alert|xml:base|example|xhtml/);
  assert.match(sanitized, /<text>Kept<\/text>/);
});

test("rejects malformed, non-SVG, multi-root, entity and declaration input", () => {
  for (const input of ["", " <svg/>", "<svgx/>", "<div/>", "<svg><g></svg>", "<svg/ ><svg/>", '<svg width="1" width="2"/>', '<svg onclick=alert(1)/>', '<svg><rect title="<x"/></svg>', "<svg>&unknown;</svg>", "<svg>&#0;</svg>", "<svg>\u0000</svg>", "<svg><!DOCTYPE x></svg>", "<svg><?xml x?></svg>", '<svg xmlns="https://example.com"/>', "<svg><!--unterminated</svg>", "<svg>text]]></svg>"]) {
    assert.equal(sanitizeDiagramSvg(input), null, input);
  }
});

test("enforces the 64 KiB UTF-8 boundary, including multi-byte text", () => {
  assert.ok(sanitizeDiagramSvg(`<svg>${"x".repeat(65536 - 11)}</svg>`));
  assert.equal(sanitizeDiagramSvg(`<svg>${"x".repeat(65536 - 10)}</svg>`), null);
  assert.equal(sanitizeDiagramSvg(`<svg>${"😀".repeat(20000)}</svg>`), null);
});

test("rejects multiple roots, invalid comments and lone Unicode surrogates", () => {
  for (const input of ["<svg/><svg/>", "<svg><!--bad--comment--></svg>", "<svg><!--bad---></svg>", "<svg>\ud800</svg>", "<svg>\udfff</svg>"]) assert.equal(sanitizeDiagramSvg(input), null);
  assert.ok(sanitizeDiagramSvg("<svg>😀</svg>"));
});

test("comments and CDATA cannot smuggle executable markup", () => {
  const sanitized = sanitizeDiagramSvg('<svg><!-- <script>alert(1)</script> --><text><![CDATA[<script>not executable</script>]]></text></svg>');
  assert.ok(sanitized);
  assert.doesNotMatch(sanitized, /<!--|<script/);
  assert.match(sanitized, /&lt;script&gt;not executable&lt;\/script&gt;/);
});

test("uses native DOMParser validation when available and fails closed on parser errors", () => {
  const previous = globalThis.DOMParser;
  let parserError = false;
  let throws = false;
  const calls: string[][] = [];
  globalThis.DOMParser = class {
    parseFromString(input: string, format: string) {
      calls.push([input, format]);
      if (throws) throw new Error("Parser unavailable");
      return { querySelector: () => parserError ? {} : null, documentElement: { localName: "svg", namespaceURI: "http://www.w3.org/2000/svg" } };
    }
  } as unknown as typeof DOMParser;
  try {
    assert.ok(sanitizeDiagramSvg("<svg/>"));
    assert.deepEqual(calls, [["<svg/>", "image/svg+xml"]]);
    parserError = true;
    assert.equal(sanitizeDiagramSvg("<svg/>"), null);
    parserError = false;
    throws = true;
    assert.equal(sanitizeDiagramSvg("<svg/>"), null);
  } finally {
    if (previous) globalThis.DOMParser = previous;
    else Reflect.deleteProperty(globalThis, "DOMParser");
  }
});

test("keeps server-rendered spec diagrams intact: wrapped labels, node prompts and arrows", () => {
  const rendered = '<svg width="100%" viewBox="0 0 680 210" role="img"><title>customer journey</title><desc>Spot then Pilot</desc><defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M2 1L8 5L2 9" fill="none" stroke="context-stroke"/></marker></defs><g class="node c-teal" onclick="sendPrompt(\'Tell me more about Spot &amp; rebook\')"><rect x="200" y="40" width="280" height="64" rx="8" stroke-width="0.5"/><text class="th" x="340" y="63" text-anchor="middle" dominant-baseline="central">Spot patients needing an</text><text class="th" x="340" y="81" text-anchor="middle" dominant-baseline="central">appointment &lt;follow up&gt;</text></g><path class="arr" d="M340 104V118H340V156" fill="none" marker-end="url(#arrow)"/></svg>';
  const sanitized = sanitizeDiagramSvg(rendered);
  assert.ok(sanitized);
  for (const kept of [/viewBox="0 0 680 210"/, /onclick="sendPrompt\('Tell me more about Spot &amp; rebook'\)"/, /appointment &lt;follow up&gt;/, /marker-end="url\(#arrow\)"/, /dominant-baseline="central"/]) assert.match(sanitized, kept);
});
