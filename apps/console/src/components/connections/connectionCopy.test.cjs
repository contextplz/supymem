const assert = require("node:assert/strict");
const { test } = require("node:test");

test("reconnect holds are actionable across business and consumer audiences", async () => {
  const { connectionState, connectionActionLabel } = await import("./connectionCopy.ts");
  for (const audience of ["Freight dispatchers", "Weekend gardeners"]) {
    const card = { id: "gmail", needs_reconnect: true, connected: false, ready: false, action: "connect", audience };
    assert.equal(connectionState(card), "Needs reconnect");
    assert.equal(connectionActionLabel(card), "Reconnect");
  }
});

test("shared Gmail is connected for unrelated ventures without treating a foreign founder as connected", async () => {
  const { connectionCopy, connectionState } = await import("./connectionCopy.ts");
  for (const audience of ["Logistics teams", "Music hobbyists"]) {
    const card = { id: "gmail", connected: true, ready: true, audience };
    assert.equal(connectionState(card), "Connected");
    assert.match(connectionCopy(card).purpose, /all ventures you own/);
    assert.match(connectionCopy(card).purpose, /15 emails\/day total/);
    assert.ok(connectionCopy(card).purpose.length <= 60);
  }
  assert.equal(connectionState({ id: "gmail", connected: false, ready: false }), "Not connected");
});

test("configured toolkit cards explain their purpose and hide unconfigured connections across unrelated domains", async () => {
  const { connectionCopy, connectionVisible, connectionActionLabel, toolkitConnection } = await import("./connectionCopy.ts");
  for (const domain of ["Inventory tools for retailers", "Dance classes for children"]) {
    const card = { id: "notion", toolkit_slug: "notion", configured: true, connected: false, action: "connect", note: `Review your research for ${domain}.`, experiment_goals: ["Customer feedback research"] };
    assert.equal(connectionVisible(card), true);
    assert.equal(connectionActionLabel(card), "Connect");
    assert.equal(connectionActionLabel({ ...card, action: "sync", connected: true, ready: true }), "Refresh status");
    assert.equal(connectionCopy(card).purpose, card.note);
    assert.deepEqual(toolkitConnection(card).experiment_goals, ["Customer feedback research"]);
    assert.equal(connectionVisible({ ...card, configured: false }), false);
    assert.equal(connectionVisible({ ...card, configured: undefined }), false);
    assert.equal(connectionVisible({ id: "gmail", configured: false }), true);
  }
});

test("toolkit disconnect verifies saved account results and stops on company changes or provider failures", async () => {
  const { disconnectToolkitAccounts } = await import("./connectionCopy.ts");
  for (const domain of ["Farm equipment scheduling", "Personal language lessons"]) {
    const card = { id: "googlesheets", toolkit_slug: "googlesheets", domain, accounts: [{ id: "receipt_one" }, { id: "receipt_two" }] };
    const calls = [];
    const revoke = async id => { calls.push(id); return { id, status: "revoked" }; };
    assert.equal(await disconnectToolkitAccounts(card, revoke, () => true), true);
    assert.deepEqual(calls, ["receipt_one", "receipt_two"]);
    calls.length = 0;
    assert.equal(await disconnectToolkitAccounts(card, revoke, () => calls.length === 0), false);
    assert.deepEqual(calls, ["receipt_one"]);
    await assert.rejects(disconnectToolkitAccounts(card, async () => ({ id: "foreign_receipt", status: "revoked" }), () => true), /not confirmed/);
    await assert.rejects(disconnectToolkitAccounts(card, async id => ({ id, status: "active" }), () => true), /not confirmed/);
    await assert.rejects(disconnectToolkitAccounts(card, async () => { throw new Error("provider unavailable"); }, () => true), /provider unavailable/);
    await assert.rejects(disconnectToolkitAccounts({ ...card, accounts: [] }, revoke, () => true), /Refresh/);
    await assert.rejects(disconnectToolkitAccounts({ ...card, accounts: [null] }, revoke, () => true), /Refresh/);
    await assert.rejects(disconnectToolkitAccounts({ ...card, accounts: "not saved accounts" }, revoke, () => true), /Refresh/);
  }
});


test("local SMTP default gives you a next step without changing hosted copy", async () => {
  const { connectionCopy, connectionSetupMessage } = await import("./connectionCopy.ts");
  for (const domain of ["Inventory tools for retailers", "Dance classes for children"]) {
    const card = { id: "smtp", default: true, domain, hint: "Add your SMTP server and sender in local settings, then refresh your email connection." };
    assert.equal(connectionCopy(card).purpose, "You use SMTP email by default.");
    assert.equal(connectionSetupMessage(card), card.hint);
    assert.match(connectionSetupMessage({ ...card, hint: null }), /You can use/);
    assert.notEqual(connectionCopy({ ...card, default: false }).purpose, connectionCopy(card).purpose);
  }
});
