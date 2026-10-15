"""A fresh native-session handoff retains no historical response scope."""
from copy import deepcopy
import json
import httpx
import pytest
from crawler.source_domain_session import SourceDomainSessions
from crawler.price_contract import SOURCE_CONTRACT
from crawler.store import Product, Run
from crawler.transport import ExtractionError
from test_source_domain_session import setup, URL
from test_price_contract import captured


def complete_capture():
    value=captured(product_type='36')
    value.update(capture_kind='controller',controller_total='5.60')
    return value


@pytest.mark.asyncio
async def test_matched_probe_retains_new_native_scope_without_supplier_calls(setup):
    store,context,pool=setup
    value=complete_capture();context.pages[0].state=deepcopy(value)
    calls=[]
    clients=[]
    def client_factory():
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda request:calls.append(request)))
        clients.append(client);return client
    manager=SourceDomainSessions(store,pool,client_factory=client_factory)
    first=await manager.retain_probe_context('run',URL,context.pages[0],{'x-csrf-token':'ephemeral-fixture','cookie':'never-copy'},value,SOURCE_CONTRACT)
    second=await manager.retain_probe_context('run',URL,context.pages[0],{'x-csrf-token':'ephemeral-fixture'},value,SOURCE_CONTRACT)
    assert first['session_scope']!=second['session_scope']
    assert first['source_epoch']!=second['source_epoch']
    assert first['context']['bootstrap']['method']=='matched_initialized_probe'
    assert first['source_requests_issued']==0 and calls==[]
    assert 'ephemeral' not in json.dumps(first) and 'never-copy' not in json.dumps(first)
    assert not context.cookie_reads
    await context.pages[0].close()
    assert manager.status(first['session_scope'])['status']=='ready'
    await manager.close()
    assert all(client.is_closed for client in clients)


@pytest.mark.asyncio
@pytest.mark.parametrize('failure',['source_changed','subtotal_mismatch','controller_changed','missing_headers','wrong_page','unowned','paused'])
async def test_probe_handoff_rejects_unbound_or_changed_context(setup,failure):
    store,context,pool=setup
    value=complete_capture();context.pages[0].state=deepcopy(value)
    contract=dict(SOURCE_CONTRACT);headers={'x-csrf-token':'fixture'}
    if failure=='source_changed':contract['common_sha256']='different'
    if failure=='subtotal_mismatch':value['displayed_total']='8.60'
    if failure=='controller_changed':context.pages[0].state['userSelectedOptions']['fileDelivery']='22'
    if failure=='missing_headers':headers={'x-requested-with':'XMLHttpRequest'}
    if failure=='wrong_page':context.pages[0].url='https://www.printograph.com/user/authenticate'
    if failure=='unowned':pool._contexts={}
    if failure=='paused':
        with store.session() as db:db.get(Run,'run').status='paused'
    manager=SourceDomainSessions(store,pool)
    with pytest.raises(ExtractionError):
        await manager.retain_probe_context('run',URL,context.pages[0],headers,value,contract)
    assert not manager.sessions and len(context.pages)==1
    await manager.close()


@pytest.mark.asyncio
async def test_real_chromium_probe_handoff_survives_source_tab_close(tmp_path,monkeypatch):
    from contextlib import asynccontextmanager
    import hashlib
    from pathlib import Path
    from types import SimpleNamespace
    from crawler import browser_dependency_probe as probe,price_contract,source_domain_session
    from crawler.config import Settings
    from crawler.probe_jobs import ProbeBudget
    from crawler.store import Store,ProbeJob
    from crawler.transport import Transport
    from test_business_card_canaries import html,INIT,URL as BC_URL
    playwright=pytest.importorskip('playwright.async_api')
    asset=b'/* local public contract fixture */'
    original=dict(price_contract.SOURCE_CONTRACT)
    contract={key:hashlib.sha256(asset).hexdigest() for key in original}
    for module in (probe,price_contract,source_domain_session):monkeypatch.setattr(module,'SOURCE_CONTRACT',contract)
    monkeypatch.setattr(probe,'CONTROLLER_PRICE_STATE_JS',probe.CONTROLLER_PRICE_STATE_JS.replace(json.dumps(original),json.dumps(contract)))
    store=Store(Settings(data_dir=tmp_path,requests_per_second=100,source_concurrency=10))
    with store.session() as db:
        db.add(Run(id='run',name='fixture',status='running'))
        db.add(Product(id='bc',run_id='run',url=BC_URL,status='paused'))
        db.add(ProbeJob(id='probe',run_id='run',owner='o',status='running',budget=100,used=0,manifest={},results=[]))
    async def recheck(*args):return {'cached':False,'evidence_id':'fixture','semantic_hash':'fixture'}
    monkeypatch.setattr(probe,'recheck_base_price',recheck)
    async with playwright.async_playwright() as runtime:
        if not Path(runtime.chromium.executable_path).exists():pytest.skip('Chromium missing')
        browser=await runtime.chromium.launch(headless=True)
        context=await browser.new_context(extra_http_headers={'x-csrf-token':'ephemeral-fixture'})
        class Page:
            def __init__(self,page):self.page=page
            def __getattr__(self,key):return getattr(self.page,key)
            async def route(self,pattern,handler):
                async def intercepted(route):
                    async def continue_fixture():
                        req=route.request
                        if req.resource_type=='document':await route.fulfill(status=200,content_type='text/html',body=html('normal'))
                        elif req.resource_type=='script':await route.fulfill(status=200,content_type='application/javascript',body=asset)
                        else:await route.fulfill(status=200,content_type='application/json',body='{"items":[{"quantity":50,"markupPrice":"5.60"}]}')
                    await handler(SimpleNamespace(request=route.request,continue_=continue_fixture,abort=route.abort,fulfill=route.fulfill))
                await self.page.route(pattern,intercepted)
        class Pool:
            _contexts={'local':context}
            @asynccontextmanager
            async def page(self,*args,**kwargs):
                page=await context.new_page()
                try:yield Page(page)
                finally:await page.close()
        pool=Pool();manager=SourceDomainSessions(store,pool)
        async def retain(page,headers,capture,loaded):
            return await manager.retain_probe_context('run',BC_URL,page,headers,capture,loaded)
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _:pytest.fail('No supplier HTTP'))) as http:
                report=await probe.probe_product(pool=pool,transport=Transport(store,'run','bc',client=http),budget=ProbeBudget(store,'probe','o'),
                    product_url=BC_URL,initialization=INIT,phases=[],on_baseline_ready=retain,settle_timeout=5,stability_seconds=.21)
            assert report['status']=='finished',report
            public=report['retained_domain_session']
            assert manager.status(public['session_scope'])['status']=='ready'
            assert len(context.pages)==1 and context.pages[0].url=='about:blank'
            assert 'ephemeral-fixture' not in json.dumps(report)
            assert report['phases'][0]['formula']['comparison']=='matched'
        finally:
            await manager.close();await context.close();await browser.close()
    store.engine.dispose()
