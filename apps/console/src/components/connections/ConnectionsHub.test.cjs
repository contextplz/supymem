const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { test } = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");
const React = require("react");
const { renderToStaticMarkup } = require("react-dom/server");
const { createHash } = require("node:crypto");

const DOMAINS = [
  { ventureId: "ven_fleet_planning", workspaceId: "wsp_fleet", email: "owner@fleet.example", purpose: "Compare your delivery planning results.",
    legacyHtmlSha256: "cd6afa9fd6ad4841ac5336a56de7978ee2a6d7e950ea1192f2d658836aeed18c" },
  { ventureId: "ven_cooking_classes", workspaceId: "wsp_cooking", email: "owner@cooking.example", purpose: "Review your cooking class bookings.",
    legacyHtmlSha256: "831aec5e99feebae24046ac1b8c0fa0d6e5f3b79877a2c4c9dab415cc6edafac" },
];
const PROVIDERS = [
  ["gmail", "Gmail"], ["google_calendar", "Google Calendar"], ["linkedin", "LinkedIn"], ["reddit", "Reddit"],
  ["googlesheets", "Google Sheets"], ["cal", "Cal.com"], ["google_analytics", "Google Analytics"],
  ["google_search_console", "Google Search Console"], ["notion", "Notion"], ["stripe", "Stripe"],
  ["typeform", "Typeform"], ["calendly", "Calendly"], ["hubspot", "HubSpot"], ["slack", "Slack"],
  ["github", "GitHub"], ["googledrive", "Google Drive"], ["googledocs", "Google Docs"],
];
const nodes = node => !node || typeof node !== "object" ? [] : [node, ...[node.props?.children].flat(8).flatMap(nodes)];
const text = node => typeof node === "string" ? node : !node || typeof node !== "object" ? "" : [node.props?.children].flat(8).map(text).join("");
const flush = () => new Promise(resolve => setImmediate(resolve));

async function fixture(domain, inboxes = null, briefingEnabled = false, uiConfig = async () => ({ ok: true, data: { founder_ui: "chat" } }), local = false) {
  const copy = await import("./connectionCopy.ts");
  const source = readFileSync(`${__dirname}/ConnectionsHub.tsx`, "utf8");
  const tree = ts.createSourceFile("ConnectionsHub.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const body = tree.statements.filter(statement => !ts.isImportDeclaration(statement)).map(statement => statement.getText(tree)).join("\n");
  const compiled = ts.transpileModule(body, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.React } }).outputText;
  const cards = Object.fromEntries(PROVIDERS.map(([id, name], index) => [id, { id, name, connected: id === "gmail", ready: true,
    configured: true, action: id === "gmail" ? "sync" : "connect", note: domain.purpose,
    ...(index > 3 ? { toolkit_slug: id, accounts: [{ id: `account_${id}` }] } : {}),
    ...(id === "gmail" ? { account_email: domain.email } : {}),
  }]));
  cards.reddit = { ...cards.reddit, connected: true, needs_reconnect: true, ready: false };
  cards.unconfigured = { id: "unconfigured", toolkit_slug: "unconfigured", configured: false };
  let cursor = 0, confirm = false, scope = { workspaceId: domain.workspaceId, ventureId: domain.ventureId, actorId: "usr_owner" };
  const slots = [], effects = [], calls = [], confirmations = [], links = [], timers = [];
  const request = async (path, options) => {
    const payload = JSON.parse(options.body);
    calls.push({ path, payload, headers: options.headers });
    if (payload.message_id) {
      cards[payload.payload.provider] = { ...cards[payload.payload.provider], connected: false, needs_reconnect: false, action: "connect" };
      return { ok: true, data: { confirmed_action: { action: "connections.disconnect", message_id: payload.message_id, state: "started" } } };
    }
    return { ok: true, data: { id: "approval_saved", actions: [{ confirmation: { action: "connections.disconnect", interpretation: "Disconnect your account?",
      consequences: ["You will stop using this account across ventures that share it."], resulting_state: { provider: payload.payload.provider, accounts: { saved_account: "saved_connection" } } } }] } };
  };
  const context = {
    exports: {}, React, URL, URLSearchParams, crypto: require("node:crypto").webcrypto, ...copy,
    NAV_ICONS: { connections: () => React.createElement("svg", { "aria-hidden": true }) },
    setTimeout: callback => {timers.push(callback); return callback;}, clearTimeout: callback => {const index = timers.indexOf(callback); if (index >= 0) timers.splice(index, 1);},
    useOperationsScope: () => scope, useIsOperator: () => false, isLocalConsole: () => local,
    useFeatureVisibility: () => (_surface, id) => !local || !["gmail", "unipile", "linkedin", "google_ads", "meta_ads"].includes(id),
    useSearchParams: () => new URLSearchParams({ workspace: domain.workspaceId, venture: domain.ventureId }),
    useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial; return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }]; },
    useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
    useMemo: callback => callback(), useCallback: callback => callback,
    useEffect(callback, deps) { const index = cursor++, prior = slots[index]; if (!prior || deps.some((value, dep) => prior[dep] !== value)) { slots[index] = deps; effects.push(callback); } },
    verifiedSessionSubject: () => scope?.actorId, verifiedSessionEpoch: () => 1, getOpsWorkspace: () => domain.workspaceId,
    getConnectionsStatus: async () => ({ workspace_id: domain.workspaceId,
      order: Object.keys(cards).filter(id => !local || !["gmail", "unipile", "linkedin", "google_ads", "meta_ads"].includes(id)),
      connections: { ...cards }, ready_count: 1, total_count: Object.keys(cards).length, blockers: [] }),
    getHubGmailVerification: async () => ({ receipt: null }),
    getGmailMailboxes: async () => {if (inboxes instanceof Error) throw inboxes; return inboxes;},
    mailboxCanSend: mailbox => mailbox.status === "active" && !mailbox.needs_reconnect,
    setDefaultGmailMailbox: async (workspaceId, mailboxId) => { calls.push({ default: mailboxId, workspaceId }); inboxes.mailboxes = inboxes.mailboxes.map(mailbox => ({ ...mailbox, is_default: mailbox.id === mailboxId })); },
    disconnectGmailMailbox: async (workspaceId, mailboxId, expectedUpdatedAt) => { calls.push({ disconnected: mailboxId, workspaceId, expectedUpdatedAt }); inboxes.mailboxes = inboxes.mailboxes.filter(mailbox => mailbox.id !== mailboxId); },
    verifyHubGmail: async (ventureId, mailboxId, workspaceId) => { calls.push({ verified: mailboxId, ventureId, workspaceId }); return { ok: true, status: "verified", email: inboxes.mailboxes.find(mailbox => mailbox.id === mailboxId).email }; },
    connectHubProvider: async (provider, payload) => { calls.push({ provider, payload }); return { authorize_url: "https://accounts.example/authorize" }; },
    syncHubProvider: async () => ({ ok: true }), api: (path, ...args) => path === "/api/ui-config" ? uiConfig() : request(path, ...args),
    revokeContentAccount: async id => { calls.push({ revoked: id }); cards.github = { ...cards.github, connected: false, accounts: [] }; return { id, status: "revoked" }; },
    founderConnectionMessage: () => "You can try connecting again.", founderSafeConnectionText: value => value,
    opsErrorMessage: (error, fallback) => error.message || fallback,
    Link: ({ children, ...props }) => React.createElement("a", props, children), ConnectTools: () => null,
    CodingAgentTiles: () => React.createElement("section", { "data-coding-agents": true }),
    ErrorNotice: ({ children, error }) => React.createElement("div", null, children || error),
    InboxBriefingConsent: props => briefingEnabled ? React.createElement("input", { type: "checkbox", "data-mailbox": props.mailboxId }) : null,
    styles: new Proxy({}, { get: (_, name) => String(name) }), isDesktopShell: () => false,
    navigator: { clipboard: { writeText: async () => {} } },
    window: { location: { href: `https://console.example/connections?workspace=${domain.workspaceId}&venture=${domain.ventureId}` },
      addEventListener() {}, removeEventListener() {}, open: href => { links.push(href); return {}; },
      confirm: value => { confirmations.push(value); return confirm; } },
  };
  vm.runInNewContext(compiled, context);
  const render = () => { cursor = 0; return context.exports.ConnectionsHub(); };
  render(); while (effects.length) effects.shift()(); await flush();
  return { render, cards, calls, confirmations, links, allow: value => { confirm = value; }, retire: () => { scope = null; },
    request: handler => { context.api = handler; }, setInboxes: value => {inboxes = value;},
    setGmailReads: (verification, mailboxes) => {context.getHubGmailVerification = verification; context.getGmailMailboxes = mailboxes;},
    setErrors: handler => {context.opsErrorMessage = handler;}, failConnection: error => {context.connectHubProvider = async () => {throw error;};},
    setConnectionRead: handler => {context.getConnectionsStatus = handler;}, expireReads: () => timers.splice(0).forEach(callback => callback()) };
}

test('local connections keep app authorization without hosted Gmail reads or mailbox tiles across domains', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain, null, false, undefined, true), reads = [];
    hub.setGmailReads(async () => {reads.push('verification'); return {receipt: null};}, async () => {reads.push('mailboxes'); return null;});
    const refresh = nodes(hub.render()).find(node => node.type === 'button' && text(node).trim() === 'Refresh');
    await refresh.props.onClick(); await flush();
    assert.deepEqual(reads, []);
    assert.ok(!nodes(hub.render()).some(node => node.props?.['aria-label'] === 'Gmail'));
    assert.ok(nodes(hub.render()).some(node => node.props?.['aria-label'] === 'GitHub'));
  }
});

test('local connections show allowed apps without logos and link to mail settings', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain, null, false, undefined, true);
    hub.cards.smtp = { id: 'smtp', name: 'Email', default: true, action: 'fix' };
    hub.cards[domain.ventureId] = { id: domain.ventureId, name: domain.purpose, toolkit_slug: domain.ventureId, configured: false, action: 'fix' };
    nodes(hub.render()).find(node => node.type === 'button' && text(node).trim() === 'Refresh').props.onClick();
    await flush();
    const cards = nodes(hub.render()).filter(node => node.type === 'article');
    const app = cards.find(card => card.props['aria-label'] === domain.purpose);
    const mail = renderToStaticMarkup(cards.find(card => card.props['aria-label'] === 'Email'));
    assert.ok(app);
    assert.match(renderToStaticMarkup(app), /<svg[^>]+aria-hidden="true"/);
    assert.match(renderToStaticMarkup(app), /href="\/settings\/local#composio-title"/);
    assert.match(mail, /href="\/settings\/local#smtp-title"/);
    assert.doesNotMatch(mail, /data-usage-control="connections_action"/);
  }
});

test('hosted social and advertising connections disappear locally while supported apps remain', async () => {
  for (const domain of DOMAINS) for (const local of [true, false]) {
    const hub = await fixture(domain, null, false, undefined, local);
    const labels = nodes(hub.render()).filter(node => node.type === 'h3').map(node => node.props.children);
    assert.equal(labels.includes('LinkedIn'), !local, domain.name);
    assert.ok(labels.includes('Google Sheets'), domain.name);
  }
});

test('your app-connections loader ends with a next step when workspace authority is missing across domains', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain, null, false, undefined, true);
    hub.retire();
    const markup = renderToStaticMarkup(hub.render());
    assert.match(markup, /Reload workspaces|Choose your venture/);
    assert.doesNotMatch(markup, /Loading your app connections/);
  }
});

test('your app-connections loader times out with a recovery step across domains', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain, null, false, undefined, true);
    hub.setConnectionRead(() => new Promise(() => {}));
    nodes(hub.render()).find(node => node.type === 'button' && text(node).trim() === 'Refresh').props.onClick();
    await flush(); hub.expireReads(); await flush();
    const markup = renderToStaticMarkup(hub.render());
    assert.doesNotMatch(markup, /Loading your app connections/);
    assert.match(markup, /Keys and connections.*Reload connections/);
  }
});

test('22: your verified hub is available while independent Gmail reads start together in the same venture and workspace', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain), reads = []; let finish;
    hub.setGmailReads(ventureId => {reads.push(ventureId); return new Promise(resolve => {finish = resolve;});},
      async workspaceId => {reads.push(workspaceId); return null;});
    const refresh = nodes(hub.render()).find(node => node.type === 'button' && text(node).trim() === 'Refresh');
    const pending = refresh.props.onClick(); await flush();
    assert.deepEqual(reads, [domain.ventureId, domain.workspaceId]);
    assert.ok(nodes(hub.render()).some(node => node.props?.['aria-label']?.startsWith('GitHub')));
    finish({receipt: null}); await pending; await flush();
    assert.equal(hub.calls.some(call => call.payload), false);
  }
});

test('08: a failed Gmail read keeps your other verified connections usable and can be retried', async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain, new Error('mailbox transport unavailable'));
    let tree = hub.render(); assert.ok(nodes(tree).some(node => node.props?.['aria-label']?.startsWith('GitHub')));
    assert.match(renderToStaticMarkup(tree), /Couldn’t read your saved Gmail status/);
    const refresh = nodes(tree).find(node => node.type === 'button' && text(node).trim() === 'Refresh');
    hub.setInboxes(null); await refresh.props.onClick(); await flush(); tree = hub.render();
    assert.doesNotMatch(renderToStaticMarkup(tree), /Couldn’t read your saved Gmail status/);
    assert.equal(hub.calls.some(call => call.payload), false);
  }
});

test('09: an unknown connection error shows plain recovery copy rather than a technical exception', async () => {
  const {opsErrorMessage} = await import('../../lib/errorCopy.ts');
  for (const domain of DOMAINS) {
    const hub = await fixture(domain); hub.setErrors(opsErrorMessage);
    hub.failConnection(new Error('API 500: unknown_connection_failure'));
    const card = nodes(hub.render()).find(node => node.type === 'article' && node.props['aria-label'] === 'GitHub');
    const connect = nodes(card).find(node => node.type === 'button' && node.props['data-usage-control'] === 'connections_action');
    assert.ok(connect); await connect.props.onClick(); await flush();
    assert.match(renderToStaticMarkup(hub.render()), /Couldn’t change this connection/);
    assert.doesNotMatch(renderToStaticMarkup(hub.render()), /API 500|unknown_connection_failure|Error:/);
  }
});

test("coding tiles mount only for an enabled current owner and flag-off HTML matches the pre-feature bytes across unrelated domains", async () => {
  for (const domain of DOMAINS) {
    const assertLegacy = hub => {
      const tree = hub.render();
      assert.equal(nodes(tree).filter(node => node.type?.name === "CodingAgentTiles").length, 0);
      assert.equal(createHash("sha256").update(renderToStaticMarkup(tree)).digest("hex"), domain.legacyHtmlSha256);
    };
    for (const data of [null, { founder_ui: "chat" }, ...[false, "true", 1].map(coding_agents_v1 => ({ coding_agents_v1 }))]) {
      const hub = await fixture(domain, null, false, async () => ({ ok: true, data }));
      assertLegacy(hub);
    }
    assertLegacy(await fixture(domain, null, false, async () => ({ ok: false, data: { coding_agents_v1: true } })));
    assertLegacy(await fixture(domain, null, false, async () => { throw new Error("Config unavailable"); }));
    let resolveConfig;
    const pending = await fixture(domain, null, false, () => new Promise(resolve => { resolveConfig = resolve; }));
    assertLegacy(pending);
    resolveConfig({ ok: true, data: { coding_agents_v1: true } }); await flush();
    const enabled = nodes(pending.render()).filter(node => node.type?.name === "CodingAgentTiles");
    assert.equal(enabled.length, 1);
    assert.match(enabled[0].key, /usr_owner/);
    assert.match(renderToStaticMarkup(pending.render()), /data-coding-agents="true"/);
    pending.retire();
    assert.equal(nodes(pending.render()).filter(node => node.type?.name === "CodingAgentTiles").length, 0);
    const stale = await fixture(domain, null, false, () => new Promise(resolve => { resolveConfig = resolve; }));
    stale.retire(); resolveConfig({ ok: true, data: { coding_agents_v1: true } }); await flush();
    assert.equal(nodes(stale.render()).filter(node => node.type?.name === "CodingAgentTiles").length, 0);
  }
});

test("toolkit controls retain company guards and scoped callbacks without another confirmation", async () => {
  const source = readFileSync(`${__dirname}/ConnectionsHub.tsx`, "utf8");
  const { toolkitConnection } = await import("./connectionCopy.ts");
  for (const domain of ["Payroll reporting for employers", "Photography lessons for hobbyists"]) {
    assert.ok(toolkitConnection({ id: "github", toolkit_slug: "github", domain, accounts: [{ id: "saved_receipt" }] }));
    assert.match(source, /connectionVisible\(c\)/);
    assert.match(source, /toolkitConnection\(card\) \? \{ return_to: `\/connections\?\$\{scopedReturn\}` \}/);
    assert.doesNotMatch(source, /window\.confirm/);
    assert.match(source, /!current\(candidate\)/);
    assert.match(source, /disconnectToolkitAccounts\(card, revokeContentAccount, active\)/);
    assert.match(source, /toolkit\?\.accounts\?\.length \|\| !toolkit/);
    assert.match(source, /onClick=\{\(\) => void onDisconnect\(card\)\}/);
    assert.match(source, /Connecting does not approve messages, posts, or payments/);
  }
});

test("two founders manage independent inbox tiles, defaults, verification and exact disconnects without changing legacy UI", async () => {
  for (const domain of DOMAINS) {
    const first = { id: `${domain.workspaceId}_first`, email: domain.email, status: "active", is_default: true, sent_today: 3, daily_send_limit: 15, needs_reconnect: false, updated_at: "2026-10-29T08:00:00" };
    const second = { ...first, id: `${domain.workspaceId}_second`, email: `you@${domain.ventureId}.example`, is_default: false, sent_today: 1 };
    const inboxes = { multi_gmail_v1: true, workspace_id: domain.workspaceId, mailboxes: [first, second] };
    const hub = await fixture(domain, inboxes);
    const tile = email => nodes(hub.render()).find(node => node.type === "article" && node.props["aria-label"] === `Gmail ${email}`);
    assert.match(renderToStaticMarkup(tile(first.email)), /Default/);
    assert.match(renderToStaticMarkup(tile(second.email)), /1 of 15 sends/);
    await nodes(tile(second.email)).find(node => node.type === "button" && text(node) === "Make default").props.onClick(); await flush();
    assert.deepEqual(hub.calls.find(call => call.default), { default: second.id, workspaceId: domain.workspaceId });
    assert.match(renderToStaticMarkup(tile(second.email)), /Default/);
    nodes(tile(first.email)).find(node => node.type === "button" && text(node) === "Verify (read only)").props.onClick(); await flush();
    assert.deepEqual(hub.calls.find(call => call.verified), { verified: first.id, ventureId: domain.ventureId, workspaceId: domain.workspaceId });
    const disconnect = () => nodes(tile(first.email)).find(node => node.type === "button" && text(node) === "Disconnect");
    disconnect().props.onClick(); await flush();
    assert.equal(hub.confirmations.length, 0);
    assert.deepEqual(hub.calls.find(call => call.disconnected), { disconnected: first.id, workspaceId: domain.workspaceId, expectedUpdatedAt: first.updated_at });
    assert.equal(tile(first.email), undefined); assert.ok(tile(second.email));
    nodes(hub.render()).find(node => node.type === "button" && text(node) === "Add another Gmail").props.onClick(); await flush();
    assert.equal(hub.calls.at(-1).payload.intent, "add");
    assert.equal(hub.calls.at(-1).payload.mailbox_id, undefined);
    const stale = await fixture(domain, { ...inboxes });
    const retained = nodes(stale.render()).find(node => node.type === "button" && text(node) === "Verify (read only)");
    stale.retire(); retained.props.onClick(); await flush(); assert.deepEqual(stale.calls, []);
    const legacy = await fixture(domain);
    assert.doesNotMatch(renderToStaticMarkup(legacy.render()), /Add another Gmail|Make default|of 15 sends/);
    const empty = await fixture(domain, { ...inboxes, mailboxes: [] });
    assert.match(renderToStaticMarkup(empty.render()), /Add another Gmail/);
    assert.equal(nodes(empty.render()).some(node => node.type === "article" && node.props["aria-label"] === "Gmail"), false);
    const paused = await fixture(domain, { ...inboxes, mailboxes: [{ ...first, status: "awaiting_reconnect", needs_reconnect: true }, second] });
    const reconnect = nodes(paused.render()).find(node => node.type === "button" && text(node) === "Reconnect");
    reconnect.props.onClick(); await flush();
    assert.equal(paused.calls.at(-1).payload.intent, "reconnect");
    assert.equal(paused.calls.at(-1).payload.mailbox_id, first.id);
    const addWhilePaused = await fixture(domain, { ...inboxes, mailboxes: [{ ...first, status: "awaiting_reconnect", needs_reconnect: true }, second] });
    nodes(addWhilePaused.render()).find(node => node.type === "button" && text(node) === "Add another Gmail").props.onClick(); await flush();
    assert.equal(addWhilePaused.calls.at(-1).payload.intent, "add");
    assert.equal(addWhilePaused.calls.at(-1).payload.mailbox_id, undefined);
  }
});

test("briefing consent is present on every per-inbox tile and both disabled flags preserve your existing tiles", async () => {
  for (const domain of DOMAINS) {
    const mailboxes = ["work", "personal"].map(id => ({ id, email: `${id}.${domain.email}`, status: "active", is_default: id === "work",
      sent_today: 0, daily_send_limit: 15, needs_reconnect: false, updated_at: "2026-10-29T08:00:00" }));
    const enabled = await fixture(domain, { multi_gmail_v1: true, workspace_id: domain.workspaceId, mailboxes }, true);
    for (const mailbox of mailboxes) {
      const tile = nodes(enabled.render()).find(node => node.type === "article" && node.props["aria-label"] === `Gmail ${mailbox.email}`);
      const markup = renderToStaticMarkup(tile);
      assert.match(markup, new RegExp(`data-mailbox="${mailbox.id}"`));
      assert.equal((markup.match(/data-mailbox=/g) || []).length, 1);
    }
    const disabled = await fixture(domain);
    assert.doesNotMatch(renderToStaticMarkup(disabled.render()), /data-mailbox=/);
    assert.doesNotMatch(renderToStaticMarkup(disabled.render()), /Add another Gmail|Default Gmail sender|Inbox briefings/);
  }
});

test("Connections page renders every configured provider, logos, account identity and status groups across unrelated ventures", async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain);
    const rendered = hub.render(), articles = nodes(rendered).filter(node => node.type === "article");
    assert.equal(articles.length, 17);
    assert.equal(articles[0].props["aria-label"], "Gmail");
    const html = renderToStaticMarkup(rendered);
    assert.match(html, new RegExp(domain.email));
    assert.ok(html.includes(domain.purpose));
    assert.match(html, /connectionConnected/);
    assert.match(html, /connectionBadge[^>]*><span aria-hidden="true">✓<\/span>Connected/);
    assert.ok(html.indexOf('id="connected-heading"') < html.indexOf('id="available-heading"'));
    assert.match(html, /Needs reconnect/);
    assert.match(html, /Not connected/);
    assert.doesNotMatch(html, /Ready to connect|unconfigured|More services|NeedsYou/);
    assert.equal(nodes(rendered).filter(node => node.type === "button" && node.props.className === "btn ").length, 1);
    for (const [id, name] of PROVIDERS) {
      const tile = articles.find(node => node.props["aria-label"] === name);
      const image = nodes(tile).find(node => node.type === "img");
      const slug = id === "google_calendar" ? "googlecalendar" : id;
      assert.equal(image.props.src, `/connections/${slug}.svg`);
    }
    const connect = nodes(rendered).find(node => node.type === "article" && node.props["aria-label"] === "Notion");
    await nodes(connect).find(node => node.type === "button").props.onClick();
    await flush();
    assert.match(renderToStaticMarkup(hub.render()), /Connecting/);
    assert.deepEqual(hub.links, ["https://accounts.example/authorize"]);
    assert.match(hub.calls.at(-1).payload.return_to, new RegExp(`workspace=${domain.workspaceId}&venture=${domain.ventureId}`));
  }
  const styles = readFileSync(`${__dirname}/../marketing/MarketingLab.module.css`, "utf8");
  assert.match(styles, /minmax\(min\(100%, 15rem\), 1fr\)/);
  assert.match(styles, /\.connectionConnected\s*\{[^}]*box-shadow: 0 0 0 3px rgba/);
  assert.match(styles, /:focus-visible/);
  assert.match(readFileSync(`${__dirname}/../../app/connections/page.tsx`, "utf8"), /<ConnectionsHub/);
});

test("Connections provider logos ship with your hosted console across unrelated ventures", {
  skip: process.env.SUPYMEM_MODE === "local" && "Hosted-only artwork. Run this test in your hosted checkout.",
}, async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain);
    const articles = nodes(hub.render()).filter(node => node.type === "article");
    for (const [, name] of PROVIDERS) {
      const tile = articles.find(node => node.props["aria-label"] === name);
      const image = nodes(tile).find(node => node.type === "img");
      assert.match(readFileSync(`${__dirname}/../../../public${image.props.src}`, "utf8"), /<svg/);
    }
  }
});

test("Gmail consent remains reachable after reconnect or disconnect for both unrelated founders", async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain);
    for (const needsReconnect of [true, false]) {
      Object.assign(hub.cards.gmail, { connected: false, needs_reconnect: needsReconnect, mailbox_id: `${domain.workspaceId}_inbox`, action: "connect" });
      const consent = nodes(hub.render()).find(node => node.type?.name === "InboxBriefingConsent");
      assert.ok(consent);
      assert.equal(consent.props.mailboxId, `${domain.workspaceId}_inbox`);
      assert.equal(consent.props.ventureId, domain.ventureId);
      assert.equal(consent.props.workspaceId, domain.workspaceId);
    }
  }
});

test("disconnect uses exact saved approval and refuses retired owners without another confirmation", async () => {
  for (const domain of DOMAINS) {
    for (const provider of ["Gmail", "GitHub", "Reddit"]) {
      const hub = await fixture(domain);
      if (provider === "GitHub") Object.assign(hub.cards.github, { connected: true, action: "sync" });
      const disconnect = () => nodes(hub.render()).filter(node => node.type === "article" && node.props["aria-label"] === provider)
        .flatMap(nodes).find(node => node.type === "button" && text(node) === "Disconnect");
      disconnect().props.onClick(); await flush();
      assert.equal(hub.confirmations.length, 0);
      if (provider === "GitHub") assert.deepEqual(hub.calls.filter(call => call.revoked).map(call => call.revoked), ["account_github"]);
      else {
        const approved = hub.calls.find(call => call.payload?.message_id);
        assert.equal(approved.path, `/api/ventures/${domain.ventureId}/chat/confirm`);
        assert.equal(approved.payload.message_id, "approval_saved");
        assert.deepEqual({ ...approved.payload.payload.accounts }, { saved_account: "saved_connection" });
        assert.equal(approved.headers["Idempotency-Key"], approved.payload.idempotency_key);
      }
      assert.match(renderToStaticMarkup(hub.render()), new RegExp(`You disconnected ${provider}`));
      const stale = await fixture(domain), retained = nodes(stale.render()).find(node => node.props?.["data-usage-control"] === "connections_disconnect");
      stale.retire(); retained.props.onClick(); await flush();
      assert.deepEqual(stale.calls, []);
    }
    const failed = await fixture(domain);
    failed.allow(true); failed.request(async () => ({ ok: false, data: null }));
    nodes(failed.render()).find(node => node.props?.["data-usage-control"] === "connections_disconnect").props.onClick(); await flush();
    assert.match(renderToStaticMarkup(failed.render()), /could not be reviewed/);
    assert.deepEqual(failed.confirmations, []);
  }
});

test("expired accounts start authorization instead of syncing stale credentials across unrelated ventures", async () => {
  for (const domain of DOMAINS) {
    const hub = await fixture(domain);
    Object.assign(hub.cards.gmail, { needs_reconnect: true, ready: false, action: "sync" });
    const tile = nodes(hub.render()).find(node => node.type === "article" && node.props["aria-label"] === "Gmail");
    assert.match(renderToStaticMarkup(tile), /Needs reconnect/);
    nodes(tile).find(node => node.type === "button" && text(node) === "Reconnect").props.onClick(); await flush();
    assert.equal(hub.calls.at(-1).provider, "gmail");
    assert.deepEqual(hub.links, ["https://accounts.example/authorize"]);
    assert.match(renderToStaticMarkup(hub.render()), /Connecting/);
  }
});
