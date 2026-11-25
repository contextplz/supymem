const assert = require("node:assert/strict");
const { readFileSync } = require("node:fs");
const { join } = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

function loadMiddleware(env = process.env) {
  const context = { exports: {}, process: { env, platform: process.platform, geteuid: process.geteuid }, URL, Headers,
    require: name => name === "next/server" ? { NextResponse: class extends Response {
      static next(options = {}) {
        const response = new this(null);
        response.requestHeaders = options.request?.headers;
        return response;
      }
      static redirect(url) { return new this(null, { status: 307, headers: { Location: url.href } }); }
    } } : name === "@/lib/betaRoutes" ? { isBetaHiddenPath: pathname => pathname === "/lab" }
      : name === "@/lib/localMode" ? require("./lib/localMode.ts") : require(name),
  };
  vm.runInNewContext(require("typescript").transpileModule(readFileSync(join(__dirname, "middleware.ts"), "utf8"), {
    compilerOptions: { module: require("typescript").ModuleKind.CommonJS },
  }).outputText, context);
  return context.exports.middleware;
}

test("your first-party document signs you in through the real proxy without sharing its proof with other origins", async () => {
  const { randomBytes } = require("node:crypto");
  const { mkdtempSync, writeFileSync, rmSync } = require("node:fs");
  const directory = mkdtempSync(join(require("node:os").tmpdir(), "owner-navigation-"));
  const originalEnv = { ...process.env }, originalFetch = global.fetch;
  try {
    process.env.SUPYMEM_MODE = "local";
    process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3100";
    process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE = join(directory, "owner-session.secret");
    const secret = randomBytes(32).toString("hex");
    writeFileSync(process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE, secret, { mode: 0o600 });
    const middleware = loadMiddleware();
    const route = await import("./app/api/[...path]/route.ts");
    for (const domain of ["procurement.example", "gardening.example"]) {
      const url = `http://127.0.0.1:3100/?venture=${domain}`;
      const document = (extra = {}) => ({ url, method: "GET", nextUrl: new URL(url), headers: new Headers({
        Host: "127.0.0.1:3100", Accept: "text/html", "Sec-Fetch-Site": "none", "Sec-Fetch-Dest": "document", ...extra,
      }) });
      let calls = 0;
      global.fetch = async (url, init) => {
        assert.equal(init.headers.get("x-local-proxy-secret") === secret, true);
        assert.equal(init.headers.get("x-local-session-proof"), null);
        calls += 1;
        return Response.json({ domain });
      };
      const api = (headers = {}, method = "GET") => new Request("http://127.0.0.1:3100/api/local/providers", {
        method, headers: { Host: "127.0.0.1:3100", ...headers },
      });
      assert.equal((await route.GET(api())).status, 403);
      const response = await middleware(document());
      assert.equal(response.status, 200);
      const cookie = response.headers.get("set-cookie");
      assert.match(cookie, /HttpOnly; SameSite=Strict/);
      assert.match(cookie, /Path=\/api/);
      assert.doesNotMatch(cookie, /Domain=/);
      assert.equal(cookie.includes(secret), false);
      const proof = response.requestHeaders.get("x-local-owner-proof");
      assert.match(proof, /^[0-9a-f]{64}$/);
      assert.notEqual(proof, secret);
      const session = { Cookie: cookie.split(";")[0], "X-Local-Session-Proof": proof, "Sec-Fetch-Site": "same-origin" };
      assert.deepEqual(await (await route.GET(api(session))).json(), { domain });
      assert.equal((await route.GET(api({ Cookie: session.Cookie, "Sec-Fetch-Site": "same-origin" }))).status, 403);
      assert.equal((await route.POST(api({ ...session, Origin: "http://127.0.0.1:8000", "Sec-Fetch-Site": "same-site" }, "POST"))).status, 403);
      assert.equal((await route.GET(api({ ...session, Host: `${domain}:3100` }))).status, 403);
      assert.equal(calls, 1);
      for (const extra of [{ "Sec-Fetch-Site": "cross-site" }, { "Sec-Fetch-Site": "same-site" },
        { Origin: "http://127.0.0.1:8000" }, { Referer: `https://${domain}/` }, { Host: `${domain}:3100` },
        { "X-Forwarded-Host": "127.0.0.1:8000" }, { "Sec-Fetch-Dest": "iframe" }]) {
        const refused = await middleware(document(extra));
        assert.equal(refused.status, 403);
        assert.equal(refused.headers.get("set-cookie"), null);
      }
      const second = await middleware(document());
      assert.equal(second.headers.get("set-cookie"), cookie);
      process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3101";
      const moved = new Request("http://127.0.0.1:3101/api/local/providers", {
        headers: { ...session, Host: "127.0.0.1:3101", Origin: "http://127.0.0.1:3101" },
      });
      assert.equal((await route.GET(moved)).status, 403);
      process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3100";
      process.env.SUPYMEM_LOCAL_ALLOW_REMOTE = "1";
      process.env.CONSOLE_ORIGIN = `http://${domain}:3100`;
      const remoteUrl = `http://${domain}:3100/`;
      assert.equal((await middleware({ ...document({ Host: `${domain}:3100` }), url: remoteUrl, nextUrl: new URL(remoteUrl) })).status, 403);
      delete process.env.SUPYMEM_LOCAL_ALLOW_REMOTE;
      process.env.CONSOLE_ORIGIN = "http://127.0.0.1:3100";
      writeFileSync(process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE, randomBytes(32).toString("hex"));
      const restarted = await middleware(document());
      assert.notEqual(restarted.headers.get("set-cookie"), cookie);
      assert.equal((await route.GET(api(session))).status, 403);
      writeFileSync(process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE, secret);
    }
  } finally {
    global.fetch = originalFetch;
    for (const name of Object.keys(process.env)) if (!(name in originalEnv)) delete process.env[name];
    Object.assign(process.env, originalEnv);
    rmSync(directory, { recursive: true, force: true });
  }
});

for (const domain of ["procurement.example", "gardening.example"]) {
  test(`your local console rejects rebound hosts before forwarding for ${domain}`, async () => {
    const env = { SUPYMEM_MODE: " LOCAL " };
    const middleware = loadMiddleware(env);
    function request(host, forwarded = "localhost:3100", pathname = "/api/auth/session") {
      return { headers: new Headers({ host, "x-forwarded-host": forwarded }), url: "http://localhost:3100/",
        nextUrl: { pathname, searchParams: new URLSearchParams({venture: domain}) } };
    }
    for (const host of [domain, `localhost@${domain}`, "localhost/", "localhost?", "localhost:invalid", ""]) {
      assert.equal((await middleware(request(host))).status, 403);
    }
    for (const host of ["localhost:3100", "127.0.0.1:3100", "127.0.0.2", "[::1]:3100"]) {
      assert.equal((await middleware(request(host))).status, 200);
    }
    assert.equal((await middleware(request("localhost", domain))).status, 403);
    assert.equal((await middleware(request("localhost", "localhost", "/lab"))).status, 200);
    for (const path of ["/admin/login", "/setup/permissions", `/v/${domain}/builds`, "/ads"]) {
      assert.equal((await middleware(request("localhost", "localhost", path))).status, 200);
    }
    for (const path of ["/auth/login", "/invite/token"]) {
      const response = await middleware(request("localhost", "localhost", path));
      assert.equal(response.status, 307);
      const destination = new URL(response.headers.get("location"));
      assert.equal(destination.pathname + destination.search, `/v/${domain}?local_notice=1`);
    }
    assert.ok(!/\(\?![^)]*api\//.test(readFileSync(join(__dirname, "middleware.ts"), "utf8")));
    delete env.SUPYMEM_MODE;
    assert.equal((await middleware(request(domain, domain))).status, 200);
    assert.equal((await middleware(request(domain, domain, "/lab"))).status, 404);
  });
}
