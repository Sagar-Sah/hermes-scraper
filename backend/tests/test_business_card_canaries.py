"""Targeted canary UI fixtures; no supplier requests or cart mutations."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from crawler import browser_dependency_probe as probe
from crawler import price_contract
from crawler.config import Settings
from crawler.probe_jobs import ProbeBudget, ProbeRunner
from crawler.store import Store, Run, ProbeJob
from crawler.transport import Transport
from test_price_contract import captured

URL=probe.ORIGIN+'/products/business-cards/order'
INIT={'size':'101','paper':'1','color':'2','quantity':'50','turnaround':'1','proof_type':'31','file_delivery':'5'}
CANARIES=[{'name':'quantity_100','key':'quantity','value':'100'}, {'name':'corners_on','key':'roundedCorners','value':'2'}]


@pytest.mark.parametrize('changes',[
    [], [{'name':'cart','key':'shoppingCartItemId','value':'1'}],
    [{'name':'proof','key':'proofType','value':'100'}],
    [{'name':'q','key':'quantity','value':True}],
    [{'name':'radius','key':'cornerRadius','value':'1'}],
    [{'name':'back','key':'raisedPrint','value':'136'}],
    [{'name':'baseline','key':'quantity','value':'100'}],
    [CANARIES[0],CANARIES[0]],
    [{'name':'q','key':'quantity','value':'100','action':'click'}],
])
def test_canary_manifest_rejects_unsupported_mutations(changes):
    with pytest.raises(ValueError):probe.validated_canary_selections(URL,INIT,[],changes)


def test_canaries_require_explicit_initializer_and_no_service_disclosure():
    assert probe.validated_canary_selections(URL,INIT,[],CANARIES)==CANARIES
    for initialization,phases in [(None,[]),(INIT,None),(INIT,['rush'])]:
        with pytest.raises(ValueError):probe.validated_canary_selections(URL,initialization,phases,CANARIES)


@pytest.mark.asyncio
async def test_probe_runner_validates_and_preserves_canary_contract(tmp_path,monkeypatch):
    store=Store(Settings(data_dir=tmp_path))
    with store.session() as s:s.add(Run(id='run',name='local',status='running'))
    runner=ProbeRunner(store,None)
    async def no_network(*args):pass
    monkeypatch.setattr(runner,'run',no_network)
    manifest={'product_url':URL,'requests':[],'browser_probes':[{'id':'canary','product_url':URL,'initialization':INIT,'phases':[],'canary_selections':CANARIES}]}
    ident=runner.start('run',manifest,budget=250)
    await asyncio.gather(*runner.tasks.values())
    assert runner.status(ident)['manifest']==manifest
    assert runner.status(ident)['budget']==250
    invalid=json.loads(json.dumps(manifest));invalid['browser_probes'][0]['canary_selections'][0]['key']='proofType'
    with pytest.raises(ValueError):runner.start('run',invalid,budget=250)
    await runner.close()


def html(mode):
    state=captured();selected=state['userSelectedOptions'];selected.update(size='101',paper='1',color='2',coating='5')
    value='100' if mode!='conflicting_store' else '101'
    metadata='' if mode=='missing_store' else 'data-currentstore="100"'
    controller_store='' if mode=='missing_store' else 'store:'+value+','
    return '''<!doctype html><html><head><title>Offline canary fixture</title>
<script src="https://static.printograph.com/orderForm.js"></script><script src="https://static.printograph.com/chunk-common.js"></script></head><body>
<div id="order-form" data-printproducttype="1" data-currentstoretype="broker" __META__>
<label for="quantity">Quantity</label><select id="quantity"><option value="50">50</option><option value="100">100</option></select>
<label for="roundedCorners">Round Corners</label><select id="roundedCorners"><option value="">None</option><option value="2">Round Corners</option></select>
<button disabled aria-controls="collapseOne">Process Type +</button><div id="collapseOne" style="display:none"></div>
<input type="hidden" name="password" value="fixture-secret-unrelated">
</div><div class="totalSection"><span class="subTotal">Subtotal</span><span class="col-4" id="price"></span></div>
<script>
const selected=__SELECTED__;
selected.shoppingCartItemId='';selected.priceGroupId='';
const data={userSelectedOptions:selected,appliedPrices:{productPrice:'5.60',roundedCorners:{'2':'3.00'}},others:{isAdmin:false,waiveFees:false,itemId:'null',editOrderItem:{}}};
const controller={__STORE__jsonObj:data,printProductType:1,fields:{'1':{options:__OPTIONS__}},couponData:{coupon:{},isCouponSpecChanged:false},get totalPrice(){return String(Number(data.appliedPrices.productPrice)+(selected.roundedCorners==='2'?3:0))}};
document.querySelector('#order-form')._vnode={component:{proxy:{$refs:{childComponents:{_:{subTree:{component:{refs:{orderFormInstance:controller}}}}}}}}};
function update(){data.appliedPrices.productPrice=selected.quantity==='100'?'6.60':'5.60';document.querySelector('#price').textContent='$'+Number(controller.totalPrice).toFixed(2);__MISMATCH__;fetch('/service/rest/v1/products/100000021/prices?quantities='+selected.quantity);}
document.addEventListener('change',e=>{selected[e.target.id]=e.target.value;__RESET__;update();});
fetch('/service/rest/v1/users/self/cart',{method:'POST',body:'{}'}).catch(()=>{});update();
</script></body></html>'''.replace('__SELECTED__',json.dumps(selected)).replace('__OPTIONS__',json.dumps(state['field_options'])).replace('__META__',metadata).replace('__STORE__',controller_store).replace('__RESET__',"if(e.target.id==='quantity')selected.proofType='100';" if mode=='dependent_reset' else '').replace('__MISMATCH__',"if(selected.quantity==='100')document.querySelector('#price').textContent='$7.60';" if mode=='price_mismatch' else '')


@pytest.mark.asyncio
@pytest.mark.parametrize('mode',['normal','dependent_reset','price_mismatch','source_unverified','missing_store','conflicting_store'])
async def test_real_chromium_targeted_canaries_stop_on_confounds_and_unresolved(tmp_path,monkeypatch,mode):
    playwright=pytest.importorskip('playwright.async_api')
    asset=b'/* source-pinned local canary fixture */'
    original=dict(price_contract.SOURCE_CONTRACT)
    fixture_contract={key:hashlib.sha256(asset).hexdigest() for key in original}
    monkeypatch.setattr(probe,'SOURCE_CONTRACT',fixture_contract)
    monkeypatch.setattr(price_contract,'SOURCE_CONTRACT',fixture_contract)
    monkeypatch.setattr(probe,'CONTROLLER_PRICE_STATE_JS',probe.CONTROLLER_PRICE_STATE_JS.replace(json.dumps(original),json.dumps(fixture_contract)))
    store=Store(Settings(data_dir=tmp_path,requests_per_second=100,source_concurrency=10))
    with store.session() as session:
        session.add(Run(id='run',name='fixture',status='running'))
        session.add(ProbeJob(id='probe',run_id='run',owner='test',status='running',budget=250,used=0,manifest={},results=[]))
    calls=[];rechecks=[]
    async def recheck(transport,budget,name,capture):
        rechecks.append(name)
        return {'phase':name,'kind':'explicit_base_recheck','evidence_id':'fixture-'+name,'semantic_hash':'fixture','cached':False}
    monkeypatch.setattr(probe,'recheck_base_price',recheck)
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():pytest.skip('Chromium missing')
        browser=await runtime.chromium.launch(headless=True);context=await browser.new_context()
        class Page:
            def __init__(self,page):self.page=page
            def __getattr__(self,key):return getattr(self.page,key)
            async def route(self,pattern,handler):
                async def intercepted(route):
                    async def continue_fixture():
                        req=route.request;calls.append((req.method,req.url))
                        if req.resource_type=='document':await route.fulfill(status=200,content_type='text/html',body=html(mode))
                        elif req.resource_type=='script':await route.fulfill(status=200,content_type='application/javascript',body=asset+b'/* changed */' if mode=='source_unverified' else asset)
                        else:await route.fulfill(status=200,content_type='application/json',body='{"items":[{"quantity":50,"markupPrice":"5.60"}]}')
                    await handler(SimpleNamespace(request=route.request,continue_=continue_fixture,abort=route.abort,fulfill=route.fulfill))
                await self.page.route(pattern,intercepted)
        class Pool:
            @asynccontextmanager
            async def page(self,*args,**kwargs):
                page=await context.new_page()
                try:yield Page(page)
                finally:await page.close()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:pytest.fail('No external HTTP'))) as http:
                report=await probe.probe_product(pool=Pool(),transport=Transport(store,'run','',client=http),budget=ProbeBudget(store,'probe','test'),
                    product_url=URL,initialization=INIT,phases=[],canary_selections=CANARIES,settle_timeout=5,stability_seconds=.21)
        finally:await context.close();await browser.close()
    assert not any('/cart' in url for _,url in calls)
    assert sum('/order?' in url for _,url in calls)==1
    assert 'fixture-secret-unrelated' not in json.dumps(report)
    phases=report['phases']
    if mode in {'normal','missing_store','conflicting_store'}:
        assert report['status']=='finished',report
        assert [p['name'] for p in phases]==['baseline','quantity_100','corners_on']
        assert [p['formula']['subtotal'] for p in phases]==['5.60','6.60','9.60']
        assert all(p['formula']['comparison']=='matched' for p in phases)
        assert set(phases[1]['changed_selections'])=={'quantity'}
        assert set(phases[2]['changed_selections'])=={'roundedCorners'}
        assert [a['key'] for a in report['actions']]==['quantity','roundedCorners']
        metadata=phases[0]['capture']['public_price_metadata']
        assert metadata['status']=={'normal':'observed','missing_store':'unresolved','conflicting_store':'conflicting'}[mode]
        assert phases[0]['capture']['coupon']['coupon_object_key_count']==0
        assert not any(phases[0]['capture']['other_context'].values())
        assert rechecks==['baseline','quantity_100','corners_on']
    else:
        assert report['status']=='unresolved'
        assert len(report['actions'])==(0 if mode=='source_unverified' else 1)
        assert report['reason']=={'source_unverified':'canary_baseline_unresolved','dependent_reset':'canary_dependent_selection_changed','price_mismatch':'canary_quote_unresolved'}[mode]
        assert len(phases)==(1 if mode=='source_unverified' else 2)
        if mode=='dependent_reset':assert phases[-1]['status']=='confounded'
        if mode=='price_mismatch':assert phases[-1]['formula']['comparison']=='mismatch'
