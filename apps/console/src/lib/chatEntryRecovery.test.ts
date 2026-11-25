import assert from "node:assert/strict";
import fs from "node:fs";
import test from "node:test";
import vm from "node:vm";
import ts from "typescript";
import { composeIdeaAnswer, savedIdeaChoiceAnswer } from "./ideaAnswer.ts";
import { clearChatDraftIfMatches, draftAfterFailedSend, readChatDraft, shouldSendOnKey, writeChatDraft } from "./chatComposer.ts";
import { apiErrorCode } from "./apiFailure.ts";
import { guidePromptById } from "./capabilityGuide.ts";
import { ideaConversationActivity } from "./ideaChatPoll.ts";
import { forgetAppRoute } from "./appResume.ts";
import { isNearBottom, scrollDecision } from "./chatScroll.ts";
import { checkedEntryCommit, clearUnsentEntryDraft, committedChatHref, committedEntryHref, continueSavedEntry,
  entryConfirmationError, entryConfirmationFailure, entryDraftKey, entryDraftResumeKey, entryError, entryOwnerKey, entryReviewLabel, founderWords, readEntryDraft, resumableEntry, splitEntryHistory, writeEntryDraft,
  type EntryCommit, type EntryHistoryItem, type EntryProposal } from "./chatEntry.ts";

function proposal(state: EntryProposal["state"] = "pending"): EntryProposal {
  return {id: "proposal1", route: "gtm", venture_name: "Harbor", message: "Review the saved direction", skipped_steps: ["idea"],
    next_step: "gtm", expires_at: "2030-01-01T00:00:00Z", state};
}
const ventures = [
  {idea: "A scheduling tool for dental clinics", audience: "Independent dental clinics", name: "Clinic Compass"},
  {idea: "An app for hikers to plan weekend routes", audience: "Weekend hikers", name: "Trail Lantern"},
] as const;
function committed(): EntryCommit {
  return {status: "committed", route: "gtm", venture_id: "venture1", workspace_id: "workspace1",
    next_step: "gtm", action: {kind: "gtm_review", venture_id: "venture1", href: "/gtm?venture=venture1"},
    message: "The saved direction has a committed venture"};
}
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(done => {resolve = done;});
  return {promise, resolve};
}
async function flush() {for (let i = 0; i < 8; i++) await new Promise(resolve => setImmediate(resolve));}

/** Run the shipped component's actual hooks and callbacks, with no DOM/network. */
function renderer(history: EntryHistoryItem[] = [{text: "My product", proposal: proposal()}], requestCrypto = crypto) {
  const source = fs.readFileSync(new URL("../app/new/page.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declaration = (name: string) => tree.statements.find((node): node is ts.FunctionDeclaration =>
    ts.isFunctionDeclaration(node) && node.name?.text === name)!;
  const component = declaration("NewVentureConversation");
  assert.ok(component?.body);
  const statements = [...component.body.statements];
  assert.ok(ts.isReturnStatement(statements.at(-1)!));
  const prefix = statements.slice(0, -1).map(node => node.getText(tree)).join("\n");
  const view = (statements.at(-1) as ts.ReturnStatement).expression!.getText(tree);
  const helpers = ["historyTurns", "handoffReceipt", "rememberReceipt"].map(name => declaration(name).getText(tree)).join("\n");
  const handoff = tree.statements.find(node => ts.isVariableStatement(node) && node.getText(tree).includes("let committedHandoff"))!.getText(tree);
  const code = ts.transpileModule(`${handoff}\n${helpers}\nglobalThis.render=function({targetVentureId,freshStart=false,resume}) {${prefix}\nreturn {preview,confirm,back,loadHistory,draft,turns,earlier,proposal,activeReceipt,historyLoaded,busy,error,view:${view}};};`,
    {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText;
  const states: any[] = [], refs: {current: any}[] = [], effectsDeps: any[][] = [];
  const callbacks: {deps: any[]; value: any}[] = [];
  let si = 0, ri = 0, ei = 0, ci = 0, effects: (() => void | (() => void))[] = [];
  const owner = {actor: "actor1", epoch: 1}, location = {pathname: "/new", search: ""}, reader = {selecting: false};
  const drafts = new Map<string, string>();
  const storage = {getItem: (key: string) => drafts.get(key) ?? null,
    setItem: (key: string, value: string) => {drafts.set(key, value);}, removeItem: (key: string) => {drafts.delete(key);}};
  const calls: {path: string; options?: any}[] = [], navigated: string[] = [];
  const deadlines = new Set<() => void>();
  let session: () => Promise<any> = async () => ({ok: true});
  let respond: (path: string, options?: any) => Promise<any> = async (path, options) => {
    if (path.startsWith("/api/entry/chat/history")) return {ok: true, data: {items: history}};
    if (path.startsWith("/api/entry/chat/proposals/") && !options?.method) return {ok: true, data: {proposal: proposal()}};
    if (path === "/api/entry/chat/confirm") return {ok: true, data: committed()};
    if (path === "/api/entry/chat/preview") return {ok: true, data: {status: "proposal", proposal: proposal()}};
    return {ok: true, data: {status: "cancelled"}};
  };
  const context = vm.createContext({IDEA_MAX_CHARS: 200000, TOO_LONG: "too long", JSON, URLSearchParams, crypto: requestCrypto, encodeURIComponent, apiErrorCode, checkedEntryCommit, committedChatHref, committedEntryHref,
    useContext: () => "home", FounderUIContext: {}, DesktopNoticeBanner: "notice",
    CharlieMascot: "CharlieMascot", setCharlieMascot() {}, WorkspaceSwitcher: "WorkspaceSwitcher", forgetAppRoute,
    continueSavedEntry, entryConfirmationError, entryConfirmationFailure, entryDraftKey, entryDraftResumeKey, entryError, entryOwnerKey, readEntryDraft, resumableEntry, splitEntryHistory, writeEntryDraft, clearUnsentEntryDraft, composeIdeaAnswer, savedIdeaChoiceAnswer,
    shouldSendOnKey, founderWords, guidePromptById, draftStorage: () => storage, readChatDraft, writeChatDraft, clearChatDraftIfMatches, draftAfterFailedSend,
    setTimeout: (run: () => void) => { deadlines.add(run); return run; }, clearTimeout: (run: () => void) => deadlines.delete(run),
    isNearBottom, scrollDecision, useChatSelection: () => () => reader.selecting, window: {location},
    styles: new Proxy({}, {get: (_, key) => key}), composerStyles: new Proxy({}, {get: (_, key) => key}), AppShell: "AppShell", ConfirmedConversation: "ConfirmedConversation", ChatPanel: "ChatPanel", ChatMessageRow: "ChatMessageRow",
    WelcomeIntro: "WelcomeIntro", Link: "Link", VoiceMic: "VoiceMic", DictateButton: "DictateButton", IntakeAttachments: "IntakeAttachments", ErrorNotice: "ErrorNotice", StageConfirmation: "StageConfirmation", ChatWorking: "ChatWorking",
    useIntakeAttachments: () => ({ files: [], evidenceIds: [], uploading: false, restore() {} }),
    React: {createElement: (type: any, props: any, ...children: any[]) => ({type, props, children})},
    verifiedSessionSubject: () => owner.actor, verifiedSessionEpoch: () => owner.epoch,
    useRouter: () => ({replace: (href: string) => {navigated.push(href);}}), ensureSession: () => session(),
    api: (path: string, options?: any) => {calls.push({path, options}); return respond(path, options);},
    useRef: (value: any) => refs[ri++] ||= {current: value},
    useState: (value: any) => {const index = si++; if (!(index in states)) states[index] = typeof value === "function" ? value() : value;
      return [states[index], (next: any) => {states[index] = typeof next === "function" ? next(states[index]) : next;}];},
    useEffect: (effect: () => void | (() => void), deps: any[]) => {const index = ei++, old = effectsDeps[index];
      if (!old || deps.some((value, i) => !Object.is(value, old[i]))) {effectsDeps[index] = deps; effects.push(effect);}},
    useCallback: (value: any, deps: any[]) => {const index = ci++, old = callbacks[index];
      if (!old || deps.some((value, i) => !Object.is(value, old.deps[i]))) callbacks[index] = {value, deps};
      return callbacks[index].value;},
  });
  vm.runInContext(code, context);
  function render(targetVentureId?: string, freshStart = false, resume?: string) {
    si = ri = ei = ci = 0; effects = [];
    const result = context.render({targetVentureId, freshStart, resume});
    for (const effect of effects) effect();
    return result;
  }
  return {owner, location, reader, calls, navigated, drafts, render, expireHistory: () => [...deadlines].forEach(run => run()),
    remount(targetVentureId?: string, freshStart = false, resume?: string) { states.length = refs.length = effectsDeps.length = callbacks.length = 0; return render(targetVentureId, freshStart, resume); },
    setResponse: (fn: typeof respond) => {respond = fn;},
    setSession: (fn: typeof session) => {session = fn;}};
}
async function loaded(history?: EntryHistoryItem[]) {
  const ui = renderer(history); ui.render(); await flush(); return {...ui, callbacks: ui.render()};
}
const posts = (ui: ReturnType<typeof renderer>) => ui.calls.filter(call => call.options?.method === "POST");

test("a stale saved venture opens your owned chooser and preserves unsent text across domains", async () => {
  for (const venture of ventures) {
    const ui = renderer([]), response = deferred<any>();
    ui.location.search = "?venture=removed";
    ui.drafts.set("supymem.lastRoute.v1:actor1", "/new?venture=removed");
    ui.setResponse(async () => response.promise);
    ui.render("removed"); await flush();
    findElement(ui.render("removed").view, "textarea").props.onChange({target: {value: venture.idea}});
    response.resolve({ok: false, status: 404}); await flush();
    const state = ui.render("removed");
    assert.ok(findElement(state.view, "WorkspaceSwitcher"));
    assert.equal(state.draft, venture.idea);
    assert.equal(ui.drafts.get(entryDraftKey(ui.owner.actor, ui.location, "removed")!), venture.idea);
    assert.equal(ui.drafts.has("supymem.lastRoute.v1:actor1"), false);
    assert.equal(posts(ui).length, 0);
    assert.equal(ui.navigated.length, 0);
  }
});

test("a missing saved proposal offers a safe resume without discarding your draft across domains", async () => {
  for (const venture of ventures) {
    const ui = renderer([]);
    ui.location.search = "?resume=removed";
    ui.drafts.set(entryDraftKey(ui.owner.actor, {pathname: "/new", search: ""})!, venture.idea);
    ui.render(undefined, false, "removed"); await flush();
    const state = ui.render(undefined, false, "removed");
    assert.ok(findElement(state.view, "WorkspaceSwitcher"));
    assert.equal(state.draft, venture.idea);
    assert.equal(posts(ui).length, 0);
  }
});

test("session/history restoration never replaces a founder's non-empty composer", async () => {
  const ui = renderer([]);
  const history = deferred<any>();
  ui.setResponse(async () => history.promise);
  ui.render(); await flush();
  let state = ui.render();
  const nodes = (node: any): any[] => !node || typeof node !== "object" ? [] : [node, ...(node.children || []).flat(Infinity).flatMap(nodes)];
  const text = "An idea typed while the saved conversation is still opening. ".repeat(3);
  nodes(state.view).find(node => node.type === "textarea").props.onChange({target: {value: text}});
  state = ui.render(); assert.equal(state.draft, text);
  ui.drafts.clear();
  ui.drafts.set(entryDraftKey(ui.owner.actor, ui.location)!, "An older saved draft");
  ui.render(undefined, true); await flush();
  history.resolve({ok: true, data: {items: []}}); await flush();
  assert.equal(ui.render(undefined, true).draft, text, "same-owner history reload must not overwrite typing");
});

test("typing across session/history completion and a remount submits the entire 154-character idea", async () => {
  const ui = renderer([]), history = deferred<any>();
  const idea = "An app that helps independent dog walkers find clients, manage bookings, send reminders, and collect recurring payments without admin hassle for $8/month.";
  assert.equal(idea.length, 154);
  const part1 = idea.slice(0, -8), part2 = idea.slice(-8);
  ui.setResponse(async path => path.includes("/history") ? history.promise :
    {ok: true, data: {status: "clarify", id: "idea1", question: "Who is it for?", choices: []}});
  ui.render(); await flush();
  findElement(ui.render().view, "textarea").props.onChange({target: {value: part1}});
  assert.equal(ui.drafts.get(entryDraftKey(ui.owner.actor, ui.location)!), part1, "each edit persists before React renders");
  ui.owner.epoch++;
  const remounted = ui.remount();
  assert.equal(findElement(remounted.view, "textarea").props.value, part1, "mount restores before the first paint");
  findElement(remounted.view, "form").props.onSubmit({preventDefault() {}});
  assert.equal(posts(ui).length, 0, "Continue cannot send before restoration/history completes");
  history.resolve({ok: true, data: {items: []}}); await flush();
  findElement(ui.render().view, "textarea").props.onChange({target: {value: part1 + part2}});
  findElement(ui.render().view, "form").props.onSubmit({preventDefault() {}});
  await flush();
  assert.equal(posts(ui).length, 1);
  assert.equal(JSON.parse(posts(ui)[0].options.body).text, idea);
  assert.equal(ui.render().turns.find((turn: any) => turn.role === "user").text, idea);
});

test("first run has no example chips and typing stays in the composer without sending across domains", async () => {
  const ui = await loaded([]), focused: unknown[] = [];
  const walk = (element: any): any[] => !element || typeof element !== "object" ? [] : Array.isArray(element) ? element.flatMap(walk) : [element, ...walk(element.children)];
  const examples = walk(ui.render().view).filter(element => element.type === "button" && /^(An app|A )/.test(element.props?.key || ""));
  assert.equal(examples.length, 0);
  findElement(ui.render().view, "textarea").props.ref.current = {focus: () => focused.push(true)};
  for (const {idea} of ventures) {
    findElement(ui.render().view, "textarea").props.onChange({target: {value: idea}});
    assert.equal(ui.render().draft, idea);
    assert.equal(ui.drafts.get(entryDraftKey(ui.owner.actor, ui.location)!), idea);
  }
  assert.equal(focused.length, 0);
  assert.equal(posts(ui).length, 0);
});

test("typing after history loads survives later history effects and Continue submits without attachments", async () => {
  const ui = renderer([]), initialHistory = deferred<any>();
  ui.setResponse(() => initialHistory.promise);
  ui.render();
  assert.equal(findElement(ui.render().view, "textarea").props.disabled, true);
  initialHistory.resolve({ok: true, data: {items: []}}); await flush();
  const ready = ui.render();
  assert.equal(findElement(ready.view, "textarea").props.disabled, false);
  findElement(ready.view, "textarea").props.onChange({target: {value: "hello"}});
  const typed = ui.render();
  const continueButton = (view: any): any => {
    if (view?.props?.["data-usage-control"] === "onboarding_continue") return view;
    return (Array.isArray(view) ? view : view?.children || []).map(continueButton).find(Boolean);
  };
  assert.equal(continueButton(typed.view).props.disabled, false);
  const laterHistory = deferred<any>(); ui.setResponse(() => laterHistory.promise);
  const reloading = typed.loadHistory(); await flush(); ui.render();
  laterHistory.resolve({ok: true, data: {items: []}}); await reloading; await flush();
  const restored = ui.render();
  assert.equal(restored.draft, "hello");
  assert.equal(findElement(restored.view, "textarea").props.disabled, false);
  assert.equal(continueButton(restored.view).props.disabled, false);
  assert.equal(posts(ui).length, 0);
  ui.setResponse(async () => ({ok: true, data: {status: "clarify", id: "hello", question: "What is your idea?", choices: []}}));
  findElement(restored.view, "form").props.onSubmit({preventDefault() {}}); await flush();
  assert.equal(posts(ui).length, 1);
  assert.equal(posts(ui)[0].path, "/api/entry/chat/preview");
  assert.equal(JSON.parse(posts(ui)[0].options.body).text, "hello");
  assert.deepEqual(JSON.parse(posts(ui)[0].options.body).evidence_ids, []);
});

test("entry drafts survive relaunch and receipt epochs, stay owner scoped, and clear only on acknowledged send", async () => {
  const ui = await loaded([]);
  const type = (text: string) => findElement(ui.render().view, "textarea").props.onChange({target: {value: text}});
  type("Unsent idea");
  ui.owner.epoch += 2; ui.remount(); await flush(); assert.equal(ui.render().draft, "Unsent idea");
  ui.owner.actor = "actor2"; ui.remount(); await flush(); assert.equal(ui.render().draft, "");
  ui.owner.actor = "actor1"; ui.remount(); await flush(); assert.equal(ui.render().draft, "Unsent idea");
  ui.location.search = "?venture=other"; ui.remount("other"); await flush(); assert.equal(ui.render("other").draft, "");
  ui.location.search = ""; ui.remount(); await flush(); assert.equal(ui.render().draft, "Unsent idea");
  const pending = deferred<any>(); ui.setResponse(() => pending.promise);
  const sending = ui.render().preview("Unsent idea"); await flush();
  assert.equal([...ui.drafts.values()].includes("Unsent idea"), true);
  pending.resolve({ok: false, data: null}); await sending; assert.equal(ui.render().draft, "Unsent idea");
  ui.setResponse(async () => ({ok: true, data: {status: "clarify", id: "question", question: "Who?", choices: []}}));
  await ui.render().preview("Unsent idea"); assert.equal(ui.drafts.size, 0);
  type("New answer");
  const acknowledged = deferred<any>(); ui.setResponse(() => acknowledged.promise);
  const next = ui.render().preview("New answer"); await flush(); type("Typed while waiting");
  acknowledged.resolve({ok: true, data: {status: "clarify", id: "next", question: "Why?", choices: []}}); await next;
  assert.equal(ui.render().draft, "Typed while waiting");
  ui.remount(); await flush(); assert.equal(ui.render().draft, "Typed while waiting");
  type(""); assert.equal(ui.drafts.size, 0);
});

test("New venture starts blank without cancelling the old draft, and Continue restores it", async () => {
  const ui = renderer([{text: "Old idea for teachers", proposal: proposal()}]);
  const oldKey = entryDraftKey(ui.owner.actor, ui.location)!;
  ui.drafts.set(oldKey, "Old audience answer");
  ui.location.search = "?fresh=new-click";
  ui.render(undefined, true); await flush();
  const fresh = ui.render(undefined, true);
  assert.equal(fresh.draft, ""); assert.equal(fresh.proposal, null); assert.equal(fresh.turns.length, 0);
  assert.equal(fresh.earlier.length, 2); assert.equal(ui.drafts.get(oldKey), "Old audience answer");
  assert.equal(posts(ui).length, 0);
  const links = (node: any): any[] => !node || typeof node !== "object" ? [] : [node, ...(node.children || []).flatMap(links)];
  const resume = links(fresh.view).find(node => node.type === "Link" && node.children?.includes("Continue your draft"));
  assert.equal(resume.props.href, "/new?resume=proposal1");
  ui.location.search = "?resume=proposal1";
  ui.remount(undefined, false, "proposal1"); await flush();
  const restored = ui.render(undefined, false, "proposal1");
  assert.equal(restored.proposal.id, "proposal1"); assert.equal(restored.draft, "Old audience answer");
  assert.equal(posts(ui).length, 0);
});

test("a fresh venture reloads its own unfinished answers and resumes them after starting another", async () => {
  const old = {text: "Old idea", proposal: proposal()}, next = {text: "New idea", proposal: {...proposal(), id: "new-proposal"}};
  const ui = renderer([old]); ui.location.search = "?fresh=first-click";
  ui.render(undefined, true); await flush();
  ui.setResponse(async path => path.includes("/history") ? {ok: true, data: {items: [old, next]}} : {ok: true, data: {status: "proposal", proposal: next.proposal}});
  await ui.render(undefined, true).preview("New idea");
  ui.remount(undefined, true); await flush();
  assert.equal(ui.render(undefined, true).proposal.id, "new-proposal");
  ui.drafts.set(entryDraftKey(ui.owner.actor, ui.location)!, "My revised new idea");
  ui.location.search = "?fresh=second-click"; ui.remount(undefined, true); await flush();
  assert.equal(ui.render(undefined, true).proposal, null); assert.equal(ui.render(undefined, true).draft, "");
  ui.location.search = "?resume=new-proposal"; ui.remount(undefined, false, "new-proposal"); await flush();
  assert.equal(ui.render(undefined, false, "new-proposal").draft, "My revised new idea");
  assert.equal(ui.render(undefined, false, "new-proposal").proposal.id, "new-proposal");
  assert.equal(ui.calls.filter(call => call.path.endsWith("/confirm")).length, 0);
});

test("fresh intake keeps old voice notes in earlier history and reloads its new notes once", async () => {
  const ui = renderer([]), oldId = "entry-voice-note_" + "7".repeat(64), newId = "entry-voice-note_" + "8".repeat(64);
  const oldNote = {id: oldId, body_md: "Old transcript", partition: "fresh", target_venture_id: null};
  const newNote = {id: newId, body_md: "New transcript", partition: "fresh", target_venture_id: null};
  ui.location.search = "?fresh=voice-click";
  ui.setResponse(async () => ({ok: true, data: {items: [], voice_notes: [oldNote]}}));
  ui.render(undefined, true); await flush();
  assert.equal(ui.render(undefined, true).turns.length, 0);
  assert.equal(ui.render(undefined, true).earlier[0].id, oldId);
  findElement(ui.render(undefined, true).view, "VoiceMic").props.onSummary(newNote.body_md, newId);
  ui.setResponse(async () => ({ok: true, data: {items: [], voice_notes: [oldNote, newNote]}}));
  ui.remount(undefined, true); await flush();
  assert.deepEqual(Array.from(ui.render(undefined, true).turns.map((turn: any) => turn.id)), [newId]);
  assert.deepEqual(Array.from(ui.render(undefined, true).earlier.map((turn: any) => turn.id)), [oldId]);
});

test("new questions and voice notes respect history readers and selection without scrolling questions across domains", async () => {
  for (const {idea} of ventures) {
    const ui = await loaded([]);
    let question = 0;
    ui.setResponse(async () => ({ok: true, data: {status: "clarify", id: `question${++question}`, question: "Who is it for?", choices: []}}));
    const thread = {scrollHeight: 1000, scrollTop: 200, clientHeight: 400};
    const log = findElement(ui.render().view, "div", "Conversation");
    log.props.ref.current = thread;
    log.props.onScroll({currentTarget: thread});
    await ui.render().preview(idea);
    const view = ui.render().view;
    const articles = (node: any): any[] => !node || typeof node !== "object" ? [] : [node, ...(Array.isArray(node) ? node : node.children || []).flatMap(articles)];
    assert.ok(findElement(view, "article"));
    assert.equal(articles(view).some(node => node.type === "article" && node.props.ref), false);
    assert.equal(thread.scrollTop, 200);
    const focused: unknown[] = [];
    findElement(view, "textarea").props.ref.current = {focus: (options: unknown) => focused.push(options)};
    ui.reader.selecting = true;
    await ui.render().preview("A focused audience");
    ui.render();
    assert.equal(thread.scrollTop, 200);
    assert.equal(focused.length, 0, "new questions never steal focus while selecting");
    const noteId = "entry-voice-note_" + "9".repeat(64);
    findElement(ui.render().view, "VoiceMic").props.onSummary("From your voice call:\n" + "Long transcript\n".repeat(80), noteId);
    ui.render();
    assert.equal(thread.scrollTop, 200);
    findElement(ui.render().view, "VoiceMic").props.onSummary("Updated transcript", noteId);
    assert.equal(ui.render().turns.filter((turn: any) => turn.id === noteId).length, 1);
    assert.equal(thread.scrollTop, 200);
    ui.reader.selecting = false;
    thread.scrollTop = 600;
    log.props.onScroll({currentTarget: thread});
    const nextNoteId = "entry-voice-note_" + "a".repeat(64);
    findElement(ui.render().view, "VoiceMic").props.onSummary("New voice notes", nextNoteId);
    ui.render();
    assert.equal(thread.scrollTop, 1000, "new arrivals follow a reader at the bottom");
    thread.scrollHeight = 1100;
    findElement(ui.render().view, "VoiceMic").props.onSummary("Updated voice notes", nextNoteId);
    ui.render();
    assert.equal(thread.scrollTop, 1000, "editing a saved transcript is not a new arrival");
  }
});

test("explicit proposal expiry re-previews the exact last answer without auto-confirming", async () => {
  const text = "A booking tool\nAudience: Independent tutors\nClarification: Paying customers";
  for (const expiresOn of ["read", "confirm"]) {
    const ui = await loaded([{text, proposal: proposal()}]); ui.calls.length = 0;
    const refreshed = {...proposal(), id: "refreshed"}; let resent = false;
    ui.setResponse(async (path, options) => {
      if (path.includes("/history")) return {ok: true, data: {items: [{text, proposal: resent ? refreshed : proposal()}]}};
      if (path === "/api/entry/chat/preview") {assert.equal(JSON.parse(options.body).text, text); resent = true; return {ok: true, data: {status: "proposal", proposal: refreshed}};}
      if (expiresOn === "read" || path.endsWith("/confirm")) return {ok: false, data: {detail: {error: {code: "proposal_expired", message: "Send again"}}}};
      return {ok: true, data: {proposal: proposal()}};
    });
    await ui.render().confirm();
    assert.equal(ui.render().proposal.id, "refreshed"); assert.equal(ui.render().error, ""); assert.equal(ui.navigated.length, 0);
    assert.equal(posts(ui).filter(call => call.path.endsWith("/preview")).length, 1);
    assert.equal(posts(ui).filter(call => call.path.endsWith("/confirm")).length, expiresOn === "confirm" ? 1 : 0);
  }
});

test("expiry recovery does not run for uncertain failures, committing proposals, or retired owners", async () => {
  for (const code of ["proposal_not_pending", "proposal_busy", "unknown", "proposal_expired"]) {
    for (const state of ["pending", "committing"] as const) {
      let resends = 0;
      const recovery = continueSavedEntry(proposal(state), async () => proposal(state),
        async () => {throw entryConfirmationFailure({error: {code}});}, () => true, async () => {resends++;});
      if (code === "proposal_expired" && state === "pending") assert.equal(await recovery, null);
      else await assert.rejects(recovery);
      assert.equal(resends, code === "proposal_expired" && state === "pending" ? 1 : 0);
    }
  }
  let current = true, resends = 0;
  await assert.rejects(continueSavedEntry(proposal(), async () => proposal(), async () => {
    current = false; throw entryConfirmationFailure({detail: {error: {code: "proposal_expired"}}});
  }, () => current, async () => {resends++;}));
  assert.equal(resends, 0);
  await assert.rejects(continueSavedEntry(proposal(), async () => proposal("committing"), async () => {
    throw entryConfirmationFailure({error: {code: "proposal_expired"}});
  }, () => true, async () => {resends++;}));
  assert.equal(resends, 0);
});

test("entry send failure preserves newer typing in memory and durable storage", async () => {
  const ui = await loaded([]), failed = deferred<any>();
  const type = (text: string) => findElement(ui.render().view, "textarea").props.onChange({target: {value: text}});
  type("Original answer"); ui.setResponse(() => failed.promise);
  const sending = ui.render().preview("Original answer"); await flush(); type("Newer answer");
  failed.resolve({ok: false, data: null}); await sending;
  assert.equal(ui.render().draft, "Newer answer");
  assert.equal([...ui.drafts.values()].includes("Newer answer"), true);
});

test("pending proposals offer a name chip and an editable normal composer across domains", async () => {
  for (const {idea, name: suggested} of ventures) {
    const saved = {...proposal(), venture_name: suggested};
    const ui = await loaded([{text: idea, proposal: saved}]);
    const view = ui.render().view;
    assert.equal(findElement(view, "input"), undefined);
    const name = findElement(view, "textarea");
    assert.equal(name.props.id, "entry-message");
    assert.equal(name.props.maxLength, 48);
    assert.equal(name.props.value, "");
    assert.equal(name.props.disabled, false);
    const chip = findElement(view, "button", suggested);
    assert.equal(chip.props.className, "option");
    assert.equal(findElement(findElement(view, "form"), "button", suggested), undefined, "the name chip is in Charlie's bubble, not the composer");
    assert.equal(posts(ui).length, 0);
    name.props.onChange({target: {value: "x".repeat(49)}});
    await ui.render().confirm();
    assert.equal(posts(ui).length, 0, "oversized names never commit");
    assert.match(ui.render().error, /48/);
    const edited = `${suggested} Studio`;
    name.props.onChange({target: {value: edited}});
    const form = findElement(ui.render().view, "form");
    form.props.onSubmit({preventDefault() {}}); form.props.onSubmit({preventDefault() {}});
    await flush();
    const confirms = posts(ui).filter(call => call.path.endsWith("/confirm"));
    assert.equal(confirms.length, 1);
    assert.equal(JSON.parse(confirms[0].options.body).name, edited);
    const picked = await loaded([{text: idea, proposal: saved}]);
    const pick = findElement(picked.render().view, "button", suggested);
    pick.props.onClick(); pick.props.onClick(); await flush();
    assert.equal(posts(picked).filter(call => call.path.endsWith("/confirm")).length, 1);
    assert.equal(JSON.parse(posts(picked).find(call => call.path.endsWith("/confirm"))!.options.body).name, suggested);
  }
});

test("U48 proposal names stay in chips on send and reload while typed edits survive and unnamed proposals stay empty", async () => {
  for (const [text, expected] of [
    ["Ledger Loom is a tool for accountants", "Ledger Loom"],
    ["PupPath: an app for dog walkers", "PupPath"],
    ["Paper Trail is a web tool for invoices", "Paper Trail"],
    ["I want to build an app for dog walkers", ""],
    ["This is an app for dog walkers", ""],
    ["My idea is a Mac app for founders", ""],
  ]) {
    const saved = { ...proposal(), venture_name: expected || undefined };
    const ui = await loaded([]);
    ui.setResponse(async path => ({ ok: true, data: path.includes("/history") ? {items: [{text, proposal: saved}]} : { status: "proposal", proposal: saved } }));
    await ui.render().preview(text);
    assert.equal(findElement(ui.render().view, "input"), undefined);
    const input = findElement(ui.render().view, "textarea");
    assert.equal(input.props.value, "");
    assert.equal(input.props.maxLength, 48);
    if (expected) assert.equal(findElement(ui.render().view, "button", expected).props.className, "option");
    else assert.ok(!JSON.stringify(ui.render().view).includes('"className":"option"'));
    input.props.onChange({ target: { value: "Founder edit" } });
    assert.equal(findElement(ui.render().view, "textarea").props.value, "Founder edit");
    ui.remount(); await flush();
    assert.equal(findElement(ui.render().view, "textarea").props.value, "Founder edit");
    assert.equal(posts(ui).filter(call => call.path.endsWith("/confirm")).length, 0);
    const restored = await loaded([{ text, proposal: saved }]);
    assert.equal(findElement(restored.render().view, "textarea").props.value, "");
    if (expected) assert.ok(findElement(restored.render().view, "button", expected));
    else assert.ok(!JSON.stringify(restored.render().view).includes('"className":"option"'));
    assert.equal(posts(restored).length, 0);
  }
});

test("proposal controls and voice actions use fixed English labels despite model-provided French", async () => {
  const french = {...proposal(), button_label: "Continuer"};
  const ui = await loaded([]);
  ui.setResponse(async () => ({ok: true, data: {status: "proposal", proposal: french}}));
  assert.equal((await ui.render().preview("A tool for tutors", true)).actions[0].label, "Continue");
  const view = ui.render().view;
  const text = JSON.stringify(view);
  assert.ok(text.includes('"Continue"'));
  assert.ok(!text.includes("Continuer"));
  assert.equal(findElement(view, "VoiceMic").props.screen.visible_actions[0], "Continue");
});

test("clarification choices submit once without requiring Continue across domains", async () => {
  for (const [idea, choices] of [["A tool for tutors", ["Tutors", "Schools"]], ["An app for home cooks", ["Home cooks", "Families"]]] as const) {
    const ui = await loaded([{text: idea, proposal: {...proposal("clarify"), choices: [...choices]}}]);
    const all = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(all)];
    const options = () => all(ui.render().view).filter(node => node.props?.["data-usage-control"] === "onboarding_choice");
    assert.ok(options().every(option => option.props["aria-pressed"] === false));
    assert.equal(all(ui.render().view).find(node => node.props?.["data-usage-control"] === "onboarding_continue").props.disabled, true);
    const form = findElement(ui.render().view, "form"), row = all(form).find(node => node.props?.className === "composerActions");
    const groups = row.children.flat().filter((node: any) => node?.props?.className === "composerTools");
    assert.ok(findElement(groups[0], "VoiceMic"));
    assert.ok(findElement(groups[1], "DictateButton"));
    assert.ok(all(groups[1]).some(node => node.props?.["data-usage-control"] === "onboarding_continue"));
    const pick = options()[0];
    assert.equal(pick.props.className, "option");
    pick.props.onClick(); pick.props.onClick();
    await flush();
    assert.equal(posts(ui).length, 1);
    assert.equal(JSON.parse(posts(ui)[0].options.body).text, `${idea}\nClarification: Assumed: ${choices[0]}`);
    assert.equal(findElement(ui.render().view, "input"), undefined);
    assert.equal(findElement(ui.render().view, "textarea").props.maxLength, 48);
  }
});

test("initial composer keeps attachment and voice on the left, dictation and Continue on the right", async () => {
  const ui = await loaded([]);
  const form = findElement(ui.render().view, "form");
  const row = form.children.flat().find((node: any) => node?.props?.className === "composerActions");
  const groups = row.children.flat();
  assert.ok(findElement(groups[0], "IntakeAttachments"));
  assert.ok(findElement(groups[0], "VoiceMic"));
  assert.ok(findElement(groups[1], "DictateButton"));
  assert.equal(findElement(groups[1], "button").props["data-usage-control"], "onboarding_continue");
});

test("clarification progress counts follow-ups on reload and Back without an invented remaining total", async () => {
  const question = (id: string) => ({...proposal("clarify"), id, choices: ["Tutors"]});
  const ui = await loaded([{text: "A booking tool\nAudience: tutors", proposal: question("question1")},
    {text: "A booking tool\nAudience: tutors\nClarification: lessons", proposal: question("question2")}]);
  const progress = (view: any): any => {
    if (!view || typeof view !== "object") return undefined;
    if (view.type === "p" && view.props?.className === "progress") return view.children.join("");
    return (Array.isArray(view) ? view : view.children || []).map(progress).find(Boolean);
  };
  assert.equal(progress(ui.render().view), "Question 3");
  assert.equal(findElement(ui.render().view, "textarea").props.placeholder, "Answer in your own words…");
  await ui.render().back();
  assert.equal(progress(ui.render().view), "Question 2");
});

test("clarification progress stays on the audience question through follow-ups and Back", async () => {
  const question = (id: string) => ({...proposal("clarify"), id, onboarding_field: "customer" as const, choices: ["Tutors"]});
  const ui = await loaded([{text: "A booking tool\nAudience: tutors", proposal: question("question1")},
    {text: "A booking tool\nAudience: tutors\nClarification: lessons", proposal: question("question2")}]);
  const progress = (view: any): any => {
    if (!view || typeof view !== "object") return undefined;
    if (view.type === "p" && view.props?.className === "progress") return view.children.join("");
    return (Array.isArray(view) ? view : view.children || []).map(progress).find(Boolean);
  };
  assert.equal(progress(ui.render().view), "Question 2");
  assert.equal(findElement(ui.render().view, "textarea").props.placeholder, "Answer in your own words…");
  await ui.render().back();
  assert.equal(progress(ui.render().view), "Question 2");
});

test("the live smalltalk journey rewinds, asks the missing audience, names and persists the exact idea across domains", async () => {
  for (const {idea, audience, name: ventureName} of ventures) {
    const ui = await loaded([]);
    const elements = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(elements)];
    const direction = `${idea}\nAudience: ${audience}`;
    const savedProposal: EntryProposal = {...proposal(), route: "startup", next_step: "idea", skipped_steps: [], venture_name: ""};
    const receipt: EntryCommit = {...committed(), route: "startup", next_step: "idea",
      action: {kind: "idea", venture_id: "venture1", href: "/v/venture1/idea"}};
    const created: Array<{name: string; raw_idea: string}> = [];
    let savedDirection = "";
    ui.setResponse(async (path, options) => {
      if (path === "/api/entry/chat/preview") {
        const text = JSON.parse(options!.body).text;
        if (!text.startsWith("hello") && text.includes("Audience:")) savedDirection = text;
        return {ok: true, data: text.startsWith("hello")
          ? {status: "clarify", id: text === "hello" ? "hello1" : "hello2", question: "What would your product do?", choices: ["Example idea"], onboarding_question: text === "hello" ? "Who is it for?" : null}
          : !text.includes("Audience:")
            ? {status: "proposal", proposal: {...savedProposal, id: "audience1", onboarding_question: "Who is it for?"}}
            : {status: "proposal", proposal: savedProposal}};
      }
      if (path === "/api/entry/chat/confirm") {
        assert.equal(JSON.parse(options!.body).proposal_id, savedProposal.id);
        created.push({name: JSON.parse(options!.body).name, raw_idea: savedDirection});
        return {ok: true, data: receipt};
      }
      if (!options?.method) return {ok: true, data: {proposal: savedProposal}};
      return {ok: true, data: {status: "cancelled"}};
    });
    const label = () => elements(ui.render().view).find(node => node.type === "label" && node.props.htmlFor === "entry-message").children.join("");
    const answer = async (text: string) => {
      findElement(ui.render().view, "textarea").props.onChange({target: {value: text}});
      const view = ui.render().view;
      const action = elements(view).find(node => node.props?.["data-usage-control"] === "onboarding_continue");
      assert.equal(action.props.type, "submit");
      assert.equal(action.children.join(""), "Continue");
      assert.equal(action.props.disabled, false);
      findElement(view, "form").props.onSubmit({preventDefault() {}});
      await flush();
    };
    assert.equal(label(), "What is your idea?");
    await answer("hello");
    assert.equal(posts(ui).length, 1);
    assert.equal(label(), "Who is it for?");
    await answer("Not sure");
    assert.equal(JSON.parse(posts(ui).at(-1)!.options.body).text, "hello\nAudience: Not sure");
    assert.equal(created.length, 0);
    assert.equal(elements(ui.render().view).some(node => node.props?.id === "venture-name"), false);
    await ui.render().back();
    assert.equal(posts(ui).at(-1)!.path, "/api/entry/chat/proposals/hello2/cancel");
    assert.equal(label(), "Who is it for?");
    assert.equal(ui.render().draft, "Not sure");
    await ui.render().back();
    assert.equal(posts(ui).at(-1)!.path, "/api/entry/chat/proposals/hello1/cancel");
    assert.equal(label(), "What is your idea?");
    assert.equal(ui.render().draft, "hello");
    await answer(idea);
    assert.equal(label(), "Who is it for?");
    assert.equal(elements(ui.render().view).some(node => node.props?.id === "venture-name"), false);
    await answer(audience);
    const preview = posts(ui).filter(call => call.path === "/api/entry/chat/preview").at(-1)!;
    assert.equal(JSON.parse(preview.options!.body).text, direction);
    assert.equal(ui.render().proposal.state, "pending");
    assert.equal(posts(ui).some(call => call.path.endsWith("/confirm")), false);
    assert.equal(findElement(ui.render().view, "input"), undefined);
    const name = findElement(ui.render().view, "textarea");
    assert.equal(name.props.id, "entry-message");
    assert.equal(name.props.maxLength, 48);
    name.props.onChange({target: {value: ventureName}});
    findElement(ui.render().view, "form").props.onSubmit({preventDefault() {}});
    await flush();
    assert.deepEqual(created, [{name: ventureName, raw_idea: direction}]);
    assert.equal(ui.render().activeReceipt.venture_id, receipt.venture_id);
    assert.equal(ui.navigated.at(-1), "/new?venture=venture1");
  }
});

test("the questionnaire keeps progress, focus, Enter, Back and a typed name in the normal composer across domains", async () => {
  for (const {idea, audience, name: ventureName} of ventures) {
    const ui = await loaded([]);
    ui.setResponse(async (path, options) => {
      if (path === "/api/entry/chat/preview") return {ok: true, data: JSON.parse(options!.body).text.includes("Audience:")
        ? {status: "proposal", proposal: {...proposal(), venture_name: ""}}
        : {status: "proposal", proposal: {...proposal(), venture_name: "", id: "audience1", route: "startup", onboarding_question: "Who is it for?"}}};
      if (path.endsWith("/confirm")) return {ok: true, data: committed()};
      return {ok: true, data: path.endsWith("/cancel") ? {status: "cancelled"} : {proposal: proposal()}};
    });
    const elements = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(elements)];
    const progress = (view: any) => elements(view).find(node => node.type === "p" && node.props?.role === "status").children.join("");
    const answer = (text: string) => findElement(ui.render().view, "textarea").props.onChange({target: {value: text}});
    const enter = (options = {}) => {
      const view = ui.render().view;
      findElement(view, "textarea").props.onKeyDown({key: "Enter", shiftKey: false, ...options, preventDefault() {},
        currentTarget: {form: {requestSubmit: () => findElement(view, "form").props.onSubmit({preventDefault() {}})}}});
    };
    let current = ui.render();
    assert.equal(progress(current.view), "Question 1");
    assert.equal(elements(current.view).filter(node => node.type === "textarea").length, 1);
    assert.equal(elements(current.view).some(node => node.props?.id === "venture-name"), false);
    let answerFocus = 0, nameFocus = 0;
    findElement(current.view, "textarea").props.ref.current = {focus(options: any) {assert.equal(options.preventScroll, true); answerFocus++;}};
    answer(idea);
    enter({nativeEvent: {isComposing: true}});
    assert.equal(progress(ui.render().view), "Question 1", "IME Enter never submits an unfinished answer");
    enter({shiftKey: true});
    assert.equal(progress(ui.render().view), "Question 1", "Shift+Enter remains a newline");
    enter();
    await flush(); current = ui.render();
    assert.equal(progress(current.view), "Question 2");
    assert.ok(answerFocus >= 1);
    assert.equal(posts(ui).length, 1, "the idea is interpreted before asking what is missing");
    assert.equal(posts(ui).some(call => call.path.endsWith("/confirm")), false);
    answer(audience); await ui.render().back();
    current = ui.render(); assert.equal(progress(current.view), "Question 1");
    assert.equal(current.draft, idea);
    const revised = `Updated direction: ${idea}`;
    answer(revised); enter(); await flush();
    answer(audience);
    enter(); await flush(); current = ui.render();
    assert.equal(progress(current.view), "Name your venture");
    assert.equal(findElement(current.view, "textarea").props.maxLength, 48);
    assert.equal(findElement(current.view, "textarea").props.value, "");
    assert.equal(findElement(current.view, "StageConfirmation"), undefined);
    assert.equal(elements(current.view).filter(node => node.type === "input" && node.props.type === "text").length, 0);
    assert.equal(elements(current.view).some(node => node.props?.["data-usage-control"] === "onboarding_asset_skip"), false);
    assert.equal(JSON.parse(posts(ui).at(-1)!.options.body).text, `${revised}\nAudience: ${audience}`);
    const name = findElement(current.view, "textarea");
    name.props.ref.current = {focus(options: any) {assert.equal(options.preventScroll, true); nameFocus++;}};
    name.props.onChange({target: {value: ventureName}});
    const suggestion = elements(ui.render().view).find(node => node.props?.["data-usage-control"] === "onboarding_suggest_name");
    assert.ok(suggestion, "a name suggestion stays an optional helper");
    await ui.render().back(); await flush(); current = ui.render();
    assert.equal(progress(current.view), "Question 2");
    assert.equal(current.draft, audience);
    enter(); await flush(); current = ui.render();
    assert.equal(nameFocus, 1);
    assert.equal(findElement(current.view, "textarea").props.value, "");
    assert.ok(findElement(current.view, "button", ventureName), "Back preserves your typed name as a confirmable chip");
    findElement(current.view, "form").props.onSubmit({preventDefault() {}}); await flush();
    const confirms = posts(ui).filter(call => call.path === "/api/entry/chat/confirm");
    assert.equal(confirms.length, 1, "the name screen commits directly without another confirmation");
    assert.equal(JSON.parse(confirms[0].options.body).name, ventureName);
    assert.deepEqual(ui.navigated, ["/new?venture=venture1"]);
  }
});

test("name suggestion chips leave the composer editable without replaying an uncertain request across domains", async () => {
  for (const {idea, name} of ventures) {
    const ui = await loaded([{text: idea, proposal: {...proposal(), venture_name: ""}}]);
    const elements = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(elements)];
    const attempts: string[] = [];
    ui.setResponse(async (path, options) => {
      assert.ok(path.endsWith("/name"));
      attempts.push(JSON.parse(options.body).request_id);
      return attempts.length === 2 ? {ok: false, status: 500, data: {}} :
        {ok: true, status: 200, data: {name: attempts.length === 1 ? name : `${name} Plus`}};
    });
    const suggestion = () => elements(ui.render().view).find(node => node.props?.["data-usage-control"] === "onboarding_suggest_name");
    assert.equal(suggestion().children.join(""), "Suggest a name");
    suggestion().props.onClick(); await flush();
    assert.ok(findElement(ui.render().view, "button", name));
    assert.equal(findElement(ui.render().view, "textarea").props.value, "");
    assert.equal(suggestion().children.join(""), "Another name");
    suggestion().props.onClick(); await flush();
    assert.match(ui.render().error, /retry/i);
    suggestion().props.onClick(); await flush();
    assert.notEqual(attempts[0], attempts[1]);
    assert.equal(attempts[1], attempts[2]);
    assert.ok(findElement(ui.render().view, "button", `${name} Plus`));
    const input = findElement(ui.render().view, "textarea");
    assert.equal(input.props.value, "");
    input.props.onChange({target: {value: `${name} Studio`}});
    assert.equal(findElement(ui.render().view, "textarea").props.value, `${name} Studio`);
    assert.equal(posts(ui).filter(call => call.path.endsWith("/confirm")).length, 0);
  }
});

test("questionnaire rejects empty and oversized answers without sending intake", async () => {
  const ui = await loaded([]);
  const submit = () => findElement(ui.render().view, "form").props.onSubmit({preventDefault() {}});
  submit(); assert.equal(posts(ui).length, 0);
  findElement(ui.render().view, "textarea").props.onChange({target: {value: "a".repeat(200001)}}); submit();
  assert.equal(posts(ui).length, 0); assert.equal(ui.render().error, "too long");
  assert.equal(ui.render().draft.length, 200001);
});

test("voice answers follow the same questionnaire without committing a venture", async () => {
  const ui = await loaded([]);
  ui.setResponse(async (_path, options) => ({ok: true, data: JSON.parse(options!.body).text.includes("Audience:")
    ? {status: "proposal", proposal: proposal()}
    : {status: "proposal", proposal: {...proposal(), id: "audience1", route: "startup", onboarding_question: "Who is it for?"}}}));
  let mic = findElement(ui.render().view, "VoiceMic");
  assert.equal((await mic.props.onVoiceSend("A lesson booking tool", {})).body_md, "Who is it for?");
  assert.equal(posts(ui).length, 1);
  mic = findElement(ui.render().view, "VoiceMic");
  await mic.props.onVoiceSend("Independent tutors", {});
  assert.equal(posts(ui).length, 2);
  assert.equal(JSON.parse(posts(ui)[1].options.body).text, "A lesson booking tool\nAudience: Independent tutors");
  assert.equal(posts(ui).some(call => call.path.endsWith("/confirm")), false);
});

test("a first message with an audience goes straight to the name chip and normal composer across domains", async () => {
  for (const {idea, audience, name} of ventures) {
    const ui = await loaded([]);
    const text = `${name}: ${idea} for ${audience}.`;
    ui.setResponse(async () => ({ok: true, data: {status: "proposal", proposal: {...proposal(), venture_name: name}}}));
    const reply = await findElement(ui.render().view, "VoiceMic").props.onVoiceSend(text, {});
    assert.notEqual(reply.body_md, "Who is it for?");
    assert.equal(posts(ui).length, 1);
    assert.equal(JSON.parse(posts(ui)[0].options.body).text, text);
    assert.equal(findElement(ui.render().view, "input"), undefined);
    assert.equal(findElement(ui.render().view, "textarea").props.maxLength, 48);
    assert.equal(findElement(ui.render().view, "textarea").props.value, "");
    assert.ok(findElement(ui.render().view, "button", name));
    assert.equal(posts(ui).filter(call => call.path.endsWith("/confirm")).length, 0);
  }
});

test("Back preserves an attached idea that already contains an Audience field", async () => {
  const ui = await loaded([]);
  const original = "# Booking tool\nAudience: tutors\nWorkflow: lessons";
  const submit = () => findElement(ui.render().view, "form").props.onSubmit({preventDefault() {}});
  findElement(ui.render().view, "textarea").props.onChange({target: {value: original}}); submit(); await flush();
  await ui.render().back();
  assert.equal(ui.render().draft, original);
});

test("a reloaded clarification with no choices remains the single free-text question", async () => {
  const ui = await loaded([{text: "My idea", proposal: {...proposal("clarify"), message: "What problem does it solve?", choices: []}}]);
  const view = ui.render().view;
  assert.ok(findElement(view, "textarea"));
  assert.equal(findElement(view, "StageConfirmation"), undefined);
  assert.ok(!JSON.stringify(view).includes('"id":"venture-name"'));
  assert.ok(findElement(view, "button"));
  await ui.render().back(); assert.equal(ui.render().draft, "My idea");
});

test("smalltalk reload restores the audience label while retaining the saved clarification and choices", async () => {
  const saved: EntryProposal = {...proposal("clarify"), route: "clarify", next_step: "clarify",
    message: "What would your product do?", choices: ["Example idea"], onboarding_question: "Who is it for?"};
  const ui = await loaded([{text: "hello", proposal: saved}]);
  assert.equal(findElement(ui.render().view, "label").children.join(""), "Who is it for?");
  assert.equal(ui.render().turns.at(-1).text, "Who is it for?");
  assert.deepEqual(Array.from(ui.render().turns.at(-1).proposal.choices), ["Example idea"]);
  assert.equal(ui.render().proposal, null);
  await findElement(ui.render().view, "VoiceMic").props.onVoiceSend("Not sure", {});
  assert.equal(JSON.parse(posts(ui)[0].options.body).text, "hello\nAudience: Not sure");
});

for (const choices of [["Expats looking for regional pickles", "Specialty food shop owners", "Supper-club hosts"],
  ["Night-shift nurses learning languages", "New immigrants studying for exams", "Tour guides learning phrases"]]) {
  for (const state of ["pending", "clarify"] as const) {
    test(`broad audience ${choices[0]} restores ${state} one-tap options and a keep-words path`, async () => {
      const original = "My product\nAudience: everyone";
      const saved: EntryProposal = {...proposal(state), route: state === "pending" ? "startup" : "clarify", next_step: "idea",
        onboarding_choices: choices, onboarding_field: "customer", onboarding_question: "That audience is too broad to reach. Pick a starting group or keep your words."};
      const ui = await loaded([{text: original, proposal: saved}]);
      assert.deepEqual(Array.from(ui.render().turns.at(-1).choices), choices);
      assert.ok(findElement(ui.render().view, "textarea"));
      const all = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(all)];
      const keep = all(ui.render().view).find(element => element.props?.["data-usage-control"] === "onboarding_skip");
      assert.ok(keep);
      const chip = findElement(ui.render().view, "button", choices[0]);
      await chip.props.onClick();
      await flush();
      assert.equal(JSON.parse(posts(ui)[0].options.body).text, original + "\nAudience: Assumed: " + choices[0]);
      const ownWords = await loaded([{text: original, proposal: saved}]);
      const keepWords = all(ownWords.render().view).find(element => element.props?.["data-usage-control"] === "onboarding_skip");
      await keepWords.props.onClick();
      await flush();
      assert.equal(JSON.parse(posts(ownWords)[0].options.body).text, original + "\nAudience: Skip this question");
    });
  }
}

test("a pending startup reload keeps its question and proposal without committing across domains", async () => {
  for (const {idea: original, audience, name} of ventures) {
    const saved: EntryProposal = {...proposal(), venture_name: name, route: "startup", next_step: "idea", onboarding_question: "Who is it for?"};
    const ui = await loaded([{text: original, proposal: saved}]);
    assert.equal(ui.render().turns.at(-1).proposal.route, "startup");
    assert.equal(ui.render().turns.at(-1).proposal.state, "pending");
    assert.equal(ui.render().proposal, null);
    assert.equal(ui.render().turns.at(-1).text, "Who is it for?");
    assert.ok(JSON.stringify(ui.render().view).includes("Who is it for?"));
    assert.ok(findElement(ui.render().view, "textarea"));
    assert.equal(findElement(ui.render().view, "label").children.join(""), "Who is it for?");
    assert.equal(posts(ui).length, 0);
    ui.setResponse(async () => ({ok: true, data: {status: "proposal", proposal: {...saved, onboarding_question: undefined}}}));
    await findElement(ui.render().view, "VoiceMic").props.onVoiceSend(audience, {});
    assert.equal(posts(ui).length, 1);
    assert.equal(posts(ui)[0].path, "/api/entry/chat/preview");
    assert.equal(JSON.parse(posts(ui)[0].options.body).text, `${original}\nAudience: ${audience}`);
    assert.equal(findElement(ui.render().view, "input"), undefined);
    assert.equal(findElement(ui.render().view, "textarea").props.maxLength, 48);
    assert.ok(findElement(ui.render().view, "button", name));
  }
});

test("onboarding defers asset questions and stage confirmation without discarding inferred assets", async () => {
  const pending = { ...proposal(), asset_prefill: { website: "https://example.test", contacts: "", built: "", tried: "" },
    asset_questions: [{ id: "website" as const, question: "Do you have a website or landing page?", recommended: "Paste your website", skip: true }],
    founder_stage: { stage: "page_live" as const, confidence: 0.9, evidence: [], confirmed_by_founder: false, updated_at: "2026-10-08T00:00:00Z", history: [] } };
  const ui = await loaded([{ text: "I already have a website", proposal: pending }]);
  const all = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(all)];
  let view = ui.render().view;
  const website = all(view).find(node => node.props?.id === "asset-website");
  assert.equal(website, undefined);
  assert.equal(findElement(view, "StageConfirmation"), undefined);
  assert.equal(posts(ui).length, 0, "optional metadata does not commit or create a parallel venture");
  await ui.render().confirm();
  const body = JSON.parse(posts(ui).find(call => call.path === "/api/entry/chat/confirm")!.options.body);
  assert.deepEqual(body.assets, { website: "https://example.test", contacts: "", built: "", tried: "" });
  assert.equal(body.founder_stage, undefined, "an inferred stage is never confirmed on the founder's behalf");
  assert.equal(body.proposal_id, pending.id);
});

test("deferred onboarding metadata keeps server URL validation errors visible and permits Back", async () => {
  const pending = { ...proposal(), asset_prefill: { website: "https://example.test", contacts: "", built: "", tried: "" },
    asset_questions: [{ id: "website" as const, question: "Do you have a website?", recommended: "Paste your website", skip: true }],
    founder_stage: { stage: "page_live" as const, confidence: 0.9, evidence: [], confirmed_by_founder: false, updated_at: "2026-10-08T00:00:00Z", history: [] } };
  const ui = await loaded([{ text: "I already have a website", proposal: pending }]);
  const all = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(all)];
  assert.equal(all(ui.render().view).some(node => node.props?.id === "asset-website"), false);
  assert.equal(findElement(ui.render().view, "StageConfirmation"), undefined);
  ui.setResponse(async (path, options) => path === "/api/entry/chat/confirm" ? {ok: false, data: {detail: {error: {code: "website_url_invalid", message: "Share a public website URL"}}}}
    : options?.method === "POST" ? {ok: true, data: {status: "cancelled"}} : {ok: true, data: {proposal: pending}});
  await ui.render().confirm();
  const body = JSON.parse(posts(ui).find(call => call.path === "/api/entry/chat/confirm")!.options.body);
  assert.equal(body.assets.website, "https://example.test"); assert.equal(body.founder_stage, undefined);
  assert.match(ui.render().error, /confirmation outcome is unconfirmed/i);
  assert.equal(ui.render().proposal.id, pending.id);
  await ui.render().back();
  assert.equal(ui.render().proposal, null);
  assert.equal(ui.render().draft, "I already have a website");
});

test("reload keeps pending and unfinished directions, and they block a second preview", async () => {
  for (const state of ["pending", "committing"] as const) {
    const ui = await loaded([{text: "My product", proposal: proposal(state)}]);
    assert.equal(ui.callbacks.proposal.id, "proposal1");
    assert.equal(ui.callbacks.proposal.state, state);
    await ui.callbacks.preview("Create a different direction");
    assert.equal(posts(ui).length, 0);
  }
});

function findElement(view: any, type?: string, label?: string): any {
  if (!view || typeof view !== "object") return undefined;
  if ((!type || view.type === type) && (!label || (view.props?.["aria-label"] ?? view.children?.join("")) === label)) return view;
  return (Array.isArray(view) ? view : view.children || []).map((child: any) => findElement(child, type, label)).find(Boolean);
}

test("onboarding keeps idea-specific segment chips on preview, reload and one-tap selection", async () => {
  const elements = (view: any): any[] => !view || typeof view !== "object" ? [] : [view, ...(Array.isArray(view) ? view : view.children || []).flatMap(elements)];
  const choices = ["Independent gym owners", "Boutique fitness studio owners", "Owners of multi-location gyms"];
  const text = "something for gym owners idk, members keep leaving";
  const saved: EntryProposal = { ...proposal(), route: "startup", next_step: "idea", message: "Got it, a few quick questions.",
    onboarding_question: "Gym owners whose members leave.\n\nWhich group should we focus on first?", onboarding_field: "customer", onboarding_choices: choices };
  const assertChips = async (ui: ReturnType<typeof renderer>) => {
    const view = ui.render().view;
    const buttons = elements(view).filter(node => node.props?.["data-usage-control"] === "onboarding_choice");
    assert.deepEqual(buttons.map(node => node.props["aria-label"]), choices);
    assert.equal(findElement(view, "textarea").props.disabled, false);
    await buttons[0].props.onClick(); await flush();
    assert.equal(JSON.parse(posts(ui).at(-1)!.options.body).text, text + "\nAudience: Assumed: " + choices[0]);
  };
  const resumed = renderer([{text, proposal: saved}]); resumed.render(); await flush(); await assertChips(resumed);
  for (const status of ["proposal", "clarify"]) {
    const ui = renderer([]); ui.render(); await flush();
    ui.setResponse(async () => ({ok: true, data: status === "proposal" ? {status, proposal: saved} : {
      status, id: saved.id, question: saved.message, choices: [], onboarding_question: saved.onboarding_question,
      onboarding_field: saved.onboarding_field, onboarding_choices: choices }}));
    findElement(ui.render().view, "textarea").props.onChange({target: {value: text}});
    findElement(ui.render().view, "form").props.onSubmit({preventDefault() {}}); await flush(); await assertChips(ui);
  }
});

function confirmedRenderer(props: {receipt: EntryCommit; initialDirection?: EntryHistoryItem | null; entryVoiceNotes?: Array<{id: string; body_md: string}>; onStatusChange?: (...args: any[]) => void}) {
  const source = fs.readFileSync(new URL("../app/new/ConfirmedConversation.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("ConfirmedConversation.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const historySource = fs.readFileSync(new URL("../components/chat/useChatHistory.ts", import.meta.url), "utf8");
  const historyTree = ts.createSourceFile("useChatHistory.ts", historySource, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS);
  const historyHook = historyTree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "useChatHistory")!.getText(historyTree).replace(/^export /, "");
  const component = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "ConfirmedConversation")!;
  const statements = [...component.body!.statements];
  const prefix = statements.slice(0, -1).map(node => node.getText(tree)).join("\n");
  const view = (statements.at(-1) as ts.ReturnStatement).expression!.getText(tree);
  const code = ts.transpileModule(`${historyHook}\nglobalThis.render=function(${component.parameters[0].getText(tree)}) {${prefix}\nreturn {send,confirmDirection,discardDirection,receipt,direction,directionQuestion,gtmError,view:${view}};};`,
    {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText;
  const states: any[] = [], refs: {current: any}[] = [], dependencies: any[][] = [];
  let si = 0, ri = 0, ei = 0, effects: (() => void)[] = [];
  const owner = {actor: "actor1", epoch: 1}, original = {...owner};
  const location = {pathname: "/new", search: "?venture=venture1"};
  const current = () => owner.actor === original.actor && owner.epoch === original.epoch && location.search === "?venture=venture1";
  const calls: {path: string; options?: any}[] = [];
  let reloads = 0;
  const chat = {messages: [] as any[], conversationsAvailable: false, contextCurrent: current, reloadHistory: async () => {reloads++;}, sendChat: async () => true};
  const timers: Array<() => void> = [];
  let now = Date.now();
  let respond = async (path: string, _options?: any): Promise<any> => {
    if (path.startsWith("/api/entry/chat/proposals/")) return {ok: true, data: {proposal: props.initialDirection?.proposal}};
    if (path === "/api/entry/chat/confirm") return {ok: true, data: {...committed(), route: "personal_build", next_step: "build",
      action: {kind: "build_review", venture_id: "venture1", href: "/v/venture1/idea"}}};
    return {ok: true, data: null};
  };
  const context = vm.createContext({HOME_NAVIGATION_EVENT: "supymem:home-navigation", ASK_EVENT: "supymem:ask", JSON, Error, crypto, Date: {now: () => now}, ideaConversationActivity, encodeURIComponent, committedEntryHref, continueSavedEntry, entryConfirmationError, entryConfirmationFailure, entryReviewLabel, resumableEntry,
    founderProductContext: (value: string) => value, gtmSendMode: () => "general", useVentureChat: () => chat, isLocalConsole: () => false,
    FounderUIContext: {value: "classic"}, useContext: (context: {value: string}) => context.value,
    styles: new Proxy({}, {get: (_, key) => key}), Link: "Link", ChatDock: "ChatDock", ErrorNotice: "ErrorNotice",
    CustomEvent: class { type: string; detail: unknown; constructor(type: string, init?: {detail?: unknown}) { this.type = type; this.detail = init?.detail; } },
    window: {location, addEventListener() {}, removeEventListener() {}, dispatchEvent() { return true; }, setInterval: (callback: () => void) => {timers.push(callback); return timers.length;}, clearInterval: () => {}},
    React: {createElement: (type: any, props: any, ...children: any[]) => ({type, props, children})},
    api: (path: string, options?: any) => {calls.push({path, options}); return respond(path, options);},
    useRef: (value: any) => refs[ri++] ||= {current: value},
    useState: (value: any) => {const index = si++; if (!(index in states)) states[index] = value;
      return [states[index], (next: any) => {states[index] = typeof next === "function" ? next(states[index]) : next;}];},
    useEffect: (effect: () => void, deps: any[]) => {const index = ei++, old = dependencies[index];
      if (!old || deps.some((value, i) => !Object.is(value, old[i]))) {dependencies[index] = deps; effects.push(effect);}},
  });
  vm.runInContext(code, context);
  function render() {
    si = ri = ei = 0; effects = [];
    const result = context.render(props);
    for (const effect of effects) effect();
    return result;
  }
  return {render, owner, location, calls, chat, timers, advance: (milliseconds: number) => {now += milliseconds;}, now,
    reloads: () => reloads, setResponse: (fn: typeof respond) => {respond = fn;}};
}

test("confirmed startup updates reassuring copy at 45 seconds and stays working", () => {
  const statuses: any[] = [];
  const child = confirmedRenderer({receipt: {...committed(), route: "startup", next_step: "idea",
    action: {kind: "idea", venture_id: "venture1", href: "/v/venture1/idea"}},
    onStatusChange: (_venture, status) => statuses.push(status)});
  child.chat.messages = [{id: "pending", role: "assistant", kind: "idea_clarification_pending", created_at: new Date(child.now).toISOString()}];
  child.render();
  assert.equal(statuses.at(-1).activity, "Preparing your next question");
  child.advance(44_999); child.timers.forEach(timer => timer()); child.render();
  assert.equal(statuses.at(-1).activity, "Preparing your next question");
  child.advance(1); child.timers.forEach(timer => timer()); child.render();
  assert.equal(statuses.at(-1).activity, "Still working on a good question. This can take up to 3 minutes.");
  assert.equal(statuses.at(-1).state, "working");
  child.chat.messages.push({id: "ready", role: "assistant", kind: "idea_clarification_ready"});
  child.render();
  assert.equal(statuses.at(-1).activity, "Review your saved idea");
  assert.equal(statuses.at(-1).state, "needs_you");
});

const originalCommit = (): EntryHistoryItem => ({text: "My product", proposal: {...proposal("committed"), target_venture_id: "venture1", receipt: committed()}});
const reroute = (state: EntryProposal["state"] = "pending"): EntryHistoryItem => ({text: "Use the original document to build this for myself", proposal: {
  ...proposal(state), id: "reroute1", target_venture_id: "venture1", route: state === "clarify" ? "clarify" : "personal_build",
  intake_path: "personal_build", next_step: state === "clarify" ? "clarify" : "build", message: state === "clarify" ? "Who will use this?" : "Review your personal build",
  choices: state === "clarify" ? ["Just me", "Customers"] : undefined,
}});

test("expired confirmed directions resend their exact targeted answer and return to review", async () => {
  const {child} = await reloadConfirmed([originalCommit(), reroute()]);
  const refreshed = {...reroute().proposal, id: "refreshed-direction"};
  child.setResponse(async (path, options) => {
    if (path === "/api/entry/chat/confirm") return {ok: false, data: {error: {code: "proposal_expired"}}};
    if (path === "/api/entry/chat/preview") {
      const body = JSON.parse(options.body);
      assert.equal(body.text, reroute().text); assert.equal(body.target_venture_id, "venture1"); assert.equal(body.use_original, true);
      return {ok: true, data: {status: "proposal", proposal: refreshed}};
    }
    return {ok: true, data: {proposal: reroute().proposal}};
  });
  await child.render().confirmDirection();
  assert.equal(child.render().direction.id, refreshed.id); assert.equal(child.render().gtmError, "");
  assert.equal(child.calls.filter(call => call.path.endsWith("/confirm")).length, 1);
  assert.equal(child.calls.filter(call => call.path.endsWith("/preview")).length, 1);
  assert.equal(child.render().receipt.route, "gtm");
});

async function reloadConfirmed(items: EntryHistoryItem[]) {
  const page = renderer(items); page.location.search = "?venture=venture1";
  page.render("venture1"); await flush();
  const props = findElement(page.render("venture1").view, "ConfirmedConversation")?.props;
  assert.ok(props, "saved receipt should restore the confirmed conversation");
  const child = confirmedRenderer(props); child.render(); await flush(); child.calls.length = 0;
  return {page, child, props};
}

test("actual scoped history reload restores the newest review, unfinished confirmation, and clarification cards", async () => {
  for (const state of ["pending", "committing", "clarify"] as const) {
    const {child} = await reloadConfirmed([originalCommit(), reroute(state)]);
    const result = child.render();
    assert.ok(findElement(result.view, "ChatDock").props.messages.some((message: any) => message.id.startsWith(state === "clarify" ? "entry-direction:" : "entry-direction-proposal:")));
    if (state === "clarify") {
      assert.equal(result.directionQuestion.text, reroute(state).text);
      assert.deepEqual(Array.from(result.directionQuestion.choices), ["Just me", "Customers"]);
    } else {
      assert.equal(result.direction.id, "reroute1");
      await result.send("Use the full document");
      assert.equal(child.calls.length, 0, "saved review must block a second preview");
      const actions = findElement(result.view, "ChatDock").props.messages.at(-1).actions;
      assert.equal(actions.some((action: any) => action.action === "entry.direction.back"), state !== "committing");
    }
  }
});

test("newer committed/cancelled history and foreign targets never resurrect an older reroute", async () => {
  for (const last of [originalCommit(), reroute("cancelled"), {...reroute(), proposal: {...reroute().proposal, target_venture_id: "other"}}]) {
    const {child} = await reloadConfirmed([originalCommit(), reroute(), last]);
    assert.equal(child.render().direction, null);
    assert.equal(child.render().directionQuestion, null);
  }
});

test("a restored reroute reads its same ID before one confirmation and follows the same venture receipt", async () => {
  for (const state of ["pending", "committing"] as const) {
    const {child} = await reloadConfirmed([originalCommit(), reroute(state)]);
    const result = child.render(); await Promise.all([result.confirmDirection(), result.confirmDirection()]);
    assert.deepEqual(child.calls.map(call => [call.path, call.options?.method || "GET"]), [
      ["/api/entry/chat/proposals/reroute1", "GET"], ["/api/entry/chat/confirm", "POST"],
    ]);
    assert.deepEqual(JSON.parse(child.calls[1].options.body), {proposal_id: "reroute1"});
    assert.equal(child.render().receipt.route, "personal_build"); assert.equal(child.render().direction, null);
    assert.equal(child.reloads(), 1);
  }
});

test("restored pending cancellation and clarification preserve their original request semantics", async () => {
  const {child} = await reloadConfirmed([originalCommit(), reroute()]);
  child.setResponse(async () => ({ok: true, data: {status: "cancelled"}}));
  await child.render().discardDirection();
  assert.equal(child.calls[0].path, "/api/entry/chat/proposals/reroute1/cancel"); assert.equal(child.render().direction, null);
  const {child: clarifying} = await reloadConfirmed([originalCommit(), reroute("clarify")]);
  clarifying.setResponse(async () => ({ok: true, data: {status: "proposal", proposal: reroute().proposal}}));
  await clarifying.render().send("Just me");
  assert.deepEqual(JSON.parse(clarifying.calls[0].options.body), {
    text: `${reroute("clarify").text}\nClarification: Just me`, target_venture_id: "venture1", use_original: true,
    request_id: JSON.parse(clarifying.calls[0].options.body).request_id,
  });
  assert.equal(clarifying.render().directionQuestion, null); assert.equal(clarifying.render().direction.id, "reroute1");
});

test("restored committing and retired actor/epoch/query callbacks cannot cancel or confirm new work", async () => {
  const {child: committing} = await reloadConfirmed([originalCommit(), reroute("committing")]);
  await committing.render().discardDirection(); assert.equal(committing.calls.length, 0);
  for (const retire of [(ui: any) => {ui.owner.actor = "actor2";}, (ui: any) => {ui.owner.epoch++;}, (ui: any) => {ui.location.search = "?venture=other";}]) {
    const {child} = await reloadConfirmed([originalCommit(), reroute()]); const callbacks = child.render(); retire(child);
    await callbacks.confirmDirection(); await callbacks.discardDirection(); await callbacks.send("Use the original document");
    assert.equal(child.calls.length, 0);
  }
  const {child} = await reloadConfirmed([originalCommit(), reroute()]), reading = deferred<any>();
  child.setResponse(() => reading.promise); const confirming = child.render().confirmDirection();
  child.owner.epoch++; reading.resolve({ok: true, data: {proposal: reroute().proposal}}); await confirming;
  assert.equal(child.calls.length, 1, "retired fresh read must never send its confirmation");
});

test("restored confirmations replay committed receipts and retain uncertain or mismatched reviews", async () => {
  const {child: replay} = await reloadConfirmed([originalCommit(), reroute("committing")]);
  const receipt = {...committed(), route: "personal_build" as const, next_step: "build",
    action: {kind: "build_review" as const, venture_id: "venture1", href: "/v/venture1/idea"}};
  replay.setResponse(async () => ({ok: true, data: {proposal: {...reroute().proposal, state: "committed", receipt}}}));
  await replay.render().confirmDirection(); assert.equal(replay.calls.length, 1);
  assert.equal(replay.render().direction, null); assert.equal(replay.render().receipt.route, "personal_build");
  for (const changed of [{id: "other"}, {target_venture_id: "other"}]) {
    const {child} = await reloadConfirmed([originalCommit(), reroute()]);
    child.setResponse(async () => ({ok: true, data: {proposal: {...reroute().proposal, ...changed}}}));
    await child.render().confirmDirection(); assert.equal(child.calls.length, 1);
    assert.equal(child.render().direction.id, "reroute1"); assert.match(child.render().gtmError, /could not be verified/);
  }
  const {child} = await reloadConfirmed([originalCommit(), reroute("committing")]);
  child.setResponse(async path => path === "/api/entry/chat/confirm" ? {ok: false, status: 500, data: {detail: "private"}}
    : {ok: true, data: {proposal: reroute("committing").proposal}});
  await child.render().confirmDirection(); assert.equal(child.calls.length, 2);
  assert.equal(child.render().direction.state, "committing"); assert.match(child.render().gtmError, /unconfirmed/);
});

test("late scoped history cannot expose a restored review after an A to B to A session retirement", async () => {
  const page = renderer(), history = deferred<any>(); page.location.search = "?venture=venture1";
  page.setResponse(() => history.promise); page.render("venture1"); await flush();
  page.owner.actor = "actor2"; page.owner.epoch++;
  page.owner.actor = "actor1"; page.owner.epoch++;
  history.resolve({ok: true, data: {items: [originalCommit(), reroute()]}}); await flush();
  page.setResponse(async () => ({ok: true, data: {items: []}}));
  assert.equal(findElement(page.render("venture1").view, "ConfirmedConversation"), undefined);
  await flush(); assert.equal(findElement(page.render("venture1").view, "ConfirmedConversation"), undefined);
});

test("explicit continuation reads the same proposal before one confirmation POST", async () => {
  const ui = await loaded(); ui.calls.length = 0;
  const first = ui.callbacks.confirm(); const duplicate = ui.callbacks.confirm(); await Promise.all([first, duplicate]);
  assert.deepEqual(ui.calls.map(call => [call.path, call.options?.method || "GET"]), [
    ["/api/entry/chat/proposals/proposal1", "GET"], ["/api/entry/chat/confirm", "POST"],
  ]);
  assert.deepEqual(JSON.parse(posts(ui)[0].options.body), {proposal_id: "proposal1", name: "Harbor", evidence_ids: []});
  assert.deepEqual(ui.navigated, ["/new?venture=venture1"]);
});

test("a pending new venture without a chosen name cannot read or confirm work", async () => {
  const ui = await loaded([{text: "My product", proposal: {...proposal(), venture_name: ""}}]);
  ui.calls.length = 0; await ui.callbacks.confirm();
  assert.equal(ui.calls.length, 0); assert.equal(ui.navigated.length, 0);
  assert.match(ui.render().error, /Give your venture a name/);
});

test("request ID failure restores the draft, clears busy, and leaves Continue retryable", async () => {
  let available = false;
  const ui = renderer([], {randomUUID: () => {
    if (!available) throw new Error("Could not prepare this request. Try again.");
    return crypto.randomUUID();
  }} as Crypto);
  ui.render(); await flush();
  findElement(ui.render().view, "textarea").props.onChange({target: {value: "hello"}});
  await ui.render().preview("hello");
  const state = ui.render();
  assert.equal(state.busy, false);
  assert.equal(state.draft, "hello");
  assert.match(state.error, /Couldn.t send|Try again/);
  assert.equal(posts(ui).length, 0);
  available = true;
  await ui.render().preview("hello");
  assert.equal(posts(ui).length, 1);
  assert.equal(posts(ui)[0].path, "/api/entry/chat/preview");
});

test("typing into an already prepared review explains Back without sending another preview", async () => {
  const ui = await loaded(); ui.calls.length = 0;
  await ui.render().preview("A changed idea");
  assert.match(ui.render().error, /Choose Back/);
  assert.equal(posts(ui).length, 0);
});

test("a fresh committed outcome navigates from its receipt without another POST", async () => {
  const ui = await loaded();
  ui.setResponse(async () => ({ok: true, data: {proposal: {...proposal("committed"), receipt: committed()}}}));
  ui.calls.length = 0; await ui.callbacks.confirm();
  assert.equal(ui.calls.length, 1); assert.equal(posts(ui).length, 0);
  assert.deepEqual(ui.navigated, ["/new?venture=venture1"]);
});

test("retained callbacks refuse actor, epoch and query retirement before React rerenders", async () => {
  for (const retire of [(ui: any) => {ui.owner.actor = "actor2";}, (ui: any) => {ui.owner.epoch++;},
    (ui: any) => {ui.location.search = "?venture=other";}]) {
    for (const name of ["preview", "confirm", "back"]) {
      const ui = await loaded(name === "preview" ? [] : undefined); ui.calls.length = 0; retire(ui);
      await ui.callbacks[name]("Old draft");
      assert.equal(ui.calls.length, 0, name); assert.equal(ui.navigated.length, 0);
    }
  }
});

test("session refresh and fresh proposal reads cannot adopt a changed owner", async () => {
  const ui = await loaded([]), refreshing = deferred<any>(); ui.setSession(() => refreshing.promise);
  ui.calls.length = 0; const preview = ui.callbacks.preview("My original draft");
  ui.owner.epoch++; refreshing.resolve({ok: true}); await preview;
  assert.equal(posts(ui).length, 0);
  const other = await loaded(), reading = deferred<any>(); other.setResponse(() => reading.promise);
  other.calls.length = 0; const confirming = other.callbacks.confirm();
  other.owner.actor = "actor2"; reading.resolve({ok: true, data: {proposal: proposal()}}); await confirming;
  assert.equal(posts(other).length, 0); assert.equal(other.navigated.length, 0);
});

test("an unknown confirmation keeps the saved direction and never creates or retries work", async () => {
  const ui = await loaded(); ui.calls.length = 0;
  ui.setResponse(async (path) => path === "/api/entry/chat/confirm"
    ? {ok: false, status: 500, data: {detail: "private diagnostic"}}
    : {ok: true, data: {proposal: proposal("committing")}});
  await ui.callbacks.confirm(); const state = ui.render();
  assert.equal(state.proposal.id, "proposal1"); assert.match(state.error, /unconfirmed/);
  assert.doesNotMatch(state.error, /private diagnostic/);
  assert.equal(posts(ui).length, 1); assert.equal(ui.navigated.length, 0);
  await state.preview("Another draft"); assert.equal(posts(ui).length, 1);
});

test("Back cannot unlock an outcome that became committing; cancellation checks saved state atomically", async () => {
  const ui = await loaded(); ui.calls.length = 0;
  ui.setResponse(async () => ({ok: true, data: {proposal: proposal("committing")}}));
  await ui.callbacks.back(); const state = ui.render();
  assert.equal(state.proposal.state, "committing"); assert.equal(posts(ui).length, 1);
  assert.equal(posts(ui)[0].path, "/api/entry/chat/proposals/proposal1/cancel");
  await state.preview("Another draft"); assert.equal(posts(ui).length, 1);
});

test("a mismatched fresh proposal is never confirmed or used for navigation", async () => {
  const ui = await loaded(); ui.calls.length = 0;
  ui.setResponse(async () => ({ok: true, data: {proposal: {...proposal(), id: "other"}}}));
  await ui.callbacks.confirm(); assert.equal(posts(ui).length, 0); assert.equal(ui.navigated.length, 0);
});

test("confirming a venture carries its verified receipt across the query remount", async () => {
  const ui = await loaded();
  await ui.callbacks.confirm();
  assert.ok(ui.navigated.includes("/new?venture=venture1"));
  const history = deferred<any>(); ui.setResponse(() => history.promise);
  ui.location.search = "?venture=venture1";
  const immediate = ui.remount("venture1");
  assert.equal(immediate.activeReceipt?.venture_id, "venture1");
  assert.ok(findElement(immediate.view, "section", "Opening saved conversation"));
  assert.equal(findElement(immediate.view, "ConfirmedConversation"), undefined);
  assert.equal(findElement(immediate.view, "WelcomeIntro"), undefined);
  assert.ok(findElement(ui.render("venture1").view, "section", "Opening saved conversation"));
  history.resolve({ok: true, data: {items: []}}); await flush();
  assert.equal(ui.render("venture1").activeReceipt?.venture_id, "venture1");
});

test("target venture reload shows recovery, while receipt handoff cannot cross account or venture", async () => {
  for (const retire of ["account", "epoch", "venture"] as const) {
    const ui = await loaded(); await ui.callbacks.confirm();
    if (retire === "account") ui.owner.actor = "actor2";
    if (retire === "epoch") ui.owner.epoch++;
    const target = retire === "venture" ? "venture2" : "venture1";
    ui.location.search = `?venture=${target}`;
    const history = deferred<any>(); ui.setResponse(() => history.promise);
    const loading = ui.remount(target);
    assert.equal(loading.activeReceipt, null);
    assert.equal(loading.historyLoaded, false);
    assert.ok(findElement(loading.view, "section", "Opening saved venture"));
    assert.equal(findElement(loading.view, "WelcomeIntro"), undefined);
    history.resolve({ok: false}); await flush();
    const recovered = ui.render(target);
    assert.equal(recovered.historyLoaded, true);
    assert.match(recovered.error, /saved conversation could not be loaded/);
    assert.equal(findElement(recovered.view, "WelcomeIntro"), undefined);
  }
});

test("handoff waits for authoritative direction before mounting the editable conversation", async () => {
  for (const state of ["pending", "committing", "clarify", "committed"] as const) {
    const ui = await loaded(); await ui.callbacks.confirm();
    const history = deferred<any>(); ui.setResponse(() => history.promise); ui.location.search = "?venture=venture1";
    const immediate = ui.remount("venture1");
    assert.ok(immediate.activeReceipt);
    assert.equal(findElement(immediate.view, "ConfirmedConversation"), undefined, "do not mount editable child with absent saved props");
    const latestReceipt = {...committed(), route: "personal_build" as const, next_step: "build",
      action: {kind: "build_review" as const, venture_id: "venture1", href: "/v/venture1/idea"}};
    const latest = state === "committed" ? {...reroute(state), proposal: {...reroute(state).proposal, receipt: latestReceipt}} : reroute(state);
    history.resolve({ok: true, data: {items: [originalCommit(), latest]}}); await flush();
    const settled = ui.render("venture1"), mounted = findElement(settled.view, "ConfirmedConversation");
    assert.ok(mounted);
    if (state === "committed") {
      assert.equal(mounted.props.receipt.route, "personal_build");
      assert.equal(mounted.props.initialDirection, null);
    } else {
      assert.equal(mounted.props.initialDirection?.proposal.state, state);
      const child = confirmedRenderer(mounted.props), rendered = child.render();
      assert.ok(findElement(rendered.view, "ChatDock").props.messages.some((message: any) => message.id.startsWith(state === "clarify" ? "entry-direction:" : "entry-direction-proposal:")));
    }
  }
});
test("failed handoff history cannot open an editable conversation from an unverified saved direction", async () => {
  const ui = await loaded(); await ui.callbacks.confirm(); ui.location.search = "?venture=venture1";
  ui.setResponse(async () => ({ok: false})); ui.remount("venture1"); await flush();
  const failed = ui.render("venture1");
  assert.ok(failed.activeReceipt);
  assert.equal(findElement(failed.view, "ConfirmedConversation"), undefined);
  assert.ok(findElement(failed.view, "section", "Opening saved conversation"));
});

test("a stalled saved-history read reaches a retry card and its late response cannot replace it", async () => {
  const ui = await loaded(); await ui.callbacks.confirm(); ui.location.search = "?venture=venture1";
  const stalled = deferred<any>(); ui.setResponse(() => stalled.promise); ui.remount("venture1");
  ui.expireHistory(); const expired = ui.render("venture1");
  assert.equal(expired.historyLoaded, true); assert.match(expired.error, /taking longer than expected/);
  assert.equal(findElement(expired.view, "ConfirmedConversation"), undefined);
  stalled.resolve({ok: true, data: {items: [originalCommit(), reroute()]}}); await flush();
  assert.equal(findElement(ui.render("venture1").view, "ConfirmedConversation"), undefined);
});


/** Drive the actual page's query/session wrapper separately from its child. */
function entryPage(actorAtMount: string | null = "actor1", initialTarget?: string) {
  const source = fs.readFileSync(new URL("../app/new/page.tsx", import.meta.url), "utf8");
  const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const declaration = tree.statements.find((node): node is ts.FunctionDeclaration => ts.isFunctionDeclaration(node) && node.name?.text === "NewVenturePage")!;
  const code = ts.transpileModule(`let committedHandoff = null; ${declaration.getText(tree).replace("export default ", "")} globalThis.render = NewVenturePage;`,
    {compilerOptions: {target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.React}}).outputText;
  const hooks: any[] = [], listeners = new Set<() => void>(), navigated: string[] = [];
  let cursor = 0, effects: (() => void)[] = [], subject = actorAtMount, epoch = 1, target = initialTarget;
  const context = vm.createContext({
    NewVentureConversation: "NewVentureConversation", verifiedSessionSubject: () => subject, verifiedSessionEpoch: () => epoch,
    useRouter: () => ({replace: (path: string) => {navigated.push(path);}}), useSearchParams: () => ({get: (key: string) => key === "venture" ? target : null}),
    window: {addEventListener: (_event: string, handler: () => void) => listeners.add(handler), removeEventListener: (_event: string, handler: () => void) => listeners.delete(handler)},
    React: {createElement: (type: any, props: any) => ({type, props})},
    useRef(initial: any) {const i = cursor++; return hooks[i] ||= {current: initial};},
    useState(initial: any) {const i = cursor++; if (!(i in hooks)) hooks[i] = typeof initial === "function" ? initial() : initial;
      return [hooks[i], (value: any) => {hooks[i] = typeof value === "function" ? value(hooks[i]) : value;}];},
    useEffect(effect: () => void, deps: any[]) {const i = cursor++, old = hooks[i];
      if (!old || deps.some((value, index) => !Object.is(value, old[index]))) {hooks[i] = deps; effects.push(effect);}},
  });
  vm.runInContext(code, context);
  const render = () => {cursor = 0; effects = []; const result = context.render(); effects.forEach(run => run()); return result;};
  return {render, navigated, route: (value?: string) => {target = value;},
    verify(value: string | null) {subject = value; epoch++; listeners.forEach(handler => handler());}};
}

test("verified account changes retire an existing intake target before its URL replacement settles", () => {
  const page = entryPage("actor1", "venture1");
  assert.equal(page.render().props.targetVentureId, "venture1");
  page.verify(null);  // Native sign-out invalidates A before B verifies.
  assert.equal(page.render().props.targetVentureId, undefined);
  assert.deepEqual(page.navigated, ["/new"]);
  page.verify("actor2");
  assert.equal(page.render().props.targetVentureId, undefined);
  page.route(); page.render();
  page.verify("actor1");
  assert.equal(page.render().props.targetVentureId, undefined);
  page.route("venture2");
  assert.equal(page.render().props.targetVentureId, "venture2", "a new same-owner venture query remains usable");
});
test("initial unverified deep links and ordinary same-owner refresh keep the intake target", () => {
  const page = entryPage(null, "venture1");
  const initial = page.render();
  assert.equal(initial.props.targetVentureId, "venture1");
  page.verify("actor1");
  assert.equal(page.render().props.key, initial.props.key, "first verification must not remount the composer");
  assert.equal(page.render().props.targetVentureId, "venture1");
  page.verify("actor1");
  assert.equal(page.render().props.key, initial.props.key, "same-owner refresh must not remount the composer");
  assert.equal(page.render().props.targetVentureId, "venture1");
  assert.deepEqual(page.navigated, []);
});
test("direct actor swaps also retire the target, while fresh intake stays fresh", () => {
  const targeted = entryPage("actor1", "venture1"); targeted.render(); targeted.verify("actor2");
  assert.equal(targeted.render().props.targetVentureId, undefined); assert.deepEqual(targeted.navigated, ["/new"]);
  const fresh = entryPage("actor1"); fresh.render(); fresh.verify("actor2");
  assert.equal(fresh.render().props.targetVentureId, undefined); assert.deepEqual(fresh.navigated, []);
});


test("complete and partial saved entry voice notes survive reload in their server partitions without duplicate IDs", async () => {
  const ui=renderer([]);
  const freshId="entry-voice-note_"+"a".repeat(64),earlierId="entry-voice-note_"+"b".repeat(64);
  const notes=[{id:freshId,body_md:"Complete founder answer. Unconfirmed speech, edit before sending: do not publish until",partition:"fresh",needs_edit:true,read_only:true,target_venture_id:null},
    {id:earlierId,body_md:"Earlier voice answer",partition:"earlier",needs_edit:false,read_only:true,target_venture_id:null}];
  ui.setResponse(async()=>({ok:true,data:{items:[],voice_notes:notes}}));ui.render();await flush();
  let current=ui.render();assert.equal(current.turns.filter((turn:any)=>turn.id===freshId).length,1);
  assert.equal(current.earlier.filter((turn:any)=>turn.id===earlierId).length,1);
  const mic=findElement(current.view,"VoiceMic");mic.props.onSummary(notes[0].body_md,freshId);mic.props.onSummary(notes[0].body_md,freshId);
  current=ui.render();assert.equal(current.turns.filter((turn:any)=>turn.id===freshId).length,1);
  ui.remount();await flush();current=ui.render();assert.equal(current.turns.filter((turn:any)=>turn.id===freshId).length,1);
  assert.ok(current.turns[0].text.includes("Unconfirmed speech"));
  assert.equal(posts(ui).length,0,"restoring saved notes must not preview or confirm them");
});

test("late entry voice history and saved-note callbacks cannot reveal notes after an account switch", async () => {
  const ui=await loaded([]),oldMic=findElement(ui.render().view,"VoiceMic");
  const read=deferred<any>();
  ui.setResponse(async()=>read.promise);const loading=ui.render().loadHistory();await flush();
  ui.owner.actor="actor2";ui.owner.epoch++;ui.setResponse(async()=>({ok:true,data:{items:[],voice_notes:[]}}));ui.render();await flush();
  oldMic.props.onSummary("Private actor1 voice answer","entry-voice-note_"+"c".repeat(64));
  read.resolve({ok:true,data:{items:[],voice_notes:[{id:"entry-voice-note_"+"c".repeat(64),body_md:"Private actor1 voice answer",partition:"fresh",target_venture_id:null}]}});await loading;await flush();
  assert.equal(ui.render().turns.some((turn:any)=>turn.text.includes("Private actor1")),false);
});

test("entry voice callbacks and late history retain their original venture query before remount", async () => {
  const ui=await loaded([]),state=ui.render(),read=deferred<any>();
  ui.setResponse(async()=>read.promise);const loading=state.loadHistory();await flush();
  ui.location.search="?venture=venture2";
  const mic=findElement(state.view,"VoiceMic"),noteId="entry-voice-note_"+"c".repeat(64);
  mic.props.onSummary("Earlier query voice answer",noteId);
  assert.equal(await mic.props.onVoiceSend("Earlier query voice answer",{idempotencyKey:"voice_original_key"}),null);
  read.resolve({ok:true,data:{items:[],voice_notes:[{id:noteId,body_md:"Earlier query voice answer",partition:"fresh",target_venture_id:null,read_only:true}]}});
  await loading;await flush();
  assert.equal(ui.render().turns.some((turn:any)=>turn.id===noteId),false);assert.equal(posts(ui).length,0);
});

test("Back preserves a durable correction note while cancelling only the reviewed proposal", async () => {
  const noteId="entry-voice-note_"+"d".repeat(64);const ui=renderer([{text:"A tool for teachers",proposal:proposal()}]);
  ui.setResponse(async(path)=>path.includes("history")?{ok:true,data:{items:[{text:"A tool for teachers",proposal:proposal()}],voice_notes:[
    {id:noteId,body_md:"Actually, the audience is architects",partition:"fresh",needs_edit:true,target_venture_id:null}]} }:{ok:true,data:{status:"cancelled"}});
  ui.render();await flush();let current=ui.render();assert.equal(current.proposal.id,"proposal1");
  const mic=findElement(current.view,"VoiceMic");const reply=await mic.props.onVoiceSend("Actually, the audience is architects",{idempotencyKey:"voice_ane_test_input_1"});
  assert.equal(reply.admitted,false);assert.equal(posts(ui).length,0);
  await ui.render().back();current=ui.render();assert.equal(current.proposal,null);
  assert.equal(current.turns.filter((turn:any)=>turn.id===noteId).length,1);assert.ok(current.turns.find((turn:any)=>turn.id===noteId).text.includes("architects"));
  assert.equal(posts(ui).filter(call=>call.path.includes("confirm")).length,0);
});


test("an older entry-history snapshot cannot erase a newly confirmed durable voice note", async () => {
  const ui=await loaded([]),read=deferred<any>();
  ui.setResponse(async()=>read.promise);const state=ui.render(),loading=state.loadHistory();await flush();
  const noteId="entry-voice-note_"+"e".repeat(64);findElement(state.view,"VoiceMic").props.onSummary("You: A tool for architects",noteId);
  read.resolve({ok:true,data:{items:[],voice_notes:[]}});await loading;await flush();
  assert.equal(ui.render().turns.filter((turn:any)=>turn.id===noteId).length,1);
});

test("entry voice history excludes unrelated target notes and note-only history has no dead Back button", async () => {
  const ui=renderer([]);
  ui.setResponse(async()=>({ok:true,data:{items:[],voice_notes:[
    {id:"entry-voice-note_"+"f".repeat(64),body_md:"Owned unconfirmed note",partition:"fresh",target_venture_id:null,needs_edit:true},
    {id:"entry-voice-note_"+"a".repeat(64),body_md:"Another venture note",partition:"fresh",target_venture_id:"ven_other",needs_edit:true},
  ]}}));ui.render();await flush();const state=ui.render();
  assert.equal(state.turns.length,1);assert.equal(state.turns[0].text,"Owned unconfirmed note");
  const hasBack=(view:any):boolean=> !!view&&typeof view==="object"&&(view.props?.["data-usage-control"]==="onboarding_back"||(Array.isArray(view)?view:view.children||[]).some(hasBack));
  assert.equal(hasBack(state.view),false);
});

test("targeted entry notes remain visible after receipt reload and Back without taking question authority", async () => {
  const page=renderer([]),firstId="entry-voice-note_"+"a".repeat(64),secondId="entry-voice-note_"+"b".repeat(64);
  const notes=[{id:firstId,body_md:"You: Original audience",partition:"fresh",target_venture_id:"venture1",pending:true,needs_edit:true,read_only:true},
    {id:secondId,body_md:"You: Latest audience\nSupymem: Review the saved direction",partition:"fresh",target_venture_id:"venture1",needs_edit:true,read_only:true},
    {id:firstId,body_md:"You: Original audience",partition:"fresh",target_venture_id:"venture1",needs_edit:true,read_only:true},
    {id:"entry-voice-note_"+"c".repeat(64),body_md:"Other venture note",partition:"fresh",target_venture_id:"other"},
    {id:"entry-voice-note_"+"d".repeat(64),body_md:"Fresh intake note",partition:"fresh",target_venture_id:null}];
  page.location.search="?venture=venture1";
  page.setResponse(async()=>({ok:true,data:{items:[originalCommit(),reroute("clarify")],voice_notes:notes}}));
  page.render("venture1");await flush();
  const props=findElement(page.render("venture1").view,"ConfirmedConversation").props;
  const child=confirmedRenderer(props);child.render();await flush();
  const savedNotes=()=>findElement(child.render().view,"ChatDock").props.messages.filter((message:any)=>message.id.startsWith("entry-voice-note_"));
  assert.equal(findElement(child.render().view,"section","Saved entry voice notes"),undefined,"notes must not occupy a pinned panel above the active question");
  const ids=()=>savedNotes().map((item:any)=>item.id);
  assert.deepEqual(Array.from(ids()),[firstId,secondId]);
  assert.ok(JSON.stringify(savedNotes()).includes("Latest audience"));
  assert.equal(JSON.stringify(savedNotes()).includes("Other venture note"),false);assert.equal(JSON.stringify(savedNotes()).includes("Fresh intake note"),false);
  const dock=findElement(child.render().view,"ChatDock");
  assert.equal(dock.props.messages.length,3);assert.ok(dock.props.messages.at(-1).id.startsWith("entry-direction:"));
  assert.deepEqual(Array.from(dock.props.messages.at(-1).actions.map((action:any)=>action.text)),["Just me","Customers"]);
  assert.equal(posts(page).length,0);assert.equal(child.calls.some(call=>call.options?.method==="POST"),false);
  await dock.props.onQuestionBack();assert.deepEqual(Array.from(ids()),[firstId,secondId]);
  assert.deepEqual(child.calls.filter(call=>call.options?.method==="POST").map(call=>call.path),["/api/entry/chat/proposals/reroute1/cancel"]);
  child.location.search="?venture=other";assert.equal(savedNotes().length,0,"retired query cannot keep showing private notes");
  child.location.search="?venture=venture1";child.owner.epoch++;assert.equal(savedNotes().length,0,"retired session cannot keep showing private notes");
  child.owner.epoch--;child.owner.actor="actor2";assert.equal(savedNotes().length,0,"another actor cannot inherit the saved notes");
});
