import assert from "node:assert/strict";
import test from "node:test";
import fs from "node:fs";
import vm from "node:vm";
import ts from "typescript";
const source=fs.readFileSync(new URL("../components/desktop/DesktopNoticeBanner.tsx",import.meta.url),"utf8");
const tree=ts.createSourceFile("DesktopNoticeBanner.tsx",source,ts.ScriptTarget.Latest,true,ts.ScriptKind.TSX);
let effect="";
function visit(node:ts.Node){
 if(!effect&&ts.isCallExpression(node)&&node.expression.getText(tree)==="useEffect")effect=node.arguments[0].getText(tree);
 ts.forEachChild(node,visit);
}visit(tree);assert.ok(effect);
const event={id:"notice_a",title:"A review",body:"A saved work",target_path:"/v/ven_a/now",subject:"usr_a",device_id:"device_a",auth_epoch:7};
function fixture(){
 let actor:string|null="usr_a",epoch=1;
 let listener!:(value:unknown)=>Promise<void>,retire!:()=>void;
 let release!:(value:unknown)=>void;
 const paused=new Promise(resolve=>{release=resolve;});
 const updates:unknown[]=[],calls:string[]=[];
 const context=vm.createContext({isDesktopShell:()=>true,verifiedSessionSubject:()=>actor,verifiedSessionEpoch:()=>epoch,
  desktopListen:async(_name:string,callback:typeof listener)=>{listener=callback;return()=>calls.push("unsubscribe");},
  desktopInvoke:async(name:string)=>{calls.push(name);return paused;},setNotice:(value:unknown)=>updates.push(value),
  window:{addEventListener:(_name:string,callback:()=>void)=>{retire=callback;},removeEventListener:()=>calls.push("remove")}});
 const compiled=ts.transpileModule(`globalThis.start=${effect};`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText;
 vm.runInContext(compiled,context);const cleanup=context.start();
 return{calls,updates,deliver:(value:unknown=event)=>listener(value),release,cleanup,retire:()=>retire(),change:(value:string|null)=>{actor=value;epoch++;}};
}
test("actual notice listener requires current native account/device/generation before showing",async()=>{
 for(const mismatch of ["subject","device_id","auth_epoch",null]){
  const f=fixture(),pending=f.deliver();
  const current=mismatch?{...event,[mismatch]:mismatch==="auth_epoch"?8:"other"}:event;
  f.release(current);await pending;
  assert.equal(f.updates.length,mismatch?0:1);
 }
});
test("actual listener withholds unsigned and signed-out notices before custody lookup",async()=>{
 const f=fixture();await f.deliver({...event,subject:undefined});assert.equal(f.calls.length,0);
 f.change(null);await f.deliver();assert.equal(f.calls.length,0);assert.equal(f.updates.length,0);
});
test("actual listener clears displayed notice and retires an in-flight old session",async()=>{
 const f=fixture(),pending=f.deliver();f.change("usr_b");f.retire();f.release(event);await pending;
 assert.deepEqual(f.updates,[null]);
});
test("actual A to B to A listener rejects delayed old native generation",async()=>{
 const f=fixture();f.change("usr_b");f.change("usr_a");
 const pending=f.deliver();f.release({...event,auth_epoch:9});await pending;assert.equal(f.updates.length,0);
});
test("actual disposed listener cannot commit a pending receipt and unsubscribes",async()=>{
 const f=fixture(),pending=f.deliver();f.cleanup();f.release(event);await pending;await new Promise(resolve=>setImmediate(resolve));
 assert.equal(f.updates.length,0);assert.ok(f.calls.includes("unsubscribe"));assert.ok(f.calls.includes("remove"));
});


test("actual committed banner effect sends an exact native receipt and never invents an unsigned presentation",()=>{
 let committed="";function find(node:ts.Node){
  if(ts.isCallExpression(node)&&node.expression.getText(tree)==="useEffect"&&node.arguments[0].getText(tree).includes('"desktop_notice_presented"'))committed=node.arguments[0].getText(tree);
  ts.forEachChild(node,find);
 }find(tree);assert.ok(committed);
 for(const value of [event,null,{...event,subject:"usr_other"}]){
  const calls:unknown[]=[],context=vm.createContext({notice:value,alreadyInChat:false,verifiedSessionSubject:()=>"usr_a",verifiedSessionEpoch:()=>1,
   desktopInvoke:(name:string,args:unknown)=>{calls.push({name,args});return Promise.resolve(true);},
   window:{addEventListener:()=>undefined,removeEventListener:()=>undefined}});
  vm.runInContext(ts.transpileModule(`globalThis.start=${committed};`,{compilerOptions:{target:ts.ScriptTarget.ES2022}}).outputText,context);
  context.start();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),value===event?[{name:"desktop_notice_presented",args:{noticeId:event.id,subject:event.subject,deviceId:event.device_id,authEpoch:event.auth_epoch}}]:[]);
 }
});
