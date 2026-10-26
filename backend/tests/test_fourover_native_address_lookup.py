"""Exact current source readonly lookup; no private data or supplier requests."""
import asyncio,json,time
from pathlib import Path
from urllib.parse import urlencode
import pytest
from test_fourover_automatic_profile import load_driver as load,PRIVATE,session_fixture
from test_fourover_customer_module_dependency import native_ready_fixture,Request,Route,Response
from test_fourover_native_metadata import Request as NativeRequest
FIXTURES=Path(__file__).resolve().parent/'fixtures/fourover_address_lookup'


@pytest.fixture
def mod():return load()

@pytest.mark.parametrize('ids,allowed',[([],True),([12,'13','13'],True),([None],False),([False],False),([True],False),([0],False),([-1],False),([1.5],False),([{}],False),([[]],False),([''],False),(['A@B'],False),(['x'*129],False),(['abc-123'],True),([1]*65,False)])
def test_native_address_lookup_closed_supported_scalar_subset(mod,ids,allowed):
    assert bool(mod.native_address_lookup_body(json.dumps({'addressIds':ids}))) is allowed

@pytest.mark.parametrize('raw',['{"addressIds":[],"save":true}','{"addressIds":[],"addressIds":[]}','{"addressIds":null}','{"addressIds":{}}','{"addressIds":[NaN]}','{"addressIds":[Infinity]}','{"addressIds":[],"firstname":"PRIVATE"}','{}','[]','null'])
def test_address_body_cannot_be_address_mutation_or_ambiguous_schema(mod,raw):
    assert not mod.native_address_lookup_body(raw)

class LookupRequest(NativeRequest):
    def __init__(self,page,url,method='GET',kind='xhr',body=''):
        super().__init__(page,url,method=method,resource=kind);self.body=body
    @property
    def reads(self):return self.private_reads
    async def header_value(self,key):self.private_reads+=1;return 'application/json'
    @property
    def post_data(self):self.private_reads+=1;return self.body

async def proved(mod,tmp_path):
    s,page,observer=await native_ready_fixture(mod,tmp_path);s.address_ready=False
    for suffix,expected in mod.ADDRESS_PUBLIC_MODULES.items():
        filename='load-addresses.js' if 'action/' in suffix else 'address-manager.js'
        req=Request(page,s.prefix+suffix,kind='script');response=Response(req,(FIXTURES/filename).read_bytes(),'application/javascript')
        await s.capture_public_script(response)
    assert len(s.address_modules)==2
    return s,page,observer

async def private_fetch(observer,page,route):
    response=Response(route.request,('{"PRIVATE":"'+PRIVATE+'"}').encode(),'application/json')
    async def trap():raise AssertionError('Private address response read')
    response.body=trap;page.responses.append(response);return response


def test_proven_native_lookup_original_private_response_shared_once_not_persisted(mod,tmp_path):
    async def run():
        s,page,observer=await proved(mod,tmp_path);calls=[]
        async def fetch(route):calls.append(route.request.body);return await private_fetch(observer,page,route)
        observer._fetch=fetch
        body=json.dumps({'addressIds':['PRIVATE_IDENTIFIER_7']})
        a=Route(page,LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body=body));await s.guard(a)
        b=Route(page,LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body=body));await s.guard(b)
        assert a.forwarded and b.forwarded and s.address_ready and s.address_reads==1 and len(calls)==1
        other=Route(page,LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body='{"addressIds":["OTHER_PRIVATE_ID"]}'));await s.guard(other)
        assert other.aborted and not other.forwarded and s.address_reads==1
        assert not s.cursor['receipts'] and not observer.permits
        assert 'PRIVATE_IDENTIFIER_7' not in json.dumps(s.cursor) and 'OTHER_PRIVATE_ID' not in json.dumps(s.cursor)
        assert PRIVATE not in b''.join(s.store.objects.values()).decode()
        await page.responses[0].dispose()
    asyncio.run(run())

@pytest.mark.parametrize('change',['unproved','body','mime','frame','source','query','method'])
def test_unknown_or_mutating_address_request_never_forwards(mod,tmp_path,change):
    async def run():
        s,page,observer=await proved(mod,tmp_path)
        if change=='unproved':s.address_modules.clear()
        url=mod.ADDRESS_LOOKUP_URL+('?action=save' if change=='query' else '')
        req=LookupRequest(page,url,method='GET' if change=='method' else 'POST',body='{"addressIds":[]}' if change!='body' else '{"addressIds":[],"street":"PRIVATE"}')
        if change=='frame':req.frame=object()
        if change=='source':page.url='https://4over.com/other'
        if change=='mime':
            async def wrong(key):req.private_reads+=1;return 'application/x-www-form-urlencoded'
            req.header_value=wrong
        route=Route(page,req);await s.guard(route)
        assert route.aborted and not route.forwarded and not page.responses and not s.address_reads
        if change in {'unproved','frame','source','query','method'}:assert req.reads==0
        assert not s.cursor['receipts'] and PRIVATE not in json.dumps(s.cursor)
    asyncio.run(run())


def test_address_handler_and_module_proof_are_not_canceled_as_quote_work(mod,tmp_path):
    async def run():
        s,page,observer=await proved(mod,tmp_path);entered=asyncio.Event();release=asyncio.Event()
        async def fetch(route):entered.set();await release.wait();return await private_fetch(observer,page,route)
        observer._fetch=fetch
        route=Route(page,LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body='{"addressIds":[]}'))
        task=asyncio.create_task(s.guard(route));await entered.wait();await s.drain()
        assert not task.done() and not s.address_ready and s.cursor['inflight'] is None
        release.set();await task;assert route.forwarded and s.address_ready
        await page.responses[0].dispose()
    asyncio.run(run())

@pytest.mark.parametrize('suffix,valid',[
 ('ThirdParty_Module/templates/modal/confirm.html',True),('Magento_Ui/template/modal/modal-popup.html?_=123',True),
 ('FourOver_Project/template/project.html?email=PRIVATE',True),('ThirdParty_Module/templates/private.html',False),
 ('ThirdParty_Module/templates/user_abcdef0123456789.html',False),('ThirdParty_Module/templates/../secret.html',False),
 ('ThirdParty_Module/api/private.json',False),('OtherNamespace/templates/public.html',False)])
def test_wider_public_template_diagnostic_never_admits_unknown_namespace(mod,suffix,valid):
    prefix='https://4over.com/static/version42/frontend/FourOver/default/en_US/'
    value=mod.public_template_diagnostic(prefix+suffix,prefix)
    assert (value is not None) is valid
    if value:
        assert 'PRIVATE' not in json.dumps(value) and '?' not in value['public_template_suffix'] and value['admission_changed'] is False
    if suffix.startswith('ThirdParty_'):assert mod.generic_public_template_suffix(prefix+suffix,prefix) is None


def test_address_readiness_failure_holds_before_semantic_reservation(mod,tmp_path):
    async def run():
        session,store,page=session_fixture(mod,tmp_path)
        session.address_ready=False
        with pytest.raises(mod.Held):await session.collect()
        assert session.cursor['inflight'] is None and session.cursor['processed_count']==session.cursor['verified_count']==0
        assert session.counts['summary']==0 and page.quantity_clicks==0
        assert not list((store.root/'fourover-native-observation-selections').rglob('*.json'))
    asyncio.run(run())


def test_already_arrived_address_proof_task_drains_before_any_private_read(mod,tmp_path):
    async def run():
        s,page,observer=await proved(mod,tmp_path);s.address_modules.clear();s.address_ready=False
        entered=asyncio.Event();release=asyncio.Event()
        async def proof():entered.set();await release.wait();s.address_modules.update(mod.ADDRESS_PUBLIC_MODULES)
        pending=asyncio.create_task(proof());s.address_module_tasks.add(pending);pending.add_done_callback(s.address_module_tasks.discard);await entered.wait()
        async def fetch(route):return await private_fetch(observer,page,route)
        observer._fetch=fetch
        req=LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body='{"addressIds":[]}');route=Route(page,req)
        task=asyncio.create_task(s.guard(route));await asyncio.sleep(.02);await s.drain()
        assert not task.done() and req.reads==0 and not s.address_ready and s.cursor['inflight'] is None
        release.set();await pending;await task;assert route.forwarded and s.address_reads==1
        await page.responses[0].dispose()
    asyncio.run(run())

@pytest.mark.parametrize('change',['source','deadline'])
def test_last_limiter_await_cannot_dispatch_address_read_after_scope_or_budget_drift(mod,tmp_path,change):
    async def run():
        s,page,observer=await proved(mod,tmp_path)
        original=observer._acquire
        async def acquire(*args):
            token=await original(*args)
            if change=='source':page.url='https://4over.com/other'
            else:s.deadline=time.monotonic()
            return token
        observer._acquire=acquire
        route=Route(page,LookupRequest(page,mod.ADDRESS_LOOKUP_URL,method='POST',body='{"addressIds":[]}'));await s.guard(route)
        assert route.aborted and not route.forwarded and s.address_reads==0 and not page.responses
        assert not observer.permits and not s.cursor['receipts']
    asyncio.run(run())


def test_visible_exact_source_b2c_profile_gate_is_access_required_without_field_reads(mod,tmp_path):
    async def run():
        s,page,_=await proved(mod,tmp_path)
        original=page.evaluate
        async def evaluate(code,arg=None):
            if code==mod.ACCOUNT_PROFILE_GATE_JS:return True
            return await original(code,arg)
        page.evaluate=evaluate
        with pytest.raises(mod.Held,match='source_account_profile_action_required'):await s.state()
        result=s.result(False,'source_account_profile_action_required')
        assert result['status']=='access_required' and result['processed_count']==0 and s.account_profile_gate_observed
        assert PRIVATE not in json.dumps(result)
    asyncio.run(run())


@pytest.mark.parametrize('namespace',['Private_Module','Token_Module','Session_Module','Email_Person','Password_Module','Passwd_Module','Secret_Module','Credential_Module','Authorization_Module','Formkey_Module'])
def test_original_public_template_namespace_is_checked_before_normalization(mod,namespace):
    prefix='https://4over.com/static/version42/frontend/FourOver/default/en_US/'
    assert mod.public_template_diagnostic(prefix+namespace+'/templates/modal/confirm.html',prefix) is None
    positive=mod.public_template_diagnostic(prefix+'ThirdParty_Module/templates/modal/confirm.html',prefix)
    assert positive['public_template_suffix']=='ThirdParty_Module/templates/modal/confirm.html'
    assert positive['admission_changed'] is False


def test_all_nested_public_source_fixtures_are_pinned():
    import hashlib
    value=json.loads((FIXTURES/'manifest.json').read_bytes())
    assert value['schema']=='fourover-native-readonly-address-source-fixtures/v1'
    for name,digest in value['source_files'].items():assert hashlib.sha256((FIXTURES/name).read_bytes()).hexdigest()==digest
    assert value['private_customer_or_address_data'] is False
