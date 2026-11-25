const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");

test("every local screen stays below the macOS window controls across domains", async () => {
  const css = readFileSync(join(__dirname, '../styles/tokens.css'), 'utf8');
  assert.match(css, /\.local-app-frame\s*\{[^}]*inset:\s*40px 0 0/);
  assert.match(css, /\.local-app-frame\s*\{[^}]*transform:\s*translateZ\(0\)/);
  assert.match(css, /\.local-app-frame \.app-shell\s*\{[^}]*height:\s*calc\(100dvh - 40px\)/);
  for (const domain of ['Fleet coordination', 'Family holidays']) {
    const source = readFileSync(join(__dirname, 'layout.tsx'), 'utf8');
    assert.match(source, /<body className=\{local \? "local-app-frame" : undefined\}/, domain);
  }
});

for (const domain of ["procurement.example", "gardening.example"]) {
  test(`your layout compiles and keeps hosted gates out of local mode for ${domain}`, async () => {
    const source = ts.transpileModule(readFileSync(join(__dirname, "layout.tsx"), "utf8"), {
      reportDiagnostics: true,
      compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX },
    });
    assert.equal(source.diagnostics.filter(item => item.category === ts.DiagnosticCategory.Error).length, 0);
    for (const env of [{}, { SUPYMEM_MODE: " LOCAL " }, { NEXT_PUBLIC_SUPYMEM_MODE: "local" }]) {
      const exported = {};
      vm.runInNewContext(source.outputText, {
        exports: exported, process: { env },
        require: id => {
          if (id.endsWith(".css")) return {};
          if (id === "next/headers") return { headers: async () => new Headers() };
          if (id === "@/components/layout/LocalModeBoundary") {
            return { LocalModeBoundary: ({ children, local }) => React.createElement("main", { "data-local": String(local) }, children) };
          }
          if (id.startsWith("@/components/desktop/")) {
            const name = id.split("/").at(-1);
            return { [name]: ({ children }) => React.createElement("span", { "data-component": name }, children) };
          }
          return require(id);
        },
      });
      const local = Boolean(env.SUPYMEM_MODE || env.NEXT_PUBLIC_SUPYMEM_MODE);
      const markup = renderToStaticMarkup(await exported.default({ children: domain }));
      assert.ok(markup.includes(domain));
      assert.ok(markup.includes(`data-local="${local}"`));
      assert.equal(markup.includes('class="local-app-frame"'), local);
      for (const name of ["DesktopUpdateGate", "DesktopProductBoundary", "ConsoleBuildUpdate", "CharlieIntro"]) {
        assert.equal(markup.includes(`data-component="${name}"`), !local);
      }
    }
  });
}

test("your local settings use the same light office shell regardless of a saved hosted layout", () => {
  const file = join(__dirname, "../components/layout/AppShell.tsx");
  const source = readFileSync(file, "utf8");
  const tree = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const shell = tree.statements.find(node => ts.isFunctionDeclaration(node) && node.name?.text === "AppShellInner");
  const declarations = shell.body.statements.filter(node => ts.isVariableStatement(node)
    && /useFounderUIState\(|const founderUI(?:Loaded)? =/.test(node.getText(tree)));
  for (const domain of ["Stockroom scheduling", "Family recipe sharing"]) for (const local of [true, false]) {
    const context = { local, sessionSubject: domain, authed: true,
      useFounderUIState: (subject, enabled) => {
        assert.equal(subject, domain);
        assert.equal(enabled, !local);
        return { layout: "classic", loaded: false };
      } };
    const actual = vm.runInNewContext(ts.transpileModule(`${declarations.map(node => node.getText(tree)).join("\n")}\n({founderUI, founderUILoaded});`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022 },
    }).outputText, context);
    assert.equal(actual.founderUI, local ? "home" : "classic", domain);
    assert.equal(actual.founderUILoaded, local, domain);
  }
  const home = shell.body.statements.find(node => ts.isIfStatement(node) && node.expression.getText(tree) === 'founderUI === "home"');
  for (const domain of ["Stockroom scheduling", "Family recipe sharing"]) for (const pathname of ["/settings/local", "/settings/local/capabilities"]) {
    const context = { exports: {}, React, local: true, founderUI: "home", effectiveVentureId: undefined,
      entryPage: false, bare: false, chatFirstHome: false, chatShown: false, localBuildActivity: false, localSubpage: false, localHomeRef: { current: null }, officeTab: false, pathname,
      chatPanelSize: { width: 400 }, sessionSubject: domain, authed: true, clarificationSkip: null,
      searchParams: new URLSearchParams({ local_notice: "1" }),
      chatAvailable: false, nativeDesktop: false, isVentureOverview: false, headerRight: null,
      shownTitle: "Settings", workspaceBodyRef: { current: null }, ideaStatus: null, connectionError: "",
      permissionRequest: null, companyOperations: false, bottomBar: null,
      children: React.createElement("h1", null, domain), captureChatLink() {}, showsDetailToggle: () => false,
      cx: (...parts) => parts.filter(Boolean).join(" "),
      FounderUIContext: React.createContext("classic"), ChatOpenContext: React.createContext(false),
      ClarificationSkipContext: React.createContext(null),
      CompanyOffice: ({ children, notices }) => React.createElement("main", { "data-office": "shared" }, notices, children),
      AppShellErrorBoundary: ({ children }) => children, WorkspaceErrorBoundary: ({ children }) => children,
      UsageTracker: () => null, NextStepStrip: () => null, AttentionToast: () => null, DesktopNoticeBanner: () => null, AgentDesk: () => null };
    vm.runInNewContext(ts.transpileModule(`function renderShell() { ${home.getText(tree)} }\nexports.renderShell = renderShell;`, {
      compilerOptions: { target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React },
    }).outputText, context);
    const markup = renderToStaticMarkup(context.exports.renderShell());
    assert.match(markup, /class="app-shell native-shell founder-shell home-shell local-settings-shell"/);
    assert.match(markup, /data-office="shared"/);
    assert.ok(markup.includes(domain));
    assert.match(markup, /data-office="shared"><p role="status">Open your venture to continue your work/);
    assert.match(markup, /href="\/settings\/local"/);
    assert.doesNotMatch(markup, /Companies|charlie-buddy|supymem-chat-panel/);
  }
});

test("your sidebar uses the capability screen name for unrelated ventures", () => {
  const file = join(__dirname, "../components/layout/SideNav.tsx");
  const tree = ts.createSourceFile(file, readFileSync(file, "utf8"), ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const source = tree.statements.filter(node => !ts.isImportDeclaration(node)).map(node => node.getText(tree)).join("\n");
  for (const domain of ["Stockroom scheduling", "Family recipe sharing"]) {
    const context = { exports: {}, React, useState: React.useState, useLocalMode: () => true, useFeatureVisibility: () => () => true,
      ...require("../lib/operationsScope.ts"), ...require("../lib/localMode.ts"),
      cx: (...parts) => parts.filter(Boolean).join(" "), styles: {},
      NAV_ICONS: new Proxy({}, { get: () => () => null }),
      Link: "a", NewVentureLink: "a", GuideNavLink: () => null, SuggestionsBadge: () => null };
    vm.runInNewContext(ts.transpileModule(source, { compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React,
    } }).outputText, context);
    for (const ventureId of [undefined, domain]) {
      const markup = renderToStaticMarkup(React.createElement(context.exports.SideNav, { pathname: "/settings/local/capabilities", ventureId }));
      assert.match(markup, /aria-label="Capabilities" aria-current="page"/);
      assert.doesNotMatch(markup, /aria-label="Features"/);
    }
  }
});
