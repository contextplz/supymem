import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import { existsSync, readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { isBetaHiddenPath } from "./betaRoutes.ts";
import * as guide from "./capabilityGuide.ts";

type Node = { type: unknown; props: Record<string, any> };
const nodes = (value: any): Node[] => Array.isArray(value) ? value.flatMap(nodes)
  : value?.props ? [value, ...nodes(value.props.children)] : [];
const text = (value: any): string => Array.isArray(value) ? value.map(text).join(" ")
  : value?.props ? text(value.props.children) : typeof value === "string" || typeof value === "number" ? String(value) : "";

function view(ventureId?: string, clipboardFails = false, motion: Record<string, unknown> = {}) {
  const states: any[] = [], copied: string[] = [], prefills: string[] = [], effects: (() => any)[] = [];
  let cursor = 0;
  const jsx = (type: any, props: any): any => typeof type === "function" ? type(props) : ({ type, props });
  const exports: Record<string, any> = {};
  const source = readFileSync(new URL("../components/guide/CapabilityGuide.tsx", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(code, {
    ...motion,
    exports, navigator: { clipboard: { async writeText(value: string) {
      if (clipboardFails) throw new Error("Clipboard unavailable");
      copied.push(value);
    } } },
    require(id: string) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

      if (id === "react") return { useRef: () => ({ current: null }), useEffect: (effect: () => any) => effects.push(effect), useState(initial: any) {
        const index = cursor++;
        if (!(index in states)) states[index] = initial;
        return [states[index], (value: any) => { states[index] = value; }];
      } };
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (id === "next/link") return { default: "a" };
      if (id === "@/lib/capabilityGuide") return guide;
      if (id === "@/lib/chatPrefill") return { prefillChat: (prompt: string) => prefills.push(prompt) };
      if (id === "@/components/layout/LocalModeBoundary") return { useLocalMode: () => false, useFeatureVisibility: () => () => true };
      if (id === "@/lib/client") return { api() { throw new Error("Unexpected hosted capability fetch"); } };
      if (id.endsWith("/capabilities/capabilities")) return { capabilityView: (value: unknown) => value };
      if (id === "@/components/layout/AppShell") return { AppShell: "shell" };
      if (id === "@/components/layout/NavIcons") return { NAV_ICONS: new Proxy({}, { get: () => () => jsx("svg", {}) }) };
      if (id.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => key }) };
      throw new Error(`Unexpected guide dependency: ${id}`);
    },
  });
  return { copied, prefills, effects, render() { cursor = 0; effects.length = 0; return exports.CapabilityGuide({ ventureId }); } };
}

test("the start guide describes the next-question flow and deferred optional details", () => {
  const start = guide.GUIDE_CAPABILITIES.find(card => card.id === "idea")!;
  assert.match(start.description, /answer the next question and confirm a venture name/);
  assert.match(start.description, /type your own name/);
  assert.match(start.note!, /stage, website, prototype and contacts later/);
  assert.equal(start.destination, "new");
});

test("guide filters combine case-insensitive words, topics and stages, with an honest empty result", () => {
  assert.ok(guide.filterGuideCapabilities("  MICROPHONE   permission  ", "All topics", "Any stage").some(card => card.id === "voice"));
  assert.ok(guide.filterGuideCapabilities("remember", "Work & memory", "Customers").some(card => card.id === "memory"));
  assert.deepEqual(guide.filterGuideCapabilities("remember", "Website", "Customers"), []);
  assert.deepEqual(guide.filterGuideCapabilities("tutors", "Start & shape", "Customers"), []);
  assert.deepEqual(guide.filterGuideCapabilities("not-a-capability-xyz", "All topics", "Any stage"), []);
  const future = guide.filterGuideCapabilities("", "Coming next", "Any stage");
  assert.ok(future.length > 0 && future.every(card => card.comingNext));
});

test("the actual global guide page preserves one valid selected venture and safely keeps portfolio fallbacks", async () => {
  const exports: Record<string, any> = {};
  const source = readFileSync(new URL("../app/guide/page.tsx", import.meta.url), "utf8");
  const code = ts.transpileModule(source, { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  vm.runInNewContext(code, { exports, require(id: string) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

    if (id === "react/jsx-runtime") return { jsx: (type: any, props: any) => ({ type, props }) };
    if (id === "@/components/guide/CapabilityGuide") return { GuidePage: "guide-page" };
    throw new Error(`Unexpected guide route dependency: ${id}`);
  } });
  for (const raw of ["ven_fixture", "  ven_fixture  "]) {
    const route = await exports.default({ searchParams: Promise.resolve({ venture: raw }) });
    assert.equal(route.props.ventureId, "ven_fixture");
    const tree = nodes(view(route.props.ventureId).render());
    assert.equal(tree.find(node => node.props["data-usage-control"] === "guide_start")?.props.href, "/v/ven_fixture");
    assert.ok(tree.some(node => node.props["data-usage-control"] === "guide_open" && node.props.href === "/v/ven_fixture/research"));
    assert.ok(tree.some(node => node.props["data-usage-control"] === "guide_open" && node.props.href === "/v/ven_fixture/site"));
  }
  for (const raw of [undefined, "", " ", ["ven_fixture"], ["ven_fixture", "ven_other"], "other", "ven_", "ven_x/../../other", "ven_x?send=1", "ven_x%2Fother", "ven_x\u0000", `ven_${"x".repeat(125)}`]) {
    const route = await exports.default({ searchParams: Promise.resolve({ venture: raw }) });
    assert.equal(route.props.ventureId, undefined, JSON.stringify(raw));
    const tree = nodes(view(route.props.ventureId).render());
    assert.equal(tree.find(node => node.props["data-usage-control"] === "guide_start")?.props.href, "/new");
    assert.equal(guide.guideDestinationHref("research", route.props.ventureId), "/");
    assert.equal(guide.guideDestinationHref("site", route.props.ventureId), "/");
  }
  const withUnrelated = await exports.default({ searchParams: Promise.resolve({ venture: "ven_fixture", start: "1", workspace: "ws_other" }) });
  assert.deepEqual(Object.keys(withUnrelated.props), ["ventureId"]);
  assert.equal(withUnrelated.props.ventureId, "ven_fixture");
  assert.equal((await exports.default({ searchParams: Promise.resolve({ unrelated: "ven_other" }) })).props.ventureId, undefined);
  assert.equal((await exports.default({})).props.ventureId, undefined);
});

test("guide destinations point to mounted screens and retain venture context without starting work", () => {
  const local = process.env.SUPYMEM_MODE === "local";
  const hrefFor = (destination: guide.GuideDestination, ventureId?: string) => guide.guideDestinationHref(destination, ventureId, local);
  const expected = {
    new: "/new", ventures: "/", overview: "/v/ven_fixture", idea: "/v/ven_fixture/idea", research: "/v/ven_fixture/research",
    site: "/v/ven_fixture/site", gtm: "/gtm?venture=ven_fixture", leads: "/v/ven_fixture/leads", experiments: "/v/ven_fixture/experiments",
    "market-map": "/v/ven_fixture/market-map",
    now: "/v/ven_fixture/now", tasks: "/v/ven_fixture/tasks", suggestions: "/v/ven_fixture/suggestions", settings: local ? "/settings/local" : "/v/ven_fixture/settings",
    connections: "/connections?venture=ven_fixture", "coding-tools": "/settings/local#coding-tools", account: local ? "/settings/local" : "/account", download: local ? "/settings/local" : "/download", plan: "/v/ven_fixture/plan",
    "local-models": "/settings/local#providers-title", data: "/settings/local#your-data-title",
    analytics: "/v/ven_fixture/analytics", builds: "/v/ven_fixture/builds", "browser-tasks": local ? "/connections" : "/v/ven_fixture/settings#settings-computer-use",
  } satisfies Record<guide.GuideDestination, string>;
  for (const [destination, href] of Object.entries(expected)) {
    assert.equal(hrefFor(destination as guide.GuideDestination, "ven_fixture"), href);
    const route = href.split(/[?#]/)[0].replace("/v/ven_fixture", "/v/[ventureId]");
    assert.ok(existsSync(new URL(`../app${route === "/" ? "" : route}/page.tsx`, import.meta.url)), `${destination} has a mounted page`);
    assert.equal(isBetaHiddenPath(href), false, `${destination} is reachable in beta`);
    assert.equal(isBetaHiddenPath(hrefFor(destination as guide.GuideDestination)), false, `${destination} fallback is reachable in beta`);
    assert.doesNotMatch(href, /[?&](?:build|start|send|approve|skip_page)=/);
  }
  for (const card of [...guide.GUIDE_CAPABILITIES, ...guide.guideContent(true).capabilities]) if (card.destination) {
    assert.ok(card.destination in expected, `${card.id} uses a verified destination`);
    for (const ventureId of ["ven_fixture", undefined])
      assert.equal(isBetaHiddenPath(hrefFor(card.destination, ventureId)), false, `${card.id} cannot link to a hidden beta route`);
  }
  for (const destination of ["overview", "idea", "research", "site", "leads", "experiments", "now", "tasks", "suggestions", "plan", "analytics", "builds"] as const)
    assert.equal(hrefFor(destination), "/", `${destination} asks for a venture first`);
  for (const destination of ["new", "gtm", "connections"] as const)
    assert.equal(hrefFor(destination), `/${destination}`);
  for (const destination of ["account", "download"] as const)
    assert.equal(hrefFor(destination), local ? "/settings/local" : `/${destination}`);
  assert.equal(hrefFor("settings"), local ? "/settings/local" : "/");
  assert.equal(hrefFor("ventures"), "/");
  assert.equal(hrefFor("research", "venture /?"), "/v/venture%20%2F%3F/research");
  assert.equal(hrefFor("gtm", "venture /?"), "/gtm?venture=venture%20%2F%3F");
});

test("unavailable capabilities cannot offer executable destinations or copyable requests", () => {
  assert.equal(new Set(guide.GUIDE_CAPABILITIES.map(card => card.id)).size, guide.GUIDE_CAPABILITIES.length, "card headings have unique ids");
  const upcoming = guide.GUIDE_CAPABILITIES.filter(card => card.comingNext);
  assert.ok(upcoming.length > 0);
  for (const card of upcoming) {
    assert.equal(card.topic, "Coming next", card.id);
    assert.equal(card.destination, undefined, card.id);
    assert.equal(card.action, undefined, card.id);
    assert.equal(card.prompt, undefined, card.id);
  }
  const tree = nodes(view("ven_fixture").render());
  const section = tree.find(node => node.type === "details" && node.props.className === "upcoming");
  assert.ok(section);
  assert.equal(nodes(section).filter(node => node.type === "a" || node.type === "button").length, 0);
  assert.match(text(section), /not available generally yet/);
});

test("actual guide exposes labeled controls, status, one primary action and working filter reset", () => {
  const h = view(), tree = nodes(h.render());
  assert.equal(tree.filter(node => node.type === "h1").length, 1);
  assert.equal(tree.filter(node => node.props.className === "primary").length, 1);
  assert.equal(tree.find(node => node.props.className === "primary")?.props.href, "/new");
  assert.ok(tree.some(node => node.type === "label" && text(node).includes("Search capabilities and examples") && nodes(node).some(child => child.type === "input")));
  assert.ok(tree.some(node => node.type === "label" && text(node).includes("I am at") && nodes(node).some(child => child.type === "select")));
  assert.ok(tree.some(node => node.props.role === "group" && node.props["aria-label"] === "Filter by topic"));
  assert.ok(tree.some(node => node.props.role === "status" && node.props["aria-live"] === "polite"));
  assert.ok(tree.some(node => node.type === "img" && node.props.alt && node.props.src === "/guide/hero.webp"));
  assert.ok(tree.some(node => node.props.href === "#guide-explore"));
  assert.ok(tree.some(node => node.props.id === "guide-explore" && node.props.tabIndex === -1));
  assert.doesNotMatch(text(h.render()), /—|GPT-6\.1|ven_fixture|error_code|insufficient_credits/);
  for (const button of tree.filter(node => node.type === "button")) assert.equal(button.props.type, "button");
  for (const article of tree.filter(node => node.type === "article" && node.props["aria-labelledby"]))
    assert.ok(nodes(article).some(node => node.type === "h3" && node.props.id === article.props["aria-labelledby"]));
  assert.ok(tree.some(node => node.props["data-usage-control"] === "guide_open" && node.props.href === "/" && text(node).includes("Open Ventures")));
  for (const link of tree.filter(node => node.props["data-usage-control"] === "guide_open" && node.props.href === "/" && !text(node).includes("Open Ventures")))
    assert.match(text(link), /Choose a venture/);
  tree.find(node => node.type === "input")!.props.onChange({ target: { value: "not-a-capability-xyz" } });
  const empty = h.render();
  assert.match(text(empty), /No matches yet/);
  nodes(empty).find(node => node.type === "button" && text(node) === "Clear filters")!.props.onClick();
  assert.equal(nodes(h.render()).filter(node => node.props["data-usage-control"] === "guide_open").length, guide.GUIDE_CAPABILITIES.filter(card => !card.comingNext && card.destination).length);
  nodes(h.render()).find(node => node.type === "select")!.props.onChange({ target: { value: "Customers" } });
  assert.equal(nodes(h.render()).find(node => node.type === "select")?.props.value, "Customers");
  assert.equal(nodes(h.render()).filter(node => node.props["data-usage-control"] === "guide_open").length,
    guide.filterGuideCapabilities("", "All topics", "Customers").filter(card => !card.comingNext && card.destination).length);
  nodes(h.render()).find(node => node.type === "button" && text(node) === "Coming next")!.props.onClick();
  const filtered = nodes(h.render());
  assert.equal(filtered.find(node => node.type === "button" && text(node) === "Coming next")?.props["aria-pressed"], true);
  assert.equal(filtered.filter(node => node.props["data-usage-control"] === "guide_copy" || node.props["data-usage-control"] === "guide_open").length, 0);
});

test("copying a usable example only writes its words, reports success and handles denied clipboard", async () => {
  for (const fails of [false, true]) {
    const h = view("ven_fixture", fails), tree = nodes(h.render());
    const card = guide.GUIDE_CAPABILITIES.find(card => card.prompt && !card.comingNext)!;
    const copy = tree.find(node => node.props["aria-label"] === `Copy example for ${card.title}`);
    assert.ok(copy);
    copy.props.onClick();
    await new Promise(setImmediate);
    assert.deepEqual(h.copied, fails ? [] : [card.prompt]);
    assert.match(text(h.render()), fails ? /copy it manually/ : /Copied\. Adapt it in your conversation\./);
  }
});

test("illustrated customer and account cards preserve outreach follow-through and credit request guidance", () => {
  const drafts = guide.GUIDE_CAPABILITIES.find(card => card.id === "drafts")!;
  assert.equal(drafts.image, "customers");
  assert.match(drafts.result, /log replies, prepare for calls, add call notes/);
  assert.match(drafts.note!, /booking link in Settings/);
  assert.match(drafts.note!, /share it yourself when answering from Gmail/);
  const account = guide.GUIDE_CAPABILITIES.find(card => card.id === "account")!;
  assert.equal(account.image, "credits");
  assert.match(account.description, /Buy more/);
  assert.match(account.note!, /A request does not fund work on its own/);
});

test("guide copy distinguishes Gmail sends and replies from community and social drafts", () => {
  const card = (id: string) => guide.GUIDE_CAPABILITIES.find(item => item.id === id)!;
  const replyCopy = [card("chat").description, card("auto-email").result, card("drafts").result,
    guide.GUIDE_USE_CASES.find(item => item.id === "first-calls")!.result,
    guide.GUIDE_TIPS.find(item => item.id === "tip-status")!.text,
    guide.GUIDE_FAQS.find(item => item.question.startsWith("Where do I find results"))!.answer];
  for (const copy of replyCopy) {
    assert.match(copy, /Replies arrive in your Gmail/);
    assert.match(copy, /Charlie tells you in chat/);
    assert.match(copy, /you answer from Gmail/);
  }
  for (const id of ["audience", "reachable-leads", "existing-customers", "drafts"]) {
    const copy = JSON.stringify(card(id));
    assert.match(copy, /connected Gmail/);
    assert.match(copy, /(?:ask|when you ask) to send (?:the exact )?reviewed/i);
    assert.match(copy, /automate all emails/i);
    assert.match(copy, /(?:community|social) drafts.*(?:post|send) them yourself/);
  }
  const approval = guide.GUIDE_FAQS.find(item => item.question === "Is anything sent without my approval?")!.answer;
  assert.match(approval, /connected Gmail/);
  assert.match(approval, /Community and social drafts are for you to copy and post yourself/);
  for (const item of [...guide.GUIDE_CAPABILITIES, ...guide.GUIDE_QUICK_START, ...guide.GUIDE_USE_CASES, ...guide.GUIDE_TIPS, ...guide.GUIDE_FAQS]) {
    assert.doesNotMatch(JSON.stringify(item), /suggested repl(?:y|ies)|drafts are copy-only|Copy-only referral|Nothing is sent for you/i);
  }
});

test("every guide image referenced exists in public/guide", {
  skip: process.env.SUPYMEM_MODE === "local" && "Hosted-only artwork. Run this test in your hosted checkout.",
}, async () => {
  const images = new Set(guide.GUIDE_CAPABILITIES.flatMap(card => card.image ? [card.image] : []));
  assert.ok(images.size > 0);
  for (const name of [...images, "hero"]) assert.ok(existsSync(new URL(`../../public/guide/${name}.webp`, import.meta.url)), `${name}.webp missing`);
  for (const item of [...guide.GUIDE_QUICK_START, ...guide.GUIDE_USE_CASES, ...guide.GUIDE_TIPS]) {
    if ("image" in item) assert.ok(existsSync(new URL(`../../public/guide/${item.image}.webp`, import.meta.url)));
  }
});

test("browser guide opens the visible task card", () => {
  const card = guide.GUIDE_CAPABILITIES.find(card => card.id === "mac")!;
  assert.equal(guide.guideDestinationHref(card.destination!, "ven_1"), "/v/ven_1/settings#settings-computer-use");
  assert.equal(isBetaHiddenPath(guide.guideDestinationHref(card.destination!, "ven_1")), false);
});

test("the guide covers chat-first work, Idea, LinkedIn, automatic email, diagrams and the per-email price", () => {
  const card = (id: string) => guide.GUIDE_CAPABILITIES.find(c => c.id === id)!;
  assert.match(card("chat").description, /main screen/);
  assert.equal(card("idea-page").destination, "idea");
  assert.match(card("linkedin").description, /found in the background when search is available.*Google results read in your own browser on your Mac/);
  assert.match(card("linkedin").result, /send each one yourself/);
  assert.match(card("auto-email").description, /day 1, 2, 4, 7, day 2, 4, 7 \(recommended\), day 3, 7 or none/);
  assert.match(card("auto-email").description, /Automate all emails.*saved daily limit.*default is 15 a day, not a fixed maximum/);
  assert.match(card("auto-email").description, /Playbook experiments/);
  assert.match(card("auto-email").note!, /source disclosure and opt-out/);
  assert.match(card("diagrams").description, /without being asked/);
  for (const c of guide.GUIDE_CAPABILITIES) assert.doesNotMatch(JSON.stringify(c), /[\u2013\u2014]/, c.id);
});

test("quick starts, use cases and tips have ready prompts, valid destinations and shipped capabilities", () => {
  assert.equal(guide.GUIDE_QUICK_START.length, 3);
  assert.ok(guide.GUIDE_USE_CASES.length >= 6 && guide.GUIDE_USE_CASES.length <= 8);
  assert.ok(guide.GUIDE_TIPS.length >= 6 && guide.GUIDE_TIPS.length <= 10);
  const items = [...guide.GUIDE_QUICK_START, ...guide.GUIDE_USE_CASES, ...guide.GUIDE_TIPS];
  const destinations = ["new", "ventures", "overview", "idea", "research", "site", "gtm", "leads", "experiments", "market-map", "now", "tasks", "suggestions", "settings", "connections", "coding-tools", "local-models", "data", "account", "download", "plan", "analytics", "builds", "browser-tasks"] satisfies guide.GuideDestination[];
  assert.equal(new Set(items.map(item => item.id)).size, items.length);
  for (const item of items) {
    assert.ok(item.prompt.trim(), item.id);
    assert.ok(destinations.includes(item.destination), item.id);
    assert.equal(guide.guidePromptById(item.id), item.prompt);
    assert.doesNotMatch(JSON.stringify(item), /[\u2013\u2014]/);
    if ("capabilityId" in item) assert.ok(guide.GUIDE_CAPABILITIES.some(card => card.id === item.capabilityId && !card.comingNext), item.id);
  }
  for (const id of [null, "", "not-a-guide-prompt", "start-idea&send=1"]) assert.equal(guide.guidePromptById(id), undefined);
});

test("the guide renders ordered sections, all native FAQs and exact prompts that prefill without sending", () => {
  const renderedGuide = view("ven_fixture"), tree = nodes(renderedGuide.render());
  assert.equal(text(tree.find(node => node.type === "h1")), "How to use Supymem");
  assert.deepEqual(tree.filter(node => node.type === "h2").map(text), ["Start here", "Use cases", "Everything Supymem can do", "Power user tips", "FAQs"]);
  const sections = tree.filter(node => node.type === "nav" && node.props["aria-label"] === "Guide sections");
  for (const link of nodes(sections).filter(node => node.type === "a")) assert.ok(tree.some(node => `#${node.props.id}` === link.props.href));
  const faqs = tree.filter(node => node.props["data-usage-control"] === "guide_faq");
  assert.ok(faqs.length >= 10 && faqs.length <= 14);
  assert.equal(faqs.length, guide.GUIDE_FAQS.length);
  guide.GUIDE_FAQS.forEach((faq, index) => {
    assert.equal(faqs[index].type, "details");
    assert.ok(nodes(faqs[index]).some(node => node.type === "summary" && text(node).includes(faq.question)));
    assert.ok(text(faqs[index]).includes(faq.answer));
  });
  assert.match(text(faqs), /without another confirmation/);
  assert.match(text(faqs), /never sends LinkedIn messages/);
  for (const asset of ["charlie-mark.png"]) {
    assert.ok(tree.some(node => node.type === "img" && node.props.src === `/charlie/${asset}` && node.props.alt));
    assert.ok(existsSync(new URL(`../../public/charlie/${asset}`, import.meta.url)));
  }
  const prompts = [...guide.GUIDE_QUICK_START, ...guide.GUIDE_USE_CASES, ...guide.GUIDE_TIPS];
  tree.filter(node => node.props["data-guide-prompt"]).forEach((node, index) => {
    assert.equal(node.type, "button");
    node.props.onClick();
    assert.equal(renderedGuide.prefills[index], prompts[index].prompt);
  });
  assert.equal(renderedGuide.prefills.length, prompts.length);
  const global = nodes(view().render()).filter(node => node.props["data-guide-prompt"]);
  global.forEach((node, index) => {
    assert.equal(node.props.href, `/new?guide_prompt=${prompts[index].id}`);
    assert.doesNotMatch(node.props.href, /[?&](?:send|approve|start)=/);
  });
});

test("scroll reveals clean up safely and reduced motion disables all animation and transitions", () => {
  let callback: any, disconnected = 0;
  const observed: any[] = [], unobserved: any[] = [];
  const element = { dataset: { guideReveal: "ready" } };
  class Observer {
    constructor(onEntries: any) { callback = onEntries; }
    observe(target: any) { observed.push(target); }
    unobserve(target: any) { unobserved.push(target); }
    disconnect() { disconnected++; }
  }
  for (const reduced of [true, false]) {
    const renderedGuide = view("ven_fixture", false, { IntersectionObserver: Observer, window: { matchMedia: () => ({ matches: reduced }) } });
    renderedGuide.render().props.ref.current = { querySelectorAll: () => [element] };
    const cleanup = renderedGuide.effects.at(-1)!();
    if (reduced) { assert.equal(cleanup, undefined); assert.equal(observed.length, 0); }
    else {
      assert.equal(element.dataset.guideReveal, "pending");
      assert.deepEqual(observed, [element]);
      callback([{ target: element, isIntersecting: false }]);
      assert.equal(element.dataset.guideReveal, "pending");
      callback([{ target: element, isIntersecting: true }]);
      assert.equal(element.dataset.guideReveal, "visible");
      assert.deepEqual(unobserved, [element]);
      cleanup(); assert.equal(disconnected, 1);
    }
  }
  const css = readFileSync(new URL("../components/guide/CapabilityGuide.module.css", import.meta.url), "utf8");
  const reduced = css.slice(css.indexOf("@media (prefers-reduced-motion: reduce)"));
  for (const property of ["animation", "transition", "transform"]) assert.match(reduced, new RegExp(`${property}: none !important`));
  assert.match(reduced, /opacity: 1 !important/);
  assert.doesNotMatch(css, /prefers-color-scheme|data-theme/);
});
