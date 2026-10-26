"""Closed historical API plus lower-driver synthetic Chromium safety tests.

Direct _attempt fixtures exercise legacy controls only; they do not authorize any
public historical source replay or claim current live supplier coverage.
"""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest
from crawler.fourover_collected_evidence import load_cache,SOURCE,CHILD,VALUES,SCOPE
from crawler.fourover_native_observation import NativeObserver,Held,PRICE,SUMMARY,attributes_retained,context_retained,parse_summary,request_context,bounded_prices
from fourover_observer_fixture import build,state,summary_form,CAMPAIGN,SNAPSHOT,PRIVATE

STATIC_TEMPLATE='https://4over.com/static/version1/frontend/FourOver/default/en_US/FourOver_Project/template/project.html'


def test_exact_context_projects_product_fields_without_account_address_or_uuid():
    captured=request_context(urlencode(summary_form()),state())
    assert captured['run_size']==500 and captured['sets']==1 and captured['attributes']==VALUES
    assert PRIVATE not in json.dumps(captured)
    assert captured['production']=={'value':'3','label':'3 Business Days'}

@pytest.mark.parametrize('key,value',[
    ('run_size','501'),('custom_run_size','1'),('qty','2'),('product_id','999'),
    ('product_options[super_attribute][211]','999'),('job_sample','1'),('digital_proof','on'),
    ('product_options[turnaround_time]','4'),('shipments[1][jobs][0][uuid]','opaque'),
])
def test_summary_payload_must_retain_exact_source_context(key,value):
    form=summary_form();form[key]=value
    with pytest.raises(Held):request_context(urlencode(form),state())

@pytest.mark.parametrize('mutate',[
    lambda s:s.update(child_id='999'),lambda s:s.update(run_size='1000'),
    lambda s:s['attributes']['211'].update(enabled=False),
    lambda s:s['attributes']['211'].update(choice_enabled=False),
    lambda s:s['services']['digital_proof'].update(checked=True),
    lambda s:s['sets'].update(value='2'),lambda s:s['sets'].update(valid=False),
])
def test_retained_dom_context_rejects_changed_controls(mutate):
    current=state();mutate(current)
    assert not context_retained(current,final=True)

@pytest.mark.parametrize('raw',[
    b'{"totals":{"subtotal":1e999999999,"grand_total":1e999999999}}',
    b'{"totals":{"subtotal":NaN,"grand_total":NaN}}',
    b'{"totals":{"subtotal":12.345,"grand_total":12.345}}',
    b'{"totals":{"subtotal":true,"grand_total":true}}',
    b'{"totals":{"subtotal":12.34,"grand_total":12.34,"postage":2}}',
    b'{"totals":{"subtotal":12.34,"grand_total":12.34,"job_sample":1}}',
    b'{"totals":{"subtotal":12.34,"subtotal":12.34,"grand_total":12.34}}',
    b'{"totals":{"subtotal":12.34,"grand_total":15}}',
])
def test_unbounded_nonfinite_ambiguous_or_unreconciled_money_is_held(raw):
    with pytest.raises(Held,match='native_summary_total_unavailable'):parse_summary(raw)


def html_fixture(env,mode='good'):
    controls=''.join(f'<label for="a{k}">Attribute {k}</label><select id="a{k}" name="super_attribute[{k}]"><option value="">Choose</option><option value="{v}">Choice {k}</option></select>' for k,v in VALUES.items())
    script=r'''
const mode=MODE, values=VALUES;let sent=false;
const bootstrap=new XMLHttpRequest();bootstrap.open('GET',STATIC_TEMPLATE);bootstrap.send();
const selected=()=>Object.entries(values).every(([k,v])=>document.querySelector(`[name="super_attribute[${k}]"]`).value===v);
for(const el of document.querySelectorAll('select'))el.addEventListener('change',()=>{
 if(!selected()||sent)return;sent=true;document.querySelector('[name=selected_configurable_option]').value='160399';
 const go=()=>fetch('/rest/default/V1/product/prices/160399'+(mode==='extra_query'?'?token=PRIVATE':'')).then(r=>r.json()).then(()=>mode==='slow_quantity_ui'?new Promise(resolve=>setTimeout(resolve,150)):undefined).then(()=>{
  document.querySelector('#rows').innerHTML='<li class="runsizes_row"><span class="runsize">500</span></li>';
  document.querySelector('li').onclick=()=>{document.querySelector('li').className='runsizes_row selected';document.querySelector('[name=run_size]').value=mode==='wrong_run'?'501':'500';};
 }).catch(()=>{});go();if(mode==='duplicate_price')go();
});
document.querySelector('button').onclick=()=>{
 document.querySelector('#setbox').hidden=false;
 if(mode==='sets2')document.querySelector('#sets').value='2';
 if(mode==='proof')document.querySelector('[name=digital_proof]').checked=true;
 const form=new URLSearchParams({product_id:'123',run_size:'500',custom_run_size:'0',qty:'1','product_options[turnaround_time]':'3',job_sample:'0',digital_proof:'0',form_key:'PRIVATE-ACCOUNT-FORM-KEY-NEVER-PERSIST','shipments[0][jobs][0][uuid]':'PRIVATE-ACCOUNT-FORM-KEY-NEVER-PERSIST'});
 for(const[k,v]of Object.entries(values))form.append(`product_options[super_attribute][${k}]`,v);
 const go=()=>fetch('/print/project/summary/',{method:'POST',body:form}).then(r=>r.json()).then(data=>{
  document.querySelector('#money').innerHTML=mode==='missing_total'?'':`<span>Subtotal: $${data.totals.subtotal.toFixed(2)}</span><span>Total: $${data.totals.grand_total.toFixed(2)}</span>`;
  if(mode==='changed_production')document.querySelector('label[for=production]').textContent='5 Business Days';
 }).catch(()=>{});go();if(mode==='duplicate_summary')go();
 fetch('/print/shipment/rates/',{method:'POST',body:'private=PRIVATE'}).catch(()=>{});
 if(mode==='popup')window.open('/checkout/cart/add/','_blank');
 if(mode==='cart')fetch('/checkout/cart/add/',{method:'POST',body:'private=PRIVATE'}).catch(()=>{});
};
'''.replace('MODE',json.dumps(mode)).replace('VALUES',json.dumps(VALUES)).replace('STATIC_TEMPLATE',json.dumps(STATIC_TEMPLATE))
    # Valid current parser grammar, synthetic version. CSP blocks all declared
    # external script loads; only the nonce-marked local fixture controller runs.
    # These declarations must never produce source/DNS traffic in this suite.
    roles=('requirejs/require.js','mage/requirejs/mixins.js','requirejs-config.js')
    declarations=''.join('<script src="https://4over.com/static/version1/frontend/FourOver/default/en_US/'+role+'"></script>' for role in roles)
    return ('<!doctype html><meta http-equiv="Content-Security-Policy" content="script-src \'nonce-local-fixture\'">'+declarations+'<main class="column main">'+controls+
        '<input name="selected_configurable_option" value=""><input name="product" value="123"><input name="run_size" value="">'+
        '<input type="checkbox" name="job_sample"><input type="checkbox" name="digital_proof">'+
        '<input id="production" name="turnaround_time" type="radio" value="3" checked><label for="production">3 Business Days</label>'+
        '<ul id="rows"></ul><button>Proceed to Shipping</button><div id="setbox" hidden><label for="sets">Sets</label><input id="sets" type="number" value="1" min="1" max="200"></div><div id="money"></div>'+
        '</main>'+env.html+'<script nonce="local-fixture">'+script+'</script>')


class Response:
    def __init__(self,body,status=200,content_type='application/json',url=''):
        self.url=url;self.raw=body;self.status=status;self.headers={'content-type':content_type};self.disposed=False
    async def body(self):return self.raw
    async def dispose(self):self.disposed=True


class Limiter:
    def __init__(self):self.active=set();self.grants=0;self.releases=0;self.on_acquire=None
    async def acquire(self,origin,rate,concurrency,check):
        await check();self.grants+=1;token=str(self.grants);self.active.add(token)
        if self.on_acquire:await self.on_acquire(self.grants)
        return token
    def release(self,*args):self.active.remove(args[1]);self.releases+=1


@pytest.fixture
async def native(tmp_path,monkeypatch):
    import crawler.fourover_native_observation as module
    from playwright.async_api import async_playwright, Route
    env=build(tmp_path)
    runtime=await async_playwright().start()
    if not Path(runtime.chromium.executable_path).exists():
        env.store.engine.dispose();await runtime.stop();pytest.skip('Local Chromium unavailable')
    browser=await runtime.chromium.launch(headless=True)
    pages=[]
    class Pool:
        @asynccontextmanager
        async def page(self,origin,headless):
            assert origin=='https://4over.com' and headless is False
            context=await browser.new_context(service_workers='block');page=await context.new_page();pages.append(page)
            try:yield page
            finally:await context.close()
    @asynccontextmanager
    async def slot(*args,**kwargs):yield
    monkeypatch.setattr(module,'validate_url',AsyncMock())
    monkeypatch.setattr(module,'browser_slot',slot)
    continued=[]
    async def no_external_continue(route,**kwargs):
        continued.append(True)
        await route.abort()
        raise AssertionError('Unmocked browser transport is forbidden in this fixture')
    monkeypatch.setattr(Route,'continue_',no_external_continue)
    observer=NativeObserver(env.store,Pool());observer.limiter=Limiter()
    sent=[];responses=[];options={'mode':'good'}
    async def fake_fetch(route):
        # Every URL below is intercepted. Calling actual route.fetch is forbidden in this suite.
        url=route.request.url;sent.append(url)
        if url==SOURCE:body=html_fixture(env,options['mode']).encode();status=200;kind='text/html'
        elif url==STATIC_TEMPLATE:body=b'<div>Local synthetic public project template</div>';status=200;kind='text/html'
        elif url.endswith(PRICE):
            amount='13.34' if options['mode']=='changed_price' else '12.340002' if options['mode']=='exact_price_drift' else '12.340001'
            body=('[{"packagePrices":[{"qty":500,"price":'+amount+'}],"tierPrices":[],"accountToken":"'+PRIVATE+'"}]').encode();status=401 if options['mode']=='auth' else 429 if options['mode']=='rate' else 200;kind='application/json'
        elif url.endswith(SUMMARY):
            body=json.dumps({'totals':{'subtotal':12.34,'grand_total':12.34},'accountToken':PRIVATE}).encode();status=200;kind='application/json'
        else:raise AssertionError('Forbidden source request forwarded: '+url)
        response=Response(body,status,kind,url);responses.append(response);return response
    async def fulfill(route,response):await route.fulfill(status=response.status,headers=response.headers,body=response.raw)
    observer._fetch=fake_fetch;observer._fulfill=fulfill
    async def observe():return await observer.observe(env.manager,CAMPAIGN,SNAPSHOT,env.checkpoint,env.rows)
    async def attempt():
        # The public q500 entry is permanently closed. Exercise only the lower
        # driver's safety on this temporary synthetic corpus, never the API.
        loaded=load_cache(env.store,SNAPSHOT,env.checkpoint,env.rows)
        async def check():
            try:module.mapped_product(env.manager,CAMPAIGN)
            except (ValueError,FileNotFoundError):raise Held('active_source_scope_changed') from None
        return await observer._attempt(loaded,check)
    yield type('Fixture',(),{'env':env,'observer':observer,'sent':sent,'responses':responses,'pages':pages,'options':options,'run':staticmethod(observe),'attempt':staticmethod(attempt)})()
    assert not observer.limiter.active
    assert observer.limiter.grants==observer.limiter.releases
    assert all(r.disposed for r in responses)
    assert not continued
    assert all(page.is_closed() for page in pages)
    await browser.close();await runtime.stop();env.store.engine.dispose()


@pytest.mark.parametrize('mode',['good','slow_quantity_ui'])
@pytest.mark.asyncio
async def test_historical_public_context_is_closed_before_any_source_transport(native,mode):
    native.options['mode']=mode
    before=native.env.manager._path(CAMPAIGN).read_bytes()
    for _ in range(2):
        result=await native.run()
        assert result['status']=='held' and result['reason_code']=='prior_native_context_historical_only',result
        assert result['supplier_requests_this_call']==0 and not result['price_fresh']
        assert not result['final_eligible'] and not result['checkout_eligible']
    assert native.sent==[] and native.pages==[]
    assert native.observer.limiter.grants==native.observer.limiter.releases==0
    assert native.env.manager._path(CAMPAIGN).read_bytes()==before


@pytest.mark.parametrize('mode',['good','slow_quantity_ui'])
@pytest.mark.asyncio
async def test_legacy_lower_driver_synthetic_controls_payable_context_no_shipping_or_account(native,mode):
    native.options['mode']=mode
    before=native.env.manager._path(CAMPAIGN).read_bytes()
    result=await native.attempt()
    assert result['status']=='matched_one_native_context',result
    assert result['controlled_requests']=={'document':1,'price':1,'summary':1}
    assert result['supplier_requests_this_call']==4 and result['native_product_subtotal_verified']
    assert result['supplier_product_total_excluding_shipping']=='12.34'
    assert result['production']=={'value':'3','label':'3 Business Days'}
    assert result['blocked_request_counts']['shipping']==1
    assert result['shipping_requests_sent']==result['cart_order_upload_checkout_requests_sent']==0
    assert PRIVATE not in json.dumps(result)
    assert native.env.manager._path(CAMPAIGN).read_bytes()==before
    previous=list(native.sent)
    closed=await native.run()
    assert closed['reason_code']=='prior_native_context_historical_only'
    assert closed['supplier_requests_this_call']==0 and native.sent==previous

@pytest.mark.parametrize('mode,reason',[
    ('duplicate_price','unexpected_native_request'),('duplicate_summary','unexpected_native_request'),
    ('extra_query','unexpected_native_request'),('sets2','native_summary_context_missing'),
    ('proof','native_summary_context_missing'),('cart','unexpected_native_request'),('popup','unexpected_native_popup'),
    ('changed_price','source_table_or_payable_price_changed'),('exact_price_drift','source_table_or_payable_price_changed'),('changed_production','native_production_not_retained'),
    ('auth','source_auth_required'),('rate','source_rate_or_availability_stop'),
])
@pytest.mark.asyncio
async def test_native_bad_context_duplicate_requests_and_source_failures_hold_without_retry(native,mode,reason):
    native.options['mode']=mode
    result=await native.attempt()
    assert result['status']=='held' and result['reason_code']==reason,result
    assert not result['native_product_subtotal_verified']
    assert result['controlled_requests']['price']<=1 and result['controlled_requests']['summary']<=1
    assert not any('/shipment/' in url or '/cart/' in url for url in native.sent)
    previous=list(native.sent);again=await native.run()
    assert again['supplier_requests_this_call']==0 and native.sent==previous
    assert PRIVATE not in json.dumps(result)

@pytest.mark.asyncio
async def test_scope_change_while_waiting_for_price_permit_sends_no_price(native):
    async def changed(number):
        if number==3:native.env.manager.tasks[CAMPAIGN]=object()
    native.observer.limiter.on_acquire=changed
    result=await native.attempt()
    assert result['reason_code']=='active_source_scope_changed' and native.sent==[SOURCE,STATIC_TEMPLATE]
    native.env.manager.tasks.clear()

@pytest.mark.asyncio
async def test_legacy_lower_driver_cancellation_releases_permit_and_public_context_stays_closed(native):
    waiting=asyncio.Event();release=asyncio.Event()
    original=native.observer._fetch
    async def hold(route):
        if route.request.url.endswith(PRICE):waiting.set();await release.wait()
        return await original(route)
    native.observer._fetch=hold
    task=asyncio.create_task(native.attempt())
    await asyncio.wait_for(waiting.wait(),10)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):await task
    again=await native.run()
    assert again['reason_code']=='prior_native_context_historical_only'
    assert again['supplier_requests_this_call']==0


@pytest.mark.parametrize('grant,reason',[(3,'native_price_context_changed'),(4,'native_summary_context_missing')])
@pytest.mark.asyncio
async def test_retained_dom_change_while_paced_is_rechecked_before_transport(native,grant,reason):
    async def changed(number):
        if number==grant:
            page=native.pages[-1]
            if grant==3:await page.evaluate("document.querySelector('[name=selected_configurable_option]').value='999'")
            else:await page.evaluate("document.querySelector('#sets').value='2'")
    native.observer.limiter.on_acquire=changed
    result=await native.attempt()
    assert result['status']=='held' and result['reason_code']==reason,result
    assert len(native.sent)==grant-1


@pytest.mark.asyncio
async def test_duplicate_native_requests_while_permit_waits_cannot_forward_reserved_price(native):
    native.options['mode']='duplicate_price'
    async def paced(number):
        if number==3:await asyncio.sleep(.2)
    native.observer.limiter.on_acquire=paced
    result=await native.attempt()
    assert result['reason_code']=='unexpected_native_request'
    assert native.sent==[SOURCE,STATIC_TEMPLATE] and result['controlled_requests']['price']==0


@pytest.mark.asyncio
async def test_cancellation_during_uninterruptible_permit_grant_releases_late_token(native):
    entered=asyncio.Event();release=asyncio.Event()
    async def paced(number):
        if number==3:entered.set();await release.wait()
    native.observer.limiter.on_acquire=paced
    task=asyncio.create_task(native.attempt())
    await asyncio.wait_for(entered.wait(),10)
    task.cancel();release.set()
    with pytest.raises(asyncio.CancelledError):await task
    assert not native.observer.limiter.active
    assert native.sent==[SOURCE,STATIC_TEMPLATE]
    again=await native.run()
    assert again['reason_code']=='prior_native_context_historical_only'


@pytest.mark.parametrize('field,value',[('price','1e999999999'),('qty','1e999999999'),('price','1e-999999999'),('price','NaN'),('price','true'),('qty','true')])
def test_native_table_numeric_expansion_is_bounded_before_shared_parser_formats(field,value):
    body=('{"qty":500,"price":12.340001}').replace('"'+field+'":'+('500' if field=='qty' else '12.340001'),'"'+field+'":'+value)
    with pytest.raises(Held):bounded_prices(('[{"packagePrices":['+body+'],"tierPrices":[]}]').encode())
