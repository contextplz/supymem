import assert from "node:assert/strict";
import test from "node:test";
import { chatFirstHidden, isIdeaJourneyPath, showsDetailToggle } from "./chatVisibility.ts";

test("the idea journey alone suppresses the global chat launcher", () => {
  assert.equal(isIdeaJourneyPath("/v/venture-1/idea"), true);
  assert.equal(isIdeaJourneyPath("/v/venture-1/idea/"), true);
  assert.equal(isIdeaJourneyPath("/v/venture-1"), false);
  assert.equal(isIdeaJourneyPath("/v/venture-1/now"), false);
  assert.equal(isIdeaJourneyPath("/v/venture-1/tasks"), false);
  assert.equal(isIdeaJourneyPath("/v/venture-1/ideation"), false);
});

test("detail toggle only appears on overview, site, research, plan and idea", () => {
  for (const suffix of ["", "/", "/site", "/research", "/plan", "/idea", "/idea/"])
    assert.equal(showsDetailToggle(`/v/venture-1${suffix}`), true, suffix);
  for (const path of ["/", "/new", "/account", "/v/venture-1/now", "/v/venture-1/tasks", "/v/venture-1/settings", "/v/venture-1/chat", "/v/venture-1/ideas", "/v/venture-1/site/preview"])
    assert.equal(showsDetailToggle(path), false, path);
});

test("chat-first hides the legacy journey but keeps the living idea and company sidebar pages", () => {
  for (const path of ["/v/v1/idea", "/v/v1/idea/?chat=1", "/v/v1/idea?edit_previous=1", "/v/v1/tasks", "/v/v1/plan", "/v/v1/research/", "/v/v1/settings", "/gtm?venture=v1"])
    assert.equal(chatFirstHidden(path), true, path);
  for (const path of ["/guide", "/guide?venture=v1", "/v/v1/guide", "/v/v1/guide/", "/v/v1", "/v/v1?office=archives", "/v/v1/idea/current", "/v/v1/linkedin", "/v/v1/connect/github", "/v/v1/device-work-orders/o1", "/", "/new", "/account", "/admin"])
    assert.equal(chatFirstHidden(path), false, path);
});
