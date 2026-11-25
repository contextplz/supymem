const assert = require("node:assert/strict"), test = require("node:test"), vm = require("node:vm");
const { readFileSync } = require("node:fs"), ts = require("typescript");
const context = { exports: {} };
vm.runInNewContext(ts.transpileModule(readFileSync(`${__dirname}/gmailMailboxes.ts`, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText, context);
const { checkedGmailMailboxes, mailboxCanSend } = context.exports;

test("committed mailbox responses use daily_send_limit and echo ownership only for the requested scope across unrelated founders", () => {
  for (const [workspaceId, ventureId, email] of [["ws_shipping", "ven_shipping", "you@freight.example"], ["ws_clay", "ven_clay", "you@ceramics.example"]]) {
    const mailbox = { id: `${workspaceId}_mail`, email, provider: "gmail", status: "active", is_default: true,
      sent_today: 0, daily_send_limit: 15, needs_reconnect: false, updated_at: "2026-10-29T08:00:00" };
    const workspace = { multi_gmail_v1: true, workspace_id: workspaceId, workspace_default_mailbox_id: mailbox.id, mailboxes: [mailbox] };
    assert.equal(checkedGmailMailboxes(workspace, workspaceId), workspace);
    assert.equal(workspace.venture_id, undefined);
    assert.equal(workspace.mailboxes[0].cap, undefined);
    const venture = { ...workspace, venture_id: ventureId, venture_default_mailbox_id: null };
    assert.equal(checkedGmailMailboxes(venture, workspaceId, ventureId), venture);
    assert.equal(mailboxCanSend(venture.mailboxes[0]), true);
    const { workspace_id, ...unscoped } = workspace;
    assert.throws(() => checkedGmailMailboxes(unscoped, workspaceId), /could not be verified/);
    assert.throws(() => checkedGmailMailboxes(workspace, workspaceId, ventureId), /could not be verified/);
    const { daily_send_limit, ...legacyMailbox } = mailbox;
    assert.throws(() => checkedGmailMailboxes({ ...workspace, mailboxes: [{ ...legacyMailbox, cap: daily_send_limit }] }, workspaceId), /could not be verified/);
  }
});

test("multi Gmail validates both founders, defaults off and never admits foreign or malformed inbox lists", () => {
  for (const [workspaceId, domain] of [["workspace_distribution", "distribution.example"], ["workspace_pottery", "pottery.example"]]) {
    const mailbox = { id: `${workspaceId}_mail`, email: `you@${domain}`, status: "active", is_default: true, sent_today: 2, daily_send_limit: 15, needs_reconnect: false, updated_at: "2026-10-29T08:00:00" };
    const data = { multi_gmail_v1: true, workspace_id: workspaceId, venture_id: `venture_${workspaceId}`, mailboxes: [mailbox] };
    assert.equal(checkedGmailMailboxes(data, workspaceId, data.venture_id), data);
    for (const legacy of [{ account_email: mailbox.email }, { ...data, multi_gmail_v1: false }, null]) assert.equal(checkedGmailMailboxes(legacy, workspaceId), null);
    assert.throws(() => checkedGmailMailboxes(data, "another_founder"), /could not be verified/);
    assert.throws(() => checkedGmailMailboxes(data, workspaceId, "another_venture"), /could not be verified/);
    for (const invalid of [null, { ...mailbox, id: "" }, { ...mailbox, daily_send_limit: -1 }, { ...mailbox, updated_at: "invalid" }, { ...mailbox, sent_today: NaN }, { ...mailbox, needs_reconnect: "false" }]) {
      assert.throws(() => checkedGmailMailboxes({ ...data, mailboxes: [invalid] }, workspaceId), /could not be verified/);
    }
    assert.throws(() => checkedGmailMailboxes({ ...data, mailboxes: [mailbox, mailbox] }, workspaceId), /could not be verified/);
    assert.equal(mailboxCanSend(mailbox), true);
    for (const inactive of [{ ...mailbox, status: "revoked" }, { ...mailbox, needs_reconnect: true }]) assert.equal(mailboxCanSend(inactive), false);
  }
});
