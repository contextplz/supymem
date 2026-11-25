const assert = require("node:assert/strict"), test = require("node:test"), vm = require("node:vm");
const { readFileSync } = require("node:fs"), ts = require("typescript");
const jsx = (type, props) => ({ type, props: props || {} });
const nodes = value => Array.isArray(value) ? value.flatMap(nodes) : value?.props ? [value, ...nodes(value.props.children)] : [];
const text = value => Array.isArray(value) ? value.map(text).join("") : value?.props ? text(value.props.children) : value == null ? "" : String(value);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("venture sender defaults are workspace-bound, retain failed selections, support inheritance and retire with either founder", async () => {
  const code = ts.transpileModule(readFileSync(`${__dirname}/VentureSenderSettings.tsx`, "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  for (const [workspaceId, domain] of [["workspace_catering", "catering.example"], ["workspace_painting", "painting.example"]]) {
    const ventureId = `${workspaceId}_venture`, calls = [], slots = [], effects = [], exports = {};
    let cursor = 0, owned = true, fail = false, response = null;
    const data = { multi_gmail_v1: true, workspace_id: workspaceId, workspace_default_mailbox_id: "first", venture_id: ventureId, venture_default_mailbox_id: null, mailboxes: [
      { id: "first", email: `you@${domain}`, status: "active", is_default: true, needs_reconnect: false },
      { id: "second", email: `you@other-${domain}`, status: "active", is_default: false, needs_reconnect: false },
    ] };
    vm.runInNewContext(code, { exports, require(name) {
      if (name === "react") return {
        useState(initial) { const index = cursor++; if (!(index in slots)) slots[index] = initial; return [slots[index], value => { slots[index] = typeof value === "function" ? value(slots[index]) : value; }]; },
        useRef(initial) { const index = cursor++; return slots[index] ??= { current: initial }; },
        useEffect(callback, deps) { const index = cursor++; if (!slots[index] || deps.some((value, position) => value !== slots[index][position])) { slots[index] = deps; effects.push(callback); } },
      };
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name.endsWith("/opsApi")) return {
        getGmailMailboxes: async (...args) => { calls.push({ read: args }); return response; },
        setDefaultGmailMailbox: async (...args) => { calls.push({ write: args }); if (fail) throw new Error("Failed"); data.venture_default_mailbox_id = args[1]; },
      };
      if (name.endsWith("/gmailMailboxes")) return { mailboxCanSend: mailbox => mailbox.status === "active" && !mailbox.needs_reconnect };
      if (name.endsWith("/client")) return { verifiedSessionSubject: () => `founder_${workspaceId}`, verifiedSessionEpoch: () => 1 };
      return { ChatAnchor: "a" };
    } });
    const render = () => { cursor = 0; const tree = exports.VentureSenderSettings({ workspaceId, ventureId, current: () => owned }); while (effects.length) effects.shift()(); return tree; };
    render(); await flush(); assert.equal(render(), null);
    response = data;
    slots.length = 0; render(); await flush();
    let tree = render(), picker = nodes(tree).find(node => node.type === "select");
    assert.ok(text(tree).includes(`Use workspace default (you@${domain})`));
    fail = true; await picker.props.onChange({ target: { value: "second" } }); await flush();
    assert.equal(nodes(render()).find(node => node.type === "select").props.value, "");
    assert.match(text(render()), /could not be confirmed/);
    fail = false; await picker.props.onChange({ target: { value: "second" } }); await flush();
    assert.equal(nodes(render()).find(node => node.type === "select").props.value, "second");
    assert.equal(JSON.stringify(calls.find(call => call.write).write), JSON.stringify([workspaceId, "second", ventureId]));
    picker = nodes(render()).find(node => node.type === "select");
    await picker.props.onChange({ target: { value: "" } }); await flush();
    assert.equal(data.venture_default_mailbox_id, null);
    assert.equal(calls.at(-1).read[0], workspaceId); assert.equal(calls.at(-1).read[1], ventureId);
    data.mailboxes[1].needs_reconnect = true;
    const before = calls.length;
    await nodes(render()).find(node => node.type === "select").props.onChange({ target: { value: "second" } });
    assert.equal(calls.length, before);
    picker = nodes(render()).find(node => node.type === "select"); owned = false;
    await picker.props.onChange({ target: { value: "first" } }); assert.equal(calls.length, before);
    assert.equal(render(), null);
  }
});
