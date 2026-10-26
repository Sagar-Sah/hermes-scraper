"""Actual update client and state helpers in Node; no service, installer or browser."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

FRONTEND=Path(__file__).resolve().parents[1]


def test_update_client_safe_apply_poll_reconnect_and_failure_contract():
    if not shutil.which('node') or not (FRONTEND/'node_modules/typescript').is_dir():
        pytest.skip('Install dashboard dependencies for TypeScript tests')
    code=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path'),{createRequire}=require('node:module');
const frontend=process.argv[1],requireHere=createRequire(path.join(frontend,'package.json'));
const ts=requireHere('typescript');
const js=ts.transpileModule(fs.readFileSync(path.join(frontend,'src/hermesUpdateState.ts'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022}}).outputText;
const scope={exports:{},AbortSignal};vm.runInNewContext(js,scope);
const {HermesUpdateClient,canApplyUpdate,updateComplete,updateStage,updateReason}=scope.exports;
let assertions=0;const check=(v)=>{assert.ok(v);assertions++};
const old='a'.repeat(40),next='b'.repeat(40);
const status=(extra={})=>({managed:true,can_check:true,can_apply:true,current_commit:old,target_commit:next,update_available:true,check_id:'checked-target-1',reason:null,operation:null,...extra});
const op=(extra={})=>({id:'op1',kind:'apply',status:'running',stage:'backing_up',error_code:null,updated_at:1,collection_paused:true,...extra});
(async()=>{
check(canApplyUpdate(status()));
for(const extra of [{managed:false},{can_apply:false},{update_available:false},{target_commit:'b'.repeat(39)},{check_id:null},{check_id:''},{operation:op()}])check(!canApplyUpdate(status(extra)));
check(!updateComplete(status({operation:op({kind:'check',status:'completed',stage:'completed'}),current_commit:next})));
check(updateStage(status({operation:op({kind:'check',status:'completed',stage:'completed'})}),null)==='Update available');
check(updateComplete(status({operation:op({status:'completed',stage:'completed'}),current_commit:next})));
check(!updateComplete(status({operation:op({status:'completed',stage:'completed'})})));
check(updateStage(status({operation:op({status:'completed',stage:'completed'})}),null)==='Waiting for the updated service');
for(const value of ['constructor','__proto__','toString']){check(typeof updateReason(value)==='string');check(typeof updateStage(status({operation:op({stage:value})}),null)==='string');}
check(updateReason('<private raw error>')==='The updater needs attention. Check the server update report.');
let calls=[],reply=status();
const client=new HermesUpdateClient(async(p,o)=>{calls.push([p,o]);return reply;});
await client.refresh();await client.check();check(calls[1][0]==='/updates/check'&&calls[1][1].body==='{}');
reply=status({can_apply:false,operation:op({status:'preparing',stage:'preparing'})});
await client.apply();check(calls.length===3&&calls[2][0]==='/updates/apply');
check(calls[2][1].method==='POST'&&calls[2][1].body===JSON.stringify({expected_commit:next,check_id:'checked-target-1'}));
await client.apply();check(calls.length===3); // Active operation cannot be submitted twice.
check(client.view.status.operation.collection_paused===true);
// Restart disconnect and recovery: never POST another apply.
let fail=false,requested=[];
const reconnect=new HermesUpdateClient(async(p,o)=>{requested.push([p,o]);if(p==='/updates/apply'||fail)throw Error('connection gone');return reply;});
reply=status();await reconnect.refresh();await reconnect.apply();check(reconnect.view.reconnecting&&reconnect.view.errorKind==='action');
fail=true;await reconnect.refresh();check(reconnect.view.error.includes('lost its connection'));
fail=false;reply=status({can_apply:false,operation:op({stage:'restarting'})});await reconnect.refresh();check(!reconnect.view.reconnecting&&!reconnect.view.error);
reply=status({can_apply:false,current_commit:next,operation:op({status:'completed',stage:'completed'})});await reconnect.refresh();check(updateComplete(reconnect.view.status));
check(requested.filter(([p])=>p==='/updates/apply').length===1);
// A failed explicit check remains visible even while ordinary status polling succeeds.
const checkFailure=new HermesUpdateClient(async(p)=>{if(p==='/updates/check')throw Error('check failed');return status();});
await checkFailure.refresh();await checkFailure.check();const message=checkFailure.view.error;await checkFailure.refresh();check(!!message&&checkFailure.view.error===message);
// Old in-flight status cannot erase the accepted operation after apply.
let resolveOld,number=0;
const stale=new HermesUpdateClient(async(p)=>{if(p==='/updates/status'&&++number===2)return new Promise(r=>resolveOld=r);return p==='/updates/apply'?status({operation:op()}):status();});
await stale.refresh();const reading=stale.refresh();await stale.apply();resolveOld(status());await reading;check(stale.view.status.operation.id==='op1');
// Concurrent clicks admit one mutating request, not two.
let release,mutations=0;
const twice=new HermesUpdateClient(async(p)=>{if(p==='/updates/apply'){mutations++;return new Promise(r=>release=r);}return status();});
await twice.refresh();const applying=twice.apply();await twice.apply();check(mutations===1);release(status({operation:op()}));await applying;
// Durable failure details remain attached after reconnect, not overwritten by connectivity UI.
reply=status({can_apply:false,reason:'dirty_checkout',operation:op({status:'failed',stage:'failed',error_code:'dirty_checkout'})});await client.refresh();check(client.view.status.operation.error_code==='dirty_checkout');check(updateReason(client.view.status.reason).includes('Local source changes'));
process.stdout.write(JSON.stringify({status:'PASS',assertions,source_requests:0,installer_runs:0}));
})().catch(e=>{console.error(e);process.exit(1)});
'''
    result=subprocess.run(['node','-e',code,str(FRONTEND)],text=True,capture_output=True,check=True)
    value=json.loads(result.stdout)
    assert value['status']=='PASS' and value['assertions']>=35


def test_update_notice_has_real_update_and_stale_dashboard_reload_controls():
    if not shutil.which('node') or not (FRONTEND/'node_modules/typescript').is_dir():
        pytest.skip('Install dashboard dependencies for TypeScript tests')
    code=r'''
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
const path=require('node:path'),{createRequire}=require('node:module');
const dir=process.argv[1],req=createRequire(path.join(dir,'package.json')),ts=req('typescript'),React=req('react'),render=req('react-dom/server').renderToStaticMarkup;
const source=ts.transpileModule(fs.readFileSync(path.join(dir,'src/BuildStatus.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const scope={exports:{},require:req,__HERMES_UI_BUILD__:'a'.repeat(64)};vm.runInNewContext(source,scope);
const {BuildNotice}=scope.exports;
const build={restart_required:true,frontend:{rebuild_required:false,built_source_sha256:'a'.repeat(64)}};
const renderNotice=(value)=>render(React.createElement(BuildNotice,{build:value,received:true,onUpdate:()=>{}}));
assert.ok(renderNotice(build).includes('>Update Hermes</button>'));
assert.ok(renderNotice({...build,restart_required:false,frontend:{...build.frontend,built_source_sha256:'b'.repeat(64)}}).includes('>Reload dashboard</button>'));
assert.equal(renderNotice({...build,restart_required:false}),'');
process.stdout.write('PASS');
'''
    result=subprocess.run(['node','-e',code,str(FRONTEND)],text=True,capture_output=True,check=True)
    assert result.stdout=='PASS'
