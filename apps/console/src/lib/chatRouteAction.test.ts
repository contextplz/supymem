import assert from "node:assert/strict";
import test from "node:test";

import { chatRouteAction, homeRouteAction, inChatActions, isChatNavigationAction } from "./chatRouteAction.ts";
import { chatFirstHidden } from "./chatVisibility.ts";

test("direct clarification stays executable while research remains navigation across unrelated domains", () => {
  for (const audience of ["factory maintenance teams", "home gardeners"]) {
    const clarification = {label: `Continue clarifying for ${audience}`, action: "idea.review", style: "primary" as const};
    assert.equal(isChatNavigationAction(clarification), false);
    assert.equal(isChatNavigationAction({label: clarification.label, name: "idea.review", style: "primary"}), false);
    assert.equal(isChatNavigationAction({label: clarification.label, action: "idea.review", style: "secondary", state_hash: audience}), true);
    assert.equal(isChatNavigationAction({label: clarification.label, name: "idea.review"}), true);
    assert.deepEqual(inChatActions([clarification]), [clarification]);
    assert.equal(isChatNavigationAction({label: `Review research for ${audience}`, action: "research", style: "primary"}), true);
    assert.equal(isChatNavigationAction({label: `Next action for ${audience}`, action: "whats_next", style: "primary", state_hash: audience, reply_choice: true}), false);
    assert.equal(isChatNavigationAction({label: `Review research for ${audience}`, action: "research", style: "primary", state_hash: audience, reply_choice: true}), false);
    assert.equal(isChatNavigationAction({label: `Review research for ${audience}`, action: "research", style: "secondary", state_hash: audience, reply_choice: true}), false);
    assert.equal(isChatNavigationAction({label: `Next action for ${audience}`, action: "whats_next", style: "primary", state_hash: audience}), true);
    assert.equal(isChatNavigationAction({label: `Review research for ${audience}`, action: "research", style: "secondary", state_hash: audience}), true);
  }
});

test("venture guides stay reachable instead of becoming next-step chat actions across unrelated domains", () => {
  for (const [ventureId, audience] of [["ven_factories", "factory maintenance teams"], ["ven_gardens", "home gardeners"]]) {
    for (const suffix of ["", "/", "?topic=customers#help"]) {
      const href = `/v/${ventureId}/guide${suffix}`;
      assert.equal(chatFirstHidden(href), false, href);
      assert.equal(homeRouteAction(href, ventureId), null, href);
      assert.equal(homeRouteAction(href, "ven_other"), null, href);
      for (const chatFirst of [true, false]) {
        for (const url of [href, `https://app.supymem.com${href}`]) {
          const action = {label: `Guide for ${audience}`, href: url, style: "primary" as const};
          assert.deepEqual(inChatActions([action], chatFirst), [action]);
        }
      }
    }
  }
});

test("company tabs stay navigable instead of posting next-step recommendations", () => {
  for (const ventureId of ["ven_factories", "ven_gardens"]) {
    for (const tab of ["archives", "toolkit", "departments"]) {
      for (const query of [`office=${tab}`, `filter=saved&office=${tab}`, `%6Fffice=${tab}&department=Research`]) {
        const href = `/v/${ventureId}?${query}#next`;
        assert.equal(chatFirstHidden(href), false);
        assert.equal(homeRouteAction(href, ventureId), null);
        assert.equal(homeRouteAction(href, "ven_other"), null);
        for (const chatFirst of [true, false]) {
          for (const url of [href, `https://app.supymem.com${href}`]) {
            const action = { label: tab, href: url, style: "primary" as const };
            assert.deepEqual(inChatActions([action], chatFirst), [action]);
          }
        }
      }
    }
    for (const suffix of ["", "#next", "?filter=saved#next"]) {
      assert.equal(homeRouteAction(`/v/${ventureId}${suffix}`, ventureId)?.action, "next_moves");
    }
  }
});

test("landing page build links prepare a build in chat rather than opening a preview", () => {
  for (const venture of ["venture-a", "venture-b"]) {
    assert.deepEqual(inChatActions([{ label: "Build", href: `/v/${venture}/site?build=1`, style: "primary" }]),
      [{ label: "Build my landing page", action: "pagebuilder.build", payload: { prepare: true }, style: "primary" }]);
    assert.equal(inChatActions([{ label: "Preview", href: `/v/${venture}/site` }])[0].action, "site.preview");
  }
});

test("offers a review CTA from a saved same-venture app URL", () => {
  const message = { id: "m1", role: "assistant" as const,
    body_md: "Confirm the idea first: https://app.supymem.com/v/ven_123/idea." };
  assert.deepEqual(chatRouteAction(message, "ven_123"), [
    { label: "Review idea", href: "/v/ven_123/idea", style: "primary" },
  ]);
  assert.deepEqual(chatRouteAction(message, "ven_123"), chatRouteAction(message, "ven_123"));
  assert.deepEqual(chatRouteAction({ ...message, body_md: "Next: app.supymem.com/v/ven_123/idea" }, "ven_123"),
    chatRouteAction(message, "ven_123"));
  assert.deepEqual(chatRouteAction({ ...message, body_md: "Next: /v/ven_123/idea" }, "ven_123"),
    chatRouteAction(message, "ven_123"));
});

test("keeps structured actions as the authority", () => {
  const message = { id: "m1", role: "assistant" as const, body_md: "/v/ven_123/idea",
    actions: [{ label: "Review saved work", href: "/v/ven_123/now" }] };
  assert.deepEqual(chatRouteAction(message, "ven_123"), []);
});

test("rejects foreign ventures, hosts, routes, and executable URL variants", () => {
  const rejected = [
    "https://evil.test/v/ven_123/idea",
    "https://evil.test/path/v/ven_123/idea",
    "evil.app.supymem.com/v/ven_123/idea",
    "//evil.test/v/ven_123/idea",
    "http://app.supymem.com/v/ven_123/idea",
    "https://app.supymem.com/v/ven_other/idea",
    "https://app.supymem.com/v/ven_123/idea?approve=true",
    "https://app.supymem.com/v/ven_123/idea#approve",
    "https://app.supymem.com/v/ven_123/device-work-orders/ord_1",
    "https://app.supymem.com/v/ven_123/idea/approve",
  ];
  for (const body_md of rejected) {
    assert.deepEqual(chatRouteAction({ id: "m1", role: "assistant", body_md }, "ven_123"), [], body_md);
  }
  assert.deepEqual(chatRouteAction({ id: "m1", role: "user", body_md: "/v/ven_123/idea" }, "ven_123"), []);
});

test("chat-first: links to hidden pages become in-chat actions; secure forms and other links stay", async () => {
  const { inChatActions } = await import("./chatRouteAction.ts");
  const { chatFirstHidden } = await import("./chatVisibility.ts");
  const out = inChatActions([
    { label: "Import contacts", href: "/gtm?venture=v1&import=1", style: "primary" },
    { label: "Review", href: "/v/v1/site?import=x" }, { label: "Check Work", href: "/v/v1/tasks" },
    { label: "Plan", href: "/v/v1/plan" }, { label: "Idea", href: "/v/v1/idea" },
    { label: "Secure", href: "/connections?venture=v1", kind: "secret_form" } as never,
  ]);
  assert.deepEqual(out.map(a => a.action || a.href), ["gtm.status", "site.preview", "status", "idea.review", "/connections?venture=v1"]);
  const idea = { label: "Idea", href: "/v/v1/idea?edit_previous=1" };
  assert.deepEqual(inChatActions([idea], false), [idea]);
  assert.equal(inChatActions([idea])[0].action, "idea.review");
  assert.equal(inChatActions([{ label: "Idea", href: "/v/v1/idea/current" }])[0].href, "/v/v1/idea/current");
  for (const segment of ["now", "tasks", "plan", "charter", "research", "site", "builds", "analytics", "experiments", "suggestions",
    "context", "settings", "org", "people", "market", "feedback", "evolution", "import", "simulate", "connections"]) {
    assert.equal(chatFirstHidden(`/v/v1/${segment}`), true, segment);
    assert.equal(inChatActions([{ label: "x", href: `/v/v1/${segment}` }])[0].href, undefined, segment);
  }
  for (const href of ["/guide", "/guide?venture=v1", "/v/v1/guide"]) {
    assert.equal(chatFirstHidden(href), false);
    assert.deepEqual(inChatActions([{ label: "How to use Supymem", href }]), [{ label: "How to use Supymem", href }]);
  }
});
