const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");
const ts = require("typescript");

const jsx = (type, props) => ({ type, props: props || {} });
const nodes = node => Array.isArray(node) ? node.flatMap(nodes) : node && typeof node === "object"
  ? [node, ...nodes(node.props?.children)] : [];
const text = node => Array.isArray(node) ? node.map(text).join("") : node && typeof node === "object"
  ? text(node.props?.children ?? node.props?.error) : node == null ? "" : String(node);

test("sender changes request a fresh exact review, preserve the footer, and fail closed for unavailable or historical inboxes", async () => {
  const code = ts.transpileModule(readFileSync(join(__dirname, "OutreachDraft.tsx"), "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  for (const [founder, domain] of [["founder_warehousing", "warehousing.example"], ["founder_dancing", "dancing.example"]]) {
    const calls = [], hooks = [], exports = {};
    let cursor = 0, owned = true;
    vm.runInNewContext(code, { exports, require(name) {
      if (name === "react") return { useState(initial) { const index = cursor++; if (!(index in hooks)) hooks[index] = initial; return [hooks[index], value => { hooks[index] = value; }]; } };
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "@/lib/gtmJourneyApi") return { safeEvidenceUrl: value => value };
      if (name === "@/lib/reachableLeads") return { guessedEmailWarning: () => "" };
      if (name === "@/lib/gmailMailboxes") return { mailboxCanSend: mailbox => mailbox.status === "active" && !mailbox.needs_reconnect };
      if (name === "@/components/primitives/ErrorNotice") return { ErrorNotice: "error-notice" };
      return { default: {} };
    } });
    const mailboxes = [
      { id: `${founder}_first`, email: `you@${domain}`, status: "active", needs_reconnect: false },
      { id: `${founder}_second`, email: `you@other-${domain}`, status: "active", needs_reconnect: false },
    ];
    const originalBody = `Your email address is listed publicly on https://${domain}.\nReply no thanks to opt out.\nyou@${domain}`;
    let draft = { doc_id: founder, to: `recipient@${domain}`, subject: "A question", body: originalBody, state: "draft", review_state: "approved", revision: "a".repeat(64), mailbox_id: mailboxes[0].id, sender: { mailbox_id: mailboxes[0].id, email: mailboxes[0].email } };
    const render = (overrides = {}, enabled = true) => { cursor = 0; return exports.Draft({ draft: { ...draft, ...overrides }, mailboxes: enabled ? mailboxes : undefined, disabled: false, current: () => owned, save: async (...args) => { calls.push(args); return true; } }); };
    let tree = render(), picker = nodes(tree).find(node => node.type === "select");
    assert.equal(picker.props.value, mailboxes[0].id);
    await picker.props.onChange({ target: { value: "foreign_mailbox" } }); assert.equal(calls.length, 0);
    await picker.props.onChange({ target: { value: mailboxes[1].id } });
    assert.equal(JSON.stringify(calls[0]), JSON.stringify(["draft", { mailbox_id: mailboxes[1].id }, draft.revision]));
    assert.ok(text(render()).includes(originalBody));
    draft = { ...draft, mailbox_id: mailboxes[1].id, sender: { mailbox_id: mailboxes[1].id, email: mailboxes[1].email }, revision: "b".repeat(64), review_state: "draft" };
    assert.equal(nodes(render()).find(node => node.type === "select").props.value, mailboxes[1].id);
    for (const historical of [{ state: "sent" }, { state: "sending" }, { kind: "reply" }, { day: 3 }, { day: 6 }, { sender_locked: true }]) assert.equal(nodes(render(historical)).some(node => node.type === "select"), false);
    mailboxes[1].needs_reconnect = true;
    tree = render(); assert.match(text(tree), /other inboxes will not be used instead/);
    assert.equal(nodes(tree).find(node => node.type === "error-notice").props.source, "gtm.outreach.sender");
    const approve = nodes(tree).find(node => node.type === "button" && text(node) === "Approve message");
    assert.equal(approve.props.disabled, true); approve.props.onClick(); assert.equal(calls.length, 1);
    assert.equal(nodes(render({ mailbox_id: "foreign" })).find(node => node.type === "button" && text(node) === "Approve message").props.disabled, true);
    mailboxes[1].needs_reconnect = false;
    picker = nodes(render()).find(node => node.type === "select"); owned = false;
    await picker.props.onChange({ target: { value: mailboxes[0].id } }); assert.equal(calls.length, 1);
    owned = true;
    nodes(render()).find(node => node.type === "button" && text(node) === "Edit").props.onClick();
    assert.match(text(render()), /Keep your signature, source disclosure, opt-out/);
    assert.equal(nodes(render()).find(node => node.type === "textarea").props.value, originalBody);
    assert.equal(nodes(render({}, false)).some(node => node.type === "select"), false);
    assert.doesNotMatch(text(render({}, false)), /Sender:|Changing your sender/);
  }
});

test("review keeps source separate from recipient copy across business and consumer ventures", async () => {
  const code = ts.transpileModule(readFileSync(join(__dirname, "OutreachDraft.tsx"), "utf8"), { compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022, jsx: ts.JsxEmit.ReactJSX,
  } }).outputText;
  for (const [venture, business, source] of [
    ["LedgerLane", "Cedar Accounts", "https://cedar-accounts.example/about"],
    ["TrailNotes", "River Walk Club", "https://river-walks.example/about"],
  ]) {
    const exports = {}, copies = [], hooks = [];
    let cursor = 0;
    vm.runInNewContext(code, { exports, navigator: { clipboard: { writeText: async value => copies.push(value) } }, require(name) {
      if (name === "react") return { useState(initial) {
        const index = cursor++;
        if (!(index in hooks)) hooks[index] = initial;
        return [hooks[index], value => { hooks[index] = value; }];
      } };
      if (name === "react/jsx-runtime") return { jsx, jsxs: jsx };
      if (name === "@/lib/gtmJourneyApi") return { safeEvidenceUrl: value => /^https?:\/\//.test(value || "") ? value : "" };
      if (name === "@/lib/reachableLeads") return { guessedEmailWarning: () => "" };
      if (name === "@/lib/gmailMailboxes") return { mailboxCanSend: mailbox => mailbox.status === "active" && !mailbox.needs_reconnect };
      if (name === "@/components/primitives/ErrorNotice") return { ErrorNotice: "error-notice" };
      if (name.endsWith(".module.css")) return { default: { draftBody: "recipient-body" } };
      throw new Error(name);
    } });
    const draft = { doc_id: "draft", to: business, subject: "A research question", state: "draft", review_state: "draft",
      revision: "revision", channel: "manual", source_url: source,
      body: `Hi there,\n\nI'm building ${venture}. Would you share your experience?\n\nThe ${venture} team\n\nIf this is not relevant, just reply "no thanks" and I will not email you again.\nBusiness outreach · 42 Lake Road` };
    const render = () => { cursor = 0; return exports.Draft({ draft, disabled: false, current: () => true, save: async () => true }); };
    let tree = render();
    const link = nodes(tree).find(node => node.type === "a" && node.props.href === source);
    assert.ok(link);
    assert.equal(link.props.rel, "noopener noreferrer");
    const body = nodes(tree).find(node => node.type === "p" && node.props.className === "recipient-body");
    assert.equal(text(body), draft.body);
    assert.ok(text(tree).includes(`Source: ${source}`));
    assert.ok(!text(body).includes("Source:"));
    assert.ok(draft.body.split("\n").filter(Boolean).every(line => !/^[a-z]/.test(line)));
    assert.ok(draft.body.includes("no thanks") && draft.body.includes("42 Lake Road"));
    await nodes(tree).find(node => node.type === "button" && text(node) === "Copy draft").props.onClick();
    assert.equal(copies[0], `${draft.subject}\n\n${draft.body}`);
    draft.channel = "email";
    tree = render();
    nodes(tree).find(node => node.type === "button" && text(node) === "Edit").props.onClick();
    tree = render();
    assert.equal(nodes(tree).find(node => node.type === "textarea").props.value, draft.body);
    assert.ok(text(tree).includes("The source stays in your review card, not the email."));
    assert.ok(nodes(tree).some(node => node.type === "a" && node.props.href === source));
    draft.source_url = "javascript:alert(1)";
    assert.ok(!nodes(render()).some(node => node.type === "a"));
  }
});
