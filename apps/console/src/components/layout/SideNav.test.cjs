const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const vm = require("node:vm");
const test = require("node:test");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

test("local chat opens the same scoped Connections page without replacing status", async () => {
  const routes = await import("../../lib/chatRouteAction.ts");
  const { ventureGrowthHrefs } = await import("../../lib/operationsScope.ts");
  const previous = process.env.NEXT_PUBLIC_SUPYMEM_MODE;
  process.env.NEXT_PUBLIC_SUPYMEM_MODE = "local";
  try {
    for (const venture of ["ven_dispatch", "ven_gardens"]) {
      const native = ventureGrowthHrefs(venture).connections;
      const status = { label: "Show connections here", action: "connections.status" };
      for (const href of [native, `/v/${venture}/connections`, `https://app.supymem.com/v/${venture}/connections`]) {
        const actions = routes.inChatActions([status, { label: "Open Connections", href, kind: "recovery_link" }], true, venture);
        assert.equal(actions[0], status);
        assert.equal(actions[1]?.href, native);
      }
      assert.deepEqual(routes.homeRouteAction(`/v/${venture}/connections`, venture), { sidebar: "view", href: native });
    }
  } finally {
    if (previous === undefined) delete process.env.NEXT_PUBLIC_SUPYMEM_MODE;
    else process.env.NEXT_PUBLIC_SUPYMEM_MODE = previous;
  }
});

test("company Connections navigation keeps native routing and verified scope for unrelated ventures", async () => {
  const tree = ts.createSourceFile("SideNav.tsx", readFileSync(`${__dirname}/SideNav.tsx`, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(tree)).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const scope = await import("../../lib/operationsScope.ts");
  for (const [ventureId, workspaceId] of [["ven_manufacturing", "wsp_manufacturing"], ["ven_pet_training", "wsp_pet_training"]]) {
    const context = { exports: {}, React, ...scope, cx: (...parts) => parts.filter(Boolean).join(" "),
      isBetaHiddenPath: () => false, useLocalMode: () => false, useFeatureVisibility: () => () => true, NAV_ICONS: new Proxy({}, { get: () => () => null }), styles: {},
      Link: "a", NewVentureLink: "a", GuideNavLink: () => null };
    vm.runInNewContext(compiled, context);
    const html = renderToStaticMarkup(context.exports.SideNav({ pathname: "/connections", operationsScope: { ventureId, workspaceId } }));
    assert.match(html, /data-chat-route-native="true"[^>]*aria-label="Connections"[^>]*aria-current="page"/);
    assert.ok(html.includes(`href="/connections?workspace=${workspaceId}&amp;venture=${ventureId}"`));
    const visibility = await import("../../lib/chatVisibility.ts");
    const beta = await import("../../lib/betaRoutes.ts");
    assert.equal(visibility.chatFirstHidden(`/connections?venture=${ventureId}`), false);
    assert.equal(beta.isBetaHiddenPath("/connections"), false);
    const chat = await import("../../lib/chatRouteAction.ts");
    assert.equal(chat.inChatActions([{ href: "/connections", label: "Your apps" }])[0].action, "connections.status");
  }
});

test("local settings navigation is available globally and within unrelated ventures only in local mode", async () => {
  const tree = ts.createSourceFile("SideNav.tsx", readFileSync(`${__dirname}/SideNav.tsx`, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(tree)).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const scope = await import("../../lib/operationsScope.ts");
  for (const ventureId of ["ven_manufacturing", "ven_pet_training"]) for (const local of [true, false]) {
    const mode = await import("../../lib/localMode.ts");
    const context = { exports: {}, React, ...scope, ...mode, useLocalMode: () => local, useFeatureVisibility: () => () => true, useState: React.useState,
      cx: (...parts) => parts.filter(Boolean).join(" "), isBetaHiddenPath: () => false,
      NAV_ICONS: new Proxy({}, { get: () => () => null }), styles: {}, Link: "a", NewVentureLink: "a", GuideNavLink: () => null, SuggestionsBadge: () => null };
    vm.runInNewContext(compiled, context);
    for (const selected of [undefined, ventureId]) for (const [pathname, label] of [
      ["/settings/local", "Settings"], ["/settings/local/capabilities", "Capabilities"],
    ]) {
      const html = renderToStaticMarkup(React.createElement(context.exports.SideNav, { pathname, ventureId: selected }));
      for (const route of ["/settings/local", "/settings/local/capabilities"]) {
        assert.equal(html.includes(`href="${route}"`), local, ventureId);
      }
      if (local) {
        assert.ok(html.includes(`href="${pathname}"`) && html.includes(`aria-label="${label}" aria-current="page"`));
        assert.equal((html.match(/aria-current="page"/g) || []).length, 1);
      }
    }
  }
});

test("local navigation exposes shipped features and coding-tool builds without beta labels", async () => {
  const source = readFileSync(`${__dirname}/SideNav.tsx`, "utf8");
  const tree = ts.createSourceFile("SideNav.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(tree)).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const scope = await import("../../lib/operationsScope.ts");
  const beta = await import("../../lib/betaRoutes.ts");
  const mode = await import("../../lib/localMode.ts");
  for (const ventureId of ["ven_manufacturing", "ven_pet_training"]) {
    const context = { exports: {}, React, ...scope, ...beta, ...mode, useLocalMode: () => true,
      useFeatureVisibility: () => (_surface, href) => !/^\/(ads|ops|marketing|community)(?:\/|\?|$)/.test(href.replace(/^\/v\/[^/]+/, "")), useState: React.useState,
      cx: (...parts) => parts.filter(Boolean).join(" "), NAV_ICONS: new Proxy({}, { get: () => () => null }),
      styles: {}, Link: "a", NewVentureLink: "a", GuideNavLink: () => null, SuggestionsBadge: () => null };
    vm.runInNewContext(compiled, context);
    for (const selected of [undefined, ventureId]) {
      const html = renderToStaticMarkup(React.createElement(context.exports.SideNav, { pathname: "/", ventureId: selected }));
      assert.doesNotMatch(html, /href="[^\"]*(?:\/ads|\/people|\/ops)(?:[?\/\"])/);
      assert.doesNotMatch(html, /\(Beta\)/);
      assert.match(html, selected ? /aria-label="Content"/ : /href="\/content/);
      assert.match(html, /aria-label="Settings"/);
    }
  }
});
