"""Portable actual-source controller/readiness tests; no supplier or saved profiles.

Uses the normal UUID-isolated loader and optional HERMES_FOUROVER_TEST_OVERLAY.
Fixture sources are public supplier JavaScript, never private account receipts.
"""
import asyncio,json,subprocess,shutil,time
from pathlib import Path
import pytest
from test_fourover_automatic_profile import load_driver as load,PRIVATE,session_fixture,state,quote_form,SOURCE,SELECTION
from copy import deepcopy
from urllib.parse import urlencode
FIXTURES=Path(__file__).resolve().parent/'fixtures/fourover_native_controller'

@pytest.fixture
def mod():return load()

@pytest.fixture
def driver():return load()

NODE=r'''
const vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8'));
const registry={},calls=[];let mutators=0;
function obs(value){function o(next){if(arguments.length){mutators++;throw Error('Probe mutated source')}return value}o.peek=()=>value;return o}
const changes=input.change,qty=changes==='qty-string'?'250':250,child=changes==='child-object'?{toString(){throw Error('Coerced unsupported object')}}:changes==='child-drift'?'701':'700';
const packages=changes==='rows-object'?{}:changes==='duplicate-row'?[{qty:250,price:10},{qty:250,price:12}]:[{qty:changes==='row-string'?'250':250,price:changes==='zero-price'?0:10}];
const modules={'FourOver_Project/js/model/product-selected-id':obs(child),'FourOver_Project/js/model/selected-runsize':obs(qty),'FourOver_Project/js/model/product-package-prices':obs(packages),'FourOver_TurnaroundTime/js/model/product/turnaround-time':obs(changes==='tat-drift'?'9':'3'),'FourOver_TurnaroundTime/js/view/product/turnaround-time':{}};
const _={isFunction:v=>typeof v==='function',findWhere:(rows,query)=>rows.find(row=>Object.entries(query).every(([key,value])=>row[key]===value))};
const context={define:(deps,factory)=>{context.factory=factory},Number,Array,Object,String,window:{},location:{href:changes==='source-drift'?'https://4over.com/other':input.source},document:{querySelectorAll:selector=>selector.startsWith('input')?[{checked:true,id:'tat3',getClientRects:()=>changes==='hidden-input'?[]:[{}],closest:()=>null}]:[{htmlFor:'tat3',getClientRects:()=>changes==='hidden-label'?[]:[{}]}]},getComputedStyle:()=>({visibility:'visible'})};
vm.createContext(context);vm.runInContext(input.processor,context);modules['FourOver_Project/js/model/product-price-processor']=context.factory(_,()=>{}, {},()=>({}),{});
vm.runInContext(input.calculator,context);modules['FourOver_ProductRunSize/js/model/price-processor']=context.factory(_,modules['FourOver_Project/js/model/product-package-prices']);
vm.runInContext(input.register,context);
if(changes!=='unregistered'){context.factory({},modules['FourOver_Project/js/model/product-price-processor'],modules['FourOver_ProductRunSize/js/model/price-processor']);modules['FourOver_ProductRunSize/js/view/register-price-processor']={};}
function r(name){if(typeof name!=='string')throw Error('Async require forbidden');calls.push(name);if(!Object.hasOwn(modules,name))throw Error('Undefined require');return modules[name]}
r.defined=name=>Object.hasOwn(modules,name);context.window.require=r;
const dom={child_id:'700',run_size:'250',production:{value:'3',visible:changes!=='hidden-input'}};
const result=vm.runInContext('('+input.probe+')',context)({source:input.source,dom,modules_proved:changes!=='unproved'});
process.stdout.write(JSON.stringify({result,mutators,require_calls:calls.length}));
'''

def probe(mod,change='none'):
    by_name={suffix.removesuffix('.js'):suffix for suffix in mod.CONTROLLER_PUBLIC_MODULES}
    raw=lambda name:(FIXTURES/by_name[name].replace('/','__')).read_text()
    payload={'probe':mod.CONTROLLER_PROBE_JS,'source':'https://4over.com/16pt-postcards','change':change,
      'processor':raw('FourOver_Project/js/model/product-price-processor'),'calculator':raw('FourOver_ProductRunSize/js/model/price-processor'),'register':raw('FourOver_ProductRunSize/js/view/register-price-processor')}
    p=subprocess.run([shutil.which('node'),'-e',NODE],input=json.dumps(payload),capture_output=True,text=True,check=True)
    result=json.loads(p.stdout);assert result['mutators']==0 and PRIVATE not in p.stdout
    return result

@pytest.mark.parametrize('change',['none','unregistered','unproved','source-drift','qty-string','child-object','child-drift','rows-object','duplicate-row','row-string','zero-price','tat-drift','hidden-input','hidden-label'])
def test_exact_native_calculator_chain_readiness_without_source_callbacks(mod,change):
    value=probe(mod,change);state={'source_controller':value['result']}
    assert mod.controller_ready(state) is (change in {'none','tat-drift','hidden-input','hidden-label'})
    assert mod.controller_ready(state,tat=True) is (change in {'none','hidden-label'})
    if change in {'unregistered','unproved','source-drift'}:assert value['require_calls']==0
    assert all(type(v) is bool or k=='schema' for k,v in value['result'].items())


def test_readiness_diagnostic_cannot_leak_unknown_keys_or_scalar_values(mod):
    value=mod.controller_diagnostic({'source_controller':{'schema':PRIVATE,PRIVATE:True,'available':True,'ready':PRIVATE,'private_price':123}})
    assert value=={'schema':'native-controller-readiness/v1','available':True}
    assert not mod.controller_ready({'source_controller':value}) and PRIVATE not in json.dumps(value)


PRODUCTION_NODE=r'''
const vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8')),f=input.change;
const base={getClientRects(){return this.hidden?[]:[{}]},getAttribute(){return null},hasAttribute(){return false},closest(){return null},parentElement:null};
const field=Object.assign({},base,{id:'tat3',value:'3',checked:f!=='unchecked',disabled:f==='disabled',hidden:f!=='visible-input'});
const parent=Object.assign({},base,{hidden:f==='hidden-ancestor',display:f==='display-none-ancestor'?'none':'block',visibility:f==='hidden-visibility-ancestor'?'hidden':'visible'});
const label=Object.assign({},base,{htmlFor:f==='foreign-for'?'other':'tat3',control:f==='foreign-control'?{}:field,textContent:'3 Business Days',hidden:f==='both-hidden',parentElement:parent});
const labels=f==='ambiguous'?[label,Object.assign({},label)]:[label];
const root={contains:e=>f!=='foreign-root'&&labels.includes(e),querySelectorAll:q=>q==='input[name="turnaround_time"]'?[field]:[],querySelector:()=>null};
const document={querySelector:q=>q==='.column.main'?root:q.startsWith('label[')?label:null,querySelectorAll:q=>q==='label'?labels:q==='[id]'?(f==='duplicate-id'?[field,{id:'tat3'}]:[field]):[]};
const context={document,window:{},getComputedStyle:e=>({visibility:e.visibility||'visible',display:e.display||'block'}),CSS:{escape:v=>v}};
const state=vm.runInNewContext('('+input.code+')',context)();process.stdout.write(JSON.stringify(state.production));
'''

@pytest.mark.parametrize('change,expected',[('hidden-input',True),('visible-input',True),('both-hidden',False),('foreign-for',False),('foreign-control',False),('foreign-root',False),('ambiguous',False),('duplicate-id',False),('hidden-ancestor',False),('display-none-ancestor',False),('hidden-visibility-ancestor',False),('unchecked',None),('disabled',None)])
def test_production_visibility_measures_only_unique_actual_dom_association(mod,change,expected):
    proc=subprocess.run([shutil.which('node'),'-e',PRODUCTION_NODE],input=json.dumps({'code':mod.automatic_state_js({'111':'10'}),'change':change}),text=True,capture_output=True,check=True)
    result=json.loads(proc.stdout)
    if expected is None:assert result is None
    else:assert result['visible'] is expected and result['value']=='3' and result['label']=='3 Business Days'


def scheduling_session(mod,tmp_path,*,limit=4,only_default=False,registered=True):
    """Synthetic quote-boundary session; native module logic is tested above."""
    s,store,old_page=session_fixture(mod,tmp_path);s.limit=limit
    quantities=[250] if only_default else [250,1000,2000]
    s.catalog['entries'][0]['historical_prices']={q:'8.00' for q in quantities}
    s.catalog['declared_domain_count']=len(quantities)
    s.tables['700']['rows']=[{'quantity':q,'price':'10.006'} for q in quantities]
    current=state();current.update(run_size='250',run_size_values=['250'],available_run_sizes=[str(q) for q in quantities])
    current['source_controller']['tat_ready']=False;current['production']['visible']=False
    current['source_controller']['registration_defined']=registered
    class Page:
        url=SOURCE;main_frame=object();actions=[]
        def locator(self,name):return old_page.locator(name)
        async def evaluate(self,script,arg=None):
            if script==mod.ATTRIBUTE_DIAGNOSTIC_JS:return {'selected_matches':True,'expected_choice_enabled':True}
            if script.startswith('() =>'):
                if s.selection is not None and s.cursor['inflight'] is None:assert s.stage=='child'
                return deepcopy(current)
            if script.startswith('({quantity,source})'):
                assert s.stage=='quantity' and s.cursor['inflight'] is not None
                assert mod.controller_ready(current)
                q=arg['quantity'];self.actions.append(q);current['run_size']=str(q);current['run_size_values']=[str(q)]
                current['production']['visible']=True;current['source_controller']['tat_ready']=True
                native_form=quote_form();native_form['run_size']=str(q)
                projection=mod.automatic_request_context(urlencode(native_form),current,selection=s.selection)
                s.summary={'epoch':s.epoch,'totals':mod.parse_summary(b'{"totals":{"subtotal":10.01,"grand_total":11.26,"small_qty_fee":1.25}}'),
                  'request_context':projection,'response_sha256':'f'*64,'observed_at':'synthetic-current'}
                return 1
            raise AssertionError('Unexpected native operation')
    page=Page();s.page=page
    return s,page,store


@pytest.mark.asyncio
async def test_default_noop_returns_immediately_after_first_real_distinct_quote(driver,tmp_path):
    s,page,store=scheduling_session(driver,tmp_path)
    assert await s.collect() is True and page.actions==[1000,250,2000]
    assert s.cursor['verified_count']==3 and s.cursor['inflight'] is None
    assert PRIVATE not in json.dumps(s.cursor)


@pytest.mark.asyncio
async def test_two_slots_collect_distinct_quote_then_default_before_yield(driver,tmp_path):
    s,page,_=scheduling_session(driver,tmp_path,limit=2)
    assert await s.collect() is False and page.actions==[1000,250]
    assert s.cursor['verified_count']==2


@pytest.mark.asyncio
@pytest.mark.parametrize('mode,reason',[('max-one','source_current_quantity_requires_distinct_quote_capacity'),('only-default','source_current_quantity_has_no_unspent_native_change'),('spent-others','source_current_quantity_has_no_unspent_native_change'),('unregistered','source_context_timeout')])
async def test_no_dummy_quote_or_reservation_when_default_cannot_initialize(driver,tmp_path,mode,reason):
    s,page,store=scheduling_session(driver,tmp_path,limit=1 if mode=='max-one' else 4,only_default=mode=='only-default',registered=mode!='unregistered')
    if mode=='spent-others':
        for q in [1000,2000]:assert driver.reserve_once(store.root,SOURCE,{**SELECTION,'run_size':q},'d'*64)
    with pytest.raises(driver.Held) as failure:await s.collect()
    assert failure.value.code==reason and not page.actions
    assert s.cursor['inflight'] is None and s.cursor['processed_count']==s.cursor['verified_count']==0
    assert not driver.spent(store.root,SOURCE,'700',250)


@pytest.mark.asyncio
async def test_insufficient_last_chunk_slot_yields_before_spending_current(driver,tmp_path):
    s,page,store=scheduling_session(driver,tmp_path);s.receipts_this_call=3
    assert await s.collect() is False and not page.actions
    assert not driver.spent(store.root,SOURCE,'700',250)
