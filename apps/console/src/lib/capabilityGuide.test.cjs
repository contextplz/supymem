const assert = require("node:assert/strict");
const test = require("node:test");

test("local landing-page guide uses your tool and the shared build activity", async () => {
  const { guideContent } = await import("./capabilityGuide.ts");
  const page = guideContent(true).capabilities.find((card) => card.id === "page");
  assert.match(page.description, /own connected coding tool/);
  assert.match(page.result, /Watch or stop.*Build activity/);
  assert.match(page.note, /Settings > Keys and connections > Coding tools/);
  assert.match(page.result, /one fix round/);
  assert.deepEqual(page.requires, ["landing_pages"]);
});

test("local guide links match the headings in Keys and connections", async () => {
  const { guideContent, guideDestinationHref } = await import("./capabilityGuide.ts");
  for (const venture of ["warehouse-inventory", "weekend-baking"]) {
    const { capabilities, faqs } = guideContent(true);
    const publishing = capabilities.find(card => card.id === "publish");
    const apps = capabilities.find(card => card.id === "connections");
    const search = faqs.find(faq => faq.question === "Do I need a search key?");
    for (const [entry, copy, title, section] of [
      [publishing, publishing.description, "Your page publishing account", "publishing"],
      [apps, apps.description, "Your connected apps", "composio"],
      [search, search.answer, "Your search and page reader", "search"],
    ]) {
      assert.ok(copy.includes(`${title}.`) || copy.includes(`${title} and`));
      assert.deepEqual(entry.settingsLink, { title, href: `/settings/local#${section}-title` });
    }
    assert.equal(guideDestinationHref(apps.destination, venture, true), `/connections?venture=${venture}`);
  }
});
