import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { sameGodModeScope } from "./godModeRequestScope.ts";
const source = fs.readFileSync(new URL("../app/v/[ventureId]/site/page.tsx", import.meta.url), "utf8");
const tree = ts.createSourceFile("page.tsx", source, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
function actual(name: string, context: Record<string, unknown>) {
  let fn = "";
  function visit(node: ts.Node) {
    if (ts.isFunctionDeclaration(node) && node.name?.text === name) fn = node.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(fn, `actual production ${name}`);
  return vm.runInNewContext(ts.transpile(`${fn}; ${name}`, { target: ts.ScriptTarget.ES2022 }), context) as (...args: unknown[]) => Promise<unknown>;
}
function initializer(name: string, context: Record<string, unknown>) {
  let text = "";
  function visit(node: ts.Node) {
    if (ts.isVariableDeclaration(node) && node.name.getText(tree) === name && node.initializer) text = node.initializer.getText(tree);
    ts.forEachChild(node, visit);
  }
  visit(tree); assert.ok(text, `actual production ${name}`);
  return vm.runInNewContext(ts.transpile(`(${text})`, {target:ts.ScriptTarget.ES2022}), context);
}
function fixture() {
  const owner = { venture: "ven_a", owner: "account_a", epoch: 1 };
  let live = owner;
  const requests: {path: string; method?: string}[] = [];
  const updates: string[] = [];
  const preview = {appId: "app_a", buildId: "build_a"};
  const revision = {baseline: {deployment_id: "dep_a", source_sha256: "sha_a"}};
  const quote = {quote_id: "quote_a", input_digest: "digest_a", ready: true, ceiling_micro: 100, expires_at: new Date(Date.now() + 60000).toISOString()};
  let release: ((value: unknown) => void) | undefined;
  const paused = new Promise(resolve => {release = resolve;});
  const context: Record<string, unknown> = {
    local: false, ventureId: "ven_a", workspaceId: "wsp_a", acceptedPreview: preview, revision, revisionQuote: quote,
    quoteSequence: {current: 0}, revisionApproved: true, revisionInstruction: "Owned revision", idea: "Owned idea", ventureName: "A", site: {version: 1},
    publishConfirm: true, publishApproval: {current:{owner,preview}}, revisionApproval: {current:{owner,preview,quote}},
    renderOwner: owner, renderedOwner: owner, ownerRef: {current: owner}, loadedScope: {current: owner},
    acceptedCustody: {current: {scope: owner, value: preview}}, revisionCustody: {current: {scope: owner, value: revision}}, quoteCustody: {current: {scope: owner, value: quote, appId: "app_a", buildId: "build_a", requestId: "request_a"}},
    liveScope: () => live, sameGodModeScope, pendingLoad: {current: new AbortController()},
    owned: (value: unknown, receipt: {scope: typeof owner; value: unknown} | null) => Boolean(receipt && receipt.value === value && sameGodModeScope(receipt.scope, live)),
    renderCurrent: () => sameGodModeScope(owner, live),
    api: async (path: string, init: {method?: string} = {}) => {requests.push({path, method: init.method}); return paused;},
    revisionRequestBody: () => ({}), publicationFailureMessage: () => "failed", errorCode: () => "failed",
    revisionSaveFailure: () => "failed", errorMessage: () => "failed", load: async () => undefined,
    estimateRevision: async () => {requests.push({path: "secondary-estimate", method: "POST"});},
  };
  for (const match of source.matchAll(/\b(set[A-Z]\w+)\(/g)) context[match[1]] = () => updates.push(match[1]);
  context.renderCurrent = initializer("renderCurrent", context);
  context.owned = initializer("owned", context);
  context.changePublicationConfirmation = actual("changePublicationConfirmation", context);
  context.changeRevisionApproval = actual("changeRevisionApproval", context);
  return {context,requests,updates,owner,preview,revision,quote,change: (value: typeof owner) => {live = value;},release: (value: unknown) => release!(value)};
}
for (const name of ["publishAcceptedBuild", "saveRevisionRequest", "estimateRevision", "startRevisionBuild"]) {
  test(`actual ${name} refuses an old render before any dispatch`, async () => {
    const f=fixture();const fn=actual(name,f.context);f.change({venture:"ven_b",owner:"account_b",epoch:2});
    // A stale callback must return without reaching the deliberately unresolved API.
    const result=await Promise.race([fn(name === "startRevisionBuild" ? f.quote : "request_a").then(()=>"returned"),new Promise(resolve=>setTimeout(()=>resolve("dispatched"),20))]);
    assert.equal(f.requests.length,0);assert.equal(result,"returned");
  });
}
test("actual saved revision continuation cannot estimate or commit after account retirement",async()=>{
  const f=fixture();const fn=actual("saveRevisionRequest",f.context);const pending=fn();
  await new Promise(resolve=>setImmediate(resolve));assert.equal(f.requests.length,1);f.updates.length=0;
  f.change({venture:"ven_a",owner:"account_b",epoch:2});f.release({ok:true,data:{id:"request_a"}});await pending;
  assert.equal(f.requests.length,1);assert.equal(f.updates.length,0);
});
test("actual build start refuses a reviewed quote replaced in the same live account",async()=>{
  const f=fixture();(f.context.quoteCustody as {current:unknown}).current={scope:f.owner,value:{...f.quote,quote_id:"quote_b"}};
  const result=await Promise.race([actual("startRevisionBuild",f.context)().then(()=>"returned"),new Promise(resolve=>setTimeout(()=>resolve("dispatched"),20))]);
  assert.equal(f.requests.length,0);assert.equal(result,"returned");
});
test("actual reviewed build uses only its still-owned app and quote",async()=>{
  const f=fixture();const run=actual("startRevisionBuild",f.context)();
  f.release({ok:true,data:{build_run_id:"run_a"}});await run;
  assert.deepEqual(f.requests,[{path:"/api/apps/app_a/builds",method:"POST"}]);
  assert.ok(f.updates.includes("setRevisionRun"));
});
test("actual quote receipt must match the reviewed preview app/build",async()=>{
  const f=fixture();(f.context.quoteCustody as {current:Record<string,unknown>}).current.appId="app_b";
  await actual("startRevisionBuild",f.context)();assert.equal(f.requests.length,0);
});
test("actual callback rejects a retired A→B→A render even with matching visible scope",async()=>{
  const f=fixture();(f.context.ownerRef as {current:unknown}).current={...f.owner};
  await actual("startRevisionBuild",f.context)();assert.equal(f.requests.length,0);
});
for(const name of ["acceptedBuildPreview","buildAwaitingReview","auditPublishedWebsite"]) {
  test(`actual ${name} stops secondary requests when its first read retires`,async()=>{
    const f=fixture();f.context.requireWebsiteScope=actual("requireWebsiteScope",f.context);
    f.context.websiteApi=actual("websiteApi",f.context);f.context.websiteFetch=actual("websiteFetch",f.context);
    f.context.isReachableLandingUrl=()=>true;
    const scope={current:f.context.renderCurrent as ()=>boolean,signal:new AbortController().signal};
    const args=name==="auditPublishedWebsite"?[scope,"ven_a","https://owned.invalid"]:["ven_a",scope];
    const run=actual(name,f.context)(...args);
    assert.equal(f.requests.length,1);f.change({venture:"ven_a",owner:"account_b",epoch:2});
    f.release({ok:true,data:{items:[{id:"app_a"}]}});
    await assert.rejects(run,/earlier session/);assert.equal(f.requests.length,1);
  });
}
test("actual preview evidence checks scope after its body read before hashing or URL creation",async()=>{
  const f=fixture();let release:(value:ArrayBuffer)=>void=()=>{};
  const bytes=new Promise<ArrayBuffer>(resolve=>{release=resolve;});
  f.context.requireWebsiteScope=actual("requireWebsiteScope",f.context);
  f.context.authenticatedFetch=async()=>({ok:true,arrayBuffer:()=>bytes});
  f.context.websiteFetch=actual("websiteFetch",f.context);
  let hashes=0;f.context.crypto={subtle:{digest:async()=>{hashes++;return new ArrayBuffer(32);}}};
  const scope={current:f.context.renderCurrent as ()=>boolean};
  const run=actual("evidenceUrl",f.context)(scope,"/owned-preview",{id:"desktop-1.png",media_type:"image/png",sha256:"owned",size_bytes:4});
  await new Promise(resolve=>setImmediate(resolve));f.change({venture:"ven_b",owner:"account_a",epoch:1});release(new ArrayBuffer(4));
  await assert.rejects(run,/earlier session/);assert.equal(hashes,0);
});
for(const name of ["publishAcceptedBuild","estimateRevision","startRevisionBuild"]) {
  test(`actual ${name} stops post-dispatch commits and continuations on retirement`,async()=>{
    const f=fixture();const run=actual(name,f.context)(name === "startRevisionBuild" ? f.quote : "request_a");assert.equal(f.requests.length,1);f.updates.length=0;
    f.change({venture:"ven_a",owner:"account_b",epoch:2});f.release({ok:true,data:{id:"owned_result"}});await run;
    assert.equal(f.requests.length,1);assert.equal(f.updates.length,0);
  });
}
test("actual old quote response cannot clear a newer quote receipt",async()=>{
  const f=fixture();const run=actual("startRevisionBuild",f.context)();assert.equal(f.requests.length,1);f.updates.length=0;
  (f.context.quoteCustody as {current:unknown}).current={scope:f.owner,value:{...f.quote,quote_id:"quote_b"},appId:"app_a",buildId:"build_a",requestId:"request_b"};
  f.release({ok:true,data:{build_run_id:"run_a"}});await run;assert.equal(f.updates.length,0);
});
test("actual estimate continuations cannot replace a newer quote in the same account",async()=>{
  const f=fixture();const releases:((value:unknown)=>void)[]=[];
  f.context.api=async(path:string)=>{f.requests.push({path});return new Promise(resolve=>releases.push(resolve));};
  const fn=actual("estimateRevision",f.context);const old=fn("request_old"),latest=fn("request_latest");
  assert.equal(releases.length,2);
  releases[1]({ok:true,data:{...f.quote,quote_id:"quote_latest"}});await latest;
  releases[0]({ok:true,data:{...f.quote,quote_id:"quote_old"}});await old;
  const receipt=(f.context.quoteCustody as {current:{value:{quote_id:string};requestId:string}}).current;
  assert.equal(receipt.value.quote_id,"quote_latest");assert.equal(receipt.requestId,"request_latest");
});

function actualInputChange(context:Record<string,unknown>,checkedState:string) {
  let callback="";
  function visit(node:ts.Node) {
    if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)) {
      const attrs=node.attributes.properties;
      if(attrs.some(a=>ts.isJsxAttribute(a)&&a.name.getText(tree)==="checked"&&a.initializer?.getText(tree)===`{${checkedState}}`)) {
        const change=attrs.find(a=>ts.isJsxAttribute(a)&&a.name.getText(tree)==="onChange") as ts.JsxAttribute;
        callback=(change.initializer as ts.JsxExpression).expression!.getText(tree);
      }
    }
    ts.forEachChild(node,visit);
  }
  visit(tree);assert.ok(callback);
  return vm.runInNewContext(ts.transpile(`(${callback})`,{target:ts.ScriptTarget.ES2022}),context);
}

test("actual retained build callback refuses after approval checkbox revocation",async()=>{
  const f=fixture();const retained=actual("startRevisionBuild",f.context);
  actualInputChange(f.context,"revisionApproved")({target:{checked:false}});
  const result=await Promise.race([retained().then(()=>"returned"),new Promise(resolve=>setTimeout(()=>resolve("dispatched"),20))]);
  assert.equal(f.requests.length,0);assert.equal(result,"returned");
});
test("actual publication callback refuses without confirmation",async()=>{
  const f=fixture();f.context.publishConfirm=false;
  const result=await Promise.race([actual("publishAcceptedBuild",f.context)().then(()=>"returned"),new Promise(resolve=>setTimeout(()=>resolve("dispatched"),20))]);
  assert.equal(f.requests.length,0);assert.equal(result,"returned");
});

function actualControlClick(context:Record<string,unknown>,control:string) {
  let callback="";
  function visit(node:ts.Node) {
    if(ts.isJsxOpeningElement(node)||ts.isJsxSelfClosingElement(node)) {
      const attrs=node.attributes.properties;
      if(attrs.some(a=>ts.isJsxAttribute(a)&&a.name.getText(tree)==="data-usage-control"&&a.initializer?.getText(tree)===JSON.stringify(control))) {
        const click=attrs.find(a=>ts.isJsxAttribute(a)&&a.name.getText(tree)==="onClick") as ts.JsxAttribute;
        callback=(click.initializer as ts.JsxExpression).expression!.getText(tree);
      }
    }
    ts.forEachChild(node,visit);
  }
  visit(tree);assert.ok(callback);
  return vm.runInNewContext(ts.transpile(`(${callback})`,{target:ts.ScriptTarget.ES2022}),context);
}

test("actual Keep private synchronously revokes retained publication callback",async()=>{
  const f=fixture();const retained=actual("publishAcceptedBuild",f.context);
  actualControlClick(f.context,"website_publish_cancel")();
  await retained();assert.equal(f.requests.length,0);
});
for(const [operation,receipt] of [["publishAcceptedBuild","publishApproval"],["startRevisionBuild","revisionApproval"]]) {
  test(`actual ${operation} consumes exact approval once before double callback`,async()=>{
    const f=fixture();const fn=actual(operation,f.context);
    const first=fn();assert.equal(f.requests.length,1);
    await fn();assert.equal(f.requests.length,1);
    assert.equal((f.context[receipt] as {current:unknown}).current,null);
    f.release({ok:false,data:{}});await first;
  });
}
test("actual checkbox creates only the exact owned quote approval",async()=>{
  const f=fixture();(f.context.revisionApproval as {current:unknown}).current=null;
  actualInputChange(f.context,"revisionApproved")({target:{checked:true}});
  const receipt=(f.context.revisionApproval as {current:{owner:unknown;preview:unknown;quote:unknown}}).current;
  assert.equal(receipt.owner,f.owner);assert.equal(receipt.preview,f.preview);assert.equal(receipt.quote,f.quote);
  const run=actual("startRevisionBuild",f.context)();assert.equal(f.requests.length,1);
  f.release({ok:false,data:{}});await run;
});
test("actual Publish review creates exact owned preview confirmation",async()=>{
  const f=fixture();(f.context.publishApproval as {current:unknown}).current=null;
  actualControlClick(f.context,"website_publish_review")();
  const receipt=(f.context.publishApproval as {current:{owner:unknown;preview:unknown}}).current;
  assert.equal(receipt.owner,f.owner);assert.equal(receipt.preview,f.preview);
  const run=actual("publishAcceptedBuild",f.context)();assert.equal(f.requests.length,1);
  f.release({ok:false,data:{}});await run;
});
test("retired approval event cannot revoke a replacement quote receipt",()=>{
  const f=fixture();const old=actualInputChange(f.context,"revisionApproved");
  const quote={...f.quote,quote_id:"replacement"};
  (f.context.quoteCustody as {current:unknown}).current={scope:f.owner,value:quote,appId:"app_a",buildId:"build_a",requestId:"replacement"};
  const receipt={owner:f.owner,preview:f.preview,quote};
  (f.context.revisionApproval as {current:unknown}).current=receipt;
  old({target:{checked:false}});
  assert.equal((f.context.revisionApproval as {current:unknown}).current,receipt);
});
