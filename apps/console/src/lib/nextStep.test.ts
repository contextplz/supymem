import assert from "node:assert/strict";
import test from "node:test";
import { dedupeStatusCta, failureOwnsCta, nextStepButton, ownedByGtmFlow, selectNextStep } from "./nextStep.ts";

const fallback = { key: "clarify", eyebrow: "Step 1", title: "Local", detail: "d", path: "idea", cta: "Go" } as const;
const server = { key: "plan.review", stage: "Step 3", title: "Server", detail: "s", cta_label: "Review", chat_action: null, payload: null, href: "/v/x/plan" };

test("idea-stage next steps show saved review in chat while classic keeps its journey link", () => {
  for (const venture of ["clinic", "library"]) {
    const idea = {...server, key: "idea.validate", href: `/v/${venture}/idea`, stage: "idea"};
    assert.deepEqual(nextStepButton(idea, `/v/${venture}`, true, true), {kind: "action", action: "idea.review", payload: {}, label: "Review"});
    assert.deepEqual(nextStepButton(idea, `/v/${venture}`, false, false), {kind: "link", href: idea.href, label: "Review"});
  }
});

test("server next step wins when valid", () => {
  assert.deepEqual(selectNextStep(server, fallback, "/v/x/idea"), { eyebrow: "Step 3", title: "Server", detail: "s", cta: "Review", href: "/v/x/plan" });
});
test("missing or malformed server step falls back to the client journey", () => {
  for (const bad of [undefined, null, {}, { ...server, href: "https://evil.example" }, { ...server, href: "//evil.example" }])
    assert.equal(selectNextStep(bad, fallback, "/v/x/idea")?.title, "Local");
  assert.equal(selectNextStep(null, null, ""), null);
});
test("nextStepButton picks link, chat, scroll or nothing", () => {
  assert.deepEqual(nextStepButton(server, "/v/x/idea"), { kind: "link", href: "/v/x/plan", label: "Review" });
  assert.equal(nextStepButton(server, "/v/x/plan"), null);
  assert.deepEqual(nextStepButton({ ...server, href: "/v/x/plan#plan-review" }, "/v/x/plan"), { kind: "scroll", id: "plan-review", label: "Review" });
  assert.deepEqual(nextStepButton({ ...server, chat_action: "plan.generate" }, "/v/x/plan"), { kind: "action", action: "plan.generate", payload: {}, label: "Review" });
  assert.equal(nextStepButton({ title: "x" }, "/v/x"), null);
});
test("chat actions that would run unconfirmed navigate instead of sending text", () => {
  for (const a of ["research.start", "work.start", "site.publish", "waitlist.enable"])
    assert.deepEqual(nextStepButton({ ...server, chat_action: a }, "/v/x/idea"), { kind: "link", href: "/v/x/plan", label: "Review" });
  assert.equal(nextStepButton({ ...server, chat_action: "work.start" }, "/v/x/plan"), null);
});
test("a saved-context research request can prefill chat without starting a paid action", () => {
  const request = { ...server, key: "research.start", href: "/v/x/research", chat_action: "chat.prefill",
    payload: { text: "Start research from my saved idea and website context." } };
  assert.deepEqual(nextStepButton(request, "/v/x/research"), { kind: "action", action: "chat.prefill",
    payload: request.payload, label: "Review" });
});
test("idea review opens a closed chat but never offers to reopen the founder's current chat", () => {
  const review = { ...server, key: "idea.review_validation", cta_label: "Review idea", href: "/v/x?chat=1" };
  const link = { kind: "link", href: review.href, label: review.cta_label };
  assert.deepEqual(nextStepButton(review, "/v/x"), link);
  assert.deepEqual(nextStepButton(review, "/v/x/plan"), link);
  assert.equal(nextStepButton(review, "/v/x/plan", true), null);
  assert.equal(nextStepButton(review, "/v/x", true), null);
  assert.equal(nextStepButton(review, "/new"), null);
  assert.deepEqual(nextStepButton(server, "/v/x/idea", true), { kind: "link", href: server.href, label: server.cta_label });
});
test("retry and build buttons post their payload to chat instead of navigating", () => {
  assert.deepEqual(nextStepButton({ ...server, chat_action: "job.retry", payload: { kind: "idea.validate" } }, "/v/x/idea"),
    { kind: "action", action: "job.retry", payload: { kind: "idea.validate" }, label: "Review" });
  assert.deepEqual(nextStepButton({ ...server, chat_action: "build.prepare", payload: { plan_task_id: "pt_1" } }, "/v/x/idea"),
    { kind: "action", action: "build.prepare", payload: { plan_task_id: "pt_1" }, label: "Review" });
});
test("dedupeStatusCta hides the bar CTA when the strip has one", () => {
  assert.equal(dedupeStatusCta({ label: "Go", href: "/v/x/plan" }, server), null);
  assert.equal(dedupeStatusCta({ label: "Fix", href: "/v/x/settings" }, server), null);
  assert.deepEqual(dedupeStatusCta({ label: "Go", href: "/v/x/plan" }, null), { label: "Go", href: "/v/x/plan" });
});

test("status bar CTA is dropped when the strip has one, unless a different failure", () => {
  const step = { key: "idea.clarify", stage: "Idea", title: "t", detail: "", cta_label: "Continue clarification", chat_action: null, payload: null, href: "/v/x/idea" };
  const cta = { label: "Answer question", href: "/v/x/idea?q=1" };
  assert.equal(dedupeStatusCta(cta, step, "needs_you", "/v/x/plan"), null);
  assert.deepEqual(dedupeStatusCta(cta, step, "working", "/v/x/plan"), cta);
  const err = { label: "See error", href: "/v/x/builds" };
  assert.deepEqual(dedupeStatusCta(err, step, "failed", "/v/x/plan"), err);
  assert.equal(dedupeStatusCta(err, { ...step, key: "plan.retry" }, "failed", "/v/x/plan"), null);
  assert.deepEqual(dedupeStatusCta(cta, null, "needs_you", "/"), cta);
});


test("queued receipt overrides stale dashboard working copy", () => {
  const fallback = { key: "work", workState: "queued", eyebrow: "Work is queued", title: "Company work is queued",
    detail: "Execution remains unconfirmed.", path: "tasks", cta: "Review saved work" } as const;
  const chosen = selectNextStep({ key: "work.running", stage: "Supymem is working", title: "Work in progress",
    detail: "continues", cta_label: "Open", href: "/v/a/tasks", chat_action: null, payload: null }, fallback, "/v/a/tasks");
  assert.equal(chosen?.title, "Company work is queued");
});

test("queued jobs do not hide an authoritative required-input or recovery step", () => {
  const work = { key: "work", workState: "queued", eyebrow: "Queued", title: "Queued", detail: "", path: "tasks", cta: "Work" } as const;
  for (const key of ["idea.clarify", "research.retry", "venture.paused"])
    assert.equal(selectNextStep({ ...server, key }, work, "/v/x/tasks")?.title, "Server");
});

test("active work keeps one progress CTA in the bar and never a self-link", () => {
  const step = { ...server, key: "work.running", href: "/v/x/now", cta_label: "View progress" };
  const cta = { label: "View progress", href: "/v/x/now" };
  assert.deepEqual(dedupeStatusCta(cta, step, "working", "/v/x/research"), cta);
  for (const href of ["/v/x/now", "/v/x/now#progress", "/v/x/now?view=all"])
    assert.equal(dedupeStatusCta({ ...cta, href }, step, "working", "/v/x/now"), null);
  assert.equal(dedupeStatusCta(cta, null, "working", "/v/x/now"), null);
});

test("a same-screen review anchor outside Now remains actionable", () => {
  const cta = { label: "Review the saved result", href: "/v/x/builds#build-review" };
  assert.deepEqual(dedupeStatusCta(cta, null, "needs_you", "/v/x/builds"), cta);
});

test("visible page choices suppress a dominant duplicate build CTA on the Website page", () => {
  const pair = { ...server, key: "site.build", href: "/v/x/site?build=1", alternatives: [
    {choice: "build", label: "Build the landing page", href: "/v/x/site?build=1"},
    {choice: "skip", label: "Find customers first", href: "/gtm?venture=x"},
  ] };
  assert.equal(dedupeStatusCta({href: pair.href}, pair, "needs_you", "/v/x/site"), null);
  const recovery = {href: "/settings"};
  assert.deepEqual(dedupeStatusCta(recovery, pair, "failed", "/v/x/site"), recovery);
  // Unrelated local product alternatives must not be interpreted as page choices.
  assert.deepEqual(dedupeStatusCta(recovery, {...pair, key: "build.product"}, "needs_you", "/v/x/site"), recovery);
});

test("Website cards own their actions, so the status bar does not repeat a self-link", () => {
  const step = {...server, key: "site.publish", href: "/v/x/site", chat_action: "site.publish"};
  for (const href of ["/v/x/site", "/v/x/site?build=1", "/v/x/site#preview"])
    assert.equal(dedupeStatusCta({href}, step, "needs_you", "/v/x/site"), null);
  assert.deepEqual(dedupeStatusCta({href: "/settings"}, step, "failed", "/v/x/site"), {href: "/settings"});
});

test("a failed run shows its recovery CTA once: the bar keeps it, the strip button yields", () => {
  const step = { key: "idea.clarify", stage: "Idea", title: "t", detail: "", cta_label: "Continue clarification", chat_action: null, payload: null, href: "/v/x/idea" };
  const credits = { label: "Request credits", href: "/v/x/settings" };
  assert.deepEqual(dedupeStatusCta(credits, step, "failed", "/v/x/idea"), credits);
  assert.equal(failureOwnsCta(step, "failed"), true);
  assert.equal(failureOwnsCta(step, "needs_you"), false);
  assert.equal(failureOwnsCta({ ...step, key: "plan.retry" }, "failed"), false);
});

test("growth-experiment step is suppressed on the Find customers page only", () => {
  const exp = { key: "experiment.plan", stage: "Learn", title: "t", detail: "", cta_label: "Review experiments", chat_action: null, payload: null, href: "/v/x/experiments" };
  assert.equal(ownedByGtmFlow(exp, "/gtm"), true);
  assert.equal(ownedByGtmFlow(exp, "/v/x/market"), true);
  assert.equal(ownedByGtmFlow(exp, "/v/x/plan"), false);
  assert.equal(ownedByGtmFlow({ ...exp, key: "site.build" }, "/gtm"), false);
});
