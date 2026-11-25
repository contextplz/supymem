import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
import { operationsHref } from "./operationsScope.ts";

// Compile shipped callbacks and renderer; every API/native/browser reply is a local fixture.
const flush = () => new Promise<void>(done => setImmediate(done));
function deferred<T>() { let resolve!: (v: T) => void; const promise = new Promise<T>(done => { resolve = done; }); return {promise,resolve}; }
function fixture(kind: "hub" | "gate" = "hub", hideVentureControls = false) {
  const file=kind==="hub"?"../components/connections/ConnectionsHub.tsx":"../components/ops/OpsWorkspaceGate.tsx";
  const source=fs.readFileSync(new URL(file,import.meta.url),"utf8"),tree=ts.createSourceFile(file,source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
  let body=tree.statements.filter(n=>!ts.isImportDeclaration(n)).map(n=>n.getText(tree).replace(/^export /,"")).join("\n");
  body=kind==="hub"?body.replace('  if (!ready) return <','  globalThis.view={refresh,onAction,onVerifyGmail,openPendingAuthorization,copyPendingAuthorization,ready,status,cards,gmailReceipt,pendingAuth,busy,banner,error};\n  if (!ready) return <')
    :body.replace('  if (loading) return <','  globalThis.view={changeWorkspace,retry,ready,scope,loadedOwner,loading,error};\n  if (loading) return <');
  assert.ok(body.includes("globalThis.view="));
  const code=ts.transpileModule(body+`\nglobalThis.component=${kind==="hub"?"ConnectionsHub":"OpsWorkspaceGate"};`,{compilerOptions:{target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.React}}).outputText;
  let subject:string|null="usr_a",epoch=1,wid="wsp_a",scope:any={workspaceId:"wsp_a",ventureId:"ven_a",actorId:"usr_a"},cursor=0,search="workspace=wsp_a&venture=ven_a";
  const slots:any[]=[],effects:(()=>void)[]=[],elements:any[]=[],listeners=new Map<string,Set<()=>void>>();
  const calls:any[]=[],writes:any[]=[],external:any[]=[];
  const card=(id="gmail",action="connect")=>({id,name:id,action,connected:true,account_email:`private_${wid}`});
  const status=()=>({workspace_id:wid,ready_count:1,total_count:1,connections:{gmail:card()},order:["gmail"],blockers:[]});
  const response=async(name:string,arg?:any)=>{
    if(name==="status")return status(); if(name==="receipt")return{receipt:{status:"verified",log_id:`private_receipt_${wid}`}};
    if(name==="connect")return{authorize_url:"https://connect.composio.dev/link/owned-fixture"}; if(name==="sync")return{ok:true};
    if(name==="verify")return{ok:true,status:"verified",receipt_saved:true,email:`private_${wid}`};
    if(name==="workspaces")return{ok:true,data:{items:[{id:"wsp_a"},{id:"wsp_b"}]}}; if(name==="venture")return{ok:true,data:{workspace_id:String(arg).endsWith("ven_b")?"wsp_b":"wsp_a"}};
    throw new Error(`Unknown fixture ${name}: ${String(arg)}`);
  };
  let handler=response;
  const call=async(name:string,arg?:any)=>{calls.push({name,arg,target:wid});return handler(name,arg);};
  const context=vm.createContext({String,JSON,Promise,Boolean,Array,Object,URL,URLSearchParams,encodeURIComponent,Date,Error,operationsHref,isLocalConsole:()=>false,
    useFeatureVisibility:()=>()=>true,
    React:{createElement:(type:any,props:any,...children:any[])=>{const el={type,props:props||{},children};elements.push(el);return el;}},
    createContext:()=>({Provider:"scope-provider"}),useContext:()=>scope,useOperationsScope:()=>scope,useIsOperator:()=>true,useSearchParams:()=>new URLSearchParams(search),
    useState:(initial:any)=>{const i=cursor++;slots[i]??={value:initial};return[slots[i].value,(v:any)=>{slots[i].value=typeof v==="function"?v(slots[i].value):v;}];},
    useRef:(initial:any)=>{const i=cursor++;return slots[i]??={current:initial};},useMemo:(fn:()=>any)=>{cursor++;return fn();},
    useCallback:(fn:any,deps:any[])=>{const i=cursor++,p=slots[i];if(!p||deps.some((v,j)=>v!==p.deps[j]))slots[i]={fn,deps};return slots[i].fn;},
    useEffect:(fn:()=>any,deps:any[])=>{const i=cursor++,p=slots[i];if(!p||deps.some((v,j)=>v!==p.deps[j])){slots[i]={deps,cleanup:p?.cleanup,setup:fn};effects.push(()=>{slots[i].cleanup?.();slots[i].cleanup=fn();});}},
    verifiedSessionSubject:()=>subject,verifiedSessionEpoch:()=>epoch,ensureSession:async()=>({ok:Boolean(subject),data:{authenticated:Boolean(subject),subject}}),
    api:async()=>({ok:true,data:{founder_ui:"chat"}}),apiSwr:(path:string)=>call(path==="/api/workspaces"?"workspaces":"venture",path),getOpsWorkspace:()=>wid,setOpsWorkspace:(value:string,ventureId?:string)=>{wid=value;writes.push({value,ventureId});},
    getConnectionsStatus:()=>call("status"),getGmailMailboxes:async()=>null,getHubGmailVerification:(vid?:string)=>call("receipt",vid),verifyHubGmail:(vid?:string)=>call("verify",vid),connectHubProvider:(id:string,payload?:any)=>call("connect",{id,payload}),syncHubProvider:(id:string)=>call("sync",id),
    connectedAccountChannels:(cards:any[])=>({connected:cards.length,total:cards.length}),connectionActionLabel:()=>"Connect",connectionState:()=>"Connected",connectionCopy:()=>({purpose:"Fixture"}),connectionSetupMessage:()=>"Fixture",founderConnectionMessage:()=>null,founderSafeConnectionText:()=>null,opsErrorMessage:(_e:any,f:string)=>f,
    Link:"link",ConnectTools:"tools",InboxBriefingConsent:()=>null,CodingAgentTiles:()=>null,ErrorNotice:"error",styles:{},desktopInvokeOrThrow:async(...args:any[])=>{external.push(args);},isDesktopShell:()=>false,navigator:{clipboard:{writeText:async(value:string)=>{external.push(["clipboard",value]);}}},
    window:{location:{href:"https://app.supymem.com/connections?workspace=wsp_a&venture=ven_a"},history:{replaceState:(...args:any[])=>external.push(["history",...args])},open:(...args:any[])=>{external.push(["open",...args]);return{};},addEventListener:(name:string,fn:()=>void)=>{if(!listeners.has(name))listeners.set(name,new Set());listeners.get(name)!.add(fn);},removeEventListener:(name:string,fn:()=>void)=>listeners.get(name)?.delete(fn)},
  });
  vm.runInContext(code,context);
  const render=()=>{cursor=0;elements.length=0;context.component(kind==="gate"?{children:"owned-child",hideVentureControls}:undefined);return{...context.view,elements:[...elements]};};
  const commit=()=>{while(effects.length)effects.shift()!();};
  const unmount=()=>slots.filter(slot=>slot?.setup).forEach(slot=>slot.cleanup?.());
  const replay=()=>{unmount();slots.filter(slot=>slot?.setup).forEach(slot=>{slot.cleanup=slot.setup();});};
  return{render,commit,unmount,replay,calls,writes,external,card,status,response,setHandler:(next:typeof handler)=>handler=next,
    query:(value:string)=>{search=value;context.window.location.href="https://app.supymem.com/connections?"+value;},
    liveQuery:(value:string)=>{context.window.location.href="https://app.supymem.com/connections?"+value;},
    selection:(nextWid:string,nextVenture="ven_b")=>{wid=nextWid;scope={workspaceId:nextWid,ventureId:nextVenture,actorId:subject};search=`workspace=${nextWid}&venture=${nextVenture}`;context.window.location.href="https://app.supymem.com/connections?"+search;},globalOnly:(next:string)=>{wid=next;},
    retire:(next:string|null,notify=true)=>{subject=next;++epoch;scope={...scope,actorId:next};if(notify)listeners.get("supymem-session-changed")?.forEach(fn=>fn());},
    mount:async()=>{render();commit();await flush();return render();},text:()=>render().elements.flatMap((e:any)=>e.children.flat(Infinity).filter((v:any)=>typeof v==="string")).join(" "),
  };
}

test("actual hub recovers mount effect replay and reloads with the renewed owner",async()=>{
  const f=fixture();f.render();f.commit();const old=f.render();
  f.replay();await flush();f.render();f.commit();await flush();
  const ready=f.render();assert.equal(ready.ready,true);
  assert.ok(f.calls.some(call=>call.name==="receipt"));
  const before=f.calls.length;assert.equal(await old.refresh(),false);assert.equal(f.calls.length,before);
  assert.equal(await ready.refresh(),true);assert.equal(f.render().ready,true);
  const settled=f.calls.length;
  for(let i=0;i<3;i++){f.render();f.commit();await flush();assert.equal(f.render().ready,true);}
  assert.equal(f.calls.length,settled,"settled renders must not start another load");
  assert.equal(f.external.length,0);
});

test("actual hub unmount retires old actions while a fresh mount and reload remain usable",async()=>{
  const f=fixture(),old=await f.mount();f.unmount();const before=f.calls.length;
  assert.equal(await old.refresh(),false);await old.onAction(old.cards[0]);await old.onVerifyGmail();
  assert.equal(f.calls.length,before);assert.equal(f.external.length,0);
  const fresh=fixture(),ready=await fresh.mount();assert.equal(ready.ready,true);
  assert.equal(await ready.refresh(),true);assert.equal(fresh.render().ready,true);
});

test("venture GTM hides workspace controls only after owned scope is verified",async()=>{
  const f=fixture("gate",true),ready=await f.mount();
  assert.equal(ready.ready,true);assert.equal(ready.scope.ventureId,"ven_a");
  assert.equal(ready.elements.some((e:any)=>e.type==="select"),false);
  assert.doesNotMatch(f.text(),/Company workspace|Refresh/);assert.match(f.text(),/owned-child/);
  f.query("workspace=wsp_a");f.render();f.commit();await flush();
  assert.equal(f.render().elements.some((e:any)=>e.type==="select"),true);
  assert.match(f.text(),/Company workspace|Refresh/);
});

test("actual old workspace picker refuses retirement and same-subject ABA",async()=>{
  for(const next of ["usr_b",null,"usr_a"]){const f=fixture("gate"),old=await f.mount();assert.equal(old.ready,true);f.retire("usr_other",false);f.retire(next,false);const n=f.writes.length;old.changeWorkspace("wsp_b");assert.equal(f.writes.length,n);assert.equal(f.render().ready,false);assert.doesNotMatch(f.text(),/owned-child/);}
});
test("current picker changes listed workspace and immediately retires its previous callback",async()=>{
  const f=fixture("gate"),old=await f.mount();old.changeWorkspace("wsp_b");assert.equal(f.writes.at(-1).value,"wsp_b");const n=f.writes.length;old.changeWorkspace("wsp_a");old.retry();assert.equal(f.writes.length,n);const now=f.render();assert.equal(now.ready,true);now.changeWorkspace("wsp_a");assert.equal(f.writes.at(-1).value,"wsp_a");assert.equal(f.writes.at(-1).ventureId,undefined);
});
test("retired workspace membership cannot install old account's selection",async()=>{
  const f=fixture("gate"),p=deferred<any>();f.setHandler(async(name,arg)=>name==="workspaces"?p.promise:f.response(name,arg));f.render();f.commit();await flush();f.retire("usr_b",false);const n=f.writes.length;p.resolve({ok:true,data:{items:[{id:"wsp_a"}]}});await flush();assert.equal(f.writes.length,n);assert.equal(f.render().ready,false);
});
test("retained hub callbacks refuse workspace/global target changes and account ABA before preparation",async()=>{
  for(const change of [(f:any)=>f.selection("wsp_b"),(f:any)=>f.globalOnly("wsp_b"),(f:any)=>{f.retire("usr_other",false);f.retire("usr_a",false);}]){const f=fixture(),old=await f.mount();change(f);const n=f.calls.length;await old.onAction(old.cards[0]);await old.onVerifyGmail();assert.equal(f.calls.length,n);assert.equal(f.render().ready,false);assert.doesNotMatch(f.text(),/private_wsp_a/);}
});
test("listed current card prepares once with exact same dispatch and return scope",async()=>{
  const f=fixture(),old=await f.mount(),p=deferred<any>();f.setHandler(async(name,arg)=>name==="connect"?p.promise:f.response(name,arg));const a=old.onAction(old.cards[0]),b=old.onAction(old.cards[0]),posts=f.calls.filter(c=>c.name==="connect");assert.equal(posts.length,1);assert.equal(posts[0].target,"wsp_a");assert.equal(posts[0].arg.payload.return_to,"/connections?workspace=wsp_a&venture=ven_a");p.resolve({authorize_url:"https://connect.composio.dev/link/owned"});await Promise.all([a,b]);assert.ok(f.render().pendingAuth);
});
test("foreign/replaced card identity and mismatched status never admit provider preparation",async()=>{
  const f=fixture(),old=await f.mount(),n=f.calls.length;await old.onAction({...old.cards[0]});await old.onAction(f.card("foreign"));assert.equal(f.calls.length,n);await old.refresh();await old.onAction(old.cards[0]);assert.equal(f.calls.filter(c=>c.name==="connect").length,0);f.setHandler(async(name,arg)=>name==="status"?{...f.status(),workspace_id:"wsp_foreign"}:f.response(name,arg));await f.render().refresh();assert.equal(f.render().ready,false);await f.render().onVerifyGmail();assert.equal(f.calls.filter(c=>c.name==="verify").length,0);
});
test("late status/receipt reads cannot populate new scope or launch old next-stage read",async()=>{
  for(const stage of ["status","receipt"]){const f=fixture(),old=await f.mount(),p=deferred<any>();f.setHandler(async(name,arg)=>name===stage?p.promise:f.response(name,arg));const a=old.refresh();await flush();f.selection("wsp_b");f.render();f.commit();f.setHandler(f.response);await f.render().refresh();const n=f.calls.length;p.resolve(stage==="status"?{...f.status(),workspace_id:"wsp_a"}:{receipt:{status:"verified",log_id:"obsolete_receipt"}});await a;assert.equal(f.calls.length,n);assert.equal(f.render().status.workspace_id,"wsp_b");assert.doesNotMatch(f.text(),/obsolete_receipt|private_wsp_a/);}
});
test("retained authorization open/copy callbacks refuse after retirement",async()=>{
  const f=fixture(),old=await f.mount();await old.onAction(old.cards[0]);const auth=f.render();assert.ok(auth.pendingAuth);assert.deepEqual(f.external.map(x=>x[0]),["open"]);f.retire("usr_b");await auth.openPendingAuthorization();await auth.copyPendingAuthorization();assert.equal(f.external.length,1);assert.equal(f.render().pendingAuth,null);
});
test("late preparation cannot restore link or clear next scope's busy action",async()=>{
  const f=fixture(),old=await f.mount(),a=deferred<any>(),b=deferred<any>();f.setHandler(async(name,arg)=>name==="connect"?a.promise:f.response(name,arg));const first=old.onAction(old.cards[0]);f.selection("wsp_b");f.render();f.commit();f.setHandler(f.response);await f.render().refresh();const now=f.render();f.setHandler(async(name,arg)=>name==="connect"?b.promise:f.response(name,arg));const second=now.onAction(now.cards[0]);a.resolve({authorize_url:"https://connect.composio.dev/link/old"});await first;assert.equal(f.render().busy,"gmail");assert.equal(f.render().pendingAuth,null);b.resolve({authorize_url:"https://connect.composio.dev/link/current"});await second;assert.match(f.render().pendingAuth.url,/current$/);
});
test("failed status removes prior action authority and recovers by read",async()=>{
  const f=fixture(),old=await f.mount();f.setHandler(async()=>{throw new Error("owned failure");});await old.refresh();assert.equal(f.render().ready,false);const n=f.calls.length;await old.onAction(old.cards[0]);assert.equal(f.calls.length,n);f.setHandler(f.response);await f.render().refresh();assert.equal(f.render().ready,true);
});
test("Gmail proof uses admitted venture and retired result launches no receipt read",async()=>{
  const f=fixture(),old=await f.mount(),p=deferred<any>();f.setHandler(async(name,arg)=>name==="verify"?p.promise:f.response(name,arg));const pending=old.onVerifyGmail();assert.equal(f.calls.at(-1).arg,"ven_a");const n=f.calls.length;f.retire("usr_b",false);p.resolve({ok:true,status:"verified",receipt_saved:true,email:"obsolete_email"});await pending;assert.equal(f.calls.length,n);assert.equal(f.render().ready,false);assert.doesNotMatch(f.text(),/obsolete_email/);
});


test("old picker also refuses an independently replaced global workspace",async()=>{
  const f=fixture("gate"),old=await f.mount();f.globalOnly("wsp_b");const n=f.writes.length;const picker=old.elements.find((e:any)=>e.type==="select");picker.props.onChange({target:{value:"wsp_a"}});assert.equal(f.writes.length,n);assert.equal(f.render().ready,false);
});
test("failed membership read hides previous private workspaces and leaves no selection authority",async()=>{
  const f=fixture("gate"),old=await f.mount();f.setHandler(async()=>{throw new Error("owned read failure");});old.retry();f.render();f.commit();await flush();const failed=f.render();assert.equal(failed.ready,false);assert.doesNotMatch(f.text(),/owned-child/);assert.equal(failed.elements.some((e:any)=>e.type==="select"),false);const n=f.writes.length;old.changeWorkspace("wsp_b");assert.equal(f.writes.length,n);
});
test("OAuth URL markers cannot dispatch unlisted query provider mutations",async()=>{
  const f=fixture();f.query("connected=foreign&oauth=connected&provider=foreign");await f.mount();assert.equal(f.calls.some(c=>c.name==="sync"||c.name==="connect"),false);assert.doesNotMatch(f.text(),/foreign authorization completed/);
});
test("current authorization controls work once admitted and refresh retires their source-list custody",async()=>{
  const f=fixture(),old=await f.mount();await old.onAction(old.cards[0]);const auth=f.render();await auth.openPendingAuthorization();await auth.copyPendingAuthorization();assert.deepEqual(f.external.map(x=>x[0]),["open","open","clipboard"]);await auth.refresh();await auth.openPendingAuthorization();await auth.copyPendingAuthorization();assert.equal(f.external.length,3);assert.equal(f.render().pendingAuth,null);
});


test("same mounted hub query navigation immediately hides A and refuses old verification/preparation",async()=>{
  const f=fixture(),old=await f.mount();f.query("workspace=wsp_b&venture=ven_b");const hidden=f.render();assert.equal(hidden.ready,false);const n=f.calls.length;await old.onAction(old.cards[0]);await old.onVerifyGmail();await hidden.refresh();assert.equal(f.calls.length,n);assert.doesNotMatch(f.text(),/private_wsp_a/);
});
test("same mounted gate query navigation retires old picker and revalidates B membership",async()=>{
  const f=fixture("gate"),old=await f.mount();f.query("workspace=wsp_b&venture=ven_b");const hidden=f.render();assert.equal(hidden.ready,false);const n=f.writes.length;old.changeWorkspace("wsp_a");old.retry();assert.equal(f.writes.length,n);f.commit();await flush();const next=f.render();assert.equal(next.ready,true);assert.equal(next.scope.workspaceId,"wsp_b");assert.equal(next.scope.ventureId,"ven_b");
});
test("query ABA before callbacks never restores the old loaded generation",async()=>{
  for(const kind of ["gate","hub"] as const){const f=fixture(kind),old=await f.mount();f.query("workspace=wsp_b&venture=ven_b");f.render();f.commit();await flush();f.query("workspace=wsp_a&venture=ven_a");f.render();f.commit();await flush();const n=f.calls.length,w=f.writes.length;if(kind==="gate")old.changeWorkspace("wsp_b");else await old.onAction(old.cards[0]);assert.equal(f.calls.length,n);assert.equal(f.writes.length,w);}
});


test("live popstate URL retirement refuses callbacks before Next commits new search selectors",async()=>{
  for(const kind of ["gate","hub"] as const){const f=fixture(kind),old=await f.mount();f.liveQuery("workspace=wsp_b&venture=ven_b");const n=f.calls.length,w=f.writes.length;if(kind==="gate"){old.changeWorkspace("wsp_a");old.retry();}else{await old.onVerifyGmail();await old.onAction(old.cards[0]);await old.refresh();}assert.equal(f.calls.length,n);assert.equal(f.writes.length,w);assert.equal(f.render().ready,false);}
});
test("live URL retirement during a read withholds its result before a new React render",async()=>{
  const f=fixture(),old=await f.mount(),p=deferred<any>();f.setHandler(async(name,arg)=>name==="status"?p.promise:f.response(name,arg));const pending=old.refresh();f.liveQuery("workspace=wsp_b&venture=ven_b");const n=f.calls.length;p.resolve({...f.status(),workspace_id:"wsp_a"});await pending;assert.equal(f.calls.length,n);assert.equal(f.render().ready,false);
});
