"""Actual timing helpers/shared clock/React output in Node; no browser/API/worker."""
import json
from pathlib import Path
import shutil
import subprocess
import pytest

FRONTEND=Path(__file__).resolve().parents[1]


def test_duration_truth_live_caps_shared_timer_and_accessible_output():
    if not shutil.which('node') or not (FRONTEND/'node_modules/typescript').is_dir():
        pytest.skip('Install dashboard dependencies for TypeScript tests')
    code=r'''
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm'),assert=require('node:assert/strict'),{createRequire}=require('node:module');
const root=process.argv[1],local=createRequire(path.join(root,'package.json')),ts=local('typescript'),React=local('react'),render=local('react-dom/server').renderToStaticMarkup;
let mono=59000,checks=0;const check=v=>{assert.ok(v);checks++};const cache=new Map();
function load(name){
 if(cache.has(name))return cache.get(name);
 const file=path.join(root,'src',name),exports={};cache.set(name,exports);
 const text=ts.transpileModule(fs.readFileSync(file,'utf8'),{compilerOptions:{module:ts.ModuleKind.CommonJS,target:ts.ScriptTarget.ES2022,jsx:ts.JsxEmit.ReactJSX}}).outputText;
 const req=id=>id.endsWith('.css')?{}:id==='./productTimingState'?load('productTimingState.ts'):id==='./timingClock'?load('timingClock.ts'):local(id);
 vm.runInNewContext(text,{exports,require:req,performance:{now:()=>mono},setInterval,clearInterval},{filename:file});return exports;
}
const {timingView,canTickTiming,durationText,measuredSeconds}=load('productTimingState.ts');
const base={schema_version:1,state:'active',active_seconds:3600,queued_seconds:120,waiting_seconds:10,paused_seconds:300,attention_seconds:30,elapsed_seconds:7200,observed_since:1000,server_now:2000,active_since:1500,live_until:2005,active_now:true,complete_history:true,coverage:'complete',clock_anomaly:false};
check(durationText(3600)==='1h 00m');check(durationText(3661)==='1h 01m');check(durationText(61)==='1m 01s');check(durationText(0)==='0s');
for(const value of [null,undefined,true,-1,NaN,Infinity,1e20,'3600']){check(!measuredSeconds(value));check(durationText(value)==='Not measured');}
let v=timingView(base,60000,59000);check(v.active===3601&&v.delta===1&&v.elapsed===7200); // Server sample already includes active_since→server_now.
check(v.label==='1h 00m active'&&v.caption==='Collecting · 2h 00m elapsed');
v=timingView(base,100000000,59000);check(v.active===3605&&v.delta===5&&v.heartbeatExpired);check(v.caption.includes('Awaiting worker update'));
v=timingView(base,58000,59000);check(v.active===3600&&v.delta===0);
v=timingView(base,100000000,59000,false);check(v.active===3600&&v.caption.startsWith('Last known'));
for(const state of ['queued','waiting','paused','attention','finished','unknown']){const t={...base,state};check(!canTickTiming(t));check(timingView(t,1000000,0).active===3600);}
v=timingView({...base,state:'queued',active_seconds:0,queued_seconds:3600},60000,59000);check(v.label==='0s active'&&v.caption.startsWith('Queued'));check(v.details.find(x=>x[0]==='Queued')[1]==='1h 00m');
v=timingView({...base,coverage:'partial',complete_history:false,active_seconds:125},60000,59000);check(v.label==='At least 2m 06s active');check(v.details.find(x=>x[0]==='Paused')[1].startsWith('At least '));
v=timingView({...base,coverage:'unknown',complete_history:false},60000,59000);check(v.active===null&&v.label==='Active time not measured'&&v.elapsed===7200);check(v.details[1][1]==='Not measured');
v=timingView({...base,schema_version:2},60000,59000);check(v.active===null&&v.elapsed===null);
v=timingView({...base,clock_anomaly:true},1000000,0);check(v.active===3600&&v.warning.includes('clock change'));
for(const t of [{...base,active_since:2001},{...base,live_until:1999},{...base,server_now:NaN},{...base,active_seconds:null}])check(!canTickTiming(t));
// Changing the device wall clock does not enter the timing computation.
const before=timingView(base,60000,59000).active;const old=Date.now;Date.now=()=>9e14;check(timingView(base,60000,59000).active===before);Date.now=old;
//99 active products share one actual ticker, and the last unsubscribe stops it.
const {createTimingClock}=load('timingClock.ts');let created=0,cancelled=0,pulse,notifications=0;
const clock=createTimingClock(()=>mono,(fn,delay)=>{check(delay===1000);created++;pulse=fn;return 7;},id=>{check(id===7);cancelled++;});
const remove=Array.from({length:99},()=>clock.subscribe(()=>notifications++));check(created===1&&cancelled===0);mono=60000;pulse();check(notifications===99&&clock.snapshot()===60000);
for(const fn of remove.slice(0,98))fn();check(cancelled===0);remove[98]();check(cancelled===1);
const last=clock.subscribe(()=>{});check(created===2);last();check(cancelled===2);
// React output exposes keyboard-native expandable details; no ticking live region.
const Component=load('ProductTiming.tsx').default;
let html=render(React.createElement(Component,{timing:base,label:'Acrylic collection time'}));
check(html.includes('<details')&&html.includes('<summary')&&html.includes('1h 00m active'));
check(html.includes('Acrylic collection time')&&html.includes('Elapsed wall time')&&html.includes('supplier waits'));
check(!html.includes('aria-live')&&!html.includes('scraping for'));
html=render(React.createElement(Component,{timing:{...base,coverage:'unknown',complete_history:false}}));check(html.includes('Active time not measured')&&html.includes('2h 00m elapsed'));
check(!html.includes('2h 00m active'));
process.stdout.write(JSON.stringify({status:'PASS',assertions:checks,rows_shared:99,network_requests:0}));
'''
    result=subprocess.run(['node','-e',code,str(FRONTEND)],text=True,capture_output=True,check=True)
    value=json.loads(result.stdout)
    assert value['status']=='PASS' and value['assertions']>=60 and value['network_requests']==0
