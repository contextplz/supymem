const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createHmac, randomBytes } = require('node:crypto');
const { tmpdir } = require('node:os');
const { join, resolve } = require('node:path');
const test = require('node:test');
const { pathToFileURL } = require('node:url');
const vm = require('node:vm');

test('health proves your local desktop instance without exposing it in hosted mode', async () => {
  const source = fs.readFileSync(`${__dirname}/route.ts`, 'utf8')
    .replace(/import .*?;\n/g, '').replace(/export /g, '').replace(/: Request/g, '');
  for (const domain of ['procurement.example', 'gardening.example']) {
    for (const mode of ['local', 'hosted']) {
      const context = { process: { env: { SUPYMEM_MODE: mode, SUPYMEM_DESKTOP_INSTANCE_ID: domain } },
        NextResponse: { json: (body, options) => ({ body, options }) } };
      context.proxy = () => ({ body: { ok: true, desktop_instance: domain }, options: { headers: { "Cache-Control": "no-store" } } });
      const result = await vm.runInNewContext(`${source}\nGET()`, context);
      assert.equal(result.body.desktop_instance, mode === 'local' ? domain : undefined);
      assert.equal(result.body.ok, true);
      assert.equal(result.options.headers['Cache-Control'], 'no-store');
    }
  }
});

test('the configured Compose console probe works without your owner session', async () => {
  const compose = fs.readFileSync(resolve(__dirname, '../../../../../../docker-compose.local.yml'), 'utf8');
  const probe = JSON.parse(compose.match(/^\s+test: (\["CMD", "node".*)$/m)[1]).at(-1);
  const owner = await import(pathToFileURL(join(__dirname, '../[...path]/route.ts')));
  const directory = fs.mkdtempSync(join(tmpdir(), 'compose-readiness-'));
  const ownerFile = join(directory, 'owner-session.secret');
  const previous = { SUPYMEM_MODE: process.env.SUPYMEM_MODE, PORT: process.env.PORT,
    CONSOLE_ORIGIN: process.env.CONSOLE_ORIGIN, SUPYMEM_LOCAL_READINESS_KEY: process.env.SUPYMEM_LOCAL_READINESS_KEY,
    SUPYMEM_LOCAL_PROXY_SECRET_FILE: process.env.SUPYMEM_LOCAL_PROXY_SECRET_FILE };
  Object.assign(process.env, { SUPYMEM_MODE: 'local', PORT: '3100', CONSOLE_ORIGIN: 'http://127.0.0.1:3100',
    SUPYMEM_LOCAL_PROXY_SECRET_FILE: ownerFile });
  delete process.env.SUPYMEM_LOCAL_READINESS_KEY;
  try {
    fs.writeFileSync(ownerFile, randomBytes(32).toString('hex'), { mode: 0o600 });
    for (const domain of ['silo-telemetry.test', 'beach-sketchbooks.test']) {
      const result = await new Promise((resolve, reject) => {
        vm.runInNewContext(probe, { process: { exit: resolve }, fetch: async (url, init) => {
          try {
            const address = new URL(url);
            assert.equal(address.origin, 'http://127.0.0.1:3100');
            assert.equal(init?.headers, undefined);
            const handler = address.pathname === '/api/health' ? owner
              : await import(pathToFileURL(join(__dirname, '..', address.pathname.split('/').at(-1), 'route.ts')));
            const response = await handler.GET(new Request(address, { headers: { Host: address.host } }));
            if (response.ok) {
              assert.equal(await response.text(), '');
              assert.equal(response.headers.get('set-cookie'), null);
            }
            return response;
          } catch (error) { reject(error); }
        } });
      });
      assert.equal(result, 0);
      for (const host of ['127.0.0.1:3100', domain]) {
        const request = new Request('http://127.0.0.1:3100/api/local/providers', { headers: { Host: host } });
        assert.equal((await owner.GET(request)).status, 403);
      }
      const readiness = await import(pathToFileURL(join(__dirname, '../local-ready/route.ts')));
      const key = randomBytes(32).toString('hex'), nonce = randomBytes(16).toString('hex');
      process.env.SUPYMEM_LOCAL_READINESS_KEY = key;
      const address = 'http://127.0.0.1:3100/api/local-ready';
      const headers = { Host: '127.0.0.1:3100' };
      assert.equal(readiness.GET(new Request(address, { headers })).status, 403);
      assert.equal(readiness.GET(new Request(address, { headers: { Host: domain, 'X-Local-Ready-Nonce': nonce } })).status, 403);
      const ready = readiness.GET(new Request(address, { headers: { ...headers, 'X-Local-Ready-Nonce': nonce } }));
      assert.equal(ready.status, 204);
      assert.equal(ready.headers.get('X-Local-Ready-Proof') === createHmac('sha256', key).update(nonce).digest('hex'), true);
      delete process.env.SUPYMEM_LOCAL_READINESS_KEY;
    }
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    fs.rmSync(directory, { recursive: true, force: true });
  }
});
