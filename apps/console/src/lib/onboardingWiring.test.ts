import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { guideContent } from "./capabilityGuide.ts";

const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");

test("Mac sign-in switches to the paste-code step on its own", () => {
  const c = read("../components/desktop/DesktopAccountConnection.tsx");
  assert.ok(c.includes("signInPhase") && c.includes("CODE_STEP_AFTER_MS"));
  assert.ok(c.includes("Paste the code from your browser"));
  assert.ok(c.includes('"focus"'), "re-reads native state when the founder returns from the browser");
});

test("invite entry normalises pasted codes and survives the redirect", () => {
  const shell = read("../components/layout/AppShell.tsx");
  assert.ok(shell.includes("normalizeInviteCode") && shell.includes("recallInvite") && shell.includes("rememberInvite"));
  assert.ok(!shell.includes('aria-label="Invitation code"'), "sign-in never asks for the code; it only gates the download");
  assert.ok(shell.includes("inviteErrorMessage"));
  assert.ok(read("./client.ts").includes("recallInvite"));
});

test("first run opens idea intake without the legacy step order", () => {
  const hub = read("../components/ventures/VentureHub.tsx");
  assert.match(hub, /const href = unsentEntryDraftHref\([^\n]+\) \|\| "\/new";/);
  assert.ok(hub.includes("router.replace(href)") && !hub.includes("BetaPathStrip"));
  assert.ok(!hub.includes("styles.heroActions"), "no redundant hero actions beside the first-run one");
  assert.ok(read("../app/v/[ventureId]/page.tsx").includes("BetaPathStrip"));
});

test("first-run cards reflow without idea example chips while the composer stays pinned", () => {
  const entry = read("../app/new/page.tsx"), css = read("../app/new/page.module.css"), tokens = read("../styles/tokens.css");
  assert.match(entry, /find customers and book a first call/);
  assert.ok(!entry.includes("BetaPathStrip"));
  assert.match(read("../components/guide/CapabilityGuide.tsx"), /content\.quickStart\.map/);
  assert.deepEqual(guideContent().quickStart.map(step => step.id), ["start-idea", "start-customers", "start-experiment"]);
  assert.deepEqual(guideContent(true).quickStart.map(step => step.id), ["clarify", "research", "customers"]);
  assert.match(read("../components/guide/CapabilityGuide.module.css"), /@media \(max-width: 720px\)[^\n]*\.quickStart \{ grid-template-columns: 1fr/);
  assert.doesNotMatch(entry, /styles\.examples/);
  assert.doesNotMatch(entry, /setDraft\(example\); answerRef\.current\?\.focus\(\)/);
  assert.match(tokens, /\.beta-path \{[^}]*repeat\(auto-fit, minmax\(min\(100%, 180px\), 1fr\)\)/);
  assert.match(css, /@media \(max-height: 800px\)/);
  assert.match(css, /grid-template-columns: repeat\(3, minmax\(0, 1fr\)\)/);
  assert.match(css, /@container \(max-width: 520px\)/);
  assert.match(css, /\.composerArea \{ flex: none/);
  assert.match(css, /\.welcome h2 \{ font-size: clamp\(/);
});

test("overview offers no Mac computer-use entry while it is off", () => {
  const overview = read("../app/v/[ventureId]/page.tsx");
  assert.ok(!overview.includes("Open Mac tasks") && !overview.includes('aria-label="Mac tasks"'));
});
