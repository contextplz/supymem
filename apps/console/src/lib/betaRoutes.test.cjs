const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const test = require('node:test');
const ts = require('typescript');
const vm = require('node:vm');
const routes = require('./betaRoutes.ts');

test('your local Content Studio is reachable from navigation and middleware across unrelated domains', async () => {
  const exports = {};
  vm.runInNewContext(ts.transpileModule(readFileSync(__dirname + '/founderRoutes.ts', 'utf8'),
    {compilerOptions: {module: ts.ModuleKind.CommonJS}}).outputText,
    {exports, require: id => require(id.includes('betaRoutes') ? './betaRoutes.ts' : './localMode.ts')});
  const source = readFileSync(__dirname + '/../middleware.ts', 'utf8')
    .replace(/^import .*;\n/gm, '').replace(/export /g, '').replace(': NextRequest', '');
  const context = {process: {env: {SUPYMEM_MODE: 'local'}}, URL, Headers, ...routes, ...require('./localMode.ts'),
    NextResponse: class {
      constructor(body, options) { this.status = options.status; }
      static next() { return {status: 200}; }
      static redirect() { return {status: 307}; }
    }};
  vm.runInNewContext(source, context);
  for (const domain of ['procurement.example', 'gardening.example']) {
    assert.equal(routes.isBetaHiddenPath('/content?venture=' + domain, true), false);
    assert.equal(routes.isBetaHiddenPath('/content/drafts', true), false);
    assert.equal(routes.isBetaHiddenPath('/content', false), true);
    assert.ok(exports.founderRouteLinks(domain, true).some(link => link.href === '/content'));
    assert.equal((await context.middleware({headers: new Headers({host: 'localhost:3100'}),
      nextUrl: {pathname: '/content'}})).status, 200);
  }
});
