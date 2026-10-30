"""Autonomy presentation and visible-only polling; no network or user browser."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest

FRONTEND = Path(__file__).resolve().parents[1]


def test_autonomous_dashboard_evidence_budget_and_hidden_polling():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Install dashboard dependencies for TypeScript tests")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const {createRequire}=require('node:module');
const frontend=process.argv[1],req=createRequire(path.join(frontend,'package.json'));
const ts=req('typescript'),React=req('react'),render=req('react-dom/server').renderToStaticMarkup;
const apiCalls=[];
const source=ts.transpileModule(fs.readFileSync(path.join(frontend,'src/AutonomousCompanion.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
const scope={exports:{},setTimeout,clearTimeout,Intl,Date,require(name){
  if(name.endsWith('.css'))return {};
  if(name==='./AutonomousRepairs')return {__esModule:true,default:()=>null}; // Repair controls have their own behavior suite.
  if(name==='./api')return {api:async(...args)=>{apiCalls.push(args);throw Error('Unexpected test API call')},count:v=>Number(v).toLocaleString(),errorMessage:e=>e.message,humanize:v=>String(v).replace(/_/g,' ')};
  return req(name);
}};
vm.runInNewContext(source,scope);
const {autonomyMoney,autonomyDuration,autonomyProductVerdict,isMuseProfile,AutonomousStatusPoller,AutonomousProductTable}=scope.exports;
let checks=0;function check(value){assert.ok(value);checks++;}
check(autonomyMoney(undefined)==='Not measured');check(autonomyMoney(null)==='Not measured');
check(autonomyMoney('')==='Not measured');check(autonomyMoney(NaN)==='Not measured');check(autonomyMoney(-1)==='Not measured');
check(autonomyMoney('25.00')==='$25.00');check(autonomyMoney('0')==='$0.00');check(autonomyMoney('2.125')==='$2.13');
check(autonomyDuration(null)==='Not measured');check(autonomyDuration(NaN)==='Not measured');check(autonomyDuration(-1)==='Not measured');
check(autonomyDuration(0)==='0s');check(autonomyDuration(65)==='1m 5s');check(autonomyDuration(3600)==='1h 00m');
check(isMuseProfile({name:'Office Muse',model:'muse-spark'}));check(!isMuseProfile({name:'Other',model:'unrelated'}));
const pilot=scope.exports.targetedPilotRequest('9532, 6862\n9532');
check(pilot.kind==='manual'&&pilot.product_ids.join(',')==='9532,6862');
check(Object.keys(pilot).join(',')==='kind,product_ids');
check(scope.exports.targetedPilotRequest(Array.from({length:99},(_,i)=>String(i+1)).join(',')).product_ids.length===99);
for(const bad of ['', '0', '09532', '-1', '9532/x', '9532.0', Array.from({length:100},(_,i)=>String(i+1)).join(','), '1'.repeat(20)]){assert.throws(()=>scope.exports.targetedPilotRequest(bad),/Enter 1–99 product IDs/);checks++;}
const readyCycle={id:'a'.repeat(32),status:'ready',can_supersede:true,revision_sha256:'b'.repeat(64),inventory_baseline_sha256:'c'.repeat(64)};
const retire=scope.exports.autonomySupersedeRequest(readyCycle);
check(retire.path==='/companion/autonomy/cycles/'+readyCycle.id+'/supersede'&&retire.method==='POST');
const retireBody=JSON.parse(retire.body);
check(retireBody.expected_revision_sha256===readyCycle.revision_sha256&&retireBody.expected_inventory_sha256===readyCycle.inventory_baseline_sha256);
check(retireBody.reason==='reviewed_mapping_inventory_refresh');
check(JSON.parse(scope.exports.autonomySupersedeRequest({...readyCycle,status:'inventory_failed',inventory_baseline_sha256:null}).body).expected_inventory_sha256===null);
for(const change of [{status:'running'},{can_supersede:false},{revision_sha256:'stale'},{inventory_baseline_sha256:'invalid'}]){assert.throws(()=>scope.exports.autonomySupersedeRequest({...readyCycle,...change}),/unstarted_inventory_revision_required/);checks++;}
check(!('start' in retireBody)&&!('create_replacement' in retireBody));
const base={id:'p1',product_id:'6383',title:'Coroplast',supplier:'4over',status:'verified'};
check(!autonomyProductVerdict(base).verified);
check(!autonomyProductVerdict({...base,result:{evidence_complete:true}}).verified);
check(!autonomyProductVerdict({...base,result:{evidence_complete:true,checks:{cost:'unknown'}}}).verified);
check(!autonomyProductVerdict({...base,result:{evidence_complete:true,unknown_checks:[],checks:{cost:'unknown'}}}).verified);
check(!autonomyProductVerdict({...base,result:{evidence_complete:true,complete:false,checks:{cost:'match'}}}).verified);
check(!autonomyProductVerdict({...base,status:'held',result:{complete:true,checks:{cost:'match'}}}).verified);
check(autonomyProductVerdict({...base,result:{evidence_complete:true,checks:{cost:'match',production:{status:'verified'},mail:'not_applicable'}}}).verified);
check(!autonomyProductVerdict({...base,result:{complete:true,checks:{cost:'match'}}}).verified);
check(autonomyProductVerdict({...base,result:{evidence_complete:true,checks:{cost:'passed'}}}).verified);
check(autonomyProductVerdict({...base,result:{complete:true,checks:{cost:'match'},unknown_checks:['quantity']}}).unknown===1);
const html=render(React.createElement(AutonomousProductTable,{products:[base,{...base,id:'p2',product_id:'6669',status:'held',reason:'supplier_login_required',repair_status:'not_applied',timing:{active_seconds:3600,elapsed_seconds:7200}},{...base,id:'p3',product_id:'9532',result:{evidence_complete:true,checks:{cost:'match',quantity:'verified'}}}]}));
check(html.includes('Needs evidence'));check(html.includes('Evidence coverage unknown'));check(html.includes('supplier login required'));
check(html.includes('1h 00m')&&html.includes('2h 00m'));check((html.match(/autonomy-verified/g)||[]).length===1);
check(!html.includes('href="undefined"'));check(html.includes('WooCommerce 6669'));
const empty=render(React.createElement(scope.exports.default));
check(empty.includes('Monthly checks'));check(empty.includes('Schedule unknown'));
check(empty.includes('Production · read only'));check(empty.includes('Staging · supported repair writes'));
check(empty.includes('Not measured'));check(empty.includes('Hard cap: $25'));
check(!empty.includes('checked=""'));check(apiCalls.length===0); // SSR and render do not fetch or launch jobs.
(async()=>{
  let visible=false,reads=0,errors=0,timer=null,delay=null,clears=0,complete;
  const clock={set(fn,ms){check(timer===null);timer=fn;delay=ms;return 1},clear(id){check(id===1);timer=null;clears++}};
  const poller=new AutonomousStatusPoller(()=>{reads++;return new Promise(resolve=>{complete=resolve})},()=>visible,()=>errors++,clock);
  poller.start();check(reads===0&&timer===null); // Hidden on first mount makes no request.
  visible=true;poller.wake();check(reads===1&&timer===null);
  poller.wake();poller.wake();check(reads===1); // No overlapping GETs.
  complete();await new Promise(setImmediate);check(delay===10000&&typeof timer==='function');
  visible=false;poller.wake();check(timer===null&&clears===1); // Visibility event removes pending poll.
  visible=true;poller.wake();check(reads===2);
  visible=false;complete();await new Promise(setImmediate);check(timer===null); // Hiding during a request does not schedule another.
  visible=true;poller.wake();check(reads===3);poller.stop();complete();await new Promise(setImmediate);
  check(timer===null&&errors===0);poller.wake();check(reads===3); // Unmounted cannot restart itself.
  let failures=0,pending;
  const fail=new AutonomousStatusPoller(async()=>{throw Error('offline')},()=>true,()=>failures++,{set(fn){pending=fn;return 2},clear(){pending=null}});
  fail.start();await new Promise(setImmediate);check(failures===1&&typeof pending==='function');fail.stop();check(pending===null);
  process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,external_requests:0,source_requests:0,service_installs:0}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["assertions"] >= 50
    assert receipt["external_requests"] == receipt["source_requests"] == receipt["service_installs"] == 0


def test_service_plan_and_plist_are_noninstalling_and_preserve_runtime():
    script = FRONTEND.parent / "scripts/install-macos-service.sh"
    plan = subprocess.run(["bash", str(script), "--dry-run"], capture_output=True, text=True, check=True)
    value = json.loads(plan.stdout)
    assert value["writes"] is False and value["starts_service"] is False
    assert value["starts_audit_schedule"] is False and value["enables_automatic_login"] is False
    assert value["requires_normal_desktop_login"] is True
    assert value["browser_limit"] == "existing .env value, otherwise 4"
    import plistlib
    definition = subprocess.run(["bash", str(script), "--print-plist"], capture_output=True, check=True)
    data = plistlib.loads(definition.stdout)
    assert data["ProgramArguments"] == ["/bin/bash", str(FRONTEND.parent / "scripts/start-local.sh")]
    assert data["WorkingDirectory"] == str(FRONTEND.parent)
    assert data["KeepAlive"] == {"SuccessfulExit": False}
    assert data["ThrottleInterval"] == 30 and data["LimitLoadToSessionType"] == ["Aqua"]
    assert set(data["EnvironmentVariables"]) == {"ATLAS_PORT", "PATH"}
    assert "password" not in definition.stdout.decode().lower() and "api_key" not in definition.stdout.decode().lower()
    syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, check=True)
    assert syntax.returncode == 0
    invalid = subprocess.run(["bash", str(script), "--install", "--force"], capture_output=True)
    assert invalid.returncode == 2


def test_autonomy_setup_recovery_and_readiness_require_real_evidence():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript'),React=req('react'),render=req('react-dom/server').renderToStaticMarkup;
const calls=[];const scope={exports:{},setTimeout,clearTimeout,Intl,Date,require(name){
 if(name.endsWith('.css'))return {};
 if(name==='./AutonomousRepairs')return {__esModule:true,default:()=>null};
 if(name==='./api')return {api:async(...args)=>{calls.push(args);throw Error('Unexpected request')},count:v=>String(v),errorMessage:e=>e.message,humanize:v=>String(v).replace(/_/g,' ')};
 return req(name);
}};
const source=ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src/AutonomousCompanion.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(source,scope);
const {autonomyRecoveryRequest,AutonomyMaintenanceNotice,AutonomyBaselineDetails,DirectorReadinessDetails}=scope.exports;
let checks=0;function check(value){assert.ok(value);checks++;}
function nodes(node,result=[]){if(!node||typeof node!=='object')return result;if(Array.isArray(node)){node.forEach(n=>nodes(n,result));return result;}result.push(node);nodes(node.props?.children,result);return result;}
for(const [active,ack] of [[false,false],[false,true],[true,false]]){
 assert.throws(()=>autonomyRecoveryRequest(active,ack),/maintenance_recovery_acknowledgment_required/);checks++;
}
const request=autonomyRecoveryRequest(true,true);
check(request.path==='/companion/autonomy/maintenance/end');check(request.method==='POST'&&request.body==='{}');
check(!request.path.includes('resume')&&!request.path.includes('start'));check(!request.body.includes('actor'));
let acknowledged=false,recovered=0;
const props={maintenance:true,acknowledged:false,busy:false,onAcknowledge:v=>acknowledged=v,onRecover:()=>{recovered++}};
check(AutonomyMaintenanceNotice({...props,maintenance:false})===null);
const notice=AutonomyMaintenanceNotice(props),elements=nodes(notice);
check(recovered===0);check(elements.find(n=>n.type==='button').props.disabled===true);
const box=elements.find(n=>n.type==='input');check(box.props.checked===false);
box.props.onChange({target:{checked:true}});check(acknowledged===true);
const ready=AutonomyMaintenanceNotice({...props,acknowledged:true});
check(nodes(ready).find(n=>n.type==='button').props.disabled===false);
nodes(ready).find(n=>n.type==='button').props.onClick();check(recovered===1);
check(nodes(AutonomyMaintenanceNotice({...props,acknowledged:true,busy:true})).find(n=>n.type==='button').props.disabled===true);
const noticeHtml=render(notice);check(noticeHtml.includes('uncertain source requests remain held'));check(noticeHtml.includes('without resuming or repeating held work'));
let captures=0,refreshes=0;
const base={baseline:{ready:false,reason:'staging_baseline_backup_and_parity_required'},capture:null,connected:false,busy:false,onCapture:()=>captures++,onRefresh:()=>refreshes++};
const missing=AutonomyBaselineDetails(base),buttons=nodes(missing).filter(n=>n.type==='button');
check(buttons.length===2&&buttons[0].props.disabled===true&&buttons[1].props.disabled===false);
buttons[1].props.onClick();check(refreshes===1&&captures===0);
const capture={id:'a'.repeat(64),manifest_sha256:'b'.repeat(64),captured_at:1720000000,ready:false,backups_verified:false,runtime_parity_verified:false,isolation_verified:false,production:{plugins:[]},staging:{plugins:[]}};
const inventory=AutonomyBaselineDetails({...base,connected:true,capture});
check(nodes(inventory).filter(n=>n.type==='button')[0].props.disabled===false);
nodes(inventory).filter(n=>n.type==='button')[0].props.onClick();check(captures===1);
const html=render(inventory);check(html.includes('Staging repairs held'));check(!html.includes('Approved staging baseline'));
const alignment={capture_id:capture.id,capture_manifest_sha256:capture.manifest_sha256,ready:false,read_only:true,
 summary:{mismatch:1,unknown:2,preserve_staging_records:1,unknown_domains:3},records:[{id:'fixture-plugin',domain:'plugins',key:'fixture',name:'Fixture Studio',status:'mismatch',reason:'same_version_code_changed',production:{version:'1.0'},staging:{version:'1.0'},proposed_action:'review_and_preserve',preserve_staging_before_alignment:true}],required_order:['verify_backups_before_alignment']};
const comparison=render(AutonomyBaselineDetails({...base,connected:true,capture,alignment}));
check(comparison.includes('Saved environment comparison')&&comparison.includes('Fixture Studio'));
check(comparison.includes('Preserve staging work first')&&comparison.includes('same version code changed'));
check(comparison.includes('does not install files')&&!comparison.includes('Approved staging baseline'));
check(!render(AutonomyBaselineDetails({...base,capture,alignment:{...alignment,capture_id:'c'.repeat(64)}})).includes('Fixture Studio'));
check(html.includes(capture.id)&&html.includes(capture.manifest_sha256));
check(html.includes('does not copy data, create a backup, approve parity or enable writes'));
for(const phrase of ['production backup','staging backup','preserved unfinished staging work','functional runtime parity','staging isolation'])check(html.includes(phrase));
check(html.includes('Missing or unknown evidence keeps repairs held'));
check(nodes(AutonomyBaselineDetails({...base,connected:true,busy:true})).filter(n=>n.type==='button').every(n=>n.props.disabled===true));
const approved=render(React.createElement(AutonomyBaselineDetails,{...base,baseline:{ready:true,id:capture.id,approved_at:1720000000,reason:'approved_staging_baseline'}}));
check(approved.includes('Approved staging baseline'));check(approved.includes('Every repair still checks the current environment'));
const unknown=render(React.createElement(DirectorReadinessDetails,{checked:null,busy:false}));
check(unknown.includes('Director held'));check(unknown.includes('director preflight required'));check(!unknown.includes('Director ready'));
const denied=render(React.createElement(DirectorReadinessDetails,{saved:{ready:false,reason:'director_preflight_required'},checked:{ready:false,reason:'director_provider_request_rejected_401'},busy:false}));
check(denied.includes('Sources &amp; AI'));check(denied.includes('protected provider field'));check(denied.includes('AI decisions stay held'));
const stale=render(React.createElement(DirectorReadinessDetails,{saved:{ready:false,reason:'director_disabled'},checked:{ready:true,reason:'ready'},busy:false}));
check(stale.includes('Director held')&&!stale.includes('Director ready'));
const budget=render(React.createElement(DirectorReadinessDetails,{checked:{ready:false,reason:'director_isolated_monthly_key_limit_required'},busy:false}));
check(budget.includes('monthly limit of $25 or less'));check(budget.includes('does not change a shared key'));
const keyFacts={ready:false,reason:'director_isolated_monthly_key_limit_required',observed_limit_usd:'100',observed_reset:'monthly',required_cap_usd:'25.00'};
const observed=render(React.createElement(DirectorReadinessDetails,{saved:keyFacts,checked:{ready:false,reason:keyFacts.reason},busy:false}));
check(observed.includes('OpenRouter currently reports a $100.00 key limit per month'));
check(observed.includes('Hermes requires $25.00 per month or less')&&observed.includes('account credit is separate'));
const rotatedFacts=render(React.createElement(DirectorReadinessDetails,{saved:{ready:false,reason:'director_preflight_required'},checked:keyFacts,busy:false}));
check(!rotatedFacts.includes('$100.00')); // Current status cannot inherit the former credential's diagnostics.
for(const invalid of [null,'NaN','Infinity','-1',true,{secret:'private'}]){
 const invalidFacts=render(React.createElement(DirectorReadinessDetails,{saved:{...keyFacts,observed_limit_usd:invalid},checked:null,busy:false}));
 check(!invalidFacts.includes('OpenRouter currently reports')&&!invalidFacts.includes('private'));
}
const checking=render(React.createElement(DirectorReadinessDetails,{saved:{ready:true,reason:'ready'},checked:null,busy:true}));
check(checking.includes('no inference request is sent'));check(!checking.includes('Director ready'));
const {environmentConnectionReady,environmentConnectionRequest,EnvironmentConnection}=scope.exports;
const savedProfile={configured:true,connected:false,base_url:'https://staging3.tonyl36.sg-host.com',role:'write',protected_preview_credentials_present:true};
check(!environmentConnectionReady('staging',undefined,'','',''));
check(environmentConnectionReady('staging',savedProfile,'','',''));
check(environmentConnectionReady('staging',savedProfile,'','preview','password'));
check(!environmentConnectionReady('staging',savedProfile,'','preview',''));
check(!environmentConnectionReady('staging',savedProfile,'','','password'));
check(!environmentConnectionReady('staging',undefined,'','preview','password'));
check(environmentConnectionReady('staging',undefined,'new-token','preview','password'));
const reused=environmentConnectionRequest('staging',savedProfile,'','preview','password');
check(reused.base_url===savedProfile.base_url&&reused.role==='write');
check(!('token' in reused)&&reused.protection_auth.username==='preview'&&reused.protection_auth.password==='password');
const rechecked=environmentConnectionRequest('staging',savedProfile,'','','');
check(!('token' in rechecked)&&!('protection_auth' in rechecked));
check(environmentConnectionRequest('production',{configured:true,base_url:'https://printitprints.com'},'','','').role==='read');
check(environmentConnectionRequest('staging',savedProfile,' replacement-token ','','').token==='replacement-token');
assert.throws(()=>environmentConnectionRequest('staging',undefined,'','',''),/connector_token_or_complete_preview_credentials_required/);checks++;
const savedHtml=render(React.createElement(EnvironmentConnection,{environment:'staging',profile:savedProfile,busy:false,onSave:async()=>{throw Error('Unexpected save')}}));
check(savedHtml.includes('Leave blank to keep the saved connector token'));
check(savedHtml.includes('Leave both blank to keep them'));
check(savedHtml.includes('Replace saved connector token'));
check(!savedHtml.includes('name="hermes-staging-connector-token"'));
check(savedHtml.includes('name="hermes-staging-preview-password"')&&savedHtml.includes('autoComplete="new-password"'));
const needsPreview=render(React.createElement(EnvironmentConnection,{environment:'staging',profile:{...savedProfile,reason:'companion_connector_http_401'},busy:false,onSave:async()=>{throw Error('Unexpected save')}}));
check(needsPreview.includes('<details open="">'));
const tokenInput=render(React.createElement(scope.exports.ConnectorTokenField,{environment:'staging',token:'',configured:true,busy:false,onChange(){}}));
check(tokenInput.includes('name="hermes-staging-connector-token"')&&tokenInput.includes('autoComplete="new-password"'));
check(tokenInput.includes('Staging preview sign-in belongs in Preview username and Preview password'));
check(!savedHtml.includes('required=""'));
check(!savedHtml.includes('disabled=""'));
check(!savedHtml.includes('replacement-token')&&!savedHtml.includes('protected-password'));
const newHtml=render(React.createElement(EnvironmentConnection,{environment:'staging',busy:false,onSave:async()=>{throw Error('Unexpected save')}}));
check(newHtml.includes('required=""')&&newHtml.includes('disabled=""'));
const checkedButOff=render(React.createElement(DirectorReadinessDetails,{saved:{ready:false,preflight_ready:true,reason:'director_disabled'},checked:null,busy:false}));
check(checkedButOff.includes('Provider check passed · Director disabled'));
check(checkedButOff.includes('AI reviews remain off until you enable'));
const rotatedKey=render(React.createElement(DirectorReadinessDetails,{saved:{ready:false,preflight_ready:false,reason:'director_preflight_required'},checked:{ready:false,preflight_ready:true,reason:'director_disabled'},busy:false}));
check(!rotatedKey.includes('Provider check passed'));check(rotatedKey.includes('Director held'));
const feedback=scope.exports.environmentVerificationFeedback;
check(feedback('staging',{configured:true,connected:true}).verified===true);
check(feedback('staging',{configured:true,connected:true}).message.includes('connection verified'));
const blocked=feedback('staging',{configured:true,connected:false,reason:'companion_connector_http_401'});
check(blocked.verified===false&&blocked.message.includes('NOT verified'));
check(blocked.message.includes('without retyping')&&blocked.message.includes('Preview username and Preview password together'));
check(!feedback('staging',{connected:false}).message.includes('Credentials saved'));
check(calls.length===0); // Rendering status and inventory cannot approve, write or launch audits.
process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,requests_during_render:calls.length}));
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["assertions"] >= 40
    assert receipt["requests_during_render"] == 0


def test_provider_changes_and_manual_refresh_reload_setup_without_clearing_edits():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript'),React=req('react');
const hooks=[],effects=[];let cursor=0;
const same=(a,b)=>a&&b&&a.length===b.length&&a.every((v,i)=>Object.is(v,b[i]));
const fakeReact={...React,
 useState(initial){const i=cursor++;if(!(i in hooks))hooks[i]=typeof initial==='function'?initial():initial;return [hooks[i],v=>{hooks[i]=typeof v==='function'?v(hooks[i]):v}];},
 useRef(initial){const i=cursor++;if(!(i in hooks))hooks[i]={current:initial};return hooks[i];},
 useCallback(fn,deps){const i=cursor++;if(!hooks[i]||!same(hooks[i].deps,deps))hooks[i]={fn,deps};return hooks[i].fn;},
 useEffect(fn,deps){const i=cursor++;if(!hooks[i]||!same(hooks[i].deps,deps)){hooks[i]={deps};effects.push({fn,deps});}}
};
let server={config:{director_provider_id:'muse-one',director_enabled:false},director:{ready:false,reason:'director_disabled'}};
hooks[0]=server;
let profiles=[{id:'muse-one',name:'Office Muse',model:'meta/muse-spark'}];const calls=[];
let connectionResult={configured:true,connected:false,reason:'companion_connector_http_401'},statusFails=false;
const responses={'/companion/autonomy/cycles':{items:[]},'/companion/autonomy/environments':{},'/companion/autonomy/baseline':{ready:false},'/companion/autonomy/baseline/latest':{capture:null}};
const scope={exports:{},setTimeout,clearTimeout,Intl,Date,require(name){
 if(name==='react')return fakeReact;
 if(name.endsWith('.css'))return {};
 if(name==='./AutonomousRepairs')return {__esModule:true,default:()=>null};
 if(name==='./api')return {api:async(route,options)=>{calls.push(route);if(route==='/companion/autonomy/environments/staging'&&options?.method==='PUT'){if(connectionResult instanceof Error)throw connectionResult;return connectionResult;}if(route==='/providers')return {items:profiles};if(route==='/companion/autonomy/status'){if(statusFails)throw Error('Status unavailable');return server;}if(route==='/companion/autonomy/director/preflight')throw Error('director_isolated_monthly_key_limit_required');if(route in responses)return responses[route];throw Error('Unexpected route '+route);},count:String,errorMessage:e=>e.message,humanize:v=>String(v).replace(/_/g,' ')};
 return req(name);
}};
const source=ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src/AutonomousCompanion.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(source,scope);
function draw(){cursor=0;effects.length=0;return scope.exports.default();}
function nodes(node,out=[]){if(!node||typeof node!=='object')return out;if(Array.isArray(node)){node.forEach(x=>nodes(x,out));return out;}out.push(node);nodes(node.props?.children,out);return out;}
function providerEffect(){return effects.find(e=>e.deps?.length===2&&typeof e.deps[1]==='function'&&e.deps[0]===server.config.director_provider_id);}
let checks=0;function check(v){assert.ok(v);checks++;}
(async()=>{
 draw();const first=providerEffect();check(!!first);first.fn();await new Promise(setImmediate);
 check(calls.filter(x=>x==='/providers').length===1);
 draw();check(!providerEffect()); // Stable provider ID does not poll the provider list.
 profiles=[...profiles,{id:'muse-two',name:'Second Muse',model:'meta/muse-spark'}];
 server={...server,config:{...server.config,director_provider_id:'muse-two'}};hooks[0]=server;
 let tree=draw();check(!!providerEffect());providerEffect().fn();await new Promise(setImmediate);
 check(calls.filter(x=>x==='/providers').length===2);
 tree=draw();check(!providerEffect());
 // Find the rendered form's worker input and change an unsaved setting.
 const worker=nodes(tree).find(n=>n.type==='input'&&n.props.type==='number');check(!!worker);
 worker.props.onChange({target:{value:'6'}});tree=draw();
 check(nodes(tree).find(n=>n.type==='input'&&n.props.type==='number').props.value===6);
 calls.length=0;
 const refresh=nodes(tree).find(n=>n.type==='button'&&nodes(n).some(c=>c.props?.children==='Refresh'));
 // Its children include text alongside an icon, so match the direct string.
 const refreshButton=refresh||nodes(tree).find(n=>n.type==='button'&&Array.isArray(n.props.children)&&n.props.children.includes('Refresh'));
 check(!!refreshButton);refreshButton.props.onClick();await new Promise(setImmediate);await new Promise(setImmediate);
 for(const route of ['/providers','/companion/autonomy/environments','/companion/autonomy/baseline','/companion/autonomy/baseline/latest','/companion/autonomy/status','/companion/autonomy/cycles'])check(calls.includes(route));
 tree=draw();check(nodes(tree).find(n=>n.type==='input'&&n.props.type==='number').props.value===6);
 check(!calls.some(route=>route.includes('/start')||route.includes('/resume')||route.includes('/preflight')));
 function stagingForm(){return nodes(tree).find(n=>n.type===scope.exports.EnvironmentConnection&&n.props.environment==='staging');}
 const input={base_url:'https://staging3.tonyl36.sg-host.com',role:'write'};
 await stagingForm().props.onSave('staging',input);tree=draw();
 let alert=nodes(tree).find(n=>n.props?.role==='alert');
 check(!!alert&&nodes(alert).some(n=>Array.isArray(n.props?.children)&&n.props.children.some(c=>typeof c==='string'&&c.includes('NOT verified'))));
 check(nodes(tree).some(n=>typeof n.props?.children==='string'&&n.props.children.includes('without retyping'))||nodes(alert).some(n=>Array.isArray(n.props?.children)&&n.props.children.some(c=>typeof c==='string'&&c.includes('without retyping'))));
 connectionResult={configured:true,connected:true};
 await stagingForm().props.onSave('staging',input);tree=draw();
 check(!nodes(tree).some(n=>n.props?.role==='alert'));
 check(nodes(tree).some(n=>n.props?.role==='status'&&typeof n.props.children==='string'&&n.props.children.includes('Staging write connection verified')));
 connectionResult=new Error('Connection request was rejected');
 await stagingForm().props.onSave('staging',input);tree=draw();
 alert=nodes(tree).find(n=>n.props?.role==='alert');check(!!alert);
 check(!JSON.stringify(alert.props.children).includes('Credentials saved'));
 check(!nodes(tree).some(n=>n.props?.role==='status'&&typeof n.props.children==='string'&&n.props.children.includes('connection verified')));
 // A rejected preflight refreshes safe status facts instead of hiding the
 // exact observed provider cap behind the generic409 error.
 const facts={ready:false,preflight_ready:false,reason:'director_isolated_monthly_key_limit_required',observed_limit_usd:'100',observed_reset:'monthly',required_cap_usd:'25.00'};
 server={...server,director:facts};hooks[0]=server;tree=draw();calls.length=0;
 function checkDirector(){return nodes(tree).find(n=>n.type==='button'&&Array.isArray(n.props.children)&&n.props.children.includes('Check director readiness'));}
 check(!!checkDirector()&&!checkDirector().props.disabled);checkDirector().props.onClick();await new Promise(setImmediate);await new Promise(setImmediate);tree=draw();
 let readiness=nodes(tree).find(n=>n.type===scope.exports.DirectorReadinessDetails);
 check(readiness.props.saved.observed_limit_usd==='100');
 check(calls.includes('/companion/autonomy/director/preflight')&&calls.includes('/companion/autonomy/status'));
 // Rotation/current backend unknown status clears the former key's facts.
 server={...server,director:{ready:false,preflight_ready:false,reason:'director_disabled'}};
 checkDirector().props.onClick();await new Promise(setImmediate);await new Promise(setImmediate);tree=draw();
 readiness=nodes(tree).find(n=>n.type===scope.exports.DirectorReadinessDetails);
 check(readiness.props.saved.observed_limit_usd===undefined&&!('observed_limit_usd' in readiness.props.checked));
 // A failed status refresh also clears old observations rather than showing
 // a former key's limit when the current credential cannot be established.
 server={...server,director:facts};hooks[0]=server;statusFails=true;tree=draw();
 checkDirector().props.onClick();await new Promise(setImmediate);await new Promise(setImmediate);tree=draw();
 readiness=nodes(tree).find(n=>n.type===scope.exports.DirectorReadinessDetails);
 check(readiness.props.saved.observed_limit_usd===undefined&&readiness.props.saved.observed_reset===undefined);
 check(!calls.some(route=>route.includes('/start')||route.includes('/resume')||route.includes('/repairs/')));
 process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,external_requests:0}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["assertions"] >= 15
    assert receipt["external_requests"] == 0


def test_saved_baseline_loads_ignore_stale_completions_and_bind_exact_capture():
    if not shutil.which("node") or not (FRONTEND / "node_modules/typescript").is_dir():
        pytest.skip("Dashboard dependencies are required")
    program = r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict');
const req=require('node:module').createRequire(path.join(process.argv[1],'package.json'));
const ts=req('typescript');let checks=0;function check(value){assert.ok(value);checks++;}
const states=[],writes=[],callbacks=[],requests=[];
const hooks={
 useState(initial){const slot=states.length;states.push(typeof initial==='function'?initial():initial);return [states[slot],value=>{states[slot]=typeof value==='function'?value(states[slot]):value;writes.push({slot,value:states[slot]});}];},
 useRef(value){return {current:value};},useEffect(){},useCallback(fn){callbacks.push(fn);return fn;},
};
function deferred(path,options){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no});const value={path,options,resolve,reject,promise};requests.push(value);return promise;}
const api=async(path,options)=>{
 if(path==='/companion/autonomy/status'){requests.push({path,options});return {enabled:false,schedule_enabled:false,baseline:{ready:false}};}
 if(path==='/companion/autonomy/cycles'){requests.push({path,options});return {items:[]};}
 return deferred(path,options);
};
const scope={exports:{},setTimeout,clearTimeout,Intl,Date,require(name){
 if(name==='react')return hooks;
 if(name.endsWith('.css'))return {};
 if(name==='./AutonomousRepairs')return {__esModule:true,default:()=>null};
 if(name==='./api')return {api,count:v=>String(v),errorMessage:e=>e.message,humanize:v=>String(v).replace(/_/g,' ')};
 return req(name);
}};
const source=ts.transpileModule(fs.readFileSync(path.join(process.argv[1],'src/AutonomousCompanion.tsx'),'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
vm.runInNewContext(source,scope);
function nodes(node,result=[]){if(!node||typeof node!=='object')return result;if(Array.isArray(node)){node.forEach(n=>nodes(n,result));return result;}result.push(node);nodes(node.props?.children,result);return result;}
const tree=scope.exports.default();check(requests.length===0); // Rendering does not read or mutate a site.
const load=callbacks.find(fn=>fn.toString().includes('/baseline/latest'));check(typeof load==='function');
const controls=nodes(tree).find(node=>node.type===scope.exports.AutonomyBaselineDetails);check(!!controls);
const flush=()=>new Promise(setImmediate);
const take=(path,index=0)=>requests.filter(request=>request.path===path)[index];
const capture=letter=>({id:letter.repeat(64),manifest_sha256:letter.toUpperCase().repeat(64),captured_at:1720000000});
const latest='/companion/autonomy/baseline/latest',baseline='/companion/autonomy/baseline';
const alignment=id=>'/companion/autonomy/baseline/alignment?capture_id='+encodeURIComponent(id);
(async()=>{
 const old=load(),fresh=load();
 take(baseline,1).resolve({ready:false,reason:'fresh_baseline'});take(latest,1).resolve({capture:capture('b')});
 await flush();check(!!take(alignment(capture('b').id)));check(!requests.some(r=>r.path==='/companion/autonomy/baseline/alignment'));
 take(alignment(capture('b').id)).resolve({capture_id:capture('b').id,capture_manifest_sha256:capture('b').manifest_sha256});
 await fresh;
 const captureSlot=writes.find(write=>write.value?.id===capture('b').id).slot;
 check(states[captureSlot].id===capture('b').id);
 const writesAfterFresh=writes.length;
 take(baseline,0).resolve({ready:false,reason:'stale_baseline'});take(latest,0).resolve({capture:capture('a')});
 await flush();check(!!take(alignment(capture('a').id)));
 take(alignment(capture('a').id)).resolve({capture_id:capture('a').id,capture_manifest_sha256:capture('a').manifest_sha256});
 await old;
 check(states[captureSlot].id===capture('b').id);check(writes.length===writesAfterFresh);
 // A capture POST invalidates a read already in flight before the POST finishes.
 const prior=load();controls.props.onCapture();await flush();
 const post=take('/companion/autonomy/baseline/capture');check(post.options.method==='POST'&&post.options.body==='{}');
 take(baseline,2).resolve({ready:false,reason:'old_before_post'});take(latest,2).resolve({capture:capture('a')});
 await flush();take(alignment(capture('a').id),1).resolve({capture_id:capture('a').id,capture_manifest_sha256:capture('a').manifest_sha256});
 await prior;check(states[captureSlot].id===capture('b').id);
 post.resolve(capture('c'));await flush();check(states[captureSlot].id===capture('c').id);
 take(baseline,3).resolve({ready:false,reason:'captured_baseline'});take(latest,3).resolve({capture:capture('c')});
 await flush();check(!!take(alignment(capture('c').id)));
 take(alignment(capture('c').id)).resolve({capture_id:capture('c').id,capture_manifest_sha256:capture('c').manifest_sha256});
 await flush();await flush();
 check(states[captureSlot].id===capture('c').id);
 check(!writes.some(write=>write.slot===captureSlot&&write.value?.id===capture('a').id));
 check(requests.filter(r=>r.options?.method==='POST').length===1);
 check(!requests.some(r=>/director|supplier|repair|approve|\/start|\/resume/.test(r.path)));
 process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,external_requests:0,source_requests:0}));
})().catch(error=>{console.error(error);process.exit(1)});
'''
    result = subprocess.run(["node", "-e", program, str(FRONTEND)], capture_output=True, text=True, check=True)
    receipt = json.loads(result.stdout)
    assert receipt["status"] == "PASS" and receipt["assertions"] >= 15
    assert receipt["external_requests"] == receipt["source_requests"] == 0
