"""Actual saved public dependency proof; no supplier or runtime imports."""
import asyncio,json,hashlib
from pathlib import Path
from urllib.parse import urlsplit
import pytest
from test_fourover_automatic_profile import load_driver as load,PRIVATE,state
from test_fourover_native_metadata import fixture as base_fixture,Request as BaseRequest,Route as BaseRoute,Response as BaseResponse

class Request(BaseRequest):
    def __init__(self,page,url,kind='xhr',private=False):super().__init__(page,url,resource=kind)
    @property
    def reads(self):return self.private_reads
class Response(BaseResponse):
    def __init__(self,request,raw,mime,status=200):super().__init__(request,raw,mime);self.status=status
class Route(BaseRoute):
    def __init__(self,page,request):super().__init__(request)
async def native_ready_fixture(mod,tmp_path):
    s,o,page,current=base_fixture(mod,tmp_path);page.responses=[];page.current=current
    return s,page,o

RAW=(Path(__file__).resolve().parent/'fixtures/fourover_customer_module/customer-data.js').read_bytes()

@pytest.fixture
def mod():return load()

@pytest.mark.parametrize('query,allowed',[
 ('?sections=&force_new_section_timestamp=',True),('?sections=customer&force_new_section_timestamp=',True),
 ('?force_new_section_timestamp=false',True),('?sections=',True),
 ('?sections=*&force_new_section_timestamp=',False),('?sections=customer,,cart',False),
 ('?force_new_section_timestamp=undefined',False),('?force_new_section_timestamp=null',False),
 ('?sections=&force_new_section_timestamp=&action=delete',False),('?sections=&sections=customer',False),
])
def test_actual_native_builder_empty_values_are_bounded_read_only(mod,query,allowed):
    assert mod.native_customer_query(urlsplit('https://4over.com/customer/section/load/'+query)) is allowed

@pytest.mark.parametrize('change',['bytes','suffix','none'])
def test_module_proof_is_exact_saved_current_public_identity(mod,change):
    raw=RAW+b' ' if change=='bytes' else RAW
    suffix='other/customer-data.js' if change=='suffix' else mod.CUSTOMER_MODULE_SUFFIX
    assert mod.customer_module_declared(suffix,raw) is (change=='none')
    assert hashlib.sha256(RAW).hexdigest()==mod.CUSTOMER_MODULE_SHA256

@pytest.mark.parametrize('change',['none','frame','prefix','mime','status','bytes'])
def test_only_actual_just_loaded_same_page_public_module_proves_dependency(mod,tmp_path,change):
    async def run():
        s,page,_=await native_ready_fixture(mod,tmp_path)
        url=s.prefix+mod.CUSTOMER_MODULE_SUFFIX
        if change=='prefix':url=url.replace('version1/','version2/')
        req=Request(page,url,kind='script')
        if change=='frame':req.frame=object()
        response=Response(req,RAW+(b' ' if change=='bytes' else b''),'text/html' if change=='mime' else 'application/javascript',201 if change=='status' else 200)
        await s.capture_public_script(response)
        assert s.customer_module_declared is (change=='none')
        assert not s.customer_declared and not s.cursor['receipts'] and s.customer_reads==0
    asyncio.run(run())

def test_native_read_waits_only_for_arrived_module_proof_then_keeps_private_response_opaque(mod,tmp_path):
    async def run():
        s,page,observer=await native_ready_fixture(mod,tmp_path)
        entered=asyncio.Event();release=asyncio.Event()
        req=Request(page,s.prefix+mod.CUSTOMER_MODULE_SUFFIX,kind='script');response=Response(req,RAW,'application/javascript')
        async def arrived():
            entered.set();await release.wait();await s.capture_public_script(response)
        pending=asyncio.create_task(arrived());s.public_asset_tasks.add(pending);pending.add_done_callback(s.public_asset_tasks.discard);s.customer_module_tasks.add(pending);pending.add_done_callback(s.customer_module_tasks.discard)
        await entered.wait()
        fetched=[]
        async def fetch(route):
            fetched.append(route.request.url);r=Response(route.request,b'{"customer":{"name":"PRIVATE"}}','application/json')
            async def trap():raise AssertionError('private customer body read')
            r.body=trap;page.responses.append(r);return r
        observer._fetch=fetch
        route=Route(page,Request(page,'https://4over.com/customer/section/load/?sections=&force_new_section_timestamp=',private=True))
        task=asyncio.create_task(s.guarded(route));await asyncio.sleep(.02)
        assert not fetched and not task.done();release.set();await pending;await task
        assert route.forwarded and not route.aborted and len(fetched)==1 and s.customer_module_declared
        assert route.request.reads==0 and PRIVATE not in json.dumps(s.cursor)
        assert all(not p.endswith('customer') for p in s.store.objects)
        await page.responses[0].dispose()
    asyncio.run(run())

def test_missing_module_proof_does_not_fetch_or_read_private_customer_payload(mod,tmp_path):
    async def run():
        s,page,_=await native_ready_fixture(mod,tmp_path)
        req=Request(page,'https://4over.com/customer/section/load/?sections=',private=True);route=Route(page,req)
        await s.guarded(route)
        assert route.aborted and not route.forwarded and req.reads==0 and s.customer_reads==0
        assert s.blocked['customer_dependency_unproved']==1
        s.capture_diagnostic('source_phase_timeout',page.current)
        assert s.cursor['last_failure_diagnostic']['customer_readiness']=={'document_declared':False,'public_module_declared':False,'native_requests_sent':0}
    asyncio.run(run())

def test_late_source_owned_templates_cannot_be_crowded_out_by_generic_scripts(mod,tmp_path):
    async def run():
        s,_,_=await native_ready_fixture(mod,tmp_path)
        for i in range(200):s.retain_public_asset(f'Magento_Ui/js/component{i}.js',b'public generic','application/javascript')
        assert len(s.public_assets)==128
        for i in range(128):s.retain_public_asset(f'FourOver_Project/template/component{i}.html',b'public source template','text/html')
        assert len(s.public_assets)==256
        assert 'FourOver_Project/template/component127.html' in s.public_assets
        assert 'Magento_Ui/js/component128.js' not in s.public_assets
        assert s.counts=={'document':0,'price':0,'summary':0,'static':0}
        assert s.customer_reads==0 and not s.cursor['receipts']
        # Existing aggregate byte limit still applies, including priority assets.
        s.public_assets={};s.public_asset_bytes=16*1024*1024
        s.retain_public_asset('FourOver_Project/template/extra.html',b'x','text/html');assert not s.public_assets
    asyncio.run(run())

@pytest.mark.parametrize('block',['customer_dependency_unproved','customer_query_unrecognized'])
def test_known_customer_dependency_failure_holds_before_reserving_quote(mod,tmp_path,block):
    async def run():
        s,page,observer=await native_ready_fixture(mod,tmp_path);s.block(block)
        class Control:
            async def count(self):return 1
        page.locator=lambda _:Control()
        async def evaluate(code,arg=None):
            if arg and 'attribute_id' in arg:return {'selected_matches':True,'expected_choice_enabled':True}
            return page.current
        page.current=__import__('test_fourover_automatic_profile').state();page.evaluate=evaluate
        async def wait(predicate,seconds=35):
            assert predicate(page.current);return page.current
        s.wait=wait;s.first_target_table_ready=True;s.address_ready=True  # Independent address prerequisite is complete in this customer-only fixture.
        s.tables['700']={'rows':[{'quantity':250,'price':'10.006'}]}
        with pytest.raises(mod.Held) as caught:await s.collect()
        assert caught.value.code=='source_customer_dependency_unproved'
        assert s.cursor['processed_count']==s.cursor['verified_count']==0 and s.counts['summary']==0
        assert not list((s.store.root/'fourover-native-observation-selections').rglob('*.json'))
    asyncio.run(run())

@pytest.mark.parametrize('mode',['closed','url','scope'])
def test_public_module_body_race_cannot_promote_dependency(mod,tmp_path,mode):
    async def run():
        s,page,_=await native_ready_fixture(mod,tmp_path)
        req=Request(page,s.prefix+mod.CUSTOMER_MODULE_SUFFIX,kind='script');r=Response(req,RAW,'application/javascript')
        calls=[]
        async def body():
            calls.append('body')
            if mode=='closed':s.closed=True
            elif mode=='url':page.url='https://4over.com/other'
            return RAW
        r.body=body
        if mode=='scope':
            async def scope():
                if calls:raise mod.Held('scope_revoked')
            s.check_scope=scope
        await s.capture_public_script(r)
        assert not s.customer_module_declared and not s.public_assets and not s.cursor['receipts']
    asyncio.run(run())

def test_private_metadata_exact_request_and_consumer_budgets_remain_bounded(mod,tmp_path):
    async def run():
        s,page,o=await native_ready_fixture(mod,tmp_path);s.customer_module_declared=True;fetched=[]
        async def fetch(route):
            fetched.append(route.request.url);r=Response(route.request,b'{"customer":{"name":"PRIVATE"}}','application/json')
            async def trap():raise AssertionError('Private customer body read')
            r.body=trap;page.responses.append(r);return r
        o._fetch=fetch
        routes=[]
        for i in range(5):
            route=Route(page,Request(page,'https://4over.com/customer/section/load/?sections=customer&_='+str(i),private=True));routes.append(route);await s.guard(route)
        assert all(r.forwarded for r in routes[:4]) and routes[4].aborted and len(fetched)==s.customer_reads==4
        first=routes[0].request.url
        duplicates=[]
        for _ in range(4):
            r=Route(page,Request(page,first,private=True));duplicates.append(r);await s.guard(r)
        assert all(r.forwarded for r in duplicates[:3]) and duplicates[3].aborted and len(fetched)==4
        assert all(r.request.reads==0 for r in routes+duplicates) and not s.cursor['receipts']
        assert 'PRIVATE' not in json.dumps(s.cursor) and not s.store.objects
        for r in page.responses:await r.dispose()
    asyncio.run(run())

def test_unproved_pending_module_is_bounded_by_existing_phase_deadline(mod,tmp_path):
    async def run():
        import time
        s,page,o=await native_ready_fixture(mod,tmp_path);s.deadline=time.monotonic()+.02
        task=asyncio.create_task(asyncio.sleep(60));s.customer_module_tasks.add(task)
        route=Route(page,Request(page,'https://4over.com/customer/section/load/?sections=',private=True))
        try:
            await asyncio.wait_for(s.guard(route),.5)
            assert route.aborted and not route.forwarded and s.customer_reads==0 and route.request.reads==0
        finally:
            task.cancel();await asyncio.gather(task,return_exceptions=True)
    asyncio.run(run())

from contextlib import asynccontextmanager
from urllib.parse import urlencode
from test_fourover_automatic_profile import SOURCE,SELECTION,Store,quote_form
from test_fourover_native_metadata import catalog
@pytest.mark.asyncio
async def test_native_public_module_without_html_init_and_empty_query_completes_intercepted_quote(mod,tmp_path):
 driver=mod
 playwright=pytest.importorskip('playwright.async_api')
 async_playwright=playwright.async_playwright
 m=driver;store=Store(tmp_path);cat=catalog(store,driver,tmp_path);path,value=driver.load_cursor(store,cat)
 public_module=RAW.decode()
 builder=public_module.split("getFromServer: function (sectionNames, forceNewSectionTimestamp) {",1)[1].split("\n        }\n    };",1)[0]
 builder="function(sectionNames,forceNewSectionTimestamp){"+builder+"}"
 loaded={'catalog':cat,'cursor_path':path,'cursor':value};selection=dict(SELECTION)
 document='''<!doctype html><html><body><main class="column main"><form action="/checkout/cart/add" method="post">
 <select name="super_attribute[111]"><option value="">Choose</option><option value="10">A</option></select>
 <select name="super_attribute[999]"><option value="">Choose</option><option value="20">B</option></select>
 <input type="hidden" name="product" value="1234"><input type="hidden" name="selected_configurable_option" value="700"><input type="hidden" name="run_size" value="250">
 <ul id="quantities"></ul><input type="checkbox" name="job_sample"><input type="checkbox" name="digital_proof">
 <div id="production"><input id="regular" type="radio" name="turnaround_time" value="3" checked><label for="regular">3 Business Days</label></div>
 <div id="money"></div><button id="quote" type="button">PROCEED TO SHIPPING</button></form></main>
 <script>window.define=()=>{};</script><script src="https://4over.com/static/version42/frontend/FourOver/default/en_US/Magento_Customer/js/customer-data.js"></script><script>
 window.fixture={earlyPrice:false,customer:false,clicks:0,quotes:0};document.querySelector('form').addEventListener('submit',e=>e.preventDefault());
 const sectionConfig={filterClientSideSections:x=>x},_={isArray:Array.isArray},options={sectionLoadUrl:'/customer/section/load/'};const $={getJSON:(url,parameters)=>{const query=new URLSearchParams();for(const key in parameters)query.set(key,parameters[key]===undefined?'':parameters[key]);const account=new XMLHttpRequest();account.open('GET',url+'?'+query);account.responseType='json';account.onload=()=>{window.fixture.customer=account.response.customer.is_logged_in===true};account.send();return{fail:()=>{}}}};const nativeBuilder=BUILDER;nativeBuilder([],undefined);
 fetch('/rest/default/V1/product/prices/700').then(r=>r.json()).then(rows=>{
 window.fixture.earlyPrice=true;document.querySelector('[name="super_attribute[111]"]').value='10';document.querySelector('[name="super_attribute[999]"]').value='20';
 const li=document.createElement('li');li.className='runsizes_row';li.innerHTML='<span class="runsize">250</span>';li.onclick=()=>{li.classList.add('selected-qty');document.querySelector('[name="run_size"]').value='250'};document.querySelector('#quantities').append(li);
 });
 document.querySelector('#quote').addEventListener('click',async()=>{
 window.fixture.clicks++;if(!window.fixture.customer||!window.fixture.earlyPrice)return;
 const result=await new Promise((resolve,reject)=>{const r=new XMLHttpRequest();r.open('POST','/print/project/summary/');r.setRequestHeader('Content-Type','application/x-www-form-urlencoded');r.responseType='json';r.onload=()=>resolve(r.response);r.onerror=reject;r.send(QUOTEFORM)});document.querySelector('#money').innerHTML='<div>Subtotal: $10.01</div><div>Total: $11.26</div>';window.fixture.quotes++;
 });</script></body></html>'''.replace('BUILDER',builder).replace('QUOTEFORM',json.dumps(urlencode(quote_form())))
 requests=[];responses=[];external=[];private_body_reads=[]
 async with async_playwright() as runtime:
  if not Path(runtime.chromium.executable_path).is_file():pytest.skip('local Chromium absent')
  browser=await runtime.chromium.launch(headless=True)
  context=await browser.new_context(service_workers='block');page=await context.new_page()
  class Observer:
   def __init__(self):self.store=store;self.pool=self;self.limiter=self;self.permits=set()
   @asynccontextmanager
   async def page(self,*a,**k):yield page
   async def _acquire(self,rate,concurrency,check):await check();token=str(len(self.permits));self.permits.add(token);return token
   def release(self,origin,token,*a):self.permits.remove(token)
   async def _fetch(self,route):
    req=route.request;requests.append((req.method,req.url))
    if req.url==SOURCE:raw=document.encode();mime='text/html'
    elif '/product/prices/700' in req.url:
     # Exact actual DOM is still unselected when this native GET arrives.
     values=await page.evaluate("[...document.querySelectorAll('select')].map(e=>e.value)");assert values==['','']
     raw=b'[{"packagePrices":[{"qty":250,"price":10.006}],"tierPrices":[]}]';mime='application/json'
    elif '/customer/section/load/' in req.url:raw=json.dumps({'customer':{'is_logged_in':True,'name':PRIVATE}}).encode();mime='application/json'
    elif req.url.endswith('/print/project/summary/'):raw=b'{"totals":{"subtotal":10.01,"grand_total":11.26,"small_qty_fee":1.25}}';mime='application/json'
    else:external.append('unexpected_fetch');raise AssertionError('Unexpected source transport')
    response=Response(req,raw,mime);responses.append(response)
    if '/customer/section/load/' in req.url:
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
  session=m.AutomaticFourOverSession(observer,loaded,{'profile_id':m.PROFILE_ID,'max_quotes_per_session':1},check)
  # Native customer-module fixture declares the unrelated address prerequisite complete.
  session.address_ready=True
  native_state=session.state
  async def independent_controller_state():
   observed=await native_state()
   # This fixture isolates customer/table transport, with the independently
   # tested calculator and TAT prerequisite already ready. The source-native
   # registration/quantity/TAT sequence runs in test_fourover_native_controller_workflow.
   observed['source_controller']=state()['source_controller']
   observed['source_ui']=state()['source_ui']
   return observed
  session.state=independent_controller_state
  async def focused_quote_action(selection):
   # This fixture isolates transport/controller behavior with independent UI
   # disclosure stubbed. Exact native model/step handoff is covered separately
   # by test_fourover_native_proceed_transition; keep this fixture's assertions.
   return await session.page.evaluate(m.automatic_quote_button_js(selection['attributes']),
    {'source':session.source,'selection':selection,'click':True})
  session.ordinary_quote_action=focused_quote_action
  # The session installs its own page route; context fallback NEVER accesses a network.
  async def deny(route):
   if route.request.url=='https://4over.com/static/version42/frontend/FourOver/default/en_US/Magento_Customer/js/customer-data.js':
    await route.fulfill(status=200,content_type='application/javascript',body=public_module);return
   external.append('unexpected_context_route');await route.abort()
  await context.route('**/*',deny)
  result=await session.run();ui=await page.evaluate('window.fixture')
  assert result['status']=='matched_current_source_quote',result
  assert result['verified_count']==1 and result['controlled_requests']['price']==1 and result['controlled_requests']['summary']==1
  assert result['customer_public_module_declared'] is True and result['customer_document_declared'] is False
  assert result['customer_metadata_requests_sent']==1 and ui=={'earlyPrice':True,'customer':True,'clicks':1,'quotes':1},ui
  assert not private_body_reads and not external and not observer.permits and all(r.disposals==1 for r in responses)
  assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(loaded['cursor']) and PRIVATE not in b''.join(store.objects.values()).decode()
  assert len(requests)==4 and result['shipping_requests_sent']==result['cart_order_upload_checkout_requests_sent']==0
  await context.close();await browser.close()
