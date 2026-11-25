import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { ideaConversationActivity, ideaPollStart, ideaPollState } from "./ideaChatPoll.ts";

const ready = { id: "a", role: "assistant" as const, kind: "idea_clarification_ready" };
const proposal = { ...ready, actions: [{ label: "Review idea", href: "/v/v1/idea", style: "primary" as const }] };

test("actual clarification polling waits 210 seconds before its slow-question notice", async () => {
  const source = fs.readFileSync(new URL("../components/chat/useVentureChat.ts", import.meta.url), "utf8");
  const tree = ts.createSourceFile("useVentureChat.ts", source, ts.ScriptTarget.Latest, true);
  let declaration: ts.VariableDeclaration | undefined;
  function find(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === "startIdeaPoll") declaration = node;
    ts.forEachChild(node, find);
  }
  find(tree);
  assert.ok(declaration?.initializer);
  const timers: Array<{callback: () => void; milliseconds: number}> = [];
  const messages = [{id: "pending", role: "assistant", kind: "idea_clarification_pending"}];
  const context = vm.createContext({Date, Promise, ideaPollState,
    polledId: {current: ""}, ideaPollGeneration: {current: 0}, current: () => true,
    load: async () => messages, useCallback: (callback: unknown) => callback,
    mergeChat: (previous: unknown) => previous, setMessages: (update: (previous: any[]) => any[]) => {messages.splice(0, messages.length, ...update(messages));},
    window: {setTimeout: (callback: () => void, milliseconds: number) => timers.push({callback, milliseconds})}});
  vm.runInContext(ts.transpileModule(`globalThis.start=${declaration.initializer.getText(tree)};`,
    {compilerOptions: {target: ts.ScriptTarget.ES2022}}).outputText, context);
  context.start("pending", false);
  let elapsed = 0;
  for (let attempt = 0; attempt < 70; attempt++) {
    const timer = timers.shift()!;
    assert.ok(timer);
    elapsed += timer.milliseconds;
    timer.callback();
    for (let turn = 0; turn < 8; turn++) await Promise.resolve();
    if (elapsed <= 200_000) assert.equal(messages.length, 1);
  }
  assert.equal(elapsed, 210_000);
  assert.equal(messages.length, 2);
  assert.match((messages[1] as {body_md?: string}).body_md || "", /taking longer to prepare/);
});

test("clarification reassures after 45 seconds without declaring a timeout", () => {
  const started = Date.parse("2026-10-18T00:00:00Z");
  for (const kind of ["entry_pending", "idea_clarification_pending"]) {
    const pending = { ...ready, kind, created_at: new Date(started).toISOString() };
    assert.equal(ideaConversationActivity([pending], false, started + 44_999), "Preparing your next question");
    for (const elapsed of [45_000, 180_000, 200_000]) {
      assert.equal(ideaConversationActivity([pending], false, started + elapsed),
        "Still working on a good question. This can take up to 3 minutes.");
    }
    assert.equal(ideaConversationActivity([pending, ready], false, started + 45_000), "Review your saved idea");
    assert.equal(ideaConversationActivity([pending], true, started + 45_000), "Saving your answer");
  }
});

test("U51 live conversation receipts retire preparation and question headers when review is ready", () => {
  const pending = { ...ready, kind: "idea_clarification_pending" };
  const question = { ...ready, actions: [{ label: "Teachers", action: "chat.reply", payload: { question_id: "question-a" } }] };
  const review = { ...ready, id: "review", kind: "idea_validation_ready" };
  assert.equal(ideaConversationActivity([pending]), "Preparing your next question");
  assert.equal(ideaConversationActivity([pending, question]), "Answer the next question about your idea");
  assert.equal(ideaConversationActivity([pending, question, review]), "Review your saved idea");
  assert.equal(ideaConversationActivity([review], true), "Saving your answer");
  assert.equal(ideaConversationActivity([]), null);
});

test("D22 failed clarification recovery replaces preparation until a retry starts", () => {
  const pending = { ...ready, kind: "idea_clarification_pending" };
  for (const error_code of ["clarification_outcome_uncertain", "clarification_invalid_output", "clarification_insufficient_credits"]) {
    const failure = { ...ready, kind: "idea_failed", actions: [
      { label: "Try again", action: "job.retry", payload: { kind: "idea.clarify_turn" } },
      { label: "Do it another way", action: "recovery.alternative", payload: { step: "idea.clarify_turn", error_code } },
    ] };
    assert.equal(ideaConversationActivity([pending, failure]), "Couldn't prepare the next question");
    assert.equal(ideaConversationActivity([pending, failure, pending]), "Preparing your next question");
    assert.equal(ideaConversationActivity([pending, failure, ready]), "Review your saved idea");
  }
});

test("idea poll keeps waiting past the proposal until validation posts", () => {
  assert.equal(ideaPollState([]), "waiting");
  assert.equal(ideaPollState([ready]), "done");
  assert.equal(ideaPollState([proposal]), "proposal");
  assert.equal(ideaPollState([proposal, { id: "b", role: "assistant", kind: "idea_validation_failed" }]), "done");
});

test("idea poll starts on mount only for pending or proposal assistant messages", () => {
  assert.equal(ideaPollStart(undefined), null);
  assert.equal(ideaPollStart({ id: "p", role: "assistant", kind: "entry_pending" }), "pending");
  assert.equal(ideaPollStart({ id: "p", role: "assistant", kind: "idea_clarification_pending" }), "pending");
  assert.equal(ideaPollStart(proposal), "proposal");
  assert.equal(ideaPollStart(ready), null);
  assert.equal(ideaPollStart({ id: "u", role: "user", kind: "entry_pending" }), null);
});
