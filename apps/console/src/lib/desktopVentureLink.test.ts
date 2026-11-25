import test from "node:test";
import assert from "node:assert/strict";
import { DESKTOP_LINK_INTENT_KEY, desktopDownloadLink, desktopLinkError, desktopVentureLink, desktopVentureLinkAtLocation, rememberDesktopLink, takeDesktopLink } from "./desktopVentureLink.ts";

test("email paths preserve their exact venture, build selection and review anchor", () => {
  for (const path of ["/", "/new", "/new?venture=ven_a", "/gtm?venture=ven_a", "/v/ven_a?chat=1",
    "/v/ven_a/now", "/v/ven_a/research", "/v/ven_a/plan", "/v/ven_a/tasks", "/v/ven_a/site?create=1",
    "/v/ven_a/analytics", "/v/ven_a/settings", "/v/ven_a/device-work-orders/dwo_a",
    "/v/ven_a/builds?app=app_a&run=build_a#build-review"]) {
    const link = new URL(desktopVentureLink("https://app.example.invalid", path)!);
    assert.equal(link.protocol, "supymem:");
    assert.equal(link.searchParams.get("path"), path);
  }
  assert.match(desktopVentureLink("https://app.staging.example.invalid", "/new")!, /^supymem-staging:/);
});

test("handoff excludes foreign origins, redirect queries and ambiguous routes", () => {
  for (const origin of ["https://evil.test", "https://app.example.invalid.evil.test", "http://app.example.invalid", "https://app.example.invalid:443", "http://127.0.0.1:3100"]) {
    assert.equal(desktopVentureLink(origin, "/new"), null, origin);
  }
  for (const path of ["https://evil.test", "//evil.test/v/ven_a", "/v/ven_a/../account", "/v/ven_a/%2e%2e/account",
    "/v/ven_a\\now", "/v/ven_a/now\n", "/account", "/gtm", "/new?next=https://evil.test", "/new?venture=ven_a&venture=ven_b",
    "/v/ven_a?chat=1#unexpected", "/v/ven_a/site?create=2", "/v/ven_a/builds?app=a&app=b", "/v/ven_a/builds?app=a&run=b&next=/account",
    "/v/not_a_venture/now", "/v/ven_a/unknown", "/v/ven_a/device-work-orders/not_an_order", "/new#fragment"]) {
    assert.equal(desktopVentureLink("https://app.example.invalid", path), null, path);
  }
});

test("native failure messages are closed and never include an untrusted payload", () => {
  for (const code of ["sign_in", "not_available", "unavailable"]) assert.ok(desktopLinkError(code));
  for (const value of [null, "https://evil.test", { message: "private" }]) assert.equal(desktopLinkError(value), null);
});

test("a later user click reads the current query and hash on the same route", () => {
  const location = { origin: "https://app.example.invalid", pathname: "/v/ven_a/builds", search: "?app=app_a&run=build_a", hash: "" };
  const old = desktopVentureLinkAtLocation(location);
  location.search = "?app=app_a&run=build_b";
  location.hash = "#build-review";
  const current = desktopVentureLinkAtLocation(location);
  assert.notEqual(current, old);
  assert.equal(new URL(current!).searchParams.get("path"), "/v/ven_a/builds?app=app_a&run=build_b#build-review");
  location.search = "?next=https://evil.test";
  assert.equal(desktopVentureLinkAtLocation(location), null);
});

test("browser auth retains only a validated current-origin route once, without credentials", () => {
  const values = new Map<string, string>();
  const storage = { getItem: (key: string) => values.get(key) || null, setItem: (key: string, value: string) => { values.set(key, value); }, removeItem: (key: string) => { values.delete(key); } };
  const path = "/v/ven_a/builds?app=app_a&run=build_a#build-review";
  rememberDesktopLink("https://app.example.invalid", path, storage, 1000);
  assert.deepEqual(Object.keys(JSON.parse(values.get(DESKTOP_LINK_INTENT_KEY)!)).sort(), ["issued_at", "origin", "path", "version"]);
  assert.equal(takeDesktopLink("https://app.example.invalid", storage, 2000), path);
  assert.equal(takeDesktopLink("https://app.example.invalid", storage, 2000), null);
  for (const invalid of ["/auth/consume?token=private", "/v/ven_a?next=https://evil.test", "/v/ven_" + "x".repeat(125)]) {
    rememberDesktopLink("https://app.example.invalid", invalid, storage, 1000);
    assert.equal(values.size, 0);
  }
  for (const [origin, now] of [["https://app.staging.example.invalid", 2000], ["https://app.example.invalid", 999], ["https://app.example.invalid", 901001]] as const) {
    rememberDesktopLink("https://app.example.invalid", path, storage, 1000);
    assert.equal(takeDesktopLink(origin, storage, now), null);
    assert.equal(values.size, 0);
  }
  values.set(DESKTOP_LINK_INTENT_KEY, JSON.stringify({ version: 1, origin: "https://app.example.invalid", path, issued_at: 1000, token: "private" }));
  assert.equal(takeDesktopLink("https://app.example.invalid", storage, 2000), null);
  assert.equal(desktopDownloadLink("https://app.example.invalid"), "https://example.invalid/downloads/Supymem.dmg");
  assert.equal(desktopDownloadLink("https://app.staging.example.invalid"), null);
});

test("blocked tab storage cannot block the literal app launch or turn into an auth redirect", () => {
  const storage = { getItem() { throw Error("blocked"); }, setItem() { throw Error("blocked"); }, removeItem() { throw Error("blocked"); } };
  assert.doesNotThrow(() => rememberDesktopLink("https://app.example.invalid", "/v/ven_a", storage, 1000));
  assert.equal(takeDesktopLink("https://app.example.invalid", storage, 1001), null);
  assert.ok(desktopVentureLink("https://app.example.invalid", "/v/ven_a"));
});
