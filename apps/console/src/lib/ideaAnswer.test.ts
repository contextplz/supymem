import assert from "node:assert/strict";
import test from "node:test";
import { readFileSync } from "node:fs";
import { canSendIdeaAnswer, leavesIdeaQuestion, composeIdeaAnswer, currentQuestionAnswer, editableIdeaAnswer, openPreviousAnswerReview, previousIdeaAnswers, questionSelectionMode, savedIdeaChoiceAnswer, savedIdeaOptionLabels, savedIdeaQuestionOptions, shortOptionLabel, toggleIdeaChoice } from "./ideaAnswer.ts";

test("editable answers hide wire prefixes without changing assumption storage", () => {
  const wire = composeIdeaAnswer(["New parents experience anxiety"], "I interviewed first-time parents in the US", true);
  assert.equal(editableIdeaAnswer(wire), "New parents experience anxiety\nI interviewed first-time parents in the US");
  assert.equal(editableIdeaAnswer("Pain: unsure if Assumed: is literal"), "Pain: unsure if Assumed: is literal");
  assert.match(wire, /^Assumed: /);
  const page = readFileSync(new URL("../app/v/[ventureId]/idea/page.tsx", import.meta.url), "utf8");
  assert.match(page, /setRevisionEdit\(saved\?\.answer \?\? editableIdeaAnswer\(/);
});

test("all displayed questions allow multiple choices; the low-level single helper stays compatible", () => {
  const mode = questionSelectionMode(undefined);
  assert.equal(mode, "multiple");
  assert.equal(questionSelectionMode("single"), "multiple");
  assert.deepEqual(toggleIdeaChoice(["Search"], "Referrals", mode), ["Search", "Referrals"]);
  assert.deepEqual(toggleIdeaChoice(["Search"], "Referrals", "single"), ["Referrals"]);
});

test("multiple choices can be added and removed before composing one answer", () => {
  const mode = questionSelectionMode("multiple");
  const first = toggleIdeaChoice([], "Search", mode);
  const second = toggleIdeaChoice(first, "Referrals", mode);
  assert.deepEqual(second, ["Search", "Referrals"]);
  assert.equal(composeIdeaAnswer(second, "  Mostly local customers  "), "Assumed: Search, Referrals\nMostly local customers");
  assert.deepEqual(toggleIdeaChoice(second, "Search", mode), ["Referrals"]);
});

test("Supymem's pick is assumed, while typed context stays on a founder line", () => {
  assert.equal(composeIdeaAnswer(["VP Portfolio Management at a lender"], "", true), "Assumed: VP Portfolio Management at a lender");
  assert.equal(composeIdeaAnswer([], "I spoke to a VP"), "I spoke to a VP");
  assert.equal(composeIdeaAnswer(["We have 5-10 paying customers"], "", true), "Assumed: We have 5-10 paying customers");
  assert.equal(composeIdeaAnswer(["Home cooks"], "  I have not spoken to any yet  ", true), "Assumed: Home cooks\nI have not spoken to any yet");
  assert.equal(composeIdeaAnswer([], "My own answer", true), "My own answer");
});

test("submitted picks remain assumed while typed conversations stay on a separate founder line", () => {
  assert.equal(composeIdeaAnswer(["A few people"], "Two clinic owners I spoke to."), "Assumed: A few people\nTwo clinic owners I spoke to.");
  assert.equal(composeIdeaAnswer(["A few people"], ""), "Assumed: A few people");
  assert.equal(composeIdeaAnswer(["A few people"], "", true), "Assumed: A few people");
  const wire = composeIdeaAnswer(["A few people"], "Two clinic owners I spoke to.");
  assert.deepEqual(wire.split("\n"), ["Assumed: A few people", "Two clinic owners I spoke to."]);
  assert.deepEqual(savedIdeaChoiceAnswer(wire, ["A few people"]), { selected: ["A few people"], freeText: "Two clinic owners I spoke to." });
  const dock = readFileSync(new URL("../components/chat/ChatDock.tsx", import.meta.url), "utf8");
  assert.match(dock, /composeIdeaAnswer\(selected, text, assumed \|\| selected\.length > 0\)/);
});

test("editing a prior answer restores saved choices and optional context", () => {
  assert.deepEqual(savedIdeaChoiceAnswer("Assumed: Search, Referrals\nMostly local customers", ["Search", "Referrals"]),
    { selected: ["Search", "Referrals"], freeText: "Mostly local customers" });
  assert.deepEqual(savedIdeaQuestionOptions({ options: ["Search", "Referrals"] }), ["Search", "Referrals"]);
  assert.deepEqual(savedIdeaQuestionOptions(["Search", "Referrals"]), ["Search", "Referrals"]);
  assert.deepEqual(savedIdeaQuestionOptions({}), []);
  assert.deepEqual(savedIdeaChoiceAnswer("Search, Referrals. Mostly local customers", ["Search", "Referrals", "Partner outreach"]),
    { selected: ["Search", "Referrals"], freeText: "Mostly local customers" });
  assert.deepEqual(savedIdeaChoiceAnswer("Business consultants / managers", ["Business consultants / managers", "Researchers"]),
    { selected: ["Business consultants / managers"], freeText: "" });
  assert.deepEqual(savedIdeaChoiceAnswer("A new answer outside listed choices", ["Search", "Referrals"]),
    { selected: [], freeText: "A new answer outside listed choices" });
});

test("saved correction wins over immutable transcript after reload and reopens as choices", () => {
  const transcript = "Bakery owner";
  const questionId = "ict_question";
  const options = ["Bakery owner", "Business consultants / managers"];
  const first = { _answer_corrections: { [questionId]: "Business consultants / managers" } };
  assert.equal(currentQuestionAnswer(transcript, questionId, first), "Business consultants / managers");
  assert.deepEqual(savedIdeaChoiceAnswer(currentQuestionAnswer(transcript, questionId, first), options),
    { selected: ["Business consultants / managers"], freeText: "" });
  const second = { _answer_corrections: { [questionId]: "Bakery owner" } };
  assert.deepEqual(savedIdeaChoiceAnswer(currentQuestionAnswer(transcript, questionId, second), options),
    { selected: ["Bakery owner"], freeText: "" });
});

test("previous answer review pairs only a question with its saved founder reply", () => {
  const question = { role: "agent", kind: "question", text: "Who pays?", options: { field: "customer" } };
  const reply = { role: "founder", kind: "answer", text: "Shop owners" };
  assert.deepEqual(previousIdeaAnswers([question, reply]), [{ question, reply, field: "customer" }]);
  assert.deepEqual(previousIdeaAnswers([question, { role: "agent", kind: "proposal", text: "Summary" }]), []);
});

test("a pending submission blocks duplicate sends", () => {
  assert.equal(canSendIdeaAnswer("Search", true), false);
  assert.equal(canSendIdeaAnswer("Search", false), true);
  assert.equal(canSendIdeaAnswer("  ", false), false);
});

test("a recommendation and typed context share the posted answer limit without clipping", () => {
  const recommended = "Independent teachers";
  const context = "x".repeat(4000 - `Assumed: ${recommended}\n`.length);
  const boundary = composeIdeaAnswer([recommended], context, true);
  assert.equal(boundary.length, 4000);
  assert.equal(canSendIdeaAnswer(`  ${boundary}\n`, false), true);
  assert.equal(canSendIdeaAnswer(boundary, true), false);
  const overflow = composeIdeaAnswer([recommended], `${context}x`, true);
  assert.equal(canSendIdeaAnswer(overflow, false), false);
  assert.equal(overflow, `Assumed: ${recommended}\n${context}x`);
});

test("Back opens and focuses answer review without changing a draft", () => {
  const selected = ["Search", "Referrals"];
  const freeText = "Try both";
  let focused = false;
  let scrolled = false;
  const summary = {
    scrollIntoView: () => { scrolled = true; },
    focus: () => { focused = true; },
  };
  const details = { open: false, querySelector: () => summary } as unknown as HTMLDetailsElement;
  openPreviousAnswerReview(details);
  assert.equal(details.open, true);
  assert.equal(scrolled, true);
  assert.equal(focused, true);
  assert.deepEqual(selected, ["Search", "Referrals"]);
  assert.equal(freeText, "Try both");
});

test("option display preserves the entire answer, regardless of old labels", () => {
  const long = "A weekly digest of the three newest competitor price changes, emailed on Mondays";
  assert.equal(shortOptionLabel(long, ["Weekly digest"], 0), long);
  assert.equal(shortOptionLabel(long), long);
  assert.equal(shortOptionLabel(long, [], 0), shortOptionLabel(long));
  assert.equal(shortOptionLabel("Yes", undefined, 0), "Yes");
  assert.equal(shortOptionLabel("Internationalization-heavy enterprise procurement teams", ["x".repeat(30)], 0), "Internationalization-heavy enterprise procurement teams");
  assert.deepEqual(savedIdeaOptionLabels({ option_labels: ["A", "B"] }), ["A", "B"]);
  assert.deepEqual(savedIdeaOptionLabels(["A"]), []);
});

test("typed commands leave an open question; ordinary answers stay answers", () => {
  assert.equal(leavesIdeaQuestion("skip these, just find me people i should talk to"), true);
  assert.equal(leavesIdeaQuestion("just find me customers"), true);
  assert.equal(leavesIdeaQuestion("Bakery customers who order on WhatsApp"), false);
  assert.equal(leavesIdeaQuestion("it is hard to find customers"), false);
});
