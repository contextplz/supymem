const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
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

function view(changes = {}) {
  return { exa_configured: true, has_audience: true, mac_connected: false, searching: false,
    queued_at: null, notice: null, needs_human: null, last_search: null, people: [], fallback: [],
    today: { new: 0, target: 10 }, searches_today: 0, search_limit: 3, next_search_at: null, ...changes };
}

function harness(initial, ventureId = "venture-a", local = initial?.local_mode === true) {
  let cursor = 0, response = initial, nextTimer = 0, paused = null;
  const slots = [], pending = [], requests = [], prefills = [], timers = new Map();
  const same = (left, right) => left && right && left.length === right.length && left.every((value, index) => Object.is(value, right[index]));
  const react = {
    useState(initialValue) {
      const index = cursor++;
      if (!(index in slots)) slots[index] = initialValue;
      return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }];
    },
    useRef(initialValue) {
      const index = cursor++;
      return slots[index] ??= { current: initialValue };
    },
    useCallback(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) slots[index] = { deps, callback };
      return slots[index].callback;
    },
    useEffect(callback, deps) {
      const index = cursor++;
      if (!same(slots[index]?.deps, deps)) {
        slots[index]?.cleanup?.();
        slots[index] = { deps };
        pending.push(() => { slots[index].cleanup = callback(); });
      }
    },
  };
  const jsx = (type, props) => typeof type === "function" ? type(props) : { type, props };
  const exports = {};
  const output = ts.transpileModule(readFileSync(join(__dirname, "page.tsx"), "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX, target: ts.ScriptTarget.ES2022,
  } }).outputText;
  vm.runInNewContext(output, { exports, console, Date, window: {
    addEventListener() {}, removeEventListener() {},
    setInterval(callback, delay) { const id = ++nextTimer; timers.set(id, { callback, delay }); return id; },
    clearInterval(id) { timers.delete(id); },
  }, require(id) {
    if (id === "react") return react;
    if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
    if (id === "next/navigation") return { useParams: () => ({ ventureId }) };
    if (id === "@/components/layout/LocalModeBoundary") return { useLocalMode: () => local };
    if (id === "@/lib/client") return {
      ensureSession: async () => {}, verifiedSessionEpoch: () => 1,
      api: async (path, init = {}) => { requests.push({ path, method: init.method || "GET" }); if (paused) await paused;
        return response?.ok === false ? response : { ok: true, data: response }; },
    };
    if (id === "@/lib/chatPrefill") return { prefillChat: value => prefills.push(value) };
    if (id === "@/lib/gtmJourneyApi") return { safeEvidenceUrl: value => value?.startsWith("https://") ? value : undefined };
    if (id === "@/lib/serverReason") return { serverReason: () => "Could not load" };
    if (id.endsWith(".css")) return { default: {} };
    return new Proxy({}, { get: (_target, name) => String(name) });
  } });
  function render() { cursor = 0; const tree = exports.default(); pending.splice(0).forEach(effect => effect()); return tree; }
  async function settle() { await new Promise(resolve => setImmediate(resolve)); return render(); }
  return { requests, prefills, timers, render, async loaded() { render(); return settle(); },
    async poll(next) { response = next; for (const timer of timers.values()) await timer.callback(); return settle(); },
    async click(button, next) { response = next; await button.props.onClick(); return settle(); },
    pause() { let resume; paused = new Promise(resolve => { resume = resolve; }); return () => { paused = null; resume(); }; },
    close() { slots.forEach(slot => slot?.cleanup?.()); },
  };
}

test('local public-web discovery needs no key or Mac and runs only when you choose it', async () => {
  for (const [ventureId, audience] of [['ven_warehousing', 'Warehouse supervisors'], ['ven_running', 'Weekend runners']]) {
    const initial = view({local_mode: true, exa_configured: false});
    const app = harness(initial, ventureId);
    const tree = await app.loaded();
    assert.match(text(tree), /Public-web profile discovery and manual drafts only/);
    assert.match(text(tree), /no automated messaging/);
    assert.match(text(tree), /Built-in search works without a key/);
    assert.doesNotMatch(text(tree), /on your Mac|read in your own browser|connect.*LinkedIn/i);
    assert.deepEqual(app.requests, [{path: `/api/ventures/${ventureId}/linkedin`, method: 'GET'}]);
    assert.equal(app.timers.size, 0);
    await app.click(findMore(tree), view({...initial, last_search: {state: 'done', message: `Your public profiles for ${audience} are saved.`}}));
    assert.deepEqual(app.requests.at(-1), {path: `/api/ventures/${ventureId}/linkedin/find`, method: 'POST'});
    app.close();
  }
});

test('repeated local search clicks submit only one request while your search is pending', async () => {
  for (const ventureId of ['ven_logistics', 'ven_cycling']) {
    const app = harness(view({local_mode: true, exa_configured: false}), ventureId);
    const tree = await app.loaded(), resume = app.pause();
    findMore(tree).props.onClick(); findMore(tree).props.onClick();
    assert.deepEqual(app.requests, [{path: `/api/ventures/${ventureId}/linkedin`, method: 'GET'},
      {path: `/api/ventures/${ventureId}/linkedin/find`, method: 'POST'}]);
    resume(); await new Promise(resolve => setImmediate(resolve));
    app.close();
  }
});

test('07: your saved LinkedIn search has a read-only retry after an initial failure', async () => {
  for (const audience of ['Commercial electricians', 'Weekend gardeners']) {
    const app = harness({ok: false}, `ven_${audience.split(' ')[0].toLowerCase()}`, true);
    assert.match(text(app.render()), /Public-web profile discovery/);
    const tree = await app.loaded(), retry = nodes(tree).find(node => node.props?.children === 'Try again');
    assert.match(text(tree), /Public-web profile discovery/);
    assert.doesNotMatch(text(tree), /on your Mac|read in your own browser/);
    assert.equal(retry.props.disabled, false);
    assert.equal(nodes(tree).find(node => node.props?.children?.includes?.('Find more people')).props.disabled, true);
    await app.click(retry, view({has_audience: true}));
    assert.equal(app.requests.length, 2); assert.ok(app.requests.every(request => request.method === 'GET'));
    assert.equal(nodes(app.render()).some(node => node.props?.children === 'Try again'), false); app.close();
  }
});

function findMore(tree) {
  return nodes(tree).find(node => node.type === "button" && text(node) === "Find more people");
}

test("empty venture with an audience loads the server auto-request and shows background searching", async () => {
  const app = harness(view({ searching: true, queued_at: "2026-10-23T12:00:00Z", last_search: { state: "queued" } }));
  assert.ok(findMore(app.render()));
  const tree = await app.loaded();
  assert.deepEqual(app.requests, [{ path: "/api/ventures/venture-a/linkedin", method: "GET" }]);
  assert.match(text(tree), /Searching in the background\. Queued at/);
  assert.match(text(tree), /People who fit your customer, found in the background from public profiles\./);
  assert.doesNotMatch(text(tree), /your Mac|No people yet/);
  assert.equal(findMore(tree).props.disabled, true);
  assert.equal([...app.timers.values()][0].delay, 3000);
  app.close();
});

test("polling keeps queued and running searches fresh and stops when results arrive", async () => {
  const person = { id: "person-a", name: "Ada Lovelace", headline: "Gym Owner", company: "Iron Gym",
    url: "https://www.linkedin.com/in/ada-lovelace", why: "Fits the audience", messages: [
      { playbook: "one", title: "Introduction", connection_note: "Connection note for Ada", linkedin_dm: "DM for Ada" },
    ] };
  const app = harness(view({ searching: true, last_search: { state: "queued" } }));
  await app.loaded();
  const running = await app.poll(view({ searching: true, last_search: { state: "running" } }));
  assert.match(text(running), /Searching in the background/);
  assert.equal(app.timers.size, 1);
  const tree = await app.poll(view({ today: { new: 1, target: 10 }, people: [person], last_search: { state: "done" } }));
  assert.equal(app.requests.length, 3);
  assert.equal(app.timers.size, 0);
  assert.doesNotMatch(text(tree), /Searching in the background/);
  assert.match(text(tree), /Ada Lovelace.*Gym Owner/);
  const profile = nodes(tree).find(node => node.type === "a" && node.props.href === person.url);
  assert.equal(text(profile), person.url);
  assert.equal(profile.props.rel, "noopener noreferrer");
  const copies = nodes(tree).filter(node => node.type === "CopyDraft");
  assert.deepEqual(copies.map(node => [node.props.buttonLabel, node.props.text]), [["Copy note", person.messages[0].connection_note], ["Copy DM", person.messages[0].linkedin_dm]]);
  assert.equal(findMore(tree).props.disabled, false);
  const searching = await app.click(findMore(tree), view({ searching: true }));
  assert.deepEqual(app.requests.at(-1), { path: "/api/ventures/venture-a/linkedin/find", method: "POST" });
  assert.match(text(searching), /Searching in the background/);
  app.close();
});

test("cap reached replaces Find more with the next search time", async () => {
  const app = harness(view({ searches_today: 3, next_search_at: "2026-10-24T00:00:00Z" }));
  const tree = await app.loaded();
  assert.equal(findMore(tree), undefined);
  assert.match(text(tree), /Today's 3 searches are used\. Next search can run at/);
  assert.ok(text(tree).includes(new Date("2026-10-24T00:00:00Z").toLocaleString()));
  assert.equal(app.timers.size, 0);
  app.close();
});

test("B2B and consumer cards show one name, role and location, one clean summary, capped progress and manual controls", async () => {
  for (const domain of [
    { ventureId: "stock-ledger", name: "Avery Chen", headline: "Dental Practice Manager", location: "Bristol", count: 22,
      why: "Found for your search: dental practice managers in Bristol. She manages supplies and purchasing for a dental practice." },
    { ventureId: "trail-circle", name: "Jordan Rivera", headline: "Trail Runner", location: "Denver", count: 3,
      why: "Found for your search: trail runners in Denver. They organize weekend runs and share routes with fellow runners." },
  ]) {
    const person = { id: "person-a", url: `https://www.linkedin.com/in/${domain.name.toLowerCase().replaceAll(" ", "-")}`,
      ...domain, company: "", snippet: `# ${domain.name}\n## About\n**Raw scraped text** [Link](https://example.test)`, messages: [] };
    const app = harness(view({ today: { new: domain.count, target: 10 }, people: [person] }), domain.ventureId);
    const tree = await app.loaded();
    const card = nodes(tree).find(node => node.type === "li");
    assert.deepEqual(nodes(card).filter(node => node.type === "strong").map(text), [domain.name]);
    assert.equal(text(card).split(domain.name).length - 1, 1);
    assert.deepEqual(nodes(card).filter(node => node.type === "p").map(text), [`${domain.headline} · ${domain.location}`, domain.why]);
    assert.ok(domain.why.length <= 220);
    assert.doesNotMatch(text(card), /#|\*|\[|Raw scraped text|Exa profile result/);
    assert.equal(nodes(card).filter(node => node.type === "blockquote").length, 0);
    assert.ok(text(tree).includes(`${Math.min(domain.count, 10)} of 10 new people today`));
    assert.doesNotMatch(text(tree), /22 of 10/);
    const profile = nodes(card).find(node => node.type === "a");
    assert.equal(profile.props.href, person.url);
    assert.equal(profile.props.rel, "noopener noreferrer");
    for (const label of ["Mark sent", "Replied"]) {
      const button = nodes(card).find(node => node.type === "button" && text(node) === label);
      assert.equal(button.props["aria-pressed"], false);
      await app.click(button, view({ people: [person] }));
      assert.deepEqual(app.requests.at(-1), { path: `/api/ventures/${domain.ventureId}/linkedin/people/person-a`, method: "PATCH" });
    }
    app.close();
  }
});

test("a slow refresh is not superseded by another polling tick", async () => {
  const app = harness(view({ searching: true }));
  await app.loaded();
  const resume = app.pause();
  const tick = [...app.timers.values()][0].callback;
  const pending = tick();
  await new Promise(resolve => setImmediate(resolve));
  await tick();
  assert.equal(app.requests.length, 2);
  resume();
  await pending;
  assert.match(text(app.render()), /Searching in the background/);
  app.close();
});

test("no audience offers Charlie's existing chat prefill without sending a search", async () => {
  const app = harness(view({ has_audience: false }));
  const tree = await app.loaded();
  assert.match(text(tree), /Set your audience so Charlie knows whom to find\./);
  nodes(tree).find(node => node.type === "button" && text(node) === "Set audience with Charlie").props.onClick();
  assert.deepEqual(app.prefills, ["Charlie, help me set the audience for this venture so we can find LinkedIn people."]);
  assert.equal(app.requests.length, 1);
  assert.equal(findMore(tree).props.disabled, true);
  assert.equal(app.timers.size, 0);
  app.close();
});

test("unconfigured Exa retains Mac copy and displays Mac searching without a recent check-in", async () => {
  const app = harness(view({ exa_configured: false, searching: true, notice: "Open the Supymem Mac app to find LinkedIn people." }));
  const tree = await app.loaded();
  assert.match(text(tree), /Google results read in your own browser on your Mac/);
  assert.match(text(tree), /Searching on your Mac\./);
  assert.equal(app.timers.size, 1);
  app.close();
});
