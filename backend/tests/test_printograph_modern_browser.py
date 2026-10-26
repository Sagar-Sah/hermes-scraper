"""Isolated source-free native browser fixtures. No supplier or runtime store."""
from copy import deepcopy
import json
from pathlib import Path
import pytest
from crawler.printograph_modern_browser import (SCHEMA,SERVICE_KEYS,SERVICE_READ_JS,SERVICE_ACTION_JS,ModernHeld,FreshQuoteGate,prepare_services,service_signature,modern_bound,_prices,_production)
from crawler.adapters.browser import SNAPSHOT_JS,normalize_snapshot,make_record
from crawler.service_context import canonical_service_context,service_context_is_bound
from crawler.record_scope import is_legacy_unscoped
URL='https://www.printograph.com/products/synthetic-native-product/order'
PRIVATE='SYNTHETIC-PRIVATE-MUST-NOT-PERSIST'
PRICE={'items':[{'quantity':100,'markupPrice':'13.00'},{'quantity':250,'markupPrice':'28.00'}]}
PRODUCTION={'items':[{'id':1,'range':{'min':3,'max':4,'nullable':None}}]}
CONTROL={'value':'1','selected_label':'Regular 3–4 Business Days'}
def raw():
 services={'proofType':'902','manualProofType':'712','fileDelivery':'502','designServices':'608'}
 groups=[{'key':k,'control_count':0,'choices':[]} for k in SERVICE_KEYS]
 for g,v,label in zip(groups[:3],('902','712','502'),('Manually Processed','No Proof','Upload Files Now')):g.update(control_count=1,choices=[{'value':v,'checked':True,'label':label,'disabled':False,'visible':True}])
 return {'observed':True,'source_url':URL,'product_type':'47','groups':groups,'model_services':services,'source_item_type':{'observed':True,'is_blank_product':False,'value':'502'},'native_total':{'observed':True,'controller_total':'13.00','applied_product_price':'13.00'},'base_context':{'product_id':'900012','specs':{'size':'11'},'production':'1'},'selection_context':{'specs':{'size':'11'},'options':{},'services':services,'quantity':'100','production':'1'}}
def observe(gate,kind='price',before=None,after=None,**kw):
 value=raw();gate.observe('https://www.printograph.com/service/rest/v1/'+('products/900012/prices' if kind=='price' else 'rules/turnarounds'),kw.get('status',200),kw.get('data',PRICE if kind=='price' else PRODUCTION),before or value,after or value,kw.get('evidence_id','receipt1'),kw.get('generation',1),kw.get('current_generation',1),kw.get('epoch',7),kw.get('current_epoch',7),kw.get('method','GET' if kind=='price' else 'POST'),kw.get('body',json.dumps({'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[712],'itemType':502})))
def test_exact_fresh_receipts_do_not_claim_arithmetic_or_completion():
 g=FreshQuoteGate();observe(g);observe(g,'production');v=g.verify(raw(),CONTROL,displayed_subtotal="13.00")
 assert v['quantity_in_owned_price_table'] and not v['component_arithmetic_verified'] and not v['source_data_finalized']
 assert v['production']=={'min':3,'max':4} and service_context_is_bound(service_signature(raw()))
@pytest.mark.parametrize('mode',['quiet','old_epoch','old_generation','quantity','services','product','origin','status','evidence'])
def test_quiet_old_foreign_or_changed_not_current(mode):
 g=FreshQuoteGate();before=raw();after=raw();kw={}
 if mode=='old_epoch':kw['current_epoch']=8
 if mode=='old_generation':kw['current_generation']=2
 if mode=='quantity':after['selection_context']['quantity']='250'
 if mode=='services':after['selection_context']['services']['manualProofType']='711'
 if mode=='product':after['base_context']['product_id']='9';before=deepcopy(after)
 if mode=='origin':after['source_url']='https://evil.invalid/product';before=deepcopy(after)
 if mode=='status':kw['status']=503
 if mode=='evidence':kw['evidence_id']='private?'+PRIVATE
 if mode!='quiet':observe(g,before=before,after=after,**kw);observe(g,'production')
 with pytest.raises(ModernHeld):g.verify(raw(),CONTROL,displayed_subtotal="13.00")
@pytest.mark.parametrize('data',[{}, {'items':[]},{'items':[{'quantity':True,'markupPrice':5}]},{'items':[{'quantity':100,'markupPrice':'NaN'}]},{'items':[{'quantity':100,'markupPrice':13},{'quantity':100,'markupPrice':14}]}])
def test_bad_table_held(data):
 with pytest.raises(ModernHeld):_prices(data)
@pytest.mark.parametrize('data',[{}, {'items':[]},{'items':[{'id':True,'range':{'min':1,'max':3}}]},{'items':[{'id':1,'range':{'min':5,'max':3}}]},{'items':[{'id':1,'range':{'min':3,'max':4,'private':PRIVATE}}]}])
def test_bad_production_held(data):
 with pytest.raises(ModernHeld):_production(data)
def test_stale_label_and_null_max_single_day_semantics():
 g=FreshQuoteGate();observe(g);observe(g,'production')
 with pytest.raises(ModernHeld,match='stale'):g.verify(raw(),{'value':'1','selected_label':'Regular 27–31 Business Days'},displayed_subtotal='13.00')
 observe(g,'production',data={'items':[{'id':1,'range':{'min':3,'max':None}}]})
 assert g.verify(raw(),{'value':'1','selected_label':'3 Business Days'},displayed_subtotal='13.00')['production']=={'min':3,'max':None}
def test_explicit_schema_isolation():
 sig=service_signature(raw());old={'service_context_observed':True,'service_context':[]}
 assert canonical_service_context(old)['version']==2
 current={**old,'service_context_schema':SCHEMA,'modern_service_observation':raw()}
 assert canonical_service_context(current)==sig
 current['pricing_context']={'service_context':sig};current['modern_service_observation']['model_services']['manualProofType']='711'
 assert canonical_service_context(current)['status']=='unknown'

def html(mode='good'):
 groups=[('proofType',[('901','Instantly Processed'),('902','Manually Processed')]),('proofTypeForManual',[('711','PDF Proof'),('712','No Proof')]),('fileDeliveryForManual',[('501','Mail Files'),('502','Upload Files Now')]),('designServices',[('607','Design Service'),('608','No Design Services')])]
 radios=''.join('<fieldset>'+''.join(f'<input type=radio name="{name}" id="{name}{v}" value="{v}" '+('checked' if ix==0 else '')+f'><label for="{name}{v}">{label}</label>' for ix,(v,label) in enumerate(choices))+'</fieldset>' for name,choices in groups)
 return '''<!doctype html><meta charset=utf-8><h1>Synthetic product</h1><style>#collapseOne:not(.show){display:none}label{display:inline-block}</style>
<main><div id=order-form data-printproducttype=47 data-currentstoretype=broker><label for=size>Size</label><select id=size><option value=11>Small</option></select><label for=quantity>Quantity</label><select id=quantity><option value=100>100</option><option value=250>250</option></select><button type=button aria-controls=collapseOne>Process</button><div id=collapseOne>RADIOS</div></div></main><div id=radioBtn><a class="turnAround active" data-toggle=turnaround data-title=1>Regular 3–4 Business Days</a></div><div class=totalSection><div class=subTotal>Subtotal (excludes shipping)</div><div class=col-4>$13.00</div></div><script>
const mode=MODE,root=document.querySelector('#order-form');const selected={size:'11',quantity:'100',productId:'900012',productionTime:'1',proofType:'901',manualProofType:'711',fileDelivery:'501',designServices:'607',shoppingCartItemId:'',priceGroupId:''};const c={printProductType:'47',fields:{'47':{specs:['size'],options:[]}},jsonObj:{userSelectedOptions:selected,appliedPrices:{productPrice:'13.00'},others:{isAdmin:false,waiveFees:false,itemId:'null',editOrderItem:{},blankProducts:[]}}};root._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:c}}}}}}}}};Object.defineProperty(c,'totalPrice',{get(){return mode==='stale_controller'?'13.00':c.jsonObj.appliedPrices.productPrice}});window.controller=c;window.actions=[];root.querySelector('button').onclick=()=>{actions.push('process');document.querySelector('#collapseOne').classList.add('show')};const aliases={proofType:'proofType',proofTypeForManual:'manualProofType',fileDeliveryForManual:'fileDelivery',designServices:'designServices'};
async function nativeQuote(){if(mode==='quiet_only')return;await Promise.all([fetch('/service/rest/v1/products/'+selected.productId+'/prices').then(r=>r.json()),fetch('/service/rest/v1/rules/turnarounds',{method:'POST',body:JSON.stringify({quantity:selected.quantity,specifications:{printProduct:{productType:47,size:11,turnarounds:[1]},other:{}},productOptions:[Number(selected.manualProofType)],itemType:Number(selected.fileDelivery)})}).then(r=>r.json())]);if(mode!=='stale_applied')c.jsonObj.appliedPrices.productPrice=selected.quantity==='100'?'13.00':'28.00';if(mode!=='stale_display'&&mode!=='stale_applied')document.querySelector('.col-4').textContent=selected.quantity==='100'?'$13.00':'$28.00';document.querySelector('.turnAround').textContent=mode==='stale_production'?'Regular 27–31 Business Days':'Regular 3–4 Business Days'}
for(const e of root.querySelectorAll('input[type=radio]'))e.onchange=()=>{selected[aliases[e.name]]=e.value;actions.push(e.name+':'+e.value);nativeQuote()};for(const e of root.querySelectorAll('select'))e.onchange=()=>{selected[e.id]=e.value;actions.push(e.id+':'+e.value);nativeQuote()};</script>'''.replace('RADIOS',radios).replace('MODE',json.dumps(mode))
@pytest.fixture
async def native_page():
 from playwright.async_api import async_playwright
 async with async_playwright() as runtime:
  if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
  browser=await runtime.chromium.launch(headless=True);context=await browser.new_context(service_workers='block');page=await context.new_page();requests=[]
  async def route(r):
   requests.append(r.request.url)
   if r.request.url==URL:await r.fulfill(status=200,content_type='text/html',body=html())
   elif r.request.url.startswith('https://www.printograph.com/service/rest/v1/products/'):await r.fulfill(status=200,json=PRICE)
   elif r.request.url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await r.fulfill(status=200,json=PRODUCTION)
   else:await r.abort();raise AssertionError('External request forbidden')
  await context.route('**/*',route);await page.goto(URL)
  try:yield page,requests
  finally:await context.close();await browser.close()
@pytest.mark.asyncio
async def test_native_safe_labels_non_type38_ids(native_page):
 page,requests=native_page;marks=[]
 async def check():pass
 async def mark():marks.append(True)
 async def settle():await page.wait_for_timeout(50)
 observed,policy=await prepare_services(page,URL,check,mark,settle)
 assert policy=={'proofType':'902','proofTypeForManual':'712','fileDeliveryForManual':'502','designServices':'608'}
 assert await page.evaluate('actions')==['process','proofType:902','proofTypeForManual:712','fileDeliveryForManual:502','designServices:608']
 assert len(marks)==5 and modern_bound(service_signature(observed))
 state=normalize_snapshot(await page.evaluate(SNAPSHOT_JS,{'modernServices':True}))
 assert {c['key'] for c in state['controls']}=={'size','quantity','productionTime'} and not state['coverage_gaps']
@pytest.mark.asyncio
@pytest.mark.parametrize('drift',['missing_flag','string_flag','edit_order','missing_field','unknown_visible','shipping','label','url'])
async def test_unknown_or_private_native_scope_held(native_page,drift):
 page,requests=native_page
 scripts={'missing_flag':"delete controller.jsonObj.others.isAdmin",'string_flag':"controller.jsonObj.others.waiveFees='false'",'edit_order':"controller.jsonObj.others.editOrderItem={private:'"+PRIVATE+"'}",'missing_field':"controller.fields['47'].options=['coating']",'unknown_visible':"document.querySelector('#collapseOne').classList.add('show');document.querySelector('#collapseOne').insertAdjacentHTML('beforeend','<input type=text name=private>')",'shipping':"document.body.insertAdjacentHTML('beforeend','<div id=esCollapse class=show></div>')",'label':"document.querySelector('label[for=proofTypeForManual712]').textContent='Unknown prepaid design'",'url':"history.replaceState(null,'','/products/other/order')"}
 await page.evaluate(scripts[drift])
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(10)
 with pytest.raises(ModernHeld):await prepare_services(page,URL,check,mark,settle)
 assert PRIVATE not in json.dumps(await page.evaluate(SERVICE_READ_JS,{'source_url':URL}))
 assert not any('cart' in r or 'upload' in r or 'shipping' in r for r in requests)

@pytest.mark.asyncio
@pytest.mark.parametrize('mode,expected_rows',[('good',2),('quiet_only',0),('stale_production',0),('first_quantity_quiet',1),('stale_display',1),('stale_controller',1),('stale_applied',1)])
async def test_full_adapter_native_service_walk_freshness_and_bounded_recovery(monkeypatch,mode,expected_rows):
 from contextlib import asynccontextmanager
 from types import SimpleNamespace
 from unittest.mock import AsyncMock
 from playwright.async_api import async_playwright,Route
 from crawler.adapters.browser import BrowserAdapter
 from crawler.adapters.printograph import AdapterError
 import crawler.browser_session as session_module
 import crawler.transport as transport_module
 from crawler.adapters import browser as adapter_module
 class Limiter:
  def __init__(self):self.issued=0;self.released=0
  async def acquire(self,*args):await args[-1]();self.issued+=1;return 'fixture'+str(self.issued)
  def release(self,*args):self.released+=1
 class Transport:
  def __init__(self):self.limiter=Limiter();self.store=SimpleNamespace(config=SimpleNamespace(requests_per_second=2,source_concurrency=2));self.observations=[]
  async def check(self):pass
  async def record_observation(self,url,status,headers,body,context=None,count_request=True):
   data=json.loads(body);self.observations.append({'data':data,'context':context});return SimpleNamespace(data=data,response_id='receipt'+str(len(self.observations)),status=status,cached=False)
 transport=Transport();seen=[];events=[];pages=[]
 async def fake_continue(route,**kwargs):
  request=route.request;seen.append(request.url)
  if request.url==URL:
   content=html(mode)
   if mode=='first_quantity_quiet':content=content.replace("if(mode==='quiet_only')return;", "if(selected.quantity==='100')return;")
   await route.fulfill(status=200,content_type='text/html',body=content)
  elif request.url=='https://www.printograph.com/service/rest/v1/products/900012/prices':await route.fulfill(status=200,json=PRICE)
  elif request.url=='https://www.printograph.com/service/rest/v1/rules/turnarounds':await route.fulfill(status=200,json=PRODUCTION)
  else:await route.abort();raise AssertionError('External fixture request forbidden: '+request.url)
 monkeypatch.setattr(Route,'continue_',fake_continue)
 monkeypatch.setattr(transport_module,'validate_url',AsyncMock())
 monkeypatch.setenv('ATLAS_BROWSER_INACTIVITY_SECONDS','2')
 async with async_playwright() as runtime:
  if not Path(runtime.chromium.executable_path).exists():pytest.skip('local Chromium absent')
  browser=await runtime.chromium.launch(headless=True)
  @asynccontextmanager
  async def page_context(*args,**kwargs):
   context=await browser.new_context(service_workers='block');page=await context.new_page();pages.append(page)
   try:yield page
   finally:await context.close()
  monkeypatch.setattr(session_module,'browser_page',page_context)
  async def emit(name,data):events.append((name,data))
  spec={'url':URL,'source_metadata':{'printograph_modern_services':True},'selection_filters':{},'shipping_contexts':[]}
  rows=[]
  try:
   async for row in BrowserAdapter().extract(spec,transport,emit):rows.append(row)
  except AdapterError as exc:
   assert expected_rows==0 and exc.code=='no_matching_configuration',(exc.code,[(n,d) for n,d in events if n in {'modern_quote_recovery','browser_traversal_diagnostics'}])
  finally:await browser.close()
 assert len(rows)==expected_rows
 assert transport.limiter.issued==transport.limiter.released
 assert all(p.is_closed() for p in pages) and len(pages)==1
 recoveries=[data for name,data in events if name=='modern_quote_recovery']
 assert len(recoveries)<=2 and all(v['attempt']==v['maximum_attempts']==1 for v in recoveries)
 if mode=='good':assert not recoveries
 if mode=='first_quantity_quiet':assert [r['quantity'] for r in rows]==[250] and recoveries
 if mode in {'stale_display','stale_controller','stale_applied'}:assert [r['quantity'] for r in rows]==[100] and recoveries
 for row in rows:
  assert row['record_schema_version']==row['pricing_context']['schema_version']==3
  assert row['pricing_context']['service_context']['model_services']=={'proofType':'902','manualProofType':'712','fileDelivery':'502','designServices':'608'}
  assert row['pricing_context']['native_freshness']['schema']=='printograph-native-dom-quote-freshness/v1'
  assert not row['source_data_finalized'] and not row['woocommerce_import_ready'] and not is_legacy_unscoped(row)
  assert PRIVATE not in json.dumps(row)
 assert all(u==URL or '/service/rest/v1/' in u for u in seen)

def test_current_model_and_visible_product_must_agree_before_row():
 g=FreshQuoteGate();observe(g);observe(g,'production')
 controls=[{'key':'size','value':'11'},{'key':'quantity','value':'250'},{'key':'productionTime','value':'1'}]
 with pytest.raises(ModernHeld,match='model_dom_product_mismatch'):g.verify(raw(),CONTROL,controls,displayed_subtotal="13.00")
 controls[1]['value']='100';assert g.verify(raw(),CONTROL,controls,displayed_subtotal="13.00")['quantity_in_owned_price_table']

@pytest.mark.asyncio
async def test_unselected_offered_proof_can_be_safely_selected(native_page):
 page,_=native_page
 await page.evaluate("controller.jsonObj.userSelectedOptions.manualProofType='';document.querySelector('[name=proofTypeForManual]:checked').checked=false")
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(30)
 value,policy=await prepare_services(page,URL,check,mark,settle)
 assert policy['proofTypeForManual']=='712' and modern_bound(service_signature(value))

@pytest.mark.asyncio
async def test_atomic_native_action_rechecks_private_context_after_awaited_read(native_page):
 page,_=native_page
 await page.evaluate("document.querySelector('#collapseOne').classList.add('show')")
 before=await page.evaluate(SERVICE_READ_JS,{'source_url':URL});group=next(g for g in before['groups'] if g['key']=='proofType');target=next(c for c in group['choices'] if c['value']=='902')
 target.update(retained=[c['value'] for c in group['choices'] if c['checked']],expected_base_context=before['base_context'],expected_selection_context=before['selection_context'])
 await page.evaluate("controller.jsonObj.others.isAdmin=true")
 with pytest.raises(Exception,match='atomic scope changed'):await page.evaluate(SERVICE_ACTION_JS,{'source_url':URL,'action':'radio','target':target})
 assert await page.evaluate('actions')==[]

def test_unbound_bootstrap_is_not_evidence_and_late_unbound_invalidates():
 gate=FreshQuoteGate();observe(gate,before={'observed':False},after={'observed':False})
 with pytest.raises(ModernHeld):gate.verify(raw(),CONTROL,displayed_subtotal="13.00")
 observe(gate);observe(gate,'production');assert gate.verify(raw(),CONTROL,displayed_subtotal="13.00")
 observe(gate,before={'observed':False},after={'observed':False})
 with pytest.raises(ModernHeld,match='unbound_response_after_evidence'):gate.verify(raw(),CONTROL,displayed_subtotal="13.00")

@pytest.mark.parametrize('change',['quantity','proof','spec','private','duplicate','method'])
def test_native_production_post_body_must_retain_current_context(change):
 gate=FreshQuoteGate();observe(gate)
 body={'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[712],'itemType':502}
 if change=='quantity':body['quantity']=250
 if change=='proof':body['productOptions']=[711]
 if change=='spec':body['specifications']['printProduct']['size']=12
 if change=='private':body['private']=PRIVATE
 encoded=json.dumps(body)
 if change=='duplicate':encoded=encoded[:-1]+',"quantity":100}'
 observe(gate,'production',body=encoded,method='GET' if change=='method' else 'POST')
 with pytest.raises(ModernHeld,match='turnaround_request_unproven'):gate.verify(raw(),CONTROL,displayed_subtotal="13.00")

def test_post_requires_all_source_whitelisted_specs_but_not_omitted_orientation():
 gate=FreshQuoteGate();value=raw();value['selection_context']['specs']['orientation']='2';value['base_context']['specs']['orientation']='2'
 observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value)
 assert gate.verify(value,CONTROL,displayed_subtotal="13.00")
 gate=FreshQuoteGate();observe(gate)
 body={'quantity':100,'specifications':{'printProduct':{'productType':47,'turnarounds':[1]},'other':{}},'productOptions':[712],'itemType':502}
 observe(gate,'production',body=json.dumps(body))
 with pytest.raises(ModernHeld):gate.verify(raw(),CONTROL,displayed_subtotal="13.00")

def test_post_must_include_current_nonempty_native_option():
 gate=FreshQuoteGate();value=raw();value['selection_context']['options']['coating']='888'
 observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value)
 with pytest.raises(ModernHeld):gate.verify(value,CONTROL,displayed_subtotal="13.00")

@pytest.mark.parametrize('mismatch',['delivery','missing_blank_witness','wrong_blank_value'])
def test_source_item_type_cannot_be_arbitrary_positive_id(mismatch):
 value=raw();body={'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[712],'itemType':502}
 if mismatch=='delivery':body['itemType']=6
 if mismatch=='missing_blank_witness':value.pop('source_item_type')
 if mismatch=='wrong_blank_value':value['source_item_type']['is_blank_product']=True
 gate=FreshQuoteGate();observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value,body=json.dumps(body))
 with pytest.raises(ModernHeld,match='turnaround_request_unproven'):gate.verify(value,CONTROL,displayed_subtotal='13.00')

def test_observed_blank_product_uses_exact_source_type13_exception():
 value=raw();value['source_item_type']={'observed':True,'is_blank_product':True,'value':'13'}
 body={'quantity':100,'specifications':{'printProduct':{'productType':47,'size':11,'turnarounds':[1]},'other':{}},'productOptions':[712],'itemType':13}
 gate=FreshQuoteGate();observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value,body=json.dumps(body))
 assert gate.verify(value,CONTROL,displayed_subtotal='13.00')['native_computed_subtotal_matches_display']

@pytest.mark.parametrize('mutate,display',[('display','12.00'),('native','13.00'),('base','13.00'),('missing','13.00'),('nan','NaN')])
def test_display_native_total_and_current_base_must_agree(mutate,display):
 value=raw();gate=FreshQuoteGate();observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value)
 if mutate=='native':value['native_total']['controller_total']='12.00'
 if mutate=='base':value['native_total']['applied_product_price']='12.00'
 if mutate=='missing':value.pop('native_total')
 with pytest.raises(ModernHeld,match='subtotal_unproven'):gate.verify(value,CONTROL,displayed_subtotal=display)

def test_native_oracle_total_can_include_fee_without_backend_summing_it():
 value=raw();value['native_total']['controller_total']='18.00';gate=FreshQuoteGate();observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value)
 result=gate.verify(value,CONTROL,displayed_subtotal='18.00')
 assert result['native_computed_subtotal_matches_display'] and not result['component_arithmetic_verified']

@pytest.mark.parametrize('mode',['disabled','hidden','changed_label'])
def test_final_quote_rechecks_source_policy_availability(mode):
 value=raw();gate=FreshQuoteGate();observe(gate,before=value,after=value);observe(gate,'production',before=value,after=value)
 choice=value['groups'][1]['choices'][0]
 if mode=='disabled':choice['disabled']=True
 if mode=='hidden':choice['visible']=False
 if mode=='changed_label':choice['label']='Paid PDF Proof'
 with pytest.raises(ModernHeld,match='policy_unavailable'):gate.verify(value,CONTROL,displayed_subtotal='13.00')

@pytest.mark.asyncio
async def test_atomic_native_final_policy_disabled_by_later_choice_is_held(native_page):
 page,_=native_page
 await page.evaluate("document.querySelector('#designServices608').addEventListener('change',()=>{const e=document.querySelector('#proofTypeForManual712');e.disabled=true;e.style.display='none';e.labels[0].style.display='none'})")
 async def check():pass
 async def mark():pass
 async def settle():await page.wait_for_timeout(30)
 with pytest.raises(ModernHeld,match='policy_reset'):await prepare_services(page,URL,check,mark,settle)

def test_actual_engine_identity_excludes_only_modern_volatile_receipt():
 import ast,hashlib
 source=Path(__file__).resolve().parents[1]/'crawler/engine.py'
 tree=ast.parse(source.read_text());fn=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='row_identity')
 digest=lambda v:hashlib.sha256(json.dumps(v,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 ns={'digest':digest};exec(compile(ast.Module(body=[fn],type_ignores=[]),str(source),'exec'),ns)
 identify=ns['row_identity'];one={'product_url':URL,'quantity':100,'currency':'USD','configuration':{'size':{'value':'11'}},'production':{'value':'1'},'service_context_schema':SCHEMA,'pricing_context':{'schema_version':3,'service_context':service_signature(raw()),'native_freshness':{'price_evidence_id':'first'}}}
 two=deepcopy(one);two['pricing_context']['native_freshness']['price_evidence_id']='second'
 assert identify(one)==identify(two)
 two['pricing_context']['service_context']['model_services']['manualProofType']='711';assert identify(one)!=identify(two)
 legacy=deepcopy(one);legacy.pop('service_context_schema');changed=deepcopy(legacy);changed['pricing_context']['native_freshness']['price_evidence_id']='second'
 assert identify(legacy)!=identify(changed)
