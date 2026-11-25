const assert = require("node:assert/strict");
const { randomBytes } = require("node:crypto");
const { mkdtempSync, readFileSync, rmSync, writeFileSync } = require("node:fs");
const { tmpdir } = require("node:os");
const { join, resolve } = require("node:path");
const { pathToFileURL } = require("node:url");
const test = require("node:test");
const vm = require("node:vm");

test("your public experiment links reach only their token validators without your owner identity", async () => {
  const route = await import(pathToFileURL(join(__dirname, "route.ts")));
  const directory = mkdtempSync(join(tmpdir(), "public-experiments-"));
  const originalEnv = { ...process.env }, originalFetch = global.fetch;
  try {
    process.env.SUPYMEM_MODE = "local";
    process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3100";
    process.env.CORE_URL = "http://127.0.0.1:3300";
    process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE = join(directory, "owner-session.secret");
    writeFileSync(process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE, randomBytes(32).toString("hex"), { mode: 0o600 });
    for (const domain of ["procurement", "gardening"]) {
      const base = `/public/waitlists/${domain}`;
      const experiment = `exp_${"a".repeat(24)}`;
      let calls = 0;
      global.fetch = async (url, init) => {
        assert.equal(init.headers.get("x-forwarded-host"), "127.0.0.1:3100");
        for (const name of ["x-local-proxy-secret", "cookie", "x-subject", "x-cos-assertion", "authorization", "x-api-key", "x-workspace-id"]) {
          assert.equal(init.headers.get(name), null);
        }
        calls += 1;
        return new Response(null, { status: init.method === "POST" ? 422 : 404 });
      };
      const request = (method, path) => new Request(`http://127.0.0.1:3100/api${path}`, {
        method, headers: { Host: "127.0.0.1:3100", Origin: "https://audience.example",
          "Sec-Fetch-Site": "cross-site", "X-Subject": "forged", "X-COS-Assertion": "forged",
          Authorization: "Bearer forged", "X-Api-Key": "forged", "X-Workspace-Id": "forged" },
      });
      for (const [method, path] of [["GET", `${base}/pricing/${experiment}/test`],
          ["GET", `${base}/surveys/${experiment}/${"b".repeat(20)}`],
          ["POST", `${base}/surveys/${experiment}/${"b".repeat(20)}`]]) {
        assert.equal((await route[method](request(method, path))).status, method === "POST" ? 422 : 404);
      }
      for (const [method, path] of [["POST", `${base}/pricing/${experiment}/test`],
          ["GET", `${base}/surveys/${experiment}/short`], ["GET", `${base}/settings`]]) {
        assert.equal((await route[method](request(method, path))).status, 403);
      }
      assert.equal(calls, 3);
    }
  } finally {
    global.fetch = originalFetch;
    for (const name of Object.keys(process.env)) if (!(name in originalEnv)) delete process.env[name];
    Object.assign(process.env, originalEnv);
    rmSync(directory, { recursive: true, force: true });
  }
});

test("your OAuth result reaches the console without granting an owner session", async () => {
  const route = await import(pathToFileURL(join(__dirname, "route.ts")));
  const previousMode = process.env.SUPYMEM_MODE, previousOrigin = process.env.CONSOLE_ORIGIN;
  const previousFetch = global.fetch;
  const consoleOrigin = "http://127.0.0.1:3100";
  try {
    Object.assign(process.env, { SUPYMEM_MODE: "local", CONSOLE_ORIGIN: consoleOrigin });
    for (const domain of ["fulfillment-radar.test", "aquarium-sketches.test"]) {
      const path = "/api/v1/content/oauth/googlesheets/callback";
      const request = new Request(`${consoleOrigin}${path}`, { headers: {
        Host: "127.0.0.1:3100", Referer: `https://${domain}/authorize`, "Sec-Fetch-Site": "cross-site",
      } });
      global.fetch = async (url, init) => {
        assert.equal(new URL(url).pathname, path);
        assert.equal(init.redirect, "error");
        assert.equal(init.headers.get("x-local-proxy-secret"), null);
        return new Response(`<a href="${consoleOrigin}/connections">Open Connections</a>`, {
          headers: { "Content-Type": "text/html", "Referrer-Policy": "no-referrer" },
        });
      };
      const response = await route.GET(request);
      assert.equal(response.status, 200);
      assert.equal(response.headers.get("cache-control"), "no-store");
      assert.equal(response.headers.get("referrer-policy"), "no-referrer");
      assert.equal(response.headers.get("set-cookie"), null);
      assert.match(await response.text(), /Open Connections/);
    }
  } finally {
    global.fetch = previousFetch;
    if (previousMode === undefined) delete process.env.SUPYMEM_MODE;
    else process.env.SUPYMEM_MODE = previousMode;
    if (previousOrigin === undefined) delete process.env.CONSOLE_ORIGIN;
    else process.env.CONSOLE_ORIGIN = previousOrigin;
  }
});

test("your protected health details still require your owner credential", async () => {
  const route = await import(pathToFileURL(join(__dirname, "route.ts")));
  const directory = mkdtempSync(join(tmpdir(), "owner-health-"));
  const originalEnv = { ...process.env }, originalFetch = global.fetch;
  try {
    process.env.SUPYMEM_MODE = "local";
    process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3100";
    process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE = join(directory, "owner-session.secret");
    const secret = randomBytes(32).toString("hex");
    writeFileSync(process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE, secret, { mode: 0o600 });
    for (const domain of ["procurement.example", "gardening.example"]) {
      global.fetch = async (url, init) => {
        assert.equal(new URL(url).pathname, "/health");
        assert.equal(init.headers.get("x-local-proxy-secret") === secret, true);
        return Response.json({ status: "ok", desktop_instance: domain });
      };
      const request = credential => new Request("http://127.0.0.1:3100/api/health", {
        headers: { Host: "127.0.0.1:3100", ...(credential ? { "X-Local-Proxy-Secret": credential } : {}) },
      });
      assert.equal((await route.GET(request())).status, 403);
      assert.equal((await route.GET(request(randomBytes(32).toString("hex")))).status, 403);
      const response = await route.GET(request(secret));
      assert.equal(response.status, 200);
      assert.equal((await response.json()).desktop_instance, domain);
    }
  } finally {
    global.fetch = originalFetch;
    for (const name of Object.keys(process.env)) if (!(name in originalEnv)) delete process.env[name];
    Object.assign(process.env, originalEnv);
    rmSync(directory, { recursive: true, force: true });
  }
});

test("your proxy requires your owner credential and attaches it server-side across domains", async () => {
  const route = await import(pathToFileURL(join(__dirname, "route.ts")));
  const directory = mkdtempSync(join(tmpdir(), "local-proxy-"));
  const filename = join(directory, "secret");
  const originalFetch = global.fetch;
  const originalEnv = Object.fromEntries(["SUPYMEM_MODE", "CORE_URL", "SUPYMEM_LOCAL_PROXY_SECRET_FILE", "COMPANY_OS_DATA_DIR"]
    .map(name => [name, process.env[name]]));
  try {
    process.env.SUPYMEM_MODE = "local";
    process.env.CORE_URL = "http://127.0.0.1:3300";
    for (const domain of ["procurement.example", "gardening.example"]) {
      process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE = filename;
      let expectedSecret = null;
      let calls = 0;
      global.fetch = async (url, init) => {
        assert.equal(url, "http://127.0.0.1:3300/local/providers");
        assert.equal(init.headers.get("x-local-proxy-secret") === expectedSecret, true);
        calls += 1;
        return Response.json({ domain });
      };
      const request = (credential = expectedSecret) => new Request("http://127.0.0.1:3100/api/local/providers", {
        headers: { Host: "127.0.0.1:3100", "X-Forwarded-Host": domain,
          ...(credential ? { "X-Local-Proxy-Secret": credential } : {}) },
      });
      for (const secret of [null, randomBytes(32).toString("hex"), randomBytes(32).toString("hex"), null]) {
        expectedSecret = secret;
        if (secret === null) rmSync(filename, { force: true });
        else writeFileSync(filename, secret, { mode: 0o600 });
        const before = calls;
        const response = await route.GET(request());
        assert.equal(response.status, secret === null ? 503 : 200);
        assert.equal(response.headers.get("cache-control"), "no-store");
        if (secret === null) {
          assert.equal((await response.json()).error.code, "local_proxy_not_ready");
          assert.equal(calls, before);
        } else {
          assert.deepEqual(await response.json(), { domain });
          assert.equal((await route.GET(request("wrong"))).status, 403);
          assert.equal((await route.GET(request(null))).status, 403);
          assert.equal(calls, before + 1);
        }
      }
      delete process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE;
      process.env.COMPANY_OS_DATA_DIR = directory;
      assert.equal((await route.GET(request())).status, 503);
    }
  } finally {
    global.fetch = originalFetch;
    for (const name of ["SUPYMEM_MODE", "CORE_URL", "SUPYMEM_LOCAL_PROXY_SECRET_FILE", "COMPANY_OS_DATA_DIR"]) {
      if (originalEnv[name] === undefined) delete process.env[name];
      else process.env[name] = originalEnv[name];
    }
    rmSync(directory, { recursive: true, force: true });
  }
});

test("your document proof reaches only your console API and never needs a native credential", async () => {
  const source = readFileSync(resolve(__dirname, "../../../../../../src-tauri/src/nav_guard.rs"), "utf8");
  assert.doesNotMatch(source, /X-Local-Proxy-Secret|__localOwnerConnect/);
  const layout = readFileSync(resolve(__dirname, "../../layout.tsx"), "utf8");
  const script = layout.match(/\? `(\(\(\) =>[\s\S]+?)` : ""/)[1];
  for (const domain of ["procurement.example", "gardening.example"]) {
    const proof = randomBytes(32).toString("hex");
    const calls = [];
    const context = { Request, Headers, URL, location: { protocol: "http:", hostname: "127.0.0.1",
      origin: "http://127.0.0.1:3100" }, window: { fetch: async request => { calls.push(request); return Response.json({ domain }); } } };
    vm.runInNewContext(script.replace('${JSON.stringify(proof)}', JSON.stringify(proof)), context);
    const pending = context.window.fetch("http://127.0.0.1:3100/api/local/providers");
    assert.equal(calls.length, 1);
    await context.window.fetch(`https://${domain}/api/status`);
    assert.equal(calls[1].headers.get("x-local-session-proof"), null);
    assert.deepEqual(await (await pending).json(), { domain });
    assert.equal(calls[0].headers.get("x-local-session-proof") === proof, true);
    assert.equal(calls[0].headers.get("x-local-proxy-secret"), null);
  }
});

test("your browser session is issued only with your credential at the exact console origin", async () => {
  const route = await import(pathToFileURL(join(__dirname, "route.ts")));
  const directory = mkdtempSync(join(tmpdir(), "local-owner-"));
  const originalFetch = global.fetch;
  const originalEnv = { ...process.env };
  try {
    process.env.SUPYMEM_MODE = "local";
    process.env.COMPANY_OS_DATA_DIR = directory;
    delete process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE;
    const secret = randomBytes(32).toString("hex");
    const proof = randomBytes(32).toString("hex");
    writeFileSync(join(directory, "owner-session.secret"), secret, { mode: 0o600 });
    for (const domain of ["procurement.example", "gardening.example"]) {
      let calls = 0;
      global.fetch = async (url, init) => {
        assert.equal(init.headers.get("x-local-proxy-secret") === secret, true);
        assert.equal(init.headers.get("x-local-session-proof"), null);
        calls += 1;
        return Response.json({ domain });
      };
      const request = (headers = {}, method = "POST", path = "/api/local/owner-session") => new Request(
        `http://127.0.0.1:3100${path}`, { method, headers: { Host: "127.0.0.1:3100", ...headers } });
      assert.equal((await route.POST(request())).status, 403);
      assert.equal((await route.POST(request({ "X-Local-Proxy-Secret": secret,
        Origin: "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-site" }))).status, 403);
      assert.equal((await route.POST(request({ "X-Local-Proxy-Secret": secret, Host: `${domain}:3100` }))).status, 403);
      assert.equal(calls, 0);
      const response = await route.POST(request({ "X-Local-Proxy-Secret": secret, "X-Local-Session-Proof": proof,
        Origin: "http://127.0.0.1:3100" }));
      assert.equal(response.status, 200);
      const cookie = response.headers.get("set-cookie");
      assert.match(cookie, /HttpOnly/);
      assert.match(cookie, /SameSite=Strict/);
      assert.match(cookie, /Path=\/api/);
      assert.doesNotMatch(cookie, /Domain=/);
      assert.equal(cookie.includes(secret), false);
      assert.equal(JSON.stringify(await response.json()).includes(secret), false);
      assert.equal((await route.GET(request({ Cookie: cookie.split(";")[0], "Sec-Fetch-Site": "same-origin" },
        "GET", "/api/local/providers"))).status, 403);
      assert.equal((await route.GET(request({ Cookie: cookie.split(";")[0], "X-Local-Session-Proof": proof,
        "Sec-Fetch-Site": "same-origin" },
        "GET", "/api/local/providers"))).status, 200);
      assert.equal(calls, 1);
    }
  } finally {
    global.fetch = originalFetch;
    for (const name of Object.keys(process.env)) if (!(name in originalEnv)) delete process.env[name];
    Object.assign(process.env, originalEnv);
    rmSync(directory, { recursive: true, force: true });
  }
});
