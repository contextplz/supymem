const assert = require("node:assert/strict"), test = require("node:test"), vm = require("node:vm");
const { readFileSync } = require("node:fs"), ts = require("typescript");
const jsx = (type, props) => ({ type, props: props || {} });
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value?.props ? [value, ...nodes(value.props.children)] : [];
const text = value => Array.isArray(value) ? value.map(text).join("") : value?.props ? text(value.props.children) : value == null ? "" : String(value);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("chat sender review binds the exact batch and blocks old confirmations before any sender change, failure or retirement", async () => {
  const code = ts.transpileModule(readFileSync(`${__dirname}/ChatOutreachReview.tsx`, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  for (const [workspaceId, domain] of [["workspace_telemetry", "telemetry.example"], ["workspace_swimming", "swimming.example"]]) {
    const ready = [], calls = [], slots = [], effects = [], exports = {}, ventureId = `${workspaceId}_venture`;
    let cursor = 0, owned = true, failure = false;
    const draft = { doc_id: "draft", revision: "a".repeat(64), state: "draft", review_state: "approved", mailbox_id: "first", sender: { mailbox_id: "first", email: `you@${domain}` }, body: "Source disclosure and opt-out kept" };
    const data = { venture_id: ventureId, drafts: [draft], batches: [{ batch_id: "batch", approval_digest: "digest" }], gmail: { multi_gmail_v1: true, mailboxes: [
      { id: "first", email: draft.sender.email, status: "active", needs_reconnect: false },
      { id: "second", email: `you@other-${domain}`, status: "active", needs_reconnect: false },
    ] } };
    vm.runInNewContext(code, { exports, require(name) {
      if (name === "react") return {
        useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial; return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }]; },
        useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
        useEffect(callback, deps) { const index = cursor++; if (!slots[index] || deps.some((value, position) => value !== slots[index][position])) { slots[index] = deps; effects.push(callback); } },
      };
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name.endsWith("/gtmJourneyApi")) return { journeyClient: {
        get: async owner => { calls.push({ read: owner }); return data; },
        review: async (owner, reviewed, decision, edits) => { assert.equal(ready.at(-1), false); calls.push({ owner, reviewed, decision, edits }); if (failure) throw new Error("Failed"); return { ...data, drafts: [{ ...reviewed, ...edits, revision: "b".repeat(64), review_state: "draft" }] }; },
      } };
      if (name.endsWith("/opsApi")) return { getOpsWorkspace: () => workspaceId };
      if (name.endsWith("/client")) return { verifiedSessionSubject: () => `founder_${workspaceId}`, verifiedSessionEpoch: () => 1 };
      if (name.endsWith("/gmailMailboxes")) return { mailboxCanSend: mailbox => mailbox.status === "active" && !mailbox.needs_reconnect };
      if (name.endsWith("/OutreachDraft")) return { Draft: "draft-review" };
      return { ChatAnchor: "a" };
    } });
    const approval = { multi_gmail_v1: true, batch_id: "batch", approval_digest: "digest", draft_ids: ["draft"] };
    const render = () => { cursor = 0; const tree = exports.ChatOutreachReview({ ventureId, messageId: "quote", approval, disabled: false, current: () => owned, onReady: value => ready.push(value) }); while (effects.length) effects.shift()(); return tree; };
    render(); await flush(); let tree = render();
    assert.equal(ready.at(-1), true); assert.ok(text(tree).includes(draft.sender.email));
    let review = nodes(tree).find(node => node.type === "draft-review");
    await review.props.save("draft", { mailbox_id: "second" }, draft.revision); await flush();
    assert.equal(ready.at(-1), false);
    assert.equal(calls.at(-1).decision, "draft"); assert.equal(calls.at(-1).reviewed.revision, draft.revision);
    assert.equal(calls.at(-1).owner.workspaceId, workspaceId);
    tree = render(); review = nodes(tree).find(node => node.type === "draft-review");
    assert.equal(review.props.draft.body, draft.body);
    assert.match(text(tree), /old approval cannot send/);
    failure = true; await review.props.save("draft", { mailbox_id: "first" }); await flush();
    assert.equal(ready.at(-1), false); assert.match(text(render()), /could not be confirmed/);
    const before = calls.length; owned = false;
    await review.props.save("draft", { mailbox_id: "first" }); assert.equal(calls.length, before); assert.equal(render(), null);
    owned = true; slots.length = 0; data.batches[0].approval_digest = "changed";
    render(); await flush(); assert.equal(ready.at(-1), false); assert.equal(nodes(render()).some(node => node.type === "draft-review"), false);
  }
  const dock = readFileSync(`${__dirname}/ChatDock.tsx`, "utf8");
  assert.match(dock, /actionName === "gtm.send" && conf.resulting_state\?\.multi_gmail_v1 === true/);
  assert.match(dock, /if \(started \|\| senderBlocked \|\| conf.state === "starting"\) return/);
});
