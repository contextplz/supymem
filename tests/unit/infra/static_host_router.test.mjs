import assert from 'node:assert/strict';
import fs from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const path = new URL('../../../infra/cloudfront/static_host_router.js', import.meta.url);
const source = fs.readFileSync(path, 'utf8');
const mapping = (app = 'app_12345678', dep = 'dep_12345678') => ({
    v: 1, app, deployment: dep, generation: 1,
    prefix: `published/${app}/${dep}`, enabled: true,
});
function runtime(entries, domain = 'supymem.com') {
    const calls = [];
    const cf = { kvs: () => ({
        exists: async host => { calls.push(host); return Object.hasOwn(entries, host); },
        get: async host => {
            const value = entries[host];
            if (value instanceof Error) throw value;
            return typeof value === 'string' ? value : JSON.stringify(value);
        },
    }) };
    const context = vm.createContext({ cf });
    vm.runInContext(source.replace("import cf from 'cloudfront';", '')
        .replace('__PUBLIC_DOMAIN__', domain).replace('__KVS_ID__', 'test-store'), context);
    return { calls, run: request => context.handler({ request }) };
}
const request = (host = 'studio.supymem.com', uri = '/', method = 'GET') => ({
    method, uri, headers: { host: { value: host }, authorization: { value: 'private' },
        cookie: { value: 'private' }, origin: { value: 'https://app.supymem.com' },
        'x-tenant-id': { value: 'app_foreign' } },
    cookies: { session: { value: 'private' } }, querystring: { tenant: { value: 'app_foreign' } },
});

test('actual edge handler scopes cache URI to tenant and immutable deployment', async () => {
    const rt = runtime({ 'studio.supymem.com': mapping(),
        'other.supymem.com': mapping('app_87654321', 'dep_87654321') });
    const a = await rt.run(request());
    const b = await rt.run(request('other.supymem.com'));
    assert.equal(a.uri, '/published/app_12345678/dep_12345678/index.html');
    assert.equal(b.uri, '/published/app_87654321/dep_87654321/index.html');
    assert.notEqual(a.uri, b.uri);
    assert.equal(Object.keys(a.querystring).length, 0);
    assert.equal(Object.keys(a.cookies).length, 0);
    assert.deepEqual(Object.keys(a.headers), ['host']);
    assert.equal((await rt.run(request('STUDIO.SUPYMEM.COM', '/assets/site.css', 'HEAD'))).uri,
        '/published/app_12345678/dep_12345678/assets/site.css');
});

test('reserved, noncanonical and external hosts never query routing state', async () => {
    for (const host of ['app.supymem.com', 'gateway.supymem.com', 'staging.supymem.com',
        'studio.supymem.com.', 'studio.supymem.com:443', 'studio.supymem.com.evil.example',
        'em2631.supymem.com', 'xn--studio.supymem.com', 'x.studio.supymem.com']) {
        const rt = runtime({ [host]: mapping() });
        assert.equal((await rt.run(request(host))).statusCode, 404, host);
        assert.equal(rt.calls.length, 0);
    }
});

test('CloudFront read-only viewer headers are preserved exactly', async () => {
    const rt = runtime({ 'studio.supymem.com': mapping() });
    const input = request('STUDIO.SUPYMEM.COM');
    const fixed = { host: { value: 'STUDIO.SUPYMEM.COM' }, 'content-length': { value: '0' },
        'transfer-encoding': { value: 'identity' }, via: { value: 'edge' }, 'cdn-loop': { value: 'cloudfront' } };
    Object.assign(input.headers, fixed);
    const result = await rt.run(input);
    for (const [key, value] of Object.entries(fixed)) assert.deepEqual(result.headers[key], value);
});

test('path tricks and unsupported methods cannot bypass a tenant prefix', async () => {
    const rt = runtime({ 'studio.supymem.com': mapping() });
    for (const path of ['//other/index.html', '/../other/index.html', '/%2e%2e/a', '/a/%252e%252e/b',
        '/a\\b', '/a/./b', '/.env', '/a?tenant=other', '/a#fragment', '/a//b']) {
        assert.equal((await rt.run(request('studio.supymem.com', path))).statusCode, 400, path);
    }
    assert.equal((await rt.run(request('studio.supymem.com', '/', 'PUT'))).statusCode, 405);
    assert.equal(rt.calls.length, 0);
});

test('missing, disabled, malformed and failed KVS results never route a default app', async () => {
    for (const value of [{ ...mapping(), prefix: 'published/app_foreign/dep_12345678' },
        { ...mapping(), enabled: false }, { ...mapping(), generation: true },
        { ...mapping(), app: ['app_12345678'] },
        { ...mapping(), extra: 'ignored?' }, '{}', 'not-json']) {
        const rt = runtime({ 'studio.supymem.com': value });
        const result = await rt.run(request());
        assert.equal(result.statusCode, 404);
        assert.equal(result.headers['cache-control'].value, 'no-store');
    }
    assert.equal((await runtime({}).run(request())).statusCode, 404);
    assert.equal((await runtime({ 'studio.supymem.com': new Error('private provider detail') }).run(request())).statusCode, 503);
});

test('staging domain cannot route a production mapping or vice versa', async () => {
    const entries = { 'studio.staging.supymem.com': mapping(), 'studio.supymem.com': mapping() };
    const rt = runtime(entries, 'staging.supymem.com');
    assert.equal((await rt.run(request())).statusCode, 404);
    assert.equal((await rt.run(request('studio.staging.supymem.com'))).uri,
        '/published/app_12345678/dep_12345678/index.html');
});
