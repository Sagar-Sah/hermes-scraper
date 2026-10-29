"""Manual release eligibility, precise requests and zero render-time mutations."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


FRONTEND = Path(__file__).resolve().parents[1]


def test_repair_release_controls_and_single_flight():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript'),React=req('react'),render=req('react-dom/server').renderToStaticMarkup;
const calls=[];const scope={exports:{},Date,require(name){
 if(name.endsWith('.css'))return {};
 if(name==='./api')return {api:async(...args)=>{calls.push(args);throw Error('Unexpected API call')},errorMessage:e=>e.message,humanize:v=>String(v).replace(/_/g,' ')};
 return req(name);
}};
const source=ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src/AutonomousRepairs.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(source,scope);
const {repairControls,repairActionRequest,RepairActionGate,RepairReleaseDetails,isRepairRecord}=scope.exports;
let checks=0;function check(value){assert.ok(value);checks++;}
const now=Date.now()/1000;
const record={id:'a'.repeat(32),status:'staging_verified',manifest_sha256:'b'.repeat(64),release_ready:true,release_hold_checks:[],
 manifest:{product_id:'9532',source:{source_observed_at:now-60},changes:[{kind:'set_cost'}],diff:[{kind:'set_cost',before:'15.00',after:'17.25'}]},
 staging_change:{change_id:'c'.repeat(32)},staging_verification:{checks:{calculator_data:'match',rendered_prices:'match',artwork_dimensions:'match',shipping:'match',customer_flow:'match'}}};
check(isRepairRecord(record));check(!isRepairRecord(null));check(!isRepairRecord({}));
check(!isRepairRecord({...record,id:'wrong'}));check(!isRepairRecord({...record,manifest:undefined}));
check(!repairControls(null,true,true,now).canApprove);
check(!repairControls(record,true,false,now).canApprove);
check(repairControls(record,true,true,now).canApprove);
check(!repairControls(record,true,true,now).canPromote); // A checkbox is not a saved approval.
check(!repairControls(record,false,true,now).canApprove);
check(!repairControls({...record,release_ready:false},true,true,now).canApprove);
check(!repairControls({...record,release_hold_checks:['shipping']},true,true,now).canApprove);
check(!repairControls({...record,status:'held'},true,true,now).canApprove);
check(!repairControls({...record,manifest_sha256:'wrong'},true,true,now).canApprove);
check(!repairControls({...record,manifest:{...record.manifest,source:{source_observed_at:now-21601}}},true,true,now).fresh);
check(!repairControls({...record,manifest:{...record.manifest,source:{source_observed_at:now+60}}},true,true,now).fresh);
check(!repairControls({...record,manifest:{...record.manifest,source:{}}},true,true,now).canApprove);
for(const key of Object.keys(record.staging_verification.checks)){
 const partial={...record,staging_verification:{checks:{...record.staging_verification.checks,[key]:'unknown'}}};
 check(!repairControls(partial,true,true,now).canApprove);
 check(repairControls(partial,true,true,now).holds.includes(key));
}
const approved={...record,production_approval:{actor:'human',manifest_sha256:record.manifest_sha256}};
check(repairControls(approved,true,false,now).canPromote);
check(!repairControls(approved,true,true,now).canApprove);
check(!repairControls({...approved,production_approval:{actor:'ai',manifest_sha256:record.manifest_sha256}},true,true,now).canPromote);
check(!repairControls({...approved,production_approval:{actor:'human',manifest_sha256:'f'.repeat(64)}},true,true,now).canPromote);
check(repairControls(record,false,false,now).canRollbackStaging);
const published={...approved,status:'production_verified',production_change:{change_id:'d'.repeat(32)}};
check(!repairControls(published,true,true,now).canRollbackStaging);
check(repairControls(published,true,true,now).canRollbackProduction);
check(!repairControls(published,false,true,now).canRollbackProduction);
check(!repairControls({...published,production_rollback:{status:'verified'}},true,true,now).canRollbackProduction);
const doc={...record,manifest:{...record.manifest,changes:[{kind:'add_document'}]}};
check(repairControls(doc,true,true,now).holds.includes('documents'));
check(repairControls({...doc,staging_verification:{checks:{...record.staging_verification.checks,documents:'match'}}},true,true,now).canApprove);
for(const action of ['approve-production','promote']){
 const request=repairActionRequest(action,record);
 check(request.path.endsWith('/'+action));
 check(request.body.manifest_sha256===record.manifest_sha256);
 check(!('actor' in request.body)); // Server derives the human actor; the browser cannot assert it.
}
check(JSON.stringify(repairActionRequest('recheck',record).body)==='{}');
check(repairActionRequest('rollback-staging',record).body.environment==='staging');
check(repairActionRequest('rollback-production',record).body.environment==='production');
const html=render(React.createElement(RepairReleaseDetails,{record,connected:true,acknowledged:false,busy:false,onAcknowledge(){},onAction(){}}));
check(html.includes(record.manifest_sha256));check(html.includes('15.00')&&html.includes('17.25'));
check(html.includes('Approve manifest')&&html.includes('Promote to main'));
check(html.includes('disabled=""'));check(!html.includes('checked=""'));
check(html.includes('Staging verification alone does not mean'));
const busy=render(React.createElement(RepairReleaseDetails,{record:approved,connected:true,acknowledged:true,busy:true,onAcknowledge(){},onAction(){}}));
check((busy.match(/disabled=""/g)||[]).length>=5);
const held=render(React.createElement(RepairReleaseDetails,{record:{...record,release_hold_checks:['shipping']},connected:true,acknowledged:false,busy:false,onAcknowledge(){},onAction(){}}));
check(held.includes('Release held: shipping'));
const empty=render(React.createElement(scope.exports.default));
check(empty.includes('Staging repairs'));check(empty.includes('Production release connection is not verified'));
check(empty.includes('Loading saved repairs'));check(calls.length===0);
(async()=>{
 const gate=new RepairActionGate();let requests=0,finish;
 const first=gate.run(()=>{requests++;return new Promise(resolve=>{finish=resolve})});
 const second=await gate.run(async()=>{requests++});
 check(second===false&&requests===1);finish();check(await first===true);
 try{await gate.run(async()=>{throw Error('offline')})}catch(error){check(error.message==='offline')}
 check(await gate.run(async()=>{requests++})===true);check(requests===2);
 process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,api_calls_during_render:calls.length}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    value = json.loads(result.stdout)
    assert value["status"] == "PASS" and value["assertions"] >= 50
    assert value["api_calls_during_render"] == 0
