const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const guide = require("../../lib/capabilityGuide.ts");

const source = readFileSync(join(__dirname, "CapabilityGuide.tsx"), "utf8");
const code = ts.transpileModule(source, { compilerOptions: {
  module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
} }).outputText;
const nodes = value => Array.isArray(value) ? value.flatMap(nodes)
  : value?.props ? [value, ...nodes(value.props.children)] : [];
const text = value => Array.isArray(value) ? value.map(text).join(" ")
  : value?.props ? text(value.props.children) : typeof value === "string" ? value : "";

function render(ventureId, window = {}, local = false, capabilities = [], prefills = [], lifecycle) {
  const exports = {};
  const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
  vm.runInNewContext(code, { exports, window, require(id) {
    if (id === "react") return { useRef: () => ({ current: null }), useEffect: callback => lifecycle?.effects.push(callback),
      useState: initial => {
        if (initial?.state === 'loading') return [lifecycle ? lifecycle.read || initial : {state: 'ready', items: capabilities},
          value => { if (lifecycle) lifecycle.read = value; }];
        if (initial === 0) return [lifecycle?.attempt || 0, update => { if (lifecycle) lifecycle.attempt = update(lifecycle.attempt || 0); }];
        return [initial, () => {}];
      } };
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (id === "next/link" || id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a" };
    if (id === "@/lib/capabilityGuide") return guide;
    if (id === "@/lib/chatPrefill") return { prefillChat: prompt => prefills.push(prompt) };
    if (id === "@/components/layout/LocalModeBoundary") return { useLocalMode: () => local,
      useFeatureVisibility: () => (_surface, id) => !local || !["mac", "voice"].includes(id) };
    if (id === "@/lib/client") return { api: lifecycle?.api || (() => { throw Error('Unexpected hosted capability fetch'); }) };
    if (id.endsWith('/capabilities/capabilities')) return require('../../app/settings/local/capabilities/capabilities.ts');
    if (id === "@/components/layout/AppShell") return { AppShell: "shell" };
    if (id === "@/components/layout/NavIcons") return { NAV_ICONS: new Proxy({}, { get: () => () => jsx("svg", {}) }) };
    if (id.endsWith(".module.css")) return { default: new Proxy({}, { get: (_, key) => key }) };
    throw new Error(`Unexpected guide dependency: ${id}`);
  } });
  return nodes(exports.CapabilityGuide({ ventureId }));
}

const domains = [
  { ventureId: "ven_warehouse_dispatch", audience: "warehouse dispatch teams" },
  { ventureId: "ven_weekend_hiking", audience: "weekend hiking groups" },
].map(domain => ({ ...domain, tree: render(domain.ventureId) }));

const readyCapabilities = ['research', 'find_customers', 'send_email', 'read_replies', 'landing_pages',
  'publish_pages', 'page_measurement', 'background_work', 'run_for_me_experiments', 'routines', 'connected_apps', 'content']
  .map(name => ({name, title: name, state: 'ready', missing: [], next_step: ''}));

function localCard(tree, id) {
  const card = tree.find(node => node.type === 'article' && node.props['aria-labelledby'] === `guide-${id}`);
  assert.ok(card, id);
  return card;
}

test('your local guide walks from the idea to evidence, builds and recovery across domains', () => {
  for (const { ventureId } of domains) {
    const tree = render(ventureId, {}, true, readyCapabilities);
    for (const [id, copy, href] of [
      ['clarify', /continue with what you shared/i, '/new'],
      ['research', /keyless.*Tavily.*Exa/i, `/v/${ventureId}/research`],
      ['drafts', /source disclosure.*opt-out.*send caps.*Send a test to yourself/i, `/gtm?venture=${ventureId}`],
      ['experiments', /design.*Record what actually happened/i, `/v/${ventureId}/experiments`],
      ['page', /private preview.*ZIP/i, `/v/${ventureId}/site`],
      ['app-build', /your own.*tool/i, `/v/${ventureId}/builds`],
      ['coding-tools', /Claude Code, Codex, Cursor or OpenCode/i, '/settings/local#coding-tools'],
      ['local-models', /Ollama.*LM Studio.*OpenAI-compatible/i, '/settings/local#providers-title'],
      ['export-support', /replaces.*does not merge/i, '/settings/local#your-data-title'],
      ['privacy', /no telemetry.*updates are off by default/i, '/settings/local'],
    ]) {
      const card = localCard(tree, id);
      assert.match(text(card), copy, id);
      assert.equal(nodes(card).find(node => node.props['data-usage-control'] === 'guide_open')?.props.href, href, id);
    }
    assert.doesNotMatch(text(tree), /connected Gmail|Automate all emails|Buy more|\u2014/i);
  }
});

test('your local guide explains every build through your own coding tool across domains', () => {
  for (const domain of domains) {
    const tree = render(domain.ventureId, {}, true, readyCapabilities);
    const page = text(localCard(tree, 'page'));
    const build = text(localCard(tree, 'app-build'));
    const tools = text(localCard(tree, 'coding-tools'));
    assert.match(build, /product, fixes, scrapers and small experiment tools/);
    assert.match(build, /Charlie helps you choose what to build/);
    assert.match(build, /writes.*brief.*starts it.*checks/);
    assert.match(build, /your laptop/);
    for (const copy of [page, build]) {
      assert.match(copy, /Watch or stop.*Build activity/);
      assert.match(copy, /Open result folder/);
      assert.match(copy, /Nothing is published.*you ask/);
      assert.match(copy, /Settings > Keys and connections > Coding tools/);
    }
    assert.match(tools, /Check the connection, review the detected location and choose Allow Charlie/);
    assert.match(tools, /your own subscription login/);
    assert.match(tools, /connection is not ready.*How to sign in.*check again/);
    assert.match(tools, /Connecting a tool does not start a build/);
    assert.ok(nodes(localCard(tree, 'coding-tools')).some(node => node.props.href === '/settings/local#coding-tools'));
    assert.match(page, /Without a connected tool, Charlie offers the connection steps/);
    assert.doesNotMatch(text(tree), /Supymem (?:builds|generates|makes) (?:your|a) (?:code|pages|landing page)|\u2014/);
    assert.ok(!build.includes(domain.audience));
  }
});

test('your local spending guidance distinguishes your saved cap from provider quota across domains', () => {
  for (const domain of domains) {
    const tree = render(domain.ventureId, {}, true, readyCapabilities);
    assert.match(text(localCard(tree, 'account')), /Monthly spending limit.*Keys and connections/);
    assert.match(text(localCard(tree, 'account')), /provider.*account/i);
    assert.match(text(tree.filter(node => node.props['data-usage-control'] === 'guide_faq')), /Monthly spending limit/);
    assert.doesNotMatch(text(tree), /Buy more|team reviews|invitation code|beta download|supymem\.com|Coming soon|Broader access|eligible workspaces|preview access|contact support/i);
  }
});

test('your local account and privacy actions open your saved settings across domains', () => {
  for (const domain of domains) {
    const tree = render(domain.ventureId, {}, true, readyCapabilities);
    for (const id of ['account', 'privacy']) {
      assert.ok(nodes(localCard(tree, id)).some(node => node.type === 'a' && node.props.href === '/settings/local'), id);
    }
    assert.doesNotMatch(text(tree.filter(node => node.props['data-usage-control'] === 'guide_faq')), /beta|invitation|approve the Mac connection/i);
  }
});

test('every local example fills a chat draft without replacing its screen link or starting work', () => {
  const prompts = guide.guideContent(true).capabilities.filter(card => card.prompt);
  for (const domain of domains) {
    const prefills = [];
    const tree = render(domain.ventureId, {}, true, readyCapabilities, prefills);
    assert.equal(prefills.length, 0, 'Rendering never starts work');
    assert.deepEqual(tree.filter(node => node.props['data-guide-prompt']).map(node => node.props['data-guide-prompt']), prompts.map(card => card.id));
    for (const { id, destination } of prompts) {
      const card = nodes(localCard(tree, id));
      card.find(node => node.props['data-guide-prompt'] === id).props.onClick();
      assert.equal(card.find(node => node.props['data-usage-control'] === 'guide_open').props.href,
        guide.guideDestinationHref(destination, domain.ventureId, true));
    }
    assert.deepEqual(prefills, prompts.map(card => card.prompt));
  }
});

test('your guide preserves the exact owner setup step instead of offering blocked research across domains', () => {
  for (const domain of domains) {
    for (const state of ['off', 'needs_setup']) {
      const next_step = 'Open Settings > Capabilities and turn on Research, then request your research again.';
      const capabilities = readyCapabilities.map(item => item.name === 'research' ? {...item, state, next_step} : item);
      const tree = render(domain.ventureId, {}, true, capabilities);
      const card = localCard(tree, 'research');
      assert.ok(text(card).includes(next_step));
      const action = nodes(card).find(node => node.props['data-usage-control'] === 'guide_open');
      assert.equal(action.type, 'a');
      assert.equal(action.props.href, '/settings/local/capabilities');
      assert.equal(action.props.onClick, undefined);
      assert.ok(!nodes(card).some(node => node.props['data-guide-prompt']));
    }
    const tree = render(domain.ventureId, {}, true);
    assert.match(text(localCard(tree, 'research')), /Settings > Capabilities/);
  }
});

test('your local guide reads saved feature readiness before offering a research action across domains', async () => {
  for (const domain of domains) {
    const calls = [], prefills = [];
    const lifecycle = {effects: [], async api(path) {
      calls.push(path);
      return {ok: true, data: {capabilities: readyCapabilities}};
    }};
    const initial = render(domain.ventureId, {}, true, [], prefills, lifecycle);
    assert.equal(nodes(localCard(initial, 'research')).find(node => node.props['data-usage-control'] === 'guide_open').props.href, '/settings/local/capabilities');
    lifecycle.effects.splice(0).forEach(effect => effect());
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(calls, ['/api/local/capabilities']);
    const loaded = render(domain.ventureId, {}, true, [], prefills, lifecycle);
    assert.equal(nodes(localCard(loaded, 'research')).find(node => node.props['data-guide-prompt'] === 'research').type, 'button');
    assert.deepEqual(prefills, []);
  }
});

test('your local guide distinguishes a pending read from failed status and can retry in place', async () => {
  for (const {ventureId} of domains) {
    const calls = [];
    const lifecycle = {effects: [], async api(path) {
      calls.push(path);
      return {ok: true, data: calls.length === 1 ? {capabilities: [{}]} : {capabilities: readyCapabilities}};
    }};
    const draw = () => render(ventureId, {}, true, [], [], lifecycle);
    let tree = draw();
    assert.match(text(tree), /Loading your feature status/);
    lifecycle.effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    tree = draw();
    assert.match(text(tree), /Couldn’t load your feature status/);
    assert.match(text(localCard(tree, 'coding-tools')), /Coding tools/);
    const retry = tree.find(node => node.type === 'button' && node.props.children === 'Try again');
    assert.ok(retry);
    lifecycle.effects.length = 0;
    retry.props.onClick();
    assert.match(text(draw()), /Loading your feature status/);
    assert.equal(lifecycle.attempt, 1);
    lifecycle.effects.shift()();
    await new Promise(resolve => setImmediate(resolve));
    tree = draw();
    assert.deepEqual(calls, ['/api/local/capabilities', '/api/local/capabilities']);
    assert.equal(nodes(localCard(tree, 'research')).find(node => node.props['data-guide-prompt'] === 'research').type, 'button');
  }
});

test('your local publication guide uses your saved publishing destination across domains', () => {
  for (const domain of domains) {
    const tree = render(domain.ventureId, {}, true, readyCapabilities);
    assert.match(text(tree), /Vercel, Netlify or S3/);
    assert.match(text(tree), /Keys and connections.*publishing/i);
    assert.doesNotMatch(text(tree), /\.supymem\.com/);
    assert.equal(tree.some(node => node.props['data-usage-control'] === 'guide_upcoming'), false);
    assert.equal(tree.some(node => node.type === 'button' && text(node) === 'Coming next'), false);
  }
});

function forEachDomain(check) {
  for (const domain of domains) {
    const card = id => {
      const article = domain.tree.find(node => node.type === "article" && node.props["aria-labelledby"] === `guide-${id}`);
      assert.ok(article, `${id} is available to you for ${domain.audience}`);
      const capability = guide.GUIDE_CAPABILITIES.find(item => item.id === id);
      if (capability.destination) assert.ok(nodes(article).some(node => node.type === "a"
        && node.props.href === guide.guideDestinationHref(capability.destination, domain.ventureId)), `${id} keeps your venture context`);
      return text(article);
    };
    assert.doesNotMatch(text(domain.tree), /\u2014/);
    check(card, domain);
  }
}

test("your guide distinguishes requested work from consent and suggestions in either domain", () => {
  forEachDomain((card, domain) => {
    for (const id of ["drafts", "daily-email-limit", "settings", "saved-builds", "mac"])
      assert.doesNotMatch(card(id), /confirmation|approval screen|approve it first|separate reviews?|gated preview/i);
    const planned = guide.GUIDE_CAPABILITIES.find(item => item.id === "app-build");
    const upcoming = domain.tree.find(node => node.type === "article" && text(node).includes(planned.title));
    assert.ok(upcoming);
    assert.doesNotMatch(text(upcoming), /separate reviews?/);
    assert.match(text(upcoming), /Ask for a plan, build or deployment when you are ready/);
    assert.match(card("drafts"), /ask to send the reviewed drafts/);
    assert.match(card("daily-email-limit"), /Raising the limit does not send anything by itself/);
    assert.match(card("mac"), /permissions/);
    assert.match(card("suggestions"), /does not grant permission to send, post, buy or delete/);
  });
});

test('37: onboarding guidance prepares you for dynamic questions rather than a fixed count', () => {
  forEachDomain(card => {assert.match(card('idea'), /answer the next question and confirm a venture name/); assert.doesNotMatch(card('idea'), /three short questions/);});
});

test('38: LinkedIn guidance explains both background and connected Mac search with manual sending', () => {
  forEachDomain((card, domain) => {
    assert.match(card('linkedin'), /background when search is available/); assert.match(card('linkedin'), /browser on your Mac/);
    assert.match(card('linkedin'), /send each one yourself/); assert.doesNotMatch(card('linkedin'), /Profile search needs the Mac app/);
    const faq = text(domain.tree.filter(node => node.props?.['data-usage-control'] === 'guide_faq'));
    assert.match(faq, /without a Mac when search is available/); assert.match(faq, /never sends LinkedIn messages/);
  });
});

test('39: venture-switching instructions name the actual sidebar destination', () => {
  forEachDomain(card => {assert.match(card('portfolio'), /All ventures at the bottom of the sidebar/); assert.doesNotMatch(card('portfolio'), /top left/);});
});

test('40: plans, resources, builds and analytics point you to the chat surfaces they describe', () => {
  forEachDomain((card, domain) => {
    for (const id of ['plan', 'resources', 'saved-builds', 'analytics']) {
      const capability = guide.GUIDE_CAPABILITIES.find(item => item.id === id);
      assert.equal(capability.destination, 'overview'); assert.match(card(id), /chat/);
      assert.equal(guide.guideDestinationHref(capability.destination, domain.ventureId), `/v/${domain.ventureId}`);
    }
  });
});

test('41: help guidance distinguishes downloading your venture report from sending an account report', () => {
  forEachDomain(card => {assert.match(card('export-support'), /Report a problem in Account settings to send a report/);
    assert.match(card('export-support'), /download report to download one, not send it/);});
});

test("new experiment and goal guidance stays non-actionable without availability signals in either domain", () => {
  forEachDomain((card, domain) => {
    const upcoming = domain.tree.find(node => node.props["data-usage-control"] === "guide_upcoming");
    for (const id of ["experiment-budget", "experiment-decisions", "call-coaching", "weekly-goals"]) {
      const capability = guide.GUIDE_CAPABILITIES.find(item => item.id === id);
      assert.equal(capability.comingNext, true);
      assert.equal(capability.topic, "Coming next");
      for (const key of ["prompt", "action", "destination"]) assert.equal(capability[key], undefined);
      assert.match(capability.description, /^Coming soon:/);
      assert.match(capability.note, /no release date is promised/);
      assert.ok(text(upcoming).includes(capability.title));
      assert.ok(!JSON.stringify(capability).includes(domain.audience));
      assert.ok(!guide.GUIDE_USE_CASES.some(item => item.capabilityId === id));
    }
    assert.ok(!nodes(upcoming).some(node => ["a", "button"].includes(node.type)));
    assert.match(text(upcoming), /saved limits when you request ongoing experiments/);
    assert.match(text(upcoming), /Ask to send reviewed drafts or explicitly request ongoing sending/);
    assert.match(text(upcoming), /what is too early to tell/);
    assert.match(text(upcoming), /Tell Charlie which decision you want to apply/);
    assert.match(text(upcoming), /spin-off idea from evidence/);
    assert.match(text(upcoming), /Mom Test coaching/);
    const stories = domain.tree.find(node => node.props.id === "guide-use-cases");
    assert.match(text(stories), /Coming soon:.*visible checklist.*required approvals/);
    const faq = domain.tree.find(node => node.props["data-usage-control"] === "guide_faq"
      && text(node).includes("Can Charlie run experiments"));
    assert.match(text(faq), /starts without a confirmation card/);
    assert.match(text(faq), /Work you did not ask for does not start on its own/);
    const connections = card("connections");
    for (const provider of ["Sheets", "Notion", "Cal.com", "Calendly", "Typeform", "Google Analytics", "Search Console", "Stripe", "HubSpot", "Slack", "GitHub"]) assert.ok(connections.includes(provider), provider);
    assert.match(connections, /when available in your workspace/);
    assert.match(connections, /Stripe revenue access is read-only/);
    assert.doesNotMatch(text(domain.tree), /experiments_autopilot_v1|goals_v1/);
  });
});

test("your next moves stay available while work runs across business and consumer ventures", () => {
  forEachDomain(card => {
    const copy = card("next-step");
    assert.match(copy, /what should I do next\?/i);
    assert.match(copy, /saved progress.*buttons/);
    assert.match(copy, /when you return/);
    assert.match(copy, /without stopping work already underway/);
    assert.match(copy, /not permission to send/);
  });
});

test("direct starts do not add another approval or waive safety checks in either domain", () => {
  forEachDomain((card, domain) => {
    const copy = card("direct-starts");
    assert.match(copy, /research your idea, find customers or build a landing page/);
    assert.match(copy, /Supported work starts directly/);
    assert.match(copy, /reach your limit.*Buy more/);
    assert.match(copy, /Sending, publishing and setting changes run when you ask/);
    assert.match(copy, /Connection, permission and safety checks still apply/);
    assert.doesNotMatch(text(domain.tree), /Anything that spends credits.*waits for your confirmation|credit ceiling you confirm first|Show the credit ceiling before starting/);
  });
});

test("guide fills the window with stretching panels and a scrolling card canvas in either domain", () => {
  const css = readFileSync(join(__dirname, "CapabilityGuide.module.css"), "utf8");
  const office = readFileSync(join(__dirname, "../office/CompanyOffice.module.css"), "utf8");
  forEachDomain((card, domain) => {
    assert.equal(domain.tree[0].props.className, "guide");
    const shell = css.match(/:global\(\.app-shell\.home-shell\):has\(\.guide\)\s*\{([^}]*)\}/)?.[1];
    assert.ok(shell, `The guide fills your window for ${domain.audience}`);
    assert.match(shell, /height:\s*100%/);
    assert.match(shell, /min-height:\s*0/);
    for (const name of ["home", "main"]) {
      const panel = office.match(new RegExp(`\\.${name}\\s*\\{([^}]*)\\}`))[1];
      assert.match(panel, /min-height:\s*0/);
      assert.doesNotMatch(panel, /(?:^|[;\s])(?:height|max-height):/);
    }
    const canvas = office.match(/\.canvas\s*\{([^}]*)\}/)[1];
    assert.match(canvas, /min-height:\s*0/);
    assert.match(canvas, /overflow:\s*auto/);
    const start = domain.tree.find(node => node.type === "section" && node.props.id === "guide-begin");
    const steps = nodes(start).filter(node => node.type === "li");
    assert.equal(steps.length, 3);
    assert.ok(steps.every(step => nodes(step).some(node => node.props["data-guide-prompt"])));
  });
});

test("your guide jumps scroll only its own canvas and keep the shell non-scrollable in either domain", () => {
  const css = readFileSync(join(__dirname, "../../styles/tokens.css"), "utf8");
  forEachDomain((card, domain) => {
    const tree = render(domain.ventureId, { getComputedStyle: () => ({ scrollMarginTop: "24px" }) });
    const links = tree.filter(node => node.type === "a" && node.props.href?.startsWith("#"));
    assert.ok(links.length > 0);
    for (const top of [60, 128]) {
      const container = { scrollTop: 175, clientTop: 2, getBoundingClientRect: () => ({ top }) };
      let focused = 0;
      const sections = new Map(tree.filter(node => node.type === "section").map((node, index) => [
        `#${node.props.id}`, { getBoundingClientRect: () => ({ top: top + container.clientTop + 500 + index * 200 - container.scrollTop }),
          focus(options) { assert.equal(node.props.tabIndex, -1); assert.equal(options.preventScroll, true); focused++; } },
      ]));
      tree[0].props.ref.current = {
        closest(selector) { assert.equal(selector, ".workspace-body"); return container; },
        querySelector(selector) { assert.ok(sections.has(selector)); return sections.get(selector); },
      };
      for (const link of links) {
        assert.equal(typeof link.props.onClick, "function", `${link.props.href} must prevent native ancestor scrolling for ${domain.audience}`);
        for (const initialScroll of [0, 175]) {
          container.scrollTop = initialScroll;
          const targetTop = sections.get(link.props.href).getBoundingClientRect().top;
          const event = { currentTarget: { hash: link.props.href }, defaultPrevented: false,
            preventDefault() { this.defaultPrevented = true; } };
          link.props.onClick(event);
          assert.equal(event.defaultPrevented, true);
          assert.equal(container.scrollTop, initialScroll + targetTop - top - container.clientTop - 24);
        }
      }
      assert.equal(focused, links.length * 2);
      tree[0].props.ref.current = null;
      const event = { currentTarget: { hash: links[0].props.href }, defaultPrevented: false,
        preventDefault() { this.defaultPrevented = true; } };
      links[0].props.onClick(event);
      assert.equal(event.defaultPrevented, true);
    }
    assert.doesNotMatch(source, /scrollIntoView|document\.|window\.scroll|location\.hash/);
    const root = css.match(/html:has\(\.app-shell\), body:has\(\.app-shell\)\s*\{([^}]*)\}/)?.[1];
    assert.ok(root, `All shell pages lock your document for ${domain.audience}`);
    assert.match(root, /height:\s*100%/);
    assert.match(root, /overflow:\s*clip/);
    for (const selector of [".app-shell", ".app-shell.home-shell"]) {
      const shell = css.match(new RegExp(`(?:^|\\n)${selector.replaceAll(".", "\\.")}\\s*\\{([^}]*)\\}`))?.[1];
      assert.ok(shell);
      assert.match(shell, /overflow:\s*clip/);
    }
  });
});

test("your guide explains the collapsible agents tray pill in either domain", () => {
  forEachDomain(card => {
    const copy = card("progress");
    assert.match(copy, /agents tray.*pill.*running, done or need you/);
    assert.match(copy, /Open it.*progress.*saved results.*collapse it/);
    assert.match(copy, /keep chatting while work runs/);
  });
});

test("slow replies retry safely without asking you to repeat work in either domain", () => {
  forEachDomain(card => {
    const copy = card("slow-replies");
    assert.match(copy, /Charlie is slow.*retries automatically when it is safe/);
    assert.match(copy, /do not need to send the same question again/);
    assert.match(copy, /reason and a Try again action when available/);
    assert.match(copy, /not an email send or a publishing action/);
  });
});

test("your research recovery retains sources and explains safe Rerun in either domain", () => {
  forEachDomain(card => {
    const copy = card("research");
    assert.match(copy, /safe unfinished work resumes automatically.*saved sources remain available/);
    assert.match(copy, /Choose Rerun when offered to continue safely; previous reports remain available/);
    assert.match(copy, /rerun just the review using saved sources without searching again/);
    assert.match(copy, /paid step whose outcome is unknown is not repeated automatically/);
  });
});

test("your long live calls renew without starting over and retain recovery in either domain", () => {
  forEachDomain(card => {
    const copy = card("voice");
    assert.match(copy, /live call renews in the background.*long calls without starting over/);
    assert.match(copy, /call cannot renew, use Retry or type instead/);
    assert.match(copy, /microphone permission/);
    assert.match(copy, /save notes when offered/);
  });
});

test("your lead status words distinguish preparation, sending and outcomes in either domain", () => {
  forEachDomain(card => {
    const copy = card("lead-status");
    for (const status of ["Suggested", "Verified", "Drafted", "Approved", "Queued", "Sent", "Reply received", "Positive reply", "Meeting booked"])
      assert.ok(copy.includes(`${status} means`), status);
    assert.match(copy, /not that their mailbox works or that you have permission to send/);
    assert.match(copy, /Queued means.*not that it was sent/);
    assert.match(copy, /not that it was delivered or read/);
    assert.match(copy, /not that you have a paying customer/);
    assert.match(copy, /Delivery unconfirmed.*check the saved sending result before trying again/);
  });
});

test("your daily email limit can rise from 15 to 100 only with your request in either domain", () => {
  forEachDomain((card, domain) => {
    const copy = card("daily-email-limit");
    assert.match(copy, /15 a day is recommended.*default daily email limit, including follow-ups/);
    assert.match(copy, /maximum of 100 a day.*Charlie saves your choice/);
    assert.match(copy, /Raising the limit does not send anything by itself/);
    assert.match(copy, /Set my email limit to 30\./);
    assert.match(copy, /sender address.*where you found the recipient's address or that you imported the contact.*opt-out line/);
    assert.match(copy, /Contacts blocked from outreach.*opted out or is not eligible.*not emailed/);
    assert.match(card("auto-email"), /default is 15 a day, not a fixed maximum/);
    assert.match(card("market-beliefs"), /saved daily limit.*higher limit up to 100/);
    const faq = domain.tree.find(node => node.type === "details" && text(node).includes("How many emails can go out each day?"));
    assert.match(text(faq), /higher daily limit, up to 100.*requested change applies directly/);
    assert.doesNotMatch(text(domain.tree), /at most 15 emails a day|Gmail, up to 15 a day/);
  });
});
