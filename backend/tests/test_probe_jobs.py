import asyncio
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
import pytest
import httpx
from crawler.config import Settings
from crawler.store import Store, Run, ProbeJob, ProbeAttempt
from crawler.probe_jobs import ProbeBudget, BrowserHTTPClient, ProbeRunner, validate_probe, public_url, completed_probe_ids
from crawler.transport import ExtractionError
from sqlalchemy import select, func

@pytest.fixture
def store(tmp_path):
    s=Store(Settings(data_dir=tmp_path))
    with s.session() as db:
        db.add(Run(id='r',name='probe',status='running'))
        db.add(ProbeJob(id='j',run_id='r',owner='o',budget=9,used=0,status='running',results=[],manifest={}))
    yield s
    s.engine.dispose()

def test_durable_budget_race_cannot_overshoot_or_reset(store):
    def reserve(_):
        try: return ProbeBudget(store,'j','o').reserve('probe','GET','https://www.printograph.com/x')
        except ExtractionError as exc:
            assert exc.code=='probe_budget_exhausted'
            return None
    with ThreadPoolExecutor(max_workers=8) as pool:
        attempts=list(pool.map(reserve,range(30)))
    assert len([x for x in attempts if x])==9
    with store.session() as db:
        assert db.get(ProbeJob,'j').used==9
        assert db.scalar(select(func.count()).select_from(ProbeAttempt))==9
        assert set(db.scalars(select(ProbeAttempt.sequence)))==set(range(1,10))
    with pytest.raises(ExtractionError,match='exhausted'):
        ProbeBudget(store,'j','o').reserve('bootstrap','GET','https://www.printograph.com/x')

@pytest.mark.parametrize('method,path',[('POST','/service/rest/v1/users/self/cart'),('DELETE','/service/rest/v1/users/self/cart'),('GET','https://other.test/a'),('POST','/service/rest/v1/users/self/orders'),('GET','/service/rest/v1/products/../../users/self')])
def test_only_readonly_quote_contracts_allowed(method,path):
    with pytest.raises(ValueError): validate_probe({'method':method,'path':path})

def test_private_query_and_context_rejected():
    for part in ({'params':{'cid':'person'}},{'params':{'itemId':'123'}},{'context':{'nested':{'API-Key':'secret'}}}):
        with pytest.raises(ValueError): validate_probe({'path':'/service/rest/v1/products',**part})
    assert 'secret' not in public_url('https://www.printograph.com/x?token=secret')


def test_resume_preserves_http_outcomes_but_retries_repaired_transport():
    results = [
        {'id': 'ok', 'status': 'observed'},
        {'id': 'unsupported', 'status': 'failed', 'code': 'http_error'},
        {'id': 'tls', 'status': 'failed', 'code': 'network_error'},
        {'id': 'auth', 'status': 'failed', 'code': 'access_required'},
        {'id': 'stopped', 'status': 'failed', 'code': 'probe_interrupted'},
        {'id': 'recovered', 'status': 'failed', 'code': 'network_error'},
        {'id': 'recovered', 'status': 'observed'},
    ]
    assert completed_probe_ids(results) == {'ok', 'unsupported', 'recovered'}

@pytest.mark.asyncio
async def test_budget_fence_and_run_pause_stop_attempts(store):
    budget=ProbeBudget(store,'j','o')
    with store.session() as db: db.get(Run,'r').status='paused'
    with pytest.raises(ExtractionError): await budget.check()
    with pytest.raises(ExtractionError): budget.reserve('probe','GET','https://www.printograph.com/x')
    with store.session() as db: assert db.get(ProbeJob,'j').used==0

@pytest.mark.asyncio
async def test_cookie_sharing_client_scopes_headers_and_never_follows_redirect(store):
    calls=[]
    class Context:
        async def cookies(self, urls):
            assert urls[0].startswith('https://www.printograph.com/')
            return [{'name':'login','value':'ephemeral-only'}]
        async def add_cookies(self, values):
            assert values[0]['domain']=='www.printograph.com'
    async def respond(request):
        calls.append(request)
        return httpx.Response(200,json={'items':[]},headers={'set-cookie':'rotation=memory; Path=/; Secure; HttpOnly'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        client=BrowserHTTPClient(Context(),{'x-csrf-token':'memory-only'},ProbeBudget(store,'j','o'),client=http)
        async with client.stream('GET','https://www.printograph.com/service/rest/v1/products',params={'productType':1},headers={'Authorization':'profile-secret','User-Agent':'bad'}) as response:
            assert response.status_code==200
        assert calls[0].headers['x-csrf-token']=='memory-only'
        assert calls[0].headers['cookie']=='login=ephemeral-only'
        assert 'authorization' not in calls[0].headers
        with pytest.raises(ExtractionError):
            async with client.stream('GET','https://evil.test/service/rest/v1/products'): pass
        with pytest.raises(ValueError):
            async with client.stream('GET','https://www.printograph.com/service/rest/v1/products?api_key=private'): pass
        await client.aclose()
        assert not client._headers
        assert not list(http.cookies.jar)
    with store.session() as db:
        assert db.get(ProbeJob,'j').used==1
        assert 'memory-only' not in str([(x.public_url,x.status) for x in db.scalars(select(ProbeAttempt))])

@pytest.mark.parametrize('path',['/service/rest/v1/product/prices','/service/rest/v1/rules/turnarounds'])
def test_malformed_post_body_rejected_cleanly(path):
    with pytest.raises(ValueError): validate_probe({'method':'POST','path':path,'json':None})

@pytest.mark.asyncio
async def test_append_preserves_budget_and_previous_manifest(store,monkeypatch):
    runner=ProbeRunner(store,None)
    async def noop(*args): pass
    monkeypatch.setattr(runner,'run',noop)
    m={'product_url':'https://www.printograph.com/products/business-cards/order','requests':[{'id':'a','path':'/service/rest/v1/products','params':{'productType':1}}]}
    runner.start('r',m,job_id='new',budget=20)
    await asyncio.gather(*runner.tasks.values())
    with store.session() as db:
        j=db.get(ProbeJob,'new');j.status='completed';j.used=7
    runner.start('r',{**m,'requests':m['requests']+[{'id':'b','path':'/service/rest/v1/products','params':{'productType':27}}]},job_id='new',budget=20)
    await asyncio.gather(*runner.tasks.values())
    assert runner.status('new')['used']==7
    with pytest.raises(ValueError): runner.start('r',m,job_id='new',budget=21)
    await runner.close()

@pytest.mark.asyncio
async def test_duplicate_probe_ids_rejected(store):
    runner=ProbeRunner(store,None)
    request={'id':'same','path':'/service/rest/v1/products','params':{'productType':1}}
    with pytest.raises(ValueError,match='unique'):
        runner.start('r',{'product_url':'https://www.printograph.com/products/business-cards/order','requests':[request,request]})


@pytest.mark.asyncio
async def test_browser_initialization_manifest_validates_public_query_before_start(store, monkeypatch):
    runner = ProbeRunner(store, None)
    async def noop(*args):
        pass
    monkeypatch.setattr(runner, 'run', noop)
    url = 'https://www.printograph.com/products/roll-labels/order'
    probe = {'id': 'configured-roll', 'product_url': url, 'phases': [],
             'initialization': {'shape': '5', 'size': '742', 'paper': '13', 'finish': '1',
                                'color': '1', 'quantity': '100', 'turnaround': '1',
                                'windPosition': '32', 'proof_type': '31', 'file_delivery': '5'}}
    manifest = {'product_url': url, 'requests': [], 'browser_probes': [probe]}
    ident = runner.start('r', manifest, budget=20)
    await asyncio.gather(*runner.tasks.values())
    assert runner.status(ident)['manifest']['browser_probes'][0]['initialization'] == probe['initialization']
    for extra in ({'cartId': '123'}, {'proof_type': '100'}, {'orientation': '2'}):
        invalid = {**probe, 'initialization': {**probe['initialization'], **extra}}
        with pytest.raises(ValueError):
            runner.start('r', {**manifest, 'browser_probes': [invalid]}, budget=20)
    await runner.close()
