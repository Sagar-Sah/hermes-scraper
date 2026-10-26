"""Portable native UI readiness regressions; no source requests or private values.

Runs the exact public registry factory and fixed DOM probe in isolated Node and
fresh, completely intercepted Chromium. No private task artifacts are required.
"""
from pathlib import Path
import json,shutil,subprocess,asyncio,hashlib
from copy import deepcopy
import pytest
from test_fourover_automatic_profile import load_driver as load,session_fixture,PRIVATE,ui_ready_state
FIXTURES=Path(__file__).resolve().parent/'fixtures/fourover_native_ui'
@pytest.fixture
def mod():return load()

def ready(mod):
 value={k:False for k in mod.UI_BOOL_KEYS}
 for k in ('modules_proved','registry_defined','tabs_module_defined','project_module_defined','registry_available','tabs_registered','project_registered','tab_domain_observed','selected_tab_in_domain','next_tab_available','current_is_product_options','next_is_shipping','guest_restriction_observed','tabs_loading_observed','project_loading_observed','shipping_active_observed','project_name_invalid_observed'):value[k]=True
 return {'schema':'native-project-ui-readiness/v1',**value,'form_count':1,'visible_error_count':0,'fields':{k:{n:0 for n in mod.UI_COUNTS} for k in mod.UI_FIELDS}}

NODE=r'''
const vm=require('vm'),input=JSON.parse(require('fs').readFileSync(0,'utf8')),f=input.change;
let requireCalls=0,getCalls=0,mutations=0,privateReads=0;const obs=v=>{const fn=()=>{mutations++;throw Error('Observable callback forbidden')};fn.peek=()=>v;return fn};
const context={define:(deps,factory)=>{context.factory=factory},WeakMap,window:{},location:{href:f==='source-drift'?'https://4over.com/other':input.source}};vm.createContext(context);vm.runInContext(input.registry,context);
const registry=context.factory({}, {debounce:fn=>fn});
const selected=f==='unknown-tab'?'unknown':f==='shipping'?'product.shipping':'product.configuration';
const tabs={guestShippingRestricted:f==='guest-unknown'?'true':f==='guest',isLoading:obs(f==='loading'),isProjectNameInvalid:obs(f==='name'),selectedTab:obs(selected),tabsData:[{alias:'product.configuration'},{alias:'product.shipping'}],shippingAndSetsTab:'product.shipping'};
if(f==='duplicate-tab')tabs.tabsData[1].alias='product.configuration';if(f==='no-next')tabs.tabsData=[{alias:'product.configuration'}];if(f==='empty-tab')tabs.tabsData[0].alias='';
const project={isLoading:obs(f==='loading'),isShippingAndSetsActive:obs(f==='shipping')};
for(const obj of [tabs,project])for(const key of ['name','projectName','address','customer'])Object.defineProperty(obj,key,{get(){privateReads++;throw Error('Private source property read')}});
if(f!=='tabs-missing')registry.set('project-tabs',tabs);if(f!=='project-missing')registry.set('project',project);
const originalGet=registry.get;registry.get=function(name){if(arguments.length!==1||!['project-tabs','project'].includes(name))throw Error('Nonfixed/asynchronous registry request');getCalls++;return originalGet.call(this,name)};
const names=['uiRegistry','FourOver_Project/js/view/project-tabs','FourOver_Project/js/view/project'];
function r(name){if(typeof name!=='string'||name!=='uiRegistry')throw Error('Unexpected module read');requireCalls++;return registry}r.defined=name=>names.includes(name)&&!(f==='undefined'&&name==='uiRegistry');context.window.require=r;
const field={getClientRects:()=>[{}],hasAttribute:()=>false,getAttribute:k=>k==='aria-invalid'&&f==='invalid-field'?'true':null,classList:{contains:k=>k==='mage-error'&&f==='invalid-field'},validity:{valid:f!=='invalid-field'&&f!=='name',valueMissing:f==='name'},parentElement:null};
for(const key of ['value','name','textContent'])Object.defineProperty(field,key,{get(){privateReads++;throw Error('Private field value/name/message read')}});
const form={querySelectorAll:q=>q==='#project_name'?(f==='name'?[field]:[]):q==='input,select,textarea'?[field]:q==='.mage-error,[aria-invalid="true"]'?(f==='invalid-field'?[field]:[]):[]};
const step={getAttribute:()=>selected,querySelectorAll:()=>f==='name'?[field]:[]};
context.document={querySelectorAll:q=>q==='#product_addtocart_form'?(f==='no-form'?[]:f==='two-form'?[form,form]:[form]):q==='[data-tab-id]'?[step]:[]};context.getComputedStyle=()=>({visibility:'visible',display:'block'});
const result=vm.runInContext('('+input.probe+')',context)({source:input.source,dom:{},modules_proved:f!=='unproved'});
process.stdout.write(JSON.stringify({result,requireCalls,getCalls,mutations,privateReads}));
'''

def probe(mod,change):
 source=FIXTURES/'Magento_Ui__js__lib__registry__registry.js'
 assert hashlib.sha256(source.read_bytes()).hexdigest()==mod.UI_PUBLIC_MODULES['Magento_Ui/js/lib/registry/registry.js']
 payload={'registry':source.read_text(),'probe':mod.UI_PROBE_JS,'source':'https://4over.com/synthetic-package','change':change}
 proc=subprocess.run([shutil.which('node'),'-e',NODE],input=json.dumps(payload),capture_output=True,text=True,check=True);result=json.loads(proc.stdout)
 assert result['privateReads']==result['mutations']==0 and PRIVATE not in proc.stdout
 return result

@pytest.mark.parametrize('change,expected,requirement',[
 ('normal',True,None),('shipping',True,None),('loading',True,None),('unproved',False,None),('undefined',False,None),('source-drift',False,None),
 ('tabs-missing',False,None),('project-missing',False,None),('unknown-tab',False,None),('duplicate-tab',False,None),('empty-tab',False,None),('no-next',False,None),
 ('no-form',False,None),('two-form',False,None),('guest',False,'source_auth_required'),('guest-unknown',False,None),('name',True,'source_project_details_required'),('invalid-field',True,None)])
def test_exact_native_registry_getter_and_flags_never_read_values_or_call_observables(mod,change,expected,requirement):
 out=probe(mod,change);state={'source_ui':out['result']}
 assert mod.ui_ready(state) is expected and mod.ui_action_requirement(state)==requirement
 assert set(out['result'])=={'schema','form_count','visible_error_count','fields',*mod.UI_BOOL_KEYS}
 assert all(set(v)==set(mod.UI_COUNTS) for v in out['result']['fields'].values())
 if change in {'unproved','undefined','source-drift'}:assert out['requireCalls']==out['getCalls']==0
 else:assert out['requireCalls']==1 and out['getCalls']==2
 if change=='invalid-field':assert out['result']['fields']['other']['native_invalid_visible']==out['result']['fields']['other']['aria_invalid_visible']==1


def test_projection_returns_every_fixed_key_and_never_untrusted_private_fields(mod):
 raw={'source_ui':{'schema':PRIVATE,PRIVATE:PRIVATE,'guest_restricted':PRIVATE,'form_count':True,'fields':{PRIVATE:{PRIVATE:PRIVATE},'quantity':{'count':-1,'visible_count':999999,'native_invalid_visible':True}}}}
 result=mod.ui_diagnostic(raw);assert PRIVATE not in json.dumps(result)
 assert set(result)=={'schema','form_count','visible_error_count','fields',*mod.UI_BOOL_KEYS}
 assert result['form_count']==0 and result['fields']['quantity']['count']==0 and result['fields']['quantity']['visible_count']==256
 assert not mod.ui_ready(raw) and mod.ui_action_requirement(raw) is None


@pytest.mark.asyncio
@pytest.mark.parametrize('change,reason',[('tabs-missing','source_ui_readiness_unproven'),('guest','source_auth_required'),('name','source_project_details_required')])
async def test_native_ui_or_account_block_stops_before_any_quantity_reservation(mod,tmp_path,change,reason):
 session,store,page=session_fixture(mod,tmp_path);original=page.evaluate
 value=ready(mod)
 if change=='tabs-missing':value['tabs_registered']=False
 if change=='guest':value['guest_restricted']=True
 if change=='name':value.update(current_project_name_present=True,project_name_invalid=True);value['fields']['project_name']['visible_count']=1
 async def evaluate(script,arg=None):
  result=await original(script,arg)
  if script.startswith('() =>') and isinstance(result,dict):result['source_ui']=deepcopy(value)
  return result
 page.evaluate=evaluate
 with pytest.raises(mod.Held) as failure:await session.collect()
 assert failure.value.code==reason and page.quantity_clicks==0
 assert session.cursor['inflight'] is None and not (tmp_path/'fourover-native-observation-selections').exists()
 assert session.diagnostic['source_ui']['schema']=='native-project-ui-readiness/v1'
 assert PRIVATE not in json.dumps(session.diagnostic)
 result=session.result(False,reason);assert result['status']==('access_required' if change in {'guest','name'} else 'held')


@pytest.mark.asyncio
async def test_late_native_ui_registration_waits_before_legitimate_quantity_action(mod,tmp_path):
 session,store,page=session_fixture(mod,tmp_path);original=page.evaluate;reads=0
 async def evaluate(script,arg=None):
  nonlocal reads
  result=await original(script,arg)
  if script.startswith('() =>') and isinstance(result,dict):
   value=ready(mod);reads+=1;value['tabs_registered']=reads>=3;result['source_ui']=value
   if reads<3:assert not (tmp_path/'fourover-native-observation-selections').exists()
  return result
 page.evaluate=evaluate
 async def wait(predicate,seconds=35):
  for _ in range(8):
   value=await session.state()
   if predicate(value):return value
   await asyncio.sleep(0)
  raise mod.Held('source_context_timeout')
 session.wait=wait
 assert await session.collect() is True and session.cursor['verified_count']==1 and page.quantity_clicks==1


def test_exact_public_ui_fixture_provenance_matches_required_module_proofs(mod):
 assert ui_ready_state()==ready(mod) and mod.ui_ready({'source_ui':ui_ready_state()})
 manifest=json.loads((FIXTURES/'manifest.json').read_text())
 assert {r['public_asset_suffix']:r['sha256'] for r in manifest['modules']}==mod.UI_PUBLIC_MODULES
 for row in manifest['modules']:
  raw=(FIXTURES/row['path']).read_bytes()
  assert len(raw)==row['bytes'] and hashlib.sha256(raw).hexdigest()==row['sha256']
  assert row['public_url'].startswith('https://4over.com/static/version') and row['public_url'].endswith('/'+row['public_asset_suffix'])

@pytest.mark.asyncio
async def test_real_chromium_probe_reads_fixed_form_flags_not_values_or_callbacks(mod):
 from playwright.async_api import async_playwright
 source='https://4over.com/synthetic-ui-probe';requests=[]
 registry=(FIXTURES/'Magento_Ui__js__lib__registry__registry.js').read_text()
 document='''<!doctype html><html><body><form id="product_addtocart_form"><div data-tab-id="product.configuration"><input id="project_name" name="private-name" required><input name="other-private" value="SYNTHETIC-PRIVATE"><select name="super_attribute[111]"><option value="10">A</option></select></div></form><script>
 window.define=(deps,factory)=>{window.registryFactory=factory};</script><script>REGISTRY</script><script>
 const registry=window.registryFactory({}, {debounce:fn=>fn}),obs=value=>{const fn=()=>{throw Error('source callback invoked')};fn.peek=()=>value;return fn};
 const tabs={guestShippingRestricted:false,isLoading:obs(false),isProjectNameInvalid:obs(true),selectedTab:obs('product.configuration'),tabsData:[{alias:'product.configuration'},{alias:'product.shipping'}],shippingAndSetsTab:'product.shipping'};
 registry.set('project-tabs',tabs);registry.set('project',{isLoading:obs(false),isShippingAndSetsActive:obs(false)});
 window.require=name=>{if(name!=='uiRegistry')throw Error('wrong module');return registry};window.require.defined=name=>['uiRegistry','FourOver_Project/js/view/project-tabs','FourOver_Project/js/view/project'].includes(name);
 for(const field of document.querySelectorAll('input,select'))for(const key of ['value','name','textContent'])Object.defineProperty(field,key,{get(){throw Error('Private field read')}});
 </script></body></html>'''.replace('REGISTRY',registry)
 async with async_playwright() as runtime:
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page()
  async def serve(route):
   requests.append(route.request.url)
   if route.request.url==source:await route.fulfill(status=200,content_type='text/html',body=document)
   else:await route.abort();raise AssertionError('Unexpected fixture network')
  await context.route('**/*',serve)
  try:
   await page.goto(source);raw=await page.evaluate(mod.UI_PROBE_JS,{'source':source,'dom':{},'modules_proved':True})
   assert mod.ui_ready({'source_ui':raw}) and mod.ui_action_requirement({'source_ui':raw})=='source_project_details_required'
   assert raw['fields']['project_name']['value_missing_visible']==1 and raw['fields']['attributes']['count']==1
   assert raw['fields']['other']['count']==1 and requests==[source]
   assert 'SYNTHETIC-PRIVATE' not in json.dumps(raw) and 'private-name' not in json.dumps(raw)
  finally:await context.close();await browser.close()


def test_boolean_form_count_is_not_a_unique_native_form(mod):
    raw=ready(mod);raw['form_count']=True
    assert not mod.ui_ready({'source_ui':raw})
