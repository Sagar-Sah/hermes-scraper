"""Portable end-to-end native controller workflow; every request locally intercepted.

Exact saved public source factories/methods execute unchanged in fresh Chromium.
No supplier network, browser profile, personal paths, checkpoint or private receipt.
"""
import asyncio,json
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode,parse_qs
import pytest
from playwright.async_api import async_playwright
from test_fourover_automatic_profile import load_driver as load,Store,SOURCE,PRIVATE,quote_form,ui_ready_state
from test_fourover_native_metadata import catalog,Response
FIXTURES=Path(__file__).resolve().parent/'fixtures'
def form(selection):
    native=quote_form();native['run_size']=str(selection['run_size']);return native

@pytest.mark.asyncio
@pytest.mark.parametrize("include_default",[False,True])
async def test_actual_source_registration_TAT_quantity_and_default_workflow(tmp_path,include_default):
 m=load();store=Store(tmp_path);cat=catalog(store,m,tmp_path);cat['entries'][0]['historical_prices']={250:'1.00',500:'8.00'} if include_default else {500:'8.00'};cat['declared_domain_count']=2 if include_default else 1
 expected_quotes=2 if include_default else 1
 cursor_path,cursor_value=m.load_cursor(store,cat);loaded={'catalog':cat,'cursor_path':cursor_path,'cursor':cursor_value};selection={'child_id':'700','run_size':500,'attributes':{'111':'10','999':'20'}}
 public_module=(FIXTURES/'fourover_customer_module/customer-data.js').read_text()
 address_action=(FIXTURES/'fourover_address_lookup/load-addresses.js').read_text();address_manager=(FIXTURES/'fourover_address_lookup/address-manager.js').read_text()
 controller_scripts={suffix:(FIXTURES/'fourover_native_controller'/suffix.replace('/','__')).read_text() for suffix in m.CONTROLLER_PUBLIC_MODULES}
 qty_source=(FIXTURES/'fourover_native_controller/product-run-size.js').read_text()
 select_body=qty_source.split('selectQty: function (priceItem) {',1)[1].split('\n        },',1)[0]
 set_body=qty_source.split('setCustomRunSizePrices: function (unitPrice, totalPrice, closestLowerQty, closestUpperQty) {',1)[1].split('\n        },',1)[0]
 quantity_methods='selectQty:function(priceItem){'+select_body+'},setCustomRunSizePrices:function(unitPrice,totalPrice,closestLowerQty,closestUpperQty){'+set_body+'}'
 if not include_default:assert m.reserve_once(store.root,SOURCE,{'child_id':'700','run_size':250,'attributes':selection['attributes']},loaded['cursor']['key'])
 builder=public_module.split('getFromServer: function (sectionNames, forceNewSectionTimestamp) {',1)[1].split('\n        }\n    };',1)[0]
 builder='function(sectionNames,forceNewSectionTimestamp){'+builder+'}'
 document=r'''<!doctype html><html><body><main class="column main"><form action="/checkout/cart/add" method="post">
 <select name="super_attribute[111]"><option value="">Choose</option><option value="10">A</option></select>
 <select name="super_attribute[999]"><option value="">Choose</option><option value="20">B</option></select>
 <input type="hidden" name="product" value="1234"><input type="hidden" name="selected_configurable_option" value="700"><input type="hidden" name="run_size" value="0">
 <ul id="quantities"></ul><input type="checkbox" name="job_sample"><input type="checkbox" name="digital_proof">
 <div id="production" hidden><input id="regular" type="radio" style="display:none" name="turnaround_time" value="3" checked><label for="regular">3 Business Days</label></div>
 <div id="money"></div><button id="quote" type="button">PROCEED TO SHIPPING</button></form></main>
 <script>window.define=(deps,factory)=>{if(deps.length===2&&deps[0]==='Magento_Checkout/js/model/url-builder')window.lookupFactory=factory;if(deps.includes('FourOver_Project/js/action/project/load-addresses'))window.managerFactory=factory};</script><script src="https://4over.com/static/version42/frontend/FourOver/default/en_US/Magento_Customer/js/customer-data.js"></script><script src="https://4over.com/static/version42/frontend/FourOver/default/en_US/FourOver_Project/js/action/project/load-addresses.js"></script><script src="https://4over.com/static/version42/frontend/FourOver/default/en_US/FourOver_Project/js/model/project/address-manager.js"></script><script>window.nativeFactories={};const oldDefine=window.define;window.define=(deps,factory)=>{const current=document.currentScript.src.split('/en_US/')[1].replace(/\.js$/,'');window.nativeFactories[current]={deps,factory};oldDefine(deps,factory)};</script>CONTROLLERS<script>
 window.fixture={earlyPrice:false,customer:false,address:false,clicks:0,quotes:0,selections:0,selection_order:[],selections_before_registration:0,tat_hidden_after_registration:false};document.querySelector('form').addEventListener('submit',e=>e.preventDefault());
 function observable(initial){let value=initial;const subscribers=[];function obs(next){if(!arguments.length)return value;if(value!==next){value=next;subscribers.forEach(fn=>fn(value))}return obs}obs.peek=()=>value;obs.subscribe=(fn,owner)=>subscribers.push(owner?fn.bind(owner):fn);return obs}
 const ko={observable,observableArray:observable},underscore={isFunction:v=>typeof v==='function',isEmpty:v=>v==null||Object.keys(v).length===0,findWhere:(rows,query)=>rows.find(row=>Object.entries(query).every(([key,value])=>row[key]===value)),each:(rows,fn,owner)=>Object.entries(rows).forEach(([key,value])=>fn.call(owner,value,Array.isArray(rows)?Number(key):key)),sortBy:(rows,key)=>rows.slice().sort((a,b)=>a[key]-b[key]),keys:Object.keys,filter:(rows,fn)=>rows.filter(fn),min:(rows,fn)=>rows.reduce((a,b)=>fn(a)<fn(b)?a:b,Infinity)};
 const modules={},stubs={'ko':ko,'underscore':underscore,'jquery':()=>({trigger:()=>{}}),'uiComponent':{extend:spec=>spec},'priceUtils':{formatPrice:String},'mage/url':{build:x=>'/'+x},'mage/translate':x=>x,'FourOver_ProductCustomOptions/js/model/custom-option-price':{},'FourOver_ProductRunSize/js/model/custom-run-size-price':observable({}),'FourOver_ProductRunSize/js/model/selected-custom-run-size':observable(0),'FourOver_TurnaroundTime/js/model/product/tat-price-percent':observable(0),'FourOver_TurnaroundTime/js/model/product/preselect-tat':observable(true)};
 function instantiate(name){if(Object.hasOwn(modules,name))return modules[name];if(Object.hasOwn(stubs,name))return stubs[name];const spec=window.nativeFactories[name];if(!spec)throw Error('Unknown synthetic dependency');return modules[name]=spec.factory(...spec.deps.map(instantiate))}
 window.require=name=>{if(typeof name!=='string'||!Object.hasOwn(modules,name))throw Error('Async or undefined require forbidden');return modules[name]};window.require.defined=name=>Object.hasOwn(modules,name);
 const child=instantiate('FourOver_Project/js/model/product-selected-id'),qty=instantiate('FourOver_Project/js/model/selected-runsize'),prices=instantiate('FourOver_Project/js/model/product-package-prices'),tat=instantiate('FourOver_TurnaroundTime/js/model/product/turnaround-time');instantiate('FourOver_Project/js/model/product-price-processor');instantiate('FourOver_ProductRunSize/js/model/price-processor');
 const tatSpec=instantiate('FourOver_TurnaroundTime/js/view/product/turnaround-time'),tatVM={...tatSpec.defaults,...tatSpec,_super(){return this},observe(names){names.forEach(name=>{if(typeof this[name]!=='function')this[name]=observable(this[name])});return this}};
 Object.assign(tatVM,{globalTatPriceConfig:{'3':0},tatOptionLabels:{'3':'3 Business Days'},tatOptionOrder:{'3':1},defaultAvailableOptions:{'100000':['3']}});tatVM.initObservable();tatVM.initialize();
 tatVM.tatOptions.subscribe(options=>{document.querySelector('#production').innerHTML=options.map(option=>'<input id="regular'+option.value+'" type="radio" style="display:none" name="turnaround_time" value="'+option.value+'" '+(String(tat.peek())===String(option.value)?'checked':'')+'><label for="regular'+option.value+'">'+option.label+'</label>').join('')});tatVM.isVisible.subscribe(visible=>{document.querySelector('#production').hidden=!visible});
 const sourceQuantity={selectedCustomRunSize:stubs['FourOver_ProductRunSize/js/model/selected-custom-run-size'],selectedLowerRunSize:observable(0),customRunSizeErrorMessage:observable(''),customRunSizeUnitPrice:observable(''),customRunSizeTotalPrice:observable(''),selectedQty:qty,formatPrice:String,QUANTITYMETHODS};
 qty.subscribe(value=>{document.querySelector('[name="run_size"]').value=String(value);document.querySelectorAll('li.runsizes_row').forEach(li=>li.classList.toggle('selected-qty',li.textContent===String(value)))});
 setTimeout(()=>{instantiate('FourOver_ProductRunSize/js/view/register-price-processor');window.fixture.tat_hidden_after_registration=tatVM.isVisible.peek()===false},650);

 const nativeStorage={post:(url,body)=>{const hooks=[];let done=false,received;const deferred={done:fn=>{hooks.push(fn);if(done)fn(received);return deferred}};const r=new XMLHttpRequest();r.open('POST',url);r.setRequestHeader('Content-Type','application/json');r.responseType='json';r.onload=()=>{done=true;received=r.response;hooks.forEach(fn=>fn(received))};r.send(body);return deferred}};const nativeLookup=window.lookupFactory({createUrl:path=>'/rest/default/V1'+path},nativeStorage);const nativeManager=window.managerFactory({findWhere:(rows,query)=>rows.find(row=>Object.entries(query).every(([key,value])=>row[key]===value))},nativeLookup);nativeManager.loadProjectAddresses([]).done(()=>{window.fixture.address=true});
 const sectionConfig={filterClientSideSections:x=>x},_={isArray:Array.isArray},options={sectionLoadUrl:'/customer/section/load/'};const $={getJSON:(url,parameters)=>{const query=new URLSearchParams();for(const key in parameters)query.set(key,parameters[key]===undefined?'':parameters[key]);const account=new XMLHttpRequest();account.open('GET',url+'?'+query);account.responseType='json';account.onload=()=>{window.fixture.customer=account.response.customer.is_logged_in===true};account.send();return{fail:()=>{}}}};const nativeBuilder=BUILDER;nativeBuilder([],undefined);
 fetch('/rest/default/V1/product/prices/700').then(r=>r.json()).then(rows=>{
 window.fixture.earlyPrice=true;document.querySelector('[name="super_attribute[111]"]').value='10';document.querySelector('[name="super_attribute[999]"]').value='20';
 prices(rows[0].packagePrices);child('700');rows[0].packagePrices.forEach(row=>{const li=document.createElement('li');li.className='runsizes_row';li.innerHTML='<span class="runsize">'+row.qty+'</span>';li.onclick=()=>{window.fixture.selections++;window.fixture.selection_order.push(row.qty);if(!window.require.defined('FourOver_ProductRunSize/js/view/register-price-processor'))window.fixture.selections_before_registration++;sourceQuantity.selectQty(row)};document.querySelector('#quantities').append(li)});qty(rows[0].packagePrices[0].qty);
 });
 document.querySelector('#quote').addEventListener('click',async()=>{
 window.fixture.clicks++;if(!window.fixture.customer||!window.fixture.address||!window.fixture.earlyPrice)return;
 if(!tatVM.isVisible.peek()||String(tat.peek())!=='3')return;
 const result=await new Promise((resolve,reject)=>{const r=new XMLHttpRequest();r.open('POST','/print/project/summary/');r.setRequestHeader('Content-Type','application/x-www-form-urlencoded');r.responseType='json';r.onload=()=>resolve(r.response);r.onerror=reject;const form=new URLSearchParams(QUOTEFORM);form.set('run_size',String(qty.peek()));r.send(form.toString())});document.querySelector('#money').innerHTML='<div>Subtotal: $'+result.totals.subtotal.toFixed(2)+'</div><div>Total: $'+result.totals.grand_total.toFixed(2)+'</div>';window.fixture.quotes++;
 });</script></body></html>'''.replace('CONTROLLERS',''.join('<script src="https://4over.com/static/version42/frontend/FourOver/default/en_US/'+suffix+'"></script>' for suffix in controller_scripts)).replace('QUANTITYMETHODS',quantity_methods).replace('BUILDER',builder).replace('QUOTEFORM',json.dumps(urlencode(form(selection))))
 requests=[];responses=[];external=[];private_body_reads=[]
 async with async_playwright() as runtime:
  browser=await runtime.chromium.launch(headless=True)
  context=await browser.new_context(service_workers='block');page=await context.new_page()
  class Observer:
   def __init__(self):self.store=store;self.pool=self;self.limiter=self;self.permits=set()
   @asynccontextmanager
   async def page(self,*a,**k):yield page
   async def _acquire(self,rate,concurrency,check):await check();token=object();self.permits.add(token);return token
   def release(self,origin,token,*a):self.permits.remove(token)
   async def _fetch(self,route):
    req=route.request;requests.append((req.method,req.url))
    if req.url==SOURCE:raw=document.encode();mime='text/html'
    elif '/Magento_Customer/js/customer-data.js' in req.url:raw=public_module.encode();mime='application/javascript'
    elif '/product/prices/700' in req.url:
     # Exact actual DOM is still unselected when this native GET arrives.
     values=await page.evaluate("[...document.querySelectorAll('select')].map(e=>e.value)");assert values==['','']
     raw=b'[{"packagePrices":[{"qty":250,"price":1},{"qty":500,"price":10.006}],"tierPrices":[]}]';mime='application/json'
    elif req.url=='https://4over.com/rest/default/V1/project/mine/addresses':
     assert req.post_data=='{"addressIds":[]}'
     assert session.cursor['inflight'] is None
     raw=json.dumps([{'id':'123','default_shipping':True,'firstname':PRIVATE,'street':[PRIVATE]}]).encode();mime='application/json'
    elif '/customer/section/load/' in req.url:raw=json.dumps({'customer':{'is_logged_in':True,'name':PRIVATE}}).encode();mime='application/json'
    elif req.url.endswith('/print/project/summary/'):
     native_q=parse_qs(req.post_data)['run_size'][0];assert native_q in {'250','500'}
     raw=(b'{"totals":{"subtotal":1.00,"grand_total":2.25,"small_qty_fee":1.25}}' if native_q=='250' else b'{"totals":{"subtotal":10.01,"grand_total":11.26,"small_qty_fee":1.25}}');mime='application/json'
    else:external.append('unexpected_fetch');raise AssertionError('Unexpected source transport')
    response=Response(req,raw,mime);responses.append(response)
    if '/customer/section/load/' in req.url or req.url.endswith('/project/mine/addresses'):
     async def forbidden_body():private_body_reads.append(True);raise AssertionError('Private customer body read')
     response.body=forbidden_body
    return response
   async def _fulfill(self,route,response):await route.fulfill(status=response.status,content_type=response.headers['content-type'],body=response.raw)
  observer=Observer()
  ns=m.AutomaticFourOverSession.run.__globals__
  ns['inspect_child_configurations']=lambda *a,**k:(cat['job']['declared_children'],{})
  ns['inspect_option_contract']=lambda *a,**k:{'binding_complete':True,'rows':cat['rows']}
  ns['document_static_prefix']=lambda raw:'https://4over.com/static/version42/frontend/FourOver/default/en_US/'
  ns['source_declares_tracker']=lambda raw:False;ns['retry_after_seconds']=lambda header,default=30:float(header or 0)
  async def check():pass
  async def validate(url):assert url==SOURCE
  @asynccontextmanager
  async def slot(*a,**k):yield
  ns['validate_url']=validate;ns['browser_slot']=slot
  session=m.AutomaticFourOverSession(observer,loaded,{'profile_id':m.PROFILE_ID,'max_quotes_per_session':expected_quotes},check)
  # This fixture tests real calculator/register/TAT behavior. Native registry
  # disclosure is an independent prerequisite covered by the dedicated UI suite.
  native_state=session.state
  async def independent_ui_state():
   observed=await native_state();observed['source_ui']=ui_ready_state();return observed
  session.state=independent_ui_state
  async def focused_quote_action(selection):
   # This fixture isolates transport/controller behavior with independent UI
   # disclosure stubbed. Exact native model/step handoff is covered separately
   # by test_fourover_native_proceed_transition; keep this fixture's assertions.
   return await session.page.evaluate(m.automatic_quote_button_js(selection['attributes']),
    {'source':session.source,'selection':selection,'click':True})
  session.ordinary_quote_action=focused_quote_action
  # The session installs its own page route; context fallback NEVER accesses a network.
  async def deny(route):
   # Only this exact public script is passed through by the source guard;
   # fulfill locally, without a network or saved browser profile.
   scripts={'Magento_Customer/js/customer-data.js':public_module,'FourOver_Project/js/action/project/load-addresses.js':address_action,'FourOver_Project/js/model/project/address-manager.js':address_manager,**controller_scripts}
   for suffix,body in scripts.items():
    if route.request.url.endswith('/'+suffix):
     await route.fulfill(status=200,content_type='application/javascript',body=body);return
   external.append('unexpected_context_route');await route.abort()
  await context.route('**/*',deny)
  result=await session.run();ui=await page.evaluate('window.fixture');retained=await session.state()
  assert retained['production']['visible'] is True and retained['source_controller']['tat_input_visible'] is False and retained['source_controller']['tat_label_visible'] is True and m.controller_ready(retained,tat=True)
  assert result['status']=='matched_current_source_quote',result
  assert result['verified_count']==expected_quotes and result['controlled_requests']['price']==1 and result['controlled_requests']['summary']==expected_quotes
  assert result['customer_metadata_requests_sent']==1 and result['customer_public_module_declared'] is True and result['customer_document_declared'] is False and result['address_metadata_requests_sent']==1 and result['address_lookup_ready'] is True and ui=={'earlyPrice':True,'customer':True,'address':True,'clicks':expected_quotes,'quotes':expected_quotes,'selections':expected_quotes,'selection_order':[500,250] if include_default else [500],'selections_before_registration':0,'tat_hidden_after_registration':True},ui
  assert not private_body_reads and not external and not observer.permits and all(r.disposals==1 for r in responses)
  assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(loaded['cursor']) and PRIVATE not in b''.join(store.objects.values()).decode()
  assert len(requests)==4+expected_quotes and result['shipping_requests_sent']==result['cart_order_upload_checkout_requests_sent']==0
  await page.evaluate("""() => {const shell=document.createElement('div');shell.className='modal-popup b2c-company-popup _show';shell.innerHTML='<div id="b2c-company-modal"><input name="firstname"></div>';document.body.append(shell);Object.defineProperty(shell.querySelector('input'),'value',{get(){throw Error('Private profile field read')}})}""")
  assert await page.evaluate(m.ACCOUNT_PROFILE_GATE_JS) is True
  await page.evaluate("document.querySelector('.b2c-company-popup').style.display='none'");assert await page.evaluate(m.ACCOUNT_PROFILE_GATE_JS) is False
  await page.evaluate("document.querySelector('.b2c-company-popup').style.display='';document.querySelector('.b2c-company-popup').classList.remove('_show')");assert await page.evaluate(m.ACCOUNT_PROFILE_GATE_JS) is False
  report={'passed':True,'actual_chromium':True,'synthetic_requests':len(requests),'supplier_requests':0,'external_requests':0,'private_customer_body_reads':0,'verified_quotes':expected_quotes,'default_deferred_and_returned_immediately':include_default,'native_price_before_dom_retention':True,'customer_metadata_native_activation':True,'actual_public_module_proof_without_html_init':True,'actual_source_empty_query_builder':True,'actual_native_address_action_and_manager':True,'private_address_body_reads':0,'address_read_before_quantity_reservation':True,'actual_b2c_visibility_gate_without_private_field_reads':True,'actual_late_registration_no_quantity_before_ready':True,'actual_source_tat_renders_naturally_from_quantity':True,'production_never_forced_visible_after_proceed':True,'hidden_native_radio_visible_unique_associated_label':True,'ordinary_quote_clicks':ui['clicks'],'current_production_and_totals_verified':True,'installed_edits':0,'DB_access':0}
  assert report['passed'] and report['supplier_requests']==report['external_requests']==0
  await context.close();await browser.close()
