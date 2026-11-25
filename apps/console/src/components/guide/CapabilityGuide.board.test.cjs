const assert = require("node:assert/strict");
const test = require("node:test");

test("board and competitor guide cards work for unrelated business and consumer ventures", async () => {
  const guide = await import("../../lib/capabilityGuide.ts");
  for (const venture of [{ id: "ven_shipping", audience: "business shipping teams" }, { id: "ven_sewing", audience: "home sewing beginners" }]) {
    const board = guide.filterGuideCapabilities("pressure-test", "Start & shape", "An idea").find(card => card.id === "idea-board");
    const grid = guide.filterGuideCapabilities("competitor grid", "Research", "An idea").find(card => card.id === "competitor-grid");
    assert.equal(board.prompt, "Pressure-test my idea");
    assert.equal(grid.prompt, "Show my competitor grid");
    assert.match(board.note, /requested review starts directly.*not evidence.*never changes your research verdict/i);
    assert.match(board.result, /optional tasks/);
    assert.match(grid.result, /under three distinct reviews are thin.*no clear gap found/);
    for (const card of [board, grid]) {
      assert.equal(guide.guideDestinationHref(card.destination, venture.id), `/v/${venture.id}`);
      assert.doesNotMatch(JSON.stringify(card), /[\u2013\u2014]/);
      assert.doesNotMatch(JSON.stringify(card), /credits?/i);
      assert.ok(!JSON.stringify(card).includes(venture.audience));
    }
    for (const id of ["leads", "market-beliefs"]) {
      assert.ok(guide.GUIDE_CAPABILITIES.some(card => card.id === id));
    }
  }
});
