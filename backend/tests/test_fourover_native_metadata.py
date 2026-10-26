"""Portable native metadata regressions; every browser request is intercepted.

Uses the UUID-isolated loader and optional HERMES_FOUROVER_TEST_OVERLAY from the
existing automatic-profile suite. No task artifacts or saved user profiles.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import hashlib,json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit,urlencode
import pytest
from test_fourover_automatic_profile import load_driver,Store,SOURCE,PRIVATE,SELECTION,state,quote_form

@pytest.fixture
def driver():return load_driver()

class Request:
    def __init__(self,page,url,method='GET',resource='xhr'):
        self.frame=page.main_frame;self.url=url;self.method=method;self.resource_type=resource;self.private_reads=0
    @property
    def post_data(self):self.private_reads+=1;raise AssertionError('Unexpected private request body read')
    async def header_value(self,name):self.private_reads+=1;raise AssertionError('Unexpected private request header read')

class Response:
    def __init__(self,request,raw,mime='application/json',private=False):
        self.request=request;self.url=request.url;self.raw=raw;self.headers={'content-type':mime};self.status=200;self.private=private;self.body_reads=0;self.disposals=0
    async def body(self):
        self.body_reads+=1
        if self.private:raise AssertionError('Private customer response must not be projected')
        return self.raw
    async def dispose(self):self.disposals+=1;assert self.disposals==1

class Route:
    def __init__(self,request):self.request=request;self.forwarded=False;self.aborted=False;self.response=None
    async def fulfill(self,response):assert response.disposals==0;self.forwarded=True;self.response=response
    async def abort(self):self.aborted=True
    async def continue_(self):raise AssertionError('No unhandled transport')
    async def fallback(self):raise AssertionError('No unhandled transport')

class Observer:
    def __init__(self,store):self.store=store;self.pool=None;self.limiter=self;self.permits=set();self.responses=[];self.fetch_count=0
    async def _acquire(self,rate,concurrency,check):await check();token=object();self.permits.add(token);return token
    def release(self,origin,token,*args):self.permits.remove(token)
    async def _fetch(self,route):
        self.fetch_count+=1
        response=Response(route.request,b'[{"packagePrices":[{"qty":250,"price":10.006}],"tierPrices":[]}]');self.responses.append(response);return response
    async def _fulfill(self,route,response):await route.fulfill(response)

def catalog(store,driver,tmp_path):
    checkpoint=tmp_path/'catalog.json';checkpoint.write_text('synthetic current catalog')
    entry={'child_id':'700','attributes':dict(SELECTION['attributes']),'historical_prices':{250:'8.00'},'fields':[]}
    return {'association':{'campaign_id':'repair','product_id':'42','supplier':'4over','source_url':SOURCE},'source_url':SOURCE,
        'job':{'declared_children':[{'child_id':'700','sku':'synthetic'}]},'rows':[],'entries':[entry],'excluded_children':[],
        'declared_domain_count':1,'reference':{'snapshot_id':'a'*32,'checkpoint_sha256':'b'*64,'option_rows_sha256':'c'*64},
        'checkpoint_path':checkpoint,'checkpoint_identity':driver.file_identity(checkpoint)}

def fixture(driver,tmp_path):
    store=Store(tmp_path);cat=catalog(store,driver,tmp_path);path,cursor=driver.load_cursor(store,cat);observer=Observer(store)
    async def scope():pass
    session=driver.AutomaticFourOverSession(observer,{'catalog':cat,'cursor_path':path,'cursor':cursor},{'profile_id':driver.PROFILE_ID,'max_quotes_per_session':1},scope)
    current=state()
    class Page:
        main_frame=object();url=SOURCE
        async def evaluate(self,*args):return deepcopy(current)
    page=Page();session.page=page;session.prefix='https://4over.com/static/version1/frontend/FourOver/default/en_US/';session.deadline=session.session_deadline
    return session,observer,page,current

@pytest.mark.asyncio
async def test_transient_native_child_table_is_delivered_once_without_quote_promotion(driver,tmp_path):
    session,observer,page,current=fixture(driver,tmp_path);current['attributes']['111']['value']=''
    first=Route(Request(page,'https://4over.com/rest/default/V1/product/prices/700'));await session.guard(first)
    duplicate=Route(Request(page,first.request.url));await session.guard(duplicate)
    assert first.forwarded and duplicate.forwarded and observer.fetch_count==1
    assert first.response is duplicate.response and session.tables['700']['rows'][0]['quantity']==250
    assert not session.cursor['receipts'] and session.selection is None and not session.cursor['verified_count']
    meta=json.loads((observer.store.root/session.tables['700']['evidence']['object_path']).read_text())
    assert meta['source_quote_verified'] is False and meta['admission_basis']=='current_declared_child_native_public_table'
    await first.response.dispose()

@pytest.mark.asyncio
@pytest.mark.parametrize('query,allowed',[('_=124',True),('_=abc',False),('quantity=250',False)])
async def test_native_timestamp_alias_shares_unchanged_response_without_second_get(driver,tmp_path,query,allowed):
    session,observer,page,_=fixture(driver,tmp_path)
    first=Route(Request(page,'https://4over.com/rest/default/V1/product/prices/700?_=123'));await session.guard(first)
    other=Route(Request(page,'https://4over.com/rest/default/V1/product/prices/700?'+query));await session.guard(other)
    assert first.forwarded and other.forwarded is allowed and other.aborted is not allowed
    assert observer.fetch_count==1 and first.response.url.endswith('?_=123')
    assert not session.cursor['receipts']
    await first.response.dispose()

@pytest.mark.asyncio
async def test_declared_customer_response_stays_private_and_original(driver,tmp_path):
    session,observer,page,_=fixture(driver,tmp_path);session.customer_declared=True
    async def fetch(route):
        observer.fetch_count+=1;r=Response(route.request,json.dumps({'customer':{'name':PRIVATE}}).encode(),private=True);observer.responses.append(r);return r
    observer._fetch=fetch
    first=Route(Request(page,'https://4over.com/customer/section/load/?sections=customer'))
    duplicate=Route(Request(page,first.request.url));await session.guard(first);await session.guard(duplicate)
    assert first.forwarded and duplicate.forwarded and observer.fetch_count==1 and session.customer_reads==1
    assert first.response is duplicate.response and first.response.body_reads==0
    assert first.request.private_reads==duplicate.request.private_reads==0
    assert PRIVATE not in json.dumps(session.cursor) and not observer.store.objects and not observer.permits
    await first.response.dispose()

@pytest.mark.asyncio
async def test_quote_drain_cannot_cancel_delayed_native_customer_metadata(driver,tmp_path):
    session,observer,page,_=fixture(driver,tmp_path);session.customer_declared=True;entered=asyncio.Event();release=asyncio.Event()
    async def fetch(route):
        observer.fetch_count+=1;entered.set();await release.wait();r=Response(route.request,b'{"customer":{}}',private=True);observer.responses.append(r);return r
    observer._fetch=fetch
    route=Route(Request(page,'https://4over.com/customer/section/load/?sections=customer'));task=asyncio.create_task(session.guard(route));await entered.wait()
    await asyncio.wait_for(session.drain(),.1);assert not task.done() and not task.cancelled()
    release.set();await task;assert route.forwarded and session.failure is None and observer.fetch_count==1
    await route.response.dispose()

@pytest.mark.parametrize('query',['?sections=customer&sections=cart','?action=delete','?sections=../../cart','?force_new_section_timestamp=delete','#private'])
def test_customer_read_query_cannot_expand_to_unknown_action(driver,query):
    assert not driver.native_customer_query(urlsplit('https://4over.com/customer/section/load/'+query))

@pytest.mark.asyncio
async def test_unknown_route_diagnostic_drops_query_and_raw_path(driver,tmp_path):
    session,observer,page,_=fixture(driver,tmp_path)
    for secret in ('FIRST-PRIVATE','SECOND-PRIVATE'):
        request=Request(page,'https://4over.com/unknown/PRIVATE-PATH?token='+secret);route=Route(request);await session.guard(route)
        assert route.aborted and request.private_reads==0
    rows=list(session.other_routes.values());assert len(rows)==1 and rows[0]['count']==2
    assert rows[0]['path_sha256']==hashlib.sha256(b'/unknown/PRIVATE-PATH').hexdigest()
    assert 'PRIVATE' not in json.dumps(rows) and observer.fetch_count==0


@pytest.mark.asyncio
async def test_native_table_customer_metadata_and_ordinary_summary_complete_in_intercepted_chromium(driver,tmp_path):
 playwright=pytest.importorskip('playwright.async_api')
 async_playwright=playwright.async_playwright
 m=driver;store=Store(tmp_path);cat=catalog(store,driver,tmp_path);path,value=driver.load_cursor(store,cat)
 loaded={'catalog':cat,'cursor_path':path,'cursor':value};selection=dict(SELECTION)
 declaration={'*':{'Magento_Customer/js/customer-data':{'sectionLoadUrl':'https://4over.com/customer/section/load/'}}}
 document='''<!doctype html><html><body><main class="column main"><form action="/checkout/cart/add" method="post">
 <select name="super_attribute[111]"><option value="">Choose</option><option value="10">A</option></select>
 <select name="super_attribute[999]"><option value="">Choose</option><option value="20">B</option></select>
 <input type="hidden" name="product" value="1234"><input type="hidden" name="selected_configurable_option" value="700"><input type="hidden" name="run_size" value="250">
 <ul id="quantities"></ul><input type="checkbox" name="job_sample"><input type="checkbox" name="digital_proof">
 <div id="production"><input id="regular" type="radio" name="turnaround_time" value="3" checked><label for="regular">3 Business Days</label></div>
 <div id="money"></div><button id="quote" type="button">PROCEED TO SHIPPING</button></form></main>
 <script type="text/x-magento-init">DECLARATION</script><script>
 window.fixture={earlyPrice:false,customer:false,clicks:0,quotes:0};document.querySelector('form').addEventListener('submit',e=>e.preventDefault());
 const account=new XMLHttpRequest();account.open('GET','/customer/section/load/?sections=customer&force_new_section_timestamp=false&_=123');account.responseType='json';account.onload=()=>{window.fixture.customer=account.response.customer.is_logged_in===true};account.send();
 fetch('/rest/default/V1/product/prices/700').then(r=>r.json()).then(rows=>{
 window.fixture.earlyPrice=true;document.querySelector('[name="super_attribute[111]"]').value='10';document.querySelector('[name="super_attribute[999]"]').value='20';
 const li=document.createElement('li');li.className='runsizes_row';li.innerHTML='<span class="runsize">250</span>';li.onclick=()=>{li.classList.add('selected-qty');document.querySelector('[name="run_size"]').value='250'};document.querySelector('#quantities').append(li);
 });
 document.querySelector('#quote').addEventListener('click',async()=>{
 window.fixture.clicks++;if(!window.fixture.customer||!window.fixture.earlyPrice)return;
 const result=await new Promise((resolve,reject)=>{const r=new XMLHttpRequest();r.open('POST','/print/project/summary/');r.setRequestHeader('Content-Type','application/x-www-form-urlencoded');r.responseType='json';r.onload=()=>resolve(r.response);r.onerror=reject;r.send(QUOTEFORM)});document.querySelector('#money').innerHTML='<div>Subtotal: $10.01</div><div>Total: $11.26</div>';window.fixture.quotes++;
 });</script></body></html>'''.replace('DECLARATION',json.dumps(declaration)).replace('QUOTEFORM',json.dumps(urlencode(quote_form())))
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
  # Customer/price fixture isolates its own dependencies; real address startup is covered separately.
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
  async def deny(route):external.append('unexpected_context_route');await route.abort()
  await context.route('**/*',deny)
  result=await session.run();ui=await page.evaluate('window.fixture')
  assert result['status']=='matched_current_source_quote',result
  assert result['verified_count']==1 and result['controlled_requests']['price']==1 and result['controlled_requests']['summary']==1
  assert result['customer_metadata_requests_sent']==1 and ui=={'earlyPrice':True,'customer':True,'clicks':1,'quotes':1},ui
  assert not private_body_reads and not external and not observer.permits and all(r.disposals==1 for r in responses)
  assert PRIVATE not in json.dumps(result) and PRIVATE not in json.dumps(loaded['cursor']) and PRIVATE not in b''.join(store.objects.values()).decode()
  assert len(requests)==4 and result['shipping_requests_sent']==result['cart_order_upload_checkout_requests_sent']==0
  await context.close();await browser.close()

@pytest.mark.asyncio
@pytest.mark.parametrize('change',['undeclared','post','other-frame'])
async def test_customer_metadata_cannot_become_unproved_or_mutating_transport(driver,tmp_path,change):
    session,observer,page,_=fixture(driver,tmp_path);session.customer_declared=change!='undeclared'
    request=Request(page,'https://4over.com/customer/section/load/?sections=customer',method='POST' if change=='post' else 'GET')
    if change=='other-frame':request.frame=object()
    route=Route(request);await session.guard(route)
    assert route.aborted and not route.forwarded and observer.fetch_count==0 and request.private_reads==0
