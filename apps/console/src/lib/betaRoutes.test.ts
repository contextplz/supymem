import assert from "node:assert/strict";
import test from "node:test";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { isBetaHiddenPath } from "./betaRoutes.ts";

test("hidden-for-beta routes are blocked, golden path stays reachable", () => {
  for (const p of ["/lab", "/content", "/ops/finance", "/ads", "/media", "/marketing", "/community", "/invite/abc",
    "/setup/permissions", "/v/ven_1/people", "/v/ven_1/org", "/v/ven_1/evolution", "/v/ven_1/simulate",
    "/v/ven_1/import", "/v/ven_1/connections", "/v/ven_1/connect/github"]) assert.equal(isBetaHiddenPath(p), true, p);
  for (const p of ["/", "/new", "/gtm", "/connections", "/account", "/admin/credits", "/v/ven_1", "/v/ven_1/idea",
    "/v/ven_1/research", "/v/ven_1/builds", "/v/ven_1/site", "/v/ven_1/settings", "/v/ven_1/device-work-orders/o1", "/v/ven_1/suggestions", "/v/ven_1/now",
    "/download", "/welcome", "/auth/consume"]) assert.equal(isBetaHiddenPath(p), false, p);
});

test("SideNav and middleware share the blocked route list", () => {
  const read = (f: string) => readFileSync(new URL(f, import.meta.url), "utf8");
  assert.ok(read("../components/layout/SideNav.tsx").includes("isBetaHiddenPath"));
  assert.ok(read("../middleware.ts").includes("isBetaHiddenPath"));
});

test("beta venture surfaces keep task review without enabling God mode or bring-your-own-key controls", () => {
  const read = (f: string) => readFileSync(new URL(f, import.meta.url), "utf8");
  assert.doesNotMatch(read("../app/v/[ventureId]/layout.tsx"), /GodModeLive/);
  const settings = read("../components/settings/VentureSettings.tsx");
  for (const gone of [/<GodModePanel/, /<ConnectTools/, /<LocalToolsHealth/, /Use my own key instead/, /How much should agents do/]) assert.doesNotMatch(settings, gone);
  assert.match(settings, /<MacTaskList key=\{`\$\{loadedScope!\.subject\}:\$\{loadedScope!\.epoch\}:\$\{ventureId\}`\} ventureId=\{ventureId\}/);
  const tasks = read("../components/settings/MacTaskList.tsx");
  assert.match(tasks, /<Link href=\{t\.review_path\}/);
  assert.doesNotMatch(tasks, /<button|method:|desktopInvoke|confirmation/);
  const overview = read("../app/v/[ventureId]/page.tsx");
  for (const gone of [/Mac tasks/, /Import code/, /computer-use/]) assert.doesNotMatch(overview, gone);
});

test("no beta-visible source links to a page the middleware 404s", () => {
  const root = new URL("../", import.meta.url).pathname;
  // Hidden surfaces may link to each other; everything else must not link into them.
  const hiddenOwn = /^(app|components)\/(admin|ops|lab|idealab|content|marketing|community|media|ads|invite|people|setup|desktop\/MacPermissionsSetup|cockpit\/TeamStrip)|^app\/v\/\[ventureId\]\/(org|evolution|simulate|import|people|market|feedback|connections|connect)\//;
  const link = /(?:href=|push\(|replace\(|assign\()\s*\{?\s*[`"'](\/(?:lab|content|marketing|community|media|ads|ops|invite|setup\/permissions)\b|\/v\/\$\{[^}]+\}\/(?:org|evolution|simulate|import|people|market|feedback|connections|connect)\b)/;
  const offenders: string[] = [];
  const walk = (dir: string) => {
    for (const name of readdirSync(root + dir)) {
      const rel = `${dir}/${name}`;
      if (statSync(root + rel).isDirectory()) { walk(rel); continue; }
      if (!/\.tsx$/.test(name) || hiddenOwn.test(rel)) continue;
      readFileSync(root + rel, "utf8").split("\n").forEach((line, i) => { if (link.test(line) && !line.includes("BETA_PERMISSIONS_SETUP")) offenders.push(`${rel}:${i + 1}`); });
    }
  };
  walk("app"); walk("components");
  assert.deepEqual(offenders, []);
});
