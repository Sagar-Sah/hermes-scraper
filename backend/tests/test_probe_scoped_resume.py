import asyncio
import pytest

from crawler.config import Settings
from crawler.engine import Engine
from crawler.probe_jobs import ProbeRunner
from crawler.store import Store, Run, Product, ProbeJob


@pytest.mark.asyncio
async def test_canary_resume_keeps_walkers_paused_and_finishes_only_live_probe(tmp_path,monkeypatch):
    store = Store(Settings(data_dir=tmp_path,s3_bucket=''))
    with store.session() as s:
        s.add(Run(id='run',name='Test',status='paused'))
        s.add(Product(id='p',run_id='run',url='https://www.printograph.com/products/business-cards/order',status='paused',epoch=7,row_count=21))
    engine = Engine(store); runner = ProbeRunner(store,None); engine.probes = runner
    gate = asyncio.Event()
    async def hold(*args): await gate.wait()
    monkeypatch.setattr(runner,'run',hold)
    manifest = {'product_url':'https://www.printograph.com/products/business-cards/order',
                'requests':[{'id':'spec','method':'GET','path':'/service/rest/v1/settings/product/specifications',
                             'params':{'productType':'1','requestValues':'true'}}]}
    try:
        key = runner.start('run',manifest,job_id='probe',budget=10,resume_run=True)
        engine.finish_run('run')
        with store.session() as s:
            assert s.get(Run,'run').status == 'running'
            product = s.get(Product,'p')
            assert (product.status,product.epoch,product.row_count) == ('paused',7,21)
            assert s.get(ProbeJob,key).used == 0
        gate.set(); await runner.tasks[key]
        engine.finish_run('run')
        with store.session() as s: assert s.get(Run,'run').status == 'paused'
        # A stale persisted running record must not keep the run alive.
        with store.session() as s: s.get(Run,'run').status = 'running'
        runner.tasks.clear(); engine.finish_run('run')
        with store.session() as s: assert s.get(Run,'run').status == 'paused'
    finally:
        gate.set(); await runner.close(); await engine.client.aclose(); store.engine.dispose()
