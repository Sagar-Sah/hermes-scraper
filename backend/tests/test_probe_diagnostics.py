"""Bootstrap errors must remain actionable without copying private messages."""
from contextlib import asynccontextmanager
import json
import pytest
from sqlalchemy import select
from crawler.config import Settings
from crawler.probe_jobs import ProbeRunner, probe_failure_diagnostic
from crawler.store import Store, Run, ProbeJob, Event

@pytest.mark.parametrize('text,expected,category', [
    ('Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://private.test/?token=SECRET', 'ERR_CERT_AUTHORITY_INVALID', 'tls_error'),
    ('Page.goto: net::ERR_ABORTED at https://private.test/?cookie=SECRET', 'ERR_ABORTED', 'navigation_interrupted'),
    ('Page.goto: net::ERR_CONNECTION_RESET', 'ERR_CONNECTION_RESET', 'network_error'),
    ('Target page, context or browser has been closed SECRET', None, 'target_closed'),
    ('net::ERR_SECRET_CREDENTIAL SECRET', None, 'browser_error'),
])
def test_diagnostic_is_allowlisted(text, expected, category):
    value=probe_failure_diagnostic(RuntimeError(text),'bootstrap_navigation')
    assert value['network_error']==expected
    assert value['category']==category
    assert 'SECRET' not in json.dumps(value)
    assert 'private.test' not in json.dumps(value)
    assert set(value)=={'stage','exception_type','category','network_error'}


def test_request_failure_fallback_and_untrusted_stage_class_are_bounded():
    Sensitive=type('SECRETException',(Exception,),{})
    value=probe_failure_diagnostic(Sensitive('private data'),'SECRETstage','net::ERR_CERT_DATE_INVALID SECRET')
    assert value=={'stage':'unknown','exception_type':'Exception','category':'tls_error','network_error':'ERR_CERT_DATE_INVALID'}


@pytest.mark.asyncio
async def test_runner_persists_bootstrap_failure_without_private_message(tmp_path,monkeypatch):
    store=Store(Settings(data_dir=tmp_path))
    url='https://www.printograph.com/products/business-cards/order'
    with store.session() as db:
        db.add(Run(id='r',name='probe',status='running'))
        db.add(ProbeJob(id='j',run_id='r',status='running',owner='o',used=0,budget=10,
                        manifest={'product_url':url,'requests':[],'browser_probes':[]},results=[]))
    class Page:
        url='about:blank'
        def __init__(self):self.context=self;self.pages=[self];self.listeners={}
        def on(self,event,callback):self.listeners[event]=callback
        def remove_listener(self,event,callback):self.listeners.pop(event,None)
        async def route(self,*args):pass
        async def goto(self,*args,**kwargs):
            raise RuntimeError('Page.goto: net::ERR_CERT_AUTHORITY_INVALID at https://private.test/?token=SECRET')
    class Pool:
        @asynccontextmanager
        async def page(self,*args,**kwargs):yield Page()
    async def safe_validate(*args):pass
    monkeypatch.setattr('crawler.probe_jobs.validate_url',safe_validate)
    runner=ProbeRunner(store,Pool())
    await runner.run('j','o')
    assert runner.status('j')['error']=='probe_internal_error:RuntimeError:bootstrap_navigation:ERR_CERT_AUTHORITY_INVALID'
    assert runner.status('j')['status']=='needs_attention'
    with store.session() as db:
        events=list(db.scalars(select(Event).where(Event.type=='probe_failure_diagnostic')))
        assert len(events)==1
        payload=events[0].payload
        assert payload['probe_id']=='j'
        assert payload['network_error']=='ERR_CERT_AUTHORITY_INVALID'
        assert 'SECRET' not in json.dumps(payload)
    store.engine.dispose()


@pytest.mark.parametrize('method,url,headers',[
    ('POST','https://www.printograph.com/service/rest/v1/settings',{'x-csrf-token':'private'}),
    ('GET','https://other.test/service/rest/v1/settings',{'x-csrf-token':'private'}),
    ('GET','https://www.printograph.com/service/rest/v1/users/self/cart',{'x-csrf-token':'private'}),
    ('GET','https://www.printograph.com/service/rest/v1/settings',{'x-requested-with':'XMLHttpRequest'}),
    ('GET','https://www.printograph.com/service/rest/v1/settings',{'x-csrf-token':''}),
])
def test_transport_witness_does_not_invent_auth_or_accept_unrelated_requests(method,url,headers):
    from crawler.probe_jobs import bootstrap_transport_witness
    assert bootstrap_transport_witness(method,url,headers) is None


@pytest.mark.asyncio
async def test_startup_only_bootstrap_reaches_probe_without_reading_account_response(tmp_path,monkeypatch):
    store=Store(Settings(data_dir=tmp_path))
    url='https://www.printograph.com/products/business-cards/order'
    with store.session() as db:
        db.add(Run(id='r',name='probe',status='running'))
        db.add(ProbeJob(id='j',run_id='r',status='running',owner='o',used=0,budget=10,
            manifest={'product_url':url,'requests':[],'browser_probes':[{'id':'b','product_url':url,'phases':[]}]},results=[]))
    actions=[]
    class Request:
        method='GET';resource_type='fetch'
        url='https://www.printograph.com/service/rest/v1/settings'
        async def all_headers(self):return {'x-csrf-token':'private-in-memory','cookie':'never-copied'}
    class Route:
        request=Request()
        async def abort(self):actions.append('startup-aborted')
        async def continue_(self):raise AssertionError('Bootstrap must not read startup response')
    class Page:
        url='about:blank'
        def __init__(self):self.context=self;self.pages=[self];self.listeners={}
        def on(self,event,callback):self.listeners[event]=callback
        def remove_listener(self,event,callback):self.listeners.pop(event,None)
        async def route(self,pattern,handler):self.handler=handler
        async def unroute(self,*args):pass
        async def goto(self,*args,**kwargs):
            # No later product request is emitted: settings was aborted first.
            await self.handler(Route())
    class Pool:
        @asynccontextmanager
        async def page(self,*args,**kwargs):yield Page()
    async def safe_validate(*args):pass
    async def controlled_probe(**kwargs):
        actions.append('controlled-probe')
        return {'status':'finished','phases':[]}
    monkeypatch.setattr('crawler.probe_jobs.validate_url',safe_validate)
    monkeypatch.setattr('crawler.browser_dependency_probe.probe_product',controlled_probe)
    runner=ProbeRunner(store,Pool())
    await runner.run('j','o')
    assert runner.status('j')['status']=='completed'
    assert actions==['startup-aborted','controlled-probe']
    with store.session() as db:
        payloads=[event.payload for event in db.scalars(select(Event))]
        witness=next(x for x in payloads if x.get('path')=='/service/rest/v1/settings')
        assert witness['header_names']==['x-csrf-token']
        assert witness['authentication_validated'] is False
        assert 'private-in-memory' not in json.dumps(payloads)
        assert 'never-copied' not in json.dumps(payloads)
    store.engine.dispose()
