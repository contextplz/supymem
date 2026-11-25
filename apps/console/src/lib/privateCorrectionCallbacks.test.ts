import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { correctionAvailabilityMatches } from "./privateCorrectionAvailability.ts";
import { apiErrorCode, apiFailure } from "./apiFailure.ts";
import { formatMicroCreditUsage, USAGE_LIMIT_MESSAGE } from "./credits.ts";
const source=fs.readFileSync(new URL("../components/builds/PrivateCorrection.tsx",import.meta.url),"utf8");
const tree=ts.createSourceFile("PrivateCorrection.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
function actual(name:string,context:Record<string,unknown>){
 let text="";function visit(node:ts.Node){if(ts.isFunctionDeclaration(node)&&node.name?.text===name)text=node.getText(tree);ts.forEachChild(node,visit);}visit(tree);assert.ok(text,name);
 const js=ts.transpileModule(text+`;globalThis.actual=${name};`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const scope=vm.createContext(context);vm.runInContext(js,scope);return scope.actual as(arg?:boolean)=>Promise<void>;
}
function actualCurrent(context:Record<string,unknown>){
 let text="";function visit(node:ts.Node){if(ts.isVariableDeclaration(node)&&node.name.getText(tree)==="current"&&node.initializer)text=node.initializer.getText(tree);ts.forEachChild(node,visit);}visit(tree);assert.ok(text);
 const js=ts.transpileModule(`globalThis.actual=${text};`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 const scope=vm.createContext(context);vm.runInContext(js,scope);return scope.actual;
}
function fixture(){
 const owner={actor:"a",epoch:1,build:"build_a",app:"app_a",artifact:"artifact_a",report:"report_a"};
 const request={id:"request_a",state:"pending",build_id:owner.build,source_artifact_id:owner.artifact,report_digest:owner.report};
 const quote={quote_id:"quote_a",input_digest:"d".repeat(64),ready:true,ceiling_micro:10000,reasons:[] as string[]};
 let release!:(value:unknown)=>void;const paused=new Promise(resolve=>{release=resolve;});
 const calls:{path:string;body:unknown;headers:unknown}[]=[],updates:string[]=[];
 const eligible={build_id:owner.build,available:true,reason:"",source_artifact_id:owner.artifact,report_digest:owner.report};
 const context:Record<string,unknown>={renderedOwner:owner,ownerRef:{current:owner},mounted:{current:true},verifiedSessionSubject:()=>owner.actor,verifiedSessionEpoch:()=>owner.epoch,inFlight:{current:false},eligibilityFlight:{current:null},eligibilityCustody:{current:{owner,value:eligible}},correctionAvailabilityMatches,apiErrorCode,dispatchCustody:{current:null},approved:{current:{owner,quote}},saved:request,quote,requestCustody:{current:{owner,value:request}},quoteCustody:{current:{owner,value:quote}},queued:false,instruction:"Correct the actual agreed customer problem headline.",api:async(path:string,init?:{body:string;headers:unknown})=>{calls.push({path,body:init?.body?JSON.parse(init.body):null,headers:init?.headers});return paused;},knownPreflightRefusal:(value:unknown)=>actual("knownPreflightRefusal",{})(value as boolean),apiFailure:()=>"unconfirmed",onQueued:()=>updates.push("onQueued")};
 for(const match of source.matchAll(/\b(set[A-Z]\w+)\(/g))context[match[1]]=()=>updates.push(match[1]);
 context.current=actualCurrent(context);
 return{context,calls,updates,owner,request,quote,retire:()=>{(context.ownerRef as {current:unknown}).current=null;},release:(value:unknown)=>release(value)};
}
test("your requested correction saves, quotes and starts without another confirmation across domains", async () => {
 for (const instruction of ["Fix purchase order reconciliation for warehouses.", "Fix family holiday booking dates."]) for (const ready of [true, false]) {
  const setup=fixture();setup.context.instruction=instruction;
  setup.quote.ready=ready;setup.quote.reasons=ready?[]:["insufficient_credits"];
  let error="";setup.context.setError=(value:string)=>{error=value;};setup.context.apiFailure=apiFailure;
  setup.context.api=async(path:string,init?:{body:string;headers:unknown})=>{
   setup.calls.push({path,body:init?.body?JSON.parse(init.body):null,headers:init?.headers});
   return {ok:true,data:path.endsWith("/estimate")?setup.quote:path.endsWith("/builds")?{build_run_id:"run_requested"}:setup.request};
  };
  setup.context.startCorrection=actual("startCorrection",setup.context);
  setup.context.estimateCorrection=actual("estimateCorrection",setup.context);
  await actual("saveCorrection",setup.context)();
  assert.equal(setup.calls.length,ready?3:2);
  if(ready){assert.equal(setup.calls[2].path,"/api/apps/app_a/builds");assert.equal((setup.calls[2].body as {approve_budget_micro:number}).approve_budget_micro,setup.quote.ceiling_micro);}
  else assert.equal(error,USAGE_LIMIT_MESSAGE);
 }
});
for(const name of ["saveCorrection","estimateCorrection","startCorrection"]){
 test(`actual ${name} refuses retired account/build before dispatch`,async()=>{const f=fixture();f.retire();await actual(name,f.context)();assert.equal(f.calls.length,0);});
 test(`actual ${name} cannot commit after in-flight retirement`,async()=>{const f=fixture();const pending=actual(name,f.context)();await new Promise(resolve=>setImmediate(resolve));assert.equal(f.calls.length,1);f.updates.length=0;f.retire();f.release({ok:true,data:{...f.request,...f.quote}});await pending;assert.equal(f.calls.length,1);assert.equal(f.updates.length,0);});
}
test("actual estimate refuses a request from another render",async()=>{const f=fixture();(f.context.requestCustody as {current:unknown}).current={owner:{...f.owner},value:f.request};await actual("estimateCorrection",f.context)();assert.equal(f.calls.length,0);});
test("actual approval revocation prevents retained start callback",async()=>{const f=fixture();await actual("changeApproval",f.context)(false);await actual("startCorrection",f.context)();assert.equal(f.calls.length,0);});
test("actual dispatch consumes approval once and uses reviewed app quote",async()=>{const f=fixture();const start=actual("startCorrection",f.context);const pending=start();assert.equal((f.context.approved as {current:unknown}).current,null);await start();assert.equal(f.calls.length,1);assert.equal(f.calls[0].path,"/api/apps/app_a/builds");assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].headers)),{"Idempotency-Key":"private-correction:quote_a"});assert.deepEqual(JSON.parse(JSON.stringify(f.calls[0].body)),{quote_id:f.quote.quote_id,input_digest:f.quote.input_digest,approve_budget_micro:10000});f.release({ok:true,data:{build_run_id:"new"}});await pending;assert.ok(f.updates.includes("onQueued"));});
test("actual foreign correction response cannot become saved feedback",async()=>{const f=fixture();const pending=actual("saveCorrection",f.context)();f.release({ok:true,data:{...f.request,build_id:"other"}});await pending;assert.ok(!f.updates.includes("setSaved"));assert.ok(f.updates.includes("setError"));});

test("actual correction save requires exact fresh eligible baseline before mutation",async()=>{
 for(const value of [null,{owner:{},value:{}},{owner:null,value:{available:false}}]){
  const f=fixture();(f.context.eligibilityCustody as {current:unknown}).current=value;
  await actual("saveCorrection",f.context)();assert.equal(f.calls.length,0);
 }
});
test("actual stale save response retires baseline eligibility and cannot save again",async()=>{
 const f=fixture();const save=actual("saveCorrection",f.context);const pending=save();
 f.release({ok:false,status:409,data:{detail:{code:"private_correction_plan_changed"}}});await pending;
 assert.equal((f.context.eligibilityCustody as {current:unknown}).current,null);
 assert.ok(!f.updates.includes("setSaved"));await save();assert.equal(f.calls.length,1);
});
test("actual eligibility check cannot admit foreign receipt or apply after retirement",async()=>{
 for(const retire of [false,true]){
  const f=fixture();const pending=actual("inspectEligibility",f.context)();
  f.updates.length=0;if(retire)f.retire();
  f.release({ok:true,data:{build_id:retire?f.owner.build:"foreign",available:true,reason:"",source_artifact_id:f.owner.artifact,report_digest:f.owner.report}});
  await pending;assert.equal((f.context.eligibilityCustody as {current:unknown}).current,null);
  assert.equal(f.updates.includes("setError"),!retire);
 }
});

test("fresh session on the same build checks eligibility while a retired read is pending",async()=>{
 const old=fixture();const oldRead=actual("inspectEligibility",old.context)();
 const fresh=fixture();fresh.owner.epoch=old.owner.epoch+1;
 fresh.context.ownerRef=old.context.ownerRef;(fresh.context.ownerRef as {current:unknown}).current=fresh.owner;
 fresh.context.eligibilityFlight=old.context.eligibilityFlight;
 const freshRead=actual("inspectEligibility",fresh.context)();
 assert.equal(fresh.calls.length,1);old.updates.length=0;fresh.updates.length=0;
 old.release({ok:true,data:{build_id:old.owner.build,available:true,reason:"",source_artifact_id:old.owner.artifact,report_digest:old.owner.report}});
 await oldRead;assert.equal(old.updates.length,0);
 assert.equal((fresh.context.eligibilityFlight as {current:unknown}).current,fresh.owner);
 fresh.release({ok:true,data:{build_id:fresh.owner.build,available:true,reason:"",source_artifact_id:fresh.owner.artifact,report_digest:fresh.owner.report}});
 await freshRead;
 assert.equal((fresh.context.eligibilityCustody as {current:{owner:unknown}}).current.owner,fresh.owner);
 assert.ok(fresh.updates.includes("setChecking"));
 assert.match(source,/\[build\.id, build\.artifact_id, build\.review\?\.report_digest, owner\.actor, owner\.epoch\]/);
});

test("actual owner predicate refuses retired A→B→A identity despite identical fields",async()=>{
 const f=fixture();(f.context.ownerRef as {current:unknown}).current={...f.owner};
 await actual("startCorrection",f.context)();assert.equal(f.calls.length,0);
});

test("actual network-uncertain dispatch cannot be reapproved before saved-status review",async()=>{
 const f=fixture();f.context.api=async(path:string,init:{body:string;headers:unknown})=>{f.calls.push({path,body:JSON.parse(init.body),headers:init.headers});throw new Error('network_unconfirmed');};
 const start=actual('startCorrection',f.context);await start();assert.equal(f.calls.length,1);
 await actual('changeApproval',f.context)(true);await start();assert.equal(f.calls.length,1);
});

test("actual source-owned preflight refusal retires quote and requires fresh explicit estimate",async()=>{
 const f=fixture();f.context.api=async()=>({ok:false,status:409,data:{detail:{code:"build_quote_expired"}}});
 await actual("startCorrection",f.context)();assert.equal((f.context.dispatchCustody as {current:unknown}).current,null);
 assert.equal((f.context.quoteCustody as {current:unknown}).current,null);
 await actual("changeApproval",f.context)(true);assert.equal((f.context.approved as {current:unknown}).current,null);
});
for(const found of [false,true])test(`actual reconciliation ${found?"finds":"does not invent"} exact saved dispatch`,async()=>{
 const f=fixture();(f.context.dispatchCustody as {current:unknown}).current={owner:f.owner,quoteId:f.quote.quote_id};
 f.context.api=async()=>({ok:true,data:{items:[{id:"correction_run",app_id:f.owner.app,quote_id:f.quote.quote_id,correction_request_id:found?f.request.id:"foreign",state:"queued"}]}});
 await actual("reviewSavedDispatch",f.context)();assert.equal(f.updates.includes("onQueued"),found);
 assert.notEqual((f.context.dispatchCustody as {current:unknown}).current,null);
});
test("actual reconciliation cannot apply after an account/epoch retirement",async()=>{
 const f=fixture();(f.context.dispatchCustody as {current:unknown}).current={owner:f.owner,quoteId:f.quote.quote_id};
 let release!:(value:unknown)=>void;f.context.api=async()=>new Promise(resolve=>{release=resolve;});
 const pending=actual("reviewSavedDispatch",f.context)();f.retire();f.updates.length=0;
 release({ok:true,data:{items:[{id:"run",app_id:f.owner.app,quote_id:f.quote.quote_id,correction_request_id:f.request.id}]}});await pending;
 assert.equal(f.updates.length,0);
});


test("actual correction approval dispatches the reviewed ceiling",async()=>{
 const f=fixture();f.quote.ceiling_micro=1_000_000;
 const pending=actual("startCorrection",f.context)();
 assert.equal((f.calls[0].body as {approve_budget_micro:number}).approve_budget_micro,1_000_000);
 f.release({ok:true,data:{build_run_id:"new"}});await pending;
});
