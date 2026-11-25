import * as chatRoutes from "./chatRouteAction.ts";
import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { checkedFounderStage, contactsImportRequested, FOUNDER_STAGES, safeSiteUrl, siteImportFromSearch } from "./founderAssetsApi.ts";
import { asServerStep } from "./nextStep.ts";
import { RUN_STATUS_REFRESH } from "./runStatusPresentation.ts";
import { plainText } from "./researchBrief.ts";
import { editableIdeaAnswer } from "./ideaAnswer.ts";
import * as gtmProgress from "./gtmProgress.ts";

const owner = { actorId: "actor_fixture", workspaceId: "workspace_fixture", ventureId: "venture_fixture" };
const record = { stage: "page_live", confidence: 0.9, evidence: [], confirmed_by_founder: false, updated_at: "2026-10-08T00:00:00Z", history: [] };
const preview = { preview_id: "preview_fixture", columns: ["Email"], mapping: { email: "Email" }, rows: [{ row_number: 1, name: "Synthetic contact", email: "qa@example.test", company: "Fixture", status: "contacted", email_check: "unknown", duplicate: false, errors: [] }], total_rows: 1, valid_rows: 1, duplicates: 0, invalid_rows: 0, expires_at: "2099-10-08T00:00:00Z" };
const flush = () => new Promise<void>(resolve => setImmediate(resolve));
function deferred<T>() { let resolve!: (value: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return { promise, resolve }; }
function harness(file: string, component: string, props: Record<string, unknown>, client: Record<string, unknown> = {}, search = "") {
  const source = fs.readFileSync(new URL(`../components/${file}.tsx`, import.meta.url), "utf8");
  const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.CommonJS, jsx: ts.JsxEmit.ReactJSX } }).outputText;
  const hooks: any[] = [], effects: (() => void)[] = [], events: Record<string, (event?: any) => void> = {}, emissions: any[] = [], timers: Array<(() => void) | null> = [];
  let cachedValue: unknown = null, cacheReads = 0;
  const session = { actor: owner.actorId, epoch: 1 };
  let cursor = 0, nextProps = props;
  const same = (a: unknown[], b: unknown[]) => a?.length === b?.length && a.every((item, index) => item === b[index]);
  const react = {
    createContext: (value: unknown) => ({ value }),
    useContext: (context: { value: unknown }) => context.value,
    useState(initial: unknown) { const i = cursor++; if (!hooks[i]) hooks[i] = { value: typeof initial === "function" ? initial() : initial }; return [hooks[i].value, (value: any) => { hooks[i].value = typeof value === "function" ? value(hooks[i].value) : value; }]; },
    useRef(initial: unknown) { const i = cursor++; return hooks[i] ||= { current: initial }; },
    useEffect(fn: () => unknown, deps: unknown[]) { const i = cursor++; if (!hooks[i] || !same(hooks[i].deps, deps)) { const prior = hooks[i]?.cleanup; hooks[i] = { deps }; effects.push(() => { prior?.(); hooks[i].cleanup = fn(); }); } },
  };
  const jsx = (type: unknown, props: unknown) => ({ type, props });
  const exports: any = {};
  vm.runInNewContext(compiled + (component === "ContactHistory" ? "\nexports.ContactHistory = ContactHistory;" : ""), { exports, URL, URLSearchParams, Date,
    CustomEvent: class { type: string; detail: unknown; constructor(type: string, options: { detail: unknown }) { this.type = type; this.detail = options.detail; } },
    setInterval: (callback: () => void) => { timers.push(callback); return timers.length; }, clearInterval(id: number) { timers[id - 1] = null; }, document: { hidden: false },
    window: { location: { search }, addEventListener(name: string, fn: () => void) { events[name] = fn; }, removeEventListener(name: string) { delete events[name]; }, dispatchEvent(event: any) { emissions.push(event); events[event.type]?.(event); } },
    require(id: string) {
    if (id === "@/components/chat/ChatLink") return { default: "a", ChatAnchor: "a", captureChatLink() {} };

      if (id.endsWith("/gtmProgress")) return gtmProgress;
      if (id.endsWith("/observability")) return { observeFounder(detail: unknown) { emissions.push(detail); } };
      if (id === "react") return react;
      if (id === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (id === "@/lib/founderUI") return { FounderUIContext: { value: "classic" } };
      if (id === "@/lib/localMode") return { isLocalConsole: () => false };
      if (id.endsWith("/founderAssetsApi")) return { checkedFounderStage, FOUNDER_STAGES, safeSiteUrl, contactsImportRequested, founderAssetsClient: client };
      if (id.endsWith("/client")) return { ...client, verifiedSessionEpoch: () => session.epoch, verifiedSessionSubject: () => session.actor };
      if (id.endsWith("/OneCard")) return { OneCard: "card" };
      if (id.endsWith("/KnownAnswer")) return { KnownAnswer: (props: Record<string, unknown>) => harness("chat/KnownAnswer", "KnownAnswer", props).render() };
      if (id.endsWith("/ResearchProgress")) return { ResearchProgress: "research-progress" };
      if (id.endsWith("/ErrorNotice")) return { ErrorNotice: "error" };
      if (id.endsWith("/OutreachDraft")) return { CopyDraft: "copy-draft", Draft: "draft" };
      if (id.endsWith("/CallNotesForm")) return { CallBriefView: "call-brief", CallNotesForm: "call-notes" };
      if (id.endsWith("/NextStepCard")) return { NextStepCard: "next-step" };
      if (id === "next/link") return { default: "link" };
      if (id === "next/navigation") return { usePathname: () => `/v/${owner.ventureId}` };
      if (id.endsWith("/apiCache")) return { cacheGet: () => { cacheReads++; return cachedValue; } };
      if (id.endsWith("/nextStep")) return { asServerStep };
      if (id.endsWith("/runStatusPresentation")) return { RUN_STATUS_REFRESH };
      if (id.endsWith("/researchBrief")) return { plainText };
      if (id.endsWith("/ideaAnswer")) return { editableIdeaAnswer };
      if (id.endsWith("/PageChoices")) return {};
      if (id.endsWith("/gtmJourneyApi") || id.endsWith("/reachableLeads") || id.endsWith("/gtmDiagnosisIntent") || id.endsWith("/SimulatedReaderCheck") || id.endsWith("/ImportContacts")) return {};
      if (id.endsWith(".css")) return { default: { button: "button", secondary: "secondary" } };
      throw new Error(id);
    },
  });
  return { render() { cursor = 0; const tree = exports[component](component === "useNextStep" ? nextProps.ventureId : nextProps); while (effects.length) effects.shift()!(); return tree; }, session, emissions,
    props(value: Record<string, unknown>) { nextProps = value; }, retire() { events["supymem-session-changed"]?.(); }, interval() { timers.forEach(callback => callback?.()); },
    poisonCache(value: unknown) { cachedValue = { data: value, fresh: true }; }, cacheReads: () => cacheReads,
    remount() { hooks.forEach(hook => hook.cleanup?.()); hooks.length = 0; timers.length = 0; }, dispatch(type: string, detail: unknown) { events[type]?.({ detail }); }, source };
}
function nodes(value: any): any[] { return !value || typeof value !== "object" ? [] : Array.isArray(value) ? value.flatMap(nodes) : [value, ...Object.values(value.props || {}).flatMap(nodes)]; }
const control = (tree: any, id: string) => nodes(tree).find(node => node.props?.["data-usage-control"] === id);
function text(value: any): string { return typeof value === "string" ? value : Array.isArray(value) ? value.map(text).join(" ") : value?.props ? Object.values(value.props).map(text).join(" ") : ""; }

test("stage confirmation offers one-click Yes and an explicit editable stage", () => {
  const selected: string[] = [], h = harness("ventures/FounderStageCard", "StageConfirmation", { stage: "page_live", confirmed: false, disabled: false, confirm: (stage: string) => selected.push(stage) });
  const initial = h.render(); assert.match(text(initial), /Landing page live/);
  control(initial, "founder_stage_confirm").props.onClick(); assert.deepEqual(selected, ["page_live"]);
  control(h.render(), "founder_stage_change").props.onClick();
  control(h.render(), "founder_stage_select").props.onChange({ target: { value: "outreach" } });
  control(h.render(), "founder_stage_confirm").props.onClick(); assert.deepEqual(selected, ["page_live", "outreach"]);
});
test("retired stage reads and retained confirmation cannot reveal or mutate another session", async () => {
  const read = deferred<unknown>(), calls: string[] = [];
  const h = harness("ventures/FounderStageCard", "FounderStageCard", { owner }, { stage: () => read.promise, confirmStage: async () => { calls.push("confirm"); return record; } });
  h.render(); h.retire(); h.session.epoch++; read.resolve(record); await flush();
  assert.doesNotMatch(text(h.render()), /Landing page live/); assert.equal(calls.length, 0);
});
test("a retained stage confirmation refuses an account retirement before a rerender", async () => {
  let calls = 0;
  const h = harness("ventures/FounderStageCard", "FounderStageCard", { owner }, { stage: async () => record, confirmStage: async () => { calls++; return record; } });
  h.render(); await flush();
  const stageCard = nodes(h.render()).find(node => typeof node.type === "function" && typeof node.props?.confirm === "function");
  assert.ok(stageCard); h.retire(); h.session.epoch++; stageCard.props.confirm("outreach"); await flush(); assert.equal(calls, 0);
});
test("a successful owned stage save refreshes its overview and emits exact next-step scope", async () => {
  let saved = 0;
  const h = harness("ventures/FounderStageCard", "FounderStageCard", { owner, onSaved: () => { saved++; } }, { stage: async () => record, confirmStage: async () => record });
  h.render(); await flush();
  nodes(h.render()).find(node => typeof node.type === "function" && typeof node.props?.confirm === "function").props.confirm("outreach"); await flush();
  assert.equal(saved, 1); assert.equal(h.emissions[0].type, "supymem-venture-changed");
  assert.equal(JSON.stringify(h.emissions[0].detail), JSON.stringify({ ventureId: owner.ventureId, actorId: owner.actorId, workspaceId: owner.workspaceId, epoch: 1 }));
});
test("the stage card uses the overview's stage receipt without duplicating its announcement", async () => {
  let reads = 0;
  const stageRecord = { ...record, stage: "outreach", message: "You're now at Outreach." };
  const h = harness("ventures/FounderStageCard", "FounderStageCard", { owner, stageRecord }, {
    stage: async () => { reads++; return record; },
  });
  h.render(); await flush();
  const view = h.render(), confirmation = nodes(view).find(node => typeof node.props?.confirm === "function");
  assert.equal(confirmation.props.stage, "outreach");
  assert.equal(reads, 0);
  assert.doesNotMatch(text(view), /You're now at Outreach/);
});
test("a delayed retired stage save cannot refresh another owner's overview or next step", async () => {
  let saved = 0; const response = deferred<unknown>();
  const h = harness("ventures/FounderStageCard", "FounderStageCard", { owner, onSaved: () => { saved++; } }, { stage: async () => record, confirmStage: () => response.promise });
  h.render(); await flush(); nodes(h.render()).find(node => typeof node.type === "function" && typeof node.props?.confirm === "function").props.confirm("outreach");
  h.retire(); h.session.epoch++; response.resolve(record); await flush(); assert.equal(saved, 0); assert.equal(h.emissions.length, 0);
});
const dashboard = (key: string) => ({ ok: true, status: 200, data: { venture: { id: owner.ventureId }, next_step: { key, stage: "Research", title: key, cta_label: "Continue", href: "/research", chat_action: null, payload: null, detail: "Synthetic step" } } });
test("page status refresh retires the ready next step and fences older reads", async () => {
  const reads: ReturnType<typeof deferred<unknown>>[] = [];
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, {
    api: () => { const response = deferred<unknown>(); reads.push(response); return response.promise; },
  });
  h.render(); reads[0].resolve(dashboard("site.ready")); await flush();
  assert.equal(h.render().key, "site.ready");
  h.dispatch(RUN_STATUS_REFRESH, undefined); assert.equal(h.render(), null);
  h.dispatch(RUN_STATUS_REFRESH, undefined);
  reads[1].resolve(dashboard("site.ready")); await flush(); assert.equal(h.render(), null);
  const failed = dashboard("work.failed");
  reads[2].resolve({ ...failed, data: { ...failed.data, run_status: { state: "failed" } } }); await flush();
  assert.equal(h.render().key, "work.failed"); assert.equal(h.render().runState, "failed");
  h.remount(); h.dispatch(RUN_STATUS_REFRESH, undefined); assert.equal(reads.length, 3);
});
test("current research readiness accompanies the owned next step without changing its action", async () => {
  const progress = { state: "leased", percent: 37, step_label: "Reading customer complaints", started_at: null };
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, {
    api: async () => ({ ...dashboard("gtm.outreach"), data: { ...dashboard("gtm.outreach").data,
      run_status: { state: "completed", research_report_ready: true, research_progress: progress } } }),
  });
  h.render(); await flush();
  assert.equal(h.render().key, "gtm.outreach");
  assert.equal(h.render().researchReportReady, true);
  assert.deepEqual(h.render().researchProgress, progress);
  h.retire(); h.session.epoch++;
  assert.equal(h.render(), null);
});
test("owned stage-change events immediately retire old next-step receipts and ignore earlier reads", async () => {
  const reads: ReturnType<typeof deferred<unknown>>[] = [];
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, { api: () => { const response = deferred<unknown>(); reads.push(response); return response.promise; } });
  assert.equal(h.render(), null, "the hook starts without a cached action");
  for (const detail of [{ ventureId: "foreign", actorId: owner.actorId, epoch: 1 }, { ventureId: owner.ventureId, actorId: "foreign", epoch: 1 }, { ventureId: owner.ventureId, actorId: owner.actorId, epoch: 0 }]) h.dispatch("supymem-venture-changed", detail);
  assert.equal(reads.length, 1);
  h.dispatch("supymem-venture-changed", { ventureId: owner.ventureId, actorId: owner.actorId, epoch: 1 }); assert.equal(h.render(), null);
  reads[0].resolve(dashboard("site.build")); await flush(); assert.equal(h.render(), null);
  reads[1].resolve(dashboard("research.start")); await flush(); assert.equal(h.render().key, "research.start"); assert.equal(reads.length, 2);
});
test("a next-step refresh cannot paint after its account epoch retires", async () => {
  const response = deferred<unknown>();
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, { api: () => response.promise, apiSwr: async () => undefined });
  h.render(); h.dispatch("supymem-venture-changed", { ventureId: owner.ventureId, actorId: owner.actorId, epoch: 1 }); h.session.epoch++;
  response.resolve(dashboard("research.start")); await flush(); assert.equal(h.render(), null);
});
test("held stage refresh and real polling never adopt an old SWR cache or flight", async () => {
  const reads: ReturnType<typeof deferred<unknown>>[] = [], oldCallbacks: Array<(value: unknown) => void> = [];
  let swrCalls = 0;
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, {
    api: () => { const response = deferred<unknown>(); reads.push(response); return response.promise; },
    apiSwr: async (_path: string, adopt: (value: unknown) => void) => { swrCalls++; oldCallbacks.push(adopt); adopt(dashboard("site.build")); },
  });
  h.poisonCache(dashboard("site.build").data);
  h.render(); await flush();
  if (reads[0]) { reads[0].resolve(dashboard("site.build")); await flush(); }
  h.dispatch("supymem-venture-changed", { ventureId: owner.ventureId, actorId: owner.actorId, epoch: 1 });
  h.interval(); oldCallbacks.forEach(callback => callback(dashboard("site.build")));
  assert.equal(h.render(), null, "the event retires the old step while fresh reads are held");
  assert.equal(swrCalls, 0, "all polling and event reads must use fresh API transport");
  assert.equal(h.cacheReads(), 0);
  reads.at(-1)!.resolve(dashboard("research.start")); await flush(); assert.equal(h.render().key, "research.start");
});
test("late pre-edit reads cannot survive a fresh event receipt, later poll or session reset", async () => {
  const reads: ReturnType<typeof deferred<unknown>>[] = [], oldCallbacks: Array<(value: unknown) => void> = [];
  let swrCalls = 0;
  const h = harness("layout/NextStepStrip", "useNextStep", { ventureId: owner.ventureId }, {
    api: () => { const response = deferred<unknown>(); reads.push(response); return response.promise; },
    apiSwr: async (_path: string, adopt: (value: unknown) => void) => { swrCalls++; oldCallbacks.push(adopt); },
  });
  h.render(); h.dispatch("supymem-venture-changed", { ventureId: owner.ventureId, actorId: owner.actorId, epoch: 1 });
  reads.at(-1)!.resolve(dashboard("research.start")); await flush(); assert.equal(h.render().key, "research.start");
  oldCallbacks.forEach(callback => callback(dashboard("site.build"))); if (reads.length > 1) reads[0].resolve(dashboard("site.build")); await flush();
  h.poisonCache(dashboard("site.build").data); // a late SWR from another component can still write the shared cache
  assert.equal(h.render().key, "research.start"); h.interval(); assert.equal(h.render().key, "research.start");
  h.retire(); assert.equal(h.render(), null, "session reset does not bootstrap an old cache");
  h.remount(); assert.equal(h.render(), null, "remount also waits for a fresh API receipt");
  assert.equal(swrCalls, 0); assert.equal(h.cacheReads(), 0);
});
test("unverified site has a reachable retry and late import cannot update a retired Website view", async () => {
  const response = deferred<unknown>(), saved: unknown[] = []; let active = true, calls = 0;
  const h = harness("site/ExistingSiteCard", "ExistingSiteCard", { owner, site: { url: "https://example.test", status: "unverified", audit: null }, current: () => active, saved: (value: unknown) => saved.push(value) }, { importSite: () => { calls++; return response.promise; } });
  const tree = h.render(); assert.match(text(tree), /could not verify it yet/); assert.ok(control(tree, "website_existing_import"));
  const form = nodes(tree).find(node => node.type === "form"); form.props.onSubmit({ preventDefault() {} }); form.props.onSubmit({ preventDefault() {} });
  assert.equal(calls, 1); active = false; h.session.epoch++; response.resolve({ site: null, founder_stage: record }); await flush(); assert.equal(saved.length, 0);
});
test("chat Website destination prefills its reviewed URL without importing automatically", () => {
  let calls = 0;
  const url = siteImportFromSearch("?import=https%3A%2F%2Fexample.test%2Fpage");
  const h = harness("site/ExistingSiteCard", "ExistingSiteCard", { owner, site: null, knownUrl: url, current: () => true, saved() {} }, { importSite: async () => { calls++; return {}; } });
  const tree = h.render(); assert.equal(control(tree, "website_existing_url").props.value, url); assert.ok(control(tree, "website_existing_import")); assert.equal(calls, 0);
});
test("chat contacts destination opens the normal importer with no contact text or mutation", () => {
  let calls = 0;
  const h = harness("gtm/ImportContacts", "ImportContacts", { owner, disabled: false, save: async () => { calls++; } }, { previewContacts: async () => { calls++; } }, "?venture=venture_fixture&import=1&text=ignored");
  const tree = h.render(); assert.equal(tree.props.open, true); assert.equal(control(tree, "gtm_contacts_text").props.value, ""); assert.equal(calls, 0);
});
test("imported history editor saves the founder's status and known date without inventing one", async () => {
  const calls: unknown[] = [];
  const h = harness("gtm/GtmJourney", "ContactHistory", { contact: { id: "fixture", imported: true, status: "new" }, disabled: false, save: async (value: unknown) => { calls.push(value); return true; } });
  control(h.render(), "gtm_contacts_history_status").props.onChange({ target: { value: "contacted" } });
  control(h.render(), "gtm_contacts_history_save").props.onClick(); await flush();
  assert.equal(JSON.stringify(calls[0]), JSON.stringify({ status: "contacted", last_contacted: null }));
  control(h.render(), "gtm_contacts_history_date").props.onChange({ target: { value: "2026-10-01" } });
  control(h.render(), "gtm_contacts_history_status").props.onChange({ target: { value: "replied" } });
  control(h.render(), "gtm_contacts_history_save").props.onClick(); await flush();
  assert.equal(JSON.stringify(calls[1]), JSON.stringify({ status: "replied", last_contacted: "2026-10-01" }));
});
test("contact preview does not commit until attestation, then commits the exact receipt once", async () => {
  const commits: unknown[][] = [];
  const h = harness("gtm/ImportContacts", "ImportContacts", { owner, disabled: false, save: async (...args: unknown[]) => { commits.push(args); return { imported: 1, skipped: 0 }; } }, { previewContacts: async () => preview });
  control(h.render(), "gtm_contacts_text").props.onChange({ target: { value: "Email\nqa@example.test" } });
  control(h.render(), "gtm_contacts_preview").props.onClick(); await flush();
  const unapproved = control(h.render(), "gtm_contacts_import"); assert.equal(unapproved.props.disabled, true); unapproved.props.onClick(); await flush(); assert.equal(commits.length, 0);
  control(h.render(), "gtm_contacts_attest").props.onChange({ target: { checked: true } });
  const approved = control(h.render(), "gtm_contacts_import"); approved.props.onClick(); approved.props.onClick(); await flush();
  assert.equal(commits.length, 1); assert.equal(commits[0][0], preview); assert.equal(commits[0][1], true); assert.match(text(h.render()), /1 contacts imported/);
});
test("changed CSV mapping or revoked consent invalidates retained import callbacks synchronously", async () => {
  for (const change of ["mapping", "consent", "basis", "note"]) {
    let commits = 0;
    const h = harness("gtm/ImportContacts", "ImportContacts", { owner, disabled: false, save: async () => { commits++; return { imported: 1, skipped: 0 }; } }, { previewContacts: async () => preview });
    control(h.render(), "gtm_contacts_text").props.onChange({ target: { value: "Email\nqa@example.test" } }); control(h.render(), "gtm_contacts_preview").props.onClick(); await flush();
    control(h.render(), "gtm_contacts_attest").props.onChange({ target: { checked: true } });
    const tree = h.render(), retained = control(tree, "gtm_contacts_import");
    if (change === "mapping") control(tree, "gtm_contacts_mapping").props.onChange({ target: { value: "Email" } });
    if (change === "consent") control(tree, "gtm_contacts_attest").props.onChange({ target: { checked: false } });
    if (change === "basis") control(tree, "gtm_contacts_basis").props.onChange({ target: { value: "legitimate_interest" } });
    if (change === "note") control(tree, "gtm_contacts_permission_note").props.onChange({ target: { value: "New permission evidence" } });
    retained.props.onClick(); await flush(); assert.equal(commits, 0, change);
  }
});
test("late file reads and previews do not reveal contacts after an account epoch changes", async () => {
  for (const request of ["file", "preview"]) {
    const response = deferred<any>();
    const h = harness("gtm/ImportContacts", "ImportContacts", { owner, disabled: false, save: async () => undefined }, { previewContacts: () => response.promise });
    if (request === "file") control(h.render(), "gtm_contacts_file").props.onChange({ target: { files: [{ name: "synthetic.csv", size: 30, text: () => response.promise }], value: "" } });
    else { control(h.render(), "gtm_contacts_text").props.onChange({ target: { value: "Email\nqa@example.test" } }); control(h.render(), "gtm_contacts_preview").props.onClick(); }
    h.retire(); h.session.epoch++; response.resolve(request === "file" ? "private contact fixture" : preview); await flush();
    const tree = h.render(); assert.equal(control(tree, "gtm_contacts_text").props.value, ""); assert.equal(control(tree, "gtm_contacts_import"), undefined);
  }
});
