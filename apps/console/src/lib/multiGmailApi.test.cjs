const assert = require("node:assert/strict"), test = require("node:test"), vm = require("node:vm");
const { readFileSync } = require("node:fs"), ts = require("typescript");
const compile = file => ts.transpileModule(readFileSync(`${__dirname}/${file}`, "utf8"), {
  compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 },
}).outputText;
const mailboxContext = { exports: {} }; vm.runInNewContext(compile("gmailMailboxes.ts"), mailboxContext);

test("mailbox requests bind local ids and the exact founder workspace while preserving legacy OAuth and verify bodies", async () => {
  for (const [workspaceId, domain] of [["workspace_logistics", "logistics.example"], ["workspace_quilting", "quilting.example"]]) {
    let result = { ok: true, data: { multi_gmail_v1: true, workspace_id: workspaceId, mailboxes: [] } };
    const calls = [], context = { exports: {}, URL, Headers, require(name) {
      if (name.endsWith("gmailMailboxes")) return mailboxContext.exports;
      if (name.endsWith("operationsScope")) return { operationsReadPath: path => path };
      if (name.endsWith("client")) return { api: async (path, init) => {
        calls.push({ path, init });
        return result;
      } };
      throw new Error(name);
    } };
    vm.runInNewContext(compile("opsApi.ts"), context);
    const client = context.exports;
    client.setOpsWorkspace(workspaceId);
    await client.connectHubProvider("gmail", { return_to: "/connections" });
    assert.equal(calls.at(-1).init.body, '{"return_to":"/connections"}');
    await client.connectHubProvider("gmail", { intent: "add", return_to: "/connections" });
    assert.equal(JSON.parse(calls.at(-1).init.body).intent, "add");
    await client.verifyHubGmail();
    assert.equal(calls.at(-1).init.body, '{"venture_id":null}');
    await client.verifyHubGmail("venture", "local/mail", workspaceId);
    assert.deepEqual(JSON.parse(calls.at(-1).init.body), { venture_id: "venture", mailbox_id: "local/mail" });
    await client.setDefaultGmailMailbox(workspaceId, "local/mail", "venture");
    assert.match(calls.at(-1).path, /connections\/gmail\/mailboxes\/default/);
    assert.deepEqual(JSON.parse(calls.at(-1).init.body), { mailbox_id: "local/mail", venture_id: "venture" });
    await client.disconnectGmailMailbox(workspaceId, "local/mail", "2026-10-29T08:00:00");
    assert.match(calls.at(-1).path, /connections\/gmail\/mailboxes\/local%2Fmail/);
    assert.equal(calls.at(-1).init.method, "DELETE");
    assert.deepEqual(JSON.parse(calls.at(-1).init.body), { confirmed: true, expected_updated_at: "2026-10-29T08:00:00" });
    await client.getGmailMailboxes(workspaceId);
    for (const call of calls) {
      assert.equal(new URL(call.path, `https://${domain}`).searchParams.get("workspace_id"), workspaceId);
      assert.equal(call.init.headers.get("X-Workspace-ID"), workspaceId);
      assert.doesNotMatch(call.init.body || "", /connected_account_id|composio_user_id/);
    }
    result = { ok: true, data: { account_email: `you@${domain}` } };
    assert.equal(await client.getGmailMailboxes(workspaceId), null);
    result = { ok: true, data: { multi_gmail_v1: false, workspace_id: workspaceId, mailboxes: [] } };
    assert.equal(await client.getGmailMailboxes(workspaceId), null);
    result = { ok: false, status: 404, data: {} };
    assert.equal(await client.getGmailMailboxes(workspaceId), null);
    result = { ok: false, status: 503, data: {} };
    await assert.rejects(() => client.getGmailMailboxes(workspaceId), /503/);
  }
});

test("sender changes use a new revision-bound review, never an approval or historical send", async () => {
  for (const [workspaceId, ventureId] of [["workspace_accounting", "venture_billing"], ["workspace_gardening", "venture_seedlings"]]) {
    const calls = [], owner = { actorId: `founder_${workspaceId}`, workspaceId, ventureId };
    const draft = { doc_id: "draft", revision: "a".repeat(64), state: "draft", review_state: "approved" };
    const journey = { venture_id: ventureId, icp: {}, prospects: [], drafts: [draft], batches: [], gmail: {}, results: { replies: [] }, estimate: {}, daily_cap: 15 };
    const context = { exports: {}, require(name) {
      if (name.includes("gmailMailboxes")) return mailboxContext.exports;
      return { api: async () => ({ ok: true, data: journey }) };
    } };
    vm.runInNewContext(compile("gtmJourneyApi.ts"), context);
    const client = context.exports.createJourneyClient(async (path, init) => { calls.push({ path, init }); return { ok: true, data: journey }; });
    await client.review(owner, draft, "draft", { mailbox_id: "second_local_mailbox" });
    assert.deepEqual(JSON.parse(calls[0].init.body), { expected_revision: draft.revision, decision: "draft", mailbox_id: "second_local_mailbox" });
    assert.equal(calls[0].init.method, "PATCH");
    assert.match(calls[0].path, new RegExp(ventureId));
    for (const invalid of [{ state: "sent" }, { state: "sending" }, { kind: "reply" }, { day: 3 }, { day: 6 }, { sender_locked: true }]) {
      assert.throws(() => client.review(owner, { ...draft, ...invalid }, "draft", { mailbox_id: "other" }), /original inbox/);
    }
    assert.throws(() => client.review(owner, draft, "approved", { mailbox_id: "other" }), /new draft review/);
    assert.equal(calls.length, 1);
    await client.review(owner, draft, "approved");
    assert.deepEqual(JSON.parse(calls[1].init.body), { expected_revision: draft.revision, decision: "approved" });
  }
});
