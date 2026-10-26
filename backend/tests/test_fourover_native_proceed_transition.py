"""Prospective native Proceed handoff; local fulfilled Chromium only."""
import asyncio,json,hashlib,sys,time,re,shutil,subprocess
from copy import deepcopy
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
import pytest
from test_fourover_automatic_profile import load_driver,session_fixture,SELECTION,SOURCE,PRIVATE
F=Path(__file__).resolve().parent/'fixtures/fourover_native_proceed'

def method(source,name):
 start=source.index(name+': function (')+len(name)+2
 begin=source.index('{',start);depth=1;pos=begin+1;quote=None;escape=False
 while depth:
  c=source[pos]
  if quote:
   if escape:escape=False
   elif c=='\\':escape=True
   elif c==quote:quote=None
  elif c in '\"\'`':quote=c
  elif c=='{':depth+=1
  elif c=='}':depth-=1
  pos+=1
 return source[start:pos]

def test_exact_saved_native_methods_write_TAT_advance_step_and_request_source_totals():
 manifest=json.loads((F/'manifest.json').read_text())
 for name,h in manifest['files'].items():assert hashlib.sha256((F/name).read_bytes()).hexdigest()==h
 tabs=(F/'FourOver_Project__js__view__project-tabs.js').read_text();summary=(F/'FourOver_Project__js__view__project__summary.js').read_text()
 assert "$('.proceed-to-shipping').on('click'" in tabs
 assert 'this.validateTurnAroundTimeValue();\n                this.setNextActiveTab();' in tabs
 assert 'this.isShippingAndSetsActive.subscribe' in summary and 'this.refreshTotals();' in summary
 project=(F/'FourOver_Project__js__view__project.js').read_text();builder=(F/'FourOver_Project__js__model__project-builder.js').read_text()
 assert 'return this.shippingAndSetsTab === this.selectedTab();' in project
 assert 'product_options: project().productOptions()' in builder and 'run_size: project().runSize()' in builder
 methods={n:method(tabs,n) for n in ['validateTurnAroundTimeValue','setNextActiveTab','getNextTabIndex','setActiveTab','validateProjectName']}
 methods['requestTotals']=method(summary,'requestTotals')
 node=r'''
 const input=JSON.parse(require('fs').readFileSync(0,'utf8')),vm=require('vm');
 const obs=v=>{const f=(...a)=>a.length?(v=a[0]):v;f.valueHasMutated=()=>{};return f};
 const options={},model={state:'default',productOptions:()=>options,name:()=> 'Hermes source quote'};
 const project=()=>model,events=[],context={project,productOptionsValidator:{isValid:()=>true},isProjectInCart:()=>false,
  _:{map:(rows,fn)=>rows.map(fn)},$:(selector)=>selector==='input[name="turnaround_time"]:checked'?{length:1,val:()=> '10'}:{find:()=>({length:1})},
  postRequest:(url,data)=>{events.push({url,keys:Object.keys(data)});return {done(fn){fn({product_options:[],totals:{subtotal:10,grand_total:10}});return this},always(fn){fn();return this}}},
  projectManager:{getProjectData:()=>({product_id:900,run_size:250,product_options:options})},projectRewardPoints:()=>{}};
 vm.createContext(context);const methods=Object.fromEntries(Object.entries(input).map(([k,v])=>[k,vm.runInContext('('+v+')',context)]));
 const tabs={tabsData:[{alias:'product.configuration'},{alias:'product.shipping'}],selectedTab:obs('product.configuration'),productOptionsTab:'product.configuration',
  isGuestRestrictedTab:()=>false,getProductOptionsTab:()=> 'product.configuration',isProjectNameInvalid:obs(false),...methods};
 tabs.goToTab=alias=>tabs.setActiveTab(alias);tabs.validateTurnAroundTimeValue();tabs.setNextActiveTab();
 if(options.turnaround_time!=='10'||tabs.selectedTab()!=='product.shipping'||tabs.isProjectNameInvalid()!==false)throw Error('Native transition mismatch');
 const totals=obs({});totals.valueHasMutated=()=>{};const opts=obs({});opts.valueHasMutated=()=>{};
 methods.requestTotals.call({isLoading:obs(false),getProjectKey:()=> 'exact',getProjectOptionsKey:()=> 'opts',summaryOptions:opts,projectTotals:totals,projectSummaryUrl:'/print/project/summary/'});
 if(events.length!==1||events[0].url!=='/print/project/summary/'||totals().exact.subtotal!==10)throw Error('Native quote mechanism mismatch');
 process.stdout.write(JSON.stringify({transition:true,summary:true,requests:events.length}));
 '''
 out=subprocess.run([shutil.which('node'),'-e',node],input=json.dumps(methods),text=True,capture_output=True,check=True)
 assert json.loads(out.stdout)=={'transition':True,'summary':True,'requests':1}

FORM={'uuid':PRIVATE,'product_id':'900','run_size':'250','custom_run_size':'0',
 'product_options[super_attribute][111]':'10','product_options[super_attribute][999]':'20',
 'product_options[turnaround_time]':'10','product_options[job_sample]':'false','product_options[digital_proof]':'false',
 'shipments[0][uuid]':PRIVATE,'shipments[0][jobs][0][uuid]':PRIVATE}
HTML='''<!doctype html><main class="column main"><form id="product_addtocart_form" action="/checkout/cart/add">
 <div data-tab-id="product.configuration" id="options">
 <select name="super_attribute[111]"><option value="10">A</option><option value="11">B</option></select>
 <select name="super_attribute[999]"><option value="20">C</option></select>
 <input name="product" value="900" type="hidden"><input name="selected_configurable_option" value="700" type="hidden">
 <input name="job_sample" type="checkbox"><input name="digital_proof" type="checkbox">
 <ul><li class="runsizes_row selected-qty"><span class="runsize">250</span></li></ul>
 <input id="tat" name="turnaround_time" value="10" type="radio" checked><label for="tat">2-4 Business Days</label>
 <button id="proceed" type="button">PROCEED TO SHIPPING</button></div>
 <div data-tab-id="product.shipping" id="shipping" hidden><span id="subtotal"></span><span id="total"></span></div>
 </form></main><script>
 const obs=initial=>{let value=initial;const f=(...args)=>args.length?(value=args[0]):value;f.peek=()=>value;return f;};
 const data={state:'default',productId:'900',runSize:obs(250),customRunSize:obs(0),
  productOptions:obs({super_attribute:{111:'10',999:'20'},job_sample:false,digital_proof:false})};
 window.privateReads=0;for(const key of ['uuid','shipments','name'])Object.defineProperty(data,key,{get(){window.privateReads++;throw Error('Private read');}});
 const tabs={guestShippingRestricted:false,selectedTab:obs('product.configuration'),tabsData:[{alias:'product.configuration'},{alias:'product.shipping'}],
  shippingAndSetsTab:'product.shipping',isLoading:obs(false),isProjectNameInvalid:obs(false)};
 const projectView={isLoading:obs(false),isShippingAndSetsActive:Object.assign(()=>tabs.selectedTab.peek()==='product.shipping',{peek:()=>tabs.selectedTab.peek()==='product.shipping'})};
 const model=obs(data),selected=obs('700'),quantity=obs(250),rows=obs([{qty:250,price:10.006}]),tat=obs('10');
 const modules={'uiRegistry':{get:key=>({'project-tabs':tabs,project:projectView})[key]},'FourOver_Project/js/model/project':model,
  'FourOver_Project/js/model/product-selected-id':selected,'FourOver_Project/js/model/selected-runsize':quantity,
  'FourOver_Project/js/model/product-package-prices':rows,'FourOver_TurnaroundTime/js/model/product/turnaround-time':tat,
  'FourOver_Project/js/model/product-price-processor':{getUnitPrice:q=>10.006/q,getItemTotalPrice:q=>10.006}};
 function req(key){return modules[key]||{}};req.defined=()=>true;window.require=req;
 window.fixture={data,tabs,projectView,model,selected,quantity,rows,tat,modules,clicks:0,requests:0};
 document.querySelector('#proceed').addEventListener('click',async()=>{
  fixture.clicks++;data.productOptions.peek().turnaround_time=document.querySelector('#tat').value;
  tabs.selectedTab('product.shipping');document.querySelector('#options').hidden=true;document.querySelector('#shipping').hidden=false;
  const body=new URLSearchParams(FORM);fixture.requests++;
  const response=await new Promise(resolve=>{const x=new XMLHttpRequest();x.open('POST','/print/project/summary/');x.setRequestHeader('Content-Type','application/x-www-form-urlencoded');x.onload=()=>resolve(x);x.onerror=()=>resolve(x);x.send(body.toString());});
  if(response.status===200){const v=JSON.parse(response.responseText);document.querySelector('#subtotal').textContent='Subtotal: $'+v.totals.subtotal;
   document.querySelector('#total').textContent='Total: $'+v.totals.grand_total;fixture.finished=true;}
 });
</script>'''.replace('FORM',json.dumps(FORM))

@asynccontextmanager
async def browser_fixture(driver):
 from playwright.async_api import async_playwright
 async with async_playwright() as p:
  browser=await p.chromium.launch(headless=True)
  page=await browser.new_page();seen=[]
  async def intercept(route):
   seen.append(route.request.url)
   if route.request.url==SOURCE:return await route.fulfill(status=200,content_type='text/html',body=HTML)
   if route.request.url=='https://4over.com/print/project/summary/':return await route.fulfill(status=200,content_type='application/json',body=json.dumps({'totals':{'subtotal':'10.01','grand_total':'11.26','small_qty_fee':'1.25'}}))
   await route.abort()
  await page.route('**/*',intercept);await page.goto(SOURCE)
  try:yield page,seen
  finally:await browser.close()

def bind(driver,s,page):
 s.page=page;s.stage='quantity';s.selection=deepcopy(SELECTION);s.epoch=3
 s.quote_failure=None
 s.controller_modules=set(driver.CONTROLLER_PUBLIC_MODULES);s.ui_modules=set(driver.UI_PUBLIC_MODULES);s.name_modules=set(driver.NAME_PUBLIC_MODULES)
 s.deadline=time.monotonic()+20
 return s.selection

@pytest.mark.asyncio
async def test_actual_native_shipping_handoff_keeps_old_strict_guard_and_new_exact_context(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);before=await s.state();assert m.context_retained(before,selection=selection) and m.controller_ready(before,tat=True)
  result=await s.ordinary_quote_action(selection);assert result['status']=='clicked' and s.proceed_receipt is not None
  await page.wait_for_function('fixture.finished===true');after=await s.state()
  assert after['production']['visible'] is False and not m.context_retained(after,selection=selection)
  with pytest.raises(m.Held):m.automatic_request_context(urlencode(FORM),after,selection=selection)
  retained=s.quote_context_state(after,selection);projection=s.quote_request_context(urlencode(FORM),after,selection)
  assert projection['production']=={'value':'10','label':'2-4 Business Days'} and projection['set_binding']=='one_native_job'
  assert after['production']['visible'] is False and all(v['visible'] is False for v in after['attributes'].values())
  assert await page.evaluate('fixture.clicks')==await page.evaluate('fixture.requests')==1 and await page.evaluate('privateReads')==0
  assert seen==[SOURCE,'https://4over.com/print/project/summary/'] and PRIVATE not in json.dumps(s.proceed_receipt)
  for key,value in [('product_id','901'),('product_id','700'),('run_size','251'),('product_options[super_attribute][111]','11'),
      ('product_options[turnaround_time]','11'),('product_options[digital_proof]','true'),
      ('shipments[0][shipping_method]','paid'),('unexpected_action','anything')]:
   wrong={**FORM,key:value}
   with pytest.raises(m.Held):s.quote_request_context(urlencode(wrong),after,selection)
  with pytest.raises(m.Held):m.automatic_request_context(urlencode(FORM)+'&run_size=250',retained,selection=selection)
  await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.asyncio
@pytest.mark.parametrize('drift',[
 'model_identity','tabs_identity','view_identity','control_identity','attribute','attribute_disabled','service','quantity','production','production_label','product_id','model_options','model_quantity',
 'model_state','model_services','step','domain','loading','project_invalid','guest','epoch','generation','missing_receipt','identity_handle'])
async def test_shipping_requires_same_native_identity_exact_step_selection_and_epoch(tmp_path,drift):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);assert (await s.ordinary_quote_action(selection))['status']=='clicked'
  await page.wait_for_function('fixture.finished===true')
  js={
   'model_identity':'fixture.model({...fixture.data})','tabs_identity':"fixture.modules.uiRegistry={get:key=>key==='project-tabs'?{...fixture.tabs}:fixture.projectView}",
   'view_identity':"fixture.modules.uiRegistry={get:key=>key==='project'?{...fixture.projectView}:fixture.tabs}",
   'control_identity':"const e=document.querySelector('select');e.replaceWith(e.cloneNode(true))",
   'attribute':"document.querySelector('select').value='11'",'attribute_disabled':"document.querySelector('select').disabled=true",
   'service':"document.querySelector('[name=job_sample]').checked=true",'quantity':"document.querySelector('.runsize').textContent='251'",
   'production':"document.querySelector('#tat').value='11'",'production_label':"document.querySelector('[for=tat]').textContent='7 Business Days'",
   'product_id':"fixture.data.productId='901'",'model_options':"fixture.data.productOptions.peek().super_attribute[111]='11'",'model_quantity':"fixture.data.runSize(251)",
   'model_state':"fixture.data.state='cart'",'model_services':"fixture.data.productOptions.peek().digital_proof=true",
   'step':"fixture.tabs.selectedTab('product.configuration')",'domain':"fixture.tabs.tabsData.push({alias:'product.addresses'})",
   'loading':"fixture.tabs.isLoading(true)",'project_invalid':"fixture.tabs.isProjectNameInvalid(true)",'guest':"fixture.tabs.guestShippingRestricted=true"}
  if drift in js:await page.evaluate(js[drift])
  if drift=='epoch':s.epoch+=1
  if drift=='generation':s.session_id='different'
  if drift=='missing_receipt':s.proceed_receipt=None
  if drift=='identity_handle':await s.proceed_identity.dispose();s.proceed_identity=None
  try:
   state=await s.state()
   if drift in {'missing_receipt','identity_handle'}:
    with pytest.raises(m.Held):m.automatic_request_context(urlencode(FORM),s.quote_context_state(state,selection),selection=selection)
   else:
    with pytest.raises(m.Held):s.quote_context_state(state,selection)
  except m.Held:
   assert drift in {'guest','identity_handle'}
  if s.proceed_identity is not None:await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.asyncio
async def test_real_guard_captures_exact_native_POST_once_after_handoff_without_external_network(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);forwarded=[]
  class Response:
   status=200;url='https://4over.com/print/project/summary/';headers={'content-type':'application/json'}
   async def body(self):return b'{"totals":{"subtotal":"10.01","grand_total":"11.26","small_qty_fee":"1.25"}}'
   async def dispose(self):pass
  async def acquire(*args):return 'synthetic'
  async def fetch(route):forwarded.append(route.request.url);return Response()
  async def fulfill(route,response):await route.fulfill(status=200,content_type='application/json',body=await response.body())
  s.observer._acquire=acquire;s.observer._fetch=fetch;s.observer._fulfill=fulfill;s.observer.limiter=SimpleNamespace(release=lambda *args:None)
  await page.route('https://4over.com/print/project/summary/',s.guard)
  state=await asyncio.wait_for(s.wait_quote(selection),10)
  assert s.summary is not None and s.counts['summary']==1 and forwarded==['https://4over.com/print/project/summary/']
  assert s.summary['request_context']['run_size']==250 and state['production']['visible'] is False
  assert s.summary['request_context']['production']=={'value':'10','label':'2-4 Business Days'}
  assert await page.evaluate('fixture.requests')==1 and await page.evaluate('privateReads')==0
  assert PRIVATE not in json.dumps(s.summary) and not s.failure and not s.quote_failure
  assert not any(url not in {SOURCE,'https://4over.com/print/project/summary/'} for url in seen)
  await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.asyncio
@pytest.mark.parametrize('mutate',[
 "fixture.tabs.guestShippingRestricted=true", "fixture.tabs.isProjectNameInvalid(true)","fixture.data.state='cart'",
 "fixture.data.productOptions.peek().super_attribute[111]='11'", "fixture.data.customRunSize(1)",
 "document.querySelector('#tat').closest('div').hidden=true"])
async def test_atomic_preProceed_failures_never_click_or_disclose_hidden_context(tmp_path,mutate):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);await page.evaluate(mutate)
  result=await s.ordinary_quote_action(selection)
  assert result['status'] in {'unavailable','context_changed'} and s.proceed_receipt is None
  assert await page.evaluate('fixture.clicks')==await page.evaluate('fixture.requests')==0 and await page.evaluate('privateReads')==0

@pytest.mark.asyncio
async def test_normal_collector_persists_full_exact_quote_then_yields_without_shipping_or_cart(tmp_path):
 m=load_driver();s,store,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  bind(m,s,page);s.selection=None;s.stage='child';s.wait=m.AutomaticFourOverSession.wait.__get__(s)
  class Response:
   status=200;url='https://4over.com/print/project/summary/';headers={'content-type':'application/json'}
   async def body(self):return b'{"totals":{"subtotal":"10.01","grand_total":"11.26","small_qty_fee":"1.25"}}'
   async def dispose(self):pass
  async def acquire(*args):return 'synthetic'
  async def fetch(route):return Response()
  async def fulfill(route,response):await route.fulfill(status=200,content_type='application/json',body=await response.body())
  s.observer._acquire=acquire;s.observer._fetch=fetch;s.observer._fulfill=fulfill;s.observer.limiter=SimpleNamespace(release=lambda *args:None)
  await page.route('https://4over.com/print/project/summary/',s.guard)
  assert await asyncio.wait_for(s.collect(),15) is False
  assert s.cursor['verified_count']==1 and s.cursor['processed_count']==1 and s.cursor['inflight'] is None
  saved=next(iter(s.cursor['receipts'].values()));assert saved['status']=='verified'
  raw=(tmp_path/saved['evidence']['object_path']).read_bytes();record=json.loads(raw)
  assert record['totals']['subtotal']=='10.01' and record['totals']['grand_total']=='11.26'
  assert record['production']=={'value':'10','label':'2-4 Business Days'}
  assert record['native_quote_transition']['production_visible_at_quote'] is False
  assert record['native_quote_transition']['production_visible_before_proceed'] is True
  assert record['source_data_finalized'] is record['woocommerce_import_ready'] is record['shipping_verified'] is False
  assert PRIVATE.encode() not in raw and await page.evaluate('fixture.requests')==1 and s.counts['summary']==1
  result=s.result(False);assert result['status']=='continuation_required' and result['domain_exhausted'] is False
  assert result['shipping_requests_sent']==result['cart_order_upload_checkout_requests_sent']==0
  await s.proceed_identity.dispose();s.proceed_identity=None

@pytest.mark.asyncio
async def test_handoff_body_cannot_substitute_legacy_allowed_child_for_current_native_model_product(tmp_path):
 m=load_driver();s,_,_=session_fixture(m,tmp_path)
 async with browser_fixture(m) as (page,seen):
  selection=bind(m,s,page);assert (await s.ordinary_quote_action(selection))['status']=='clicked'
  await page.wait_for_function('fixture.finished===true');observed=await s.state();adapted=s.quote_context_state(observed,selection)
  swapped=urlencode({**FORM,'product_id':'700'})
  # Existing default reducer intentionally allows the DOM parent OR child.
  assert m.automatic_request_context(swapped,adapted,selection=selection)['product_id']=='700'
  assert s.proceed_receipt['project_context']['product_id']=='900'
  with pytest.raises(m.Held,match='source_quote_context_changed'):s.quote_request_context(swapped,observed,selection)
  assert s.quote_request_context(urlencode(FORM),observed,selection)['product_id']=='900'
  await s.proceed_identity.dispose();s.proceed_identity=None
