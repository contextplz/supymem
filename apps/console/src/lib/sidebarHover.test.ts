import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";

test("sidebar keeps deliberate hover and Settings click-to-navigate stable through the flyout", () => {
  const source = fs.readFileSync(new URL("../components/layout/AppShell.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("AppShell.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let close = "", enter = "", leave = "", press = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "closeSidebar") close = node.initializer!.getText(tree);
    if (ts.isJsxOpeningElement(node) && node.tagName.getText(tree) === "aside") {
      for (const attribute of node.attributes.properties) if (ts.isJsxAttribute(attribute) && ts.isJsxExpression(attribute.initializer!)) {
        if (attribute.name.getText(tree) === "onPointerEnter") enter = attribute.initializer.expression!.getText(tree);
        if (attribute.name.getText(tree) === "onPointerLeave") leave = attribute.initializer.expression!.getText(tree);
        if (attribute.name.getText(tree) === "onMouseDownCapture") press = attribute.initializer.expression!.getText(tree);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(close && enter && leave && press);
  let expanded = false, focus: object | null = null, timer: {run: () => void; delay: number} | null = null;
  const context = vm.createContext({sidebarPinned: false, sidebarExpanded: false, sidebarHover: {current: null},
    setSidebarExpanded: (value: boolean) => {expanded = value; context.sidebarExpanded = value;},
    setTimeout: (run: () => void, delay: number) => {timer = {run, delay}; return timer;},
    clearTimeout: () => {timer = null;}, document: {activeElement: {closest: () => focus}},
  });
  vm.runInContext(ts.transpileModule(`const closeSidebar = ${close}; globalThis.enter = ${enter}; globalThis.leave = ${leave}; globalThis.press = ${press};`, {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  const event = {pointerType: "mouse", currentTarget: {contains: () => false}};
  context.enter(event); assert.equal(expanded, false); assert.equal(timer!.delay, 250);
  context.leave(); assert.equal(timer, null); assert.equal(expanded, false);
  focus = {}; context.enter(event); const blocked = timer!; timer = null; blocked.run(); assert.equal(expanded, false);
  focus = null; context.enter(event); const deliberate = timer!; timer = null; deliberate.run(); assert.equal(expanded, true);
  context.leave(); assert.equal(expanded, false);
  context.enter({...event, pointerType: "touch"}); assert.equal(timer, null);
  const css = fs.readFileSync(new URL("../styles/tokens.css", import.meta.url), "utf8");
  assert.doesNotMatch(css, /\.sidebar:hover/);
  assert.match(css, /\.app-shell:is\(\.sidebar-pinned, \.sidebar-expanded\).*grid-template-columns: var\(--w-sidebar\)/);
  assert.match(css, /\.app-shell:has\(\.sidebar:focus-within\)/);

  const navSource = fs.readFileSync(new URL("../components/layout/SideNav.tsx", import.meta.url), "utf8");
  const nav = ts.createSourceFile("SideNav.tsx", navSource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const functions = nav.statements.filter(node => ts.isFunctionDeclaration(node)
    && ["isActive", "NavLink"].includes(node.name!.text)).map(node => node.getText(nav)).join("\n");
  for (const ventureId of ["ven_dispatch", "ven_cooking"]) for (const local of [true, false]) {
    const pathname = `/v/${ventureId}/research`, destination = local ? "/settings/local" : `/v/${ventureId}/settings`;
    const linkContext = vm.createContext({Link: "a", NewVentureLink: "new-venture", styles: {}, useLocalMode: () => local,
      pathname, destination,
      cx: (...values: unknown[]) => values.filter(Boolean).join(" "), NAV_ICONS: {settings: () => null},
      React: {createElement: (type: unknown, props: unknown) => ({type, props})}});
    vm.runInContext(ts.transpileModule(`${functions}; globalThis.link = NavLink({item: {href: destination, label: 'Settings', icon: 'settings', beta: 'review'}, pathname});`,
      {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText, linkContext);
    const link = linkContext.link;
    assert.equal(link.type, "a"); assert.equal(link.props["aria-label"], "Settings");
    assert.equal(link.props.title.includes("(Beta)"), !local);
    for (const flyout of [false, true]) {
      let focused = false, href = pathname;
      const anchor = {focus(options: {preventScroll: boolean}) { assert.equal(options.preventScroll, true); focused = true; }};
      const down = {button: 0, target: {closest: () => anchor}, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; }};
      context.enter(event);
      if (flyout) { timer!.run(); timer = null; }
      const wideAtPress: boolean = expanded || focused;
      context.press(down);
      if (!down.defaultPrevented) anchor.focus({preventScroll: true});
      assert.equal(expanded || focused, wideAtPress, "mousedown focus must not move the link before mouseup");
      if (flyout) context.leave();
      assert.equal(expanded || focused, wideAtPress, "pointerleave must not collapse the pressed flyout");
      assert.equal(timer, null);
      const click = {defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }};
      link.props.onClick?.(click);
      if (!click.defaultPrevented) href = link.props.href;
      assert.equal(href, destination);
      context.leave();
    }
  }
});
