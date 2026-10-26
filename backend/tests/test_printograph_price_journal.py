"""Synthetic projections + local TLS host-mapped Chromium only; no supplier calls."""
import asyncio
import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
import pytest_asyncio

FILE = Path(__file__).parents[1] / 'crawler/printograph_price_journal.py'
SPEC = importlib.util.spec_from_file_location('crawler.printograph_price_journal', FILE)
m = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = m
SPEC.loader.exec_module(m)

BASE = 'https://www.printograph.com/service/rest/v1/products'
LOOKUP = BASE + '?productType=39&turnaround=1&shape=15&size=101&orientation=1&paper=1&color=3'
PRICE = BASE + '/900/prices'
OPTIONS = BASE + '/900/options?requestValues=true&eddm=false'
FEE = BASE + '/900/options/55/prices'


def capture(**updates):
    selected = dict(shape='15',size='101',orientation='1',paper='1',color='3',coating='5',
                    spotCoating='',paperFinish='1',proofType='31',manualProofType='1',designServices='42',
                    fileDelivery='5',quantity='50',productId='900',productionTime='1')
    base = updates.pop('base','10.00'); fee = updates.pop('fee',None)
    selected.update(updates)
    applied = {'productPrice':base}
    total = base
    if selected['spotCoating']:
        applied['spotCoating'] = {selected['spotCoating']:fee or '2.01'}
        total = format(m.Decimal(base)+m.Decimal(fee or '2.01'),'.2f')
    return {'observed':True,'settled':True,'capture_kind':'controller',
            'source_contract':dict(m.CONTRACT),'source_url':m.SOURCE_URL,'product_type':'39','store_type':'broker',
            'currency':'USD','field_specs':list(m.BASE_FIELDS['39']),'field_options':list(m.OPTION_FIELDS['39']),
            'userSelectedOptions':selected,'appliedPrices':applied,'other_context':{k:False for k in m.NORMAL_CONTEXT_FLAGS},
            'coupon':{'observed':True,'mode':'source_state','coupon_object_observed':True,'coupon_object_key_count':0,
                      'parameters':{},'is_coupon_spec_changed':False},'controller_total':total,'displayed_total':total}


def journal(clock=None):
    j=m.PriceJournal('a'*32,**({'clock':clock} if clock else {}))
    j.bind_document('frame','loader',m.SOURCE_URL,m.CONTRACT)
    return j


def begin(j,**after):
    before=capture(color='1',productId='899',**after)
    return j.begin_epoch(before,0,'color','3')


def body(items):
    return json.dumps({'items':items}).encode()


def deliver(j,identifier,url,items,**flags):
    assert j.request(identifier,'frame','loader',url,'GET','XHR')
    j.response(identifier,status=200,mime='application/json',response_url=url,
               timing={'requestTime':1,'receiveHeadersEnd':2},**flags)
    raw=body(items)
    if j.loading_finished(identifier,len(raw)+300):j.accept_body(identifier,raw)


def seed(j,*,spot=False):
    ep=begin(j,spotCoating='3' if spot else '')
    deliver(j,'lookup',LOOKUP,[{'id':900}])
    if spot:deliver(j,'options',OPTIONS,[{'id':55,'type':{'id':3}}])
    deliver(j,'base',PRICE,[{'quantity':50,'markupPrice':'10.00'},{'quantity':100,'markupPrice':'16.00'}])
    if spot:deliver(j,'fee',FEE,[{'quantity':50,'markupPrice':'2.01'},{'quantity':100,'markupPrice':'3.01'}])
    return ep


@pytest.mark.parametrize('url',[
    PRICE.replace('https:','http:'),PRICE.replace('www.printograph.com','evil.invalid'),
    PRICE.replace('www.printograph.com','www.printograph.com.evil.invalid'),
    PRICE.replace('www.printograph.com','user:PRIVATE@www.printograph.com'),
    PRICE.replace('www.printograph.com','www.printograph.com:443'),PRICE+'#PRIVATE',PRICE+'?token=PRIVATE',
    PRICE+'?priceGroup=PRIVATE',PRICE+'?designService=true',PRICE+'?itemId=PRIVATE&cid=PRIVATE',
    FEE+'?quantities=100&quantities=200',FEE+'?quantities=1,2',FEE+'?%71uantities=100',
    PRICE.replace('/900/','/%39%30%30/'),PRICE.replace('/900/','/../900/'),PRICE+'\n',
    LOOKUP+'&account=PRIVATE',LOOKUP.replace('productType=39','productType=2')])
def test_exact_urls_only_and_errors_do_not_contain_values(url):
    with pytest.raises(m.JournalError) as error:m.project_request(url)
    assert 'PRIVATE' not in str(error.value) and 'http' not in str(error.value)


def test_four_get_classes_project_numeric_only():
    assert m.project_request(LOOKUP+'&').kind=='product_identity'
    assert m.project_request(OPTIONS).kind=='option_identity'
    assert m.project_request(PRICE).public()=={'kind':'base_price','product_id':'900'}
    assert m.project_request(FEE+'?quantities=100').quantity_filter=='100'
    for method,resource in [('POST','XHR'),('GET','Document'),('PATCH','Fetch')]:
        with pytest.raises(m.JournalError):m.project_request(PRICE,method,resource)


@pytest.mark.parametrize('items',[
    [],[{'quantity':0,'markupPrice':'10'}],[{'quantity':True,'markupPrice':'10'}],
    [{'quantity':50,'markupPrice':'0'}],[{'quantity':50,'markupPrice':'-10'}],
    [{'quantity':50,'markupPrice':10}],[{'quantity':50,'markupPrice':'1e10'}],
    [{'quantity':50,'markupPrice':'NaN'}],[{'quantity':50,'markupPrice':'1,00'}],
    [{'quantity':50,'markupPrice':'1000000000'}],[{'quantity':50,'markupPrice':'0.'+'1'*9}],
    [{'quantity':50,'markupPrice':'10'},{'quantity':50,'markupPrice':'10'}]])
def test_invalid_and_ambiguous_price_rows_fail_closed(items):
    with pytest.raises(m.JournalError):m.project_body(m.project_request(PRICE),body(items))


def test_numeric_projection_preserves_source_rounding_and_drops_private_fields():
    raw=body([{'quantity':50,'markupPrice':'1,234.565','listPrice':'PRIVATE-RETAIL','account':'PRIVATE-ACCOUNT'}])
    value=m.project_body(m.project_request(PRICE),raw)
    assert value['rows']['50']=={'markup_price':'1234.565','source_cents':'1234.57'}
    assert 'PRIVATE' not in json.dumps(value)
    assert m.project_body(m.project_request(FEE),body([{'quantity':50,'markupPrice':'0'}]))['rows']['50']['source_cents']=='0.00'
    for raw in [b'{"items":[],"items":[]}',b'{"items":[{"quantity":50,"markupPrice":"10"}],"x":NaN}',b'{}',b'x'*(m.MAX_BODY+1)]:
        with pytest.raises(m.JournalError):m.project_body(m.project_request(PRICE),raw)


def test_scope_requires_current_broker_pair_complete_context_and_normal_flags():
    for mutate in [lambda c:c['source_contract'].update(common_sha256='f'*64),
                   lambda c:c.update(source_url=m.SOURCE_URL+'?private'),lambda c:c.update(store_type='retail'),
                   lambda c:c['other_context'].update(has_price_group=True),
                   lambda c:c['userSelectedOptions'].update(proofType='101',fileDelivery='9')]:
        c=capture();mutate(c)
        with pytest.raises(m.JournalError):m.project_context(c)
    before=capture(color='',quantity='0',productId='')
    assert m.project_context(before,complete=False)['selected']['color']==''
    with pytest.raises(m.JournalError):m.project_context(before)


def test_fresh_unfiltered_tables_reuse_exact_rows_with_original_TTL_and_fee_context():
    current=[100.0];j=journal(lambda:current[0]);ep=seed(j,spot=True)
    first=j.finalize_epoch(ep,capture(spotCoating='3'),1)
    assert first['status']=='matched_fresh_observed_components'
    assert [x['source_cents'] for x in first['components']]==['10.00','2.01']
    current[0]=110
    ep=j.begin_epoch(capture(spotCoating='3'),1,'quantity','100')
    second=j.finalize_epoch(ep,capture(quantity='100',base='16.00',spotCoating='3',fee='3.01'),2)
    assert second['status']=='matched_fresh_observed_components'
    assert all(x['status']=='reused_fresh_observed_table' and x['ttl_remaining_seconds']==290
               and x['new_network_request_for_current_selection'] is False for x in second['components'])
    assert [x['source_cents'] for x in second['components']]==['16.00','3.01']
    assert len(j.snapshot()['events'])==4
    assert all(second[k] is False for k in ['source_data_finalized','fresh_source_verified','woocommerce_import_ready','checkout_verified'])
    current[0]=401
    ep=j.begin_epoch(capture(quantity='100',base='16.00',spotCoating='3',fee='3.01'),2,'quantity','50')
    expired=j.finalize_epoch(ep,capture(spotCoating='3'),3)
    assert expired['status']=='held' and expired['reasons']==['product_selection_mapping_missing']


@pytest.mark.parametrize('change', ['color','production','fees','loader','bundle','missing_row','price_mismatch'])
def test_same_epoch_or_equal_amount_does_not_replace_complete_context(change):
    j=journal();ep=seed(j);assert j.finalize_epoch(ep,capture(),1)['status']=='matched_fresh_observed_components'
    ep=j.begin_epoch(capture(),1,'quantity','100')
    c=capture(quantity='100',base='16.00')
    if change=='color':c['userSelectedOptions']['color']='2'
    elif change=='production':c['userSelectedOptions']['productionTime']='3'
    elif change=='fees':c=capture(quantity='100',base='16.00',spotCoating='3')
    elif change=='loader':j.invalidate('document_changed')
    elif change=='bundle':c['source_contract']['common_sha256']='0'*64
    elif change=='missing_row':c['userSelectedOptions']['quantity']='250'
    else:c=capture(quantity='100',base='16.01')
    assert j.finalize_epoch(ep,c,2)['status']=='held'


def test_filtered_fee_table_cannot_cross_quantities_even_with_extra_row():
    j=journal();ep=begin(j,spotCoating='3')
    deliver(j,'lookup',LOOKUP,[{'id':900}]);deliver(j,'opts',OPTIONS,[{'id':55,'type':{'id':3}}])
    deliver(j,'base',PRICE,[{'quantity':50,'markupPrice':'10'},{'quantity':100,'markupPrice':'16'}])
    deliver(j,'fee',FEE+'?quantities=50',[{'quantity':50,'markupPrice':'2.01'},{'quantity':100,'markupPrice':'3.01'}])
    assert j.finalize_epoch(ep,capture(spotCoating='3'),1)['status']=='matched_fresh_observed_components'
    ep=j.begin_epoch(capture(spotCoating='3'),1,'quantity','100')
    result=j.finalize_epoch(ep,capture(quantity='100',base='16',spotCoating='3',fee='3.01'),2)
    assert result['status']=='held' and 'fresh_exact_quantity_missing' in result['reasons']


def test_late_old_base_conflicts_inflight_and_native_memory_without_journal_hold():
    j=journal();ep=begin(j)
    deliver(j,'wrong',LOOKUP.replace('color=3','color=2'),[{'id':900}])
    deliver(j,'base',PRICE,[{'quantity':50,'markupPrice':'10'}])
    assert j.finalize_epoch(ep,capture(),1)['reasons']==['product_selection_mapping_missing']
    j=journal();ep=seed(j)
    deliver(j,'conflict',PRICE,[{'quantity':50,'markupPrice':'11'}])
    assert 'fresh_tables_conflict' in j.finalize_epoch(ep,capture(),1)['reasons']
    j=journal();ep=seed(j);assert j.request('pending','frame','loader',PRICE,'GET','XHR')
    assert 'native_requests_inflight' in j.finalize_epoch(ep,capture(),1)['reasons']
    j=journal();ep=begin(j)
    assert j.finalize_epoch(ep,capture(),1)['status']=='held'


@pytest.mark.parametrize('flag',[{'from_disk':True},{'from_service_worker':True},{'from_prefetch':True}])
def test_cached_responses_are_never_fresh(flag):
    j=journal();ep=begin(j)
    deliver(j,'lookup',LOOKUP,[{'id':900}]);deliver(j,'cached',PRICE,[{'quantity':50,'markupPrice':'10'}],**flag)
    assert j.finalize_epoch(ep,capture(),1)['status']=='held'
    assert j.snapshot()['events'][-1]['reason']=='cache_only'


def test_private_atomic_persistence_contains_no_raw_source_or_error_values(tmp_path):
    j=journal();ep=seed(j);j.finalize_epoch(ep,capture(),1)
    assert j.request('private','frame','loader',PRICE+'?itemId=PRIVATE-ACCOUNT','GET','XHR') is False
    record=j.persist(tmp_path)
    saved=tmp_path/'printograph-price-journals'/record['file'];data=saved.read_bytes()
    assert stat_mode(saved)==0o600 and stat_mode(saved.parent)==0o700
    assert 'PRIVATE' not in data.decode() and 'Authorization' not in data.decode()
    assert b'/service/rest/' not in data and b'www.printograph.com/products/special-shapes/order' in data
    assert hashlib.sha256(data).hexdigest()==record['sha256']
    saved.chmod(0o644)
    with pytest.raises(m.JournalError):j.persist(tmp_path)


def stat_mode(path):return path.stat().st_mode & 0o777


class LocalHandler(BaseHTTPRequestHandler):
    protocol_version='HTTP/1.1'
    def log_message(self,*_):pass
    def do_GET(self):
        self.server.seen.append(self.path)
        path=self.path.split('?')[0]
        if path=='/products/special-shapes/order':
            raw=b'<!doctype html><main id="order-form">Local synthetic product only</main>';content='text/html'
        elif path=='/service/rest/v1/products':raw=body([{'id':900}]);content='application/json'
        elif path.endswith('/options'):raw=body([{'id':55,'type':{'id':3}}]);content='application/json'
        elif path.endswith('/options/55/prices'):raw=body([{'quantity':50,'markupPrice':'2.01'},{'quantity':100,'markupPrice':'3.01'}]);content='application/json'
        elif path.endswith('/900/prices'):raw=body([{'quantity':50,'markupPrice':'10.00'},{'quantity':100,'markupPrice':'16.00'}]);content='application/json'
        elif path.endswith('/901/prices') or path.endswith('/902/prices'):
            raw=json.dumps({'items':[{'quantity':50,'markupPrice':'10'}],'padding':'x'*(m.MAX_BODY*3)}).encode();content='application/json'
        elif path.endswith('/903/prices'):
            raw=b'{"items":[{"quantity":50,"markupPrice":"10"}],"note":"PRIVATE-RESPONSE"}';content='application/json'
        else:raw=b'not found';content='text/plain'
        self.send_response(200);self.send_header('Content-Type',content)
        self.send_header('Cache-Control','public,max-age=600' if path.endswith('/900/prices') else 'no-store')
        if path.endswith('/902/prices'):
            raw=gzip.compress(raw);self.send_header('Content-Encoding','gzip')
        self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)


@pytest_asyncio.fixture
async def browser_page(tmp_path):
    from playwright.async_api import async_playwright
    certificate=tmp_path/'local.crt';key=tmp_path/'local.key'
    subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-keyout',str(key),'-out',str(certificate),
                    '-days','1','-subj','/CN=www.printograph.com','-addext','subjectAltName=DNS:www.printograph.com'],
                   check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    public_pem=subprocess.run(['openssl','x509','-in',str(certificate),'-pubkey','-noout'],check=True,stdout=subprocess.PIPE).stdout
    public_der=subprocess.run(['openssl','pkey','-pubin','-outform','DER'],input=public_pem,check=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL).stdout
    spki=m.base64.b64encode(hashlib.sha256(public_der).digest()).decode()
    server=ThreadingHTTPServer(('127.0.0.1',0),LocalHandler);server.seen=[]
    context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(certificate,key)
    server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:
        async with async_playwright() as p:
            if not Path(p.chromium.executable_path).exists():pytest.skip('Local Chromium unavailable')
            browser=await p.chromium.launch(headless=True,args=['--no-proxy-server','--ignore-certificate-errors-spki-list='+spki,
                f'--host-resolver-rules=MAP www.printograph.com 127.0.0.1:{server.server_port}, MAP * ~NOTFOUND'])
            ctx=await browser.new_context(service_workers='block')
            page=await ctx.new_page();await page.goto(m.SOURCE_URL,wait_until='load')
            try:yield page,server
            finally:await ctx.close();await browser.close()
    finally:server.shutdown();server.server_close();thread.join(timeout=2)


async def native_get(page,url):
    # The synthetic local source calls fetch; the observer never calls this helper.
    await page.evaluate('(url)=>fetch(url).then(r=>r.json())',url)
    await asyncio.sleep(.05)


@pytest.mark.asyncio
async def test_real_owned_page_observer_adds_no_HTTP_and_reuses_native_table(browser_page):
    page,server=browser_page;j=m.PriceJournal('b'*32)
    before=list(server.seen);observer=m.OwnedPageCDPObserver(page,j)
    await observer.attach(m.CONTRACT);assert server.seen==before
    ep=j.begin_epoch(capture(color='1',productId='899'),0,'color','3')
    try:
        for url in [LOOKUP,PRICE]:await native_get(page,url)
        await observer.drain()
        result=j.finalize_epoch(ep,capture(),1)
        assert result['status']=='matched_fresh_observed_components',j.snapshot()
        assert server.seen==before+[LOOKUP.split('.com')[1],PRICE.split('.com')[1]]
        ep=j.begin_epoch(capture(),1,'quantity','100')
        reuse=j.finalize_epoch(ep,capture(quantity='100',base='16'),2)
        assert reuse['components'][0]['status']=='reused_fresh_observed_table'
        assert server.seen==before+[LOOKUP.split('.com')[1],PRICE.split('.com')[1]]
    finally:await observer.close()


@pytest.mark.asyncio
async def test_real_browser_cache_is_held_and_does_not_cause_source_refetch(browser_page):
    page,server=browser_page;j=m.PriceJournal('c'*32);observer=m.OwnedPageCDPObserver(page,j)
    await observer.attach(m.CONTRACT);ep=j.begin_epoch(capture(color='1',productId='899'),0,'color','3')
    try:
        for url in [LOOKUP,PRICE,PRICE]:await native_get(page,url)
        await observer.drain()
        assert server.seen.count(PRICE.split('.com')[1])==1
        assert any(r.cached or r.reason in {'cache_only','network_transfer_unproven'} for r in j._requests.values())
        # Fresh original table remains usable; the cached event never creates another fresh table.
        assert len([r for r in j._requests.values() if r.spec.kind=='base_price' and j._fresh(r)])==1
        assert j.finalize_epoch(ep,capture(),1)['status']=='matched_fresh_observed_components'
    finally:await observer.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('product',['901','902'])
async def test_real_uncompressed_and_compressed_decoded_bounds_fail_closed(browser_page,product):
    page,server=browser_page;j=m.PriceJournal('d'*32);observer=m.OwnedPageCDPObserver(page,j)
    await observer.attach(m.CONTRACT);ep=j.begin_epoch(capture(color='1',productId='899'),0,'color','3')
    url=BASE+'/'+product+'/prices'
    try:
        await native_get(page,url);await observer.drain()
        records=list(j._requests.values());assert len(records)==1
        assert records[0].reason == 'body_unavailable'  # Chromium evicts the oversized decoded resource before retrieval.
        assert not records[0].projection and records[0].body_bytes==0
        assert j.finalize_epoch(ep,capture(),1)['status']=='held'
        assert server.seen.count(url.split('.com')[1])==1
    finally:await observer.close()


def test_option_mapping_conflict_and_source_failure_cannot_reuse_old_evidence():
    j=journal();ep=seed(j,spot=True)
    deliver(j,'other-opts',OPTIONS,[{'id':56,'type':{'id':3}}])
    assert j.finalize_epoch(ep,capture(spotCoating='3'),1)['reasons']==['option_identity_conflict']
    j=journal();ep=seed(j);j.finalize_epoch(ep,capture(),1)
    ep=j.begin_epoch(capture(),1,'quantity','100')
    assert j.request('new-failure','frame','loader',PRICE,'GET','XHR')
    j.response('new-failure',status=401,mime='application/json',response_url=PRICE)
    assert 'current_component_response_failed' in j.finalize_epoch(ep,capture(quantity='100',base='16'),2)['reasons']


def test_redirect_or_response_identity_drift_prevents_body_read():
    j=journal();begin(j)
    assert j.request('same','frame','loader',PRICE,'GET','XHR')
    assert not j.request('same','frame','loader','https://www.printograph.com/service/rest/v1/users/self','GET','XHR',redirected=True)
    # The redirected target may be outside the classifier; response URL must still be checked.
    j.response('same',status=200,mime='application/json',response_url='https://www.printograph.com/service/rest/v1/users/self',
               timing={'requestTime':1,'receiveHeadersEnd':2})
    assert j.loading_finished('same',100) is False
    assert j._requests['same'].reason=='response_identity_mismatch'
    j=journal();begin(j);j.request('same','frame','loader',PRICE,'GET','XHR')
    j.request('same','frame','loader',PRICE,'GET','XHR',redirected=True)
    assert j._requests['same'].reason=='redirected_response'


def test_eight_epoch_budget_never_silently_evicts_fresh_tables_or_records():
    j=journal();ep=seed(j);j.finalize_epoch(ep,capture(),1)
    for rev in range(1,m.MAX_EPOCHS):
        before=capture(quantity='50' if rev%2 else '100',base='10' if rev%2 else '16')
        target='100' if rev%2 else '50'
        ep=j.begin_epoch(before,rev,'quantity',target)
        result=j.finalize_epoch(ep,capture(quantity=target,base='16' if target=='100' else '10'),rev+1)
        assert result['status']=='matched_fresh_observed_components'
        assert result['components'][0]['originating_epoch']==1
    assert len(j._epochs)==m.MAX_EPOCHS and len(j._mappings)==1
    assert len(j.snapshot()['epochs'])==m.MAX_EPOCHS
    with pytest.raises(m.JournalError,match='epoch_budget_exceeded'):
        j.begin_epoch(capture(quantity='100',base='16'),m.MAX_EPOCHS,'quantity','50')
    assert len(j._epochs)==m.MAX_EPOCHS and len(j._requests)==2
    j=journal();ep=begin(j)
    for index in range(m.MAX_REQUESTS):assert j.request(str(index),'frame','loader',PRICE,'GET','XHR')
    assert not j.request('overflow','frame','loader',PRICE,'GET','XHR')
    assert len(j._requests)==m.MAX_REQUESTS
    assert 'request_budget_exceeded' in j.finalize_epoch(ep,capture(),1)['reasons']


def test_blank_native_chain_retains_partial_epochs_then_binds_only_actual_full_identity():
    j=journal()
    state=capture(shape='',size='',orientation='1',paper='',color='',quantity='0',productId='',base='0')
    for revision,(key,value) in enumerate([('shape','15'),('size','101'),('orientation','2'),('paper','1')]):
        ep=j.begin_epoch(state,revision,key,value)
        state=copy.deepcopy(state);state['userSelectedOptions'][key]=value
        result=j.finalize_epoch(ep,state,revision+1)
        assert result['status']=='held' and not result['components']
        assert not any(r.bound_nonquantity for r in j._requests.values())
    ep=j.begin_epoch(state,4,'color','3')
    final=capture(orientation='2')
    deliver(j,'actual-full-mapping',LOOKUP.replace('orientation=1','orientation=2'),[{'id':900}])
    deliver(j,'actual-table',PRICE,[{'quantity':50,'markupPrice':'10'},{'quantity':100,'markupPrice':'16'}])
    assert j.finalize_epoch(ep,final,5)['status']=='matched_fresh_observed_components'
    ep=j.begin_epoch(final,5,'quantity','100')
    result=j.finalize_epoch(ep,capture(orientation='2',quantity='100',base='16'),6)
    assert result['components'][0]['status']=='reused_fresh_observed_table'
    assert result['components'][0]['originating_epoch']==5
    assert len(j.snapshot()['epochs'])==6 and len(j._requests)==2


@pytest.mark.asyncio
async def test_real_same_document_navigation_invalidates_without_HTTP(browser_page):
    page,server=browser_page;j=m.PriceJournal('e'*32);observer=m.OwnedPageCDPObserver(page,j)
    await observer.attach(m.CONTRACT);ep=j.begin_epoch(capture(color='1',productId='899'),0,'color','3')
    try:
        for url in [LOOKUP,PRICE]:await native_get(page,url)
        await observer.drain();before=list(server.seen)
        await page.evaluate("history.pushState({},'',location.pathname+'?PRIVATE=1')")
        await asyncio.sleep(.03)
        assert j.finalize_epoch(ep,capture(),1)['reasons']==['document_changed']
        assert server.seen==before and 'PRIVATE' not in json.dumps(j.snapshot())
    finally:await observer.close()


@pytest.mark.asyncio
async def test_real_unrelated_page_and_private_endpoint_are_unrecorded(browser_page):
    page,server=browser_page;j=m.PriceJournal('f'*32);observer=m.OwnedPageCDPObserver(page,j)
    await observer.attach(m.CONTRACT);ep=j.begin_epoch(capture(color='1',productId='899'),0,'color','3')
    other=await page.context.new_page()
    try:
        await other.goto(m.SOURCE_URL)
        await native_get(other,PRICE)
        # Same owning page's unrelated HTTP is not projected or read, either.
        await page.evaluate("fetch('/service/rest/v1/users/self').then(r=>r.text())")
        await observer.drain()
        assert not j.snapshot()['events'] and not j._requests
        assert j.finalize_epoch(ep,capture(),1)['status']=='held'
    finally:await other.close();await observer.close()


def test_price_cannot_bind_before_native_identity_body_finished():
    j=journal();ep=begin(j)
    assert j.request('identity','frame','loader',LOOKUP,'GET','XHR')
    j.response('identity',status=200,mime='application/json',response_url=LOOKUP,
               timing={'requestTime':1,'receiveHeadersEnd':2})
    # Price request starts after headers but before the mapping body has completed.
    deliver(j,'interleaved-price',PRICE,[{'quantity':50,'markupPrice':'10'}])
    assert j.loading_finished('identity',400)
    j.accept_body('identity',body([{'id':900}]))
    result=j.finalize_epoch(ep,capture(),1)
    assert result['status']=='held' and result['reasons']==['fresh_exact_quantity_missing']


def test_exact_one_choice_revision_and_request_frame_loader_are_required():
    j=journal();ep=seed(j)
    result=j.finalize_epoch(ep,capture(),2)
    assert result['reasons']==['selection_revision_not_retained']
    j=journal();begin(j)
    for frame,loader in [('other','loader'),('frame','other')]:
        assert j.request('unowned',frame,loader,PRICE,'GET','XHR') is False
    assert not j._requests
    with pytest.raises(m.JournalError):m.project_request(PRICE.replace('/900/','/0900/'))
    with pytest.raises(m.JournalError):m.project_body(m.project_request(PRICE),body([{'quantity':'050','markupPrice':'10'}]))


class GuardedDict(dict):
    def get(self,key,*args):
        assert key not in {'headers','postData','requestHeaders','cookies','initiator'}
        return super().get(key,*args)


class FakeSession:
    def __init__(self):self.commands=[];self.callbacks={};self.detached=False;self.body_entered=asyncio.Event();self.release_body=asyncio.Event()
    async def send(self,name,params=None):
        self.commands.append((name,params))
        if name=='Page.getFrameTree':return {'frameTree':{'frame':{'id':'frame','loaderId':'loader','url':m.SOURCE_URL}}}
        if name=='Network.getResponseBody':
            self.body_entered.set();await self.release_body.wait()
            return {'body':body([{'quantity':50,'markupPrice':'10'}]).decode(),'base64Encoded':False}
        return {}
    def on(self,event,callback):self.callbacks[event]=callback
    def remove_listener(self,event,callback):self.callbacks.pop(event,None)
    async def detach(self):self.detached=True


class FakePage:
    url=m.SOURCE_URL
    def __init__(self,session):self.session=session;self.context=self;self.callbacks={}
    async def new_cdp_session(self,page):assert page is self;return self.session
    def on(self,event,callback):self.callbacks[event]=callback
    def remove_listener(self,event,callback):self.callbacks.pop(event,None)


@pytest.mark.asyncio
async def test_adapter_never_accesses_headers_POST_bodies_or_forbidden_commands_and_cancels_cleanly():
    session=FakeSession();page=FakePage(session);j=m.PriceJournal('1'*32)
    observer=m.OwnedPageCDPObserver(page,j);await observer.attach(m.CONTRACT);ep=begin(j)
    observer._request(GuardedDict(requestId='price',frameId='frame',loaderId='loader',type='XHR',
                    request=GuardedDict(url=PRICE,method='GET',headers='PRIVATE-HEADER',postData='PRIVATE-BODY')))
    observer._response(GuardedDict(requestId='price',response=GuardedDict(url=PRICE,status=200,mimeType='application/json',
                       headers='PRIVATE-HEADER',timing={'requestTime':1,'receiveHeadersEnd':2})))
    observer._finished({'requestId':'price','encodedDataLength':500})
    await asyncio.wait_for(session.body_entered.wait(),1)
    await observer.close()
    assert session.detached and not observer._tasks and not page.callbacks and not session.callbacks
    assert j._requests['price'].reason=='cancelled'
    assert j.finalize_epoch(ep,capture(),1)['status']=='held'
    assert {name for name,_ in session.commands}=={'Page.getFrameTree','Page.enable','Network.enable','Network.getResponseBody'}
    assert 'PRIVATE' not in json.dumps(j.snapshot())


@pytest.mark.asyncio
async def test_adapter_never_reads_redirected_private_body_or_body_after_navigation():
    session=FakeSession();page=FakePage(session);j=m.PriceJournal('2'*32)
    observer=m.OwnedPageCDPObserver(page,j);await observer.attach(m.CONTRACT);begin(j)
    try:
        observer._request({'requestId':'redirect','frameId':'frame','loaderId':'loader','type':'XHR','request':{'url':PRICE,'method':'GET'}})
        observer._response({'requestId':'redirect','response':{'url':'https://www.printograph.com/service/rest/v1/users/self',
            'status':200,'mimeType':'application/json','timing':{'requestTime':1,'receiveHeadersEnd':2}}})
        observer._finished({'requestId':'redirect','encodedDataLength':500})
        observer._request({'requestId':'old','frameId':'frame','loaderId':'loader','type':'XHR','request':{'url':PRICE,'method':'GET'}})
        observer._response({'requestId':'old','response':{'url':PRICE,'status':200,'mimeType':'application/json',
                             'timing':{'requestTime':1,'receiveHeadersEnd':2}}})
        observer._navigated({'frame':{'id':'frame','loaderId':'new-loader','url':m.SOURCE_URL}})
        observer._finished({'requestId':'old','encodedDataLength':500})
        await observer.drain()
        assert not any(name=='Network.getResponseBody' for name,_ in session.commands)
    finally:await observer.close()
