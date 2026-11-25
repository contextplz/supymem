import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { FIRST_RUN_CTA, FIRST_RUN_VALUE_PROP } from "./betaCopy.ts";

test("first-run empty state copy is wired into the ventures hub", () => {
  assert.equal(FIRST_RUN_CTA, "What is your idea?");
  assert.match(FIRST_RUN_VALUE_PROP, /find your first customers/);
  const hub = readFileSync(new URL("../components/ventures/VentureHub.tsx", import.meta.url), "utf8");
  assert.match(hub, /const href = unsentEntryDraftHref\([^\n]+\) \|\| "\/new";/);
  assert.ok(hub.includes("router.replace(href)") && hub.includes("FIRST_RUN_CTA") && hub.includes("FIRST_RUN_VALUE_PROP"));
  assert.ok(!hub.includes("BetaPathStrip"));
  const account = readFileSync(new URL("../components/settings/AccountSettings.tsx", import.meta.url), "utf8");
  assert.match(account, /<NewVentureLink>What is your idea\?<\/NewVentureLink>/);
});

test("venture hub requests lightweight summaries without claiming an unknown team is empty", () => {
  const hub = readFileSync(new URL("../components/ventures/VentureHub.tsx", import.meta.url), "utf8");
  assert.ok(hub.includes('apiSwr("/api/ventures?summary=true"'));
  assert.ok(hub.includes('apiSwr("/api/ventures/deleted?summary=true"'));
  const card = readFileSync(new URL("../components/ventures/VentureCard.tsx", import.meta.url), "utf8");
  assert.ok(card.includes('venture.agents && <p className={styles.team}'));
});

test("beta hides native Mac surfaces", () => {
  const read = (p: string) => readFileSync(new URL(p, import.meta.url), "utf8");
  assert.match(read("../components/chat/ChatDock.tsx"), /showMacTasks = false/);
  const god = read("../components/settings/GodModePanel.tsx");
  assert.ok(god.includes("coming soon in beta") && !god.includes("allow_mac: e.target.checked"));
  const settings = read("../components/settings/VentureSettings.tsx");
  assert.ok(!settings.includes("<AgentSessionPane"));
});
