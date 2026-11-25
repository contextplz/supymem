const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { resolve } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

function nodes(tree) {
  if (!tree) return [];
  if (Array.isArray(tree)) return tree.flatMap(nodes);
  return typeof tree === "object" ? [tree, ...nodes(tree.props?.children)] : [];
}

function text(tree) {
  if (tree == null || typeof tree === "boolean") return "";
  if (Array.isArray(tree)) return tree.map(text).join("");
  return typeof tree === "object" ? text(tree.props?.children) : String(tree);
}

function load(file, modules, globals = {}) {
  const exports = {};
  const output = ts.transpileModule(readFileSync(file, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, URL, Date, console, ...globals,
    require: name => { assert.ok(name in modules, name); return modules[name]; } });
  return exports;
}

function harness(venture, initial) {
  let cursor = 0, epoch = 1, response = initial, deferred;
  const slots = [], effects = [], events = [], eventTypes = [], requests = [], requestOptions = [], downloads = [], imports = [], listeners = new Map();
  const same = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const react = {
    useState(initialValue) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initialValue;
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef(initialValue) { const index = cursor++; return slots[index] ??= { current: initialValue }; },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = { deps, callback };
      return slots[index].callback;
    },
    useEffect(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) {
        slots[index]?.cleanup?.(); slots[index] = { deps };
        effects.push(() => { slots[index].cleanup = callback(); });
      }
    },
  };
  const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
  const window = {
    addEventListener(name, listener) { if (!listeners.has(name)) listeners.set(name, new Set()); listeners.get(name).add(listener); },
    removeEventListener(name, listener) { listeners.get(name)?.delete(listener); },
    dispatchEvent(event) { events.push(event.detail); eventTypes.push(event.type); for (const listener of listeners.get(event.type) || []) listener(event); },
    confirm: () => true,
  };
  const evidenceUrls = { safeEvidenceUrl(value) { try { const url = new URL(value); return ["https:", "http:"].includes(url.protocol) && !url.username && !url.password ? url.href : undefined; } catch {} } };
  const page = load(resolve(__dirname, "Leads.tsx"), {
    react, "react/jsx-runtime": { jsx, jsxs: jsx }, "next/navigation": { useParams: () => ({ ventureId: venture }) },
    "@/components/layout/AppShell": { AppShell: props => jsx("shell", props) },
    "@/components/layout/LocalModeBoundary": { useFeatureVisibility: () => () => true, useLocalMode: () => true },
    "@/components/primitives/ErrorNotice": { ErrorNotice: props => jsx("error", props) },
    "@/components/chat/ChatLink": { ChatAnchor: props => jsx("a", props) },
    "@/lib/client": { ensureSession: async () => {}, verifiedSessionEpoch: () => epoch, verifiedSessionSubject: () => "founder",
      api: async (path, options) => { requests.push(path); requestOptions.push(options); if (deferred) return await deferred; return { ok: true, status: 200, data: response }; },
      downloadFile: async (...args) => { downloads.push(args); return true; } },
    "@/lib/gtmJourneyApi": evidenceUrls,
    "@/lib/reachableLeads": load(resolve(__dirname, "../../lib/reachableLeads.ts"), { "./gtmJourneyApi.ts": evidenceUrls }),
    "@/lib/serverReason": { serverReason: (_, fallback) => fallback },
    "./ImportContacts": { ImportContacts: props => jsx("contact-import", props) },
    "./OutreachDraft": { CopyDraft: props => jsx("message-draft", props) },
    "@/lib/founderAssetsApi": { founderAssetsClient: { importContacts: async (...args) => { imports.push(args); return { imported: 1, skipped: 0 }; } } },
    "@/components/gtm/GtmJourney.module.css": { default: new Proxy({}, { get: (_, key) => key }) },
  }, { window, CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options.detail; } } });
  return { page, events, eventTypes, requests, requestOptions, downloads, imports,
    render() { cursor = 0; const tree = page.LeadsList({ ventureId: venture }); effects.splice(0).forEach(effect => effect()); return tree; },
    async flush() { for (let index = 0; index < 8; index++) await Promise.resolve(); },
    respond(data) { response = data; },
    pause() { let finish; deferred = new Promise(resolve => { finish = resolve; }); return (data, status = 200) => { deferred = null; finish({ ok: status < 400, status, data }); }; },
    changeSession() { epoch++; window.dispatchEvent({ type: "supymem-session-changed" }); },
    actionDone() { window.dispatchEvent({ type: "supymem-chat-action-done", detail: { ventureId: venture } }); },
  };
}

test('empty Leads lets you ask Charlie for help without starting a search', async () => {
  for (const venture of ['ven_logistics', 'ven_outdoors']) {
    const app = harness(venture, {venture_id: venture, total: 0, items: []});
    app.render(); await app.flush();
    assert.deepEqual(app.events, []);
    const action = nodes(app.render()).find(node => node.type === 'button' && text(node) === 'Find customers with Charlie');
    assert.ok(action);
    action.props.onClick();
    assert.deepEqual(app.eventTypes, ['supymem:ask']);
    assert.equal(app.events[0].action, 'gtm.find');
    assert.equal(app.events[0].payload.prepare, true);
    assert.equal(app.events[0].payload.run, false);
    assert.deepEqual(app.requests, [`/api/ventures/${venture}/leads/contacts`]);
  }
});

test('your leads refresh is busy and preserves your saved rows across domains', async () => {
  for (const [venture, name] of [['ven_freight', 'Freight planner'], ['ven_garden', 'Garden club']]) {
    const data = {venture_id: venture, workspace_id: 'workspace', total: 1, items: [{id: 'lead', name, source: 'web', status: 'verified', created_at: '2026-11-07', can_draft: true, contact_id: 'contact', website: 'https://example.org', saved: {}}]};
    const view = harness(venture, data);
    view.render(); await view.flush();
    const finish = view.pause();
    nodes(view.render()).find(node => node.props.children === 'Refresh leads').props.onClick();
    let tree = view.render();
    assert.equal(nodes(tree).find(node => node.props.children === 'Refresh leads').props.disabled, true);
    assert.match(text(tree), /Refreshing your leads/);
    assert.match(text(tree), new RegExp(name));
    finish(data); await view.flush(); tree = view.render();
    assert.equal(nodes(tree).find(node => node.props.children === 'Refresh leads').props.disabled, false);
  }
});

test('your filtered leads give you one action to clear every filter across domains', async () => {
  for (const [venture, name] of [['ven_freight', 'Freight planner'], ['ven_garden', 'Garden club']]) {
    const view = harness(venture, {venture_id: venture, workspace_id: 'workspace', total: 1, items: [{id: 'lead', name, source: 'web', status: 'verified', created_at: '2026-11-07', saved: {}}]});
    view.render(); await view.flush();
    let tree = view.render();
    nodes(tree).find(node => node.type === 'input' && node.props.type === 'search').props.onChange({target: {value: 'not a match'}});
    const filters = nodes(tree).filter(node => node.type === 'select');
    filters[0].props.onChange({target: {value: 'imported'}}); filters[1].props.onChange({target: {value: 'sent'}});
    nodes(view.render()).find(node => node.props.children === 'Clear filters').props.onClick();
    tree = view.render();
    assert.match(text(tree), new RegExp(name));
    assert.ok(nodes(tree).filter(node => node.type === 'select' || node.type === 'input').every(node => node.props.value === ''));
  }
});

test('your repeated lead actions include your current lead name across domains', async () => {
  for (const [venture, name] of [['ven_freight', 'Freight planner'], ['ven_garden', 'Garden club']]) {
    const view = harness(venture, {venture_id: venture, workspace_id: 'workspace', total: 1, items: [{id: 'lead', name, source: 'web', status: 'sent', created_at: '2026-11-07', email: 'hello@example.org', can_draft: true, contact_id: 'contact', website: 'https://example.org', saved: {}}]});
    view.render(); await view.flush();
    const tree = view.render();
    for (const label of ['Company website', 'Draft email', 'Mark replied', 'Mark booked']) {
      assert.ok(nodes(tree).find(node => node.props.children === label).props['aria-label'].includes(name));
    }
  }
});

test('one click drafts only your selected lead and late results cannot cross your session', async () => {
  for (const [ventureId, name] of [['ven_delivery', 'Dispatch supervisor'], ['ven_knitting', 'Home knitter']]) {
    const lead = {id: 'selected', contact_id: 'selected', name, source: 'imported', status: 'suggested',
      created_at: '2026-11-14', can_draft: true, saved: {}};
    const data = {venture_id: ventureId, workspace_id: 'workspace', total: 1, items: [lead]};
    const app = harness(ventureId, data);
    app.render(); await app.flush();
    const finish = app.pause();
    const draft = nodes(app.render()).find(node => node.type === 'button' && text(node) === 'Draft email');
    draft.props.onClick(); draft.props.onClick();
    await app.flush();
    assert.deepEqual(app.requests, [`/api/ventures/${ventureId}/leads/contacts`, `/api/ventures/${ventureId}/leads/contacts/selected/draft`]);
    assert.equal(app.events.length, 0);
    app.respond({...data, total: 0, items: []}); app.changeSession();
    finish({...data, receipt: {body_md: 'Your email draft is saved. Nothing was sent.'}});
    await app.flush();
    assert.ok(!text(app.render()).includes(name));
    assert.doesNotMatch(text(app.render()), /Your email draft is saved/);
  }
});

test('Leads gives uncontacted companies a contact page or lookup and a copyable draft in two domains', async () => {
  for (const [venture, domain, name, kind] of [['ven_freight', 'freight.example', 'Freight planner', 'website'], ['ven_garden', 'garden.example', 'Garden club', 'contact_form']]) {
    const website = `https://${domain}/`;
    const lead = { id: 'lead', contact_id: 'lead', name, source: 'web', website, source_url: website,
      status: 'suggested', created_at: '2026-11-07', can_draft: false };
    const contactPage = `${website}contact`;
    const routed = { ...lead, id: 'routed', contact_id: null, name: `${name} team`, routes: [{ kind, company_contact: kind === 'website', url: contactPage }] };
    const data = { venture_id: venture, total: 2, items: [lead, routed] };
    const app = harness(venture, data);
    const card = index => nodes(app.render()).filter(node => node.type === 'li')[index];
    const button = (index, label) => nodes(card(index)).find(node => node.type === 'button' && text(node) === label);
    const page = index => nodes(card(index)).find(node => node.type === 'a' && text(node) === 'Open contact page');
    app.render(); await app.flush();
    assert.equal(page(1)?.props.href, contactPage);
    assert.ok(button(1, 'Find a contact'));
    assert.equal(button(1, 'Draft a message'), undefined);
    assert.equal(button(0, 'Draft a message'), undefined);
    assert.equal(button(0, 'Mark replied'), undefined);
    assert.equal(button(0, 'Mark booked'), undefined);
    assert.equal(page(0), undefined);
    const finish = app.pause();
    button(0, 'Find a contact').props.onClick();
    finish({ detail: { message: 'In Settings > Capabilities, switch on Research.' } }, 409);
    await app.flush();
    assert.match(nodes(app.render()).find(node => node.type === 'error').props.error, /Settings > Capabilities/);
    const found = { ...lead, contact_page: contactPage };
    app.respond({ ...data, items: [found, routed] });
    button(0, 'Find a contact').props.onClick(); await app.flush();
    assert.equal(app.requests.at(-1), `/api/ventures/${venture}/leads/contacts/lead/find-contact`);
    assert.equal(app.requestOptions.at(-1).method, 'POST');
    assert.equal(page(0)?.props.href, contactPage);
    assert.equal(button(0, 'Find a contact'), undefined);
    const message_draft = `I found you on ${domain}. Reply no thanks to opt out.`;
    app.respond({ ...data, items: [{ ...found, message_draft }, routed] });
    button(0, 'Draft a message').props.onClick(); await app.flush();
    assert.equal(app.requests.at(-1), `/api/ventures/${venture}/leads/contacts/lead/draft`);
    assert.equal(app.requestOptions.at(-1).method, 'POST');
    assert.equal(nodes(card(0)).find(node => node.type === 'message-draft')?.props.text, message_draft);
    assert.deepEqual(app.events, []);

    for (const status of ['verified', 'drafted', 'approved', 'queued', 'delivery unconfirmed', 'opted out', 'bounced', 'sent', 'reply received', 'positive reply', 'meeting booked']) {
      app.respond({ ...data, items: [{ ...found, status }, routed] });
      nodes(app.render()).find(node => node.type === 'button' && text(node) === 'Refresh leads').props.onClick(); await app.flush();
      const contacted = ['sent', 'reply received', 'positive reply', 'meeting booked'].includes(status);
      assert.equal(Boolean(button(0, 'Mark replied')), contacted, status);
      assert.equal(Boolean(button(0, 'Mark booked')), contacted, status);
      if (['opted out', 'bounced'].includes(status)) {
        assert.equal(page(0), undefined);
        assert.equal(button(0, 'Draft a message'), undefined);
      }
      if (contacted) {
        assert.equal(button(0, 'Mark booked').props.disabled, status === 'meeting booked');
        assert.equal(button(0, 'Mark replied').props.disabled, status !== 'sent');
      }
    }
  }
});

test("Leads shows address-less search routes and reuses your venture-scoped contact import in two domains", async () => {
  for (const [venture, audience] of [["ven_dispatch", "Freight coordinators"], ["ven_home", "Home gardeners"]]) {
    const routes = [{ kind: "website", url: "https://public.example/person" },
      { kind: "forum", url: "https://community.example/post" },
      { kind: "linkedin", url: "https://www.linkedin.com/in/public-person" },
      { kind: "contact_form", url: "https://public.example/contact" }];
    const items = routes.map((route, index) => ({ id: `person_${index}`, contact_id: null, name: `${audience} ${index}`,
      source: "web", source_url: `https://public.example/people/${index}`, routes: [route], status: "suggested",
      email: null, can_draft: false, created_at: "2026-11-01", saved: {} }));
    const app = harness(venture, { venture_id: venture, workspace_id: "workspace", total: 4, items });
    app.render(); await app.flush();
    let tree = app.render();
    assert.ok(text(tree).includes("4 of 4 leads"));
    for (const item of items) {
      assert.ok(text(tree).includes(item.name));
      assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === item.source_url));
      assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === item.routes[0].url));
    }
    assert.ok(!nodes(tree).some(node => node.type === "button" && /Draft email|Mark replied|Mark booked/.test(text(node))));
    const importer = nodes(tree).find(node => node.type === "contact-import");
    assert.ok(importer, "Reuse the existing contact importer on your Leads page");
    const preview = { preview_id: "preview" };
    await importer.props.save(preview, true, "consent", "You asked to stay in touch");
    assert.equal(app.imports.length, 1);
    assert.deepEqual(JSON.parse(JSON.stringify(app.imports[0])), [{ actorId: "founder", workspaceId: "workspace", ventureId: venture }, preview, true, "consent", "You asked to stay in touch"]);
    app.respond({ venture_id: venture, workspace_id: "workspace", total: 0, items: [] }); app.actionDone(); await app.flush();
    tree = app.render();
    assert.ok(nodes(tree).some(node => node.type === "contact-import"), "You can add someone before finding your first lead");
    app.changeSession();
    assert.ok(!nodes(app.render()).some(node => node.type === "contact-import"));
  }
});

test('35: your lead sources and outreach remain readable without support diagnostics', async () => {
  for (const audience of ['Procurement teams', 'Amateur musicians']) {
    const lead = {id: 'lead', name: audience, status: 'drafted', source: 'web', source_url: 'https://source.example/person',
      created_at: '2026-10-31', why: `Recorded interest from ${audience}`, saved: {evidence: {internal_debug: 'private diagnostic'}},
      outreach: [{subject: 'A question', body: 'Your email address is listed publicly on source.example. Opt out by replying no. 12 Lane Road'}]};
    const app = harness('ven_domain', {venture_id: 'ven_domain', total: 1, items: [lead]}); app.render(); await app.flush();
    const tree = app.render();
    assert.match(text(tree), /Why this lead matches: Recorded interest/);
    assert.doesNotMatch(text(tree), /Technical details for support|All saved information|internal_debug|private diagnostic/);
    assert.match(text(tree), /Your email address is listed publicly/); assert.match(text(tree), /Opt out by replying no/);
    assert.ok(nodes(tree).some(node => node.type === 'a' && node.props.href === lead.source_url));
  }
});

test("venture Leads retains all sources, filters, safe contact actions, navigation and session isolation across unrelated domains", async () => {
  const root = resolve(__dirname, "../../");
  const routes = load(resolve(root, "lib/chatRouteAction.ts"), {
    "./chatVisibility.ts": require("../../lib/chatVisibility.ts"),
    "./localMode.ts": require("../../lib/localMode.ts"), "./operationsScope.ts": require("../../lib/operationsScope.ts"),
  });
  const guide = load(resolve(root, "lib/capabilityGuide.ts"), {});
  const usage = load(resolve(root, "lib/usageFeature.ts"), {});
  const catalog = readFileSync(resolve(root, "../../../services/company_os/app/analytics/feature_catalog.py"), "utf8");
  const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
  let local = false;
  const nav = load(resolve(root, "components/layout/SideNav.tsx"), {
    react: { useState: initial => [initial, () => {}] }, "react/jsx-runtime": { jsx, jsxs: jsx },
    "@/components/chat/ChatLink": { default: "a" }, "@/components/chat/NewVentureLink": { NewVentureLink: "a" },
    "@/lib/cx": { cx: (...values) => values.filter(Boolean).join(" ") }, "@/lib/betaRoutes": { isBetaHiddenPath: () => false },
    "./NavIcons": { NAV_ICONS: new Proxy({}, { get: () => () => "icon" }) },
    "./LocalModeBoundary": { useLocalMode: () => local, useFeatureVisibility: () => () => true },
    "@/lib/localMode": load(resolve(root, "lib/localMode.ts"), {}),
    "./SideNav.module.css": { default: {} }, "./SuggestionsBadge": { SuggestionsBadge: () => null },
    "@/components/guide/GuideNavLink": { GuideNavLink: () => null },
    "@/lib/operationsScope": { ventureGrowthHrefs: id => ({ gtm: `/gtm?venture=${id}`, content: `/content?venture=${id}`,
      community: `/community?venture=${id}`, connections: `/connections?venture=${id}`, execution: `/v/${id}/execution` }),
      companyOverviewHref: () => "/", operationsHref: path => path },
  });
  for (const { venture, name, role, company } of [
    { venture: "ven_freight", name: "Mara Chen", role: "Freight coordinator", company: "Freight team" },
    { venture: "ven_garden", name: "Luis Rivera", role: "Home gardener", company: "Garden club" },
  ]) {
    const lead = { id: "lead_public", name, role, company, source: "web", source_url: "https://public.example/people",
      website: "https://public.example", email: "person@public.example", can_draft: true, company_inbox: false,
      linkedin_url: "https://linkedin.com/in/public-person", x_handle: "public_person", phone: "+1 202 555 0142",
      status: "sent", created_at: "2026-10-20T10:00:00Z", why: "Fits your chosen audience", notes: "You met at a workshop",
      draft: { subject: "One question", body: "Your email address is listed publicly on public.example. Reply no thanks to opt out. 42 Lake Road" }, saved: { country: "US" } };
    const initial = { venture_id: venture, workspace_id: "owned_workspace", total: 3, items: [lead,
      { ...lead, id: "lead_library", source: "library", status: "drafted", company_inbox: true },
      { ...lead, id: "lead_import", source: "imported", status: "opted out", can_draft: false }] };
    const app = harness(venture, initial);
    app.render(); await app.flush();
    let tree = app.render();
    assert.deepEqual(app.requests, [`/api/ventures/${venture}/leads/contacts`]);
    for (const phrase of [name, role, company, "3 of 3 leads", "Lead library", "Found on the web public.example", "Imported", "Opted out", "Email draft", "Your email address is listed publicly", "Notes: You met at a workshop"]) assert.ok(text(tree).includes(phrase), phrase);
    assert.equal(nodes(tree).filter(node => node.type === "li").length, 3);
    assert.equal(nodes(tree).filter(node => node.type === "button" && text(node) === "Draft email").length, 2);
    assert.deepEqual(Array.from(app.page.reachLinks(lead), link => link.label), ["Email", "LinkedIn", "X", "Phone"]);
    assert.equal(app.page.reachLinks({ ...lead, can_draft: false, linkedin_url: "javascript:alert(1)", x_handle: "bad/../path", phone: "javascript:bad" }).length, 0);
    assert.equal(app.page.reachLinks({ ...lead, linkedin_url: "https://user:password@public.example" }).some(link => link.label === "LinkedIn"), false);
    for (const [label, suffix, method] of [["Draft email", "draft", "POST"], ["Mark replied", "history", "PATCH"], ["Mark booked", "history", "PATCH"]]) {
      nodes(app.render()).find(node => node.type === "button" && text(node) === label).props.onClick();
      await app.flush();
      assert.equal(app.requests.at(-1), `/api/ventures/${venture}/leads/contacts/${lead.id}/${suffix}`);
      assert.equal(app.requestOptions.at(-1).method, method);
    }
    assert.deepEqual(app.events, []);
    nodes(app.render()).find(node => node.type === "button" && text(node) === "Remove from leads").props.onClick();
    assert.equal(app.events.length, 1);
    assert.equal(app.eventTypes[0], "supymem:ask");
    assert.equal(app.events[0].action, "gtm.eligibility");
    assert.deepEqual(JSON.parse(JSON.stringify(app.events[0].payload)), { contact_id: lead.id, skip: true, remove: true });
    nodes(app.render()).find(node => node.type === "button" && text(node) === "Export leads CSV").props.onClick();
    await app.flush();
    assert.deepEqual(app.downloads, [[`/api/ventures/${venture}/leads/contacts/export`, "leads.csv"]]);
    for (const action of ["outreach.draft", "chat.reply", "gtm.eligibility", "gtm.status"]) assert.ok(catalog.includes(`"${action}"`), action);
    nodes(tree).find(node => node.type === "input").props.onChange({ target: { value: "workshop" } });
    tree = app.render();
    nodes(tree).filter(node => node.type === "select")[0].props.onChange({ target: { value: "library" } });
    tree = app.render(); assert.ok(text(tree).includes("1 of 3 leads"));
    nodes(tree).filter(node => node.type === "select")[1].props.onChange({ target: { value: "sent" } });
    tree = app.render(); assert.ok(text(tree).includes("No leads match your filters."));
    assert.equal(app.page.filterLeads(initial.items, "nobody matches", "", "").length, 0);
    assert.equal(app.page.filterLeads(initial.items, name, "", "").length, 3);
    for (local of [true, false]) {
      const links = nodes(nav.SideNav({ pathname: `/v/${venture}/leads`, ventureId: venture })).filter(node => node.type === "a");
      const leadsLink = links.find(link => link.props["aria-label"] === "Leads");
      assert.equal(leadsLink.props.href, `/v/${venture}/leads`);
      assert.equal(leadsLink.props["aria-current"], "page");
      assert.equal(links[links.indexOf(leadsLink) + 1].props["aria-label"], "LinkedIn");
      assert.equal(links.some(link => link.props.href === "/settings/local/capabilities"), local);
    }
    assert.equal(routes.homeRouteAction(`/v/${venture}/leads`, venture).sidebar, "view");
    assert.equal(routes.homeRouteAction(`/v/${venture}/leads`, venture).href, `/v/${venture}/leads`);
    assert.equal(usage.usageFeature(`/v/${venture}/leads`), "gtm");
    assert.equal(guide.guideDestinationHref(guide.GUIDE_CAPABILITIES.find(card => card.id === "leads").destination, venture), `/v/${venture}/leads`);
    const finish = app.pause(); app.actionDone(); await app.flush();
    app.respond({ ...initial, items: [], total: 0 }); app.changeSession();
    assert.ok(!text(app.render()).includes(name), "session change immediately clears workspace PII");
    finish(initial); await app.flush();
    assert.ok(!text(app.render()).includes(name), "a late old-session response cannot restore workspace PII");
    nodes(app.render()).find(node => node.type === "button" && text(node) === "Refresh leads").props.onClick();
    await app.flush(); tree = app.render();
    assert.ok(text(tree).includes("You have no saved leads yet"));
    assert.ok(nodes(tree).some(node => node.type === "contact-import"));
    assert.ok(!nodes(tree).some(node => node.type === "button" && text(node) === "Find my first customers"));
  }
});

test("Leads card headings show matching contact and company names only once across domains", async () => {
  for (const [venture, name, company] of [["ven_logistics", "Harbor Freight", " harbor freight "], ["ven_outdoors", "Trail Club", "Trail Club"]]) {
    const items = [{ id: "organization", name, company, source: "web", status: "suggested", saved: {} },
      { id: "person", name: "Taylor", company: name, role: "Coordinator", source: "web", status: "verified", saved: {} }];
    const app = harness(venture, { venture_id: venture, total: items.length, items });
    app.render(); await app.flush();
    const headings = nodes(app.render()).filter(node => node.type === "summary" && nodes(node).some(child => child.type === "strong")).map(text);
    assert.deepEqual(headings, [name, `Taylor, Coordinator at ${name}`]);
    assert.ok(nodes(app.render()).some(node => node.props?.["aria-label"] === `Details for ${name}`));
  }
});

test("Leads details retain outreach truth and cap guidance without confirmation cards in two domains", async () => {
  for (const [venture, name, role] of [["ven_workshop", "Robin", "Repair coordinator"], ["ven_hiking", "Sam", "Weekend hiker"]]) {
    const statuses = ["suggested", "verified", "drafted", "approved", "queued", "sent", "reply received", "positive reply", "meeting booked", "delivery unconfirmed"];
    const items = statuses.map((status, index) => ({ id: `${venture}_${index}`, name, role, source: "web", status,
      created_at: "2026-10-27T12:00:00Z", can_draft: false, saved: {},
      source_url: 'https://public.example/about', why: `You matched ${role}.`, truth_basis: 'Recorded by you',
      interest_note: "You recorded interest. This is not a received reply or a booked meeting.",
      outreach: [{ doc_id: `${index}_first`, subject: `First email ${index}`, body: "Your email address is listed publicly on public.example. Reply no thanks to opt out. Your sending address.", truth_state: "sent" },
        { doc_id: `${index}_next`, subject: `Next email ${index}`, body: "Your saved follow-up", truth_state: status }] }));
    const action = { label: "Review 30 a day", action: "autopilot.send_cap", confirmation: { action: "autopilot.send_cap", resulting_state: { send_max: 30 } } };
    const app = harness(venture, { venture_id: venture, total: items.length, items,
      outreach_next_step: { reason: "cap_reached", body_md: "15 a day is recommended. Your emails stay saved.", actions: [action] } });
    app.render(); await app.flush();
    let tree = app.render();
    assert.ok(text(tree).includes("Your next step") && text(tree).includes("15 a day is recommended"));
    assert.ok(text(tree).includes("Open chat and ask me to review your daily send limit."));
    for (const label of ['Suggested lead', 'Address found in saved source', 'Email drafted', 'Approved to send', 'Scheduled to send', 'Sent', 'Reply received', 'Positive reply', 'Meeting booked', 'Delivery not confirmed']) {
      assert.ok(text(tree).includes(`Outreach status: ${label}`), label);
    }
    assert.match(text(tree), /Status: Scheduled to send/);
    assert.ok(text(tree).includes(`You matched ${role}.`) && text(tree).includes('Recorded by you'));
    assert.ok(nodes(tree).some(node => node.props?.href === 'https://public.example/about'));
    assert.equal(nodes(tree).filter(node => node.type === "h3").length, items.length * 2);
    assert.ok(text(tree).includes("not mailbox delivery verification") && text(tree).includes("not a received reply"));
    assert.ok(!nodes(tree).some(node => node.type === "button" && text(node) === action.label));
    assert.deepEqual(app.events, []);
    nodes(tree).filter(node => node.type === "select")[1].props.onChange({ target: { value: "positive reply" } });
    tree = app.render(); assert.ok(text(tree).includes("1 of 10 leads"));
    app.changeSession(); assert.ok(!text(app.render()).includes("Your next step"));
  }
});
