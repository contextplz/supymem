import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import { randomUUID } from "node:crypto";
import ts from "typescript";

const source = fs.readFileSync(new URL("../../next.config.ts", import.meta.url), "utf8");

async function securityHeaders(env: Record<string, string>) {
  const context = vm.createContext({ process: { env: { ...env } }, randomUUID, URL });
  const code = ts.transpileModule(source.replace(/^import .*;\n/gm, "").replace(
    "export default nextConfig;", "globalThis.config = nextConfig;"), {
    compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.None },
  }).outputText;
  vm.runInContext(code, context);
  const rules = await context.config.headers();
  assert.equal(rules[0].source, "/:path*");
  const headers = Object.fromEntries(rules[0].headers.map((header: { key: string; value: string }) => [header.key, header.value]));
  return { headers, mode: context.config.env.NEXT_PUBLIC_SUPYMEM_MODE };
}

function assertLocalPolicy(csp: string) {
  assert.match(csp, /(?:^|; )report-uri \/api\/csp-report(?:;|$)/);
  assert.doesNotMatch(csp, /127\.0\.0\.1:3200|\.supymem\.com/);
}

test("console reports CSP without enforcing resource restrictions; anti-framing is real", async () => {
  for (const domain of ["warehouse-planning.test", "weekend-gardens.test"]) {
    const env = { NODE_ENV: "production", NEXT_PUBLIC_GATEWAY_URL: `https://${domain}/base` };
    const local = await securityHeaders({ ...env, SUPYMEM_MODE: "local" });
    assert.equal(local.headers["Content-Security-Policy"], "frame-ancestors 'none'");
    assert.equal(local.headers["X-Frame-Options"], "DENY");
    assert.equal(local.headers["X-Content-Type-Options"], "nosniff");
    assert.equal(local.headers["Referrer-Policy"], "strict-origin-when-cross-origin");
    const csp = local.headers["Content-Security-Policy-Report-Only"];
    for (const source of ["https://fonts.googleapis.com", "https://fonts.gstatic.com",
      "https://api.openai.com", "data:", "blob:", "ipc:"]) {
      assert.ok(csp.includes(source), source);
    }
    assert.ok(csp.includes("script-src 'self' 'unsafe-inline'"));
    assert.ok(!csp.includes("'unsafe-eval'"));
    assert.ok(csp.includes("frame-src 'self' https: http: data: blob:"));
    assertLocalPolicy(csp);
    assert.ok(!csp.includes(domain));

    const defaults = await securityHeaders(env);
    const defaultPolicy = defaults.headers["Content-Security-Policy-Report-Only"];
    if (defaults.mode === "local") {
      assert.equal(defaultPolicy, csp);
    } else {
      for (const source of [`https://${domain}`, `wss://${domain}`, "https://events.supymem.com",
        `report-uri https://${domain}/csp-report`]) {
        assert.ok(defaultPolicy.includes(source), source);
      }
    }
  }
});

test("development allows Next eval and uses the report endpoint for its mode", async () => {
  const env = { NODE_ENV: "development" };
  const local = await securityHeaders({ ...env, SUPYMEM_MODE: "local" });
  const csp = local.headers["Content-Security-Policy-Report-Only"];
  assert.ok(csp.includes("'unsafe-eval'"));
  assertLocalPolicy(csp);

  const defaults = await securityHeaders(env);
  const defaultPolicy = defaults.headers["Content-Security-Policy-Report-Only"];
  if (defaults.mode === "local") {
    assert.equal(defaultPolicy, csp);
  } else {
    assert.ok(defaultPolicy.includes("'unsafe-eval'"));
    assert.ok(defaultPolicy.includes("connect-src 'self' http://127.0.0.1:3200"));
    assert.ok(defaultPolicy.includes("report-uri http://127.0.0.1:3200/csp-report"));
  }
});
